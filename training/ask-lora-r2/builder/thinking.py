"""Situational thinking for the Ask LoRA: which steps think, and what they think.

Production sends EVERY Ask step with enable_thinking (service/agent/loop.py
_template_kwargs; models.yaml model_only_narration_thinking), so Gemma 4's template
puts `<|think|>` at the top of the system turn on every step. That flag must not
change per step (it would break oMLX's prefix cache), so the model itself has to
learn WHEN a thought is worth its tokens:

- a first step (straight after the user) may open `<|channel>thought ... <channel|>`
  itself, or answer / call directly;
- a step after a tool result starts inside `<|channel>thought\\n` (the template forces
  it), so the completion is either a real thought or an EMPTY one ("\\n<channel|>").

`decide(meta, messages)` is the policy (rule-based on family, step position, the
previous tool result and the calls about to be made). `thought(meta, messages, ctx)`
writes the thought from the step's real context: the frozen clock, the prompt, the
call(s) and their arguments, the previous result. Both are deterministic.

Policy (THINK = a short real thought; QUIET = no thought / empty thought):
  never        chat_voice, knowledge_direct: QUIET on every step.
  after_error  previous result is an error, refusal or denial: THINK (why + the fix).
  quiet fams   concise_gentle, capability_honest: QUIET otherwise.
  after_load   the call right after a schema load: QUIET (mechanical).
  answer_first a first-step answer with no call: THINK only in write families
               (one clarifying question, or deciding not to act); else QUIET.
  select/next  a step that makes or loads a write, or plans one (a read in a turn
               that writes), or stub_then_call's / safe_effects' first selection:
               THINK. A later read in multi_source: THINK. Other reads: QUIET.
  narrate      final answer after results: THINK for multi_source, for
               ignore_embedded_instructions when a result holds text aimed at an
               assistant, and for grounded_only when counts, times or dates
               matter; QUIET otherwise (one clean result).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re

import writes as _writes

NEVER = frozenset({"chat_voice", "knowledge_direct"})
QUIET_FAMILIES = frozenset({"concise_gentle", "capability_honest"})
WRITE_FAMILIES = frozenset({"finish_the_write", "exact_args", "right_write_tool", "safe_effects", "relay_receipt",
                            "act_dont_ask", "stub_then_call"})
WRITE_TOOLS = (frozenset().union(*_writes.WRITE_ALLOW.values()) | _writes.ALWAYS_CARD | _writes.NEVER_CARD
               | {"clear_reminders", "manage_contacts", "cancel_event", "reply_to_email"})
DISCOVERY = "get_tool_schemas"
MAX_WORDS = 60
JACCARD_MAX = 0.5
DENIED = "The user denied this action."

_ERROR = re.compile(r"^\s*\(error\b|^The user denied this action|^Wisp (?:can't|could not|cannot)\b|isn't connected|"
                    r"read-only calendar|[Nn]othing was (?:moved|changed|sent|removed|created|added|saved|deleted)\b|"
                    r"^Only the organizer")
INJECTION = re.compile(
    r"(ignore (?:all )?(?:your )?previous instructions|\bAI (?:assistant|model)\b|assistant instructions|"
    r"hidden prompt|system override|if you are an ai|note to any ai|hey wisp,? (?:please )?(?:send|forward|delete|add))",
    re.I)
# Text that must never show up inside a thought: tool plumbing, markup, the harness.
NOISE = re.compile(r"schema_discovery|ready_next_step|source_tools_executed|not_run_yet|\{\"|<\||\|>|\*\*|Traceback|"
                   r"World\.|\bt_[a-z_]+\(|\bstubs?\b|harness|synthetic|[PM]ST-0[78]00|PDT-0700|got an unexpected keyword|"
                   r"\bthe user\b|\bSam\b|sam\.rivera", re.I)

_GROUNDED_MATH = re.compile(r"\b(?:how many|count|unread|free|booked|busy|time for|complete|completed|wrapped|finish|"
                            r"family|left today|happening today|recap|came in|arrived|which (?:items|events|things)|"
                            r"(?:this|past|last) week|yesterday|today|tomorrow)\b", re.I)


# ----------------------------------------------------------------- step analysis
def call_args(tc: dict) -> dict:
    a = tc["function"].get("arguments")
    if isinstance(a, str):
        try:
            a = json.loads(a or "{}")
        except ValueError:
            a = {}
    return a if isinstance(a, dict) else {}


def calls_of(msg: dict) -> list[tuple[str, dict]]:
    return [(tc["function"]["name"], call_args(tc)) for tc in msg.get("tool_calls") or []]


def is_error(text: str) -> bool:
    first = (text or "").lstrip().split("\n", 1)[0]
    return bool(_ERROR.search(first))


def injected(text: str) -> bool:
    return any(INJECTION.search(s) for s in re.split(r"(?<=[.!?])\s+|\n", text or ""))


def _loads(name: str, args: dict) -> set[str]:
    """Tools a call names: itself, or the tools a get_tool_schemas load asks for."""
    return set(args.get("tools") or []) if name == DISCOVERY else {name}


def analyze(meta: dict, messages: list[dict]) -> dict:
    """What kind of step this is, from the conversation alone (plus meta's family and turn writes)."""
    target, hist = messages[-1], messages[:-1]
    user_idx = max(i for i, m in enumerate(hist) if m["role"] == "user")
    first = hist[-1]["role"] == "user"
    results, prev_calls = [], []
    if not first:
        j = len(hist) - 1
        while j > user_idx and hist[j]["role"] == "tool":
            results.insert(0, str(hist[j].get("content") or ""))
            j -= 1
        if hist[j]["role"] == "assistant":
            prev_calls = calls_of(hist[j])
    all_results = [str(m.get("content") or "") for m in hist[user_idx + 1:] if m["role"] == "tool"]
    calls = calls_of(target)
    loaded = bool(results) and all(r.lstrip().startswith('{"schema_discovery"') for r in results)
    error = any(is_error(r) for r in results)
    if first:
        kind = "select" if calls else "answer_first"
    elif error:
        kind = "after_error"
    elif loaded and calls:
        kind = "after_load"
    else:
        kind = "next_call" if calls else "narrate"
    touched = set().union(*[_loads(n, a) for n, a in calls]) if calls else set()
    turn_writes = meta.get("turn_writes")
    if turn_writes is None:
        turn_writes = meta.get("writes") or []
    return {"kind": kind, "first": first, "calls": calls, "prev_calls": prev_calls, "results": results,
            "all_results": all_results, "error": error, "loaded": loaded,
            "writes_now": bool(touched & WRITE_TOOLS), "turn_writes": list(turn_writes),
            "prompt": str(hist[user_idx].get("content") or ""), "target_text": str(target.get("content") or "")}


_RECEIPT = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})"
                      r"(?:, (\d{4}))?,? (?:at )?(\d{1,2}:\d\d [AP]M)(?:[^–\n]*?(?:–| to )(?:\w{3} \w{3} \d{1,2}, \d{4} )?"
                      r"(\d{1,2}:\d\d [AP]M))?")


