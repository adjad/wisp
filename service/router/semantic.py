"""Semantic tool retrieval — the fallback for requests that match no regex rule.

WHY THIS EXISTS
---------------
`router.rule_route` maps intent to a curated tool subset with hand-written
regexes, and every one of those rules was added after a live, verified failure.
That approach is correct and stays exactly as it is for the high-stakes domains
(mail, calendar, messages, system control, files) — but it does not scale to a
registry of several hundred tools, for two independent reasons:

1. Context. All 54 tools registered today serialize to ~11,500 tokens, 72% of a
   16k window before a single user message. Every tool added costs every
   unscoped request its schema size forever.
2. Selection accuracy. Measured on this roster: 5-7 tools offered -> 3/3 correct
   calls; the 16-tool ambiguous menu -> 10/10 on oQ4e; all 44 -> 2/3, and it
   reached for an unrelated tool. A long menu is a reliability problem, not just
   a token one.

So the fallback stops being a fixed list and becomes a RETRIEVAL: embed the
user's message, rank every registered tool by cosine similarity, and hand the
model the best 8-10. The registry can then grow without bound while the model
still sees a short, relevant menu — which is the only number that affects either
the context budget or selection accuracy.

WHAT IT COSTS
-------------
One embedding call (~60-150ms warm, plus a cold load when semantic retrieval is
first needed) against the 320MB embedder, plus a pure-Python cosine pass
(~5-60ms at 500-1000 tools). It
REPLACES ~1,000 tokens of schema prefill relative to the static core set, so net
turn time is roughly flat. It adds zero decode tokens, and decode is 91% of
latency here — see [[moe-decode-dominates-latency]]. The real win is upstream of
all of that: a wrong tool call costs an entire extra agent step (5-20s of
decode), which dwarfs every millisecond spent choosing correctly.

ASYMMETRY
---------
Qwen3-Embedding is an asymmetric model: queries carry an instruction prefix,
passages go in bare. `embedder.embed_queries` applies the prefix and `_embed`
does not, so tool documents go through `_embed` and user messages through
`embed_queries`. Getting this backwards silently degrades ranking rather than
failing, so it is worth stating.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

from service.paths import MOE_DIR
from service.search.embedder import (EmbedUnavailable, _embed, _normalize,
                                     doc_key, embed_queries, embedding_model)
from service.tools.registry import REGISTRY, Tool

_CACHE_PATH = MOE_DIR / "cache" / "tool_vectors.json"

# How many tools the model is ultimately offered. 8-10 keeps the menu inside the
# regime this roster measures as reliable (see the module docstring) while
# leaving room for the pinned floor below.
DEFAULT_K = 10

# Keep only candidates scoring at least this fraction of the top hit.
#
# Cosine scores are NOT comparable across queries — measured on the live index,
# 0.45 is the top hit for "play some jazz" while 0.50 is only 8th for "what's on
# my calendar tomorrow" — so an absolute threshold cannot work. A RELATIVE one
# can, because the useful signal is the cliff after the real answers: jazz scores
# 0.4496 / 0.4449 and then drops to 0.3125, and everything past that drop is
# noise (get_stock_price, list_bluetooth_devices).
#
# Set conservatively. This is a token/precision optimization, not a recall
# mechanism: a 2-tool menu beats a 12-tool one on both prompt size and selection
# accuracy WHEN the winner is unambiguous, but trimming a candidate the user
# actually wanted is the expensive failure. 0.78 only ever cuts past a genuine
# cliff, and _MIN_KEEP guarantees the model is never left with a menu too short
# to recover from a near-miss at rank 1.
_REL_FLOOR = 0.78
_MIN_KEEP = 5

# Always offered regardless of what retrieval returns.
#
# `run_shell` is the escape hatch — the one tool that can reach anything the
# others can't, and it is confirm-gated so the user sees it before it runs. It
# was in the static `_CORE_TOOLS` for exactly this reason and retrieval must not
# quietly remove it. `recall` is here because a conversational follow-up ("what
# did I say about that?") is the single most common thing to land on this route,
# and it embeds poorly — the user's phrasing names the FACT, never the act of
# remembering it.
_PINNED = ("recall", "run_shell")

# Categories a request must show write intent to reach.
#
# SCOPED TO WHAT THE POLICY ENGINE DOES NOT ALREADY CONFIRM. The first version
# of this gate listed everything that mutates — sends, calendar writes, tool
# authoring, network writes — and that was redundant twice over. Measured
# against `safety.policy.decide` under this machine's config (`full_access:
# true`), those categories all resolve to CONFIRM regardless of access mode:
# the user sees the action before it happens, which is a far stronger guarantee
# than hiding the schema. Only `fs_write`, `fs_delete` and `shell` come back
# ALLOW — they run silently — and `shell` is pinned below as the deliberate
# escape hatch.
#
# So the gate now covers exactly the silent-and-irreversible pair. This is the
# `rm -rf three weeks of log files` class: nothing in a vector says one
# neighbor is destructive, and no confirmation prompt will catch it afterwards.
#
# Narrowing this back was not a softening. The wide version cost real recall for
# nothing: on the alias eval, every gated miss sat at rank 0 — perfect
# retrieval, discarded by a gate whose protection the policy engine was already
# providing. And each of those misses is the failure mode the router's own
# comments keep recording: the model reasons correctly toward an action, has no
# way to perform it, and tells the user it cannot.
_GATED_CATEGORIES = frozenset({"fs_write", "fs_delete"})

# Vocabulary that means "act on my files", for the gate above.
#
# `router.has_write_intent` is the general write test and it is tuned for a
# different job — deciding whether an ALREADY-MATCHED domain route needs its
# write tools. Reused as a visibility gate it misses most of how people actually
# ask for file work: measured, it returns False for "tidy up my downloads",
# "reorganize my desktop", "throw away the old installer" and "file those away
# somewhere else", each of which is unambiguously a write.
#
# Widening `_WRITE_INTENT_RE` itself would be the wrong fix — it is load-bearing
# for every domain route, and its comments record several incidents caused by it
# matching too eagerly on reads. A separate, narrow pattern used only here keeps
# that one untouched.
_FILE_ACTION_RE = re.compile(
    r"\b(?:tidy|reorgani[sz]e|organi[sz]e|clean\s*up|sort|declutter|"
    r"rename|move|copy|zip|unzip|compress|extract|"
    r"throw\s+(?:away|out)|get\s+rid\s+of|stash|"
    r"trash|delete|remove|wipe|purge|"
    r"save|write|overwrite|back\s*up|archive)\b|"
    # "file those away", "put the old ones away" — `file` and `put` are only
    # write verbs when paired with "away". Bare \bfile\b would match "what's in
    # this file", which is a read, and bare \bput\b would match "put something
    # on" (music). The trailing particle is what disambiguates them.
    r"\b(?:files?d?|put)\s+(?:\w+\s+){0,3}away\b", re.I)


def _docs(tool: Tool) -> list[str]:
    """The documents embedded for one tool — its definition, plus ONE PER ALIAS.

    Multi-vector, and that is the whole design. The obvious approach is to
    concatenate name + description + every alias into a single document and
    embed it once; measured on the live index that scored 77.5% on the tools'
    OWN alias text, which should be a near-perfect case. Averaging a formal
    description together with five colloquial phrasings produces a centroid that
    sits close to none of them — the specific wording that would have matched is
    exactly what gets blurred away.

    Scoring each alias separately and taking the MAX over a tool's vectors keeps
    every phrasing sharp: "it's way too loud" is allowed to match `set_volume`
    strongly on its own terms without having to out-vote the tool's paragraph of
    prose. `embedder.rank` already takes max-not-mean across sub-queries for the
    same reason, stated in its own docstring.

    Aliases are example USER UTTERANCES. A tool is named for what it does to the
    machine while a person asks for what they want, and closing that gap is what
    these are for. They cost nothing at inference — they enter this index and no
    prompt, ever.
    """
    return [f"{tool.name.replace('_', ' ')}. {tool.description}", *tool.aliases]


def _load_cache() -> dict[str, list[float]]:
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/corrupt cache just means "rebuild"
        return {}
    if raw.get("model") != embedding_model():
        # Vectors from a different embedder are not comparable with new ones.
        return {}
    vecs = raw.get("vectors")
    return vecs if isinstance(vecs, dict) else {}


def _save_cache(vectors: dict[str, list[float]]) -> None:
    """Best-effort persist. A failure here costs a re-embed at next startup,
    never correctness — the in-memory index is already built either way."""
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 5 decimals on a unit-normalized vector is far below the margin that
        # separates ranked candidates, and it roughly halves the file.
        payload = {"model": embedding_model(),
                   "vectors": {k: [round(x, 5) for x in v] for k, v in vectors.items()}}
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_CACHE_PATH)
    except Exception:  # noqa: BLE001
        pass


class ToolIndex:
    """Name -> unit vector for every registered tool.

    Keyed on the CONTENT of the document rather than the tool name, so editing
    a description or adding an alias re-embeds exactly that one tool and every
    other cache entry survives. Skills register and unregister tools at runtime,
    so the index also has to be cheap to refresh — `build` only embeds what it
    has never seen.
    """

    def __init__(self) -> None:
        # Flat parallel arrays rather than dict-of-lists: scoring walks every
        # vector on every ambiguous request, and at ~200 tools x ~5 documents
        # that is a thousand dot products per turn. One tight loop over two flat
        # lists is meaningfully faster than a nested walk, and this is the only
        # place the shape matters.
        self._owner: list[str] = []                  # row -> tool name
        self._rows: list[list[float]] = []           # row -> unit vector
        self._keys: dict[str, list[str]] = {}        # tool name -> its doc keys
        self._by_key: dict[str, list[float]] = {}    # doc key -> vector (the cache)
        self._lock = asyncio.Lock()
        self._loaded = False

    async def build(self, *, timeout: float = 60.0) -> int:
        """Embed every registered tool document that isn't already cached.
        Returns the number of documents embedded this pass (0 on a warm start)."""
        # Before hashing any document: aliases are PART of the embedded text, so
        # applying them after a build would leave every cache key stale and
        # force a full re-embed on the next request.
        from service.router.tool_aliases import apply as apply_aliases
        apply_aliases()
        async with self._lock:
            if not self._loaded:
                self._by_key = _load_cache()
                self._loaded = True

            wanted: dict[str, list[str]] = {n: _docs(t) for n, t in REGISTRY.items()}
            keys = {n: [doc_key(d) for d in docs] for n, docs in wanted.items()}

            missing: dict[str, str] = {}
            for name, docs in wanted.items():
                for key, doc in zip(keys[name], docs):
                    if key not in self._by_key:
                        missing[key] = doc

            embedded = 0
            if missing:
                order = list(missing)
                vecs = await _embed([missing[k] for k in order], timeout=timeout)
                for k, v in zip(order, vecs):
                    self._by_key[k] = _normalize(v)
                embedded = len(order)

            self._owner, self._rows = [], []
            for name in wanted:
                for key in keys[name]:
                    vec = self._by_key.get(key)
                    if vec is not None:
                        self._owner.append(name)
                        self._rows.append(vec)
            self._keys = keys

            if embedded:
                # Drop entries for documents no longer referenced, so an edited
                # description or an uninstalled skill doesn't leave vectors
                # accumulating in the file forever.
                live = {k for ks in keys.values() for k in ks}
                self._by_key = {k: v for k, v in self._by_key.items() if k in live}
                _save_cache(self._by_key)
            return embedded

    def is_stale(self) -> bool:
        """True when the registry has changed since the last build — a skill
        registered a tool, an MCP server connected, or a description was edited."""
        if len(self._keys) != len(REGISTRY):
            return True
        return any(self._keys.get(n) != [doc_key(d) for d in _docs(t)]
                   for n, t in REGISTRY.items())

    async def rank(self, text: str, *, timeout: float = 10.0) -> list[tuple[str, float]]:
        """Every registered tool, scored against `text`, best first.

        A tool's score is the MAX over its documents, never the mean — see
        `_docs`. One alias matching well IS the signal; the tool's other four
        aliases describing unrelated phrasings must not dilute it.
        """
        if self.is_stale() or not self._rows:
            await self.build()
        if not self._rows:
            raise EmbedUnavailable("tool index is empty")
        qv = (await embed_queries([text], timeout=timeout))[0]
        best: dict[str, float] = {}
        for name, vec in zip(self._owner, self._rows):
            s = sum(a * b for a, b in zip(qv, vec))
            if s > best.get(name, -2.0):
                best[name] = s
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


_INDEX = ToolIndex()


def index() -> ToolIndex:
    return _INDEX


async def warm() -> int:
    """Explicitly build the tool index.

    This is no longer called at Wisp startup, so merely opening the app does
    not load the embedding model. The normal candidate path builds lazily on
    the first ambiguous request. Best-effort callers may still invoke this
    manually; failure leaves the router's static-core fallback available.
    """
    try:
        return await _INDEX.build()
    except EmbedUnavailable:
        return 0


def _allowed(name: str, *, writing: bool) -> bool:
    tool = REGISTRY.get(name)
    if tool is None:
        return False
    return writing or tool.category not in _GATED_CATEGORIES


def _gate_open(text: str, *, writing: bool) -> bool:
    """Whether file-mutating tools may be offered for this request at all."""
    return writing or bool(_FILE_ACTION_RE.search(text))


async def candidates(text: str, *, writing: bool, k: int = DEFAULT_K,
                     timeout: float = 10.0) -> list[str]:
    """The tool names to offer for a request that matched no regex rule.

    Raises EmbedUnavailable if the embedder can't be reached — the caller must
    fall back to the static core list rather than offering nothing.

    Returned SORTED, not in rank order. The model reads a menu, not a ranking,
    and ordering by score means two phrasings of the same request produce two
    different prompt prefixes — which costs a prompt-cache hit on oMLX's
    single-entry cache for no benefit. Rank decides membership; sorting decides
    presentation.
    """
    scored = await _INDEX.rank(text, timeout=timeout)
    open_gate = _gate_open(text, writing=writing)
    eligible = [(n, s) for n, s in scored
                if n not in _PINNED and _allowed(n, writing=open_gate)]
    picked: list[str] = []
    if eligible:
        cutoff = eligible[0][1] * _REL_FLOOR
        for i, (name, score) in enumerate(eligible[:k]):
            if i >= _MIN_KEEP and score < cutoff:
                break
            picked.append(name)
    picked.extend(n for n in _PINNED if n in REGISTRY)
    return sorted(set(picked))
