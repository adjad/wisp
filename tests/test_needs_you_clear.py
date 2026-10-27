"""Clearing a row from the Brief's Needs you list, and Mark done for a reminder.

Clear hides the row (item id + fingerprint of its text and due time) and never writes
to Reminders; a changed reminder comes back. Mark done uses only the verified completion
path (schedule_extras.complete_commitment -> the app's exact complete_reminder write).

Synthetic stores and a stubbed native outbox only; no EventKit, no live backend.
"""
from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import AsyncMock

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-needs-you-clear-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ["HOME"] = _scratch.name

from fastapi.testclient import TestClient  # noqa: E402

import service.main as main  # noqa: E402
from service.assistant import outbox  # noqa: E402
from service.assistant import store as store_module  # noqa: E402
from service.assistant.daily_workspace import (NEEDS_YOU_SHOWN, clean, key, needs_you,  # noqa: E402
                                               needs_you_fingerprint)
from service.assistant.needs_you_api import reminder_for  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import schedule_extras  # noqa: E402

NOW = time.time()
END = NOW + 6 * 3600


def reminder(title, due, ident=None):
    return dict(id=ident or key("reminders", title), source="reminders", title=title, due_ts=due,
                reason="Due in this day.")


def pick(attention, dismissed=None):
    return needs_you(attention, now=NOW, day_end=END, dismissed=dismissed)


def six():
    return [reminder(f"Thing {n}", NOW - (10 - n) * 3600) for n in range(6)]


# --- needs_you filtering ---------------------------------------------------------------

def test_items_carry_a_fingerprint_and_reminders_can_be_completed():
    result = pick([reminder("Pay rent", NOW - 3600),
                   dict(id="wisp:t1", source="wisp", title="Finish slides", due_ts=None,
                        reason="On your day plan.")])
    rent, slides = result["items"]
    assert rent["fingerprint"] == needs_you_fingerprint("Pay rent", NOW - 3600)
    assert rent.get("can_complete") is True
    assert "can_complete" not in slides, "a Day task is not a reminder to complete"
    assert slides["fingerprint"] == needs_you_fingerprint("Finish slides", None)


def test_clearing_hides_the_item_and_the_next_one_fills_the_slot():
    before = pick(six())
    assert len(before["items"]) == NEEDS_YOU_SHOWN and before["more"] == 2
    first = before["items"][0]
    after = pick(six(), {first["id"]: first["fingerprint"]})
    assert first["id"] not in [i["id"] for i in after["items"]]
    assert len(after["items"]) == NEEDS_YOU_SHOWN, "the next item takes the cleared slot"
    assert [i["text"] for i in after["items"]] == [i["text"] for i in before["items"][1:]] + ["Thing 4"]
    assert after["more"] == 1, "more counts only what is still listed"


def test_a_changed_reminder_comes_back():
    item = pick(six())["items"][0]
    cleared = {item["id"]: item["fingerprint"]}
    moved = six()[:3]
    moved[0] = dict(moved[0], due_ts=NOW - 30 * 60)  # a new due date
    assert item["id"] in [i["id"] for i in pick(moved, cleared)["items"]]
    renamed = six()[:3]
    renamed[0] = dict(renamed[0], title="Thing 0 (again)")
    assert item["id"] in [i["id"] for i in pick(renamed, cleared)["items"]]


def test_a_new_text_from_the_same_person_comes_back():
    def text(received):
        return dict(id="messages:mom", source="messages", title="Mom: hi", due_ts=None, sender="Mom",
                    person=True, received_ts=received, reason="Important conversation context")
    one = pick([text(NOW - 600)])["items"][0]
    cleared = {one["id"]: one["fingerprint"]}
    assert pick([text(NOW - 600)], cleared)["items"] == []
    # A different text has a different id, as in the service; the row is named by the newest.
    two = pick([text(NOW - 600), dict(text(NOW - 60), id="messages:mom2")], cleared)
    assert two["items"] and two["items"][0]["text"] == "2 texts from Mom"


# --- the endpoints -----------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(main, "assistant_store", s)
    monkeypatch.setattr(store_module, "assistant_store", s)
    monkeypatch.setattr(schedule_extras, "_store", s)
    yield s
    s._db.close()


def _synced(store, title="Wisp verification timed 2113", due=None):
    due = NOW - 2 * 86400 if due is None else due
    store.sync_source("reminders", [{"source_id": "ek-1", "kind": "reminder", "title": title, "when_ts": due}],
                      diagnostics={"authorized": True, "count": 1, "snapshot_started_at": NOW})
    cid = store._db.execute("SELECT id FROM commitments WHERE source='reminders' AND source_id='ek-1'").fetchone()["id"]
    return cid, key("reminders", cid), needs_you_fingerprint(clean(title), due)


def _forbid_writes(store, monkeypatch):
    native = AsyncMock(side_effect=AssertionError("Clear must never write to Reminders"))
    monkeypatch.setattr(outbox, "request", native)
    for name in ("set_status", "delete"):
        monkeypatch.setattr(store, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    return native


def test_clear_records_a_dismissal_and_never_touches_the_reminder(store, monkeypatch):
    cid, item_id, fingerprint = _synced(store)
    native = _forbid_writes(store, monkeypatch)
    response = TestClient(main.app).post(f"/assistant/needs_you/{item_id}/clear", json={"fingerprint": fingerprint})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "id": item_id, "decision": "cleared"}
    native.assert_not_called()
    assert store.needs_you_dismissals() == {item_id: fingerprint}
    row = store._db.execute("SELECT status,title FROM commitments WHERE id=?", (cid,)).fetchone()
    assert (row["status"], row["title"]) == ("active", "Wisp verification timed 2113")


