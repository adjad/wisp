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


# ------------------------------------------- hardening: Astra review 2026-09-08 --

def test_an_unresolvable_send_time_never_becomes_an_immediate_send():
    """A time phrase we cannot parse must stay pending, not fall back to now.

    An empty timestamp is how "send now" is represented, so a failed parse that
    writes one silently converts "at 25pm" into an immediate send.
    """
    for prompt in ("text mom at 6 p.m. saying hello",
                   "text mom at 25pm saying hello"):
        plan = compile_task(prompt, now=NOW)
        assert plan is not None, prompt
        assert plan.temporal.absolute_iso == "", prompt
        assert plan.missing_slots == ["temporal.time"], prompt
        assert plan.parameters["schedule_requested"].value, prompt


def test_an_unresolvable_send_time_asks_and_then_schedules():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        asked = _turn(store, "s1", "text Trishy at 25pm saying hello",
                      assistant, resolver)
        assert asked is not None
        assert asked.executable is False
        assert asked.plan.steps == []
        assert "25pm" in asked.response

        fixed = _turn(store, "s1", "6pm", assistant, resolver)
        assert fixed is not None and fixed.executable is True
        assert fixed.plan.steps[0].tool == "schedule_send"
        assert fixed.plan.steps[0].args["when"] == "2026-09-08T18:00"
    finally:
        temp.cleanup()


def test_a_substring_only_contact_match_requires_explicit_selection():
    """find_contacts matches exact -> word -> substring and returns the first
    tier that hits, so a lone result is not evidence of a close match."""
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    try:
        asked = _turn(store, "s1", "text trish that I'll be late",
                      assistant, resolver)
        assert asked is not None
        assert asked.event == "recipient_needs_confirmation"
        assert asked.plan.resolved_recipient is None
        assert asked.executable is False
        assert "Trishy" in asked.response

        confirmed = _turn(store, "s1", "yes", assistant, resolver)
        assert confirmed is not None and confirmed.executable is True
        assert confirmed.plan.resolved_recipient["address"] == "+15551234567"
        assert confirmed.plan.resolved_recipient["match_tier"] == "exact"
    finally:
        temp.cleanup()


def test_a_whole_word_contact_match_resolves_without_a_question():
    temp, store, assistant = _stores()
    resolver = _resolver({"name": "Mom Smith", "handles": ["+15551110000"],
                          "preferred": "+15551110000"})
    try:
        turn = _turn(store, "s1", "text mom that I'll be late", assistant, resolver)
        assert turn is not None and turn.executable is True
        assert turn.plan.resolved_recipient["match_tier"] == "word"
    finally:
        temp.cleanup()


def test_a_multi_handle_contact_records_what_it_chose_between():
    temp, store, assistant = _stores()
    resolver = _resolver({"name": "Trishy",
                          "handles": ["+15551234567", "trishy@example.com"],
                          "preferred": "+15551234567"})
    try:
        turn = _turn(store, "s1", "text Trishy that I'll be late", assistant, resolver)
        assert turn is not None and turn.executable is True
        resolved = turn.plan.resolved_recipient
        assert resolved["address"] == "+15551234567"
        assert resolved["handles_considered"] == ["+15551234567",
                                                  "trishy@example.com"]
    finally:
        temp.cleanup()


def test_the_executor_refuses_a_second_attempt_at_the_same_revision():
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")
    claimed: list[list[str]] = []
    try:
        plan = _ready_plan(store, assistant, resolver)
        approver = type("Approver", (),
                        {"confirm": AsyncMock(return_value=True)})()
        first = asyncio.run(execute_task(
            plan, AsyncMock(), approver,
            on_claim=lambda p, call_id: bool(
                claimed.append(list(p.claimed_calls)) or True)))
        assert first.status == "completed"
        assert fake.await_count == 1
        # The claim is recorded BEFORE the send, so a crash here still leaves
        # evidence that the attempt happened.
        assert claimed and claimed[0] == plan.claimed_calls

        plan.status = "running"
        second = asyncio.run(execute_task(plan, AsyncMock(), approver))
        assert second.status == "failed"
        assert "already attempted" in second.response
        assert fake.await_count == 1
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()


def test_two_concurrent_executions_send_only_once():
    """The duplicate check cannot be made before awaiting approval.

    Both executions can pass an early check and sit in `confirm` together, so
    the claim has to be an atomic write taken immediately before the tool runs.
    """
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")

    async def _confirm(action):
        # Hold both executions inside approval at the same time.
        await asyncio.sleep(0.01)
        return True

    confirm = staticmethod(_confirm)
    try:
        plan = _ready_plan(store, assistant, resolver)
        approver = type("Approver", (), {"confirm": confirm})()

        def on_claim(p, call_id):
            return store.claim_effect_call(p.id, call_id)

        async def both():
            return await asyncio.gather(
                execute_task(plan, AsyncMock(), approver, on_claim=on_claim),
                execute_task(plan, AsyncMock(), approver, on_claim=on_claim))

        first, second = asyncio.run(both())
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()

    assert fake.await_count == 1, "the send ran more than once"
    statuses = sorted([first.status, second.status])
    assert statuses == ["completed", "failed"]
    loser = first if first.status == "failed" else second
    assert "already attempted" in loser.response