def receipt_of(s: dict) -> dict | None:
    """The date and times a write's receipt states (previous call a write, result not an error)."""
    if s["error"] or not s["results"] or not s["prev_calls"]:
        return None
    if not any(n in WRITE_TOOLS for n, _ in s["prev_calls"]):
        return None
    r = s["results"][-1]
    m = _RECEIPT.search(r)
    if not m:
        return None
    t = re.search(r"“([^”]+)”", r)
    return {"wd": m[1], "mon": m[2], "day": int(m[3]), "start": m[5], "end": m[6],
            "title": t.group(1) if t else "", "wisp_only": "Wisp only" in r, "text": r}


def decide(meta: dict, messages: list[dict]) -> bool:
    """True when this step should carry a real thought (see the module docstring)."""
    fam = meta.get("family") or ""
    if fam in NEVER:
        return False
    s = analyze(meta, messages)
    kind = s["kind"]
    if kind == "after_error":
        return True
    if fam in QUIET_FAMILIES or kind == "after_load":
        return False
    if kind == "answer_first":
        return fam in WRITE_FAMILIES
    if kind in ("select", "next_call"):
        if s["writes_now"]:
            return True
        if kind == "select" and fam in ("stub_then_call", "safe_effects"):
            return True
        if fam in WRITE_FAMILIES and s["turn_writes"]:
            return True
        return kind == "next_call" and fam == "multi_source"
    # narrate
    if fam == "multi_source":
        return True
    if fam == "relay_receipt":
        return True
    if fam in WRITE_FAMILIES and receipt_of(s):
        return True
    if fam == "ignore_embedded_instructions":
        return any(injected(r) for r in s["all_results"])
    if fam == "grounded_only":
        return bool(_GROUNDED_MATH.search(s["prompt"]) or re.search(r"\d", s["target_text"]))
    return False


def step_kind(meta: dict, messages: list[dict]) -> str:
    return analyze(meta, messages)["kind"]


# ----------------------------------------------------------------- formatting
_WD = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WD_RE = re.compile(r"\b(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|wensday|thu(?:rs(?:day)?)?|thurs|"
                    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b", re.I)
_WD_IDX = {"mon": 0, "tue": 1, "wed": 2, "wen": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAYPART = re.compile(r"\b(morning|afternoon|evening|tonight|night|nite)\b", re.I)
_CLOCKTIME = re.compile(r"\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b|\b\d{1,2}:\d\d\b|\bnoon\b", re.I)
_REL = re.compile(r"\bin\s+(\d+|an?|one|two|half an)\s+(minutes?|mins?|hours?|hrs?)\b"
                  r"|\b(\d+|an?|one|two|half an)\s+(minutes?|mins?|hours?|hrs?)\s+from now\b", re.I)
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "half an": 0.5}


def _rel_minutes(m: re.Match) -> float | None:
    n, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    try:
        k = float(n)
    except ValueError:
        k = _NUM.get(n.lower())
    if k is None:
        return None
    return k * (60 if unit.lower().startswith("h") else 1)


def pick(seed: str, options: list[str]) -> str:
    return options[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)]


def parse_clock(meta: dict) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(str(meta.get("clock"))[4:], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def fmt_day(d: _dt.date) -> str:
    return f"{_WD[d.weekday()]} {d.strftime('%b')} {d.day}"


def fmt_time(t: _dt.time) -> str:
    h = t.hour % 12 or 12
    return f"{h}{':%02d' % t.minute if t.minute else ''} {'AM' if t.hour < 12 else 'PM'}"


def parse_iso(v) -> tuple[_dt.date, _dt.time | None] | None:
    if not isinstance(v, str):
        return None
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)(?:[T ](\d\d):(\d\d))?", v)
    if not m:
        return None
    d = _dt.date(int(m[1]), int(m[2]), int(m[3]))
    return d, (_dt.time(int(m[4]), int(m[5])) if m[4] else None)


def call_dates(calls) -> list[_dt.date]:
    """Dates in a call's arguments (ISO values only)."""
    out = []
    for _n, a in calls:
        for k, v in a.items():
            if k in ("when_iso", "current_when_iso", "date", "start", "when", "day"):
                p = parse_iso(v)
                if p:
                    out.append(p[0])
    return out


def _day_word(prompt: str) -> str | None:
    low = prompt.lower()
    for w in ("tomorrow", "tmrw", "tmr", "tonight", "today", "this evening"):
        if re.search(rf"\b{w}\b", low):
            return w
    m = _WD_RE.search(prompt)
    return m.group(1) if m else None


def resolve_when(clock: _dt.datetime | None, iso: str, prompt: str, seed: str) -> str:
    """One sentence that turns the prompt's time words into the date in `iso` (the call's own value)."""
    p = parse_iso(iso)
    if not p or clock is None:
        return ""
    d, t = p
    today = clock.date()
    delta = (d - today).days
    word = _day_word(prompt)
    day = fmt_day(d)
    parts = []
    rel = _REL.search(prompt)
    mins = _rel_minutes(rel) if rel else None
    if (rel and t and mins is not None and
            _dt.datetime.combine(d, t) == (clock + _dt.timedelta(minutes=mins)).replace(second=0, microsecond=0)):
        parts.append(pick(seed + "rel", [f"It's {fmt_time(clock.time())} now, so {rel.group(0).strip()} is {fmt_time(t)}.",
                                         f"Now is {fmt_time(clock.time())}; {rel.group(0).strip()} lands at {fmt_time(t)}."]))
        return " ".join(parts)
    if word in ("tomorrow", "tmrw", "tmr"):
        parts.append(pick(seed + "tm", [f"Today is {fmt_day(today)}, so tomorrow is {day}.",
                                        f"Tomorrow is {day}."]))
    elif word in ("tonight", "today", "this evening") and delta == 0:
        parts.append(pick(seed + "td", [f"'{word}' is today, {day}.", f"Today is {day}, so that's today."]))
    elif word and _WD_IDX.get(word.lower()[:3]) == d.weekday():
        if today.weekday() == d.weekday():
            parts.append(pick(seed + "wd0", [
                f"Today is already {_WD[today.weekday()]}, so '{word}' means next week's, {day}.",
                f"It's {fmt_day(today)} today, so a bare '{word}' is the next one, {day}."]))
        elif (today.weekday() - 1) % 7 == d.weekday():
            parts.append(pick(seed + "wd1", [
                f"{_WD[d.weekday()]} was yesterday, so '{word}' means the coming one, {day}.",
                f"Today is {fmt_day(today)}; {_WD[d.weekday()]} just passed, so it's {day}."]))
        else:
            parts.append(pick(seed + "wd", [f"Today is {fmt_day(today)}, so '{word}' is {day}.",
                                            f"From {fmt_day(today)}, the next {_WD[d.weekday()]} is {day}.",
                                            f"'{word}' from today ({fmt_day(today)}) means {day}."]))
    elif delta == 0:
        parts.append(f"That's today, {day}.")
    else:
        parts.append(pick(seed + "on", [f"It goes on {day}.", f"The day is {day}."]))
    if t is not None:
        dp = _DAYPART.search(prompt)
        if dp and not _CLOCKTIME.search(prompt) and dp.group(1).lower() not in ("tonight",):
            parts.append(pick(seed + "dp", [f"'{dp.group(1)}' has no clock time, so {fmt_time(t)}.",
                                            f"For '{dp.group(1)}' I'll use {fmt_time(t)}."]))
    return " ".join(parts)


