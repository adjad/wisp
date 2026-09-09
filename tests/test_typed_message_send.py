"""Strict tests for the typed outbound (message/email) task boundary.

Recipient resolution lives in the ENGINE, between compile and plan. These
tests pin that placement: the compiler never guesses an address, the planner
refuses to build a step without one, and the executor never resolves anything
— so approval always binds to an exact, already-rendered destination.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import (
    compile_email_send, compile_message_send, compile_task,
)
from service.tasks.engine import finish_task, prepare_task_turn
from service.tasks.executor import execute_task
from service.tasks.models import TaskPlan
from service.tasks.planner import InvalidTaskPlan, plan_task
from service.tools.registry import REGISTRY, Tool


NOW = datetime(2026, 9, 8, 10, 0)

TRISHY = {"name": "Trishy", "handles": ["+15551234567"], "preferred": "+15551234567"}
MOM_TWO_EMAILS = {
    "name": "Mom",
    "handles": ["mom.home@gmail.com", "mom.work@example.org"],
    "preferred": "mom.home@gmail.com",
}
MOM_PHONE_ONLY = {"name": "Mom", "handles": ["+15559998888"],
                  "preferred": "+15559998888"}


def _stores():
    temp = tempfile.TemporaryDirectory(prefix="wisp-typed-send-")
    root = Path(temp.name)
    return temp, SessionStore(root / "sessions.db"), AssistantStore(root / "assistant.db")


def _resolver(*rows):
    """A Contacts stand-in that records lookups and can be re-programmed.

    Reassign `resolve.rows` to model a later turn asking about a different
    channel, where the same name yields a different usable handle set.
    """
    calls: list[str] = []

    def resolve(name: str) -> list[dict]:
        calls.append(name)
        return list(resolve.rows)

    resolve.calls = calls  # type: ignore[attr-defined]
    resolve.rows = list(rows)  # type: ignore[attr-defined]
    return resolve


def _turn(store, sid, prompt, assistant_store, resolver):
    return prepare_task_turn(store, sid, prompt, assistant_store=assistant_store,
                             now=NOW, contacts_resolver=resolver)


# ---------------------------------------------------------------- compiler --

def test_compiler_emits_a_raw_handle_and_never_an_address():
    plan = compile_message_send("text Trishy that I'll be late", now=NOW)
    assert plan is not None
    assert plan.intent == "message.send"
    assert plan.channel.value == "messages"
    assert plan.recipient is not None
    assert plan.recipient.value == "Trishy"
    assert plan.recipient.source == "explicit"
    assert plan.subject.value == "I'll be late"
    # The compiler has no store: an address here would be an invention.
    assert plan.resolved_recipient is None
    assert plan.missing_slots == []


def test_compiler_leaves_source_backed_delivery_to_the_workflow_path():
    for prompt in ("text mom my calendar for tomorrow",
                   "send mom my email summaries",
                   "text dad the weather for the weekend",
                   "message mom my reminders for today"):
        assert compile_task(prompt, now=NOW) is None, prompt


def test_compiler_does_not_fire_on_questions_or_negations():
    for prompt in ("did mom text me?", "don't text mom that I'm late",
                   "message board is broken"):
        assert compile_task(prompt, now=NOW) is None, prompt


# ---------------------------------------------------------------- resolution --

def test_single_contact_resolves_and_plans_one_exact_step():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        turn = _turn(store, "s1", "text Trishy that I'll be late", assistant, resolver)
        assert turn is not None
        assert turn.executable is True
        assert turn.response == ""
        plan = turn.plan
        assert plan.resolved_recipient["address"] == "+15551234567"
        assert plan.resolved_recipient["source"] == "contacts"
        assert plan.resolved_recipient["channel"] == "messages"
        assert [step.tool for step in plan.steps] == ["send_message"]
        assert plan.steps[0].args == {"to": "+15551234567", "text": "I'll be late"}
        assert plan.steps[0].max_calls == 1
    finally:
        temp.cleanup()


def test_multiple_saved_emails_ask_once_then_accept_a_partial_reply():
    temp, store, assistant = _stores()
    # Two people called "mom" — the message send opens on a clarification.
    resolver = _resolver({"name": "Mom", "handles": ["+15551110000"],
                          "preferred": "+15551110000"},
                         {"name": "Mom Cell", "handles": ["+15552220000"],
                          "preferred": "+15552220000"})
    try:
        opened = _turn(store, "s1", "text mom that dinner is at seven", assistant, resolver)
        assert opened is not None and opened.event == "recipient_ambiguous"
        # Retarget to email: the same name now has two usable addresses.
        resolver.rows = [MOM_TWO_EMAILS]
        asked = _turn(store, "s1", "email instead", assistant, resolver)
        assert asked is not None
        assert asked.event == "recipient_ambiguous"
        assert asked.plan.missing_slots == ["recipient.address"]
        assert asked.plan.resolved_recipient is None
        assert "mom.home@gmail.com" in asked.response
        assert "mom.work@example.org" in asked.response
        assert asked.executable is False

        picked = _turn(store, "s1", "the gmail one", assistant, resolver)
        assert picked is not None
        assert picked.plan.resolved_recipient["address"] == "mom.home@gmail.com"
        # An email still needs a subject line before it can be planned.
        assert picked.plan.missing_slots == ["email.subject"]
        assert picked.executable is False

        ready = _turn(store, "s1", "Dinner", assistant, resolver)
        assert ready is not None and ready.executable is True
        assert ready.plan.steps[0].args == {
            "to": "mom.home@gmail.com", "subject": "Dinner",
            "body": "dinner is at seven"}
    finally:
        temp.cleanup()


def test_a_phone_number_is_never_downgraded_into_an_email_send():
    temp, store, assistant = _stores()
    resolver = _resolver()  # the message send opens on an unknown name
    try:
        _turn(store, "s1", "text mom that I landed", assistant, resolver)
        resolver.rows = [MOM_PHONE_ONLY]
        asked = _turn(store, "s1", "email instead", assistant, resolver)
        assert asked is not None
        assert asked.event == "recipient_no_email"
        assert asked.plan.missing_slots == ["recipient.address"]
        assert asked.plan.resolved_recipient is None
        assert asked.executable is False

        literal = _turn(store, "s1", "+15559998888", assistant, resolver)
        assert literal is not None
        assert literal.event == "recipient_channel_mismatch"
        assert literal.plan.resolved_recipient is None
        assert literal.executable is False
    finally:
        temp.cleanup()


def test_an_unknown_name_asks_and_never_autocorrects():
    temp, store, assistant = _stores()
    resolver = _resolver()  # no matches
    try:
        turn = _turn(store, "s1", "text trishe that I'll be late", assistant, resolver)
        assert turn is not None
        assert turn.event == "recipient_not_found"
        assert turn.plan.resolved_recipient is None
        assert turn.executable is False
        assert "trishe" in turn.response
        assert turn.plan.steps == []
    finally:
        temp.cleanup()


def test_bare_me_asks_instead_of_filling_from_identity():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        turn = _turn(store, "s1", "text me that the build finished", assistant, resolver)
        assert turn is not None
        assert turn.event == "recipient_self_unspecified"
        assert turn.plan.resolved_recipient is None
        assert turn.executable is False
        # The user's own address exists in the prompt for attribution only.
        assert resolver.calls == []
    finally:
        temp.cleanup()


def test_channel_correction_keeps_the_body_and_reresolves_the_address():
    temp, store, assistant = _stores()
    resolver = _resolver()
    try:
        first = _turn(store, "s1", "text mom that dinner is at seven", assistant, resolver)
        assert first is not None
        before = first.plan.revision
        assert first.plan.channel.value == "messages"

        resolver.rows = [MOM_TWO_EMAILS]
        corrected = _turn(store, "s1", "email instead", assistant, resolver)
        assert corrected is not None
        plan = corrected.plan
        assert plan.intent == "email.send"
        assert plan.channel.value == "email"
        assert plan.channel.source == "correction"
        # Body and requested recipient survive; only the address snapshot dies.
        assert plan.subject.value == "dinner is at seven"
        assert plan.recipient.value == "mom"
        assert plan.resolved_recipient is None
        assert plan.revision > before
    finally:
        temp.cleanup()


# ------------------------------------------------------------------ planner --

def test_planner_refuses_an_unresolved_or_mismatched_recipient():
    plan = compile_message_send("text Trishy that I'll be late", now=NOW)
    assert plan is not None
    try:
        plan_task(plan)
    except InvalidTaskPlan as exc:
        assert "resolved recipient" in str(exc)
    else:
        raise AssertionError("planner accepted a plan with no resolved address")

    plan.resolved_recipient = {"address": "trishy@example.com", "channel": "email"}
    try:
        plan_task(plan)
    except InvalidTaskPlan as exc:
        assert "different channel" in str(exc)
    else:
        raise AssertionError("planner accepted a cross-channel address")


def test_plan_round_trip_preserves_the_resolution_snapshot():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        turn = _turn(store, "s1", "text Trishy that I'll be late", assistant, resolver)
        assert turn is not None
        restored = TaskPlan.from_dict(turn.plan.to_dict())
        assert restored.to_dict() == turn.plan.to_dict()
        assert restored.resolved_recipient["address"] == "+15551234567"
        assert restored.steps[0].args["to"] == "+15551234567"
    finally:
        temp.cleanup()


# ----------------------------------------------------------------- executor --

def _fake_send(result: str):
    original = REGISTRY["send_message"]
    fake = AsyncMock(return_value=result)
    REGISTRY["send_message"] = Tool(
        "send_message", "fixture", original.parameters, original.category, fake)
    return original, fake


def _ready_plan(store, assistant, resolver, sid="s1"):
    turn = _turn(store, sid, "text Trishy that I'll be late", assistant, resolver)
    assert turn is not None and turn.executable
    return turn.plan


def test_approval_preview_shows_the_requested_name_and_resolved_address():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")
    seen: list[dict] = []

    async def _confirm(action):
        seen.append(action)
        return True

    confirm = staticmethod(_confirm)

    try:
        plan = _ready_plan(store, assistant, resolver)
        execution = asyncio.run(execute_task(
            plan, AsyncMock(), type("Approver", (), {"confirm": confirm})()))
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()

    assert execution.status == "completed"
    assert seen, "an outbound send must be confirmed"
    preview = seen[0]["preview"]
    assert "Trishy → +15551234567" in preview
    assert "I'll be late" in preview
    assert seen[0]["args"] == {"to": "+15551234567", "text": "I'll be late"}


def test_a_receipt_that_names_another_address_is_not_a_success():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, _ = _fake_send("Message sent to +15550000000.")
    try:
        plan = _ready_plan(store, assistant, resolver)
        execution = asyncio.run(execute_task(
            plan, AsyncMock(),
            type("Approver", (), {"confirm": AsyncMock(return_value=True)})()))
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()
    assert execution.status == "failed"
    assert "not confirmed as sent" in execution.response


def test_dry_run_never_calls_the_send_tool():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")
    fake.side_effect = AssertionError("dry run executed send_message")
    try:
        plan = _ready_plan(store, assistant, resolver)
        execution = asyncio.run(execute_task(
            plan, AsyncMock(),
            type("Approver", (), {"confirm": AsyncMock()})(), test_mode=True))
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()
    assert execution.status == "planned"
    assert fake.await_count == 0


def test_a_denied_send_is_terminal_and_a_bare_yes_cannot_revive_it():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")
    try:
        plan = _ready_plan(store, assistant, resolver)
        execution = asyncio.run(execute_task(
            plan, AsyncMock(),
            type("Approver", (), {"confirm": AsyncMock(return_value=False)})()))
        assert execution.status == "denied"
        assert fake.await_count == 0
        finish_task(store, "s1", plan, status="denied", result=execution.response)

        again = _turn(store, "s1", "yes", assistant, resolver)
        assert again is not None
        assert again.event == "closed_send_followup"
        assert again.executable is False
        assert fake.await_count == 0
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()


def test_cancelling_an_open_send_says_send_not_reminder():
    temp, store, assistant = _stores()
    resolver = _resolver()
    try:
        _turn(store, "s1", "text trishe that I'll be late", assistant, resolver)
        cancelled = _turn(store, "s1", "never mind", assistant, resolver)
        assert cancelled is not None
        assert cancelled.plan.status == "cancelled"
        assert "send" in cancelled.response
        assert "reminder" not in cancelled.response
    finally:
        temp.cleanup()


# ------------------------------------------------- phase 2b: email + schedule --

def test_email_send_compiles_and_asks_for_a_missing_subject():
    plan = compile_email_send("email mom saying I landed safely", now=NOW)
    assert plan is not None
    assert plan.intent == "email.send"
    assert plan.channel.value == "email"
    assert plan.recipient.value == "mom"
    assert plan.subject.value == "I landed safely"
    assert plan.missing_slots == ["email.subject"]

    with_subject = compile_email_send(
        "email mom about dinner saying I will be there at seven", now=NOW)
    assert with_subject is not None
    assert with_subject.parameters["email_subject"].value == "dinner"
    assert with_subject.subject.value == "I will be there at seven"
    assert with_subject.missing_slots == []


def test_reply_and_forward_stay_on_the_router():
    for prompt in ("reply to that email saying thanks",
                   "respond to mom's email saying I'll be there",
                   "forward that to dan"):
        assert compile_task(prompt, now=NOW) is None, prompt


def test_a_trailing_time_belongs_to_the_body_not_the_schedule():
    plan = compile_task("text mom that I'll be there at 6pm", now=NOW)
    assert plan is not None
    assert plan.subject.value == "I'll be there at 6pm"
    assert plan.temporal.absolute_iso == ""


def test_a_time_before_the_body_schedules_the_send():
    for prompt, expected in (
            ("text mom at 6pm saying I am on my way", "2026-09-08T18:00"),
            ("text mom in 10 minutes saying heading out", "2026-09-08T10:10"),
            ("email dan tomorrow morning saying the report is ready",
             "2026-09-09T09:00")):
        plan = compile_task(prompt, now=NOW)
        assert plan is not None, prompt
        assert plan.temporal.absolute_iso == expected, prompt
        assert plan.temporal.source == "explicit", prompt


def test_a_scheduled_send_binds_the_address_at_approval_time():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        turn = _turn(store, "s1", "text Trishy at 6pm saying I am on my way",
                     assistant, resolver)
        assert turn is not None and turn.executable is True
        step = turn.plan.steps[0]
        assert step.tool == "schedule_send"
        assert step.args == {"channel": "message", "to": "+15551234567",
                             "when": "2026-09-08T18:00",
                             "body": "I am on my way"}
    finally:
        temp.cleanup()


def test_a_clock_time_already_past_today_rolls_forward_not_backward():
    plan = compile_task("text Trishy at 8am saying I am on my way", now=NOW)
    assert plan is not None
    # 8am has gone at 10am, so the shared resolver moves to the next 8am
    # rather than producing a time in the past.
    assert plan.temporal.absolute_iso == "2026-09-09T08:00"


def test_a_past_scheduled_time_asks_instead_of_sending_now():
    """Defence in depth: a scheduled send must never degrade to an immediate one.

    The shared resolver rolls clock times forward, so this state is not
    reachable from a single utterance today. It is reachable from a persisted
    plan whose time has since passed, which is exactly when firing "now"
    instead of "at 8am" would be worst.
    """
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        plan = compile_task("text Trishy at 6pm saying placeholder", now=NOW)
        assert plan is not None
        plan.temporal.absolute_iso = "2026-09-08T08:00"  # now in the past
        plan.subject = type(plan.subject)("", "", 0, 0.0, "")
        plan.recompute_status()
        assert plan.missing_slots == ["subject"]
        store.save_workflow("s1", plan.to_dict())

        turn = _turn(store, "s1", "I am on my way", assistant, resolver)
        assert turn is not None
        assert turn.event == "past_send_time"
        assert turn.executable is False
        assert turn.plan.steps == []
        assert turn.plan.temporal.absolute_iso == ""

        fixed = _turn(store, "s1", "6pm", assistant, resolver)
        assert fixed is not None and fixed.executable is True
        assert fixed.plan.steps[0].args["when"] == "2026-09-08T18:00"
    finally:
        temp.cleanup()


def test_scheduled_approval_preview_shows_the_send_time():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original = REGISTRY["schedule_send"]
    fake = AsyncMock(
        return_value="Scheduled: text to +15551234567 on Tue Sep 8 at 6:00 PM")
    REGISTRY["schedule_send"] = Tool("schedule_send", "fixture",
                                     original.parameters, original.category, fake)
    seen: list[dict] = []

    async def _confirm(action):
        seen.append(action)
        return True

    confirm = staticmethod(_confirm)
    try:
        turn = _turn(store, "s1", "text Trishy at 6pm saying I am on my way",
                     assistant, resolver)
        assert turn is not None and turn.executable
        execution = asyncio.run(execute_task(
            turn.plan, AsyncMock(), type("Approver", (), {"confirm": confirm})()))
    finally:
        REGISTRY["schedule_send"] = original
        temp.cleanup()
    assert execution.status == "completed"
    assert "When: Tue Sep 8 at 6:00 PM" in seen[0]["preview"]
    assert "To: Trishy → +15551234567" in seen[0]["preview"]


def test_an_email_send_plans_exact_args_after_its_subject_arrives():
    temp, store, assistant = _stores()
    resolver = _resolver({"name": "Dan", "handles": ["dan@example.com"],
                          "preferred": "dan@example.com"})
    try:
        asked = _turn(store, "s1", "email dan saying the report is ready",
                      assistant, resolver)
        assert asked is not None
        assert asked.plan.missing_slots == ["email.subject"]
        # The address resolves before the subject is asked for: an unreachable
        # recipient should not be discovered after the user answers questions.
        assert asked.plan.resolved_recipient["address"] == "dan@example.com"

        ready = _turn(store, "s1", "Q3 report", assistant, resolver)
        assert ready is not None and ready.executable is True
        assert ready.plan.steps[0].tool == "send_email"
        assert ready.plan.steps[0].args == {
            "to": "dan@example.com", "subject": "Q3 report",
            "body": "the report is ready"}
    finally:
        temp.cleanup()
