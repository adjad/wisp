"""Pure private Calendar block planning; no writes or attendee availability."""
from __future__ import annotations

from datetime import datetime
import math
from zoneinfo import ZoneInfo

from service.tasks.temporal import unambiguous_local_time


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _title(value):
    return " ".join(str(value or "").split()).casefold()


def private_meeting_arguments(args):
    """Validate the complete, exact human-reviewed commit payload without coercion."""
    import re
    if not isinstance(args, dict) or set(args) != {
            "capability_token", "title", "when_iso", "duration_min"}:
        raise ValueError("Provide the complete private meeting proposal arguments.")
    token, title, start = (args[name] for name in ("capability_token", "title", "when_iso"))
    if (not isinstance(token, str)
            or re.fullmatch(r"wisp_meeting_cap_[0-9a-f]{64}", token) is None):
        raise ValueError("Private meeting capability is invalid; request a new preview.")
    if (not isinstance(title, str) or not title or title != title.strip() or len(title) > 500
            or not isinstance(start, str) or not start or len(start) > 64
            or type(args["duration_min"]) is not int or not 1 <= args["duration_min"] <= 1440):
        raise ValueError("Private meeting title, exact start and duration are invalid.")
    if re.search(r"wisp_meeting_cap_[0-9a-f]{64}", title + start, re.I):
        raise ValueError("Private meeting capability cannot appear in event content.")
    return {name: args[name] for name in ("capability_token", "title", "when_iso", "duration_min")}


def unique_calendar_rows(rows):
    """Collapse repeated snapshot reads of one ID, never distinct occurrences."""
    found = {}
    for row in rows:
        if row.get("source") != "calendar" or row.get("status") not in (None, "active"):
            continue
        key = row.get("id")
        if not key:
            raise ValueError("Calendar occurrence identity is missing; refresh Calendar.")
        value = {name: row.get(name) for name in
                 ("id", "title", "when_ts", "end_ts", "all_day", "source_id")}
        if key in found and found[key] != value:
            raise ValueError("Calendar changed between snapshots; refresh before planning.")
        found[key] = value
    return list(found.values())


def select_occurrence(rows, title, *, expected_id="", bounds):
    """Select only a uniquely identified occurrence starting in the chosen range."""
    candidates = [r for r in unique_calendar_rows(rows)
                  if _number(r.get("when_ts")) and bounds[0] <= r["when_ts"] < bounds[1]
                  and _title(title) in _title(r.get("title"))]
    exact = [r for r in candidates if _title(r.get("title")) == _title(title)]
    if exact:
        candidates = exact
    if expected_id:
        candidates = [r for r in candidates if r["id"] == expected_id]
    if len(candidates) != 1:
        return {"status": "needs_input", "reason":
                "Choose one exact Calendar occurrence; no unique match was selected.",
                "choices": candidates[:8], "choices_truncated": len(candidates) > 8}
    row = candidates[0]
    if row.get("all_day") or not _number(row.get("end_ts")) or row["end_ts"] <= row["when_ts"]:
        return {"status": "needs_input", "reason":
                "The reference is all-day or has no valid stored end time; ask for an exact meeting start."}
    return {"status": "selected", "reference": row}


def plan_private_block(rows, *, title, start_ts, duration_min, timezone, now_ts, reference=None):
    """Check elapsed-time intervals against real Calendar ends, half-open at both boundaries."""
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Provide a meeting title of 1–500 characters.")
    if type(duration_min) is not int or not 1 <= duration_min <= 1440:
        raise ValueError("Provide a duration of 1–1440 whole minutes.")
    if not _number(start_ts) or not _number(now_ts) or start_ts < now_ts:
        raise ValueError("Provide an exact meeting start in the future.")
    zone = ZoneInfo(timezone)
    start = datetime.fromtimestamp(start_ts, zone)
    if start.second or start.microsecond or not unambiguous_local_time(start):
        raise ValueError("Meeting start has hidden seconds or an ambiguous local wall time.")
    end_ts = start_ts + duration_min * 60
    end = datetime.fromtimestamp(end_ts, zone)
    conflicts, incomplete = [], []
    for row in unique_calendar_rows(rows):
        begin, finish = row.get("when_ts"), row.get("end_ts")
        if not _number(begin):
            incomplete.append(row)
        elif not _number(finish) or finish < begin or (row.get("all_day") and finish == begin):
            # An unknown end could still cover the requested interval. Do not
            # invent a duration, including for an overnight or all-day item.
            if begin < end_ts:
                incomplete.append(row)
        elif begin < end_ts and finish > start_ts:
            conflicts.append(row)
    if incomplete:
        return {"status": "needs_input", "reason":
                "Calendar has items with unknown or invalid ends; availability cannot be verified.",
                "items": incomplete[:8]}
    if conflicts:
        return {"status": "conflict", "reason": "The proposed interval overlaps Calendar events.",
                "conflicts": conflicts[:8], "conflicts_truncated": len(conflicts) > 8}
    return {"status": "ready", "private_block": {
                "title": title.strip(), "when_iso": start.isoformat(), "duration_min": duration_min},
            "end_iso": end.isoformat(), "reference": reference,
            "scope": "User Calendar only; other attendees' availability is unverified.",
            "effect": "No event created. A private block needs explicit Calendar approval; attendees are not invited."}


def exact_start(when_iso, timezone):
    """Bind a local ISO start to an IANA zone without accepting offset drift."""
    try:
        value = datetime.fromisoformat(when_iso)
        local = value.replace(tzinfo=ZoneInfo(timezone)) if value.tzinfo is None else value.astimezone(ZoneInfo(timezone))
        if (value.replace(tzinfo=None) != local.replace(tzinfo=None)
                or (value.tzinfo is not None and value.utcoffset() != local.utcoffset())
                or value.second or value.microsecond or not unambiguous_local_time(local)):
            raise ValueError()
        return local.timestamp()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError("Use an exact, unambiguous local ISO datetime with the correct UTC offset.") from exc
