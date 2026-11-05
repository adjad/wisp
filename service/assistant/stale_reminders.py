"""When an undated Apple reminder has gone stale, and what Wisp does about it.

The user's rule (2026-10-18): an undated reminder about a week old gets one question,
"keep it or delete it?"; anything older that they have not chosen to keep is neither
mentioned nor used. The same rule covers dated reminders a week or more overdue.

Ages count from EventKit's creationDate (``created_ts``). A reminder whose creation date
is unknown is treated as unknown age, never as stale, so nothing disappears by guesswork.

States:
- ``fresh``   younger than ASK_AFTER_S; used normally.
- ``unknown`` no creation date; used normally.
- ``kept``    the user chose Keep; used normally and never asked about again.
- ``ask``     ASK_AFTER_S <= age < ASK_UNTIL_S and not kept; Wisp offers Keep / Delete
              once (the notch shows it until the user answers), and nothing else uses it.
- ``stale``   older than ASK_UNTIL_S and not kept; hidden everywhere, no question either.

Dated reminders follow the same states, aged by how long they are OVERDUE
(``classify_overdue``); one not yet a week past due is untouched.

Reading of "anything older than that": from one week on, an unkept undated reminder is
already excluded from briefs, Day plans, attention, alerts and model context; the only
place it appears is the question itself, offered for one more week. After that it is
silent. Reminders.app is never changed except by an explicit Delete click.

A reminder to get ready for a dated event gets the same question early, once that date
has passed (2026-11-04: "Review all of this before your 10/30 midterm." still offered
Plan time five days later). ``passed_date`` reads this from the title. It is deliberately
narrow, because a miss only leaves an ordinary overdue reminder while a false match hides
work: late work ("Finish HW4 by 10/30"), follow-ups ("Review 10/30 midterm results") and
titles naming no event ("Finish this by Friday") never match. It reads any event after a
calendar date, and weekdays and "tomorrow" only before a known kind of event.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, time as day_time, timedelta
from typing import NamedTuple

DAY_S = 86400
ASK_AFTER_S = 7 * DAY_S
ASK_UNTIL_S = 14 * DAY_S

KEEP = "keep"
EXCLUDED_STATES = frozenset({"ask", "stale"})


_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DATE = (r"(?:\d{4}-\d{1,2}-\d{1,2}(?![\d/-])|\d{1,2}/\d{1,2}(?:/\d{2,4})?(?![\d/])"
         r"|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
         r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?\b(?:,?\s+\d{4}\b)?)")
_ANY_DATE = re.compile(r"(?<![\w/-])" + _DATE, re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_RELATIVE = rf"(?:(?:next\s+|this\s+)?(?:{'|'.join(_WEEKDAYS)})|tomorrow|tonight|today)"
# Things that happen once, on their day. A weekday or "tomorrow" is looser than a calendar
# date, so it needs one of these. Not "class" or "deadline": a deadline passing leaves late
# work to do, which is what the ordinary overdue row is for.
_EVENT = (r"(?:midterms?|exams?|finals?|quiz(?:zes)?|tests?|interviews?|appointments?|presentations?|demos?"
          r"|flights?|trips?|recitals?|concerts?|weddings?|parties|party|meetings?|sessions?|lectures?|seminars?"
          r"|workshops?|conferences?|fairs?|ceremony|graduation|birthdays?|reunions?|games?|matches|dinners?"
          r"|lunch(?:es)?|shows?|events?|visits?|checkups?|surgery|hearings?|rehearsals?|tours?|orientation"
          r"|vacations?)\b")
# After a calendar date any noun names the event ("before the 11/10 career fair"), except
# words that only say when ("before 10/30 at noon") or carry on the sentence.
_NOT_A_NAME = (r"(?!(?:please|at|or|and|if|so|then|to|by|on|in|noon|midnight|pm|am|morning|afternoon|evening|night"
               r"|eod|cob|end|day|week|month|latest|anyway|if)\b)")
_NAME = rf"{_NOT_A_NAME}[a-z][\w'-]*\b"
_FILLER = r"(?:[\w'-]+\s+){0,3}"
_DETERMINER = r"(?:(?:the|your|my|our|[\w-]+'s)\s+)?"
_LEADS = (
    # "… before your 10/30 midterm", "Attend the exam on Oct 30"
    r"\b(?:before|ahead\s+of|prior\s+to|in\s+time\s+for|attend(?:ing)?|go\s+to|get\s+ready\s+for)\s+" + _DETERMINER,
    # "Study notes for the 10/30 midterm", "Buy a gift for Sam's 11/20 birthday"
    r"^\s*(?:review|study|prep|prepare|practice|cram|rehearse|pack|buy|book|print|bring)\b[^.!?]*?\bfor\s+" + _DETERMINER,
)
_LEAD = "(?:" + "|".join(_LEADS) + ")"
_CONNECT = r"\s*(?:on\s+|at\s+|[,(-]\s*)?"
# The title is just the event: "10/30 midterm", "Reminder: Midterm on Oct 30"
_ALONE_START, _ALONE_END = r"^\s*(?:reminder:\s*)?" + _DETERMINER, r"[\s.!)]*$"
_MENTIONS = [re.compile(pattern, re.I) for pattern in (
    rf"{_LEAD}(?P<when>{_DATE})(?:'s)?\s+{_NAME}",
    rf"{_LEAD}{_FILLER}{_NAME}\s+on\s+(?P<when>{_DATE})",
    rf"{_LEAD}{_FILLER}{_EVENT}{_CONNECT}(?P<when>{_DATE})",
    rf"{_LEAD}(?P<when>{_RELATIVE})(?:'s)?\s+{_FILLER}{_EVENT}",
    rf"{_LEAD}{_FILLER}{_EVENT}{_CONNECT}(?P<when>{_RELATIVE})\b",
    rf"{_ALONE_START}(?P<when>{_DATE}|{_RELATIVE})(?:'s)?\s+{_FILLER}{_EVENT}{_ALONE_END}",
    rf"{_ALONE_START}{_FILLER}{_EVENT}{_CONNECT}(?P<when>{_DATE}|{_RELATIVE}){_ALONE_END}",
)]
# What comes after the event, what says it isn't over, and work that can still be handed
# in late, all leave something to do.
_STILL_OPEN = re.compile(
    r"\b(?:after|missed|make-?up|results?|grades?|graded|regrade|corrections?|resched\w*|postponed|moved"
    r"|refunds?|receipts?|follow[- ]?up|recap|debrief|thanks?|expenses?|reimburse\w*|retakes?|redo|deadline|due"
    r"|email|ask|call|text|message|pay|send|reply|cancel|submit|turn\s+in|hw\d*|homework|assignments?|essays?"
    r"|rent|bills?|forms?|applications?|invoices?|taxes)\b", re.I)
# A yearless date further than this from the reminder's own time isn't trusted to mean
# any particular year, so the reminder is left alone.
_YEARLESS_REACH_DAYS = 120


class PassedDate(NamedTuple):
    on: date
    label: str        # as the title wrote it: "10/30", "Oct 30"

    def over_at(self) -> float:
        """The first moment after that day, local time."""
        return datetime.combine(self.on + timedelta(days=1), day_time.min).timestamp()


def _title_date(text: str, anchor: date) -> date | None:
    """The date ``text`` means. A year it states is used as written. Without one, the
    occurrence nearest ``anchor`` (when the reminder was due or written), so "1/5" on a
    reminder due Dec 28 is next January and "12/28" on one due Jan 2 is last December."""
    text = text.strip().rstrip(".").casefold()
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    year = None
    try:
        if re.match(r"\d{4}-", text):
            return date(numbers[0], numbers[1], numbers[2])
        if "/" in text:
            month, day = numbers[0], numbers[1]
            if len(numbers) > 2:
                year = numbers[2] + (2000 if numbers[2] < 100 else 0)
        else:
            month, day = _MONTH_NAMES.index(text[:3]) + 1, numbers[0]
            if len(numbers) > 1:
                year = numbers[1]
        if year is not None:
            return date(year, month, day)
    except (ValueError, IndexError):
        return None
    options = []
    for candidate in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            options.append(date(candidate, month, day))
        except ValueError:      # Feb 29 outside a leap year; a month 30 in day/month order
            continue
    if not options:
        return None
    nearest = min(options, key=lambda d: abs((d - anchor).days))
    return nearest if abs((nearest - anchor).days) <= _YEARLESS_REACH_DAYS else None


def _latest_day(when: str, anchor: date) -> date | None:
    """The last day a weekday or "tomorrow" could mean, counted from the reminder's own
    time, or None for a calendar date. "next" may be read either of two ways, so it takes
    the later: nothing is called over while one reading is still ahead."""
    words = when.casefold().split()
    if words[-1] in ("today", "tonight"):
        return anchor
    if words[-1] == "tomorrow":
        return anchor + timedelta(days=1)
    if words[-1] not in _WEEKDAYS:
        return None
    ahead = (_WEEKDAYS.index(words[-1]) - anchor.weekday()) % 7
    return anchor + timedelta(days=ahead + (7 if words[0] == "next" else 0))


def passed_date(title: object, anchor_ts: object, now: float) -> PassedDate | None:
    """The day named in a title whose event is over, or None. ``anchor_ts`` is the item's
    due time, or when it was written if it has no due time. A source with neither passes
    ``now``, which leaves a weekday or "tomorrow" ahead and so never matches. The event's
    own day still counts as ahead, and so does a second date in the title that hasn't
    come yet."""
    anchor_ts = valid_created_ts(anchor_ts)
    if not isinstance(title, str) or anchor_ts is None:
        return None
    text = title[:300]
    found = next(filter(None, (pattern.search(text) for pattern in _MENTIONS)), None)
    if found is None or _STILL_OPEN.search(text):
        return None
    anchor = datetime.fromtimestamp(anchor_ts).date()
    today = datetime.fromtimestamp(now).date()
    named = " ".join(found.group("when").split()).rstrip(".")
    on = _latest_day(named, anchor) or _title_date(named, anchor)
    if on is None or on >= today:
        return None
    for other in _ANY_DATE.findall(text):
        other_on = _title_date(other, anchor)
        if other_on is None or other_on >= today:
            return None
    return PassedDate(on, named[0].upper() + named[1:] if named[0].isalpha() else named)


def valid_created_ts(value: object) -> float | None:
    """A usable creation time, or None. Booleans, non-finite and non-positive values are
    not times; a future time (clock skew) is still accepted and simply reads as fresh."""
    if type(value) not in (int, float):
        return None
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value >= 253402300800:
        return None
    return value


def classify(created_ts: object, decision: str | None, now: float, title: object = None) -> str:
    if decision == KEEP:
        return "kept"
    created = valid_created_ts(created_ts)
    if created is None:
        return "unknown"
    age = now - created
    if age < ASK_AFTER_S:
        return "ask" if passed_date(title, created, now) else "fresh"
    if age < ASK_UNTIL_S:
        return "ask"
    return "stale"


def classify_overdue(due_ts: object, decision: str | None, now: float, title: object = None) -> str:
    """The same states for a DATED incomplete Apple reminder, aged by time past its due
    time (2026-10-18: "Get doordash order", due Oct 2, still showed 16 days later). Not
    yet overdue, or overdue by less than ASK_AFTER_S, reads ``fresh``, unless the title's
    own date has passed. One the user set to ring later is theirs: it isn't overdue."""
    if decision == KEEP:
        return "kept"
    due = valid_created_ts(due_ts)
    if due is None:
        return "unknown"
    overdue = now - due
    if overdue < ASK_AFTER_S:
        return "ask" if overdue > 0 and passed_date(title, due, now) else "fresh"
    if overdue < ASK_UNTIL_S:
        return "ask"
    return "stale"


def excluded(created_ts: object, decision: str | None, now: float) -> bool:
    """True when this undated reminder must not be used or mentioned anywhere."""
    return classify(created_ts, decision, now) in EXCLUDED_STATES


def policy() -> dict:
    return {"ask_after_days": ASK_AFTER_S // DAY_S, "ask_until_days": ASK_UNTIL_S // DAY_S,
            "applies_to": "incomplete Apple reminders: undated ones by age, dated ones by time overdue",
            "asks_early": "a reminder to get ready for an event whose date in the title has passed",
            "age_from": {"undated": "Reminders creation date; unknown creation date is never stale",
                         "overdue": "due time"}}
