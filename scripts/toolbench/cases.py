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
      trap: "why this case exists"       # free text, shown in reports

`expect` and `no_tool` are mutually exclusive. A case with neither only checks
safety, format and the answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

    @property
    def call_budget(self) -> int:
        return self.max_calls or (len(self.expect) + 3)


_CASE_KEYS = {"id", "prompt", "history", "expect", "no_tool", "forbid", "answer", "max_calls", "trap"}
_ANSWER_KEYS = {"contains_all", "contains_any", "not_contains"}


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
    answer = dict(raw.get("answer") or {})
    if set(answer) - _ANSWER_KEYS:
        raise ValueError(f"{where}: unknown answer keys {sorted(set(answer) - _ANSWER_KEYS)}")
    return Case(
        id=str(raw["id"]), category=category, prompt=str(raw["prompt"]),
        history=tuple(raw.get("history") or ()), expect=expect,
        no_tool=bool(raw.get("no_tool")), forbid=frozenset(raw.get("forbid") or ()),
        answer=answer, max_calls=int(raw.get("max_calls") or 0), trap=str(raw.get("trap") or ""))


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


def select(cases: list[Case], *, only: str = "", ids: str = "") -> list[Case]:
    cats = {c.strip() for c in only.split(",") if c.strip()}
    want = {i.strip() for i in ids.split(",") if i.strip()}
    return [c for c in cases if (not cats or c.category in cats) and (not want or c.id in want)]
