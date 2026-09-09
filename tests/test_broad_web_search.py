"""Broad-topic discovery contracts. Synthetic providers/HTTP only; no live calls."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from service.research import web
from service.tools import web_tools


REAL_CLIENT = httpx.AsyncClient


def hit(title: str, path: str = "page", *, host: str = "source.example.test",
        snippet: str = "") -> web.SearchHit:
    return web.SearchHit(title, f"https://{host}/{path}", snippet=snippet, domain=host)


class OfflineCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "",
                                                  "WISP_OPENALEX_API_KEY": ""}))
        self.requests: list[httpx.Request] = []
        self.handler = None

        def handle(request):
            self.requests.append(request)
            if self.handler is None:
                raise AssertionError(f"Unexpected network attempt: {request.url.host}")
            return self.handler(request)

        self.enterContext(patch.object(httpx, "AsyncClient", side_effect=lambda **kwargs:
            REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)))
        # Fresh locks also make event-loop ownership deterministic across tests.
        self.enterContext(patch.object(web, "_OPENALEX_SEARCH_LOCK", asyncio.Lock()))
        self.enterContext(patch.object(web, "_WIKIMEDIA_SEARCH_LOCK", asyncio.Lock()))

    def providers(self, *, ddg=(), wikipedia=(), openalex=(), brave=()):
        mocks = {}
        for name, rows in (("ddg", ddg), ("wikipedia", wikipedia),
                           ("openalex", openalex), ("brave", brave)):
            mock = AsyncMock(side_effect=rows if isinstance(rows, Exception) else None,
                             return_value=list(rows) if not isinstance(rows, Exception) else None)
            mocks[name] = self.enterContext(patch.object(web, f"_search_{name}", mock))
        return mocks


class BroadCoverageTests(OfflineCase):
    async def test_general_web_keeps_its_result_budget_across_topics(self):
        # Exact input/result fixtures, not live relevance measurements. The old
        # equal round-robin returned only 2 of these 6 useful web hits per query.
        topics = (
            "clean cast iron skillet", "SQL JOIN", "Go JSON", "C++ map",
            "Python installation", "python habitat", "Kyoto station luggage storage hours",
            "migraine treatment guidelines", "causes of Ming dynasty collapse",
            "coffee grinder comparison", "chickpea dinner recipes", "repair bicycle puncture",
            "jazz improvisation exercises", "renew passport documents", "京都 観光", "Paris",
        )
        for query in topics:
            with self.subTest(query=query):
                useful = [hit(query, f"guide-{i}") for i in range(6)]
                background = [hit("Generic background", f"background-{i}",
                                  host="en.wikipedia.org") for i in range(6)]
                with patch.object(web, "_search_ddg", AsyncMock(return_value=useful)), \
                     patch.object(web, "_search_wikipedia", AsyncMock(return_value=background)), \
                     patch.object(web, "_search_openalex", AsyncMock(return_value=background)) as papers:
                    results = await web.search_web(query, limit=6)
                self.assertEqual([row.url for row in results], [row.url for row in useful])
                self.assertEqual([row.rank for row in results], list(range(1, 7)))
                papers.assert_not_awaited()

    async def test_academic_intent_balances_relevant_papers_and_general_web(self):
        query = "research papers on migraine prevention"
        mocks = self.providers(
            ddg=[hit("Migraine prevention guidelines", "guideline")],
            openalex=[hit("Migraine prevention trial", "W12", host="openalex.org"),
                      hit("Coffee economics", "W13", host="openalex.org")],
            wikipedia=[hit("Migraine", "Migraine", host="en.wikipedia.org",
                           snippet="Migraine prevention options.")])
        results = await web.search_web(query)
        self.assertEqual([row.url.rsplit("/", 1)[1] for row in results],
                         ["W12", "guideline", "Migraine"])
        mocks["openalex"].assert_awaited_once()

    async def test_compact_science_query_retains_relevant_scholarly_fallback(self):
        query = "vaccine lyophilization thermostability field performance"
        mocks = self.providers(ddg=web.WebError("challenge"),
            openalex=[hit("Vaccine lyophilization thermostability", "W12", host="openalex.org"),
                      hit("Hockey field performance", "W13", host="openalex.org")])
        rows = await web.search_web(query)
        self.assertEqual([row.url for row in rows], ["https://openalex.org/W12"])
        self.assertIn("scholarly background", web.render_search_results(rows))
        mocks["openalex"].assert_awaited_once()

    async def test_research_discovery_persists_compact_query_fallback_source(self):
        from service.research.orchestrator import ResearchManager, _fallback_plan
        from service.research.store import ResearchStore
        query = "vaccine lyophilization thermostability field performance"
        self.providers(ddg=web.WebError("challenge"), openalex=[
            hit("Vaccine lyophilization thermostability field performance", "W12", host="openalex.org")])
        with tempfile.TemporaryDirectory() as directory:
            store = ResearchStore(Path(directory) / "research.db")
            try:
                manager = ResearchManager(store)
                plan = _fallback_plan(query, "quick")
                job = store.create_job(query, plan, state="running")
                # Exercise the real callsite, discovery pipeline and store. No
                # worker, model, page fetch or network is started by this path.
                await manager._discover(job["id"], [("Q1", query)], plan, round_index=1)
                sources = store.sources(job["id"])
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0]["url"], "https://openalex.org/W12")
                self.assertEqual(sources[0]["query"], query)
                self.assertEqual(store.event_payloads(job["id"], "query")[0]["results"], 1)
            finally:
                store.close()

    async def test_randomized_controlled_trials_selects_scholarly_provider(self):
        mocks = self.providers(ddg=[hit("Migraine prevention", str(i)) for i in range(8)])
        await web.search_web("randomized controlled trials migraine prevention")
        mocks["openalex"].assert_awaited_once()

    async def test_specialist_filter_preserves_short_and_unicode_anchors(self):
        for query in ("SQL JOIN", "Go JSON", "C++ map", "Git rebase", "Paris", "京都 観光"):
            with self.subTest(query=query):
                self.assertTrue(web._background_matches(query, hit(query)))
        self.assertFalse(web._background_matches("Python installation", hit("Python habitat")))
        self.assertFalse(web._background_matches("vaccine lyophilization stability", hit("Apple crisp recipe")))

    async def test_unrelated_background_cannot_fill_missing_general_results(self):
        self.providers(ddg=[hit("Python installation guide")],
                       wikipedia=[hit("Python habitat", host="en.wikipedia.org")])
        results = await web.search_web("Python installation")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Python installation guide")

    async def test_general_failure_keeps_background_and_reports_limited_coverage(self):
        self.providers(ddg=web.WebError("challenge"),
                       wikipedia=[hit("Ming dynasty collapse", host="en.wikipedia.org")])
        results = await web.search_web("Ming dynasty collapse")
        output = web.render_search_results(results)
        self.assertIn("General-web results were unavailable", output)
        self.assertIn("encyclopedia background", output)
        self.assertIn("https://en.wikipedia.org/page", output)

    async def test_failed_general_and_off_topic_background_raise_honest_failure(self):
        self.providers(ddg=web.WebError("challenge"), wikipedia=[hit("Bird migration")])
        with self.assertRaisesRegex(web.WebError, "ddg: WebError"):
            await web.search_web("C++ map")

    async def test_empty_results_and_blank_query_are_distinct_from_failure(self):
        mocks = self.providers()
        self.assertEqual(await web.search_web("missing topic"), [])
        for mock in mocks.values():
            mock.reset_mock()
        self.assertEqual(await web.search_web("  \n  "), [])
        for mock in mocks.values():
            mock.assert_not_awaited()

    async def test_operators_only_go_to_general_providers_unchanged(self):
        for query in ('site:docs.python.org asyncio cancellation',
                      '"SQL JOIN" -site:example.com', 'clinical trials filetype:pdf',
                      'headphones -bluetooth', 'intitle:migraine'):
            with self.subTest(query=query):
                with patch.object(web, "_search_ddg", AsyncMock(return_value=[])) as general, \
                     patch.object(web, "_search_openalex", AsyncMock(return_value=[])) as papers, \
                     patch.object(web, "_search_wikipedia", AsyncMock(return_value=[])) as wiki:
                    await web.search_web(query)
                general.assert_awaited_once_with(query, 8)
                papers.assert_not_awaited()
                wiki.assert_not_awaited()

    async def test_ordinary_colon_does_not_disable_background_discovery(self):
        query = "Paris: museums and transport"
        mocks = self.providers(wikipedia=[hit("Paris museums and transport", host="en.wikipedia.org")])
        rows = await web.search_web(query)
        self.assertEqual(len(rows), 1)
        mocks["wikipedia"].assert_awaited_once_with(query, 8)

    async def test_duplicate_invalid_results_do_not_consume_slots_or_mutate_inputs(self):
        original = hit("SQL JOIN", "guide?utm_source=fixture")
        invalid = web.SearchHit("Bad port", "https://example.test:bad/guide")
        self.providers(ddg=[original, invalid, hit("SQL JOIN", "guide"),
                            hit("SQL JOIN example", "example")])
        rows = await web.search_web("SQL JOIN", limit=2)
        self.assertEqual([row.url.rsplit("/", 1)[1] for row in rows], ["guide", "example"])
        self.assertEqual([row.rank for row in rows], [1, 2])
        self.assertEqual(original.rank, 0)
        self.assertIn("utm_source", original.url)

    async def test_independent_general_provider_survives_ddg_challenge(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        mocks = self.providers(ddg=web.WebError("DuckDuckGo returned HTTP 202"),
                              brave=[hit("Kyoto luggage storage hours")],
                              wikipedia=[hit("Kyoto railway history")])
        rows = await web.search_web("Kyoto luggage storage hours", limit=1)
        self.assertEqual(rows[0].title, "Kyoto luggage storage hours")
        self.assertFalse(rows.coverage_note)
        mocks["brave"].assert_awaited_once()

    async def test_brave_failure_does_not_erase_ddg(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        self.providers(ddg=[hit("Coffee grinder comparison")], brave=web.WebError("HTTP 429"))
        self.assertEqual(len(await web.search_web("Coffee grinder comparison")), 1)


class SearchLifecycleTests(OfflineCase):
    async def test_fast_complete_general_results_cancel_slow_background(self):
        cancelled = asyncio.Event()
        async def blocked(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.providers(ddg=[hit("SQL JOIN")])
        with patch.object(web, "_search_wikipedia", blocked):
            rows = await asyncio.wait_for(web.search_web("SQL JOIN", limit=1), 0.5)
        self.assertEqual(len(rows), 1)
        self.assertTrue(cancelled.is_set())

    async def test_deadline_keeps_completed_partial_result_and_reaps_provider(self):
        cancelled = asyncio.Event()
        async def blocked(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.providers(ddg=[hit("Go JSON")])
        with patch.object(web, "_search_wikipedia", blocked), \
             patch.object(web, "_SEARCH_DEADLINE_SECONDS", 0.02):
            rows = await asyncio.wait_for(web.search_web("Go JSON", limit=6), 0.5)
        self.assertEqual(len(rows), 1)
        self.assertTrue(cancelled.is_set())

    async def test_deadline_includes_actual_provider_lock_wait(self):
        self.enterContext(patch.object(web, "_search_ddg", AsyncMock(return_value=[])))
        self.enterContext(patch.object(web, "_search_openalex", AsyncMock(return_value=[])))
        lock = web._WIKIMEDIA_SEARCH_LOCK
        await lock.acquire()
        try:
            with patch.object(web, "_SEARCH_DEADLINE_SECONDS", 0.02):
                with self.assertRaisesRegex(web.WebError, "wikipedia: timed out"):
                    await asyncio.wait_for(web.search_web("history of Paris"), 0.5)
            self.assertEqual(self.requests, [])
            self.assertTrue(lock.locked())
        finally:
            lock.release()
        await asyncio.wait_for(lock.acquire(), 0.5)
        lock.release()

    async def test_caller_cancellation_reaps_all_children(self):
        started = [asyncio.Event(), asyncio.Event()]
        cancelled = [asyncio.Event(), asyncio.Event()]
        def provider(index):
            async def blocked(*_args):
                started[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled[index].set()
            return blocked
        with patch.object(web, "_search_ddg", provider(0)), \
             patch.object(web, "_search_wikipedia", provider(1)):
            task = asyncio.create_task(web.search_web("Paris"))
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(all(event.is_set() for event in cancelled))

    async def test_provider_exception_text_is_not_exposed(self):
        self.providers(ddg=ValueError("secret-token in remote request"), wikipedia=[])
        with self.assertRaises(web.WebError) as caught:
            await web.search_web("Paris")
        self.assertIn("ValueError", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))


class ProviderAdapterTests(OfflineCase):
    async def test_ddg_skips_bad_urls_duplicates_and_limits_after_validation(self):
        rows = [("bad", "https://example.test:bad/x"), ("not web", "javascript:alert(1)"),
                ("SQL JOIN", "https://docs.example.test/join?utm_source=a"),
                ("duplicate", "https://docs.example.test/join"),
                ("SQL JOIN examples", "https://docs.example.test/examples")]
        body = "".join(f'<a class="result__a" href="{url}">{title}</a>'
                       '<div class="result__snippet">Useful <b>text</b>.</div>'
                       for title, url in rows)
        self.handler = lambda _request: httpx.Response(200, text=body)
        results = await web._search_ddg("SQL JOIN", 2)
        self.assertEqual([row.title for row in results], ["SQL JOIN", "SQL JOIN examples"])
        self.assertEqual(results[0].snippet, "Useful text .")

    async def test_ddg_challenge_is_a_failure(self):
        self.handler = lambda _request: httpx.Response(202, text="challenge")
        with self.assertRaisesRegex(web.WebError, "HTTP 202"):
            await web._search_ddg("test", 2)

    async def test_brave_no_key_makes_no_request(self):
        self.assertEqual(await web._search_brave("SQL JOIN", 6), [])
        self.assertEqual(self.requests, [])

    async def test_brave_auth_query_and_defensive_structured_results(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        query = 'site:docs.example.test "C++ map" -ads'
        self.handler = lambda _request: httpx.Response(200, json={"web": {"results": [
            None, {"title": "bad", "url": "https://example.test:bad/"},
            {"title": "<b>C++ map</b>", "url": "https://docs.example.test/map?utm_source=x",
             "description": "<b>Reference</b> " + "x" * 2000},
            {"title": "duplicate", "url": "https://docs.example.test/map"},
            {"title": "C++ map examples", "url": "https://docs.example.test/examples"},
        ]}})
        rows = await web._search_brave(query, 2)
        request = self.requests[0]
        self.assertEqual(request.url.host, "api.search.brave.com")
        self.assertEqual(request.url.params["q"], query)
        self.assertEqual(request.url.params["count"], "2")
        self.assertEqual(request.headers["X-Subscription-Token"], "fixture-only")
        self.assertNotIn("fixture-only", str(request.url))
        self.assertEqual([row.title for row in rows], ["C++ map", "C++ map examples"])
        self.assertLessEqual(len(rows[0].snippet), 900)
        self.assertNotIn("<b>", web.render_search_results(rows))

    async def test_brave_refuses_redirect_and_sanitizes_remote_error(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        for status in (302, 401, 429, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.handler = lambda _request: httpx.Response(status,
                    headers={"Location": "https://unrelated.example.test/"}, text="fixture-only")
                with self.assertRaisesRegex(web.WebError, f"HTTP {status}") as caught:
                    await web._search_brave("SQL JOIN", 1)
                self.assertNotIn("fixture-only", str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_brave_rejects_malformed_top_level_payloads(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        for data in ([], None, {"error": "nope"}, {"web": []}, {"web": {"results": "bad"}}):
            with self.subTest(data=data):
                self.handler = lambda _request: httpx.Response(200, text=json.dumps(data))
                with self.assertRaises(web.WebError):
                    await web._search_brave("SQL JOIN", 1)

    async def test_brave_invalid_json_is_a_provider_failure(self):
        self.enterContext(patch.dict(os.environ, {"WISP_BRAVE_SEARCH_API_KEY": "fixture-only"}))
        self.handler = lambda _request: httpx.Response(200, text="<html>upstream error</html>")
        with self.assertRaisesRegex(web.WebError, "invalid JSON"):
            await web._search_brave("SQL JOIN", 1)

    async def test_specialist_adapters_keep_valid_siblings(self):
        self.handler = lambda request: httpx.Response(200, json=(
            {"results": [None, "bad", {"id": "W1", "display_name": "Migraine prevention",
                "publication_year": 2025}, {"id": "W2", "display_name": []},
                {"id": "W3", "display_name": "Migraine trial"}]}
            if request.url.host == "api.openalex.org" else
            {"query": {"search": [None, {"title": {}}, {"title": "Paris", "snippet": "<b>City</b>"},
                                   {"title": "Paris"}, {"title": "Kyoto"}]}}))
        papers = await web._search_openalex("migraine", 2)
        wiki = await web._search_wikipedia("cities", 2)
        self.assertEqual([row.title for row in papers], ["Migraine prevention", "Migraine trial"])
        self.assertEqual([row.title for row in wiki], ["Paris", "Kyoto"])
        self.assertEqual(wiki[0].snippet, "City")


class ChatSearchTests(OfflineCase):
    async def test_independent_semantic_families_keep_route_and_time_contract(self):
        # Exercise the real dated-feed HTTP and output filter, not only a route
        # predicate: the wrong path can both narrow and broaden a requested range.
        from datetime import datetime, timezone
        from email.utils import format_datetime

        now = 1_800_000_000
        ages = ((300, "Five minute report"), (43200, "Twelve hour report"),
                (90000, "Twenty five hour report"), (108000, "Thirty hour report"),
                (129600, "Thirty six hour report"))
        xml = "<rss><channel>" + "".join(
            f"<item><title>{title}</title><link>https://publisher.example.test/{age}</link>"
            f"<pubDate>{format_datetime(datetime.fromtimestamp(now - age, timezone.utc))}</pubDate>"
            "</item>" for age, title in ages) + "</channel></rss>"

        def feed(request):
            self.assertEqual(request.url.host, "news.google.com")
            return httpx.Response(200, text=xml)

        self.handler = feed
        families = (
            ("WEB16-QUOTED-RANGE", False, (
                'latest news from "last week"', 'latest news from "yesterday"',
                'latest news for "the past 48 hours"', 'latest news as of "last Monday"')),
            ("WEB16-INTERVAL-UNITS", False, (
                "latest news from the past 48 hrs", "latest news from the past 48h",
                "latest news from the past 30 minutes", "latest news from the last hour",
                "latest news from the last 2 hrs")),
            ("WEB16-CURRENTYEAR-1", True, (
                "latest news today about the 2026 World Cup",
                "breaking news right now about Formula 1 2026",
                "latest news today about September Labs 2026")),
            ("WEB16-CURRENTTITLE-1", True, (
                "latest sports news from Monday Night Football",
                "latest entertainment news from Friday Night Lights",
                "latest football news from Sunday Ticket")),
            ("WEB16-CURRENTOPERATOR-1", True, (
                "latest news today site:docs.python.org", "latest news today site:history.com",
                "latest news today site:archive.org")),
            ("WEB16-PASTFORM-1", False, (
                "latest news from two days prior", "latest news from two days previously",
                "latest news over the previous 48 hrs", "latest news over the prior 2-day period",
                "latest news from the Monday before Labor Day")),
            ("WEB16-FRACTIONAL-DAY-1", False, (
                "latest news over the past day and a half", "latest news over the previous day and a half",
                'latest news for "the past day and a half"', "latest news in the last day and a half",
                "latest news over the last 24 hours and a half", "latest news from the past day and a half",
                "latest news for the previous day and a half", "latest news from the last 24 hours and a half",
                "latest news for past day and 1/2 day", 'latest news for "past day and 1/2 day"',
                "latest news over the past 1½ days", "latest news over the past 1 1/2 days",
                'latest news for "the past 1½ days"', "latest news over the past day and 1½ hours",
                "latest news over the past day and 1 1/2 hours")),
            ("WEB16-SYNTAX-PERIOD-1", False, (
                "latest news from Monday ", "latest news from Monday -sports",
                "latest news from Monday site:bbc.com", "latest news from Monday site:bbc.com?",
                "latest news from the past 48 hours\t", "latest news from the past 48 hours site:bbc.com",
                'latest news on "September 1" site:bbc.com', 'latest news from "last week" -sports',
                "latest news from yesterday -sports", "latest news from the past 30 minutes -sports",
                "latest news from Monday (site:bbc.com OR site:reuters.com)",
                "latest news from Monday lang:en", "latest news from Monday language:en",
                "latest news from Monday NOT site:example.test",
                "latest news from Monday NOT (site:bbc.com)",
                "latest news today NOT (NOT before:2020)",
                "latest news today NOT -before:2020")),
            ("WEB16-SYNTAX-PERIOD-1", True, (
                "latest news today NOT (before:2020)",)),
            ("WEB16-CURRENT-TOPIC-ON-1", True, (
                "latest news on API pricing today", "latest news on history museums today",
                "latest news on archive.org today")),
            ("WEB16-CURRENT-TOPIC-ON-1", False, (
                "guide on the latest Hacker News API",
                "information on the latest news API documentation",
                "advice on how to write news headlines today")),
        )
        for family, current, queries in families:
            for query in queries:
                with self.subTest(family=family, query=query):
                    self.requests.clear()
                    with patch.object(web_tools.time, "time", return_value=now), \
                         patch.object(web_tools, "search_web", AsyncMock(return_value=[
                             hit("Twenty five hour report", "twenty-five-hour-result"),
                             hit("Thirty hour report", "thirty-hour-result"),
                             hit("Thirty six hour report", "older-result")])) as general:
                        output = await web_tools.web_search(query)
                    if current:
                        general.assert_not_awaited()
                        self.assertEqual(len(self.requests), 1)
                        self.assertEqual(self.requests[0].url.params["q"], query + " when:1d")
                        self.assertIn("Five minute report", output)
                        self.assertIn("Twelve hour report", output)
                        self.assertNotIn("Twenty five hour report", output)
                        self.assertNotIn("Thirty hour report", output)
                        self.assertNotIn("Thirty six hour report", output)
                        self.assertIn("last 24 hours", output)
                    else:
                        general.assert_awaited_once_with(query, limit=6)
                        self.assertEqual(self.requests, [])
                        self.assertIn("Twenty five hour report", output)
                        self.assertIn("Thirty hour report", output)
                        self.assertIn("Thirty six hour report", output)
                        self.assertNotIn("last 24 hours", output)

    async def assert_news_route(self, query: str, *, current: bool):
        with patch.object(web_tools, "search_web", AsyncMock(return_value=[hit(query)])) as general, \
             patch.object(web_tools, "current_news", AsyncMock(return_value="DATED")) as dated:
            output = await web_tools.web_search(query)
        if current:
            dated.assert_awaited_once_with(query, limit=6)
            general.assert_not_awaited()
            self.assertEqual(output, "DATED")
        else:
            general.assert_awaited_once_with(query, limit=6)
            dated.assert_not_awaited()
            self.assertIn("URL: https://source.example.test/page", output)

    async def test_fixed_interval_spellings_have_the_same_duration(self):
        # Alias, spacing and marker choices do not change a duration. Only a
        # supported rolling day may acquire the actual feed's day filter.
        durations = ((24, ("hours", "hrs", "h"), True),
                     (48, ("hours", "hrs", "h"), False),
                     (30, ("minutes", "mins", "m"), False),
                     (1, ("hour", "hr", "h"), False),
                     (1, ("day", "d"), True), (2, ("days", "d"), False),
                     (1440, ("minutes", "min"), True), (86400, ("seconds", "s"), True))
        for marker in ("last", "past", "previous", "prior", "preceding"):
            for quantity, aliases, current in durations:
                for unit in aliases:
                    for separator in (" ", "", "-"):
                        query = f"latest news over the {marker} {quantity}{separator}{unit} about markets"
                        with self.subTest(query=query):
                            await self.assert_news_route(query, current=current)

    async def test_quoted_time_uses_the_same_range_grammar(self):
        values = (("yesterday", False), ("last week", False), ("last Monday", False),
                  ("the past 48 hours", False), ("previous 30 mins", False),
                  ("prior 2-day period", False), ("last hour", False),
                  ("past 24h", True), ("past 1440 minutes", True),
                  ("two days prior", False), ("two days previously", False))
        for frame in ("from", "for", "as of"):
            for value, current in values:
                for opening, closing in (("", ""), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
                    query = f"latest news {frame} {opening}{value}{closing} about markets"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=current)

    async def test_compound_or_conflicting_periods_do_not_collapse_to_one_day(self):
        for join in (" and ", " plus ", ", ", ", and ", "+"):
            for extra in ("2 hours", "one week"):
                for opening, closing in (("", ""), ('"', '"')):
                    query = f"latest news for {opening}past 1 day{join}{extra}{closing} about markets"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)
        for query in ("news today from Monday", "news today from 2026 about the World Cup",
                      "latest news past 24h and past 2 hours", 'news from "Past 24h" when:7d',
                      "latest news today about the 2026 World Cup from Monday",
                      'latest news from "Previous Week" from two days prior'):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=False)

    async def test_fractional_duration_continuations_share_the_compound_rule(self):
        for base in ("day", "1 day", "24 hours", "24h"):
            for tail in (" and a half", " and one half", " and half", " and another half",
                         " and a quarter", " and three quarters", " and 1/2", " and 0.5",
                         " and ½", " plus a half", "-and-a-half", "+1/2",
                         " and .5 hours", " and 1/2 hour", " and ½ hour",
                         " and 0.5 hours", " and a half hour"):
                for opening, closing in (("", ""), ('"', '"'), ("“", "”")):
                    query = f"latest news for {opening}the past {base}{tail}{closing}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)
        for value in ("past 1.5 days", "past one and a half days", "past one-and-a-half days"):
            with self.subTest(value=value):
                await self.assert_news_route(f"latest news for {value}", current=False)

    async def test_fractional_topic_words_do_not_extend_a_real_one_day_interval(self):
        for duration in ("day", "one day", "24 hours", "24h", "1440 minutes"):
            for topic in ("", " and a half-price sale", " and Half-Life",
                          " and a half price sale", " and the Half Moon festival"):
                query = f"latest news over the past {duration}{topic}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=True)
        for query in ('latest news today about "A Day and a Half"',
                      'latest news for "past 24 hours"', "latest news over the past day and 0.0"):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=True)

    async def test_search_syntax_and_whitespace_preserve_the_extracted_period(self):
        historical = ("latest news from Monday", "latest news from the past 48 hours",
                      "latest news from the past 30 minutes", 'latest news on "September 1"',
                      'latest news from "last week"', "latest news from yesterday")
        current = ("news", "latest news today", "latest news from Monday Night Football",
                   'latest news from "Previous Week"', 'latest news from "Monday"',
                   "latest news over the past 24h")
        suffixes = ("", " ", "\t", "\n", " -sports", " site:bbc.com",
                    " https://publisher.example.test", ' -"current events"',
                    " (site:bbc.com OR site:reuters.com)",
                    " ((site:bbc.com OR site:reuters.com))",
                    " (site:bbc.com AND (site:reuters.com OR -sports))")
        for expected, queries in ((False, historical), (True, current)):
            for base in queries:
                for suffix in suffixes:
                    for prefix in ("", "site:bbc.com "):
                        query = prefix + base + suffix
                        with self.subTest(query=query):
                            await self.assert_news_route(query, current=expected)
                with self.subTest(base=base, whitespace="internal"):
                    await self.assert_news_route(base.replace(" ", "\t"), current=expected)
        for suffix in (" (about markets)", " , about markets", " (from yesterday)"):
            with self.subTest(suffix=suffix):
                await self.assert_news_route("latest news from Monday" + suffix, current=False)

    async def test_topic_on_matches_about_after_real_dates_take_precedence(self):
        for topic in ("API pricing", "history museums", "archive.org"):
            for introducer in ("about", "on", "regarding"):
                for date, current in (("", True), (" on Monday", False), (" from 2020", False)):
                    query = f"latest news{date} {introducer} {topic} today"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=current)
        for query in ("Hacker News API docs", "latest news API docs", "history of BBC News"):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=False)

    async def test_news_topic_split_preserves_the_requested_format(self):
        templates = (
            ("guide {} the latest Hacker News API", False),
            ("information {} the latest news API documentation", False),
            ("advice {} how to write news headlines today", False),
            ("guide {} the latest news about API documentation", False),
            ("guide {} the latest world news", False),
            ("latest news {} API pricing today", True),
            ("latest headlines {} documentation costs today", True),
            ("latest news on Monday {} API pricing today", False),
        )
        for template, current in templates:
            for introducer in ("on", "about", "regarding"):
                query = template.format(introducer)
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=current)

    async def test_mixed_fraction_notation_keeps_the_complete_period(self):
        for quantity in ("1½", "1 ½", "1 1/2", "1-1/2", "1 1⁄2", "2¼", "2 3/4", ".5"):
            for value in (f"past {quantity} days", f"past day and {quantity} hours",
                          f"past day and {quantity}"):
                for opening, closing in (("", ""), ('"', '"'), ("“", "”")):
                    query = f"latest news for {opening}{value}{closing}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)
        for query in ("latest news over the past 1 day", "latest news over the past 24h",
                      "latest news over the past 1440 minutes",
                      "latest news about a 1½-day festival", "latest news about a 1 1/2-day festival",
                      "latest news over the past 24h and a 1½-hour documentary",
                      "latest news over the past 24h and a 1 1/2-hour documentary"):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=True)

    async def test_language_and_negated_filters_preserve_period_and_polarity(self):
        for base, current in (("latest news from Monday", False),
                              ("latest news from the past 48 hours", False),
                              ('latest news on "September 1"', False), ("news", True),
                              ("latest news today", True), ('latest news from "Previous Week"', True)):
            for syntax in ("lang:en", "language:en", 'language:"en-US"',
                           "NOT site:example.test", "NOT lang:en", "(NOT site:example.test)",
                           "NOT (site:example.test)", "NOT ((lang:en))",
                           "NOT (site:example.test OR language:en)"):
                for query in (f"{base} {syntax}", f"{syntax} {base}"):
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=current)
        for operator in ("before:2020", "after:2020", "when:7d"):
            for syntax, current in ((operator, False), (f"NOT {operator}", True),
                                    (f"-{operator}", True), (f"(NOT {operator})", True),
                                    (f"NOT NOT {operator}", False), (f'"NOT {operator}"', True),
                                    (f"NOT ({operator})", True), (f"NOT (({operator}))", True),
                                    (f"NOT (NOT {operator})", False), (f"NOT -{operator}", False),
                                    (f"-({operator})", True), (f"-(-{operator})", False)):
                query = f"latest news today {syntax}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=current)

    async def test_weekday_source_nouns_and_temporal_clauses_remain_distinct(self):
        for weekday in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            for source_tail in (" Ticket", " Night Orchestra", " Morning Bulletin"):
                for suffix in ("", " about markets", " today site:history.com"):
                    query = f"latest news from {weekday}{source_tail}{suffix}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=True)
            for temporal_tail in (" night", " morning about markets", " before Labor Day"):
                query = f"latest news from the {weekday}{temporal_tail}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=False)

    async def test_year_topics_and_framed_years_have_separate_roles(self):
        for year in ("1730", "2026", "2100"):
            for topic in (f"the {year} World Cup", f"Formula 1 {year}", f"September Labs {year}"):
                query = f"latest news today about {topic}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=True)
            for frame in ("in", "from", "during", "as of"):
                for opening, closing in (("", ""), ('"', '"')):
                    query = f"latest news today {frame} {opening}{year}{closing} about markets"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)

    async def test_search_payloads_do_not_supply_prose_intent(self):
        for payload in ("site:docs.python.org", "site:history.com", "site:archive.org",
                        'site:"docs.python.org"', "intitle:yesterday", 'intitle:"last week"',
                        "inurl:2026", "https://history.example.test/archive",
                        '-"last week"', "-when:7d", '"when:7d"'):
            for suffix, current in (("", True), (" from Monday", False)):
                query = f"latest news today {payload}{suffix}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=current)
        for query in ("software updates intitle:news", "software updates https://news.example.test"):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=False)

    async def test_quoted_source_ambiguity_has_narrow_precedence(self):
        for value in ("Previous Week", "Prior Two Days", "The Preceding Week"):
            for frame, title, current in (("from", value, True), ("from", value.lower(), False),
                                          ("for", value, False), ("as of", value, False)):
                query = f'latest news {frame} "{title}"'
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=current)
        for value in ("Prior 2 Days", "Two Days Ago", "Two Days Prior", "Last Monday"):
            query = f'latest news from "{value}"'
            with self.subTest(query=query):
                await self.assert_news_route(query, current=False)

    async def test_extraction_review_neighbors_preserve_time_and_operator_context(self):
        for query in ('latest news over "the past 48 hours"', 'latest news since "yesterday"',
                      'latest news on "Monday"', 'latest news as of "Monday morning"',
                      'latest news within "the last 2 hours"', 'latest news between "2020" and "2021"',
                      "latest news today (after:2020-01-01)", "latest news from last week please",
                      "latest news from yesterday please", "latest news from 2020 please",
                      "latest news from the past 48 hours worldwide", "latest news in September 2025"):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=False)
        query = "latest news today (site:history.com OR site:archive.org)"
        with self.subTest(query=query):
            await self.assert_news_route(query, current=True)

    async def test_weekday_topic_suffixes_do_not_change_the_requested_period(self):
        # WEB16-PASTTIME-2: appending an independent topic/region/filter clause
        # must not change the routing selected for the framed weekday alone.
        suffixes = ("", " about markets", " regarding elections", " covering energy",
                    " focused on local schools", " with coverage of central banks", " on the election",
                    ' in India site:bbc.com -sports "coal price"')
        for weekday in ("Monday", "Tuesday", "Friday", "Sunday", "Mon", "Fri", "Sun"):
            for frame in ("from", "on", "as of"):
                for suffix in suffixes:
                    query = f"latest news {frame} {weekday}{suffix}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)

    async def test_rolling_interval_markers_share_one_range_contract(self):
        # WEB16-PASTRANGE-1: marker synonyms and topic suffixes preserve the
        # explicit interval; the dedicated feed can honor only a rolling day.
        periods = (("2 days", False), ("two days", False), ("48 hours", False),
                   ("1.5 days", False), ("week", False), ("two weeks", False),
                   ("quarter", False), ("2 quarters", False), ("fortnight", False),
                   ("two fortnights", False), ("weekend", False), ("3 weekends", False),
                   ("24 hours", True), ("twenty-four hours", True), ("one day", True))
        for marker in ("last", "past", "previous", "prior", "preceding"):
            for period, current in periods:
                for suffix in ("", " about markets", " in India -sports", " about Days Gone"):
                    query = f"latest news over the {marker} {period}{suffix}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=current)

    async def test_topic_qualifiers_keep_current_and_named_source_routes(self):
        for source in ("The Sun", "Sun Microsystems", "Monday.com", "Monday’s product team",
                       '"Monday"', '"Prior Two Days"', '"Previous Week"', '"The Preceding Week"'):
            for suffix in ("", " about markets", " regarding product releases"):
                query = f"latest news from {source}{suffix}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=True)

    async def test_framed_quoted_temporal_values_preserve_the_requested_period(self):
        values = (("on", "September 1"), ("dated", "Sept. 1st"),
                  ("as of", "1 September"), ("on", "09/01"),
                  ("on", "September 1, 2026"), ("on", "September 1 2026"),
                  ("dated", "1 September 2025"), ("as of", "1 September,2025"),
                  ("from", "two days ago"), ("from", "48 hours ago"),
                  ("as of", "a week ago"))
        for frame, value in values:
            for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
                for suffix in ("", " about markets", " in India -sports"):
                    query = f"latest news {frame} {opening}{value}{closing}{suffix}"
                    with self.subTest(query=query):
                        await self.assert_news_route(query, current=False)

    async def test_explicit_range_operators_never_get_an_appended_day_filter(self):
        for operator in ("when:7d", "when:1d", "when:1m", "before:2026-09-01", "after:2026-08-01"):
            for suffix in ("", ' site:bbc.com -sports "coal price"'):
                query = f"latest news {operator}{suffix}"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=False)

    async def test_quoted_titles_and_excluded_terms_are_not_positive_intent(self):
        for query in ('latest news about "Last Week Tonight" today',
                      "latest news today -history -archives",
                      'latest news today -history about "Last Week Tonight"',
                      "latest news about 'The Prior Week' today",
                      'latest news about "September 1" today',
                      'latest news about "Two Days Ago" today'):
            with self.subTest(query=query):
                await self.assert_news_route(query, current=True)
        for title in ("September 1, 2026", "September 1 2026", "1 September 2025", "1 September,2025"):
            for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
                query = f"latest news about {opening}{title}{closing} today"
                with self.subTest(query=query):
                    await self.assert_news_route(query, current=True)
        with self.subTest(query="latest software updates -news"):
            await self.assert_news_route("latest software updates -news", current=False)

    async def test_explicit_past_points_and_calendar_dates_keep_general_search(self):
        # WEB16-PASTTIME-1: these refer to a requested period/point, not a
        # rolling day. Keep the exact query, including any region/operators.
        queries = (
            "latest news from two days ago",
            "latest headlines 48 hours ago",
            "current election news three weeks ago",
            "latest news on September 1",
            "latest world news from Monday",
            "latest headlines a week ago",
            "latest news two hours ago",
            "latest news from a couple of months ago",
            "latest news from two-and-a-half days ago",
            "latest headlines 48h ago",
            "latest headlines 1.5 days ago",
            "latest headlines three weeks earlier",
            "latest news from a few months back",
            "current news from Sept. 1st",
            "latest news dated 1 September",
            "latest headlines as of the 1st of September",
            "latest news before September 2",
            "latest headlines September 1",
            "latest news on 09/01",
            "latest news dated 1-9",
            "latest headlines on Tuesday",
            "latest world news as of Friday",
            "latest news from Mon.",
            "latest news from Monday morning",
            "latest headlines from Tuesday in India site:bbc.com -sports",
            'latest news from two days ago in India site:bbc.com -sports "coal price"',
        )
        for query in queries:
            with self.subTest(query=query):
                with patch.object(web_tools, "search_web", AsyncMock(return_value=[hit(query)])) as general, \
                     patch.object(web_tools, "current_news", AsyncMock(return_value="WRONG 24H ROUTE")) as dated:
                    output = await web_tools.web_search(query)
                general.assert_awaited_once_with(query, limit=6)
                dated.assert_not_awaited()
                self.assertIn("URL: https://source.example.test/page", output)

    async def test_news_as_a_subject_keeps_general_search(self):
        for query in ("Hacker News API docs", "build a Hacker News clone", "history of BBC News",
                      "newspaper headlines in 1945", "how to write headlines", "latest news API docs",
                      "news coverage of the 2008 financial crisis", 'site:bbc.com news archives',
                      "latest news this week", "latest headlines from yesterday",
                      "latest science news in the past 7 days", "latest world news since June",
                      "latest news after:2025-01-01", "latest news from last Tuesday",
                      "latest news from the past two weeks", "latest news from last seven days",
                      "latest news from the past couple of days"):
            with self.subTest(query=query):
                with patch.object(web_tools, "search_web", AsyncMock(return_value=[hit(query)])) as general, \
                     patch.object(web_tools, "current_news", AsyncMock(return_value="WRONG ROUTE")) as news:
                    output = await web_tools.web_search(query)
                general.assert_awaited_once_with(query, limit=6)
                news.assert_not_awaited()
                self.assertIn("URL: https://source.example.test/page", output)

    async def test_current_news_keeps_the_strict_dated_route(self):
        for query in ("world news today", "latest science news", "stock market news today in India",
                      "latest headlines", "breaking election news", "news", "world news",
                      "latest news: India stock market", 'latest news about "Example Corp"',
                      "science news from the last 24 hours",
                      "latest news today in India", "latest news about May Company",
                      "latest news about Monday.com", 'latest news from "Monday.com"',
                      "latest news on Monday.com", "latest news from Monday’s product team",
                      'latest news about "Two Days Ago"', "latest news about 'Two Days Ago'",
                      "latest news about “Two Days Ago”", "latest news about March Madness",
                      "latest news from The Sun", "latest headlines from Sun Microsystems",
                      "science news from the last twenty-four hours",
                      "science news from the past one day"):
            with self.subTest(query=query):
                with patch.object(web_tools, "search_web", AsyncMock()) as general, \
                     patch.object(web_tools, "current_news", AsyncMock(return_value="DATED")) as news:
                    self.assertEqual(await web_tools.web_search(query), "DATED")
                news.assert_awaited_once_with(query, limit=6)
                general.assert_not_awaited()

    async def test_current_news_failure_never_substitutes_undated_results(self):
        with patch.object(web_tools, "current_news", AsyncMock(side_effect=web.WebError("no dated feed"))), \
             patch.object(web_tools, "search_web", AsyncMock()) as general:
            output = await web_tools.web_search("news today")
        self.assertIn("web search failed", output)
        general.assert_not_awaited()

    async def test_news_feed_preserves_full_user_query(self):
        query = "stock market news today in India -cryptocurrency"
        self.handler = lambda _request: httpx.Response(200, text="<rss><channel/></rss>")
        await web_tools.current_news(query)
        self.assertEqual(self.requests[0].url.params["q"], query + " when:1d")

    async def test_chat_exposes_background_only_coverage(self):
        rows = web.SearchResults([hit("Paris")], coverage_note="Encyclopedia background only.")
        with patch.object(web_tools, "search_web", AsyncMock(return_value=rows)):
            output = await web_tools.web_search("Paris")
        self.assertTrue(output.startswith("Encyclopedia background only."))


if __name__ == "__main__":
    unittest.main()
