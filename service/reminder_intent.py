"""Reminder intent and deterministic defaults for ordinary time phrases."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from service.tasks.temporal import DAYPART_HOURS as STANDARD_PART_OF_DAY_HOURS

REMINDER_CREATE_RE = re.compile(
    r"\bremind me\b|\b(?:send|give)\s+me\s+(?:an?\s+)?reminder\b|"
    r"\b(?:remember|don'?t forget)\s+to\s+\w+|"
    r"\b(?:add|create|set(?:\s+up)?|make|schedule)\s+"
    r"(?:me\s+)?(?:(?:an?|the|my)\s+)?(?:reminders?|alarm)\b"
    r"(?!\s+(?:code|password|system|volume|sound)\b)", re.I)
_CLOCK = re.compile(
    r"\b(?:\d{1,2}:\d{2}(?:\s*[ap]\.?m\.?)?|"
    r"\d{1,2}\s*[ap]\.?m\.?|noon|midnight)\b", re.I)
_OFFSET = re.compile(
    r"\b(?:in\s+)?(?:\d+|an?|one|two|three|four|five|ten|fifteen|"
    r"twenty|thirty|forty|sixty|half\s+an?)\s*"
    r"(?:minutes?|mins?|hours?|hrs?|days?|weeks?)\b|"
    r"\bthe\s+day\s+before\b", re.I)
_APPOINTMENT = re.compile(r"\b(?:appointment|repair|meeting|reservation|event)\b", re.I)
_TIME_WORD = r"morning|afternoon|evening|night"
_WEEKDAY = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_NAMED_ALERT = re.compile(
    rf"\b(?:today|tomorrow|tonight|{_WEEKDAY})"
    rf"(?:\s+(?:{_TIME_WORD}))?(?:\s+at\s+\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)?)?\b|"
    rf"\b(?:this|next|later)?\s*(?:{_TIME_WORD})\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}(?:st|nd|rd|th)\b",
    re.I,
)

# Product defaults are shared by reminders, alarms and scheduled actions.
# A date without a time uses the morning default.


def has_unsupported_alert_clock(text: str) -> bool:
    """Recognize clock wording we must clarify, never reduce to a date default.

    This is a bounded rejection guard, not a natural-language clock parser.
    In particular, "half six tomorrow" is not merely "tomorrow". Relative
    durations ("in half an hour") and the supported bare "tomorrow at 6"
    convention remain owned by the existing temporal resolvers.
    """
    hour_word = r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    if re.search(
            rf"\b(?:half\s+(?:past\s+)?|(?:a\s+)?quarter\s+(?:past|to)\s+)"
            rf"(?:{hour_word}|\d{{1,2}})\b|"
            rf"(?:^|\b(?:at|for)\s+){hour_word}"
            r"(?=\s*(?:$|[.!?,]|[ap]\.?m\.?\b|tomorrow\b|today\b|o['’]?clock\b))|"
            rf"\b{hour_word}\s+(?:[ap]\.?m\.?|o['’]?clock)\b|"
            r"(?:^|\b(?:at|for)\s+)\d{1,2}\s*(?:or|/|-)\s*\d{1,2}\b", text, re.I):
        return True
    # Do not let a partial numeric match turn "tomorrow at 6:7" into 6pm,
    # or an invalid clock such as 25:00 into tomorrow's 09:00 default.
    for clock in re.finditer(
            r"\b(?P<hour>\d+)(?::(?P<minute>\d*)(?:\s*(?P<ampm>[ap]\.?m\.?))?"
            r"|\s*(?P<bare_ampm>[ap]\.?m\.?))",
            text, re.I):
        # Colon pairs inside a subject/target ("prepare for my 1:1") are
        # not clocks. Invalid colon-only tokens need a temporal introducer
        # or a bare time-answer position; am/pm already identifies a clock.
        ampm = clock["ampm"] or clock["bare_ampm"]
        if not ampm and text[:clock.start()].strip() and not re.search(
                r"\b(?:at|for)\s*$", text[:clock.start()], re.I):
            continue
        hour, minute = int(clock["hour"]), clock["minute"]
        if ((minute is not None and (len(minute) != 2 or int(minute) > 59))
                or hour > 23 or (ampm and not 1 <= hour <= 12)):
            return True
    return False


def is_unsupported_time_answer(text: str) -> bool:
    """An unresolved clock reply, not a new request mentioning that clock."""
    return bool(re.match(
        r"^\s*(?:(?:at|for|make it|set it for|yes[, ]*)\s*)?"
        rf"(?:\d|half\b|(?:a\s+)?quarter\b|one\b|two\b|three\b|four\b|"
        rf"five\b|six\b|seven\b|eight\b|nine\b|ten\b|eleven\b|twelve\b|"
        rf"today\b|tomorrow\b|tonight\b|{_WEEKDAY}\b)", text, re.I)
        and len(text.split()) <= 16 and has_unsupported_alert_clock(text)
        and not re.search(r"\b(?:don't|do not|never|cancel|delete|instead\s+of)\b", text, re.I))


def has_alert_time(text: str) -> bool:
    if has_unsupported_alert_clock(text):
        return False
    text = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow", text,
                  flags=re.I)
    if re.search(r"\bthe\s+day\s+before\b", text, re.I):
        return True
    if (offset := _OFFSET.search(text)) and (
            re.search(r"\bin\s+", offset.group(), re.I) or
            re.match(r"\s+(?:before|ahead\s+of|early|earlier)\b", text[offset.end():], re.I)):
        return True
    clock = _CLOCK.search(text)
    if not clock:
        named = _NAMED_ALERT.search(text)
        if named:
            # "my repair appointment tomorrow" names the appointment's date,
            # not an alert time. "set an alarm tomorrow for my repair" names
            # the alarm's day and should use the standard morning time.
            return _APPOINTMENT.search(text[:named.start()]) is None
        return bool(re.search(r"\b(?:at\s+(?:the\s+)?(?:start|same\s+time)|"
                              r"when\s+it\s+(?:starts|begins))\b", text, re.I))
    # An appointment's clock time isn't the user's chosen alert time.
    return not _APPOINTMENT.search(text[:clock.start()])


def resolve_alert_datetime(text: str, *, now: datetime | None = None) -> datetime | None:
    """Resolve common user time phrases to Wisp's standardized local time."""
    if has_unsupported_alert_clock(text):
        return None
    now = now or datetime.now()
    lowered = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow",
                     text.lower())
    lowered = re.sub(r"\s+", " ", lowered).strip()
    clock = _CLOCK.search(lowered)
    if clock and _APPOINTMENT.search(lowered[:clock.start()]):
        return None

    from service.tools.timeranges import BadWhen, resolve_when

    candidates: list[str] = []
    if m := re.search(
            rf"\b(?P<day>today|tomorrow|tonight|{_WEEKDAY})\b"
            rf"(?:\s+(?P<word>{_TIME_WORD}))?"
            r"(?:\s+at\s+(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight))?",
            lowered, re.I):
        phrase = m.group("day")
        if m.group("word"):
            phrase += " " + m.group("word")
        if m.group("clock"):
            phrase += ((" " if m.group("clock") in {"noon", "midnight"} else " at ")
                       + m.group("clock"))
        candidates.append(phrase)
    if m := re.search(
            r"\b(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+"
            rf"(?P<day>today|tomorrow|{_WEEKDAY})\b", lowered, re.I):
        candidates.insert(0, f"{m.group('day')} at {m.group('clock')}")
    if m := re.search(
            rf"\b(?P<word>noon|midnight)\s+(?P<day>today|tomorrow|{_WEEKDAY})\b",
            lowered, re.I):
        candidates.insert(0, f"{m.group('day')} {m.group('word')}")
    if clock:
        candidates.append(clock.group(0))

    # Standalone/"this" part of day means its next sensible occurrence.
    if m := re.search(rf"\b(?:(this|next|later)\s+)?({_TIME_WORD})\b", lowered, re.I):
        modifier, word = m.group(1), m.group(2)
        day = now.replace(hour=STANDARD_PART_OF_DAY_HOURS[word], minute=0,
                          second=0, microsecond=0)
        if modifier == "next" or (modifier != "this" and day <= now):
            day += timedelta(days=1)
        candidates.insert(0, day.isoformat(timespec="minutes"))

    for phrase in candidates:
        try:
            resolved, _ = resolve_when(phrase, now=now)
            return resolved.replace(second=0, microsecond=0)
        except BadWhen:
            continue
    return None


def is_time_answer(text: str) -> bool:
    return bool(re.match(
        r"^\s*(?:(?:at|in|make it|set it for|yes[, ]*)\s*)?"
        rf"(?:\d|noon\b|midnight\b|today\b|tomorrow\b|tonight\b|"
        rf"{_TIME_WORD}\b|{_WEEKDAY}\b|an?\b|one\b|two\b|three\b|the\s+day\b|"
        r"half\b|fifteen\b|thirty\b|sixty\b|the same time\b|when it\b)", text, re.I)
        and len(text.split()) <= 16 and has_alert_time(text) and not re.search(
        r"\b(?:don't|do not|never|cancel|delete|instead\s+of)\b", text, re.I)
    )


def asks_alert_time(text: str) -> bool:
    return bool(re.search(r"\b(?:what time|when|how (?:long|much|early)|how far)\b"
                          r"[^?\n]{0,140}\b(?:remind|reminder|alarm|before|alert)\b", text, re.I))
