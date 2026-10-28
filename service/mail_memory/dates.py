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
- An impossible clock ("Friday at 28") costs only the clock: the day still
  resolves. The resolver refuses the whole phrase, so it is read again bare.
- A clock range ("6:00 PM-7:30 PM") is dropped before anything else reads the
  phrase: a due date never carries a clock, and the range read as two dates.
- A weekday beside a date ("Tuesday Sep 22") counts only when it is that
  date's weekday; a mismatch stays refused.
- Mail-only shims the chat resolver lacks: month-only dates ("October 2026",
  due at the end of that month), "next week" / "in N days|weeks" /
  "N days left|remaining|to go", and numeric
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
_COUNT = r"a|one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2}"
_RELATIVE = re.compile(rf"\b(?:in|within)\s+({_COUNT})\s+(day|week)s?\b"
                       rf"|\b({_COUNT})\s+(day|week)s?\s+(?:left|remaining|to go)\b"
                       r"|\b(next|this)\s+week\b", re.I)
_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10}
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_SEPT = re.compile(r"\bsept\b\.?", re.I)
_CHANGE = re.compile(r"\b(?:moved|changed|rescheduled|postponed|instead of|from)\b", re.I)
# A clock, with its "at": "at 28", "at 9:75", "25:30", "13pm", "0 p.m.". Never a date:
# "10/28", "28th" and "2026-10-28" are left alone by the lookarounds.
_CLOCK = re.compile(r"(?:(?<!\w)(?:at|@)\s*)?(?<![\w/:.-])\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\b\.?)"
                    r"|(?:(?<!\w)(?:at|@)\s*)?(?<![\w/:.-])\d{1,2}:\d{2}(?![\w/:-])"
                    r"|(?<!\w)(?:at|@)\s*\d{1,2}(?![\w/:.-])", re.I)
# A clock RANGE: "6:00 PM-7:30 PM", "from 4:00 - 9:00 PM", "11:30am – 3:30pm". The resolver
# reads "PM-7" as a UTC offset and the spaced form as two dates, so the event's day was lost.
# One side must look like a clock (":mm" or am/pm), so a day range ("Nov 3-5") is untouched,
# and a bare left number right after a month is the DAY: "Oct 22 - 6:00 PM" is a date and a
# start time, not a range, and dropping it left "Oct", due at the month's end (audit of 2020ee5f).
_CLOCK_RANGE = re.compile(r"(?:(?<!\w)(?:from|between)\s+)?(?<![\w/:.-])(\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\b\.?)?)"
                          r"\s*(?:-|–|—|\bto\b|\buntil\b|\btill\b)\s*"
                          r"(\d{1,2}(?::\d{2})?(?:\s*[ap]\.?m\b\.?)?)(?![\w/:-])", re.I)
_CLOCKISH = re.compile(r":\d{2}|[ap]\.?m", re.I)
_AFTER_MONTH = re.compile(rf"\b{_MONTH}\s*$", re.I)
# "Tuesday Sep 22, 2026": the resolver reads "Tuesday" as the NEXT Tuesday after the email
# was sent and refuses the pair as conflicting. When the weekday is the date's own, the
# weekday is only decoration; when it is not ("Friday, October 22"), the refusal stands.
_WEEKDAY = re.compile(r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|rsday|urday)?\b\.?,?", re.I)
_DAY_INDEX = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


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


def _drop_clock_range(m: re.Match) -> str:
    if _CLOCKISH.search(m.group(1)):
        return " "
    if _CLOCKISH.search(m.group(2)) and not _AFTER_MONTH.search(m.string, 0, m.start(1)):
        return " "
    return m.group(0)


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
    phrase = _CLOCK_RANGE.sub(_drop_clock_range, (phrase or "")).strip()
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
        if m.group(5):
            monday = sent.date() - timedelta(days=sent.weekday())
            offset = 7 if m.group(5).lower() == "next" else 0
            return Due(monday + timedelta(days=offset + 6), "week")
        count, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        n = _NUMBERS.get(count.lower()) or int(count)
        return Due(sent.date() + timedelta(days=n * (7 if unit.lower() == "week" else 1)), "day")

    first = month_first() if month_first_order is None else month_first_order
    numeric = _NUMERIC.search(phrase)
    if numeric:
        a, b, year = int(numeric.group(1)), int(numeric.group(2)), numeric.group(3)
        month, day = (a, b) if first else (b, a)
        if not (1 <= month <= 12):
            return Due(None, "", "bad_numeric_date")
        written = f"{calendar.month_name[month]} {day}" + (f", {'20' + year if len(year) == 2 else year}" if year else "")
        phrase = phrase[:numeric.start()] + written + phrase[numeric.end():]

    # The resolver knows "Sep" and "September" but not "Sept", which US mail uses a lot.
    phrase = _SEPT.sub("Sep", phrase)
    got = resolve(phrase, sent, tz, "mail_memory")
    if got.blocked == "invalid_time":
        # "Friday at 28", "Nov 3 at 24:00": the clock is impossible but the day is not.
        # A due date never carries a clock, so read the phrase again without it.
        bare = _CLOCK.sub(" ", phrase).strip()
        retry = resolve(bare, sent, tz, "mail_memory") if bare and bare != phrase else got
        got = retry if retry.day is not None else got
    if got.blocked == "conflicting_times":
        named = {_DAY_INDEX[w.group(1).lower()] for w in _WEEKDAY.finditer(phrase)}
        bare = _WEEKDAY.sub(" ", phrase).strip()
        retry = resolve(bare, sent, tz, "mail_memory") if len(named) == 1 and bare else got
        got = retry if retry.day is not None and retry.day.weekday() in named else got
    if got.day is None:
        return Due(None, "", got.blocked or "unresolved")
    return Due(got.day, "day")
