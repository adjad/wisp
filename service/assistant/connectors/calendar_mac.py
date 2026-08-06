"""macOS Calendar connector via EventKit (PyObjC).

Reads the next N days of events from every calendar the user has in Calendar.app
— including subscribed Google/iCloud accounts, so a Canvas feed synced into
Google Calendar shows up here for free (no separate Canvas connector needed).

TCC: reading events needs the Calendar permission, attributed to whatever process
is responsible for this one. When the backend runs as a child of Wisp.app (the
normal case) the prompt says "Wisp" and the app's Info.plist carries the usage
strings. Until it's granted, `available()` reports the reason and poll() returns
[] — the rest of the assistant keeps working on manual commitments.

Everything here is defensive: EventKit calls are wrapped so a permission change
or a malformed event can never take the scheduler down.
"""
from __future__ import annotations

import asyncio

try:
    import EventKit as _EK
    from Foundation import NSDate
    _IMPORT_ERR = ""
except Exception as e:  # noqa: BLE001 — PyObjC/EventKit missing shouldn't crash import
    _EK = None
    NSDate = None
    _IMPORT_ERR = str(e)

# Title heuristics: tag Canvas-style academic items so reminders can treat an
# exam more urgently than a lecture. Deterministic, cheap, no LLM.
_EXAM_WORDS = ("exam", "midterm", "final", "quiz", "test")
_ASSIGN_WORDS = ("due", "assignment", "homework", "hw", "problem set", "pset",
                 "lab", "project", "essay", "paper", "submission", "deadline")


def _classify(title: str, calendar_name: str) -> str:
    t = (title or "").lower()
    if any(w in t for w in _EXAM_WORDS):
        return "exam"
    if any(w in t for w in _ASSIGN_WORDS):
        return "assignment"
    return "event"


class CalendarConnector:
    name = "calendar"

    def __init__(self, horizon_days: int = 14, interval_s: float = 300.0) -> None:
        self.horizon_days = horizon_days
        self.interval_s = interval_s
        self._store = None
        self._reason = ""
        if _EK is not None:
            try:
                self._store = _EK.EKEventStore.alloc().init()
            except Exception as e:  # noqa: BLE001
                self._reason = f"EventKit init failed: {e}"

    # --- permission -------------------------------------------------------
    def _auth_ok(self) -> bool:
        if _EK is None:
            return False
        status = _EK.EKEventStore.authorizationStatusForEntityType_(_EK.EKEntityTypeEvent)
        # 3 = authorized (pre-14), 4 = fullAccess (Sonoma+)
        return status in (3, 4)

    def request_access(self) -> None:
        """Best-effort access request. The real prompt is expected to come from
        the Swift side (clean Wisp.app identity); this covers the dev path."""
        if self._store is None:
            return
        try:
            if hasattr(self._store, "requestFullAccessToEventsWithCompletion_"):
                self._store.requestFullAccessToEventsWithCompletion_(lambda g, e: None)
            else:
                self._store.requestAccessToEntityType_completion_(
                    _EK.EKEntityTypeEvent, lambda g, e: None)
        except Exception:  # noqa: BLE001
            pass

    def available(self) -> tuple[bool, str]:
        if _EK is None:
            return False, f"EventKit unavailable ({_IMPORT_ERR or 'not installed'})"
        if self._store is None:
            return False, self._reason or "EventKit store not initialized"
        if not self._auth_ok():
            return False, "Calendar access not granted (System Settings ▸ Privacy ▸ Calendars ▸ Wisp)"
        return True, "ok"

    # --- poll -------------------------------------------------------------
    async def poll(self) -> list[dict]:
        ok, _ = self.available()
        if not ok:
            return []
        # EventKit is synchronous; run it off the event loop.
        return await asyncio.to_thread(self._read_events)

    def _read_events(self) -> list[dict]:
        try:
            start = NSDate.date()
            end = NSDate.dateWithTimeIntervalSinceNow_(self.horizon_days * 86400)
            pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(
                start, end, None)
            events = self._store.eventsMatchingPredicate_(pred) or []
        except Exception:  # noqa: BLE001 — never take down the scheduler
            return []

        out: list[dict] = []
        for ev in events:
            try:
                sd = ev.startDate()
                if sd is None:
                    continue
                when_ts = sd.timeIntervalSince1970()
                title = str(ev.title() or "(untitled)")
                cal = ev.calendar()
                cal_name = str(cal.title()) if cal else ""
                kind = "meeting" if (not ev.isAllDay() and _classify(title, cal_name) == "event") \
                    else _classify(title, cal_name)
                # EKEvent.eventIdentifier is stable across polls for the same event
                sid = str(ev.eventIdentifier() or f"{title}-{when_ts}")
                loc = ev.location()
                out.append({
                    "source_id": sid,
                    "kind": kind,
                    "title": title,
                    "context": cal_name,
                    "when_ts": when_ts,
                    "all_day": bool(ev.isAllDay()),
                    "location": str(loc) if loc else None,
                    "url": None,
                    "confidence": 1.0,
                })
            except Exception:  # noqa: BLE001 — skip a bad event, keep the rest
                continue
        return out
