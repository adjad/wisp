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
                      "science news from the last 24 hours"):
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
