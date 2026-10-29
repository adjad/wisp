"""Resolve a spoken time range ("this month", "last week") to an epoch window,
and a spoken delivery TIME ("in 10 minutes", "at 6pm") to an absolute moment.

Exists because the read tools had no way to express a RANGE at all. Verified
failure, 2026-08-09: asked to "give an update to my mom about what I did this
month and last month", the model had only `day` (one specific day), `query`
(a keyword search over message TEXT) and `count`. So it searched for the literal
word "August" — and `view_messages(count=50, query="August")` returned a message
from **Thu Apr 9** reading "we are looking at dates at the end of July/beginning
of August". The user saw an answer built from April. That is not a tool-selection
mistake: `view_messages` was the right tool, and the question was inexpressible
with its parameters.

WISP RESOLVES THE DATES, NOT THE MODEL. That is the whole design point. The
agent system prompt already states at length that this model is "reliably wrong"
at date arithmetic — week numbers, days-between, month boundaries — so a
`since`/`until` pair of ISO dates would hand it exactly the job it cannot do,
and a wrong boundary is invisible in the output. A vocabulary of period WORDS
moves that arithmetic into Python, where it is testable.

`period` accepts, case-insensitively:
    today, yesterday
    this week, last week            (weeks start Monday)
    this month, last month
    this year, last year
    last N days/weeks/months/quarters/years
                                     (also "past N ...", bare "N ..."; N may
                                      be a digit or spelled out one..twelve —
                                      "three weeks" works the same as "3
                                      weeks". Each is an EXACT rolling window
                                      ending today — "3 weeks" is exactly 21
                                      days, "6 months" is exactly 6 calendar
                                      months back from today, not a
                                      nearest-available bucket.)
    YYYY-MM                         (a specific month)
    YYYY-MM-DD                      (a specific day)

EXTENDED 2026-08-23 to cover weeks/months/quarters/years, not just days.
Motivated by a DIFFERENT tool (get_stock_price, see service/tools/web_tools.py)
that had its own separate, looser period parser: asked for a price "exactly
three weeks ago", it silently substituted a 3-MONTH bucket (the nearest thing
it had) with no error and no signal of the mismatch, and the model then
relabeled that 3-month-old price as "three weeks ago" in a message sent to
the user's mom. This module already avoided that shape of failure for
email/messages/notes/browser-history — days were always an exact rolling
window, and anything unrecognized raises `BadPeriod` rather than guessing —
but "3 weeks" itself had nowhere to land before this: it isn't "this
week"/"last week", and the old day-only regex didn't match "week" at all, so
it would have hit the same silent-guess risk had any caller's vocabulary
included it. Generalizing the day regex to all four units, with the same
exact-math-no-bucket-guessing discipline, closes that gap everywhere at once
rather than only in the one tool that happened to get hit.

The same gap existed on the WRITE side for `schedule_send`, which asked the
model to fill in `when_iso` — an absolute ISO datetime it had to compute
itself from whatever the user said. Verified failure, 2026-08-19: asked to
schedule an email "to go out in 10 minutes" at 21:43 real time, the model
computed when_iso as "2026-08-19T09:52:00" — 9:52 AM, a 12-hour AM/PM error.
schedule_send's own past-time check correctly rejected it, but the model's
final answer to the user still claimed success ("queued to send at 9:52 AM
today... it should go out shortly"), because a tool error is not the same
thing as the model noticing one. `resolve_when` moves this arithmetic into
Python too: `schedule_send` now only needs the model to extract the raw
phrase, never to do the arithmetic on it.

`when` accepts, case-insensitively:
    in N minutes, in N hours        (also "N minutes"/"N hours" without "in",
                                      "in 1 hour and 30 minutes", "half an hour")
    at H, at H:MM, H(am|pm), H:MM(am|pm), or a bare 24-hour H:MM
                                     (no am/pm on a 1-12 hour -> nearest future
                                      occurrence, trying today before tomorrow)
    today, tonight, tomorrow
    monday..sunday, optionally "next monday" etc
    any of the above + "morning"/"afternoon"/"evening"/"night"/"noon"/"midnight"
    any of the above + "at <clock time>"        ("tomorrow at 9am")
    a bare ISO datetime, e.g. 2026-08-25T15:00  (fallback, not the primary path)
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

from service.tasks.temporal import DAYPART_HOURS, unambiguous_local_time

# Spelled-out counts up to a dozen — "three weeks ago" is at least as common
# in real speech as "3 weeks ago", and the model is asked to pass the user's
# own phrase through rather than translate it into a digit first.
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
              "twelve": 12}
_UNIT_ALIASES = {
    "d": "day", "day": "day", "days": "day",
    "wk": "week", "week": "week", "weeks": "week",
    "mo": "month", "month": "month", "months": "month",
    "qtr": "quarter", "quarter": "quarter", "quarters": "quarter",
    "yr": "year", "year": "year", "years": "year",
}
# "last 30 days" / "past 7 days" / "30 days" / "3 weeks" / "six months" /
# "2 quarters" / "1 year" — a count plus any unit in _UNIT_ALIASES.
_LAST_N_UNIT_RE = re.compile(
    r"^(?:the\s+)?(?:last|past)?\s*"
    r"(\d{1,4}|" + "|".join(_NUM_WORDS) + r")\s*"
    r"(" + "|".join(sorted(_UNIT_ALIASES, key=len, reverse=True)) + r")$")
_YYYY_MM_RE = re.compile(r"^(\d{4})-(\d{1,2})$")

# Upper bound per unit, chosen so the BadPeriod message can teach a concrete
# "use 1 to N" range rather than just rejecting silently. Days/weeks cap at
# ~10 years' worth; months/quarters/years cap a little looser since nobody
# realistically asks for span this old from local mail/message caches, but a
# bound still catches a garbled number before it reaches datetime.replace.
_UNIT_MAX = {"day": 3650, "week": 521, "month": 120, "quarter": 40, "year": 200}


def _months_before(d: datetime, n: int) -> datetime:
    """`d` shifted back exactly `n` calendar months, with the day clamped
    into the target month's actual range — Aug 31 minus 1 month lands on
    Jul 31, and Aug 31 minus 6 months lands on Feb 28 (or 29), never an
    invalid Feb 31. Mirrors `_add_month` below but preserves the day instead
    of always landing on the 1st — `_add_month` answers "which month",
    this answers "the same point N months earlier", which is what a rolling
    'last N months' window needs.
    """
    total = d.year * 12 + (d.month - 1) - n
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _years_before(d: datetime, n: int) -> datetime:
    """`d` shifted back exactly `n` years, clamping Feb 29 into Feb 28 on a
    target year that isn't a leap year."""
    year = d.year - n
    day = 28 if (d.month == 2 and d.day == 29 and not calendar.isleap(year)) else d.day
    return d.replace(year=year, day=day)


