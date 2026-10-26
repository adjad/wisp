"""Deterministic, local day planning. Imported commitments are never mutated."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math
import re
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


# Every key validate_preferences knows. A stored payload is read through this
# set (stored_preferences): a key written by a newer build (as plan_all_day was
# to builds before it) is ignored on read, so rolling back never breaks the
# plan. Writes stay strict: the API model forbids unknown fields.
PREFERENCE_KEYS = frozenset({"start_minute", "end_minute", "not_before", "plan_all_day"})


def stored_preferences(day, timezone, stored) -> dict:
    """Validate preferences read back from the store, ignoring unknown keys."""
    stored = stored if isinstance(stored, dict) else {}
    return validate_preferences(day, timezone, **{k: v for k, v in stored.items() if k in PREFERENCE_KEYS})


def validate_preferences(day, timezone, start_minute=540, end_minute=1080, not_before=None,
                         plan_all_day=False):
    """``plan_all_day``: the person chose to plan this day although an all-day
    event reads like time away (the Day tab's "Plan my day anyway")."""
    day_bounds(day, timezone)
    if type(plan_all_day) is not bool:
        raise ValueError("plan_all_day must be true or false")
    if any(type(v) is not int for v in (start_minute, end_minute)) or not 0 <= start_minute < end_minute <= 1440:
        raise ValueError("Working hours must be whole minutes, start before end, within one day")
    wall_time(day, timezone, start_minute)
    wall_time(day, timezone, end_minute)
    if not_before is not None:
        not_before = number(not_before, "not_before")
        lo, hi = day_bounds(day, timezone)
        if not lo <= not_before <= hi:
            raise ValueError("Running-late time must be within the selected day")
    result = dict(start_minute=start_minute, end_minute=end_minute, not_before=not_before)
    if plan_all_day:   # stored only when chosen, so every older payload reads the same
        result["plan_all_day"] = True
    return result


def validate_task(value):
    expected = {"id", "title", "day", "timezone", "kind", "duration_minutes", "priority", "due_ts", "pinned_start",
                "status", "revision", "carried_from", "origin_ts"}
    if set(value) - expected:
        raise ValueError("Unknown task field")
    # When this task first entered a plan, kept through every carry so a task
    # that keeps moving still ages from its real start (checkin.carry_forward).
    origin = value.get("origin_ts")
    if origin is not None and (type(origin) not in (int, float) or not 0 < float(origin) < 253402300800):
        raise ValueError("origin_ts must be a Unix timestamp")
    # The day this task was first planned for, when the evening check-in or the
    # morning carry-forward moved it (service/assistant/checkin.py). Absent or
    # None for a task still on its original day.
    carried = value.get("carried_from")
    if carried is not None:
        if not isinstance(carried, str):
            raise ValueError("carried_from must be an ISO date")
        try:
            if date.fromisoformat(carried).isoformat() != carried:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("carried_from must be an ISO date") from None
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


# An all-day event only reserves the day when it reads like time away. Course
# readings, assignment windows, birthdays and holidays you still work through are
# all-day markers, not occupancy, and must not stop the day from being planned.
_RESERVES_DAY = re.compile(
    r"\b(out of (the )?office|ooo|pto|vacation|day off|off work|sick|leave|away|"
    r"trip|travel|flight|fly(ing)?|retreat|offsite|off-site|conference|summit|"
    r"camp|wedding|moving|busy|blocked|unavailable|no meetings)\b", re.I)


def reserves_day(title: str) -> bool:
    return bool(_RESERVES_DAY.search(title or ""))


# A calendar of holidays or birthdays never holds the person's own time away,
# whatever its entries are called ("Moving Day" in a holiday feed, a contact's
# "Vacation" birthday note). Named by the calendar, like
# service/tools/assistant_tools._is_holiday_calendar_item.
_MARKER_CALENDAR = re.compile(r"\b(holidays?|birthdays?|feiertage|jours? f[eé]ri[eé]s)\b", re.I)


def marker_calendar(commitment: dict) -> bool:
    return any(_MARKER_CALENDAR.search(str(commitment.get(key) or ""))
               for key in ("context", "calendar", "calendar_name"))


# --- Can Wisp read the planner's sources? -------------------------------------
#
# The stored receipt says only whether the last read was usable. What the person
# can do about an unusable one comes from this launch's report by the app
# (scheduler.connectors_status): access turned off, a permission prompt nobody
# answered, or an app that hasn't reported at all. The Brief and Day say that,
# with the one setting that fixes it, instead of "hasn't synced yet" forever.

# How long a source may stay "syncing" (or unreported) after launch before the
# wait is reported as a problem: a real first read takes seconds
# (sync_status.SYNC_STALL_S).
ACCESS_STALL_S = 90.0

# What the readers can name as the reason a source can't be read
# (WS2 reason_kind). Anything else reads as a generic "unavailable".
ACCESS_KINDS = frozenset({"calendar_access", "reminders_access", "full_disk_access", "mail_automation",
                          "mail_not_configured", "mail_closed", "notes_closed", "no_accounts"})

_ACCESS_LABEL = {"calendar": "Calendar", "reminders": "Reminders"}
_ACCESS_PANE = {"calendar": "Calendars", "reminders": "Reminders"}

# EKAuthorizationStatus raw values the Calendar reader sends as "status".
_EK_NOT_DETERMINED, _EK_RESTRICTED, _EK_DENIED, _EK_WRITE_ONLY = 0, 1, 2, 4


def source_access(source: str, live: dict | None, *, now: float, started_at: float,
                  stall: float = ACCESS_STALL_S) -> dict:
    """``state`` ("ready", "syncing" or "unavailable"), ``blocked`` (what the
    person must grant, e.g. "calendar_access", or None) and ``reason`` (one
    plain sentence) for Calendar or Reminders, from this launch's reader report.

    ``live`` is scheduler.connectors_status()[source] or None when the app has
    not reported this source since the service started at ``started_at``."""
    label = _ACCESS_LABEL.get(source, source.title())
    pane = _ACCESS_PANE.get(source, label)
    settings = f"System Settings > Privacy & Security > {pane}"
    kind = f"{source}_access"
    if not live or not live.get("last_sync"):
        if now - started_at <= stall:
            return dict(state="syncing", blocked=None, reason=f"Reading your {label}…")
        return dict(state="unavailable", blocked="app_not_reporting",
                    reason=f"Wisp hasn't been able to read {label} yet. Quit and reopen Wisp if this doesn't clear.")
    diagnostics = live.get("diagnostics") or {}
    named = diagnostics.get("reason_kind")
    reported = str(diagnostics.get("reason") or "").strip()
    status = diagnostics.get("status")
    syncing = bool(live.get("syncing"))
    since = live.get("syncing_since") or live.get("last_sync")
    stalled = syncing and isinstance(since, (int, float)) and now - since > stall
    if live.get("available") and not syncing:
        return dict(state="ready", blocked=None, reason="Up to date")
    if named in ACCESS_KINDS:
        return dict(state="unavailable", blocked=named,
                    reason=reported or f"Wisp can't read your {label}. Turn on {label} for Wisp in {settings}.")
    if diagnostics.get("authorized") is False:
        if status in (_EK_RESTRICTED, _EK_DENIED, _EK_WRITE_ONLY):
            why = ("Wisp can only add to your Calendar, not read it." if status == _EK_WRITE_ONLY
                   else f"{label} access is off for Wisp.")
            return dict(state="unavailable", blocked=kind,
                        reason=f"{why} Turn on {label} for Wisp in {settings}.")
        if syncing and not stalled:
            return dict(state="syncing", blocked=None, reason=f"Waiting for you to allow {label}…")
        # Unanswered (status 0), or a reader that sent its own sentence.
        return dict(state="unavailable", blocked=kind,
                    reason=reported or (f"macOS hasn't recorded an answer about {label} for Wisp. "
                                        f"Allow it in {settings}; if Wisp isn't listed there, quit and reopen Wisp."))
    if syncing and not stalled:
        return dict(state="syncing", blocked=None, reason=f"Reading your {label}…")
    return dict(state="unavailable", blocked=None,
                reason=reported if reported and reported not in ("ok", "waiting for the app to sync")
                else f"Wisp couldn't read your {label} just now. It will try again in a minute.")


def with_access(sources: dict, live: dict, *, now: float, started_at: float) -> dict:
    """The planner's source health with ``state``, ``blocked`` and a plain
    ``reason`` for each source that isn't ready. A ready source is left as is."""
    out = {}
    for source, health in sources.items():
        health = dict(health)
        if not health.get("ready"):
            access = source_access(source, live.get(source), now=now, started_at=started_at)
            health["state"] = "syncing" if access["state"] == "ready" else access["state"]
            health["blocked"] = access["blocked"]
            if access["state"] != "ready":
                health["reason"] = access["reason"]
        else:
            health.update(state="ready", blocked=None)
        out[source] = health
    return out


def build_plan(day, timezone, tasks, commitments, preferences, status, *, now):
    lo, hi = day_bounds(day, timezone)
    preferences = stored_preferences(day, timezone, preferences)
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
                # Only time away reserves the day, never a holiday or birthday
                # feed's entry, and not once the person chose to plan it anyway.
                all_day_constraint = all_day_constraint or (
                    not preferences.get("plan_all_day") and not marker_calendar(c)
                    and reserves_day(c.get("title") or "") and (not valid_end or until > when))
            elif valid_end:
                blocks.append(item)
            else:
                unknown_duration = True
                item["warnings"].append("Fixed event has no end time")
                blocks.append(item)
        elif lo <= when < hi:
            deadlines.append(dict(id=c["id"], title=c["title"], due_ts=when, source=c["source"],
                                      **({'all_day': True, 'due_date': c.get('due_date')} if c.get('all_day') else {})))
    if not sources["calendar"]["ready"]:
        warnings.append("Calendar is not current. Flexible tasks are waiting for a successful sync.")
    if old_unknown:
        warnings.append("Older calendar events have unknown end times. Suggested slots are provisional; refresh Calendar and check for ongoing events.")
    if unknown_duration:
        warnings.append("Some fixed events have no end time. Flexible tasks are waiting for complete event times.")
    if all_day_constraint:
        warnings.append("All-day calendar events may reserve this day. Flexible tasks stay unscheduled; choose to plan the day anyway or pin a task.")
    if not sources["reminders"]["ready"]:
        warnings.append("Reminders are not current; some deadlines may be missing.")
    active = [t for t in tasks if t["status"] == "active" and
              (t["day"] == day or (t.get("pinned_start") is not None and t["pinned_start"] < hi
                                  and t["pinned_start"] + t["duration_minutes"] * 60 > lo))]
    for t in active:
        if t.get("pinned_start") is None:
            continue
        s, duration = t["pinned_start"], t["duration_minutes"] * 60
        # Keep the authored task editable, but show its pin only on days it occupies.
        if s >= hi or s + duration <= lo:
            continue
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
        reason, code, largest_gap, cursor = None, None, None, earliest
        if earliest >= end and start < now:
            # The working day is over: there is no gap to measure, and "the
            # longest free stretch is 0 minutes" would read as a full calendar.
            code = "after_hours"
            reason = "It's after your working hours"
        elif not sources["calendar"]["ready"] or unknown_duration or all_day_constraint:
            code = "all_day" if all_day_constraint else "source_incomplete"
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
                code = "no_contiguous_gap"
                gap_cursor, largest_gap = earliest, 0
                for occupied in sorted(blocks, key=lambda b: (b["start"], b["id"])):
                    if occupied["end"] is None or occupied["end"] <= occupied["start"]:
                        continue
                    largest_gap = max(largest_gap, max(0, min(limit, occupied["start"]) - gap_cursor))
                    gap_cursor = max(gap_cursor, occupied["end"])
                largest_gap = int(max(largest_gap, max(0, limit - gap_cursor)) // 60)
                reason = (f"Needs {t['duration_minutes']} uninterrupted minutes; largest available gap "
                          f"before the deadline or end of working hours is {largest_gap} minutes")
        if reason:
            unscheduled.append(dict(task_id=t["id"], title=t["title"], reason=reason, reason_code=code,
                                    required_minutes=t["duration_minutes"], largest_gap_minutes=largest_gap))
        else:
            blocks.append(dict(id="task:" + t["id"], task_id=t["id"], title=t["title"], kind=t["kind"],
                               start=cursor, end=cursor + t["duration_minutes"] * 60, warnings=[]))
    return dict(day=day, timezone=timezone, generated_at=now, preferences=preferences, sources=sources,
                provisional=bool(warnings), warnings=warnings, tasks=tasks,
                blocks=sorted(blocks, key=lambda b: (b["start"], b["id"])),
                deadlines=sorted(deadlines, key=lambda d: (d["due_ts"], d["id"])),
                all_day=all_day, unscheduled=unscheduled)
