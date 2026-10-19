"""The evening check-in, the morning carry-forward, and the weekly goal pulse.

A chief of staff does three short things every day: asks what got done, moves
what did not, and keeps the goal in view. None of it needs typing.

  * Evening (`build`): "You planned 4 things today." Each active task takes one
    tap: Done, Tomorrow or Drop (`decide_task`). Then tomorrow's events, each
    Keep, Move or Cancel; Move and Cancel only open Ask with the request, so the
    existing draft-and-confirm path decides what is sent.
  * Morning (`carry_forward`): anything still active from an earlier day moves
    onto today, marked `carried_from`, and the brief says how many came over.
    Not forever: a task that first entered a plan a week ago (or came from a
    reminder that old) is left behind instead, the same rule the user set for
    stale reminders, so nothing rides along unexamined for weeks.
  * Weekly (`goal_pulse`): one goal, one tap: On track, Behind or Done. Behind
    makes it the goal the brief leads with; Done retires it from the rotation.

State that is not a task lives in ~/.moe/checkins.json (which days were
checked in, what was decided) and in the profile (goal statuses). Tasks are
mutated only through the store's own validated save, so the planner, the
window and the notch all see the same rows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
import math
import os
import threading
import time
from zoneinfo import ZoneInfo

from service.assistant.stale_reminders import ASK_AFTER_S, DAY_S, EXCLUDED_STATES, valid_created_ts
from service.paths import MOE_DIR

_log = logging.getLogger(__name__)

STATE_PATH = MOE_DIR / "checkins.json"
# The evening check-in becomes due at this local hour. Earlier, the day is not
# over; the user can still open it from Today, it just does not ask.
CHECKIN_HOUR = 17
# Only decisions a tap can make. Anything else is a 422, never a guess.
TASK_DECISIONS = ("done", "tomorrow", "drop")
GOAL_STATUSES = ("on_track", "behind", "done")
GOAL_PULSE_DAYS = 7
MAX_EVENTS = 8

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# State file


def _read_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        return {"days": {}}
    return raw


def _write_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, STATE_PATH)


def _day_state(state: dict, day: str) -> dict:
    return state["days"].setdefault(day, {"decisions": [], "completed_at": None,
                                          "carried": [], "carried_at": None, "notified_at": None})


def day_record(day: str) -> dict:
    with _lock:
        return dict(_day_state(_read_state(), day))


def _update_day(day: str, mutate) -> dict:
    with _lock:
        state = _read_state()
        record = _day_state(state, day)
        mutate(record)
        # Keep the file small: the last 60 days are plenty for "you've moved
        # this three evenings running".
        for old in sorted(state["days"])[:-60]:
            del state["days"][old]
        _write_state(state)
        return dict(record)


# ---------------------------------------------------------------------------
# Time helpers


def local_day(now: float, timezone: str) -> str:
    return datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def is_due(now: float, timezone: str, *, hour: int = CHECKIN_HOUR) -> bool:
    return datetime.fromtimestamp(now, ZoneInfo(timezone)).hour >= hour


# ---------------------------------------------------------------------------
# Evening


def _task_row(task: dict) -> dict:
    return {k: task.get(k) for k in ("id", "title", "status", "revision", "kind", "duration_minutes",
                                      "priority", "due_ts", "day", "carried_from", "origin_ts")}


def _event_rows(store, day: str, timezone: str) -> list[dict]:
    from service.assistant.today import day_bounds
    lo, hi = day_bounds(day, timezone)
    rows = []
    for event in store.calendar_events_overlapping(lo, hi):
        if event.get("status", "active") != "active":
            continue
        rows.append({"id": event.get("id"), "title": event.get("title") or "Untitled event",
                     "when_ts": event.get("when_ts"), "end_ts": event.get("end_ts"),
                     "all_day": bool(event.get("all_day")), "source_id": event.get("source_id")})
    rows.sort(key=lambda r: (r["when_ts"] is None, r["when_ts"] or 0))
    return rows[:MAX_EVENTS]


def build(store, day: str, timezone: str, *, now: float, profile: dict | None = None) -> dict:
    """Everything the check-in card shows for `day`. Read-only."""
    snapshot = store.today_snapshot(day, timezone)
    tasks = [t for t in snapshot["tasks"] if t.get("day") == day]
    active = [_task_row(t) for t in tasks if t.get("status") == "active"]
    done = [t for t in tasks if t.get("status") == "done"]
    record = day_record(day)
    tomorrow = next_day(day)
    try:
        events = _event_rows(store, tomorrow, timezone)
    except Exception:  # noqa: BLE001 — tomorrow's events are a courtesy, never a failure
        events = []
    return {
        "day": day, "timezone": timezone,
        "due": is_due(now, timezone) and local_day(now, timezone) == day,
        "completed": record.get("completed_at") is not None,
        "planned": len(tasks), "done": len(done),
        "tasks": active,
        "tomorrow": {"day": tomorrow, "events": events},
        "carried": list(record.get("carried") or []),
        "goal_pulse": goal_pulse(profile, now=now) if profile is not None else None,
    }


def decide_task(store, task_id: str, decision: str, *, day: str, timezone: str, now: float) -> dict:
    """Apply one tap to one task and record it. Returns the saved task."""
    if decision not in TASK_DECISIONS:
        raise ValueError("decision must be done, tomorrow or drop")
    task = store.today_task(task_id)
    if task is None:
        raise KeyError("Task no longer exists")
    values: dict = {}
    if decision == "done":
        values["status"] = "done"
    elif decision == "drop":
        values["status"] = "dismissed"
    else:
        from service.assistant.today import day_bounds
        tomorrow = next_day(day)
        lo, _ = day_bounds(tomorrow, timezone)
        values["day"] = tomorrow
        values["timezone"] = timezone
        values["pinned_start"] = None               # a pin belongs to the day it was made on
        values["carried_from"] = task.get("carried_from") or task.get("day") or day
        values["origin_ts"] = task_origin(task, timezone)
        due = task.get("due_ts")
        if due is not None and due < lo:
            values["due_ts"] = None                 # a deadline already missed is not tomorrow's
    saved = store.today_save_task(values, task_id=task_id, revision=task["revision"])

    def record(day_state: dict) -> None:
        day_state["decisions"].append({"task_id": task_id, "title": task.get("title", ""),
                                       "decision": decision, "at": now})
    _update_day(day, record)
    return saved


def complete(day: str, *, now: float) -> dict:
    return _update_day(day, lambda record: record.__setitem__("completed_at", now))


def mark_notified(day: str, *, now: float) -> dict:
    return _update_day(day, lambda record: record.__setitem__("notified_at", now))


def times_moved(title: str, *, before_day: str, days: int = 14) -> int:
    """How many recent evenings this title was moved to tomorrow: the signal
    behind "you've moved this three evenings running"."""
    key = title.strip().casefold()
    if not key:
        return 0
    with _lock:
        state = _read_state()
    count = 0
    for day, record in state["days"].items():
        if day >= before_day or (date.fromisoformat(before_day) - date.fromisoformat(day)).days > days:
            continue
        count += sum(1 for d in record.get("decisions", [])
                     if d.get("decision") == "tomorrow" and str(d.get("title", "")).strip().casefold() == key)
    return count


# ---------------------------------------------------------------------------
# Morning


CARRY_MAX_AGE_S = ASK_AFTER_S   # the user's rule for reminders, applied to carried tasks too


def task_origin(task: dict, timezone: str, reminder_origin: float | None = None) -> float:
    """When this task first entered a plan: its recorded origin, else the start
    of the first day it was planned for, and never later than the reminder it
    came from (matched by title), so a task made from a two-week-old reminder
    is two weeks old on day one."""
    from service.assistant.today import day_bounds
    candidates = []
    recorded = task.get("origin_ts")
    if type(recorded) in (int, float) and recorded > 0:
        candidates.append(float(recorded))
    try:
        candidates.append(day_bounds(task.get("carried_from") or task["day"], timezone)[0])
    except (ValueError, TypeError, KeyError):
        pass
    if reminder_origin is not None:
        candidates.append(float(reminder_origin))
    return min(candidates) if candidates else float("inf")


def _title_key(title: object) -> str:
    return " ".join(str(title or "").split()).casefold()


def reminder_index(store, now: float) -> dict[str, dict]:
    """Reminders by normalised title: the earliest time each one became due or
    was created, and whether the stale-reminder policy now excludes it
    (service/assistant/stale_reminders.py).

    Exclusion comes from the store's ``hidden_reminder_titles`` when it has one
    (undated and overdue dated reminders alike, normalised the same way as
    here); until then, from the undated rows' own stale state."""
    index: dict[str, dict] = {}

    def note(title, origin, excluded):
        key = _title_key(title)
        if not key:
            return
        entry = index.setdefault(key, {"origin": None, "excluded": False})
        if origin is not None and (entry["origin"] is None or origin < entry["origin"]):
            entry["origin"] = float(origin)
        entry["excluded"] = entry["excluded"] or bool(excluded)

    try:
        # Aging a task against its reminder, not showing the reminder: a reminder the
        # stale policy hides still supplies its due time, so its age reads true.
        for c in store.active_between(None, now, include_stale=True):  # dated, already due
            if c.get("source") in ("reminders", "manual") and c.get("kind") == "reminder":
                note(c.get("title"), c.get("when_ts"), False)
    except Exception:  # noqa: BLE001 — carry-forward must never fail on a read
        pass
    hidden = getattr(store, "hidden_reminder_titles", None)
    if callable(hidden):
        try:
            for title in hidden(now):
                note(title, None, True)
        except Exception:  # noqa: BLE001
            pass
    rows = getattr(store, "_undated_rows", None)
    if callable(rows):
        try:
            for r in rows(now):
                state = r.get("stale_state")
                # A reminder the user chose to keep is wanted at any age, so it
                # does not age a task of the same name.
                note(r.get("title"), None if state == "kept" else valid_created_ts(r.get("created_ts")),
                     state in EXCLUDED_STATES)
        except Exception:  # noqa: BLE001
            pass
    return index


