"""Turn the words in a message into a concrete time, the way a person reads a chat.

`service.temporal_facts` is an evidence extractor and refuses to guess: a bare
"6PM" is `missing_date`, a bare "Friday" is `ambiguous_weekday`, a yearless date
stays yearless. That is right for a deadline parser and wrong for a chat, where
"meet me at 6PM" plainly means the next 6PM. This module is the policy layer on
top: it takes the extractor's spans and applies conversational defaults, and it
RECORDS every default it applied (`inferred`), so the detector can weigh them and
the label report can say which inference was behind a miss.

It also overrides two judgments the extractor makes for deadline text and that are
wrong for chat:
  * "7:30" with no am/pm is read as 07:30 with no ambiguity flag, which would turn
    "dinner tomorrow at 7:30" into breakfast. Meridiem is judged from the quote.
  * Any "not" or "if" in the clause marks it negated or conditional, so "due Monday,
    if it's not already done" was refused as cancelled. Cancellation and hedging are
    judged here from words that actually mean them.

What it will not do, by design:
  * pick a winner between competing days or times in one message (`conflicting_times`);
  * treat "in 20 minutes" as a commitment (that is an ETA, not a plan);
  * resolve a cancelled clause, or a past-tense reference.

Pure: no clock, no I/O. `arrival` is the aware datetime the message landed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from service.temporal_facts import EvidenceContext, extract_temporal_facts

_WEEKDAYS = {name: i for i, name in enumerate(
    "monday tuesday wednesday thursday friday saturday sunday".split())}
_WEEKDAY_RE = re.compile(r"\b(?:(next|this)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_EXPLICIT_MERIDIEM = re.compile(r"\d\s*(?:am|pm)\b|\bnoon\b|\bmidnight\b", re.I)
_TWENTY_FOUR_HOUR = re.compile(r"\b(?:0\d|1[3-9]|2[0-3]):\d{2}\b")
_PAST = re.compile(r"\b(?:was|were|went|came|ate|saw|left|finished|ended|"
                   r"started|yesterday|last\s+(?:night|week|month))\b", re.I)
_CANCEL = re.compile(r"\b(?:cancel(?:l?ed)?|can'?t|cannot|won'?t|no\s+longer|postpone[ds]?|"
                     r"not\s+(?:going|coming|able|available|happening|gonna|making))\b", re.I)
# A proposal or a question, not a statement of fact. A bare "if" is not one: "due Monday,
# if it's not already done" states a due date.
_TENTATIVE = re.compile(r"\?|\b(?:maybe|might|possibly|perhaps|tentative|how\s+about|what\s+about|"
                        r"should\s+we|could\s+we|can\s+we|wanna|want\s+to|lmk|let\s+me\s+know\s+if|"
                        r"if\s+you(?:'re|\s+are|\s+can|\s+want|\s+could))\b", re.I)
# Zone abbreviations and offsets a sender might write after a clock. Deliberately a list: the
# extractor reports every following word as a "timezone", so matching the shape would decline
# "at 11:30 lmk". Omits "AT"/"HT", which are ordinary words.
_ZONE_ABBREVIATION = re.compile(
    r"^(?:[ECMP][SD]T|[ECMP]T|AK[SD]T|H[SD]T|GMT|UTC|BST|CE?S?T|EE?S?T|WE?S?T|MSK|IST|JST|KST|SGT|HKT|"
    r"PKT|SAST|AE[SD]T|AC[SD]T|AWST|NZ[SD]T|Pacific(?:\s+time)?|Eastern(?:\s+time)?|"
    r"Central(?:\s+time)?|Mountain(?:\s+time)?)?(?:\s*[+-]\d{1,2}(?::?\d{2})?)?$", re.I)

# A zone named in words right after a clock ("3pm Eastern"), which the extractor ignores. A
# capitalised word after it ("Central Park") makes it a place, not a zone.
_ZONE_WORD = re.compile(
    r"\d\s*(?i:am|pm)?\s*(?i:eastern|central|mountain|pacific|atlantic|hawaii|alaska)"
    r"\b(?!\s+[A-Z])")


@dataclass(frozen=True)
class Resolved:
    start: datetime | None          # aware; None when only a date is known
    day: date | None                # set whenever anything resolved
    has_clock: bool
    quote: str
    inferred: tuple[str, ...] = ()  # e.g. ("date:next_occurrence", "meridiem")
    tentative: bool = False         # a question or proposal, or a guessed meridiem
    blocked: str | None = None      # why nothing was resolved


class _AmbiguousWall(ValueError):
    """A local wall time that does not exist (spring gap) or happens twice (autumn fold)."""


def _at(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    """A wall time in `zone`. Refuses a DST gap or fold instead of silently picking an instant:
    the sender meant one of them, and a reminder an hour off is worse than none."""
    wall = datetime.combine(day, time(hour, minute))
    first, second = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise _AmbiguousWall(str(wall))
    return first


def _next_clock(arrival: datetime, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    today = _at(arrival.date(), hour, minute, zone)
    return today if today > arrival else _at(arrival.date() + timedelta(days=1), hour, minute, zone)


def _far(start: datetime, arrival: datetime) -> bool:
    """A bare clock with no day ("at 6") means "soon". Read as 12+ hours away it is a
    guess (said at 11pm, did they mean tomorrow evening or tomorrow morning?)."""
    return start - arrival > timedelta(hours=12)


def _source_zone(fact, tz: str) -> ZoneInfo | None | bool:
    """The zone the sender stated, if it differs from the user's.

    None: no zone was stated (or it is the user's own), so the conversational defaults apply.
    A ZoneInfo: an unambiguous named zone ("UTC", "America/New_York").
    False: a zone was stated that is not safe to read ("EST", "PT", "+02:00", "3pm Eastern");
    the caller declines rather than guessing, because the guess moves the reminder by hours."""
    named = fact.start.timezone
    if named and named != tz:
        if named in ("UTC", "Z") or "/" in named:
            try:
                return ZoneInfo("UTC" if named == "Z" else named)
            except (KeyError, ValueError, OSError):
                return False
        # The extractor files ANY word after a clock ("lmk", "ok", "gym") under timezone, so only
        # a real abbreviation or offset counts; every other word is just a word.
        if _ZONE_ABBREVIATION.match(named):
            return False
    if _ZONE_WORD.search(fact.context_span.quote):
        return False                          # the extractor dropped a zone word it did not parse
    return None


def _in_source_zone(f, source: ZoneInfo, zone: ZoneInfo, quote: str, inferred: list,
                    tentative: bool) -> Resolved:
    s = f.start
    ambiguous_clock = (1 <= s.hour <= 12 and not _EXPLICIT_MERIDIEM.search(f.span.quote)
                       and not _TWENTY_FOUR_HOUR.search(f.span.quote))
    if not (s.year and s.month and s.day) or ambiguous_clock:
        # A date or am/pm we would have to guess, in a zone that is not ours: refuse.
        return Resolved(None, None, False, quote, tentative=tentative, blocked="other_timezone")
    wall = datetime(s.year, s.month, s.day, s.hour, s.minute or 0)
    first, second = wall.replace(tzinfo=source, fold=0), wall.replace(tzinfo=source, fold=1)
    if first.utcoffset() != second.utcoffset():
        return Resolved(None, None, False, quote, tentative=tentative, blocked="other_timezone")
    if first.astimezone(ZoneInfo("UTC")).astimezone(source).replace(tzinfo=None) != wall:
        return Resolved(None, None, False, quote, tentative=tentative, blocked="other_timezone")
    start = first.astimezone(zone)
    return Resolved(start, start.date(), True, quote, tuple(inferred + ["timezone:converted"]),
                    tentative)


def _weekday_date(arrival: datetime, quote: str) -> date | None:
    """Nearest upcoming such weekday. Said on that weekday in the evening, or as
    "next <weekday>", it means a week out."""
    m = _WEEKDAY_RE.search(quote)
    if not m:
        return None
    ahead = (_WEEKDAYS[m.group(2).lower()] - arrival.weekday()) % 7
    if ahead == 0 and ((m.group(1) or "").lower() == "next" or arrival.hour >= 20):
        ahead = 7
    return arrival.date() + timedelta(days=ahead)


def _yearless(arrival: datetime, month: int, day: int) -> date | None:
    for year in (arrival.year, arrival.year + 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        if cand >= arrival.date():
            return cand
    return None


# A day the sender named. If one was written and the result still carries no day, it was lost
# on the way (the extractor can drop it), and "the next 11:30" is a guess about a different day.
_STATED_DAY = re.compile(
    r"\b(?:tomorrow|tmrw|tmr|tmw|day\s+after\s+tomorrow|" + "|".join(_WEEKDAYS) + r")\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.I)


def _without_stray_words(text: str, tz: str, arrival: datetime, source_id: str) -> str:
    """Drop a trailing word the extractor mistook for a timezone ("... 11:30am lol").

    The extractor files ANY short word after a clock under `timezone`, and once it has chosen
    that "zone" it can no longer resolve a "tomorrow" earlier in the same phrase. Setting a word
    that is not a real zone off with a comma restores the phrase to what the sender meant, and keeps
    the word ("lmk" still marks a proposal)."""
    for _ in range(3):
        facts = extract_temporal_facts(text, captured_at=arrival, timezone=tz,
                                       evidence=EvidenceContext("attention", source_id)).facts
        for f in facts:
            named, quote = f.start.timezone, f.span.quote
            if (f.start.hour is not None and named and named != tz and named not in ("UTC", "Z")
                    and "/" not in named and not _ZONE_ABBREVIATION.match(named)
                    and quote in text):
                # The word follows the clock; the span may run on past it ("... 11:30am ur place").
                spaced = re.sub(r"(?<=[\dapmAPM])\s+(" + re.escape(named) + r")\b", r", \1", quote, count=1)
                if spaced != quote:
                    text = text.replace(quote, spaced, 1)
                    break
        else:
            return text
    return text


def resolve(text: str, arrival: datetime, tz: str, source_id: str = "x") -> Resolved:
    """Best single time stated in `text`, or a `Resolved` explaining why none."""
    arrival = arrival.astimezone(ZoneInfo(tz))
    text = _without_stray_words(text, tz, arrival, source_id)
    try:
        got = _resolve(text, arrival, tz, source_id)
    except _AmbiguousWall:
        return Resolved(None, None, False, "", blocked="dst_ambiguous")
    if got.has_clock and "date:next_occurrence" in got.inferred and _STATED_DAY.search(text):
        return Resolved(None, None, False, got.quote, tentative=got.tentative, blocked="date_unresolved")
    return got


def _resolve(text: str, arrival: datetime, tz: str, source_id: str) -> Resolved:
    zone = ZoneInfo(tz)
    arrival = arrival.astimezone(zone)
    facts = list(extract_temporal_facts(
        text, captured_at=arrival, timezone=tz,
        evidence=EvidenceContext("attention", source_id)).facts)
    if not facts:
        return Resolved(None, None, False, "", blocked="no_time_stated")
    quote = " / ".join(f.span.quote for f in facts)

    if any("negated_or_cancelled" in f.uncertainties and _CANCEL.search(f.context_span.quote)
           for f in facts):
        return Resolved(None, None, False, quote, blocked="negated_or_cancelled")
    facts = [f for f in facts if f.start.precision != "instant"]    # "in 20 minutes" is an ETA
    if not facts:
        return Resolved(None, None, False, quote, blocked="relative_eta")

    tentative = any(_TENTATIVE.search(f.context_span.quote) for f in facts)
    clocks = [f for f in facts if f.start.hour is not None]
    if len({(f.start.hour, f.start.minute, f.start.timezone) for f in clocks}) > 1:
        return Resolved(None, None, False, quote, tentative=tentative, blocked="conflicting_times")
    source = _source_zone(clocks[0], tz) if clocks else None
    if source is False:                       # a zone was named that cannot be read safely
        return Resolved(None, None, False, quote, tentative=tentative, blocked="other_timezone")

    first = min(f.span.start for f in facts)
    clause = text[max((text.rfind(c, 0, first) for c in ".!?\n"), default=-1) + 1:first]
    if _PAST.search(clause):
        return Resolved(None, None, False, quote, blocked="past_reference")

    # Every day the message mentions. Mentions that agree ("Monday (10/5)") are one day.
    # The day the message itself arrived on is dropped when another day is named
    # ("Quick Sunday heads-up. ... due Monday"): it describes the present, not the plan.
    days: dict[date, tuple[str, ...]] = {}
    for f in facts:
        s = f.start
        if s.year and s.month and s.day:
            days.setdefault(date(s.year, s.month, s.day), ())
        elif s.month and s.day:
            cand = _yearless(arrival, s.month, s.day)
            if cand:
                days.setdefault(cand, ("date:year",))
        else:
            cand = _weekday_date(arrival, f.span.quote)
            if cand:
                days.setdefault(cand, ("date:nearest_weekday",))
    if len(days) > 1 and arrival.date() in days:
        del days[arrival.date()]
    if len(days) > 1:
        return Resolved(None, None, False, quote, tentative=tentative, blocked="conflicting_times")
    day, inferred = next(iter(days.items())) if days else (None, ())
    inferred = list(inferred)

    if not clocks:
        if day is None:
            return Resolved(None, None, False, quote, tentative=tentative, blocked="no_usable_time")
        return Resolved(None, day, False, quote, tuple(inferred), tentative)

    f = clocks[0]
    hour, minute = f.start.hour, f.start.minute or 0
    if source is not None:                    # the sender named a zone: keep that instant
        return _in_source_zone(f, source, zone, quote, inferred, tentative)
    ambiguous = (1 <= hour <= 12 and not _EXPLICIT_MERIDIEM.search(f.span.quote)
                 and not _TWENTY_FOUR_HOUR.search(f.span.quote))
    if ambiguous:
        inferred.append("meridiem")
        hour %= 12
        if day is None:                                 # nearest upcoming of am/pm
            start = min(_next_clock(arrival, h, minute, zone) for h in (hour, hour + 12))
            return Resolved(start, start.date(), True, quote,
                            tuple(inferred + ["date:next_occurrence"]),
                            tentative or _far(start, arrival))
        # A known day with a bare hour: 1-6 is the afternoon; 7-11 could be either.
        if not 1 <= hour <= 6:
            tentative = True
        hour += 12
        return Resolved(_at(day, hour, minute, zone), day, True, quote, tuple(inferred), tentative)
    if day is None:
        start = _next_clock(arrival, hour, minute, zone)
        return Resolved(start, start.date(), True, quote,
                        tuple(inferred + ["date:next_occurrence"]),
                        tentative or _far(start, arrival))
    return Resolved(_at(day, hour, minute, zone), day, True, quote, tuple(inferred), tentative)
