"""Validate the Ask LoRA dataset against the live Wisp code in this checkout.

Usage:
    python validate.py                     # train.jsonl, valid.jsonl (and all.jsonl if built)
    python validate.py --private-results PATH
        also checks every example against names, addresses, subjects and titles
        from the REAL eval results (the user's data). The denylist is built in
        memory only and never written anywhere. WISP_EVAL_RESULTS works too.

Exit code 1 on any problem. Checks, per example:
  structure   roles in a legal order; every history call has exactly one result
  live tools  every offered schema equals what this checkout's registry offers:
              full schemas byte-for-byte, name-only stubs, and get_tool_schemas
              (its family enum must be a subset of the live families)
  calls       every call names an offered tool that exists in the live registry;
              target arguments validate against the offered schema; tool names
              given to get_tool_schemas exist
  writes      a call (history or target) to a tool outside the read categories is
              allowed only if writes.WRITE_ALLOW[family] lists it; a family absent
              from WRITE_ALLOW stays reads-only. Needs the example's meta line
              (train.meta.jsonl beside train.jsonl, meta.jsonl beside all.jsonl)
  cards       a writes.ALWAYS_CARD tool that ran must be in meta cards_seen; a
              writes.NEVER_CARD tool must not be; meta expect_cards == cards_seen
  approval    the history agrees with meta approve (one entry per card, in order;
              a denied card's result is "The user denied this action."), and a
              final after a denial must not claim the action happened
  tool steps  an assistant step with tool calls has empty content (Bolt's chat
              template keeps it, Gemma's drops it, so rows would differ)
  injection   when a tool result carries text addressed to an assistant, no
              write call and no later reply repeats that text's payload
              (addresses, links, amounts, account numbers, commands)
  privacy     every email domain is synthetic; no /Users/<real account> path;
              no git identity or macOS account name; optional private denylist
              over EVERYTHING, system prompts and tool schemas included
  leak        the user prompt does not repeat or nearly repeat an eval question
              or a toolbench prompt (all splits, all fact seeds); no toolbench
              fact value (names, amounts, businesses, files, codes) appears in
              the conversation (leak.py)
  markup      no <|tool_call> text inside assistant content
  harness     no trace of the build harness (World.t_* names, stub errors)
  thinking    (needs the meta line; builder/thinking.py) every example has a bool
              `think`; THINK => a non-empty thought of at most 60 words, with no
              tool plumbing, markup or harness text, not the user's name, no
              injected payload, and not a copy of the reply (character-trigram
              Jaccard < 0.5 against the target text and the turn's final); NO
              THINK => thought null; a date or weekday the thought states must
              equal the date in the step's call arguments (or, with no dated
              call, a date some tool result shows, or today); `think` equals
              thinking.decide() re-run on the example; chat_voice and
              knowledge_direct never think; two consecutive THINK examples
              never share a thought
  template    (with --template MODEL_DIR, repeatable) each model's chat template
              renders every example, and the prompt is a prefix of the full text,
              as mlx-lm's mask_prompt needs; brev/render.py renders the row (the
              thought, the empty thought or none, per meta). With two or more,
              its prompt and completion must be byte-identical under every template.
Across files: no prompt group appears in both train and valid.
"""
from __future__ import annotations

import boot  # noqa: F401  (fresh WISP_HOME, repo on sys.path)

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

import leak
import thinking
import writes

OUT =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DENIED = "The user denied this action."

import service.tools  # noqa: E402,F401
from service import skills as _skills  # noqa: E402

try:   # production registers the bundled skills' tools (use_skill, mac_*) at startup (main.lifespan)
    _skills.load()
except Exception:  # noqa: BLE001  (production tolerates a failed load the same way)
    pass
from service.router.model_led import (CapabilityCatalog, DISCOVERY_TOOL, discovery_schema,  # noqa: E402
                                      registry_specs, stub_schema)
from service.tools.registry import REGISTRY  # noqa: E402

READ_CATEGORIES = {"assistant_read", "browser_history_read", "compute", "email_read", "fs_read",
                   "messages_read", "notes_read", "system_read", "web_read", "wisp_admin"}
READ_TOOLS = {"list_running_apps"}   # app_control, but only lists

