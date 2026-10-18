"""The evening check-in, the morning carry-forward, and the weekly goal pulse.

A chief of staff does three short things every day: asks what got done, moves
what did not, and keeps the goal in view. None of it needs typing.

  * Evening (`build`): "You planned 4 things today." Each active task takes one
    tap: Done, Tomorrow or Drop (`decide_task`). Then tomorrow's events, each
    Keep, Move or Cancel; Move and Cancel only open Ask with the request, so the
    existing draft-and-confirm path decides what is sent.
  * Morning (`carry_forward`): anything still active from an earlier day moves
    onto today, marked `carried_from`, and the brief says how many came over.
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
import os
import threading
import time
from zoneinfo import ZoneInfo

from service.paths import MOE_DIR

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
                                      "priority", "due_ts", "day", "carried_from")}


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


def carry_forward(store, day: str, timezone: str, *, now: float) -> list[dict]:
    """Move every still-active task from an earlier day onto `day`.

    Idempotent: once moved, nothing earlier is active, so a second call (the
    scheduler and the Today tab both ask) moves nothing and the recorded list
    for the day is unchanged."""
    moved = []
    for task in store.today_active_tasks_before(day):
        values = {"day": day, "timezone": timezone, "pinned_start": None,
                  "carried_from": task.get("carried_from") or task["day"]}
        due = task.get("due_ts")
        if due is not None and due < now:
            values["due_ts"] = None
        try:
            moved.append(store.today_save_task(values, task_id=task["id"], revision=task["revision"]))
        except (ValueError, KeyError):
            continue  # changed under us or no longer valid; the next morning gets it
    if moved:
        def record(day_state: dict) -> None:
            known = {c["id"] for c in day_state.get("carried", [])}
            for task in moved:
                if task["id"] not in known:
                    day_state.setdefault("carried", []).append(
                        {"id": task["id"], "title": task["title"], "from": task.get("carried_from")})
            day_state["carried_at"] = now
        _update_day(day, record)
    return moved


def carried_today(day: str) -> list[dict]:
    return list(day_record(day).get("carried") or [])


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
