from __future__ import annotations

import unittest

import service.tools  # noqa: F401
from service.router.reranker import _action_clauses
from service.router.router import route


class CompoundTaskRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_actions_get_ordered_singleton_obligations(self) -> None:
        prompt = (
            "Please do these in this order: check my calendar tomorrow; then "
            "list saved contact names containing Route; then convert 68 degrees "
            "Fahrenheit to Celsius; then list connected MCP servers and their "
            "tool counts; then write a Word summary to /tmp/brief.docx."
        )
        decision = await route(prompt)
        expected = [
            "get_upcoming", "list_contacts", "convert_units", "wisp_mcp",
            "write_document",
        ]
        self.assertEqual(expected, decision.tool_subset)
        self.assertEqual(
            tuple(frozenset({name}) for name in expected),
            decision.required_tool_groups,
        )
        self.assertTrue(decision.multi_round)
        self.assertEqual([], decision.direct_calls)

    async def test_read_only_compound_does_not_offer_outbound_tools(self) -> None:
        prompt = (
            "Please do these in this order: check my Work calendar for tomorrow; "
            "then list saved contact names containing Route; then read Mom's text "
            "messages from yesterday; then find Route pages I visited yesterday."
        )
        decision = await route(prompt)
        offered = set(decision.tool_subset or [])
        self.assertTrue(
            {"get_upcoming", "list_contacts", "view_messages",
             "search_browser_history"} <= offered)
        self.assertFalse(
            {"send_message", "send_email", "forward_email", "run_shell"} & offered)

    async def test_unsent_constraint_never_adds_a_send_or_extra_action(self) -> None:
        prompt = (
            "Prepare an unsent text to Mom saying Please review this. "
            "Then summarize yesterday's email. "
            "Leave the message unsent for review only."
        )
        decision = await route(prompt)
        offered = set(decision.tool_subset or [])
        self.assertIn("draft_message", offered)
        self.assertIn("summarize_emails", offered)
        self.assertFalse({"send_message", "send_email"} & offered)
        self.assertEqual(2, len(decision.required_tool_groups))

    def test_bare_semicolon_keeps_authorization_with_destructive_action(self) -> None:
        prompt = (
            "show a preview, then delete only saved facts containing Route old job; "
            "I authorize that exact scoped cleanup; then list connected MCP servers"
        )
        self.assertEqual(
            [
                "show a preview, then delete only saved facts containing Route old "
                "job; I authorize that exact scoped cleanup",
                "list connected MCP servers",
            ],
            _action_clauses(prompt),
        )


if __name__ == "__main__":
    unittest.main()