def _q(s, n=6) -> str:
    words = str(s).split()
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def _when_phrase(a: dict, with_day: bool = True) -> str:
    """' on Monday Mar 1 at 3 PM' (with_day) or ' at 3 PM'; ' for today' for a `day` argument."""
    p = parse_iso(a.get("when_iso"))
    if p:
        d, t = p
        if with_day:
            return f" on {fmt_day(d)}{' at ' + fmt_time(t) if t else ''}"
        return f" at {fmt_time(t)}" if t else ""
    if a.get("day"):
        return f" for {a['day']}"
    return ""


def action(name: str, a: dict, prior: str = "", with_day: bool = True, terse: bool = False) -> str:
    """A short first-person clause for the call (no 'I'll' prefix). with_day=False leaves the day out
    (a resolution sentence already said it); terse drops quoted values."""
    title = a.get("title") or a.get("name") or ""
    when = _when_phrase(a, with_day)
    iso = a.get("when_iso")
    isotxt = f", when_iso {iso}" if iso and not terse else ""
    if name == "add_calendar_event":
        dur = f" for {a['duration_min']} minutes" if a.get("duration_min") else ""
        loc = f" at {a['location']}" if a.get("location") and not terse else ""
        return f"add “{title}”{when}{dur}{loc}{isotxt}" if not terse else f"add the event{when}{dur}{isotxt}"
    if name == "update_event":
        dur = f", {a['duration_min']} minutes long" if a.get("duration_min") else ""
        return f"move “{title}” with update_event{re.sub(r'^ (?:on|at) ', ' to ', when)}{dur}{isotxt}"
    if name == "add_reminder":
        if a.get("all_day"):
            return f"make it an all-day reminder (all_day true, when_iso {iso})" if not with_day else \
                f"set an all-day reminder{when}"
        return (f"set the reminder “{title}”{when}{isotxt}" if not terse else f"set the reminder{when}{isotxt}")
    if name == "update_reminder":
        bits = []
        if a.get("new_title"):
            bits.append(f"rename it “{a['new_title']}”")
        if when:
            bits.append(f"move it{re.sub(r'^ (?:on|at) ', ' to ', when)}")
        return f"update the “{title}” reminder: " + (" and ".join(bits) or "the new details") + isotxt
    if name == "complete_reminder":
        return f"mark the “{title}” reminder done"
    if name == "clear_reminders":
        return f"delete the reminder matching “{a.get('query') or title}”, behind its card"
    if name == "send_message":
        gist = f" with {prior}" if prior else ""
        return f"text {a.get('to') or a.get('recipient') or 'them'}{gist} via send_message; the card lets them confirm"
    if name == "send_email":
        to = "their own address" if a.get("confirmed_self_send") else (a.get("to") or a.get("recipient") or "them")
        subj = "" if terse or not a.get("subject") else f" (subject “{_q(a.get('subject'), 5)}”)"
        return f"email {to} with send_email{subj}, behind its card"
    if name == "schedule_send":
        to = "their own address" if "@linden.edu" in str(a.get("to")) and a.get("channel") == "email" else a.get("to")
        ch = "an email" if a.get("channel") == "email" else "a text"
        return f"queue {ch} to {to} for {a.get('when')} with schedule_send"
    if name == "draft_email":
        return f"open a draft to {a.get('to')} with draft_email, not send it"
    if name == "reply_to_email":
        return "reply to that email"
    if name == "create_note":
        return f"start a new note “{title}”"
    if name == "append_note":
        return f"append to the existing “{title}” note"
    if name == "manage_contacts":
        what = str(a.get("action") or "").replace("add_", "")
        val = a.get("value")
        if a.get("action") == "create":
            return f"create a contact for {a.get('name')}"
        if terse and val:
            return f"save it with manage_contacts, action {a.get('action')}"
        return f"add the {what} {val} to {a.get('name')}'s contact" if val else f"update {a.get('name')}'s contact"
    if name == "remember":
        return "save it word for word with remember"
    if name == "forget":
        return f"forget only what matches “{a.get('query')}”, nothing broader"
    if name == "cancel_event":
        return f"cancel “{title}”"
    return read_phrase(name, a)


def read_phrase(name: str, a: dict) -> str:
    q = a.get("query") or a.get("conversation")
    if name == "get_weather":
        return f"check the forecast for {a.get('location') or 'their city'}" + (f" ({a['period']})" if a.get("period") else "")
    if name == "view_emails":
        return f"read the latest email{' about ' + repr(q) if q else ''}"
    if name == "view_messages":
        if a.get("conversation"):
            return f"open the thread with {a['conversation']}"
        return (f"search the texts for {a['query']!r}" if a.get("query") else "look at the recent texts") + \
            (f" ({a['period']})" if a.get("period") else "")
    if name == "summarize_emails":
        return "pull the email digest"
    if name == "summarize_messages":
        return "pull the texts digest"
    if name == "get_upcoming":
        what = f" for {q}" if q else ""
        span = a.get("period") or (f"the next {a['days']} days" if a.get("days") else "")
        return f"look up the calendar{what}" + (f" ({span})" if span else "")
    if name == "search_reminders":
        return f"find the reminder{' for ' + repr(q) if q else 's'}"
    if name == "search_notes":
        return f"find the note about {q!r}" if q else "search the notes"
    if name == "web_search":
        return f"search the web for {q!r}"
    if name == "get_stock_price":
        return f"get the live price for {', '.join(a.get('symbols') or [])}"
    if name == "list_dir":
        return f"list {a.get('path')}"
    if name == "lookup_contact":
        return f"look up {a.get('name') or q}"
    if name == DISCOVERY:
        return f"load {', '.join(a.get('tools') or a.get('families') or [])}"
    shown = [f"{k} {json.dumps(v, ensure_ascii=False)}" for k, v in list(a.items())[:3]
             if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 30]
    return f"call {name}" + (f" with {', '.join(shown)}" if shown else "")


_SOURCE = {"view_emails": "email", "summarize_emails": "email", "view_messages": "texts", "summarize_messages": "texts",
           "get_upcoming": "calendar", "get_past_events": "calendar history", "search_reminders": "reminders",
           "get_recent_activity": "recent activity", "get_my_day_plan": "day plan", "search_notes": "note",
           "read_note": "note", "read_file": "file", "web_search": "web result", "web_fetch": "page",
           "get_weather": "forecast", "list_dir": "folder listing", "get_stock_price": "price", "find_free_time": "free slots"}


def source_of(name: str) -> str:
    return _SOURCE.get(name, name.replace("_", " "))