class BadPeriod(ValueError):
    """`period` wasn't a phrase we understand. Carries a message aimed at the
    MODEL, listing what it can say instead — a bad argument should teach the
    caller the vocabulary rather than just failing."""


def _month_start(d: datetime) -> datetime:
    return datetime(d.year, d.month, 1)


def _add_month(d: datetime, delta: int) -> datetime:
    """First-of-month `delta` months away, without a calendar dependency."""
    month = d.month - 1 + delta
    return datetime(d.year + month // 12, month % 12 + 1, 1)


_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_ABBREV = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
               "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
# Parts of today: the whole day is the window (the tools filter what is past).
_TODAY_WORDS = {"tonight", "this evening", "this afternoon", "this morning", "today evening",
                "this morning and afternoon", "later today", "the rest of today",
                "rest of today", "the rest of the day", "rest of the day"}
# Wrappers people (and the model) put around a period: "on Friday", "in the past
# 3 days", "over the weekend", "for this week", "since Monday" is handled below.
_LEAD_WORDS = re.compile(r"^(?:on|in|over|during|for|within|from)\s+(?:the\s+)?")
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)?"
_MONTH_DAY_RE = re.compile(r"^([a-z]+)\.?\s+" + _ORDINAL + r"$")      # nov 3, november 3rd
_DAY_MONTH_RE = re.compile(r"^" + _ORDINAL + r"\s+(?:of\s+)?([a-z]+)$")  # 3rd of november
_THE_NTH_RE = re.compile(r"^(?:the\s+)?" + _ORDINAL + r"$")             # the 3rd


