"""Strict tests for the typed reminder task boundary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import time
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_reminder_create
from service.tasks.engine import finish_task, prepare_task_turn
from service.tasks.executor import execute_task
from service.tasks.models import TaskPlan
from service.tasks.planner import plan_task
from service.tools.registry import REGISTRY, Tool


NOW = datetime(2026, 9, 3, 10, 0)


def _stores():
    temp = tempfile.TemporaryDirectory(prefix="wisp-typed-task-")
    root = Path(temp.name)
    return temp, SessionStore(root / "sessions.db"), AssistantStore(root / "assistant.db")


def test_versioned_golden_corpus_compiles_exactly():
    fixture = Path(__file__).resolve().parents[1] / "test_fixtures/task_engine/reminder_create_v1.json"
    data = json.loads(fixture.read_text())
    assert data["protocol"]["version"] == 1
    for case in data["cases"]:
        plan = compile_reminder_create(case["prompt"], now=NOW)
        if case.get("compile", "present") is None:
            assert plan is None, case["id"]
            continue
        assert plan is not None, case["id"]
        assert plan.intent == case["intent"], case["id"]
        assert plan.owner.value == case["owner"], case["id"]
        assert plan.subject.value == case["subject"], case["id"]
        assert plan.recipient is case["recipient"], case["id"]
        assert plan.channel.value == case["channel"], case["id"]
        assert plan.temporal.reference == case.get("reference", ""), case["id"]
        assert plan.temporal.absolute_iso == case.get("absolute_iso", ""), case["id"]
        assert plan.missing_slots == case["missing_slots"], case["id"]
        tools = [] if plan.missing_slots else [step.tool for step in plan_task(plan)]
        assert tools == case["tools"], case["id"]


def test_task_plan_round_trip_preserves_typed_nested_values():
    original = compile_reminder_create(
        "Remind me tomorrow night to call Mom", now=NOW)
    assert original is not None
    plan_task(original)
    restored = TaskPlan.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.subject.value == "call Mom"
    assert restored.recipient is None
    assert restored.temporal.absolute_iso == "2026-09-04T20:00"
    assert restored.steps[0].tool == "add_reminder"


def test_compiler_separates_owner_mentioned_person_and_recipient():
    prompt = ("can u send me a reminder before my move in date to ask trishy "
              "for a 48 or 64gb mac min")
    plan = compile_reminder_create(prompt, now=NOW)
    assert plan is not None
    assert plan.intent == "reminder.create"
    assert plan.owner.value == "user"
    assert plan.recipient is None
    assert plan.channel.value == "reminders"
    assert plan.subject.value == "ask trishy for a 48 or 64gb mac min"
    assert plan.temporal.reference == "my move in date"
    assert plan.missing_slots == ["temporal.lead_time"]


def test_daypart_defaults_are_canonical_and_do_not_clarify():
    expected = {
        "Remind me tomorrow morning to call Mom": "2026-09-04T09:00",
        "Remind me tomorrow afternoon to call Mom": "2026-09-04T15:00",
        "Remind me tomorrow evening to call Mom": "2026-09-04T18:00",
        "Remind me tomorrow night to call Mom": "2026-09-04T20:00",
        "Create a reminder tomorrow to send my vaccine report to UCSC":
            "2026-09-04T09:00",
    }
    for prompt, when_iso in expected.items():
        plan = compile_reminder_create(prompt, now=NOW)
        assert plan is not None, prompt
        assert plan.temporal.absolute_iso == when_iso, prompt
        assert plan.status == "ready", prompt


def test_negated_updates_and_compound_effects_stay_on_legacy_path():
    prompts = (
        "do not set an alarm at noon tomorrow",
        "delete my old reminders",
        "move my reminder to tomorrow",
        "Create a reminder tonight and tell Mom through Messages",
    )
    for prompt in prompts:
        assert compile_reminder_create(prompt, now=NOW) is None, prompt


def test_planner_produces_one_exact_effect_with_grounded_arguments():
    plan = compile_reminder_create(
        "Remind me tomorrow night to call Mom", now=NOW)
    assert plan is not None
    steps = plan_task(plan)
    assert len(steps) == 1
    assert steps[0].tool == "add_reminder"
    assert steps[0].args == {
        "title": "call Mom", "when_iso": "2026-09-04T20:00",
        "kind": "reminder",
    }
    assert steps[0].effect is True
    assert steps[0].max_calls == 1


def test_event_relative_followup_preserves_task_and_resolves_user_event():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        move_in = datetime(2026, 9, 12, 9, 0)
        assistant.sync_source("calendar", [{
            "source_id": "move-in", "kind": "event",
            "title": "UCSC Move-In Appointment", "when_ts": move_in.timestamp(),
            "context": "Personal",
        }])
        first = prepare_task_turn(
            sessions, sid,
            "send me a reminder before my move in date to ask Trishy for a Mac Mini",
            assistant_store=assistant, now=NOW)
        assert first is not None
        assert first.plan.status == "waiting_for_input"
        assert first.response == (
            "I haven’t created a reminder yet. How long before your move in date "
            "should I remind you?")

        correction = prepare_task_turn(
            sessions, sid, "oh i need it and do it on reminders",
            assistant_store=assistant, now=NOW)
        assert correction is not None
        assert correction.event == "clarification_repeated"
        assert correction.plan.recipient is None
        assert correction.plan.subject.value == "ask Trishy for a Mac Mini"

        second = prepare_task_turn(
            sessions, sid, "the day before", assistant_store=assistant, now=NOW)
        assert second is not None and second.executable
        assert second.plan.temporal.reference_id == "move-in"
        assert second.plan.temporal.absolute_iso == "2026-09-11T09:00"
        assert [step.tool for step in second.plan.steps] == ["add_reminder"]
        assert second.plan.steps[0].args["title"] == "ask Trishy for a Mac Mini"
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_missing_subject_and_time_are_filled_in_order_without_hijacking_questions():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        first = prepare_task_turn(
            sessions, sid, "remind me", assistant_store=assistant, now=NOW)
        assert first is not None
        assert first.response == "What should I remind you about?"
        assert prepare_task_turn(
            sessions, sid, "what is the weather?", assistant_store=assistant,
            now=NOW) is None
        subject = prepare_task_turn(
            sessions, sid, "call Mom", assistant_store=assistant, now=NOW)
        assert subject is not None
        assert subject.plan.subject.value == "call Mom"
        assert subject.response == "I haven’t created a reminder yet. When should I remind you?"
        ready = prepare_task_turn(
            sessions, sid, "tomorrow morning", assistant_store=assistant, now=NOW)
        assert ready is not None and ready.executable
        assert ready.plan.steps[0].args == {
            "title": "call Mom", "when_iso": "2026-09-04T09:00",
            "kind": "reminder",
        }
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_ambiguous_event_reference_fails_closed_without_effect_step():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        for number, hour in ((1, 9), (2, 12)):
            assistant.sync_source("calendar", [{
                "source_id": f"move-{number}", "kind": "event",
                "title": "Move-in", "when_ts": datetime(2026, 9, 12, hour).timestamp(),
                "context": f"Calendar {number}",
            }])
        # sync_source replaces one source's rows, so insert the second calendar
        # candidate under a separate source identity directly through one call.
        assistant.sync_source("calendar", [
            {"source_id": "move-1", "kind": "event", "title": "UCSC Move-in",
             "when_ts": datetime(2026, 9, 12, 9).timestamp(), "context": "Personal"},
            {"source_id": "move-2", "kind": "event", "title": "Move-in prep",
             "when_ts": datetime(2026, 9, 12, 12).timestamp(), "context": "School"},
        ])
        turn = prepare_task_turn(
            sessions, sid,
            "remind me a day before my move-in date to pack",
            assistant_store=assistant, now=NOW)
        assert turn is not None
        assert "several matching events" in turn.response.lower()
        assert turn.executable is False
        assert turn.plan.steps == []
        assert turn.plan.status == "waiting_for_input"

        chosen = prepare_task_turn(
            sessions, sid, "the UCSC move-in", assistant_store=assistant, now=NOW)
        assert chosen is not None and chosen.executable
        assert chosen.plan.temporal.reference_id == "move-1"
        assert chosen.plan.temporal.absolute_iso == "2026-09-11T09:00"
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_running_task_blocks_only_duplicate_attempts_not_unrelated_conversation():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        first = prepare_task_turn(
            sessions, sid, "remind me tomorrow to pack",
            assistant_store=assistant, now=NOW)
        assert first is not None and first.plan.status == "running"
        unrelated = prepare_task_turn(
            sessions, sid, "what is the weather?", assistant_store=assistant,
            now=NOW)
        assert unrelated is None
        retry = prepare_task_turn(
            sessions, sid, "retry", assistant_store=assistant, now=NOW)
        assert retry is not None
        assert retry.event == "duplicate_blocked"
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_executor_dry_run_never_calls_tool_and_reports_planned():
    plan = compile_reminder_create("remind me tomorrow to pack", now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = "running"
    original = REGISTRY["add_reminder"]
    fake = AsyncMock(return_value="Reminder set: should not execute")
    REGISTRY["add_reminder"] = Tool(
        "add_reminder", "fixture", original.parameters, "assistant_write", fake)
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    try:
        execution = asyncio.run(execute_task(
            plan, emit, type("Approver", (), {"confirm": AsyncMock()})(),
            test_mode=True))
    finally:
        REGISTRY["add_reminder"] = original
    assert execution.status == "planned"
    assert fake.await_count == 0
    assert [event["type"] for event in events] == ["tool_call", "tool_result"]


def test_executor_accepts_only_verified_reminder_receipt_and_blocks_repeat():
    plan = compile_reminder_create("remind me tomorrow to pack", now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = "running"
    temp = tempfile.TemporaryDirectory(prefix="wisp-task-receipt-")
    assistant = AssistantStore(Path(temp.name) / "assistant.db")
    original = REGISTRY["add_reminder"]

    async def persist_reminder(title, when_iso, kind="reminder"):
        assistant.add_manual(title, datetime.fromisoformat(when_iso).timestamp(), kind=kind)
        return "Reminder set: “pack” — Fri Sep 4 at 9:00 AM."

    fake = AsyncMock(side_effect=persist_reminder)
    REGISTRY["add_reminder"] = Tool(
        "add_reminder", "fixture", original.parameters, "assistant_write", fake)

    async def emit(_event):
        return None

    try:
        execution = asyncio.run(execute_task(
            plan, emit, type("Approver", (), {"confirm": AsyncMock()})(),
            assistant_store=assistant))
        assert execution.status == "completed"
        assert fake.await_count == 1
        plan.status = "completed"
        repeated = asyncio.run(execute_task(
            plan, emit, type("Approver", (), {"confirm": AsyncMock()})(),
            assistant_store=assistant))
        assert repeated.status == "failed"
        assert fake.await_count == 1
    finally:
        REGISTRY["add_reminder"] = original
        assistant._db.close()
        temp.cleanup()


def test_unverified_success_string_is_a_failure():
    plan = compile_reminder_create("remind me tomorrow to pack", now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = "running"
    temp = tempfile.TemporaryDirectory(prefix="wisp-task-no-receipt-")
    assistant = AssistantStore(Path(temp.name) / "assistant.db")
    original = REGISTRY["add_reminder"]
    fake = AsyncMock(return_value="Reminder set: “pack” — but nothing persisted")
    REGISTRY["add_reminder"] = Tool(
        "add_reminder", "fixture", original.parameters, "assistant_write", fake)

    async def emit(_event):
        return None

    try:
        execution = asyncio.run(execute_task(
            plan, emit, type("Approver", (), {"confirm": AsyncMock()})(),
            assistant_store=assistant))
        assert execution.status == "failed"
        assert "Reminder creation was not verified" in execution.response
    finally:
        REGISTRY["add_reminder"] = original
        assistant._db.close()
        temp.cleanup()


def test_legacy_summary_workflow_lookup_ignores_active_typed_task():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        turn = prepare_task_turn(
            sessions, sid, "remind me to pack",
            assistant_store=assistant, now=NOW)
        assert turn is not None and turn.plan.status == "waiting_for_input"
        assert sessions.active_task(sid)["id"] == turn.plan.id
        assert sessions.active_workflow(sid) is None
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_finish_task_writes_terminal_audit_state():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        turn = prepare_task_turn(
            sessions, sid, "remind me tomorrow to pack",
            assistant_store=assistant, now=NOW)
        assert turn is not None and turn.executable
        finish_task(sessions, sid, turn.plan, status="completed", result="receipt")
        assert sessions.active_task(sid) is None
        assert sessions.workflow_events(turn.plan.id)[-1]["event"] == "completed"
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_deterministic_compile_and_plan_p95_is_under_budget():
    durations = []
    for _ in range(500):
        started = time.perf_counter()
        plan = compile_reminder_create(
            "Remind me tomorrow evening to call Mom", now=NOW)
        assert plan is not None
        plan_task(plan)
        durations.append((time.perf_counter() - started) * 1000)
    durations.sort()
    p95 = durations[int(len(durations) * 0.95)]
    assert p95 < 50, f"deterministic compile+plan p95 was {p95:.3f}ms"


def test_agent_endpoint_dry_run_uses_typed_plan_without_model_or_effect():
    from service import main

    async def collect():
        response = await main.agent({
            "prompt": "Remind me tomorrow night to call Mom",
            "test_mode": True,
            "debug": False,
        })
        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
        return [json.loads(line[6:]) for line in raw.decode().splitlines()
                if line.startswith("data: ")]

    original = REGISTRY["add_reminder"]
    fake = AsyncMock(side_effect=AssertionError("dry run executed add_reminder"))
    REGISTRY["add_reminder"] = Tool(
        "add_reminder", "fixture", original.parameters, "assistant_write", fake)
    try:
        events = asyncio.run(collect())
    finally:
        REGISTRY["add_reminder"] = original

    task = next(event for event in events if event["type"] == "task_plan")
    assert task["trace"]["model_invocations"] == 0
    assert task["trace"]["engine_ms"] < 50
    calls = [event for event in events if event["type"] == "tool_call"]
    assert len(calls) == 1
    assert calls[0]["name"] == "add_reminder"
    assert calls[0]["args"]["title"] == "call Mom"
    assert calls[0]["args"]["when_iso"].endswith("T20:00")
    assert fake.await_count == 0
    assert not any(event["type"] == "routed" for event in events)
    assert events[-1]["type"] == "done"