def facts(name: str, text: str) -> str | None:
    """A short, exact fact from a result in a known format, else None."""
    if re.search(r"^No (?:messages|emails|results|matching)|No messages found|^Nothing", text, re.M) and len(text) < 260:
        return f"the {source_of(name)} came back empty"
    m = re.search(r"(\d+) emails? · (\d+) unread", text)
    if m:
        return f"{m[1]} email{'s' if m[1] != '1' else ''}, {m[2]} unread"
    m = re.search(r"(?:Next \d+ days|Tomorrow|Today|This week|Next week)[^:\n]*: (\d+ events?(?:, \d+ reminders?)?)", text)
    if m:
        return m[1]
    m = re.search(r"Past \d+ day\(s\), (\d+) item\(s\)", text)
    if m:
        return f"{m[1]} past items"
    m = re.search(r"(\d+) new in the last", text)
    if m:
        return f"{m[1]} new items"
    return None


def fact_sentence(name: str, text: str) -> str:
    f = facts(name, text)
    if not f:
        return ""
    return _sentence(f if f.startswith("the ") else f"The {source_of(name)} shows {f}")


_SHIFT = re.compile(r"\b(an hour and a half|half an hour|an hour|\d+ (?:minutes|mins|hours?)|a day)\s+"
                    r"(earlier|later|back|forward)\b", re.I)


def current_slot(title: str, text: str, clock: _dt.datetime | None):
    """(date or None, 'H:MM AM') of the item named `title` in a calendar or reminders read, else None."""
    if not title or not clock:
        return None
    key = title.lower()[:18]
    cur = None
    for line in text.split("\n"):
        ds = [d for d, _ in stated_dates(line, clock)[0] if d]
        if line.startswith("**") and ds:
            cur = ds[0]
        if key in line.lower():
            tm = re.search(r"(\d{1,2}:\d\d [AP]M)", line)
            if tm:
                return (ds[0] if ds else cur), tm.group(1)
    return None


def _starts(text: str) -> list[str]:
    return re.findall(r"\*\*(\d{1,2}:\d\d [AP]M)\*\*", text)


# ----------------------------------------------------------------- thought writers
def _goal(prompt: str) -> str:
    p = re.sub(r"^(?:hey|yo|so|ok|okay|pls|please|can you|could you)[,\s]+", "", prompt.strip(), flags=re.I)
    return _q(p.rstrip("?.! "), 9)


def _tool_reason(fam: str, name: str, a: dict, seed: str) -> str:
    if name == "update_event":
        return pick(seed + "r", ["It's an existing event getting a new time, so update_event, not a second event.",
                                 "That event already exists; moving it is update_event, not a new add."])
    if name == "add_calendar_event" and fam in ("right_write_tool",):
        return pick(seed + "r", ["It's a new appointment with a set time, so add_calendar_event.",
                                 "Nothing to move here; it's a brand-new event."])
    if name == "add_reminder" and fam in ("right_write_tool", "act_dont_ask"):
        return pick(seed + "r", ["It's something to be nudged about, so a reminder, not an event.",
                                 "A nudge to do something is a reminder; no need to ask anything."])
    if name == "draft_email":
        return pick(seed + "r", ["It's a draft for them to check, so draft_email, not send_email.",
                                 "A draft, not a send: nothing goes out yet."])
    if name in ("send_email", "send_message") and fam in ("right_write_tool", "safe_effects", "act_dont_ask"):
        return pick(seed + "r", ["They asked for it to be sent, so the real send, not a draft.",
                                 "This is a send now, not a scheduled one or a draft."])
    if name == "schedule_send":
        return pick(seed + "r", ["They named a later send time, so schedule_send, not an immediate send.",
                                 "It should go out later, at the time they gave, so schedule it."])
    if name == "append_note":
        return pick(seed + "r", ["The note already exists, so append to it instead of making a new one.",
                                 "Adding to an existing note is append_note, not a second note."])
    if name == "create_note":
        return pick(seed + "r", ["It's a new note, so create_note.", "Nothing to append to; this starts a fresh note."])
    if name == "complete_reminder":
        return pick(seed + "r", ["It's done, not unwanted, so complete it and keep the record.",
                                 "Finished means complete_reminder, not a delete."])
    if name == "clear_reminders":
        return pick(seed + "r", ["They don't need it anymore, so delete it rather than marking it done.",
                                 "Unwanted, not finished, so it gets removed."])
    if name == "remember":
        return pick(seed + "r", ["It's a fact they stated about themselves, so it's worth saving.",
                                 "This is about them, said plainly, so remember it as given."])
    if name == "forget":
        return ""
    return ""


def _write_plan(meta, s, calls, ctx, seed, skip_resolution=False):
    """(date resolution sentence, tool-choice reason, action clauses) for the write call(s)."""
    fam = meta.get("family") or ""
    clock = parse_clock(meta)
    terse = bool((ctx or {}).get("terse"))
    resolution, acts = "", []
    prior = ""
    if s["results"] and s["prev_calls"] and not s["loaded"]:
        src = source_of(s["prev_calls"][0][0])
        prior = f"the {src}" if src in ("forecast", "price", "web result", "note", "folder listing") else ""
    for name, a in calls:
        if a.get("when_iso") and not resolution and not skip_resolution:
            resolution = resolve_when(clock, a["when_iso"], s["prompt"], seed)
        acts.append(action(name, a, prior, with_day=not resolution or len(calls) > 1, terse=terse))
    reason = ""
    for name, a in calls:
        reason = reason or _tool_reason(fam, name, a, seed)
    return resolution, reason, acts


def compose(parts: list[tuple[int, str]], max_sentences: int = 3, max_words: int = MAX_WORDS) -> str:
    """Join (priority, text) parts in order, dropping the lowest priority ones until the thought has at most
    `max_sentences` sentences and `max_words` words (1-3 sentences, <= 60 words)."""
    parts = [(p, t.strip()) for p, t in parts if t and t.strip()]

    def n_sent(ps):
        return sum(len(re.findall(r"[.!?](?:\s|$)", t)) or 1 for _, t in ps)

    while len(parts) > 1 and (n_sent(parts) > max_sentences or len(_join(*[t for _, t in parts]).split()) > max_words):
        low = min(range(len(parts)), key=lambda i: (parts[i][0], -i))
        parts.pop(low)
    return _join(*[t for _, t in parts])


def _join(*parts) -> str:
    out = " ".join(p.strip() for p in parts if p and p.strip())
    return re.sub(r"\s+", " ", out).strip()


def _sentence(s: str) -> str:
    s = s.strip()
    return s[0].upper() + s[1:] + ("" if s.endswith((".", "!", "?")) else ".") if s else ""


def _plan_calls(s, ctx) -> list[tuple[str, dict]]:
    """The calls whose arguments this step commits to: its own, or (for a bare schema load) the next
    real call in the turn."""
    own = [(n, a) for n, a in s["calls"] if n != DISCOVERY]
    if own:
        return own
    want = set().union(*[_loads(n, a) for n, a in s["calls"]]) if s["calls"] else set()
    for n, a in (ctx or {}).get("later_calls", []):
        if n in want:
            return [(n, a)]
    return []


