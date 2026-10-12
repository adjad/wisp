#!/usr/bin/env python3
"""Validator for the Ling V3 model-led tool-calling dataset (standard library only; no network, no model).

    python3 validate.py [--dir DIR] [--check-repro] [--out validation.json]

Checks: JSON validity, per-step schema validity of every call against the schemas offered AT THAT STEP, protocol
order (no real tool before its schema is loaded, <=2 discovery calls, receipts match the host's expansion rules),
call-id pairing, no identical repeated call, final answer present, row-prefix consistency across steps, family split
integrity, held-out (eval) prompt overlap, category counts, explicit-authority cues for sends/destructive tools,
honest handling of denials and errors, synthetic-only PII, date sanity, tool-result length.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISCOVERY = "get_tool_schemas"
DECLINED = "The user declined; nothing was sent."
REJECT = ("Schema selection rejected: Requested tools must belong to the selected admitted families. "
          "No source was read or action performed.")
EXPECTED = {  # category -> (train, dev)
    "outbound_sends_drafts": (198, 22), "compound_read_then_act": (162, 18), "stop_after_success_no_loop": (126, 14),
    "single_tool_reads": (126, 14), "writes": (90, 10), "error_handling": (72, 8), "clarify_ambiguity": (54, 6),
    "chat_no_tool": (45, 5), "followups_history": (27, 3)}
OUTBOUND = {"send_message", "send_email", "reply_to_email", "forward_email", "schedule_send", "place_call"}
DESTRUCTIVE = {"cancel_event", "clear_reminders", "clear_past_reminders", "clear_memory", "forget", "delete_path", "trash_file",
               "delete_knowledge", "complete_reminder", "update_reminder", "quit_app", "force_quit_app", "archive_email", "flag_email", "mark_email_read"}
WRITE_TOOLS = OUTBOUND | DESTRUCTIVE | {"add_reminder", "add_calendar_event", "create_note", "append_note", "remember", "set_timer",
                                         "set_alarm", "set_volume", "toggle_setting", "set_appearance", "draft_email", "draft_message", "open_app", "music"}
READ_ONLY_CATEGORIES = {"stop_after_success_no_loop", "single_tool_reads", "chat_no_tool"}
CUES = {
    "send_message": r"text|message|msg|imessage|ping|dm\b|tell|know|shoot|send|reach|word|heads|say|email|mail|reply|answer|respond|write|contact|follow|touch|pass|drop|fire|hit",
    "send_email": r"email|e-mail|mail|send|write|reach|contact|follow|text|message|tell|know|touch|reply|answer|respond|shoot|drop|ping|use",
    "reply_to_email": r"reply|respond|answer|write back|get back|hit reply|send a reply",
    "forward_email": r"forward|pass|send",
    "schedule_send": r"text|email|mail|send|message|schedule|queue|remind|have a message|delay|land|go out",
    "cancel_event": r"cancel|delete|remove|take .* off|get rid|drop|scrap",
    "clear_reminders": r"delete|clear|wipe|remove",
    "trash_file": r"trash|delete|throw|remove|get rid",
    "complete_reminder": r"done|finished|complete|check|tick|mark",
    "update_reminder": r"move|push|change|reschedule|shift|update",
    "forget": r"forget|delete", "place_call": r"call|ring|dial|facetime|phone|get .* on the phone",
    "quit_app": r"quit|close|shut|exit", "clear_past_reminders": r"clear|delete|get rid|remove|wipe|dump|purge",
    "archive_email": r"archive|file away", "flag_email": r"flag", "mark_email_read": r"mark|read",
}
STRICT_OVERLAP = 0.4  # spec requires rejecting > 0.5; this validator is stricter
PERIOD_WORDS = {"today", "tomorrow", "yesterday", "this week", "next week", "this month", "next month", "last week", "last month"}


# ----------------------------------------------------------------------------- helpers

def load_jsonl(path, errors, name):
    out = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            try:
                out.append(json.loads(line))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}:{i}: invalid JSON ({e})")
    return out


def canon(a):
    return json.dumps(a, sort_keys=True, ensure_ascii=False)


def check_schema(value, schema, path="$"):
    """Strict JSON-schema subset check -> list of problems. Unknown keys are always flagged."""
    errs = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing required '{k}'")
        for k, v in value.items():
            if k not in props:
                errs.append(f"{path}: unknown key '{k}'")
            else:
                errs.extend(check_schema(v, props[k], f"{path}.{k}"))
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            errs.append(f"{path}: items not unique")
        for i, x in enumerate(value):
            if "items" in schema:
                errs.extend(check_schema(x, schema["items"], f"{path}[{i}]"))
    elif t == "string":
        if not isinstance(value, str):
            errs.append(f"{path}: expected string")
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errs.append(f"{path}: expected integer")
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errs.append(f"{path}: expected number")
    elif t == "boolean":
        if not isinstance(value, bool):
            errs.append(f"{path}: expected boolean")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: value not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum")
    return errs


def parse_now(sys1):
    m = re.search(r"The current date and time is \w+, (\w+) (\d+), (\d{4}) at (\d+):(\d+) (AM|PM)\.", sys1)
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    mo, d, y, h, mi, ap = m.groups()
    h = int(h) % 12 + (12 if ap == "PM" else 0)
    return dt.datetime(int(y), months.index(mo) + 1, int(d), h, int(mi))


def parse_catalog(sys1):
    cat = {}
    for line in sys1.splitlines():
        m = re.match(r"^([a-z_]+): (.*?) Tools: (.*)$", line)
        if m:
            cat[m.group(1)] = m.group(3).split(", ")
    return cat


WD_NAMES = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def stale_today_problems(msgs_after_user, now):
    now_min = now.hour * 60 + now.minute
    probs = []
    for m in msgs_after_user:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if tc["function"]["name"] == "find_free_time":
                    a = json.loads(tc["function"]["arguments"])
                    if a.get("period") == "today" and a.get("day_start", 0) * 60 < now_min:
                        probs.append("find_free_time today window starts before now")
        if m["role"] == "tool" and m["content"].startswith("Schedule - today"):
            for line in m["content"].splitlines():
                mm = re.match(r"\s+(\d{1,2})(?::(\d{2}))? ([AP])M - ", line)
                if mm and (int(mm.group(1)) % 12 + (12 if mm.group(3) == "P" else 0)) * 60 + int(mm.group(2) or 0) < now_min:
                    probs.append("agenda lists a 'today' item before the current time")
    return probs


def prompt_times(text):
    """Explicit clock times mentioned in a prompt -> set of (hour, minute)."""
    out = set()
    t = text.lower()
    for h, m, ap in re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", t):
        h = int(h) % 12 + (12 if ap == "pm" else 0)
        out.add((h, int(m or 0)))
    for h, m in re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s?(?:am|pm))", t):
        out.add((int(h), int(m)))
    if re.search(r"\bnoon\b", t):
        out.add((12, 0))
    return out


def slot_problems(fn, args, text, now):
    """Consistency between the user's words (weekday / today / tomorrow / clock time) and the call arguments."""
    probs = []
    t = text.lower()
    wds = {WD_NAMES[w] for w in re.findall(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", t)}
    date_val = None
    if fn in ("add_reminder", "add_calendar_event", "update_reminder") and "when_iso" in args:
        date_val = dt.datetime.fromisoformat(args["when_iso"])
    elif fn in ("find_free_time", "get_upcoming", "get_weather") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(args.get("period", ""))):
        date_val = dt.datetime.fromisoformat(args["period"])
    elif fn in ("find_free_time", "get_upcoming", "get_weather") and args.get("period") in ("today", "tomorrow"):
        date_val = dt.datetime.combine(now.date() + dt.timedelta(days=1 if args["period"] == "tomorrow" else 0), dt.time())
    if date_val is not None and fn != "update_reminder" or (fn == "update_reminder" and date_val is not None):
        if len(wds) == 1 and not re.search(r"\b(tomorrow|tmrw|tmw|today|tonight|in \d+|in an hour)\b", t):
            if date_val.weekday() not in wds:
                probs.append(f"{fn}: date {date_val.date()} is not the weekday named in the prompt")
        elif not wds:
            if re.search(r"\b(tomorrow|tmrw|tmw)\b", t) and not re.search(r"\b(today|tonight)\b", t) and date_val.date() != now.date() + dt.timedelta(days=1):
                probs.append(f"{fn}: date {date_val.date()} != tomorrow")
            if re.search(r"\b(today|tonight)\b", t) and not re.search(r"\b(tomorrow|tmrw|tmw)\b", t) and date_val.date() != now.date():
                probs.append(f"{fn}: date {date_val.date()} != today")
    times = prompt_times(t)
    rel = re.search(r"\bin (\d+|an|a|one|two|three) (minutes?|hours?|hour|min)\b|\bin an hour", t)
    if fn in ("add_reminder", "add_calendar_event", "update_reminder") and "when_iso" in args and times and not rel:
        w = dt.datetime.fromisoformat(args["when_iso"])
        if (w.hour, w.minute) not in times:
            probs.append(f"{fn}: time {w.hour:02d}:{w.minute:02d} not among times in prompt {sorted(times)}")
    if fn == "find_free_time" and len(times) == 1 and "day_start" in args and args.get("day_end", 0) - args["day_start"] == 1:
        (h, m), = tuple(times)
        if args["day_start"] != h:
            probs.append(f"find_free_time: day_start {args['day_start']} != prompt hour {h}")
    return probs


# ----------------------------------------------------------------------------- body fidelity (send_body_fidelity)

STOP = set("""a an the and or but if so to of in on at by for with from as is are was were be been am do does did have has had will would
can could should shall may might it its this that these those there here then than too very just also not no yes up out about into over
m s t re ll ve d what when where which who whom how why all any some more most other such only own same few each""".split())
ALLOW_PRON = {"i", "me", "my", "mine", "you", "your", "yours", "we", "our", "us", "they", "them", "their"}
TYPO_MAP = {"tmrw": "tomorrow", "tmw": "tomorrow", "msg": "message", "pls": "please", "thx": "thanks", "u": "you", "appt": "appointment",
            "bc": "because", "tnight": "tonight", "min": "minutes", "mins": "minutes", "relpy": "reply", "wrtie": "write"}
SEND_FRAME = re.compile(r"\AHi (?P<first>[A-Za-z.' -]+),\n\n(?P<content>.+)\n\nTest User\Z", re.S)


def stem(w):
    if len(w) > 4 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 3 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and w.endswith("es"):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def content_words(text, drop_pron=False):
    out = set()
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        t = TYPO_MAP.get(t, t)
        if t in STOP or (drop_pron and t in ALLOW_PRON):
            continue
        out.add(stem(t))
    return out


WRITE_FIELDS = {"add_reminder": ("title",), "add_calendar_event": ("title", "location"), "create_note": ("title", "body"),
                "append_note": ("text",), "remember": ("fact",), "set_timer": ("label",), "update_reminder": ("new_title",)}
SENDS = {"send_message": "text", "send_email": "body", "reply_to_email": "body", "schedule_send": "body", "draft_message": "text", "draft_email": "body"}
SUCCESS_RE = re.compile(r"^(Message sent|Email sent|Sent to|Reply sent|Scheduled)")


def split_frame(body):
    m = SEND_FRAME.match(body)
    return (m.group("content"), True) if m else (body, False)


def fidelity_problems(turn_msgs, user_text_all):
    """-> (problems, quote_requirements). turn_msgs: messages after the current user message."""
    probs, quotes = [], []
    allowed = content_words(user_text_all) | ALLOW_PRON
    results = {}
    for m in turn_msgs:
        if m["role"] == "tool":
            results[m["tool_call_id"]] = m["content"]
    for m in turn_msgs:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc["function"]["name"]
                if fn not in ("lookup_contact", DISCOVERY) and fn not in SENDS and fn not in WRITE_FIELDS:
                    allowed |= content_words(results.get(tc["id"], ""))
    for m in turn_msgs:
        if not (m["role"] == "assistant" and m.get("tool_calls")):
            continue
        for tc in m["tool_calls"]:
            fn = tc["function"]["name"]
            try:
                a = json.loads(tc["function"]["arguments"])
            except Exception:  # noqa: BLE001
                continue
            if fn in SENDS and isinstance(a.get(SENDS[fn]), str):
                body = a[SENDS[fn]]
                is_email = fn in ("send_email", "reply_to_email", "draft_email") or (fn == "schedule_send" and a.get("channel") == "email")
                content, framed = split_frame(body)
                if is_email and not framed:
                    probs.append(f"{fn}: email body lacks the exact 'Hi <First>,\\n\\n...\\n\\nTest User' frame")
                if not is_email and framed:
                    probs.append(f"{fn}: message body carries an email frame")
                extra = sorted(w for w in content_words(content) if w not in allowed and w not in {stem(x) for x in ALLOW_PRON})
                if extra:
                    probs.append(f"{fn}: words not from the user's request: {extra}")
                if fn in ("draft_message", "draft_email"):
                    # looser (kept for clarity; the strict check above already applies): no invented digits / weekdays / months
                    for tok in re.findall(r"[a-z0-9]*\d[a-z0-9]*|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december", content.lower()):
                        if stem(TYPO_MAP.get(tok, tok)) not in allowed:
                            probs.append(f"{fn}: invented date/number token '{tok}'")
                res = results.get(tc["id"], "")
                if fn not in ("draft_message", "draft_email") and SUCCESS_RE.match(res):
                    quotes.append(body)
            elif fn in WRITE_FIELDS:
                for fld in WRITE_FIELDS[fn]:
                    v = a.get(fld)
                    if isinstance(v, str):
                        extra = sorted(w for w in content_words(v) if w not in allowed)
                        if extra:
                            probs.append(f"{fn}.{fld}: words not from the user's request: {extra}")
    return probs, quotes


def norm_tokens(s):
    return re.findall(r"[a-z0-9']+", s.lower())


def hsh(t):
    return hashlib.sha1(" ".join(t).encode()).hexdigest()[:12]


def heldout_overlap(held, text):
    t = norm_tokens(text)
    if not t:
        return 0.0
    full = hsh(t)
    best = 0.0
    for it in held:
        if it["full"] == full:
            return 1.0
        m = min(len(t), it["ntok"])
        if m < 3:
            continue
        n = min(6, m)
        a = {hsh(t[i:i + n]) for i in range(len(t) - n + 1)}
        b = set(it["sh"].get(str(n), []))
        if a and b:
            best = max(best, len(a & b) / len(a | b))
    return best


def jacc_ngrams(a, b, n=4):
    ta, tb = norm_tokens(a), norm_tokens(b)
    if len(ta) < n or len(tb) < n:
        return 1.0 if ta == tb and ta else 0.0
    A = {tuple(ta[i:i + n]) for i in range(len(ta) - n + 1)}
    B = {tuple(tb[i:i + n]) for i in range(len(tb) - n + 1)}
    return len(A & B) / len(A | B)


# ----------------------------------------------------------------------------- main validation

class Report:
    def __init__(self):
        self.checks = collections.OrderedDict()

    def add(self, name, ok, detail=None, examples=None):
        self.checks[name] = {"passed": bool(ok), "detail": detail, "examples": (examples or [])[:8]}

    @property
    def passed(self):
        return all(c["passed"] for c in self.checks.values())


def run(d: Path, check_repro=False):
    rep = Report()
    load_errors = []
    reg_doc = json.loads((d / "assets" / "tools.json").read_text())
    REG = {s["function"]["name"]: s for s in reg_doc["full_registry_schemas"]}
    DISC = reg_doc["discovery_schema_step0"][0]
    ctx_note = json.loads((d / "assets" / "system_parts.json").read_text())["context_note"]
    held = json.loads((d / "assets" / "heldout_shingles.json").read_text())["items"]
    rows = {s: load_jsonl(d / f"{s}.jsonl", load_errors, f"{s}.jsonl") for s in ("train", "dev")}
    prov = {s: load_jsonl(d / f"{s}.provenance.jsonl", load_errors, f"{s}.provenance.jsonl") for s in ("train", "dev")}
    convs = load_jsonl(d / "conversations.jsonl", load_errors, "conversations.jsonl")
    rep.add("json_valid_all_files", not load_errors, f"{len(load_errors)} errors", load_errors)
    # intent text (typo-free prompt + earlier user turns) used only for the authority-cue check
    PROV_CUR = {p["id"]: (p.get("user_prompt_clean") or p["user_prompt"]) for s_ in ("train", "dev") for p in prov[s_]}
    PROV_CLEAN = {p["id"]: " ".join(p.get("history_user_prompts", []) + [p.get("user_prompt_clean") or p["user_prompt"]])
                  for s_ in ("train", "dev") for p in prov[s_]}

    fail = collections.defaultdict(list)

    def bad(check, msg):
        fail[check].append(msg)

    row_by_conv = collections.defaultdict(list)
    est_tokens, tgt_tokens = {"train": [], "dev": []}, {"train": [], "dev": []}
    tool_counter = collections.Counter()
    max_overlap = 0.0
    all_prompts = {"train": collections.Counter(), "dev": collections.Counter()}
    email_domains_bad, phones_bad = [], []

    for split in ("train", "dev"):
        for row in rows[split]:
            rid = row.get("id", "?")
            m = re.fullmatch(r"v3-([a-z]+)-(train|dev)-(\d{4})-s(\d+)", rid)
            if not m or m.group(2) != split:
                bad("row_schema", f"{rid}: bad id/split")
                continue
            if set(row) != {"id", "conversation_id", "tools", "messages"}:
                bad("row_schema", f"{rid}: keys {sorted(row)}")
            if rid.rsplit("-s", 1)[0] != row["conversation_id"]:
                bad("row_schema", f"{rid}: conversation_id mismatch")
            row_by_conv[row["conversation_id"]].append((int(m.group(4)), row, split))
            msgs = row["messages"]
            if len(msgs) < 5 or [x["role"] for x in msgs[:3]] != ["system"] * 3:
                bad("row_schema", f"{rid}: need 3 system messages")
                continue
            sys0, sys1, sys2 = (msgs[i]["content"] for i in range(3))
            if not sys0.startswith("You are Wisp") or "MODEL-LED CAPABILITY DISCOVERY" not in sys1 or sys2 != ctx_note:
                bad("system_messages", f"{rid}: system messages not the captured model-led prompts")
            if "Name: Test User" not in sys0 or "test.user@example.invalid" not in sys0 or "example.com" in sys0:
                bad("synthetic_identity", f"{rid}: identity block not synthetic")
            if any(k in msgs[0] or k in msgs[1] or k in msgs[2] for k in ("training",)):
                bad("row_schema", f"{rid}: system message carries training flag")
            tgt = msgs[-1]
            if tgt["role"] != "assistant" or tgt.get("training") is not True:
                bad("masking", f"{rid}: last message must be the training:true assistant")
            for x in msgs[3:-1]:
                if x["role"] == "assistant" and x.get("training") is not False:
                    bad("masking", f"{rid}: non-target assistant not training:false")
                if x["role"] != "assistant" and "training" in x:
                    bad("masking", f"{rid}: non-assistant message has training flag")
            # locate the current user turn (last user message)
            ui = max(i for i, x in enumerate(msgs) if x["role"] == "user")
            user_text = msgs[ui]["content"]
            turn = msgs[ui + 1:-1]
            # --- replay the discovery state
            catalog = parse_catalog(sys1)
            enum_f = DISC["function"]["parameters"]["properties"]["families"]["items"]["enum"]
            attempts, loaded = 0, []
            pending = {}
            expect_tool_ids = []
            last_failure_tool = None
            seen_calls = set()
            turn_ok = True
            i = 0
            n_fail_results = 0
            while i < len(turn):
                x = turn[i]
                if x["role"] != "assistant" or not x.get("tool_calls"):
                    bad("protocol", f"{rid}: unexpected message {x['role']} inside turn at {i}")
                    turn_ok = False
                    break
                if x.get("content") != "":
                    bad("protocol", f"{rid}: tool-call turn must have empty content")
                calls = x["tool_calls"]
                results = turn[i + 1:i + 1 + len(calls)]
                if len(results) != len(calls) or any(r_["role"] != "tool" for r_ in results):
                    bad("call_pairing", f"{rid}: tool results missing")
                    turn_ok = False
                    break
                ids = [c_["id"] for c_ in calls]
                if len(set(ids)) != len(ids) or any(r_["tool_call_id"] != c_["id"] for r_, c_ in zip(results, calls)):
                    bad("call_pairing", f"{rid}: id mismatch/order")
                offered_now = list(loaded) + ([DISCOVERY] if attempts < 2 else [])
                for c_, r_ in zip(calls, results):
                    fn = c_["function"]["name"]
                    try:
                        args = json.loads(c_["function"]["arguments"])
                    except Exception:  # noqa: BLE001
                        bad("arguments_json", f"{rid}: non-JSON arguments for {fn}")
                        continue
                    if fn not in offered_now:
                        bad("protocol", f"{rid}: in-turn call to unoffered {fn}")
                    key = (fn, canon(args))
                    if key in seen_calls:
                        bad("no_repeat", f"{rid}: repeated call {fn}")
                    seen_calls.add(key)
                    if len(r_["content"]) >= 400:
                        bad("result_length", f"{rid}: tool result {len(r_['content'])} chars")
                    if fn == DISCOVERY:
                        attempts += 1
                        fams = args.get("families") if isinstance(args, dict) else None
                        tools_req = args.get("tools") if isinstance(args, dict) else None
                        ok = (isinstance(args, dict) and set(args) <= {"families", "tools"} and isinstance(fams, list) and 1 <= len(fams) <= 3
                              and len(set(fams)) == len(fams) and all(f in enum_f and f in catalog for f in fams))
                        if ok:
                            allowed = {n for f in fams for n in catalog[f]}
                            req = tools_req if tools_req is not None else sorted(allowed)
                            ok = (isinstance(req, list) and 1 <= len(req) <= 24 and len(set(req)) == len(req) and set(req) <= allowed)
                        if ok:
                            names = list(dict.fromkeys([*loaded, *req]))
                            ok = len(names) <= 24
                        if ok:
                            loaded = names
                            exp = json.dumps({"schema_discovery": "ready_next_step", "families": fams, "available_tools": names,
                                              "source_tools_executed": False})
                            if r_["content"] != exp:
                                bad("discovery_receipt", f"{rid}: receipt != host expansion")
                        else:
                            if r_["content"] != REJECT:
                                bad("discovery_receipt", f"{rid}: invalid discovery without rejection receipt")
                    else:
                        txt = r_["content"]
                        if txt.startswith("(") and (txt.endswith(")") or "NOT" in txt):
                            n_fail_results += 1
                i += 1 + len(calls)
            if n_fail_results > 2:
                bad("error_policy", f"{rid}: {n_fail_results} failing results in a turn")
            if not turn_ok:
                continue
            # --- offered tools equal replayed state
            offered = loaded + ([DISCOVERY] if attempts < 2 else [])
            names_row = [t_["function"]["name"] for t_ in row["tools"]]
            if names_row != offered:
                bad("offered_tools", f"{rid}: tools {names_row} != replay {offered}")
            for t_ in row["tools"]:
                n_ = t_["function"]["name"]
                if (n_ == DISCOVERY and t_ != DISC) or (n_ != DISCOVERY and t_ != REG.get(n_)):
                    bad("offered_tools", f"{rid}: schema for {n_} differs from registry")
            if not turn and names_row != [DISCOVERY]:
                bad("protocol", f"{rid}: round 0 must offer only get_tool_schemas")
            # scoped system prompt follows loaded tools
            has_send_rule = "You CAN send things" in sys0
            if has_send_rule != bool({"send_email", "send_message", "reply_to_email", "draft_email", "draft_message", "schedule_send"} & set(loaded)):
                bad("system_scope", f"{rid}: send rule presence does not match loaded tools")
            # --- the target
            now = parse_now(sys1)
            for pr in stale_today_problems(msgs[ui + 1:], now):
                bad("today_staleness", f"{rid}: {pr}")
            if tgt.get("tool_calls"):
                if tgt.get("content") != "":
                    bad("protocol", f"{rid}: tool-call target must have empty content")
                tids = [c_["id"] for c_ in tgt["tool_calls"]]
                allids = [c_["id"] for x in turn if x.get("tool_calls") for c_ in x["tool_calls"]]
                if len(set(tids)) != len(tids) or set(tids) & set(allids):
                    bad("call_pairing", f"{rid}: duplicate tool_call id")
                for c_ in tgt["tool_calls"]:
                    if c_.get("type") != "function":
                        bad("row_schema", f"{rid}: tool call type")
                    fn = c_["function"]["name"]
                    if fn not in offered:
                        bad("protocol", f"{rid}: target calls {fn} which is not offered (real tool before schema load?)")
                        continue
                    try:
                        args = json.loads(c_["function"]["arguments"])
                    except Exception:  # noqa: BLE001
                        bad("arguments_json", f"{rid}: target arguments not JSON")
                        continue
                    tool_counter[fn] += 1
                    schema = DISC if fn == DISCOVERY else REG[fn]
                    for e in check_schema(args, schema["function"]["parameters"]):
                        bad("schema_valid", f"{rid}: {fn} {e}")
                    key = (fn, canon(args))
                    if key in seen_calls:
                        bad("no_repeat", f"{rid}: target repeats earlier call {fn}")
                    seen_calls.add(key)
                    if fn == DISCOVERY:
                        if not turn and False:
                            pass
                        if attempts >= 2:
                            bad("protocol", f"{rid}: third discovery call")
                        fams = args.get("families", [])
                        if not (1 <= len(fams) <= 3):
                            bad("protocol", f"{rid}: families count")
                        if "tools" not in args or len(args["tools"]) > 8:
                            bad("protocol", f"{rid}: discovery should name the needed tools (<=8 here)")
                        allowed = {n for f in fams for n in catalog.get(f, [])}
                        if not set(args.get("tools", [])) <= allowed:
                            bad("protocol", f"{rid}: target discovery tools outside selected families (would be rejected)")
                    else:
                        for fld in ("when_iso",):
                            if fld in args:
                                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", args[fld]):
                                    bad("date_sanity", f"{rid}: {fn}.{fld} format {args[fld]}")
                                else:
                                    w = dt.datetime.fromisoformat(args[fld])
                                    if w <= now or w > now + dt.timedelta(days=370):
                                        bad("date_sanity", f"{rid}: {fn}.{fld} {args[fld]} outside (now, now+1y]")
                        if fn in ("get_upcoming", "find_free_time") and "period" in args:
                            p = args["period"]
                            if not (p in PERIOD_WORDS or (re.fullmatch(r"\d{4}-\d{2}-\d{2}", p) and dt.date.fromisoformat(p) >= now.date())):
                                bad("date_sanity", f"{rid}: {fn}.period {p}")
                        if fn == "find_free_time":
                            if not (0 <= args.get("day_start", 9) < args.get("day_end", 18) <= 23):
                                bad("date_sanity", f"{rid}: day_start/day_end")
                        pv_ = PROV_CLEAN.get(row["conversation_id"], "")
                        cur_ = PROV_CUR.get(row["conversation_id"], pv_)
                        for pr in slot_problems(fn, args, cur_, now):
                            bad("slot_consistency", f"{rid}: {pr}")
                        # explicit authority cue for sends / destructive tools (whole conversation's user text)
                        if fn in CUES:
                            pv = PROV_CLEAN.get(row["conversation_id"])
                            utext = (pv if pv else " ".join(x["content"] for x in msgs[3:ui + 1] if x["role"] == "user")).lower()
                            if not re.search(CUES[fn], utext):
                                bad("explicit_authority", f"{rid}: {fn} without matching user cue")
            else:
                if not tgt.get("content", "").strip():
                    bad("final_present", f"{rid}: empty final")
            # --- identity / PII scan on all message text
            blob = "\n".join(x.get("content", "") or "" for x in msgs) + "\n".join(
                c_["function"]["arguments"] for x in msgs if x.get("tool_calls") for c_ in x["tool_calls"])
            for em in re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]+", blob):
                if not em.endswith(".invalid"):
                    email_domains_bad.append(f"{rid}: {em}")
            for ph in re.findall(r"\+1[\s-]?\(?\d{3}\)?[\s-]?\d{3,4}[\s-]?\d{0,4}", blob):
                if not re.match(r"\+1 555 01\d\d$", ph.strip()):
                    phones_bad.append(f"{rid}: {ph}")
            est_tokens[split].append(len(json.dumps(row, ensure_ascii=False)) / 4)
            tgt_tokens[split].append(len(json.dumps(tgt, ensure_ascii=False)) / 4)

    # ---- per-conversation checks
    prov_by_id = {p["id"]: (p, s) for s in ("train", "dev") for p in prov[s]}
    conv_user_prompts = {}
    for cid, lst in row_by_conv.items():
        lst.sort(key=lambda x: x[0])
        steps = [s for s, _, _ in lst]
        split = lst[0][2]
        if steps != list(range(len(steps))):
            bad("steps_contiguous", f"{cid}: steps {steps}")
        last = lst[-1][1]["messages"][-1]
        if last.get("tool_calls") or not last.get("content", "").strip():
            bad("final_present", f"{cid}: conversation does not end in final assistant text")
        for s, r_, _ in lst[:-1]:
            if not r_["messages"][-1].get("tool_calls"):
                bad("final_present", f"{cid}: non-final step is text")
        # prefix consistency
        def normmsg(x):
            y = {k: v for k, v in x.items() if k != "training"}
            return json.dumps(y, sort_keys=True)
        for (s0, r0, _), (s1, r1, _) in zip(lst, lst[1:]):
            a = [normmsg(x) for x in r0["messages"][3:]]
            b = [normmsg(x) for x in r1["messages"][3:]]
            if b[:len(a)] != a:
                bad("prefix_consistency", f"{cid}: step {s1} is not an extension of step {s0}")
        # user prompt bookkeeping
        msgs_last = lst[-1][1]["messages"]
        ui = max(i for i, x in enumerate(msgs_last) if x["role"] == "user")
        up = msgs_last[ui]["content"]
        conv_user_prompts[cid] = up
        all_prompts[split][up.strip().lower()] += 1
        ov = max(heldout_overlap(held, x["content"]) for x in msgs_last[3:ui + 1] if x["role"] == "user")
        max_overlap = max(max_overlap, ov)
        if ov >= STRICT_OVERLAP:
            bad("heldout_overlap", f"{cid}: Jaccard {ov:.2f} vs held-out eval prompt")
        # provenance
        if cid not in prov_by_id:
            bad("provenance", f"{cid}: no provenance")
            continue
        p, psplit = prov_by_id[cid]
        if psplit != split or p.get("split") != split:
            bad("provenance", f"{cid}: split mismatch")
        # gold sequence == calls in the last row
        gold = [{"name": c_["function"]["name"], "arguments": json.loads(c_["function"]["arguments"])}
                for x in msgs_last[ui + 1:] if x.get("tool_calls") for c_ in x["tool_calls"]]
        pg = [{k: v for k, v in g.items() if k != "rejected"} for g in p["gold_calls"]]
        if gold != pg:
            bad("provenance", f"{cid}: gold_calls differ from conversation")
        if p["n_steps"] != len(lst):
            bad("provenance", f"{cid}: n_steps mismatch")
        # category-level policies
        tools_called = [g["name"] for g in gold]
        cat = p["category"]
        if cat in READ_ONLY_CATEGORIES and set(tools_called) & WRITE_TOOLS:
            bad("read_only_categories", f"{cid}: {cat} calls write tool {set(tools_called) & WRITE_TOOLS}")
        if cat == "chat_no_tool" and (tools_called or len(lst) != 1 or [t_["function"]["name"] for t_ in lst[0][1]["tools"]] != [DISCOVERY]):
            bad("chat_no_tool", f"{cid}: chat conversation used tools")
        if tools_called and tools_called[0] != DISCOVERY:
            bad("protocol", f"{cid}: first call is not get_tool_schemas")
        if len([n for n in tools_called if n == DISCOVERY]) > 2:
            bad("protocol", f"{cid}: more than two discovery calls")
        # send body fidelity + final quotes the exact body
        uall = " ".join([PROV_CLEAN.get(cid, "")] + [x["content"] for x in msgs_last[3:ui + 1] if x["role"] == "user"])
        fprobs, quotes = fidelity_problems(msgs_last[ui + 1:], uall)
        for pr in fprobs:
            bad("write_fidelity" if pr.split(".")[0].split(":")[0] in WRITE_FIELDS else "send_body_fidelity", f"{cid}: {pr}")
        flat = re.sub(r"\s+", " ", last.get("content", ""))
        for qb in quotes:
            if re.sub(r"\s+", " ", qb) not in flat:
                bad("final_quotes_body", f"{cid}: final answer does not quote the exact body that was sent")
        # denial / failure honesty
        tool_msgs = [x["content"] for x in msgs_last[ui + 1:] if x["role"] == "tool"]
        final = last.get("content", "")
        neg = re.compile(r"not sent|n't send|wasn't sent|weren't sent|didn't go|declin|held off|haven't sent|nothing (was|has|went)|couldn't|can't|unable|not (been )?(added|saved|created|cancel|sent|verified)|failed|didn't|did not|no[,.]| isn't|not free|haven't|hasn't|without", re.I)
        if any(t_ == DECLINED for t_ in tool_msgs):
            if not neg.search(final):
                bad("denial_honesty", f"{cid}: final does not reflect declined send")
            if re.match(r"(Sent|Done\b|Texted|Message sent|Email sent|Both)", final) and "declin" not in final.lower() and "not" not in final.lower():
                bad("denial_honesty", f"{cid}: final claims success after denial")
        if tool_msgs and tool_msgs[-1].startswith("(") and any(g in WRITE_TOOLS for g in tools_called):
            if not neg.search(final) or re.match(r"(Sent|Done\b|Added|Saved|Created)", final):
                bad("error_honesty", f"{cid}: final may claim success after a failed write/send")
        if tool_msgs and (tool_msgs[-1].startswith("(error") or tool_msgs[-1].startswith("Wisp could not")) and not final.strip():
            bad("error_honesty", f"{cid}: silent on error")

    # ---- row/conversation consistency with conversations.jsonl
    conv_ids = {c_.get("id") for c_ in convs}
    if conv_ids != set(row_by_conv) or len(convs) != len(conv_ids):
        bad("conversations_file", f"conversations.jsonl ids differ from rows ({len(conv_ids)} vs {len(row_by_conv)})")
    for c_ in convs:
        if "tools_per_round" not in c_ or not c_["messages"][-1].get("content"):
            bad("conversations_file", f"{c_.get('id')}: missing tools_per_round/final")

    # ---- split integrity & counts
    fam_split = collections.defaultdict(set)
    scen = collections.Counter()
    tpl_split = collections.defaultdict(set)
    counts = {"train": collections.Counter(), "dev": collections.Counter()}
    for s in ("train", "dev"):
        for p in prov[s]:
            fam_split[p["family_id"]].add(s)
            tpl_split[p["template_id"]].add(s)
            scen[p["scenario_id"]] += 1
            counts[s][p["category"]] += 1
            for k in ("id", "split", "category", "family_id", "scenario_id", "template_id", "gold_calls", "targeted_failure_class"):
                if k not in p:
                    bad("provenance", f"{p.get('id')}: missing {k}")
    for f, ss in fam_split.items():
        if len(ss) > 1:
            bad("family_split", f"family {f} appears in {sorted(ss)}")
    for t_, ss in tpl_split.items():
        if len(ss) > 1:
            bad("family_split", f"template {t_} appears in {sorted(ss)}")
    if any(v > 1 for v in scen.values()):
        bad("family_split", "duplicate scenario_id")
    for cat, (tn, dn) in EXPECTED.items():
        if counts["train"][cat] != tn or counts["dev"][cat] != dn:
            bad("category_counts", f"{cat}: train {counts['train'][cat]}/{tn}, dev {counts['dev'][cat]}/{dn}")
    if sum(counts["train"].values()) != 900 or sum(counts["dev"].values()) != 100:
        bad("category_counts", "train/dev totals must be 900/100")
    dup_in = [p for s in ("train", "dev") for p, c_ in all_prompts[s].items() if c_ > 1]
    if dup_in:
        bad("duplicate_prompts", f"{len(dup_in)} user prompts repeated within a split: {dup_in[:3]}")
    shared = set(all_prompts["train"]) & set(all_prompts["dev"])
    if shared:
        bad("duplicate_prompts", f"{len(shared)} identical prompts in both splits: {list(shared)[:3]}")
    # informational: closest dev->train prompt similarity (4-gram Jaccard)
    tr_prompts = [conv_user_prompts[p["id"]] for p in prov["train"] if p["id"] in conv_user_prompts]
    dv_prompts = [conv_user_prompts[p["id"]] for p in prov["dev"] if p["id"] in conv_user_prompts]
    max_dev_train = 0.0
    for dp in dv_prompts:
        for tp in tr_prompts:
            max_dev_train = max(max_dev_train, jacc_ngrams(dp, tp))
    if email_domains_bad:
        bad("synthetic_only", f"{len(email_domains_bad)} non-.invalid email addresses, e.g. {email_domains_bad[:3]}")
    if phones_bad:
        bad("synthetic_only", f"{len(phones_bad)} phone numbers outside +1 555 01xx, e.g. {phones_bad[:3]}")

    # ---- reproducibility
    if check_repro:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([sys.executable, str(d / "generate.py"), "--output-dir", td], check=True, capture_output=True)
            for fn in ("train.jsonl", "dev.jsonl", "train.provenance.jsonl", "dev.provenance.jsonl", "conversations.jsonl", "DATA_CARD.md", "stats.json"):
                a = hashlib.sha256((d / fn).read_bytes()).hexdigest()
                b = hashlib.sha256((Path(td) / fn).read_bytes()).hexdigest()
                if a != b:
                    bad("reproducible", f"{fn} differs on regeneration")

    # ---- assemble report
    descr = {
        "row_schema": "row keys, id pattern, conversation_id, 3 system messages",
        "system_messages": "captured model-led system prompts present",
        "synthetic_identity": "identity block is Test User / test.user@example.invalid",
        "masking": "only the last assistant message of a row is training:true",
        "protocol": "discovery first, no real tool before schema load, <=2 discovery calls, empty content on tool turns",
        "discovery_receipt": "tool receipts equal the host's cumulative expansion / rejection text",
        "offered_tools": "row.tools equals replayed offered set with registry-exact schemas",
        "system_scope": "scoped system rules match loaded tools",
        "arguments_json": "tool-call arguments are JSON strings",
        "schema_valid": "every target call valid against the schema offered at that step (types, required, enum, no extra keys)",
        "call_pairing": "tool_call ids unique and paired 1:1 with tool messages in order",
        "no_repeat": "no identical (name,args) call repeated within a turn",
        "result_length": "tool results < 400 chars",
        "final_present": "every conversation ends with non-empty assistant text; only the last step is text",
        "steps_contiguous": "step numbering contiguous per conversation",
        "prefix_consistency": "each step extends the previous step's messages",
        "date_sanity": "when_iso/period/day_start sane relative to the system date",
        "today_staleness": "'today' reads never show items or slots earlier than the reference time",
        "send_body_fidelity": "send/draft/schedule/reply bodies contain only the user's stated content (email: exact Hi <First>/Test User frame)",
        "write_fidelity": "reminder/event/note/memory/timer titles and bodies use the user's words",
        "final_quotes_body": "after a successful send the final answer quotes the exact body sent",
        "slot_consistency": "weekday / today / tomorrow / clock time in the user's words match the call arguments",
        "explicit_authority": "send/destructive tools only with a matching user cue",
        "read_only_categories": "read-only categories never call write/send/destructive tools",
        "chat_no_tool": "chat conversations use no tools",
        "denial_honesty": "declined sends are reported, not claimed",
        "error_honesty": "failed writes/sends are not claimed as done",
        "error_policy": "at most 2 failing tool results per turn",
        "provenance": "provenance complete and consistent with conversations",
        "family_split": "families/templates/scenarios are never shared across splits",
        "category_counts": "exact category counts per split",
        "duplicate_prompts": "no repeated user prompt within or across splits",
        "heldout_overlap": "normalized n-gram Jaccard (n=min(6,len)) < 0.4 vs every held-out eval prompt (spec: <= 0.5)",
        "synthetic_only": "only .invalid emails and +1 555 01xx phones",
        "conversations_file": "conversations.jsonl matches rows",
        "reproducible": "regeneration is byte-identical",
    }
    for name, text in descr.items():
        if name == "reproducible" and not check_repro:
            continue
        rep.add(name, not fail.get(name), text if not fail.get(name) else f"{len(fail[name])} failure(s): {text}", fail.get(name))
    stat = lambda xs: {"mean": round(statistics.mean(xs)), "p50": round(sorted(xs)[len(xs) // 2]), "p95": round(sorted(xs)[int(len(xs) * 0.95)]), "max": round(max(xs))}
    out = {
        "passed": rep.passed, "checks": rep.checks,
        "counts": {s: {"conversations": len(prov[s]), "rows": len(rows[s]), "by_category": dict(counts[s]),
                       "families": len({p["family_id"] for p in prov[s]})} for s in ("train", "dev")},
        "token_estimate_chars_div4": {s: {"row": stat(est_tokens[s]), "target": stat(tgt_tokens[s])} for s in ("train", "dev")},
        "heldout_max_jaccard": round(max_overlap, 3), "dev_vs_train_max_4gram_jaccard": round(max_dev_train, 3),
        "target_tool_call_counts": dict(tool_counter.most_common()),
        "failure_total": sum(len(v) for v in fail.values()),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(HERE))
    ap.add_argument("--check-repro", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    d = Path(a.dir)
    res = run(d, a.check_repro)
    (Path(a.out) if a.out else d / "validation.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    for n, c in res["checks"].items():
        print(("PASS " if c["passed"] else "FAIL ") + n + ("" if c["passed"] else f"  -> {c['detail']}"))
        for e in c["examples"][:4]:
            if not c["passed"]:
                print("      ", e)
    print("OVERALL:", "PASS" if res["passed"] else "FAIL", "| held-out max Jaccard", res["heldout_max_jaccard"],
          "| dev-vs-train max 4-gram Jaccard", res["dev_vs_train_max_4gram_jaccard"])
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
