"""Registered daily briefing with synthetic source stores; no native/model/network."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.tools import everyday, assistant_tools, email_tools, imessage_tools, web_tools
from service.tools.registry import REGISTRY, run_tool

NOW = dt.datetime(2026, 10, 12, 8)
STAMP = NOW.timestamp()


class Store:
    def __init__(self):
        self.receipts = {source: {"available": True, "syncing": False,
            "snapshot_started_at": STAMP - 1,
            "diagnostics": {"coverage_start": STAMP - 86400, "coverage_end": STAMP + 60 * 86400}}
            for source in ("calendar", "reminders")}
        self.rows = []
        self.view = {"freshness": "current", "status": "complete", "stored": 0, "total": 0}
        self.undated = []
        self.reads = []

    @contextmanager
    def transaction(self, *, write):
        assert write is False
        yield self

    def execute(self, sql):
        source = "calendar" if "'calendar'" in sql else "reminders"
        self.reads.append(source)
        value = self.receipts.get(source)
        return SimpleNamespace(fetchone=lambda: {"payload": json.dumps(value)} if value is not None else None)

    def active_between(self, start=None, end=None, *, include_stale=False):
        return deepcopy(self.rows)

    def undated_listing(self, query):
        return deepcopy(self.view), [deepcopy(row) for row in self.undated if query.casefold() in row["title"].casefold()]


def reminder(title, delta=0, *, source="reminders", account="Work", status="active", **extra):
    return {"id": title, "source": source, "title": title, "when_ts": STAMP + delta,
            "kind": "reminder", "account": account, "status": status, **extra}


@pytest.fixture
def sources(monkeypatch):
    real_datetime = dt.datetime
    class Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(STAMP, tz) if tz else NOW
    monkeypatch.setattr(everyday.dt, "datetime", Clock)
    monkeypatch.setattr(everyday.time, "time", lambda: STAMP)
    store = Store()
    monkeypatch.setattr(assistant_tools, "assistant_store", store)
    monkeypatch.setattr(assistant_tools, "get_upcoming", AsyncMock(return_value="Verified calendar item"))
    monkeypatch.setattr(email_tools, "summarize_emails", AsyncMock(return_value="Cached email subject"))
    monkeypatch.setattr(email_tools, "_headers_at", STAMP - 1)
    monkeypatch.setattr(email_tools, "email_sync_state", lambda: "ready")
    monkeypatch.setattr(imessage_tools, "summarize_messages", AsyncMock(return_value="Cached message excerpt"))
    monkeypatch.setattr(imessage_tools, "messages_sync_state", lambda: "ready")
    monkeypatch.setattr(web_tools, "get_weather", AsyncMock(return_value="Weather for San Francisco, California, United States: sunny"))
    monkeypatch.setattr(web_tools, "_yahoo_get", AsyncMock(side_effect=AssertionError("no live weather")))
    from service.assistant.hub import hub
    monkeypatch.setattr(hub, "publish", AsyncMock(side_effect=AssertionError("no native source requests")))
    return store


def dispatch(**args):
    assert REGISTRY["daily_brief"].func is everyday.daily_brief
    return asyncio.run(run_tool(REGISTRY["daily_brief"], args))


def only_reminders(**args):
    return dispatch(include_calendar=False, include_activity=False, include_weather=False, **args)


def test_registered_default_has_overdue_next_seven_days_and_separate_undated(sources):
    sources.rows = [reminder("Overdue task", -3600), reminder("Due later today", 3600),
                    reminder("Within seven days", 6 * 86400), reminder("Too far", 8 * 86400),
                    reminder("Completed task", 2000, status="done"),
                    reminder("Calendar is not a reminder", 2000, source="calendar")]
    sources.undated = [{"title": "Someday task"}]
    sources.view.update(stored=1, total=1)
    result = dispatch()
    for expected in ("overdue: Overdue task", "upcoming: Due later today", "Within seven days", "Someday task",
                     "neither overdue nor upcoming", "Verified calendar item", "Cached email subject", "Cached message excerpt"):
        assert expected in result
    assert "Too far" not in result and "Completed task" not in result and "Calendar is not a reminder" not in result
    assert "Weather unknown" in result and "never guess" in result
    assert "2026-10-19T08:00" in result
    assistant_tools.get_upcoming.assert_awaited_once_with(period="today", calendar_only=True)
    web_tools.get_weather.assert_not_awaited()


def test_precise_period_account_and_query_do_not_widen_to_default_tasks(sources):
    sources.rows = [reminder("Expense selected", 86400), reminder("Expense overdue", -300),
                    reminder("Expense other account", 86400, account="Personal"),
                    reminder("Other title", 86400), reminder("Expense day after", 2 * 86400)]
    result = only_reminders(period="tomorrow", account="Work", query="Expense")
    assert "Expense selected" in result
    for excluded in ("Expense overdue", "Expense other account", "Other title", "Expense day after"):
        assert excluded not in result
    assert "overdue outside window excluded" in result and "Undated reminders excluded" in result


def test_explicit_overdue_and_undated_optins_remain_separately_labeled(sources):
    sources.rows = [reminder("Old task", -86400)]
    sources.undated = [{"title": "Undated task"}]
    result = only_reminders(period="tomorrow", include_overdue=True, include_undated=True)
    assert "overdue: Old task" in result and "Undated task" in result


@pytest.mark.parametrize("problem", ["missing", "stale", "future", "failed", "partial", "syncing"])
def test_reminder_source_problems_withhold_native_and_preserve_honest_local_rows(sources, problem):
    sources.rows = [reminder("Native must be hidden", 3600), reminder("Wisp review", 3600, source="manual")]
    if problem == "missing":
        sources.receipts.pop("reminders")
    else:
        receipt = sources.receipts["reminders"]
        if problem == "stale": receipt["snapshot_started_at"] = STAMP - 1201
        if problem == "future": receipt["snapshot_started_at"] = STAMP + 1
        if problem == "failed": receipt["available"] = False
        if problem == "partial": receipt["diagnostics"]["complete"] = False
        if problem == "syncing": receipt["syncing"] = True
    result = only_reminders()
    assert "Apple Reminders unknown" in result and "Native must be hidden" not in result
    assert "Wisp review" in result and "Apple copy/status unverified" in result
    assert "Undated reminders unknown" in result
    assert "No dated matches" not in result


@pytest.mark.parametrize("problem", ["missing", "stale", "failed", "partial", "coverage"])
def test_calendar_source_failure_never_becomes_empty_schedule(sources, problem):
    if problem == "missing": sources.receipts.pop("calendar")
    elif problem == "stale": sources.receipts["calendar"]["snapshot_started_at"] = STAMP - 1201
    elif problem == "failed": sources.receipts["calendar"]["available"] = False
    elif problem == "partial": sources.receipts["calendar"]["complete"] = False
    else: sources.receipts["calendar"]["diagnostics"]["coverage_end"] = STAMP + 1
    result = dispatch(include_reminders=False, include_activity=False, include_weather=False)
    assert "Calendar unknown" in result and "Nothing scheduled" not in result
    assistant_tools.get_upcoming.assert_not_awaited()


def test_source_exclusions_do_not_touch_excluded_sources(sources):
    result = dispatch(include_calendar=False, include_reminders=False, include_email=False,
                      include_messages=False, include_weather=False, location="SF")
    assert sources.reads == []
    for tool in (assistant_tools.get_upcoming, email_tools.summarize_emails,
                 imessage_tools.summarize_messages, web_tools.get_weather):
        tool.assert_not_awaited()
    assert "unknown" not in result and "Weather" not in result


def test_exact_period_and_account_forwarded_to_supported_sources(sources):
    result = dispatch(period="tomorrow", account="Work", location="SF", include_reminders=False)
    assistant_tools.get_upcoming.assert_awaited_once_with(calendar_only=True, period="tomorrow", account="Work")
    email_tools.summarize_emails.assert_awaited_once_with(period="tomorrow", account="Work")
    web_tools.get_weather.assert_awaited_once_with("SF", period="tomorrow")
    imessage_tools.summarize_messages.assert_not_awaited()
    assert "Messages unknown for the requested account/title filter" in result


def test_title_filter_is_not_silently_dropped_for_activity(sources):
    result = dispatch(query="Exam", include_reminders=False, include_weather=False)
    assistant_tools.get_upcoming.assert_awaited_once_with(calendar_only=True, period="today", query="Exam")
    email_tools.summarize_emails.assert_not_awaited()
    imessage_tools.summarize_messages.assert_not_awaited()
    assert "Email unknown for the requested title filter" in result


def test_undated_partial_and_account_identity_limits_are_explicit(sources):
    sources.view.update(status="truncated", stored=1, total=3)
    sources.undated = [{"title": "No date"}]
    result = only_reminders()
    assert "No date" in result and "1 stored of 3 total" in result
    result = only_reminders(account="Work")
    assert "cache lacks account identity on undated rows" in result and "- No date" not in result


def test_stale_undated_snapshot_is_not_reused(sources):
    sources.view["freshness"] = "stale"
    sources.undated = [{"title": "Old undated"}]
    result = only_reminders()
    assert "Undated reminders unknown" in result and "Old undated" not in result


def test_default_reminder_horizon_is_independent_of_calendar_days(sources):
    sources.rows = [reminder("Day two", 2 * 86400), reminder("Day four", 4 * 86400)]
    result = dispatch(days=1, reminder_days=3, include_activity=False, include_weather=False)
    assert "Day two" in result and "Day four" not in result
    assistant_tools.get_upcoming.assert_awaited_once_with(calendar_only=True, period="today")


@pytest.mark.parametrize("args", [{"days": 0}, {"days": 15}, {"reminder_days": 61},
                                  {"period": "nonsense unsupported range"}, {"account": 5},
                                  {"include_email": "false"}, {"include_overdue": "false"}])
def test_invalid_constraints_fail_before_any_read(sources, args):
    assert dispatch(**args).startswith("(error")
    assert sources.reads == []
    assistant_tools.get_upcoming.assert_not_awaited()


def test_source_failure_empty_reply_and_timeout_are_named_unknown(sources, monkeypatch):
    assistant_tools.get_upcoming.side_effect = RuntimeError("synthetic source failure")
    email_tools.summarize_emails.return_value = ""
    async def stalled(**args):
        await asyncio.sleep(1)
    imessage_tools.summarize_messages.side_effect = stalled
    monkeypatch.setattr(everyday, "_PER_SOURCE_TIMEOUT", 0.01)
    result = dispatch(include_reminders=False, include_weather=False)
    assert "Schedule unavailable" in result and "Email returned no usable evidence" in result
    assert "Messages took too long" in result and "source outcome unknown" in result


def test_stale_email_and_failed_messages_do_not_call_summaries(sources, monkeypatch):
    monkeypatch.setattr(email_tools, "_headers_at", STAMP - 1201)
    monkeypatch.setattr(imessage_tools, "messages_sync_state", lambda: "unavailable")
    result = dispatch(include_calendar=False, include_reminders=False, include_weather=False)
    assert "Email unknown" in result and "Messages unknown" in result
    email_tools.summarize_emails.assert_not_awaited()
    imessage_tools.summarize_messages.assert_not_awaited()


def test_unknown_past_calendar_does_not_substitute_today(sources):
    result = dispatch(period="yesterday", include_reminders=False, include_activity=False, include_weather=False)
    assert "Calendar unknown for a past range" in result
    assistant_tools.get_upcoming.assert_not_awaited()


def test_activity_exclusion_dominates_individual_activity_defaults(sources):
    result = dispatch(include_activity=False)
    email_tools.summarize_emails.assert_not_awaited()
    imessage_tools.summarize_messages.assert_not_awaited()
    assert "Email:" not in result and "Messages:" not in result


def test_other_account_stale_wisp_record_is_not_counted_in_scoped_output(sources):
    sources.rows = [reminder("Old personal record", -100 * 86400, source="manual", account="Personal"),
                    reminder("Selected work record", 3600)]
    result = only_reminders(account="Work")
    assert "Selected work record" in result
    assert "Old personal record" not in result and "older Wisp-only records withheld" not in result


def test_weather_failure_remains_unknown_without_dropping_other_sources(sources):
    web_tools.get_weather.side_effect = RuntimeError("synthetic provider unavailable")
    result = dispatch(location="SF", include_reminders=False)
    assert "Weather unavailable" in result and "source outcome unknown" in result
    assert "Verified calendar item" in result and "Cached email subject" in result
