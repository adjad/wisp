"""Shared pieces of the r4 chat eval: the prompts file, the reply text measures and the mechanical checks.

Pure Python plus PyYAML: no `service` import, no model, no network. chat_eval.py (runs the models),
judge_pairs.py (blind pairs, mechanical checks, the gate scorer) and the tests all import this.

THE PROMPTS FILE (prompts_r4.yaml)
  unit kinds   single | conversation | variety   (see the header of the yaml)
  conversation units may carry `skill`, `probe` and `checks`. `turn` in a check is the 0-based index of the
  USER turn whose reply is checked; reply i is the reply to user turn i.

CHECK TYPES (all evaluated on the replies, whatever each model said earlier)
  contains   {turn, any|all|none: [terms]}          the reply has (any/all) or lacks (none) these terms
  recall     {turn, source_turn, any: [terms]}      the reply gives back a detail the USER said in turn
                                                     `source_turn`, 3 to 8 turns earlier
  groups     {turn, groups: [[terms]..], min_groups, sources?: [turns]}
                                                     the reply uses at least `min_groups` of the user's details
  forbid     {turns: all|[..], any: [phrases]}     invented context: none of these may appear in those replies
  standing   {rule, value?, from_turn, to_turn}     a standing instruction, checked on EVERY reply in the range.
                                                     rules: max_sentences, min_sentences, max_words, min_words,
                                                     no_question_end, no_bullets, no_emoji
  overlap    {turn, with_turn, min}                  the reply repeats an earlier reply of the same model: at
                                                     least `min` of its content words occur in that reply
  shorter    {turn, than_turn}                       the reply has fewer words than that earlier reply

Term matching (lower-cased, markdown stars dropped, thousands separators dropped): a term of 1-3 letters or
digits matches whole tokens only ("au", "212"); a plain word matches at the start of a token ("pastr" matches
"pastries"); anything with punctuation ("2.54", "co-op") matches as a substring.
"""
from __future__ import annotations

import math
import re
import statistics
from pathlib import Path

import yaml

KINDS = ("single", "conversation", "variety")
SKILLS = ("recall", "standing", "emotional", "casual_fact")
CHECK_TYPES = ("contains", "recall", "groups", "forbid", "standing", "overlap", "shorter")
STANDING_RULES = ("max_sentences", "min_sentences", "max_words", "min_words",
                  "no_question_end", "no_bullets", "no_emoji")
RULES_WITH_VALUE = ("max_sentences", "min_sentences", "max_words", "min_words")
RECALL_MIN_DISTANCE, RECALL_MAX_DISTANCE = 3, 8
HERE = Path(__file__).resolve().parent
DEFAULT_PROMPTS = HERE / "prompts_r4.yaml"


# ------------------------------------------------------------------------------------ prompts file
def load_prompts(path=None) -> list[dict]:
    doc = yaml.safe_load(Path(path or DEFAULT_PROMPTS).read_text())
    if not isinstance(doc, dict) or not isinstance(doc.get("units"), list):
        raise ValueError("prompts file needs a top-level `units:` list")
    return doc["units"]


def unit_runs(unit: dict) -> list[tuple[int, list[str]]]:
    """[(variant, [user turns])]: one session per entry. single and variety sessions have one turn."""
    kind = unit["kind"]
    if kind == "single":
        return [(0, [unit["prompt"]])]
    if kind == "variety":
        return [(i, [p]) for i, p in enumerate(unit["prompts"])]
    return [(0, list(unit["turns"]))]


def model_turns(units: list[dict]) -> int:
    return sum(len(turns) for u in units for _, turns in unit_runs(u))


def _strs(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() for x in value)


def validate_units(units: list[dict]) -> list[str]:
    """Problems with the prompts file (empty = fine). Structural only: no model, no repository."""
    errors: list[str] = []
    seen: set[str] = set()
    for u in units:
        uid = u.get("id") if isinstance(u, dict) else None
        if not isinstance(uid, str) or not uid:
            errors.append(f"unit without an id: {str(u)[:60]}")
            continue
        if uid in seen:
            errors.append(f"{uid}: duplicate id")
        seen.add(uid)
        kind = u.get("kind")
        if kind not in KINDS:
            errors.append(f"{uid}: kind must be one of {KINDS}, not {kind!r}")
            continue
        if not isinstance(u.get("category"), str) or not u["category"]:
            errors.append(f"{uid}: category missing")
        if kind == "single" and not (isinstance(u.get("prompt"), str) and u["prompt"].strip()):
            errors.append(f"{uid}: single unit needs a prompt")
        if kind == "variety" and not (_strs(u.get("prompts")) and len(u["prompts"]) >= 2):
            errors.append(f"{uid}: variety unit needs >= 2 prompts")
        if kind == "conversation":
            if not (_strs(u.get("turns")) and len(u["turns"]) >= 2):
                errors.append(f"{uid}: conversation needs >= 2 turns")
                continue
            errors += _validate_conversation(u)
        elif u.get("checks") or u.get("skill"):
            errors.append(f"{uid}: checks and skill are for conversations only")
    return errors


