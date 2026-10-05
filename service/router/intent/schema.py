"""Versioned read-only intent contract. Inspired by Claude's architecture schema
(678aab4) and the independently evaluated intent_v2 prototype; no tool authority.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

DOMAINS = frozenset({"calendar", "reminders", "email", "messages", "notes"})
NAMED_PERIODS = ("today", "tomorrow", "yesterday", "tonight", "this week", "next week",
                 "last week", "this weekend", "next weekend", "this month", "next month", "last month")
TIME_PROPERTIES = {
    "named": {"type": "string", "enum": list(NAMED_PERIODS)},
    "date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    "start": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    "end": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
    "month": {"type": "string", "pattern": r"^\d{4}-\d{2}$"},
    "last_n_days": {"type": "integer", "minimum": 1, "maximum": 366},
    "rolling_days": {"type": "integer", "minimum": 1, "maximum": 60},
}
SOURCE_PROPERTIES = {
    "domain": {"type": "string", "enum": sorted(DOMAINS)},
    "operation": {"type": "string", "enum": ["overview", "records", "free_time"]},
    "time": {"type": "object", "additionalProperties": False, "properties": TIME_PROPERTIES},
    **{k: {"type": "string", "minLength": 1, "maxLength": 200}
       for k in ("query", "conversation", "account")},
    "scope": {"type": "string", "enum": ["all", "today", "tomorrow", "overdue", "upcoming"]},
    "unread": {"type": "boolean"},
    "count": {"type": "integer", "minimum": 1, "maximum": 100},
    "minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["version", "kind", "sources", "excluded_sources", "unsupported_constraints"],
    "properties": {
        "version": {"type": "integer", "enum": [1]},
        "kind": {"type": "string", "enum": ["read", "inline", "none", "unsupported"]},
        "sources": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["domain", "operation"], "properties": SOURCE_PROPERTIES}},
        "excluded_sources": {"type": "array", "uniqueItems": True,
                             "items": {"type": "string", "enum": sorted(DOMAINS | {"web"})}},
        "unsupported_constraints": {"type": "array", "maxItems": 8,
                                    "items": {"type": "string", "maxLength": 200}},
    },
}


@dataclass(frozen=True)
class TimeScope:
    named: str | None = None
    date: str | None = None
    start: str | None = None
    end: str | None = None
    month: str | None = None
    last_n_days: int | None = None
    rolling_days: int | None = None

    def period(self) -> str | None:
        if self.start:
            return f"{self.start} to {self.end}"
        if self.last_n_days:
            return f"last {self.last_n_days} days"
        return self.named or self.date or self.month


@dataclass(frozen=True)
class SourceIntent:
    domain: str
    operation: str
    time: TimeScope | None = None
    query: str | None = None
    conversation: str | None = None
    account: str | None = None
    unread: bool | None = None
    count: int | None = None
    minutes: int | None = None
    scope: str | None = None


@dataclass(frozen=True)
class Intent:
    version: int
    kind: str
    sources: tuple[SourceIntent, ...]
    excluded_sources: frozenset[str] = frozenset()
    unsupported_constraints: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningResult:
    disposition: Literal["compiled", "clarify", "declined"]
    calls: tuple[tuple[str, dict], ...] = ()
    response: str = ""
    intent: Intent | None = None
    reason: str = ""
    attempts: int = 0