def test_a_separately_loaded_copy_of_the_plan_cannot_send_again():
    """Two processes loading the same persisted plan both start with an empty
    in-memory claim list, so the database has to be the arbiter."""
    temp, store, assistant = _stores()
    resolver = _resolver(TRISHY)
    original, fake = _fake_send("Message sent to +15551234567.")
    try:
        plan = _ready_plan(store, assistant, resolver)
        approver = type("Approver", (),
                        {"confirm": AsyncMock(return_value=True)})()

        def on_claim(p, call_id):
            return store.claim_effect_call(p.id, call_id)

        first = asyncio.run(execute_task(plan, AsyncMock(), approver,
                                         on_claim=on_claim))
        assert first.status == "completed"

        reloaded = TaskPlan.from_dict(plan.to_dict())
        reloaded.claimed_calls = []  # as a fresh process would load it
        reloaded.status = "running"
        second = asyncio.run(execute_task(reloaded, AsyncMock(), approver,
                                          on_claim=on_claim))
        assert second.status == "failed"
        assert fake.await_count == 1
    finally:
        REGISTRY["send_message"] = original
        temp.cleanup()


def test_the_literal_body_guard_errs_in_both_directions():
    """The keyword guard does NOT prove that what it accepts is literal.

    It rejects some source-backed requests. It cannot establish that an
    utterance without a listed source word contains the user's own words, and
    it rejects literal content that happens to mention a source noun. Both
    directions are pinned here so the limitation stays visible until the
    supplied-vs-retrieved boundary is built properly.
    """
    # Direction 1 — literal content wrongly REJECTED. Falls through to the
    # router, which is the pre-existing path, so the cost is a missed
    # optimisation rather than a wrong send.
    for prompt in ("text mom saying I read your email",
                   "text mom saying the mail came"):
        assert compile_task(prompt, now=NOW) is None, prompt

    # Direction 2 — retrieval instructions wrongly ACCEPTED as literal bodies.
    # These become the message text verbatim. Nothing in the current guard
    # catches them, and this is the real gap.
    for prompt, body in (
            ("text mom saying what the score was", "what the score was"),
            ("text mom saying whatever dan said in his last text",
             "whatever dan said in his last text")):
        plan = compile_task(prompt, now=NOW)
        assert plan is not None, prompt
        assert plan.subject.value == body, prompt


def test_an_answer_naming_a_candidate_is_not_dropped_as_a_new_request():
    """The unrelated-request guard keys off leading verbs, and the natural way
    to answer "which address?" starts with one of them."""
    temp, store, assistant = _stores()
    resolver = _resolver()
    try:
        _turn(store, "s1", "text mom that dinner is at seven", assistant, resolver)
        resolver.rows = [MOM_TWO_EMAILS]
        asked = _turn(store, "s1", "email instead", assistant, resolver)
        assert asked is not None and asked.event == "recipient_ambiguous"

        # Starts with "email", which the guard treats as a new request.
        picked = _turn(store, "s1", "email the work one", assistant, resolver)
        assert picked is not None, "the answer was dropped as unrelated"
        assert picked.plan.resolved_recipient["address"] == "mom.work@example.org"
    finally:
        temp.cleanup()


def test_a_genuine_new_request_still_interrupts_an_open_clarification():
    """The guard must keep doing its job: a real request in another domain is
    not an answer just because a clarification is open."""
    temp, store, assistant = _stores()
    resolver = _resolver()
    try:
        _turn(store, "s1", "text mom that dinner is at seven", assistant, resolver)
        resolver.rows = [MOM_TWO_EMAILS]
        _turn(store, "s1", "email instead", assistant, resolver)

        unrelated = _turn(store, "s1", "check my email for purchases from PlayStation",
                          assistant, resolver)
        assert unrelated is None, "an unrelated request was consumed as an answer"
    finally:
        temp.cleanup()


def test_reply_to_email_accepts_an_account_through_the_dispatcher():
    """Exercised through run_tool(), not by calling the function directly.

    A parameter the Python signature accepts but the registered schema omits is
    dead code on the real path: the dispatcher rejects the call before the
    bridge is ever reached, and account pinning silently reverts to
    first-matching-account.
    """
    from service.tools.registry import get_tool, run_tool

    tool = get_tool("reply_to_email")
    assert "account" in tool.parameters["properties"]

    seen: dict = {}

    async def fake_request(event_type, payload, **kwargs):
        seen.update(payload)
        return {"ok": True, "message_id": "<abc@x>", "account": "Work",
                "recipient": "mom@example.com", "subject": "Lease"}

    # action_tools binds `app_request` at import time, so patch the module
    # attribute it actually calls.
    import service.tools.action_tools as action_tools
    real = action_tools.app_request
    action_tools.app_request = fake_request
    try:
        out = asyncio.run(run_tool(tool, {
            "message_id": "<abc@x>", "body": "on my way", "account": "Work"}))
    finally:
        action_tools.app_request = real

    assert "unexpected argument" not in out
    assert seen.get("account") == "Work", "the account never reached the bridge"
    assert "mom@example.com" in out and "Work" in out and "<abc@x>" in out
