"""Old undated reminders: ask once around a week, then stop using them.

The user's rule: an undated reminder about a week old gets one question (Keep / Delete);
anything older that was not kept is neither mentioned nor used. Ages come from the
Reminders creation date; an unknown creation date is never stale. Dated reminders are
untouched. Delete removes the reminder from Reminders.app only through the verified
native path, and only on an explicit click.

Synthetic stores and a fixture hub only; no EventKit, no live backend.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-stale-reminders-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ["HOME"] = _scratch.name

from fastapi.testclient import TestClient  # noqa: E402

import service.main as main  # noqa: E402
from service.assistant import outbox, stale_reminders  # noqa: E402
from service.assistant import store as store_module  # noqa: E402
from service.assistant.hub import Hub  # noqa: E402
from service.assistant.stale_reminders import ASK_AFTER_S, ASK_UNTIL_S, DAY_S, classify  # noqa: E402
from service.assistant.store import AssistantStore, parse_undated_snapshot  # noqa: E402
from service.assistant.verified_reminders import validate_payload  # noqa: E402
from service.tools import assistant_tools  # noqa: E402

NOW = time.time()
FRESH = NOW - 2 * DAY_S
ASK = NOW - 9 * DAY_S
OLDER_ASK = NOW - 12 * DAY_S
STALE = NOW - 40 * DAY_S


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(main, "assistant_store", s)
    monkeypatch.setattr(assistant_tools, "assistant_store", s)
    monkeypatch.setattr(store_module, "assistant_store", s)
    yield s
    s._db.close()


_clock = iter(range(1_000, 1_000_000))


def _sync(store, undated, *, total=None, events=()):
    """One authoritative native read, as the app posts it."""
    started = float(next(_clock))
    snapshot = parse_undated_snapshot(undated, len(undated) if total is None else total)
    assert snapshot is not None
    store.sync_source("reminders", list(events),
                      diagnostics={"authorized": True, "count": len(events),
                                   "snapshot_started_at": started},
                      undated=snapshot)


def _row(sid, title, created=None):
    row = {"source_id": sid, "title": title, "context": "Reminders"}
    if created is not None:
        row["created_ts"] = created
    return row


def _standard(store):
    _sync(store, [_row("f", "Fresh errand", FRESH), _row("a", "Ask about this", ASK),
                  _row("a2", "Older ask", OLDER_ASK), _row("s", "Ancient idea", STALE),
                  _row("u", "No creation date")])


def _titles(rows):
    return sorted(r["title"] for r in rows)


# --- policy -----------------------------------------------------------------------------

def test_classify_boundaries():
    assert classify(NOW - ASK_AFTER_S + 1, None, NOW) == "fresh"
    assert classify(NOW - ASK_AFTER_S, None, NOW) == "ask"
    assert classify(NOW - ASK_UNTIL_S + 1, None, NOW) == "ask"
    assert classify(NOW - ASK_UNTIL_S, None, NOW) == "stale"
    assert classify(None, None, NOW) == "unknown"
    assert classify(STALE, "keep", NOW) == "kept"
    assert classify(NOW + 5 * DAY_S, None, NOW) == "fresh", "clock skew reads as fresh"
    for bad in (True, "123", float("nan"), float("inf"), 0, -5):
        assert classify(bad, None, NOW) == "unknown", bad


def test_snapshot_keeps_creation_dates_and_drops_bad_ones():
    snap = parse_undated_snapshot(
        [_row("a", "A", 1_700_000_000.0), _row("b", "B", True), _row("c", "C", "yesterday"),
         _row("d", "D", float("inf")), _row("e", "E")], 5)
    assert snap is not None, "a bad creation date is no reason to reject the read"
    assert [r[3] for r in snap.rows] == [1_700_000_000.0, None, None, None, None]


# --- exclusion: every reader goes through the store ------------------------------------

def test_store_hides_ask_and_stale_but_keeps_fresh_and_unknown(store):
    _standard(store)
    assert _titles(store.undated_reminders()) == ["Fresh errand", "No creation date"]
    view = store.undated_view()
    assert view["stale_hidden"] == 3
    assert (view["total"], view["stored"]) == (2, 2), "counts describe only what readers see"
    assert store.undated_reminders("ancient") == []


def test_reminder_search_and_day_listing_never_mention_hidden_items(store):
    _standard(store)
    section, caveat = assistant_tools._undated_report("", "all")
    assert "Fresh errand" in section and "No creation date" in section
    for hidden in ("Ask about this", "Older ask", "Ancient idea"):
        assert hidden not in section and hidden not in caveat
    view, rows = store.undated_listing()
    assert _titles(rows) == ["Fresh errand", "No creation date"] and view["status"] == "complete"
    raw = store.raw_reminder_list_snapshot()
    assert {r["title"] for r in raw["records"]} == {"Fresh errand", "No creation date"}


def test_no_reader_bypasses_the_store_filter():
    """The filter lives in AssistantStore.undated_reminders. A module that queried the
    table itself would quietly bring stale reminders back into briefs or prompts."""
    root = Path(__file__).resolve().parents[1] / "service"
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py")
                 if p.name != "store.py" and re.search(r"assistant_undated_reminders", p.read_text())]
    assert offenders == []


def test_dated_reminders_are_untouched(store):
    old_dated = {"source_id": "d1", "kind": "reminder", "title": "Old dated",
                 "when_ts": NOW - 30 * DAY_S}
    _sync(store, [_row("s", "Ancient idea", STALE)], events=[old_dated])
    assert any(r["title"] == "Old dated" for r in store.history(days=60))


# --- the question -----------------------------------------------------------------------

def test_asks_are_oldest_first_and_only_in_the_window(store):
    _standard(store)
    asks = store.stale_reminder_asks(NOW)
    assert [a["source_id"] for a in asks] == ["a2", "a"]
    assert asks[1] == {"kind": "stale_reminder", "source_id": "a", "title": "Ask about this",
                       "context": "Reminders", "created_ts": ASK, "age_days": 9,
                       "actions": ["keep", "delete"]}


def test_no_asks_from_a_list_that_is_not_current(store):
    _standard(store)
    store.sync_source("reminders", [], diagnostics={"authorized": True,
                                                     "snapshot_started_at": float(next(_clock))})
    assert store.undated_view()["freshness"] == "stale"
    assert store.stale_reminder_asks(NOW) == []


def test_keep_is_permanent_and_brings_the_reminder_back(store):
    _standard(store)
    client = TestClient(main.app)
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "a"}).status_code == 200
    assert "Ask about this" in _titles(store.undated_reminders())
    assert [a["source_id"] for a in store.stale_reminder_asks(NOW)] == ["a2"]
    # Still kept after another sync and after a restart.
    _standard(store)
    reopened = AssistantStore(Path(store._db.execute("PRAGMA database_list").fetchone()[2]))
    try:
        assert "Ask about this" in _titles(reopened.undated_reminders())
    finally:
        reopened._db.close()
    # A long-silent stale reminder can be kept too.
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "s"}).status_code == 200
    assert "Ancient idea" in _titles(store.undated_reminders())


def test_keep_refuses_fresh_unknown_and_missing(store):
    _standard(store)
    client = TestClient(main.app)
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "f"}).status_code == 409
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "u"}).status_code == 409
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "nope"}).status_code == 404
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "a", "x": 1}).status_code == 422


def test_complete_snapshot_prunes_keeps_for_vanished_reminders(store):
    _standard(store)
    store.keep_stale_reminder("a", "Ask about this")
    _sync(store, [_row("a", "Ask about this", ASK)] + [_row(f"x{i}", "x") for i in range(3)], total=10)
    assert store._db.execute("SELECT COUNT(*) FROM assistant_stale_reminder_decisions").fetchone()[0] == 1
    _sync(store, [_row("f", "Fresh errand", FRESH)])
    assert store._db.execute("SELECT COUNT(*) FROM assistant_stale_reminder_decisions").fetchone()[0] == 0


def test_get_endpoint_shape(store):
    _standard(store)
    body = TestClient(main.app).get("/assistant/stale_reminders").json()
    assert [a["source_id"] for a in body["asks"]] == ["a2", "a"]
    assert body["hidden"] == 3 and body["freshness"] == "current"
    assert body["policy"]["ask_after_days"] == 7 and body["policy"]["ask_until_days"] == 14


# --- delete -----------------------------------------------------------------------------

def test_only_delete_may_target_an_undated_reminder():
    base = {"action_id": "a1", "source_id": "ek", "expected_title": "T", "expected_due_ts": None}
    assert validate_payload("delete_reminder", {"type": "delete_reminder", **base})
    for kind in ("complete_reminder",):
        with pytest.raises(ValueError):
            validate_payload(kind, {"type": kind, **base})
    with pytest.raises(ValueError):
        validate_payload("update_reminder", {"type": "update_reminder", **base,
                                             "title": "U", "due_ts": 2_000_000_000.0})
    with pytest.raises(ValueError):
        validate_payload("delete_reminder", {"type": "delete_reminder", **base, "expected_due_ts": 0})


def _delete_with_receipt(store, source_id, receipt_for):
    """Drive the real endpoint through a fixture hub, answering as the app would."""
    async def run():
        fixture_hub = Hub(store)
        queue = fixture_hub.subscribe()
        try:
            with patch.object(outbox, "hub", fixture_hub):
                from service.assistant import stale_reminders_api
                pending = asyncio.create_task(
                    stale_reminders_api.delete(stale_reminders_api.Answer(source_id=source_id)))
                event = await asyncio.wait_for(queue.get(), 2)
                assert event["type"] == "delete_reminder"
                assert event["expected_due_ts"] is None and event["source_id"] == source_id
                row = store.event_by_key("action:" + event["action_id"])
                claim = store.claim_calendar_action(row["id"], event["type"], event["action_id"],
                                                    row["payload"])
                assert claim["execute"] is True
                receipt = receipt_for(event)
                store.complete_calendar_action(row["id"], event["type"], claim["claim_token"], receipt)
                outbox.complete(event["action_id"], receipt)
                return await pending
        finally:
            fixture_hub.unsubscribe(queue)
    return asyncio.run(run())


def test_verified_delete_removes_the_reminder_and_keeps_counts_true(store):
    _standard(store)
    result = _delete_with_receipt(store, "a", lambda e: {
        "ok": True, "status": "succeeded", "error": "", "source_id": e["source_id"], "is_absent": True})
    assert result["ok"] is True and result["decision"] == "deleted"
    assert store.stale_reminder_target("a") is None
    assert [a["source_id"] for a in store.stale_reminder_asks(NOW)] == ["a2"]
    view = store.undated_view()
    assert (view["total"], view["stored"], view["stale_hidden"]) == (2, 2, 2)
    # An identical retry reuses the verified receipt instead of acting again.
    assert store.reminder_terminal_retry_state("delete_reminder", {
        "source_id": "a", "expected_title": "Ask about this", "expected_due_ts": None}) == "desired"


def test_failed_native_delete_changes_nothing(store):
    _standard(store)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        _delete_with_receipt(store, "a", lambda e: {
            "ok": False, "status": "failed", "error": "Reminder changed since selection; nothing changed"})
    assert err.value.status_code == 502
    assert store.stale_reminder_target("a") is not None


def test_delete_refuses_fresh_and_a_list_that_is_not_current(store, monkeypatch):
    _standard(store)
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(outbox, "request", request)
    client = TestClient(main.app)
    assert client.post("/assistant/stale_reminders/delete", json={"source_id": "f"}).status_code == 409
    store.sync_source("reminders", [], diagnostics={"authorized": True,
                                                     "snapshot_started_at": float(next(_clock))})
    assert client.post("/assistant/stale_reminders/delete", json={"source_id": "a"}).status_code == 409
    request.assert_not_awaited()


def test_unknown_native_outcome_is_not_reported_as_deleted(store, monkeypatch):
    _standard(store)
    monkeypatch.setattr(outbox, "request", AsyncMock(return_value={
        "ok": False, "status": "unknown", "error": "Native reminder deletion outcome is unknown"}))
    response = TestClient(main.app).post("/assistant/stale_reminders/delete", json={"source_id": "a"})
    assert response.status_code == 409 and "unknown" in response.json()["detail"]


def test_undated_retry_state(store):
    _standard(store)
    payload = {"source_id": "a", "expected_title": "Ask about this", "expected_due_ts": None}
    assert store.reminder_terminal_retry_state("delete_reminder", payload) == "expected"
    assert store.reminder_terminal_retry_state(
        "delete_reminder", {**payload, "expected_title": "Renamed"}) == "other"
    assert store.reminder_terminal_retry_state(
        "delete_reminder", {**payload, "source_id": "gone"}) == "other", \
        "absent without a verified receipt is not proof of deletion"


def test_policy_constants_match_the_user_rule():
    assert stale_reminders.ASK_AFTER_S == 7 * DAY_S
    assert stale_reminders.ASK_UNTIL_S == 14 * DAY_S
