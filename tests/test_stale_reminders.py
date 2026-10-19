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


def test_recently_overdue_dated_reminders_are_untouched(store):
    """Under a week past due, a dated reminder is ordinary overdue work."""
    recent = {"source_id": "d1", "kind": "reminder", "title": "Recently due",
              "when_ts": NOW - 3 * DAY_S}
    _sync(store, [_row("s", "Ancient idea", STALE)], events=[recent])
    assert any(r["title"] == "Recently due" for r in store.history(days=60))


# --- the question -----------------------------------------------------------------------

def test_asks_are_oldest_first_and_only_in_the_window(store):
    _standard(store)
    asks = store.stale_reminder_asks(NOW)
    assert [a["source_id"] for a in asks] == ["a2", "a"]
    assert asks[1] == {"kind": "stale_reminder", "reason": "undated", "source_id": "a",
                       "title": "Ask about this", "context": "Reminders", "created_ts": ASK,
                       "due_ts": None, "age_days": 9, "actions": ["keep", "delete"]}


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


def _native_request():
    """The app's own request: JSON, no Origin header."""
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(b"content-type", b"application/json")]})


def _delete_with_receipt(store, source_id, receipt_for):
    """Drive the real endpoint through a fixture hub, answering as the app would."""
    async def run():
        fixture_hub = Hub(store)
        queue = fixture_hub.subscribe()
        try:
            with patch.object(outbox, "hub", fixture_hub):
                from service.assistant import stale_reminders_api
                pending = asyncio.create_task(
                    stale_reminders_api.delete(stale_reminders_api.Answer(source_id=source_id),
                                               _native_request()))
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


# --- hardening (audit notes on #279) ---------------------------------------------------

# The same cases tests/RemindersPolicyChecks.swift pins for RemindersWriter.undatedTitle.
# The app sends titles in this form, so the backend must leave every one unchanged, or the
# title it hands back for a Delete would no longer match the reminder.
SWIFT_UNDATED_TITLES = ["Buy milk", "(untitled)", "Idea", "a" * 500, "a" * 499,
                        "é" * 250]


def test_backend_keeps_the_apps_normalised_titles_unchanged():
    rows = [_row(f"t{i}", title) for i, title in enumerate(SWIFT_UNDATED_TITLES)]
    snap = parse_undated_snapshot(rows, len(rows))
    assert [r[1] for r in snap.rows] == SWIFT_UNDATED_TITLES


def test_padded_title_round_trips_into_the_delete_payload(store, monkeypatch):
    """Before #279's follow-up the store kept "Call bank" while Reminders held " Call bank ",
    so every Delete failed. The app now sends the normalised title and compares against it."""
    _sync(store, [_row("p", "Call bank", ASK)])
    request = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "",
                                      "source_id": "p", "is_absent": True})
    monkeypatch.setattr(outbox, "request", request)
    assert TestClient(main.app).post("/assistant/stale_reminders/delete",
                                     json={"source_id": "p"}).status_code == 200
    assert request.await_args.args[1]["expected_title"] == "Call bank"


def test_existing_database_without_creation_dates_migrates(tmp_path):
    import sqlite3
    path = tmp_path / "assistant.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE assistant_undated_reminders (source_id TEXT PRIMARY KEY, "
                "title TEXT NOT NULL, context TEXT, synced_at REAL NOT NULL)")
    old.execute("INSERT INTO assistant_undated_reminders VALUES ('o','Old row','Reminders',1)")
    old.commit()
    old.close()
    upgraded = AssistantStore(path)
    try:
        cols = {r["name"] for r in upgraded._db.execute("PRAGMA table_info(assistant_undated_reminders)")}
        assert "created_ts" in cols
        assert upgraded.stale_reminder_target("o")["stale_state"] == "unknown"
        assert [r["title"] for r in upgraded.undated_reminders()] == ["Old row"]
        upgraded.keep_stale_reminder("o", "Old row")  # the decisions table exists
    finally:
        upgraded._db.close()
    # An older build's insert names its columns and still works on the upgraded file.
    again = sqlite3.connect(path)
    again.execute("INSERT INTO assistant_undated_reminders(source_id,title,context,synced_at) "
                  "VALUES ('n','New row',NULL,2)")
    again.commit()
    again.close()


