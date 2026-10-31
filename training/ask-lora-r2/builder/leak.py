"""Eval-leak check: a training prompt must not repeat, or nearly repeat, any
question of the 2026-10-29 Ask eval (eval_questions.jsonl) or any prompt of the
toolbench (scripts/toolbench/cases/*.yaml, ALL splits).

Weekday and month names and numbers are folded to placeholders first, so a
template ("anything to prep for Friday?") counts as the same question for any
day. A prompt leaks when its folded form equals a held-out question's, or their
character-trigram Jaccard similarity is at least THRESHOLD.

Toolbench prompts carry ``{fact}`` placeholders. Every prompt, and every user
turn in a case's history, is bound with make_facts(None) and make_facts(0..49),
deduplicated by folded form, and indexed the same way as the eval questions. It
is loaded lazily on first use and cached for the process.

Toolbench fact VALUES (the names, amounts, businesses, files and codes its
graders substring-match on) are also denied anywhere in an example's
conversation: see ``fact_values`` and ``fact_hits``. The scan covers user,
assistant and tool messages and tool-call arguments, not system prompts or tool
schemas, which are production text pinned to the live code."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date
from functools import lru_cache

DATASET_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# builder/ -> ask-lora-r2/ -> training/ -> repo root (same rule as boot.py)
REPO = os.environ.get("WISP_REPO") or os.path.abspath(os.path.join(DATASET_DIR, "..", ".."))
THRESHOLD = 0.7
SEEDS = range(50)

_DAYS = r"(?:mon|tues|tue|wednes|wed|thurs|thu|fri|satur|sat|sun)(?:day)?s?"
_MONTHS = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)")


def fold(text: str) -> str:
    s = text.lower().replace("’", "'").replace("'", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(rf"\b{_DAYS}\b", " wd ", s)
    s = re.sub(rf"\b{_MONTHS}\b", " mo ", s)
    s = re.sub(r"\b\d+(?:st|nd|rd|th)?\b", " n ", s)
    return re.sub(r"\s+", " ", s).strip()


def trigrams(text: str) -> set[str]:
    s = f" {fold(text)} "
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _entry(id_: str, prompt: str) -> dict:
    return {"id": id_, "prompt": prompt, "fold": fold(prompt), "tri": trigrams(prompt)}


def load_eval(path: str | None = None) -> list[dict]:
    path = path or os.path.join(DATASET_DIR, "eval_questions.jsonl")
    out = []
    for line in open(path):
        q = json.loads(line)
        out.append(_entry(q["id"], q["prompt"]))
    return out


# ------------------------------------------------------------------ chat eval
@lru_cache(maxsize=1)
def load_chat(path: str | None = None) -> tuple[dict, ...]:
    """Every user turn of the chat-quality eval (chateval_prompts.yaml, a copy of the
    eval's prompts.yaml): single prompts, each wording of a variety set and each turn
    of a conversation. Ids are ``chat:<unit id>[#n]``."""
    import yaml
    path = path or os.path.join(DATASET_DIR, "chateval_prompts.yaml")
    with open(path) as f:
        units = yaml.safe_load(f)["units"]
    out = []
    for u in units:
        texts = [u["prompt"]] if "prompt" in u else list(u.get("prompts") or u.get("turns") or [])
        for i, t in enumerate(texts):
            out.append(_entry(f"chat:{u['id']}" + (f"#{i}" if len(texts) > 1 else ""), str(t)))
    return tuple(out)


# ------------------------------------------------------------------ toolbench
def _toolbench():
    """(load_cases, make_facts, FACT_POOLS) from this checkout's scripts/toolbench."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    if not os.path.isdir(os.path.join(REPO, "scripts", "toolbench", "cases")):
        raise RuntimeError(f"toolbench cases not found under {REPO}; the leak check cannot run without them")
    from scripts.toolbench.cases import load_cases
    from scripts.toolbench.world import FACT_POOLS, make_facts
    return load_cases, make_facts, FACT_POOLS


@lru_cache(maxsize=1)
def load_toolbench() -> tuple[dict, ...]:
    """Every toolbench prompt and history user turn (all splits), bound across the
    canonical world and seeds 0-49, one entry per distinct folded form."""
    load_cases, make_facts, _ = _toolbench()
    worlds = [make_facts(None)] + [make_facts(s) for s in SEEDS]
    seen: dict[str, dict] = {}
    for c in load_cases():
        for facts in worlds:
            b = c.bind(facts)
            texts = [(f"tb:{c.id}", b.prompt)]
            for i, turn in enumerate(b.history):
                if isinstance(turn, dict) and isinstance(turn.get("user"), str) and turn["user"].strip():
                    texts.append((f"tb:{c.id}#h{i}", turn["user"]))
            for id_, text in texts:
                f = fold(text)
                if f and f not in seen:
                    seen[f] = _entry(id_, text)
    return tuple(seen.values())


# Fact values that are common words, weekdays, kinds of event or calendar names
# rather than something a grader can fingerprint.
_GENERIC = {"dentist", "family", "halloween", "friday", "saturday", "monday", "thursday", "yoga", "passport",
            "book club", "team standup", "vendor sync", "pay rent", "family trip", "us holidays", "pasta",
            "laundry", "oat", "example corp", "yoga class"}
# Toolbench values that are ALSO the kit persona's own: the kit's user is Sam Rivera
# and one friend is Maya Chen, so denying them would reject every example. The
# prompt-level leak check still applies to the questions that name them.
_KIT_PERSONA = {"sam rivera", "maya"}
_TIME = re.compile(r"^\d{1,2}(?::\d\d)?\s*(?:am|pm)?$", re.I)
_DATE = re.compile(rf"^{_MONTHS}\s+\d{{1,2}}$", re.I)


def _distinctive(key: str, value: str) -> bool:
    if key.endswith(("_first", "_tail")) or value.lower() in _GENERIC | _KIT_PERSONA:
        return False
    if _TIME.match(value) or _DATE.match(value) or re.fullmatch(rf"{_DAYS}|{_MONTHS}", value, re.I):
        return False
    if re.search(r"\d", value):
        return len(value) >= 4
    return len(value) >= 4 and bool(re.search(r"[A-Za-z]{4}", value))


@lru_cache(maxsize=1)
def fact_values() -> tuple[str, ...]:
    """The toolbench fact values the graders match on, lowercased: every distinctive
    CANONICAL (gate world) value, plus the pool alternatives that carry digits
    (amounts, ids, codes, file names: never a coincidence). Date-derived facts and
    common words are left out. Pool NAMES stay allowed: seeded worlds exist so
    training data can vary them, and several overlap the kit persona's own."""
    _, make_facts, pools = _toolbench()
    base = make_facts(None)
    moved = make_facts(None, today=date(2027, 3, 3))
    derived = {k for k in base if base[k] != moved[k]}
    out: set[str] = set()
    for key, value in base.items():
        if key not in derived and _distinctive(key, str(value)):
            out.add(str(value).lower())
    for key, pool in pools.items():
        for value in pool:
            value = str(value)
            if re.search(r"\d", value) and len(value) >= 5 and _distinctive(key, value):
                out.add(value.lower())
    return tuple(sorted(out))


@lru_cache(maxsize=1)
def _fact_regex():
    vals = sorted(fact_values(), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(v) for v in vals) + r")(?![a-z0-9])", re.I)


def conversation_text(ex: dict) -> str:
    """User, assistant and tool message text plus tool-call arguments (no system prompt, no tool schemas)."""
    parts = []
    for m in ex.get("messages") or []:
        if m.get("role") == "system":
            continue
        parts.append(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments")
            parts.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
    return "\n".join(parts)


def fact_hits(text: str) -> list[str]:
    """Toolbench fact values (lowercased) found in ``text``."""
    return sorted({m.group(0).lower() for m in _fact_regex().finditer(text)})


# ------------------------------------------------------------------ index
class LeakIndex:
    def __init__(self, questions: list[dict] | None = None, toolbench: bool | None = None, chat: bool = False):
        """``questions`` defaults to the eval questions plus the toolbench prompts;
        pass your own list to index just that (toolbench and the chat eval are then off
        unless asked; the chat eval's prompts are indexed whenever toolbench is)."""
        custom = questions is not None
        questions = list(questions) if custom else load_eval()
        if toolbench is None:
            toolbench = not custom
        if toolbench:
            questions += list(load_toolbench())
        if toolbench or chat:
            questions += list(load_chat())
        self.questions = questions
        self.by_fold: dict[str, dict] = {}
        for q in questions:
            self.by_fold.setdefault(q["fold"], q)
        self.folds = {f: q["id"] for f, q in self.by_fold.items()}
        self.inverted: dict[str, list[int]] = {}
        for i, q in enumerate(questions):
            for g in q["tri"]:
                self.inverted.setdefault(g, []).append(i)

    def nearest(self, prompt: str) -> tuple[float, dict | None]:
        f, t = fold(prompt), trigrams(prompt)
        if f in self.by_fold:
            return 1.0, self.by_fold[f]
        shared = Counter(i for g in t for i in self.inverted.get(g, ()))
        best, who = 0.0, None
        for i, inter in shared.items():
            q = self.questions[i]
            j = inter / max(1, len(t) + len(q["tri"]) - inter)
            if j > best:
                best, who = j, q
        return best, who

    def leaks(self, prompt: str) -> tuple[bool, float, dict | None]:
        score, q = self.nearest(prompt)
        return score >= THRESHOLD, score, q

    fact_hits = staticmethod(fact_hits)
