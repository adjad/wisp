#!/usr/bin/env python3
"""Run the adversarial router corpus through Wisp in intercepted test mode.

Every case is forced to ``mode='plan'`` regardless of its corpus mode. Wisp's
real router and resident model choose tools normally, but the agent loop returns
synthetic TEST MODE results before any tool function runs. No Mail launch,
compose window, send, write, deletion, or setting change can occur.

Examples:
    .venv/bin/python scripts/eval_router_adversarial.py --only live_regression
    .venv/bin/python scripts/eval_router_adversarial.py --only compound --repeat 3
    .venv/bin/python scripts/eval_router_adversarial.py --ids capability_matrix,seq66_t3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import eval_core
from scripts.eval_ling_tasks import Task
from tests.router_adversarial_cases import CASES, PromptCase

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")


def _safe_json(results: list["LiveResult"]) -> str:
    """Serialize results while stripping contact details injected by context."""
    payload = [asdict(item) | {"passed_tool_contract": item.passed_tool_contract}
               for item in results]
    text = json.dumps(payload, indent=2)
    return _PHONE_RE.sub("[REDACTED_PHONE]", _EMAIL_RE.sub("[REDACTED_EMAIL]", text))


@dataclass
class LiveResult:
    id: str
    category: str
    prompt: str
    repetition: int
    route: dict
    tools: list[dict]
    answer: str
    error: str
    elapsed_s: float
    missing_required: list[str]
    missing_one_of: list[str]
    called_forbidden: list[str]
    contract_tools: list[str]
    arg_mismatches: list[str]

    @property
    def passed_tool_contract(self) -> bool:
        return not (self.missing_required or self.missing_one_of or self.called_forbidden
                    or self.arg_mismatches)


def _task(case: PromptCase) -> Task:
    # mode=plan is unconditional: even corpus read cases are selection tests in
    # this harness. expect is used only by eval_core's confirmation guard; all
    # confirmations are still denied because live_writes remains False.
    expected = tuple(dict.fromkeys((*case.required, *case.one_of)))
    return Task(case.id, case.category, case.prompt, expected, mode="plan",
                grade_for=case.expected)


async def run_case(client: httpx.AsyncClient, case: PromptCase, repetition: int) -> LiveResult:
    turn = await eval_core.run_turn(client, _task(case), case.prompt, live_writes=False)
    called = {tool["name"] for tool in turn.tools}
    contract_tools = {name for group in turn.route.get("required_tool_groups", [])
                      for name in group}
    planned = called | contract_tools
    missing_required = sorted(set(case.required) - planned)
    missing_one_of = (sorted(case.one_of) if case.one_of and not (set(case.one_of) & planned)
                      else [])
    called_forbidden = sorted(set(case.forbidden) & called)
    arg_mismatches: list[str] = []
    for tool_name, key, expected in case.required_args:
        matching = [tool.get("args", {}) for tool in turn.tools if tool.get("name") == tool_name]
        if not any(args.get(key) == expected for args in matching):
            arg_mismatches.append(f"{tool_name}.{key}={expected!r}")
    return LiveResult(
        id=case.id, category=case.category, prompt=case.prompt, repetition=repetition,
        route=turn.route, tools=turn.tools, answer=turn.answer, error=turn.error,
        elapsed_s=turn.elapsed_s, missing_required=missing_required,
        missing_one_of=missing_one_of, called_forbidden=called_forbidden,
        contract_tools=sorted(contract_tools), arg_mismatches=arg_mismatches,
    )


async def main_async(args) -> int:
    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    ids = {part.strip() for part in args.ids.split(",") if part.strip()}
    selected = [case for case in CASES
                if (not wanted or case.category in wanted or case.id in wanted)
                and (not ids or case.id in ids)]
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print("No cases matched.")
        return 2

    output = Path(args.output)
    results: list[LiveResult] = []
    async with httpx.AsyncClient() as client:
        try:
            health = (await client.get(f"{eval_core.BASE}/health", timeout=15)).json()
        except Exception as exc:  # noqa: BLE001
            print(f"Wisp is not reachable at {eval_core.BASE}: {exc}")
            return 1
        print(f"Wisp {health.get('default_model', '?')} · {len(selected)} cases × {args.repeat} "
              "· intercepted TEST MODE", flush=True)

        for repetition in range(1, args.repeat + 1):
            for index, case in enumerate(selected, 1):
                started = time.monotonic()
                result = await run_case(client, case, repetition)
                results.append(result)
                output.write_text(_safe_json(results))
                called = [tool["name"] for tool in result.tools]
                status = "PASS" if result.passed_tool_contract and not result.error else "FAIL"
                print(f"[{status}] r{repetition} {index}/{len(selected)} {case.id} "
                      f"({time.monotonic() - started:.1f}s) tools={called}", flush=True)
                if result.contract_tools:
                    print(f"       contract: {', '.join(result.contract_tools)}", flush=True)
                for label, values in (("missing", result.missing_required),
                                      ("one-of missing", result.missing_one_of),
                                      ("forbidden", result.called_forbidden)):
                    if values:
                        print(f"       {label}: {', '.join(values)}", flush=True)
                if result.arg_mismatches:
                    print(f"       argument mismatch: {', '.join(result.arg_mismatches)}",
                          flush=True)
                if result.error:
                    print(f"       error: {result.error}", flush=True)

    passed = sum(item.passed_tool_contract and not item.error for item in results)
    print(f"\n{passed}/{len(results)} runs met the declared tool contract")
    print(f"results: {output}")
    print("All turns used Wisp TEST MODE; no assistant tool executed.")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated categories or ids")
    parser.add_argument("--ids", default="", help="comma-separated exact ids")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", default="router_adversarial_live_results.json")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
