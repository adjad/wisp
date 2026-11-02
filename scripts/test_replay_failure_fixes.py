"""Run the complete isolated Python regression gate.

Usage: .venv/bin/python scripts/test_replay_failure_fixes.py [--jobs N]
       [--only-outside-simulation-qa]
No live prompt replay or external send is part of this runner.

Every module runs in its own process with its own disposable HOME/WISP_HOME.
``--jobs N`` (default: one per CPU) runs that many modules at once; ``--jobs 1``
is the historical sequential, streaming run. ``--only-outside-simulation-qa``
runs modules not covered by Simulation QA's full profile, plus the engine
socket checks whose live branches are skipped inside its OS sandbox.
"""
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]

# The routing-contract repair removed every inherited baseline. Any module
# failure is now a release regression; exemptions must be introduced only with
# an explicit owner and exact-output proof on a future branch base.
KNOWN_BASELINE_RESULTS = {}
KNOWN_BASELINE_FAILURES = frozenset(KNOWN_BASELINE_RESULTS)

# Classification is explicit; discovery remains recursive and automatic. These
# modules intentionally grade legacy check counters or unittest main() output
# when run as scripts. Every other discovered module goes through pytest, whose
# exit code 5 prevents an empty module from being counted as a passing test.
LEGACY_SCRIPT_TESTS = frozenset({
    "tests/test_action_tools_sanitize.py",
    "tests/test_alias_reachability.py",
    "tests/test_approver_timeout.py",
    "tests/test_assistant_dedupe.py",
    "tests/test_assistant_migrations.py",
    "tests/test_brief_fallback.py",
    "tests/test_broad_web_search.py",
    "tests/test_compound_claims.py",
    "tests/test_compound_task_routing.py",
    "tests/test_contextual_outbound_routing.py",
    "tests/test_destructive_shell.py",
    "tests/test_direct_dispatch.py",
    "tests/test_direct_dispatch_exec.py",
    "tests/test_email_scoping.py",
    "tests/test_error_translation.py",
    "tests/test_execution_contract_loop.py",
    "tests/test_fit_window.py",
    "tests/test_forced_step_withholding.py",
    "tests/test_message_attribution.py",
    "tests/test_move_and_coverage.py",
    "tests/test_multi_source_fallback.py",
    "tests/test_narration_thinking.py",
    "tests/test_notes_defaults.py",
    "tests/test_bundled_attribution.py",
    "tests/test_bundled_engine_api.py",
    "tests/test_bundled_engine.py",
    "tests/test_bundled_engine_mode.py",
    "tests/test_engine_packaging.py",
    "tests/test_engine_licenses.py",
    "tests/test_bundled_engine_startup.py",
    "tests/test_paths_override.py",
    "tests/test_phase1_tools.py",
    "tests/test_recent_activity.py",
    "tests/test_reminder_bulk_clear.py",
    "tests/test_reminder_creation.py",
    "tests/test_reminder_update.py",
    "tests/test_research_library.py",
    "tests/test_research_mode.py",
    "tests/test_retry_nudge.py",
    "tests/test_router_adversarial_cases.py",
    "tests/test_router_execution_contract.py",
    "tests/test_router_no_vision.py",
    "tests/test_router_scoping.py",
    "tests/test_sandbox_wire.py",
    "tests/test_sandbox_world.py",
    "tests/test_schedule_send.py",
    "tests/test_search_reliability.py",
    "tests/test_semantic_routing.py",
    "tests/test_shell_boundary.py",
    "tests/test_short_circuit.py",
    "tests/test_source_sync_contract.py",
    "tests/test_streamed_answer_discard.py",
    "tests/test_sync_readiness.py",
    "tests/test_think_leak.py",
    "tests/test_timeranges.py",
    "tests/test_tool_dispatch.py",
    "tests/test_tool_outcomes.py",
    "tests/test_tool_test_mode.py",
    "tests/test_user_reported_regressions_20260903.py",
})

_TEST_COUNT_PATTERNS = (
    re.compile(r"\b(?P<count>\d+)\s+passed\b"),
    re.compile(r"\bRan\s+(?P<count>\d+)\s+tests?\b"),
    re.compile(r"\bok:\s+(?P<count>\d+)\s+prompts validated\b"),
)
_CHECK_SUMMARY = re.compile(
    r"^(?P<passed>\d+) passed, (?P<failed>\d+) failed$", re.MULTILINE,
)


