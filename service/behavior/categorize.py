"""Keyword taxonomy: event/reminder title -> one of ``types.CATEGORIES``.

Categories are what let "Morning lift", "Spin class" and "Gym" count as the same
habit, so the rules favour precision: whole-word matches only ("run" must not
fire on "Cash runway review"), and ordered so the more specific reading wins
("Dev / Robin 1:1" is a one-on-one, not a meeting; "Hiring sync" is a meeting,
"Sync with Dev" a one-on-one). Whatever stays "other" may be labelled by the
optional Phraser, constrained to the same fixed list and cached by title.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

from service.behavior.types import CATEGORIES

CATEGORIZED_KINDS = frozenset({"event", "event_cancelled", "event_moved", "event_declined",
                               "reminder", "reminder_done", "reminder_dismissed"})


def _words(*words: str) -> re.Pattern:
    return re.compile(r"(?<![\w])(?:" + "|".join(words) + r")(?![\w])", re.I)


# (category, pattern) in priority order; the first match wins.
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("one_on_one", re.compile(r"(?<![\w])(?:1\s*:\s*1|1\s*on\s*1|one[\s-]on[\s-]one)(?![\w])"
                              r"|\bsync\s+with\b", re.I)),
    # "Dev / Robin", "Sam Lee / Jo": two capitalised names around a slash (case-sensitive,
    # so "Q3 planning / budget" stays a meeting).
    ("one_on_one", re.compile(r"^\s*[A-Z][a-z'.-]+(?:\s+[A-Z][a-z'.-]+)?\s*/\s*[A-Z][a-z'.-]+"
                              r"(?:\s+[A-Z][a-z'.-]+)?(?:\s|$)")),
    ("fitness", _words(r"gym", r"workouts?", r"work\s*out", r"lift(?:ing)?", r"run(?:ning)?",
                       r"jog(?:ging)?", r"yoga", r"pilates", r"spin", r"swim(?:ming)?", r"climb(?:ing)?",
                       r"training", r"crossfit", r"bootcamp", r"cycling", r"tennis", r"hike")),
    ("health", _words(r"dentist", r"doctor", r"dr\.?", r"therapy", r"therapist", r"physio",
                      r"checkup", r"check-up", r"clinic", r"appointment")),
    ("travel", _words(r"flight", r"fly(?:ing)?", r"train", r"drive", r"travel", r"airport",
                      r"commute", r"uber", r"taxi")),
    ("focus", _words(r"focus", r"deep\s+work", r"heads\s+down", r"block", r"no\s+meetings",
                     r"writing\s+time", r"maker\s+time")),
    ("meal", _words(r"lunch", r"dinner", r"breakfast", r"brunch", r"coffee")),
    ("class", _words(r"class", r"lecture", r"seminar", r"course", r"lesson", r"workshop")),
    ("social", _words(r"party", r"drinks", r"happy\s+hour", r"birthday", r"wedding", r"bbq",
                      r"game\s+night")),
    ("errand", _words(r"groceries", r"grocery", r"pick\s*up", r"pickup", r"drop\s*off",
                      r"errands?", r"market", r"pharmacy", r"post\s+office", r"laundry")),
    ("meeting", _words(r"standup", r"stand-up", r"sync", r"review", r"meeting", r"interview",
                       r"retro", r"planning", r"check-in", r"check\s+in", r"debrief", r"all-hands",
                       r"offsite", r"kickoff", r"demo", r"walkthrough", r"board")),
    ("call", _words(r"call", r"phone", r"facetime", r"zoom")),
    ("personal", _words(r"haircut", r"family", r"kids?", r"school", r"date\s+night")),
)


def categorize(subject: str, attrs: Mapping[str, Any] | None = None) -> str:
    """Keyword category for a title; "other" when nothing matches.

    A "call" with three or more attendees is a meeting (a group call is a
    meeting; "Call Mom" is a call)."""
    text = (subject or "").strip()
    if not text:
        return "other"
    for category, rule in _RULES:
        if rule.search(text):
            if category == "call" and _attendee_count(attrs) >= 3:
                return "meeting"
            return category
    return "other"


def _attendee_count(attrs: Mapping[str, Any] | None) -> int:
    if not attrs:
        return 0
    people = attrs.get("attendees")
    if isinstance(people, (list, tuple)):
        return len(people)
    try:
        return int(attrs.get("attendee_count") or 0)
    except (TypeError, ValueError):
        return 0


def needs_category(kind: str) -> bool:
    return kind in CATEGORIZED_KINDS


def label_leftovers(titles: Iterable[str], cache: dict[str, str],
                    categorize_fn: Callable[[list[str], tuple[str, ...]], dict[str, str]] | None,
                    *, limit: int = 50) -> bool:
    """Ask the phraser to label uncached "other" titles, in place in ``cache``.

    Titles the phraser omits (or answers outside the fixed list) are cached as
    "other" so we never ask about them again. Any phraser failure is swallowed:
    the keyword category stands. Returns True when the cache changed."""
    pending = sorted({t for t in titles if t and t not in cache})[:limit]
    if not pending or categorize_fn is None:
        return False
    try:
        answer = categorize_fn(pending, CATEGORIES) or {}
    except Exception:  # noqa: BLE001 - the model is optional; keyword categories stand
        return False
    for title in pending:
        label = answer.get(title) if isinstance(answer, dict) else None
        cache[title] = label if label in CATEGORIES else "other"
    return True