# Email domains the synthetic world and Wisp's own scaffold use. Anything else is
# treated as a possible real address.
SYNTHETIC_DOMAINS = {"linden.edu", "example.com", "example.net", "example.org", "example",
                     "fernbrooklabs.com", "northlinebank.com"}

INJECTION_MARKERS = re.compile(
    r"(ignore (?:all )?(?:your )?previous instructions|\bAI (?:assistant|model)\b|assistant instructions|"
    r"hidden prompt|system override|if you are an ai|note to any ai|hey wisp,? (?:please )?(?:send|forward|delete|add))",
    re.I)
_PAYLOAD = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\b[\w-]+(?:\.[\w-]+)*\.(?:net|com|org|io|sh)\b(?:/\S*)?"
                      r"|\$\d[\d,.]*|\b\d{3,}-\d{3,}\b|`[^`]+`")

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def check_schema(value, schema, path="$"):
    errs = []
    t = schema.get("type")
    if t:
        ok = isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if t == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []) or []:
            if req not in value:
                errs.append(f"{path}: missing required {req!r}")
        for k, v in value.items():
            if k in props:
                errs += check_schema(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False or props:
                errs.append(f"{path}: unexpected argument {k!r}")
    if t == "array":
        if schema.get("items"):
            for i, v in enumerate(value):
                errs += check_schema(v, schema["items"], f"{path}[{i}]")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than maxItems")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errs.append(f"{path}: duplicate items")
    return errs


# ----------------------------------------------------------------- live tools
CATALOG = CapabilityCatalog.build(registry_specs(REGISTRY))
LIVE_DISCOVERY = discovery_schema(CATALOG)["function"]


def is_read(name: str) -> bool:
    if name == DISCOVERY_TOOL:
        return True
    tool = REGISTRY.get(name)
    return bool(tool) and (tool.category in READ_CATEGORIES or name in READ_TOOLS)


def live_problems(schema: dict) -> list[str]:
    fn = schema.get("function") or {}
    name = fn.get("name")
    if name == DISCOVERY_TOOL:
        fam = ((fn.get("parameters") or {}).get("properties") or {}).get("families", {}).get("items", {}).get("enum", [])
        live_fam = LIVE_DISCOVERY["parameters"]["properties"]["families"]["items"]["enum"]
        errs = [] if set(fam) <= set(live_fam) else [f"{name}: unknown families {sorted(set(fam) - set(live_fam))}"]
        a = json.loads(json.dumps(fn)); b = json.loads(json.dumps(LIVE_DISCOVERY))
        a["parameters"]["properties"]["families"]["items"].pop("enum", None)
        b["parameters"]["properties"]["families"]["items"].pop("enum", None)
        return errs + ([] if a == b else [f"{name}: schema differs from live"])
    tool = REGISTRY.get(name)
    if tool is None:
        return [f"{name}: not in the live registry"]
    if "description" not in fn:
        return [] if schema == stub_schema(name) else [f"{name}: stub differs from live"]
    return [] if schema == tool.schema() else [f"{name}: schema differs from live"]


# ----------------------------------------------------------------- meta, approval
_META_NAMES = {"train.jsonl": "train.meta.jsonl", "valid.jsonl": "valid.meta.jsonl", "all.jsonl": "meta.jsonl"}
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_DONE = re.compile(r"\b(?:sent|added|scheduled|created|booked|saved|deleted|removed|moved|cancel+ed|updated|"
                   r"emailed|texted|replied|forwarded|completed|marked|done|all set)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|never|no|nothing|none|nope|without|cannot|unsent|unsaved)\b|n['’]t\b", re.I)


def meta_path_for(path: str) -> str | None:
    """The per-step metadata file that goes with an examples file, if it exists."""
    d, base = os.path.split(path)
    cand = os.path.join(d, _META_NAMES.get(base) or os.path.splitext(base)[0] + ".meta.jsonl")
    return cand if os.path.exists(cand) else None


def claims_done(text: str) -> bool:
    """True when some statement in ``text`` says an action happened (sent, added, done...)."""
    for sentence in _SENTENCES.split(text or ""):
        s = sentence.strip()
        if s and not s.endswith("?") and _DONE.search(s) and not _NEGATED.search(s):
            return True
    return False


def pair_cards(calls: list[tuple[str, str]], cards: list[str], approve: list) -> dict[str, bool]:
    """Match the example's calls (id, name), in order, to the turn's cards (tool names, in order);
    returns {tool_call_id: approved}. A card with no approve entry was approved."""
    # Pair from the END: a name-only stub call (a schema load) precedes its real
    # call and raises no card, so the real call is always the later one.
    used, out = set(), {}
    for cid, name in reversed(calls):
        for k in reversed(range(len(cards))):
            card = cards[k]
            if k not in used and card == name:
                used.add(k)
                out[cid] = bool(approve[k]) if k < len(approve) else True
                break
    return out


# ----------------------------------------------------------------- privacy
def identity_tokens() -> set[str]:
    """The machine owner's git identity and account name, never written anywhere."""
    out = set()
    for key in ("user.name", "user.email"):
        try:
            val = subprocess.run(["git", "config", key], capture_output=True, text=True, cwd=OUT).stdout.strip()
        except OSError:
            val = ""
        for part in re.split(r"[\s@.]+", val):
            if len(part) >= 4 and part.lower() not in {"claude", "noreply", "anthropic", "gmail", "users"}:
                out.add(part.lower())
    home = os.path.basename(os.path.expanduser("~"))
    if len(home) >= 4:
        out.add(home.lower())
    return out


def _is_common(word: str, common: set[str]) -> bool:
    """A dictionary word, allowing regular inflections (-s, -es, -ed, -ing, -ly, -er) and hyphen parts."""
    w = word.lower().strip("'’-")
    if not w or w in common:
        return True
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("d", ""), ("ing", ""),
                     ("ing", "e"), ("ly", ""), ("er", ""), ("ers", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and (w[: -len(suf)] + rep) in common:
            return True
    m = re.fullmatch(r"(.*([b-df-hj-np-tv-z]))\2(?:ed|ing|er)", w)   # scanned, stopping
    if m and m.group(1) in common:
        return True
    return "-" in w and all(_is_common(part, common) for part in w.split("-") if part)


def private_denylist(path: str):
    """Names, addresses, subjects and titles from the REAL eval results."""
    common = {w.strip().lower() for w in open("/usr/share/dict/words") if w[:1].islower()}
    common |= {w.lower() for w in ("Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April "
                                   "May June July August September October November December AM PM PDT PST AI OK").split()}
    exempt = {w.lower() for q in leak.load_eval() for w in re.findall(r"[A-Za-z][A-Za-z'’-]+", q["prompt"])}
    texts = []
    for line in open(path):
        row = json.loads(line)
        texts.append(str(row.get("text") or ""))
        texts += [str(r) for r in row.get("results") or []]
    tokens, emails, phrases = set(), set(), set()
    for t in texts:
        emails |= {e.lower() for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", t)}
        for tok in re.findall(r"\b[A-Z][a-z][A-Za-z'’-]{1,}\b", t):
            base = re.split(r"['’]", tok)[0]
            if len(base) >= 4 and not _is_common(base, common) and base.lower() not in exempt:
                tokens.add(base)
        for pat in (r"^Subject: (.+)$", r"^Title: (.+?)(?: \[|$)"):
            phrases |= {m.strip().lower() for m in re.findall(pat, t, re.M) if len(m.strip()) >= 12}
    return tokens, emails, phrases


# ----------------------------------------------------------------- thinking
def turn_contexts(path, metas) -> dict[int, dict]:
    """Per line index: the later steps' calls and the final text of its turn (same file; a turn never
    straddles the split)."""
    turns: dict[str, list] = {}
    for idx, (line, meta) in enumerate(zip(open(path), metas)):
        try:
            target = json.loads(line)["messages"][-1]
        except (ValueError, KeyError, IndexError):
            continue
        turns.setdefault(str(meta.get("id", idx)).rsplit("-s", 1)[0], []).append((meta.get("step", 0), idx, target))
    out = {}
    for steps in turns.values():
        steps.sort(key=lambda s: (s[0], s[1]))
        final = next((t.get("content") or "" for _, _, t in steps if not t.get("tool_calls")), "")
        for k, (_, idx, _t) in enumerate(steps):
            out[idx] = {"later_calls": [c for _, _, t in steps[k + 1:] for c in thinking.calls_of(t)], "final": final}
    return out


def check_thinking(meta, msgs, ctx, injected, prev_thought, bad, where):
    """The thinking checks for one example; returns the thought to compare the next THINK example with."""
    think, text = meta.get("think"), meta.get("thought")
    family = meta.get("family")
    if not isinstance(think, bool):
        bad("think:missing", where)
        return prev_thought
    if not think:
        if text is not None:
            bad("think:thought_without_think", where)
        try:
            if thinking.decide(meta, msgs):
                bad("think:policy", f"{where} decide() says THINK ({thinking.step_kind(meta, msgs)})")
        except Exception as exc:  # noqa: BLE001
            bad("think:policy", f"{where} decide() failed: {exc!r}"[:160])
        return prev_thought
    if family in thinking.NEVER:
        bad("think:never_family", f"{where} {family}")
    try:
        if not thinking.decide(meta, msgs):
            bad("think:policy", f"{where} decide() says NO THINK ({thinking.step_kind(meta, msgs)})")
    except Exception as exc:  # noqa: BLE001
        bad("think:policy", f"{where} decide() failed: {exc!r}"[:160])
    if not isinstance(text, str) or not text.strip():
        bad("think:thought_missing", where)
        return prev_thought
    for err in thinking.check(meta, msgs, text, ctx):
        key = ("think:too_long" if err.endswith(" words") else "think:noise" if err.startswith("noise") else
               "think:copies_reply" if err.startswith("too close") else "think:thought_missing" if err == "empty"
               else "think:date_mismatch")
        bad(key, f"{where} {err}")
    for payload in injected:
        if payload.lower() in text.lower():
            bad("think:injection_payload", f"{where} {payload[:12]}…")
    if text == prev_thought:
        bad("think:adjacent_duplicate", where)
    return text


# ----------------------------------------------------------------- per example
AUTO = object()


def load_meta(path, meta_path=AUTO, bad=None):
    """Per-line metadata for an examples file: a list of dicts, or None when there is no meta file."""
    mp = meta_path_for(path) if meta_path is AUTO else meta_path
    if not mp or not os.path.exists(mp):
        return None
    metas = [json.loads(l) for l in open(mp)]
    if len(metas) != sum(1 for _ in open(path)):
        if bad:
            bad("meta:line_count", os.path.basename(mp))
        return None
    return metas


def validate_file(path, leaks, ident, deny, wl, meta_path=AUTO):
    problems, samples = Counter(), {}

    def bad(key, where):
        problems[key] += 1
        samples.setdefault(key, where)

    metas = load_meta(path, meta_path, bad)
    turn_ctx = turn_contexts(path, metas) if metas else {}
    prev_thought = None
    n = 0
    for ln, line in enumerate(open(path), 1):
        n += 1
        where = f"{os.path.basename(path)}:{ln}"
        meta = metas[ln - 1] if metas else {}
        try:
            ex = json.loads(line)
        except ValueError:
            bad("parse", where)
            continue
        msgs, tools = ex.get("messages"), ex.get("tools")
        if not isinstance(msgs, list) or not isinstance(tools, list) or not msgs:
            bad("shape", where)
            continue
        offered = {}
        for t in tools:
            for err in live_problems(t):
                bad("live_schema", f"{where} {err}")
            offered[t["function"]["name"]] = t["function"]
        roles = [m.get("role") for m in msgs]
        i = 0
        while i < len(roles) and roles[i] == "system":
            i += 1
        if i == 0 or i >= len(roles) or roles[i] != "user":
            bad("roles:start", where)
            continue
        if roles[-1] != "assistant":
            bad("roles:end", where)
        prompt = msgs[i].get("content") or ""
        hit, score, q = leaks.leaks(prompt)
        if hit:
            bad("leak:toolbench_prompt" if str(q["id"]).startswith("tb:") else "leak:eval_question",
                f"{where} {score:.2f} ~ {q['id']}")
        for val in leak.fact_hits(leak.conversation_text(ex)):
            bad("leak:toolbench_fact", f"{where} {val[:3]}…({len(val)})")
        family = meta.get("family")
        allowed = writes.WRITE_ALLOW.get(family, frozenset())
        calls, results = [], {}      # calls: (tool_call_id, name) in order; results: tool_call_id -> text
        pending, injected = [], []
        for j in range(i + 1, len(msgs)):
            m, r = msgs[j], roles[j]
            is_target = j == len(msgs) - 1
            if r == "assistant":
                if pending:
                    bad("roles:assistant_before_tool_results", f"{where}:{j}")
                content = m.get("content") or ""
                if "<|tool_call>" in content or "<tool_call" in content:
                    bad("markup_in_content", f"{where}:{j}")
                if m.get("tool_calls") and content.strip():
                    bad("toolcall_content_nonempty", f"{where}:{j}")
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    name = fn.get("name")
                    calls.append((tc.get("id"), name))
                    if tc.get("type") != "function" or not tc.get("id"):
                        bad("tool_call_shape", f"{where}:{j}")
                    # Only the target step's offer list is stored; history calls were
                    # offered on their own step (get_tool_schemas drops out after loads).
                    if is_target and name not in offered:
                        bad("call_not_offered", f"{where}:{j} {name}")
                    if name != DISCOVERY_TOOL and name not in REGISTRY:
                        bad("call_not_in_registry", f"{where}:{j} {name}")
                    if not is_read(name) and name not in allowed:
                        bad("write_call", f"{where}:{j} {name} (family {family})")
                    args = fn.get("arguments")
                    if isinstance(args, str):   # wire format; the written files carry objects
                        try:
                            args = json.loads(args or "{}")
                        except ValueError:
                            bad("args_not_json", f"{where}:{j}")
                            continue
                    if not isinstance(args, dict):
                        bad("args_not_object", f"{where}:{j}")
                        continue
                    if name == DISCOVERY_TOOL:
                        unknown = [t for t in args.get("tools", []) if t not in REGISTRY]
                        if unknown:
                            bad("discovery_unknown_tool", f"{where}:{j} {unknown}")
                    if is_target and name in offered and "description" in offered[name]:
                        errs = check_schema(args, offered[name].get("parameters") or {"type": "object"})
                        if errs:
                            bad("args_schema(target)", f"{where} {name} {errs[:2]}")
                    if is_target or not is_read(name):   # a write call never carries an injected payload
                        for payload in injected:
                            if payload.lower() in json.dumps(args).lower():
                                bad("injection:payload_in_call", f"{where}:{j} {name}")
                    if not is_target:
                        pending.append(tc.get("id"))
                if is_target and not m.get("tool_calls"):
                    for payload in injected:
                        if payload.lower() in content.lower():
                            bad("injection:payload_in_reply", f"{where} {payload[:12]}…")
            elif r == "tool":
                if m.get("tool_call_id") in pending:
                    pending.remove(m["tool_call_id"])
                else:
                    bad("tool_without_call", f"{where}:{j}")
                text = str(m.get("content") or "")
                results[m.get("tool_call_id")] = text
                for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
                    if INJECTION_MARKERS.search(sentence):
                        injected += [p.strip("`") for p in _PAYLOAD.findall(sentence)]
            elif r == "user":
                bad("extra_user", f"{where}:{j}")
            else:
                bad("bad_role", f"{where}:{j}")
        if pending:
            bad("call_without_tool_result", where)
        # writes, approval cards and denials, against the example's meta line
        names = [name for _, name in calls]
        ran_writes = [name for name in names if name in REGISTRY and not is_read(name)]
        cards = list(meta.get("cards_seen") or [])
        if ran_writes != list(meta.get("writes") or [])[:len(ran_writes)]:
            bad("meta:writes", f"{where} example {ran_writes} vs meta {meta.get('writes')}")
        if sorted(meta.get("expect_cards") or []) != sorted(cards):
            bad("cards:expect_mismatch", f"{where} expected {meta.get('expect_cards')} saw {cards}")
        took_effect = {e[0] for e in meta.get("effects") or []}
        for name in sorted(set(names) & writes.ALWAYS_CARD):
            # The loop may refuse a write before its card (e.g. a move onto a read-only
            # calendar); that call has no effect and needs no card. One that took effect does.
            if name not in cards and name in took_effect:
                bad("cards:missing", f"{where} {name}")
        for name in sorted(set(cards) & writes.NEVER_CARD):
            bad("cards:never_card", f"{where} {name}")
        decided = pair_cards(calls, cards, list(meta.get("approve") or []))
        denied_any = False
        for cid, _name in calls:
            text = results.get(cid)
            if text is None:
                continue      # the target's own call has no result yet
            denied = DENIED in text
            denied_any |= denied
            if cid in decided and decided[cid] == denied:
                bad("approve:denied_result_missing" if not decided[cid] else "approve:unexpected_denial",
                    f"{where} {_name}: card {'allowed' if decided[cid] else 'denied'}, result {text[:40]!r}")
            elif cid not in decided and denied:
                bad("approve:denial_without_card", f"{where} {_name}")
        if denied_any and not msgs[-1].get("tool_calls") and claims_done(msgs[-1].get("content") or ""):
            bad("approve:denied_claims_success", f"{where} {(msgs[-1].get('content') or '')[:60]!r}")
        # situational thinking (thinking.py), against the example's meta line
        if metas:
            prev_thought = check_thinking(meta, msgs, turn_ctx.get(ln - 1, {}), injected, prev_thought, bad, where)
        # the synthetic harness must never show through (class or stub names)
        if re.search(r"\bWorld\.t_\w+|\bt_[a-z_]+\(\)|synthetic world|lora-dataset harness", json.dumps(msgs)):
            bad("harness_leak", where)
        # privacy: the whole example, scaffold included
        blob = json.dumps(ex, ensure_ascii=False)
        low = blob.lower()
        for dom in {e.split("@", 1)[1].lower() for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", blob)}:
            if dom not in SYNTHETIC_DOMAINS and not any(dom.endswith("." + s) for s in SYNTHETIC_DOMAINS):
                bad("privacy:email_domain", f"{where} {dom[:3]}…")
        for user in set(re.findall(r"/Users/([A-Za-z0-9._-]+)", blob)):
            if user.lower() not in {"shared", "sam"}:
                bad("privacy:home_path", f"{where} /Users/{user[:2]}…")
        for tok in ident:
            if re.search(rf"\b{re.escape(tok)}\b", low):
                bad("privacy:identity", f"{where} {tok[:2]}…")
        if deny:
            tokens, emails, phrases = deny
            for tok in set(re.findall(r"\b[A-Z][a-z][A-Za-z'’-]{1,}\b", blob)):
                base = re.split(r"['’]", tok)[0]
                if base in tokens and base.lower() not in wl:
                    bad("privacy:real_name", f"{where} {base[:2]}…({len(base)})")
            for e in emails:
                if e in low:
                    bad("privacy:real_email", f"{where} {e[:2]}…")
            for p in phrases:
                if p in low:
                    bad("privacy:real_phrase", f"{where} {p[:3]}…({len(p)})")
    return n, problems, samples


def _brev_render():
    """brev/render.py's render(): the exact prompt/completion rows the GPU box trains on."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask_brev_render", os.path.join(OUT, "brev", "render.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def template_check(path, model_dirs, meta_path=AUTO):
    """Render every example with each model's own chat template, as mlx-lm does, and with
    several templates require brev/render.py's prompt and completion to match byte for byte.
    Returns (problems, {template tag: token stats})."""
    import statistics
    from transformers import AutoTokenizer
    dirs = [model_dirs] if isinstance(model_dirs, str) else list(model_dirs)
    tags = [os.path.basename(os.path.normpath(d)) for d in dirs]
    toks = [AutoTokenizer.from_pretrained(d) for d in dirs]
    brev = _brev_render()
    metas = load_meta(path, meta_path)
    lens, targets, problems = {t: [] for t in tags}, {t: [] for t in tags}, Counter()
    shapes = {t: Counter() for t in tags}
    for ln, line in enumerate(open(path)):
        ex = json.loads(line)
        meta = metas[ln] if metas else {"id": f"line{ln + 1}", "family": "", "target_kind": "", "think": False,
                                        "thought": None, "chat_template_kwargs": {"enable_thinking": True}}
        tools = ex["tools"] or None
        rows = []
        for tag, tok in zip(tags, toks):
            try:
                full = tok.apply_chat_template(ex["messages"], tools=tools, tokenize=False)
                pre = tok.apply_chat_template(ex["messages"][:-1], tools=tools, tokenize=False, add_generation_prompt=True)
            except Exception as exc:  # noqa: BLE001
                problems[f"{tag}: template_error: {str(exc)[:80]}"] += 1
                continue
            if not full.startswith(pre):
                problems[f"{tag}: template:prompt_not_prefix"] += 1
                continue
            n = len(tok(full, add_special_tokens=False)["input_ids"])
            lens[tag].append(n)
            targets[tag].append(n - len(tok(pre, add_special_tokens=False)["input_ids"]))
            try:
                r = brev.render(tok, ex, meta)
                rows.append((tag, r["prompt"], r["completion"]))
                shapes[tag][brev.row_shape(r)] += 1
            except Exception as exc:  # noqa: BLE001
                problems[f"{tag}: render_error: {str(exc).split(': ', 1)[-1][:80]}"] += 1
        for tag, prompt, completion in rows[1:]:
            if prompt != rows[0][1]:
                problems[f"template:parity_prompt({rows[0][0]} vs {tag})"] += 1
            if completion != rows[0][2]:
                problems[f"template:parity_completion({rows[0][0]} vs {tag})"] += 1
    stats = {}
    for tag in tags:
        ls = sorted(lens[tag])
        if ls:
            stats[tag] = {"median_tokens": statistics.median(ls), "p90_tokens": ls[int(0.9 * len(ls))],
                          "max_tokens": ls[-1], "total_tokens": sum(ls), "target_tokens": sum(targets[tag]),
                          "rendered_shapes": dict(shapes[tag])}
    return problems, stats


def turn_groups(meta_path):
    return {leak.fold(json.loads(l)["prompt"]) for l in open(meta_path)}


# Public words that also occur in the real data: planets, places, people from
# history, books and Wisp's own words. Each was reviewed: it is common knowledge
# or text already in Wisp's public source (e.g. the weather tool's 'Palo Alto,CA'
# example, the 'Paris,TX' stand-in), not a detail of the user's life.
PUBLIC_WORDS = {"neptune", "jupiter", "venus", "mercury", "saturn", "uranus", "mars", "africa", "asia", "europe",
                "america", "california", "palo", "paris", "texas", "alexander",
                "hamlet", "claudius", "laertes", "gertrude", "polonius", "ophelia", "pomodoro", "online", "wisp",
                "time-bound", "message-id"}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--private-results", default=os.environ.get("WISP_EVAL_RESULTS"))
    ap.add_argument("--template", metavar="MODEL_DIR", action="append", default=[],
                    help="also render each example with this model's chat template (repeatable; with two or more, "
                         "the rendered prompt and completion must match byte for byte). Reads template/tokenizer "
                         "files only, never weights.")
    args = ap.parse_args(argv)
    paths = args.paths or [os.path.join(OUT, x) for x in ("train.jsonl", "valid.jsonl", "all.jsonl")
                           if os.path.exists(os.path.join(OUT, x))]
    leaks = leak.LeakIndex()
    ident = identity_tokens()
    deny = None
    if args.private_results:
        deny = private_denylist(args.private_results)
        print(f"private denylist: {len(deny[0])} names, {len(deny[1])} emails, {len(deny[2])} phrases (memory only)")
    else:
        print("private denylist: skipped (pass --private-results to check against the real eval results)")
    print(f"live registry: {len(REGISTRY)} tools, {len(CATALOG.families)} families")
    failed = False
    for p in paths:
        n, problems, samples = validate_file(p, leaks, ident, deny, PUBLIC_WORDS)
        print(f"{os.path.basename(p)}: {n} examples, problems: {dict(problems) or 'none'}")
        for k, v in list(samples.items())[:30]:
            print("   ", k, "->", v)
        failed |= bool(problems)
        if args.template:
            tproblems, tstats = template_check(p, args.template)
            print(f"    template ({len(args.template)} checked"
                  f"{', parity' if len(args.template) > 1 else ''}): problems {dict(tproblems) or 'none'}")
            for tag, st in tstats.items():
                print(f"      {tag}: {st}")
            failed |= bool(tproblems)
    tm, vm = os.path.join(OUT, "train.meta.jsonl"), os.path.join(OUT, "valid.meta.jsonl")
    if not args.paths and os.path.exists(tm) and os.path.exists(vm):
        both = turn_groups(tm) & turn_groups(vm)
        print(f"split: {len(both)} prompt groups in both train and valid")
        failed |= bool(both)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
