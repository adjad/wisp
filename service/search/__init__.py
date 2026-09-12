"""Smart Search — the AI-powered replacement for ⌘F.

Four tiers, rendered progressively, each usable on its own (see
docs/SMART_SEARCH_DESIGN.md):

    T0 literal   ~1ms    exact substring — the ⌘F floor, never lost
    T1 lexical   ~10ms   BM25 + stems + fuzzy, no model
    T2 semantic  ~120ms  embeddings over chunks, via oMLX /v1/embeddings
    T3 answer    ~1-2s   grounded synthesis on the resident model + verification

Nothing here may trigger a model swap: the embedder is a 320MB model that
co-fits with everything, and synthesis runs on whatever is already loaded.
"""
from service.search.engine import search_stream, extract_and_index

__all__ = ["search_stream", "extract_and_index"]
