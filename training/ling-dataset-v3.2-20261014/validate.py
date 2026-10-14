#!/usr/bin/env python3
"""Validator for the Ling V3.2 dataset (standard library only; no network, no model).

    python3 validate.py [--check-repro] [--out validation.json]

FAILS (exit 1) on any problem, including a placeholder short-description asset. Checks:
  * assets: short descriptions real (not placeholder) and covering every built-in; the stdlib mirrors in generate.py
    (build_system, skills block, terse renderer, catalog) reproduce the reference renderings Wisp's own code produced
    at capture time (assets/capture_verification.json);
  * unknown tools: every tool name in every field (offered tools, calls, discovery args, receipts, catalog text,
    system text, results, finals) exists in assets/tools.json; no retired V3.1 name survives anywhere;
  * per-step protocol replay (host rules on origin/main): offered set and ORDER (loaded real schemas, sorted stubs,
    get_tool_schemas last while < 2 loads), discovery receipts / rejections, stub calls only for offered stubs, real
    calls only for loaded tools, <= 2 loads, <= 24 schemas, call-id pairing, no identical repeated real call (guarded
    repeats excepted), message 0/1/2 equal to the rebuilt prompts;
  * schema/argument validity of every call (targets and context) against tools.json: types, enums, required,
    unknown keys, minimum/maximum; stub guesses must be valid partial arguments of the later real call;
  * schema style: one style per conversation; full rows carry tools.json schemas byte-for-byte; terse rows carry the
    short line and NO parameter descriptions (a property named "description" excepted) and otherwise equal tools.json;
    terse web_fetch calls to crypto/news use the exact endpoint forms from the full description;
  * per-category conversation counts equal V3.1's minus the recorded drops; terse share 40-60% per split;
  * content preservation: outside recorded migrations, every user/assistant/tool text and every real call is
    identical to the V3.1 source conversation;
  * held-out eval overlap (hashed shingles, Jaccard < 0.4), synthetic PII only;
  * forbidden-call rules carried from V3.1: vague-destructive / no-referent clarifications call no tool, chat/code
    categories call no tool, sends/destructive tools need an explicit user cue, send recipients come from the user's
    words or an earlier tool result, no delete tool, `move_path` to the Trash only for an explicit trash/delete of a
    named file, guarded repeats are never targets;
  * V2 replay rows byte-identical to V3.1's and appended last; masking flags (only the last message trains).
"""
import argparse, collections, copy, hashlib, json, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate as G  # noqa: E402  (import only; main() is never called)

DISCOVERY = "get_tool_schemas"
REG = G.REG
CAT_TOOLS = G.CATALOG_TOOLS
FAMILY = G.FAMILY
SHORT = G.SHORT
RETIRED = G.RETIRED_NAMES
RETIRED_US = [n for n in RETIRED if "_" in n]
RETIRED_RE = re.compile(r"\b(" + "|".join(sorted(RETIRED_US, key=len, reverse=True)) + r")\b")
BACKTICK_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")
GUARD_MARK = "already ran with these exact arguments"
STRICT_OVERLAP = 0.4
OUTBOUND = {"send_message", "send_email", "reply_to_email", "forward_email", "schedule_send"}
CUES = {   # V3.1 cues, renamed for merged tools, plus the new destructive/confirm-gated survivors
    "send_message": r"text|message|msg|imessage|ping|dm\b|tell|know|shoot|send|reach|word|heads|say|email|mail|reply|answer|respond|write|contact|follow|touch|pass|drop|fire|hit|remind|nudge|invite|share|update|ask|check with|congrat|wish|thank|decline|accept|buzz|line|note|slack|over to",
    "send_email": r"email|e-mail|mail|send|write|reach|contact|follow|text|message|tell|know|touch|reply|answer|respond|shoot|drop|ping|use|remind|nudge|invite|share|update|ask|check with|congrat|wish|thank|decline|accept|line|note",
    "reply_to_email": r"reply|respond|answer|write back|get back|hit reply|send a reply",
    "forward_email": r"forward|pass|send",
    "schedule_send": r"text|email|mail|send|message|schedule|queue|remind|have a message|delay|land|go out",
    "cancel_event": r"cancel|delete|remove|take .* off|get rid|drop|scrap|decline|bail|back out|pull out",
    "clear_reminders": r"delete|clear|wipe|remove",
    "complete_reminder": r"done|finished|complete|check|tick|mark",
    "update_reminder": r"move|push|change|reschedule|shift|update|make it|instead|bump|actually|let.s do",
    "forget": r"forget|delete", "quit_app": r"quit|close|shut|exit",
    "clear_past_reminders": r"clear|delete|get rid|remove|wipe|dump|purge",
    "email_triage": r"archive|file away|flag|mark|read|unread|inbox",
    "move_path": r"move|rename|sort|organi[sz]e|tidy|file|put|trash|delete|throw|get rid",
    "mac_set_volume": r"volume|louder|quieter|turn (it )?(up|down)|sound|speaker|output level|crank|max",
    "mac_mute": r"mute|silence|sound", "mac_lock_screen": r"lock|secure the display", "mac_media": r"play|pause|resume|skip|next|previous|song|track|music|spotify",
}
CLARIFY_FAMILIES = {"sf_vague_destructive", "sf_no_referent"}
CRYPTO_URL = re.compile(r"^https://api\.coingecko\.com/api/v3/simple/price\?ids=[a-z0-9-]+&vs_currencies=usd$")
NEWS_URL = re.compile(r"^https://news\.google\.com/rss/search\?q=[^&\s]+&hl=en-US&gl=US&ceid=US:en$")


