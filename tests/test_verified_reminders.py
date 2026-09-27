"""Synthetic contract and crash/restart checks; no EventKit or user state."""
from __future__ import annotations

from pathlib import Path
import asyncio
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
    finally:
        store._db.close()


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
