"""Behavior regressions for the content/time gaps in the September handoffs."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_task
from service.tasks.engine import prepare_task_turn
from service.tasks.planner import InvalidTaskPlan, plan_task
from service.tasks.reply_engine import prepare_task_turn_async
from service.tasks.source_readers import MailReader


NOW = datetime(2026, 9, 8, 10)


@pytest.fixture
def session(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    assistant = AssistantStore(tmp_path / "assistant.db")

    def turn(prompt):
        return prepare_task_turn(store, "s", prompt, assistant_store=assistant, now=NOW,
                                 contacts_resolver=lambda _: [{"name": "Mom", "handles": ["+15551234567"],
                                                              "preferred": "+15551234567"}])
    return store, assistant, turn


@pytest.mark.parametrize("body", [
    "what the score was", "whatever Dan said in his last text", "my calendar for tomorrow",
    "a summary of the report", "summarize the latest update", "the weather tomorrow",
])
@pytest.mark.parametrize("prefix", ["text mom saying ", "email mom about results saying ",
                                    "reply to the email from Dan saying "])
def test_source_instruction_never_builds_a_send_step(prefix, body):
    plan = compile_task(prefix + body, now=NOW)
    assert plan is not None and "content.mode" in plan.missing_slots
    with pytest.raises(InvalidTaskPlan):
        plan_task(plan)
    assert not plan.steps


def test_content_clarification_survives_unrelated_turn_and_requires_exact_answer(session):
    store, _, turn = session
    opened = turn("text mom saying what the score was")
    assert opened.event == "language_clarification" and not opened.executable
    assert turn("check my calendar") is None
    assert store.active_task("s")["id"] == opened.plan.id
    assert not turn("yes").executable
    ready = turn('"The final score was 3–1."')
    assert ready.executable
    assert ready.plan.steps[0].args["text"] == "The final score was 3–1."
    assert ready.plan.revision > opened.plan.revision


def test_explicit_literal_can_contain_the_retrieval_wording(session):
    _, _, turn = session
    turn("text mom saying whatever Dan said in his last text")
    ready = turn("use those exact words")
    assert ready.executable
    assert ready.plan.steps[0].args["text"] == "whatever Dan said in his last text"


def test_language_clarification_can_be_cancelled_without_reviving_the_send(session):
    _, _, turn = session
    turn("text mom saying what the score was")
    cancelled = turn("cancel")
    assert cancelled.plan.status == "cancelled" and not cancelled.executable
    closed = turn("yes")
    assert closed.event == "closed_send_followup" and not closed.executable


@pytest.mark.parametrize("when", ["on Friday", "in ten minutes", "at noon", "tomorrow morning", "tomorrow at 6pm"])
def test_other_trailing_time_forms_also_need_interpretation(when):
    plan = compile_task("text mom saying Thanks " + when, now=NOW)
    assert "temporal.interpretation" in plan.missing_slots
    assert plan.parameters["time_clarification"].value == {"when": when, "body": "Thanks"}


@pytest.mark.parametrize("body", ["I read your email", "the mail came", "I read the weather forecast"])
def test_source_noun_inside_supplied_sentence_is_literal(session, body):
    _, _, turn = session
    result = turn("text mom saying " + body)
    assert result.executable
    assert result.plan.steps[0].args["text"] == body


@pytest.mark.parametrize("answer, expected_body, tool", [
    ("send then", "Thanks", "schedule_send"),
    ("part of the message", "Thanks at 6pm", "send_message"),
])
def test_trailing_time_is_not_interpreted_until_the_user_selects(session, answer, expected_body, tool):
    _, _, turn = session
    asked = turn("text mom saying Thanks at 6pm")
    assert not asked.executable and not asked.plan.steps
    ready = turn(answer)
    assert ready.executable
    step = ready.plan.steps[0]
    assert step.tool == tool
    assert step.args.get("body", step.args.get("text")) == expected_body
    if tool == "schedule_send":
        assert step.args["when"] == "2026-09-08T18:00"


def test_invalid_trailing_delivery_time_cannot_become_send_now(session):
    _, _, turn = session
    turn("text mom saying Thanks at 25pm")
    asked = turn("send then")
    assert not asked.executable
    assert "temporal.time" in asked.plan.missing_slots
    assert not asked.plan.steps
    fixed = turn("tomorrow at 6pm")
    assert fixed.executable and fixed.plan.steps[0].tool == "schedule_send"


def test_quoted_body_keeps_both_source_words_and_trailing_time(session):
    _, _, turn = session
    ready = turn('text mom saying "what the score was at 6pm"')
    assert ready.executable
    assert ready.plan.steps[0].args["text"] == "what the score was at 6pm"


def test_reply_content_clarification_precedes_native_preparation(session):
    store, assistant, _ = session
    reader = MailReader([{"message_id": "<one>", "account": "Work", "sender": "Dan",
                          "subject": "Results", "ts": NOW.timestamp(), "body": "Original"}],
                        accounts=["Work"], synced_at=NOW.timestamp())
    native = AsyncMock(return_value=(None, "fixture stopped before native execution"))

    async def run(prompt):
        return await prepare_task_turn_async(store, "s", prompt, assistant_store=assistant,
                                             now=NOW, mail_reader=reader, reply_preparer=native)

    asked = asyncio.run(run("reply to the email from Dan saying what the score was"))
    assert not asked.executable and native.await_count == 0
    answered = asyncio.run(run('"The score was 3–1."'))
    assert native.await_count == 1
    assert native.call_args.args[0]["body"] == "The score was 3–1."
    assert not answered.executable


def test_reply_scheduling_clarification_cannot_fall_through_to_immediate_reply(session):
    store, assistant, _ = session
    reader = MailReader([], accounts=["Work"], synced_at=NOW.timestamp())
    native = AsyncMock()

    async def run(prompt):
        return await prepare_task_turn_async(store, "s", prompt, assistant_store=assistant,
                                             now=NOW, mail_reader=reader, reply_preparer=native)

    asyncio.run(run("reply to the email from Dan saying Thanks at 6pm"))
    answer = asyncio.run(run("send then"))
    assert answer is not None and not answer.executable
    assert "isn’t supported" in answer.response
    assert native.await_count == 0
