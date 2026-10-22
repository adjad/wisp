"""Civil-date Calendar creation bounds; no clock or native effects."""
from datetime import date, datetime, time, timedelta
import math
import re
from zoneinfo import ZoneInfo


def all_day_bounds(day: str, timezone: str) -> tuple[float, int]:
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Calendar requires an ISO civil date")
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("Calendar requires a named time zone")
    zone = ZoneInfo(timezone)
    civil = date.fromisoformat(day)
    start = datetime.combine(civil, time(), zone)
    end = datetime.combine(civil + timedelta(days=1), time(), zone)
    # A missing/repeated midnight cannot be silently turned into another day.
    for wall in (start, end):
        if (wall.replace(fold=0).utcoffset() != wall.replace(fold=1).utcoffset()
                or datetime.fromtimestamp(wall.timestamp(), zone) != wall):
            raise ValueError("Calendar date has ambiguous midnight bounds")
    elapsed = (end.timestamp() - start.timestamp()) / 60
    if not math.isfinite(elapsed) or elapsed != int(elapsed) or not 1 <= elapsed <= 10080:
        raise ValueError("Calendar date has invalid bounds")
    return start.timestamp(), int(elapsed)
