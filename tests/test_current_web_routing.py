"""Current public-information prompts use Ling's dedicated web-search path."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from itertools import product
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
                 "how to send messages to 415-555-1212", "how to delete a file"),
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


if __name__ == "__main__":
    unittest.main()