def _undated_delete_event(store, source_id, title, action_id):
    payload = {"type": "delete_reminder", "action_id": action_id, "source_id": source_id,
               "expected_title": title, "expected_due_ts": None}
    row = store.enqueue_event(payload, dedupe_key="action:" + action_id,
                              target={"type": "verified_reminder"}, expires_at=time.time() + 45)
    claim = store.claim_calendar_action(row["id"], "delete_reminder", action_id, payload)
    assert claim["execute"] is True
    return row, claim


def _absent(source_id):
    return {"ok": True, "status": "succeeded", "error": "", "source_id": source_id, "is_absent": True}


@pytest.mark.asyncio
async def test_undated_delete_receipt_through_the_http_endpoint(store):
    from httpx import ASGITransport, AsyncClient
    _standard(store)
    row, claim = _undated_delete_event(store, "a", "Ask about this", "reminder:http-a")
    fut = asyncio.get_running_loop().create_future()
    outbox._pending["reminder:http-a"] = fut
    try:
        async with AsyncClient(transport=ASGITransport(app=main.app),
                               base_url="http://fixture.invalid") as client:
            response = await client.post("/assistant/action_result", json={
                "event_id": row["id"], "action_id": "reminder:http-a", "kind": "delete_reminder",
                "claim_token": claim["claim_token"], "result": _absent("a")})
        assert response.status_code == 200 and response.json()["recorded"] is True
        assert fut.result() == _absent("a"), "a verified delete is delivered, not held for readback"
    finally:
        outbox._pending.pop("reminder:http-a", None)
    assert store.stale_reminder_target("a") is None
    view = store.undated_view()
    assert (view["total"], view["stored"], view["stale_hidden"]) == (2, 2, 2)


def test_delayed_older_receipt_does_not_count_twice(store):
    _standard(store)
    older, older_claim = _undated_delete_event(store, "a", "Ask about this", "reminder:older")
    newer, newer_claim = _undated_delete_event(store, "a", "Ask about this", "reminder:older:1")
    store.complete_calendar_action(newer["id"], "delete_reminder", newer_claim["claim_token"], _absent("a"))
    before = store._db.execute("SELECT total,stored FROM assistant_undated_state").fetchone()
    store.complete_calendar_action(older["id"], "delete_reminder", older_claim["claim_token"], _absent("a"))
    after = store._db.execute("SELECT total,stored FROM assistant_undated_state").fetchone()
    assert tuple(before) == tuple(after), "the late receipt resolves its action without a second decrement"
    assert store.event(older["id"])["result"] is not None


def test_a_read_from_before_the_delete_cannot_bring_it_back(store):
    _standard(store)
    started_before = float(next(_clock))
    row, claim = _undated_delete_event(store, "a", "Ask about this", "reminder:resync")
    store.complete_calendar_action(row["id"], "delete_reminder", claim["claim_token"], _absent("a"))
    listing = [_row("f", "Fresh errand", FRESH), _row("a", "Ask about this", ASK),
               _row("a2", "Older ask", OLDER_ASK), _row("s", "Ancient idea", STALE),
               _row("u", "No creation date")]
    stale_read = parse_undated_snapshot(listing, len(listing))
    store.sync_source("reminders", [], diagnostics={"authorized": True,
                                                     "snapshot_started_at": started_before},
                      undated=stale_read)
    assert store.stale_reminder_target("a") is None
    assert [a["source_id"] for a in store.stale_reminder_asks(NOW)] == ["a2"], "only the deleted row is skipped"
    raw = store._db.execute("SELECT total,stored FROM assistant_undated_state").fetchone()
    assert tuple(raw) == (4, 4), "the skipped row is out of both counts"
    # A read that began after the receipt is believed: a Recently Deleted restore keeps its ID.
    store.sync_source("reminders", [], diagnostics={"authorized": True,
                                                     "snapshot_started_at": time.time() + 1},
                      undated=parse_undated_snapshot(listing, len(listing)))
    assert store.stale_reminder_target("a") is not None