def _validate_conversation(u: dict) -> list[str]:
    uid, turns = u["id"], u["turns"]
    n = len(turns)
    errors: list[str] = []
    new = uid.startswith("mt_")
    if new and not 6 <= n <= 10:
        errors.append(f"{uid}: new conversations have 6-10 user turns, this has {n}")
    if "skill" in u and u["skill"] not in SKILLS:
        errors.append(f"{uid}: skill must be one of {SKILLS}")
    if new and "skill" not in u:
        errors.append(f"{uid}: new conversation needs a skill")
    if "probe" in u and not _strs(u["probe"]):
        errors.append(f"{uid}: probe must be a list of strings (quote numbers)")
    checks = u.get("checks") or []
    if new and not checks:
        errors.append(f"{uid}: new conversation needs checks")
    text = [norm(t) for t in turns]

    def turn_ok(c, key="turn"):
        v = c.get(key)
        return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n

    for i, c in enumerate(checks):
        where = f"{uid} check {i}"
        t = c.get("type") if isinstance(c, dict) else None
        if t not in CHECK_TYPES:
            errors.append(f"{where}: type must be one of {CHECK_TYPES}")
            continue
        if t in ("contains", "recall", "groups", "overlap", "shorter") and not turn_ok(c):
            errors.append(f"{where}: turn must be an index 0..{n - 1}")
            continue
        if t == "contains":
            if not any(k in c for k in ("any", "all", "none")):
                errors.append(f"{where}: contains needs any, all or none")
            for k in ("any", "all", "none"):
                if k in c and not _strs(c[k]):
                    errors.append(f"{where}: {k} must be a list of strings (quote numbers and yes/no)")
        elif t == "recall":
            src = c.get("source_turn")
            if not turn_ok(c, "source_turn"):
                errors.append(f"{where}: recall needs source_turn")
            elif not RECALL_MIN_DISTANCE <= c["turn"] - src <= RECALL_MAX_DISTANCE:
                errors.append(f"{where}: source_turn {src} is {c['turn'] - src} turns before turn {c['turn']}; "
                              f"recall must reach back {RECALL_MIN_DISTANCE}-{RECALL_MAX_DISTANCE} turns")
            elif not _strs(c.get("any")):
                errors.append(f"{where}: recall needs `any`")
            elif not any(term_in(x, text[src]) for x in c["any"]):
                errors.append(f"{where}: no term of {c['any']} appears in the user's turn {src}: {turns[src]!r}")
        elif t == "groups":
            groups = c.get("groups")
            if not (isinstance(groups, list) and groups and all(_strs(g) for g in groups)):
                errors.append(f"{where}: groups must be a list of non-empty term lists")
                continue
            mg = c.get("min_groups", 1)
            if not (isinstance(mg, int) and 1 <= mg <= len(groups)):
                errors.append(f"{where}: min_groups must be 1..{len(groups)}")
            srcs = c.get("sources")
            if srcs is not None:
                if not (isinstance(srcs, list) and srcs and all(isinstance(s, int) and 0 <= s < c["turn"] for s in srcs)):
                    errors.append(f"{where}: sources must be earlier user-turn indices")
                    continue
                dists = [c["turn"] - s for s in srcs]
                if max(dists) > RECALL_MAX_DISTANCE or max(dists) < RECALL_MIN_DISTANCE:
                    errors.append(f"{where}: the oldest source must be {RECALL_MIN_DISTANCE}-{RECALL_MAX_DISTANCE} turns back")
                for g in groups:
                    if not any(term_in(x, text[s]) for x in g for s in srcs):
                        errors.append(f"{where}: no term of group {g} appears in the user's turns {srcs}")
        elif t == "forbid":
            tt = c.get("turns")
            if tt != "all" and not (isinstance(tt, list) and tt and all(isinstance(x, int) and 0 <= x < n for x in tt)):
                errors.append(f"{where}: turns must be `all` or a list of turn indices")
            if not _strs(c.get("any")):
                errors.append(f"{where}: forbid needs `any`")
            else:
                for phrase in c["any"]:
                    if any(term_in(phrase, x) for x in text):
                        errors.append(f"{where}: forbidden phrase {phrase!r} appears in the user's own turns")
        elif t == "standing":
            rule = c.get("rule")
            if rule not in STANDING_RULES:
                errors.append(f"{where}: rule must be one of {STANDING_RULES}")
            if rule in RULES_WITH_VALUE and not (isinstance(c.get("value"), int) and c["value"] >= 0):
                errors.append(f"{where}: rule {rule} needs an integer `value`")
            f, e = c.get("from_turn"), c.get("to_turn", n - 1)
            if not (isinstance(f, int) and isinstance(e, int) and 0 <= f <= e < n):
                errors.append(f"{where}: from_turn <= to_turn must be turn indices 0..{n - 1}")
        elif t == "overlap":
            w, m = c.get("with_turn"), c.get("min")
            if not (isinstance(w, int) and 0 <= w < c["turn"]):
                errors.append(f"{where}: with_turn must be an earlier turn")
            if not (isinstance(m, (int, float)) and 0 < m <= 1):
                errors.append(f"{where}: min must be in (0, 1]")
        elif t == "shorter":
            w = c.get("than_turn")
            if not (isinstance(w, int) and 0 <= w < c["turn"]):
                errors.append(f"{where}: than_turn must be an earlier turn")
    return errors


