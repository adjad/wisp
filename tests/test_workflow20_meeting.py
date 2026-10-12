"""Private meeting proposals over fictional Calendar snapshots; no native effects."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from service.calendar_planning import exact_start, plan_private_block, select_occurrence
from service.tools import schedule_extras as S
from service.tools.registry import REGISTRY, classify_tool_outcome, run_tool


ZONE = "America/Los_Angeles"
NOW = dt.datetime(2026, 10, 12, 10, tzinfo=ZoneInfo(ZONE))


def stamp(value):
    return dt.datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(ZONE)).timestamp()


def event(title="Dentist", start="2026-10-13T10:00", end="2026-10-13T11:15", **extra):
    return {"id": "cal.dentist", "source_id": "native.dentist", "source": "calendar",
            "status": "active", "title": title, "when_ts": stamp(start),
            "end_ts": stamp(end) if end else None, "all_day": False, **extra}


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def calendar(monkeypatch):
    original_zone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", ZONE)
    time.tzset()
    monkeypatch.setattr(S, "dt", SimpleNamespace(datetime=Clock, time=dt.time, timedelta=dt.timedelta))
    monkeypatch.setattr(S, "_availability_timezone", lambda: ZONE)
    readiness = AsyncMock(return_value={"sources": [{"id": "calendar", "state": "ready"}]})
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", readiness)
    state = SimpleNamespace(rows=[event()], reads=[], readiness=readiness, sync={
        "available": True, "syncing": False, "last_sync": NOW.timestamp(),
        "diagnostics": {"coverage_start": stamp("2026-10-01T00:00"),
                        "coverage_end": stamp("2026-12-01T00:00")}})
    def snapshot(day, zone):
        assert zone == ZONE
        state.reads.append(day)
        # Keep unknown-ended earlier events, as the real snapshot does.
        lo = stamp(day + "T00:00")
        hi = (dt.datetime.fromisoformat(day) + dt.timedelta(days=1)).replace(tzinfo=ZoneInfo(ZONE)).timestamp()
        rows = [row for row in state.rows if row.get("when_ts", 0) < hi
                and (row.get("end_ts") is None or row["end_ts"] > lo or row["when_ts"] >= lo)]
        return {"commitments": rows, "sources": {"calendar": state.sync}}
    monkeypatch.setattr(S, "_store", SimpleNamespace(today_snapshot=snapshot))
    async def forbidden(*args, **kwargs):
        raise AssertionError("Meeting read must never create or send anything")
    monkeypatch.setattr("service.assistant.outbox.request", forbidden)
    yield state
    if original_zone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_zone
    time.tzset()


def call(**args):
    return asyncio.run(run_tool(REGISTRY["plan_meeting"],
                               {"title": "Meeting with Morgan", "duration_min": 30, **args}))


def payload(text):
    return json.loads(text.split(": ", 1)[1])


@pytest.mark.parametrize("offset,expected", [(0, "11:15"), (15, "11:30"), (60, "12:15"), (1440, "11:15")])
def test_event_end_plus_elapsed_offset_has_exact_interval(calendar, offset, expected):
    text = call(after_event_title="dentist", event_period="2026-10-13", offset_minutes=offset)
    plan = payload(text)
    assert plan["status"] == "ready"
    day = "2026-10-14" if offset == 1440 else "2026-10-13"
    assert plan["private_block"] == {"title": "Meeting with Morgan", "duration_min": 30,
                                     "when_iso": f"{day}T{expected}:00-07:00"}
    assert plan["reference"]["id"] == "cal.dentist"
    assert "not invited" in plan["effect"] and "No event created" in plan["effect"]
    assert "unverified" in plan["scope"]
    calendar.readiness.assert_awaited_once_with(("calendar",))


@pytest.mark.parametrize("period", ["today", "tomorrow", "this week", "next week", "this month", "next month", "2026-10-13"])
def test_bounded_reference_period_is_read(calendar, period):
    text = call(after_event_title="Dentist", event_period=period)
    assert calendar.reads and len(calendar.reads) <= 31
    assert classify_tool_outcome("plan_meeting", text).status in {"succeeded", "needs_input"}


def test_explicit_start_conflict_and_touching_boundary(calendar):
    conflict = payload(call(when_iso="2026-10-13T11:00:00-07:00"))
    assert conflict["status"] == "conflict"
    assert conflict["conflicts"][0]["id"] == "cal.dentist"
    assert classify_tool_outcome("plan_meeting", call(when_iso="2026-10-13T11:00")).status == "needs_input"
    assert payload(call(when_iso="2026-10-13T11:15"))["status"] == "ready"
    assert payload(call(when_iso="2026-10-13T09:30"))["status"] == "ready"


@pytest.mark.parametrize("changes", [{"end_ts": None}, {"end_ts": float("nan")},
    {"end_ts": stamp("2026-10-13T09:00")}, {"end_ts": stamp("2026-10-13T10:00")}, {"all_day": True}])
def test_reference_missing_or_invalid_end_and_all_day_asks(calendar, changes):
    calendar.rows[0].update(changes)
    result = call(after_event_title="Dentist", event_period="tomorrow")
    assert classify_tool_outcome("plan_meeting", result).status == "needs_input"
    assert "private_block" not in result


def test_duplicate_occurrences_need_exact_id_and_never_display_dedupe(calendar):
    calendar.rows.append(event(id="cal.other", source_id="native.other"))
    text = call(after_event_title="Dentist", event_period="tomorrow")
    assert payload(text)["status"] == "needs_input"
    assert len(payload(text)["choices"]) == 2
    selected = payload(call(after_event_title="Dentist", event_period="tomorrow", expected_event_id="cal.other"))
    assert selected["status"] == "ready" and selected["reference"]["id"] == "cal.other"
    assert payload(call(after_event_title="Dentist", event_period="tomorrow", expected_event_id="cal."))["status"] == "needs_input"


def test_exact_title_precedes_partial_and_title_does_not_select_reminders(calendar):
    calendar.rows += [event(title="Dentist preparation", id="cal.prep"),
                      event(source="reminders", id="rem.dentist")]
    result = payload(call(after_event_title="Dentist", event_period="tomorrow"))
    assert result["reference"]["id"] == "cal.dentist"


def test_overnight_overlap_and_target_end_day_are_checked(calendar):
    calendar.rows = [event(start="2026-10-13T23:50", end="2026-10-14T00:20")]
    result = payload(call(when_iso="2026-10-13T23:45", duration_min=60))
    assert result["status"] == "conflict"
    assert calendar.reads == ["2026-10-13", "2026-10-14"]


@pytest.mark.parametrize("end,all_day", [(None, False), (None, True), ("2026-10-13T00:00", True)])
def test_unknown_busy_ends_do_not_invent_hour_or_free_slot(calendar, end, all_day):
    calendar.rows = [event(start="2026-10-13T00:00", end=end, all_day=all_day)]
    result = payload(call(when_iso="2026-10-13T13:00"))
    assert result["status"] == "needs_input"


def test_all_day_known_end_blocks_and_missing_end_after_target_does_not(calendar):
    calendar.rows = [event(start="2026-10-13T00:00", end="2026-10-14T00:00", all_day=True)]
    assert payload(call(when_iso="2026-10-13T13:00"))["status"] == "conflict"
    calendar.rows = [event(start="2026-10-13T14:00", end=None)]
    assert payload(call(when_iso="2026-10-13T13:00"))["status"] == "ready"


@pytest.mark.parametrize("sync", [{"available": False}, {"syncing": True}, {"last_sync": NOW.timestamp()-181},
    {"last_sync": NOW.timestamp()+1}, {"last_sync": None}, {"diagnostics": {}},
    {"diagnostics": {"coverage_start": stamp("2026-10-14T00:00"), "coverage_end": stamp("2026-11-01T00:00")}}])
def test_stale_incomplete_unavailable_calendar_fails_closed(calendar, sync):
    calendar.sync.update(sync)
    text = call(when_iso="2026-10-13T13:00")
    assert classify_tool_outcome("plan_meeting", text).status == "failed"
    assert "private_block" not in text


@pytest.mark.parametrize("state", ["syncing", "unavailable", "denied"])
def test_not_ready_sources_never_read_store(calendar, state):
    calendar.readiness.return_value = {"sources": [{"id": "calendar", "state": state}]}
    assert "error" in call(when_iso="2026-10-13T13:00")
    assert calendar.reads == []


@pytest.mark.parametrize("args", [{}, {"when_iso": "2026-10-13T13:00", "after_event_title": "Dentist"},
    {"after_event_title": "Dentist"}, {"after_event_title": "Dentist", "event_period": "whenever"}])
def test_ambiguous_request_has_no_source_effects(calendar, args):
    text = call(**args)
    assert classify_tool_outcome("plan_meeting", text).status == "needs_input"
    assert calendar.reads == [] and calendar.readiness.await_count == 0


@pytest.mark.parametrize("args", [
    {"duration_min": 0}, {"duration_min": True}, {"duration_min": 1441}, {"title": " "},
    {"offset_minutes": -1}, {"offset_minutes": True}, {"offset_minutes": 1441},
    {"when_iso": "2026-10-13T13:00", "event_period": "tomorrow"},
    {"when_iso": "2026-10-13T13:00", "offset_minutes": 1},
    {"when_iso": "2026-10-13T13:00:01"}, {"when_iso": "2026-10-13T13:00-08:00"},
    {"when_iso": "2026-11-01T01:30-07:00"}, {"when_iso": "2027-03-14T02:30"},
    {"when_iso": "2026-10-11T13:00"}, {"when_iso": "tomorrow at 1"}])
def test_invalid_inputs_never_yield_executable_proposal(calendar, args):
    text = call(**args)
    assert classify_tool_outcome("plan_meeting", text).status == "failed"
    assert "private_block" not in text


def test_changing_same_occurrence_across_snapshots_fails_closed():
    with pytest.raises(ValueError, match="changed between snapshots"):
        select_occurrence([event(), event(end="2026-10-13T11:30")], "Dentist",
                          bounds=(stamp("2026-10-13T00:00"), stamp("2026-10-14T00:00")))


def advancing_refresh(calendar, monkeypatch, before, after):
    clock = [before]
    class MovingClock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(S, "dt", SimpleNamespace(datetime=MovingClock, time=dt.time, timedelta=dt.timedelta))
    async def refresh(sources):
        assert sources == ("calendar",)
        await asyncio.sleep(0)
        clock[0] = after
        calendar.sync.update(last_sync=after.timestamp(), snapshot_started_at=after.timestamp())
        return {"sources": [{"id": "calendar", "state": "ready"}]}
    calendar.readiness.side_effect = refresh


def test_refresh_newer_than_request_clock_is_fresh(calendar, monkeypatch):
    advancing_refresh(calendar, monkeypatch, NOW, NOW + dt.timedelta(seconds=2))
    result = payload(call(when_iso="2026-10-13T13:00"))
    assert result["status"] == "ready"


def test_refresh_crossing_midnight_recomputes_relative_reference_day(calendar, monkeypatch):
    before = dt.datetime(2026, 10, 12, 23, 59, 59, tzinfo=ZoneInfo(ZONE))
    after = before + dt.timedelta(seconds=2)
    advancing_refresh(calendar, monkeypatch, before, after)
    calendar.rows = [event(start="2026-10-14T10:00", end="2026-10-14T11:15")]
    result = payload(call(after_event_title="Dentist", event_period="tomorrow"))
    assert calendar.reads == ["2026-10-14"]
    assert result["private_block"]["when_iso"] == "2026-10-14T11:15:00-07:00"


def test_start_that_elapses_during_refresh_is_rejected(calendar, monkeypatch):
    advancing_refresh(calendar, monkeypatch, NOW, NOW + dt.timedelta(minutes=2))
    result = call(when_iso="2026-10-12T10:01")
    assert classify_tool_outcome("plan_meeting", result).status == "failed"
    assert "private_block" not in result


@pytest.mark.parametrize("change", ["insert", "delete"])
@pytest.mark.parametrize("scope", ["overnight", "reference_range"])
def test_distinct_id_insert_or_delete_between_day_reads_rejects_mixed_generation(calendar, monkeypatch, change, scope):
    original_snapshot = S._store.today_snapshot
    calendar.sync["snapshot_started_at"] = NOW.timestamp() - 2
    other = event(title="Inserted or deleted", id="cal.distinct", source_id="native.distinct",
                  start="2026-10-13T23:50", end="2026-10-13T23:55")
    if change == "delete":
        calendar.rows.append(other)
    calls = []
    def changing_snapshot(day, zone):
        if calls:
            calendar.sync["snapshot_started_at"] = NOW.timestamp() - 1
            if change == "insert":
                calendar.rows.append(other)
            else:
                calendar.rows = [r for r in calendar.rows if r["id"] != "cal.distinct"]
        calls.append(day)
        return json.loads(json.dumps(original_snapshot(day, zone)))
    monkeypatch.setattr(S, "_store", SimpleNamespace(today_snapshot=changing_snapshot))
    args = ({"when_iso": "2026-10-13T23:45", "duration_min": 60} if scope == "overnight" else
            {"after_event_title": "Dentist", "event_period": "this week"})
    result = call(**args)
    assert len(calls) == 2
    assert "sync changed between snapshots" in result
    assert classify_tool_outcome("plan_meeting", result).status == "failed"
    assert "private_block" not in result


def test_receipt_metadata_change_rejected_even_without_generation_field(calendar, monkeypatch):
    original_snapshot = S._store.today_snapshot
    calls = []
    def changing_snapshot(day, zone):
        if calls:
            calendar.sync["last_sync"] = NOW.timestamp() - 1
        calls.append(day)
        return json.loads(json.dumps(original_snapshot(day, zone)))
    monkeypatch.setattr(S, "_store", SimpleNamespace(today_snapshot=changing_snapshot))
    result = call(when_iso="2026-10-13T23:45", duration_min=60)
    assert "sync changed between snapshots" in result
    assert "private_block" not in result


def test_elapsed_duration_across_dst_uses_real_end_offset():
    start = exact_start("2026-11-01T00:30-07:00", ZONE)
    result = plan_private_block([], title="Private block", start_ts=start,
        duration_min=120, timezone=ZONE, now_ts=NOW.timestamp())
    assert result["end_iso"] == "2026-11-01T01:30:00-08:00"


def test_registry_contract_keeps_private_plan_read_only_and_existing_write_approved(calendar):
    from service.safety.policy import decide
    tool = REGISTRY["plan_meeting"]
    assert tool.category == "assistant_read"
    assert set(tool.parameters["required"]) == {"title", "duration_min"}
    plan = payload(call(when_iso="2026-10-13T13:00"))
    block = plan["private_block"]
    assert set(block) == {"title", "when_iso", "duration_min"}
    create = REGISTRY["add_calendar_event"]
    assert create.category == "calendar_write"
    assert decide(create.category, block, tool=create.name).tier == "confirm"
    assert "attendees" not in create.parameters["properties"]
    result = asyncio.run(run_tool(tool, {"title": "Private", "duration_min": 30,
                                       "when_iso": "2026-10-13T13:00", "attendees": ["morgan@example.com"]}))
    assert result.startswith("(error")


@pytest.fixture
def host_request(calendar, monkeypatch):
    from service.memory.capture import current_source
    handle = current_source.set({"source_type": "user_request", "source_id": "request.morgan",
                                "session_id": "session.morgan", "quote": "Create a private block after the dentist tomorrow."})
    S._meeting_capabilities.clear()
    clock = [1000.0]
    monkeypatch.setattr(S, "_capability_clock", lambda: clock[0])
    state = SimpleNamespace(clock=clock, calendar=calendar, source=current_source)
    try:
        yield state
    finally:
        S._meeting_capabilities.clear()
        current_source.reset(handle)


def proposal_args(**changes):
    return payload(call(when_iso="2026-10-13T13:00", **changes))["commit_args"]


def commit(args):
    return asyncio.run(S.commit_private_meeting(**args))


def approved_commit(args):
    with S.approved_private_meeting(args):
        return commit(args)


def test_host_proposal_issues_exact_commit_args_and_meaningful_private_review(host_request):
    args = payload(call(after_event_title="Dentist", event_period="tomorrow", offset_minutes=15))["commit_args"]
    assert set(args) == {"capability_token", "title", "when_iso", "duration_min"}
    assert re.fullmatch(r"wisp_meeting_cap_[0-9a-f]{64}", args["capability_token"])
    assert args["when_iso"] == "2026-10-13T11:30:00-07:00" and args["duration_min"] == 30
    review = S.private_meeting_review(args)
    assert all(value in review for value in ("Meeting with Morgan", "2026-10-13T11:30:00-07:00",
        "2026-10-13T12:00:00-07:00", "America/Los_Angeles", "30 minutes", "Dentist", "cal.dentist", "11:15"))
    assert "No attendees will be invited" in review and args["capability_token"] not in review
    assert re.fullmatch(r"[0-9a-f]{64}", S._meeting_capabilities[args["capability_token"]]["evidence"])
    assert REGISTRY["commit_private_meeting"].category == "calendar_write"
    from service.safety.policy import decide
    assert decide("calendar_write", args, tool="commit_private_meeting").tier == "confirm"


def test_read_only_caller_without_host_context_gets_no_capability(calendar):
    from service.memory.capture import current_source
    handle = current_source.set(None)
    try:
        result = payload(call(when_iso="2026-10-13T13:00"))
        assert result["status"] == "ready" and "commit_args" not in result
        assert "host request" in result["commit_unavailable"]
    finally:
        current_source.reset(handle)


@pytest.mark.parametrize("quote", ["Schedule a meeting with Morgan after the dentist tomorrow.",
    "Preview a private calendar block after the dentist tomorrow.", "Do not create a private calendar block.",
    "Please don't create a private block.", 'Create a meeting named "private calendar block".',
    "Create a private block and invite Morgan.", "Create a private block, but don't actually create it.",
    "Explain how to create a private block.", "Create a private block while inviting Morgan.",
    "Create a private block and add Morgan as a guest."])
def test_only_explicit_private_creation_human_request_can_issue_capability(host_request, quote):
    source = host_request.source.get()
    handle = host_request.source.set({**source, "quote": quote})
    try:
        result = payload(call(when_iso="2026-10-13T13:00"))
        assert result["status"] == "ready" and "commit_args" not in result
        assert "explicitly authorize" in result["commit_unavailable"]
    finally:
        host_request.source.reset(handle)


@pytest.mark.parametrize("quote", ["Create a private calendar block after the dentist tomorrow.",
    "Please add a private block for Morgan.", "Could you schedule a private block after the dentist?",
    "Find a time and then create a private block.", "Create a private block; don't invite anyone.",
    "Create a private block with no attendees.", "Create a private personal calendar block."])
def test_explicit_private_creation_phrasings_acquire_only_private_authority(host_request, quote):
    source = host_request.source.get()
    handle = host_request.source.set({**source, "quote": quote})
    try:
        args = proposal_args()
        assert "Private block only" in S.private_meeting_review(args)
    finally:
        host_request.source.reset(handle)


@pytest.mark.parametrize("field,value", [("title", "Different meeting"), ("when_iso", "2026-10-13T13:01:00-07:00"),
    ("duration_min", 45), ("duration_min", True), ("capability_token", "wisp_meeting_cap_" + "0" * 64),
    ("title", " Meeting with Morgan"), ("when_iso", "2026-10-13T13:00")])
def test_forged_or_changed_commit_arguments_never_offer_approval(host_request, field, value):
    args = proposal_args()
    args[field] = value
    with pytest.raises(ValueError):
        S.private_meeting_review(args)
    with pytest.raises(ValueError):
        with S.approved_private_meeting(args):
            pytest.fail("Invalid arguments entered the approval context")
    assert commit(args).startswith("(error:")


@pytest.mark.parametrize("change", [{"source_id": "different.request"}, {"session_id": "different.session"},
    {"quote": "Changed original user request"}, {"source_type": "email"}, None])
def test_capability_is_bound_to_exact_host_origin(host_request, change):
    args = proposal_args()
    before = host_request.source.get()
    handle = host_request.source.set(None if change is None else {**before, **change})
    try:
        with pytest.raises(ValueError):
            S.private_meeting_review(args)
        assert commit(args).startswith("(error:")
    finally:
        host_request.source.reset(handle)


def test_direct_call_without_host_approval_does_not_consume_or_dispatch(host_request):
    args = proposal_args()
    assert "requires exact host approval" in commit(args)
    assert S.private_meeting_review(args)
    assert not S._meeting_capabilities[args["capability_token"]]["consumed"]


def test_approval_context_restores_and_does_not_follow_mutated_args(host_request):
    args = proposal_args()
    with S.approved_private_meeting(args):
        args["duration_min"] = 45
        assert commit(args).startswith("(error:")
    args["duration_min"] = 30
    assert "requires exact host approval" in commit(args)


def test_success_uses_exact_native_payload_and_single_use_receipt(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": "native.created"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    result = approved_commit(args)
    assert result.startswith("Private calendar block created: ")
    receipt = payload(result)
    assert receipt["source_id"] == "native.created" and receipt["end_iso"] == "2026-10-13T13:30:00-07:00"
    assert "no attendees invited" in receipt["scope"] and args["capability_token"] not in result
    native.assert_awaited_once_with("create_calendar_event", {"title": args["title"], "when_ts": stamp("2026-10-13T13:00"),
        "duration_min": 30, "location": ""})
    assert commit(args).startswith("(error:")
    with pytest.raises(ValueError):
        S.private_meeting_review(args)
    assert native.await_count == 1


@pytest.mark.parametrize("native_result", [{"ok": True}, {"ok": 1, "source_id": "native.id"},
    {"ok": True, "source_id": ""}, {"ok": True, "status": "unknown", "source_id": "native.id"},
    {"ok": False, "status": "unknown", "error": "timeout"}, {"ok": False}, None, "saved",
    {"ok": True, "source_id": "native.id"}, {"ok": True, "status": "succeeded", "source_id": "native.id"},
    {"ok": True, "status": "succeeded", "error": "contradiction", "source_id": "native.id"},
    {"ok": True, "status": "succeeded", "error": "", "source_id": "native.id", "extra": "unexpected"}])
def test_malformed_or_uncertain_native_receipt_never_succeeds_or_retries(host_request, monkeypatch, native_result):
    args = proposal_args()
    native = AsyncMock(return_value=native_result)
    monkeypatch.setattr("service.assistant.outbox.request", native)
    result = approved_commit(args)
    assert result.startswith("(error:") and "outcome unknown" in result and "no retry" in result
    assert commit(args).startswith("(error:") and native.await_count == 1


def test_native_failure_and_exception_keep_capability_consumed(host_request, monkeypatch):
    native = AsyncMock(return_value={"ok": False, "status": "failed", "error": "Permission denied"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    args = proposal_args()
    assert "creation failed; no retry" in approved_commit(args)
    assert commit(args).startswith("(error:") and native.await_count == 1
    args = proposal_args()
    native.side_effect = RuntimeError(args["capability_token"])
    result = approved_commit(args)
    assert "outcome unknown" in result and args["capability_token"] not in result
    assert commit(args).startswith("(error:") and native.await_count == 2


@pytest.mark.parametrize("mutation", ["receipt", "reference_end", "reference_deleted", "conflict", "unique_row", "unavailable"])
def test_calendar_change_after_approval_consumes_without_native_effect(host_request, monkeypatch, mutation):
    args = payload(call(after_event_title="Dentist", event_period="tomorrow"))["commit_args"]
    native = AsyncMock(return_value={"ok": True, "source_id": "should.not.create"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    with S.approved_private_meeting(args):
        calendar = host_request.calendar
        if mutation == "receipt":
            calendar.sync["last_sync"] -= 1
        elif mutation == "reference_end":
            calendar.rows[0]["end_ts"] += 15 * 60
        elif mutation == "reference_deleted":
            calendar.rows.clear()
        elif mutation == "conflict":
            calendar.rows.append(event(id="cal.new", start="2026-10-13T11:20", end="2026-10-13T11:25"))
        elif mutation == "unique_row":
            calendar.rows.append(event(id="cal.other", start="2026-10-13T17:00", end="2026-10-13T18:00"))
        else:
            calendar.sync["available"] = False
        result = commit(args)
    assert result.startswith("(error:") and "no event created" in result
    native.assert_not_awaited()
    with pytest.raises(ValueError):
        S.private_meeting_review(args)


def test_expiration_before_approval_and_during_refresh_fails_closed(host_request, monkeypatch):
    args = proposal_args()
    host_request.clock[0] += S._MEETING_TTL_SECONDS
    with pytest.raises(ValueError):
        S.private_meeting_review(args)
    args = proposal_args()
    async def slow_refresh(sources):
        host_request.clock[0] += S._MEETING_TTL_SECONDS
        return {"sources": [{"id": "calendar", "state": "ready"}]}
    host_request.calendar.readiness.side_effect = slow_refresh
    assert "changed after approval" in approved_commit(args)


def test_host_context_change_during_refresh_prevents_dispatch(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    async def changed_context(sources):
        await asyncio.sleep(0)
        host_request.source.set({**host_request.source.get(), "source_id": "changed.during.refresh"})
        return {"sources": [{"id": "calendar", "state": "ready"}]}
    host_request.calendar.readiness.side_effect = changed_context
    assert "originating meeting request changed" in approved_commit(args)
    native.assert_not_awaited()
    assert S._meeting_capabilities[args["capability_token"]]["consumed"]


def test_revalidation_exception_consumes_without_dispatch(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    host_request.calendar.readiness.side_effect = RuntimeError(args["capability_token"])
    result = approved_commit(args)
    assert result.startswith("(error:") and "no event created" in result
    assert args["capability_token"] not in result
    native.assert_not_awaited()
    assert S._meeting_capabilities[args["capability_token"]]["consumed"]


def test_revalidation_cancellation_consumes_without_dispatch(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock()
    monkeypatch.setattr("service.assistant.outbox.request", native)
    host_request.calendar.readiness.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        approved_commit(args)
    native.assert_not_awaited()
    assert S._meeting_capabilities[args["capability_token"]]["consumed"]


def test_receipt_cannot_smuggle_capability_or_failed_contradictions(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": args["capability_token"]})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    result = approved_commit(args)
    assert "outcome unknown" in result and args["capability_token"] not in result
    args = proposal_args()
    native.return_value = {"ok": False, "status": "failed", "error": ""}
    assert "outcome unknown" in approved_commit(args)


def test_concurrent_commit_claims_only_one_native_effect(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock(return_value={"ok": True, "status": "succeeded", "error": "", "source_id": "native.once"})
    monkeypatch.setattr("service.assistant.outbox.request", native)
    async def refreshing(sources):
        await asyncio.sleep(0)
        return {"sources": [{"id": "calendar", "state": "ready"}]}
    host_request.calendar.readiness.side_effect = refreshing
    async def race():
        with S.approved_private_meeting(args):
            return await asyncio.gather(S.commit_private_meeting(**args), S.commit_private_meeting(**args))
    results = asyncio.run(race())
    assert sum(result.startswith("Private calendar block created: ") for result in results) == 1
    assert sum(result.startswith("(error:") for result in results) == 1
    assert native.await_count == 1


def test_native_cancellation_does_not_restore_authority(host_request, monkeypatch):
    args = proposal_args()
    native = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("service.assistant.outbox.request", native)
    with pytest.raises(asyncio.CancelledError):
        approved_commit(args)
    assert commit(args).startswith("(error:") and native.await_count == 1


def test_bounded_capability_state_fails_closed_and_prunes_expired(host_request, monkeypatch):
    monkeypatch.setattr(S, "_MEETING_CAPACITY", 2)
    proposal_args()
    proposal_args()
    assert len(S._meeting_capabilities) == 2
    assert "capacity reached" in call(when_iso="2026-10-13T13:00")
    host_request.clock[0] += S._MEETING_TTL_SECONDS
    proposal_args()
    assert len(S._meeting_capabilities) == 1


def test_capability_values_cannot_be_native_content_or_source_queries(host_request):
    args = proposal_args()
    for field in ("title", "when_iso", "after_event_title", "event_period", "expected_event_id"):
        request = {"when_iso": "2026-10-13T13:00", field: args["capability_token"]}
        assert "capability cannot appear" in call(**request)
    args["title"] = args["capability_token"]
    with pytest.raises(ValueError):
        S.private_meeting_review(args)


def test_recursive_capability_redaction_scrubs_keys_and_preserves_clean_identity(host_request):
    args = proposal_args()
    token = args["capability_token"]
    value = {token: [token, {"args": json.dumps(args)}, (token, 30)]}
    cleaned = S.redact_meeting_capabilities(value)
    assert token not in str(cleaned) and "private meeting capability" in str(cleaned)
    assert value[token][0] == token  # Never mutate ephemeral execution args.
    assert S.redact_meeting_capabilities(cleaned) is cleaned
    from service.tools.registry import DisplayOnlyToolResult
    display = DisplayOnlyToolResult("Publisher display", model_text="Safe model summary", artifact_kind="news")
    clean = {"display": display, "items": ["clean", 30]}
    assert S.redact_meeting_capabilities(clean) is clean
    assert S.redact_meeting_capabilities(display) is display
