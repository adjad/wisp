"""Focused regression coverage for the forward calendar agenda presentation.

These fixtures deliberately resemble a busy month, but never read a real
calendar or reminder store.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


def _ready(monkeypatch):
    monkeypatch.setattr(
        assistant_tools, "time", SimpleNamespace(time=lambda: NOW.timestamp()))
    monkeypatch.setattr(
        assistant_tools, "assistant_store", SimpleNamespace(
            upcoming=lambda **_kwargs: [], active_between=lambda *_args: []))
    monkeypatch.setattr(
        timeranges, "resolve_span",
        lambda period: timeranges.resolve_period(period, now=NOW))
    monkeypatch.setattr(
        "service.assistant.sync_status.ensure_sources",
        AsyncMock(return_value={"sources": []}))


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
    rows = [
        _row("Past this morning", NOW - timedelta(minutes=1)),
        _row("Later today", NOW + timedelta(hours=2)),
        _row("Tomorrow meeting", NOW + timedelta(days=1, hours=1)),
        _row("Day after tomorrow", NOW + timedelta(days=2)),
    ]
    monkeypatch.setattr(assistant_tools.assistant_store, "active_between", lambda *_args: rows)

    today = asyncio.run(assistant_tools.get_upcoming(period="today"))
    tomorrow = asyncio.run(assistant_tools.get_upcoming(period="tomorrow"))

    assert "Later today" in today
    assert "Past this morning" not in today and "Tomorrow meeting" not in today
    assert "Tomorrow meeting" in tomorrow
    assert "Later today" not in tomorrow and "Day after tomorrow" not in tomorrow


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


def test_empty_forward_range_uses_the_existing_helpful_empty_state(monkeypatch):
    _ready(monkeypatch)

    result = asyncio.run(assistant_tools.get_upcoming(period="today"))

    assert result.startswith("Today is Wednesday, September 9, 2026. Nothing scheduled in today.")