def _weekday(word: str) -> int | None:
    word = word.rstrip("s") if word.endswith("days") else word  # "fridays"
    if word in _DAY_NAMES:
        return _DAY_NAMES.index(word)
    return _DAY_ABBREV.get(word)


def resolve_period(period: str, *, now: datetime | None = None, forward: bool = False,
                   extended: bool = True) -> tuple[float, float, str]:
    """(start_epoch, end_epoch, human_label) for `period`; end is EXCLUSIVE.

    `now` is injectable so the tests don't depend on today's date. `forward`
    says which way a bare weekday, "the weekend" or a date without a year
    points: True for a schedule ("what's on Friday" = the coming Friday),
    False (the default) for history ("emails from Friday" = the last one).
    Today always counts as either. `extended=False` keeps the original
    vocabulary only (weather has whole-day forecasts and refuses dayparts).
    """
    now = now or datetime.now()
    today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    p = " ".join((period or "").strip().lower().split())
    p = re.sub(r"[?!.,]+$", "", p)
    p = _LEAD_WORDS.sub("", p) if not p.startswith("the ") else p
    if not p:
        raise BadPeriod("(period was empty)")

    def win(start: datetime, end: datetime, label: str):
        return start.timestamp(), end.timestamp(), label

    def day_win(d: datetime):
        label = d.strftime("%A, %B %-d") + (f", {d.year}" if d.year != today.year else "")
        return win(d, d + timedelta(days=1), label)

    if not extended:
        return _resolve_core(p, period, today, win)
    if p in _TODAY_WORDS:
        return win(today, today + timedelta(days=1), "today")

    # A weekday: "friday", "on fri", "this friday", "next friday", "last friday".
    m = re.match(r"^(this|next|last|past|coming|upcoming)?\s*([a-z]+)$", p)
    if m and (wd := _weekday(m.group(2))) is not None:
        mod = m.group(1)
        ahead = (wd - today.weekday()) % 7
        behind = (today.weekday() - wd) % 7
        if mod in ("next", "coming", "upcoming"):
            d = today + timedelta(days=ahead or 7)
        elif mod in ("last", "past"):
            d = today - timedelta(days=behind or 7)
        elif mod == "this":
            d = today + timedelta(days=ahead) if forward or ahead <= behind else today - timedelta(days=behind)
        else:
            d = today + timedelta(days=ahead) if forward else today - timedelta(days=behind)
        return day_win(d)

    # Weekends run Saturday 00:00 to Monday 00:00.
    m = re.match(r"^(?:the\s+)?(this|next|last|past|coming|upcoming)?\s*weekend$", p)
    if m:
        mod = m.group(1)
        sat_ahead = today + timedelta(days=(5 - today.weekday()) % 7)
        in_weekend = today.weekday() >= 5
        current = today - timedelta(days=today.weekday() - 5) if in_weekend else None
        if mod in ("last", "past"):
            start = (current or sat_ahead) - timedelta(days=7)
        elif mod == "next":
            start = current + timedelta(days=7) if current else sat_ahead
        elif current is not None:
            start = current
        elif forward or mod in ("this", "coming", "upcoming"):
            start = sat_ahead
        else:
            start = sat_ahead - timedelta(days=7)
        return win(start, start + timedelta(days=2),
                   f"the weekend of {start:%B %-d}")

    # "since monday": from that day (the last one, today included) to today's end.
    m = re.match(r"^since\s+(?:last\s+)?([a-z]+)$", p)
    if m and (wd := _weekday(m.group(1))) is not None:
        start = today - timedelta(days=(today.weekday() - wd) % 7)
        return win(start, today + timedelta(days=1), f"since {start:%A, %B %-d}")

    # A date without a year: "nov 3", "november 3rd", "3rd of november", "the 3rd".
    md = None
    if (m := _MONTH_DAY_RE.match(p)) and m.group(1) in _MONTHS:
        md = (_MONTHS[m.group(1)], int(m.group(2)))
    elif (m := _DAY_MONTH_RE.match(p)) and m.group(2) in _MONTHS:
        md = (_MONTHS[m.group(2)], int(m.group(1)))
    elif (m := _THE_NTH_RE.match(p)) and (p.startswith("the ") or re.search(r"(st|nd|rd|th)$", p)):
        n = int(m.group(1))
        if not 1 <= n <= 31:
            raise BadPeriod(f"(there is no day {n} in a month)")
        md = (today.month, n)
        # For a schedule, "the 3rd" said on the 29th is next month's 3rd, and
        # "the 30th" in February is March's: the next month that has that day.
        if forward and (n < today.day or n > calendar.monthrange(today.year, today.month)[1]):
            nxt = _add_month(today, 1)
            while n > calendar.monthrange(nxt.year, nxt.month)[1]:
                nxt = _add_month(nxt, 1)
            return day_win(datetime(nxt.year, nxt.month, n))
    if md is not None:
        month, dom = md
        year = today.year
        def dated(y):
            # Feb 29 outside a leap year is named, never moved to Feb 28.
            if not 1 <= dom <= calendar.monthrange(y, month)[1]:
                raise BadPeriod(f"(there is no {calendar.month_name[month]} {dom} in {y})")
            return datetime(y, month, dom)
        if not 1 <= dom <= max(calendar.monthrange(2028, month)[1], 1):
            raise BadPeriod(f"(there is no {calendar.month_name[month]} {dom})")
        if forward:
            d = (datetime(year, month, dom) if dom <= calendar.monthrange(year, month)[1]
                 and datetime(year, month, dom) >= today else dated(year + 1))
        else:
            d = (datetime(year, month, dom) if dom <= calendar.monthrange(year, month)[1]
                 else dated(year - 1))
            if d > today + timedelta(days=31):
                # History about a date well ahead means last year's; a few days
                # ahead is just this year's date (a calendar search, a typo).
                d = dated(year - 1)
        return day_win(d)

    return _resolve_core(p, period, today, win)