# ------------------------------------------------------------------------------------ text measures
_EMOJI = re.compile("[\U0001F000-\U0001F2FF\U0001F300-\U0001FAFF☀-⛿✀-✒✙-➿"
                    "⭐⭕⬛⬜❤️]")
_STOP = frozenset("""that this with have from they them their there what when where which while would could should
about been being were will your just like than then also into over some such only very more most much many
its it's i'm you're don't can't won't isn't aren't doesn't didn't""".split())


def norm(text: str) -> str:
    """Lower-cased, curly quotes straightened, markdown stars dropped, 1,000 -> 1000, spaces collapsed."""
    t = str(text or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("*", "")
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    return re.sub(r"[ \t\r\f\v]+", " ", t)


def term_in(term: str, text_norm: str) -> bool:
    t = norm(term).strip()
    if not t:
        return False
    if re.fullmatch(r"[a-z0-9]{1,3}", t):
        rx = rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"
    elif re.fullmatch(r"[a-z0-9' -]+", t):
        rx = rf"(?<![a-z0-9]){re.escape(t)}"
    else:
        return t in text_norm
    return re.search(rx, text_norm) is not None


def _strip_md(text: str) -> str:
    t = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*(?:[-*•‣◦▪–—]|\d{1,2}[.)])\s+", "", t)
    return t.replace("*", "").replace("`", "")


_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|mr|mrs|ms|dr|st|approx|no|u\.s|a\.m|p\.m)\.", re.I)


def sentences(text: str) -> list[str]:
    """Sentences of a reply: split after . ! ? or a line break; abbreviations and decimals do not split."""
    t = _ABBREV.sub(lambda m: m.group(0).replace(".", "\u0001"), _strip_md(text))
    parts = re.split(r"(?<=[.!?…])[\"')\]”’]*\s+|\n+", t)
    return [p.replace("\u0001", ".").strip() for p in parts if re.search(r"[A-Za-z0-9]", p)]


def words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", _strip_md(text).strip()) if re.search(r"[A-Za-z0-9]", w)]


def has_bullets(text: str) -> bool:
    return re.search(r"(?m)^\s{0,6}(?:[-*•‣◦▪–—]\s+\S|\d{1,2}[.)]\s+\S)", str(text or "")) is not None


def ends_with_question(text: str) -> bool:
    t = str(text or "").rstrip()
    t = _EMOJI.sub("", t).rstrip(" \t\n*_\"')]”’`")
    return t.endswith("?")


def emoji_count(text: str) -> int:
    return len(_EMOJI.findall(str(text or "")))


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", norm(text)) if len(w) >= 4 and w not in _STOP}


# ------------------------------------------------------------------------------------ non-replies, leaks
# A casual message answered with a failed lookup or a stall. r3 examples: "I couldn't find any web results for
# cozy movies", "I don't have your home city yet, so I can't suggest a dinner". Heuristic: the judge's
# `not_a_reply` flag is scored next to it, and neither is ever the only evidence in a report.
_STALL_SHORT = re.compile(
    r"\bcouldn'?t find\b|\bcould not find\b|\bno (?:web |search )*results?\b"
    r"|\b(?:unable|wasn'?t able|was not able) to (?:find|access|check|retrieve|look)\b"
    r"|\b(?:can'?t|cannot) (?:access|check|retrieve|look up|find)\b"
    r"|\bdon'?t have (?:your|any|that|access)\b[^.!?\n]*\b(?:yet|can'?t|cannot)\b"
    r"|\bi haven'?t (?:checked|read|looked|found)\b", re.I)
