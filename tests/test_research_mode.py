"""Deterministic research-mode tests; no live model or network required."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.research import cache as research_cache
from service.research import coverage as coverage_lib
from service.research import rank as rank_lib
from service.research.citations import critic_verify
from service.research.orchestrator import (ResearchManager, _annotate_empty_sections,
                                           _fallback_plan, _normalize_plan,
                                           _select_research_model)
from service.research.store import ResearchStore
from service.research.web import (_BingParser, _DDGParser, _ReadableHTML, _bing_target,
                                  _ddg_target, _wikipedia_title_from_url, Page, SearchHit,
                                  WebError, canonicalize_url, fetch_page, validate_public_url)
from service.research import web as web_module


class WebParsingTests(unittest.TestCase):
    def test_canonicalization_drops_tracking_and_fragment(self):
        self.assertEqual(
            canonicalize_url("HTTPS://Example.COM/a//b?utm_source=x&id=4#part"),
            "https://example.com/a/b?id=4")

    def test_duckduckgo_results_are_structured(self):
        parser = _DDGParser()
        parser.feed('''
          <div class="result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpaper">A Paper</a>
            <a class="result__snippet">The useful result snippet.</a>
          </div>''')
        self.assertEqual(len(parser.results), 1)
        self.assertEqual(parser.results[0]["title"], "A Paper")
        self.assertEqual(parser.results[0]["snippet"], "The useful result snippet.")
        self.assertEqual(_ddg_target(parser.results[0]["href"]), "https://example.com/paper")

    def test_bing_fallback_results_are_structured(self):
        parser = _BingParser()
        parser.feed('''<ol><li class="b_algo"><h2><a href="https://example.org/study">
          Independent Study</a></h2><div class="b_caption"><p>Measured findings.</p></div></li></ol>''')
        self.assertEqual(parser.results, [{"title": "Independent Study",
            "href": "https://example.org/study", "snippet": "Measured findings."}])

    def test_bing_tracking_url_is_decoded_before_ranking(self):
        wrapped = ("https://www.bing.com/ck/a?u="
                   "a1aHR0cHM6Ly9vcm5pdGguYWkvb3JuaXRoXzFfNS5odG1s&ntb=1")
        self.assertEqual(_bing_target(wrapped), "https://ornith.ai/ornith_1_5.html")

    def test_html_extractor_drops_scripts_and_navigation(self):
        parser = _ReadableHTML()
        parser.feed('''<html><head><title>Real title</title><script>IGNORE_ME()</script></head>
          <body><nav>Menu junk</nav><article><h1>Finding</h1>
          <p>The measured result was 42 percent in 2026.</p></article></body></html>''')
        text = parser.text()
        self.assertEqual(parser.title, "Real title")
        self.assertIn("42 percent", text)
        self.assertNotIn("IGNORE_ME", text)
        self.assertNotIn("Menu junk", text)

    def test_private_targets_are_rejected_before_fetch(self):
        for url in ("http://127.0.0.1/admin", "http://10.0.0.4/", "http://[::1]/"):
            with self.assertRaises(Exception):
                asyncio.run(validate_public_url(url))


class _FakeResearchClient:
    def __init__(self):
        self.exclusive_loads: list[bool] = []

    async def ensure_only(self, _model: str, *, exclusive: bool = False, **_kwargs):
        self.exclusive_loads.append(exclusive)

    async def models(self):
        return ["Huihui-Ornith-1.5-9B-abliterated-oQ6e"]

    async def chat(self, _model: str, messages: list[dict], **_kwargs):
        system = messages[0]["content"]
        user = messages[1]["content"]
        if system.startswith("Create a compact research plan"):
            content = ('{"title":"Test report","objective":"Verify two findings",'
                       '"subquestions":[{"question":"First finding?"},'
                       '{"question":"Second finding?"}]}')
        elif system.startswith("Generate precise public-web search queries"):
            content = ('[{"subquestion_id":"Q1","query":"first official finding"},'
                       '{"subquestion_id":"Q2","query":"second official finding"}]')
        elif system.startswith("Extract atomic evidence"):
            if '"source_id": "S1"' in user:
                content = ('[{"subquestion_id":"Q1","claim":"First finding is verified.",'
                           '"quote":"The first verified finding is forty two percent.",'
                           '"stance":"supports","confidence":0.9}]')
            else:
                content = ('[{"subquestion_id":"Q2","claim":"Second finding is verified.",'
                           '"quote":"The second verified finding is independently confirmed.",'
                           '"stance":"supports","confidence":0.9}]')
        else:
            content = ("# Test report\n\nFirst finding is verified [E:1].\n\n"
                       "Second finding is verified [E:2].")
        return {"choices": [{"message": {"content": content}}]}


class ResearchStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.tmp.name) / "research.db")
        self.manager = ResearchManager(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_job_source_evidence_and_report_citations(self):
        plan = _fallback_plan("Test a claim", "quick")
        job = self.store.create_job("Test a claim", plan, state="running")
        jid = job["id"]
        sid = self.store.add_source(jid, url="https://example.com/a",
            canonical_url="https://example.com/a", title="Primary source",
            domain="example.com", score=1.0)
        self.store.update_source(jid, sid, status="read", body="The measured value was 42 percent.")
        eid = self.store.add_evidence(jid, source_id=sid, subquestion_id="Q1",
            claim="The measured value was 42 percent.", quote="The measured value was 42 percent.",
            start_offset=0, end_offset=40, confidence=0.9)
        self.assertEqual(eid, "E1")
        client = _FakeResearchClient()
        report, metadata = await self.manager._render_report(client, jid,
            "# Result\n\nThe measured value was 42 percent [E:1].\n\n"
            "This unsupported sentence has no evidence.\n\nA fabricated marker [E:99].",
            plan)
        self.assertIn("[1]", report)
        self.assertNotIn("E:99", report)
        self.assertNotIn("unsupported sentence", report)
        self.assertNotIn("fabricated marker", report)
        self.assertEqual(metadata["sources"][0]["quote"], "The measured value was 42 percent.")

        report, _ = await self.manager._render_report(client, jid,
            "# Result\n\nThe measured value was 999 percent [E:1].\n\n"
            "Unrelated audio and vision modalities are supported [E:1].",
            plan)
        self.assertNotIn("999", report)
        self.assertNotIn("audio", report)

    async def test_event_cursor_is_replayable(self):
        job = self.store.create_job("x", _fallback_plan("x", "quick"))
        self.store.event(job["id"], "status", {"text": "one"})
        first = self.store.events_after(job["id"], 0)
        later = self.store.events_after(job["id"], first[-1]["seq"])
        self.assertEqual(later, [])

    def test_stale_ornith_alias_resolves_to_live_abliterated_oq6e(self):
        selected = _select_research_model(
            "Huihui-Ornith-1.5-9B-abliterated-mlx-8Bit",
            ["Ling-3.0-tiny-oQ4e", "Huihui-Ornith-1.5-9B-abliterated-oQ6e"])
        self.assertEqual(selected, "Huihui-Ornith-1.5-9B-abliterated-oQ6e")


class ResearchPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.tmp.name) / "research.db")
        self.manager = ResearchManager(self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_bounded_pipeline_builds_verified_report(self):
        client = _FakeResearchClient()

        async def fake_search(query: str, *, limit: int = 8):
            n = 1 if "first" in query else 2
            return [SearchHit(title=f"{query} — Source {n}", url=f"https://example{n}.com/a",
                domain=f"example{n}.com", rank=1, query=query)]

        async def fake_fetch(url: str):
            first = "example1" in url
            text = ("The first verified finding is forty two percent. Supporting context for testing."
                    if first else
                    "The second verified finding is independently confirmed. Supporting context for testing.")
            return Page(url=url, canonical_url=url, title="Source", text=text,
                        content_type="text/html", published_at="2026-08-25")

        with patch("service.research.orchestrator.search_web", fake_search), \
             patch("service.research.orchestrator.fetch_page", fake_fetch):
            created = await self.manager.create(client, "Verify two findings", depth="quick")
            jid = created["id"]
            self.manager.start(jid, client)
            await self.manager._tasks[jid]

        result = self.manager.detail(jid)
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["evidence_count"], 2)
        self.assertIn("First finding is verified [1]", result["report_md"])
        self.assertIn("Second finding is verified [2]", result["report_md"])
        self.assertEqual(len(result["report"]["sources"]), 2)
        self.assertTrue(client.exclusive_loads)
        self.assertTrue(all(client.exclusive_loads))


class SourceQualityTests(unittest.TestCase):
    def test_gov_domain_is_official(self):
        cls, reason = rank_lib.classify_source(url="https://cdc.gov/x", domain="cdc.gov", title="Guidance")
        self.assertEqual(cls, "official")

    def test_arxiv_is_academic(self):
        cls, _ = rank_lib.classify_source(url="https://arxiv.org/abs/1", domain="arxiv.org", title="A Paper")
        self.assertEqual(cls, "academic")

    def test_reddit_is_community(self):
        cls, _ = rank_lib.classify_source(url="https://reddit.com/r/x", domain="reddit.com", title="Thread")
        self.assertEqual(cls, "community")

    def test_ordinary_publisher_is_reputable_secondary(self):
        cls, _ = rank_lib.classify_source(url="https://example-news.com/a", domain="example-news.com",
                                          title="A real headline about something")
        self.assertEqual(cls, "reputable_secondary")


class CoverageAndContradictionTests(unittest.TestCase):
    def _plan(self):
        return _fallback_plan("test", "quick")

    def test_two_domains_meet_coverage_one_domain_does_not(self):
        plan = self._plan()
        rows = [
            {"subquestion_id": "Q1", "domain": "a.com", "source_id": "S1", "quality_class": "", "stance": "supports"},
            {"subquestion_id": "Q1", "domain": "b.com", "source_id": "S2", "quality_class": "", "stance": "supports"},
            {"subquestion_id": "Q2", "domain": "a.com", "source_id": "S1", "quality_class": "", "stance": "supports"},
        ]
        cov = coverage_lib.coverage(rows, plan)
        self.assertTrue(cov["Q1"]["met"])
        self.assertFalse(cov["Q2"]["met"])
        self.assertEqual(cov["Q3"]["independent_sources"], 0)

    def test_single_official_source_satisfies_coverage(self):
        plan = self._plan()
        rows = [{"subquestion_id": "Q1", "domain": "cdc.gov", "source_id": "S1",
                "quality_class": "official", "stance": "supports"}]
        cov = coverage_lib.coverage(rows, plan)
        self.assertTrue(cov["Q1"]["met"])

    def test_opposing_stance_is_flagged_as_contradiction(self):
        rows = [
            {"evidence_id": "E1", "subquestion_id": "Q1", "source_id": "S1",
             "quote": "The product launched in May 2026.", "stance": "supports"},
            {"evidence_id": "E2", "subquestion_id": "Q1", "source_id": "S2",
             "quote": "The product has not launched as of 2026.", "stance": "contradicts"},
        ]
        found = coverage_lib.detect_contradictions(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subquestion_id"], "Q1")

    def test_conflicting_numbers_over_overlapping_claim_are_flagged(self):
        rows = [
            {"evidence_id": "E1", "subquestion_id": "Q1", "source_id": "S1",
             "quote": "The measured latency was 42 milliseconds in testing.", "stance": "supports"},
            {"evidence_id": "E2", "subquestion_id": "Q1", "source_id": "S2",
             "quote": "The measured latency was 99 milliseconds in testing.", "stance": "supports"},
        ]
        found = coverage_lib.detect_contradictions(rows)
        self.assertEqual(len(found), 1)

    def test_same_source_pairs_are_never_flagged(self):
        rows = [
            {"evidence_id": "E1", "subquestion_id": "Q1", "source_id": "S1",
             "quote": "It was 42 percent.", "stance": "supports"},
            {"evidence_id": "E2", "subquestion_id": "Q1", "source_id": "S1",
             "quote": "It was 99 percent.", "stance": "supports"},
        ]
        self.assertEqual(coverage_lib.detect_contradictions(rows), [])


class CriticVerifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_labels_pass_through(self):
        async def call_json(_system, _user):
            return [{"index": 0, "label": "supported"}, {"index": 1, "label": "contradicted"}]
        labels = await critic_verify(call_json, [
            {"index": 0, "line": "a", "quotes": ["q"]}, {"index": 1, "line": "b", "quotes": ["q"]}])
        self.assertEqual(labels, {0: "supported", 1: "contradicted"})

    async def test_invalid_label_is_dropped(self):
        async def call_json(_system, _user):
            return [{"index": 0, "label": "definitely-true"}]
        labels = await critic_verify(call_json, [{"index": 0, "line": "a", "quotes": ["q"]}])
        self.assertEqual(labels, {})

    async def test_model_failure_returns_empty(self):
        async def call_json(_system, _user):
            raise RuntimeError("boom")
        labels = await critic_verify(call_json, [{"index": 0, "line": "a", "quotes": ["q"]}])
        self.assertEqual(labels, {})

    async def test_empty_claims_short_circuits_without_a_call(self):
        called = False
        async def call_json(_system, _user):
            nonlocal called
            called = True
            return []
        labels = await critic_verify(call_json, [])
        self.assertEqual(labels, {})
        self.assertFalse(called)


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ResearchStore(Path(self.tmp.name) / "research.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_chunks_round_trip(self):
        job = self.store.create_job("x", _fallback_plan("x", "quick"))
        sid = self.store.add_source(job["id"], url="https://a.com", canonical_url="https://a.com")
        self.store.add_chunk(job["id"], sid, "C1", start_offset=0, end_offset=10, text="0123456789", score=1.5)
        rows = self.store.chunks(job["id"], sid)
        self.assertEqual(rows[0]["text"], "0123456789")
        self.assertEqual(rows[0]["end_offset"], 10)

    def test_contradictions_round_trip_and_clear(self):
        job = self.store.create_job("x", _fallback_plan("x", "quick"))
        self.store.add_contradiction(job["id"], subquestion_id="Q1",
            evidence_id_a="E1", evidence_id_b="E2", description="conflict")
        self.assertEqual(len(self.store.contradictions(job["id"])), 1)
        self.store.clear_contradictions(job["id"])
        self.assertEqual(self.store.contradictions(job["id"]), [])

    def test_pin_prevents_purge_of_stale_bodies(self):
        job = self.store.create_job("x", _fallback_plan("x", "quick"))
        jid = job["id"]
        sid = self.store.add_source(jid, url="https://a.com", canonical_url="https://a.com")
        self.store.update_source(jid, sid, status="read", body="some text")
        # update_job doesn't allow backdating updated_at directly (it always
        # stamps "now"); simulate an old, stale job via raw SQL instead.
        self.store._db.execute("UPDATE research_jobs SET updated_at=0 WHERE id=?", (jid,))
        self.store._db.commit()
        self.store.set_pinned(jid, True)
        self.store.purge_stale_bodies(older_than_days=0)
        self.assertEqual(self.store.sources(jid)[0]["body"], "some text")
        self.store.set_pinned(jid, False)
        removed = self.store.purge_stale_bodies(older_than_days=0)
        self.assertEqual(removed, 1)
        self.assertEqual(self.store.sources(jid)[0]["body"], "")

    def test_delete_job_removes_all_derived_rows(self):
        job = self.store.create_job("x", _fallback_plan("x", "quick"))
        jid = job["id"]
        sid = self.store.add_source(jid, url="https://a.com", canonical_url="https://a.com")
        self.store.add_evidence(jid, source_id=sid, subquestion_id="Q1", claim="c", quote="q" * 25,
                                start_offset=0, end_offset=25)
        self.store.delete_job(jid)
        self.assertIsNone(self.store.get_job(jid))
        self.assertEqual(self.store.sources(jid), [])
        self.assertEqual(self.store.evidence(jid), [])

    def test_migration_adds_columns_to_a_preexisting_database(self):
        path = Path(self.tmp.name) / "legacy.db"
        import sqlite3
        legacy = sqlite3.connect(str(path))
        legacy.execute("""CREATE TABLE research_jobs (
            id TEXT PRIMARY KEY, prompt TEXT NOT NULL, state TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}', steering TEXT NOT NULL DEFAULT '',
            report_md TEXT NOT NULL DEFAULT '', report_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
            pause_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        legacy.execute("INSERT INTO research_jobs (id,prompt,state,created_at,updated_at) VALUES "
                      "('j1','p','running',0,0)")
        legacy.commit()
        legacy.close()
        migrated = ResearchStore(path)
        job = migrated.get_job("j1")
        self.assertEqual(job["stop_reason"], "")
        self.assertFalse(job["pinned"])
        self.assertEqual(job["model_calls"], 0)
        migrated.close()


