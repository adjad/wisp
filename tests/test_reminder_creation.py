"""Reminder routing + real store/agent verifier, using isolated fixtures only."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

_scratch = tempfile.TemporaryDirectory(prefix="wisp-reminder-creation-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.agent import loop
from service.assistant import scheduler
from service.assistant.hub import hub
from service.assistant.store import AssistantStore
from service.router.router import route
from service.tools import assistant_tools as A
from service.tools.registry import classify_tool_outcome

ORIGINAL = "can you set an alarm for me tommorow for the iphone repair thing"


class ScriptedClient:
    def __init__(self, script):
        self.script = iter(script)
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        reply = next(self.script, "I've set your alarm.")
        if isinstance(reply, tuple):
            name, args = reply
            message = {"role": "assistant", "content": "I've set it.", "tool_calls": [{
                "id": f"call-{len(self.requests)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            message = {"role": "assistant", "content": reply}
        yield {"kind": "content", "text": message["content"]}
        yield {"kind": "final", "message": message}


class ReminderCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AssistantStore(Path(_scratch.name) / f"{self._testMethodName}.db")
        self.addCleanup(self.store._db.close)
        self.enterContext(patch.object(A, "assistant_store", self.store))
        self.enterContext(patch.object(scheduler, "_sync_status", {}))
        scheduler.record_sync("calendar", 1, {"authorized": True})
        scheduler.record_sync("reminders", 0, {"authorized": True})
        self.publish = self.enterContext(patch.object(hub, "publish", new_callable=AsyncMock))
        self.enterContext(patch.object(loop, "audit"))
        self.appointment = (datetime.now() + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
        self.alert = self.appointment - timedelta(minutes=30)
        self.store.sync_source("calendar", [{"source_id": "fixture-repair", "kind": "meeting",
            "title": "iPhone repair", "when_ts": self.appointment.timestamp(), "context": "Test"}])

    async def run_flow(self, script, prompt=ORIGINAL, *, decision=None, approve=True, test_mode=False):
        decision = decision or await route(prompt)
        client = ScriptedClient(script)
        events = []

        async def emit(event):
            events.append(event)
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=approve)})()
        result = await loop.run_agent(client, "Ling-3.0-tiny-oQ4e",
            [{"role": "user", "content": prompt}], emit, approver,
            tools=decision.tool_subset, expect_tool_first=decision.expect_tool_first,
            force_first_tool=decision.force_first_tool, multi_round=decision.multi_round,
            required_tool_groups=decision.required_tool_groups, forbidden_tools=decision.forbidden_tools,
            tool_argument_bindings=decision.tool_argument_bindings,
            reminder_action=decision.reminder_action, max_steps=5, test_mode=test_mode)
        return result, events, client

    def reminder_rows(self):
        return [r for r in self.store.upcoming(days=3) if r["source"] == "manual"]

    async def test_alarm_date_only_uses_standard_morning_time(self):
        d = await route(ORIGINAL)
        self.assertEqual(d.reminder_action, "create")
        self.assertEqual(d.force_first_tool, "add_reminder")
        self.assertTrue(d.tool_argument_bindings["add_reminder"]["when_iso"].endswith("09:00"))
        result, events, _ = await self.run_flow([
            ("add_reminder", {"title": "iPhone repair", "when_iso": self.alert.isoformat()}),
            "Done",
        ], decision=d)
        self.assertTrue(result.startswith("Reminder set:"), result)
        self.assertEqual(len(self.reminder_rows()), 1)
        self.assertEqual(datetime.fromtimestamp(self.reminder_rows()[0]["when_ts"]).hour, 9)
        self.assertFalse(any(e.get("type") == "delta" for e in events))

    async def test_wrong_substitutions_are_blocked_before_real_reminder_write(self):
        result, _, _ = await self.run_flow([
            ("add_calendar_event", {"title": "repair", "when_iso": self.alert.isoformat()}),
            ("remember", {"fact": "I set a reminder"}),
            ("add_reminder", {"title": "repair", "when_iso": self.alert.isoformat()}),
            "All set."])
        self.assertTrue(result.startswith("Reminder set:"), result)
        self.assertEqual(len(self.reminder_rows()), 1)

    async def test_precise_time_requires_actual_creation_and_receipt(self):
        prompt = "set an alarm at 12:30 pm tomorrow for the iphone repair"
        d = await route(prompt)
        self.assertIn(frozenset({"add_reminder"}), d.required_tool_groups)
        result, events, _ = await self.run_flow([
            "I've set your alarm.",
            ("add_reminder", {"title": "iPhone repair", "when_iso": self.alert.isoformat()}),
            "Clock alarm set!"], prompt, decision=d)
        self.assertTrue(result.startswith("Reminder set:"), result)
        self.assertNotIn("Clock", result)
        self.assertEqual(len(self.reminder_rows()), 1)
        self.assertEqual(self.reminder_rows()[0]["when_ts"], self.alert.timestamp())
        self.assertEqual(sum(e.args[0]["type"] == "create_apple_reminder"
                             for e in self.publish.await_args_list), 1)
        self.assertNotIn("also added", result)
        self.assertFalse(any(e.get("type") == "delta" for e in events))

    async def test_stubborn_model_cannot_claim_success_without_write(self):
        result, events, _ = await self.run_flow([], "set an alarm at noon tomorrow")
        self.assertIn("No reminder was added", result)
        self.assertEqual(self.reminder_rows(), [])
        self.assertFalse(any(e.get("type") == "delta" for e in events))

    async def test_time_reply_stays_on_reminder_workflow(self):
        d = await route("30 minutes before", last_assistant="How long before the appointment should I remind you?")
        self.assertEqual(d.reminder_action, "create")
        self.assertEqual(d.required_tool_groups, (frozenset({"get_upcoming"}), frozenset({"add_reminder"})))
        result, _, _ = await self.run_flow([("get_upcoming", {}),
            ("add_reminder", {"title": "iPhone repair", "when_iso": self.alert.isoformat()}),
            "Done"], "30 minutes before", decision=d)
        self.assertTrue(result.startswith("Reminder set:"))
        self.assertEqual(len(self.reminder_rows()), 1)

    async def test_appointment_time_is_not_alert_time(self):
        d = await route("set an alarm for my repair appointment tomorrow at 1pm")
        self.assertEqual(d.reminder_action, "clarify_time")

    async def test_missing_reminder_followup_does_not_repeat_memory_save(self):
        d = await route("i dont see it", last_assistant="I've set a reminder for your appointment.",
                        last_tools="get_upcoming,remember")
        self.assertEqual(d.tool_subset, ["get_upcoming"])
        self.assertEqual(d.reminder_action, "clarify_time")
        d = await route("i dont see it", last_assistant="Reminder set: repair", last_tools="add_reminder")
        self.assertEqual(d.tool_subset, ["get_upcoming"])  # verify, don't duplicate

    async def test_calendar_read_explicitly_reports_zero_reminders(self):
        text = await A.get_upcoming(days=2)
        self.assertIn("Calendar events: 1; Wisp/Apple reminders: 0", text)
        self.assertIn("[Calendar event]", text)

    async def test_past_date_cannot_count_as_success(self):
        result = await A.add_reminder("repair", "2000-01-01T12:30")
        self.assertIn("past", result)
        self.assertEqual(self.reminder_rows(), [])
        for text in ("", "(bad when_iso)", "(in the past — not added)", "saved a memory"):
            self.assertEqual(classify_tool_outcome("add_reminder", text).status, "failed")

    async def test_dry_run_is_not_success_and_does_not_write(self):
        result, _, _ = await self.run_flow([("add_reminder", {
            "title": "repair", "when_iso": self.alert.isoformat()}), "Done"],
            "set an alarm at noon tomorrow", test_mode=True)
        self.assertIn("Dry run only", result)
        self.assertEqual(self.reminder_rows(), [])

    async def test_denial_is_not_success_and_does_not_write(self):
        from service.safety.policy import Decision, Tier
        with patch.object(loop, "decide", return_value=Decision(Tier.CONFIRM, "test confirmation")):
            result, _, _ = await self.run_flow([("add_reminder", {
                "title": "repair", "when_iso": self.alert.isoformat()}), "Done"],
                "set an alarm at noon tomorrow", approve=False)
        self.assertIn("not completed", result)
        self.assertEqual(self.reminder_rows(), [])

    @unittest.skipUnless(os.environ.get("WISP_LIVE_REMINDER_TEST") == "1", "opt-in local Ling integration")
    async def test_live_ling_asks_then_creates_from_lead_time(self):
        from service.inference.omlx_client import OMLXClient
        remote = OMLXClient()

        class LiveClient:
            async def ensure_only(self, *args, **kwargs):
                pass  # Never switch the user's loaded model in a fixture test.

            async def stream_events(self, *args, **kwargs):
                async for event in remote.stream_events(*args, **kwargs):
                    yield event

        history = [{"role": "user", "content": ORIGINAL}]
        events = []

        async def emit(event):
            events.append(event)

        async def turn(prompt, previous=None):
            d = await route(prompt, last_assistant=previous)
            return await loop.run_agent(LiveClient(), "Ling-3.0-tiny-oQ4e", history,
                emit, type("Approver", (), {"confirm": AsyncMock(return_value=True)})(),
                tools=d.tool_subset, expect_tool_first=d.expect_tool_first,
                required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
                reminder_action=d.reminder_action, multi_round=d.multi_round, max_steps=5)

        answer = await turn(ORIGINAL)
        self.assertIn("What time", answer)
        self.assertEqual(self.reminder_rows(), [])
        history.extend([{"role": "assistant", "content": answer},
                        {"role": "user", "content": "30 minutes before"}])
        answer = await turn("30 minutes before", answer)
        self.assertTrue(answer.startswith("Reminder set:"), answer)
        self.assertEqual(len(self.reminder_rows()), 1)
        self.assertEqual(self.reminder_rows()[0]["when_ts"], self.alert.timestamp())
        self.assertFalse(any(e.get("type") == "delta" for e in events))

    async def test_read_only_and_unrelated_requests_cannot_create_reminders(self):
        d = await route("do not set an alarm at noon tomorrow")
        self.assertNotIn("add_reminder", d.tool_subset)
        d = await route("what's the weather tomorrow at noon?", last_assistant="What time should I remind you?")
        self.assertEqual(d.reminder_action, "")


if __name__ == "__main__":
    unittest.main()
