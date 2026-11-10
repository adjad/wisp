"""One deterministic time vocabulary for every typed task."""
from __future__ import annotations

from datetime import datetime, timedelta
import re


DAYPART_HOURS = {
    "morning": 9,
    "afternoon": 15,
    "evening": 18,
    "night": 20,
    "tonight": 20,
}

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "sixty": 60,
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_NAMED_DATE = re.compile(
    r"\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?\b",
    re.I,
)
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_WEEKDAY_NAMES = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
# A day this resolver does not compute. It must ask (or fall through) rather
# than settle on a nearby day it does know: "the day after tomorrow" is not
# tomorrow, and "on the 15th" is not today.
_UNSUPPORTED_DAY = re.compile(
    r"\bday\s+after\s+(?:tomorrow|tommorow|tommorrow|tmrw|tmrow)\b"
    r"|\bon\s+(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b"
    r"|\bthe\s+\d{1,2}(?:st|nd|rd|th)(?=\s*(?:$|[,.;!?]|(?:at|of|by|to|for|and|in)\b))",
    re.I,
)
# Every clock a sentence can state. One the resolver did not use means it
# would otherwise pick a default hour while the user named a different one.
_CLOCK_TOKEN = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\b"
    r"|\bat\s+\d{1,2}(?::\d{2})?(?=\s*(?:$|[,.;!?]|o'?clock\b|(?:on|to|and|in|this|next|"
    r"today|tonight|tomorrow|morning|afternoon|evening|night|" + _WEEKDAY_NAMES + r")\b))",
    re.I,
)


def has_unsupported_day(text: str) -> bool:
    """True for a day phrase resolve_named_time cannot compute."""
    return bool(_UNSUPPORTED_DAY.search(text or ""))


def has_clock_token(text: str) -> bool:
    """True when the text states a clock time anywhere."""
    return bool(_CLOCK_TOKEN.search(text or ""))


def _unused_clock(value: str, spans: list[tuple[int, int]]) -> bool:
    return any(not any(start <= token.start() < end for start, end in spans)
               for token in _CLOCK_TOKEN.finditer(value))


def local_timezone_name(now: datetime | None = None) -> str:
    zone = (now.tzinfo if now is not None and now.tzinfo is not None
            else (now or datetime.now()).astimezone().tzinfo)
    return str(getattr(zone, "key", None) or zone or "local")


def unambiguous_local_time(value: datetime) -> bool:
    """Reject local DST gaps/folds before a wall time becomes an epoch."""
    try:
        first = value.replace(fold=0).timestamp()
        second = value.replace(fold=1).timestamp()
        if first != second:
            return False
        back = datetime.fromtimestamp(first, tz=value.tzinfo)
        return back.replace(tzinfo=None) == value.replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return False