def test_dismissals_survive_a_new_store_on_the_same_database(tmp_path):
    first = AssistantStore(tmp_path / "assistant.db")
    first.dismiss_needs_you("reminders:" + "a" * 24, "b" * 24)
    first._db.close()
    again = AssistantStore(tmp_path / "assistant.db")
    try:
        assert again.needs_you_dismissals() == {"reminders:" + "a" * 24: "b" * 24}
    finally:
        again._db.close()


def test_clear_refuses_bad_ids_pages_and_form_posts(store, monkeypatch):
    _forbid_writes(store, monkeypatch)
    client = TestClient(main.app)
    good = "reminders:" + "a" * 24
    assert client.post("/assistant/needs_you/not-an-id/clear", json={"fingerprint": "b" * 24}).status_code == 422
    assert client.post(f"/assistant/needs_you/{good}/clear", json={"fingerprint": "x"}).status_code == 422
    assert client.post(f"/assistant/needs_you/{good}/clear", json={"fingerprint": "b" * 24},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post(f"/assistant/needs_you/{good}/clear", content='{"fingerprint": "' + "b" * 24 + '"}',
                       headers={"Content-Type": "text/plain"}).status_code in (415, 422)
    assert store.needs_you_dismissals() == {}


def test_the_today_workspace_leaves_a_cleared_reminder_out(store, monkeypatch):
    from service.assistant import daily_workspace as dw
    _, item_id, fingerprint = _synced(store)
    monkeypatch.setattr(dw, "collect_evidence", lambda *a, **k: {"reminders": dict(ready=True, records=[
        dict(id=store._db.execute("SELECT id FROM commitments WHERE source='reminders'").fetchone()["id"],
             title="Wisp verification timed 2113", due_ts=NOW - 2 * 86400)])})
    day = time.strftime("%Y-%m-%d", time.gmtime(NOW))
    shown = dw.workspace(store, day, "UTC", now=NOW)["needs_you"]
    assert [i["id"] for i in shown["items"]] == [item_id]
    store.dismiss_needs_you(item_id, fingerprint)
    assert dw.workspace(store, day, "UTC", now=NOW)["needs_you"] == dict(items=[], more=0)


def test_mark_done_uses_only_the_verified_complete_path(store, monkeypatch):
    cid, item_id, fingerprint = _synced(store)
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "",
                                     "source_id": "ek-1", "is_completed": True})
    monkeypatch.setattr(outbox, "request", native)
    calls = []
    real = schedule_extras.complete_commitment

    async def spy(row, s=None):
        calls.append(row["id"])
        return await real(row, s)
    monkeypatch.setattr(schedule_extras, "complete_commitment", spy)
    response = TestClient(main.app).post(f"/assistant/needs_you/{item_id}/done", json={"fingerprint": fingerprint})
    assert response.status_code == 200, response.text
    assert calls == [cid]
    native.assert_awaited_once()
    kind, payload = native.await_args.args
    assert kind == "complete_reminder", "never delete_reminder"
    assert payload["source_id"] == "ek-1" and payload["expected_title"] == "Wisp verification timed 2113"
    assert store._db.execute("SELECT status FROM commitments WHERE id=?", (cid,)).fetchone()["status"] == "done"


def test_mark_done_keeps_the_reminder_when_completion_is_not_verified(store, monkeypatch):
    cid, item_id, fingerprint = _synced(store)
    monkeypatch.setattr(outbox, "request", AsyncMock(return_value={"ok": False, "status": "unknown",
                                                                   "error": "native reply lost"}))
    response = TestClient(main.app).post(f"/assistant/needs_you/{item_id}/done", json={"fingerprint": fingerprint})
    assert response.status_code == 502
    assert "native reply lost" in response.json()["detail"]
    assert store._db.execute("SELECT status FROM commitments WHERE id=?", (cid,)).fetchone()["status"] == "active"


def test_mark_done_refuses_a_changed_or_unknown_reminder(store, monkeypatch):
    _, item_id, _ = _synced(store)
    native = AsyncMock(side_effect=AssertionError("nothing to complete"))
    monkeypatch.setattr(outbox, "request", native)
    client = TestClient(main.app)
    stale = needs_you_fingerprint("Wisp verification timed 2113", NOW - 5 * 86400)
    assert client.post(f"/assistant/needs_you/{item_id}/done", json={"fingerprint": stale}).status_code == 409
    assert client.post("/assistant/needs_you/reminders:" + "0" * 24 + "/done",
                       json={"fingerprint": stale}).status_code == 404
    assert client.post("/assistant/needs_you/wisp:" + "0" * 24 + "/done",
                       json={"fingerprint": stale}).status_code == 404, "a Day task is never a reminder"
    native.assert_not_called()


def test_reminder_for_finds_only_active_dated_reminders(store):
    cid, item_id, _ = _synced(store)
    assert reminder_for(store, item_id)["id"] == cid
    assert reminder_for(store, "email:" + item_id.split(":")[1]) is None
    store.set_status(cid, "done")
    assert reminder_for(store, item_id) is None