_STALL_ANYWHERE = re.compile(
    r"haven'?t checked a current personal source|nothing i can check|records it\b|private data is in context"
    r"|something went wrong|i ran into an error", re.I)
SHORT_REPLY_CHARS = 350


def non_reply(text, error=None, timeout=False) -> str | None:
    """Why this reply is not a reply (None when it is one)."""
    if timeout:
        return "timeout"
    if error:
        return "error"
    t = str(text or "").strip()
    if len(re.findall(r"[A-Za-z0-9]", t)) < 2:
        return "empty"
    if _STALL_ANYWHERE.search(t) or (len(t) < SHORT_REPLY_CHARS and _STALL_SHORT.search(t)):
        return "stall"
    return None


# Internals that must never reach a chat reply: snake_case identifiers (tool names: use_skill, search_web),
# skill names ("the idea-refine skill"), host markers and tool-call JSON.
_LEAK = [
    ("snake_case", re.compile(r"\b[a-z]{2,}(?:_[a-z0-9]+)+\b")),
    ("skill", re.compile(r"\b[a-z]+(?:-[a-z]+)+ skills?\b|\b(?:use|using|activate|activating|enable|run|running)\s+(?:the\s+)?[\w-]+\s+skill\b", re.I)),
    ("marker", re.compile(r"\bUNTRUSTED\b|\btool[ _]?(?:call|result)s?\b|\bfunction[ _]call\b", re.I)),
    ("json", re.compile(r"\{\s*\"[A-Za-z_]+\"\s*:")),
]


def leaks(text, names=()) -> list[str]:
    """Leaked internals in a reply, as 'kind:text' (empty = clean). `names` adds exact tool/skill names."""
    t = str(text or "")
    found = [f"{kind}:{m.group(0)}" for kind, rx in _LEAK for m in rx.finditer(t)]
    low = norm(t)
    found += [f"name:{n}" for n in names if n and re.search(rf"(?<![a-z0-9_]){re.escape(n.lower())}(?![a-z0-9_])", low)]
    return found


# ------------------------------------------------------------------------------------ mechanical checks
def _res(check: dict, idx: int, turn, ok: bool, detail: str, **extra) -> dict:
    return {"check": idx, "type": check["type"], "turn": turn, "ok": bool(ok), "detail": detail, **extra}


def _standing_ok(rule: str, value, reply: str) -> tuple[bool, str]:
    if rule == "max_sentences":
        n = len(sentences(reply))
        return n <= value, f"{n} sentences (max {value})"
    if rule == "min_sentences":
        n = len(sentences(reply))
        return n >= value, f"{n} sentences (min {value})"
    if rule == "max_words":
        n = len(words(reply))
        return n <= value, f"{n} words (max {value})"
    if rule == "min_words":
        n = len(words(reply))
        return n >= value, f"{n} words (min {value})"
    if rule == "no_question_end":
        q = ends_with_question(reply)
        return not q, "ends with a question" if q else "no question at the end"
    if rule == "no_bullets":
        b = has_bullets(reply)
        return not b, "has a bullet or numbered list" if b else "no list"
    if rule == "no_emoji":
        n = emoji_count(reply)
        return n == 0, f"{n} emoji"
    raise ValueError(rule)


