"""Current public-information prompts use Ling's dedicated web-search path."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_scratch = tempfile.TemporaryDirectory()
os.environ["WISP_HOME"] = _scratch.name

from service.router.pinning import apply_session_pin  # noqa: E402
from service.router.router import _LING_WEB_MODEL, route  # noqa: E402


class CurrentWebRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_direct_ling_search(self, prompt: str) -> None:
        decision = await route(prompt)

        self.assertEqual(decision.role, "agent")
        self.assertEqual(decision.model, _LING_WEB_MODEL)
        self.assertTrue(decision.needs_tools)
        self.assertEqual(decision.tool_subset, ["web_search"])
        self.assertEqual(decision.direct_calls, [("web_search", {"query": prompt})])
        self.assertEqual(decision.force_first_tool, None)
        self.assertEqual(decision.required_tool_groups,
                         (frozenset({"web_search"}),))
        self.assertEqual(decision.tool_argument_bindings,
                         {"web_search": {"query": prompt}})
        self.assertIn("run_shell", decision.forbidden_tools)
        self.assertIn("http_request", decision.forbidden_tools)

    async def test_current_public_event_phrasings_search_directly(self) -> None:
        for prompt in (
            "what is the situation in Iran right now",
            "what's happening in Sudan",
            "latest developments in Ukraine",
            "what is going on in Taiwan currently",
            "recent developments regarding the EU AI Act",
            "give me a brief on the situation in Iran",
            "what is the latest situation in Iran",
            "what is happening with Iran right now",
            "tell me the current situation in Ukraine",
            "update me on the situation in Taiwan",
            "what's the latest on the EU AI Act?",
            "how are things developing in Gaza right now?",
            "what changed today in Taiwan",
            "latest developments in Python",
        ):
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)

    async def test_explicit_web_and_news_requests_also_stay_on_ling(self) -> None:
        for prompt in (
            "search the web for the latest Python release",
            "research online using the web",
            "latest news about semiconductor exports",
            "show me today's headlines",
        ):
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)

    async def test_session_pin_cannot_replace_ling_on_direct_web_route(self) -> None:
        prompt = "what's happening in Iran"
        decision = await route(prompt)
        pinned = apply_session_pin(
            decision,
            {"pinned_role": "coding", "pinned_model": "Huihui-Ornith-test"},
            prompt,
        )
        self.assertIs(pinned, decision)
        self.assertEqual(pinned.model, _LING_WEB_MODEL)
        self.assertEqual(pinned.direct_calls,
                         [("web_search", {"query": prompt})])

    async def test_configured_agent_model_cannot_replace_ling(self) -> None:
        with patch("service.router.router.role_to_model",
                   return_value="Huihui-Ornith-test"):
            decision = await route("what's happening in Iran")
        self.assertEqual(decision.model, _LING_WEB_MODEL)
        self.assertEqual(decision.direct_calls,
                         [("web_search", {"query": "what's happening in Iran"})])

    async def test_explicit_no_web_request_wins_over_current_wording(self) -> None:
        for prompt in (
            "what is the situation in Iran right now without using the web",
            "latest developments in Ukraine, but don't search the web",
            "what's happening in Sudan? Answer from existing knowledge only",
            "what's happening in Sudan, don't go online",
            "what's happening in Sudan, offline only",
            "what's happening in Sudan without external sources",
            "what's happening in Sudan, don't browse",
            "what's happening in Sudan? Don't look it up online.",
            "what's happening in Sudan? Use only what you already know.",
        ):
            with self.subTest(prompt=prompt):
                decision = await route(prompt)
                self.assertFalse(decision.needs_tools)
                self.assertEqual(decision.role, "agent")
                self.assertEqual(decision.model, _LING_WEB_MODEL)
                self.assertEqual(decision.direct_calls, [])
                self.assertIsNone(decision.tool_subset)
                self.assertTrue(
                    {"web_search", "web_fetch", "http_request", "run_shell"}
                    <= decision.forbidden_tools
                )

    async def test_no_web_request_cannot_regain_shell_from_agent_pin(self) -> None:
        prompt = "latest news about semiconductor exports, but don't search the web"
        decision = await route(prompt)
        pinned = apply_session_pin(
            decision,
            {"pinned_role": "agent", "pinned_model": "Huihui-Ornith-stale"},
            prompt,
        )
        self.assertEqual(pinned.model, _LING_WEB_MODEL)
        self.assertFalse(pinned.needs_tools)
        self.assertIsNone(pinned.tool_subset)
        self.assertIn("run_shell", pinned.forbidden_tools)

    async def test_current_outbound_report_requires_web_search(self) -> None:
        prompt = "what is happening in Iran and text Mom a summary"
        decision = await route(prompt)
        offered = set(decision.tool_subset or ())
        self.assertEqual(decision.model, _LING_WEB_MODEL)
        self.assertIn("web_search", offered)
        self.assertIn("lookup_contact", offered)
        self.assertIn("send_message", offered)
        self.assertIn(frozenset({"web_search"}), decision.required_tool_groups)
        self.assertNotIn("run_shell", offered)
        self.assertFalse(any("wikipedia" in name for name in offered))

    async def test_non_public_or_non_current_questions_keep_existing_routes(self) -> None:
        prompts = (
            "explain how Iran's political system works",
            "what's happening on my calendar today",
            "what's happening in my messages",
            "what's happening in this Python function",
            "what is the situation in Iran in 1979?",
            "what is the situation in Iran during the 1980s?",
            "what's happening in the plot of this novel?",
            "what's happening in my Downloads folder?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                decision = await route(prompt)
                self.assertNotIn("web_search",
                                 {name for name, _ in decision.direct_calls})
                self.assertFalse(
                    decision.model == _LING_WEB_MODEL
                    and decision.tool_subset == ["web_search"],
                    decision.reason,
                )


if __name__ == "__main__":
    unittest.main()