def _resolve_core(p, period, today, win):
    """The original vocabulary: today, this/last week, N days, YYYY-MM(-DD)."""
    if p in ("today",):
        return win(today, today + timedelta(days=1), "today")
    if p == "tomorrow":
        return win(today + timedelta(days=1), today + timedelta(days=2), "tomorrow")
    if p == "next week":
        start = today + timedelta(days=7 - today.weekday())
        return win(start, start + timedelta(days=7), "next week")
    if p == "next month":
        start = _add_month(today, 1)
        return win(start, _add_month(start, 1), f"{start:%B %Y}")
    if p in ("yesterday",):
        return win(today - timedelta(days=1), today, "yesterday")

    if p in ("this week", "the week", "week"):
        start = today - timedelta(days=today.weekday())
        return win(start, start + timedelta(days=7), "this week")
    if p == "last week":
        start = today - timedelta(days=today.weekday() + 7)
        return win(start, start + timedelta(days=7), "last week")

    if p in ("this month", "the month", "month"):
        start = _month_start(today)
        return win(start, _add_month(start, 1), f"{today:%B %Y}")
    if p == "last month":
        start = _add_month(_month_start(today), -1)
        return win(start, _month_start(today), f"{start:%B %Y}")

    if p in ("this year", "the year", "year"):
        start = datetime(today.year, 1, 1)
        return win(start, datetime(today.year + 1, 1, 1), str(today.year))
    if p == "last year":
        start = datetime(today.year - 1, 1, 1)
        return win(start, datetime(today.year, 1, 1), str(today.year - 1))

    if (m := _LAST_N_UNIT_RE.match(p)):
        raw, unit_word = m.group(1), m.group(2)
        n = int(raw) if raw.isdigit() else _NUM_WORDS[raw]
        unit = _UNIT_ALIASES[unit_word]
        if not 1 <= n <= _UNIT_MAX[unit]:
            raise BadPeriod(f"(a {n}-{unit} range is out of range — use 1 to "
                             f"{_UNIT_MAX[unit]} {unit}s)")
        # A single fixed exclusive end (today + 1 day) with `start` computed
        # exactly N units before IT, not before `today` — that's what makes
        # "last 7 days" land on [today-6, today] inclusive (verified by
        # test_timeranges.py) fall out of the SAME formula as months/years
        # rather than needing a separate off-by-one rule per unit.
        end = today + timedelta(days=1)
        if unit == "day":
            start = end - timedelta(days=n)
        elif unit == "week":
            start = end - timedelta(days=n * 7)
        elif unit == "month":
            start = _months_before(end, n)
        elif unit == "quarter":
            start = _months_before(end, n * 3)
        else:  # year
            start = _years_before(end, n)
        return win(start, end, f"the last {n} {unit}{'s' if n != 1 else ''}")

    if (m := _YYYY_MM_RE.match(p)):
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            raise BadPeriod(f"(there is no month {month} — use YYYY-MM, e.g. 2026-07)")
        start = datetime(year, month, 1)
        return win(start, _add_month(start, 1), f"{start:%B %Y}")

    try:                                    # a bare YYYY-MM-DD
        d = datetime.fromisoformat(p)
    except ValueError:
        raise BadPeriod(
            f"(couldn't understand the period {period!r}. Use one of: 'today', "
            "'tomorrow', 'yesterday', a weekday ('friday', 'next monday', 'last "
            "friday'), 'this weekend', 'last weekend', a date ('nov 3', "
            "'the 3rd'), 'this week', 'last week', 'this month', 'last month', "
            "'this year', 'last year', 'last N days/weeks/months/quarters/"
            "years' (N as a digit or spelled out, e.g. '3 weeks' or 'six "
            "months'), 'YYYY-MM' for a specific month, or 'YYYY-MM-DD' for a "
            "specific day.)") from None
    start = datetime(d.year, d.month, d.day)
    return win(start, start + timedelta(days=1), start.strftime("%A, %B %-d"))