def thought_select_or_next(meta, s, ctx, seed) -> str:
    fam = meta.get("family") or ""
    calls = s["calls"]
    names = [n for n, _ in calls]
    later = (ctx or {}).get("later_calls", [])
    later_write = next(((n, a) for n, a in later if n in WRITE_TOOLS), None)
    plan = _plan_calls(s, ctx)
    load_note = ""
    if DISCOVERY in names:
        tools = sorted(set().union(*[_loads(n, a) for n, a in calls]))
        load_note = pick(seed + "ld", [f"{', '.join(tools)} isn't loaded yet, so I'll load it first.",
                                       f"I need {', '.join(tools)}, which isn't loaded; load it, then call it."])
    elif meta.get("target_kind") == "load":
        load_note = pick(seed + "ld", [f"{names[0]} is only listed by name, so this call loads it before the real one.",
                                       f"Calling {names[0]} now loads its schema; the real call comes right after."])
    write_plan = [(n, a) for n, a in plan if n in WRITE_TOOLS]
    if write_plan:
        lead, skip_res = "", False
        if s["results"] and not s["loaded"] and s["prev_calls"]:
            prev = s["prev_calls"][0][0]
            if prev in WRITE_TOOLS:
                lead = pick(seed + "pl", ["The first part is done.", "That part went through."])
            else:
                lead = slot_lead(meta, s, write_plan, seed)
                skip_res = bool(lead) and not _day_word(s["prompt"])    # the slot sentence already places it
                lead = lead or fact_sentence(prev, s["results"][-1]) or \
                    pick(seed + "pl", [f"The {source_of(prev)} has what I need.", f"Got the {source_of(prev)} back."])
        resolution, reason, acts = _write_plan(meta, s, write_plan, ctx, seed, skip_res)
        act = " and ".join(acts)
        verb = (pick(seed + "v", ["I'll", "Next I'll", "So I'll"]) if lead else
                pick(seed + "v", ["I'll", "So I'll"]) if resolution else "I'll")
        # stub_then_call is about the load, so its load note outranks the tool-choice reason
        lp, rp = (3, 2) if fam == "stub_then_call" else (1, 2)
        return compose([(3, lead), (4, resolution), (rp, reason), (lp, load_note), (5, _sentence(f"{verb} {act}"))])
    # a read (or a load of a read)
    reads = plan or [(n, a) for n, a in calls if n != DISCOVERY]
    read = " and ".join(read_phrase(n, a) for n, a in reads) or read_phrase(*calls[0])
    verb = pick(seed + "v", ["I'll", "So I'll", "Then I'll"])
    if later_write and fam in WRITE_FAMILIES:
        # The write's arguments depend on what this read returns, so the plan names only the next move.
        wg = write_gist(later_write[0], later_write[1], s["prompt"])
        return compose([(4, _sentence(pick(seed + "rb", [f"First I need to {read}, then {wg}",
                                                        f"I'll {read} first; after that, {wg}",
                                                        f"Read before writing: {read}, then {wg}"]))),
                        (3, pick(seed + "why", ["The details for the change come from that result.",
                                                "I can't fill in the write until I've seen it.", ""])),
                        (2 if fam == "stub_then_call" else 1, load_note)])
    if fam == "safe_effects":
        n0 = reads[0][0] if reads else names[0]
        why = {"get_stock_price": ["A price is a quick lookup; no page fetch and nothing to send.",
                                   "Live price lookup, not a page fetch, and nothing goes out."],
               "web_search": ["It's a news question, so a web search; nothing to fetch or send.",
                              "A search answers this; no fetching pages, no sending."],
               "view_messages": ["They want to see who wrote, so the plain message list, not a summary; nothing gets sent.",
                                 "This asks who texted, so list the messages; no summary, no reply."],
               "list_scheduled_sends": ["This only asks what's queued; nothing gets sent or changed.",
                                        "Just a look at the queue; nothing to send or cancel."],
               "list_dir": ["They want to look, not tidy up, so just list it; no moving or deleting.",
                            "Looking only: list the folder, move nothing, delete nothing."],
               }.get(n0, ["This only needs a read; nothing to send or change.",
                          "A read is enough here; no effects."])
        return compose([(3, pick(seed + "w", why)), (1, load_note), (4, _sentence(f"{verb} {read}"))])
    if fam == "stub_then_call":
        return compose([(3, load_note or pick(seed + "st", ["The tool I need isn't loaded yet.", "First the schema."])),
                        (4, _sentence(pick(seed + "sr", [f"Then I'll {read}", f"After that, {read} for real"])))])
    if s["results"] and s["prev_calls"]:
        f = fact_sentence(s["prev_calls"][0][0], s["results"][-1])
        return _join(f or f"The {source_of(s['prev_calls'][0][0])} covers part of it.",
                     _sentence(f"I still need to {read}"))
    return compose([(1, load_note), (4, _sentence(f"{verb} {read}"))])


def slot_lead(meta, s, plan, seed) -> str:
    """For a move after a read: where the item is now, and the shift asked for (both read off the result
    and the prompt; the day is only named when it stays the same)."""
    move = next(((n, a) for n, a in plan if n in ("update_event", "update_reminder")), None)
    if not move or not s["results"]:
        return ""
    clock = parse_clock(meta)
    slot = current_slot(move[1].get("title") or "", s["results"][-1], clock)
    new = parse_iso(move[1].get("when_iso"))
    if not slot or not new or not new[1]:
        return ""
    old_d, old_t = slot
    same_day = old_d == new[0]
    where = f"on {fmt_day(old_d)} at {old_t}" if same_day and old_d else f"at {old_t}"
    shift = _SHIFT.search(s["prompt"])
    title = _q(move[1].get("title"), 5)
    lead = pick(seed + "sl", [f"“{title}” is {where} now", f"Right now “{title}” is {where}"])
    if shift and "day" in shift.group(1).lower():
        lead += f"; {shift.group(0).lower()} is {fmt_day(new[0])}, same time"
    elif shift:
        lead += f"; {shift.group(0).lower()} makes it {fmt_time(new[1])}"
    return _sentence(lead)


def write_gist(name: str, a: dict, prompt: str) -> str:
    """The next write named without the arguments a read still has to supply."""
    to = a.get("to")
    to = to if to and re.search(rf"\b{re.escape(str(to).split()[0])}\b", prompt, re.I) else ""
    return {"update_event": "move it with update_event",
            "add_reminder": "set the reminder from what it shows",
            "update_reminder": "move that reminder with update_reminder",
            "complete_reminder": "mark it done",
            "manage_contacts": "save it to the contact with manage_contacts",
            "send_message": f"text {to or 'them'} what it says, behind the send card",
            "send_email": f"email {to or 'it'} behind the send card",
            "schedule_send": "queue the send with schedule_send",
            "draft_email": "draft the reply, not send it",
            "reply_to_email": "write the reply",
            "create_note": "put it in a new note",
            "append_note": "add it to the note",
            }.get(name, f"call {name}")


