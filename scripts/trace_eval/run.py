#!/usr/bin/env python3
"""Run Wisp's real router/agent against exact synthetic tool fixtures.

All tool bodies are replaced by run_routing_stress_suite.bootstrap before a
case is attempted. Only the local model endpoint is reachable. Outbound tools
produce simulated receipts and never call Mail, Messages, or the native bridge.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts import run_routing_stress_suite as stress
from scripts.trace_eval.core import digest, json_safe, load_cases, matches_args, score, verify_manifest

_LAST_STATE: dict = {}


def fixture(name: str, args: dict) -> str:
    state = stress.ACTIVE.get()
    case = state["case"]
    rules = [r for r in case["fixture_rules"] if r["tool"] == name and
             matches_args(args, r.get("args", {}))]
    if len(rules) != 1:
        state["fixture_mismatches"].append({"tool": name, "args": args,
                                            "matching_rules": len(rules)})
        return "(error: no unique synthetic fixture matches this tool and its arguments)"
    rule = rules[0]
    state["fixture_receipts"].append({"tool": name, "args": args,
                                      "status": rule.get("status", "read")})
    return rule["result"]


def stress_case(case: dict) -> dict:
    """Adapt a trace contract to the existing isolated Wisp replay entrypoint."""
    return {**case, "category": case.get("category", "tool_flow"),
            "title": case.get("title", case["id"]),
            "prompt_sha256": hashlib.sha256(case["prompt"].encode()).hexdigest(),
            "required_tools": [r["tool"] for r in case["required_calls"]],
            "forbidden_tools": [], "ordering_edges": case.get("after_result", []),
            "clarification_expected": False}


def synthetic_model_overlay(model: str) -> str:
    """Replace only chat-role IDs; keep packaged non-chat providers unchanged."""
    roles = ("fast", "router", "coding", "reasoning", "agent", "general")
    return "roles:\n" + "".join(f"  {role}: {json.dumps(model)}\n" for role in roles)


async def run_one(case: dict, client, timeout: float) -> tuple[dict, dict]:
    adapted = stress_case(case)
    # run_case creates its own ACTIVE state; wrap its state factory so the
    # argument-sensitive fixture can record exact receipts and mismatches.
    trace = await stress.run_case(adapted, client, "Synthetic trace-eval scenario; no real state.",
                                  timeout, verdict=case.get("simulated_approval", "approve"))
    trace["fixture_mismatches"] = _LAST_STATE.get("fixture_mismatches", [])
    trace["fixture_receipts"] = _LAST_STATE.get("fixture_receipts", [])
    trace["actual_model"] = trace.get("route", {}).get("model")
    trace["clock"] = case["clock"]
    trace["tool_categories"] = {name: tool.category for name, tool in
                                stress.RUNTIME["registry"].items()}
    trace = json_safe(trace)
    return trace, score(case, trace)


async def main_async(args) -> None:
    if args.manifest:
        verify_manifest(args.cases, args.manifest)
    cases = load_cases(args.cases)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise ValueError("no cases selected")
    clocks = {case["clock"] for case in cases}
    if len(clocks) != 1:
        raise ValueError("one replay run must use one frozen clock")
    frozen_clock = next(iter(clocks))
    output = args.output.resolve()
    private_root = (Path(__file__).resolve().parents[2] / ".evo/trace_eval").resolve()
    if not output.is_relative_to(private_root) or output == private_root:
        raise ValueError("output must be a child of the local .evo/trace_eval directory")
    root = Path(__file__).resolve().parents[2]
    sources = [path for path in (root / "service").rglob("*")
               if path.is_file() and path.suffix in {".py", ".yaml", ".json", ".toml"}
               and "__pycache__" not in path.parts]
    sources += [path for path in (root / "scripts/trace_eval").rglob("*.py")
                if "__pycache__" not in path.parts]
    sources.append(root / "scripts/run_routing_stress_suite.py")
    sources.append(root / "docs/WISP_TOOL_ACTIVATION_INVENTORY.json")
    overlay = synthetic_model_overlay(args.model)
    run_spec = {"cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                "requested_model": args.model, "clock": frozen_clock,
                "model_revision": args.model_revision,
                "selected_case_ids": [case["id"] for case in cases],
                "timeout_s": args.timeout,
                "synthetic_overlay_sha256": hashlib.sha256(overlay.encode()).hexdigest(),
                "source_sha256": {str(path.relative_to(root)): hashlib.sha256(
                    path.read_bytes()).hexdigest() for path in sorted(set(sources))}}
    manifest_path = output / "trace_eval_run_manifest.json"
    if output.exists() and any(output.iterdir()):
        if not args.resume or not manifest_path.exists():
            raise ValueError("output is nonempty; use a new path or a compatible --resume")
        if json.loads(manifest_path.read_text()) != run_spec:
            raise ValueError("resume rejected: cases, model, clock, or runner code changed")
    elif args.resume:
        raise ValueError("nothing to resume")
    os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(time, "tzset"):
        time.tzset()
    stress.CLOCK = datetime.fromisoformat(frozen_clock).astimezone(
        ZoneInfo("America/Los_Angeles"))
    stress.EMAIL = "fixture-user@example.test"
    stress.PHONE = "+1-202-555-0142"
    isolated_home = output / "isolated_home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.yaml").write_text(overlay)
    stress.bootstrap(output, args.model, copy_user_context=False)
    stress.dump(manifest_path, run_spec)
    from service.memory import identity
    identity.user_name = lambda: "Fixture User"
    identity.user_emails = lambda: [stress.EMAIL]
    from service.memory import prompt_blocks
    prompt_blocks.memory_block = lambda **kwargs: ""
    prompt_blocks.now_line = lambda **kwargs: (
        "\nThe current date and time is "
        f"{stress.CLOCK:%A, %B %d, %Y at %I:%M %p}, America/Los_Angeles. "
        "Resolve relative dates against this.")
    stress.fixture = fixture
    original_new_state = stress.new_state

    def state_with_receipts(case):
        global _LAST_STATE
        state = original_new_state(case)
        state["fixture_mismatches"] = []
        state["fixture_receipts"] = []
        _LAST_STATE = state
        return state

    stress.new_state = state_with_receipts
    real = stress.RUNTIME["client_type"](timeout=args.timeout)
    await real.ensure_only(args.model)
    client = stress.MeasuredClient(real)
    try:
        for case in cases:  # serial: one local model lane, no timing contention
            path = output / "cases" / f"{case['id']}.json"
            if args.resume and path.exists():
                trace = json.loads(path.read_text())
                if trace.get("id") != case["id"] or trace.get("prompt") != case["prompt"] or \
                        trace.get("clock") != case["clock"] or \
                        trace.get("actual_model") != case.get("expected_model"):
                    raise ValueError(f"resume trace identity mismatch: {case['id']}")
                prior_score = output / "scores" / f"{case['id']}.json"
                if prior_score.exists() and json.loads(prior_score.read_text()).get(
                        "trace_sha256") != digest(trace):
                    raise ValueError(f"resume trace was modified: {case['id']}")
                stress.dump(prior_score, score(case, trace))
                continue
            trace, verdict = await run_one(case, client, args.timeout)
            stress.dump(path, trace)
            stress.dump(output / "scores" / f"{case['id']}.json", verdict)
            print(json.dumps({"id": case["id"], "status": verdict["status"],
                              "failures": verdict["failures"]}), flush=True)
    finally:
        await real.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="Ling-3.0-tiny-oQ6e")
    parser.add_argument("--model-revision", default="unverified-local-artifact",
                        help="record a stable model artifact/revision ID when available")
    parser.add_argument("--ids", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