def resolve_span(period: str, *, now: datetime | None = None, forward: bool = False,
                 extended: bool = True) -> tuple[float, float, str]:
    """Like resolve_period, but also accepts a COMPOUND range joined by "and"
    or "to" — "this month and last month", "last month to this month".

    Verified need: the request that exposed this whole gap was literally "what I
    did this month and last month". Resolving each side and taking the union
    (rather than making the model issue two calls and merge them) keeps it to
    one tool call and one coherent label.
    """
    p = " ".join((period or "").strip().lower().split())
    parts = re.split(r"\s+(?:and|to|through|thru|until|\-\-|:)\s+|\s*\.\.\s*", p)
    parts = [x for x in (s.strip() for s in parts) if x]
    if len(parts) < 2:
        return resolve_period(period, now=now, forward=forward, extended=extended)
    windows = [resolve_period(x, now=now, forward=forward, extended=extended) for x in parts]
    start = min(w[0] for w in windows)
    end = max(w[1] for w in windows)
    # Label from the outermost pair, in chronological order.
    first = min(windows, key=lambda w: w[0])[2]
    last = max(windows, key=lambda w: w[1])[2]
    return start, end, f"{first} through {last}" if first != last else first


# The `period` argument's JSON-schema description, shared by every tool that
# takes one so the vocabulary can't drift between them.
PERIOD_ARG = {
    "type": "string",
    "description": ("time range to cover, in WORDS — 'today', 'tomorrow', "
                    "'yesterday', a weekday ('friday', 'next monday', 'last "
                    "friday'), 'this weekend', 'last weekend', a date ('nov 3', "
                    "'the 3rd'), 'this week', 'last week', 'this month', 'last month', "
                    "'this year', 'last year', or 'last N days/weeks/months/"
                    "quarters/years' for an EXACT span (N as a digit or "
                    "spelled out — 'last 3 weeks' and 'last three weeks' both "
                    "work, and mean a precise 21-day window, not the nearest "
                    "month). Also 'YYYY-MM' for a specific month, or "
                    "'YYYY-MM-DD' for a specific day. Two can be joined: "
                    "'this month and last month'. "
                    "ONLY pass this when the user NAMED a time ('what did I do "
                    "in July', 'emails from last week'). OMIT it for questions "
                    "with no time in them — 'anything I need to reply to?', "
                    "'what's new?', 'anything important?' — those mean the "
                    "recent inbox, and scoping them to a calendar range can "
                    "return nothing at all (early on a Monday 'this week' is a "
                    "few hours long). Do NOT put a month name in `query`, which "
                    "searches the message text and will match unrelated "
                    "messages that merely mention that month."),
}


# ---------------------------------------------------------------------------
# resolve_when — spoken delivery TIME -> absolute moment. See module
# docstring for the verified failure this closes.

class BadWhen(ValueError):
    """`when` wasn't a phrase we understand. Carries a message aimed at the
    MODEL, listing what it can say instead — a bad argument should teach the
    caller the vocabulary rather than just failing."""


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

