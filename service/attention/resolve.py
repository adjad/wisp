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


@dataclass(frozen=True)
class Resolved:
    start: datetime | None          # aware; None when only a date is known
    day: date | None                # set whenever anything resolved
    has_clock: bool
    quote: str
    inferred: tuple[str, ...] = ()  # e.g. ("date:next_occurrence", "meridiem")
    tentative: bool = False         # a question or proposal, or a guessed meridiem
    blocked: str | None = None      # why nothing was resolved


def _at(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour, minute), zone)


def _next_clock(arrival: datetime, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    today = _at(arrival.date(), hour, minute, zone)
    return today if today > arrival else _at(arrival.date() + timedelta(days=1), hour, minute, zone)


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


def resolve(text: str, arrival: datetime, tz: str, source_id: str = "x") -> Resolved:
    """Best single time stated in `text`, or a `Resolved` explaining why none."""
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
    if len({(f.start.hour, f.start.minute) for f in clocks}) > 1:
        return Resolved(None, None, False, quote, tentative=tentative, blocked="conflicting_times")

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
    ambiguous = (1 <= hour <= 12 and not _EXPLICIT_MERIDIEM.search(f.span.quote)
                 and not _TWENTY_FOUR_HOUR.search(f.span.quote))
    if ambiguous:
        inferred.append("meridiem")
        hour %= 12
        if day is None:                                 # nearest upcoming of am/pm
            start = min(_next_clock(arrival, h, minute, zone) for h in (hour, hour + 12))
            return Resolved(start, start.date(), True, quote,
                            tuple(inferred + ["date:next_occurrence"]), tentative)
        # A known day with a bare hour: 1-6 is the afternoon; 7-11 could be either.
        if not 1 <= hour <= 6:
            tentative = True
        hour += 12
        return Resolved(_at(day, hour, minute, zone), day, True, quote, tuple(inferred), tentative)
    if day is None:
        start = _next_clock(arrival, hour, minute, zone)
        return Resolved(start, start.date(), True, quote,
                        tuple(inferred + ["date:next_occurrence"]), tentative)
    return Resolved(_at(day, hour, minute, zone), day, True, quote, tuple(inferred), tentative)
