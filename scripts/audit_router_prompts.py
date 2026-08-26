#!/usr/bin/env python3
"""Statically audit adversarial prompts without running a model or tool.

Deterministic rules run normally. Semantic routes use the repository's
deterministic word-overlap stub from ``tests.stub_embedder``; no inference
server, embedding model, chat model, cache file, or assistant tool is touched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service.router.router as R  # noqa: E402
import service.tools  # noqa: E402,F401  register the complete tool roster
from tests.router_adversarial_cases import CASES, PromptCase, validate  # noqa: E402
from tests.stub_embedder import install as install_stub_embedder  # noqa: E402


@dataclass(frozen=True)
class StaticResult:
    id: str
    category: str
    mode: str
    prompt: str
    status: str
    role: str | None
    needs_tools: bool | None
    reason: str
    tool_subset: tuple[str, ...] | None
    force_first_tool: str | None
    clarify_channel: bool | None
    clarify_target: bool | None
    multi_round: bool | None
    direct_calls: tuple[str, ...]
    findings: tuple[str, ...]
    expected: str


def _pure_decision(case: PromptCase):
    """Mirror route() until the first operation that can invoke a model."""
    if R.confirms_offered_action(case.prompt, case.last_assistant):
        inherited = R._confirmation_subset(case.last_tools)
        return ((R._finalize(inherited, case.prompt), "rule")
                if inherited is not None else (None, "retrieval_pending"))

    channel = R._channel_answer_subset(case.prompt, case.last_assistant, case.last_tools)
    if channel is not None:
        return R._finalize(channel, case.prompt), "rule"

    reply = R._contextual_reply_subset(case.prompt, case.last_tools)
    if reply is not None:
        return R._finalize(reply, case.prompt), "rule"

    fragment = R._fragment_continuation(case.prompt, case.last_tools)
    if fragment is not None:
        return R._finalize(fragment, case.prompt), "rule"

    decision = R.rule_route(case.prompt)
    if decision is not None:
        status = "retrieval_pending" if decision.needs_tools and decision.tool_subset is None else "rule"
        if status == "rule":
            decision = R._finalize(decision, case.prompt)
        return decision, status

    continuation = R._write_continuation_subset(case.prompt, case.last_tools)
    if continuation is not None:
        return R._finalize(continuation, case.prompt), "rule"
    return None, "retrieval_pending"


async def inspect(case: PromptCase) -> StaticResult:
    decision, status = _pure_decision(case)
    if status == "retrieval_pending":
        decision = await R.route(case.prompt, last_assistant=case.last_assistant,
                                 last_tools=case.last_tools)
        status = "stub_retrieval"
    subset = None if decision is None or decision.tool_subset is None else tuple(decision.tool_subset)
    findings: list[str] = []

    # Tool checks are conclusive only after a rule provides a concrete subset.
    # Retrieval-pending cases are deferred rather than guessed.
    if subset is not None:
        missing = sorted(set(case.required) - set(subset))
        if missing:
            findings.append(f"missing required tools: {', '.join(missing)}")
        # A channel-ambiguous route deliberately withholds every committing
        # outbound tool until the user answers "text or email?". Do not call
        # that structural safety behavior a missing-tool bug.
        channel_choices_withheld = (
            decision is not None
            and decision.clarify_channel
            and case.clarify_channel is True
            and set(case.one_of) <= R._CHANNEL_OUTBOUND_TOOLS
        )
        if (case.one_of and not channel_choices_withheld
                and not (set(case.one_of) & set(subset))):
            findings.append("missing every acceptable tool: " + ", ".join(case.one_of))
        dangerous = sorted(set(case.forbidden) & set(subset))
        if dangerous:
            findings.append(f"offers forbidden tools: {', '.join(dangerous)}")

    if decision is not None:
        if case.clarify_channel is not None and decision.clarify_channel != case.clarify_channel:
            findings.append(
                f"clarify_channel={decision.clarify_channel}, expected {case.clarify_channel}")
        if case.clarify_target is not None and decision.clarify_target != case.clarify_target:
            findings.append(
                f"clarify_target={decision.clarify_target}, expected {case.clarify_target}")

    if status == "stub_retrieval" and findings:
        findings = ["stub retrieval: " + finding for finding in findings]

    return StaticResult(
        id=case.id, category=case.category, mode=case.mode, prompt=case.prompt,
        status=status, role=None if decision is None else decision.role,
        needs_tools=None if decision is None else decision.needs_tools,
        reason="ambiguous default" if decision is None else decision.reason,
        tool_subset=subset,
        force_first_tool=None if decision is None else decision.force_first_tool,
        clarify_channel=None if decision is None else decision.clarify_channel,
        clarify_target=None if decision is None else decision.clarify_target,
        multi_round=None if decision is None else decision.multi_round,
        direct_calls=() if decision is None else tuple(name for name, _ in decision.direct_calls),
        findings=tuple(findings), expected=case.expected,
    )


def _print_human(results: list[StaticResult]) -> None:
    for result in results:
        badge = "STUB" if result.status == "stub_retrieval" else ("FLAG" if result.findings else "OK")
        tools = "none" if result.tool_subset is None else ", ".join(result.tool_subset) or "none"
        print(f"[{badge:5}] {result.id} ({result.category}, {result.mode})")
        print(f"        route={result.role or 'default'} needs_tools={result.needs_tools} "
              f"force={result.force_first_tool or '-'} multi={result.multi_round}")
        print(f"        flags=channel:{result.clarify_channel} target:{result.clarify_target} "
              f"tools={tools}")
        for finding in result.findings:
            print(f"        ! {finding}")

    concrete = [r for r in results if r.status == "rule"]
    stubbed = [r for r in results if r.status == "stub_retrieval"]
    flagged = [r for r in concrete if r.findings]
    print()
    print(f"Static audit: {len(results)} prompts; {len(concrete)} concrete rule routes; "
          f"{len(stubbed)} offline stub-retrieval routes; {len(flagged)} concrete routes flagged")
    print("No model, embedding, assistant tool, or external action was run.")


async def _inspect_all(cases: list[PromptCase]) -> list[StaticResult]:
    # Sequential on purpose: every case shares the deterministic in-memory
    # index, and parallel builds would add noise to a logic audit.
    return [await inspect(case) for case in cases]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated case ids or categories")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    errors = validate()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 2

    wanted = {part.strip() for part in args.only.split(",") if part.strip()}
    selected = [c for c in CASES if not wanted or c.id in wanted or c.category in wanted]
    if not selected:
        print("No prompts matched --only.", file=sys.stderr)
        return 2

    install_stub_embedder()
    results = asyncio.run(_inspect_all(selected))
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        _print_human(results)
    # Findings are the purpose of this exploratory audit, not process errors.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