def h(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_schema(value, schema, path="$", partial=False):
    """Strict JSON-schema subset check (V3.1's) plus minimum/maximum; partial=True skips `required` (stub guesses)."""
    errs = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        if not partial:
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
    elif t == "string" and not isinstance(value, str):
        errs.append(f"{path}: expected string")
    elif t == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        errs.append(f"{path}: expected integer")
    elif t == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        errs.append(f"{path}: expected number")
    elif t == "boolean" and not isinstance(value, bool):
        errs.append(f"{path}: expected boolean")
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: value not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum")
    return errs


_MAP, _LIST, _ONE = G._SCHEMA_MAP_KEYS, G._SCHEMA_LIST_KEYS, G._SCHEMA_ONE_KEYS


def has_param_description(node, keep=frozenset()):
    """True if any SCHEMA position carries string `description` prose, ignoring properties listed in `keep` (they keep
    their full schema in terse mode) and data under default/enum/const/examples. Written independently of the renderer."""
    if not isinstance(node, dict):
        return False
    for k, val in node.items():
        if k == "description" and isinstance(val, str):
            return True
        if k in _MAP and isinstance(val, dict):
            if any(has_param_description(sub) for name, sub in val.items() if not (k == "properties" and name in keep)):
                return True
        elif k in _LIST and isinstance(val, list):
            if any(has_param_description(sub) for sub in val):
                return True
        elif k in _ONE and isinstance(val, dict) and has_param_description(val):
            return True
    return False


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


def read_jsonl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x]