# --- overdue dated reminders (2026-10-18: "Get doordash order", 16 days overdue) --------

from service.assistant.stale_reminders import classify_overdue  # noqa: E402


def _dated(sid, title, due):
    return {"source_id": sid, "kind": "reminder", "title": title, "when_ts": due,
            "context": "Reminders"}


def _overdue(store):
    _sync(store, [_row("f", "Fresh errand", FRESH)], events=[
        _dated("o-recent", "Recently due", NOW - 3 * DAY_S),
        _dated("o-ask", "Go to club meeting", NOW - 9 * DAY_S),
        _dated("o-stale", "Get doordash order", NOW - 16 * DAY_S),
        _dated("o-future", "Dentist", NOW + 2 * DAY_S),
    ])


def test_classify_overdue_boundaries():
    assert classify_overdue(NOW - ASK_AFTER_S + 1, None, NOW) == "fresh"
    assert classify_overdue(NOW - ASK_AFTER_S, None, NOW) == "ask"
    assert classify_overdue(NOW - ASK_UNTIL_S, None, NOW) == "stale"
    assert classify_overdue(NOW + DAY_S, None, NOW) == "fresh", "not due yet"
    assert classify_overdue(NOW - 40 * DAY_S, "keep", NOW) == "kept"
    assert classify_overdue(None, None, NOW) == "unknown"


def test_week_old_overdue_reminders_leave_every_store_reader(store):
    _overdue(store)
    titles = lambda items: {c["title"] for c in items}  # noqa: E731
    assert titles(store.history(days=60)) == {"Recently due"}
    assert "Get doordash order" not in titles(store.active_between())
    assert "Go to club meeting" not in titles(store.active_between())
    assert {"Recently due", "Dentist"} <= titles(store.active_between())
    assert "Get doordash order" not in titles(store.recently_added(0))
    # An explicit removal still reaches them.
    assert {"Go to club meeting", "Get doordash order"} <= titles(store.history(days=60, include_stale=True))


def test_a_wisp_twin_of_a_hidden_reminder_is_hidden_with_it(store):
    _overdue(store)
    twin = store.add_manual("Get doordash order", NOW - 16 * DAY_S)
    assert twin["id"] in store.hidden_commitment_ids(NOW)
    assert "Get doordash order" not in {c["title"] for c in store.active_between()}


def test_hidden_set_helper_covers_undated_and_overdue(store):
    _sync(store, [_row("a", "Ask about this", ASK), _row("f", "Fresh errand", FRESH)],
          events=[_dated("o-stale", "  Get  DoorDash order ", NOW - 16 * DAY_S)])
    hidden = {(r["source_id"], r["reason"], r["stale_state"]) for r in store.hidden_reminders(NOW)}
    assert hidden == {("a", "undated", "ask"), ("o-stale", "overdue", "stale")}
    assert store.hidden_reminder_titles(NOW) == {"ask about this", "get doordash order"}


def test_overdue_asks_carry_their_reason_and_days_overdue(store):
    _overdue(store)
    asks = store.stale_reminder_asks(NOW)
    assert [(a["source_id"], a["reason"]) for a in asks] == [("o-ask", "overdue")]
    assert asks[0]["due_ts"] == NOW - 9 * DAY_S and asks[0]["created_ts"] is None
    assert asks[0]["age_days"] == 9
    body = TestClient(main.app).get("/assistant/stale_reminders").json()
    assert [a["source_id"] for a in body["asks"]] == ["o-ask"] and body["hidden"] == 2


