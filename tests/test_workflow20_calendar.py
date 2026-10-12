"""Actual calendar query/availability semantics over synthetic sources only."""
import asyncio
import datetime as dt
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.tools import assistant_tools as A, schedule_extras as S


ROWS = [{"id": str(n), "title": title} for n, title in enumerate([
    "Physics Midterm", "Physics Quiz", "Physics Final", "Chemistry Midterm",
    "Physics Test Prep", "Physics exam review", "Dentist appointment",
])]


@pytest.mark.parametrize("query,titles", [
    ("physics test", ["Physics Midterm", "Physics Final"]),
    ("physcis exam", ["Physics Midterm", "Physics Final"]),
    ("when is my physics midterm", ["Physics Midterm"]),
    ("physics quiz", ["Physics Quiz"]),
    ("physics assessment", ["Physics Midterm", "Physics Quiz", "Physics Final"]),
    ("chemistry test", ["Chemistry Midterm"]),
    ("biology exam", []), ("dentist", ["Dentist appointment"]),
    ("physics final", ["Physics Final"]),
    ("test", ["Physics Midterm", "Physics Final", "Chemistry Midterm"]),
    ("Physics Test Prep", ["Physics Test Prep"]),
])
def test_assessment_candidates_preserve_identity_and_ambiguity(query, titles):
    matched = A._match_schedule_query(ROWS, query)
    assert [row["title"] for row in matched] == titles
    assert all(row is ROWS[int(row["id"])] for row in matched)


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 30, 10)


def complete_calendar_snapshot(rows=()):
    return {"commitments": list(rows), "tasks": [], "sources": {"calendar": {
        "available": True, "syncing": False, "last_sync": Clock.now().timestamp(),
        "diagnostics": {"coverage_start": dt.datetime(2026, 10, 1).timestamp(),
                        "coverage_end": dt.datetime(2026, 12, 1).timestamp()}}}}


@pytest.fixture
def ready(monkeypatch):
    previous_zone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    monkeypatch.setattr(S, "dt", SimpleNamespace(datetime=Clock, date=dt.date, time=dt.time, timedelta=dt.timedelta))
    monkeypatch.setattr(S, "_availability_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={
        "sources": [{"id": "calendar", "state": "ready"}, {"id": "reminders", "state": "ready"}],
        "reminders_fresh": True}))
    captured = []
    def snapshot(day, zone):
        captured.append(day)
        return complete_calendar_snapshot()
    monkeypatch.setattr(S, "_store", SimpleNamespace(today_snapshot=snapshot))
    yield captured
    if previous_zone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous_zone
    time.tzset()


@pytest.mark.parametrize("period,first,last,count", [
    ("today", "2026-10-30", "2026-10-30", 1),
    ("tomorrow", "2026-10-31", "2026-10-31", 1),
    ("this week", "2026-10-30", "2026-11-01", 3),
    ("next week", "2026-11-02", "2026-11-08", 7),
    ("this month", "2026-10-30", "2026-10-31", 2),
    ("next month", "2026-11-01", "2026-11-30", 30),
])
def test_exact_range_uses_each_local_day_not_fixed_seconds(period, first, last, count, ready):
    result = asyncio.run(S.find_free_time(period=period, minutes=45))
    assert len(ready) == count and ready[0] == first and ready[-1] == last
    assert "candidate" in result and "has not been created" in result


def test_real_end_and_overnight_blocks_not_assumed_hour(ready, monkeypatch):
    def stamp(day, hour): return dt.datetime(2026, 10, day, hour).timestamp()
    rows = [{"id": "synthetic.overnight", "source": "calendar", "when_ts": stamp(30, 23), "end_ts": stamp(31, 11), "status": "active"},
            {"id": "synthetic.afternoon", "source": "calendar", "when_ts": stamp(31, 12), "end_ts": stamp(31, 15), "status": "active"}]
    monkeypatch.setattr(S, "_store", SimpleNamespace(today_snapshot=lambda *a: complete_calendar_snapshot(rows)))
    result = asyncio.run(S.find_free_time(period="tomorrow", minutes=60))
    assert "11:00 AM–12:00 PM" in result and "3:00 PM–4:00 PM" in result
    assert "9:00 AM" not in result and "1:00 PM" not in result


@pytest.mark.parametrize("state", ["unavailable", "syncing", "denied", "stale"])
def test_availability_fails_closed_for_unverified_sources(state, ready, monkeypatch):
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={
        "sources": [{"id": "calendar", "state": state}, {"id": "reminders", "state": "ready"}], "reminders_fresh": True}))
    result = asyncio.run(S.find_free_time(period="next week"))
    assert "No study slot has been verified" in result and ready == []
    assert "candidate slots" not in result


@pytest.mark.parametrize("period", ["last week", "2026-10-29", "next year", "whenever"])
def test_unsupported_or_past_range_has_no_snapshot_reads(period, ready):
    assert "error" in asyncio.run(S.find_free_time(period=period))
    assert ready == []


