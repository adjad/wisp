"""T2 Smart Search retrieval via oMLX's /v1/embeddings.

Tool routing can use the separate reranker-only path. The embedder remains the
semantic tier for document search, loads lazily, and may be evicted by either a
chat turn or the tool reranker when memory must be reclaimed.

Vectors are plain Python lists and similarity is a hand-rolled dot product. For
a single document (a few hundred chunks x 1024 dims) that is well under a
millisecond, and it keeps numpy out of the bundled venv.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import OrderedDict

import httpx

from service.config import models_config, omlx_api_key, omlx_base_url
from service.search.chunker import Chunk

# Qwen3-Embedding is asymmetric: queries carry an instruction prefix, passages
# go in bare. Measured on the design doc's own example, the prefix widened the
# gap between the answer and the nearest distractor (0.72 vs 0.37, against
# 0.76 vs 0.43 unprefixed) — better separation is what matters, not raw score.
_QUERY_INSTRUCT = (
    "Instruct: Given a search query, retrieve relevant passages that answer it\n"
    "Query: "
)

# Batch size for the embed call. Large enough to amortize HTTP, small enough
# that a huge document streams rather than blocking on one giant request.
_BATCH = 32

# Per-document vector cache, keyed by sha256 of the extracted text, so
# re-invoking search on the same page is instant. LRU-bounded because these are
# ~4KB per chunk and a day of browsing would otherwise grow without limit.
_CACHE_MAX_DOCS = 200
_cache: "OrderedDict[str, list[list[float]]]" = OrderedDict()
_cache_lock = asyncio.Lock()


def embedding_model() -> str:
    """The oMLX model id for the `embedding` role."""
    return models_config()["roles"].get(
        "embedding", "Qwen3-Embedding-0.6B-4bit-DWQ")


def doc_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class EmbedUnavailable(RuntimeError):
    """The embedder could not be reached or the model isn't installed.

    Callers must treat this as "T2 is off today", never as a failed search —
    T0/T1 still have real results to show.
    """


# A novel-length document is hundreds of chunks — serially awaiting one HTTP
# round trip per batch made the FIRST search on a long book noticeably slow.
# Running batches concurrently (bounded, so a huge document doesn't fire 50
# requests at oMLX at once) cuts that wall-clock time by roughly this factor.
_MAX_CONCURRENT_BATCHES = 4


async def _embed(texts: list[str], *, timeout: float) -> list[list[float]]:
    if not texts:
        return []
    url = omlx_base_url() + "/v1/embeddings"
    headers = {"Authorization": f"Bearer {omlx_api_key()}",
               "Content-Type": "application/json"}
    batches = [texts[i:i + _BATCH] for i in range(0, len(texts), _BATCH)]
    sem = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)

    async def run(client: httpx.AsyncClient, idx: int,
                  batch: list[str]) -> tuple[int, list[list[float]]]:
        async with sem:
            try:
                r = await client.post(url, headers=headers,
                                      json={"model": embedding_model(), "input": batch})
            except httpx.HTTPError as e:
                raise EmbedUnavailable(str(e)) from e
        if r.status_code != 200:
            raise EmbedUnavailable(f"{r.status_code}: {r.text[:200]}")
        try:
            payload = r.json()
        except ValueError as e:
            raise EmbedUnavailable("invalid embedding response JSON") from e
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbedUnavailable("embedding count mismatch")
        # Indices establish which passage a vector belongs to. A duplicate or
        # absent index must never quietly attribute evidence to the wrong text.
        by_index: dict[int, list[float]] = {}
        for row in data:
            if not isinstance(row, dict):
                raise EmbedUnavailable("invalid embedding record")
            index = row.get("index")
            if (type(index) is not int or not 0 <= index < len(batch)
                    or index in by_index):
                raise EmbedUnavailable("invalid embedding indices")
            vector = row.get("embedding")
            _validate_vector(vector)
            by_index[index] = vector
        vecs = [by_index[i] for i in range(len(batch))]
        return idx, vecs

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
        tasks = [asyncio.create_task(run(c, i, b)) for i, b in enumerate(batches)]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            # gather does not cancel siblings when one fails. Reap them before
            # closing the HTTP client, including on caller cancellation.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    out: list[list[float]] = []
    for _, vecs in sorted(results, key=lambda t: t[0]):
        out.extend(vecs)
    if len({len(v) for v in out}) != 1:
        raise EmbedUnavailable("embedding dimensions mismatch")
    return out


def _validate_vector(v: object) -> None:
    if not isinstance(v, list) or not v:
        raise EmbedUnavailable("invalid embedding vector")
    try:
        if any(type(x) not in (float, int) or not math.isfinite(x) for x in v):
            raise ValueError
        norm = math.hypot(*v)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as e:
        raise EmbedUnavailable("invalid embedding vector values") from e


def _normalize(v: list[float]) -> list[float]:
    _validate_vector(v)
    n = math.hypot(*v)
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


async def is_cached(key: str) -> bool:
    """Whether `key`'s vectors are already indexed — lets a caller decide
    whether to warn the user an embed pass is about to run, without paying for
    the embed itself."""
    async with _cache_lock:
        return key in _cache


# In-flight embed passes, keyed the same as the vector cache. Without this,
# the prewarm call fired the moment the search panel opens and the user's
# first real query landing a moment later — both against the same uncached
# document — race to index it independently: two full embedding passes over
# every chunk, each running _MAX_CONCURRENT_BATCHES requests, stacked on top
# of each other against oMLX. For a novel-length document that's a real spike
# (measured: contributed to an oMLX crash during testing). Coalescing means
# the second caller just awaits the first's in-progress work.
_inflight: dict[str, asyncio.Task] = {}


async def index_document(key: str, chunks: list[Chunk], *,
                         timeout: float = 120.0) -> list[list[float]]:
    """Embed every chunk (or return the cached vectors). Pre-normalized, so
    similarity later is a bare dot product."""
    async with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
        task = _inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(_do_index(key, chunks, timeout))
            _inflight[key] = task
            # All waiters may leave during debounce. Retrieve any eventual
            # exception even then; the next search can retry a failed pass.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    # Indexing belongs to the document, not the current keystroke. Let its
    # bounded HTTP pass finish as prewarm even if every current waiter leaves.
    return await asyncio.shield(task)


async def _do_index(key: str, chunks: list[Chunk], timeout: float) -> list[list[float]]:
    """The actual embed pass, run at most once per key regardless of how many
    concurrent callers are waiting on it (see the _inflight coalescing above)."""
    try:
        vecs = [_normalize(v) for v in await _embed([c.text for c in chunks], timeout=timeout)]
        async with _cache_lock:
            _cache[key] = vecs
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_MAX_DOCS:
                _cache.popitem(last=False)
        return vecs
    finally:
        async with _cache_lock:
            # Only the indexing task can retire its slot. A canceled waiter
            # must not make a still-running pass invisible to the next query.
            if _inflight.get(key) is asyncio.current_task():
                del _inflight[key]


async def embed_queries(queries: list[str], *, timeout: float = 30.0) -> list[list[float]]:
    prefixed = [_QUERY_INSTRUCT + q for q in queries]
    return [_normalize(v) for v in await _embed(prefixed, timeout=timeout)]


def rank(query_vecs: list[list[float]], doc_vecs: list[list[float]], *,
         limit: int = 20) -> list[tuple[int, float]]:
    """Best score per chunk across all sub-queries, descending.

    Max (not mean) across sub-queries on purpose: a two-part question like
    "traits AND color" should rank a chunk that nails one half, rather than
    penalizing it for not covering both — coverage is the synthesizer's job.
    """
    if not query_vecs or not doc_vecs:
        return []
    # Query and document calls can succeed individually with incompatible
    # dimensions (for example after an engine/model change). zip would silently
    # truncate, turning corrupt similarity into apparently valid citations.
    if len({len(v) for v in query_vecs + doc_vecs}) != 1:
        raise EmbedUnavailable("query/document embedding dimensions mismatch")
    best: dict[int, float] = {}
    for qv in query_vecs:
        for i, dv in enumerate(doc_vecs):
            s = _dot(qv, dv)
            if s > best.get(i, -2.0):
                best[i] = s
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:limit]


_warm_state = {"model": "", "at": 0.0}


async def warm() -> bool:
    """Best-effort preload so the first ⌘⇧F of the session isn't paying a cold
    model load. Cheap enough to call on every search; re-warms hourly."""
    now = time.time()
    if _warm_state["model"] == embedding_model() and now - _warm_state["at"] < 3600:
        return True
    try:
        await _embed(["warm"], timeout=180.0)
    except EmbedUnavailable:
        return False
    _warm_state.update({"model": embedding_model(), "at": now})
    return True