def _tests() -> list[Path]:
    """Discover every repository test module in the pytest-configured roots."""
    return sorted({
        *(ROOT / "tests").rglob("test_*.py"),
    })


def _command(path: Path) -> list[str]:
    """Use direct execution for legacy check-counter and unittest scripts."""
    relative = str(path.relative_to(ROOT))
    if relative in LEGACY_SCRIPT_TESTS:
        return [sys.executable, relative]
    return [sys.executable, "-m", "pytest", "-q", "-rs", relative]


def _reports_nonzero_test_count(output: str) -> bool:
    """Require direct legacy scripts to prove that they executed tests."""
    return any(
        int(match.group("count")) > 0
        for pattern in _TEST_COUNT_PATTERNS
        for match in pattern.finditer(output)
    )


def _legacy_failure_signature(
        output: str) -> tuple[int, int, tuple[str, ...]] | None:
    """Parse the final counter summary and exact failed-check identities."""
    summaries = list(_CHECK_SUMMARY.finditer(output))
    if len(summaries) != 1:
        return None
    summary = summaries[0]
    failures = tuple(sorted(
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("FAIL ")
    ))
    passed = int(summary.group("passed"))
    failed = int(summary.group("failed"))
    if passed + failed == 0 or failed != len(failures):
        return None
    return (
        passed,
        failed,
        failures,
    )


def _matches_known_baseline(path: str, returncode: int, output: str) -> bool:
    """Accept only the exact expected failed checks from a clean test run."""
    expected = KNOWN_BASELINE_RESULTS.get(path)
    return bool(
        expected is not None
        and returncode == 1
        and "Traceback (most recent call last):" not in output
        and _legacy_failure_signature(output) == expected
    )


