"""A deterministic stand-in for the oMLX embedder, for tests.

Semantic retrieval is now on the critical path for several routes, which would
otherwise make every routing test depend on a live oMLX with an embedding model
loaded. That is the wrong dependency for a logic test: it turns "did the router
scope this correctly" into "is the inference server up", and it makes the suite
unrunnable offline.

Scores by word overlap rather than by cosine geometry. That is enough to
exercise every branch that belongs to the ROUTER (ranking, max-over-documents,
the relative floor, the write gate, the pinned floor, the safety net) without
pinning assertions to one embedding model's behavior — which is a property of
the model, not of this codebase, and is measured separately and for real by
`scripts/test_retrieval.py`.
"""
from __future__ import annotations

_VOCAB: dict[str, int] = {}
_DIM = 512


def _vec(text: str) -> list[float]:
    words = {w.strip(".,'\"!?()[]").lower() for w in text.split()}
    for w in words:
        _VOCAB.setdefault(w, len(_VOCAB))
    v = [0.0] * _DIM
    for w in words:
        if w:
            v[_VOCAB[w] % _DIM] = 1.0
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v] if norm else v


def install() -> None:
    """Point `router.semantic` at the stub and reset its index.

    Also disables the on-disk vector cache, so a test run neither reads the
    user's real `~/.moe/cache/tool_vectors.json` (whose vectors come from a
    different model and are meaningless here) nor overwrites it with stub
    vectors — which would poison the next real run until something noticed the
    embedding model had "changed".
    """
    from service.router import semantic

    async def _embed(texts, *, timeout=60.0):
        return [_vec(t) for t in texts]

    async def _embed_queries(queries, *, timeout=30.0):
        return [_vec(q) for q in queries]

    semantic._embed = _embed                  # type: ignore[assignment]
    semantic.embed_queries = _embed_queries   # type: ignore[assignment]
    semantic._load_cache = lambda: {}         # type: ignore[assignment]
    semantic._save_cache = lambda vectors: None  # type: ignore[assignment]
    semantic._INDEX = semantic.ToolIndex()
