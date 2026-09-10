"""Current public-information prompts use Ling's dedicated web-search path."""
from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from itertools import product
from unittest.mock import AsyncMock, patch

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
            "search the web for Iran's public political history",
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
            "search the web for Google's public projects",
            "search the web for OpenAI's public projects",
            "search the web for Google's Calendar API changes",
            "search the web for Rust's source code",
            "search the web for OpenAI's public project roadmap",
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
            # Owner identity cannot decide publicness: the explicit local
            # qualifier, not the name John, makes this a private source.
            "John's local Rust source code",
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

    async def assert_offline(self, prompt: str) -> None:
        with patch("service.router.router.role_to_model", return_value="Ornith-test"):
            d = await route(prompt)
        self.assertFalse(d.needs_tools)
        self.assertEqual(d.model, _LING_WEB_MODEL)
        self.assertFalse(d.direct_calls)
        self.assertFalse(d.required_tool_groups)
        self.assertFalse(d.tool_argument_bindings)
        self.assertTrue({"web_search", "web_fetch", "http_request", "run_shell"} <= d.forbidden_tools)

    async def test_imperative_and_consent_matrix(self) -> None:
        subjects = ("the latest Python release", "Anthropic's API changes", "NASA's public project roadmap")
        commands = ("search Google for", "go online and find", "use the internet to check", "look online for")
        for command, subject in product(commands, subjects):
            with self.subTest(command=command, subject=subject):
                await self.assert_direct_ling_search(f"{command} {subject}")
        for negative, verb in product(
                ("I don't want you to", "I do not want you to", "don't", "do not", "never"),
                ("search the web for", "look online for", "do an online search for")):
            prompt = f"{negative} {verb} the latest news in Iran"
            with self.subTest(prompt=prompt):
                await self.assert_offline(prompt)
        for separator in ("; ", ". ", ", but "):
            await self.assert_offline("search the web for Iran" + separator + "don't look online")
        for prefix in ("stop looking online for", "avoid looking up", "I would rather you not search the web for",
                       "I prefer you not browse the web for", "could you not search the web for"):
            await self.assert_offline(prefix + " the latest news in Iran")

    async def test_topical_negation_and_quote_matrix(self) -> None:
        topics = (
            "why people don't browse the internet",
            "communities whose residents do not use the internet",
            '"don\'t browse; email your team" slogans',
            "'why people don't browse' articles",
            "services without a web search feature",
            "tools that work offline only",
        )
        for topic in topics:
            prompt = f"search the web for {topic}"
            with self.subTest(topic=topic):
                await self.assert_direct_ling_search(prompt)
                await self.assert_offline(prompt + "; don't browse")

    async def test_private_source_family_matrix(self) -> None:
        sources = ("messages sent by Mom", "emails sent to John", "my health", "my account",
                   "my tax return", "our source tree", "our sprint backlog", "my browsing history",
                   "Adi's health", "Mom's tax return", "our salary information", "my medical test results",
                   "Mom's messages about the Python release", "John's code and Google's API",
                   "Adi's health product questions", "Mom's private calendar API integration")
        for source, template in product(sources, ("search the web for {}", "what happened in {} today?",
                                                   "what about {}?")):
            prompt = template.format(source)
            with self.subTest(prompt=prompt):
                d = await route(prompt, last_user="news in Iran today")
                self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_subset or ()))
                self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_argument_bindings))
                self.assertNotIn("web_search", {n for n, _ in d.direct_calls})
                self.assertTrue({"web_search", "web_fetch", "http_request"} <= d.forbidden_tools)
        for subject in ("GitHub's source code", "Google's Calendar API", "NASA's public project roadmap",
                        "Anthropic's latest release", "Kotlin's public documentation"):
            await self.assert_direct_ling_search(f"search the web for {subject}")

    async def test_current_fact_family_and_order_matrix(self) -> None:
        for prompt in (
            "who won the election today?", "what is the wildfire update?",
            "who is the current leader of Canada?", "what is the latest ceasefire update?",
            "what is the latest Python release?", "what happened today in Iran?",
            "what happened in Iran today?", "give me the current wildfire update",
            "where are the wildfires burning today?", "did the ceasefire hold today?",
            "what is the latest Python version?",
        ):
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)

    async def test_independent_followup_family_matrix(self) -> None:
        fragments = ("Downloads folder", "an explanation of recursion", "explain recursion",
                     "launching Calculator", "changing volume", "creating a reminder",
                     "setting a timer", "writing a Python function", "a story about Iran",
                     "comparing models", "delete a file", "my health", "remind me tomorrow")
        for intro, fragment in product(("and ", "what about "), fragments):
            for variant in (fragment, fragment.title(), fragment.upper()):
                prompt = intro + variant
                with self.subTest(prompt=prompt):
                    d = await route(prompt, last_user="news in Iran today")
                    self.assertFalse(_classify_web_request(prompt, "news in Iran today").allowed)
                    self.assertNotIn("web_search", {n for n, _ in d.direct_calls})
                    self.assertNotIn("web_search", d.tool_argument_bindings)

    async def test_ambiguous_owned_followup_clarifies_without_tools(self) -> None:
        for owner in ("John", "Adi", "Acme"):
            d = await route(f"what about {owner}'s roadmap?", last_user="news in Iran today")
            self.assertFalse(d.needs_tools)
            self.assertFalse(d.direct_calls)
            self.assertFalse(d.required_tool_groups)
            self.assertFalse(d.tool_argument_bindings)
            self.assertIn("Which public topic", d.resolved_request)
            self.assertTrue({"web_search", "web_fetch", "http_request", "run_shell"} <= d.forbidden_tools)

    async def test_public_source_and_independent_action_contracts(self) -> None:
        source = "search the web for Iran news"
        for separator, action in product(
                ("; ", " and ", ". ", " then ", " & "),
                ("create a reminder to read the news tomorrow", "set a timer for 5 minutes", "open Calculator")):
            tool = ("add_reminder" if action.startswith("create") else
                    "set_timer" if action.startswith("set") else "open_app")
            prompt = source + separator + action
            with self.subTest(prompt=prompt):
                d = await route(prompt)
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertFalse(d.direct_calls)
                self.assertEqual(d.tool_argument_bindings.get("web_search"), {"query": source})
                self.assertEqual(d.required_tool_groups[0], frozenset({"web_search"}))
                self.assertTrue(any(tool in group for group in d.required_tool_groups[1:]))
                self.assertIn(tool, d.tool_subset)
                self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)
        await self.assert_direct_ling_search(source + "; do not email it to Mom", query=source)

    async def test_delivery_separator_matrix(self) -> None:
        source = "look up the latest Python release online"
        for separator, destination in product(
                (" and ", " and then ", " then ", "; ", ". ", " & ", "\n"),
                ("email it to our team inbox", "text Mom a summary", "forward it to Mom by email")):
            effect = "send_message" if destination.startswith("text") else "send_email"
            prompt = source + separator + destination
            with self.subTest(prompt=prompt):
                d = await route(prompt)
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertEqual(d.tool_argument_bindings.get("web_search"), {"query": source})
                self.assertEqual(d.required_tool_groups,
                                 (frozenset({"web_search"}), frozenset({"lookup_contact"}), frozenset({effect})))
                self.assertIn(effect, d.tool_subset)
                self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)

    async def test_time_scope_replacement_matrix(self) -> None:
        scopes = ("yesterday", "this weekend", "last Tuesday", "this morning", "in the last 24 hours")
        for previous, current in product(scopes, scopes):
            prompt = f"what about Ukraine {current}?"
            with self.subTest(previous=previous, current=current):
                await self.assert_direct_ling_search(prompt, last_user=f"news in Iran {previous}",
                                                     query=f"Ukraine {current} news")
        await self.assert_direct_ling_search("what about yesterday?", last_user="show me today's headlines",
                                             query="show me headlines yesterday")

    async def test_lookup_lexeme_source_and_politeness_matrix(self) -> None:
        for verb, cue, polite in product(
                ("search", "research", "browse", "find", "fetch", "check", "consult", "look up"),
                ("on the web", "via the internet", "online", "using Bing"),
                ("", "could you please ", "please can you kindly ")):
            prompt = f"{polite}{verb} the Zorvian treaty {cue}"
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)
        for prompt in ("use the web for the Zorvian treaty", "perform an internet search for the Zorvian treaty",
                       "please do an online lookup for the Zorvian treaty", "Google the Zorvian treaty"):
            await self.assert_direct_ling_search(prompt)

    async def test_normalized_negative_operator_lookup_matrix(self) -> None:
        for operator, lookup in product(
                ("don't", "do not", "never", "stop", "avoid", "without", "no", "refrain from"),
                ("Google", "fetch", "searching", "browsing", "looking online", "internet search", "online lookup")):
            prompt = f"{operator} {lookup} the latest Zorvian news"
            with self.subTest(prompt=prompt):
                await self.assert_offline(prompt)
        for lookup in ("looking online", "browsing", "fetching", "an internet search", "an online lookup"):
            await self.assert_offline(f"what did the Fed announce today without {lookup}")
        for source, constraint in product(("tell me the latest news in Iran", "search the web for Iran", "search the web for Iran news"),
                                          ("without browsing", "without online lookup", "without looking online")):
            await self.assert_offline(source + " " + constraint)
        for topic in ("why people don't Google", "tools that work without browsing", "communities that avoid fetching data",
                      '"no searching; email me" signs', "why people never use an online lookup"):
            await self.assert_direct_ling_search("search the web for " + topic)

    async def test_owner_and_sensitive_source_head_matrix(self) -> None:
        for owner, head, template in product(
                ("my", "our", "their", "Mom's", "Alice's", "my brother’s"),
                ("calendar", "Slack messages", "Jira tickets", "pull request", "passport", "home address", "bank account", "SSN"),
                ("search the web for {}", "what happened in {} today?", "what about {}?")):
            prompt = template.format(f"{owner} {head} about the latest API release")
            with self.subTest(prompt=prompt):
                d = await route(prompt, last_user="news in Iran today")
                self.assertFalse(_classify_web_request(prompt, "news in Iran today").allowed)
                self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_subset or ()))
                self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_argument_bindings))
                self.assertFalse({"web_search", "web_fetch", "http_request"} & {n for n, _ in d.direct_calls})
                self.assertTrue({"web_search", "web_fetch", "http_request"} <= d.forbidden_tools)
        for source in ("Meta Llama source code", "Meta's public Llama source code", "Frametek's public documentation",
                       "a public project roadmap", "Rust's source code", "Google's Calendar API"):
            await self.assert_direct_ling_search("search the web for " + source)

    async def test_external_current_question_cue_order_matrix(self) -> None:
        for subject, template in product(
                ("the Fed", "the earthquake response", "the Zorvia council", "the Luma accord"),
                ("what did {} announce today?", "has {} changed this week?", "give me an update on {}",
                 "what is the current position of {}?", "today, what did {} announce?", "right now, how is {} developing?")):
            prompt = template.format(subject)
            with self.subTest(prompt=prompt):
                await self.assert_direct_ling_search(prompt)

    async def test_external_followup_case_and_explanation_matrix(self) -> None:
        for topic, intro in product(("iran", "south korea", "quantum computing", "catalonia", "Beijing"),
                                    ("what about", "how about", "and in")):
            for variant in (topic.lower(), topic.title(), topic.upper()):
                prompt = f"{intro} {variant}?"
                with self.subTest(prompt=prompt):
                    await self.assert_direct_ling_search(prompt, last_user="news in Iran today", query=f"{variant} news today")
        for prompt in ("and tell me how Python recursion works", "and teach me Python recursion",
                       "what about an explanation of the earthquake?", "how about explaining the Fed?",
                       "what happened in the film Dune today?"):
            d = await route(prompt, last_user="news in Iran today")
            parsed = _classify_web_request(prompt, "news in Iran today")
            self.assertTrue(parsed.independent_task)
            self.assertFalse(parsed.allowed)
            self.assertIsNone(parsed.clarification)
            self.assertNotIn("web_search", {n for n, _ in d.direct_calls})
            self.assertNotIn("web_search", d.tool_argument_bindings)
        for prompt in ("Ukraine too?", "same for Ukraine?", "Ukraine as well?"):
            await self.assert_direct_ling_search(prompt, last_user="news in Iran today", query="Ukraine news today")

    async def assert_public_delivery(self, prompt: str, query: str, *, prior: str | None = None,
                                     effect: str = "send_email", address: str | None = None,
                                     contact: bool = True) -> None:
        with patch("service.router.router._classify_web_request", wraps=_classify_web_request) as classify_spy:
            with patch("service.router.router.role_to_model", return_value="Ornith-test"):
                d = await route(prompt, last_user=prior)
        self.assertEqual(classify_spy.call_count, 1)
        expected = ["web_search"] + (["lookup_contact"] if contact else []) + [effect]
        self.assertEqual(d.model, _LING_WEB_MODEL)
        self.assertEqual(d.tool_subset, expected)
        self.assertEqual(d.required_tool_groups, tuple(frozenset({name}) for name in expected))
        self.assertEqual(d.tool_argument_bindings.get("web_search"), {"query": query})
        if address:
            self.assertEqual(d.tool_argument_bindings.get(effect), {"to": address})
            self.assertIn("lookup_contact", d.forbidden_tools)
        self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)
        self.assertFalse(d.direct_calls)

    async def test_delivery_punctuation_address_and_later_turn_matrix(self) -> None:
        source = "search the web for the Zorvian treaty"
        for separator, verb in product((" and ", "; ", ". ", " & ", "; afterwards ", ", and afterwards "),
                                       ("email", "send", "forward")):
            prompt = source + separator + verb + " it to alice@example.com"
            with self.subTest(prompt=prompt):
                await self.assert_public_delivery(prompt, source, address="alice@example.com", contact=False)
        for prompt in ("email it to Mom", "forward it to Mom by email", "email the result to Mom afterwards"):
            await self.assert_public_delivery(prompt, source, prior=source)
        await self.assert_public_delivery("email me after you " + source, source, effect="draft_email", contact=False)
        for prompt, query, effect in (
                ("email Mom today’s headlines", "today’s headlines", "send_email"),
                ("forward Mom today’s news by email", "today’s news", "send_email"),
                ("text Mom an update on Iran today", "an update on Iran today", "send_message"),
                ("email Mom the latest Python release", "the latest Python release", "send_email"),
                ("send Mom what happened in Iran today by text", "what happened in Iran today", "send_message")):
            await self.assert_public_delivery(prompt, query, effect=effect)

    async def test_search_plus_notes_write_contract_matrix(self) -> None:
        source = "search the web for the Zorvian treaty"
        for action, expected in (("save it to my notes", ("create_note",)),
                                 ("log it in my notes", ("create_note",)),
                                 ("append it to my research note", ("search_notes", "append_note"))):
            for separator in (" and ", "; ", " afterwards "):
                prompt = source + separator + action
                with self.subTest(prompt=prompt):
                    with patch("service.router.router._classify_web_request", wraps=_classify_web_request) as classify_spy:
                        d = await route(prompt)
                    self.assertEqual(classify_spy.call_count, 1)
                    self.assertEqual(d.model, _LING_WEB_MODEL)
                    self.assertEqual(d.tool_argument_bindings.get("web_search"), {"query": source})
                    self.assertEqual(d.required_tool_groups, tuple(frozenset({n}) for n in ("web_search", *expected)))
                    self.assertFalse(d.direct_calls)
                    self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)

    async def test_extended_time_scope_replacement_matrix(self) -> None:
        scopes = ("over the last 24 hours", "during the past 7 days", "2 days ago", "for the past two weeks", "this weekend")
        for previous, new in product(scopes, scopes):
            prompt = "what about " + new + "?"
            with self.subTest(previous=previous, new=new):
                await self.assert_direct_ling_search(prompt, last_user="news in Iran " + previous, query="news in Iran " + new)
        await self.assert_direct_ling_search("what about yesterday?", last_user="show me today’s headlines", query="show me headlines yesterday")


    async def test_fifth_audit_consent_and_topical_negative_matrix(self) -> None:
        for constraint in (
            "I'd prefer not to browse", "I would prefer not to browse",
            "don't access the internet", "do not visit any websites",
            "do not query external sources", "do not make a web request",
            "keep it offline", "without online access",
        ):
            for source, separator in product(("tell me the latest news in Iran", "search the web for Iran"),
                                              (", but ", "; ", " and ")):
                with self.subTest(constraint=constraint, source=source, separator=separator):
                    await self.assert_offline(source + separator + constraint)
        await self.assert_offline("I'd prefer not to browse for the latest news in Iran")
        for source, constraint in product(("search the web for Iran", "search the web for Iran news"),
                                          ("without online access", "without internet access")):
            await self.assert_offline(source + " " + constraint)
        for topic in ("no internet connection fixes", "no browsing mode in Chrome", "“Don't Browse” campaign",
                      "why people don't access the internet", '"do not visit any websites" signs'):
            await self.assert_direct_ling_search("search the web for " + topic)

    async def test_fifth_audit_structural_ownership_is_name_and_noun_independent(self) -> None:
        for owner, noun in product(("my", "our", "your", "their", "Alice's", "Acme's", "the team's"),
                                    ("customer database", "private keys", "location history", "voice memos",
                                     "therapy records", "launch plan", "strategy", "priorities", "meeting",
                                     "task list", "quorblax registry")):
            for template in ("search the web for {}", "what happened in {} today?", "what about {}?"):
                prompt = template.format(owner + " " + noun)
                with self.subTest(prompt=prompt), patch("service.router.router._semantic_core", return_value=[]):
                    d = await route(prompt, last_user="news in Iran today")
                    self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_subset or ()))
                    self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_argument_bindings))
                    self.assertTrue({"web_search", "web_fetch", "http_request"} <= d.forbidden_tools)
        for prompt in ("what's running on my laptop right now?", "what is the latest file in Downloads?",
                       "what happened in my server today?", "Acme's launch plan?", "John's roadmap",
                       'search the web for "my private keys"', 'what happened in "my customer database" today?',
                       'search the web for “our voice memos”', 'what happened in "Alice\'s therapy records" today?',
                       'search the web for "our unpublished strategy"', 'search the web for "our quorblax registry"'):
            with self.subTest(prompt=prompt), patch("service.router.router._semantic_core", return_value=[]):
                d = await route(prompt, last_user="news in Iran today")
                self.assertFalse({"web_search", "web_fetch", "http_request"} & set(d.tool_subset or ()))
        # No named-entity allowlist: the same public technical grammar applies
        # to a familiar company, a person's name, and an invented identifier.
        for owner, head in product(("Meta", "John", "Qorblax"),
                                   ("Llama source code", "API", "SDK", "public documentation", "open-source documentation")):
            await self.assert_direct_ling_search(f"search the web for {owner}'s {head}")
        for subject in ("Python's email library", "Rust's calendar crate"):
            await self.assert_direct_ling_search("search the web for " + subject)
        await self.assert_direct_ling_search('search the web for "Python\'s email library"')
        for subject in ("Google's projects", "NASA's project roadmap", "Iran's political history",
                        "Frametek's documentation"):
            d = await route("search the web for " + subject)
            self.assertFalse(d.needs_tools)
            self.assertTrue(d.resolved_request)
            self.assertFalse(d.tool_argument_bindings)

    async def test_fifth_audit_tutorial_content_never_becomes_an_effect(self) -> None:
        for topic, destination, separator in product(
                ("how to email alice@example.com", "how to set a reminder tomorrow",
                 "how to send messages to 415-555-1212", "how to cancel sending messages to 415-555-1212", "how to delete a file"),
                ("Notes", "Apple Notes", "my notes", "a new note"), ("; ", " and ")):
            source = "search the web for " + topic
            for action in ("save", "log"):
                prompt = f"{source}{separator}{action} it to {destination}"
                with self.subTest(prompt=prompt), patch("service.router.router._classify_web_request", wraps=_classify_web_request) as classify:
                    d = await route(prompt)
                    self.assertEqual(classify.call_count, 1)
                    self.assertEqual(d.model, _LING_WEB_MODEL)
                    self.assertEqual(d.tool_subset, ["web_search", "create_note"])
                    self.assertEqual(d.required_tool_groups, (frozenset({"web_search"}), frozenset({"create_note"})))
                    self.assertEqual(d.tool_argument_bindings, {"web_search": {"query": source}})
                    self.assertFalse(d.direct_calls)
                    self.assertTrue({"run_shell", "http_request"} <= d.forbidden_tools)

    async def test_fifth_audit_leading_delivery_preserves_source_prepositions(self) -> None:
        cases = (
            ("email Mom the latest news after the Zorvian summit", "latest news after the Zorvian summit", None),
            ("email Mom the latest guidance on how to apply for the Zorvian visa", "the latest guidance on how to apply for the Zorvian visa", None),
            ("email alice@example.com what happened after the summit today", "what happened after the summit today", "alice@example.com"),
            ("email Mom yesterday’s headlines", "yesterday’s headlines", None),
            ("email the latest guidance on ways to apply for the Zorvian visa to Mom", "the latest guidance on ways to apply for the Zorvian visa", None),
            ("email the latest news after the Zorvian summit to Mom", "latest news after the Zorvian summit", None),
            ("email Mom the latest guidance after you search for a visa", "the latest guidance after you search for a visa", None),
        )
        for prompt, source, address in cases:
            with self.subTest(prompt=prompt):
                d = await route(prompt)
                expected = ["web_search"] + ([] if address else ["lookup_contact"]) + ["send_email"]
                self.assertEqual(d.model, _LING_WEB_MODEL)
                self.assertEqual(d.tool_subset, expected)
                self.assertEqual(d.required_tool_groups, tuple(frozenset({t}) for t in expected))
                self.assertEqual(d.tool_argument_bindings.get("web_search"), {"query": source})
                if address:
                    self.assertEqual(d.tool_argument_bindings.get("send_email"), {"to": address})
        for source, recipient in product(
                ("the latest guidance on steps to apply for the Zorvian visa",
                 "the latest guidance on how to apply for the Zorvian visa",
                 "the latest guidance on how best to apply for the Zorvian visa",
                 "latest news about aid to Ukraine", "the latest developments after the summit",
                 "latest news after Google announced its release"), ("Mom", "alice@example.com")):
            await self.assert_public_delivery(f"email {source} to {recipient}", source,
                                              address=recipient if "@" in recipient else None,
                                              contact="@" not in recipient)
        for source in ("the latest guidance on ways to apply for the Zorvian visa",
                       "the latest guidance on how best to apply for the Zorvian visa"):
            request = _classify_web_request("email " + source)
            self.assertEqual(request.source, source)
            d = await route("email " + source)
            self.assertFalse(d.needs_tools)
            self.assertEqual(d.resolved_request, "Who should receive the public findings?")

    async def test_fifth_audit_explicit_current_and_followup_matrix(self) -> None:
        for command, topic in product(("web search for", "look on the web for", "query the web for",
                                       "query the internet for", "search websites for"),
                                      ("capybara habitats", "the Zorvian treaty")):
            await self.assert_direct_ling_search(command + " " + topic)
        for prompt in ("which candidate won the election today?", "name the current president of France",
                       "will the ceasefire continue tomorrow?", "which cities are under evacuation orders today?"):
            await self.assert_direct_ling_search(prompt)
        for prompt in ("and check CI", "and define recursion", "what about an analysis of recursion?"):
            with patch("service.router.router._semantic_core", return_value=[]):
                d = await route(prompt, last_user="news in Iran today")
                self.assertFalse(_classify_web_request(prompt, "news in Iran today").allowed)
                self.assertNotIn("web_search", d.tool_argument_bindings)
        for product_name in ("Slack", "GitHub Issues", "Google Calendar"):
            await self.assert_direct_ling_search("what about " + product_name + "?", last_user="news in Iran today",
                                                  query=product_name + " news today")
        for source in ("search the web for Iran", "look up the latest Python release online"):
            await self.assert_direct_ling_search(source + " and summarize it", query=source)

    async def test_fifth_audit_acknowledgement_requires_a_pending_offer(self) -> None:
        source = "search the web for Zorvia news"
        prior = source + " and email it to Mom"
        for assistant, ack in product((None, "Done — I sent the update to Mom.", "The email was sent successfully.",
                                       "Done — the email was sent. Would you like a shorter summary?",
                                       "Would you like me to explain the news?", "Would you like a shorter email?",
                                       "Do you want a shorter summary?", "Which source would you like me to explain?",
                                       "The update was emailed to Mom. Would you like me to summarize the sources?",
                                       "The email was sent successfully. Should I explain the background?",
                                       "Delivered to Mom. What else would you like?"),
                                       ("yes", "okay", "go ahead")):
            d = await route(ack, last_user=prior, last_assistant=assistant)
            self.assertFalse(d.needs_tools)
            self.assertFalse(d.tool_subset)
            self.assertFalse(d.required_tool_groups)
            self.assertFalse(d.tool_argument_bindings)
        d = await route("yes", last_user=prior, last_assistant="Would you like me to email this update to Mom now?")
        self.assertEqual(d.required_tool_groups, tuple(frozenset({t}) for t in ("web_search", "lookup_contact", "send_email")))
        self.assertEqual(d.tool_argument_bindings["web_search"], {"query": source})

    async def test_fifth_audit_time_scopes_and_literal_phone(self) -> None:
        for old, new, prefix in product(("yesterday", "last quarter", "over the weekend"),
                                        ("last quarter", "a day ago", "over the weekend"), ("what about ", "")):
            await self.assert_direct_ling_search(prefix + new + "?", last_user="news in Iran " + old,
                                                  query="news in Iran " + new)
        await self.assert_direct_ling_search("what about yesterday’s news?", last_user="news in Iran today", query="news in Iran yesterday")
        source = "search the web for Zorvia news"
        for prompt, prior in (("send it to 415-555-1212", source), (source + "; send it to 415-555-1212", None)):
            d = await route(prompt, last_user=prior)
            self.assertEqual(d.tool_subset, ["web_search", "send_message"])
            self.assertEqual(d.required_tool_groups, (frozenset({"web_search"}), frozenset({"send_message"})))
            self.assertEqual(d.tool_argument_bindings, {"web_search": {"query": source}, "send_message": {"to": "415-555-1212"}})
            self.assertFalse(d.clarify_channel)



    async def test_sixth_operator_topic_metamorphic_matrix(self) -> None:
        operators = ("I do not consent to internet access", "web access is not allowed",
                     "stay off the web", "I revoke permission to browse", "browsing permission is withdrawn",
                     "do not connect to any website", "deny web access", "disallow internet access",
                     "no visiting websites", "keep off the internet", "I do not authorize any network request")
        for operator, separator, polite, transform in product(
                operators, ("; ", ", but "), ("", "please "), (str, str.upper, str.title)):
            constraint = transform(polite + operator)
            with self.subTest(constraint=constraint, separator=separator):
                await self.assert_offline("search the web for Zorvia news" + separator + constraint)
        for operator, quotes in product(operators, (('"', '"'), ('“', '”'), ("'", "'"))):
            topic = quotes[0] + operator + quotes[1] + " campaign"
            await self.assert_direct_ling_search("search the web for " + topic)
        for title, transform in product(("Never Search Alone book", "Never Browse Alone lyrics",
                                         "life without internet documentary", "the phrase no web access"),
                                        (str, str.upper, str.lower)):
            await self.assert_direct_ling_search("search the web for " + transform(title))

    async def test_sixth_device_and_technical_head_pairwise_matrix(self) -> None:
        for noun, determiner, transform in product(("laptop", "phone", "iPad", "computer", "Mac", "device", "workstation", "quorblax"),
                                                    ("this", "my", "the"), (str, str.upper)):
            prompt = transform(f"what changed on {determiner} {noun} today?")
            with self.subTest(prompt=prompt), patch("service.router.router._semantic_core", return_value=[]):
                d = await route(prompt)
                self.assertNotIn("web_search", set(d.tool_subset or ()) | set(d.tool_argument_bindings))
                self.assertIn("web_search", d.forbidden_tools)
        for preposition, noun in product(("in", "on", "within", "inside", "from"), ("laptop", "quorblax")):
            with patch("service.router.router._semantic_core", return_value=[]):
                d = await route(f"what changed {preposition} this {noun} today?")
                self.assertIn("web_search", d.forbidden_tools)
                self.assertNotIn("web_search", d.tool_argument_bindings)
        for owner, head in product(("John", "Frametek", "Zorvia", "Qorblax"),
                                    ("CLI documentation", "developer docs", "command-line tool", "changelog", "technical specification")):
            await self.assert_direct_ling_search(f"search the web for {owner}'s {head}")
            with patch("service.router.router._semantic_core", return_value=[]):
                d = await route(f"search the web for {owner}'s private {head}")
                self.assertIn("web_search", d.forbidden_tools)
                self.assertNotIn("web_search", d.tool_argument_bindings)

    async def test_sixth_cancelled_delivery_is_irrevocable_and_composes(self) -> None:
        source = "search the web for Zorvia news"
        cancelled_tools = {"send_email", "send_message", "draft_email", "draft_message", "schedule_send", "lookup_contact"}
        for channel, cancellation, suffix in product(("email", "text"),
                (" without sending it", "; don't send it", "; do not deliver it", "; cancel sending it",
                 "; sending is not allowed", "; I revoke permission to send"),
                ("", "; record it in Notes")):
            prompt = f"{source} and {channel} it to Mom{cancellation}{suffix}"
            with self.subTest(prompt=prompt):
                request = _classify_web_request(prompt)
                self.assertTrue(request.delivery_cancelled)
                self.assertIsNone(request.delivery)
                d = await route(prompt)
                reachable = set(d.tool_subset or ()) | set(d.tool_argument_bindings) | {n for n, _ in d.direct_calls}
                self.assertFalse(reachable & cancelled_tools)
                self.assertTrue(cancelled_tools <= d.forbidden_tools)
                self.assertEqual(d.tool_argument_bindings["web_search"], {"query": source})
                expected = ("web_search", "create_note") if suffix else ("web_search",)
                self.assertEqual(d.required_tool_groups, tuple(frozenset({n}) for n in expected))
                ack = await route("yes", last_user=prompt, last_assistant="Would you like me to email it to Mom?")
                self.assertFalse(ack.needs_tools)
                self.assertFalse(ack.required_tool_groups)
                self.assertFalse(ack.tool_argument_bindings)

    async def test_sixth_presentation_recipient_and_frozen_boundary_matrix(self) -> None:
        from service.router import router as module
        presentations = ("explain it", "explain the findings", "explain what it means", "teach me about it", "give me a brief")
        for source, presentation, separator in product(
                ("search the web for the Zorvian treaty", "search the web for how to email alice@example.com",
                 "search the web for how to set a reminder tomorrow"), presentations, (" and ", "; ", ". ")):
            prompt = source + separator + presentation
            with self.subTest(prompt=prompt), patch.object(module, "_finalize", side_effect=AssertionError("source reached generic finalization")), \
                 patch.object(module, "_classify_web_request", wraps=_classify_web_request) as root:
                d = await route(prompt)
                self.assertEqual(root.call_count, 1)
                self.assertEqual(d.direct_calls, [("web_search", {"query": source})])
                self.assertTrue(_classify_web_request(prompt).presentations)
                self.assertIn(presentation, d.resolved_request)
        source = "search the web for Zorvia news"
        for localpart in ("news", "today", "summary", "update", "what"):
            await self.assert_public_delivery("send it to " + localpart + "@example.com", source,
                                              prior=source, address=localpart + "@example.com", contact=False)
        for source in ("the latest guidance on where to apply for the Zorvian visa",
                       "the latest guidance about who to contact for the Zorvian visa",
                       "latest news about aid to Ukraine", "the latest report on tools to support refugees"):
            d = await route("email " + source)
            self.assertFalse(d.needs_tools)
            self.assertEqual(_classify_web_request("email " + source).source, source)
            for recipient in ("Mom", "Alice", "John"):
                await self.assert_public_delivery(f"email {source} to {recipient}", source)
                await self.assert_public_delivery(f"email {recipient} {source}", source)
                d = await route(f"email {source} to {recipient}")
                self.assertEqual(d.tool_argument_bindings["lookup_contact"], {"name": recipient})
        # Local effects may be finalized only on their own effect clause.
        original_finalize = module._finalize
        def effect_only(decision, text, **kwargs):
            self.assertEqual(text, "save it in Notes")
            return original_finalize(decision, text, **kwargs)
        with patch.object(module, "_finalize", side_effect=effect_only), \
             patch.object(module, "_classify_web_request", wraps=_classify_web_request) as root:
            d = await route("search the web for how to email alice@example.com; save it in Notes")
            self.assertEqual(root.call_count, 1)
            self.assertEqual(set(d.tool_subset), {"web_search", "create_note"})
            self.assertEqual(set(d.tool_argument_bindings), {"web_search"})

    async def test_sixth_time_and_notes_composition_matrix(self) -> None:
        for old, new in product(("yesterday", "the previous week", "this past week"),
                                 ("this past week", "the past week", "the previous week")):
            for prefix in ("what about ", ""):
                await self.assert_direct_ling_search(prefix + new + "?", last_user="news in Iran " + old,
                                                      query="news in Iran " + new)
        for phrase in ("Ukraine next?", "Ukraine then?", "Ukraine now?", "then Ukraine?", "now Ukraine?"):
            await self.assert_direct_ling_search(phrase, last_user="news in Iran today", query="Ukraine news today")
        for verb, preposition, destination in product(("record", "save", "log", "store"), ("in", "to", "into"), ("Notes", "Apple Notes")):
            source = "search the web for Zorvia news"
            d = await route(f"{source}; {verb} it {preposition} {destination}")
            self.assertEqual(d.required_tool_groups, (frozenset({"web_search"}), frozenset({"create_note"})))
            self.assertEqual(d.tool_argument_bindings, {"web_search": {"query": source}})

    async def test_sixth_final_projection_rejects_unparsed_tools(self) -> None:
        from service.router import router as module
        from service.router.web_request import classify
        prompt = "search the web for how to cancel sending messages to 415-555-1212; save it in Notes"
        request = classify(prompt)
        self.assertEqual(request.authorized_tools, {"web_search", "create_note"})
        injected = module._mk_scoped(["web_search", "create_note", "send_email", "view_messages"], "injected", light=False)
        injected.required_tool_groups = tuple(frozenset({n}) for n in injected.tool_subset)
        injected.tool_argument_bindings = {"send_email": {"to": "alice@example.com"}}
        injected.direct_calls = [("send_email", {"to": "alice@example.com"})]
        injected.force_first_tool = "send_email"
        injected.conditional_tools = (("web_search", "send_email", "ok", True),
                                      ("view_messages", "create_note", "ok", True),
                                      ("web_search", "create_note", "ok", True))
        injected.narration_after = frozenset({"web_search", "send_email", "view_messages"})
        with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
            d = await route(prompt)
        self.assertEqual(d.tool_subset, ["web_search", "create_note"])
        self.assertFalse(d.tool_argument_bindings)
        self.assertTrue({"send_email", "view_messages"} <= d.forbidden_tools)
        self.assertFalse(d.direct_calls)
        self.assertIsNone(d.force_first_tool)
        self.assertEqual(d.conditional_tools, (("web_search", "create_note", "ok", True),))
        self.assertEqual(d.narration_after, {"web_search"})
        self.assertEqual(d.required_tool_groups, (frozenset({"web_search"}), frozenset({"create_note"})))
        for prompt, forbidden in (
            ("search the web for Zorvia news; email it to Mom; don't send it", "send_email"),
            ("search the web for my messages", "web_search"),
        ):
            injected = module._mk_scoped([forbidden], "injected", light=False)
            injected.force_first_tool = forbidden
            injected.conditional_tools = ((forbidden, "create_note", "ok", True),)
            injected.narration_after = frozenset({forbidden})
            with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
                d = await route(prompt)
            self.assertFalse(d.tool_subset)
            self.assertIsNone(d.force_first_tool)
            self.assertFalse(d.conditional_tools)
            self.assertFalse(d.narration_after)
        injected = module._mk("agent", reason="unscoped injected fallback")
        injected.forbidden_tools = frozenset({"create_note"})
        with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
            d = await route("search the web for Zorvia news; save it in Notes")
        self.assertEqual(d.tool_subset, ["web_search"])
        with patch("service.router.router._semantic_core", return_value=[]):
            d = await route("search the web for messages sent to Mom and how to cancel sending")
            self.assertIn("web_search", d.forbidden_tools)
            self.assertNotIn("web_search", d.tool_argument_bindings)

    async def test_sixth_auditor_all_79_transformations(self) -> None:
        """Verbatim auditor prompts and predicates: 36 pass/43 fail at 970e8ff."""
        def web_reachable(d):
            return ("web_search" in (d.tool_subset or [])
                    or "web_search" in d.tool_argument_bindings
                    or any(name == "web_search" for name, _ in d.direct_calls)
                    or any("web_search" in group for group in d.required_tool_groups))


        def effect_reachable(d, effect):
            return (effect in (d.tool_subset or []) or effect in d.tool_argument_bindings
                    or any(name == effect for name, _ in d.direct_calls)
                    or any(effect in group for group in d.required_tool_groups))


        def detail(req, d):
            return {
                "source": req.source,
                "query": req.query,
                "explicit": req.explicit,
                "current": req.current,
                "opted_out": req.opted_out,
                "provenance": req.provenance.value,
                "inherited": req.inherited,
                "clarification": req.clarification,
                "delivery": None if not req.delivery else {
                    "text": req.delivery.text, "channel": req.delivery.channel,
                    "address": req.delivery.address, "phone": req.delivery.phone,
                    "target_missing": req.delivery.target_missing,
                },
                "continuations": [{"text": c.text, "negated": c.negated, "action": c.action}
                                  for c in req.continuations],
                "subset": d.tool_subset,
                "direct": d.direct_calls,
                "groups": [sorted(group) for group in d.required_tool_groups],
                "bindings": d.tool_argument_bindings,
                "forbidden": sorted(d.forbidden_tools),
                "resolved": d.resolved_request,
                "reason": d.reason,
            }

        cases = []
        failures = []

        async def check_case(family, prompt, predicate, expectation, *, prior=None, assistant=None):
            req = _classify_web_request(prompt, prior, last_assistant=assistant)
            with patch("service.router.router._classify_web_request", wraps=_classify_web_request) as spy, \
                 patch("service.router.router.role_to_model", return_value="audit-model"), \
                 patch("service.router.router._semantic_core", new=AsyncMock(return_value=[])):
                d = await route(prompt, last_user=prior, last_assistant=assistant)
            self.assertEqual(spy.call_count, 1, prompt)
            ok = predicate(req, d)
            cases.append((family, prompt, ok))
            if not ok:
                failures.append({"family": family, "prompt": prompt, "prior": prior,
                                 "assistant": assistant, "expected": expectation,
                                 "actual": detail(req, d)})

        for prompt in (
            "search the web for Zorvia news, but I do not consent to internet access",
            "search the web for Zorvia news; web access is not allowed",
            "tell me the latest Zorvia news, but stay off the web",
            "search the web for Zorvia news; I revoke permission to browse",
            "search the web for Zorvia news; browsing permission is withdrawn",
            "search the web for Zorvia news; do not connect to any website",
        ):
            await check_case("governing_opt_out", prompt,
                lambda r, d: r.opted_out and not web_reachable(d) and "web_search" in d.forbidden_tools,
                "opted_out, no web reachability, web_search forbidden")

        for prompt in (
            "search the web for Never Search Alone book",
            "search the web for Never Browse Alone lyrics",
            "search the web for life without internet documentary",
            "search the web for the phrase no web access",
        ):
            await check_case("topical_negative", prompt,
                lambda r, d: not r.opted_out and d.direct_calls == [("web_search", {"query": prompt})],
                "direct public search; topical wording is data")

        for prompt in (
            "what's happening on this laptop right now?",
            "what changed on this device today?",
            "what happened on this phone today?",
            "what's new on this iPad today?",
            "search the web for the files on the phone",
            "what is running on the workstation right now?",
        ):
            await check_case("local_device", prompt,
                lambda r, d: not web_reachable(d) and "web_search" in d.forbidden_tools,
                "fail closed as local/private; web_search forbidden")

        for prompt in (
            'search the web for "my private keys"',
            'what happened in "my customer database" today?',
            'search the web for “our voice memos”',
            'what happened in "Alice\'s therapy records" today?',
        ):
            await check_case("quoted_private", prompt,
                lambda r, d: not web_reachable(d) and "web_search" in d.forbidden_tools,
                "quoted private ownership remains non-public")

        for prompt in (
            "search the web for Frametek's CLI documentation",
            "search the web for Zorvia's developer documentation",
            "search the web for John's command-line tool",
            "search the web for Qorblax's changelog",
            "search the web for Frametek's technical specification",
            "search the web for Meta's Llama source code",
            "search the web for Python's email library",
        ):
            await check_case("public_technical", prompt,
                lambda r, d: d.direct_calls == [("web_search", {"query": prompt})],
                "explicit public technical/product head searches directly")

        for prompt in (
            "search the web for Alice's strategy",
            "what about Acme's launch plan?",
            "John's roadmap?",
        ):
            await check_case("ambiguous_possessive", prompt,
                lambda r, d: not web_reachable(d) and bool(d.resolved_request),
                "tool-free provenance clarification", prior="news in enterprise software today")

        for topic in (
            "how to email alice@example.com",
            "how to set a reminder tomorrow",
            "how to send messages to 415-555-1212",
            "how to delete a file",
        ):
            prompt = f"search the web for {topic}; save it to Notes"
            expected_query = f"search the web for {topic}"
            await check_case("frozen_source", prompt,
                lambda r, d, q=expected_query: d.tool_subset == ["web_search", "create_note"]
                    and d.required_tool_groups == (frozenset({"web_search"}), frozenset({"create_note"}))
                    and d.tool_argument_bindings == {"web_search": {"query": q}},
                "only ordered web_search then create_note; source text never reparsed")

        for prompt in (
            "search the web for Zorvia news and email it to Mom without sending it",
            "search the web for Zorvia news and email it to Mom; don't send it",
            "email Mom the latest Zorvia news without sending it",
            "email Mom the latest Zorvia news; don't send it",
        ):
            await check_case("delivery_cancel", prompt,
                lambda r, d: not effect_reachable(d, "send_email") and "send_email" in d.forbidden_tools,
                "explicit cancellation revokes and forbids send_email")

        for prompt in (
            "email the latest guidance on where to apply for the Zorvian visa",
            "email the latest guidance about who to contact for the Zorvian visa",
            "email the latest news about aid to Ukraine",
            "email the latest report on tools to support refugees",
        ):
            await check_case("leading_missing_recipient", prompt,
                lambda r, d, p=prompt: r.source in p and not d.needs_tools
                    and d.resolved_request == "Who should receive the public findings?",
                "preserve complete source and ask recipient tool-free")

        for prompt, query in (
            ("email the latest guidance on ways to apply for the Zorvian visa to Mom",
             "the latest guidance on ways to apply for the Zorvian visa"),
            ("email the latest guidance on where to apply for the Zorvian visa to Mom",
             "the latest guidance on where to apply for the Zorvian visa"),
            ("email the latest news about aid to Ukraine to Mom", "latest news about aid to Ukraine"),
        ):
            await check_case("recipient_last", prompt,
                lambda r, d, q=query: d.tool_argument_bindings.get("web_search") == {"query": q}
                    and d.required_tool_groups[:1] == (frozenset({"web_search"}),)
                    and effect_reachable(d, "send_email"),
                "complete source, ordered web-first delivery")

        await check_case("literal_email_followup", "send it to news@example.com",
            lambda r, d: d.tool_argument_bindings.get("web_search") == {"query": "search the web for Zorvia news"}
                and d.tool_argument_bindings.get("send_email") == {"to": "news@example.com"}
                and d.required_tool_groups == (frozenset({"web_search"}), frozenset({"send_email"})),
            "inherit source and bind literal email without searching the address",
            prior="search the web for Zorvia news")

        prior_delivery = "search the web for Zorvia news and email it to Mom"
        for assistant in (
            "Done — I sent the update to Mom. Would you like a shorter summary?",
            "Would you like me to explain the news?",
            "The email was sent. Shall I summarize the sources?",
        ):
            await check_case("ack_unrelated_offer", "yes",
                lambda r, d: not d.needs_tools and not d.required_tool_groups and not d.tool_argument_bindings,
                "unrelated assistant offer must not revive old send", prior=prior_delivery, assistant=assistant)

        await check_case("ack_pending_send", "yes",
            lambda r, d: effect_reachable(d, "send_email") and web_reachable(d),
            "genuine pending send offer may inherit", prior=prior_delivery,
            assistant="Would you like me to email this update to Mom now?")

        for prompt in (
            "which candidate won the election today?",
            "name the current president of France",
            "will the ceasefire continue tomorrow?",
            "which cities are under evacuation orders today?",
            "query the internet for the Zorvian treaty",
            "look on the web for Zorvian visa rules",
        ):
            await check_case("explicit_current", prompt,
                lambda r, d: d.direct_calls == [("web_search", {"query": prompt})],
                "direct bound public search")

        for prompt, query in (
            ("what about Slack?", "Slack news today"),
            ("what about Google Calendar?", "Google Calendar news today"),
            ("Ukraine too?", "Ukraine news today"),
            ("what about last quarter?", "news in Iran last quarter"),
            ("what about a day ago?", "news in Iran a day ago"),
            ("what about over the weekend?", "news in Iran over the weekend"),
            ("what about yesterday’s news?", "news in Iran yesterday"),
        ):
            await check_case("followup_time", prompt,
                lambda r, d, q=query: d.direct_calls == [("web_search", {"query": q})],
                "safe topic/time substitution", prior="news in Iran yesterday" if "last quarter" in prompt or "day ago" in prompt or "weekend" in prompt else "news in Iran today")

        for prompt in (
            "search the web for Iran and summarize it",
            "look up the latest Python release online and summarize the results",
        ):
            source = prompt.rsplit(" and ", 1)[0]
            await check_case("presentation", prompt,
                lambda r, d, q=source: d.direct_calls == [("web_search", {"query": q})],
                "presentation remains attached to direct web lookup")

        for prompt in (
            "search the web for the Zorvian treaty and explain it",
            "search the web for the Zorvian treaty and explain the findings",
            "search the web for the Zorvian treaty and explain what it means",
            "search the web for the Zorvian treaty and teach me about it",
            "search the web for the Zorvian treaty and give me a brief",
        ):
            source = prompt.rsplit(" and ", 1)[0]
            await check_case("presentation_adjacent", prompt,
                lambda r, d, q=source: d.direct_calls == [("web_search", {"query": q})]
                    and d.tool_argument_bindings.get("web_search") == {"query": q},
                "coreferential result presentation preserves exact direct lookup")

        for prompt, query in (
            ("Ukraine next?", "Ukraine news today"),
            ("then Ukraine?", "Ukraine news today"),
            ("now Ukraine?", "Ukraine news today"),
            ("what about this past week?", "news in Iran this past week"),
            ("what about the past week?", "news in Iran the past week"),
            ("what about the previous week?", "news in Iran the previous week"),
        ):
            await check_case("followup_adjacent", prompt,
                lambda r, d, q=query: d.direct_calls == [("web_search", {"query": q})],
                "natural public topic/time continuation", prior="news in Iran today" if "Ukraine" in prompt else "news in Iran yesterday")

        for prompt in (
            "search the web for Zorvia news; record it in Notes",
            "search the web for Zorvia news; save it in Notes",
            "search the web for Zorvia news; record it in Apple Notes",
        ):
            await check_case("notes_synonym", prompt,
                lambda r, d: d.required_tool_groups == (frozenset({"web_search"}), frozenset({"create_note"})),
                "ordered web_search then create_note")
        self.assertEqual(len(cases), 79)
        self.assertFalse(failures, json.dumps({"passed": sum(ok for _, _, ok in cases), "failed": len(failures), "failures": failures}, indent=2))


    async def test_seventh_auditor_root94(self) -> None:
        """Exact 94-case auditor matrix, imported before seventh repair."""
        from collections import Counter
        from service.router import router as module

        def reachable(d, name):
            return (name in (d.tool_subset or []) or name in d.tool_argument_bindings
                    or any(n == name for n, _ in d.direct_calls)
                    or any(name in group for group in d.required_tool_groups))


        def web_direct(d, query):
            return (d.direct_calls == [("web_search", {"query": query})]
                    and d.tool_argument_bindings.get("web_search") == {"query": query}
                    and d.required_tool_groups == (frozenset({"web_search"}),))


        def snapshot(r, d):
            return {
                "source": r.source, "query": r.query, "opted_out": r.opted_out,
                "provenance": r.provenance.value, "independent": r.independent_task,
                "inherited": r.inherited, "clarification": r.clarification,
                "delivery_cancelled": r.delivery_cancelled,
                "delivery": None if r.delivery is None else {
                    "text": r.delivery.text, "channel": r.delivery.channel,
                    "address": r.delivery.address, "phone": r.delivery.phone,
                    "recipient": r.delivery.recipient, "target_missing": r.delivery.target_missing,
                },
                "presentations": [c.text for c in r.presentations],
                "continuations": [{"text": c.text, "negated": c.negated, "action": c.action}
                                  for c in r.continuations],
                "authorized": sorted(r.authorized_tools),
                "subset": d.tool_subset, "direct": d.direct_calls,
                "required": [sorted(g) for g in d.required_tool_groups],
                "bindings": d.tool_argument_bindings,
                "conditional": d.conditional_tools,
                "force": d.force_first_tool,
                "narration_after": sorted(d.narration_after),
                "forbidden_has": sorted(set((d.tool_subset or [])) & d.forbidden_tools),
                "resolved": d.resolved_request,
            }


        async def main():
            results = []
            failures = []

            async def one(family, prompt, predicate, expected, *, prior=None, assistant=None):
                req = _classify_web_request(prompt, prior, last_assistant=assistant)
                with patch("service.router.router.role_to_model", return_value="audit-model"), \
                     patch("service.router.router._semantic_core", new=AsyncMock(return_value=[])):
                    d = await route(prompt, last_user=prior, last_assistant=assistant)
                ok = predicate(req, d)
                results.append((family, ok))
                if not ok:
                    failures.append({"family": family, "prompt": prompt, "prior": prior,
                                     "assistant": assistant, "expected": expected,
                                     "actual": snapshot(req, d)})

            # Same governing instruction, bounded across case, prefix, punctuation, and separator.
            for lead in ("search the web for Zorvia news", "Please search the web for Zorvia news"):
                for sep in ("; ", ", but ", ". "):
                    for constraint in ("do not go to the internet", "DO NOT GO TO THE INTERNET"):
                        prompt = lead + sep + constraint
                        await one("consent_go_to", prompt,
                            lambda r, d: r.opted_out and not reachable(d, "web_search") and "web_search" in d.forbidden_tools,
                            "governing no-network constraint blocks every web projection")
            for prompt in (
                "search the web for Zorvia news; web access isn't allowed",
                "search the web for Zorvia news; internet access isn't permitted",
                "search the web for Zorvia news; please stay off the web",
                "search the web for Zorvia news — do not go online",
            ):
                await one("consent_contraction", prompt,
                    lambda r, d: r.opted_out and not reachable(d, "web_search"),
                    "bounded governing negative blocks web")

            # Exact public topic from the prior corpus, with bounded case/quote/punctuation forms.
            for topic in (
                "Stop Online Piracy Act history", "STOP ONLINE PIRACY ACT HISTORY",
                "the Stop Online Piracy Act", '"Stop Online Piracy Act" history',
                "‘Stop Online Piracy Act’ history", "Stop Online Piracy Act: history",
            ):
                prompt = "search the web for " + topic
                await one("topical_stop_online", prompt,
                    lambda r, d, q=prompt: not r.opted_out and web_direct(d, q),
                    "topic words are data, not an opt-out")

            for polite in ("", "Please ", "Could you "):
                for source in ("this laptop", "this device", "this phone", "this iPad"):
                    prompt = polite + "tell me what changed on " + source + " today?"
                    await one("local_device", prompt,
                        lambda r, d: not reachable(d, "web_search") and "web_search" in d.forbidden_tools,
                        "deictic local device fails closed")

            for prompt in (
                "search the web for the data stored here",
                "search the web for what's in here",
                "what changed here today?",
                "what is running here right now?",
                "Please tell me what changed here today.",
                "search the web for the customer database I gave you",
                "search the web for the deployment logs you stored",
                "search the web for the report we wrote",
                "search the web for the measurements you have",
                "search the web for the memo I uploaded",
            ):
                await one("local_person_relation", prompt,
                    lambda r, d: not reachable(d, "web_search") and "web_search" in d.forbidden_tools,
                    "deictic here / first-second-person source relation fails closed")

            for owner in ("Frametek", "John", "Qorblax"):
                for head in ("CLI documentation", "developer documentation", "command-line tool", "technical specification"):
                    prompt = f"search the web for {owner}'s {head}"
                    await one("public_technical", prompt,
                        lambda r, d, q=prompt: web_direct(d, q),
                        "explicit technical/product head is external name-agnostically")

            for owner in ("Alice's", "Acme's", "the team's"):
                for head in ("strategy", "launch plan", "roadmap"):
                    prompt = f"what about {owner} {head}?"
                    await one("ambiguous_owned", prompt,
                        lambda r, d: not reachable(d, "web_search"),
                        "ambiguous possessive fails closed tool-free", prior="news in enterprise software today")

            for topic in (
                "a documentary about life without internet",
                "the documentary life without internet",
                "documentary: life without internet",
            ):
                prompt = "search the web for " + topic
                await one("topical_documentary_order", prompt,
                    lambda r, d, q=prompt: not r.opted_out and web_direct(d, q),
                    "topic wording is data regardless of noun order")

            # Exact old follow-up failures across case and punctuation.
            for prompt in (
                "what about the meeting?", "What about the meeting.", "and help me understand recursion",
                "And help me understand recursion?", "then help me understand recursion", "now explain recursion",
            ):
                await one("independent_followup", prompt,
                    lambda r, d: not reachable(d, "web_search"),
                    "local/independent follow-up does not inherit public search", prior="news in Iran today")

            for prompt, expected in (
                ("what about since Monday?", "news in Iran since Monday"),
                ("What about since Monday.", "news in Iran since Monday"),
                ("since Monday?", "news in Iran since Monday"),
                ("what about since monday?", "news in Iran since monday"),
            ):
                await one("since_time", prompt,
                    lambda r, d, q=expected: web_direct(d, q),
                    "time-only follow-up preserves previous topic and replaces scope", prior="news in Iran yesterday")

            # Reversed public source -> Notes wording from the original probe/report family.
            for verb in ("record", "save", "log"):
                for destination in ("my notes", "Notes"):
                    prompt = f"{verb} the Zorvian treaty in {destination} after you search the web for it"
                    await one("reverse_notes", prompt,
                        lambda r, d: d.required_tool_groups == (frozenset({"web_search"}), frozenset({"create_note"})),
                        "frozen public lookup then ordered Notes write")

            # Bound channels and recipient shapes around known-good delivery grammar.
            delivery_cases = (
                ("email Mom the latest news after the Zorvian summit", "web_search", "send_email"),
                ("text Mom the latest news after the Zorvian summit", "web_search", "send_message"),
                ("email alice@example.com what happened after the summit today", "web_search", "send_email"),
                ("text 415-555-1212 what happened after the summit today", "web_search", "send_message"),
                ("email the latest guidance on how to apply for the Zorvian visa to Mom", "web_search", "send_email"),
                ("text the latest guidance on how to apply for the Zorvian visa to Mom", "web_search", "send_message"),
            )
            for prompt, source_tool, effect in delivery_cases:
                await one("delivery_shapes", prompt,
                    lambda r, d, s=source_tool, e=effect: d.required_tool_groups[0] == frozenset({s}) and reachable(d, e),
                    "frozen source precedes requested channel/recipient delivery")

            # Root authorization projection: deliberately inject unauthorized effects into every field.
            for unauthorized in ("send_email", "add_reminder", "open_app", "view_messages"):
                prompt = "search the web for how to send a message; save it in Notes"
                injected = module._mk_scoped(["web_search", "create_note", unauthorized], "injected", light=False)
                injected.required_tool_groups = (frozenset({"web_search"}), frozenset({"create_note"}), frozenset({unauthorized}))
                injected.tool_argument_bindings = {unauthorized: {"value": "secret"}}
                injected.direct_calls = [(unauthorized, {"value": "secret"})]
                injected.force_first_tool = unauthorized
                injected.conditional_tools = (("web_search", unauthorized, "ok", True),
                                              (unauthorized, "create_note", "ok", True),
                                              ("web_search", "create_note", "ok", True))
                injected.narration_after = frozenset({"web_search", unauthorized})
                req = _classify_web_request(prompt)
                with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
                    d = await route(prompt)
                ok = (not reachable(d, unauthorized) and unauthorized in d.forbidden_tools
                      and d.force_first_tool is None
                      and all(unauthorized not in item[:2] for item in d.conditional_tools)
                      and unauthorized not in d.narration_after)
                results.append(("root_projection", ok))
                if not ok:
                    failures.append({"family": "root_projection", "prompt": prompt,
                                     "unauthorized": unauthorized,
                                     "expected": "unauthorized tool removed from every projection",
                                     "actual": snapshot(req, d)})

            counts = Counter(f for f, _ in results)
            passes = Counter(f for f, ok in results if ok)
            print(json.dumps({"total": len(results), "passed": sum(ok for _, ok in results),
                              "failed": len(failures),
                              "families": {f: {"total": counts[f], "passed": passes[f],
                                                "failed": counts[f] - passes[f]} for f in counts},
                              "failures": failures}, indent=2, default=str))

        with patch("builtins.print") as printed:
            await main()
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["total"], 94)
        self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))

    async def test_seventh_auditor_outbound62(self) -> None:
        """Exact 62-case auditor matrix, imported before seventh repair."""
        from collections import Counter
        from service.router import router as module

        def reachable(d, tool):
            return (
                tool in (d.tool_subset or [])
                or tool in d.tool_argument_bindings
                or d.force_first_tool == tool
                or tool in d.narration_after
                or any(name == tool for name, _ in d.direct_calls)
                or any(tool in group for group in d.required_tool_groups)
                or any(tool in item[:2] for item in d.conditional_tools)
            )


        def ordered(d, *tools):
            return d.required_tool_groups == tuple(frozenset({tool}) for tool in tools)


        def detail(r, d):
            return {
                "source": r.source,
                "query": r.query,
                "delivery": None if not r.delivery else r.delivery.__dict__,
                "cancelled": r.delivery_cancelled,
                "effects": sorted(r.authorized_effects),
                "authorized": sorted(r.authorized_tools),
                "continuations": [c.__dict__ | {"provenance": c.provenance.value} for c in r.continuations],
                "subset": d.tool_subset,
                "direct": d.direct_calls,
                "groups": [sorted(g) for g in d.required_tool_groups],
                "bindings": d.tool_argument_bindings,
                "forced": d.force_first_tool,
                "conditional": d.conditional_tools,
                "narration_after": sorted(d.narration_after),
                "forbidden": sorted(d.forbidden_tools),
                "resolved": d.resolved_request,
            }


        async def main():
            results = []

            async def check(family, prompt, predicate, expectation, prior=None, assistant=None):
                request = _classify_web_request(prompt, prior, last_assistant=assistant)
                with patch("service.router.router.role_to_model", return_value="audit-model"), patch(
                    "service.router.router._semantic_core", new=AsyncMock(return_value=[])
                ):
                    decision = await route(prompt, last_user=prior, last_assistant=assistant)
                ok = bool(predicate(request, decision))
                results.append({
                    "family": family, "prompt": prompt, "prior": prior, "assistant": assistant,
                    "ok": ok, "expected": expectation, "actual": detail(request, decision),
                })

            # Public source predicates are data. Only the explicitly requested Notes effect is reachable.
            for topic in (
                "how to email alice@example.com",
                '"how to set a reminder tomorrow"',
                "how to text 415-555-1212 safely",
                'ways to delete a file',
                "how to open an app on macOS",
                "why people send messages after midnight",
            ):
                for separator in ("; ", ", then ", " — then "):
                    prompt = f"Please search the web for {topic}{separator}save the results to Notes."
                    await check(
                        "frozen_source", prompt,
                        lambda r, d: r.authorized_effects == {"create_note"}
                        and ordered(d, "web_search", "create_note")
                        and not any(reachable(d, x) for x in ("send_email", "send_message", "add_reminder", "delete_path", "open_app")),
                        "only ordered web_search -> create_note; source predicates never become effects",
                    )

            # Explicit cancellation must remove and forbid the intended channel effect.
            cancel_cases = (
                ("Search the web for Zorvia news, then email it to Mom; please don't send it.", "send_email"),
                ("Search the web for Zorvia news, then email it to Mom — could you not send it?", "send_email"),
                ("Search the web for Zorvia news, then email it to Mom — can you not send it?", "send_email"),
                ("Search the web for Zorvia news, then email it to Mom — would you not send it?", "send_email"),
                ("Search the web for Zorvia news, then email it to Mom; actually, don't send it.", "send_email"),
                ("Email Mom the latest Zorvia news, but do not email it.", "send_email"),
                ("Text 415-555-1212 the latest Zorvia news without messaging them.", "send_message"),
                ("Text 415-555-1212 the latest Zorvia news; cancel the message.", "send_message"),
            )
            for prompt, effect in cancel_cases:
                await check(
                    "delivery_cancel", prompt,
                    lambda r, d, e=effect: r.delivery_cancelled and r.delivery is None and not reachable(d, e) and e in d.forbidden_tools,
                    f"cancellation frozen; {effect} absent everywhere and forbidden",
                )

            # Source-internal after/to/on/how-to must remain in the query; outer destinations bind separately.
            source_cases = (
                ("Email Mom the latest news after the Zorvian summit.", "latest news after the Zorvian summit", "send_email", "Mom"),
                ("Email Mom the latest guidance on how to apply for the Zorvian visa.", "the latest guidance on how to apply for the Zorvian visa", "send_email", "Mom"),
                ("Email the latest guidance on how to apply for the Zorvian visa to Mom.", "the latest guidance on how to apply for the Zorvian visa", "send_email", "Mom"),
                ("Email the latest news about Zorvia to Mom.", "latest news about Zorvia", "send_email", "Mom"),
                ("Email the latest update on Zorvia to alice@example.com.", "the latest update on Zorvia", "send_email", "alice@example.com"),
                ("Email the latest report after the summit to John Doe.", "the latest report after the summit", "send_email", "John Doe"),
                ("Text the latest news on flights to Paris to (415) 555-1212.", "latest news on flights to Paris", "send_message", "(415) 555-1212"),
            )
            for prompt, query, effect, recipient in source_cases:
                await check(
                    "source_recipient", prompt,
                    lambda r, d, q=query, e=effect, recipient=recipient: (r.query or "").rstrip(".") == q
                    and (d.tool_argument_bindings.get("web_search", {}).get("query") or "").rstrip(".") == q
                    and ordered(d, "web_search", *( ["lookup_contact"] if "@" not in recipient and not any(c.isdigit() for c in recipient) else []), e),
                    "complete source preserved, recipient separate, web first",
                )

            # A terminal source-internal `to ProperNoun` is not a recipient slot.
            for prompt, query, effect in (
                ("Email the latest route to Paris", "the latest route to Paris", "send_email"),
                ("Email the latest flight to Tokyo", "the latest flight to Tokyo", "send_email"),
                ("Text the latest route to Berlin", "the latest route to Berlin", "send_message"),
                ("Email the latest travel guidance to Japan", "the latest travel guidance to Japan", "send_email"),
            ):
                await check(
                    "source_internal_to", prompt,
                    lambda r, d, q=query, e=effect: r.query == q and not reachable(d, e)
                    and r.clarification == "Who should receive the public findings?",
                    "complete source retained and missing recipient clarified without delivery",
                )

            # Literal follow-up recipients must attach to the frozen source, never become query text.
            prior = "search the web for Zorvia news"
            followups = (
                ("Email it to alice+brief@example.co.uk.", "send_email", "alice+brief@example.co.uk"),
                ("Text it to +1 415-555-1212.", "send_message", "+1 415-555-1212"),
                ("Email it to John Doe.", "send_email", "John Doe"),
                ("email it to john doe", "send_email", "john doe"),
            )
            for prompt, effect, recipient in followups:
                await check(
                    "recipient_followup", prompt,
                    lambda r, d, e=effect, recipient=recipient: r.inherited and r.query == prior
                    and d.tool_argument_bindings.get("web_search") == {"query": prior}
                    and reachable(d, e)
                    and (d.tool_argument_bindings.get(e) == {"to": recipient}
                         or d.tool_argument_bindings.get("lookup_contact") == {"name": recipient}),
                    "frozen source inherited and literal recipient bound to the requested delivery",
                    prior=prior,
                )

            # Notes operations remain ordered after the frozen public lookup.
            notes_cases = (
                ("save it to Notes", "create_note"),
                ("save it in Apple Notes", "create_note"),
                ("log it into Notes", "create_note"),
                ("store it to my notes", "create_note"),
                ("record it in Notes", "create_note"),
                ("append it to Notes", "append_note"),
            )
            for clause, effect in notes_cases:
                prompt = f"Search the web for Zorvia news; {clause}."
                await check(
                    "notes_order", prompt,
                    lambda r, d, e=effect: d.required_tool_groups[0] == frozenset({"web_search"})
                    and d.required_tool_groups[-1] == frozenset({e}) and reachable(d, e),
                    f"ordered web_search -> {effect}",
                )

            # Acknowledgements cannot revive a completed or unrelated historical send.
            old = "search the web for Zorvia news and email it to Mom"
            assistant_cases = (
                "Done — I sent the update to Mom.",
                "Would you like me to summarize the sources?",
                "I sent it. Would you like me to text Dad the receipt?",
                "The update is sent. Shall I email you the unrelated invoice?",
            )
            for answer in ("yes", "okay", "go ahead"):
                for assistant in assistant_cases:
                    await check(
                        "ack_no_replay", answer,
                        lambda r, d: not reachable(d, "send_email") and not reachable(d, "send_message"),
                        "no old outbound effect replayed",
                        prior=old, assistant=assistant,
                    )

            # A matching pending send offer may preserve the original frozen request.
            for answer in ("yes", "please do", "go ahead"):
                await check(
                    "ack_pending", answer,
                    lambda r, d: r.inherited and reachable(d, "web_search") and reachable(d, "send_email"),
                    "matching pending email offer inherits frozen result and delivery",
                    prior=old, assistant="Would you like me to email this update to Mom now?",
                )

            failed = [item for item in results if not item["ok"]]
            print(json.dumps({"total": len(results), "passed": len(results)-len(failed), "failed": len(failed), "failures": failed}, indent=2, default=str))

        with patch("builtins.print") as printed:
            await main()
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["total"], 62)
        self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))

    async def test_seventh_auditor_followup67(self) -> None:
        """Exact 67-case auditor matrix, imported before seventh repair."""
        from collections import Counter
        from service.router import router as module

        def reachable(d, tool):
            return (tool in (d.tool_subset or [])
                    or tool in d.tool_argument_bindings
                    or any(name == tool for name, _ in d.direct_calls)
                    or any(tool in group for group in d.required_tool_groups))


        def direct(d, query):
            return (d.direct_calls == [("web_search", {"query": query})]
                    and d.tool_argument_bindings.get("web_search") == {"query": query}
                    and d.required_tool_groups == (frozenset({"web_search"}),))


        def ordered_notes(d, query):
            return (d.tool_subset == ["web_search", "create_note"]
                    and d.required_tool_groups == (
                        frozenset({"web_search"}), frozenset({"create_note"}))
                    and d.tool_argument_bindings.get("web_search") == {"query": query})


        def snap(r, d):
            return {
                "source": r.source, "query": r.query, "explicit": r.explicit,
                "current": r.current, "provenance": r.provenance.value,
                "inherited": r.inherited, "independent": r.independent_task,
                "clarification": r.clarification,
                "presentations": [x.text for x in r.presentations],
                "continuations": [
                    {"text": x.text, "action": x.action, "negated": x.negated}
                    for x in r.continuations],
                "delivery": None if r.delivery is None else {
                    "channel": r.delivery.channel, "recipient": r.delivery.recipient,
                    "address": r.delivery.address, "phone": r.delivery.phone,
                    "target_missing": r.delivery.target_missing},
                "subset": d.tool_subset, "direct": d.direct_calls,
                "required": [sorted(x) for x in d.required_tool_groups],
                "bindings": d.tool_argument_bindings,
                "forbidden": sorted(d.forbidden_tools),
                "resolved": d.resolved_request,
            }


        async def main():
            rows, failures = [], []

            async def check(family, prompt, predicate, expected, *, prior=None, assistant=None):
                req = _classify_web_request(prompt, prior, last_assistant=assistant)
                with patch("service.router.router.role_to_model", return_value="audit-model"), \
                     patch("service.router.router._semantic_core", new=AsyncMock(return_value=[])):
                    decision = await route(prompt, last_user=prior, last_assistant=assistant)
                ok = predicate(req, decision)
                rows.append((family, ok))
                if not ok:
                    failures.append({"family": family, "prompt": prompt, "prior": prior,
                                     "assistant": assistant, "expected": expected,
                                     "actual": snap(req, decision)})

            # Explicit/current grammar: same operation under bounded case, politeness, punctuation.
            explicit = (
                "Please search the web for the Zorvian treaty",
                "PLEASE SEARCH THE WEB FOR THE ZORVIAN TREATY",
                "Could you please search the web for the Zorvian treaty?",
                "Would you mind searching the internet for the Zorvian treaty?",
                "Kindly query the internet for the Zorvian treaty.",
            )
            for prompt in explicit:
                await check("explicit_polite", prompt, lambda r, d, q=prompt: direct(d, q),
                            "one exact bound web lookup")
            for prompt in (
                "Please, search the web for the Zorvian treaty.",
                "Could you, please search the web for the Zorvian treaty?",
                "Would you mind, searching the internet for the Zorvian treaty?",
            ):
                await check("explicit_polite_comma", prompt, lambda r, d, q=prompt: direct(d, q),
                            "polite punctuation does not suppress explicit lookup")

            for prompt in (
                "What is happening in Zorvia today?",
                "WHAT IS HAPPENING IN ZORVIA TODAY?",
                "Please tell me what is happening in Zorvia today.",
                "Could you tell me what is happening in Zorvia today?",
                "What’s happening in Zorvia today?",
            ):
                await check("current_polite", prompt, lambda r, d, q=prompt: direct(d, q),
                            "current public fact gets exact bound lookup")
            for prompt in (
                "Please, tell me what is happening in Zorvia today.",
                "Could you, tell me what is happening in Zorvia today?",
            ):
                await check("current_polite_comma", prompt, lambda r, d, q=prompt: direct(d, q),
                            "polite punctuation does not suppress current lookup")

            # Same public follow-up/time substitution under bounded case, punctuation, polite prefix.
            prior_today = "news in Iran today"
            for prompt, query in (
                ("What about Ukraine?", "Ukraine news today"),
                ("WHAT ABOUT UKRAINE?", "UKRAINE news today"),
                ("How about Ukraine!", "Ukraine news today"),
                ("Ukraine too.", "Ukraine news today"),
                ("Ukraine, too?", "Ukraine news today"),
                ("Please, what about Ukraine?", "Ukraine news today"),
                ("Could you do Ukraine next?", "Ukraine news today"),
            ):
                await check("followup_form", prompt, lambda r, d, q=query: direct(d, q),
                            "inherit public operation and replace topic", prior=prior_today)

            prior_yesterday = "news in Iran yesterday"
            for prompt, query in (
                ("What about last month?", "news in Iran last month"),
                ("How about this quarter?", "news in Iran this quarter"),
                ("THE PAST WEEK?", "news in Iran THE PAST WEEK"),
                ("Earlier today?", "news in Iran today"),
                ("Please, what about last month?", "news in Iran last month"),
                ("Could you do the past week?", "news in Iran the past week"),
            ):
                await check("time_followup_form", prompt, lambda r, d, q=query: direct(d, q),
                            "replace old time while retaining public topic", prior=prior_yesterday)

            # Presentation operations must remain metadata on one source lookup.
            for suffix in (
                "summarize it", "Summarize the results", "please explain the findings",
                "teach me about it", "give me a brief", "give me the key points",
                "outline the findings", "compare the results", "list the findings",
                "turn it into bullet points",
            ):
                prompt = "search the web for the Zorvian treaty; " + suffix
                source = "search the web for the Zorvian treaty"
                await check("presentation_form", prompt,
                            lambda r, d, q=source: direct(d, q) and bool(r.presentations),
                            "one exact lookup plus typed presentation metadata")

            # Notes writes: same frozen lookup and ordered local write across supported verbs,
            # prepositions, case, destination, separators, and polite prefixes.
            for verb in ("save", "record", "log", "store"):
                for prep, destination in (("to", "Notes"), ("in", "my notes"), ("into", "Apple Notes")):
                    suffix = f"{verb} it {prep} {destination}"
                    prompt = "search the web for the Zorvian treaty; " + suffix
                    source = "search the web for the Zorvian treaty"
                    await check("notes_form", prompt, lambda r, d, q=source: ordered_notes(d, q),
                                "web_search then create_note; exact frozen source")
            for suffix in (
                "please save it to Notes", "Please record the findings in Apple Notes",
                "then store the results into my notes",
            ):
                prompt = "search the web for the Zorvian treaty, and " + suffix
                source = "search the web for the Zorvian treaty"
                await check("notes_polite", prompt, lambda r, d, q=source: ordered_notes(d, q),
                            "politeness/separator preserves ordered Notes write")

            # Bare acknowledgements are tied only to a genuine pending delivery offer.
            prior = "search the web for Zorvia news and email it to Mom"
            pending = "Would you like me to email this update to Mom now?"
            for prompt in ("yes", "Yes!", "yes please", "Yes, please.", "sure", "Sure, go ahead.", "please do"):
                await check("ack_pending", prompt,
                            lambda r, d: reachable(d, "web_search") and reachable(d, "send_email"),
                            "genuine pending offer authorizes inherited lookup and delivery",
                            prior=prior, assistant=pending)
            completed = "Done — I sent the update to Mom. Would you like a shorter summary?"
            for prompt in ("yes", "Yes!", "yes please", "Yes, please.", "sure", "Sure, go ahead.", "please do"):
                await check("ack_completed", prompt,
                            lambda r, d: not reachable(d, "web_search") and not reachable(d, "send_email"),
                            "completed send plus unrelated offer cannot replay effects",
                            prior=prior, assistant=completed)

            counts = Counter(f for f, _ in rows)
            passed = Counter(f for f, ok in rows if ok)
            print(json.dumps({
                "total": len(rows), "passed": sum(ok for _, ok in rows), "failed": len(failures),
                "families": {f: {"total": counts[f], "passed": passed[f],
                                 "failed": counts[f] - passed[f]} for f in counts},
                "failures": failures,
            }, indent=2, default=str))

        with patch("builtins.print") as printed:
            await main()
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["total"], 67)
        self.assertEqual(report["failed"], 0, json.dumps(report, indent=2))

    async def test_seventh_auditor_privacy18(self) -> None:
        """Exact 18-case auditor matrix, imported before seventh repair."""
        from collections import Counter
        from service.router import router as module

        def web_reachable(d):
            return ("web_search" in (d.tool_subset or [])
                    or "web_search" in d.tool_argument_bindings
                    or any(n == "web_search" for n, _ in d.direct_calls)
                    or any("web_search" in g for g in d.required_tool_groups))

        CASES = [
            ("governing_consent", "search the web for Zorvia news; internet access isn't allowed", False),
            ("governing_consent", "search the web for Zorvia news; web access isn’t authorized", False),
            ("governing_consent", "search the web for Zorvia news; browsing isn't permitted", False),
            ("local_deixis", "search the web for the data stored here", False),
            ("local_deixis", "search the web for what's in here", False),
            ("local_deixis", "what changed here today?", False),
            ("local_deixis", "what is running here right now?", False),
            ("first_second_person_source", "search the web for the customer database I gave you", False),
            ("first_second_person_source", "search the web for the deployment logs you stored", False),
            ("first_second_person_source", "search the web for the report we wrote", False),
            ("first_second_person_source", "search the web for the measurements you have", False),
            ("first_second_person_source", "search the web for a memo I uploaded", False),
            ("topical_data", "search the web for a documentary about life without internet", True),
            ("topical_data", "search the web for the documentary life without internet", True),
            ("topical_data", "search the web for documentary: life without internet", True),
            ("control_optout", "search the web for Zorvia news; web access is not allowed", False),
            ("control_owned", "search the web for my customer database", False),
            ("control_public", "search the web for Qorblax's CLI documentation", True),
        ]

        async def main():
            fails=[]
            for family, prompt, expect_web in CASES:
                req=_classify_web_request(prompt)
                with patch("service.router.router.role_to_model", return_value="audit-model"), patch("service.router.router._semantic_core", new=AsyncMock(return_value=[])):
                    d=await route(prompt)
                got=web_reachable(d)
                ok=got == expect_web
                print(f"{'PASS' if ok else 'FAIL'} {family}: {prompt}")
                if not ok:
                    item={"family":family,"prompt":prompt,"expected_web":expect_web,"actual_web":got,
                          "request":{"explicit":req.explicit,"current":req.current,"opted_out":req.opted_out,
                                     "provenance":req.provenance.value,"allowed":req.allowed,"query":req.query,
                                     "clarification":req.clarification},
                          "route":{"direct":d.direct_calls,"groups":[sorted(x) for x in d.required_tool_groups],
                                   "bindings":d.tool_argument_bindings,"forbidden":sorted(d.forbidden_tools),
                                   "resolved":d.resolved_request,"reason":d.reason}}
                    fails.append(item)
                    print(json.dumps(item, indent=2, default=str))
            print(f"TOTAL={len(CASES)} PASS={len(CASES)-len(fails)} FAIL={len(fails)}")

        with patch("builtins.print") as printed:
            await main()
        self.assertEqual(printed.call_args.args[0], "TOTAL=18 PASS=18 FAIL=0",
                         "\n".join(str(call.args[0]) for call in printed.call_args_list))


    async def test_seventh_independent_consent_privacy80(self) -> None:
        """Fresh independent structural cases preserved before their repair."""
        from service.router.web_request import classify

        cases = []
        def add(label, prompt, allowed, prior=None):
            cases.append((label, prompt, allowed, prior))

        governors = [
            "You mustn't browse the web", "You shouldn't access the internet",
            "You can't use online sources", "You may not browse the web",
            "You aren't authorized to search the web", "You aren't allowed to access the internet",
            "Web browsing isn't permitted", "Internet access isn't authorized",
            "I don't authorize online searches", "We haven't authorized web access",
            "Permission to browse the web is denied", "I revoke permission to access the internet",
        ]
        for i, governor in enumerate(governors):
            add('governance', 'Search the web for Python releases' + ['; ', ' — ', ' – '][i % 3] + governor, False)
            add('quoted topic', 'Search the web for the phrase "' + governor + '"', True)

        relatives = [
            'that I wrote', 'which we built', 'you created', 'that I have written',
            'which we have developed', 'that you have generated', 'that I am writing',
            'we are building', 'that you are editing', 'I own', 'that we maintain',
            'that you manage', 'that I keep', 'which we use internally',
        ]
        for relative in relatives:
            add('owned technical', 'Search the web for the latest API documentation ' + relative, False)

        public = [
            "OpenAI's public API documentation", "Python's latest release",
            "Rust's public source code", 'the API documentation published by OpenAI',
            'the API documentation that Microsoft publishes',
            'the public SDK that supports Python', 'the open source CLI that supports Linux',
            'the public API reference for Python', 'the code interpreter public documentation',
            'the latest API documentation for the web',
        ]
        for topic in public:
            add('public technical', 'Search the web for ' + topic, True)

        for topic in ['the API documentation here', 'the SDK stored here', 'the source code in this workspace',
                      'the API documentation from this conversation', 'the latest API documentation we discussed',
                      'the public SDK in our checkout']:
            add('local context', 'Search the web for ' + topic, False)
        for topic in ['the song "Wish You Were Here"', 'the phrase "the API documentation here"',
                      'the phrase "permission is denied"', 'the Stop Online Piracy campaign',
                      'communities without internet', 'the slogan "Do not browse the web"']:
            add('nominal control', 'Search the web for ' + topic, True)

        for prefix in ['Search the web for ', 'What is new in ', 'What about ']:
            for topic in ['the API documentation that I wrote', 'the SDK you generated here',
                          "my private notes about Python's public API", 'the open source CLI we maintain']:
                add('projection private', prefix + topic, False, 'Search the web for Python releases today')
        for text in ['Search the web for Python releases — do not browse',
                     'Search the web for Python releases – permission to search is revoked',
                     'Search the web for Python releases; search permission is not granted',
                     'Search the web for Python releases; I withhold consent for web access',
                     'Search the web for Python releases; I do not consent to online access',
                     'Search the web for Python releases; you are prohibited from browsing',
                     'Search the web for Python releases; please keep this off the internet',
                     'Search the web for Python releases; use existing knowledge only']:
            add('governance extra', text, False)

        async def main():
            failures = []
            with patch('service.router.router.role_to_model', return_value='synthetic-Ornith'), patch(
                    'service.router.router._semantic_core', new=AsyncMock(return_value=[])):
                for label, prompt, expected, prior in cases:
                    parsed = classify(prompt, last_user=prior)
                    decision = await route(prompt, last_user=prior)
                    actual = parsed.allowed
                    calls = [(n, a) for n, a in decision.direct_calls if n in {'web_search', 'web_fetch', 'http_request'}]
                    if actual != expected or (not expected and calls):
                        failures.append(dict(label=label, prompt=prompt, expected=expected, actual=actual,
                                             opted_out=parsed.opted_out, provenance=parsed.provenance,
                                             query=parsed.query, clarification=parsed.clarification,
                                             direct_calls=calls, offered=decision.tool_subset,
                                             forbidden=sorted(set(decision.forbidden_tools) & {'web_search', 'web_fetch', 'http_request', 'run_shell'})))
            print(json.dumps(dict(cases=len(cases), failures=len(failures), results=failures), indent=2))

        with patch("builtins.print") as printed:
            await main()
        report = json.loads(printed.call_args.args[0])
        self.assertEqual(report["cases"], 80)
        self.assertEqual(report["failures"], 0, json.dumps(report["results"], indent=2))

    async def test_eighth_external_request_consent_and_projection(self) -> None:
        from service.router import router as module
        # Exact auditor reproduction; the first row is also the preserved
        # failing case from the older 34-case privacy probe.
        cases = (
            ("search the web for Zorvia news, but make no external requests", False),
            ("SEARCH THE WEB FOR ZORVIA NEWS, BUT MAKE NO EXTERNAL REQUESTS", False),
            ("search the web for Zorvia news; make no external requests", False),
            ("search the web for Zorvia news — make no external requests", False),
            ('search the web for the phrase "make no external requests"', True),
            ("search the web for a documentary titled ‘Make No External Requests’", True),
        )
        blocked = {"web_search", "web_fetch", "http_request", "run_shell"}

        def reached(decision):
            names = set(decision.tool_subset or ()) | set(decision.tool_argument_bindings) | set(decision.narration_after)
            names.update(name for name, _ in decision.direct_calls)
            names.update(name for group in decision.required_tool_groups for name in group)
            names.update(name for item in decision.conditional_tools for name in item[:2])
            if decision.force_first_tool:
                names.add(decision.force_first_tool)
            return names

        for prompt, expect_web in cases:
            with self.subTest(prompt=prompt):
                request = _classify_web_request(prompt)
                with patch.object(module, "role_to_model", return_value="audit-model"), \
                     patch.object(module, "_semantic_core", new=AsyncMock(return_value=[])):
                    decision = await route(prompt)
                self.assertEqual("web_search" in reached(decision), expect_web)
                self.assertEqual(request.allowed, expect_web)
                self.assertEqual(request.opted_out, not expect_web)
                if expect_web:
                    self.assertEqual(request.query, prompt)
                    self.assertEqual(decision.tool_argument_bindings["web_search"], {"query": prompt})
                    continue
                self.assertEqual(request.source.lower(), "search the web for zorvia news")
                self.assertNotIn("external requests", (request.query or "").lower())
                self.assertFalse(blocked & reached(decision))
                self.assertTrue(blocked <= decision.forbidden_tools)
                # A downstream fallback cannot reintroduce network access
                # through any execution metadata after root consent denial.
                injected = module._mk_scoped(sorted(blocked), "synthetic projection injection", light=False)
                injected.direct_calls = [(name, {"query": prompt}) for name in blocked]
                injected.required_tool_groups = tuple(frozenset({name}) for name in blocked)
                injected.tool_argument_bindings = {name: {"query": prompt} for name in blocked}
                injected.force_first_tool = "web_search"
                injected.conditional_tools = tuple((name, name, "ok", True) for name in blocked)
                injected.narration_after = frozenset(blocked)
                with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)), \
                     patch.object(module, "_classify_web_request", wraps=_classify_web_request) as root:
                    decision = await route(prompt)
                self.assertEqual(root.call_count, 1)
                self.assertFalse(blocked & reached(decision))
                self.assertTrue(blocked <= decision.forbidden_tools)

    async def test_seventh_denied_root_projection_all_execution_fields(self) -> None:
        from service.router import router as module
        source = "search the web for Zorvia news"
        previous = source + "; email it to Mom"
        for prompt, prior, assistant, blocked in (
            (source + " — You can't use online sources", None, None, {"web_search", "web_fetch", "http_request", "run_shell"}),
            ("search the web for the API documentation that we maintain", None, None, {"web_search", "web_fetch", "http_request", "run_shell"}),
            ("Email the latest route to Paris", None, None, {"web_search", "send_email", "lookup_contact"}),
            (previous + " — could you not send it?", None, None, {"send_email", "send_message", "lookup_contact"}),
            ("Yes, please.", previous, "I sent it. Would you like me to text Dad the receipt?", {"web_search", "send_email", "send_message", "lookup_contact"}),
            ("Sure, go ahead.", previous, "Shall I email you the unrelated invoice?", {"web_search", "send_email", "send_message", "lookup_contact"}),
            (source, None, None, {"unregistered_injected_effect", "send_email", "view_messages"}),
        ):
            with self.subTest(prompt=prompt):
                injected = module._mk_scoped(list(blocked), "injected downstream fallback", light=False)
                injected.direct_calls = [(name, {"value": "synthetic"}) for name in blocked]
                injected.required_tool_groups = tuple(frozenset({name}) for name in blocked)
                injected.tool_argument_bindings = {name: {"value": "synthetic"} for name in blocked}
                injected.force_first_tool = next(iter(blocked))
                injected.conditional_tools = tuple((name, name, "ok", True) for name in blocked)
                injected.narration_after = frozenset(blocked)
                with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)), \
                     patch.object(module, "_classify_web_request", wraps=_classify_web_request) as root:
                    d = await route(prompt, last_user=prior, last_assistant=assistant,
                                    last_tools="send_email, web_search, run_shell")
                self.assertEqual(root.call_count, 1)
                reached = set(d.tool_subset or ()) | set(d.tool_argument_bindings) | set(d.narration_after)
                reached.update(name for name, _ in d.direct_calls)
                reached.update(name for group in d.required_tool_groups for name in group)
                reached.update(name for item in d.conditional_tools for name in item[:2])
                if d.force_first_tool:
                    reached.add(d.force_first_tool)
                self.assertFalse(blocked & reached)
                self.assertTrue(blocked <= d.forbidden_tools)

    async def test_seventh_standalone_action_offer_is_scoped_without_history_replay(self) -> None:
        offer = "Shall I run it?\n```bash\nls\n```"
        for acknowledgement in ("do it", "yes please"):
            with self.subTest(acknowledgement=acknowledgement):
                parsed = _classify_web_request(acknowledgement, last_assistant=offer)
                self.assertTrue(parsed.standalone_offer)
                self.assertEqual(parsed.pending_offer.action_text, "run it")
                with patch("service.router.router._semantic_core", new=AsyncMock(return_value=["run_shell"])) as retrieve:
                    decision = await route(acknowledgement, last_assistant=offer,
                                           last_tools="send_email, web_search")
                retrieve.assert_awaited_once_with("run it")
                self.assertEqual(decision.tool_subset, ["run_shell"])
                self.assertFalse(decision.direct_calls)
                self.assertIn("send_email", decision.forbidden_tools)
                self.assertNotIn("web_search", decision.tool_subset)
                self.assertEqual(decision.resolved_request,
                                 "Perform only the currently acknowledged offer: run it")
                # A current non-delivery offer does not re-authorize a prior
                # public-result delivery, even when prior tool logs name it.
                decision = await route(acknowledgement, last_assistant=offer,
                                       last_user="search the web for Zorvia news; email it to Mom",
                                       last_tools="send_email, web_search")
                self.assertFalse(decision.needs_tools)
                self.assertFalse(decision.tool_subset)

    async def test_seventh_pending_identity_and_argument_projection(self) -> None:
        from service.router import router as module
        from dataclasses import FrozenInstanceError
        source = "search the web for Zorvia news"
        prior = source + "; email it to Mom"
        for completion, ack in product(("I've sent it.", "We've delivered it.", "It's sent.", "Sent.", "They were delivered."),
                                        ("yes please", "Sure, go ahead.")):
            with self.subTest(completion=completion, ack=ack):
                d = await route(ack, last_user=prior, last_assistant=completion + " Would you like me to email this update to Mom now?")
                self.assertFalse(d.needs_tools)
                self.assertFalse(d.tool_subset)
        for suffix in ("", "; save it in Notes"):
            prompt = source + "; email it to news@example.com" + suffix
            parsed = _classify_web_request(prompt)
            names = ["web_search", "send_email"] + (["create_note"] if suffix else [])
            injected = module._mk_scoped(names, "argument substitution attempt", light=False)
            injected.direct_calls = [("send_email", {"to": "wrong@example.com", "body": "unverified"})]
            injected.tool_argument_bindings = {"web_search": {"query": "unrelated"}, "send_email": {"to": "wrong@example.com"}}
            with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
                d = await route(prompt)
            self.assertEqual(d.tool_argument_bindings["web_search"], {"query": source})
            self.assertEqual(d.tool_argument_bindings["send_email"], {"to": "news@example.com"})
            self.assertNotIn("create_note", d.tool_argument_bindings)
            self.assertFalse(d.direct_calls)
            with self.assertRaises(FrozenInstanceError):
                parsed.delivery.recipient = "wrong@example.com"
        for assistant, forbidden in (
            ("Want me to send that text to Dad?", {"send_email", "web_search"}),
            ("Should I add that to your calendar?", {"send_email", "send_message"}),
        ):
            injected = module._mk_scoped(list(forbidden), "standalone offer injection", light=False)
            injected.force_first_tool = next(iter(forbidden))
            with patch.object(module, "_route_request", new=AsyncMock(return_value=injected)):
                d = await route("yes please", last_assistant=assistant)
            self.assertFalse(forbidden & set(d.tool_subset or ()))
            self.assertTrue(forbidden <= d.forbidden_tools)

if __name__ == "__main__":
    unittest.main()
