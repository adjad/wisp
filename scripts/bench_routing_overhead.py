#!/usr/bin/env python3
"""Compare lexical ranking reuse with the uncached algorithm, entirely offline.

Run in a fresh process: python -B scripts/bench_routing_overhead.py --output /tmp/routes.json
This measures Python routing CPU, not engine TTFT, model accuracy or UI latency.
Tools are inert; sockets, subprocesses and credentials are blocked during probes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("no_tool", "hello", {}),
    ("ambiguous", "I could use some help", {}),
    ("tool", "open Safari", {}),
    ("tool", "run the tests", {}),
    ("read", "what's my battery", {}),
    ("read", "summarize my messages", {}),
    ("read", "summarize my emails", {}),
    ("read", "what's on my calendar tomorrow", {}),
    ("quoted", 'Explain the phrase "lock my screen".', {}),
    ("negative", "Do not lock my screen.", {}),
    ("quoted", 'Read the word "clipboard" aloud.', {}),
    ("quoted", 'What does "delete all files" mean?', {}),
    ("negative", "do not summarize my messages", {}),
    ("scope", "show my work calendar only, without reminders", {}),
    ("scope", "summarize only unread emails from Priya today", {}),
    ("scope", "summarize my notes without reading email or messages", {}),
    ("temporal", "what's on my calendar next Friday", {}),
    ("temporal", "what's on my calendar between October 5 and October 9", {}),
    ("outbound", "text Mom running late", {}),
    ("outbound", "text +1 650 555 0134 that I'm outside", {}),
    ("outbound", "text (650) 555-0134 that I'm outside", {}),
    ("outbound", "email jane@example.test asking to move our meeting", {}),
    ("outbound", "email jane@example.test my calendar tomorrow", {}),
    ("compositional", "summarize my emails and text it to Mom", {}),
    ("compositional", "Check my calendar; then search notes for the agenda; do not send anything", {}),
    ("compositional", "Check my calendar tomorrow. Text Mom the result. Save the summary to a file.", {}),
    ("compositional", "Open Safari. Open Safari. Open Safari.", {}),
    ("followup", "from yesterday", {"last_tools": "summarize_messages", "last_user": "summarize my messages"}),
    ("followup", "move the first one to 4pm", {"last_tools": "get_upcoming", "last_user": "what's on my calendar tomorrow", "last_assistant": "1. Fixture meeting at 2pm."}),
]


def uncached_shortlist(retrieval, text: str, *, limit: int = 60) -> list[str]:
    """Pre-change reference: retain every rank-fusion contribution."""
    score: dict[str, float] = {}
    for query, depth in [(text, 30), *[(c, 15) for c in retrieval._action_clauses(text)]]:
        for rank, name in enumerate(retrieval.lexical_rank(query)[:depth]):
            score[name] = score.get(name, 0.0) + 1.0 / (20 + rank)
    return [name for name, _ in sorted(
        score.items(), key=lambda item: (item[1], item[0]), reverse=True
    )[:limit]]


def summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"n": len(values), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if len(ordered) >= 20 else None}


def trace(decision) -> dict:
    return {**decision.as_dict(), "tool_subset": decision.tool_subset,
            "expect_tool_first": decision.expect_tool_first,
            "force_first_tool": decision.force_first_tool,
            "clarify_channel": decision.clarify_channel,
            "clarify_target": decision.clarify_target}


def forbidden(*args, **kwargs):
    raise AssertionError("Offline benchmark forbids network, processes, credentials and native execution")


async def benchmark(reps: int, *, stress: bool = False) -> dict:
    if "service.paths" in sys.modules:
        raise RuntimeError("Run this benchmark in a fresh process before importing Wisp")
    # This one fixed read-only Git command records provenance before the
    # process guard; the guarded region never starts a subprocess.
    source_sha = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    paths = ["service/router/reranker.py", "service/router/router.py", "service/config/models.yaml"]
    source_digests = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}
    with tempfile.TemporaryDirectory(prefix="wisp-routing-bench-") as scratch, ExitStack() as stack:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WISP_")}
        env.update(WISP_HOME=scratch, WISPAIR_HOME=str(Path(scratch) / "air"), PYTHONDONTWRITEBYTECODE="1")
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("socket.socket.connect", forbidden))
        stack.enter_context(patch("socket.socket.connect_ex", forbidden))
        stack.enter_context(patch("subprocess.Popen", forbidden))
        from service import config
        stack.enter_context(patch.object(config, "OMLX_SETTINGS", Path(scratch) / "absent-settings.json"))
        stack.enter_context(patch.object(config, "OMLX_MODEL_SETTINGS", Path(scratch) / "absent-models.json"))
        stack.enter_context(patch.object(config, "omlx_api_key", forbidden))
        stack.enter_context(patch.object(config, "_credential", forbidden))
        import service.tools  # noqa: F401
        from service.tools.registry import REGISTRY
        stack.enter_context(patch.dict(REGISTRY, {n: replace(t, func=forbidden) for n, t in REGISTRY.items()}))
        from service.router import router, reranker
        if (config.models_config().get("tool_retrieval") or {}).get("provider") != "lexical":
            raise RuntimeError("Offline benchmark requires the packaged lexical retrieval configuration")
        candidate = reranker.lexical_shortlist

        def reference(text, *, limit=reranker.DEFAULT_SHORTLIST):
            return uncached_shortlist(reranker, text, limit=limit)

        reranker._lexical_index()
        cases = list(CASES)
        if stress:
            cases.append(("repeated_sentence_stress", "Help me reason about this synthetic project. "
                          + "Synthetic project work items include fixture alpha beta gamma. " * 100, {}))
        rows = []
        for kind, prompt, context in cases:
            timings = {metric: {arm: [] for arm in ("reference", "candidate")}
                       for metric in ("route", "standalone_lexical_candidates")}
            calls = {arm: [] for arm in ("reference", "candidate")}
            decisions = {}
            for rep in range(reps):
                pair = {}
                arms = ("reference", "candidate") if rep % 2 == 0 else ("candidate", "reference")
                for arm in arms:
                    fn = reference if arm == "reference" else candidate
                    with patch.object(reranker, "lexical_shortlist", fn):
                        start = time.perf_counter()
                        decision = trace(await router.route(prompt, **context))
                        timings["route"][arm].append((time.perf_counter() - start) * 1000)
                        if arm in decisions and decision != decisions[arm]:
                            raise AssertionError("Unstable route trace")
                        decisions[arm] = decision
                        with patch.object(reranker, "lexical_rank", wraps=reranker.lexical_rank) as ranked:
                            start = time.perf_counter()
                            names = reranker.lexical_candidates(prompt, writing=router.has_write_intent(prompt))
                            timings["standalone_lexical_candidates"][arm].append((time.perf_counter() - start) * 1000)
                            calls[arm].append(ranked.call_count)
                        pair[arm] = names
                if pair["reference"] != pair["candidate"] or decisions["reference"] != decisions["candidate"]:
                    raise AssertionError("Rank reuse changed a tool menu or route contract")
            rows.append({"kind": kind, "prompt": prompt, "context": context,
                         "timings": {m: {a: summary(v) for a, v in arms.items()} for m, arms in timings.items()},
                         "rank_calls": {a: sorted(set(v)) for a, v in calls.items()},
                         "identical_menu_and_route": True, "trace": decisions["candidate"],
                         "intent_correctness": "ungraded; equality does not establish correct intent"})
        # Controlled cold state is an in-process BM25 index reset only. It
        # never unloads a model or changes production configuration.
        cold = {}
        for arm, fn in (("reference", reference), ("candidate", candidate)):
            values = []
            for _ in range(reps):
                reranker._LEXICAL = None
                with patch.object(reranker, "lexical_shortlist", fn):
                    start = time.perf_counter()
                    reranker.lexical_candidates("open Safari", writing=True)
                    values.append((time.perf_counter() - start) * 1000)
            cold[arm] = summary(values)
        return {"source_sha": source_sha, "source_sha256": source_digests,
                "python": sys.version, "platform": {"system": os.uname().sysname, "machine": os.uname().machine},
                "packaged_python": "3.13.14", "retrieval": "lexical", "registered_tools": len(REGISTRY),
                "routable_tools": len(reranker._lexical_index().names), "engine_ttft": None,
                "engine_ttft_note": "unmeasured: no engine/backend calls", "ui_and_queue_latency": None,
                "cold_index_definition": "fresh in-process BM25 index; imports/engine cold load excluded",
                "cold_index_standalone_candidates": cold, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=30)
    parser.add_argument("--stress", action="store_true", help="Include the expensive synthetic repeated-sentence case")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.reps < 1:
        parser.error("--reps must be positive")
    sys.path.insert(0, str(ROOT))
    sys.dont_write_bytecode = True
    result = asyncio.run(benchmark(args.reps, stress=args.stress))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "source_sha": result["source_sha"], "cases": len(result["rows"]),
                      "reps": args.reps, "identical_menus_and_routes": True, "engine_ttft": "unmeasured"}))


if __name__ == "__main__":
    main()
