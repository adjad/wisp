"""Strict Phase 2 coverage for typed reminder update/complete/delete tasks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.tasks.engine import finish_task, prepare_task_turn
from service.tasks.executor import execute_task
from service.tasks.models import TaskPlan
from service.tasks.planner import plan_task
from service.tools.registry import REGISTRY, Tool
from service.tools import assistant_tools


NOW = datetime(2026, 9, 3, 10, 0)


def _stores():
    temp = tempfile.TemporaryDirectory(prefix="wisp-typed-operations-")
    root = Path(temp.name)
    return temp, SessionStore(root / "sessions.db"), AssistantStore(root / "assistant.db")


def _add(store: AssistantStore, title: str, when: datetime) -> dict:
    return store.add_manual(title, when.timestamp())


def test_phase2_golden_corpus_compiles_and_plans_exactly():
    fixture = (Path(__file__).resolve().parents[1]
               / "test_fixtures/task_engine/reminder_operations_v1.json")
    data = json.loads(fixture.read_text())
    assert data["protocol"]["version"] == 1
    for case in data["cases"]:
        plan = compile_task(case["prompt"], now=NOW)
        assert plan is not None, case["id"]
        assert plan.intent == case["intent"], case["id"]
        assert plan.target.value == case["target"], case["id"]
        assert {key: slot.value for key, slot in plan.parameters.items()} == case["parameters"], case["id"]
        assert plan.temporal.absolute_iso == case.get("absolute_iso", ""), case["id"]
        assert plan.missing_slots == case.get("missing_slots", []), case["id"]
        if case["tool"]:
            plan.resolved_targets = [{
                "id": "fixture", "duplicate_ids": [], "title": "Dentist reminder",
                "when_ts": datetime(2026, 9, 5, 18).timestamp(), "source": "manual",
            }]
            assert [step.tool for step in plan_task(plan)] == [case["tool"]], case["id"]
        else:
            assert plan.status == "waiting_for_input", case["id"]


def test_delete_today_selects_reminders_only_and_exact_scope():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        wanted = _add(assistant, "Take medication", NOW.replace(hour=18))
        _add(assistant, "Tomorrow task", NOW + timedelta(days=1))
        assistant.sync_source("calendar", [{
            "source_id": "meeting", "kind": "event", "title": "Team meeting",
            "when_ts": NOW.replace(hour=14).timestamp(),
        }])
        turn = prepare_task_turn(
            sessions, sid, "delete my reminders for today",
            assistant_store=assistant, now=NOW)
        assert turn is not None and turn.executable
        assert turn.plan.intent == "reminder.delete"
        assert [item["id"] for item in turn.plan.resolved_targets] == [wanted["id"]]
        assert len(turn.plan.steps) == 1
        assert turn.plan.steps[0].tool == "clear_reminders"
        assert turn.plan.steps[0].args == {
            "scope": "today", "expected_ids": [wanted["id"]]}
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_specific_ambiguous_target_requires_exact_followup():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        _add(assistant, "Dentist cleaning", NOW + timedelta(days=1))
        _add(assistant, "Call dentist", NOW + timedelta(days=2))
        first = prepare_task_turn(
            sessions, sid, "mark my dentist reminder as done",
            assistant_store=assistant, now=NOW)
        assert first is not None and not first.executable
        assert first.event == "target_ambiguous"
        assert first.plan.steps == []
        second = prepare_task_turn(
            sessions, sid, "Dentist cleaning", assistant_store=assistant, now=NOW)
        assert second is not None and second.executable
        assert second.plan.steps[0].tool == "complete_reminder"
        assert second.plan.steps[0].args == {
            "title": "Dentist cleaning",
            "expected_id": second.plan.resolved_targets[0]["id"],
        }
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_no_match_and_vague_delete_fail_closed_without_tool():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        no_match = prepare_task_turn(
            sessions, sid, "delete my playstation reminder",
            assistant_store=assistant, now=NOW)
        assert no_match is not None
        assert no_match.event == "target_not_found"
        assert no_match.executable is False
        assert no_match.plan.steps == []
        assert "couldn’t find" in no_match.response

        other_sid = sessions.create_session()
        vague = prepare_task_turn(
            sessions, other_sid, "clear reminders", assistant_store=assistant, now=NOW)
        assert vague is not None
        assert vague.plan.missing_slots == ["scope"]
        assert vague.executable is False
        assert vague.response == (
            "Which reminders should I delete: today, tomorrow, past due, upcoming, or all?")
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_negated_or_compound_operations_never_partially_execute():
    for prompt in (
        "do not delete my reminders for today",
        "don't mark my dentist reminder done",
        "never move my vaccine reminder to today",
        "delete my dentist reminder and email Mom about it",
        "mark my dentist reminder done and tell Mom",
    ):
        assert compile_task(prompt, now=NOW) is None, prompt


def test_update_compiles_exact_target_and_canonical_change():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        row = _add(assistant, "Send vaccine report to UCSC", NOW + timedelta(days=1, hours=8))
        turn = prepare_task_turn(
            sessions, sid, "reschedule the vaccine reminder to tomorrow afternoon",
            assistant_store=assistant, now=NOW)
        assert turn is not None and turn.executable
        assert turn.plan.resolved_targets[0]["id"] == row["id"]
        assert turn.plan.steps[0].tool == "update_reminder"
        assert turn.plan.steps[0].args == {
            "title": "Send vaccine report to UCSC",
            "expected_id": row["id"],
            "when_iso": "2026-09-04T15:00",
        }
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_immediate_day_correction_uses_completed_typed_task_context():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        created = prepare_task_turn(
            sessions, sid, "remind me tomorrow evening to send my vaccine report to UCSC",
            assistant_store=assistant, now=NOW)
        assert created is not None and created.executable
        _add(assistant, "send my vaccine report to UCSC", NOW + timedelta(days=1, hours=8))
        finish_task(sessions, sid, created.plan, status="completed", result="fixture")

        corrected = prepare_task_turn(
            sessions, sid, "I mean today sorry", assistant_store=assistant, now=NOW)
        assert corrected is not None and corrected.executable
        assert corrected.plan.intent == "reminder.update"
        assert corrected.plan.target.source == "context"
        assert corrected.plan.steps[0].tool == "update_reminder"
        assert corrected.plan.steps[0].args == {
            "title": "send my vaccine report to UCSC",
            "expected_id": corrected.plan.resolved_targets[0]["id"],
            "day": "today"}
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_move_to_today_clarifies_when_preserved_time_has_passed():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        _add(assistant, "Vaccine report", NOW + timedelta(days=1) - timedelta(hours=2))
        first = prepare_task_turn(
            sessions, sid, "move my vaccine reminder to today",
            assistant_store=assistant, now=NOW)
        assert first is not None and not first.executable
        assert first.event == "past_time_clarification"
        assert first.response == (
            "That reminder’s original time has already passed today. What time today should I use?")
        second = prepare_task_turn(
            sessions, sid, "3 pm", assistant_store=assistant, now=NOW)
        assert second is not None and second.executable
        assert second.plan.steps[0].args["when_iso"] == "2026-09-03T15:00"
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_clear_reminders_aborts_if_selection_changes_after_preview():
    temp, _, assistant = _stores()
    try:
        first = _add(assistant, "First reminder", NOW + timedelta(days=1))
        _add(assistant, "Second reminder", NOW + timedelta(days=2))
        real = assistant_tools.assistant_store
        assistant_tools.assistant_store = assistant
        try:
            result = asyncio.run(assistant_tools.clear_reminders(
                "all", expected_ids=[first["id"]]))
        finally:
            assistant_tools.assistant_store = real
        assert result.startswith("(error: the selected reminder set changed")
        assert len(assistant.active_between()) == 2
    finally:
        assistant._db.close()
        temp.cleanup()


def _execute_with_fake(plan: TaskPlan, assistant: AssistantStore, fake,
                       *, allow_confirm: bool = True):
    name = plan.steps[0].tool
    original = REGISTRY[name]
    wrapped = AsyncMock(side_effect=fake)
    REGISTRY[name] = Tool(name, "fixture", original.parameters, original.category, wrapped)
    events: list[dict] = []

    async def emit(event):
        events.append(event)

    approver = type("Approver", (), {"confirm": AsyncMock(return_value=allow_confirm)})()
    try:
        execution = asyncio.run(execute_task(
            plan, emit, approver, assistant_store=assistant))
    finally:
        REGISTRY[name] = original
    return execution, wrapped, approver, events


def test_executor_verifies_update_complete_and_delete_postconditions():
    operations = [
        ("move my vaccine reminder to tomorrow afternoon", "Reminder updated: fixture", "update"),
        ("mark my vaccine reminder as done", "Marked fixture done.", "complete"),
        ("delete my vaccine reminder", "Cleared 1 reminder(s).", "delete"),
    ]
    for prompt, receipt, operation in operations:
        temp, sessions, assistant = _stores()
        try:
            sid = sessions.create_session()
            row = _add(assistant, "Vaccine report", NOW + timedelta(days=2))
            turn = prepare_task_turn(
                sessions, sid, prompt, assistant_store=assistant, now=NOW)
            assert turn is not None and turn.executable, operation

            async def fake(**_kwargs):
                if operation == "update":
                    assistant.update_schedule([row["id"]],
                                              datetime(2026, 9, 4, 15).timestamp())
                elif operation == "complete":
                    assistant.set_status(row["id"], "done")
                else:
                    assistant.set_status(row["id"], "dismissed")
                return receipt

            execution, wrapped, approver, events = _execute_with_fake(
                turn.plan, assistant, fake)
            assert execution.status == "completed", (operation, execution)
            assert wrapped.await_count == 1
            assert [event["type"] for event in events] == ["tool_call", "tool_result"]
            if operation == "delete":
                assert approver.confirm.await_count == 1
                action = approver.confirm.await_args.args[0]
                assert "Vaccine report" in action["preview"]
        finally:
            sessions._db.close()
            assistant._db.close()
            temp.cleanup()


def test_executor_rejects_success_text_without_state_change_for_every_phase2_effect():
    for prompt in (
        "move my vaccine reminder to tomorrow afternoon",
        "mark my vaccine reminder as done",
        "delete my vaccine reminder",
    ):
        temp, sessions, assistant = _stores()
        try:
            sid = sessions.create_session()
            _add(assistant, "Vaccine report", NOW + timedelta(days=2))
            turn = prepare_task_turn(
                sessions, sid, prompt, assistant_store=assistant, now=NOW)
            assert turn is not None and turn.executable

            async def lie(**_kwargs):
                return "Success"

            execution, _, _, _ = _execute_with_fake(turn.plan, assistant, lie)
            assert execution.status == "failed", prompt
            assert "not verified" in execution.response, prompt
        finally:
            sessions._db.close()
            assistant._db.close()
            temp.cleanup()


def test_delete_denial_is_terminal_and_does_not_execute_or_reconfirm():
    temp, sessions, assistant = _stores()
    try:
        sid = sessions.create_session()
        _add(assistant, "Vaccine report", NOW + timedelta(days=2))
        turn = prepare_task_turn(
            sessions, sid, "delete my vaccine reminder",
            assistant_store=assistant, now=NOW)
        assert turn is not None and turn.executable

        async def should_not_run(**_kwargs):
            raise AssertionError("denied delete executed")

        execution, wrapped, approver, _ = _execute_with_fake(
            turn.plan, assistant, should_not_run, allow_confirm=False)
        assert execution.status == "denied"
        assert wrapped.await_count == 0
        assert approver.confirm.await_count == 1
    finally:
        sessions._db.close()
        assistant._db.close()
        temp.cleanup()


def test_agent_endpoint_dry_runs_each_phase2_operation_without_model_or_effect():
    from service import main

    async def collect(prompt: str):
        response = await main.agent({
            "prompt": prompt, "test_mode": True, "debug": False,
        })
        raw = b""
        async for chunk in response.body_iterator:
            raw += chunk if isinstance(chunk, bytes) else chunk.encode()
        return [json.loads(line[6:]) for line in raw.decode().splitlines()
                if line.startswith("data: ")]

    temp, _, assistant = _stores()
    original_store = main.assistant_store
    try:
        # This endpoint uses the wall clock, unlike the injected NOW in unit
        # tests above. A fixed fixture eventually becomes an overdue reminder.
        _add(assistant, "Vaccine report", datetime.now() + timedelta(days=2))
        main.assistant_store = assistant
        expected = {
            "move my vaccine reminder to tomorrow afternoon": "update_reminder",
            "mark my vaccine reminder as done": "complete_reminder",
            "delete my vaccine reminder": "clear_reminders",
        }
        for prompt, tool in expected.items():
            original_tool = REGISTRY[tool]
            fake = AsyncMock(side_effect=AssertionError(f"dry run executed {tool}"))
            REGISTRY[tool] = Tool(
                tool, "fixture", original_tool.parameters, original_tool.category, fake)
            try:
                events = asyncio.run(collect(prompt))
            finally:
                REGISTRY[tool] = original_tool
            task = next(event for event in events if event["type"] == "task_plan")
            assert task["trace"]["model_invocations"] == 0, prompt
            calls = [event for event in events if event["type"] == "tool_call"]
            assert len(calls) == 1, prompt
            assert calls[0]["name"] == tool, prompt
            assert calls[0]["test_mode"] is True, prompt
            assert fake.await_count == 0, prompt
    finally:
        main.assistant_store = original_store
        assistant._db.close()
        temp.cleanup()
