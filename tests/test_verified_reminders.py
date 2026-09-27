"""Synthetic contract and crash/restart checks; no EventKit or user state."""
from __future__ import annotations

from pathlib import Path
import asyncio
import sqlite3
import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from service.assistant.verified_reminders import validate_result
from service.assistant.store import AssistantStore
from service.assistant.hub import Hub
from service.assistant import outbox
from service.assistant.reminders import due_reminders
from httpx import ASGITransport, AsyncClient


def payload(kind: str, action_id: str = "a1") -> dict:
    value = {"type": kind, "action_id": action_id}
    if kind == "create_reminder":
        value.update(title="Take medicine", due_ts=2_000_000_000.0,
                     commitment_kind="reminder")
    else:
        value.update(source_id="ek-123", expected_title="Take medicine",
                     expected_due_ts=2_000_000_000.0)
        if kind == "update_reminder":
            value.update(title="Take evening medicine", due_ts=2_000_003_600.0)
    return value


def success(kind: str) -> dict:
    value = {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123"}
    if kind in {"create_reminder", "update_reminder"}:
        value.update(title=("Take medicine" if kind == "create_reminder" else
                            "Take evening medicine"),
                     due_ts=(2_000_000_000.0 if kind == "create_reminder" else 2_000_003_600.0))
    elif kind == "complete_reminder":
        value["is_completed"] = True
    else:
        value["is_absent"] = True
    return value


def record_verified(store: AssistantStore, kind: str, action_id: str,
                    fields: dict, receipt: dict) -> None:
    action = {"type": kind, "action_id": action_id, **fields}
    row = store.enqueue_event(action, dedupe_key="action:" + action_id,
                              target={"type": "verified_reminder"},
                              expires_at=time.time() + 45)
    claim = store.claim_calendar_action(row["id"], kind, action_id, action)
    assert claim["execute"] is True
    store.complete_calendar_action(row["id"], kind, claim["claim_token"], receipt)


@pytest.mark.parametrize("kind", ["create_reminder", "update_reminder",
                                   "complete_reminder", "delete_reminder"])
def test_production_event_claim_and_receipt_are_atomic(tmp_path: Path, kind: str):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    try:
        if kind != "create_reminder":
            store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
                "title": "Take medicine", "when_ts": 2_000_000_000.0}])
        if kind == "update_reminder":
            original = store._db.execute(
                "SELECT id FROM commitments WHERE source='reminders' AND source_id='ek-123'"
            ).fetchone()["id"]
            store.mark_notified(original, "due")
        p = payload(kind)
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], kind, "a1", p)
        assert claim["execute"] is True
        assert store.claim_calendar_action(row["id"], kind, "a1", p)["execute"] is False
        assert store.event(row["id"])["result"] is None
        with pytest.raises(ValueError):
            store.complete_calendar_action(row["id"], kind, claim["claim_token"],
                                           {"ok": True, "status": "succeeded", "error": ""})
        assert store.event(row["id"])["result"] is None
        if kind == "update_reminder":
            assert store.already_notified(original, "due")
        store.complete_calendar_action(row["id"], kind, claim["claim_token"], success(kind))
        assert store.acknowledge_event(row["id"], kind) is True
        matching = store._db.execute("SELECT * FROM commitments WHERE source='reminders' AND source_id='ek-123'").fetchall()
        assert len(matching) == 1
        native = dict(matching[0])
        if kind == "update_reminder":
            assert native["title"] == "Take evening medicine"
            assert native["when_ts"] == 2_000_003_600.0
            assert not store.already_notified(original, "due")
        elif kind == "complete_reminder":
            assert native["status"] == "done"
            store.sync_source("reminders", [], diagnostics={"snapshot_started_at": time.time()})
            kept = store._db.execute("SELECT status FROM commitments WHERE id=?", (native["id"],)).fetchone()
            assert kept is not None and kept["status"] == "done"
        elif kind == "delete_reminder":
            assert native["status"] == "dismissed"
    finally:
        store._db.close()


def test_production_unknown_reconciles_without_reexecuting(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    try:
        p = payload("create_reminder")
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "create_reminder", "a1", p)
        store.complete_calendar_action(row["id"], "create_reminder", claim["claim_token"],
            {"ok": False, "status": "unknown", "error": "native reply lost"})
        assert store.claim_calendar_action(row["id"], "create_reminder", "a1", p)["execute"] is False
        assert not store._db.execute("SELECT 1 FROM commitments WHERE source='reminders'").fetchone()
        with pytest.raises(ValueError):
            store.complete_calendar_action(row["id"], "create_reminder", claim["claim_token"],
                                           success("create_reminder"))
        store.complete_calendar_action(row["id"], "create_reminder", claim["claim_token"],
                                       success("create_reminder"), reconcile=True)
        assert store.acknowledge_event(row["id"], "create_reminder") is True
        assert store._db.execute("SELECT 1 FROM commitments WHERE source='reminders'").fetchone()
    finally:
        store._db.close()


