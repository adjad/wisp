"""Local-time helpers shared by the miners, predictor, nudges and persona.

Every behavioral statement is about the user's wall clock ("weekday mornings",
"2-5pm"), so all bucketing happens in a named IANA zone via ``zoneinfo``. That
keeps DST transitions correct: 6:45 stays 6:45 the week the clocks change, which
a fixed UTC offset would get wrong for half of an 8-week history.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAYS = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)


def resolve_tz(name: str | None = None) -> tzinfo:
    """The named zone, else the machine's zone, else UTC.

    The machine zone is read from ``TZ`` or the ``/etc/localtime`` symlink
    (both macOS and Linux point it into a zoneinfo tree) so that we still get a
    DST-aware ``ZoneInfo`` rather than today's fixed offset."""
    candidates = [name, os.environ.get("TZ")]
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            candidates.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for cand in candidates:
        if not cand:
            continue
        try:
            return ZoneInfo(cand.lstrip(":"))
        except (ZoneInfoNotFoundError, ValueError):
            continue
    if name is None and Path("/etc/localtime").exists():
        local = datetime.now().astimezone().tzinfo
        if local is not None:
            return local
    return ZoneInfo("UTC")


def tz_name(tz: tzinfo) -> str:
    return getattr(tz, "key", None) or str(tz)


def local_dt(ts: float, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def minute_of(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def at_minute(day: date, minute: float, tz: tzinfo) -> float:
    """Epoch seconds for ``minute`` after local midnight of ``day`` (may exceed 1440)."""
    base = datetime.combine(day, dtime(0, 0), tz)
    whole = int(minute)
    if 0 <= whole < 24 * 60:
        dt = base.replace(hour=whole // 60, minute=whole % 60)
        return dt.timestamp() + (minute - whole) * 60
    return at_minute(day + timedelta(days=whole // (24 * 60)), minute % (24 * 60), tz)


def day_start(day: date, tz: tzinfo) -> float:
    return datetime.combine(day, dtime(0, 0), tz).timestamp()


def day_end(day: date, tz: tzinfo) -> float:
    return day_start(day + timedelta(days=1), tz)


def daterange(first: date, last: date):
    """Inclusive range of dates."""
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


def count_weekdays(first: date, last: date) -> list[int]:
    """How many of each weekday fall in [first, last] (inclusive)."""
    counts = [0] * 7
    if last < first:
        return counts
    days = (last - first).days + 1
    full, rest = divmod(days, 7)
    for wd in range(7):
        counts[wd] = full
    for i in range(rest):
        counts[(first.weekday() + i) % 7] += 1
    return counts


def daypart(minute: int) -> str:
    """morning 5-12, afternoon 12-17, evening 17-22, night otherwise."""
    if 5 * 60 <= minute < 12 * 60:
        return "morning"
    if 12 * 60 <= minute < 17 * 60:
        return "afternoon"
    if 17 * 60 <= minute < 22 * 60:
        return "evening"
    return "night"


DAYPART_RANGES = {"morning": (300, 720), "afternoon": (720, 1020), "evening": (1020, 1320)}


def round_to(minute: float, step: int) -> int:
    return int(math.floor(minute / step + 0.5)) * step


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank-with-interpolation percentile of an already sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


# ---------------------------------------------------------------- formatting

def fmt_clock(minute: int, *, suffix: bool = True) -> str:
    """6:45am, 2pm, 12:30pm. ``suffix=False`` drops am/pm (for ranges)."""
    minute = int(minute) % (24 * 60)
    h, m = divmod(minute, 60)
    ampm = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    text = f"{h12}" if m == 0 else f"{h12}:{m:02d}"
    return text + (ampm if suffix else "")


def fmt_range(start: int, end: int) -> str:
    """"6:45–7:45am", "2–5pm", "11:30am–1pm"."""
    # Midnight as an end (1440) is "12am", not pm: 11pm–12am, never "11–12am".
    same_half = (start < 720 and end < 720) or (720 <= start and end < 24 * 60)
    if same_half:
        return f"{fmt_clock(start, suffix=False)}–{fmt_clock(end)}"
    return f"{fmt_clock(start)}–{fmt_clock(end)}"


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def days_word(weekdays) -> str:
    """Plural noun for a weekday set: weekdays, Mondays, Mon/Wed/Fri, days."""
    wds = tuple(sorted(set(weekdays)))
    if not wds or wds == ALL_DAYS:
        return "days"
    if wds == WEEKDAYS:
        return "weekdays"
    if wds == WEEKEND:
        return "weekend days"
    if len(wds) == 1:
        return DAY_NAMES[wds[0]] + "s"
    return "/".join(DAY_ABBR[w] for w in wds)


def when_phrase(weekdays, part: str, *, most: bool = False) -> str:
    """"on Mon, Wed and Fri mornings", "on weekday afternoons", "every evening",
    "on Sunday evenings"; ``most`` gives "most weekday afternoons"."""
    wds = tuple(sorted(set(weekdays)))
    plural = {"morning": "mornings", "afternoon": "afternoons", "evening": "evenings",
              "night": "nights"}.get(part, part + "s")
    if not wds or wds == ALL_DAYS:
        return f"most {plural}" if most else f"every {part}"
    if wds == WEEKDAYS:
        prefix = "weekday"
    elif wds == WEEKEND:
        prefix = "weekend"
    elif len(wds) == 1:
        prefix = DAY_NAMES[wds[0]]
    else:
        prefix = _join([DAY_ABBR[w] for w in wds])
    return f"most {prefix} {plural}" if most else f"on {prefix} {plural}"


def on_days(weekdays) -> str:
    """"on weekdays", "on Mondays", "on Mon, Wed and Fri", "every day"."""
    wds = tuple(sorted(set(weekdays)))
    if not wds or wds == ALL_DAYS:
        return "every day"
    if wds == WEEKDAYS:
        return "on weekdays"
    if wds == WEEKEND:
        return "on weekends"
    if len(wds) == 1:
        return f"on {DAY_NAMES[wds[0]]}s"
    return "on " + _join([DAY_ABBR[w] for w in wds])
