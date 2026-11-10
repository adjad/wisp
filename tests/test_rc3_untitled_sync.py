"""A calendar event or dated reminder with no title is kept as "(untitled)" by the
sync endpoint instead of being dropped, so Day still shows the time as taken."""
import time

import pytest
from fastapi.testclient import TestClient

import service.main as main
from service.assistant.store import AssistantStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    s = AssistantStore(tmp_path / "assistant.db")
    monkeypatch.setattr(main, "assistant_store", s)
    return s


def _sync(source, events):
    return TestClient(main.app).post("/assistant/sync/calendar", json={"source": source, "events": events})


@pytest.mark.parametrize("source", ["calendar", "reminders"])
@pytest.mark.parametrize("title", ["", "   ", None])
def test_a_blank_title_is_kept_as_untitled(store, source, title):
    now = time.time()
    event = {"kind": "event", "when_ts": now + 3600, "end_ts": now + 7200, "source_id": "ev-1"}
    if title is not None:
        event["title"] = title
    response = _sync(source, [event])
    assert response.status_code == 200 and response.json()["synced"] == 1, response.text
    rows = store.upcoming(now, days=3)
    assert [r["title"] for r in rows] == ["(untitled)"]


def test_a_blank_title_without_a_source_id_gets_a_stable_one(store):
    now = time.time()
    event = {"kind": "event", "title": "", "when_ts": now + 3600}
    assert _sync("calendar", [event]).json()["synced"] == 1
    assert _sync("calendar", [event]).json()["synced"] == 1
    assert [r["title"] for r in store.upcoming(now, days=3)] == ["(untitled)"]


def test_real_titles_and_missing_times_are_unchanged(store):
    now = time.time()
    events = [{"kind": "event", "title": "  Design review ", "when_ts": now + 3600, "source_id": "a"},
              {"kind": "event", "title": "", "source_id": "b"}]          # no time: still skipped
    assert _sync("calendar", events).json()["synced"] == 1
    assert [r["title"] for r in store.upcoming(now, days=3)] == ["  Design review "]