def test_keep_restores_an_overdue_reminder_and_survives_undated_pruning(store):
    _overdue(store)
    client = TestClient(main.app)
    response = client.post("/assistant/stale_reminders/keep", json={"source_id": "o-stale"})
    assert response.status_code == 200 and response.json()["reason"] == "overdue"
    assert "Get doordash order" in {c["title"] for c in store.history(days=60)}
    _overdue(store)  # a complete undated read prunes Keeps, but not this one
    assert store.stale_reminder_target("o-stale")["stale_state"] == "kept"
    assert client.post("/assistant/stale_reminders/keep",
                       json={"source_id": "o-recent"}).status_code == 409


def test_overdue_delete_sends_the_exact_due_time(store, monkeypatch):
    _overdue(store)
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(outbox, "request", request)
    response = TestClient(main.app).post("/assistant/stale_reminders/delete", json={"source_id": "o-ask"})
    assert response.status_code == 200 and response.json()["reason"] == "overdue"
    kind, payload = request.await_args.args
    assert kind == "delete_reminder"
    assert payload == {"source_id": "o-ask", "expected_title": "Go to club meeting",
                       "expected_due_ts": NOW - 9 * DAY_S}


def test_verified_overdue_delete_retires_the_row(store):
    _overdue(store)
    payload = {"type": "delete_reminder", "action_id": "reminder:o", "source_id": "o-stale",
               "expected_title": "Get doordash order", "expected_due_ts": NOW - 16 * DAY_S}
    row = store.enqueue_event(payload, dedupe_key="action:reminder:o",
                              target={"type": "verified_reminder"}, expires_at=time.time() + 45)
    claim = store.claim_calendar_action(row["id"], "delete_reminder", "reminder:o", payload)
    store.complete_calendar_action(row["id"], "delete_reminder", claim["claim_token"], _absent("o-stale"))
    assert store.stale_reminder_target("o-stale") is None
    assert "Get doordash order" not in {c["title"] for c in store.history(days=60, include_stale=True)}


def test_an_ambiguous_native_id_is_hidden_but_never_offered(store):
    _sync(store, [], events=[_dated("dup", "Repeat", NOW - 9 * DAY_S),
                             _dated("dup", "Repeat", NOW - 10 * DAY_S)])
    assert store.stale_reminder_asks(NOW) == []
    assert store.stale_reminder_target("dup") is None
    assert "Repeat" not in {c["title"] for c in store.history(days=60)}


def test_reminder_tools_hide_but_removals_reach(store):
    _overdue(store)
    listed = {c["title"] for c in assistant_tools.reminders_matching("all")}
    assert "Get doordash order" not in listed and "Recently due" in listed
    assert "Get doordash order" in {c["title"] for c in assistant_tools.reminders_matching("all", include_stale=True)}
    assert "Get doordash order" in {c["title"] for c in assistant_tools.past_due_matching()}


def test_task_evidence_drops_hidden_overdue_rows(store, monkeypatch):
    from service.tools import task_evidence
    _overdue(store)
    result = task_evidence._native_source("reminders", now=NOW, start=NOW - DAY_S, end=NOW + DAY_S,
                                          query="", include_overdue=True, include_undated=False)
    assert result["matching_dated"] == 1, "only the 3-day-overdue row is evidence"
    assert result["dated_window_cached"] == 1


# --- audit B1/N1 on #286: removals reach hidden reminders; a kept one never vanishes ----

def _typed_turn(store, prompt):
    from datetime import datetime
    from service.memory.store import SessionStore
    from service.tasks.engine import prepare_task_turn
    sessions = SessionStore(Path(tempfile.mkdtemp(prefix="wisp-stale-typed-")) / "sessions.db")
    try:
        return prepare_task_turn(sessions, sessions.create_session(), prompt,
                                 assistant_store=store, now=datetime.fromtimestamp(NOW))
    finally:
        sessions._db.close()


def test_typed_delete_of_overdue_reminders_reaches_hidden_ones(store):
    _overdue(store)
    turn = _typed_turn(store, "delete my overdue reminders")
    assert turn is not None and turn.executable, getattr(turn, "event", None)
    titles = {t["title"] for t in turn.plan.resolved_targets}
    assert {"Get doordash order", "Go to club meeting", "Recently due"} <= titles
    assert turn.plan.steps[0].tool == "clear_reminders"


