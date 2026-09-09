"""Source-independent reference selection, with explicit search coverage.

Readers filter domain-specific fields; this module owns cardinality and stable
selection. Limiting displayed candidates never changes the match count.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol
import re


@dataclass
class SourceRef:
    kind: str
    query: str = ""
    hints: dict[str, str] = field(default_factory=dict)


@dataclass
class Candidate:
    kind: str
    id: str
    label: str
    fields: dict = field(default_factory=dict)
    actionable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceBatch:
    candidates: list[Candidate] = field(default_factory=list)
    available: bool = True
    complete: bool = True  # complete within `scope`, never a claim about all mail
    scope: str = "the searched source"
    synced_at: float = 0
    reason: str = ""


@dataclass
class Resolution:
    status: Literal["resolved", "ambiguous", "no_match", "unavailable", "not_actionable"]
    candidates: list[Candidate] = field(default_factory=list)
    snapshot: dict | None = None
    question: str = ""
    total_matches: int = 0
    scope: str = ""
    synced_at: float = 0


class SourceReader(Protocol):
    kind: str

    def candidates(self, ref: SourceRef, *, now: datetime) -> SourceBatch: ...


def resolve_reference(ref: SourceRef, reader: SourceReader, *, now: datetime,
                      max_offered: int = 5) -> Resolution:
    if max_offered < 1 or ref.kind != reader.kind:
        raise ValueError("invalid reference reader or offer limit")
    batch = reader.candidates(ref, now=now)
    if not batch.available or not batch.complete:
        return Resolution("unavailable", question=batch.reason or
                          f"I couldn’t finish checking {batch.scope}. Nothing was sent.",
                          scope=batch.scope, synced_at=batch.synced_at)
    # IDs are domain-scoped identities. Never merge by label or subject.
    candidates = list({c.id: c for c in batch.candidates}.values())
    offered = candidates[:max_offered]
    base = dict(candidates=offered, total_matches=len(candidates),
                scope=batch.scope, synced_at=batch.synced_at)
    if not candidates:
        return Resolution("no_match", question=f"I found no matching item in {batch.scope}. Which item should I use?", **base)
    if len(candidates) == 1:
        item = candidates[0]
        if not item.actionable:
            return Resolution("not_actionable", question=
                              f"I can see {item.label}, but can’t act on it yet. Refresh it in its source app.", **base)
        return Resolution("resolved", snapshot=item.to_dict(), **base)
    lines = [f"{i + 1}. {item.label}" for i, item in enumerate(offered)]
    remaining = len(candidates) - len(offered)
    suffix = f"\nThere are {remaining} more matches; narrow the sender, topic or account to see them." if remaining else ""
    return Resolution("ambiguous", question="Which one do you mean?\n" + "\n".join(lines) + suffix, **base)


def select_candidate(offered: list[dict], reply: str) -> Candidate | None:
    value = reply.strip().strip(".! ").casefold()
    ordinals = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
    value = re.sub(r"^(?:the|use|choose)\s+", "", value)
    value = re.sub(r"\s+one$", "", value)
    index = ordinals.get(value)
    if value.isdigit():
        index = int(value) - 1
    if index is not None:
        return Candidate(**offered[index]) if 0 <= index < len(offered) else None
    if value in {"yes", "sure", "that one"}:
        return Candidate(**offered[0]) if len(offered) == 1 else None
    if not value or reply.rstrip().endswith("?"):
        return None
    # Selection must identify an offered label, not a new action or query.
    if re.search(r"\b(?:check|search|send|reply|delete|open|what|why|how)\b", value):
        return None
    tokens = re.findall(r"[\w@.]+", value)
    hits = [item for item in offered if tokens and all(
        token in item["label"].casefold() for token in tokens)]
    return Candidate(**hits[0]) if len(hits) == 1 else None
