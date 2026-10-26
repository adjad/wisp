"""Reminder alerts expire, never burst on a first Calendar sync, and never repeat
an alert something else already gave (a routine nudge, a native Reminders alarm).
"""
from __future__ import annotations

import json
import time

import pytest

from service.assistant import reminders
from service.assistant.behavior_runtime import BEHAVIOR_CONTEXT
from service.assistant.reminders import REMINDER_EXPIRY_S, due_reminders
from service.assistant.store import CALENDAR_BASELINE_KEY, FIRST_LAUNCH_KEY, AssistantStore


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path / "assistant.db")
    yield s
    s._db.close()


def _upgrade(store) -> None:
    """Make `store` look like an install that predates the first-launch marker."""
    store.record_completion(FIRST_LAUNCH_KEY, "0")


# --- expiry -------------------------------------------------------------------

def test_reminder_event_carries_its_expiry(store):
    _upgrade(store)
    now = time.time()
    store.add_manual("Fixture standup", now + 300, kind="meeting")
    [note] = due_reminders(store, now)
    row = store.event(note["event_id"])
    assert note["expires_at"] == pytest.approx(now + 300 + REMINDER_EXPIRY_S)
    assert row["expires_at"] == pytest.approx(note["expires_at"])


def test_expired_reminder_is_superseded_before_replay(store, monkeypatch):
    _upgrade(store)
    now = time.time()
    store.add_manual("Fixture standup", now + 300, kind="meeting")
    [note] = due_reminders(store, now)
    assert [e["id"] for e in store.pending_events()] == [note["event_id"]]
    # The app was away until after the meeting: nothing to replay, nothing to attempt.
    later = now + 300 + REMINDER_EXPIRY_S + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert store.pending_events() == []
    assert store.event(note["event_id"])["state"] == "superseded"
    assert store.event_attempt(note["event_id"]) is False
    # A late receipt is a harmless no-op, and nothing re-queues for that stage.
    assert store.acknowledge_event(note["event_id"], "reminder") is True
    assert due_reminders(store, later) == []


def test_reminder_queued_without_expiry_expires_from_its_event_time(store, monkeypatch):
    """Rows queued by the previous version have expires_at NULL."""
    _upgrade(store)
    now = time.time()
    store.add_manual("Fixture standup", now + 300, kind="meeting")
    [note] = due_reminders(store, now)
    with store._db:
        store._db.execute("UPDATE assistant_events SET expires_at=NULL WHERE id=?", (note["event_id"],))
    assert [e["id"] for e in store.pending_events()] == [note["event_id"]]
    monkeypatch.setattr(time, "time", lambda: now + 300 + REMINDER_EXPIRY_S + 1)
    assert store.pending_events() == []


def test_delivery_attempt_checks_only_its_own_reminder(store, monkeypatch):
    _upgrade(store)
    now = time.time()
    store.add_manual("First fixture", now + 300, kind="meeting")
    store.add_manual("Second fixture", now + 900, kind="meeting")
    first, second = sorted(due_reminders(store, now), key=lambda n: n["when_ts"])
    monkeypatch.setattr(time, "time", lambda: now + 300 + REMINDER_EXPIRY_S + 1)
    # Attempting the live one leaves the other row for the next replay to retire.
    assert store.event_attempt(second["event_id"]) is True
    assert store.event(first["event_id"])["state"] == "pending"
    assert [e["id"] for e in store.pending_events()] == [second["event_id"]]


# --- first Calendar sync --------------------------------------------------------

def _calendar(store, now, **items):
    store.sync_source("calendar", [
        {"source_id": sid, "title": title, "kind": kind, "when_ts": now + offset}
        for sid, (title, kind, offset) in items.items()],
        diagnostics={"authorized": True, "snapshot_started_at": time.time()})


def test_first_calendar_sync_does_not_burst(store, monkeypatch):
    now = time.time()
    _calendar(store, now,
              exam=("Fixture exam", "exam", 3 * 86400),          # T-1w already passed
              soon=("Fixture sync", "meeting", 8 * 60),          # T-30m and T-10m passed
              later=("Fixture review", "meeting", 2 * 3600))     # nothing passed yet
    assert due_reminders(store, now) == []
    assert due_reminders(store, now + 60) == []
    # Stages that come due after the sync still fire on time.
    [review] = due_reminders(store, now + 2 * 3600 - 1700)
    assert review["title"] == "Fixture review" and review["stage"] == "T-30m"
    [exam] = due_reminders(store, now + 2 * 86400 + 60)
    assert exam["title"] == "Fixture exam" and exam["stage"] == "T-1d"


def test_baseline_runs_once(store):
    now = time.time()
    _calendar(store, now, a=("Fixture sync", "meeting", 3600))
    assert store.completion(CALENDAR_BASELINE_KEY) is not None
    _calendar(store, now, a=("Fixture sync", "meeting", 3600), b=("Fixture call", "meeting", 20 * 60))
    [call] = due_reminders(store, now)
    assert call["title"] == "Fixture call" and call["stage"] == "T-30m"


def test_upgraded_install_is_not_baselined(store):
    _upgrade(store)
    now = time.time()
    _calendar(store, now, soon=("Fixture sync", "meeting", 8 * 60))
    [note] = due_reminders(store, now)
    assert note["stage"] == "T-10m"
    assert store.completion(CALENDAR_BASELINE_KEY) is not None