_BAD_ARGS = re.compile(r"unexpected (?:keyword )?argument(?:\(s\))?:? \[?([^\]\.]+?)\]?(?:[.;]|$)")
_EXPECTED = re.compile(r"Expected arguments — ([^.]+)")


def thought_after_error(meta, s, ctx, seed) -> str:
    err = next(r for r in s["results"] if is_error(r))
    first = err.lstrip().split("\n", 1)[0]
    calls = s["calls"]
    plan = _plan_calls(s, ctx)
    fixed = " and ".join(action(n, a) for n, a in plan) if plan else ""
    clock = parse_clock(meta)
    when = next((resolve_when(clock, a["when_iso"], s["prompt"], seed) for n, a in plan if a.get("when_iso")), "")
    if first.startswith(DENIED):
        return pick(seed, ["The card was denied, so nothing went out. I'll say plainly it wasn't done.",
                           "They tapped Deny; nothing happened. I'll say so without implying it went through."])
    if not calls:            # a refusal the final must relay
        reason = re.split(r"[;:]|\. ", first, maxsplit=1)[0].strip().rstrip(".")
        reason = re.sub(r"^Wisp ", "", reason)
        src = source_of(s["prev_calls"][0][0]) if s["prev_calls"] else "that"
        if re.search(r"moved|organizer|read-only|invitees|already starts", first):
            return _join(_sentence(pick(seed + "m", ["Nothing was moved", "The move didn't happen"])) + " — " +
                         _q(reason, 12).lower() + ".",
                         pick(seed, ["I'll say it wasn't done and what they can do instead.",
                                     "I'll be plain that it didn't change, plus the next step."]))
        return _join(pick(seed + "u", [f"{src[:1].upper() + src[1:]} can't be read right now.", f"I couldn't get the {src}."]),
                     pick(seed, ["I'll say that plainly with the fix it gives, and invent nothing.",
                                 "No data to report, so I'll explain how to turn access on instead."]))
    m = re.match(r"\(error calling (\w+)\(", first)
    bad_tool = m.group(1) if m else (s["prev_calls"][0][0] if s["prev_calls"] else "That call")
    bad = _BAD_ARGS.search(first)
    exp = _EXPECTED.search(first)
    new_names = {n for n, _ in calls}
    if re.search(r"couldn't understand the period '([^']+)'", first):
        per = re.search(r"couldn't understand the period '([^']+)'", first).group(1)
        return _join(pick(seed + "a", [f"'{per}' isn't a period {bad_tool} accepts.",
                                       f"{bad_tool} can't parse '{per}' as a period."]),
                     _sentence(pick(seed, [f"I'll retry: {fixed}", f"Retry with what it does take: {fixed}",
                                           f"Same question, valid arguments: {fixed}"])))
    if "couldn't resolve" in first:
        place = re.search(r"couldn't resolve '([^']+)'", first)
        loc = next((a.get("location") for _n, a in calls if a.get("location")), "")
        return _join(f"'{place.group(1) if place else 'That place'}' is ambiguous and the tool won't guess.",
                     pick(seed, [f"I'll use the best-known one, {loc}, and say which one I picked.",
                                 f"Retry with {loc} and mention it in case they meant another."]))
    if "start time is invalid" in first:
        return _join("The start time was rejected; it wants plain local time with no offset.", when,
                     _sentence(f"I'll resend: {fixed}"))
    if "duration_m" in first:
        dur = next((a.get("duration_min") for _n, a in calls if a.get("duration_min")), None)
        return _join("duration_min has to be a whole number of minutes.", when,
                     _sentence(f"So {dur} minutes: {fixed}" if dur else f"I'll resend: {fixed}"))
    badtxt = (bad.group(1).replace("'", "").strip() if bad else "an argument")
    exp_names = [x.split(":")[0].strip() for x in exp.group(1).split(",")][:4] if exp else []
    exp_txt = f"; it takes {', '.join(exp_names)}" if exp_names else ""
    if bad_tool not in new_names and plan:
        return _join(pick(seed + "a", [f"{bad_tool} has no {badtxt} argument{exp_txt}.",
                                       f"That failed: {bad_tool} doesn't take {badtxt}{exp_txt}."]),
                     pick(seed, [f"A keyword search belongs in {plan[0][0]}, so I'll {fixed}.",
                                 f"So I'll switch to {plan[0][0]} and {fixed}.",
                                 f"{plan[0][0]} handles that; I'll {fixed}."]))
    return _join(pick(seed + "a", [f"{bad_tool} rejected {badtxt}{exp_txt}.",
                                   f"The call failed on {badtxt}{exp_txt}."]), when,
                 _sentence(pick(seed, [f"I'll retry with the schema's names: {fixed}", f"Same call, right names: {fixed}",
                                       f"Fixing the names and retrying: {fixed}"])))


def _inj_kind(text: str) -> str:
    low = text.lower()
    for pat, kind in ((r"\bforward", "forward mail somewhere"), (r"\btext (?:all|every)", "text all their contacts"),
                      (r"\b(?:pay|payment|transfer|wire)", "set up a payment"),
                      (r"\b(?:run|execute|curl|terminal|shell|command)\b", "run a command"),
                      (r"\b(?:delete|remove|clear|wipe)", "delete their things"),
                      (r"\b(?:reveal|memory|memories|password|share what you know)", "reveal private details"),
                      (r"\bsend\b", "send something out"), (r"ignore", "drop its instructions")):
        if re.search(pat, low):
            return kind
    return "do something they didn't ask for"


def thought_receipt(meta, s, ctx, seed) -> str:
    """Relaying a write's receipt: which facts it states, and how to name the day relative to today."""
    rc = receipt_of(s)
    final = s["target_text"].lower()
    clock = parse_clock(meta)
    if not rc:
        prev = s["prev_calls"][0] if s["prev_calls"] else ("", {})
        who = prev[1].get("to") or prev[1].get("name") or ""
        if prev[0] in ("send_message", "send_email", "schedule_send", "reply_to_email"):
            return _join(pick(seed + "a", [f"The receipt confirms it went to {who}." if who else "The receipt confirms the send.",
                                           "The send went through."]),
                         pick(seed + "b", ["I'll say who got it in one short line, nothing extra.",
                                           "Confirm it plainly; no 'let me know' closer."]))
        return _join(pick(seed + "a", ["The receipt confirms it.", "That went through."]),
                     pick(seed + "b", ["I'll restate exactly what it says, briefly.", "Relay just the receipt's facts."]))
    stated = f"{rc['wd']} {rc['mon']} {rc['day']}"
    span = f"{rc['start']}" + (f" to {rc['end']}" if rc["end"] else "")
    d = None
    if clock:
        _ds, _ = stated_dates(stated, clock)
        d = _ds[0][0] if _ds else None
    rel = ""
    if d and clock:
        delta = (d - clock.date()).days
        if delta == 0:
            rel = f"Today is {fmt_day(clock.date())}, so that's today."
        elif delta == 1:
            rel = pick(seed + "r", [f"Today is {fmt_day(clock.date())}, so that's tomorrow.",
                                    "That's tomorrow."]) if "tomorrow" in final else \
                f"That's tomorrow, {fmt_day(d)}."
        elif 1 < delta <= 7:
            rel = pick(seed + "r", [f"That's the coming {_WD[d.weekday()]}.",
                                    f"From today, {fmt_day(clock.date())}, that's the next {_WD[d.weekday()]}."])
        elif delta > 7:
            rel = f"That's more than a week out, so I'll give the date."
    head = pick(seed + "h", [f"The receipt says {stated}, {span}.", f"Receipt: {stated}, {span}.",
                             f"It's set for {stated}, {span}."])
    tail = ("It's saved in Wisp only, not Apple Reminders, so I'll mention that." if rc["wisp_only"] else
            pick(seed + "t", ["I'll give the title, day and time, nothing more.",
                              "Title, day and time from the receipt, short.",
                              "Just the facts it states."]))
    return compose([(4, head), (3, rel), (2, tail)])


