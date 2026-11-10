"""Finished refresh signals leave the event table; everything else stays.

Every source sync stores a durable "changed" event so a refresh published
between two stream connections is replayed. Once the app has acknowledged
it nothing reads the row again, and it used to stay for good.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from service.assistant import store as store_module
from service.assistant.store import REFRESH_EVENT_KEEP_S, REFRESH_EVENT_KINDS, AssistantStore


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path / "assistant.db")
    yield s
    s._db.close()


def _age(store, event_id: str, seconds: float) -> None:
    """Make a row look as if it was stored and finished `seconds` ago."""
    with store._write_transaction():
        store._db.execute(
            "UPDATE assistant_events SET created_at=created_at-?,"
            "acknowledged_at=acknowledged_at-? WHERE id=?", (seconds, seconds, event_id))


def _acked(store, payload: dict, *, age: float = 0.0, **kwargs) -> str:
    row = store.enqueue_event(payload, **kwargs)
    assert store.acknowledge_event(row["id"], payload["type"]) is True
    if age:
        _age(store, row["id"], age)
    return row["id"]


def _ids(store) -> set[str]:
    return {r["id"] for r in store._db.execute("SELECT id FROM assistant_events")}


def _prune(store, **kwargs) -> int:
    with store._write_transaction():
        return store._prune_refresh_events(**kwargs)


OLD = REFRESH_EVENT_KEEP_S + 60


def test_the_refresh_kinds_are_the_ones_the_service_publishes():
    assert set(REFRESH_EVENT_KINDS) == {"changed", "sync_assistant_sources_now", "sync_emails_now"}
    # None of them is a kind whose finished row is kept as a receipt or a repeat guard.
    assert not set(REFRESH_EVENT_KINDS) & set(store_module.HELD_EVENT_KINDS)


def test_old_finished_refresh_signals_are_removed(store):
    gone = {
        _acked(store, {"type": "changed"}, age=OLD),
        _acked(store, {"type": "sync_emails_now"}, age=OLD),
        _acked(store, {"type": "sync_assistant_sources_now", "sources": ["email"]}, age=OLD),
    }
    assert _prune(store) == 3
    assert not gone & _ids(store)


def test_a_waiting_refresh_signal_is_never_removed(store):
    waiting = store.enqueue_event({"type": "changed"})["id"]
    with store._write_transaction():
        store._db.execute("UPDATE assistant_events SET created_at=created_at-? WHERE id=?",
                          (OLD * 100, waiting))
    assert _prune(store) == 0
    assert [e["id"] for e in store.pending_events()] == [waiting]


def test_a_just_finished_refresh_signal_keeps_its_receipt(store):
    recent = _acked(store, {"type": "changed"}, age=REFRESH_EVENT_KEEP_S - 60)
    assert _prune(store) == 0
    # The app repeats a receipt it could not confirm; that must still succeed.
    assert store.acknowledge_event(recent, "changed") is True


def test_other_kinds_keep_their_finished_rows(store):
    now = time.time()
    store.add_manual("Fixture standup", now + 300, kind="meeting")
    from service.assistant.reminders import due_reminders
    store.record_completion(store_module.FIRST_LAUNCH_KEY, "0")
    [note] = due_reminders(store, now)
    assert store.acknowledge_event(note["event_id"], "reminder") is True
    _age(store, note["event_id"], OLD * 100)
    brief = _acked(store, {"type": "daily_brief", "text": "Fixture brief"}, age=OLD * 100,
                   dedupe_key="daily-brief:2030-01-02:morning",
                   target={"type": "brief", "date": "2030-01-02", "part": "morning"})
    update = _acked(store, {"type": "codex_task_update", "title": "Fixture task"}, age=OLD * 100)
    old_changed = _acked(store, {"type": "changed"}, age=OLD)
    assert _prune(store) == 1
    left = _ids(store)
    assert {note["event_id"], brief, update} <= left and old_changed not in left
    assert store.any_daily_brief() is True


def test_a_claimed_or_answered_row_of_a_refresh_kind_is_kept(store):
    claimed = _acked(store, {"type": "changed"}, age=OLD)
    answered = _acked(store, {"type": "changed"}, age=OLD)
    with store._write_transaction():
        store._db.execute("UPDATE assistant_events SET claim_token='fixture' WHERE id=?", (claimed,))
        store._db.execute("UPDATE assistant_events SET result='{}' WHERE id=?", (answered,))
    assert _prune(store) == 0
    assert {claimed, answered} <= _ids(store)


def test_one_pass_removes_at_most_its_batch(store):
    for _ in range(5):
        _acked(store, {"type": "changed"}, age=OLD)
    assert _prune(store, limit=2) == 2
    assert _prune(store, limit=2) == 2
    assert _prune(store, limit=2) == 1
    assert _ids(store) == set()


def test_replay_prunes_at_most_once_a_minute(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(store_module.time, "monotonic", lambda: clock[0])
    store._refresh_pruned_at = None
    first = _acked(store, {"type": "changed"}, age=OLD)
    assert store.pending_events() == []
    assert first not in _ids(store)
    second = _acked(store, {"type": "changed"}, age=OLD)
    clock[0] += store_module._REFRESH_PRUNE_EVERY_S - 1
    store.pending_events()
    assert second in _ids(store)
    clock[0] += 2
    store.pending_events()
    assert second not in _ids(store)


def test_opening_the_store_clears_the_backlog_and_keeps_the_rest(tmp_path):
    path = tmp_path / "assistant.db"
    first = AssistantStore(path)
    try:
        old = _acked(first, {"type": "changed"}, age=OLD)
        waiting = first.enqueue_event({"type": "changed"})["id"]
        kept = _acked(first, {"type": "codex_task_update", "title": "Fixture task"}, age=OLD)
    finally:
        first._db.close()
    second = AssistantStore(path)
    try:
        assert _ids(second) == {waiting, kept}
        assert old not in _ids(second)
    finally:
        second._db.close()


def test_the_waiting_index_is_created_once_and_used(tmp_path):
    path = tmp_path / "assistant.db"
    AssistantStore(path)._db.close()
    with sqlite3.connect(path) as legacy:   # an install from before the index
        legacy.execute("DROP INDEX idx_assistant_events_pending")
    for _ in range(2):                      # added on upgrade, untouched after
        opened = AssistantStore(path)
        try:
            rows = opened._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_assistant_events_pending'").fetchall()
            assert len(rows) == 1 and "state='pending'" in rows[0][0]
            plan = " ".join(r[-1] for r in opened._db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM assistant_events "
                "WHERE state='pending' ORDER BY created_at,id"))
            assert "idx_assistant_events_pending" in plan
        finally:
            opened._db.close()
