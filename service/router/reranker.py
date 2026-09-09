"""Tool retrieval with a lexical shortlist and Qwen3-Reranker.

This is the low-memory alternative to semantic tool retrieval. It never calls
the embedding endpoint: a pure-Python BM25 pass narrows the registry, then the
reranker cross-scores only that shortlist. Before reranking it evicts a loaded
embedding model, so Wisp cannot accidentally retain Ling + embedder + reranker
at the same time.

The reranker is not a substitute for the deterministic router. Regex rules and
execution contracts still decide known high-risk domains; this module is only
the fallback for requests whose tool set remains unresolved.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from service.config import models_config, omlx_api_key, omlx_base_url
from service.router.semantic import _PINNED, _allowed, _docs, _gate_open
from service.router.tool_aliases import apply as apply_aliases
from service.search.chunker import Chunk
from service.search.embedder import embedding_model
from service.search.lexical import BM25
from service.tools.registry import REGISTRY

DEFAULT_SHORTLIST = 60
DEFAULT_K = 12
_PER_CLAUSE = 2
_QUERY_INSTRUCT = (
    "Instruct: Select the Wisp tool that performs the requested action. "
    "Distinguish reading data, drafting, sending, scheduling, changing, and "
    "deleting. Rank a tool only when it can perform the request.\nRequest: "
)


class RerankUnavailable(RuntimeError):
    """The reranker endpoint or configured model could not be used."""


def reranker_model() -> str:
    cfg = models_config()
    return str((cfg.get("roles") or {}).get(
        "reranker", "Qwen3-Reranker-0.6B-mlx-6bit"))


def _action_clauses(text: str) -> list[str]:
    """Conservatively split explicit task lists without shredding noun lists."""
    parts = re.split(
        # Most semicolons delimit tasks. Preserve the narrow cases where the
        # following span is authorization or scope for the preceding action.
        r"\s*;\s*(?!(?:i\s+authorize|you\s+have\s+my\s+permission|ask\s+me|"
        r"only\s+if|do\s+not|don'?t|never|without|keep|leave|if\s+one\s+part|"
        r"one\s+last\s+constraint)\b)(?:(?:then|after\s+that|next)\s+)?|"
        r"(?<=[.!?])\s+(?=[A-Z])|\n+|"
        r"\s+\b(?:and then|after that|next)\b\s+",
        text,
        flags=re.I,
    )
    out: list[str] = []
    for part in parts:
        part = re.sub(
            r"^(?:please do these in this order:|"
            r"for these tasks, use only the named sources and targets:|"
            r"i have a few things to finish\.)\s*",
            "", part, flags=re.I,
        )
        part = re.sub(r"^(?:then|also|first|second|third|fourth|fifth|finally|separately)\s*[:,]?\s+",
                      "", part, flags=re.I).strip(" ,")
        if len(part.split()) < 3:
            continue
        if re.match(r"^(?:i\s+authorize|you\s+have\s+my\s+permission|ask\s+me|"
                    r"only\s+if)\b", part, re.I):
            if out:
                out[-1] += "; " + part
            continue
        if re.match(r"^(?:keep the results|leave everything|one last constraint|"
                    r"leave (?:it|this|the message|the draft)|without sending|"
                    r"if one part|i mean the actual items|i authorize|"
                    r"you have my permission|for that first request|"
                    r"tell me if wisp cannot)", part, re.I):
            continue
        out.append(part)
    return out if len(out) > 1 else [text]


@dataclass
class _LexicalIndex:
    names: list[str]
    docs: list[str]
    bm25: BM25
    signature: tuple[tuple[str, tuple[str, ...], str], ...]


_LEXICAL: _LexicalIndex | None = None


def _signature() -> tuple[tuple[str, tuple[str, ...], str], ...]:
    return tuple((name, tuple(tool.aliases), tool.description)
                 for name, tool in REGISTRY.items())


def _lexical_index() -> _LexicalIndex:
    global _LEXICAL
    apply_aliases()
    sig = _signature()
    if _LEXICAL is not None and _LEXICAL.signature == sig:
        return _LEXICAL
    names = list(REGISTRY)
    docs = [" ".join(_docs(REGISTRY[name])) for name in names]
    chunks = [Chunk(i, doc, 0, len(doc)) for i, doc in enumerate(docs)]
    _LEXICAL = _LexicalIndex(names, docs, BM25(chunks), sig)
    return _LEXICAL


def lexical_rank(text: str) -> list[str]:
    idx = _lexical_index()
    hits = idx.bm25.search(text, limit=len(idx.names))
    return [idx.names[hit.chunk_idx] for hit in hits]


def lexical_shortlist(text: str, *, limit: int = DEFAULT_SHORTLIST) -> list[str]:
    """Reciprocal-rank fusion over the full request and explicit task clauses."""
    score: dict[str, float] = {}
    for query, depth in [(text, 30), *[(c, 15) for c in _action_clauses(text)]]:
        for rank, name in enumerate(lexical_rank(query)[:depth]):
            score[name] = score.get(name, 0.0) + 1.0 / (20 + rank)
    return [name for name, _ in sorted(
        score.items(), key=lambda item: (item[1], item[0]), reverse=True
    )[:limit]]


def lexical_candidates(text: str, *, writing: bool, k: int = 20) -> list[str]:
    """Zero-model routing fallback, measured before the optional cross-encoder."""
    open_gate = _gate_open(text, writing=writing)
    picked = [name for name in lexical_shortlist(text, limit=k)
              if name in REGISTRY and name not in _PINNED
              and _allowed(name, writing=open_gate)]
    picked.extend(name for name in _PINNED if name in REGISTRY)
    return sorted(set(picked))


async def _evict_embedder(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    """Best-effort enforcement of the two-model ceiling before reranking."""
    try:
        status = await client.get("/v1/models/status", headers=headers)
        if status.status_code != 200:
            return
        loaded = {m.get("id") for m in status.json().get("models", []) if m.get("loaded")}
        model = embedding_model()
        if model not in loaded:
            return
        await client.post(f"/v1/models/{model}/unload", headers=headers)
        for _ in range(50):
            await asyncio.sleep(0.1)
            status = await client.get("/v1/models/status", headers=headers)
            if status.status_code != 200:
                return
            loaded = {m.get("id") for m in status.json().get("models", [])
                      if m.get("loaded")}
            if model not in loaded:
                return
    except httpx.HTTPError:
        return


async def _rank_one(client: httpx.AsyncClient, headers: dict[str, str], query: str,
                    names: list[str], *, top_n: int) -> list[tuple[str, float]]:
    docs = [" ".join(_docs(REGISTRY[name])) for name in names]
    try:
        response = await client.post(
            "/v1/rerank", headers=headers,
            json={"model": reranker_model(), "query": _QUERY_INSTRUCT + query,
                  "documents": docs, "top_n": min(top_n, len(docs)),
                  "return_documents": False},
        )
    except httpx.HTTPError as exc:
        raise RerankUnavailable(str(exc)) from exc
    if response.status_code != 200:
        raise RerankUnavailable(f"{response.status_code}: {response.text[:240]}")
    out: list[tuple[str, float]] = []
    for row in response.json().get("results", []):
        try:
            out.append((names[int(row["index"])], float(row["relevance_score"])))
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RerankUnavailable("invalid reranker response") from exc
    return out


async def candidates(text: str, *, writing: bool, k: int = DEFAULT_K,
                     shortlist: int = DEFAULT_SHORTLIST,
                     timeout: float = 30.0) -> list[str]:
    """Return a compact tool menu without loading or calling an embedder."""
    clauses = _action_clauses(text)
    pool = lexical_shortlist(text, limit=shortlist)
    open_gate = _gate_open(text, writing=writing)
    pool = [n for n in pool if n in REGISTRY and n not in _PINNED
            and _allowed(n, writing=open_gate)]
    if not pool:
        raise RerankUnavailable("lexical shortlist is empty")

    headers = {"Authorization": f"Bearer {omlx_api_key()}",
               "Content-Type": "application/json"}
    timeout_cfg = httpx.Timeout(timeout, connect=5.0)
    async with httpx.AsyncClient(base_url=omlx_base_url(), timeout=timeout_cfg) as client:
        await _evict_embedder(client, headers)
        if len(clauses) == 1:
            ranked = await _rank_one(client, headers, text, pool, top_n=k)
            picked = [name for name, _ in ranked]
        else:
            # Each action gets independent representation. A single score over a
            # five-action prompt otherwise rewards the dominant clause and drops
            # the remaining four tools before the agent ever sees them.
            per_clause = await asyncio.gather(*(
                _rank_one(client, headers, clause, pool, top_n=_PER_CLAUSE)
                for clause in clauses
            ))
            score: dict[str, float] = {}
            for rows in per_clause:
                for name, value in rows:
                    score[name] = max(score.get(name, float("-inf")), value)
            picked = [name for name, _ in sorted(
                score.items(), key=lambda item: item[1], reverse=True
            )[:k]]
    picked.extend(name for name in _PINNED if name in REGISTRY)
    return sorted(set(picked))
