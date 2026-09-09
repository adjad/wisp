"""Contracts for complete regression-gate discovery and isolation."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from scripts import test_replay_failure_fixes as gate


def test_gate_discovers_every_configured_test_module() -> None:
    expected = sorted((gate.ROOT / "tests").glob("test_*.py"))
    expected += sorted((gate.ROOT / "air" / "tests").glob("test_*.py"))
    assert gate._tests() == expected


def test_gate_children_use_disposable_home_and_wisp_state(monkeypatch) -> None:
    monkeypatch.setenv("WISP_HOME", "/host/wisp")
    monkeypatch.setenv("WISP_LIVE_REMINDER_TEST", "1")
    with tempfile.TemporaryDirectory(prefix="wisp-gate-contract-") as state_dir:
        env = gate._environment(state_dir)

        assert Path(env["HOME"]).parent == Path(state_dir)
        assert Path(env["WISP_HOME"]).parent == Path(state_dir)
        assert Path(env["WISPAIR_HOME"]).parent == Path(state_dir)
        assert env["WISP_TEST_PYTHON"]
        assert env["PYTHONOPTIMIZE"] == "0"
        assert "WISP_LIVE_REMINDER_TEST" not in env
        assert env["PYTHONPATH"] == str(gate.ROOT)


def test_gate_uses_the_invoking_interpreter() -> None:
    path = gate.ROOT / "tests" / "test_regression_gate.py"
    assert gate._command(path)[:3] == [sys.executable, "-m", "pytest"]