def carry_forward(store, day: str, timezone: str, *, now: float) -> list[dict]:
    """Move every still-active task from an earlier day onto `day`, unless it
    has aged out.

    A task is left where it is, and so drops out of today's plan, when it
    first entered a plan a week or more ago (counting a source reminder's due
    or creation time, matched by title), or when its title matches an undated
    reminder the stale policy has stopped using. Nothing is deleted; the task
    stays on its last day, and the day record lists what aged out.

    Idempotent: once moved, nothing earlier is active, so a second call (the
    scheduler and the Today tab both ask) moves nothing and the recorded list
    for the day is unchanged."""
    moved, aged = [], []
    index = None
    for task in store.today_active_tasks_before(day):
        if index is None:
            index = reminder_index(store, now)
        match = index.get(_title_key(task.get("title")))
        origin = task_origin(task, timezone, match["origin"] if match else None)
        if match and match["excluded"]:
            aged.append((task, "stale_reminder", origin))
            continue
        if now - origin >= CARRY_MAX_AGE_S:
            aged.append((task, "aged_out", origin))
            continue
        values = {"day": day, "timezone": timezone, "pinned_start": None,
                  "carried_from": task.get("carried_from") or task["day"], "origin_ts": origin}
        due = task.get("due_ts")
        if due is not None and due < now:
            values["due_ts"] = None
        try:
            moved.append(store.today_save_task(values, task_id=task["id"], revision=task["revision"]))
        except (ValueError, KeyError):
            continue  # changed under us or no longer valid; the next morning gets it
    if moved or aged:
        def record(day_state: dict) -> None:
            known = {c["id"] for c in day_state.get("carried", [])}
            for task in moved:
                if task["id"] not in known:
                    day_state.setdefault("carried", []).append(
                        {"id": task["id"], "title": task["title"], "from": task.get("carried_from")})
            known = {c["id"] for c in day_state.get("aged_out", [])}
            for task, reason, origin in aged:
                if task["id"] not in known:
                    day_state.setdefault("aged_out", []).append(
                        {"id": task["id"], "title": task["title"], "from": task.get("carried_from") or task["day"],
                         "reason": reason, "age_days": int(max(0.0, now - origin) // DAY_S)})
            day_state["carried_at"] = now
        _update_day(day, record)
    return moved


def aged_out_today(day: str) -> list[dict]:
    """Tasks the morning carry-forward left behind for `day`, with why."""
    return list(day_record(day).get("aged_out") or [])


def carried_today(day: str) -> list[dict]:
    return list(day_record(day).get("carried") or [])


# ---------------------------------------------------------------------------
# Moving an event: where else it could go


SUGGESTION_GRID_S = 15 * 60       # candidate starts sit on quarter hours
SUGGESTION_DAYS = 3               # the event's day and the two after it
SUGGESTION_LIMIT = 3


def _weekday(ts: float, timezone: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(timezone)).strftime("%A")


def _clock(ts: float, timezone: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(timezone)).strftime("%-I:%M %p")


def _ceil_grid(ts: float) -> float:
    return float(math.ceil(ts / SUGGESTION_GRID_S) * SUGGESTION_GRID_S)


def find_event(store, event_id: str, day: str, timezone: str) -> dict | None:
    """The calendar event the check-in showed, by id or source id, on `day`."""
    from service.assistant.today import day_bounds
    lo, hi = day_bounds(day, timezone)
    for event in store.calendar_events_overlapping(lo, hi):
        if event.get("status", "active") != "active":
            continue
        if event_id in (event.get("id"), event.get("source_id")):
            return event
    return None


def move_suggestions(store, event: dict, timezone: str, *, now: float, behavior=None,
                     days: int = SUGGESTION_DAYS, limit: int = SUGGESTION_LIMIT) -> dict:
    """Up to `limit` other times for `event`, found the way the Day tab plans:
    free time inside working hours on the event's day and the next `days - 1`
    days, around calendar events, planned tasks and the learned "likely busy"
    and "possible time away" windows (service/assistant/daily_workspace.py).
    Deterministic, read-only, no model call; the agent still confirms any move.

    `behavior` is an optional callable(day) -> availability snapshot, the same
    public contract the Day tab's workspace uses."""
    from service.assistant import daily_workspace as daily
    from service.assistant.today import day_bounds, wall_time
    start = event.get("when_ts")
    if start is None or not math.isfinite(start):
        return {"event": _event_summary(event, timezone), "suggestions": [], "reason": "This event has no start time to move from."}
    end = event.get("end_ts")
    duration = (end - start) if isinstance(end, (int, float)) and math.isfinite(end) and end > start else 3600.0
    event_day = local_day(start, timezone)
    start_local = datetime.fromtimestamp(start, ZoneInfo(timezone))
    start_minute = start_local.hour * 60 + start_local.minute
    suggestions: list[dict] = []
    seen: set[float] = set()

    def add(slot_start: float, reason: str) -> None:
        if slot_start in seen or len(suggestions) >= limit:
            return
        seen.add(slot_start)
        suggestions.append({"start_ts": slot_start, "end_ts": slot_start + duration,
                            "day": local_day(slot_start, timezone), "reason": reason,
                            "label": _weekday(slot_start, timezone) + " " + _clock(slot_start, timezone)})

    for offset in range(max(1, days)):
        day = (date.fromisoformat(event_day) + timedelta(days=offset)).isoformat()
        lo, hi = day_bounds(day, timezone)
        try:
            plan = daily.workspace(store, day, timezone, now=now,
                                   behavior=behavior(day) if behavior else None)["proposed_plan"]
        except Exception as exc:  # noqa: BLE001 — the saved plan still answers when a source is away
            _log.info("move_suggestions: workspace unavailable for %s (%s); using the saved plan", day, exc)
            from service.assistant.today import build_plan
            state = store.today_snapshot(day, timezone)
            plan = build_plan(day, timezone, state["tasks"], state["commitments"], state["preferences"],
                              state["sources"], now=now)
        prefs = plan["preferences"]
        # Wall-clock minutes, not seconds from midnight: a DST day is 23 or 25 hours long.
        try:
            work_lo = wall_time(day, timezone, prefs["start_minute"])
            work_hi = wall_time(day, timezone, prefs["end_minute"])
        except ValueError:                     # working hours fall in the DST gap: skip the day
            continue
        try:
            same_time = wall_time(day, timezone, start_minute) if offset else None
        except ValueError:                     # the event's own minute is ambiguous that day
            same_time = None
        occupied = []
        for block in plan["blocks"]:
            if block.get("id") == event.get("id") or (block.get("start") == start and block.get("title") == event.get("title")):
                continue                      # the event being moved does not block itself
            b_start = block.get("start")
            if b_start is None or not math.isfinite(b_start):
                continue
            b_end = block.get("end")
            if b_end is None or not math.isfinite(b_end) or b_end <= b_start:
                b_end = b_start + 3600
            occupied.append((max(lo, b_start), min(hi, b_end)))
        earliest = max(work_lo, _ceil_grid(now + 5 * 60))
        free = [(a, b) for a, b in daily._remaining(earliest, work_hi, occupied) if b - a >= duration]

        def fits(candidate: float) -> bool:
            return any(a <= candidate and candidate + duration <= b for a, b in free)

        def first_after(point: float) -> float | None:
            for a, b in free:
                c = _ceil_grid(max(a, point))
                if c + duration <= b:
                    return c
            return None

        weekday = _weekday(lo, timezone)
        if offset == 0:
            later = first_after(start + duration)
            if later is not None:
                before = [b for b in plan["blocks"] if finite_end(b) and finite_end(b) <= later]
                before.sort(key=finite_end)
                reason = (f"Later that day, after {before[-1]['title']}" if before and later - finite_end(before[-1]) < 3600
                          else "Later that day")
                add(later, reason)
        else:
            if same_time is not None and fits(same_time):
                add(same_time, f"Same time {weekday}")
            first = first_after(work_lo)
            if first is not None:
                add(first, f"First free slot {weekday}")
        if len(suggestions) >= limit:
            break
    return {"event": _event_summary(event, timezone), "duration_minutes": int(duration // 60),
            "suggestions": suggestions[:limit]}


def finite_end(block: dict) -> float | None:
    end = block.get("end")
    return end if isinstance(end, (int, float)) and math.isfinite(end) else None


def _event_summary(event: dict, timezone: str) -> dict:
    start = event.get("when_ts")
    return {"id": event.get("id"), "source_id": event.get("source_id"), "title": event.get("title") or "Untitled event",
            "when_ts": start, "end_ts": event.get("end_ts"),
            "label": (_weekday(start, timezone) + " " + _clock(start, timezone)) if isinstance(start, (int, float)) and math.isfinite(start) else None}


# ---------------------------------------------------------------------------
# Weekly goal pulse


def goal_pulse(profile: dict | None, *, now: float) -> dict | None:
    """The one goal to ask about this week, or None when nothing is due.

    Due once every GOAL_PULSE_DAYS after the last answer (or after the goals
    were written). Goals marked done are skipped; a goal marked behind comes
    first, since it is the one worth checking on."""
    if not profile:
        return None
    goals = [g for g in profile.get("goals") or []]
    statuses = profile.get("goal_status") or {}
    open_goals = [g for g in goals if (statuses.get(g) or {}).get("status") != "done"]
    if not open_goals:
        return None
    last = profile.get("goal_pulse_at") or profile.get("updated_at")
    if last is not None and now - float(last) < GOAL_PULSE_DAYS * 86400:
        return None
    behind = [g for g in open_goals if (statuses.get(g) or {}).get("status") == "behind"]
    # Rotate through the open goals by week so each gets asked about.
    week = int(now // (7 * 86400))
    goal = behind[0] if behind else open_goals[week % len(open_goals)]
    return {"goal": goal, "status": (statuses.get(goal) or {}).get("status"),
            "options": list(GOAL_STATUSES)}
