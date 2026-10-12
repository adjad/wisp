"""Synthetic Calendar occurrence reads; no native, provider, or model access."""
import asyncio
from datetime import datetime, timedelta
import os
import socket
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.assistant.store import AssistantStore
from service.tools import assistant_tools as A, timeranges
from service.tools.registry import REGISTRY, run_tool

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 12, 10, tzinfo=ZONE)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    monkeypatch.setattr(A, "time", SimpleNamespace(time=lambda: NOW.timestamp()))
    monkeypatch.setattr(timeranges, "resolve_span", lambda value: timeranges.resolve_period(value, now=NOW))
    store = AssistantStore(tmp_path / "synthetic.db")
    monkeypatch.setattr(A, "assistant_store", store)
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={
        "sources": [{"id": source, "label": source.title(), "state": "ready"}
                    for source in ("calendar", "reminders")], "reminders_fresh": True}))
    def forbidden(*args, **kwargs):
        raise AssertionError("No native/network/model effects permitted")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from service.assistant.hub import hub
    import importlib
    monkeypatch.setattr(hub, "publish", AsyncMock(side_effect=forbidden))
    monkeypatch.setattr(importlib.import_module("service.assistant.outbox"), "request", AsyncMock(side_effect=forbidden))
    yield store
    store._db.close()
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def event(identity, *, when=None, account="school@example.test", location="Room 101", **extra):
    start = (when or NOW + timedelta(hours=1)).timestamp()
    return {"source_id": identity, "source": "calendar", "title": "Physics Midterm",
            "kind": "event", "when_ts": start, "end_ts": start + 3600,
            "account": account, "context": "School", "location": location, **extra}


def dispatch(**args):
    assert REGISTRY["get_upcoming"].func is A.get_upcoming
    return asyncio.run(run_tool(REGISTRY["get_upcoming"], args))


@pytest.mark.parametrize("period", ["", "today", "tomorrow", "this week", "next week", "this month", "next month"])
def test_actual_registry_keeps_distinct_native_occurrences(isolated, period):
    when = NOW + timedelta(hours=1)
    if period and period not in {"today", "this week", "this month"}:
        start, _, _ = timeranges.resolve_period(period, now=NOW)
        when = datetime.fromtimestamp(start, ZONE) + timedelta(hours=11)
    isolated.sync_source("calendar", [event("native.one", when=when),
        event("native.two", when=when, location="Room 202")])
    result = dispatch(period=period, calendar_only=True, query="physics test")
    assert "Calendar events: 2" in result
    assert result.count("Physics Midterm") == 2
    assert "Room 101" in result and "Room 202" in result


@pytest.mark.parametrize("period", ["", "today"])
def test_account_filter_cannot_lose_other_same_title_occurrence(isolated, period):
    isolated.sync_source("calendar", [event("personal", account="home@example.test"),
        event("work", account="work@example.test", location="Work room")])
    result = dispatch(period=period, calendar_only=True, account="work@example.test")
    assert "Calendar events: 1" in result and "Work room" in result
    assert "Room 101" not in result and "Nothing scheduled" not in result


@pytest.mark.parametrize("difference", ["native_id", "account", "context", "exact_start", "missing_id", "conflicting_payload"])
def test_presentation_identity_never_uses_title_and_minute(difference):
    first, second = event("same"), event("same")
    if difference == "native_id": second["source_id"] = "other"
    if difference == "account": second["account"] = "other@example.test"
    if difference == "context": second["context"] = "Different calendar"
    if difference == "exact_start": second["when_ts"] += 1
    if difference == "missing_id":
        first.pop("source_id")
        second.pop("source_id")
    if difference == "conflicting_payload": second["location"] = "Conflicting room"
    result = A._collapse_schedule_rows([first, second])
    assert len(result) == 2
    assert result == sorted([first, second], key=lambda row: row["when_ts"])


def test_exact_duplicate_occurrence_shown_once_and_original_fields_kept():
    first = event("native.same")
    assert A._collapse_schedule_rows([first, dict(first)]) == [first]


def test_partial_duplicate_cannot_bridge_conflicting_native_payloads():
    full = event("same")
    partial = dict(full)
    partial.pop("end_ts")
    conflict = {**full, "end_ts": full["end_ts"] + 3600}
    result = A._collapse_schedule_rows([partial, full, conflict])
    assert len(result) == 2
    assert {row["end_ts"] for row in result} == {full["end_ts"], conflict["end_ts"]}
    assert "end_ts" not in partial  # Source evidence is not mutated.
    assert len(A._collapse_schedule_rows([full, conflict, partial])) == 3


@pytest.mark.parametrize("identity", [None, "", " ", 12, True])
def test_invalid_native_identity_never_collapses(identity):
    first = event(identity)
    assert len(A._collapse_schedule_rows([first, dict(first)])) == 2


def test_recurring_native_id_keeps_each_exact_start(isolated):
    isolated.sync_source("calendar", [event("series"),
        event("series", when=NOW + timedelta(days=1, hours=1), location="Next occurrence")])
    result = dispatch(calendar_only=True)
    assert result.count("Physics Midterm") == 2 and "Next occurrence" in result


def test_manual_reminder_mirror_collapse_is_unchanged():
    manual = {**event("manual"), "source": "manual", "kind": "reminder"}
    native = {**manual, "source": "reminders", "source_id": "apple", "when_ts": manual["when_ts"] + 1}
    assert A._collapse_schedule_rows([native, manual]) == [manual]


def test_raw_and_normal_read_duplicate_not_counted_twice(isolated):
    isolated.sync_source("calendar", [event("one")])
    assert "Calendar events: 1" in dispatch(period="today", calendar_only=True)


