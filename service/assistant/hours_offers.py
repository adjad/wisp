"""The offer to move an event that falls outside working hours.

"“Piano recital prep” is at 7:30 PM, after your working hours. Thursday at
9:00 AM is free." It shows on the Day tab. Nothing here changes a calendar:
accepting hands the existing request to Ask, whose confirmation card and verified
native move (loop.bind_event_move, CalendarMove.swift) are what can move anything.

Wisp sees all of someone's evenings, and most of them are nobody's mistake, so
this errs hard towards saying nothing. An event is only offered when it is:

- a one-off: repeating events are skipped, because a class or a standing practice
  is at that time on purpose;
- theirs alone: no invitees, not organized by someone else, on a calendar that
  accepts changes. An unknown count or calendar counts as no;
- timed, under MAX_EVENT_HOURS long, and entirely outside working hours on a
  working day (a Saturday is not "outside working hours" to anyone);
- worth a move: not a meal, workout or night out (PERSONAL_WORDS), starts at
  least MIN_NOTICE_S from now and within LOOKAHEAD_DAYS, with a real free slot
  inside working hours to name.

And then rarely: at most DAILY_CAP events a local day, one offer per event ever
(a dismissal or an ignored offer is final), never during quiet hours, and
DISMISS_PAUSE_AFTER dismissals in a row pause offers for DISMISS_PAUSE_DAYS.
Turned off entirely with user_hours.save(offer_moves=False).

State is $WISP_HOME/hours_offers.json, owner-only. No model is called.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from service import user_hours
from service.paths import moe_dir, private_write_bytes

_log = logging.getLogger(__name__)

DAILY_CAP = 1
LOOKAHEAD_DAYS = 3              # today and the next two days
SLOT_SEARCH_DAYS = 6            # Friday night's next working slot is on Monday
MIN_NOTICE_S = 2 * 3600
MIN_SLOT_NOTICE_S = 30 * 60
MAX_EVENT_HOURS = 4
MAX_CANDIDATES = 3              # each one plans several days, so try only a few per call
DISMISS_PAUSE_AFTER = 3
DISMISS_PAUSE_DAYS = 30
KEEP_DAYS = 120
_REPEAT_WINDOW_DAYS = 28

# Life, not work: moving these into the workday is never what anyone wants.
PERSONAL_WORDS = re.compile(
    r"\b(?:breakfast|brunch|lunch|dinner|supper|drinks?|coffee|party|birthday|anniversary|wedding|date night|"
    r"gym|workout|yoga|pilates|run|running|hike|swim|climbing|tennis|soccer|football|basketball|game|"
    r"movie|concert|show|theater|theatre|flight|train|church|temple|mosque|service|family|kids?)\b", re.I)

_lock = threading.RLock()


def path() -> Path:
    return moe_dir() / "hours_offers.json"


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    private_write_bytes(path(), json.dumps(state, indent=1, sort_keys=True, allow_nan=False).encode("utf-8"))


def _events(state: dict) -> dict:
    events = state.get("events")
    return events if isinstance(events, dict) else {}


def offer_id(source_id: str) -> str:
    return hashlib.sha256(str(source_id).encode("utf-8")).hexdigest()[:24]


def _minute(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def skip_reason(event: dict, work: user_hours.Hours, zone: ZoneInfo, *, now: float) -> str | None:
    """Why this event gets no offer, or None. The rules from the top of this file,
    apart from the free slot and the limits on how often."""
    if event.get("all_day"):
        return "all-day"
    if event.get("calendar_writable") != 1:
        return "calendar is read-only or unknown"
    if event.get("invitee_count") != 0:
        return "has invitees, or unknown"
    # Not "unknown counts as no": with no invitees EventKit usually names no organizer at
    # all, so that's what the person's own events look like (CalendarReader leaves it out).
    if event.get("organizer_is_user") == 0:
        return "organized by someone else"
    if (event.get("occurrences") or 0) > 1:
        return "repeats"
    start, end = event.get("when_ts"), event.get("end_ts")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return "no clear time"
    if end - start > MAX_EVENT_HOURS * 3600:
        return "too long"
    if start < now + MIN_NOTICE_S:
        return "too soon"
    if PERSONAL_WORDS.search(str(event.get("title") or "")):
        return "personal"
    first = datetime.fromtimestamp(start, zone)
    last = datetime.fromtimestamp(end - 60, zone)
    if first.weekday() not in work.days and not (work.wraps and work.contains_time(first)):
        return "not a working day"
    if first.date() != last.date():
        return "crosses midnight"
    if work.contains_time(first) or work.contains_time(last):
        return "inside working hours"
    # Starts before the workday and ends after it: the event covers the whole day.
    span_start, span_end = work.day_span()
    if not work.wraps and _minute(first) < span_start and _minute(last) >= span_end:
        return "spans the working day"
    return None


def _repeats_by_title(event: dict, rows: list[dict], zone: ZoneInfo) -> bool:
    """A class imported as separate events still repeats: the same title at the same
    time of day on another date."""
    title = " ".join(str(event.get("title") or "").split()).casefold()
    start = datetime.fromtimestamp(event["when_ts"], zone)
    for row in rows:
        if row.get("source_id") == event.get("source_id") or not isinstance(row.get("when_ts"), (int, float)):
            continue
        if " ".join(str(row.get("title") or "").split()).casefold() != title:
            continue
        other = datetime.fromtimestamp(row["when_ts"], zone)
        if other.date() != start.date() and _minute(other) == _minute(start):
            return True
    return False


def _slot_inside(slot: dict, work: user_hours.Hours, zone: ZoneInfo, *, now: float) -> bool:
    """The planner's suggestions honor a day's own hours, which "Plan tonight" can
    stretch; the offer promises a time inside the person's usual ones."""
    start, end = slot.get("start_ts"), slot.get("end_ts")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return False
    if start < now + MIN_SLOT_NOTICE_S:
        return False
    return work.contains_time(datetime.fromtimestamp(start, zone)) and work.contains_time(
        datetime.fromtimestamp(end - 60, zone))


