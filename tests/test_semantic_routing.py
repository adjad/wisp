"""The semantic fallback must retrieve the right tools without arming the wrong ones.

`router.route`'s ambiguous path used to hand out a fixed 14-name list
(`_CORE_TOOLS`). It now embeds the request and retrieves ~10 tools out of the
whole registry, so the registry can grow past 500 entries without the model's
menu — or the context budget — growing with it.

Two properties have to hold together, and they pull in opposite directions:

  RECALL — the tool the user needs must be IN the menu. A tool that isn't
  offered cannot be called, and the observed failure is not an error but a
  confident "I can't do that" (see `_write_continuation_subset` in router.py for
  the incident that shape came from).

  THE GATE — file-mutating tools must NOT be offered on a read. `fs_write` and
  `fs_delete` are the only categories `safety.policy.decide` returns ALLOW for
  under this machine's `full_access: true` config, so nothing downstream will
  catch a wrong pick: it just happens. Every other mutating category resolves
  to CONFIRM and does not need hiding.

Both directions are checked here because tightening either one silently breaks
the other — the gate started far wider and cost 36 of 160 alias queries, every
one of them ranked FIRST before the gate discarded it.

Runs fully offline: the embedder is stubbed, so this stays a logic test and
never depends on oMLX being up. `scripts/test_retrieval.py` is the live
counterpart that measures real recall against the real model.

    .venv/bin/python tests/test_semantic_routing.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from service.router import semantic  # noqa: E402
from service.router.router import has_write_intent  # noqa: E402
from service.tools.registry import REGISTRY  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    # Deterministic word-overlap stand-in for the embedder — see
    # tests/stub_embedder.py for why these assertions must not depend on oMLX.
    from tests.stub_embedder import install
    install()

    print("retrieval")
    got = await semantic.candidates("read the contents of a file", writing=False)
    check("a read request retrieves tools", len(got) > 0, f"got {got}")
    check("menu stays small", len(got) <= semantic.DEFAULT_K + len(semantic._PINNED),
          f"{len(got)} tools")

    print("\npinned floor")
    for probe in ("something totally unrelated to anything", "xyzzy plugh"):
        got = await semantic.candidates(probe, writing=False)
        missing = [p for p in semantic._PINNED if p in REGISTRY and p not in got]
        check(f"pinned survive {probe[:28]!r}", not missing, f"missing {missing}")

    print("\nthe write gate")
    gated = [n for n, t in REGISTRY.items() if t.category in semantic._GATED_CATEGORIES]
    check("registry has gated tools to test", bool(gated))

    # A read must not surface them, no matter how the ranking falls.
    for read_q in ("what is in my downloads folder", "show me the file contents",
                   "how many files are on my desktop"):
        got = await semantic.candidates(
            read_q, writing=semantic._gate_open(read_q, writing=False), k=40)
        leaked = [t for t in got if t in gated]
        check(f"no fs write/delete on {read_q[:30]!r}", not leaked, f"leaked {leaked}")

    # And a genuine file action must open it — this is the half that regressed
    # when the gate leaned on has_write_intent alone.
    for write_q in ("tidy up my downloads folder", "reorganize my desktop",
                    "throw away the old installer", "file those away somewhere else",
                    "delete that screenshot"):
        check(f"gate opens for {write_q[:30]!r}",
              semantic._gate_open(write_q, writing=has_write_intent(write_q)))

    print("\nthe safety net")
    # Inject failure into the SELECTED provider. Packaged routing is lexical;
    # patching only the inactive embedder never exercised the safety net.
    from service.router import router, reranker
    from service.search.embedder import EmbedUnavailable
    for provider, module, method, mock_type in (
            ("embedding", semantic, "candidates", AsyncMock),
            ("reranker", reranker, "candidates", AsyncMock),
            ("lexical", reranker, "lexical_candidates", Mock)):
        for failure in ("exception", "empty"):
            selected = mock_type(**(
                {"side_effect": EmbedUnavailable("simulated provider failure")}
                if failure == "exception" else {"return_value": []}))
            with patch.object(router, "models_config", return_value={
                    "tool_retrieval": {"provider": provider}}), \
                    patch.object(module, method, selected):
                fallback = await router._semantic_core("anything at all")
            check(f"{provider}/{failure} actually calls the selected provider",
                  selected.call_count == 1)
            check(f"{provider}/{failure} returns exactly the nonempty static core",
                  bool(fallback) and fallback == router._core_tools(), f"got {fallback}")
    inactive = AsyncMock(side_effect=AssertionError("inactive embedding provider called"))
    with patch.object(router, "models_config", return_value={
            "tool_retrieval": {"provider": "lexical"}}), \
            patch.object(semantic, "candidates", inactive):
        lexical = await router._semantic_core("anything at all")
    check("lexical routing does not invoke the embedding provider", inactive.call_count == 0)
    check("lexical routing returns lexical candidates, not static-core membership",
          lexical == reranker.lexical_candidates("anything at all", writing=False))

    print("\nindex maintenance")
    idx = semantic.index()
    await idx.build()
    check("index covers every registered tool",
          set(idx._owner) == set(REGISTRY),
          f"{len(set(idx._owner))} indexed vs {len(REGISTRY)} registered")
    check("multi-vector: more rows than tools", len(idx._rows) > len(set(idx._owner)),
          f"{len(idx._rows)} rows / {len(set(idx._owner))} tools")
    check("a rebuilt index is not stale", not idx.is_stale())

    # Editing a description must invalidate exactly that tool, not the world.
    victim = next(iter(REGISTRY))
    original = REGISTRY[victim].description
    REGISTRY[victim].description = original + " (edited)"
    check("an edited description marks the index stale", idx.is_stale())
    n = await idx.build()
    check("only the edited tool re-embeds", n == 1, f"re-embedded {n} documents")
    REGISTRY[victim].description = original
    await idx.build()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