@pytest.mark.parametrize("period", ["", "today"])
def test_calendar_only_does_not_render_manual_winner_as_second_native_event(isolated, period):
    native = event("calendar")
    isolated.sync_source("calendar", [native])
    isolated.add_manual(native["title"], native["when_ts"])
    result = dispatch(period=period, calendar_only=True)
    assert "Calendar events: 1" in result and "Wisp/Apple reminders: 0" in result
    assert result.count("Physics Midterm") == 1 and "Room 101" in result


def test_mixed_read_still_collapses_manual_reminder_pair_and_sorts(isolated):
    when = NOW.timestamp() + 7200
    isolated.add_manual("Call dentist", when)
    isolated.sync_source("reminders", [{"source_id": "dentist", "kind": "reminder",
        "title": "Call dentist", "when_ts": when}])
    isolated.sync_source("calendar", [event("one"), event("two", location="Room 202")])
    result = dispatch()
    assert "Calendar events: 2" in result and "Wisp/Apple reminders: 1" in result
    assert result.count("Call dentist") == 1
    assert result.index("Physics Midterm") < result.index("Call dentist")


def test_default_days_preserves_inclusive_upper_boundary_and_clamp(isolated):
    # Legacy days is an elapsed-seconds horizon, including a DST transition.
    end = datetime.fromtimestamp(NOW.timestamp() + 60 * 86400, ZONE)
    isolated.sync_source("calendar", [event("edge.one", when=end),
        event("edge.two", when=end, location="Other edge"),
        event("outside", when=end + timedelta(seconds=1), location="Outside")])
    result = dispatch(days=100, calendar_only=True)
    assert "next 60 day(s)" in result and "Calendar events: 2" in result
    assert "Other edge" in result and "Outside" not in result


def test_explicit_window_excludes_end_and_default_excludes_elapsed(isolated):
    tomorrow = NOW.replace(hour=0) + timedelta(days=1)
    isolated.sync_source("calendar", [event("elapsed", when=NOW - timedelta(minutes=1), location="Elapsed"),
        event("inside", location="Inside"), event("end", when=tomorrow, location="Tomorrow")])
    assert "Elapsed" not in dispatch(calendar_only=True)
    today = dispatch(period="today", calendar_only=True)
    assert "Elapsed" in today and "Inside" in today and "Tomorrow" not in today


def test_calendar_only_and_unavailable_sources_remain_scoped(isolated, monkeypatch):
    isolated.sync_source("calendar", [event("one")])
    isolated.sync_source("reminders", [{**event("reminder"), "kind": "reminder", "title": "Reminder decoy"}])
    assert "Reminder decoy" not in dispatch(calendar_only=True)
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={
        "sources": [{"id": "calendar", "label": "Calendar", "state": "unavailable"}]}))
    result = dispatch(calendar_only=True)
    assert "could not check Calendar" in result and "Physics Midterm" not in result
    assert "Nothing scheduled" not in result


def test_pure_route_bound_calendar_read_reaches_actual_registry(isolated):
    from service.router.router import route
    from service.workflows.reads import compile_read
    prompt = 'Show my calendar events today named "Physics Midterm" without reminders'
    plan = compile_read(prompt)
    assert plan == ([("get_upcoming", {"period": "today", "query": "Physics Midterm", "calendar_only": True})], "")
    decision = asyncio.run(route(prompt))
    assert decision.tool_argument_bindings["get_upcoming"] == plan[0][0][1]
    assert "search_reminders" not in decision.tool_subset
    isolated.sync_source("calendar", [event("one"), event("two", location="Room 202")])
    result = dispatch(**decision.tool_argument_bindings["get_upcoming"])
    assert "Calendar events: 2" in result
    assert "Room 101" in result and "Room 202" in result


@pytest.mark.parametrize("period", ["", "today"])
@pytest.mark.parametrize("raw_empty", [False, True])
def test_independent_legacy_calendar_read_cannot_mix_or_resurrect_generations(isolated, monkeypatch, period, raw_empty):
    first = event("same", location="Captured room")
    later = {**first, "location": "Independent later room"}
    state = {"generation": "first"}
    def raw_read(*args):
        assert state["generation"] == "first"
        captured = [] if raw_empty else [dict(first)]
        state["generation"] = "later"  # Simulate sync between independent reads.
        return captured
    def legacy_read(*args, **kwargs):
        assert state["generation"] == "later"
        return [later]
    monkeypatch.setattr(isolated, "calendar_events_overlapping", raw_read)
    monkeypatch.setattr(isolated, "upcoming", legacy_read)
    monkeypatch.setattr(isolated, "active_between", legacy_read)
    result = dispatch(period=period, calendar_only=True)
    assert "Independent later room" not in result
    if raw_empty:
        assert "Physics Midterm" not in result and "Nothing scheduled" in result
    else:
        assert "Calendar events: 1" in result and "Captured room" in result


@pytest.mark.parametrize("period", ["", "today"])
def test_legacy_manual_calendar_provenance_is_not_a_native_contribution(isolated, monkeypatch, period):
    native = event("same", location="Captured room")
    secondary = {**native, "source": "manual", "kind": "reminder",
        "source_id": "manual", "location": "", "duplicate_sources": ["calendar", "reminders"],
        "duplicate_ids": ["calendar.local", "reminder.local"]}
    monkeypatch.setattr(isolated, "calendar_events_overlapping", lambda *args: [native])
    monkeypatch.setattr(isolated, "upcoming", lambda **kwargs: [secondary])
    monkeypatch.setattr(isolated, "active_between", lambda *args: [secondary])
    result = dispatch(period=period)
    assert "Calendar events: 1; Wisp/Apple reminders: 1" in result
    assert "Captured room" in result and result.count("Physics Midterm") == 2
    assert secondary["duplicate_sources"] == ["calendar", "reminders"]
