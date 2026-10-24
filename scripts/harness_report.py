#!/usr/bin/env python3
"""Where agent-turn time goes, from the local diagnostics journal. Read-only.

Usage: python scripts/harness_report.py [--dir ~/.moe/diagnostics] [--slowest 5] [--json]

Splits each agent turn into model time (the `model_step` rows that
agent/loop._run_step records), tool time (tool_call -> tool_result) and the
rest, which is Wisp's own overhead: routing, readiness checks, the oMLX
transport and request assembly. Also counts the model calls a turn wasted:
empty responses (a retry follows) and prefix resets (a step offered a
different tool set than the step before, so oMLX re-prefilled the prompt
after the system text).

No service import, database, network, model or subprocess. The journal is
content-free by construction (service/diagnostics.py), so neither is this.
Turns recorded before model_step existed report model time as unknown.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path


def _default_dir() -> Path:
    home = os.environ.get("WISP_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".moe") / "diagnostics"


def turn_breakdown(trace: dict) -> dict | None:
    """One agent trace -> its time split, or None for other kinds/empty traces."""
    if trace.get("kind") != "agent":
        return None
    events = [e for e in trace.get("events") or () if isinstance(e, dict)]
    if not events:
        return None
    total = max(int(e.get("elapsed_ms") or 0) for e in events)
    steps = [e for e in events if e.get("stage") == "model_step"]
    model_ms = sum(int(e.get("model_ms") or 0) for e in steps)
    tool_ms = 0
    pending = None
    for e in events:
        if e.get("stage") == "tool_call" and pending is None:
            pending = int(e.get("elapsed_ms") or 0)
        elif e.get("stage") == "tool_result" and pending is not None:
            tool_ms += max(0, int(e.get("elapsed_ms") or 0) - pending)
            pending = None
    routed = next((int(e.get("elapsed_ms") or 0) for e in events if e.get("stage") == "routed"), None)
    instrumented = bool(steps)
    return {
        "trace_id": trace.get("trace_id"),
        "started_at": trace.get("started_at"),
        "status": trace.get("status"),
        "total_ms": total,
        "route_ms": routed,
        "model_calls": len(steps),
        "model_ms": model_ms if instrumented else None,
        "ttft_ms": sum(int(e.get("ttft_ms") or 0) for e in steps) if instrumented else None,
        "tool_ms": tool_ms,
        "overhead_ms": max(0, total - model_ms - tool_ms) if instrumented else None,
        "empty_calls": sum(1 for e in steps if e.get("empty") is True),
        "prefix_resets": sum(1 for e in steps if e.get("prefix_reset") is True),
    }


def load(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.json")):
        try:
            trace = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        row = turn_breakdown(trace) if isinstance(trace, dict) else None
        if row is not None:
            rows.append(row)
    return rows


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def summarize(rows: list[dict]) -> dict:
    model_turns = [r for r in rows if r["model_calls"]]
    calls = sum(r["model_calls"] for r in model_turns)
    out = {
        "agent_turns": len(rows),
        "turns_without_model": len(rows) - len(model_turns),
        "model_turns": len(model_turns),
    }
    for key in ("total_ms", "model_ms", "ttft_ms", "tool_ms", "overhead_ms"):
        values = [r[key] for r in model_turns if r[key] is not None]
        out[key] = {"p50": _pct(values, 0.5), "p90": _pct(values, 0.9),
                    "share": round(sum(values) / max(1, sum(r["total_ms"] for r in model_turns)), 3)}
    out["model_calls_per_turn_p50"] = statistics.median([r["model_calls"] for r in model_turns]) if model_turns else None
    out["empty_call_rate"] = round(sum(r["empty_calls"] for r in model_turns) / calls, 3) if calls else None
    out["prefix_reset_rate"] = round(sum(r["prefix_resets"] for r in model_turns) / calls, 3) if calls else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=_default_dir())
    parser.add_argument("--slowest", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()
    rows = load(args.dir)
    summary = summarize(rows)
    slowest = sorted((r for r in rows if r["model_calls"]), key=lambda r: -r["total_ms"])[:args.slowest]
    if args.json:
        print(json.dumps({"summary": summary, "slowest": slowest}, indent=2))
        return 0
    print(f"{summary['agent_turns']} agent turns, {summary['model_turns']} with model calls "
          f"({summary['turns_without_model']} answered without the model)")
    if not summary["model_turns"]:
        print("No model_step rows yet: they are recorded from this change on.")
        return 0
    for key, label in (("total_ms", "turn"), ("model_ms", "model"), ("ttft_ms", "  of it to first token"),
                       ("tool_ms", "tools"), ("overhead_ms", "Wisp overhead")):
        s = summary[key]
        print(f"  {label:24} p50 {s['p50']} ms   p90 {s['p90']} ms   {s['share']:.0%} of turn time")
    print(f"  model calls per turn p50 {summary['model_calls_per_turn_p50']}, "
          f"empty {summary['empty_call_rate']:.0%}, prefix resets {summary['prefix_reset_rate']:.0%}")
    print("Slowest turns:")
    for r in slowest:
        print(f"  {r['trace_id']}  {r['total_ms']} ms = model {r['model_ms']} + tools {r['tool_ms']} "
              f"+ overhead {r['overhead_ms']}  ({r['model_calls']} calls, {r['empty_calls']} empty, "
              f"{r['prefix_resets']} resets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
