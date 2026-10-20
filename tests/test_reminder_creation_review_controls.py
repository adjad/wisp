"""Independent reminder review controls; isolated stores and synthetic tools."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import service.tools  # noqa: F401
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.tasks.engine import prepare_task_turn
from service.tasks.models import TaskPlan
from service.tasks.planner import plan_task


NOW = datetime(2026, 10, 19, 16, 9)
REQUEST = (
    "set a reminder on october 20th for vicor earnings report "
    "and another on the 28th for microsoft and google earnings report"
)


@pytest.fixture
def stores(tmp_path):
    sessions = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")
    try:
        yield sessions, assistant, sessions.create_session()
    finally:
        sessions._db.close()
        assistant._db.close()


def test_explicit_clock_answer_replaces_pending_all_day_mode(stores):
    sessions, assistant, sid = stores
    initial = prepare_task_turn(
        sessions, sid, "create an all-day reminder to review report",
        assistant_store=assistant, now=NOW,
    )
    assert initial is not None and not initial.executable
    assert initial.plan.parameters["all_day"].value is True

    answered = prepare_task_turn(
        sessions, sid, "tomorrow at 3pm", assistant_store=assistant, now=NOW,
    )
    assert answered is not None and answered.executable
    assert len(answered.plan.steps) == 1
    args = answered.plan.steps[0].args
    assert args["title"] == "review report"
    assert args["when_iso"] == "2026-10-20T15:00"
    assert args.get("all_day", False) is False
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("success_statuses", [["failed"], ["succeeded", "failed"], []])
async def test_restored_batch_cannot_override_verified_success_contract(
    monkeypatch, success_statuses,
):
    from service.tasks import executor

    plan = compile_task(REQUEST, now=NOW)
    assert plan is not None
    plan_task(plan)
    plan.status = "running"
    restored = TaskPlan.from_dict(plan.to_dict())
    restored.steps[0].success_statuses = success_statuses
    execute = AsyncMock(side_effect=AssertionError("invalid batch executed a tool"))
    claim = Mock(side_effect=AssertionError("invalid batch claimed an effect"))
    monkeypatch.setattr(executor, "run_tool", execute)

    result = await executor.execute_task(
        restored, AsyncMock(), SimpleNamespace(confirm=AsyncMock()),
        assistant_store=SimpleNamespace(active_between=lambda *args, **kwargs: []),
        on_claim=claim, now=NOW,
    )
    assert result.status == "failed"
    assert not result.tool_calls and not result.tool_results
    assert not restored.claimed_calls
    execute.assert_not_awaited()
    claim.assert_not_called()


@pytest.mark.parametrize("prompt", [
    "create an all-day reminder tomorrow at 3pm to review report",
    "remind me tomorrow at 3pm to review report all day",
    "set an all-day reminder on october 20th at 3pm for review report",
])
def test_initial_all_day_and_explicit_clock_conflict_never_executes(stores, prompt):
    sessions, assistant, sid = stores
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.parametrize("companion", [
    "mute my volume", "disable Wi-Fi", "frobnicate the gizmo",
])
def test_unknown_unquoted_companion_cannot_partially_create_reminder(stores, companion):
    sessions, assistant, sid = stores
    prompt = "set a reminder on october 20th for review and " + companion
    plan = compile_task(prompt, now=NOW)
    assert plan is None or plan.status != "ready"
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


@pytest.mark.parametrize("subject", [
    "review, mute my volume",
    "review then mute my volume",
    "review but disable Wi-Fi",
    "review. Mute my volume",
])
def test_unquoted_companion_separators_cannot_create_partial_reminder(stores, subject):
    sessions, assistant, sid = stores
    prompt = "set a reminder on october 20th for " + subject
    plan = compile_task(prompt, now=NOW)
    assert plan is None or plan.status != "ready"
    turn = prepare_task_turn(
        sessions, sid, prompt, assistant_store=assistant, now=NOW,
    )
    assert turn is None or not turn.executable
    assert turn is None or not turn.plan.steps
    assert assistant.active_between(0, datetime(2027, 1, 1).timestamp()) == []


def test_lowercase_company_conjunction_remains_one_exact_subject():
    plan = compile_task(REQUEST, now=NOW)
    assert plan is not None
    steps = plan_task(plan)
    assert [(step.args["title"], step.args["when_iso"]) for step in steps] == [
        ("vicor earnings report", "2026-10-20T09:00"),
        ("microsoft and google earnings report", "2026-10-28T09:00"),
    ]


@pytest.mark.parametrize("title", [
    "review and disable Wi-Fi",
    "email Sam and another on the 28th for a report",
    "review, mute my volume",
])
def test_fully_quoted_conjunction_is_literal_reminder_content(title):
    plan = compile_task(
        'set a reminder on october 20th for "' + title + '"', now=NOW,
    )
    assert plan is not None
    steps = plan_task(plan)
    assert len(steps) == 1
    assert steps[0].tool == "add_reminder"
    assert steps[0].args["title"] == title
