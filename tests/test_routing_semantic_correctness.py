"""Meaning-based regressions discovered during routing latency parity review."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import service.tools  # noqa: F401
from service.router import router as R
from service.router.web_request import classify


class SemanticRoutingCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(R, "models_config", return_value={
            "tool_retrieval": {"provider": "lexical"}}))

    async def test_stock_comparison_uses_named_companies_not_reference(self):
        prompt = ("can you send a message to mom with the share prices of google and micron "
                  "and compare it to the price of these shares from exactly three weeks ago? Be descriptive")
        d = await R.route(prompt)
        self.assertIn(("get_stock_price", {"symbols": ["google", "micron"], "period": "3 weeks"}), d.direct_calls)
        self.assertIn(frozenset({"send_message"}), d.required_tool_groups)
        self.assertIn("send_email", d.forbidden_tools)

    def test_exact_stock_names_and_unresolved_references(self):
        for text, names in (
            ("share price of Apple and Microsoft from exactly two weeks ago", ["Apple", "Microsoft"]),
            ("share prices of AMD, NVDA and Intel compared with their prices from three weeks ago", ["AMD", "NVDA", "Intel"]),
            ("price of Berkshire Hathaway from exactly four weeks ago", ["Berkshire Hathaway"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(R._stock_exact_args(text)["symbols"], names)
        for text in ("price of these shares from three weeks ago", "price of them from two weeks ago"):
            with self.subTest(text=text):
                self.assertIsNone(R._stock_exact_args(text))

    async def test_bare_personal_schedule_stays_local_and_read_only(self):
        for prompt in ("Show me tomorrow's schedule, but don't add or change anything.",
                       "Show me today's schedule.", "Show me the schedule for tomorrow.",
                       "What's tomorrow's schedule?", "What is today's agenda?",
                       "Show me today's appointments.", "What are tomorrow's meetings?",
                       "Any meetings tomorrow?", "Any appointments today?",
                       "Any classes this week?", "What meetings are scheduled tomorrow?"):
            with self.subTest(prompt=prompt):
                self.assertFalse(classify(prompt).allowed)
                d = await R.route(prompt)
                self.assertIn("get_upcoming", d.tool_subset)
                self.assertNotIn("web_search", d.tool_subset)
                self.assertFalse({"add_calendar_event", "update_event", "cancel_event", "add_reminder"} & set(d.tool_subset))

    async def test_explicit_public_schedules_still_search(self):
        for prompt in ("Search the web for tomorrow's train schedule.",
                       "Show me tomorrow's public conference schedule.",
                       "Show me tomorrow's schedule for the Olympics.",
                       "Any Olympics events tomorrow?", "Any public conference events today?"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertIn("web_search", d.tool_subset)
                self.assertNotIn("get_upcoming", d.tool_subset)

    async def test_confirmation_retains_immediately_offered_note_action(self):
        for offer in ("I can create that note. Would you like me to go ahead?",
                      "I can create a note called Trip plan. Shall I do it?",
                      "I can create that note if you'd like. Shall I go ahead?"):
            with self.subTest(offer=offer):
                d = await R.route("Yes, go ahead.", last_assistant=offer,
                                  last_tools="get_upcoming create_note send_email")
                self.assertIn("create_note", d.tool_subset)
                self.assertIn("create", d.resolved_request.lower())
                self.assertNotIn("send_email", d.tool_subset)
                self.assertNotIn("send_message", d.tool_subset)

    async def test_generic_confirmation_cannot_invent_or_replay_action(self):
        for offer in ("Would you like me to go ahead?",
                      "I cannot create that note. Would you like me to go ahead?",
                      "I can never create that note. Would you like me to go ahead?",
                      "I can no longer create that note. Would you like me to go ahead?",
                      "I can create that note. The weather is sunny. Would you like me to go ahead?",
                      'You said "I can create that note." Would you like me to go ahead?'):
            with self.subTest(offer=offer):
                d = await R.route("Yes", last_assistant=offer, last_tools="create_note send_email")
                self.assertFalse(d.needs_tools)

    async def test_explicit_current_offer_wins_and_completed_offer_not_replayed(self):
        d = await R.route("Yes", last_assistant="I can create that note. Shall I explain it instead?")
        self.assertNotIn("create_note", d.tool_subset or [])
        d = await R.route("Yes", last_assistant="Done, I created that note. Would you like me to go ahead?")
        self.assertFalse(d.needs_tools)

    async def test_note_confirmation_with_current_user_context(self):
        for context in ({"last_user": "Write that up as a note."},
                        {"recent_users": ["Check tomorrow's calendar.", "Create a note with those plans."]},
                        {"last_user": "Save that in Notes."},
                        {"last_user": "Add a note with those plans."},
                        {"last_user": 'Create a note saying "do not deploy Friday".'}):
            with self.subTest(context=context):
                d = await R.route("Yes, go ahead.",
                                  last_assistant="I can create that note. Would you like me to go ahead?",
                                  last_tools="send_email get_upcoming", **context)
                self.assertIn("create_note", d.tool_subset)
                self.assertNotIn("send_email", d.tool_subset)
                source = context.get("last_user") or context["recent_users"][-1]
                self.assertIn(source, d.resolved_request)
        for prior in ("Don't create that note.", "Search the web for news and email it to Mom.",
                      "Explain how to create a note."):
            with self.subTest(prior=prior):
                d = await R.route("Yes", last_user=prior,
                                  last_assistant="I can create that note. Shall I go ahead?",
                                  last_tools="create_note send_email")
                self.assertFalse(d.needs_tools)