def evaluate_conversation(unit: dict, replies: list[str | None]) -> dict:
    """Mechanical checks of one conversation sample.

    `replies[i]` is the reply to user turn i (None when it never came: error, timeout or a stopped run).
    Returns {"checks": [...], "ok": bool, "missing": [turns], "non_replies": {turn: why}, "leaks": {turn: [..]},
    "repeats": {turn: why}}. `ok` is the explicit checks only and no missing reply; non-replies, leaks and
    repeats are reported beside it (the gate counts them separately)."""
    n = len(unit["turns"])
    reps = list(replies) + [None] * (n - len(replies))
    low = [None if r is None else norm(r) for r in reps]
    results: list[dict] = []
    for i, c in enumerate(unit.get("checks") or []):
        t = c["type"]
        if t in ("contains", "recall", "groups", "overlap", "shorter"):
            turn = c["turn"]
            r, rl = reps[turn], low[turn]
            if r is None:
                results.append(_res(c, i, turn, False, "no reply"))
            elif t in ("contains", "recall"):
                ok, why = True, []
                if c.get("any"):
                    hit = [x for x in c["any"] if term_in(x, rl)]
                    ok &= bool(hit)
                    why.append(f"any {c['any']}: " + (f"found {hit[0]!r}" if hit else "none found"))
                if c.get("all"):
                    miss = [x for x in c["all"] if not term_in(x, rl)]
                    ok &= not miss
                    why.append(f"all: missing {miss}" if miss else "all present")
                if c.get("none"):
                    bad = [x for x in c["none"] if term_in(x, rl)]
                    ok &= not bad
                    why.append(f"none: found {bad}" if bad else "none present")
                results.append(_res(c, i, turn, ok, "; ".join(why)))
            elif t == "groups":
                hit = [g[0] for g in c["groups"] if any(term_in(x, rl) for x in g)]
                need = c.get("min_groups", 1)
                results.append(_res(c, i, turn, len(hit) >= need, f"{len(hit)}/{len(c['groups'])} groups used (need {need})"))
            elif t == "overlap":
                ref = reps[c["with_turn"]]
                if ref is None:
                    results.append(_res(c, i, turn, False, "no earlier reply to compare"))
                else:
                    a, b = content_words(r), content_words(ref)
                    frac = len(a & b) / len(a) if a else 0.0
                    results.append(_res(c, i, turn, frac >= c["min"], f"{frac:.2f} of its content words in reply {c['with_turn']} (need {c['min']})"))
            else:  # shorter
                ref = reps[c["than_turn"]]
                if ref is None:
                    results.append(_res(c, i, turn, False, "no earlier reply to compare"))
                else:
                    a, b = len(words(r)), len(words(ref))
                    results.append(_res(c, i, turn, a < b, f"{a} words vs {b} in reply {c['than_turn']}"))
        elif t == "forbid":
            turns = range(n) if c["turns"] == "all" else c["turns"]
            for turn in turns:
                if low[turn] is None:
                    continue
                bad = [x for x in c["any"] if term_in(x, low[turn])]
                results.append(_res(c, i, turn, not bad, f"found {bad}" if bad else "clean"))
        else:  # standing
            for turn in range(c["from_turn"], c.get("to_turn", n - 1) + 1):
                if reps[turn] is None:
                    results.append(_res(c, i, turn, False, "no reply", rule=c["rule"]))
                    continue
                ok, why = _standing_ok(c["rule"], c.get("value"), reps[turn])
                results.append(_res(c, i, turn, ok, why, rule=c["rule"]))
    missing = [i for i, r in enumerate(reps) if r is None]
    skip_repeat = {c["turn"] for c in unit.get("checks") or [] if c["type"] in ("overlap", "shorter")}
    return {
        "checks": results,
        "ok": not missing and all(r["ok"] for r in results),
        "missing": missing,
        "non_replies": {i: w for i, r in enumerate(reps) if r is not None and (w := non_reply(r))},
        "leaks": {i: lk for i, r in enumerate(reps) if r is not None and (lk := leaks(r))},
        "repeats": _repeats(reps, skip_repeat),
    }


def _shingles(text: str, n: int = 4) -> set[tuple]:
    w = re.findall(r"[a-z']+", norm(text))
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _repeats(reps: list, skip: set[int]) -> dict[int, str]:
    """Replies that repeat an earlier reply of the same conversation: the same long opening sentence, or half
    of a long reply's 4-grams already said. Replies a check asks to repeat are skipped."""
    out: dict[int, str] = {}
    for i, r in enumerate(reps):
        if r is None or i in skip or i == 0:
            continue
        first = sentences(r)[:1]
        first_n = norm(first[0]).strip(" .!?") if first else ""
        sh = _shingles(r)
        for j in range(i):
            if reps[j] is None:
                continue
            prev = sentences(reps[j])[:1]
            if first_n and len(first_n.split()) >= 6 and prev and norm(prev[0]).strip(" .!?") == first_n:
                out[i] = f"same opening sentence as reply {j}"
                break
            if len(words(r)) >= 12 and sh:
                share = len(sh & _shingles(reps[j])) / len(sh)
                if share >= 0.5:
                    out[i] = f"{share:.0%} of its 4-grams already in reply {j}"
                    break
    return out


# ------------------------------------------------------------------------------------ statistics
def sign_test_p(wins: int, losses: int) -> float:
    """Exact two-sided binomial p (fair coin) on decisive pairs; ties are dropped by the caller."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def median(xs) -> float:
    xs = list(xs)
    return float(statistics.median(xs)) if xs else 0.0
