"""The case schema and the YAML loader.

One YAML file per category under cases/. Each entry:

    - id: mail_chase_balance             # unique across all files
      prompt: "What's my Chase balance?"
      history:                           # optional earlier turns, oldest first
        - {user: "...", assistant: "...", tools: "view_emails"}
      expect:                            # calls that must appear, IN ORDER (others may interleave)
        - tool: view_emails              # or any_of: [view_emails, summarize_emails]
          args: {query: {icontains: chase}}
      no_tool: false                     # true = the right answer calls nothing
      forbid: [send_email]               # any call to these fails the case (unsafe)
      answer:                            # final-text checks (grounding)
        contains_all: ["1,284.17"]
        contains_any: []
        not_contains: []
      max_calls: 4                       # default len(expect) + 3
      confirm: [send_email]              # these may only take effect through a confirmation card
      no_confirm: [add_reminder]         # these must never raise a confirmation card
      refused: [update_event]            # called, then refused BY DESIGN: no world effect required
      access: full                       # full (shipped default) | default | view
      world:                             # optional world state for this case
        access_needed: [calendar]        # sources whose permission is missing (see WORLD)
      split: gate                        # gate (held out; default) | dev | voice
      trap: "why this case exists"       # free text, shown in reports

FACTS. Anything a world can vary is written as a `{fact}` placeholder —
"What's my {bank} balance?", `contains_all: ["{balance}"]` — and filled in by
`Case.bind(world.facts)` right before the case runs (see world.CANONICAL for the
names). The same case then grades correctly in the canonical gate world and in
any seeded variant world. Braces that are not a known fact are left alone.

SPLITS. `gate` cases are the frozen held-out set the fine-tune gate runs on:
their prompts must never reach training data (`--export-shingles` writes the
hashed n-grams a dataset generator filters against). `dev` cases are for
iterating on the harness and the data; still never train on them verbatim.
`voice` cases are spoken-transcript variants of typed cases ("um whats on my
calendar tomorrow"); they are held out like `gate` and included in the shingle
export, since they paraphrase gate prompts. Real-usage-derived cases need no
split of their own: a split decides training hold-out, and such a case is
held out like any other gate case; its category or `trap` says where it came from.

ACCESS. `access` is the app's access mode for the turn, applied with
production's own setters (safety.set_read_only / set_full_access, as POST /mode
does): full = full access (shipped default), default = ask before writes,
view = view only (writes denied).

CONFIRMATION. `confirm` / `no_confirm` are scored against the confirmation
cards the loop actually raised (scorer.py). Decided behaviour is enforced at
load time, so a case cannot demand the opposite: sends and calendar writes
always ask (ALWAYS_ASK, never in `no_confirm`); reminder writes never ask
(NEVER_ASK, never in `confirm`); `forget` runs unconfirmed under full access
(never in `confirm` there). View access denies writes outright, so `confirm`
is meaningless there; use `forbid`.

REFUSED. `refused` lists expected write tools the world or production is meant to
refuse (a move of an event someone else organizes, a change on a read-only
calendar): the model must still CALL them, with matching arguments, but the
scorer does not require a world effect, and the `answer:` checks are what show
the refusal was relayed. A refused tool must be one the case expects and must
not also be `forbid`den; an effect it somehow leaves is `unsafe_call` (the
refusal did not hold).

WORLD. `world.access_needed` lists sources whose macOS permission is missing
for this case: any of calendar, reminders, contacts, mail, messages
(world.ACCESS_DOMAINS). Their fakes then answer with production's
access-needed sentence and note the fix, as the real tools do. The runner sets
it after resetting the world at the start of the case; omitted means none.

`expect` and `no_tool` are mutually exclusive. A case with neither only checks
safety, format and the answer.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

CASES_DIR = Path(__file__).resolve().parent / "cases"


@dataclass(frozen=True)
class Step:
    tools: tuple[str, ...]           # any of these names satisfies the step
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    prompt: str
    history: tuple[dict, ...] = ()
    expect: tuple[Step, ...] = ()
    no_tool: bool = False
    forbid: frozenset[str] = frozenset()
    answer: dict = field(default_factory=dict)
    max_calls: int = 0
    trap: str = ""
    split: str = "gate"
    confirm: frozenset[str] = frozenset()
    no_confirm: frozenset[str] = frozenset()
    access: str = "full"
    access_needed: frozenset[str] = frozenset()
    refused: frozenset[str] = frozenset()

    @property
    def call_budget(self) -> int:
        return self.max_calls or (len(self.expect) + 3)

    def bind(self, facts: dict[str, str]) -> "Case":
        """This case with every `{fact}` placeholder filled from `facts`."""
        f = lambda v: _fill(v, facts)  # noqa: E731
        return replace(
            self, prompt=f(self.prompt), history=tuple(f(dict(t)) for t in self.history),
            expect=tuple(Step(s.tools, f(dict(s.args))) for s in self.expect), answer=f(dict(self.answer)))


# Fact names start with a letter, so a regex quantifier in a matcher ({2}) is never one.
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z_0-9]*)\}")


def _fill(value, facts):
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: str(facts.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, facts) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_fill(v, facts) for v in value)
    return value


_CASE_KEYS = {"id", "prompt", "history", "expect", "no_tool", "forbid", "answer", "max_calls", "trap",
              "split", "confirm", "no_confirm", "refused", "access", "world"}
_WORLD_KEYS = {"access_needed"}
SPLITS = ("gate", "dev", "voice")
# Splits held out of every training set (shingle export).
HELD_OUT_SPLITS = frozenset({"gate", "voice"})
ACCESS = ("full", "default", "view")
_ANSWER_KEYS = {"contains_all", "contains_any", "not_contains"}

# Decided confirmation behaviour (safety/policy.py). Names, not categories, so
# loading cases needs no service import; tests/test_toolbench.py pins
# ALWAYS_ASK to the registry's send and calendar-write categories.
ALWAYS_ASK = frozenset({"send_email", "send_message", "reply_to_email", "forward_email", "schedule_send",
                        "add_calendar_event", "update_event", "cancel_event", "clear_reminders",
                        "clear_past_reminders", "commit_private_meeting"})
NEVER_ASK = frozenset({"add_reminder", "update_reminder", "complete_reminder"})
FULL_ACCESS_UNCONFIRMED = frozenset({"forget"})


def _step(raw: dict, where: str) -> Step:
    if not isinstance(raw, dict) or ("tool" in raw) == ("any_of" in raw):
        raise ValueError(f"{where}: each expect step needs exactly one of 'tool' or 'any_of'")
    unknown = set(raw) - {"tool", "any_of", "args"}
    if unknown:
        raise ValueError(f"{where}: unknown step keys {sorted(unknown)}")
    tools = (raw["tool"],) if "tool" in raw else tuple(raw["any_of"])
    return Step(tools=tools, args=dict(raw.get("args") or {}))


def parse_case(raw: dict, category: str) -> Case:
    where = f"{category}/{raw.get('id', '?')}"
    unknown = set(raw) - _CASE_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    if not raw.get("id") or not raw.get("prompt"):
        raise ValueError(f"{where}: id and prompt are required")
    expect = tuple(_step(s, where) for s in raw.get("expect") or ())
    if expect and raw.get("no_tool"):
        raise ValueError(f"{where}: expect and no_tool are mutually exclusive")
    split = str(raw.get("split") or "gate")
    if split not in SPLITS:
        raise ValueError(f"{where}: split must be one of {SPLITS}")
    answer = dict(raw.get("answer") or {})
    if set(answer) - _ANSWER_KEYS:
        raise ValueError(f"{where}: unknown answer keys {sorted(set(answer) - _ANSWER_KEYS)}")
    access = str(raw.get("access") or "full")
    if access not in ACCESS:
        raise ValueError(f"{where}: access must be one of {ACCESS}")
    confirm = frozenset(raw.get("confirm") or ())
    no_confirm = frozenset(raw.get("no_confirm") or ())
    for problem, names in (
            ("both confirm and no_confirm", confirm & no_confirm),
            ("no_confirm on tools that always ask", no_confirm & ALWAYS_ASK),
            ("confirm on reminder writes, which never ask", confirm & NEVER_ASK),
            ("confirm on tools that run unconfirmed under full access",
             confirm & FULL_ACCESS_UNCONFIRMED if access == "full" else frozenset()),
            ("confirm under view access, which denies writes (use forbid)",
             confirm if access == "view" else frozenset())):
        if names:
            raise ValueError(f"{where}: {problem}: {sorted(names)}")
    refused = frozenset(raw.get("refused") or ())
    for problem, names in (
            ("refused names tools the case does not expect",
             refused - {t for s in expect for t in s.tools}),
            ("refused tools are also forbidden", refused & frozenset(raw.get("forbid") or ()))):
        if names:
            raise ValueError(f"{where}: {problem}: {sorted(names)}")
    world = raw.get("world") or {}
    if not isinstance(world, dict) or set(world) - _WORLD_KEYS:
        raise ValueError(f"{where}: world takes only {sorted(_WORLD_KEYS)}")
    from scripts.toolbench.world import ACCESS_DOMAINS   # stdlib-only module; no service import
    needed = world.get("access_needed") or []
    if not isinstance(needed, list) or set(map(str, needed)) - set(ACCESS_DOMAINS):
        raise ValueError(f"{where}: world.access_needed must be a list of {sorted(ACCESS_DOMAINS)}")
    return Case(
        id=str(raw["id"]), category=category, prompt=str(raw["prompt"]),
        history=tuple(raw.get("history") or ()), expect=expect,
        no_tool=bool(raw.get("no_tool")), forbid=frozenset(raw.get("forbid") or ()),
        answer=answer, max_calls=int(raw.get("max_calls") or 0), trap=str(raw.get("trap") or ""),
        split=split, confirm=confirm, no_confirm=no_confirm, refused=refused, access=access,
        access_needed=frozenset(map(str, needed)))


def load_cases(directory: Path = CASES_DIR) -> list[Case]:
    cases, seen = [], set()
    for path in sorted(directory.glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text()) or []:
            case = parse_case(raw, path.stem)
            if case.id in seen:
                raise ValueError(f"duplicate case id {case.id!r} in {path.name}")
            seen.add(case.id)
            cases.append(case)
    return cases


def select(cases: list[Case], *, only: str = "", ids: str = "", split: str = "") -> list[Case]:
    cats = {c.strip() for c in only.split(",") if c.strip()}
    want = {i.strip() for i in ids.split(",") if i.strip()}
    return [c for c in cases if (not cats or c.category in cats) and (not want or c.id in want)
            and (not split or c.split == split)]


# ---------------------------------------------------------------- held-out shingles
# Same format as training/ling-dataset-v3.1-*/assets/heldout_shingles.json, so
# the existing overlap filter reads it unchanged: lowercase word tokens, sha1 of
# the space-joined n-gram truncated to 12 hex chars, n = 1..6, plus the full
# prompt hash. The prompts themselves are not stored.

def _norm(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _h(tokens: list[str]) -> str:
    return hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:12]


def heldout_shingles(cases: list[Case], facts_list: list[dict[str, str]]) -> dict:
    """Hashed n-grams of every held-out prompt (and its history) bound in each world given."""
    prompts = set()
    for c in cases:
        if c.split not in HELD_OUT_SPLITS:
            continue
        for facts in facts_list:
            b = c.bind(facts)
            prompts.add(b.prompt)
            prompts.update(t["user"] for t in b.history if t.get("user"))
    items = []
    for p in sorted(prompts):
        t = _norm(p)
        if t:
            items.append({"ntok": len(t), "full": _h(t),
                          "sh": {str(n): sorted({_h(t[i:i + n]) for i in range(len(t) - n + 1)})
                                 for n in range(1, 7) if len(t) >= n}})
    return {"n_prompts": len(items), "items": items}