def _first_slot(store, event: dict, work: user_hours.Hours, timezone: str, zone: ZoneInfo, *, now: float, behavior) -> dict | None:
    from service.assistant import checkin
    found = checkin.move_suggestions(store, event, timezone, now=now, behavior=behavior,
                                     days=SLOT_SEARCH_DAYS, limit=SLOT_SEARCH_DAYS * 2)
    return next((s for s in found.get("suggestions", []) if _slot_inside(s, work, zone, now=now)), None)


def _present(event: dict, slot: dict, work: user_hours.Hours, zone: ZoneInfo) -> dict:
    start = datetime.fromtimestamp(event["when_ts"], zone)
    before = not work.wraps and _minute(start) < work.start_minute
    flat = " ".join(str(event.get("title") or "Untitled event").split())
    return {"id": offer_id(event["source_id"]), "event_id": event.get("id"), "source_id": event["source_id"],
            "title": flat, "when_ts": event["when_ts"], "end_ts": event["end_ts"],
            "duration_minutes": max(1, round((event["end_ts"] - event["when_ts"]) / 60)),
            "side": "before" if before else "after",
            "slot": {"start_ts": slot["start_ts"], "label": slot.get("label") or "", "reason": slot.get("reason") or ""}}


def next_offer(store, timezone: str, *, now: float, behavior=None) -> dict | None:
    """The one offer to show now, or None, recording it as shown. Safe to call on
    every refresh: an offer already shown today comes back, with a slot that is
    still free, and never counts twice. Writes only this module's own state."""
    zone = ZoneInfo(timezone)
    moment = datetime.fromtimestamp(now, zone)
    if not user_hours.offers_moves() or user_hours.in_quiet_hours(moment):
        return None
    work = user_hours.working_hours()
    today = moment.date().isoformat()
    from service.assistant.today import day_bounds
    with _lock:
        state = _load()
        paused = state.get("paused_until")
        if isinstance(paused, (int, float)) and now < paused:
            return None
        seen = _events(state)
        shown_today = {key for key, row in seen.items() if isinstance(row, dict) and row.get("day") == today}
        lo, _ = day_bounds(today, timezone)
        hi = day_bounds((moment.date() + timedelta(days=LOOKAHEAD_DAYS - 1)).isoformat(), timezone)[1]
        window = _REPEAT_WINDOW_DAYS * 86400
        rows = store.calendar_offer_rows(lo - window, hi + window)
        tries = 0
        for event in (r for r in rows if lo <= r["when_ts"] < hi):
            key = offer_id(event["source_id"])
            row = seen.get(key)
            if isinstance(row, dict) and (row.get("state") != "shown" or row.get("day") != today):
                continue                     # one offer per event, whatever came of it
            if row is None and len(shown_today) >= DAILY_CAP:
                continue
            if skip_reason(event, work, zone, now=now) or _repeats_by_title(event, rows, zone):
                continue
            if tries >= MAX_CANDIDATES:
                break
            tries += 1
            try:
                slot = _first_slot(store, event, work, timezone, zone, now=now, behavior=behavior)
            except Exception as exc:         # noqa: BLE001 - no slot found is no offer, never an error
                _log.info("hours_offers: no slot for an event (%s)", type(exc).__name__)
                slot = None
            if slot is None:
                continue
            if row is None:
                seen[key] = {"state": "shown", "day": today, "at": now}
                state["events"] = _prune(seen, now)
                _save(state)
            return _present(event, slot, work, zone)
    return None


def _prune(events: dict, now: float) -> dict:
    cutoff = now - KEEP_DAYS * 86400
    return {k: v for k, v in events.items()
            if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and v["at"] >= cutoff}


def _finish(id_: str, outcome: str, *, now: float) -> bool:
    with _lock:
        state = _load()
        events = _events(state)
        row = events.get(id_)
        if not isinstance(row, dict):
            return False
        events[id_] = {**row, "state": outcome, "at": now}
        state["events"] = events
        if outcome == "dismissed":
            streak = state.get("dismiss_streak")
            streak = (streak if type(streak) is int and streak >= 0 else 0) + 1
            if streak >= DISMISS_PAUSE_AFTER:
                state["paused_until"] = now + DISMISS_PAUSE_DAYS * 86400
                streak = 0
            state["dismiss_streak"] = streak
        else:
            state["dismiss_streak"] = 0
        _save(state)
        return True


def dismiss(id_: str, *, now: float) -> bool:
    """Keep it where it is: this event is never offered again."""
    return _finish(id_, "dismissed", now=now)


def accept(id_: str, *, now: float) -> bool:
    """The person took the offer to Ask, where the move is confirmed or isn't.
    Recorded so it isn't offered again; the calendar is untouched here."""
    return _finish(id_, "accepted", now=now)