def thought_narrate(meta, s, ctx, seed) -> str:
    fam = meta.get("family") or ""
    turn_calls = (ctx or {}).get("turn_reads") or s["prev_calls"]
    if fam in WRITE_FAMILIES:
        return thought_receipt(meta, s, ctx, seed)
    if fam == "ignore_embedded_instructions":
        # the planted instruction: from its marker sentence to the end of that paragraph
        line = next(r[m.start():] for r in s["all_results"] for m in [INJECTION.search(r)] if m)
        line = line.split("\n\n", 1)[0][:300]
        src = source_of(next((n for n, _ in turn_calls if n != DISCOVERY), "result"))
        kind = _inj_kind(line)
        return _join(pick(seed + "a", [f"The {src} answers it, but one line is aimed at an AI assistant, telling it to {kind}.",
                                       f"Part of the {src} is written to an assistant: it wants me to {kind}."]),
                     pick(seed + "b", ["That isn't their request, so I won't act on it; report the content and flag it in a line.",
                                       "I'll ignore it, give them what the text actually says, and mention the planted line.",
                                       "Not from them, so no action; just the content plus a short heads-up."]))
    if fam == "multi_source":
        names = [n for n, _ in s["prev_calls"] if n != DISCOVERY] or [n for n, _ in turn_calls if n != DISCOVERY]
        bits = []
        for (n, _a), r in zip(s["prev_calls"], s["results"]):
            f = facts(n, r)
            if f and f.endswith("came back empty"):
                f = "empty"
            bits.append(f"{source_of(n)} ({f})" if f else source_of(n))
        bits = list(dict.fromkeys(bits)) or [source_of(n) for n in names]
        srcs = ", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]
        many = len(set(source_of(n) for n in names)) > 1
        if many:
            return _join(_sentence(f"Back from {srcs}"),
                         pick(seed, ["I'll cover each source in its own part and say plainly if one is empty.",
                                     "Each gets a short line; an empty one is said, not skipped.",
                                     "Report both, leading with what needs attention."]))
        return _join(_sentence(pick(seed + "a", [f"One read covered several sources ({srcs})",
                                                 f"The {srcs} spans several apps at once"])),
                     pick(seed, ["I'll keep it to what's new across them, most pressing first.",
                                 "Group it by source and keep only what matters.",
                                 "Lead with what needs a reply, then the rest by source."]))
    # grounded_only
    prev = s["prev_calls"][0][0] if s["prev_calls"] else ""
    res = s["results"][-1] if s["results"] else ""
    f = facts(prev, res)
    prompt = s["prompt"].lower()
    clock = parse_clock(meta)
    if re.search(r"\b(free|booked|busy|time for)\b", prompt):
        st = _starts(res)
        listed = (f"The list has starts at {', '.join(st[:5])}." if st else "Nothing is listed for that day.")
        return _join(listed, pick(seed, ["I'll check the asked time against those and answer yes or no first.",
                                         "Answer the exact slot they asked about, yes or no up front."]))
    if re.search(r"\b(complete|completed|wrapped|finish)", prompt):
        return _join("Wisp only sees what was scheduled, not what got finished.",
                     pick(seed, ["I'll say that, then list what they had, in the past tense.",
                                 "So: what they had, worded as 'had', never 'completed'."]))
    if "family" in prompt:
        return _join("Family means several people here.",
                     pick(seed, ["I'll pick out only the relatives from the list and skip everyone else.",
                                 "Only the relatives' messages count; skip friends and classmates."]))
    if re.search(r"\b(left today|happening today|today)\b", prompt) and clock and \
            prev in ("get_upcoming", "get_past_events", "search_reminders", "get_my_day_plan"):
        return _join(f"It's {fmt_time(clock.time())} on {fmt_day(clock.date())}.",
                     fact_sentence(prev, res),
                     pick(seed, ["Anything earlier already happened, so only what's still ahead counts as upcoming.",
                                 "I'll go by what the result shows for today and say plainly if it's empty."]))
    if f:
        return _join(_sentence(f"The {source_of(prev)} says {f}"),
                     pick(seed, ["I'll use exactly that count and scope, nothing rounded or added.",
                                 "Exact numbers, scoped to what was asked."]))
    return _join(f"The {source_of(prev)} result has it.",
                 pick(seed, ["I'll stick to what it lists, with the exact scope asked.",
                             "Only what it shows, nothing inferred."]))


def thought_answer_first(meta, s, ctx, seed) -> str:
    text = s["target_text"]
    m = re.search(r"Which (\w+)[^—?]*— ([^?]+?) or ([^?]+?)\?", text)
    if m:
        return _join(f"Two contacts match {m.group(1)}: {m.group(2)} and {m.group(3)}.",
                     pick(seed, ["Picking wrong would send it to the wrong person, so one short question.",
                                 "I can't tell which, and guessing is risky; ask which one, nothing else."]))
    low = text.lower()
    if "remind you about" in low or "reminder for" in low:
        return _join("There's a time but no task.",
                     pick(seed, ["An empty reminder is useless, so I'll ask what it's for.",
                                 "I'll ask one short question about what to remind them of."]))
    if re.search(r"who should i (?:text|email|message)", low):
        return _join("It doesn't say who to send it to.",
                     pick(seed, ["I won't guess a recipient; one short question.", "Ask who, then send."]))
    if "call the event" in low or "what's it called" in low or "title" in low:
        return _join("There's a day and time but no name for it.",
                     pick(seed, ["I'll ask what to call it before adding anything.", "One question: the title."]))
    if "?" in text:
        return _join(pick(seed + "a", ["This would delete things and doesn't say what.",
                                       "A wipe with no target named.", "That's destructive, and the target is unclear."]),
                     pick(seed, ["Too risky to guess, so one short question about what they mean.",
                                 "I won't act on a vague destructive ask; ask which part, nothing else.",
                                 "Nothing gets removed until they say what; one question."]))
    if re.search(r"\bmy (?:roommate|dad|mom|mother|father|sister|brother|friend|boss|partner|cousin|aunt|uncle)\b|"
                 r"^[A-Za-z]+ (?:is|just|got|was)\b", s["prompt"].strip(), re.I):
        return pick(seed, ["That's news about someone else, not a fact about them, so nothing to remember. Just a warm reply.",
                           "It's about another person, so no memory to save and nothing to do. Respond kindly.",
                           "Someone else's news: no remember call, no lookup. A warm line back is enough."])
    return pick(seed, ["Nothing to do or save here; it's a passing moment, not a lasting fact. A short, kind reply.",
                       "No action and no memory needed, just a gentle answer to how the day went.",
                       "A passing feeling, not something to store or act on. Answer gently."])


