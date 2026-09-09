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


def test_gate_failure_baseline_is_an_exact_ratchet() -> None:
    known = set(gate.KNOWN_BASELINE_FAILURES)

    assert gate._classify_failures(known) == ([], [])
    assert gate._classify_failures(known | {"tests/test_new_regression.py"}) == (
        ["tests/test_new_regression.py"],
        [],
    )
    resolved = sorted(known)[0]
    assert gate._classify_failures(known - {resolved}) == ([], [resolved])


def test_known_failures_are_discovered_and_owned_by_the_routing_repair() -> None:
    discovered = {str(path.relative_to(gate.ROOT)) for path in gate._tests()}

    assert gate.KNOWN_BASELINE_FAILURES <= discovered
    assert gate.KNOWN_BASELINE_FAILURES == {
        "tests/test_alias_reachability.py",
        "tests/test_forced_step_withholding.py",
        "tests/test_router_scoping.py",
        "tests/test_semantic_routing.py",
    }
