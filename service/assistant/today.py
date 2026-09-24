"""Deterministic, local day planning. Imported commitments are never mutated."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RevisionConflict(ValueError):
    pass


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def day_bounds(day: str, timezone: str):
    try:
        d = date.fromisoformat(day)
        if d.isoformat() != day:
            raise ValueError()
        zone = ZoneInfo(timezone)
        following = d + timedelta(days=1)
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Use an ISO date and an IANA timezone") from exc
    return (datetime.combine(d, time(), zone).timestamp(),
            datetime.combine(following, time(), zone).timestamp())


def wall_time(day, timezone, minute):
    """Reject nonexistent/ambiguous wall times rather than silently picking a fold."""
    d = date.fromisoformat(day)
    naive = datetime.combine(d, time()) + timedelta(minutes=minute)
    zone = ZoneInfo(timezone)
    candidates = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        stamp = aware.timestamp()
        if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == naive:
            candidates.add(stamp)
    if len(candidates) != 1:
        raise ValueError("Working hours fall in a daylight-saving gap or repeated hour; choose another time")
    return candidates.pop()


def validate_preferences(day, timezone, start_minute=540, end_minute=1080, not_before=None):
    day_bounds(day, timezone)
    if any(type(v) is not int for v in (start_minute, end_minute)) or not 0 <= start_minute < end_minute <= 1440:
        raise ValueError("Working hours must be whole minutes, start before end, within one day")
    wall_time(day, timezone, start_minute)
    wall_time(day, timezone, end_minute)
    if not_before is not None:
        not_before = number(not_before, "not_before")
        lo, hi = day_bounds(day, timezone)
        if not lo <= not_before <= hi:
            raise ValueError("Running-late time must be within the selected day")
    return dict(start_minute=start_minute, end_minute=end_minute, not_before=not_before)


def validate_task(value):
    expected = {"id", "title", "day", "timezone", "kind", "duration_minutes", "priority", "due_ts", "pinned_start", "status", "revision"}
    if set(value) - expected:
        raise ValueError("Unknown task field")
    title = value.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValueError("Title must contain 1–500 characters")
    task_lo, task_hi = day_bounds(value.get("day"), value.get("timezone", "UTC"))
    if value.get("kind") not in {"study", "project", "task"}:
        raise ValueError("Kind must be study, project, or task")
    if type(value.get("duration_minutes")) is not int or not 1 <= value["duration_minutes"] <= 1440:
        raise ValueError("Duration estimate must be 1–1440 whole minutes")
    if type(value.get("priority")) is not int or value["priority"] not in range(1, 4):
        raise ValueError("Priority must be 1 (high), 2, or 3")
    if value.get("status") not in {"active", "done", "dismissed"}:
        raise ValueError("Invalid task status")
    result = {**value, "title": title.strip()}
    for key in ("due_ts", "pinned_start"):
        if result.get(key) is not None:
            result[key] = number(result[key], key)
            # Keep dates representable on every native client.
            if not 0 <= result[key] <= 253402214400:
                raise ValueError(f"{key} is outside the supported date range")
        else:
            result[key] = None
    if result.get("pinned_start") is not None and not task_lo <= result["pinned_start"] < task_hi:
        raise ValueError("Pinned start must be within the task day in its timezone; unpin before moving days")
    return result


def source_health(status, now, bounds):
    result = {}
    for source in ("calendar", "reminders"):
        info = status.get(source, {})
        stamp = info.get("last_sync")
        fresh = (isinstance(stamp, (int, float)) and math.isfinite(stamp)
                 and 0 <= now - stamp <= 180)
        ready = bool(info.get("available")) and not info.get("syncing") and fresh
        diagnostics = info.get("diagnostics") or {}
        coverage_start, coverage_end = diagnostics.get("coverage_start"), diagnostics.get("coverage_end")
        covered = (all(isinstance(v, (int, float)) and math.isfinite(v) for v in (coverage_start, coverage_end))
                   and coverage_start <= bounds[0] and coverage_end >= bounds[1])
        coverage_missing = source == "calendar" and ready and not covered
        if coverage_missing:
            ready = False
        result[source] = {"ready": ready, "last_sync": stamp if isinstance(stamp, (int, float)) and math.isfinite(stamp) else None,
                          "reason": "Up to date" if ready else (
                              "Selected day is outside confirmed calendar coverage" if coverage_missing else
                              "Sync is older than three minutes" if info.get("available") and not fresh
                              else str(info.get("reason") or "Waiting for the app to sync"))}
    return result


def build_plan(day, timezone, tasks, commitments, preferences, status, *, now):
    lo, hi = day_bounds(day, timezone)
    preferences = validate_preferences(day, timezone, **preferences)
    start = wall_time(day, timezone, preferences["start_minute"])
    end = wall_time(day, timezone, preferences["end_minute"])
    earliest = max(start, math.ceil(now / 60) * 60, preferences.get("not_before") or start)
    sources = source_health(status, now, (lo, hi))
    warnings, blocks, deadlines, all_day, unscheduled = [], [], [], [], []
    unknown_duration = False
    all_day_constraint = False
    old_unknown = False
    for c in commitments:
        when, until = c.get("when_ts"), c.get("end_ts")
        if when is None or not math.isfinite(when):
            continue
        if c["source"] == "calendar" or (c["source"] == "manual" and c.get("kind") in {"event", "meeting"}):
            valid_end = isinstance(until, (int, float)) and math.isfinite(until) and until >= when
            if not valid_end and when < lo:
                # An unknown old duration proves neither occupancy nor freedom
                # today. Keep suggestions explicitly provisional, but do not
                # invent an event that occupies every future day indefinitely.
                old_unknown = True
                continue
            relevant = when < hi and ((until > lo or (until == when and when >= lo)) if valid_end else (when >= lo or not c.get("all_day")))
            if not relevant:
                continue
            item = dict(id=c["id"], title=c["title"], start=when, end=until if valid_end else None,
                        kind=c["source"], task_id=None, warnings=[])
            if c.get("all_day"):
                all_day.append(item)
                all_day_constraint = all_day_constraint or not valid_end or until > when
            elif valid_end:
                blocks.append(item)
            else:
                unknown_duration = True
                item["warnings"].append("Fixed event has no end time")
                blocks.append(item)
        elif lo <= when < hi:
            deadlines.append(dict(id=c["id"], title=c["title"], due_ts=when, source=c["source"]))
    if not sources["calendar"]["ready"]:
        warnings.append("Calendar is not current. Flexible tasks are waiting for a successful sync.")
    if old_unknown:
        warnings.append("Older calendar events have unknown end times. Suggested slots are provisional; refresh Calendar and check for ongoing events.")
    if unknown_duration:
        warnings.append("Some fixed events have no end time. Flexible tasks are waiting for complete event times.")
    if all_day_constraint:
        warnings.append("All-day calendar events may reserve this day. Flexible tasks stay unscheduled; pin a task to explicitly choose time.")
    if not sources["reminders"]["ready"]:
        warnings.append("Reminders are not current; some deadlines may be missing.")
    active = [t for t in tasks if t["status"] == "active" and
              (t["day"] == day or (t.get("pinned_start") is not None and t["pinned_start"] < hi
                                  and t["pinned_start"] + t["duration_minutes"] * 60 > lo))]
    for t in active:
        if t.get("pinned_start") is None:
            continue
        s, duration = t["pinned_start"], t["duration_minutes"] * 60
        notes = []
        if s < lo:
            notes.append("Continues from the previous day")
        if all_day_constraint:
            notes.append("Pinned on a day with an all-day event; check availability")
        if s < start or s + duration > end:
            notes.append("Pinned outside working hours")
        if s < now:
            notes.append("Pinned time is in the past; mark done or move it")
        if t.get("due_ts") is not None and s + duration > t["due_ts"]:
            notes.append("Pinned block ends after its deadline")
        blocks.append(dict(id="task:" + t["id"], title=t["title"], start=s, end=s + duration,
                           kind=t["kind"], task_id=t["id"], warnings=notes))
    # Preserve conflicting pins and describe conflicts on both sides.
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if (a["end"] is not None and b["end"] is not None and a["end"] > a["start"]
                    and b["end"] > b["start"] and a["start"] < b["end"] and b["start"] < a["end"]):
                a["warnings"].append("Overlaps " + b["title"])
                b["warnings"].append("Overlaps " + a["title"])
    flexible = sorted((t for t in active if t.get("pinned_start") is None),
                      key=lambda t: (t["priority"], t.get("due_ts") or float("inf"), t["id"]))
    for t in flexible:
        reason, cursor = None, earliest
        if not sources["calendar"]["ready"] or unknown_duration or all_day_constraint:
            reason = ("All-day event needs an explicit time choice" if all_day_constraint else
                      "Waiting for a current calendar with complete event times")
        else:
            duration = t["duration_minutes"] * 60
            limit = min(end, t["due_ts"] if t.get("due_ts") is not None else end)
            for b in sorted(blocks, key=lambda b: (b["start"], b["id"])):
                if b["end"] is None or b["end"] <= b["start"] or b["end"] <= cursor:
                    continue
                if cursor + duration <= b["start"]:
                    break
                cursor = max(cursor, b["end"])
            if cursor + duration > limit:
                reason = "No uninterrupted time before the deadline or end of working hours"
        if reason:
            unscheduled.append(dict(task_id=t["id"], title=t["title"], reason=reason))
        else:
            blocks.append(dict(id="task:" + t["id"], task_id=t["id"], title=t["title"], kind=t["kind"],
                               start=cursor, end=cursor + t["duration_minutes"] * 60, warnings=[]))
    return dict(day=day, timezone=timezone, generated_at=now, preferences=preferences, sources=sources,
                provisional=bool(warnings), warnings=warnings, tasks=tasks,
                blocks=sorted(blocks, key=lambda b: (b["start"], b["id"])),
                deadlines=sorted(deadlines, key=lambda d: (d["due_ts"], d["id"])),
                all_day=all_day, unscheduled=unscheduled)
