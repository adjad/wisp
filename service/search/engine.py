"""Tier orchestration — the thing the endpoint actually calls.

Results stream out as each tier lands, so the user sees literal matches in ~1ms
and never stares at an empty box waiting for a model. Every tier is independently
failable: no embedder just means no T2, and the search is still a better ⌘F.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from service.inference.omlx_client import OMLXClient
from service.search import embedder, query as qmod, synth
from service.search.chunker import Chunk, chunk
from service.search.lexical import BM25, Hit, focus_span, fold, literal_hits, query_terms

# Reciprocal Rank Fusion constant. The standard 60 — it exists precisely so
# BM25 scores and cosine similarities can be merged without pretending they
# live on the same scale.
_RRF_K = 60

# A document past this many chunks is "long" (~40-50 pages at ~1000 chars/
# chunk with overlap; a 300-page novel lands around 700-800). Past this
# threshold, synthesis widens its passage/token budget and lets a
# larger-context model take the answer if one's already resident — see
# synth.answer's `long_doc` parameter.
_LONG_DOC_CHUNKS = 120

# Document-level questions ("what is this book about") get a STRUCTURAL sample
# alongside the query-matched chunks: front matter, where a title/author/byline
# almost always lives, plus an even spread so the model sees the shape of the
# whole thing — but NOT at the expense of what retrieval actually found.
#
# The first version of this reserved front(3) + spread(7) against a budget of
# 10, which meant the matched chunks were appended into an already-full list
# and silently dropped every single time. Widening "what is the cyclops" then
# answered from the title page and six random pages while the genuinely
# relevant Cyclopes passages sat unused — the overview read as evasive because
# it had never been shown the evidence. Each source now gets its own reserved
# allocation.
_GLOBAL_FRONT_CHUNKS = 2
_GLOBAL_SPREAD_CHUNKS = 5
_GLOBAL_MATCHED_CHUNKS = 8
GLOBAL_BUDGET = _GLOBAL_FRONT_CHUNKS + _GLOBAL_SPREAD_CHUNKS + _GLOBAL_MATCHED_CHUNKS


def _global_picks(n_chunks: int, matched: list[int], *,
                  budget: int = GLOBAL_BUDGET) -> list[int]:
    """Evidence for a whole-document question, in reading order.

    Three sources, each with a reserved allocation so none can crowd out the
    others: front matter (identity — a title page answers "who wrote this" that
    no amount of semantic similarity will), the best retrieval matches
    (relevance — what the user actually asked about), and an even spread
    (coverage — the arc of the document). Returned in reading order, because
    passages arriving in document order let the model infer structure, which is
    most of what "what is this about" is really asking.
    """
    picks: list[int] = []

    def add(i: int, cap: int) -> bool:
        if 0 <= i < n_chunks and i not in picks and len(picks) < min(cap, budget):
            picks.append(i)
            return True
        return False

    # 1. Front matter — identity.
    for i in range(min(_GLOBAL_FRONT_CHUNKS, n_chunks)):
        add(i, _GLOBAL_FRONT_CHUNKS)

    # 2. Retrieval hits — relevance. Reserved BEFORE the spread so a question
    #    with strong matches is answered from them.
    cap = len(picks) + _GLOBAL_MATCHED_CHUNKS
    for i in matched:
        add(i, cap)

    # 3. Even spread — coverage, filling whatever budget remains.
    if n_chunks > _GLOBAL_FRONT_CHUNKS:
        span = n_chunks - _GLOBAL_FRONT_CHUNKS
        step = max(1, span // (_GLOBAL_SPREAD_CHUNKS + 1))
        for k in range(1, _GLOBAL_SPREAD_CHUNKS + 1):
            add(_GLOBAL_FRONT_CHUNKS + k * step, budget)

    return sorted(picks)

# Documents keep their chunking + BM25 index between invocations, keyed the same
# way as the vector cache so all three stay coherent.
_docs: dict[str, tuple[list[Chunk], BM25]] = {}
_DOC_MAX = 200


def extract_and_index(text: str) -> tuple[str, list[Chunk], BM25]:
    """Chunk + build the lexical index for `text`, memoized by content hash."""
    key = embedder.doc_key(text)
    cached = _docs.get(key)
    if cached is None:
        chunks = chunk(text)
        cached = (chunks, BM25(chunks))
        _docs[key] = cached
        while len(_docs) > _DOC_MAX:
            _docs.pop(next(iter(_docs)))
    return key, cached[0], cached[1]


def _chunk_for_offset(chunks: list[Chunk], pos: int) -> int:
    for c in chunks:
        if c.start <= pos < c.end:
            return c.idx
    return -1


def _snippet(text: str, start: int, end: int, *, pad: int = 90) -> dict:
    """A result row: the matched span plus enough surrounding text to read it."""
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    # Don't slice mid-word at the edges.
    if s > 0:
        sp = text.find(" ", s, start)
        s = sp + 1 if sp != -1 else s
    if e < len(text):
        sp = text.rfind(" ", end, e)
        e = sp if sp != -1 else e
    return {
        "start": start, "end": end,
        "prefix": text[s:start].replace("\n", " "),
        "match": text[start:end].replace("\n", " "),
        "suffix": text[end:e].replace("\n", " "),
        "line": text.count("\n", 0, start) + 1,
    }


def _rrf(*ranked: list[int]) -> list[int]:
    scores: dict[int, float] = {}
    for lst in ranked:
        for rank_i, idx in enumerate(lst):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (_RRF_K + rank_i + 1)
    return [i for i, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def _excluded(text: str, chunks: list[Chunk], idx: int, terms: list[str]) -> bool:
    if not terms:
        return False
    body = fold(chunks[idx].text)
    return any(fold(t) in body for t in terms)


async def search_stream(client: OMLXClient, text: str, raw_query: str, *,
                        want_answer: bool = True,
                        force_answer: bool = False,
                        force_global: bool = False) -> AsyncIterator[dict]:
    """Yield tier events: query, literal, lexical, semantic, answer, done.

    `force_answer` is the UI's ⌘↵ — synthesize even for a query that doesn't
    look like a question. An explicitly quoted/regex query still wins, since
    that's the user asking for exactness by name.
    """
    t0 = time.perf_counter()
    pq = qmod.parse(raw_query)
    if force_answer and pq.mode == "navigate":
        pq.mode = "answer"
    # The user escalated after a span lookup found no direct answer: re-run it
    # against a structural sample of the whole document instead of the chunks
    # that merely share words with the question.
    if force_global:
        pq.scope = "global"
        pq.mode = "answer"
    yield {"event": "query", "parsed": pq.to_dict()}

    if not pq.clean:
        yield {"event": "done", "ms": 0}
        return

    key, chunks, bm25 = extract_and_index(text)
    if not chunks:
        yield {"event": "done", "ms": int((time.perf_counter() - t0) * 1000),
               "empty": True}
        return

    # ---- T0 literal ------------------------------------------------------
    lits: list[Hit] = literal_hits(text, pq.exact[0] if pq.exact else pq.clean)
    yield {
        "event": "literal",
        "count": len(lits),
        "results": [dict(_snippet(text, h.start, h.end), kind="literal",
                         chunk_idx=_chunk_for_offset(chunks, h.start))
                    for h in lits[:50]],
        "ms": int((time.perf_counter() - t0) * 1000),
    }
    if pq.mode == "literal":
        yield {"event": "done", "ms": int((time.perf_counter() - t0) * 1000)}
        return

    # ---- T1 lexical ------------------------------------------------------
    lex = bm25.search(pq.clean, limit=30)
    lex = [h for h in lex if not _excluded(text, chunks, h.chunk_idx, pq.exclude)]
    yield {
        "event": "lexical",
        "results": [dict(_snippet(text, h.start, h.end), kind="lexical",
                         chunk_idx=h.chunk_idx, score=round(h.score, 3))
                    for h in lex[:20]],
        "ms": int((time.perf_counter() - t0) * 1000),
    }

    # ---- T2 semantic -----------------------------------------------------
    long_doc = len(chunks) >= _LONG_DOC_CHUNKS
    sem_ranked: list[int] = []
    try:
        # Only announce indexing when it's actually about to happen — a doc
        # already in cache (the common case once prewarm has had a moment to
        # run) shouldn't flash a status line it doesn't need.
        if not await embedder.is_cached(key):
            yield {"event": "indexing", "chunks": len(chunks)}
        queries = qmod.retrieval_queries(pq)
        doc_vecs, q_vecs = await asyncio.gather(
            embedder.index_document(key, chunks),
            embedder.embed_queries(queries),
        )
        scored = embedder.rank(q_vecs, doc_vecs, limit=20)
        scored = [(i, s) for i, s in scored
                  if not _excluded(text, chunks, i, pq.exclude)]
        sem_ranked = [i for i, _ in scored]
        # Point each result at the sentence that carries the match, not the
        # chunk's first 160 chars — otherwise every semantic hit in a document
        # shows whatever heading happens to lead its chunk.
        terms = query_terms(pq.clean)
        sem_results = []
        for i, s in scored[:12]:
            fs, fe = focus_span(chunks[i], terms)
            sem_results.append(dict(_snippet(text, fs, fe, pad=0),
                                    kind="semantic", chunk_idx=i, score=round(s, 3)))
        yield {"event": "semantic", "results": sem_results,
               "ms": int((time.perf_counter() - t0) * 1000)}
    except embedder.EmbedUnavailable as e:
        # T2 off today. T0/T1 already delivered; say so rather than pretending.
        yield {"event": "semantic_unavailable", "reason": str(e)[:200]}

    # ---- T3 answer -------------------------------------------------------
    if not (want_answer and pq.mode == "answer"):
        yield {"event": "done", "ms": int((time.perf_counter() - t0) * 1000)}
        return

    fused = _rrf([h.chunk_idx for h in lex if h.chunk_idx >= 0], sem_ranked)
    if not fused:
        fused = [_chunk_for_offset(chunks, h.start) for h in lits[:3]]
        fused = [i for i in fused if i >= 0]

    budget = 10 if long_doc else 5
    if pq.scope == "global":
        # Whole-document question: structural evidence ALONGSIDE the matched
        # spans (see _global_picks — the two are separately allocated).
        picks = _global_picks(len(chunks), fused)
    else:
        picks = fused[:budget]
    if not picks:
        yield {"event": "done", "ms": int((time.perf_counter() - t0) * 1000)}
        return

    yield {"event": "answering", "passages": picks, "long_doc": long_doc,
           "scope": pq.scope}

    # A whole-document question on a long document is the one case worth
    # loading a stronger model for — see synth.pick_model. The progress
    # callback surfaces the load so a 20s wait reads as work, not a hang.
    upgrade = pq.scope == "global" and long_doc
    upgrades: list[str] = []

    async def _on_upgrade(model: str) -> None:
        upgrades.append(model)

    task = asyncio.ensure_future(synth.answer(
        client, pq.clean, chunks, picks, long_doc=long_doc, scope=pq.scope,
        upgrade_model=upgrade, on_progress=_on_upgrade))
    # Surface the upgrade notice as soon as the callback fires, without
    # blocking the answer itself.
    notified = False
    while not task.done():
        if upgrades and not notified:
            notified = True
            yield {"event": "upgrading_model", "model": upgrades[0]}
        await asyncio.sleep(0.05)
    if upgrades and not notified:
        yield {"event": "upgrading_model", "model": upgrades[0]}
    result = await task
    if result.get("text"):
        yield {"event": "answer", **result,
               "ms": int((time.perf_counter() - t0) * 1000)}
    elif result.get("not_found"):
        # `can_widen` tells the UI whether escalating to a whole-document
        # sample is still worth offering — pointless if that's what just ran.
        yield {"event": "answer_not_found", "model": result.get("model"),
               "scope": pq.scope, "can_widen": pq.scope != "global",
               "ms": int((time.perf_counter() - t0) * 1000)}
    elif result.get("error"):
        yield {"event": "answer_error", "reason": result["error"][:200]}

    yield {"event": "done", "ms": int((time.perf_counter() - t0) * 1000)}