def test_unavailable_calendar_read_waits_for_real_data(store):
    now = time.time()
    store.today_source_unavailable("calendar", {"authorized": False, "syncing": True,
                                                "snapshot_started_at": now - 10})
    assert store.completion(CALENDAR_BASELINE_KEY) is None
    _calendar(store, now, soon=("Fixture sync", "meeting", 8 * 60))
    assert due_reminders(store, now) == []


def test_store_opened_on_existing_database_is_an_upgrade(tmp_path):
    """A database written before this marker existed is never treated as day one."""
    path = tmp_path / "legacy.db"
    first = AssistantStore(path)
    now = time.time()
    first.add_manual("Fixture standup", now + 300, kind="meeting")
    [note] = due_reminders(first, now)
    with first._db:
        first._db.execute("DELETE FROM assistant_completion WHERE key=?", (FIRST_LAUNCH_KEY,))
        first._db.execute("UPDATE assistant_events SET expires_at=NULL WHERE id=?", (note["event_id"],))
    first._db.close()
    reopened = AssistantStore(path)
    try:
        assert reopened.completion(FIRST_LAUNCH_KEY) == "0" and reopened.first_launch_at() is None
        # The legacy pending row still replays while it is current.
        assert [e["id"] for e in reopened.pending_events()] == [note["event_id"]]
    finally:
        reopened._db.close()
    again = AssistantStore(path)
    assert again.completion(FIRST_LAUNCH_KEY) == "0"
    again._db.close()


# --- no duplicate or internal alerts ---------------------------------------------

def test_routine_hold_gets_no_lead_alerts(store):
    _upgrade(store)
    now = time.time()
    store.add_manual("Tentative: Gym (Wisp suggested)", now + 600, kind="event",
                     context=BEHAVIOR_CONTEXT + " — gym tomorrow")
    assert due_reminders(store, now) == []


def test_routine_reminder_due_at_its_nudge_is_not_repeated(store):
    _upgrade(store)
    now = time.time()
    store.add_manual("Review calendar overlap", now, context=BEHAVIOR_CONTEXT)
    assert due_reminders(store, now + 1) == []


def test_routine_reminder_for_later_still_fires(store):
    _upgrade(store)
    now = time.time()
    row = store.add_manual("Pack gym bag", now + 30, context=BEHAVIOR_CONTEXT)
    with store._db:
        store._db.execute("UPDATE commitments SET created_at=? WHERE id=?", (now - 6 * 3600, row["id"]))
    [note] = due_reminders(store, now + 31)
    assert note["title"] == "Pack gym bag" and note["stage"] == "due"


@pytest.mark.parametrize("source", ["calendar", "manual"])
def test_user_item_with_similar_words_still_fires(store, source):
    _upgrade(store)
    now = time.time()
    store.add_manual("Tentative: dentist", now + 600, kind="event", source=source)
    [note] = due_reminders(store, now)
    assert note["title"] == "Tentative: dentist"


def _record_native_write(store, source_id, due_ts, *, kind="create_reminder", action="a1"):
    payload = {"type": kind, "action_id": action, "title": "Take medicine", "due_ts": due_ts}
    if kind == "create_reminder":
        payload["commitment_kind"] = "reminder"
    row = store.enqueue_event(payload, dedupe_key="action:" + action, target={"type": "verified_reminder"})
    result = {"ok": True, "status": "succeeded", "error": "", "source_id": source_id,
              "title": "Take medicine", "due_ts": due_ts}
    with store._db:
        store._db.execute("UPDATE assistant_events SET result=? WHERE id=?", (json.dumps(result), row["id"]))


def test_native_alarm_replaces_wisps_due_alert(store):
    _upgrade(store)
    now = time.time()
    due = float(int(now // 60) * 60)
    store.sync_source("reminders", [{"source_id": "ek-1", "kind": "reminder",
                                     "title": "Take medicine", "when_ts": due}])
    _record_native_write(store, "ek-1", due)
    assert store.native_alarm_covers("ek-1", due)
    assert due_reminders(store, now) == []


def test_reminder_moved_outside_wisp_gets_wisps_alert(store):
    _upgrade(store)
    now = time.time()
    due = float(int(now // 60) * 60)
    _record_native_write(store, "ek-1", due - 3600)   # Wisp's alarm was for an hour earlier
    store.sync_source("reminders", [{"source_id": "ek-1", "kind": "reminder",
                                     "title": "Take medicine", "when_ts": due}])
    assert not store.native_alarm_covers("ek-1", due)
    [note] = due_reminders(store, now)
    assert note["stage"] == "due"


def test_newest_native_write_decides(store):
    _upgrade(store)
    due = 2_000_000_000.0
    _record_native_write(store, "ek-1", due, action="a1")
    time.sleep(0.01)
    _record_native_write(store, "ek-1", due + 3600, kind="update_reminder", action="a2")
    assert not store.native_alarm_covers("ek-1", due)
    assert store.native_alarm_covers("ek-1", due + 3600)
    assert not store.native_alarm_covers("ek-other", due)


def test_lapsed_stages():
    assert reminders.lapsed_stages("exam", 10 * 86400, 0) == []
    assert reminders.lapsed_stages("exam", 86400, 0) == ["T-1w", "T-1d"]
    assert reminders.lapsed_stages("meeting", 600, 0) == ["T-30m", "T-10m"]
    assert reminders.lapsed_stages("unknown", 1200, 0) == ["T-30m"]