@pytest.mark.parametrize("reminder_state", ["ready", "pending", "unavailable"])
def test_tomorrow_todo_route_reads_dated_native_reminders_in_exact_window(ready, monkeypatch, tmp_path, reminder_state):
    from service.assistant.store import AssistantStore
    from service.router import router as R
    from service.tools import timeranges
    monkeypatch.setattr(A.time, "time", lambda: Clock.now().timestamp())
    monkeypatch.setattr(timeranges, "datetime", Clock)
    ensure = AsyncMock(return_value={"sources": [
        {"id": "calendar", "label": "Calendar", "state": "ready"},
        {"id": "reminders", "label": "Reminders", "state":
         "unavailable" if reminder_state == "unavailable" else "ready"}],
        "reminders_fresh": reminder_state == "ready"})
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", ensure)
    store = AssistantStore(tmp_path / "fictional-reminders.db")
    monkeypatch.setattr(A, "assistant_store", store)
    try:
        store.sync_source("reminders", [
            {"source_id": name, "title": name, "kind": "reminder",
             "when_ts": dt.datetime.fromisoformat(when).timestamp()}
            for name, when in (("native overdue item", "2026-10-29T11:00"),
                               ("native today item", "2026-10-30T11:00"),
                               ("native tomorrow item", "2026-10-31T11:00"),
                               ("native following day item", "2026-11-01T00:00"))
        ])
        store.sync_source("calendar", [{"source_id": "native.appointment", "title": "Tomorrow appointment",
            "kind": "event", "when_ts": dt.datetime(2026, 10, 31, 9).timestamp(),
            "end_ts": dt.datetime(2026, 10, 31, 10).timestamp()}])
        route = asyncio.run(R.route("what's on my to-do list tomorrow?"))
        args = next(args for name, args in route.direct_calls if name == "get_upcoming")
        assert args == {"period": "tomorrow"}
        result = asyncio.run(A.get_upcoming(**args))
        ensure.assert_awaited_once_with(("calendar", "reminders"))
        assert "Tomorrow appointment" in result
        assert ("native tomorrow item" in result) == (reminder_state == "ready")
        assert all(title not in result for title in
                   ("native overdue item", "native today item", "native following day item"))
        if reminder_state != "ready":
            assert "Reminders" in result
            assert "not shown" in result or "incomplete" in result
        assert "native tomorrow item" not in asyncio.run(
            A.get_upcoming(**args, calendar_only=True))
    finally:
        store._db.close()


@pytest.mark.parametrize("period,expected", [
    ("today", {"ended today", "ongoing", "overnight", "future reminder"}),
    ("tomorrow", {"overnight", "tomorrow appointment"}),
    ("this week", {"ongoing", "overnight", "tomorrow appointment", "future reminder"}),
    ("this month", {"ongoing", "overnight", "tomorrow appointment", "future reminder"}),
])
def test_calendar_periods_preserve_overlap_and_reminder_forward_scope(period, expected, monkeypatch):
    from service.tools import timeranges
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    class AgendaClock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 12, 10)
    def stamp(day, hour):
        return dt.datetime(2026, 10, day, hour).timestamp()
    now = stamp(12, 10)
    def event(title, day, hour, end_day, end_hour, source="calendar"):
        return {"id": title, "title": title, "source": source, "when_ts": stamp(day, hour),
                "end_ts": stamp(end_day, end_hour), "status": "active", "context": ""}
    rows = [event("ended today", 12, 8, 12, 9), event("ongoing", 11, 23, 12, 12),
            event("overnight", 12, 23, 13, 2), event("tomorrow appointment", 13, 10, 13, 11),
            event("boundary ended", 11, 22, 12, 0), event("later month", 31, 23, 31, 23),
            event("elapsed reminder", 12, 9, 12, 9, "wisp"),
            event("future reminder", 12, 11, 12, 11, "wisp")]
    try:
        monkeypatch.setattr(A.time, "time", lambda: now)
        monkeypatch.setattr(timeranges, "datetime", AgendaClock)
        monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={
            "sources": [{"id": "calendar", "label": "Calendar", "state": "ready"},
                        {"id": "reminders", "label": "Reminders", "state": "ready"}],
            "reminders_fresh": True}))
        monkeypatch.setattr(A, "assistant_store", SimpleNamespace(
            calendar_events_overlapping=lambda start, end: [r for r in rows if r["source"] == "calendar"
                and r["when_ts"] < end and r["end_ts"] > start],
            active_between=lambda start, end: [r for r in rows if start <= r["when_ts"] < end]))
        result = asyncio.run(A.get_upcoming(period=period))
        if period == "this month":
            expected = expected | {"later month"}
        for row in rows:
            assert (row["title"] in result) == (row["title"] in expected), result
        if period == "tomorrow":
            assert "continued into 2026-10-13" in result, result
            assert "continued into today" not in result, result
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_quiz_number_is_part_of_assessment_identity():
    rows = [{"id":"one","title":"Physics Quiz 1"},{"id":"two","title":"Physics Quiz 2"},{"id":"three","title":"Physics Quiz Three"}]
    assert A._match_schedule_query(rows,"physics quiz two") == [rows[1]]
    assert A._match_schedule_query(rows,"physics quiz 3") == [rows[2]]
