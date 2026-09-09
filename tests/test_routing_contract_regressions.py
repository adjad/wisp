"""Offline final-route and typed-plan regressions for SIM-ROUTE-1..4."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Import-time service stores must be disposable even when this file is run
# directly, rather than through the independently isolated simulation runner.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-routing-imports-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service.tools  # noqa: F401
from service.reminder_intent import (
    has_alert_time, has_unsupported_alert_clock, resolve_alert_datetime,
)
from service.router import router as R
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_reminder_create, compile_reminder_update
from service.tasks.engine import prepare_task_turn
from service.tasks.planner import InvalidTaskPlan, plan_task
from service.agent import loop
from service.tools.registry import REGISTRY
from service.tools import assistant_tools


NOW = datetime(2026, 9, 9, 10)
CREATION = {"add_reminder", "add_calendar_event", "set_alarm"}


class ScriptedClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.requests.append({"tools": tools, **kwargs})
        reply = next(self.replies, "What exact time should I use for the reminder?")
        if isinstance(reply, tuple):
            name, arguments = reply
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{len(self.requests)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        else:
            message = {"role": "assistant", "content": reply}
        yield {"kind": "final", "message": message}


class RoutingContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(R, "models_config", return_value={
            "tool_retrieval": {"provider": "lexical"}}))
        self.enterContext(patch.object(R, "resolve_alert_datetime", side_effect=
            lambda text: resolve_alert_datetime(text, now=NOW)))
        self.enterContext(patch.object(loop, "audit"))

    async def run_loop(self, prompt, replies):
        d = await R.route(prompt)
        client = ScriptedClient(replies)
        emit = AsyncMock()
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=False)})()
        result = await loop.run_agent(
            client, "Ling-3.0-tiny-oQ4e", [{"role": "user", "content": prompt}],
            emit, approver, tools=d.tool_subset, expect_tool_first=d.expect_tool_first,
            force_first_tool=d.force_first_tool, multi_round=d.multi_round,
            required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
            tool_argument_bindings=d.tool_argument_bindings,
            reminder_action=d.reminder_action, include_memory_context=False, max_steps=4)
        return result, client, approver

    async def test_existing_note_edit_is_not_calendar_creation(self):
        for prompt in ("add a line to the meeting note",
                       "add agenda items to my meeting note"):
            with self.subTest(prompt=prompt):
                self.assertIn("append_note", R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                self.assertEqual(set(d.tool_subset), {"search_notes", "append_note"})
                self.assertEqual(d.required_tool_groups, (
                    frozenset({"search_notes"}), frozenset({"append_note"})))
                self.assertFalse(CREATION.intersection(d.tool_subset))
        for prompt in ("add a meeting to my calendar tomorrow at 3pm",
                       "add a calendar event titled meeting notes tomorrow"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["add_calendar_event"])

    async def test_reminder_reads_reach_the_overdue_inclusive_source(self):
        for prompt in ("when is my vaccine reminder", "find my dentist reminder",
                       "what does my reminder say", "find my overdue dentist reminder"):
            with self.subTest(prompt=prompt):
                self.assertIn("search_reminders", R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["search_reminders"])
                self.assertEqual(d.force_first_tool, "search_reminders")
                self.assertEqual(d.direct_calls, [])
                self.assertEqual(d.reminder_action, "")
        d = await R.route("what is on my calendar")
        self.assertEqual(d.direct_calls, [("get_upcoming", {"days": 60})])
        d = await R.route("find free time between my reminders")
        self.assertIn("find_free_time", d.tool_subset)

    async def test_reschedule_is_an_update_not_creation(self):
        for word in ("reschedule", "rescheduled", "rescheduling", "reschedules"):
            self.assertEqual(R._normalize_typos(word), word)
        for prompt, target in (
                ("reschedule the meeting to tomorrow", "update_event"),
                ("please reschedule my appointment to tomorrow", "update_event"),
                ("I need you to reschedule my reminder to tomorrow", "update_reminder"),
                ("reschedule my reminder", "update_reminder")):
            with self.subTest(prompt=prompt):
                self.assertIn(target, R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                self.assertEqual(set(d.tool_subset), {"get_upcoming", target})
                self.assertEqual(d.required_tool_groups, ())
                self.assertIsNone(d.force_first_tool)
                self.assertEqual(d.reminder_action, "")
                self.assertEqual(d.tool_argument_bindings, {})
                self.assertFalse(CREATION.intersection(d.tool_subset))
        both = await R.route("reschedule my meeting and my reminder to tomorrow")
        self.assertEqual(set(both.tool_subset), {"get_upcoming", "update_event", "update_reminder"})
        self.assertEqual(both.required_tool_groups, ())

    async def test_incomplete_reschedule_can_ask_without_forcing_an_update(self):
        question = "Which reminder should I reschedule, and when?"
        result, client, approver = await self.run_loop("reschedule my reminder", [question])
        self.assertEqual(result, question)
        self.assertEqual(len(client.requests), 1)
        self.assertNotEqual(client.requests[0].get("tool_choice"), "required")
        approver.confirm.assert_not_awaited()

    async def test_embedded_future_task_verbs_do_not_hijack_creation(self):
        for prompt in ("create a reminder tomorrow to reschedule my meeting",
                       "remind me tomorrow to reschedule my dentist reminder",
                       "remind me to find my dentist reminder tomorrow",
                       "remind me tomorrow to append a line to my meeting note"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.reminder_action, "create")
                self.assertIn("add_reminder", d.tool_subset)
                self.assertFalse({"update_event", "update_reminder", "append_note"}
                                 .intersection(d.tool_subset))

    async def test_capability_lists_cannot_act_or_start_reminder_clarification(self):
        for prompt in ("Can you send texts and create reminders?",
                       "Can you send text messages, create reminders, and read browser history?"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["wisp_capabilities"])
                self.assertEqual(d.direct_calls[0][0], "wisp_capabilities")
                self.assertEqual(d.required_tool_groups, (frozenset({"wisp_capabilities"}),))
                self.assertEqual(d.reminder_action, "")
                self.assertIsNone(d.force_first_tool)
                self.assertEqual(d.tool_argument_bindings, {})
        d = await R.route("can you send texts to Mom and create reminders for tomorrow")
        self.assertNotIn("wisp_capabilities", d.tool_subset)
        self.assertIn("send_message", d.tool_subset)

    def assertClockClarification(self, d):
        self.assertEqual(d.reminder_action, "clarify_time")
        self.assertEqual(d.tool_subset, ["get_upcoming"])
        self.assertFalse(CREATION.intersection(d.tool_subset))
        self.assertEqual(d.direct_calls, [])
        self.assertIsNone(d.force_first_tool)
        self.assertFalse(d.expect_tool_first)
        self.assertEqual(d.required_tool_groups, ())
        self.assertEqual(d.tool_argument_bindings, {})

    async def test_unsupported_clock_initial_and_followup_cannot_create(self):
        for clock in ("half six tomorrow", "half past six tomorrow", "quarter to six tomorrow",
                      "tomorrow at six", "tomorrow at 6:7", "tomorrow at 25:00",
                      "6 or 7 tomorrow", "six pm tomorrow", "25pm tomorrow"):
            with self.subTest(clock=clock):
                self.assertTrue(has_unsupported_alert_clock(clock))
                self.assertFalse(has_alert_time(clock))
                self.assertIsNone(resolve_alert_datetime(clock, now=NOW))
                self.assertClockClarification(await R.route("set an alarm for " + clock))
                self.assertClockClarification(await R.route(
                    clock, last_user="set an alarm for my medicine",
                    last_assistant="What time should I set the reminder?"))
        self.assertClockClarification(await R.route("set an alarm at half six"))
        unrelated = await R.route("show my notes about half six",
                                  last_assistant="What time should I set the reminder?")
        self.assertEqual(unrelated.tool_subset, ["search_notes"])
        self.assertEqual(unrelated.reminder_action, "")
        question = await R.route("what does half six mean?",
                                 last_assistant="What time should I set the reminder?")
        self.assertEqual(question.reminder_action, "")

    async def test_supported_clock_date_defaults_and_relative_times_are_preserved(self):
        for prompt, expected in (
                ("set an alarm at 6:30 am tomorrow", "2026-09-10T06:30"),
                ("set an alarm tomorrow", "2026-09-10T09:00"),
                ("set an alarm tomorrow at 6", "2026-09-10T18:00"),
                ("remind me tomorrow at 9am to prepare for my 1:1", "2026-09-10T09:00"),
                ("set an alarm at noon tomorrow", "2026-09-10T12:00"),
                ("set an alarm at 6pm tomorrow for half an hour of exercise", "2026-09-10T18:00")):
            with self.subTest(prompt=prompt):
                self.assertFalse(has_unsupported_alert_clock(prompt))
                self.assertEqual(resolve_alert_datetime(prompt, now=NOW).isoformat(timespec="minutes"), expected)
                d = await R.route(prompt)
                self.assertEqual(d.reminder_action, "create")
                self.assertEqual(d.tool_argument_bindings["add_reminder"]["when_iso"], expected)
        for text in ("in half an hour", "30 minutes before", "the day before"):
            with self.subTest(text=text):
                self.assertFalse(has_unsupported_alert_clock(text))
                self.assertTrue(has_alert_time(text))
        d = await R.route("set an alarm for my repair appointment tomorrow at 1pm")
        self.assertEqual(d.reminder_action, "clarify_time")

    async def test_attempted_creation_during_clock_clarification_is_never_executed(self):
        effect = AsyncMock(side_effect=AssertionError("creation must not execute"))
        # Real dispatch rejection, not test_mode's dry-run interception.
        with patch.object(REGISTRY["add_reminder"], "func", effect):
            result, _, approver = await self.run_loop(
                "set an alarm for half six tomorrow", [
                    ("add_reminder", {"title": "medicine", "when_iso": "2026-09-10T09:00"}),
                    "What exact time should I use for the reminder?"])
        effect.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        self.assertNotIn("Reminder set:", result)
        self.assertIn("?", result)


class TypedClockContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-routing-contract-")))
        self.sessions = SessionStore(root / "sessions.db")
        self.assistant = AssistantStore(root / "assistant.db")
        self.addCleanup(self.sessions._db.close)
        self.addCleanup(self.assistant._db.close)
        self.sid = self.sessions.create_session()

    def prepare(self, prompt):
        return prepare_task_turn(self.sessions, self.sid, prompt,
                                 assistant_store=self.assistant, now=NOW)

    def test_reminder_search_reads_overdue_records_and_excludes_calendar_events(self):
        self.assistant.add_manual("dentist overdue", (NOW - timedelta(days=1)).timestamp())
        self.assistant.add_manual("dentist upcoming", (NOW + timedelta(days=1)).timestamp())
        self.assistant.sync_source("calendar", [{
            "source_id": "fixture-meeting", "kind": "event", "title": "dentist calendar event",
            "when_ts": (NOW + timedelta(days=1)).timestamp()}])
        with patch.object(assistant_tools, "assistant_store", self.assistant):
            result = asyncio.run(assistant_tools.search_reminders("dentist"))
        self.assertIn("dentist overdue", result)
        self.assertIn("dentist upcoming", result)
        self.assertNotIn("dentist calendar event", result)

    def test_initial_typed_creation_and_update_have_no_guessed_time(self):
        for prompt in ("set an alarm for half six tomorrow",
                       "set an alarm at half six tomorrow to take medicine",
                       "set an alarm for 6 or 7 tomorrow",
                       "set an alarm for six pm tomorrow", "set an alarm for 25pm tomorrow"):
            with self.subTest(prompt=prompt):
                plan = compile_reminder_create(prompt, now=NOW)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.status, "waiting_for_input")
                self.assertEqual(plan.temporal.absolute_iso, "")
                self.assertEqual(plan.temporal.defaulted_part_of_day, "")
                self.assertIn("temporal.time", plan.missing_slots)
                with self.assertRaises(InvalidTaskPlan):
                    plan_task(plan)
        plan = compile_reminder_update("reschedule my medicine reminder to tomorrow at half six", now=NOW)
        self.assertEqual(plan.target.value, "medicine")
        self.assertEqual(plan.temporal.absolute_iso, "")
        self.assertIn("update.change", plan.missing_slots)
        valid = compile_reminder_create("remind me tomorrow at 9am to prepare for my 1:1", now=NOW)
        self.assertEqual(valid.status, "ready")
        self.assertEqual(valid.temporal.absolute_iso, "2026-09-10T09:00")
        self.assertEqual(valid.subject.value, "prepare for my 1:1")

    def test_pending_create_preserves_plan_then_accepts_precise_reply(self):
        first = self.prepare("set an alarm at half six tomorrow to take medicine")
        self.assertFalse(first.executable)
        self.assertIn("temporal.time", first.plan.missing_slots)
        snapshot = self.sessions.active_task(self.sid)
        for reply in ("half six tomorrow", "tomorrow at 6:7", "6 or 7 tomorrow"):
            second = self.prepare(reply)
            self.assertIsNotNone(second)
            self.assertFalse(second.executable)
            self.assertEqual(second.plan.steps, [])
            self.assertEqual(second.event, "clock_clarification")
            self.assertEqual(self.sessions.active_task(self.sid), snapshot)
        final = self.prepare("6:30 am tomorrow")
        self.assertTrue(final.executable)
        self.assertEqual(final.plan.id, first.plan.id)
        self.assertEqual(final.plan.subject.value, first.plan.subject.value)
        self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T06:30")
        self.assertEqual([step.tool for step in final.plan.steps], ["add_reminder"])

    def test_pending_update_preserves_target_then_accepts_precise_reply(self):
        row = self.assistant.add_manual("medicine", (NOW + timedelta(days=2)).timestamp())
        first = self.prepare("reschedule my medicine reminder")
        self.assertFalse(first.executable)
        snapshot = self.sessions.active_task(self.sid)
        second = self.prepare("half six tomorrow")
        self.assertFalse(second.executable)
        self.assertEqual(second.event, "clock_clarification")
        self.assertEqual(self.sessions.active_task(self.sid), snapshot)
        final = self.prepare("6:30 am tomorrow")
        self.assertTrue(final.executable)
        self.assertEqual(final.plan.id, first.plan.id)
        self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T06:30")
        self.assertEqual(final.plan.steps[0].tool, "update_reminder")
        self.assertEqual(final.plan.steps[0].args["expected_id"], row["id"])

    def test_pending_clock_does_not_swallow_cancellation_or_new_task(self):
        self.prepare("set an alarm at half six tomorrow to take medicine")
        self.assertIsNone(self.prepare("check my email"))
        self.assertIsNone(self.prepare("show my notes about half six"))
        cancelled = self.prepare("cancel")
        self.assertEqual(cancelled.plan.status, "cancelled")
        self.assertFalse(cancelled.executable)
        fresh = self.prepare("remind me tomorrow at noon to drink water")
        self.assertTrue(fresh.executable)
        self.assertEqual(fresh.plan.temporal.absolute_iso, "2026-09-10T12:00")


if __name__ == "__main__":
    unittest.main()
