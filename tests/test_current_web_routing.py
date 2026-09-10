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
from service.router.router import _LING_WEB_MODEL, _classify_web_request, route  # noqa: E402


class CurrentWebRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_direct_ling_search(self, prompt: str, *,
                                        last_user: str | None = None,
                                        query: str | None = None) -> None:
        with patch("service.router.router.role_to_model", return_value="Huihui-Ornith-test"):
            decision = await route(prompt, last_user=last_user)
        query = query if query is not None else prompt

        self.assertEqual(decision.role, "agent")
        self.assertEqual(decision.model, _LING_WEB_MODEL)
        self.assertTrue(decision.needs_tools)
        self.assertEqual(decision.tool_subset, ["web_search"])
        self.assertEqual(decision.direct_calls, [("web_search", {"query": query})])
        self.assertEqual(decision.force_first_tool, None)
        self.assertEqual(decision.required_tool_groups,
                         (frozenset({"web_search"}),))
        self.assertEqual(decision.tool_argument_bindings,
                         {"web_search": {"query": query}})
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
            "give me an update on the situation in Iran",
            "what is the status of the conflict in Sudan right now",
            "are there any new developments in Ukraine",
            "what's happening with the movie industry right now",
            "what's happening in the movie industry right now",
            "what's happening with book bans right now",
            "what happened in Iran today?",
            "what has happened in Ukraine this week?",
            "what happened in Sudan yesterday?",
            "what happened today in Iran?",
            "what happened this morning in Ukraine?",
            "what's new in Iran today?",
            "any updates on Iran today?",
        ):
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)

    async def test_explicit_web_and_news_requests_also_stay_on_ling(self) -> None:
        for prompt in (
            "search the web for the latest Python release",
            "research online using the web",
            "latest news about semiconductor exports",
            "show me today's headlines",
            "look up the latest Python release online",
            "find the latest Python release online",
            "check the latest Python release online",
            "Search the web for reports about communities with no internet",
            "search the web for offline maps",
            "search the web for Apple's latest product release",
            "search the web for Iran's political history",
            "search the web for cities without internet access",
            "search the web for how to avoid using the internet",
            "search the web for OpenAI's code interpreter",
            "search the web for climate change news",
            "search the web for how to delete a file in Python",
            "use the web to find the latest Python release",
            "use the internet to check the latest Rust release",
            "search the web for communities that avoid using the internet",
            "search the web for communities that do not use the internet",
            "search the web for communities that are without any internet",
            "search the web for tools to use without any web access",
            "search the web for Google's projects",
            "search the web for OpenAI's projects",
            "search the web for Google's Calendar API changes",
            "search the web for Rust's source code",
            "search the web for OpenAI's latest project roadmap",
            "use the web to find communities that do not use the internet",
            "look up communities that avoid using the internet online",
            "check the web for the latest Python release",
            "do a web search for the latest Python release",
            "look it up on the web: latest Python release",
            "search the web for cities, towns and villages that do not use the internet",
            "search the web for tools that work without browsing",
            "search the web for the schedule of the Olympics",
            "search the web for how to text and send messages in Python",
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
            "what's happening in Sudan? Don’t browse the internet.",
            "what's happening in Sudan? Avoid using the internet.",
            "what's happening in Sudan? Please refrain from browsing the web.",
            "what's happening in Sudan? Use your existing knowledge only.",
            "what happened in Iran today without a web search",
            "what happened in Iran today without any web search",
            "what happened in Iran today with no browsing",
            "look up the latest Python release online; cancel the web search",
            "latest news in Iran; stop browsing",
            "skip the web search",
            "what happened in Iran today? No browsing",
            "look up the latest Python release online; cancel that search",
            "search the web for cities without internet access; avoid browsing",
            "Don't search for the latest news",
            "stop searching for the latest news in Iran",
            "Never research the latest news in Iran",
            "search the web for communities that avoid the internet; avoid browsing",
            "search the web for tools to use without any web access, but without a web search",
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

    async def test_private_context_never_becomes_a_public_query(self) -> None:
        for context in (
            "our calendar", "team calendar", "Adi's calendar", "Adi’s calendar",
            "Mom's messages", "shared inbox", "our project", "John's code",
            "our team's project", "their internal notes", "the shared team inbox",
            "the developers' code", "our private project documents",
            "our confidential quarterly strategy project", "our calendars",
            "Adi's schedule", "emails from Mom", "our Apollo roadmap", "my codebase",
            "our Google Calendar appointments", "our OpenAI integration roadmap",
            "our Google's project",
            "John's Rust source code",
            "the calendar for our team", "Google's integration with our codebase",
            "Google's access to my email",
        ):
            for prompt in (f"what's happening in {context} today?",
                           f"search the web for news from {context}",
                           f"what about {context}?"):
                with self.subTest(prompt=prompt):
                    decision = await route(prompt, last_user="what happened in Iran today?")
                    self.assertNotIn("web_search", {n for n, _ in decision.direct_calls})
                    self.assertNotIn("web_search", decision.tool_argument_bindings)
                    self.assertTrue({"web_search", "web_fetch", "http_request"}
                                    <= decision.forbidden_tools)
                    self.assertFalse({"web_search", "web_fetch", "http_request"}
                                     & set(decision.tool_subset or ()))

    async def test_followups_keep_public_topic_and_override_old_time_scope(self) -> None:
        for prior, prompt, query in (
            ("what's happening in Iran", "and in Ukraine?", "Ukraine news latest"),
            ("what happened in Iran today?", "what about Ukraine yesterday?",
             "Ukraine yesterday news"),
            ("news in Iran today", "and yesterday?", "news in Iran yesterday"),
            ("latest news in Iran today", "and last week?", "news in Iran last week"),
            ("news in Iran yesterday", "what about Ukraine today?", "Ukraine today news"),
            ("latest news in Iran last Tuesday", "and today?", "news in Iran today"),
            ("news in Iran yesterday", "what about Ukraine this morning?",
             "Ukraine this morning news"),
            ("news in Iran today", "what about Ukraine last Tuesday?",
             "Ukraine last Tuesday news"),
            ("what happened in Iran yesterday?", "what about tomorrow?",
             "what happened in Iran tomorrow"),
            ("what happened in Iran today?", "what happened in Taiwan yesterday?",
             "what happened in Taiwan yesterday?"),
        ):
            with self.subTest(prior=prior, prompt=prompt):
                await self.assert_direct_ling_search(prompt, last_user=prior, query=query)

    async def test_offline_followup_overrides_public_context(self) -> None:
        for prompt in ("what about Ukraine without any web search?", "cancel the web search"):
            with self.subTest(prompt=prompt):
                d = await route(prompt, last_user="what happened in Iran today?")
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertFalse(d.needs_tools)
                self.assertEqual(d.direct_calls, [])
                self.assertTrue({"web_search", "web_fetch", "http_request", "run_shell"}
                                <= d.forbidden_tools)

    async def test_followup_cannot_convert_a_new_local_or_explanatory_task(self) -> None:
        for prompt in ("and explain Python recursion", "what about the plot of this novel?",
                       "and debug John's code", "and open my calendar",
                       "what about writing a Python function?",
                       "what about debugging a Python function?",
                       "and how do I implement this in Python?",
                       "Explain Google Chrome", "Explain Bing Crosby",
                       "Explain Google Chrome's architecture"):
            with self.subTest(prompt=prompt):
                d = await route(prompt, last_user="what happened in Iran today?")
                self.assertNotIn("web_search", {name for name, _ in d.direct_calls})

    async def test_delivery_is_not_a_private_source_or_part_of_the_query(self) -> None:
        for source, delivery, effect in (
            ("look up the latest Python release online", "email it to our team inbox", "send_email"),
            ("use the web to find the latest Rust release", "email it to Adi", "send_email"),
            ("search the web for climate news", "text Mom a summary", "send_message"),
            ("what is happening in Iran", "text Mom a summary", "send_message"),
        ):
            prompt = f"{source} and {delivery}"
            with self.subTest(prompt=prompt):
                request = _classify_web_request(prompt)
                self.assertEqual(request.source, source)
                self.assertFalse(request.private)
                self.assertEqual(request.delivery.text, delivery)
                with patch("service.router.router.role_to_model", return_value="Ornith-test"):
                    d = await route(prompt)
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertEqual(d.tool_argument_bindings["web_search"], {"query": source})
                self.assertEqual(d.required_tool_groups[0], frozenset({"web_search"}))
                self.assertIn(frozenset({effect}), d.required_tool_groups)
                self.assertIn(effect, d.tool_subset)
                self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)
                self.assertFalse({"get_upcoming", "view_emails", "summarize_emails"}
                                 & set(d.tool_subset or ()))

    async def test_public_outbound_query_survives_separators_and_followups(self) -> None:
        for prompt, prior, query, effect in (
            ("look up the latest Python release online; email it to our team inbox", None,
             "look up the latest Python release online", "send_email"),
            ("what about Ukraine and text Mom a summary", "what happened in Iran today?",
             "Ukraine news today", "send_message"),
            ("text Mom the latest news in Iran", None, "latest news in Iran", "send_message"),
        ):
            with self.subTest(prompt=prompt):
                d = await route(prompt, last_user=prior)
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertEqual(d.tool_argument_bindings["web_search"], {"query": query})
                self.assertEqual(d.required_tool_groups[0], frozenset({"web_search"}))
                self.assertIn(frozenset({effect}), d.required_tool_groups)

    async def test_private_source_stays_private_when_delivery_is_explicit(self) -> None:
        d = await route("look up emails from Mom online and email them to our team inbox")
        self.assertNotIn("web_search", d.tool_argument_bindings)
        self.assertTrue({"web_search", "web_fetch", "http_request"} <= d.forbidden_tools)
        d = await route("and today?", last_user="what's happening in our Apollo roadmap?")
        self.assertNotIn("web_search", {name for name, _ in d.direct_calls})
        self.assertTrue({"web_search", "web_fetch", "http_request", "run_shell"}
                        <= d.forbidden_tools)
        self.assertNotIn("web_search", d.tool_subset or ())

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
