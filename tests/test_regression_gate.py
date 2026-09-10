"""Contracts for complete regression-gate discovery and isolation."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts import test_replay_failure_fixes as gate


def test_gate_discovers_every_configured_test_module() -> None:
    expected = []
    for root in (gate.ROOT / "tests",):
        for directory, _subdirs, files in os.walk(root):
            expected.extend(
                Path(directory) / name
                for name in files
                if fnmatch.fnmatchcase(name, "test_*.py")
            )
    expected.sort()
    assert gate._tests() == expected


def test_gate_recursively_discovers_nested_modules(monkeypatch, tmp_path) -> None:
    nested = tmp_path / "tests" / "nested" / "deeper"
    nested.mkdir(parents=True)
    expected = nested / "test_nested_failure.py"
    expected.write_text("def test_failure(): assert False\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    assert gate._tests() == [expected]


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


def test_source_text_cannot_reclassify_an_ordinary_pytest_module(
        monkeypatch, tmp_path) -> None:
    path = tmp_path / "tests" / "test_adversarial.py"
    path.parent.mkdir()
    path.write_text(
        "# sys.exit(0) must not change classification\n"
        "def test_failure(): assert False\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    command = gate._command(path)
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert subprocess.run(command, cwd=tmp_path, check=False).returncode == 1


def test_empty_pytest_module_cannot_pass(monkeypatch, tmp_path) -> None:
    path = tmp_path / "tests" / "test_empty.py"
    path.parent.mkdir()
    path.write_text("# deliberately no tests\n", encoding="utf-8")
    monkeypatch.setattr(gate, "ROOT", tmp_path)

    command = gate._command(path)
    assert subprocess.run(command, cwd=tmp_path, check=False).returncode == 5


def test_legacy_script_must_report_a_nonzero_test_count() -> None:
    assert gate._reports_nonzero_test_count("29 passed, 0 failed\n")
    assert gate._reports_nonzero_test_count("Ran 12 tests in 0.2s\nOK\n")
    assert gate._reports_nonzero_test_count("ok: 85 prompts validated\n")
    assert not gate._reports_nonzero_test_count("0 passed, 0 failed\n")
    assert not gate._reports_nonzero_test_count("completed successfully\n")


def test_gate_failure_baseline_is_an_exact_ratchet() -> None:
    known = set(gate.KNOWN_BASELINE_FAILURES)

    assert gate._classify_failures(known) == ([], [])
    assert gate._classify_failures(known | {"tests/test_new_regression.py"}) == (
        ["tests/test_new_regression.py"],
        [],
    )
    resolved = sorted(known)[0]
    assert gate._classify_failures(known - {resolved}) == ([], [resolved])
    assert gate._classify_failures(known, {resolved}) == ([resolved], [])


def test_known_baseline_requires_exact_failures_and_counts() -> None:
    path = "tests/test_forced_step_withholding.py"
    expected = gate.KNOWN_BASELINE_RESULTS[path]
    failures = "\n".join(f"  {line}" for line in sorted(expected[2]))
    output = f"{failures}\n\n{expected[0]} passed, {expected[1]} failed\n"

    assert gate._matches_known_baseline(path, 1, output)
    assert not gate._matches_known_baseline(path, 0, output)
    assert not gate._matches_known_baseline(path, 2, output)
    assert not gate._matches_known_baseline(path, 1, "")
    assert not gate._matches_known_baseline(
        path, 1, output.replace("14 passed, 2 failed", "15 passed, 2 failed"),
    )
    assert not gate._matches_known_baseline(
        path, 1, output + "  FAIL newly broken check\n",
    )
    assert not gate._matches_known_baseline(
        path, 1, output.replace("takes the reorganize-files route", "changed identity"),
    )
    assert not gate._matches_known_baseline(
        path, 1, output + "Traceback (most recent call last):\nboom\n",
    )
    assert not gate._matches_known_baseline(path, 1, output + output)
    assert not gate._matches_known_baseline(
        path, 1, output.replace(
            "FAIL run_shell is on the table", "FAIL takes the reorganize-files route",
        ),
    )


def test_empty_or_worsened_known_module_is_a_regression() -> None:
    path = "tests/test_forced_step_withholding.py"
    known = set(gate.KNOWN_BASELINE_FAILURES)

    # The runner places either shape in baseline_mismatches; classification
    # must block even though the module path itself is expected to be red.
    assert not gate._matches_known_baseline(path, 0, "0 passed, 0 failed\n")
    assert gate._classify_failures(known, {path}) == ([path], [])


def test_known_failures_are_discovered_and_owned_by_the_routing_repair() -> None:
    discovered = {str(path.relative_to(gate.ROOT)) for path in gate._tests()}

    assert gate.KNOWN_BASELINE_FAILURES <= discovered
    assert gate.KNOWN_BASELINE_FAILURES == {
        "tests/test_alias_reachability.py",
        "tests/test_forced_step_withholding.py",
        "tests/test_router_scoping.py",
        "tests/test_semantic_routing.py",
    }


def test_legacy_manifest_is_explicit_and_fully_discovered() -> None:
    discovered = {str(path.relative_to(gate.ROOT)) for path in gate._tests()}

    assert gate.LEGACY_SCRIPT_TESTS <= discovered
    assert "air/tests/test_air.py" not in gate.LEGACY_SCRIPT_TESTS