def test_typed_delete_and_reschedule_by_name_reach_a_hidden_reminder(store):
    _overdue(store)
    deleting = _typed_turn(store, "delete the doordash reminder")
    assert deleting is not None and deleting.executable, getattr(deleting, "event", None)
    assert [t["title"] for t in deleting.plan.resolved_targets] == ["Get doordash order"]
    moving = _typed_turn(store, "move the doordash reminder to tomorrow at 9am")
    assert moving is not None and moving.event != "target_not_found"


def test_clear_reminders_and_its_preview_include_hidden_reminders(store):
    _overdue(store)
    preview = assistant_tools.reminders_matching("past_due", include_stale=True)
    assert "Get doordash order" in {c["title"] for c in preview}
    import inspect
    from service.agent import loop
    source = inspect.getsource(loop)
    assert source.count("reminders_matching(_scope,") == 2
    assert source.count("include_stale=True)") >= 2, "both confirmation previews read like the tool"
    assert "include_stale=True" in inspect.getsource(assistant_tools.clear_reminders)


def test_a_kept_reminder_is_not_hidden_by_a_same_titled_unanswered_one(store):
    due = NOW - 16 * DAY_S
    _sync(store, [], events=[_dated("kept", "Call mom", due), _dated("other", "Call mom", due)])
    store.keep_stale_reminder("kept", "Call mom")
    hidden = store.hidden_commitment_ids(NOW)
    kept_row = store._db.execute("SELECT id FROM commitments WHERE source_id='kept'").fetchone()["id"]
    assert kept_row not in hidden, "another Apple reminder is never a twin"
    assert "Call mom" in {c["title"] for c in store.history(days=60)}


def test_hidden_ids_take_one_twin_query(store):
    _sync(store, [], events=[_dated(f"o{i}", f"Old {i}", NOW - (15 + i) * DAY_S) for i in range(50)])
    statements = []
    store._db.set_trace_callback(statements.append)
    try:
        hidden = store.hidden_commitment_ids(NOW)
    finally:
        store._db.set_trace_callback(None)
    assert len(hidden) == 50
    assert sum("source<>'reminders'" in s for s in statements) == 1


# --- Day tasks that duplicate a hidden reminder (2026-10-18: task 8cfed052, made by the
# old Plan time from "Get doordash order" before #289, still showed on the Day plan and in
# attention as "On your day plan." after the reminder itself was hidden) ---------------

from datetime import datetime, timezone as _tz  # noqa: E402

from service.assistant import today_api  # noqa: E402


def _day_task(store, title, *, origin=None, legacy=False):
    values = {"title": title, "day": datetime.fromtimestamp(NOW, _tz.utc).date().isoformat(),
              "timezone": "UTC", "duration_minutes": 30}
    if origin is not None:
        values["origin_ts"] = origin
    task = store.today_save_task(values)
    if legacy:  # saved before today_tasks.created_at existed
        with store._db:
            store._db.execute("UPDATE today_tasks SET created_at=NULL WHERE id=?", (task["id"],))
    return task


def _today(store, monkeypatch):
    monkeypatch.setattr(today_api, "assistant_store", store)
    day = datetime.fromtimestamp(NOW, _tz.utc).date().isoformat()
    body = TestClient(main.app).get("/assistant/today", params={"day": day, "timezone": "UTC"}).json()
    planned = {t["id"] for t in body["tasks"]}
    attention = {i["title"] for i in body["workspace"]["attention"] if i["source"] == "wisp"}
    return planned, attention


