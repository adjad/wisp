"""T2 semantic retrieval via oMLX's /v1/embeddings.

The embedder is deliberately NOT routed through OMLXClient.ensure_only: it must
never evict the resident chat model, and it must never be evicted by one. It is
a 320MB model against a 21.4GB ceiling, so it simply co-resides — oMLX loads it
on first use and the idle unloader is told to leave it alone.

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
        data = r.json().get("data") or []
        if len(data) != len(batch):
            raise EmbedUnavailable("embedding count mismatch")
        vecs = [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
        return idx, vecs

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as c:
        results = await asyncio.gather(*(run(c, i, b) for i, b in enumerate(batches)))

    out: list[list[float]] = []
    for _, vecs in sorted(results, key=lambda t: t[0]):
        out.extend(vecs)
    return out


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


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
    try:
        return await task
    finally:
        async with _cache_lock:
            # Only the caller that created it clears the slot — a second
            # caller awaiting the same task must not race the first caller's
            # own cleanup out from under it.
            if _inflight.get(key) is task:
                del _inflight[key]


async def _do_index(key: str, chunks: list[Chunk], timeout: float) -> list[list[float]]:
    """The actual embed pass, run at most once per key regardless of how many
    concurrent callers are waiting on it (see the _inflight coalescing above)."""
    vecs = [_normalize(v) for v in await _embed([c.text for c in chunks], timeout=timeout)]
    async with _cache_lock:
        _cache[key] = vecs
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_DOCS:
            _cache.popitem(last=False)
    return vecs


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
