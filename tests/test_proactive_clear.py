"""Clear and Mark done on the proactive Brief's rows.

Clear hides a to-do or a mail/text thread (item id + fingerprint of its text and time)
across cycles and restarts; it never checks the to-do off and never writes to
Reminders, Mail or Messages. A changed row comes back. Mark done on a dated reminder's
to-do goes through schedule_extras.complete_commitment only, and the row leaves only on
its verified result.

Synthetic stores, fixture schedules and a stubbed native outbox; no live backend.
"""
from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import AsyncMock

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-clear-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from service.assistant import outbox  # noqa: E402
from service.assistant import store as assistant_store_module  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.proactive import api, cycle, sources  # noqa: E402
from service.proactive import store as store_mod  # noqa: E402
from service.proactive.prepare import todo_id  # noqa: E402
from service.proactive.store import ProactiveStore  # noqa: E402
from service.tools import schedule_extras  # noqa: E402

NOW = time.time()
TZ = "UTC"
ME = "me@example.com"
TITLE = "Wisp verification timed 2113"


def sched(due=NOW - 2 * 86400, title=TITLE):
    return {"calendar": [dict(id="ev1", title="Standup", when_ts=NOW + 3600, all_day=False, account=ME)],
            "reminders": [dict(id="c-rem", title=title, when_ts=due, all_day=False, source="reminders", account=ME),
                          dict(id="undated:u1", title="Someday idea", when_ts=None, all_day=False,
                               source="reminders", account=ME)]}


THREAD = dict(id="thread:t1", source="messages", account=ME, who="Mom", title="Dinner Sunday?",
              summary="Mom asked about dinner on Sunday.", needs_reply=True, received_ts=NOW - 600)


def snap(store, schedule=None, threads=None):
    store.save_state(threads=[THREAD] if threads is None else threads, as_of=NOW)
    return cycle.snapshot(now=NOW, tz=TZ, store=store, schedule_fn=lambda n, z: schedule or sched(),
                          own=ME, linked=set())


@pytest.fixture()
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


def by_title(view, title):
    return next((t for t in view["todos"] if t["title"] == title), None)


def test_rows_carry_fingerprints_and_only_dated_reminders_can_be_completed(store):
    view = snap(store)
    rem, event, undated = by_title(view, TITLE), by_title(view, "Standup"), by_title(view, "Someday idea")
    assert rem["fingerprint"] == cycle.item_fingerprint(TITLE, NOW - 2 * 86400)
    assert rem.get("can_complete") is True
    assert "can_complete" not in event and "can_complete" not in undated
    assert all(len(t["fingerprint"]) == 24 for t in view["todos"])
    assert view["threads"][0]["fingerprint"] == cycle.item_fingerprint("Dinner Sunday?", NOW - 600)


def test_a_cleared_todo_stays_hidden_across_cycles_and_restarts(store, tmp_path):
    rem = by_title(snap(store), TITLE)
    store.clear_item(rem["id"], rem["fingerprint"])
    assert by_title(snap(store), TITLE) is None
    assert not store.is_done(rem["id"]), "Clear never checks the to-do off"
    store.save_state(cards=[], todos=[], as_of=NOW + 3600)  # another cycle's output
    assert by_title(snap(store), TITLE) is None
    reopened = ProactiveStore(tmp_path / "proactive")      # a restart
    assert by_title(snap(reopened), TITLE) is None
    assert by_title(snap(reopened), "Standup") is not None, "only the cleared row goes"


def test_a_changed_reminder_comes_back(store):
    rem = by_title(snap(store), TITLE)
    store.clear_item(rem["id"], rem["fingerprint"])
    moved = by_title(snap(store, sched(due=NOW + 3 * 3600)), TITLE)
    assert moved is not None and moved["id"] == rem["id"]


def test_a_cleared_thread_hides_until_a_new_message(store):
    thread = snap(store)["threads"][0]
    store.clear_item(thread["id"], thread["fingerprint"])
    assert snap(store)["threads"] == []
    newer = dict(THREAD, received_ts=NOW - 60, title="Dinner Sunday? Bring dessert")
    assert [t["id"] for t in snap(store, threads=[newer])["threads"]] == ["thread:t1"]


# --- the endpoints -----------------------------------------------------------------------