_TIME_WORD_HOURS = {
    **DAYPART_HOURS, "noon": 12, "midday": 12, "midnight": 0,
}

# "in 10 minutes" / "10 minutes" / "in 1 hour and 30 minutes" / "in 2 hours"
_DURATION_RE = re.compile(
    r"^(?:in\s+)?"
    r"(?:(?P<hours>\d{1,3})\s*(?:h|hr|hrs|hour|hours)\b)?"
    r"\s*(?:(?:and\s+)?(?P<minutes>\d{1,3})\s*(?:m|min|mins|minute|minutes)\b)?$"
)

# "6", "at 6", "6pm", "6:30", "6:30pm", "18:00"
_CLOCK_RE = re.compile(
    r"^(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?$"
)

# "tonight", "tomorrow morning", "monday at 9am", "next friday"
_DAY_CLOCK_RE = re.compile(
    r"^(?:(?P<modifier>this|next)\s+)?"
    r"(?P<day>today|tonight|tomorrow|" + "|".join(_WEEKDAYS) + r")"
    r"(?:\s+(?P<timeword>morning|noon|midday|afternoon|evening|night|midnight))?"
    r"(?:\s+at\s+(?P<clock>.+))?$"
)


def _parse_duration_minutes(p: str) -> int | None:
    if p in ("half an hour", "a half hour", "half hour", "half-hour"):
        return 30
    if p in ("an hour", "a hour", "one hour"):
        return 60
    m = _DURATION_RE.match(p)
    if not m or (m.group("hours") is None and m.group("minutes") is None):
        return None
    return int(m.group("hours") or 0) * 60 + int(m.group("minutes") or 0)


