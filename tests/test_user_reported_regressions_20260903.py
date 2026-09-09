"""Strict regressions for the Wisp failures reported on 2026-09-03."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.agent import loop  # noqa: E402
from service.assistant import brief  # noqa: E402
from service.router.router import route  # noqa: E402
from service.tools import action_tools, assistant_tools  # noqa: E402
from service.tools.registry import REGISTRY, Tool  # noqa: E402
from service.workflows.compiler import compile_new  # noqa: E402
from service.reminder_intent import has_alert_time, is_time_answer  # noqa: E402


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_events(self, *args, **kwargs):
        self.calls += 1
        yield {"kind": "content", "text": "unexpected narration"}
        yield {"kind": "final", "message": {"role": "assistant",
                                                "content": "unexpected narration",
                                                "tool_calls": None}}


class _Approver:
    async def confirm(self, action):
        raise AssertionError(f"draft_message unexpectedly requested approval: {action}")


class UserReportedRegressions20260903(unittest.TestCase):
    def test_send_me_a_reminder_keeps_people_inside_the_reminder_text(self) -> None:
        prompt = ("can u send me a reminder before my move in date to ask trishy "
                  "for a 48 or 64gb mac min")
        self.assertIsNone(compile_new(prompt), "must not become an outbound workflow")
        decision = asyncio.run(route(prompt))
        self.assertEqual(decision.reminder_action, "clarify_time")
        self.assertEqual(decision.tool_subset, ["get_upcoming"])
        self.assertEqual(decision.force_first_tool, "get_upcoming")
        self.assertEqual([set(group) for group in decision.required_tool_groups],
                         [{"get_upcoming"}])
        self.assertFalse(decision.clarify_channel)
        self.assertFalse({"lookup_contact", "send_message", "send_email",
                          "draft_message", "draft_email"}
                         & set(decision.tool_subset or ()))

    def test_ownership_correction_preserves_pending_reminder_question(self) -> None:
        prior = ("can u send me a reminder before my move in date to ask trishy "
                 "for a 48 or 64gb mac min")
        decision = asyncio.run(route(
            "oh i need it and do it on reminders",
            last_user=prior,
            last_assistant=("I haven’t created a reminder yet. What time should I "
                            "remind you, or how long before the appointment?"),
            last_tools="get_upcoming"))
        self.assertEqual(decision.reminder_action, "clarify_time")
        self.assertEqual(decision.tool_subset, ["get_upcoming"])
        self.assertFalse(decision.direct_calls,
                         "must not dump a router-direct raw calendar result")
        self.assertFalse(decision.clarify_channel)

        class EmptyClient:
            async def ensure_only(self, *args, **kwargs):
                return None

            async def stream_events(self, *args, **kwargs):
                yield {"kind": "final", "message": {
                    "role": "assistant", "content": "", "tool_calls": None}}

        events: list[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        answer = asyncio.run(loop.run_agent(
            EmptyClient(), "test-model",
            [{"role": "user", "content": prior},
             {"role": "assistant", "content": "How long before should I remind you?"},
             {"role": "user", "content": "oh i need it and do it on reminders"}],
            emit, _Approver(), tools=decision.tool_subset,
            reminder_action=decision.reminder_action, max_steps=2))
        self.assertIn("how long before your move-in date?", answer)
        self.assertNotIn("Upcoming (", answer,
                         "empty narration must never leak the raw calendar output")

    def test_day_before_is_a_valid_answer_to_reminder_timing_question(self) -> None:
        for answer in ("a day before", "the day before", "two days before"):
            self.assertTrue(has_alert_time(answer), answer)
            self.assertTrue(is_time_answer(answer), answer)

    def test_send_me_email_summary_is_an_inline_read(self) -> None:
        prompt = "send me my email summaries for today"
        self.assertIsNone(compile_new(prompt), "must not create an outbound workflow")
        decision = asyncio.run(route(prompt))
        self.assertEqual(decision.tool_subset, ["summarize_emails"])
        self.assertEqual(decision.direct_calls,
                         [("summarize_emails", {"day": "today"})])
        self.assertFalse(decision.clarify_channel)
        self.assertFalse({"send_email", "send_message", "draft_email", "draft_message"}
                         & set(decision.tool_subset or ()))

    def test_world_news_message_has_a_complete_ordered_route(self) -> None:
        prompt = "send a message to mom with the world news for today"
        plan = compile_new(prompt)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.sources, ["news"])
        self.assertEqual((plan.channel, plan.recipient, plan.delivery),
                         ("messages", "mom", "send"))
        decision = asyncio.run(route(prompt))
        self.assertEqual(decision.tool_subset,
                         ["web_search", "lookup_contact", "send_message"])
        self.assertEqual([set(group) for group in decision.required_tool_groups],
                         [{"web_search"}, {"lookup_contact"}, {"send_message"}])

    def test_message_draft_emits_structured_card_and_stops_narration(self) -> None:
        original = REGISTRY["draft_message"]

        async def fake_draft(to: str, text: str) -> str:
            return f"Message draft prepared in Wisp for {to} — nothing has been sent."

        REGISTRY["draft_message"] = Tool(
            name="draft_message", description="fake editable draft",
            parameters={"type": "object", "properties": {
                "to": {"type": "string"}, "text": {"type": "string"}},
                "required": ["to", "text"]},
            category="messages_draft", func=fake_draft)
        events: list[dict] = []

        async def emit(event: dict) -> None:
            events.append(event)

        client = _Client()
        try:
            answer = asyncio.run(loop.run_agent(
                client, "Agents-A1-4B-oQe6",
                [{"role": "user", "content": "draft Mom a message"}],
                emit, _Approver(), tools=["draft_message"], max_steps=2,
                direct_calls=[("draft_message", {"to": "Mom", "text": "Hi Mom"})]))
        finally:
            REGISTRY["draft_message"] = original

        card = next((event for event in events
                     if event.get("type") == "message_draft"), None)
        self.assertEqual(card, {"type": "message_draft", "to": "Mom",
                                "text": "Hi Mom"})
        self.assertEqual(answer, "")
        self.assertEqual(client.calls, 0, "the model must not replace the card with prose")

    def test_calendar_summary_excludes_holiday_calendar_by_default(self) -> None:
        events = [
            {"source": "calendar", "context": "US Holidays", "account": "Subscribed Calendars",
             "when_ts": 1_800_000_000.0, "kind": "event", "title": "Labor Day",
             "all_day": True, "organizer": "", "location": ""},
            {"source": "calendar", "context": "Personal", "account": "iCloud",
             "when_ts": 1_800_000_100.0, "kind": "meeting", "title": "Move-in prep",
             "all_day": False, "organizer": "", "location": ""},
        ]
        readiness = {"sources": [
            {"id": "calendar", "label": "Calendar", "state": "ready"},
            {"id": "reminders", "label": "Reminders", "state": "ready"},
        ]}
        with patch("service.assistant.sync_status.ensure_sources",
                   new=AsyncMock(return_value=readiness)), \
             patch.object(assistant_tools.assistant_store, "upcoming", return_value=events), \
             patch.object(assistant_tools.time, "time", return_value=1_799_999_000.0):
            default = asyncio.run(assistant_tools.get_upcoming(days=7))
            explicit = asyncio.run(assistant_tools.get_upcoming(days=7, include_holidays=True))

        self.assertNotIn("Labor Day", default)
        self.assertIn("Move-in prep", default)
        self.assertIn("Labor Day", explicit)

    def test_daily_brief_calendar_context_excludes_holiday_calendar(self) -> None:
        events = [
            {"source": "calendar", "context": "US Holidays", "account": "Subscribed Calendars",
             "when_ts": 1_800_000_000.0, "kind": "event", "title": "Rosh Hashanah",
             "all_day": True, "organizer": "", "location": ""},
            {"source": "calendar", "context": "Personal", "account": "iCloud",
             "when_ts": 1_800_000_100.0, "kind": "meeting", "title": "Office hours",
             "all_day": False, "organizer": "", "location": ""},
        ]

        def ready(source: str) -> dict:
            return {"id": source, "label": source.title(), "state": "ready"}

        with patch("service.assistant.sync_status.source_status", side_effect=ready), \
             patch.object(brief.assistant_store, "upcoming", return_value=events):
            block = brief._calendar_block(1_799_999_000.0)
        self.assertNotIn("Rosh Hashanah", block)
        self.assertIn("Office hours", block)

    def test_draft_send_endpoint_uses_exact_edited_payload(self) -> None:
        # The endpoint is exercised with a mocked Messages bridge; this test
        # must never send a real text.
        from service import main

        fake = AsyncMock(return_value="Message sent to Mom (+15555550123).")
        with patch.object(action_tools, "send_message", new=fake):
            result = asyncio.run(main.assistant_send_message_draft(
                {"to": "Mom", "text": "The edited final text"}))
        self.assertTrue(result["ok"])
        fake.assert_awaited_once_with(to="Mom", text="The edited final text")


if __name__ == "__main__":
    unittest.main()