@pytest.fixture()
def client(store, tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(assistant_store_module, "assistant_store", s)
    monkeypatch.setattr(schedule_extras, "_store", s)
    monkeypatch.setattr(store_mod, "_store", store)
    real = cycle.snapshot
    monkeypatch.setattr(cycle, "snapshot", lambda account=None, **_: real(
        now=NOW, tz=TZ, store=store, own=ME, linked=set(), account=account,
        schedule_fn=lambda n, z: sources.schedule(s, n, z, own=ME, linked=set())))
    s.sync_source("reminders", [{"source_id": "ek-1", "kind": "reminder", "title": TITLE,
                                 "when_ts": NOW - 2 * 86400}],
                  diagnostics={"authorized": True, "count": 1, "snapshot_started_at": NOW})
    app = FastAPI()
    app.include_router(api.router)
    test_client = TestClient(app)
    test_client.assistant = s
    yield test_client
    s._db.close()


def _todo(client):
    view = cycle.snapshot()
    return by_title(view, TITLE)


def test_clear_todo_never_writes_to_reminders(client, store, monkeypatch):
    native = AsyncMock(side_effect=AssertionError("Clear must never write to Reminders"))
    monkeypatch.setattr(outbox, "request", native)
    monkeypatch.setattr(client.assistant, "set_status", lambda *a: (_ for _ in ()).throw(AssertionError("set_status")))
    todo = _todo(client)
    assert todo is not None and todo["can_complete"] is True
    response = client.post(f"/assistant/proactive/todos/{todo['id']}/clear", json={"fingerprint": todo["fingerprint"]})
    assert response.status_code == 200 and response.json()["decision"] == "cleared"
    native.assert_not_called()
    assert _todo(client) is None and not store.is_done(todo["id"])
    row = client.assistant._db.execute("SELECT status FROM commitments WHERE source_id='ek-1'").fetchone()
    assert row["status"] == "active"


def test_clear_thread_and_bad_requests(client, store):
    store.save_state(threads=[THREAD], as_of=NOW)
    thread = cycle.snapshot()["threads"][0]
    ok = client.post(f"/assistant/proactive/threads/{thread['id']}/clear", json={"fingerprint": thread["fingerprint"]})
    assert ok.status_code == 200 and cycle.snapshot()["threads"] == []
    assert client.post("/assistant/proactive/threads/x/clear", json={"fingerprint": "nope"}).status_code == 422
    assert client.post("/assistant/proactive/threads/x/clear", json={"fingerprint": "a" * 24},
                       headers={"Origin": "https://example.com"}).status_code == 403


def test_mark_done_uses_only_the_verified_complete_path(client, store, monkeypatch):
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "",
                                     "source_id": "ek-1", "is_completed": True})
    monkeypatch.setattr(outbox, "request", native)
    calls = []
    real = schedule_extras.complete_commitment

    async def spy(row, s=None):
        calls.append(row["id"])
        return await real(row, s)
    monkeypatch.setattr(schedule_extras, "complete_commitment", spy)
    todo = _todo(client)
    cid = client.assistant._db.execute("SELECT id FROM commitments WHERE source_id='ek-1'").fetchone()["id"]
    assert todo["id"] == todo_id(cid)
    response = client.post(f"/assistant/proactive/todos/{todo['id']}/done", json={"fingerprint": todo["fingerprint"]})
    assert response.status_code == 200, response.text
    assert calls == [cid]
    kind, payload = native.await_args.args
    assert kind == "complete_reminder" and payload["source_id"] == "ek-1"
    assert client.assistant._db.execute("SELECT status FROM commitments WHERE id=?", (cid,)).fetchone()["status"] == "done"
    assert _todo(client) is None, "the row leaves once completion is verified"


def test_mark_done_keeps_the_row_when_not_verified(client, store, monkeypatch):
    monkeypatch.setattr(outbox, "request", AsyncMock(return_value={"ok": False, "status": "unknown",
                                                                   "error": "native reply lost"}))
    todo = _todo(client)
    response = client.post(f"/assistant/proactive/todos/{todo['id']}/done", json={"fingerprint": todo["fingerprint"]})
    assert response.status_code == 502 and "native reply lost" in response.json()["detail"]
    assert _todo(client) is not None and not store.is_done(todo["id"])


def test_mark_done_refuses_changed_and_non_reminder_rows(client, store, monkeypatch):
    native = AsyncMock(side_effect=AssertionError("nothing to complete"))
    monkeypatch.setattr(outbox, "request", native)
    todo = _todo(client)
    stale = cycle.item_fingerprint(TITLE, NOW - 9 * 86400)
    assert client.post(f"/assistant/proactive/todos/{todo['id']}/done", json={"fingerprint": stale}).status_code == 409
    assert client.post("/assistant/proactive/todos/todo:nope/done", json={"fingerprint": stale}).status_code == 404
    native.assert_not_called()
