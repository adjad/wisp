"""Run the complete isolated Python regression gate.

Usage: .venv/bin/python scripts/test_replay_failure_fixes.py
No live prompt replay or external send is part of this runner.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

# These exact legacy-script results already fail on the branch base and are
# owned by PR #19. Matching the path alone is unsafe: a crash, empty script, or
# additional failed check in the same module must remain a regression.
KNOWN_BASELINE_RESULTS = {
    "tests/test_alias_reachability.py": (
        81,
        8,
        (
            "FAIL 'append_note' reachable via its own alias 'add a line to the meeting note' — rule='calendar event creation -> add_calendar_event' offered=['add_calendar_event']",
            "FAIL 'search_reminders' reachable via its own alias 'find my dentist reminder' — rule='calendar lookup -> get_upcoming (router-direct, 60d)' offered=['get_upcoming']",
            "FAIL 'search_reminders' reachable via its own alias 'what does my reminder say' — rule='calendar lookup -> get_upcoming (router-direct, 60d)' offered=['get_upcoming']",
            "FAIL 'search_reminders' reachable via its own alias 'when is my vaccine reminder' — rule='calendar lookup -> get_upcoming (router-direct, 60d)' offered=['get_upcoming']",
            "FAIL 'set_alarm' reachable via its own alias 'set an alarm for half six tomorrow' — rule='reminder creation -> scoped tools (3) [time named -> forced]' offered=['add_calendar_event', 'add_reminder', 'get_upcoming']",
            "FAIL 'update_event' reachable via its own alias 'reschedule the meeting to tomorrow' — rule='calendar event creation -> add_calendar_event' offered=['add_calendar_event']",
            "FAIL 'update_reminder' reachable via its own alias 'reschedule my reminder' — rule='reminder creation -> scoped tools (3)' offered=['add_calendar_event', 'add_reminder', 'get_upcoming']",
            "FAIL 'wisp_capabilities' reachable via its own alias 'can you send texts and create reminders' — rule='reminder+messages (compound) -> scoped tools (8)' offered=['add_calendar_event', 'add_reminder', 'draft_message', 'get_upcoming', 'lookup_contact', 'send_message', 'summarize_messages', 'view_messages']",
        ),
    ),
    "tests/test_forced_step_withholding.py": (
        14,
        2,
        (
            "FAIL run_shell is on the table (pinned escape hatch) ['find_files', 'organize_files']",
            "FAIL takes the reorganize-files route (['find_files', 'organize_files'], 'reorganize files')",
        ),
    ),
    "tests/test_router_scoping.py": (
        147,
        1,
        (
            "FAIL every unrouted tool is retrievable (has aliases) no route AND no aliases: ['clear_memory', 'search_conversations']",
        ),
    ),
    "tests/test_semantic_routing.py": (
        19,
        1,
        (
            "FAIL embedder failure falls back to the static core — got ['cancel_scheduled_send', 'clear_reminders', 'find_local_events', 'get_recent_activity', 'list_scheduled_sends', 'live_captions', 'log_entry', 'manage_contacts', 'manage_timers', 'recall', 'run_shell', 'search_conversations', 'set_display', 'set_fitness_goal', 'summarize_emails', 'switch_app', 'view_emails', 'view_messages', 'wikipedia_summary']",
        ),
    ),
}
KNOWN_BASELINE_FAILURES = frozenset(KNOWN_BASELINE_RESULTS)

# Classification is explicit; discovery remains recursive and automatic. These
# modules intentionally grade legacy check counters or unittest main() output
# when run as scripts. Every other discovered module goes through pytest, whose
# exit code 5 prevents an empty module from being counted as a passing test.
LEGACY_SCRIPT_TESTS = frozenset({
    "air/tests/test_air.py",
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
        *(ROOT / "air" / "tests").rglob("test_*.py"),
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


def main() -> int:
    tests = _tests()
    commands = [_command(path) for path in tests]
    failed: set[str] = set()
    baseline_mismatches: set[str] = set()
    for path, command in zip(tests, commands):
        print("Running:", " ".join(command), flush=True)
        with tempfile.TemporaryDirectory(prefix="wisp-regression-") as state_dir:
            env = _environment(state_dir)
            relative = str(path.relative_to(ROOT))
            if relative in LEGACY_SCRIPT_TESTS:
                proc = subprocess.run(
                    command, cwd=ROOT, env=env, text=True,
                    capture_output=True, check=False,
                )
                print(proc.stdout, end="")
                print(proc.stderr, end="", file=sys.stderr)
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
                    baseline_mismatches.add(relative)
                if proc.returncode == 0 and not passed:
                    print(
                        f"ERROR: legacy test reported no executed tests: {relative}",
                        flush=True,
                    )
            else:
                passed = subprocess.run(
                    command, cwd=ROOT, env=env, check=False,
                ).returncode == 0
            if not passed:
                failed.add(str(path.relative_to(ROOT)))
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
