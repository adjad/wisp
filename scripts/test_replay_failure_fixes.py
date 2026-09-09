"""Run isolated replay regressions and legacy check-counter scripts.

Usage: .venv/bin/python scripts/test_replay_failure_fixes.py
No live prompt replay or external send is part of this runner.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "test_replay_failure_fixes", "test_workflow_engine", "test_typed_task_engine",
    "test_typed_reminder_operations", "test_typed_message_send",
    "test_typed_email_reply",
    "test_reply_bridge_simulation",
    "test_outbound_language_clarification",
    "test_scheduled_send_claims",
    "test_workflow_bindings", "test_timeranges",
    "test_tool_outcomes", "test_tool_calling_invariants", "test_email_scoping",
    "test_brief_fallback", "test_reminder_creation", "test_reminder_update",
    "test_source_sync_contract", "test_sync_readiness",
    "test_outbound_payload_presentation", "test_daily_summary_delivery",
    "test_user_reported_regressions_20260902", "test_user_reported_regressions_20260903",
    "test_user_reported_regressions_20260903_noon",
    "test_user_reported_regressions_20260908",
]


def main():
    commands = [[sys.executable, "-m", "pytest", "-q", "-rs",
                 *[f"tests/{name}.py" for name in TESTS]]]
    # These legacy files use check()/FAIL counters, which pytest alone cannot
    # reliably grade. Their main functions exit nonzero on failed checks.
    commands.extend([sys.executable, f"tests/{name}.py"] for name in (
        "test_email_scoping", "test_brief_fallback", "test_timeranges",
        "test_execution_contract_loop"))
    failed = []
    # Import-time store migrations must never run against the user's live
    # databases. Each gate gets fresh state, independent of local history and
    # other check-counter scripts; individual tests still use their own fixtures.
    for command in commands:
        print("Running:", " ".join(command), flush=True)
        with tempfile.TemporaryDirectory(prefix="wisp-regression-") as state_dir:
            env = {**os.environ, "WISP_HOME": state_dir}
            if subprocess.run(command, cwd=ROOT, env=env).returncode:
                failed.append(command)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
