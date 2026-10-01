"""A native source (Calendar/Reminders) must never read "syncing" forever.

Live finding: the app re-posted Reminders every minute with
`{"authorized": false, "syncing": true}` because macOS had never decided the
permission (EKAuthorizationStatus.notDetermined). The backend took the flag at
face value, so Reminders stayed "syncing" indefinitely: the Daily Summary held
back, the status pill spun, and nothing told the person to grant access.
"""
from __future__ import annotations

import pytest

from service.assistant import scheduler
from service.assistant import sync_status as S


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(scheduler, "_sync_status", {})
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(scheduler.time, "time", lambda: clock["now"])
    monkeypatch.setattr(S.time, "time", lambda: clock["now"])
    monkeypatch.setattr(scheduler, "STARTED_AT", clock["now"])
    return clock


def advance(clock, seconds):
    clock["now"] += seconds


def waiting(source="reminders"):
    scheduler.record_sync(source, 0, diagnostics={"authorized": False, "syncing": True,
                                                  "snapshot_started_at": 1.0})


def test_a_permission_that_is_never_answered_stops_reading_as_syncing(clean):
    waiting()
    assert S.source_status("reminders")["state"] == "syncing"
    # The app keeps re-posting the same waiting state once a minute.
    for _ in range(3):
        advance(clean, 60)
        waiting()
    status = S.source_status("reminders")
    assert status["state"] == "unavailable", status
    assert "System Settings" in status["reason"] and "Reminders" in status["reason"], status["reason"]


def test_re_posting_does_not_restart_the_clock(clean):
    waiting()
    advance(clean, S.SYNC_STALL_S - 5)
    waiting()
    advance(clean, 10)
    waiting()
    assert S.source_status("reminders")["state"] == "unavailable"


def test_a_short_wait_is_still_syncing(clean):
    waiting()
    advance(clean, S.SYNC_STALL_S - 1)
    assert S.source_status("reminders")["state"] == "syncing"


def test_granting_access_recovers_immediately(clean):
    waiting()
    advance(clean, 600)
    assert S.source_status("reminders")["state"] == "unavailable"
    scheduler.record_sync("reminders", 4, diagnostics={"authorized": True, "count": 4})
    assert S.source_status("reminders")["state"] == "ready"
    # A later wait starts a fresh window rather than inheriting the old one.
    waiting()
    assert S.source_status("reminders")["state"] == "syncing"


def test_an_app_that_never_reports_is_named_not_waited_on(clean):
    assert S.source_status("reminders")["state"] == "syncing"          # just started
    advance(clean, S.SYNC_STALL_S + 1)
    status = S.source_status("reminders")
    assert status["state"] == "unavailable"
    assert "app" in status["reason"].lower(), status["reason"]


def test_calendar_gets_the_same_protection(clean):
    waiting("calendar")
    advance(clean, S.SYNC_STALL_S + 1)
    status = S.source_status("calendar")
    assert status["state"] == "unavailable" and "Calendar" in status["reason"], status


def test_a_genuinely_ready_source_is_untouched(clean):
    scheduler.record_sync("calendar", 3, diagnostics={"authorized": True, "count": 3})
    advance(clean, 10_000)
    assert S.source_status("calendar")["state"] == "ready"


def test_denied_access_is_still_unavailable_immediately(clean):
    scheduler.record_sync("reminders", 0, diagnostics={"authorized": False, "syncing": False})
    assert S.source_status("reminders")["state"] == "unavailable"


def test_the_daily_summary_stops_waiting_on_a_stalled_source(clean):
    waiting()
    advance(clean, S.SYNC_STALL_S + 1)
    snapshot = S.summary_snapshot()
    assert "reminders" not in snapshot["pending"], snapshot["pending"]
    row = next(s for s in snapshot["sources"] if s["id"] == "reminders")
    assert row["state"] == "unavailable"
    assert "System Settings" in row["progress_detail"], row["progress_detail"]
