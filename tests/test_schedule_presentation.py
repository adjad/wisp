"""Focused regression coverage for the forward calendar agenda presentation.

These fixtures deliberately resemble a busy month, but never read a real
calendar or reminder store.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.assistant import brief
from service.assistant.store import AssistantStore
from service.tools import assistant_tools, timeranges


NOW = datetime(2026, 9, 9, 10, 0)


def _row(title: str, when: datetime, *, source: str = "calendar", **extra) -> dict:
    return {
        "title": title,
        "source": source,
        "kind": "event" if source == "calendar" else "reminder",
        "when_ts": when.timestamp(),
        "location": "",
        "organizer": "Implementation Account",
        "account": "work@example.test",
        **extra,
    }


def _ready(monkeypatch, *, calendar_state: str = "ready"):
    monkeypatch.setattr(
        assistant_tools, "time", SimpleNamespace(time=lambda: NOW.timestamp()))
    monkeypatch.setattr(
        assistant_tools, "assistant_store", SimpleNamespace(
            upcoming=lambda **_kwargs: [], active_between=lambda *_args: [],
            calendar_events_overlapping=lambda *_args: []))
    monkeypatch.setattr(
        timeranges, "resolve_span",
        lambda period: timeranges.resolve_period(period, now=NOW))
    monkeypatch.setattr(
        "service.assistant.sync_status.ensure_sources",
        AsyncMock(return_value={"sources": [
            {"id": "calendar", "label": "Calendar", "state": calendar_state},
            {"id": "reminders", "label": "Reminders", "state": "ready"},
        ]}))


@pytest.fixture
def store(tmp_path):
    instance = AssistantStore(tmp_path / "assistant.db")
    yield instance
    instance._db.close()


def test_busy_month_is_a_clean_agenda_not_a_raw_storage_dump(monkeypatch):
    _ready(monkeypatch)
    rows = [_row("Already happened", NOW - timedelta(minutes=1))]
    rows += [_row(f"Calendar {n}", NOW + timedelta(days=n // 3, hours=n % 3))
             for n in range(32)]
    rows += [_row(f"Reminder {n}", NOW + timedelta(days=n // 2, hours=5), source="reminders")
             for n in range(13)]
    rows[1]["location"] = "Room 301"
    # A familiar manual/Apple mirror must read as one commitment.
    rows.extend([
        _row("Call dentist", NOW + timedelta(days=3, hours=2), source="manual"),
        _row("Call dentist", NOW + timedelta(days=3, hours=2), source="reminders"),
    ])
    monkeypatch.setattr(assistant_tools.assistant_store, "active_between", lambda *_args: rows)

    result = asyncio.run(assistant_tools.get_upcoming(period="this month"))

    assert result.startswith("Upcoming — September 2026")
    assert "Already happened" not in result
    assert "Today · Wed Sep 9" in result
    assert "Calendar events" in result and "Reminders" in result
    assert "Calendar 0" in result and "Reminder 12" in result
    assert "Room 301" in result
    assert result.count("Call dentist") == 1
    assert "Implementation Account" not in result
    assert "work@example.test" not in result
    assert "(now)" not in result


def test_today_and_tomorrow_respect_forward_day_boundaries(monkeypatch):
    _ready(monkeypatch)
    day_start, day_end = assistant_tools._local_day_bounds(NOW.timestamp())
    rows = [
        _row("Past this morning", NOW - timedelta(minutes=1)),
        _row("All-day event", datetime.fromtimestamp(day_start), all_day=True,
             end_ts=day_end),
        _row("Overnight event", NOW - timedelta(hours=11),
             end_ts=day_start + 3600),
        _row("Later today", NOW + timedelta(hours=2)),
        _row("Tomorrow meeting", NOW + timedelta(days=1, hours=1)),
        _row("Day after tomorrow", NOW + timedelta(days=2)),
    ]
    monkeypatch.setattr(
        assistant_tools.assistant_store, "active_between",
        lambda start, end: [row for row in rows
                            if start <= row["when_ts"] < end])
    monkeypatch.setattr(
        assistant_tools.assistant_store, "calendar_events_overlapping",
        lambda start, end: [row for row in rows
                            if row["source"] == "calendar"
                            and row["when_ts"] < end
                            and (row["when_ts"] >= start
                                 or row.get("end_ts", 0) > start)])

    today = asyncio.run(assistant_tools.get_upcoming(period="today"))
    tomorrow = asyncio.run(assistant_tools.get_upcoming(period="tomorrow"))

    assert "Later today" in today
    assert "Past this morning" in today
    assert "All-day event" in today and "All day" in today
    assert "Overnight event" in today and "continued into today" in today
    assert "Tomorrow meeting" not in today
    assert "Tomorrow meeting" in tomorrow
    assert "Later today" not in tomorrow and "Day after tomorrow" not in tomorrow


def test_calendar_day_overlap_keeps_started_and_all_day_events(store):
    zone = ZoneInfo("America/Los_Angeles")
    day = datetime(2026, 10, 8, tzinfo=zone)
    day_start = day.timestamp()
    day_end = (day + timedelta(days=1)).timestamp()
    yesterday_start = (day - timedelta(days=1)).replace(hour=23).timestamp()
    yesterday_end = day_start + 3600
    today_started = datetime(2026, 10, 8, 9, 30, tzinfo=zone).timestamp()
    today_ended = datetime(2026, 10, 8, 10, 30, tzinfo=zone).timestamp()
    zero_duration = datetime(2026, 10, 8, 11, 0, tzinfo=zone).timestamp()
    day_end_event = day_end + 3600

    def event(source_id, when_ts, end_ts, *, all_day=False):
        return {"source_id": source_id, "kind": "event", "title": source_id,
                "when_ts": when_ts, "end_ts": end_ts, "all_day": all_day}

    store.sync_source("calendar", [
        event("overnight", yesterday_start, yesterday_end),
        event("all-day", day_start, day_end, all_day=True),
        event("started-today", today_started, today_ended),
        event("zero-duration", zero_duration, zero_duration),
        event("unknown-end-yesterday", yesterday_start, None),
        event("ended-at-midnight", yesterday_start, day_start),
        event("starts-tomorrow", day_end, day_end_event),
    ])

    results = store.calendar_events_overlapping(day_start, day_end)

    assert [row["title"] for row in results] == [
        "overnight", "all-day", "started-today", "zero-duration"]


def test_daily_schedule_labels_overnight_and_multiday_start_dates(store, monkeypatch):
    now = datetime(2026, 10, 8, 10).timestamp()
    today = datetime.fromtimestamp(now).date()
    day_start, day_end = assistant_tools._local_day_bounds(now)
    overnight_start = datetime.combine(today - timedelta(days=1), datetime.min.time()) \
        .replace(hour=23).timestamp()
    overnight_end = datetime.combine(today, datetime.min.time()).replace(hour=1).timestamp()
    multiday_start = datetime.combine(today - timedelta(days=2), datetime.min.time()) \
        .replace(hour=22).timestamp()
    multiday_end = now + 3600
    started_today = now - 1800

    def event(source_id, title, when_ts, end_ts, *, all_day=False, kind="event"):
        return {"source_id": source_id, "kind": kind, "title": title,
                "when_ts": when_ts, "end_ts": end_ts, "all_day": all_day}

    store.sync_source("calendar", [
        event("all-day", "All-day event", day_start, day_end, all_day=True),
        event("started-today", "Started today", started_today, now + 1800,
              kind="meeting"),
        event("overnight", "Overnight event", overnight_start, overnight_end,
              kind="meeting"),
        event("multi-day", "Multi-day event", multiday_start, multiday_end,
              kind="meeting"),
    ])
    monkeypatch.setattr(brief, "assistant_store", store)
    monkeypatch.setattr(
        "service.assistant.sync_status.source_status",
        lambda source: {"id": source, "label": source.title(), "state": "ready"})

    section = brief._schedule_section(now)

    rows = {title: next(line for line in section.splitlines() if title in line)
            for title in ("All-day event", "Started today", "Overnight event",
                          "Multi-day event")}
    assert "All day" in rows["All-day event"]
    assert "earlier today" in rows["Started today"]
    for title, start in (("Overnight event", overnight_start),
                         ("Multi-day event", multiday_start)):
        expected_start = datetime.fromtimestamp(start).strftime("%a %-I:%M %p")
        assert f"Started {expected_start}" in rows[title]
        assert "continued into today" in rows[title]
        assert "earlier today" not in rows[title]


def test_month_range_keeps_future_month_items_and_separates_sources(monkeypatch):
    _ready(monkeypatch)
    rows = [
        _row("September event", datetime(2026, 9, 17, 9), location="Campus"),
        _row("September reminder", datetime(2026, 9, 17, 17), source="manual"),
        _row("October event", datetime(2026, 10, 1, 9)),
    ]
    monkeypatch.setattr(assistant_tools.assistant_store, "active_between", lambda *_args: rows)

    result = asyncio.run(assistant_tools.get_upcoming(period="this month"))

    assert "September event" in result and "September reminder" in result
    assert "October event" not in result
    assert "Calendar events\n  - 9:00 AM — September event @ Campus" in result
    assert "Reminders\n  - 5:00 PM — September reminder" in result


def test_fresh_empty_calendar_is_a_genuine_empty_state(monkeypatch):
    _ready(monkeypatch)

    result = asyncio.run(assistant_tools.get_upcoming(period="today"))

    assert result == "Today is Wednesday, September 9, 2026. Nothing scheduled in today."
    assert "access may not" not in result
    assert "first sync" not in result


def test_syncing_and_unavailable_calendar_remain_distinct_from_empty_success(monkeypatch):
    _ready(monkeypatch, calendar_state="syncing")

    syncing = asyncio.run(assistant_tools.get_upcoming(period="today"))

    assert syncing.startswith("Wisp is still syncing your calendar")

    _ready(monkeypatch, calendar_state="unavailable")

    unavailable = asyncio.run(assistant_tools.get_upcoming(period="today"))

    assert unavailable.startswith("Wisp could not check Calendar")
    assert "Nothing scheduled" not in unavailable