class RecoveryTests(unittest.TestCase):
    def test_orphaned_running_job_is_marked_paused_on_startup(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = ResearchStore(Path(tmp.name) / "research.db")
            job = store.create_job("x", _fallback_plan("x", "quick"), state="running")
            store.update_job(job["id"], state="running")
            ResearchManager(store)  # constructing the manager sweeps orphaned jobs
            self.assertEqual(store.get_job(job["id"])["state"], "paused")
            store.close()
        finally:
            tmp.cleanup()


class FetchCacheTests(unittest.TestCase):
    def test_put_then_get_round_trips_within_ttl(self):
        with patch.object(research_cache, "CACHE_DIR", Path(tempfile.mkdtemp())):
            research_cache.put("https://a.com/x", url="https://a.com/x", title="T",
                               text="body text", content_type="text/html", published_at="2026-01-01")
            hit = research_cache.get("https://a.com/x")
            self.assertIsNotNone(hit)
            self.assertEqual(hit["text"], "body text")

    def test_miss_for_unknown_url(self):
        with patch.object(research_cache, "CACHE_DIR", Path(tempfile.mkdtemp())):
            self.assertIsNone(research_cache.get("https://never-cached.com"))


class DomainEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_mid_run_domain_edit_persists_on_plan(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = ResearchStore(Path(tmp.name) / "research.db")
            manager = ResearchManager(store)
            client = _FakeResearchClient()
            created = await manager.create(client, "Some topic", depth="quick")
            jid = created["id"]
            updated = manager.update_domains(jid, blocked=["Spam.example.com"])
            self.assertIn("spam.example.com", updated["plan"]["blocked_domains"])
            store.close()
        finally:
            tmp.cleanup()


class WaybackFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_403_falls_back_to_an_available_snapshot(self):
        async def direct(url):
            if url == "https://blocked.example/a":
                raise WebError("HTTP 403")
            return Page(url=url, canonical_url=url, title="Archived copy",
                       text="The archived body text.", content_type="text/html")

        async def snapshot(url):
            self.assertEqual(url, "https://blocked.example/a")
            return "https://web.archive.org/web/2026/https://blocked.example/a"

        with patch.object(web_module, "_fetch_direct", direct), \
             patch.object(web_module, "_wayback_snapshot_url", snapshot):
            page = await fetch_page("https://blocked.example/a")
        self.assertTrue(page.via_archive)
        self.assertEqual(page.url, "https://blocked.example/a")
        self.assertEqual(page.text, "The archived body text.")

    async def test_no_snapshot_reraises_original_error(self):
        async def direct(url):
            raise WebError("HTTP 404")

        async def snapshot(url):
            return ""

        with patch.object(web_module, "_fetch_direct", direct), \
             patch.object(web_module, "_wayback_snapshot_url", snapshot):
            with self.assertRaisesRegex(WebError, "HTTP 404"):
                await fetch_page("https://gone.example/a")

    async def test_ssrf_refusal_never_triggers_a_wayback_lookup(self):
        called = False

        async def direct(url):
            raise WebError("local and private-network URLs are blocked")

        async def snapshot(url):
            nonlocal called
            called = True
            return "https://web.archive.org/web/2026/http://127.0.0.1/admin"

        with patch.object(web_module, "_fetch_direct", direct), \
             patch.object(web_module, "_wayback_snapshot_url", snapshot):
            with self.assertRaises(WebError):
                await fetch_page("http://127.0.0.1/admin")
        self.assertFalse(called)

    async def test_wayback_lookup_itself_never_reaches_the_network_in_this_test(self):
        # A real network call here would make this test flaky/offline-hostile;
        # confirm the lookup function degrades to "" on any failure rather than
        # raising, since fetch_page relies on that to fall through cleanly.
        with patch("httpx.AsyncClient.get", side_effect=RuntimeError("no network in tests")):
            result = await web_module._wayback_snapshot_url("https://example.com/x")
        self.assertEqual(result, "")


class WikipediaFetchTests(unittest.IsolatedAsyncioTestCase):
    def test_ordinary_article_url_parses_to_lang_and_title(self):
        self.assertEqual(_wikipedia_title_from_url("https://en.wikipedia.org/wiki/Cancer_vaccine"),
                         ("en", "Cancer vaccine"))

    def test_mobile_and_non_english_subdomains_are_recognized(self):
        self.assertEqual(_wikipedia_title_from_url("https://en.m.wikipedia.org/wiki/Cancer"),
                         ("en", "Cancer"))
        self.assertEqual(_wikipedia_title_from_url("https://fr.wikipedia.org/wiki/Vaccin"),
                         ("fr", "Vaccin"))

    def test_percent_encoded_titles_are_decoded(self):
        self.assertEqual(_wikipedia_title_from_url("https://en.wikipedia.org/wiki/S%C3%A3o_Paulo"),
                         ("en", "São Paulo"))

    def test_non_article_paths_are_not_routed_through_the_api(self):
        self.assertIsNone(_wikipedia_title_from_url("https://en.wikipedia.org/w/index.php?title=X"))
        self.assertIsNone(_wikipedia_title_from_url("https://example.com/wiki/Not_wikipedia"))

    async def test_fetch_page_routes_wikipedia_urls_through_the_api(self):
        async def fake_article(lang, title):
            self.assertEqual((lang, title), ("en", "Cancer vaccine"))
            return Page(url="https://en.wikipedia.org/wiki/Cancer_vaccine",
                       canonical_url="https://en.wikipedia.org/wiki/Cancer_vaccine",
                       title="Cancer vaccine", text="A cancer vaccine is a vaccine that treats cancer.",
                       content_type="text/plain")

        async def unexpected(url):
            raise AssertionError("should not scrape the HTML page when the API succeeds")

        with patch.object(web_module, "_fetch_wikipedia_article", fake_article), \
             patch.object(web_module, "_fetch_direct", unexpected):
            page = await fetch_page("https://en.wikipedia.org/wiki/Cancer_vaccine")
        self.assertIn("cancer vaccine", page.text.lower())

    async def test_missing_article_falls_back_to_the_ordinary_fetch_path(self):
        async def no_article(lang, title):
            return None

        async def direct(url):
            return Page(url=url, canonical_url=url, title="Fallback", text="Fallback body text here.",
                       content_type="text/html")

        with patch.object(web_module, "_fetch_wikipedia_article", no_article), \
             patch.object(web_module, "_fetch_direct", direct):
            page = await fetch_page("https://en.wikipedia.org/wiki/Nonexistent_Article_Xyz")
        self.assertEqual(page.title, "Fallback")


class PlanFallbackLeniencyTests(unittest.TestCase):
    """Regression coverage for a real bad run: a real 'cancer vaccinations'
    query produced a report whose section headers were the generic fallback
    template verbatim, because one malformed subquestion in the model's plan
    JSON discarded every other good one instead of just being skipped."""

    def test_one_malformed_item_does_not_discard_the_good_ones(self):
        raw = {"title": "Cancer vaccines", "objective": "the state of cancer vaccinations",
               "subquestions": [
                   {"question": "What cancer vaccines are in late-stage trials?"},
                   {"text": "this item uses the wrong key entirely"},
                   {"question": "What regulatory approvals have been granted?"},
               ]}
        plan = _normalize_plan(raw, "the state of cancer vaccinations", "standard")
        questions = [q["question"] for q in plan["subquestions"]]
        self.assertIn("What cancer vaccines are in late-stage trials?", questions)
        self.assertIn("What regulatory approvals have been granted?", questions)
        self.assertEqual(len(questions), 2)

    def test_a_null_domains_field_does_not_discard_the_plan(self):
        # A common small-model quirk: emitting `null` instead of `[]`.
        raw = {"title": "T", "objective": "O",
               "subquestions": [{"question": "First real question here?"},
                                {"question": "Second real question here?"}],
               "allowed_domains": None, "blocked_domains": None}
        plan = _normalize_plan(raw, "O", "standard")
        self.assertEqual(len(plan["subquestions"]), 2)
        self.assertEqual(plan["allowed_domains"], [])

    def test_a_bare_string_item_is_salvaged(self):
        raw = {"subquestions": ["A bare string subquestion?", {"question": "A normal one?"}]}
        plan = _normalize_plan(raw, "topic", "standard")
        questions = [q["question"] for q in plan["subquestions"]]
        self.assertIn("A bare string subquestion?", questions)
        self.assertIn("A normal one?", questions)

    def test_totally_unusable_plan_still_falls_back_safely(self):
        plan = _normalize_plan({"subquestions": "not even a list"}, "topic", "quick")
        self.assertEqual(plan["subquestions"], _fallback_plan("topic", "quick")["subquestions"])


class ReportRenderingTests(unittest.TestCase):
    def test_empty_section_gets_an_honest_note_not_a_blank_gap(self):
        lines = ["## Executive Summary", "", "## Findings", "A real finding here."]
        out = _annotate_empty_sections(lines)
        idx = out.index("## Executive Summary")
        self.assertIn("No claims in this section passed verification", out[idx + 1])

    def test_section_with_content_is_left_alone(self):
        lines = ["## Findings", "A real finding here."]
        self.assertEqual(_annotate_empty_sections(lines), lines)


class SourcesAppendixDedupTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for a real bad run: one page mined for six atomic
    quotes rendered as six near-identical numbered lines in '## Sources',
    which reads as padding rather than as one well-used source."""

    async def test_same_url_collapses_to_one_appendix_line_with_all_numbers(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            store = ResearchStore(Path(tmp.name) / "research.db")
            manager = ResearchManager(store)
            plan = _fallback_plan("Test", "quick")
            job = store.create_job("Test", plan, state="running")
            jid = job["id"]
            sid = store.add_source(jid, url="https://cancer.gov/what-is-cancer",
                canonical_url="https://cancer.gov/what-is-cancer", title="What Is Cancer?",
                domain="cancer.gov", score=1.0)
            store.update_source(jid, sid, status="read",
                body="Cancer arises from genetic changes. Metastatic cells share molecular features.")
            e1 = store.add_evidence(jid, source_id=sid, subquestion_id="Q1",
                claim="c1", quote="Cancer arises from genetic changes.", start_offset=0, end_offset=34)
            e2 = store.add_evidence(jid, source_id=sid, subquestion_id="Q1",
                claim="c2", quote="Metastatic cells share molecular features.", start_offset=35, end_offset=77)
            client = _FakeResearchClient()
            report, metadata = await manager._render_report(client, jid,
                f"# R\n\nCancer arises from genetic changes [E:{e1[1:]}].\n\n"
                f"Metastatic cells share molecular features [E:{e2[1:]}].", plan)
            sources_section = report.split("## Sources")[1]
            self.assertEqual(sources_section.count("What Is Cancer?"), 1)
            self.assertIn("[1, 2]", sources_section)
            self.assertEqual(len(metadata["sources"]), 2)  # UI drawer keeps per-quote granularity
            store.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