def _describe_duration(total_minutes: int) -> str:
    h, m = divmod(total_minutes, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    return "in " + " ".join(parts)


def _resolve_clock(hour: int, minute: int, ampm: str | None,
                    now: datetime) -> datetime:
    """A bare clock time on its own, e.g. "6pm" or "18:00" or "6" — resolved
    against `now`, not a specific day the caller already picked.

    With am/pm (or a >12 24-hour hour) this is unambiguous: today at that
    time, or tomorrow if today's has already passed. Without am/pm on a
    1-12 hour it's genuinely ambiguous, so this picks the SOONEST future
    occurrence — the same "next occurrence" convention voice assistants use
    for a bare clock time — rather than guessing a meridiem.
    """
    today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    if hour >= 13 or hour == 0:
        if ampm:
            raise BadWhen(f"({hour} isn't a 12-hour clock hour, so am/pm "
                           "doesn't apply to it)")
        candidate = today.replace(hour=hour, minute=minute)
        return candidate if candidate > now else candidate + timedelta(days=1)
    if ampm:
        h = (hour % 12) + (12 if ampm == "pm" else 0)
        candidate = today.replace(hour=h, minute=minute)
        return candidate if candidate > now else candidate + timedelta(days=1)
    candidates = sorted(
        today.replace(hour=(hour % 12) + (12 if is_pm else 0), minute=minute)
        + timedelta(days=day_offset)
        for day_offset in (0, 1) for is_pm in (False, True))
    for c in candidates:
        if c > now:
            return c
    return candidates[-1]


def _resolve_day_clock(m: re.Match, now: datetime) -> datetime:
    """A day word (today/tonight/tomorrow/a weekday), optionally with a
    time-of-day word or an explicit clock time attached."""
    today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    day, modifier = m.group("day"), m.group("modifier")
    timeword, clock = m.group("timeword"), m.group("clock")

    if day in ("today", "tonight"):
        base = today
    elif day == "tomorrow":
        base = today + timedelta(days=1)
    else:
        offset = (_WEEKDAYS.index(day) - today.weekday()) % 7
        base = today + timedelta(days=offset)
        if modifier == "next":
            base += timedelta(days=7)

    if clock:
        cm = _CLOCK_RE.match(clock.strip())
        if not cm:
            raise BadWhen(f"(couldn't understand the time {clock!r})")
        hour, minute = int(cm.group("hour")), int(cm.group("minute") or 0)
        if hour > 23 or minute > 59:
            raise BadWhen(f"(couldn't understand the time {clock!r})")
        ampm = cm.group("ampm")
        if hour >= 13:
            if ampm:
                raise BadWhen(f"({hour} isn't a 12-hour clock hour, so am/pm "
                               "doesn't apply to it)")
            h = hour
        elif ampm:
            h = (hour % 12) + (12 if ampm == "pm" else 0)
        else:
            # An explicit day removes the "nearest future occurrence" trick
            # (both readings are equally "in the future"), so fall back to
            # how these actually get said out loud: a small bare hour named
            # alongside a day is almost always evening ("Monday at 6").
            h = (hour % 12) + (12 if 1 <= hour <= 7 else 0)
        dt = base.replace(hour=h, minute=minute)
    elif timeword:
        dt = base.replace(hour=_TIME_WORD_HOURS[timeword], minute=0)
    elif day == "tonight":
        dt = base.replace(hour=_TIME_WORD_HOURS["tonight"], minute=0)
    else:
        dt = base.replace(hour=9, minute=0)

    # A named WEEKDAY that lands in the past (e.g. "monday" said on a Monday
    # afternoon) means next week's, not a rejected time. "today"/"tonight"/
    # "tomorrow" are left as-is even if past — the caller's own past-time
    # check should surface that honestly rather than silently reinterpreting
    # a day the user explicitly named.
    if day in _WEEKDAYS and dt <= now:
        dt += timedelta(days=7)
    return dt


def _checked_wall_time(value: datetime) -> datetime:
    if not unambiguous_local_time(value):
        raise BadWhen("(ambiguous or nonexistent local clock time; choose another time or provide an explicit UTC offset)")
    return value


def resolve_when(when: str, *, now: datetime | None = None
                 ) -> tuple[datetime, str]:
    """(absolute_datetime, human_label) for a spoken delivery-time phrase.
    See the module docstring for the vocabulary and the failure this closes.
    """
    now = now or datetime.now()
    p = " ".join((when or "").strip().lower().split())
    if not p:
        raise BadWhen("(when was empty)")

    if (minutes := _parse_duration_minutes(p)) is not None:
        if minutes <= 0:
            raise BadWhen(f"(couldn't understand the duration in {when!r})")
        # Durations name elapsed time, including through a clock transition.
        dt = datetime.fromtimestamp(now.timestamp() + minutes * 60, tz=now.tzinfo)
        if dt.tzinfo is None and not unambiguous_local_time(dt):
            dt = dt.astimezone()  # retain the selected fold when serialized
        return dt, _describe_duration(minutes)

    if (m := _DAY_CLOCK_RE.match(p)):
        dt = _checked_wall_time(_resolve_day_clock(m, now))
        return dt, dt.strftime("%A %b %-d at %-I:%M %p")

    if (m := _CLOCK_RE.match(p)):
        hour, minute = int(m.group("hour")), int(m.group("minute") or 0)
        if hour > 23 or minute > 59:
            raise BadWhen(f"(couldn't understand the time {when!r})")
        dt = _checked_wall_time(_resolve_clock(hour, minute, m.group("ampm"), now))
        return dt, dt.strftime("%a %b %-d at %-I:%M %p")

    try:
        dt = datetime.fromisoformat(when.strip())
    except ValueError:
        raise BadWhen(
            f"(couldn't understand the time {when!r}. Say it the way the "
            "user did — 'in 10 minutes', 'in 2 hours', 'at 6pm', 'tonight', "
            "'tomorrow morning', 'monday at 9am' — do NOT compute an exact "
            "date/time yourself.)") from None
    if dt.tzinfo is None:
        dt = _checked_wall_time(dt)
    return dt, dt.strftime("%a %b %-d at %-I:%M %p")


# The `when` argument's JSON-schema description, shared by every tool that
# takes a delivery TIME (as opposed to a `period` range) so the vocabulary
# can't drift between them.
WHEN_ARG = {
    "type": "string",
    "description": ("WHEN this should happen, in the user's own WORDS — 'in "
                    "10 minutes', 'in 2 hours', 'at 6pm', 'tonight', "
                    "'tomorrow morning', 'monday at 9am'. Wisp resolves this "
                    "to an exact time in Python. Do NOT compute an ISO date/"
                    "time yourself — date/time arithmetic is exactly what "
                    "you are reliably wrong at, and a 12-hour am/pm slip is "
                    "invisible until the message goes out at the wrong "
                    "time."),
}