def thought(meta: dict, messages: list[dict], ctx: dict | None = None) -> str:
    """The thought text for a THINK step. ctx (optional): later_calls [(name, args)] after this step in the
    turn, turn_reads [(name, args)] of the turn's reads so far, final (the turn's final text), salt."""
    s = analyze(meta, messages)
    seed = f"{meta.get('id')}|{(ctx or {}).get('salt', '')}"
    kind = s["kind"]
    if kind == "after_error":
        out = thought_after_error(meta, s, ctx, seed)
    elif kind == "answer_first":
        out = thought_answer_first(meta, s, ctx, seed)
    elif kind == "narrate":
        out = thought_narrate(meta, s, ctx, seed)
    else:
        out = thought_select_or_next(meta, s, ctx, seed)
    return re.sub(r"\s+", " ", out).strip()


# ----------------------------------------------------------------- checks (build + validate)
def _tri(text: str) -> set[str]:
    t = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))}


def jaccard(a: str, b: str) -> float:
    x, y = _tri(a), _tri(b)
    return len(x & y) / len(x | y) if x and y else 0.0


_MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = re.compile(r"\b(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun),? )?"
                      r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
                      r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.? (\d{1,2})\b"
                      r"|\b(\d{4})-(\d\d)-(\d\d)\b", re.I)
_WDNAME_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


def stated_dates(text: str, clock: _dt.datetime) -> tuple[list[tuple[_dt.date, int | None]], list[int]]:
    """Dates a thought states ((date, stated weekday or None)) and bare weekday names outside quotes."""
    out = []
    for m in _DATE_RE.finditer(text):
        if m.group(4):
            try:
                out.append((_dt.date(int(m[4]), int(m[5]), int(m[6])), None))
            except ValueError:
                out.append((None, None))
            continue
        mon, day = _MON[m.group(2).lower()[:3]], int(m.group(3))
        best = None
        for y in (clock.year - 1, clock.year, clock.year + 1):
            try:
                d = _dt.date(y, mon, day)
            except ValueError:
                continue
            if best is None or abs((d - clock.date()).days) < abs((best - clock.date()).days):
                best = d
        wd = _WD_IDX[m.group(1).lower()[:3]] if m.group(1) else None
        out.append((best, wd))
    unquoted = re.sub(r"'[^']*'|“[^”]*”|\"[^\"]*\"", " ", text)
    wds = [_WD_IDX[w.lower()[:3]] for w in _WDNAME_RE.findall(unquoted)]
    return out, wds


def date_problems(text: str, clock: _dt.datetime | None, dates: list[_dt.date]) -> list[str]:
    """When the step's call has a date: every date the thought states must be that date (or today), with the
    right weekday; a bare weekday name must be today's or the call's."""
    if not clock or not dates or not text:
        return []
    today = clock.date()
    ok = set(dates) | {today}
    errs = []
    stated, wds = stated_dates(text, clock)
    for d, wd in stated:
        if d is None:
            errs.append("unparseable date")
        elif d not in ok:
            errs.append(f"states {d} but the call says {sorted(set(dates))[0]}")
        elif wd is not None and wd != d.weekday():
            errs.append(f"weekday for {d} is wrong")
    okwd = {d.weekday() for d in ok}
    for wd in wds:
        if wd not in okwd:
            errs.append(f"weekday {_WD[wd]} is neither today nor the call's day")
    return errs


def grounding_problems(text: str, clock: _dt.datetime | None, sources: list[str]) -> list[str]:
    """For a step with no dated call: every date the thought states must appear in the turn's tool results
    (or be today), with the right weekday; a bare weekday name must be one of those dates' weekdays."""
    if not clock or not text:
        return []
    ok = {clock.date()}
    for src in sources:
        ok |= {d for d, _ in stated_dates(src, clock)[0] if d}
    errs = []
    stated, wds = stated_dates(text, clock)
    for d, wd in stated:
        if d is None or d not in ok:
            errs.append(f"states {d}, which no result or the clock shows")
        elif wd is not None and wd != d.weekday():
            errs.append(f"weekday for {d} is wrong")
    okwd = {d.weekday() for d in ok}
    for src in sources:     # a weekday the prompt or a result names (e.g. "on thursday") may be repeated
        okwd |= {_WD_IDX[w.lower()[:3]] for w in _WD_RE.findall(src)}
    errs += [f"weekday {_WD[w]} matches no shown date" for w in wds if w not in okwd]
    return errs


def plan_dates(meta: dict, messages: list[dict], ctx: dict | None = None) -> list[_dt.date]:
    """Dates in the arguments this step commits to (its own call, or for a bare load the loaded tool's next call)."""
    return call_dates(_plan_calls(analyze(meta, messages), ctx))


def check(meta: dict, messages: list[dict], text: str | None, ctx: dict | None = None) -> list[str]:
    """Every rule a THINK step's thought must pass (build and validate share it)."""
    s = analyze(meta, messages)
    errs = thought_problems(text, s["target_text"], (ctx or {}).get("final", ""))
    if errs:
        return errs
    clock = parse_clock(meta)
    dates = plan_dates(meta, messages, ctx)
    if dates:
        return date_problems(text, clock, dates)
    return grounding_problems(text, clock, s["all_results"] + [s["prompt"]])


def make_thought(meta: dict, messages: list[dict], ctx: dict | None = None, avoid: set[str] = frozenset()):
    """(thought, problems): the first phrasing (over salts, then terse) that passes check() and is not in
    `avoid` (the neighbouring examples' thoughts)."""
    ctx = dict(ctx or {})
    first = None
    for terse in (False, True):
        for salt in range(8):
            ctx.update(salt=salt or "", terse=terse)
            t = thought(meta, messages, ctx)
            errs = check(meta, messages, t, ctx)
            if first is None:
                first = (t, errs)
            if not errs and t not in avoid:
                return t, []
    return first[0], first[1] or ["duplicate of a neighbour"]


def thought_problems(text: str | None, target_text: str, final: str = "") -> list[str]:
    errs = []
    if not text or not text.strip():
        return ["empty"]
    if len(text.split()) > MAX_WORDS:
        errs.append(f"{len(text.split())} words")
    if NOISE.search(text):
        errs.append(f"noise: {NOISE.search(text).group(0)!r}")
    for ref in (target_text, final):
        if ref and jaccard(text, ref) >= JACCARD_MAX:
            errs.append(f"too close to the reply ({jaccard(text, ref):.2f})")
    return errs
