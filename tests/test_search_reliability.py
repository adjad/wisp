"""Offline Smart Search contracts: real tiers, synthetic HTTP/model replies.

Run with a temporary WISP_HOME. No live engine, credentials, or source apps.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from service.search import embedder, engine, synth
from service.search.chunker import chunk

DOCUMENT = "The launch code is violet. The review starts on Thursday."
QUERY = "What is the launch code?"
HTTP_CLIENT = httpx.AsyncClient


class SearchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(embedder, "_cache", {}))
        # Preserve the production LRU API without sharing vectors across tests.
        from collections import OrderedDict
        embedder._cache = OrderedDict()
        self.enterContext(patch.object(embedder, "_inflight", {}))
        self.enterContext(patch.object(embedder, "_cache_lock", asyncio.Lock()))
        self.enterContext(patch.object(embedder, "embedding_model", return_value="fixture-embedder"))
        self.enterContext(patch.object(embedder, "omlx_base_url", return_value="http://fixture.invalid"))
        self.enterContext(patch.object(embedder, "omlx_api_key", return_value="fixture-token"))
        self.enterContext(patch.object(synth, "pick_model", AsyncMock(return_value=("fixture-model", False))))
        self.enterContext(patch.object(synth, "no_thinking_kwargs", return_value={}))

    async def asyncTearDown(self):
        tasks = list(embedder._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def http(self, handler):
        transport = httpx.MockTransport(handler)
        self.enterContext(patch.object(embedder.httpx, "AsyncClient",
            side_effect=lambda **kwargs: HTTP_CLIENT(transport=transport, **kwargs)))

    def task(self, coro):
        task = asyncio.create_task(coro)

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    async def events(self, **kwargs):
        return [ev async for ev in engine.search_stream(FixtureModel(), DOCUMENT, QUERY, **kwargs)]


class FixtureModel:
    async def chat(self, _model, _messages, **_kwargs):
        return {"choices": [{"message": {"content": 'The launch code is "violet" [1].'}}]}


class IndexLifecycleTests(SearchCase):
    async def test_cancelling_first_waiter_preserves_second_and_single_pass(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(_texts, **_kwargs):
            started.set()
            await release.wait()
            return [[3.0, 4.0]]

        mock = self.enterContext(patch.object(embedder, "_embed", side_effect=blocked))
        chunks = chunk(DOCUMENT)
        first = self.task(embedder.index_document("doc", chunks))
        await asyncio.wait_for(started.wait(), 1)
        second = self.task(embedder.index_document("doc", chunks))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        self.assertIn("doc", embedder._inflight)
        release.set()
        self.assertEqual(await asyncio.wait_for(second, 1), [[0.6, 0.8]])
        self.assertEqual(mock.call_count, 1)
        self.assertFalse(embedder._inflight)
        self.assertEqual(await embedder.index_document("doc", chunks), [[0.6, 0.8]])
        self.assertEqual(mock.call_count, 1)

    async def test_cancelling_joined_waiter_preserves_prewarm(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(_texts, **_kwargs):
            started.set()
            await release.wait()
            return [[1.0, 0.0]]

        self.enterContext(patch.object(embedder, "_embed", side_effect=blocked))
        prewarm = self.task(embedder.index_document("doc", chunk(DOCUMENT)))
        await asyncio.wait_for(started.wait(), 1)
        query = self.task(embedder.index_document("doc", chunk(DOCUMENT)))
        await asyncio.sleep(0)
        query.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await query
        release.set()
        self.assertEqual(await asyncio.wait_for(prewarm, 1), [[1.0, 0.0]])

    async def test_abandoned_index_is_joined_by_next_query(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(_texts, **_kwargs):
            started.set()
            await release.wait()
            return [[1.0, 0.0]]

        mock = self.enterContext(patch.object(embedder, "_embed", side_effect=blocked))
        old = self.task(embedder.index_document("doc", chunk(DOCUMENT)))
        await asyncio.wait_for(started.wait(), 1)
        old.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old
        next_query = self.task(embedder.index_document("doc", chunk(DOCUMENT)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(next_query, 1)
        self.assertEqual(mock.call_count, 1)
        self.assertFalse(embedder._inflight)

    async def test_abandoned_failure_is_reaped_and_retry_succeeds(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def failed(_texts, **_kwargs):
            started.set()
            await release.wait()
            raise embedder.EmbedUnavailable("fixture failure")

        mock = self.enterContext(patch.object(embedder, "_embed", side_effect=failed))
        old = self.task(embedder.index_document("doc", chunk(DOCUMENT)))
        await asyncio.wait_for(started.wait(), 1)
        background = embedder._inflight["doc"]
        old.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await old
        finished = asyncio.Event()
        background.add_done_callback(lambda _task: finished.set())
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        self.assertFalse(embedder._inflight)
        self.assertFalse(await embedder.is_cached("doc"))
        # The done callback consumes failures even with no active waiter.
        self.assertFalse(background._log_traceback)
        mock.side_effect = None
        mock.return_value = [[0.0, 1.0]]
        self.assertEqual(await embedder.index_document("doc", chunk(DOCUMENT)), [[0.0, 1.0]])


class EmbeddingResponseTests(SearchCase):
    async def test_invalid_payloads_degrade_to_lexical_answer(self):
        bad = [
            b"not json", [], {}, {"data": None}, {"data": {}}, {"data": []},
            {"data": [None]}, {"data": [{}]},
            {"data": [{"index": "0", "embedding": [1.0]}]},
            {"data": [{"index": True, "embedding": [1.0]}]},
            {"data": [{"index": -1, "embedding": [1.0]}]},
            {"data": [{"index": 1, "embedding": [1.0]}]},
            {"data": [{"index": 0}]},
        ]
        for vector in (None, "vector", [], ["1"], [None], [True], [[1]],
                       [float("nan")], [float("inf")], [0.0, 0.0], [10 ** 400]):
            bad.append({"data": [{"index": 0, "embedding": vector}]})
        current = None
        self.http(lambda _request: httpx.Response(200, content=current))
        for payload in bad:
            with self.subTest(payload=str(payload)[:100]):
                current = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                events = await self.events()
                types = [e["event"] for e in events]
                self.assertIn("lexical", types)
                self.assertIn("semantic_unavailable", types)
                self.assertNotIn("semantic", types)
                self.assertNotIn("error", types)
                self.assertEqual(types[-1], "done")
                answer = next(e for e in events if e["event"] == "answer")
                self.assertIn("violet", answer["text"])
                self.assertTrue(answer["citations"])

    async def test_indices_are_required_unique_and_complete(self):
        current = None
        self.http(lambda _request: httpx.Response(200, json={"data": current}))
        for indices in ([0, 0], [0, 2], [-1, 1], [0, None]):
            with self.subTest(indices=indices):
                current = [{"index": i, "embedding": [1.0]} for i in indices]
                with self.assertRaises(embedder.EmbedUnavailable):
                    await embedder._embed(["a", "b"], timeout=1)

    async def test_shuffled_indices_keep_passage_order(self):
        self.http(lambda _request: httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]}))
        self.assertEqual(await embedder._embed(["a", "b"], timeout=1),
                         [[1.0, 0.0], [0.0, 1.0]])

    async def test_different_dimensions_across_batches_are_rejected(self):
        self.enterContext(patch.object(embedder, "_BATCH", 1))
        self.http(lambda request: httpx.Response(200, json={"data": [{
            "index": 0, "embedding": [1.0] if json.loads(request.content)["input"] == ["a"]
            else [1.0, 2.0],
        }]}))
        with self.assertRaises(embedder.EmbedUnavailable):
            await embedder._embed(["a", "b"], timeout=1)

    async def test_query_document_dimension_mismatch_falls_back(self):
        def response(request):
            query = json.loads(request.content)["input"][0].startswith("Instruct:")
            return httpx.Response(200, json={"data": [{
                "index": 0, "embedding": [1.0] if query else [1.0, 0.0],
            }]})
        self.http(response)
        events = await self.events()
        self.assertIn("semantic_unavailable", [e["event"] for e in events])
        self.assertIn("answer", [e["event"] for e in events])

    async def test_failed_batch_cancels_sibling_before_client_closes(self):
        self.enterContext(patch.object(embedder, "_BATCH", 1))
        started, stopped = asyncio.Event(), asyncio.Event()

        async def response(request):
            if json.loads(request.content)["input"] == ["fail"]:
                await started.wait()
                return httpx.Response(503)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.http(response)
        with self.assertRaises(embedder.EmbedUnavailable):
            await asyncio.wait_for(embedder._embed(["fail", "blocked"], timeout=1), 1)
        self.assertTrue(stopped.is_set())

    async def test_cancelling_http_pass_reaps_all_batches(self):
        self.enterContext(patch.object(embedder, "_BATCH", 1))
        started, stopped = set(), set()
        ready = asyncio.Event()

        async def response(request):
            text = json.loads(request.content)["input"][0]
            started.add(text)
            if len(started) == 2:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(text)

        self.http(response)
        task = self.task(embedder._embed(["a", "b"], timeout=1))
        await asyncio.wait_for(ready.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stopped, {"a", "b"})

    async def test_http_failure_keeps_navigation_results(self):
        self.http(lambda _request: httpx.Response(503))
        events = await self.events(want_answer=False)
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(next(e for e in events if e["event"] == "lexical")["results"])
        self.assertNotIn("answering", [e["event"] for e in events])


class SearchLifecycleTests(SearchCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.http(lambda request: httpx.Response(200, json={"data": [
            {"index": i, "embedding": [1.0, 0.0]}
            for i, _text in enumerate(json.loads(request.content)["input"])
        ]}))

    async def test_healthy_search_preserves_tiers_and_real_grounding(self):
        events = await self.events()
        self.assertEqual([e["event"] for e in events], ["query", "literal", "lexical",
            "indexing", "semantic", "answering", "answer", "done"])
        answer = next(e for e in events if e["event"] == "answer")
        self.assertIn('"violet" [1]', answer["text"])
        citation = answer["citations"][0]
        self.assertIn("violet", DOCUMENT[citation["start"]:citation["end"]])
        again = await self.events()
        self.assertNotIn("indexing", [e["event"] for e in again])

    async def test_exact_query_never_calls_optional_tiers(self):
        self.enterContext(patch.object(embedder, "index_document", side_effect=AssertionError("unexpected indexing")))
        events = [e async for e in engine.search_stream(FixtureModel(), DOCUMENT, '"violet"')]
        self.assertEqual([e["event"] for e in events], ["query", "literal", "done"])
        self.assertEqual(events[1]["count"], 1)

    async def test_cancelling_search_stops_real_synthesis_chat(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        class BlockedModel:
            async def chat(self, *_args, **_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        seen = []

        async def consume():
            async for event in engine.search_stream(BlockedModel(), DOCUMENT, QUERY):
                seen.append(event)

        request = self.task(consume())
        await asyncio.wait_for(started.wait(), 1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(stopped.is_set())
        self.assertNotIn("answer", [e["event"] for e in seen])

    async def test_closing_iterator_at_upgrade_stops_synthesis(self):
        stopped = asyncio.Event()

        async def blocked(*_args, on_progress, **_kwargs):
            await on_progress("fixture-model")
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.enterContext(patch.object(synth, "answer", side_effect=blocked))
        stream = engine.search_stream(FixtureModel(), DOCUMENT, QUERY)
        async for event in stream:
            if event["event"] == "upgrading_model":
                await stream.aclose()
                break
        self.assertTrue(stopped.is_set())

    async def test_retrieval_failure_cancels_request_owned_query(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def failed(*_args, **_kwargs):
            await started.wait()
            raise embedder.EmbedUnavailable("fixture index failed")

        async def blocked(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.enterContext(patch.object(embedder, "index_document", side_effect=failed))
        self.enterContext(patch.object(embedder, "embed_queries", side_effect=blocked))
        events = await asyncio.wait_for(self.events(), 1)
        self.assertTrue(stopped.is_set())
        self.assertIn("answer", [e["event"] for e in events])

    async def test_cancelling_retrieval_leaves_shared_index_for_next_search(self):
        started, release, query_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        doc_calls = 0
        query_calls = 0

        async def embed(texts, **_kwargs):
            nonlocal doc_calls, query_calls
            if texts[0].startswith("Instruct:"):
                query_calls += 1
                if query_calls == 1:
                    try:
                        await asyncio.Event().wait()
                    finally:
                        query_stopped.set()
            else:
                doc_calls += 1
                started.set()
                await release.wait()
            return [[1.0, 0.0] for _ in texts]

        self.enterContext(patch.object(embedder, "_embed", side_effect=embed))
        request = self.task(self.events())
        await asyncio.wait_for(started.wait(), 1)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(query_stopped.is_set())
        release.set()
        events = await asyncio.wait_for(self.events(), 1)
        self.assertEqual(doc_calls, 1)
        self.assertIn("answer", [e["event"] for e in events])

    async def test_force_global_preserves_scope_and_citations(self):
        events = await self.events(force_global=True)
        answer = next(e for e in events if e["event"] == "answer")
        self.assertEqual(answer["scope"], "global")
        self.assertTrue(answer["citations"])


if __name__ == "__main__":
    unittest.main()
