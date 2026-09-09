"""Regression contracts for Simulation QA reporting and child isolation."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from scripts import run_simulation_qa as simqa


def test_unittest_skip_is_subtracted_from_passed_total() -> None:
    output = (
        ".....s.......\n"
        "----------------------------------------------------------------------\n"
        "Ran 13 tests in 0.052s\n\n"
        "OK (skipped=1)\n"
    )
    assert simqa._counts(output, 0) == (12, 0, 1)


def test_unittest_failures_errors_and_expected_outcomes_are_accounted() -> None:
    output = (
        "Ran 7 tests in 0.100s\n\n"
        "FAILED (failures=1, errors=1, skipped=1, expected failures=1, "
        "unexpected successes=1)\n"
    )
    assert simqa._counts(output, 1) == (2, 3, 2)


def test_pytest_summary_accepts_any_outcome_order() -> None:
    assert simqa._counts("2 failed, 14 passed, 1 skipped in 0.39s\n", 1) == (14, 2, 1)
    assert simqa._counts("31 passed, 1 skipped in 0.29s\n", 0) == (31, 0, 1)


def test_partial_pytest_summary_does_not_invent_failed_count() -> None:
    assert simqa._counts("14 passed\nINTERNALERROR> collection aborted\n", 3) == (14, None, 0)


def test_legacy_counter_and_native_check_summaries_are_counted() -> None:
    assert simqa._counts("29 passed, 0 failed\n", 0) == (29, 0, 0)
    assert simqa._counts("Research Library: 95 native checks passed\n", 0) == (95, 0, 0)


def test_commands_without_a_count_contract_are_explicitly_unreported() -> None:
    assert simqa._counts("Build complete!\n", 0) == (None, None, None)
    result = simqa.GateResult("compile", ["fixture"], 0, 0.1, None, None, None, "", "")
    totals = simqa._totals([result], 0.1)
    assert totals == {
        "gates": 1, "passed_gates": 1, "failed_gates": 0,
        "reported_gates": 0, "unreported_gates": 1, "incomplete_gates": 1,
        "counts_complete": False,
        "passed": 0, "failed": 0, "skipped": 0, "duration_s": 0.1,
    }


def test_child_process_uses_fake_home_and_strips_host_wisp_overrides(
        monkeypatch, tmp_path: Path) -> None:
    hostile_home = tmp_path / "host-home"
    hostile_template = hostile_home / "installed-template.jinja"
    hostile_template.parent.mkdir()
    hostile_template.write_text("host template must not be read")
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.setenv("CODEX_HOME", str(hostile_home / ".codex"))
    monkeypatch.setenv("WISP_LING_TEMPLATE", str(hostile_template))
    monkeypatch.setenv("WISP_LIVE_REMINDER_TEST", "1")
    monkeypatch.setenv("WISP_MAIL_REPLY_LIVE", "1")
    monkeypatch.setenv("WISP_QA_SEED", "1")
    monkeypatch.setenv("WISP_SANDBOX_HOME", str(hostile_home / "sandbox"))
    monkeypatch.setenv("WISP_BRAVE_SEARCH_API_KEY", "host-secret")
    monkeypatch.setenv("WISP_OPENALEX_API_KEY", "host-secret")

    probe = (
        "import json, os; from pathlib import Path; "
        "print(json.dumps({'home': str(Path.home()), 'home_exists': Path.home().is_dir(), "
        "'wisp_home': os.environ['WISP_HOME'], 'wispair_home': os.environ['WISPAIR_HOME'], "
        "'present': sorted(k for k in os.environ if k.startswith('WISP_') or k == 'CODEX_HOME')}))"
    )
    result = simqa._run("fixture/environment", [sys.executable, "-c", probe])
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert (result.passed, result.failed, result.skipped) == (None, None, None)
    assert payload["home_exists"]
    assert payload["home"] != str(hostile_home)
    assert Path(payload["wisp_home"]).parent == Path(payload["home"]).parent
    assert Path(payload["wispair_home"]).parent == Path(payload["home"]).parent
    assert payload["present"] == ["WISP_HOME", "WISP_TEST_PYTHON"]
