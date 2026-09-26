"""Synthetic calendar interval and reminder clarification regressions."""
from __future__ import annotations

from datetime import datetime
import time
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.agent.loop import _calendar_create_preview
from service.assistant import outbox
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.reminder_intent import has_unsupported_alert_clock
from service.tasks.compiler import compile_reminder_create
from service.tasks.engine import prepare_task_turn
from service.tasks.temporal import resolve_named_time
from service.tools.assistant_tools import add_calendar_event, calendar_interval_label


NOW = datetime(2026, 9, 25, 16)


def test_range_reply_cannot_create_reminder_at_end_or_wrong_date(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")
    try:
        sid = sessions.create_session()
        first = prepare_task_turn(
            sessions, sid, "Set a reminder on September 28th from 6-7pm to study",
            assistant_store=assistant, now=NOW)
        assert first is not None and not first.executable
        assert first.plan.missing_slots == ["temporal.time"]

        reply = prepare_task_turn(
            sessions, sid, "September 28th from 6-7pm",
            assistant_store=assistant, now=NOW)
        assert reply is not None and reply.event == "clock_clarification"
        assert not reply.executable and not reply.plan.temporal.absolute_iso
        assert reply.plan.steps == []

        exact = prepare_task_turn(
            sessions, sid, "September 28th at 6pm",
            assistant_store=assistant, now=NOW)
        assert exact is not None and exact.executable
        assert exact.plan.temporal.absolute_iso == "2026-09-28T18:00"
        assert exact.plan.steps[0].args["when_iso"] == "2026-09-28T18:00"
    finally:
        sessions._db.close()
        assistant._db.close()


@pytest.mark.parametrize("phrase", [
    "September 28th from 6-7pm",
    "September 28, 6-7 PM",
    "Sept 28 from 6 to 7pm",
    "September 28th from 6pm to 7pm",
    "September 28th from 6pm-7pm",
    "September 28th from 6:00pm to 7:00pm",
])
def test_named_date_range_never_falls_back_to_end_clock(phrase):
    assert has_unsupported_alert_clock(phrase, time_answer=True)
    assert resolve_named_time(phrase, now=NOW) == (None, "")


@pytest.mark.parametrize("range_text", ["6pm to 7pm", "6pm-7pm", "6:00pm to 7:00pm"])
def test_spelled_range_keeps_reminder_subject_and_waits_for_one_alert(tmp_path, range_text):
    sessions = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")
    try:
        sid = sessions.create_session()
        first = prepare_task_turn(sessions, sid,
            f"Set a reminder on September 28th from {range_text} to study",
            assistant_store=assistant, now=NOW)
        assert first is not None and not first.executable
        assert first.plan.subject.value == "study"
        assert first.plan.missing_slots == ["temporal.time"]
        reply = prepare_task_turn(sessions, sid,
            f"September 28th from {range_text}", assistant_store=assistant, now=NOW)
        assert reply is not None and not reply.executable
        assert reply.event == "clock_clarification"
        assert reply.plan.subject.value == "study"
        assert not reply.plan.temporal.absolute_iso
    finally:
        sessions._db.close()
        assistant._db.close()


def test_named_date_is_preserved_for_single_clock_and_subject_tail():
    assert not has_unsupported_alert_clock("2026-09-28 at 6pm", time_answer=True)
    assert resolve_named_time("September 28th at 6pm", now=NOW)[0] == datetime(2026, 9, 28, 18)
    assert resolve_named_time("2026-09-28 at 6pm", now=NOW)[0] == datetime(2026, 9, 28, 18)
    plan = compile_reminder_create("Set a reminder to study September 28 at 6pm", now=NOW)
    assert plan is not None and plan.temporal.absolute_iso == "2026-09-28T18:00"
    assert plan.subject.value == "study"
    iso_plan = compile_reminder_create("Set a reminder to study 2026-09-28 at 6pm", now=NOW)
    assert iso_plan is not None and iso_plan.temporal.absolute_iso == "2026-09-28T18:00"
    assert iso_plan.subject.value == "study"
    zoned = NOW.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    resolved, _ = resolve_named_time("September 28 at 6pm", now=zoned)
    assert resolved is not None
    assert resolved.utcoffset().total_seconds() == -7 * 3600
    assert resolved.isoformat() == "2026-09-28T18:00:00-07:00"
    zoned_plan = compile_reminder_create("Set a reminder on September 28 at 6pm to study", now=zoned)
    assert zoned_plan is not None
    assert zoned_plan.temporal.absolute_iso == "2026-09-28T18:00-07:00"
    assert zoned_plan.temporal.timezone == "America/Los_Angeles"
    assert resolve_named_time("September 31 at 6pm", now=NOW) == (None, "")


@pytest.mark.asyncio
async def test_calendar_preview_payload_and_success_keep_both_endpoints(monkeypatch):
    when_iso = "2026-09-28T18:00:00-07:00"
    args = {"title": "Fixture", "when_iso": when_iso, "duration_min": 60}
    preview = _calendar_create_preview(args)
    interval = calendar_interval_label(when_iso, 60)
    assert "6:00 PM" in preview and "7:00 PM" in preview
    assert "Sep 28, 2026" in preview and "UTC-07:00" in preview
    assert interval in preview

    request = AsyncMock(return_value={"ok": True, "source_id": "fixture-native"})
    monkeypatch.setattr(outbox, "request", request)
    receipt = await add_calendar_event("Fixture", when_iso, 60)
    assert interval in receipt
    event_type, payload = request.await_args.args
    assert event_type == "create_calendar_event"
    assert payload == {
        "title": "Fixture", "when_ts": datetime.fromisoformat(when_iso).timestamp(),
        "duration_min": 60, "location": "",
    }


@pytest.mark.asyncio
async def test_invalid_duration_never_reaches_native_bridge(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(outbox, "request", request)
    result = await add_calendar_event("Fixture", "2026-09-28T18:00:00-07:00", 0)
    assert "nothing changed" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_dst_gap_and_fold_cannot_be_previewed_or_created(monkeypatch):
    zone = ZoneInfo("America/Los_Angeles")
    now = NOW.replace(tzinfo=zone)
    assert resolve_named_time("March 14 2027 at 2:30am", now=now) == (None, "")
    assert resolve_named_time("November 1 2026 at 1:30am", now=now) == (None, "")
    request = AsyncMock()
    monkeypatch.setattr(outbox, "request", request)
    try:
        with monkeypatch.context() as local_zone:
            local_zone.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            for when_iso in ("2027-03-14T02:30", "2026-11-01T01:30"):
                assert "invalid or ambiguous" in calendar_interval_label(when_iso, 60)
                result = await add_calendar_event("Fixture", when_iso, 60)
                assert "nothing changed" in result
    finally:
        time.tzset()
    request.assert_not_awaited()