def test_restart_keeps_exclusive_claim_and_rejects_partial_receipt(tmp_path: Path):
    path = tmp_path / "assistant.sqlite"
    first = AssistantStore(path)
    p = payload("update_reminder")
    row = first.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                              expires_at=time.time() + 45)
    claim = first.claim_calendar_action(row["id"], "update_reminder", "a1", p)
    first._db.close()

    second = AssistantStore(path)
    try:
        blocked = second.claim_calendar_action(row["id"], "update_reminder", "a1", p)
        assert blocked["execute"] is False
        assert blocked["claim_token"] == claim["claim_token"]
        partial = success("update_reminder")
        partial.pop("due_ts")
        with pytest.raises(ValueError):
            second.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"], partial)
        assert second.event(row["id"])["result"] is None
        with pytest.raises(ValueError):
            second.acknowledge_event(row["id"], "update_reminder")
    finally:
        second._db.close()


def test_wrong_native_identity_and_values_are_rejected():
    p = payload("update_reminder")
    for changed in ({"source_id": "different"}, {"title": "different"},
                    {"due_ts": p["due_ts"] + 60}, {"ok": False}):
        with pytest.raises(ValueError):
            validate_result("update_reminder", p, {**success("update_reminder"), **changed})


@pytest.mark.asyncio
async def test_outbox_waits_for_durable_verified_native_receipt(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    try:
        with patch.object(outbox, "hub", hub):
            waiting = asyncio.create_task(outbox.request("create_reminder", {
                "title": "Take medicine", "due_ts": 2_000_000_000.0,
                "commitment_kind": "reminder"}, timeout=2))
            event = await asyncio.wait_for(queue.get(), 1)
            row = store.event_by_key("action:" + event["action_id"])
            assert row is not None and row["target"]["type"] == "verified_reminder"
            claim = store.claim_calendar_action(row["id"], event["type"],
                                                event["action_id"], row["payload"])
            assert claim["execute"] is True
            result = success("create_reminder")
            store.complete_calendar_action(row["id"], event["type"], claim["claim_token"], result)
            assert outbox.complete(event["action_id"], result) is True
            assert await waiting == result
            assert store.acknowledge_event(row["id"], event["type"]) is True
    finally:
        hub.unsubscribe(queue)
        store._db.close()


@pytest.mark.asyncio
async def test_timeout_then_same_create_cannot_issue_second_native_action(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    args = {"title": "Take medicine", "due_ts": 2_000_000_000.0,
            "commitment_kind": "reminder"}
    try:
        with patch.object(outbox, "hub", hub):
            waiting = asyncio.create_task(outbox.request("create_reminder", args, timeout=.1))
            event = await asyncio.wait_for(queue.get(), 1)
            row = store.event_by_key("action:" + event["action_id"])
            claim = store.claim_calendar_action(row["id"], event["type"],
                                                event["action_id"], row["payload"])
            assert claim["execute"] is True
            assert (await waiting)["status"] == "unknown"
            retry = await outbox.request("create_reminder", args, timeout=.1)
            assert retry["status"] == "unknown"
            assert queue.empty()
            assert len(store.reminder_actions_by_prefix(event["action_id"])) == 1
            store.complete_calendar_action(row["id"], event["type"], claim["claim_token"],
                {"ok": False, "status": "unknown", "error": "reply lost"})
            assert (await outbox.request("create_reminder", args, timeout=.1))["status"] == "unknown"
            store.complete_calendar_action(row["id"], event["type"], claim["claim_token"],
                                           success("create_reminder"), reconcile=True)
            assert await outbox.request("create_reminder", args, timeout=.1) == success("create_reminder")
            assert queue.empty()
    finally:
        hub.unsubscribe(queue)
        store._db.close()


@pytest.mark.asyncio
async def test_repeated_update_after_round_trip_gets_new_native_action(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    a = 2_000_000_000.0
    b = 2_000_003_600.0
    store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
        "title": "A", "when_ts": a}])

    async def perform(old_title: str, old_due: float, title: str, due: float) -> dict:
        args = {"source_id": "ek-123", "expected_title": old_title,
                "expected_due_ts": old_due, "title": title, "due_ts": due}
        waiting = asyncio.create_task(outbox.request("update_reminder", args, timeout=2))
        event = await asyncio.wait_for(queue.get(), 1)
        row = store.event_by_key("action:" + event["action_id"])
        assert row is not None
        claim = store.claim_calendar_action(row["id"], event["type"],
                                            event["action_id"], row["payload"])
        assert claim["execute"] is True
        receipt = {"ok": True, "status": "succeeded", "error": "",
                   "source_id": "ek-123", "title": title, "due_ts": due}
        store.complete_calendar_action(row["id"], event["type"], claim["claim_token"], receipt)
        assert outbox.complete(event["action_id"], receipt)
        assert await waiting == receipt
        return event

    try:
        with patch.object(outbox, "hub", hub):
            first = await perform("A", a, "B", b)
            replay = await outbox.request("update_reminder", {
                "source_id": "ek-123", "expected_title": "A", "expected_due_ts": a,
                "title": "B", "due_ts": b}, timeout=.1)
            assert replay["ok"] is True and queue.empty()
            await perform("B", b, "A", a)
            third = await perform("A", a, "B", b)
            assert third["action_id"] == first["action_id"] + ":1"
            current = store._db.execute(
                "SELECT title,when_ts FROM commitments WHERE source='reminders' AND source_id='ek-123'"
            ).fetchone()
            assert current["title"] == "B" and current["when_ts"] == b
    finally:
        hub.unsubscribe(queue)
        store._db.close()


