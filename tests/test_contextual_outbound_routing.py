"""Regression tests for grounded multi-turn outbound reports.

These prompts are taken from the failed live Wisp conversation on 2026-09-01.
The strict assertions are intentional: merely offering a plausible tool is not
enough. The route must require the source, recipient resolution, and exact
delivery tool in order, while withholding neighboring effect tools.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.memory.store import SessionStore  # noqa: E402
from service.router.router import route  # noqa: E402


def routed(prompt: str, **context):
    return asyncio.run(route(prompt, **context))


def groups(decision) -> list[set[str]]:
    return [set(group) for group in decision.required_tool_groups]


class ContextualOutboundRoutingTests(unittest.TestCase):
    def assert_plan(self, decision, expected: list[str]) -> None:
        self.assertEqual(decision.tool_subset, expected)
        self.assertEqual(groups(decision), [{name} for name in expected])
        self.assertTrue(decision.multi_round)
        self.assertFalse(decision.clarify_channel)

    def test_calendar_summary_to_mom_via_messages_is_ordered(self) -> None:
        decision = routed(
            "Send Mom a message with a summary of my calendar tomorrow.")
        self.assert_plan(
            decision, ["get_upcoming", "lookup_contact", "send_message"])
        self.assertNotIn("add_reminder", decision.tool_subset or ())
        self.assertNotIn("schedule_send", decision.tool_subset or ())

    def test_email_summaries_to_contact_via_messages_is_ordered(self) -> None:
        decision = routed("send trishe a message with my email summaries")
        self.assert_plan(
            decision, ["summarize_emails", "lookup_contact", "send_message"])
        self.assertNotIn("forward_email", decision.tool_subset or ())

    def test_yes_recovers_prior_channel_recipient_and_payload(self) -> None:
        decision = routed(
            "yes",
            last_user="ok send my calender to mom via messages",
            last_assistant="Want me to send these to your mom now?",
            last_tools="get_upcoming",
        )
        self.assert_plan(
            decision, ["get_upcoming", "lookup_contact", "send_message"])
        self.assertFalse({"add_reminder", "add_calendar_event", "send_email"}
                         & set(decision.tool_subset or ()))

    def test_explicit_address_continuation_uses_send_not_forward(self) -> None:
        decision = routed(
            "send it to archanachetan888@gmail.com",
            last_user="ok send it",
            recent_users=["send mom my calender for this month", "ok send it"],
            last_assistant="I need Mom's email address.",
            last_tools=None,
        )
        self.assert_plan(decision, ["get_upcoming", "send_email"])
        self.assertNotIn("forward_email", decision.tool_subset or ())

    def test_stock_confirmation_refetches_before_email_send(self) -> None:
        decision = routed(
            "yes",
            last_user="send my stock report to mom via email",
            last_assistant="Would you like me to send the report?",
            last_tools="get_stock_price, forward_email, view_messages",
        )
        self.assert_plan(
            decision, ["get_stock_price", "lookup_contact", "send_email"])
        self.assertNotIn("send_message", decision.tool_subset or ())

    def test_contact_answer_keeps_original_email_summary_task(self) -> None:
        decision = routed(
            "its on contacts",
            last_user="send trishe a message with my email summaries",
            last_assistant="I don't have a saved contact for Trishe.",
            last_tools="search_notes",
        )
        self.assert_plan(
            decision, ["summarize_emails", "lookup_contact", "send_message"])

    def test_unnamed_channel_cannot_silently_send(self) -> None:
        decision = routed(
            "send mom my email summaries and my schedule for this month")
        self.assertTrue(decision.clarify_channel)
        self.assertEqual(
            decision.tool_subset,
            ["get_upcoming", "summarize_emails", "lookup_contact"],
        )
        self.assertFalse({"send_message", "send_email", "draft_message",
                          "draft_email", "forward_email"}
                         & set(decision.tool_subset or ()))

    def test_channel_answer_restores_the_whole_pending_report(self) -> None:
        decision = routed(
            "messages",
            last_user="send mom my email summaries and my schedule for this month",
            recent_users=[
                "send mom my email summaries and my schedule for this month"],
            last_assistant="Should I send that via Messages or email?",
            last_tools="get_upcoming, summarize_emails",
        )
        self.assert_plan(
            decision,
            ["get_upcoming", "summarize_emails", "lookup_contact", "send_message"],
        )

    def test_scheduled_calendar_message_uses_schedule_send_only(self) -> None:
        decision = routed(
            "scedule send a message to mom at 9pm with my schedule for tmrow")
        self.assert_plan(
            decision, ["get_upcoming", "lookup_contact", "schedule_send"])
        self.assertNotIn("send_message", decision.tool_subset or ())

    def test_draft_calendar_message_cannot_send(self) -> None:
        decision = routed("draft a message to Mom with my calendar summary")
        self.assert_plan(
            decision, ["get_upcoming", "lookup_contact", "draft_message"])
        self.assertNotIn("send_message", decision.tool_subset or ())

    def test_scheduled_news_report_does_not_read_inbox(self) -> None:
        decision = routed("send a news report to my email at 10am today")
        self.assert_plan(decision, ["web_search", "schedule_send"])
        self.assertFalse({"view_emails", "summarize_emails", "forward_email"}
                         & set(decision.tool_subset or ()))

    def test_store_exposes_previous_user_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp) / "sessions.db")
            sid = store.create_session()
            store.add_turn(sid, "user", "send Mom my calendar via Messages")
            store.add_turn(sid, "assistant", "Want me to send it?",
                           tool_digest="get_upcoming")
            self.assertEqual(
                store.last_user_turn(sid),
                "send Mom my calendar via Messages",
            )
            store.add_turn(sid, "user", "ok send it")
            self.assertEqual(
                store.recent_user_turns(sid),
                ["send Mom my calendar via Messages", "ok send it"],
            )

if __name__ == "__main__":
    unittest.main()
