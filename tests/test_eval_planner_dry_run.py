"""Smoke-test planner menu generation without an inference client."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_planner.py"


def test_dry_run_builds_three_case_menus_without_a_model() -> None:
    home = ROOT / ".codex-home"
    (home / ".moe").mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["WISP_HOME"] = str(home / ".moe")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--cases",
         "calendar-001,chat-001,notes-001"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PLANNER DRY RUN — no inference client or model calls" in result.stdout
    for case_id in ("calendar-001", "chat-001", "notes-001"):
        assert f"CASE {case_id} " in result.stdout
    assert result.stdout.count("MENU (") == 3
    assert "removed PR #211 mappings for tools absent from the current registry" in result.stderr
    assert "keychain_read" in result.stderr