def run(d, check_repro):
    problems = collections.defaultdict(list)
    counts = collections.Counter()

    def bad(check, msg):
        problems[check].append(msg)

    # ------------------------------------------------------------------ assets
    if SHORT.get("placeholder"):
        bad("short_descriptions_placeholder", "assets/short_descriptions.json is a PLACEHOLDER; re-render with the real export")
    builtins = {n for n, c in G.TOOLS["tool_category"].items() if n not in G.TOOLS["skill_tools"]}
    missing_short = sorted(builtins - set(SHORT["tools"]))
    if missing_short:
        bad("short_descriptions_coverage", f"built-ins without a short line: {missing_short}")
    if set(SHORT["tools"]) - set(REG) - set(G.TOOLS["source"]["unavailable_not_offered"]):
        bad("short_descriptions_coverage", "short lines for tools that are not registered")
    ver = json.loads((d / "assets/capture_verification.json").read_text())
    for n in REG:
        if h(G.real_schema(n, "terse")) != ver["terse_schema_sha256"][n]:
            bad("mirror_terse", f"{n}: generate.real_schema(terse) differs from registry.model_schema terse rendering")
    for k, want in ver["build_system_sha256"].items():
        offered = set() if k == "(none)" else (set(REG) if k == "(all)" else {k})
        if hashlib.sha256(G.build_system(offered).encode()).hexdigest() != want:
            bad("mirror_build_system", f"build_system({k}) differs from loop.build_system")
    for t, want in ver["skills_context_block"].items():
        if G.skills_block(t) != want:
            bad("mirror_skills_block", f"skills_block({t!r}) differs from skills_context_block")
    for n, s in REG.items():
        if n not in G.TOOLS["tool_family"]:
            bad("assets", f"{n} has no family")

    held = json.loads((d / "assets/heldout_shingles.json").read_text())["items"]
    v31_prov = {}
    for split in ("train", "dev"):
        for p in read_jsonl(G.CACHE / f"v31_new/{split}.provenance.jsonl"):
            v31_prov[p["conversation_id"]] = p
    for p in read_jsonl(G.CACHE / "v3_replay.provenance.jsonl"):
        v31_prov[p["source_conversation_id"]] = p
    src_rows = {}
    for f in ["v31_new/train.jsonl", "v31_new/dev.jsonl", "v3_replay.train.jsonl", "v3_replay.dev.jsonl"]:
        for r in read_jsonl(G.CACHE / f):
            src_rows.setdefault(r["conversation_id"], []).append(r)
    report = json.loads((d / "migration_report.json").read_text())
    dropped = {x["source_conversation_id"]: x for x in report["dropped"]}

    style_share = {}
    cat_counts = {"train": collections.Counter(), "dev": collections.Counter()}
    for split in ("train", "dev"):
        raw = [x for x in (d / f"{split}.jsonl").read_text().splitlines() if x]
        v2 = [x for x in (G.V31 / f"v2_replay.{split}.jsonl").read_text().splitlines() if x]
        if raw[-len(v2):] != v2:
            bad("v2_replay", f"{split}: V2 replay rows are not V3.1's, byte-for-byte, at the end")
        rows = [json.loads(x) for x in raw[:-len(v2)]]
        if any(r["id"].startswith("v2r-") for r in rows):
            bad("v2_replay", f"{split}: v2r rows before the end block")
        prov = {p["conversation_id"]: p for p in read_jsonl(d / f"{split}.provenance.jsonl")}
        by_conv = collections.OrderedDict()
        for r in rows:
            by_conv.setdefault(r["conversation_id"], []).append(r)
        if set(by_conv) != set(prov):
            bad("provenance", f"{split}: conversations in rows and provenance differ")
        counts[f"{split}_rows"] = len(rows)
        counts[f"{split}_conversations"] = len(by_conv)
        sty = collections.Counter()
        for cid, crow in by_conv.items():
            p = prov.get(cid)
            if not p:
                continue
            cat_counts[split][p["category"]] += 1
            sty[p["schema_style"]] += 1
            validate_conversation(cid, crow, p, src_rows.get(p["source_conversation_id"]), held, bad, counts)
        style_share[split] = dict(sty)
        tot = sum(sty.values())
        if tot and not (0.4 <= sty.get("terse", 0) / tot <= 0.6):
            bad("schema_style_mix", f"{split}: terse share {sty.get('terse', 0)}/{tot} outside 40-60%")
    # category counts vs V3.1 minus drops
    v31_counts = G.v31_counts()
    import ext_v32_mac as MAC
    v31_counts["train"]["v32:" + MAC.CATEGORY] = MAC.N_TRAIN    # authored in V3.2: expected = generator allocation
    v31_counts["dev"]["v32:" + MAC.CATEGORY] = MAC.N_DEV
    for split in ("train", "dev"):
        drops = collections.Counter(("v31:" if x["source"] == "v31_new" else "v3r:") + x["category"]
                                    for x in report["dropped"] if x["split"] == split)
        for cat in set(v31_counts[split]) | set(cat_counts[split]):
            want = v31_counts[split].get(cat, 0) - drops.get(cat, 0)
            if cat_counts[split].get(cat, 0) != want:
                bad("category_counts", f"{split}/{cat}: {cat_counts[split].get(cat, 0)} != V3.1 {v31_counts[split].get(cat, 0)} - dropped {drops.get(cat, 0)}")
    # conversations.jsonl
    conv_ids = [json.loads(x)["conversation_id"] for x in (d / "conversations.jsonl").read_text().splitlines() if x]
    all_prov = [p for s in ("train", "dev") for p in read_jsonl(d / f"{s}.provenance.jsonl")]
    if sorted(conv_ids) != sorted(p["conversation_id"] for p in all_prov):
        bad("conversations_file", "conversations.jsonl ids differ from provenance")
    if check_repro:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([sys.executable, str(HERE / "generate.py"), "--output-dir", td], check=True, stdout=subprocess.DEVNULL)
            for f in ["train.jsonl", "dev.jsonl", "train.provenance.jsonl", "dev.provenance.jsonl", "conversations.jsonl",
                      "stats.json", "DATA_CARD.md", "migration_report.json"]:
                if (Path(td) / f).read_bytes() != (d / f).read_bytes():
                    bad("reproducible", f"{f} differs on regeneration")
    result = {"passed": not problems, "problem_counts": {k: len(v) for k, v in sorted(problems.items())},
              "problems": {k: v[:20] for k, v in sorted(problems.items())}, "counts": dict(counts),
              "schema_style_conversations": style_share, "category_counts": {s: dict(sorted(c.items())) for s, c in cat_counts.items()},
              "dropped": len(report["dropped"]), "short_descriptions_source_sha": SHORT.get("source_sha"),
              "short_descriptions_placeholder": bool(SHORT.get("placeholder")), "check_repro": check_repro,
              "data_sha256": {f: hashlib.sha256((d / f).read_bytes()).hexdigest() for f in ("train.jsonl", "dev.jsonl")}}
    return result


