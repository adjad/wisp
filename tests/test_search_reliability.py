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
from service.search.lexical import BM25, fold, literal_hits, tokenize
from service.config.endpoints import Endpoint, Target

DOCUMENT = "The launch code is violet. The review starts on Thursday."
QUERY = "What is the launch code?"
HTTP_CLIENT = httpx.AsyncClient


class LexicalFoldTests(unittest.TestCase):
    def test_all_ascii_codepoints_fold_to_lower_without_changing_length(self):
        text = "".join(chr(codepoint) for codepoint in range(128))
        folded = fold(text)
        self.assertEqual(folded, text.lower())
        self.assertEqual(len(folded), len(text))
        self.assertTrue(folded.isascii())

    def test_empty_and_ascii_punctuation_whitespace_and_controls(self):
        self.assertEqual(fold(""), "")
        text = "\x00\t VIOLET!!\nA_b-9\r"
        self.assertEqual(fold(text), "\x00\t violet!!\na_b-9\r")
        self.assertEqual(len(fold(text)), len(text))

    def test_unicode_folding_keeps_the_existing_fallback_results(self):
        samples = {
            "Café": "cafe",
            "e\u0301": "e\u0301",
            "İ": "i",
            "ß": "ß",
            "👩🏽‍💻": "👩🏽‍💻",
            "ÉΣß": "eσß",
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(fold(text), expected)

    def test_literal_hits_keep_original_spans_order_and_limit(self):
        text = "VIOLET, violet. Violet"
        hits = literal_hits(text, "  violet  ")
        self.assertEqual([(hit.start, hit.end) for hit in hits], [(0, 6), (8, 14), (16, 22)])
        self.assertEqual([(hit.start, hit.end) for hit in literal_hits(text, "violet", limit=2)],
                         [(0, 6), (8, 14)])
        self.assertEqual(literal_hits(text, "violet", limit=0), [])
        self.assertEqual(literal_hits(text, "absent"), [])
        self.assertEqual(literal_hits(text, "  \t  "), [])

        accented = "Café; cafe"
        self.assertEqual([(hit.start, hit.end) for hit in literal_hits(accented, "cafe")],
                         [(0, 4), (6, 10)])

    def test_sparse_unicode_preserves_per_codepoint_case_and_composition(self):
        # Each middle uses the sparse path; expected text is independent of
        # Python's contextual whole-string lower and global normalization.
        cases = [("ΟΣ ΣΟΣ ΟΣΑ", "οσ σοσ οσα"), ("İ É e\u0301", "i e e\u0301"),
                 ("👩🏽‍💻 ’ ß", "👩🏽‍💻 ’ ß"), ("\ud800 A \udfff", "\ud800 a \udfff")]
        for middle, expected in cases:
            with self.subTest(middle=middle):
                text = "A" * 256 + middle + "Z" * 256
                self.assertEqual(fold(text), "a" * 256 + expected + "z" * 256)
                self.assertEqual(len(fold(text)), len(text))

    def test_unicode_density_boundaries_and_fragment_ends(self):
        # 31/32/33 characters straddle the first sparse threshold. With two
        # accents, 63/64/65 characters straddle the next one.
        for size, accents in ((31, 1), (32, 1), (33, 1), (63, 2), (64, 2), (65, 2)):
            for where in ("first", "middle", "last"):
                plain = "A" * (size - accents)
                pos = {"first": 0, "middle": len(plain) // 2, "last": len(plain)}[where]
                with self.subTest(size=size, accents=accents, where=where):
                    text = plain[:pos] + "É" * accents + plain[pos:]
                    self.assertEqual(fold(text), "a" * pos + "e" * accents + "a" * (len(plain) - pos))
                    self.assertEqual(len(fold(text)), size)

    def test_tokenizer_and_bm25_results_remain_stable(self):
        self.assertEqual(tokenize("Project VIOLET, café e\u0301!"),
                         ["project", "violet", "cafe", "e"])
        texts = [
            "Project violet finds the colour guide.",
            "The violet code and its price are confidential.",
            "A puppy watches the launch code.",
        ]
        chunks = [piece for text in texts for piece in chunk(text)]
        bm25 = BM25(chunks)
        hits = bm25.search("launch code")
        self.assertEqual(
            [(hit.chunk_idx, round(hit.score, 6), hit.start, hit.end, hit.kind) for hit in hits],
            [(2, 1.512717, 0, 32, "lexical"), (1, 0.434457, 0, 47, "lexical")],
        )


class SearchCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(engine, "_docs", {}))
        self.enterContext(patch.object(embedder, "_cache", {}))
        # Preserve the production LRU API without sharing vectors across tests.
        from collections import OrderedDict
        embedder._cache = OrderedDict()
        self.enterContext(patch.object(embedder, "_inflight", {}))
        self.enterContext(patch.object(embedder, "_cache_lock", asyncio.Lock()))
        self.enterContext(patch.object(embedder, "embedding_model", return_value="fixture-embedder"))
        self.target = Target("embedding", Endpoint("fixture", "https://fixture.invalid", "env:FIXTURE_EMBED_KEY"), "fixture-embedder")
        self.enterContext(patch.dict("os.environ", {"FIXTURE_EMBED_KEY": "fixture-token"}))
        self.enterContext(patch.object(embedder, "embedding_target", return_value=self.target))
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


class LazyLexicalTests(SearchCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()

        async def synthetic_embed(texts, **_kwargs):
            return [[1.0, float(len(text) % 5 + 1), 2.0] for text in texts]

        self.enterContext(patch.object(embedder, "_embed", side_effect=synthetic_embed))
        self.enterContext(patch.object(synth, "answer", AsyncMock(return_value={
            "text": "Synthetic answer.", "model": "fixture", "citations": [],
        })))
        self.build = self.enterContext(patch.object(engine, "BM25", wraps=BM25))
        self.split = self.enterContext(patch.object(engine, "chunk", wraps=chunk))

    async def collect(self, text=DOCUMENT, query='"violet"', **kwargs):
        return [event async for event in engine.search_stream(None, text, query, **kwargs)]

    def test_public_index_stays_eager_and_reuses_actual_objects(self):
        key, chunks, index = engine.extract_and_index(DOCUMENT)
        self.assertIsInstance(chunks, list)
        self.assertIsInstance(index, BM25)
        other_key, other_chunks, other_index = engine.extract_and_index(DOCUMENT)
        self.assertEqual(other_key, key)
        self.assertIs(other_chunks, chunks)
        self.assertIs(other_index, index)
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.split.call_count, 1)

    async def test_quoted_then_normal_then_prewarm_share_chunks_and_one_index(self):
        await self.collect()
        key = embedder.doc_key(DOCUMENT)
        document = engine._docs[key]
        self.assertIsNone(document.bm25)
        self.assertEqual(self.build.call_count, 0)
        await self.collect(query="violet", want_answer=False)
        index = document.bm25
        self.assertIsInstance(index, BM25)
        public_key, chunks, public_index = engine.extract_and_index(DOCUMENT)
        self.assertEqual(public_key, key)
        self.assertIs(chunks, document.chunks)
        self.assertIs(public_index, index)
        await self.collect()
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.split.call_count, 1)

    async def test_public_prewarm_completes_a_literal_only_record_once(self):
        await self.collect()
        document = engine._docs[embedder.doc_key(DOCUMENT)]
        self.assertIsNone(document.bm25)
        _, chunks, index = engine.extract_and_index(DOCUMENT)
        self.assertIs(chunks, document.chunks)
        self.assertIs(index, document.bm25)
        await self.collect()
        _, other_chunks, other_index = engine.extract_and_index(DOCUMENT)
        self.assertIs(other_chunks, chunks)
        self.assertIs(other_index, index)
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.split.call_count, 1)

    async def test_literal_modes_empty_input_and_no_match_never_build(self):
        for text, query in ((DOCUMENT, '"violet"'), (DOCUMENT, "regex:violet"),
                            (DOCUMENT, '"absent"'), ("   ", "violet"),
                            (DOCUMENT, "")):
            with self.subTest(text=text, query=query):
                events = await self.collect(text, query)
                self.assertEqual(events[-1]["event"], "done")
                self.assertNotIn("lexical", [event["event"] for event in events])
                if query == '"absent"':
                    literal = next(event for event in events if event["event"] == "literal")
                    self.assertEqual(literal["results"], [])
                    self.assertEqual(literal["count"], 0)
        self.assertEqual(self.build.call_count, 0)

    async def test_closing_navigation_after_t0_avoids_index(self):
        for query in ("violet", "absent"):
            with self.subTest(query=query):
                stream = engine.search_stream(None, DOCUMENT, query, want_answer=False)
                try:
                    self.assertEqual((await anext(stream))["event"], "query")
                    literal = await anext(stream)
                    self.assertEqual(literal["event"], "literal")
                    self.assertEqual(literal["count"], 0 if query == "absent" else 1)
                    self.assertEqual(self.build.call_count, 0)
                finally:
                    await stream.aclose()
        self.assertEqual(self.build.call_count, 0)
        self.assertIsNone(engine._docs[embedder.doc_key(DOCUMENT)].bm25)

    async def test_fifo_count_cap_and_eviction_between_t0_and_t1(self):
        self.assertEqual(engine._DOC_MAX, 200)
        stream = engine.search_stream(None, DOCUMENT, "violet", want_answer=False)
        try:
            await anext(stream)
            literal = await anext(stream)
            original = engine._docs[embedder.doc_key(DOCUMENT)]
            # Cache hits must not promote the oldest record (FIFO, not LRU).
            for i in range(engine._DOC_MAX - 1):
                await self.collect(f"Other document {i}.")
            await self.collect()
            await self.collect("Final document evicts the oldest.")
            self.assertEqual(len(engine._docs), engine._DOC_MAX)
            self.assertNotIn(embedder.doc_key(DOCUMENT), engine._docs)
            self.assertIn(embedder.doc_key("Other document 0."), engine._docs)
            self.assertEqual(self.build.call_count, 0)
            tail = [event async for event in stream]
        finally:
            await stream.aclose()
        self.assertEqual(tail[0]["event"], "lexical")
        self.assertTrue(tail[0]["results"])
        self.assertEqual(tail[0]["results"][0]["chunk_idx"], literal["results"][0]["chunk_idx"])
        self.assertEqual(self.build.call_count, 1)
        self.assertIsInstance(original.bm25, BM25)
        self.assertIs(self.build.call_args.args[0], original.chunks)
        self.assertEqual(len(engine._docs), engine._DOC_MAX)
        self.assertNotIn(embedder.doc_key(DOCUMENT), engine._docs)

    async def test_constructor_failure_propagates_and_keeps_chunks_retryable(self):
        failure = RuntimeError("synthetic BM25 failure")
        self.build.side_effect = failure
        stream = engine.search_stream(None, DOCUMENT, "violet", want_answer=False)
        await anext(stream)
        await anext(stream)
        with self.assertRaisesRegex(RuntimeError, "synthetic BM25 failure"):
            await anext(stream)
        document = engine._docs[embedder.doc_key(DOCUMENT)]
        self.assertIsNone(document.bm25)
        with self.assertRaisesRegex(RuntimeError, "synthetic BM25 failure"):
            engine.extract_and_index(DOCUMENT)
        self.assertIsNone(document.bm25)
        # A failed public call keeps the same exception behavior. Retaining its
        # bounded chunk record avoids rechunking and leaves literal search usable.
        await self.collect()
        self.build.side_effect = None
        _, chunks, index = engine.extract_and_index(DOCUMENT)
        self.assertIs(chunks, document.chunks)
        self.assertIs(index, document.bm25)
        self.assertEqual(self.build.call_count, 3)
        self.assertEqual(self.split.call_count, 1)
        engine.extract_and_index(DOCUMENT)
        self.assertEqual(self.build.call_count, 3)

    async def test_literal_rows_preserve_unicode_original_spans_and_overlap_policy(self):
        text = "Café café\nbanana BANANA; İ e\u0301 👩🏽‍💻."
        cases = [('"cafe"', [(0, 4), (5, 9)]), ('"ana"', [(11, 14), (18, 21)]),
                 ('"i"', [(25, 26)]), ('"e\u0301"', [(27, 29)]),
                 ('"👩🏽‍💻"', [(30, 34)])]
        for query, spans in cases:
            with self.subTest(query=query):
                events = await self.collect(text, query)
                literal = next(event for event in events if event["event"] == "literal")
                self.assertEqual([(row["start"], row["end"]) for row in literal["results"]], spans)
                for row in literal["results"]:
                    self.assertEqual(row["match"], text[row["start"]:row["end"]])
                    self.assertEqual(row["line"], text.count("\n", 0, row["start"]) + 1)
                    chunks = engine._docs[embedder.doc_key(text)].chunks
                    self.assertEqual(row["chunk_idx"], engine._chunk_for_offset(chunks, row["start"]))
        self.assertEqual(self.split.call_count, 1)
        self.assertEqual(self.build.call_count, 0)

    async def test_cold_events_equal_eager_prewarm_including_forced_modes(self):
        text = DOCUMENT + "\n\n" + ("Paragraph has other facts. " * 80)
        cases = [("", {}, ["query", "done"]),
                 ('"violet"', {}, ["query", "literal", "done"]),
                 ("regex:violet", {}, ["query", "literal", "done"]),
                 ("violet", {"want_answer": False}, None),
                 ("violet -Thursday", {"want_answer": False}, None),
                 (QUERY, {}, None), ("violet", {"force_answer": True}, None),
                 ('"violet"', {"force_answer": True}, ["query", "literal", "done"]),
                 ('"violet"', {"force_global": True}, None), ("summary", {}, None)]
        for query, kwargs, expected in cases:
            with self.subTest(query=query, kwargs=kwargs):
                collected = []
                for prewarm in (False, True):
                    engine._docs.clear()
                    embedder._cache.clear()
                    if prewarm:
                        engine.extract_and_index(text)
                    events = await self.collect(text, query, **kwargs)
                    collected.append([{k: v for k, v in event.items() if k != "ms"}
                                      for event in events])
                self.assertEqual(*collected)
                names = [event["event"] for event in collected[0]]
                if expected is not None:
                    self.assertEqual(names, expected)
                else:
                    self.assertEqual(names[:5], ["query", "literal", "lexical", "indexing", "semantic"])
                    self.assertEqual(names[-1], "done")
                    if kwargs.get("want_answer") is not False:
                        self.assertIn("answering", names)
                        self.assertIn("answer", names)
                if kwargs.get("force_global"):
                    self.assertEqual(collected[0][0]["parsed"]["mode"], "answer")
                    answering = next(event for event in collected[0] if event["event"] == "answering")
                    self.assertEqual(answering["scope"], "global")
        # Empty documents must keep their complete eager-path event behavior too.
        cold = await self.collect("   ", "violet")
        engine.extract_and_index("   ")
        eager = await self.collect("   ", "violet")
        self.assertEqual([{k: v for k, v in event.items() if k != "ms"} for event in cold],
                         [{k: v for k, v in event.items() if k != "ms"} for event in eager])

    async def test_sparse_unicode_literal_rows_keep_exact_original_offsets(self):
        text = "A" * 512 + "\nCAFÉ ΟΣ İ e\u0301 🙂 VIOLET" + "Z" * 512
        cases = [('"cafe"', (513, 517), "CAFÉ"), ('"οσ"', (518, 520), "ΟΣ"),
                 ('"İ e\u0301"', (521, 525), "İ e\u0301"), ('"e\u0301"', (523, 525), "e\u0301"),
                 ('"🙂"', (526, 527), "🙂"), ('"violet"', (528, 534), "VIOLET")]
        for query, span, original in cases:
            with self.subTest(query=query):
                events = await self.collect(text, query)
                self.assertEqual([event["event"] for event in events], ["query", "literal", "done"])
                rows = events[1]["results"]
                self.assertEqual([(row["start"], row["end"], row["match"], row["line"])
                                  for row in rows], [(*span, original, 2)])
                chunks = engine._docs[embedder.doc_key(text)].chunks
                self.assertEqual(rows[0]["chunk_idx"], engine._chunk_for_offset(chunks, span[0]))
        self.assertEqual(self.build.call_count, 0)


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
        self.assertIn(embedder.cache_key("doc", self.target), embedder._inflight)
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
        background = embedder._inflight[embedder.cache_key("doc", self.target)]
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