@pytest.mark.asyncio
async def test_identical_create_after_verified_delete_gets_new_native_action(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    args = {"title": "Take medicine", "due_ts": 2_000_000_000.0,
            "commitment_kind": "reminder"}

    async def perform(kind: str, payload: dict, source_id: str) -> dict:
        waiting = asyncio.create_task(outbox.request(kind, payload, timeout=2))
        event = await asyncio.wait_for(queue.get(), 1)
        row = store.event_by_key("action:" + event["action_id"])
        assert row is not None
        claim = store.claim_calendar_action(row["id"], kind,
                                            event["action_id"], row["payload"])
        assert claim["execute"] is True
        receipt = {"ok": True, "status": "succeeded", "error": "", "source_id": source_id}
        if kind == "create_reminder":
            receipt.update(title=args["title"], due_ts=args["due_ts"])
        else:
            receipt["is_absent"] = True
        store.complete_calendar_action(row["id"], kind, claim["claim_token"], receipt)
        assert outbox.complete(event["action_id"], receipt)
        assert await waiting == receipt
        return event

    try:
        with patch.object(outbox, "hub", hub):
            first = await perform("create_reminder", args, "ek-old")
            assert (await outbox.request("create_reminder", args, timeout=.1))["source_id"] == "ek-old"
            assert queue.empty()
            await perform("delete_reminder", {"source_id": "ek-old",
                "expected_title": args["title"], "expected_due_ts": args["due_ts"]}, "ek-old")
            # The first old snapshot prunes the dismissed commitment. The
            # second was also fetched before deletion and must not resurrect
            # it after the commitment row (the old marker) has vanished.
            before_delete = time.time() - 2
            store.sync_source("reminders", [],
                              diagnostics={"snapshot_started_at": before_delete})
            assert store._db.execute(
                "SELECT 1 FROM assistant_reminder_terminals WHERE source_id='ek-old'"
            ).fetchone()
            store.sync_source("reminders", [{"source_id": "ek-old", "kind": "reminder",
                "title": args["title"], "when_ts": args["due_ts"]}],
                diagnostics={"snapshot_started_at": before_delete + .1})
            assert not store._db.execute(
                "SELECT 1 FROM commitments WHERE source='reminders' AND source_id='ek-old' "
                "AND status='active'").fetchone()
            second = await perform("create_reminder", args, "ek-new")
            assert second["action_id"] == first["action_id"] + ":1"
            rows = store._db.execute(
                "SELECT source_id,status FROM commitments WHERE source='reminders' ORDER BY source_id"
            ).fetchall()
            assert [(row["source_id"], row["status"]) for row in rows] == [
                ("ek-new", "active")]
    finally:
        hub.unsubscribe(queue)
        store._db.close()


@pytest.mark.asyncio
async def test_completed_reminder_reopened_by_fresh_sync_needs_new_completion(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    item = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    store.sync_source("reminders", [item])

    async def complete_once(due: float) -> dict:
        args = {"source_id": "ek-123", "expected_title": item["title"],
                "expected_due_ts": due}
        waiting = asyncio.create_task(outbox.request("complete_reminder", args, timeout=2))
        event = await asyncio.wait_for(queue.get(), 1)
        row = store.event_by_key("action:" + event["action_id"])
        assert row is not None
        claim = store.claim_calendar_action(row["id"], event["type"],
                                            event["action_id"], row["payload"])
        assert claim["execute"] is True
        receipt = {"ok": True, "status": "succeeded", "error": "",
                   "source_id": "ek-123", "is_completed": True}
        store.complete_calendar_action(row["id"], event["type"], claim["claim_token"], receipt)
        assert outbox.complete(event["action_id"], receipt)
        assert await waiting == receipt
        return event

    try:
        with patch.object(outbox, "hub", hub):
            first = await complete_once(item["when_ts"])
            assert (await outbox.request("complete_reminder", {
                "source_id": "ek-123", "expected_title": item["title"],
                "expected_due_ts": item["when_ts"]}, timeout=.1))["ok"] is True
            assert queue.empty()
            later = {**item, "when_ts": item["when_ts"] + 3_600}
            await asyncio.sleep(.002)
            store.sync_source("reminders", [later],
                              diagnostics={"snapshot_started_at": time.time()})
            assert store._db.execute(
                "SELECT COUNT(*) FROM commitments WHERE source='reminders' AND status='done' "
                "AND source_id LIKE 'wisp-history:%'"
            ).fetchone()[0] == 1
            assert store._db.execute(
                "SELECT COUNT(*) FROM commitments WHERE source='reminders' AND source_id='ek-123' "
                "AND status='active'"
            ).fetchone()[0] == 1
            second = await complete_once(later["when_ts"])
            await asyncio.sleep(.002)
            store.sync_source("reminders", [later],
                              diagnostics={"snapshot_started_at": time.time()})
            third = await complete_once(later["when_ts"])
            assert third["action_id"] == second["action_id"] + ":1"
            assert first["action_id"] != second["action_id"]
            rows = store._db.execute(
                "SELECT source_id,when_ts,status FROM commitments WHERE source='reminders' "
                "ORDER BY when_ts,source_id"
            ).fetchall()
            assert len(rows) == 3 and all(row["status"] == "done" for row in rows)
            assert [row["when_ts"] for row in rows] == [
                item["when_ts"], later["when_ts"], later["when_ts"]]
            assert [row["source_id"] for row in rows].count("ek-123") == 1
    finally:
        hub.unsubscribe(queue)
        store._db.close()


def test_reschedule_back_to_notified_time_gets_new_generation(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    now = time.time()
    a = float(int(now // 60) * 60)
    b = a + 3_600
    try:
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": a}])
        first = due_reminders(store, now=now)
        assert len(first) == 1
        store.acknowledge_event(first[0]["event_id"], "reminder")
        native_id = store._db.execute(
            "SELECT id FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchone()["id"]
        original_generation = store.reminder_generation(native_id)
        for index, (old, new) in enumerate(((a, b), (b, a))):
            action_id = f"move-{index}"
            payload = {"type": "update_reminder", "action_id": action_id,
                       "source_id": "ek-123", "expected_title": "Take medicine",
                       "expected_due_ts": old, "title": "Take medicine", "due_ts": new}
            row = store.enqueue_event(payload, dedupe_key="action:" + action_id,
                                      target={"type": "verified_reminder"},
                                      expires_at=time.time() + 45)
            claim = store.claim_calendar_action(row["id"], "update_reminder", action_id, payload)
            store.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"],
                {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123",
                 "title": "Take medicine", "due_ts": new})
        assert store.reminder_generation(native_id) != original_generation
        assert not store.already_notified(native_id, "due")
        repeated = due_reminders(store, now=now)
        assert len(repeated) == 1 and repeated[0]["event_id"] != first[0]["event_id"]
    finally:
        store._db.close()


def test_reopened_reminder_can_move_back_to_historical_due_time(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    a = 2_000_000_000.0
    b = a + 3_600
    try:
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": a}])
        record_verified(store, "complete_reminder", "complete-a", {
            "source_id": "ek-123", "expected_title": "Take medicine", "expected_due_ts": a},
            {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123",
             "is_completed": True})
        time.sleep(.002)
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": b}],
            diagnostics={"snapshot_started_at": time.time()})
        record_verified(store, "update_reminder", "move-back", {
            "source_id": "ek-123", "expected_title": "Take medicine",
            "expected_due_ts": b, "title": "Take medicine", "due_ts": a},
            {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123",
             "title": "Take medicine", "due_ts": a})
        rows = store._db.execute(
            "SELECT source_id,when_ts,status FROM commitments WHERE source='reminders' "
            "ORDER BY status,source_id"
        ).fetchall()
        assert len(rows) == 2
        assert any(row["source_id"] == "ek-123" and row["status"] == "active"
                   and row["when_ts"] == a for row in rows)
        assert any(row["source_id"].startswith("wisp-history:")
                   and row["status"] == "done" and row["when_ts"] == a for row in rows)
    finally:
        store._db.close()


def test_same_time_reopen_creates_new_notification_incarnation(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    now = time.time()
    due = float(int(now // 60) * 60)
    try:
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": due}])
        first = due_reminders(store, now=now)
        assert len(first) == 1
        store.acknowledge_event(first[0]["event_id"], "reminder")
        record_verified(store, "complete_reminder", "complete-once", {
            "source_id": "ek-123", "expected_title": "Take medicine", "expected_due_ts": due},
            {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123",
             "is_completed": True})
        time.sleep(.002)
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": due}],
            diagnostics={"snapshot_started_at": time.time()})
        repeated = due_reminders(store, now=now)
        assert len(repeated) == 1 and repeated[0]["event_id"] != first[0]["event_id"]
        assert store._db.execute(
            "SELECT COUNT(*) FROM commitments WHERE source='reminders' AND status='done'"
        ).fetchone()[0] == 1
    finally:
        store._db.close()


def test_terminal_receipt_isolated_from_same_title_same_minute_native_twin(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    due = 2_000_000_000.0
    title = "Take medicine"
    native = [{"source_id": sid, "kind": "reminder", "title": title, "when_ts": due}
              for sid in ("ek-one", "ek-two")]
    try:
        store.sync_source("reminders", native)
        record_verified(store, "complete_reminder", "complete-one", {
            "source_id": "ek-one", "expected_title": title, "expected_due_ts": due},
            {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-one",
             "is_completed": True})
        stale_started = time.time() - 2
        store.sync_source("reminders", native,
                          diagnostics={"snapshot_started_at": stale_started})
        rows = store._db.execute(
            "SELECT source_id,status FROM commitments WHERE source='reminders' "
            "ORDER BY source_id"
        ).fetchall()
        assert [(row["source_id"], row["status"]) for row in rows] == [
            ("ek-one", "done"), ("ek-two", "active")]
        assert store._db.execute(
            "SELECT source_id FROM assistant_reminder_terminals"
        ).fetchone()["source_id"] == "ek-one"
    finally:
        store._db.close()


def test_uncertain_completion_does_not_create_terminal_watermark(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    try:
        store.sync_source("reminders", [{"source_id": "ek-123", "kind": "reminder",
            "title": "Take medicine", "when_ts": 2_000_000_000.0}])
        p = payload("complete_reminder")
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "complete_reminder", "a1", p)
        store.complete_calendar_action(row["id"], "complete_reminder", claim["claim_token"],
            {"ok": False, "status": "unknown", "error": "native reply lost"})
        assert not store._db.execute(
            "SELECT 1 FROM assistant_reminder_terminals WHERE source_id='ek-123'"
        ).fetchone()
        assert store._db.execute(
            "SELECT status FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchone()["status"] == "active"
        store.complete_calendar_action(row["id"], "complete_reminder", claim["claim_token"],
                                       success("complete_reminder"), reconcile=True)
        assert store._db.execute(
            "SELECT status FROM assistant_reminder_terminals WHERE source_id='ek-123'"
        ).fetchone()["status"] == "done"
    finally:
        store._db.close()


def test_stale_incomplete_snapshot_cannot_undo_verified_completion(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    item = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        p = payload("complete_reminder")
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "complete_reminder", "a1", p)
        store.complete_calendar_action(row["id"], "complete_reminder", claim["claim_token"],
                                       success("complete_reminder"))
        stale_started = time.time() - 2
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": stale_started})
        kept = store._db.execute(
            "SELECT status FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchall()
        assert len(kept) == 1 and kept[0]["status"] == "done"
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": time.time()})
        kept = store._db.execute(
            "SELECT status FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchall()
        assert len(kept) == 1 and kept[0]["status"] == "done"
        # A later native snapshot containing the same ID is positive evidence
        # that the user reopened the item after the verified completion.
        time.sleep(.002)
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": time.time()})
        reopened = store._db.execute(
            "SELECT status FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchall()
        assert len(reopened) == 1 and reopened[0]["status"] == "active"
    finally:
        store._db.close()


def test_empty_sync_during_claim_cannot_erase_completed_history(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    item = {"source_id": "ek-123", "kind": "assignment", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        p = payload("complete_reminder")
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "complete_reminder", "a1", p)
        store._db.close()
        store = AssistantStore(tmp_path / "assistant.sqlite")
        # EventKit has already completed the item, but its readback receipt has
        # not reached Wisp. An independent authoritative sync sees an empty feed.
        store.sync_source("reminders", [], diagnostics={"snapshot_started_at": time.time()})
        assert store._db.execute(
            "SELECT status FROM commitments WHERE source_id='ek-123'"
        ).fetchone()["status"] == "active"
        store.complete_calendar_action(row["id"], "complete_reminder", claim["claim_token"],
                                       success("complete_reminder"))
        history = store._db.execute(
            "SELECT kind,status FROM commitments WHERE source_id='ek-123'"
        ).fetchall()
        assert [(entry["kind"], entry["status"]) for entry in history] == [("assignment", "done")]
        assert store.reminder_terminal_retry_state("complete_reminder", p) == "desired"
    finally:
        store._db.close()


def test_prewrite_snapshot_cannot_erase_verified_create_or_revert_update(tmp_path: Path):
    path = tmp_path / "assistant.sqlite"
    store = AssistantStore(path)
    old = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
           "when_ts": 2_000_000_000.0}
    try:
        started_before_create = time.time() - 2
        record_verified(store, "create_reminder", "create-a", {
            "title": old["title"], "due_ts": old["when_ts"],
            "commitment_kind": "reminder"}, success("create_reminder"))
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": started_before_create})
        assert store._db.execute(
            "SELECT title FROM commitments WHERE source_id='ek-123'"
        ).fetchone()["title"] == old["title"]
        assert store.reminder_create_retry_state(store.reminder_actions_by_prefix("create-a")[0]) == "present"

        started_before_update = time.time() - 1
        record_verified(store, "update_reminder", "update-a", {
            "source_id": "ek-123", "expected_title": old["title"],
            "expected_due_ts": old["when_ts"], "title": "Take evening medicine",
            "due_ts": 2_000_003_600.0}, success("update_reminder"))
        store._db.close()
        store = AssistantStore(path)
        store.sync_source("reminders", [old],
                          diagnostics={"snapshot_started_at": started_before_update})
        rows = store._db.execute(
            "SELECT title,when_ts,status FROM commitments WHERE source_id='ek-123'"
        ).fetchall()
        assert [(entry["title"], entry["when_ts"], entry["status"]) for entry in rows] == [
            ("Take evening medicine", 2_000_003_600.0, "active")]
        assert store.reminder_update_state({"source_id": "ek-123",
            "expected_title": old["title"], "expected_due_ts": old["when_ts"],
            "title": "Take evening medicine", "due_ts": 2_000_003_600.0}) == "desired"
        # A later snapshot may prove that the native item was changed again.
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": time.time()})
        assert not store._db.execute(
            "SELECT 1 FROM commitments WHERE source_id='ek-123'"
        ).fetchone()
    finally:
        store._db.close()


@pytest.mark.parametrize("old_outcome", ["pending", "unknown"])
@pytest.mark.parametrize("later_kind", ["complete_reminder", "delete_reminder"])
def test_older_claim_cannot_block_later_verified_terminal_reopen(
        tmp_path: Path, old_outcome: str, later_kind: str):
    path = tmp_path / "assistant.sqlite"
    store = AssistantStore(path)
    item = {"source_id": "ek-123", "kind": "assignment", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        old = payload("update_reminder", "old-update")
        row = store.enqueue_event(old, dedupe_key="action:old-update",
                                  target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "update_reminder", "old-update", old)
        if old_outcome == "unknown":
            store.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"],
                {"ok": False, "status": "unknown", "error": "Native reply lost"})
            store.acknowledge_event(row["id"], "update_reminder")
        started_before_terminal = time.time() - 2
        record_verified(store, later_kind, "later-terminal", {
            "source_id": "ek-123", "expected_title": item["title"],
            "expected_due_ts": item["when_ts"]}, success(later_kind))
        store._db.close()
        store = AssistantStore(path)
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": started_before_terminal})
        assert not store._db.execute(
            "SELECT 1 FROM commitments WHERE source='reminders' AND source_id='ek-123' "
            "AND status='active'"
        ).fetchone()
        time.sleep(.002)
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": time.time()})
        rows = store._db.execute(
            "SELECT source_id,status FROM commitments WHERE source='reminders' ORDER BY status"
        ).fetchall()
        assert ("ek-123", "active") in [(r["source_id"], r["status"]) for r in rows]
        if later_kind == "complete_reminder":
            assert any(r["source_id"].startswith("wisp-history:") and r["status"] == "done"
                       for r in rows)
        old_result = store.event(row["id"])["result"]
        if old_outcome == "pending":
            assert old_result is None
        else:
            assert old_result["status"] == "unknown"
            # A late positive readback for that older action resolves its
            # event without overwriting the later terminal/reopen state.
            store.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"],
                                           success("update_reminder"), reconcile=True)
            assert store._db.execute(
                "SELECT title FROM commitments WHERE source_id='ek-123' AND status='active'"
            ).fetchone()["title"] == item["title"]
        time.sleep(.002)
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": time.time()})
        assert store.reminder_terminal_retry_state(later_kind, {
            "source_id": "ek-123", "expected_title": item["title"],
            "expected_due_ts": item["when_ts"]}) == "expected"
    finally:
        store._db.close()


def test_newer_verified_update_supersedes_old_unknown_but_new_claim_still_protects(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    item = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        old = payload("update_reminder", "old-update")
        row = store.enqueue_event(old, dedupe_key="action:old-update",
                                  target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "update_reminder", "old-update", old)
        store.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"],
            {"ok": False, "status": "unknown", "error": "Native reply lost"})
        record_verified(store, "update_reminder", "new-update", {
            "source_id": "ek-123", "expected_title": item["title"],
            "expected_due_ts": item["when_ts"], "title": "Take evening medicine",
            "due_ts": 2_000_003_600.0}, success("update_reminder"))
        time.sleep(.002)
        changed = {**item, "title": "Changed externally", "when_ts": 2_000_007_200.0}
        store.sync_source("reminders", [changed],
                          diagnostics={"snapshot_started_at": time.time()})
        assert store._db.execute(
            "SELECT title FROM commitments WHERE source_id='ek-123'"
        ).fetchone()["title"] == "Changed externally"
        newer = {"type": "update_reminder", "action_id": "new-unknown", "source_id": "ek-123",
                 "expected_title": changed["title"], "expected_due_ts": changed["when_ts"],
                 "title": "New uncertain title", "due_ts": 2_000_010_800.0}
        pending = store.enqueue_event(newer, dedupe_key="action:new-unknown",
                                      target={"type": "verified_reminder"},
                                      expires_at=time.time() + 45)
        store.claim_calendar_action(pending["id"], "update_reminder", "new-unknown", newer)
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": time.time()})
        assert store._db.execute(
            "SELECT title FROM commitments WHERE source_id='ek-123'"
        ).fetchone()["title"] == "Changed externally"
    finally:
        store._db.close()


@pytest.mark.parametrize("receipt_order", ["old_then_new", "new_then_old"])
def test_delayed_old_receipt_cannot_override_newer_claim_or_terminal(
        tmp_path: Path, receipt_order: str):
    path = tmp_path / "assistant.sqlite"
    store = AssistantStore(path)
    item = {"source_id": "ek-123", "kind": "assignment", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        old = {"type": "update_reminder", "action_id": "old-update",
               "source_id": "ek-123", "expected_title": item["title"],
               "expected_due_ts": item["when_ts"], "title": "Take evening medicine",
               "due_ts": item["when_ts"] + 3_600}
        old_row = store.enqueue_event(old, dedupe_key="action:old-update",
                                      target={"type": "verified_reminder"},
                                      expires_at=time.time() + 45)
        old_claim = store.claim_calendar_action(
            old_row["id"], "update_reminder", "old-update", old)
        new = payload("complete_reminder", "new-completion")
        new_row = store.enqueue_event(new, dedupe_key="action:new-completion",
                                      target={"type": "verified_reminder"},
                                      expires_at=time.time() + 45)
        new_claim = store.claim_calendar_action(
            new_row["id"], "complete_reminder", "new-completion", new)
        old_success = {"ok": True, "status": "succeeded", "error": "", "source_id": "ek-123",
                       "title": old["title"], "due_ts": old["due_ts"]}
        new_success = {**success("complete_reminder"), "source_id": "ek-123"}
        if receipt_order == "new_then_old":
            store.complete_calendar_action(new_row["id"], "complete_reminder",
                                           new_claim["claim_token"], new_success)
        store.complete_calendar_action(old_row["id"], "update_reminder",
                                       old_claim["claim_token"], old_success)
        store._db.close()
        store = AssistantStore(path)
        if receipt_order == "old_then_new":
            # The old success arrived after the newer claim. It must not
            # release that claim's protection while its receipt is pending.
            store.sync_source("reminders", [],
                              diagnostics={"snapshot_started_at": time.time()})
            assert store._db.execute(
                "SELECT status FROM commitments WHERE source_id='ek-123'"
            ).fetchone()["status"] == "active"
            store.complete_calendar_action(new_row["id"], "complete_reminder",
                                           new_claim["claim_token"], new_success)
        else:
            assert store._db.execute(
                "SELECT action_id FROM assistant_reminder_terminals WHERE source_id='ek-123'"
            ).fetchone()["action_id"] == "new-completion"
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": time.time()})
        history = store._db.execute(
            "SELECT title,when_ts,status FROM commitments WHERE source_id='ek-123'"
        ).fetchone()
        assert history["status"] == "done"
        if receipt_order == "old_then_new":
            assert (history["title"], history["when_ts"]) == (old["title"], old["due_ts"])
        assert store._db.execute(
            "SELECT claim_order FROM assistant_reminder_verified WHERE source_id='ek-123'"
        ).fetchone()["claim_order"] == store.event(new_row["id"])["claim_order"]
    finally:
        store._db.close()


def test_claim_order_migration_recovers_newer_verified_action(tmp_path: Path):
    path = tmp_path / "assistant.sqlite"
    store = AssistantStore(path)
    item = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    try:
        store.sync_source("reminders", [item])
        old = payload("update_reminder", "old-update")
        row = store.enqueue_event(old, dedupe_key="action:old-update",
                                  target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "update_reminder", "old-update", old)
        store.complete_calendar_action(row["id"], "update_reminder", claim["claim_token"],
            {"ok": False, "status": "unknown", "error": "Native reply lost"})
        record_verified(store, "complete_reminder", "new-completion", {
            "source_id": "ek-123", "expected_title": item["title"],
            "expected_due_ts": item["when_ts"]}, success("complete_reminder"))
    finally:
        store._db.close()
    with sqlite3.connect(path) as legacy:
        legacy.execute("DROP INDEX idx_assistant_events_claim_order")
        legacy.execute("ALTER TABLE assistant_events DROP COLUMN claim_order")
        legacy.execute("ALTER TABLE assistant_reminder_verified DROP COLUMN claim_order")
    store = AssistantStore(path)
    try:
        claims = store._db.execute(
            "SELECT kind,claim_order FROM assistant_events ORDER BY claim_order"
        ).fetchall()
        assert [row["kind"] for row in claims] == ["update_reminder", "complete_reminder"]
        assert claims[0]["claim_order"] < claims[1]["claim_order"]
        assert store._db.execute(
            "SELECT claim_order FROM assistant_reminder_verified WHERE source_id='ek-123'"
        ).fetchone()["claim_order"] == claims[1]["claim_order"]
        time.sleep(.002)
        store.sync_source("reminders", [item],
                          diagnostics={"snapshot_started_at": time.time()})
        assert store._db.execute(
            "SELECT status FROM commitments WHERE source_id='ek-123'"
        ).fetchone()["status"] == "active"
    finally:
        store._db.close()


def test_delete_watermark_survives_restart_and_pre_table_migration(tmp_path: Path):
    path = tmp_path / "assistant.sqlite"
    item = {"source_id": "ek-123", "kind": "reminder", "title": "Take medicine",
            "when_ts": 2_000_000_000.0}
    before_delete = time.time() - 2
    store = AssistantStore(path)
    try:
        store.sync_source("reminders", [item])
        p = payload("delete_reminder")
        row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                                  expires_at=time.time() + 45)
        claim = store.claim_calendar_action(row["id"], "delete_reminder", "a1", p)
        store.complete_calendar_action(row["id"], "delete_reminder", claim["claim_token"],
                                       success("delete_reminder"))
        store.acknowledge_event(row["id"], "delete_reminder")
        store.sync_source("reminders", [],
                          diagnostics={"snapshot_started_at": before_delete})
        assert not store._db.execute(
            "SELECT 1 FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchone()
    finally:
        store._db.close()

    reopened = AssistantStore(path)
    try:
        reopened.sync_source("reminders", [item],
                             diagnostics={"snapshot_started_at": before_delete + .1})
        assert not reopened._db.execute(
            "SELECT 1 FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchone()
        # Simulate upgrading a database created before the watermark table.
        reopened._db.execute("DROP TABLE assistant_reminder_terminals")
        reopened._db.commit()
    finally:
        reopened._db.close()

    migrated = AssistantStore(path)
    try:
        migrated.sync_source("reminders", [item],
                             diagnostics={"snapshot_started_at": before_delete + .2})
        assert not migrated._db.execute(
            "SELECT 1 FROM commitments WHERE source='reminders' AND source_id='ek-123'"
        ).fetchone()
        assert migrated._db.execute(
            "SELECT status FROM assistant_reminder_terminals WHERE source_id='ek-123'"
        ).fetchone()["status"] == "dismissed"
    finally:
        migrated._db.close()


@pytest.mark.asyncio
async def test_confirmed_failure_gets_one_new_generation_then_unknown_blocks(tmp_path: Path):
    store = AssistantStore(tmp_path / "assistant.sqlite")
    hub = Hub(store)
    queue = hub.subscribe()
    args = {"title": "Take medicine", "due_ts": 2_000_000_000.0,
            "commitment_kind": "reminder"}
    try:
        with patch.object(outbox, "hub", hub):
            first = asyncio.create_task(outbox.request("create_reminder", args, timeout=1))
            event = await asyncio.wait_for(queue.get(), 1)
            row = store.event_by_key("action:" + event["action_id"])
            claim = store.claim_calendar_action(row["id"], event["type"],
                                                event["action_id"], row["payload"])
            failed = {"ok": False, "status": "failed", "error": "access denied before write"}
            store.complete_calendar_action(row["id"], event["type"], claim["claim_token"], failed)
            outbox.complete(event["action_id"], failed)
            assert await first == failed
            second = asyncio.create_task(outbox.request("create_reminder", args, timeout=.1))
            retry_event = await asyncio.wait_for(queue.get(), 1)
            assert retry_event["action_id"] == event["action_id"] + ":1"
            retry_row = store.event_by_key("action:" + retry_event["action_id"])
            store.claim_calendar_action(retry_row["id"], retry_event["type"],
                                        retry_event["action_id"], retry_row["payload"])
            assert (await second)["status"] == "unknown"
            assert (await outbox.request("create_reminder", args, timeout=.1))["status"] == "unknown"
            assert queue.empty()
            assert len(store.reminder_actions_by_prefix(event["action_id"])) == 2
    finally:
        hub.unsubscribe(queue)
        store._db.close()


@pytest.mark.asyncio
async def test_unknown_http_receipt_waits_for_positive_reconciliation(tmp_path: Path):
    from service import main

    store = AssistantStore(tmp_path / "assistant.sqlite")
    p = payload("create_reminder")
    row = store.enqueue_event(p, dedupe_key="action:a1", target={"type": "verified_reminder"},
                              expires_at=time.time() + 45)
    claim = store.claim_calendar_action(row["id"], "create_reminder", "a1", p)
    fut = asyncio.get_running_loop().create_future()
    outbox._pending["a1"] = fut
    try:
        with patch.object(main, "assistant_store", store):
            async with AsyncClient(transport=ASGITransport(app=main.app),
                                   base_url="http://fixture.invalid") as client:
                envelope = {"event_id": row["id"], "action_id": "a1",
                            "kind": "create_reminder", "claim_token": claim["claim_token"]}
                first = await client.post("/assistant/action_result", json={**envelope,
                    "result": {"ok": False, "status": "unknown", "error": "native reply lost"}})
                assert first.status_code == 200 and first.json()["delivered"] is False
                assert not fut.done()
                second = await client.post(f"/assistant/events/{row['id']}/reconcile_reminder",
                                           json={k: v for k, v in envelope.items() if k != "event_id"}
                                                | {"result": success("create_reminder")})
                assert second.status_code == 200
                assert fut.result() == success("create_reminder")
    finally:
        outbox._pending.pop("a1", None)
        store._db.close()


@pytest.mark.asyncio
async def test_collapsed_multiple_native_ids_stop_before_partial_group_write(tmp_path: Path):
    from service.tools import assistant_tools, schedule_extras

    store = AssistantStore(tmp_path / "assistant.sqlite")
    due = time.time() + 7200
    store.sync_source("reminders", [
        {"source_id": source_id, "kind": "reminder", "title": "Call dentist", "when_ts": due}
        for source_id in ("ek-one", "ek-two")])
    selected = next(row for row in store.upcoming(days=2) if row["title"] == "Call dentist")
    future = datetime.fromtimestamp(due + 3600).strftime("%Y-%m-%dT%H:%M")
    native = AsyncMock()
    try:
        with patch.object(assistant_tools, "assistant_store", store), \
             patch.object(schedule_extras, "_store", store), \
             patch.object(outbox, "request", native):
            assert "several native reminders" in (await assistant_tools.update_reminder(
                title="Call dentist", when_iso=future)).lower()
            assert "several native reminders" in (await schedule_extras.complete_reminder(
                "Call dentist")).lower()
            assert "several native records" in (await assistant_tools._retire(selected)).lower()
            native.assert_not_awaited()
    finally:
        store._db.close()