def resolve_named_time(text: str, *, now: datetime | None = None,
                       clock_check: bool = True) -> tuple[datetime | None, str]:
    """Resolve a user time and report which daypart default was applied.

    A default hour is refused when the text names a clock the day phrase did
    not take. clock_check=False is for a caller that knows every such clock
    came from quoted title words, which are not alert evidence.
    """
    now = now or datetime.now()
    value = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow",
                   " ".join(text.lower().split()))
    # Never resolve one endpoint of a range as the time of a single reminder
    # or scheduled action. The caller must ask which exact alert time to use.
    from service.reminder_intent import has_unsupported_alert_clock
    if has_unsupported_alert_clock(value, time_answer=True) or has_unsupported_day(value):
        return None, ""
    clock = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight"
    # A clock with no am/pm counts only after "at": "tomorrow at 7", never the
    # digits of "tomorrow 3 pages".
    bare = r"\d{1,2}(?::\d{2})?(?![\d:])(?!\s*[ap]\.?m\b)"
    ampm = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)"
    day = (r"today|tonight|tomorrow|monday|tuesday|wednesday|thursday|"
           r"friday|saturday|sunday")
    timeword = r"morning|afternoon|evening|night"

    # An appointment's stated time is reference data, not the requested alert
    # time.  "alarm at 1 for the appointment" remains a real alert because the
    # appointment noun follows the clock; "alarm for my appointment at 1" asks
    # for a separate lead time.
    temporal_pos = re.search(rf"\b(?:{day}|{clock}|{timeword})\b", value, re.I)
    appointment = re.search(
        r"\b(?:appointment|repair|meeting|reservation|event)\b", value, re.I)
    if temporal_pos and appointment and appointment.start() < temporal_pos.start():
        return None, ""

    from service.tools.timeranges import BadWhen, resolve_when

    # A named date takes precedence over any bare clock. Previously the date
    # was dropped and "Sep 28 at 7pm" became the next 7pm, sometimes today.
    named = _NAMED_DATE.search(value)
    iso = _ISO_DATE.search(value)
    dated = iso or named
    if dated:
        year = int(dated.group("year") or now.year)
        month = (int(dated.group("month")) if iso
                 else _MONTHS[dated.group("month")[:3]])
        try:
            day = datetime(year, month, int(dated.group("day")), tzinfo=now.tzinfo)
        except ValueError:
            return None, ""
        if not dated.group("year") and day.date() < now.date():
            try:
                day = day.replace(year=year + 1)
            except ValueError:
                return None, ""
        if match := re.match(rf",?\s+at\s+(?P<hour>\d{{1,2}})(?::(?P<minute>\d{{2}}))?"
                             rf"(?![\d:])(?!\s*[ap]\.?m\b)", value[dated.end():], re.I):
            # "nov 15 at 5" reads the hour as it would on any named day.
            from service.tools.timeranges import _bare_hour_on_a_day
            hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
            if hour > 23 or minute > 59:
                return None, ""
            chosen = day.replace(hour=hour if hour > 12 else _bare_hour_on_a_day(hour, "", None),
                                 minute=minute)
            return (chosen, "") if unambiguous_local_time(chosen) else (None, "")
        if match := re.search(rf"\b(?:{clock})\b", value[dated.end():], re.I):
            clock_phrase = match.group()
        elif match := re.search(rf"\b(?:{clock})\b", value[:dated.start()], re.I):
            clock_phrase = match.group()
        else:
            if clock_check and _unused_clock(value, [dated.span()]):
                return None, ""
            morning = day.replace(hour=9)
            return (morning, "morning") if unambiguous_local_time(morning) else (None, "")
        try:
            # resolve_when's bare-clock path expects a naive local datetime.
            # Attach the caller's zone only after it resolves the clock on
            # the explicitly selected date.
            resolved, _ = resolve_when(clock_phrase, now=day.replace(tzinfo=None))
        except BadWhen:
            return None, ""
        chosen = resolved.replace(second=0, microsecond=0, tzinfo=now.tzinfo)
        return (chosen, "") if unambiguous_local_time(chosen) else (None, "")

    phrases: list[tuple[str, str, tuple[int, int]]] = []
    if match := re.search(
            rf"\b(?P<day>{day})\b(?:\s+(?P<word>{timeword}))?"
            rf"(?:\s+at\s+(?P<clock>{clock})|\s+at\s+(?P<bare>{bare})"
            rf"|\s+(?P<direct>{ampm})|\s+(?P<named>noon|midnight))?\b", value, re.I):
        phrase = match.group("day")
        defaulted = ""
        if match.group("word"):
            phrase += " " + match.group("word")
            defaulted = match.group("word")
        # "tomorrow noon" and "tomorrow at noon" are the same time, as are
        # "tomorrow 7pm" and "tomorrow at 7pm".
        if said := (match.group("clock") or match.group("bare")
                    or match.group("direct") or match.group("named")):
            phrase += (" " if said in {"noon", "midnight"} and not match.group("word") else " at ")
            phrase += said
            defaulted = ""
        elif not match.group("word") and match.group("day") != "tonight":
            defaulted = "morning"
        elif match.group("day") == "tonight":
            defaulted = "tonight"
        phrases.append((phrase, defaulted, match.span()))
    if match := re.search(rf"(?:\bat\s+(?P<bare>{bare})|\b(?P<clock>{clock}))"
                          rf"\s+(?:on\s+)?(?P<day>{day})\b", value, re.I):
        said = match.group("clock") or match.group("bare")
        phrases.insert(0, (f"{match.group('day')} at {said}", "", match.span()))
    if not phrases and (match := re.search(rf"\b(?:at|by|this|next|later)?\s*(?P<word>{timeword})\b",
                                           value, re.I)):
        if clock_check and _unused_clock(value, [match.span()]):
            return None, ""
        word = match.group("word")
        target = now.replace(hour=DAYPART_HOURS[word], minute=0, second=0, microsecond=0)
        if target <= now and not re.search(rf"\bthis\s+{word}\b", value, re.I):
            target += timedelta(days=1)
        return target, word
    if not phrases and (match := re.search(rf"\b(?P<clock>{clock})\b", value, re.I)):
        phrases.append((match.group("clock"), "", match.span()))

    for phrase, defaulted, span in phrases:
        # A default hour stands only when no clock was said elsewhere.
        if clock_check and defaulted and _unused_clock(value, [span]):
            continue
        try:
            resolved, _ = resolve_when(phrase, now=now)
            return resolved.replace(second=0, microsecond=0), defaulted
        except BadWhen:
            continue
    return None, ""


