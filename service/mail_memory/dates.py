"""Turn the date phrase a model copied out of an email into a due date.

Gemma reads deadlines well but cannot do calendar arithmetic: "by Friday" in an
email sent Wednesday 2026-08-05 came back as 2026-08-11 in every run of the
2026-10-26 bench. So the model only copies the date PHRASE, verbatim, and this
module resolves it against the email's sent time, reusing the attention
resolver (`service.attention.resolve`), which got that case right.

IMPORTANT rules:
- One phrase, one date. A phrase naming two dates ("moved from November 12 to
  November 19") is refused, never resolved: the resolver picks the first date,
  which in a correction is the superseded one.
- Mail-only shims the chat resolver lacks: month-only dates ("October 2026",
  due at the end of that month), "next week" / "in N days|weeks", and numeric
  dates read in the user's month/day order.
- Pure apart from `month_first()`, which reads the macOS locale once.
"""
from __future__ import annotations

import calendar
import functools
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from service.attention.resolve import resolve
from service.temporal_facts import EvidenceContext, extract_temporal_facts

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_MONTH = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_MONTH_ONLY = re.compile(rf"^\W*(?:in|by|for|during|until|before|around)?\s*(?:early|mid|late)?\s*"
                         rf"(?:-\s*)?({_MONTH})(?:\s*,?\s*(\d{{4}}))?\W*$", re.I)
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+\d{{1,2}}\b|\b\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b", re.I)
_RELATIVE = re.compile(r"\b(?:in|within)\s+(a|one|two|three|four|\d{1,2})\s+(day|week)s?\b"
                       r"|\b(next|this)\s+week\b", re.I)
_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_CHANGE = re.compile(r"\b(?:moved|changed|rescheduled|postponed|instead of|from)\b", re.I)


@dataclass(frozen=True)
class Due:
    day: date | None
    precision: str          # "day", "week" or "month"; "" when unresolved
    reason: str = ""        # why nothing resolved


@functools.lru_cache(maxsize=1)
def month_first() -> bool:
    """True when the user writes dates month/day (en_US and most of the Americas)."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    country = out.split("@")[0].rpartition("_")[2].upper()
    return not country or country in {"US", "CA", "PH", "FM", "MH", "PW", "BZ"}


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _date_mentions(phrase: str, sent: datetime, tz: str) -> int:
    facts = extract_temporal_facts(phrase, captured_at=sent, timezone=tz,
                                   evidence=EvidenceContext("mail_memory", "phrase")).facts
    spans = sum(2 if (f.end is not None or f.relation in ("range", "alternatives")) else 1
                for f in facts if f.start.month is not None or f.start.day is not None)
    return max(spans, len(_MONTH_DAY.findall(phrase)))


def resolve_due(phrase: str, sent: datetime, tz: str, *, month_first_order: bool | None = None) -> Due:
    """The due date `phrase` names, read as of `sent` (an aware datetime) in `tz`."""
    phrase = (phrase or "").strip()
    if not phrase:
        return Due(None, "", "no_phrase")
    sent = sent.astimezone(ZoneInfo(tz))
    if _date_mentions(phrase, sent, tz) > 1 or (_CHANGE.search(phrase) and _MONTH_DAY.search(phrase)):
        return Due(None, "", "several_dates")

    m = _MONTH_ONLY.match(phrase)
    if m:
        month = _MONTHS[m.group(1).lower().rstrip(".")]
        year = int(m.group(2)) if m.group(2) else (sent.year if month >= sent.month else sent.year + 1)
        return Due(_month_end(year, month), "month")

    m = _RELATIVE.search(phrase)
    if m and not _MONTH_DAY.search(phrase):
        if m.group(3):
            monday = sent.date() - timedelta(days=sent.weekday())
            offset = 7 if m.group(3).lower() == "next" else 0
            return Due(monday + timedelta(days=offset + 6), "week")
        n = _NUMBERS.get(m.group(1).lower()) or int(m.group(1))
        return Due(sent.date() + timedelta(days=n * (7 if m.group(2).lower() == "week" else 1)), "day")

    first = month_first() if month_first_order is None else month_first_order
    numeric = _NUMERIC.search(phrase)
    if numeric:
        a, b, year = int(numeric.group(1)), int(numeric.group(2)), numeric.group(3)
        month, day = (a, b) if first else (b, a)
        if not (1 <= month <= 12):
            return Due(None, "", "bad_numeric_date")
        written = f"{calendar.month_name[month]} {day}" + (f", {'20' + year if len(year) == 2 else year}" if year else "")
        phrase = phrase[:numeric.start()] + written + phrase[numeric.end():]

    got = resolve(phrase, sent, tz, "mail_memory")
    if got.day is None:
        return Due(None, "", got.blocked or "unresolved")
    return Due(got.day, "day")