def test_a_day_task_twin_of_a_hidden_reminder_leaves_the_plan_and_attention(store, monkeypatch):
    _overdue(store)
    legacy = _day_task(store, "Get doordash order", legacy=True)          # the 8cfed052 case
    review = _day_task(store, "Review: get  DoorDash order", legacy=True)
    from_plan_time = _day_task(store, "Get doordash order", origin=NOW - 16 * DAY_S)
    other = _day_task(store, "Write report")
    planned, attention = _today(store, monkeypatch)
    assert other["id"] in planned and "Write report" in attention
    assert not {legacy["id"], review["id"], from_plan_time["id"]} & planned
    assert not any("doordash" in t.casefold() for t in attention)
    from service.assistant import daily_workspace
    day = datetime.fromtimestamp(NOW, _tz.utc).date().isoformat()
    alone = daily_workspace.workspace(store, day, "UTC", now=NOW)["attention"]
    assert not any("doordash" in i["title"].casefold() for i in alone if i["source"] == "wisp")
    # Nothing was deleted, and a Keep on the reminder brings them all back.
    assert store.today_task(legacy["id"])["status"] == "active"
    assert TestClient(main.app).post("/assistant/stale_reminders/keep",
                                     json={"source_id": "o-stale"}).status_code == 200
    planned, attention = _today(store, monkeypatch)
    assert {legacy["id"], review["id"], from_plan_time["id"]} <= planned
    assert "Get doordash order" in attention


def test_a_day_task_planned_after_the_reminder_was_hidden_stays(store, monkeypatch):
    _overdue(store)   # "Get doordash order" hidden since 9 days ago
    typed_today = _day_task(store, "Get doordash order")                   # created now
    recent_origin = _day_task(store, "Get doordash order", origin=NOW - DAY_S, legacy=True)
    planned, attention = _today(store, monkeypatch)
    assert {typed_today["id"], recent_origin["id"]} <= planned
    assert "Get doordash order" in attention


def test_today_tasks_created_at_migrates_and_is_set_once(tmp_path):
    import sqlite3
    path = tmp_path / "assistant.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE today_tasks (id TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL, "
               "revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)")
    db.commit()
    db.close()
    s = AssistantStore(path)
    try:
        task = _day_task(s, "Write report")
        first = s._db.execute("SELECT created_at FROM today_tasks WHERE id=?", (task["id"],)).fetchone()[0]
        assert first is not None
        s.today_save_task({"title": "Write the report"}, task_id=task["id"], revision=1)
        again = s._db.execute("SELECT created_at FROM today_tasks WHERE id=?", (task["id"],)).fetchone()[0]
        assert again == first, "an edit or carry keeps the original save time"
    finally:
        s._db.close()


def test_the_evening_check_in_card_hides_the_same_task_twin(store):
    from service.assistant import checkin
    _overdue(store)
    legacy = _day_task(store, "Get doordash order", legacy=True)
    other = _day_task(store, "Write report")
    day = datetime.fromtimestamp(NOW, _tz.utc).date().isoformat()
    card = checkin.build(store, day, "UTC", now=NOW)
    shown = {t["id"] for t in card["tasks"]}
    assert other["id"] in shown and legacy["id"] not in shown



# --- Keep/Delete answer only the app, never a browser page (2026-10-18) -------------------

@pytest.mark.parametrize("action", ["keep", "delete"])
def test_answers_from_a_browser_page_are_refused(store, monkeypatch, action):
    _standard(store)
    request = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(outbox, "request", request)
    client = TestClient(main.app)
    url = f"/assistant/stale_reminders/{action}"
    for origin in ("https://example.invalid", "null", "http://localhost:8765"):
        page = client.post(url, json={"source_id": "a"}, headers={"Origin": origin})
        assert page.status_code == 403, origin
    form = client.post(url, content=b'{"source_id": "a"}', headers={"Content-Type": "text/plain"})
    assert form.status_code in (415, 422)
    request.assert_not_awaited()
    assert store.stale_reminder_target("a")["stale_state"] == "ask", "nothing was kept or deleted"
    # The app's own request (JSON, no Origin) still answers.
    assert client.post("/assistant/stale_reminders/keep", json={"source_id": "a"}).status_code == 200