def parse_lead_seconds(text: str) -> int | None:
    value = " ".join(text.lower().split())
    if re.search(r"\b(?:the|a|one) day before\b", value):
        return 86400
    if re.search(r"\b(?:the )?same time\b|\bwhen (?:it|the .+?) (?:starts|begins)\b", value):
        return 0
    match = re.search(
        r"\b(?P<number>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|"
        r"ten|fifteen|twenty|thirty|forty|sixty)\s*"
        r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\s*"
        r"(?:before|ahead of|early|earlier)\b", value)
    if not match:
        return None
    raw = match.group("number")
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
    if count is None:
        return None
    unit = match.group("unit")
    factor = (60 if unit.startswith(("min",)) else
              3600 if unit.startswith(("hour", "hr")) else
              86400 if unit.startswith("day") else 604800)
    return count * factor


def parse_delay_seconds(text: str) -> int | None:
    """"in 10 minutes" -> 600.  A delay FROM now, not a lead BEFORE an event."""
    match = re.search(
        r"\bin\s+(?P<number>\d+|a|an|one|two|three|four|five|six|seven|eight|"
        r"nine|ten|fifteen|twenty|thirty|forty|sixty)\s*"
        r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
        " ".join(text.lower().split()))
    if not match:
        return None
    raw = match.group("number")
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
    if count is None:
        return None
    unit = match.group("unit")
    factor = (60 if unit.startswith("min") else
              3600 if unit.startswith(("hour", "hr")) else
              86400 if unit.startswith("day") else 604800)
    return count * factor


def apply_lead(reference_when: datetime, lead_seconds: int) -> datetime:
    return reference_when - timedelta(seconds=max(0, int(lead_seconds)))


def reference_tokens(reference: str) -> set[str]:
    normalized = re.sub(r"[-_]", " ", reference.lower())
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    return tokens - {
        "my", "the", "a", "an", "date", "event", "appointment", "meeting",
        "reservation", "time", "calendar", "before", "after",
    }


def resolve_event_reference(reference: str, items: list[dict]) -> list[dict]:
    """Return only equally-best calendar matches for a reference phrase."""
    wanted = reference_tokens(reference)
    if not wanted:
        return []
    scored: list[tuple[float, dict]] = []
    for item in items:
        if item.get("source") != "calendar":
            continue
        title_tokens = reference_tokens(str(item.get("title") or ""))
        overlap = len(wanted & title_tokens)
        if not overlap:
            continue
        score = overlap / len(wanted)
        if wanted <= title_tokens:
            score += 1.0
        scored.append((score, item))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [item for score, item in scored if score == best]
