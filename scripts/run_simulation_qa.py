#!/usr/bin/env python3
"""Run Wisp's deterministic, non-mutating Simulation QA gates.

Every Python test runs in its own process and temporary WISP_HOME.  This is
intentional: several legacy tests use process-global registries and check
counters, so a blanket ``pytest tests`` can both create false failures and
silently miss failed checks.

This runner never starts Wisp, calls a model server, installs cache fixtures,
opens Mail drafts, or seeds/mutates native apps.  Native gates compile and run
only fixture-backed or pure contract checks.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PROFILE_TESTS = {
    "conversation": {
        "tests/test_conversation_memory.py",
        "tests/test_conversational_skills.py",
        "tests/test_contextual_outbound_routing.py",
        "tests/test_workflow_bindings.py",
        "tests/test_workflow_engine.py",
    },
    "sources": {
        "tests/test_brief_fallback.py",
        "tests/test_email_scoping.py",
        "tests/test_message_attribution.py",
        "tests/test_multi_source_fallback.py",
        "tests/test_reply_bridge_simulation.py",
        "tests/test_sandbox_wire.py",
        "tests/test_source_sync_contract.py",
        "tests/test_sync_readiness.py",
    },
    "outbound": {
        "tests/test_action_tools_sanitize.py",
        "tests/test_outbound_language_clarification.py",
        "tests/test_outbound_payload_presentation.py",
        "tests/test_schedule_send.py",
        "tests/test_scheduled_send_claims.py",
        "tests/test_typed_email_reply.py",
        "tests/test_typed_message_send.py",
        "tests/test_typed_reminder_operations.py",
        "tests/test_typed_task_engine.py",
    },
    "safety": {
        "tests/test_approver_timeout.py",
        "tests/test_destructive_shell.py",
        "tests/test_execution_contract_loop.py",
        "tests/test_forced_step_withholding.py",
        "tests/test_tool_calling_invariants.py",
        "tests/test_tool_outcomes.py",
        "tests/test_tool_test_mode.py",
    },
    "reliability": {
        "air/tests/test_air.py",
        "tests/test_assistant_dedupe.py",
        "tests/test_daily_summary_delivery.py",
        "tests/test_error_translation.py",
        "tests/test_latency_prompt_contract.py",
        "tests/test_lazy_inference_readiness.py",
        "tests/test_paths_override.py",
        "tests/test_retry_nudge.py",
        "tests/test_sandbox_world.py",
        "tests/test_search_reliability.py",
        "tests/test_streamed_answer_discard.py",
        "tests/test_tool_dispatch.py",
    },
    "research": {
        "tests/test_research_library.py",
        "tests/test_research_mode.py",
    },
    "routing": {
        "tests/test_alias_reachability.py",
        "tests/test_compound_claims.py",
        "tests/test_compound_task_routing.py",
        "tests/test_direct_dispatch.py",
        "tests/test_direct_dispatch_exec.py",
        "tests/test_historical_routing_regressions.py",
        "tests/test_router_adversarial_cases.py",
        "tests/test_router_execution_contract.py",
        "tests/test_router_no_vision.py",
        "tests/test_router_scoping.py",
        "tests/test_semantic_routing.py",
        "tests/test_search_reliability.py",
        "tests/test_short_circuit.py",
    },
}

# Explicitly reviewed as deterministic and non-mutating at the infrastructure
# baseline.  The full profile refuses to auto-run a newly added test until it
# is classified here; this prevents an innocently named live test from entering
# an offline release gate without review.
ADDITIONAL_FULL_TESTS = {
    "tests/test_codex_monitor.py",
    "tests/test_fit_window.py",
    "tests/test_lexical_tool_retrieval.py",
    "tests/test_move_and_coverage.py",
    "tests/test_narration_thinking.py",
    "tests/test_notes_defaults.py",
    "tests/test_phase1_tools.py",
    "tests/test_recent_activity.py",
    "tests/test_reminder_bulk_clear.py",
    "tests/test_reminder_creation.py",
    "tests/test_reminder_update.py",
    "tests/test_replay_failure_fixes.py",
    "tests/test_think_leak.py",
    "tests/test_timeranges.py",
    "tests/test_user_reported_regressions_20260902.py",
    "tests/test_user_reported_regressions_20260903.py",
    "tests/test_user_reported_regressions_20260903_noon.py",
    "tests/test_user_reported_regressions_20260908.py",
}
SAFE_FULL_TESTS = set().union(*PROFILE_TESTS.values(), ADDITIONAL_FULL_TESTS)

NATIVE_PROFILES = {"sources", "outbound", "reliability"}
EXCLUDED_LIVE_COMMANDS = [
    "scripts/test_all_tools.py",
    "scripts/test_mail_reply_live.sh --live-prepare",
    "scripts/replay_prompts.py",
    "scripts/seed_real_apps.sh",
    "scripts/clear_real_apps.sh",
    "scripts/install_cache_fixtures.sh",
    "scripts/restore_cache_backup.sh",
    "scripts/run.sh",
    "scripts/package_app.sh",
    "write-enabled model/retrieval/routing evaluations",
]
_COUNT_PATTERNS = (
    re.compile(r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?(?:, (?P<skipped>\d+) skipped)?"),
    re.compile(r"Ran (?P<passed>\d+) tests?"),
    re.compile(r"(?P<passed>\d+) (?:regression )?checks passed"),
)


@dataclass
class GateResult:
    name: str
    command: list[str]
    returncode: int
    duration_s: float
    passed: int
    failed: int
    skipped: int
    stdout: str
    stderr: str


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "git command failed")
    return proc.stdout.strip()


def _counts(output: str, returncode: int) -> tuple[int, int, int]:
    matches: list[re.Match[str]] = []
    for pattern in _COUNT_PATTERNS:
        matches.extend(pattern.finditer(output))
    if not matches:
        return (1 if returncode == 0 else 0, 0 if returncode == 0 else 1, 0)
    match = max(matches, key=lambda item: item.end())
    passed = int(match.groupdict().get("passed") or 0)
    failed = int(match.groupdict().get("failed") or 0)
    skipped = int(match.groupdict().get("skipped") or 0)
    if returncode and failed == 0:
        failed = 1
    return passed, failed, skipped


def _run(name: str, command: list[str], cwd: Path = ROOT) -> GateResult:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="wisp-simqa-state-") as state_dir:
        env = {
            **os.environ,
            "WISP_HOME": state_dir,
            "WISPAIR_HOME": str(Path(state_dir) / "air"),
            "WISP_TEST_PYTHON": sys.executable,
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
        }
        # These are opt-in live tests.  Their absence is part of the safety
        # envelope, even if a caller happens to export them in their shell.
        env.pop("WISP_LIVE_REMINDER_TEST", None)
        env.pop("WISP_MAIL_REPLY_LIVE", None)
        proc = subprocess.run(
            command, cwd=cwd, env=env, text=True, capture_output=True, check=False
        )
    duration = time.monotonic() - started
    passed, failed, skipped = _counts(proc.stdout + "\n" + proc.stderr, proc.returncode)
    result = GateResult(
        name=name,
        command=command,
        returncode=proc.returncode,
        duration_s=round(duration, 3),
        passed=passed,
        failed=failed,
        skipped=skipped,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    state = "PASS" if proc.returncode == 0 else "FAIL"
    print(
        f"[{state}] {name} ({passed} passed, {failed} failed, "
        f"{skipped} skipped; {duration:.2f}s)",
        flush=True,
    )
    if proc.returncode:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
    return result


def _python_command(path: str) -> list[str]:
    source = (ROOT / path).read_text(encoding="utf-8")
    legacy = "if __name__ ==" in source or "sys.exit(" in source
    if legacy:
        return [sys.executable, path]
    command = [sys.executable, "-m", "pytest"]
    if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1":
        command.extend(["-p", "pytest_asyncio.plugin"])
    return [*command, "-q", "-rs", path]


def _dependencies() -> dict[str, str]:
    required = {"pytest": "pytest", "pytest-asyncio": "pytest_asyncio"}
    missing = [distribution for distribution, module in required.items()
               if importlib.util.find_spec(module) is None]
    if missing:
        raise RuntimeError(f"missing Simulation QA dependencies: {', '.join(missing)}")
    return {name: importlib.metadata.version(name) for name in required}


def _native_gates(build_dir: Path) -> list[tuple[str, list[str]]]:
    module_cache = str(build_dir / "module-cache")
    mail_db = str(build_dir / "mail-db-regression")
    sync_label = str(build_dir / "source-sync-label")
    return [
        (
            "native/mail-reply-contract",
            ["bash", "scripts/test_mail_reply_contract.sh"],
        ),
        (
            "native/search-contract",
            ["bash", "scripts/test_search_contract.sh"],
        ),
        (
            "native/research-library-contract",
            ["bash", "scripts/test_research_library_contract.sh"],
        ),
        (
            "native/mail-db-compile",
            [
                "swiftc", "-module-cache-path", module_cache,
                "app/Sources/WispApp/MailDBReader.swift",
                "tests/MailDBReaderRegression.swift", "-lsqlite3", "-o", mail_db,
            ],
        ),
        ("native/mail-db-contract", [mail_db]),
        (
            "native/source-sync-label-compile",
            [
                "swiftc", "-module-cache-path", module_cache,
                "app/Sources/WispApp/WispClient.swift",
                "tests/SourceSyncLabelRegression.swift", "-o", sync_label,
            ],
        ),
        ("native/source-sync-label-contract", [sync_label]),
    ]


def _selected_tests(profiles: list[str]) -> list[str]:
    if "full" in profiles:
        discovered = {
            str(path.relative_to(ROOT))
            for path in (ROOT / "tests").glob("test_*.py")
        } | {"air/tests/test_air.py"}
        unknown = sorted(discovered - SAFE_FULL_TESTS)
        missing = sorted(SAFE_FULL_TESTS - discovered)
        if unknown or missing:
            details = []
            if unknown:
                details.append(f"unclassified tests: {unknown}")
            if missing:
                details.append(f"missing reviewed tests: {missing}")
            raise RuntimeError("unsafe full-profile manifest drift; " + "; ".join(details))
        return sorted(SAFE_FULL_TESTS)
    selected: set[str] = set()
    for profile in profiles:
        selected.update(PROFILE_TESTS[profile])
    return sorted(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choices = ["full", *sorted(PROFILE_TESTS)]
    parser.add_argument(
        "--profile", action="append", choices=choices,
        help="Repeat to combine targeted profiles; default: full.",
    )
    parser.add_argument(
        "--expected-sha", required=True,
        help="Exact 40-character candidate commit that must remain checked out.",
    )
    parser.add_argument(
        "--base-sha", help="Optional comparison commit recorded with changed paths.",
    )
    parser.add_argument(
        "--report", type=Path,
        help="Caller-owned path for complete JSON results and captured gate logs.",
    )
    parser.add_argument(
        "--safety-mode", choices=["offline"], default="offline",
        help="Only offline, non-mutating simulation is supported.",
    )
    native_mode = parser.add_mutually_exclusive_group()
    native_mode.add_argument(
        "--skip-native", action="store_true",
        help="Skip Swift gates; retain a separate native-only report before verdict.",
    )
    native_mode.add_argument(
        "--only-native", action="store_true",
        help="Run only the non-sending Swift gates.",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="Infrastructure development only; candidate verdicts must never use this.",
    )
    parser.add_argument("--list", action="store_true", help="List selected gates and exit.")
    args = parser.parse_args()
    if not args.list and args.report is None:
        parser.error("--report is required unless --list is used")

    profiles = args.profile or ["full"]
    expected = args.expected_sha.lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        parser.error("--expected-sha must be a full 40-character commit SHA")
    start_sha = _git("rev-parse", "HEAD")
    if start_sha != expected:
        parser.error(f"HEAD is {start_sha}, expected {expected}")
    dirty = _git("status", "--porcelain")
    if dirty and not args.allow_dirty:
        parser.error("candidate Worktree is dirty; exact-SHA verdicts require a clean checkout")

    tests = [] if args.only_native else _selected_tests(profiles)
    run_native = args.only_native or (
        not args.skip_native
        and ("full" in profiles or bool(NATIVE_PROFILES.intersection(profiles)))
    )
    if args.list:
        for path in tests:
            print(path)
        if run_native:
            for name, _command in _native_gates(Path("/tmp/wisp-simqa-list")):
                print(name)
        print("excluded live/mutating commands:")
        for command in EXCLUDED_LIVE_COMMANDS:
            print(f"  {command}")
        return 0
    dependencies = _dependencies() if tests else {}

    changed_paths = (
        _git("diff", "--name-only", f"{args.base_sha}..{start_sha}").splitlines()
        if args.base_sha else []
    )
    started = time.monotonic()
    results: list[GateResult] = []
    for path in tests:
        results.append(_run(path, _python_command(path)))

    if run_native:
        with tempfile.TemporaryDirectory(prefix="wisp-simqa-native-") as build:
            for name, command in _native_gates(Path(build)):
                results.append(_run(name, command))

    end_sha = _git("rev-parse", "HEAD")
    stable = end_sha == start_sha == expected
    totals = {
        "gates": len(results),
        "passed": sum(item.passed for item in results),
        "failed": sum(item.failed for item in results),
        "skipped": sum(item.skipped for item in results),
        "duration_s": round(time.monotonic() - started, 3),
    }
    ok = stable and all(item.returncode == 0 for item in results)
    report = {
        "schema_version": 1,
        "safety_mode": args.safety_mode,
        "environment": {
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "dependencies": dependencies,
        },
        "status": "PASS" if ok else "FAIL",
        "candidate_sha": start_sha,
        "ending_sha": end_sha,
        "sha_stable": stable,
        "dirty_allowed": bool(args.allow_dirty),
        "native_mode": "only" if args.only_native else ("skip" if args.skip_native else "included"),
        "profiles": profiles,
        "base_sha": args.base_sha,
        "changed_paths": changed_paths,
        "excluded_live_commands": EXCLUDED_LIVE_COMMANDS,
        "totals": totals,
        "results": [asdict(item) for item in results],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "candidate_sha", "sha_stable", "totals")}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
