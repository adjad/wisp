"""H-9: a sync must never be able to erase the user's own (manual) reminders.

`sync_source` deletes every active row of its source that is absent from the
input. That is right for Calendar and Reminders (upstream is authoritative) and
catastrophic for "manual", the user's own Wisp-created reminders, which exist
nowhere upstream. The HTTP endpoint accepted "manual"/"wisp" as a source and the
store did not object, so `{"source": "manual", "events": []}` deleted them all.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

_scratch = tempfile.TemporaryDirectory(prefix="wisp-manual-sync-")
os.environ.setdefault("WISP_HOME", _scratch.name)

from fastapi.testclient import TestClient  # noqa: E402

import service.main as main  # noqa: E402
from service.assistant.store import QA_SEED_SOURCE, AssistantStore  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(main, "assistant_store", s)
    return s


def _titles(s, now):
    return sorted(c["title"] for c in s.upcoming(now, days=3))


def _event(sid, title, when):
    return {"kind": "event", "title": title, "when_ts": when, "source_id": sid}


def test_the_store_refuses_to_replace_the_manual_source(store):
    now = time.time()
    store.add_manual("Call the dentist", now + 7200)
    with pytest.raises(ValueError):
        store.sync_source("manual", [])
    assert _titles(store, now) == ["Call the dentist"]


def test_a_refused_manual_sync_with_items_adds_nothing_either(store):
    now = time.time()
    store.add_manual("Call the dentist", now + 7200)
    with pytest.raises(ValueError):
        store.sync_source("manual", [_event("x", "Injected", now + 3600)])
    assert _titles(store, now) == ["Call the dentist"]


@pytest.mark.parametrize("source", ["manual", "wisp", "Manual", " MANUAL ", "wisp_seed", "mail", "", "x" * 40])
def test_the_endpoint_only_accepts_the_native_sources(store, source):
    now = time.time()
    store.add_manual("Call the dentist", now + 7200)
    response = TestClient(main.app).post("/assistant/sync/calendar", json={"source": source, "events": []})
    assert response.status_code == 422, (source, response.text)
    assert _titles(store, now) == ["Call the dentist"]


@pytest.mark.parametrize("source", [None, 5, ["manual"], {"s": "manual"}, True])
def test_the_endpoint_rejects_a_non_string_source(store, source):
    now = time.time()
    store.add_manual("Call the dentist", now + 7200)
    response = TestClient(main.app).post("/assistant/sync/calendar", json={"source": source, "events": []})
    assert response.status_code == 422, (source, response.text)
    assert _titles(store, now) == ["Call the dentist"]


@pytest.mark.parametrize("source", ["calendar", "reminders", "Apple Calendar", "apple_reminders"])
def test_native_syncs_still_work_and_leave_manual_reminders_alone(store, source):
    now = time.time()
    store.add_manual("Call the dentist", now + 7200)
    ok = TestClient(main.app).post("/assistant/sync/calendar", json={
        "source": source, "events": [_event("e1", "Team lunch", now + 3600)]})
    assert ok.status_code == 200 and ok.json()["synced"] >= 1, ok.text
    empty = TestClient(main.app).post("/assistant/sync/calendar", json={"source": source, "events": []})
    assert empty.status_code == 200, empty.text
    assert "Call the dentist" in _titles(store, now), "an empty native sync removed a manual reminder"


def test_the_qa_seed_source_still_works_in_process(store):
    # scripts/wisp_testdata.py seeds through the store directly; that path must keep working.
    assert store.sync_source(QA_SEED_SOURCE, [_event("seed-1", "Seed", time.time() + 3600)]) == 1
