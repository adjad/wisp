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


def local_timezone_name(now: datetime | None = None) -> str:
    zone = (now or datetime.now().astimezone()).astimezone().tzinfo
    return str(getattr(zone, "key", None) or zone or "local")


def resolve_named_time(text: str, *, now: datetime | None = None) -> tuple[datetime | None, str]:
    """Resolve a user time and report which daypart default was applied."""
    now = now or datetime.now()
    value = re.sub(r"\b(?:tommorow|tommorrow|tmrw|tmrow)\b", "tomorrow",
                   " ".join(text.lower().split()))
    clock = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight"
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

    phrases: list[tuple[str, str]] = []
    if match := re.search(
            rf"\b(?P<day>{day})\b(?:\s+(?P<word>{timeword}))?"
            rf"(?:\s+at\s+(?P<clock>{clock}))?\b", value, re.I):
        phrase = match.group("day")
        defaulted = ""
        if match.group("word"):
            phrase += " " + match.group("word")
            defaulted = match.group("word")
        if match.group("clock"):
            phrase += (" " if match.group("clock") in {"noon", "midnight"} else " at ")
            phrase += match.group("clock")
            defaulted = ""
        elif not match.group("word") and match.group("day") != "tonight":
            defaulted = "morning"
        elif match.group("day") == "tonight":
            defaulted = "tonight"
        phrases.append((phrase, defaulted))
    if match := re.search(rf"\b(?P<clock>{clock})\s+(?P<day>{day})\b", value, re.I):
        phrases.insert(0, (f"{match.group('day')} at {match.group('clock')}", ""))
    if not phrases and (match := re.search(rf"\b(?:at|by|this|next|later)?\s*(?P<word>{timeword})\b",
                                           value, re.I)):
        word = match.group("word")
        target = now.replace(hour=DAYPART_HOURS[word], minute=0, second=0, microsecond=0)
        if target <= now and not re.search(rf"\bthis\s+{word}\b", value, re.I):
            target += timedelta(days=1)
        return target, word
    if not phrases and (match := re.search(rf"\b(?P<clock>{clock})\b", value, re.I)):
        phrases.append((match.group("clock"), ""))

    from service.tools.timeranges import BadWhen, resolve_when
    for phrase, defaulted in phrases:
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