def validate_conversation(cid, rows, p, src, held, bad, counts):
    style, variant = p["schema_style"], p["catalog_variant"]
    cat_tools = set(CAT_TOOLS[variant])
    full = rows[-1]["messages"]
    # ---- row shape / masking / ids / prefix consistency
    for k, r in enumerate(rows):
        if r["id"] != f"{cid}-s{k}" or set(r) != {"id", "conversation_id", "tools", "messages"}:
            bad("row_schema", f"{r['id']}: bad id/keys")
        ms = r["messages"]
        if ms[-1].get("training") is not True or ms[-1]["role"] != "assistant":
            bad("masking", f"{r['id']}: last message must be the training:true assistant")
        for m in ms[:-1]:
            if m["role"] == "assistant" and m.get("training") is not False:
                bad("masking", f"{r['id']}: earlier assistant not training:false")
            if m["role"] != "assistant" and "training" in m:
                bad("masking", f"{r['id']}: non-assistant message has a training flag")
        real_here = {t["function"]["name"] for t in r["tools"] if "properties" in t["function"]["parameters"]}
        for tc in ms[-1].get("tool_calls") or []:
            n_, a_ = tc["function"]["name"], tc["function"]["arguments"]
            if n_ not in REG and n_ != DISCOVERY:
                bad("unknown_tool", f"{r['id']}: target calls unknown tool {n_}")
            elif n_ in real_here and n_ != DISCOVERY and check_schema(a_, REG[n_]["function"]["parameters"]):
                bad("schema_valid", f"{r['id']}: target {n_} {check_schema(a_, REG[n_]['function']['parameters'])}")
        strip = lambda xs: [{kk: vv for kk, vv in x.items() if kk != "training"} for x in xs]
        if strip(ms[1:]) != strip(full[1:len(ms)]):
            bad("prefix_consistency", f"{r['id']}: not a prefix of the conversation")
    if full[-1].get("tool_calls") or not (full[-1].get("content") or "").strip():
        bad("final_present", f"{cid}: does not end in a final assistant answer")
    last_user = max(i for i, m in enumerate(full) if m["role"] == "user")
    user_text = full[last_user]["content"]
    all_user = " ".join(m["content"] for m in full[3:last_user + 1] if m["role"] == "user")
    clean = p.get("user_prompt_clean") or user_text
    intent_text = (" ".join(p.get("history_user_prompts") or []) + " " + clean).lower()
    now_sentence = full[1]["content"].split("\n")[1]
    # ---- replay
    loaded = list(p["preload"])
    if any(t not in cat_tools for t in loaded) or len(loaded) > 12:
        bad("preload", f"{cid}: preload not admitted or too large")
    attempts, seen = 0, set()
    turn = full[last_user + 1:]
    row_at = {len(r["messages"]) - 1: r for r in rows}
    ctx_text = all_user.lower()
    i, abs_i = 0, last_user + 1
    guard_prev = None
    while i < len(turn):
        m = turn[i]
        r = row_at.get(abs_i)
        expect_tools = G.render_tools(loaded, attempts, variant, style)
        expect_sys = G.sys_messages(loaded, now_sentence, variant, user_text)
        if r is not None:
            names = [t["function"]["name"] for t in r["tools"]]
            if names != G.offered(loaded, attempts, variant):
                bad("offered_tools", f"{r['id']}: offered names/order differ from the host offer")
            for t in r["tools"]:
                n = t["function"]["name"]
                if n not in REG and n != DISCOVERY:
                    bad("unknown_tool", f"{r['id']}: offered unknown tool {n}")
            if r["tools"] != expect_tools:
                bad("offered_schemas", f"{r['id']}: offered schemas differ from the {style} rendering")
            for t in r["tools"]:
                f = t["function"]
                n = f["name"]
                if n in loaded:
                    if style == "full" and t != REG[n]:
                        bad("full_schema", f"{r['id']}: {n} is not the tools.json schema")
                    if style == "terse" and n in SHORT["tools"]:
                        keep = frozenset(SHORT.get("keep_parameter_prose", {}).get(n, []))
                        if has_param_description(f["parameters"], keep):
                            bad("terse_schema", f"{r['id']}: {n} keeps parameter prose outside KEEP_PARAMETER_PROSE")
                        for kp in keep:
                            props = REG[n]["function"]["parameters"].get("properties", {})
                            if kp in props and f["parameters"]["properties"].get(kp) != props[kp]:
                                bad("terse_schema", f"{r['id']}: {n}.{kp} must keep its full schema (KEEP_PARAMETER_PROSE)")
                        if f["description"] != SHORT["tools"][n]:
                            bad("terse_schema", f"{r['id']}: {n} description is not the short line")
                    elif style == "terse" and t != REG[n]:
                        bad("terse_schema", f"{r['id']}: {n} has no short line, so it must keep its full schema")
            if r["messages"][:3] != expect_sys:
                bad("system_messages", f"{r['id']}: system messages differ from the rebuilt prompts")
        if m["role"] != "assistant":
            bad("protocol", f"{cid}: unexpected {m['role']} in turn")
            break
        tcs = m.get("tool_calls") or []
        res = turn[i + 1:i + 1 + len(tcs)]
        if tcs and (m.get("content") or "") != "":
            bad("protocol", f"{cid}: tool-call turn with content")
        if [x.get("tool_call_id") for x in res] != [t["id"] for t in tcs] or any(x["role"] != "tool" for x in res):
            bad("call_pairing", f"{cid}: tool results do not pair with calls")
        offered_now = set(G.offered(loaded, attempts, variant))
        names = [t["function"]["name"] for t in tcs]
        for n in names:
            if n not in REG and n != DISCOVERY:
                bad("unknown_tool", f"{cid}: call to unknown tool {n}")
            if n not in offered_now:
                bad("protocol", f"{cid}: call to unoffered {n}")
        if tcs:
            kinds = {"disc" if n == DISCOVERY else ("real" if n in loaded else "stub") for n in names}
            if len(kinds) != 1:
                bad("protocol", f"{cid}: message mixes discovery/stub/real calls")
            kind = kinds.pop()
            if kind == "disc":
                a = tcs[0]["function"]["arguments"]
                errs = check_schema(a, G.DISC_SCHEMA[variant]["function"]["parameters"])
                if errs:
                    bad("schema_valid", f"{cid}: get_tool_schemas {errs}")
                for t in a.get("tools", []):
                    if t not in REG:
                        bad("unknown_tool", f"{cid}: discovery requests unknown {t}")
                attempts += 1
                fams = a.get("families", [])
                fam_names = {t for f in fams for t in G.CATALOG[variant].get(f, [])}
                req = a.get("tools") or sorted(fam_names)
                if any(f not in G.CATALOG[variant] for f in fams):
                    want = G.rejection("Unknown, excluded or unavailable capability family")
                elif not set(req) <= fam_names:
                    want = G.rejection("Requested tools must belong to the selected admitted families")
                else:
                    names_new = list(dict.fromkeys([*loaded, *req]))
                    want = G.receipt(fams, names_new)
                    if len(names_new) > 24:
                        bad("protocol", f"{cid}: more than 24 schemas")
                    loaded = names_new
                if res and res[0]["content"] != want:
                    bad("discovery_receipt", f"{cid}: receipt/rejection differs from the host's")
                if want.startswith("Schema selection rejected") and r is not None:
                    bad("protocol", f"{cid}: a rejected discovery is a training target")
            elif kind == "stub":
                new = list(dict.fromkeys(names))
                attempts += 1
                fams = list(dict.fromkeys(FAMILY[n] for n in new))
                names_new = list(dict.fromkeys([*loaded, *new]))
                want = G.receipt(fams, names_new)
                for x in res:
                    if x["content"] != want:
                        bad("discovery_receipt", f"{cid}: stub receipt differs from the host's implicit discovery")
                for t in tcs:
                    a = t["function"]["arguments"]
                    if not isinstance(a, dict) or check_schema(a, REG[t["function"]["name"]]["function"]["parameters"], partial=True):
                        bad("schema_valid", f"{cid}: stub guess for {t['function']['name']} is not a valid partial argument set")
                    later = [tc["function"]["arguments"] for mm in turn for tc in (mm.get("tool_calls") or [])
                             if tc["function"]["name"] == t["function"]["name"] and tc is not t]
                    if a and not any(all(k in la and la[k] == v for k, v in a.items()) for la in later):
                        bad("stub_guess", f"{cid}: stub guess for {t['function']['name']} is not a subset of the real call")
                loaded = names_new
            else:
                for t, x in zip(tcs, res):
                    n, a = t["function"]["name"], t["function"]["arguments"]
                    errs = check_schema(a, REG[n]["function"]["parameters"])
                    refused = x["content"].startswith(("(error calling", "(exit code"))
                    if errs and r is None and refused and p["source"] == "v32_new":
                        counts["context_only_refused_calls"] += 1     # never a target; the next step is the correction
                    elif errs:
                        bad("schema_valid", f"{cid}: {n} {errs}")
                    key = (n, json.dumps(a, sort_keys=True))
                    guarded = GUARD_MARK in x["content"]
                    if guarded:
                        if key not in seen:
                            bad("no_repeat", f"{cid}: guard text for a first call {n}")
                        if r is not None:
                            bad("guard_target", f"{cid}: a guarded repeat is a training target")
                        if not x["content"].startswith(f"({n} already ran"):
                            bad("unknown_tool", f"{cid}: guard text names another tool")
                    elif key in seen:
                        bad("no_repeat", f"{cid}: identical repeated call {n}")
                    if guard_prev and key == guard_prev and not guarded:
                        bad("guard_followup", f"{cid}: repeats the guarded call right after the guard")
                    seen.add(key)
                    # forbidden-call rules
                    if n in CUES and not re.search(CUES[n], intent_text):
                        bad("explicit_authority", f"{cid}: {n} without a matching user cue")
                    if n in OUTBOUND and "to" in a:
                        tos = re.split(r"\s*[,;]\s*", str(a["to"]))
                        digits = re.sub(r"\D", "", ctx_text)
                        for t_ in tos:
                            t_ = t_.strip().lower()
                            d_ = re.sub(r"\D", "", t_)
                            if t_ and not (t_ in ctx_text or (len(d_) >= 7 and d_ in digits)):
                                bad("recipient_provenance", f"{cid}: {n}.to {t_!r} appears in no earlier turn or tool result")
                    if n == "move_path" and str(a.get("destination", "")).startswith("~/.Trash") and not G.TRASH_CUE.search(intent_text):
                        bad("forbidden_trash", f"{cid}: move to Trash without an explicit trash/delete request")
                    if n == "web_fetch" and style == "terse":
                        u = str(a.get("url", ""))
                        if "coingecko" in u and not CRYPTO_URL.match(u):
                            bad("terse_web_fetch_url", f"{cid}: crypto fetch not in the documented endpoint form")
                        if "news.google" in u and not NEWS_URL.match(u):
                            bad("terse_web_fetch_url", f"{cid}: news fetch not in the documented endpoint form")
                    guard_prev = key if guarded else None
            if attempts > 2:
                bad("protocol", f"{cid}: more than two loading steps")
            for x in res:
                ctx_text += " " + x["content"].lower()
                for mm in json.loads(x["content"]).get("available_tools", []) if x["content"].startswith('{"schema_discovery"') else []:
                    if mm not in REG:
                        bad("unknown_tool", f"{cid}: receipt lists unknown {mm}")
        i += 1 + len(tcs)
        abs_i += 1 + len(tcs)
    # ---- text-level unknown/retired names anywhere in the conversation (all rows' messages)
    for r in rows:
        for m in r["messages"]:
            txt = m.get("content") or ""
            hit = RETIRED_RE.search(txt)
            if hit:
                bad("retired_name_in_text", f"{r['id']}: '{hit.group(1)}' in a {m['role']} message")
            if m["role"] == "system":
                for w in BACKTICK_RE.findall(txt):
                    if w in G.OLD_NAMES and w not in REG and w != DISCOVERY:
                        bad("retired_name_in_text", f"{r['id']}: `{w}` in system text")
        cat_line = [ln for ln in r["messages"][1]["content"].split("\n") if " Tools: " in ln]
        for ln in cat_line:
            for t in ln.split(" Tools: ", 1)[1].split(", "):
                if t not in REG:
                    bad("unknown_tool", f"{r['id']}: catalog lists unknown {t}")
    # ---- forbidden-call category rules
    real_calls = [g for g in p["gold_calls"] if g["name"] != DISCOVERY and not g.get("stub")]
    if p.get("family_id") in CLARIFY_FAMILIES and not p.get("history_user_prompts") and real_calls:
        bad("forbidden_call", f"{cid}: vague/no-referent request with no history must clarify without a tool call")
    if p.get("family_id") in CLARIFY_FAMILIES and not p.get("history_user_prompts"):
        counts["clarify_no_tool_conversations"] += 1
    if ("chat" in p["source_category"]) and p["gold_calls"]:
        bad("chat_no_tool", f"{cid}: chat/code conversation uses tools")
    gold = [{"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}
            for m in turn for tc in (m.get("tool_calls") or [])]
    if [{"name": g["name"], "arguments": g["arguments"]} for g in p["gold_calls"]] != gold:
        bad("provenance", f"{cid}: gold_calls differ from the conversation")
    if p["n_steps"] != len(rows):
        bad("provenance", f"{cid}: n_steps mismatch")
    # ---- held-out overlap + PII
    for t in [user_text] + list(p.get("history_user_prompts") or []):
        if heldout_overlap(held, t) >= STRICT_OVERLAP:
            bad("heldout_overlap", f"{cid}: Jaccard >= {STRICT_OVERLAP} vs a held-out eval prompt")
    blob = json.dumps(full, ensure_ascii=False)
    for em in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]+", blob):
        if not em.endswith(".invalid") and not em.endswith("@example.com"):
            bad("synthetic_only", f"{cid}: non-synthetic address {em}")
    if p["source"] == "v32_new":
        mac_rules(cid, p, turn, user_text, full, bad, counts)
        return
    # ---- content preservation vs the V3.1 source
    if src is None:
        bad("source", f"{cid}: source conversation missing")
        return
    sfull = src[-1]["messages"]
    s_last = max(i for i, m in enumerate(sfull) if m["role"] == "user")
    if [x["content"] for x in sfull[3:s_last + 1]] != [x["content"] for x in full[3:last_user + 1]]:
        bad("content_preservation", f"{cid}: history/user text differs from source")
    if not p["migrated"]:
        def essence(msgs):
            out = []
            for x in msgs:
                if x["role"] == "assistant":
                    calls = [(t["function"]["name"], t["function"]["arguments"]) for t in (x.get("tool_calls") or [])
                             if t["function"]["name"] != DISCOVERY]
                    out.append(("a", x.get("content") or "", calls))
                elif not x["content"].startswith('{"schema_discovery"'):
                    out.append(("t", x["content"]))
            return out
        if essence(sfull[s_last + 1:]) != essence(turn):
            bad("content_preservation", f"{cid}: unmigrated conversation differs from its source")
    else:
        counts["migrated_conversations"] += 1
        for mg in p["migrations"]:
            if not mg.get("migration_rule"):
                bad("provenance", f"{cid}: migration without a rule")


