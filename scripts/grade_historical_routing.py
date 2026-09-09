#!/usr/bin/env python3
"""Strict router-only grade for prompts copied from user debug exports.

No tool body or language model is called. The grade checks whether the router
makes every required action reachable and withholds tools known to have caused
the original failure. This intentionally prefers false failures over false
passes: an alternative group passes only when at least one named alternative
is present, and any forbidden tool is an immediate failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import service.tools  # noqa: F401
from service.router.router import route

DEFAULT_FIXTURE = ROOT / "test_fixtures/routing_regressions/historical_prompts.json"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()
    payload = json.loads(args.fixture.read_text(encoding="utf-8"))
    results = []
    for case in payload["cases"]:
        started = time.perf_counter()
        decision = await route(
            case["prompt"],
            last_assistant=case.get("last_assistant"),
            last_tools=case.get("last_tools"),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        offered = set(decision.tool_subset or [])
        groups = case.get("required_tool_groups", [])
        missing = [group for group in groups if not offered.intersection(group)]
        forbidden = sorted(offered.intersection(case.get("forbidden_tools", [])))
        tools_mismatch = case.get("expect_tools") is False and decision.needs_tools
        state_mismatch = []
        if (expected := case.get("expected_reminder_action")) is not None:
            if decision.reminder_action != expected:
                state_mismatch.append(
                    f"reminder_action={decision.reminder_action!r}, expected={expected!r}")
        if (expected := case.get("expected_clarify_channel")) is not None:
            if decision.clarify_channel is not expected:
                state_mismatch.append(
                    f"clarify_channel={decision.clarify_channel!r}, expected={expected!r}")
        passed = not missing and not forbidden and not tools_mismatch and not state_mismatch
        results.append({
            "id": case["id"], "passed": passed, "elapsed_ms": round(elapsed_ms, 3),
            "offered": sorted(offered), "missing_groups": missing,
            "forbidden_offered": forbidden, "reason": decision.reason,
            "needs_tools": decision.needs_tools,
            "state_mismatch": state_mismatch,
        })
        label = "PASS" if passed else "FAIL"
        details = []
        if missing:
            details.append(f"missing={missing}")
        if forbidden:
            details.append(f"forbidden={forbidden}")
        if tools_mismatch:
            details.append("unexpected tool route")
        details.extend(state_mismatch)
        print(f"{label} {case['id']} {elapsed_ms:8.2f}ms  {decision.reason}" +
              ("  " + " ".join(details) if details else ""))
    passed = sum(r["passed"] for r in results)
    summary = {
        "fixture": str(args.fixture), "passed": passed, "total": len(results),
        "strict_pass_rate": passed / max(1, len(results)), "results": results,
    }
    print(f"\nstrict: {passed}/{len(results)} = {summary['strict_pass_rate']:.2%}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
