"""Synthetic store coverage for local-day Calendar overlap queries."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from service.assistant.store import AssistantStore
from service.assistant import brief
from service.tools import assistant_tools


@pytest.fixture
def store(tmp_path):
    instance = AssistantStore(tmp_path / "assistant.db")
    yield instance
    instance._db.close()


def test_calendar_day_overlap_keeps_started_and_all_day_events(store):
    zone = ZoneInfo("America/Los_Angeles")
    day = datetime(2026, 10, 8, tzinfo=zone)
    day_start = day.timestamp()
    day_end = (day + timedelta(days=1)).timestamp()
    yesterday_start = (day - timedelta(days=1)).replace(hour=23).timestamp()
    yesterday_end = day_start + 3600
    today_started = datetime(2026, 10, 8, 9, 30, tzinfo=zone).timestamp()
    today_ended = datetime(2026, 10, 8, 10, 30, tzinfo=zone).timestamp()
    day_end_event = day_end + 3600

    def event(source_id, when_ts, end_ts, *, all_day=False):
        return {"source_id": source_id, "kind": "event", "title": source_id,
                "when_ts": when_ts, "end_ts": end_ts, "all_day": all_day}

    store.sync_source("calendar", [
        event("overnight", yesterday_start, yesterday_end),
        event("all-day", day_start, day_end, all_day=True),
        event("started-today", today_started, today_ended),
        event("ended-at-midnight", yesterday_start, day_start),
        event("starts-tomorrow", day_end, day_end_event),
    ])

    results = store.calendar_events_overlapping(day_start, day_end)

    assert [row["title"] for row in results] == [
        "overnight", "all-day", "started-today"]


def test_daily_brief_and_today_agenda_include_started_and_all_day_events(
        store, monkeypatch):
    now = datetime(2026, 10, 8, 10).timestamp()
    day_start, day_end = assistant_tools._local_day_bounds(now)
    store.sync_source("calendar", [
        {"source_id": "all-day", "kind": "event", "title": "All-day event",
         "when_ts": day_start, "end_ts": day_end, "all_day": True},
        {"source_id": "started", "kind": "meeting", "title": "Started meeting",
         "when_ts": now - 1800, "end_ts": now + 1800, "all_day": False},
    ])
    monkeypatch.setattr(brief, "assistant_store", store)
    monkeypatch.setattr(
        "service.assistant.sync_status.source_status",
        lambda source: {"id": source, "label": source.title(), "state": "ready"})

    block = brief._calendar_block(now)
    agenda = brief._agenda(now)

    assert "ON THE CALENDAR TODAY (2 item(s))" in block
    assert "All-day event" in block and "Started meeting" in block
    assert len(agenda["events"]) == 2
    assert {item["title"] for item in agenda["events"]} == {
        "All-day event", "Started meeting"}