MAC_FORBIDDEN = {"run_shell", "run_applescript"}
SPECIFIC_MUSIC = re.compile(r"\b(playlist|album|discover weekly|taylor swift|rhapsody|rumours)\b", re.I)
NOT_LOCKED = re.compile(r"\b(NOT locked|isn't locked|is not locked|still unlocked|couldn't lock|could not lock)\b", re.I)


def mac_rules(cid, p, turn, user_text, full, bad, counts):
    """Forbidden/required behaviour for v32_mac_controls (mirrors the mac-controls skill body)."""
    low = (" ".join(p.get("history_user_prompts") or []) + " " + user_text).lower()
    results = {m["tool_call_id"]: m["content"] for m in turn if m["role"] == "tool"}
    # executed calls only: discovery calls and name-only stub calls (answered by a receipt) are not executions
    calls = [(tc["function"]["name"], tc["function"]["arguments"], i) for i, m in enumerate(turn)
             for tc in (m.get("tool_calls") or [])
             if tc["function"]["name"] != DISCOVERY and not results.get(tc["id"], "").startswith('{"schema_discovery"')]
    final = full[-1].get("content") or ""
    real = [(n, a) for n, a, _ in calls]
    for m in turn:
        for tc in m.get("tool_calls") or []:
            if tc["function"]["name"] in MAC_FORBIDDEN:
                bad("mac_forbidden_call", f"{cid}: {tc['function']['name']} called (or stubbed) for a mac control")
    for n, a in real:
        if n in MAC_FORBIDDEN:
            bad("mac_forbidden_call", f"{cid}: {n} used for a mac control")
    for m in turn:
        for tc in m.get("tool_calls") or []:
            n, a = tc["function"]["name"], tc["function"]["arguments"]
            res = results.get(tc["id"], "")
            if n == "mac_set_volume" and not res.startswith(("(error calling", "(exit code")) and "level" in a:
                if not (isinstance(a["level"], int) and not isinstance(a["level"], bool) and 0 <= a["level"] <= 100):
                    bad("mac_volume_level", f"{cid}: executed level {a['level']!r} is not an integer 0-100")
            if n == "mac_media" and a:
                if "spotify" in low and a.get("app") not in (None, "spotify") or ("spotify" not in low and "app" in a):
                    bad("mac_media_app", f"{cid}: app must be omitted (Apple Music) unless the user names Spotify")
            if n == "mac_lock_screen" and res.startswith("(exit code") and not NOT_LOCKED.search(final):
                bad("mac_lock_error_honesty", f"{cid}: lock failed but the answer does not say the screen is NOT locked")
            if n in G.TOOLS["skill_tools"] and res == "The user denied this action.":
                later = [c for c in calls if c[2] > turn.index(m)]
                if later:
                    bad("mac_denial_retry", f"{cid}: calls after a denied confirmation")
                counts["mac_denials"] += 1
    names = [n for n, _ in real]
    if re.search(r"\bmute\b", low) and not re.search(r"\bunmute\b", low) and "mac_set_volume" in names:
        bad("mac_mute_not_zero", f"{cid}: mute must be mac_mute, not a volume change")
    if re.search(r"\b(what'?s|what is|how loud|am i muted|is my)\b", low) and set(names) - {"mac_get_volume"}:
        bad("mac_read_only", f"{cid}: a volume question must only call mac_get_volume")
    if re.search(r"turn it (up|down)|louder|quieter|volume (up|down)|bring the sound|bump the sound|speakers (back|a little)", low) \
            and "mac_set_volume" in names and not re.search(r"\d", user_text):
        if names[:1] != ["mac_get_volume"]:
            bad("mac_relative_volume", f"{cid}: relative change without reading the volume first")
        cur = [int(re.match(r"volume (\d+)%", results[tc["id"]]).group(1)) for m in turn for tc in (m.get("tool_calls") or [])
               if tc["function"]["name"] == "mac_get_volume" and re.match(r"volume \d+%", results.get(tc["id"], ""))]
        new = [a["level"] for n, a in real if n == "mac_set_volume"]
        if cur and new and not (abs(abs(new[0] - cur[0]) - 20) <= 5 or new[0] in (0, 100)):
            bad("mac_relative_volume", f"{cid}: {cur[0]} -> {new[0]} is not about 20 (or clamped)")
    if p["family_id"] == "mc_specific" and any(n == "mac_media" for n in names):
        bad("mac_specific_music", f"{cid}: mac_media cannot pick specific music")
    if SPECIFIC_MUSIC.search(user_text) and "mac_media" in names:
        bad("mac_specific_music", f"{cid}: mac_media called for a specific song/playlist")
    counts["mac_conversations"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=HERE)
    ap.add_argument("--check-repro", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    res = run(a.dir, a.check_repro)
    (a.out or a.dir / "validation.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: res[k] for k in ("passed", "problem_counts", "counts", "schema_style_conversations", "dropped",
                                          "short_descriptions_placeholder")}, indent=1))
    for k, v in res["problems"].items():
        print(k, v[:5])
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