def _environment(state_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("WISP_"):
            env.pop(key, None)
    fake_home = Path(state_dir) / "home"
    fake_home.mkdir()
    env.update({
        "HOME": str(fake_home),
        "WISP_HOME": str(Path(state_dir) / "wisp"),
        "WISPAIR_HOME": str(Path(state_dir) / "air"),
        "WISP_TEST_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONOPTIMIZE": "0",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    })
    return env


def _classify_failures(
        failed: set[str], baseline_mismatches: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Return new regressions and stale baseline entries, in stable order."""
    mismatches = baseline_mismatches or set()
    return (
        sorted((failed - KNOWN_BASELINE_FAILURES) | mismatches),
        sorted(KNOWN_BASELINE_FAILURES - failed),
    )


# These modules are listed in Simulation QA, but their real-listener branches
# skip under its network sandbox. They also need an unrestricted PR run.
UNSANDBOXED_SOCKET_TESTS = frozenset({
    "tests/test_bundled_attribution.py",
    "tests/test_bundled_engine.py",
})


def _outside_simulation_qa(tests: list[Path]) -> list[Path]:
    """Uncovered modules plus socket branches not exercised by the sandbox.

    Filter once in discovery order, so membership in both groups never adds a
    second execution. Ordinary fully covered modules are still deduplicated.
    """
    try:
        from scripts.run_simulation_qa import SAFE_FULL_TESTS
    except ModuleNotFoundError:  # Direct execution puts scripts/ first on sys.path.
        from run_simulation_qa import SAFE_FULL_TESTS
    return [path for path in tests
            if str(path.relative_to(ROOT)) not in SAFE_FULL_TESTS
            or str(path.relative_to(ROOT)) in UNSANDBOXED_SOCKET_TESTS]


_OUTPUT_LOCK = threading.Lock()
# Real-socket peer attestation under a fixed wall-clock readiness budget: run
# alone after the concurrent modules finish (Simulation QA does the same).
RUN_ALONE_TESTS = frozenset({"tests/test_runtime_peer.py"})


def _run_module(path: Path, command: list[str], *, capture: bool) -> tuple[bool, bool]:
    """Run one isolated module; return (passed, baseline_mismatch).

    With ``capture`` (parallel runs) the module's whole output is printed as
    one block when it finishes, so concurrent modules never interleave.
    """
    relative = str(path.relative_to(ROOT))
    header = "Running: " + " ".join(command)
    out, err = [], []
    if not capture:
        print(header, flush=True)
    else:
        out.append(header + "\n")
    mismatch = False
    with tempfile.TemporaryDirectory(prefix="wisp-regression-") as state_dir:
        env = _environment(state_dir)
        if relative in LEGACY_SCRIPT_TESTS:
            proc = subprocess.run(
                command, cwd=ROOT, env=env, text=True,
                capture_output=True, check=False,
            )
            out.append(proc.stdout)
            err.append(proc.stderr)
            output = proc.stdout + "\n" + proc.stderr
            passed = proc.returncode == 0 and _reports_nonzero_test_count(
                output
            )
            if (
                relative in KNOWN_BASELINE_FAILURES
                and not _matches_known_baseline(
                    relative, proc.returncode, output,
                )
            ):
                mismatch = True
            if proc.returncode == 0 and not passed:
                out.append(
                    f"ERROR: legacy test reported no executed tests: {relative}\n"
                )
            if not capture:
                print(proc.stdout, end="")
                print(proc.stderr, end="", file=sys.stderr)
                if proc.returncode == 0 and not passed:
                    print(
                        f"ERROR: legacy test reported no executed tests: {relative}",
                        flush=True,
                    )
        elif not capture:
            passed = subprocess.run(
                command, cwd=ROOT, env=env, check=False,
            ).returncode == 0
        else:
            proc = subprocess.run(
                command, cwd=ROOT, env=env, text=True,
                capture_output=True, check=False,
            )
            out.append(proc.stdout)
            err.append(proc.stderr)
            passed = proc.returncode == 0
    if capture:
        with _OUTPUT_LOCK:
            print("".join(out), end="", flush=True)
            print("".join(err), end="", file=sys.stderr, flush=True)
    return passed, mismatch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs", type=int, default=None, metavar="N",
        help="Concurrent modules (default: one per CPU; 1 runs sequentially).",
    )
    parser.add_argument(
        "--only-outside-simulation-qa", action="store_true",
        help="Run uncovered modules and supplemental unrestricted engine socket checks.",
    )
    args = parser.parse_args(argv)
    jobs = max(1, os.cpu_count() or 1) if args.jobs is None else args.jobs
    if jobs < 1:
        parser.error("--jobs must be at least 1")
    tests = _tests()
    if args.only_outside_simulation_qa:
        covered = len(tests)
        tests = _outside_simulation_qa(tests)
        print(
            f"Simulation QA supplement: running {len(tests)} of {covered} modules "
            "for uncovered or unrestricted socket coverage.",
            flush=True,
        )
    commands = [_command(path) for path in tests]
    failed: set[str] = set()
    baseline_mismatches: set[str] = set()
    if jobs == 1:
        outcomes = [_run_module(path, command, capture=False)
                    for path, command in zip(tests, commands)]
    else:
        print(f"Regression gate: {len(commands)} modules, {jobs} parallel jobs", flush=True)
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="regression") as pool:
            futures = {path: pool.submit(_run_module, path, command, capture=True)
                       for path, command in zip(tests, commands)
                       if str(path.relative_to(ROOT)) not in RUN_ALONE_TESTS}
            by_path = {path: future.result() for path, future in futures.items()}
        for path, command in zip(tests, commands):              # pool is idle now
            if path not in by_path:
                by_path[path] = _run_module(path, command, capture=True)
        outcomes = [by_path[path] for path in tests]
    for path, (passed, mismatch) in zip(tests, outcomes):
        relative = str(path.relative_to(ROOT))
        if mismatch:
            baseline_mismatches.add(relative)
        if not passed:
            failed.add(relative)
    regressions, resolved = _classify_failures(failed, baseline_mismatches)
    print(
        f"Regression gate: {len(commands) - len(failed)}/{len(commands)} "
        "test modules passed",
        flush=True,
    )
    for path in sorted(
            (failed & KNOWN_BASELINE_FAILURES) - baseline_mismatches):
        print(f"KNOWN FAILURE (PR #19): {path}", flush=True)
    for path in sorted(baseline_mismatches):
        print(f"BASELINE MISMATCH: {path}", flush=True)
    for path in regressions:
        print(f"REGRESSION: {path}", flush=True)
    for path in resolved:
        print(f"BASELINE RESOLVED; remove suppression: {path}", flush=True)
    return 1 if regressions or resolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
