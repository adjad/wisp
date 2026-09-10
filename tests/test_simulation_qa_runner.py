"""Regression contracts for Simulation QA reporting and child isolation."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from scripts import run_simulation_qa as simqa


def test_full_manifest_covers_the_reviewed_deterministic_test_tree() -> None:
    discovered = {
        str(path.relative_to(simqa.ROOT))
        for path in (simqa.ROOT / "tests").glob("test_*.py")
    } | {"air/tests/test_air.py"}

    assert set(simqa._selected_tests(["full"])) == discovered
    assert "tests/test_assistant_migrations.py" in simqa.PROFILE_TESTS["reliability"]
    assert "tests/test_assistant_recovery.py" in simqa.PROFILE_TESTS["reliability"]
    assert "tests/test_broad_web_search.py" in simqa.PROFILE_TESTS["reliability"]
    assert "tests/test_broad_web_search.py" in simqa.PROFILE_TESTS["research"]
    assert "tests/test_email_digest_presentation.py" in simqa.PROFILE_TESTS["sources"]
    assert "tests/test_privacy_sync.py" in simqa.PROFILE_TESTS["sources"]
    assert "tests/test_schedule_presentation.py" in simqa.PROFILE_TESTS["sources"]
    assert "tests/test_scheduled_send_preview.py" in simqa.PROFILE_TESTS["outbound"]
    assert "tests/test_regression_gate.py" in simqa.PROFILE_TESTS["reliability"]
    assert "tests/test_shell_boundary.py" in simqa.PROFILE_TESTS["safety"]


def test_native_manifest_automates_the_privacy_revocation_contract(tmp_path: Path) -> None:
    gates = dict(simqa._native_gates(tmp_path))

    compile_command = gates["native/privacy-sync-compile"]
    assert "app/Sources/WispApp/BrowserHistoryReader.swift" in compile_command
    assert "app/Sources/WispApp/ContactsReader.swift" in compile_command
    assert "tests/PrivacySyncChecks.swift" in compile_command
    assert simqa._NATIVE_GATE_DEPENDENCIES["native/privacy-sync-contract"] == (
        "native/privacy-sync-compile"
    )


def test_full_manifest_rejects_an_unreviewed_test_file(
        monkeypatch, tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_reviewed.py").write_text("", encoding="utf-8")
    (tests / "test_unreviewed.py").write_text("", encoding="utf-8")
    air = tmp_path / "air" / "tests"
    air.mkdir(parents=True)
    (air / "test_air.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(simqa, "ROOT", tmp_path)
    monkeypatch.setattr(simqa, "SAFE_FULL_TESTS", {
        "air/tests/test_air.py", "tests/test_reviewed.py",
    })

    with pytest.raises(RuntimeError, match=r"unclassified tests: \['tests/test_unreviewed.py'\]"):
        simqa._selected_tests(["full"])


def test_unittest_skip_event_leaves_parent_pass_count_unknown() -> None:
    output = (
        ".....s.......\n"
        "----------------------------------------------------------------------\n"
        "Ran 13 tests in 0.052s\n\n"
        "OK (skipped=1)\n"
    )
    assert simqa._counts(output, 0) == (None, 0, 1)


def test_unittest_failures_errors_and_expected_outcomes_are_accounted() -> None:
    output = (
        "Ran 7 tests in 0.100s\n\n"
        "FAILED (failures=1, errors=1, skipped=1, expected failures=1, "
        "unexpected successes=1)\n"
    )
    assert simqa._counts(output, 1) == (None, 3, 2)


def test_unittest_subtest_failures_do_not_invent_parent_pass_count() -> None:
    output = "Ran 2 tests in 0.010s\n\nFAILED (failures=2)\n"
    assert simqa._counts(output, 1) == (None, 2, 0)


def test_unittest_failed_status_without_details_is_incomplete() -> None:
    output = "Ran 2 tests in 0.010s\n\nFAILED\n"
    assert simqa._counts(output, 1) == (None, None, 0)


def test_actual_unittest_skipped_subtests_are_incomplete() -> None:
    fixture = """
import unittest

class Fixture(unittest.TestCase):
    def test_skipped_subtests(self):
        for item in range(2):
            with self.subTest(item=item):
                self.skipTest('fixture skip')

    def test_passing_parent(self):
        self.assertTrue(True)

unittest.main()
"""
    result = simqa._run(
        "fixture/skipped-subtests", [sys.executable, "-c", fixture]
    )
    assert (result.passed, result.failed, result.skipped) == (None, 0, 2)
    totals = simqa._totals([result], 0.0)
    assert totals["counts_complete"] is False
    assert totals["incomplete_gates"] == 1


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
        "gates": 1, "passed_gates": 1, "failed_gates": 0, "blocked_gates": 0,
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
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    monkeypatch.setenv("BASH_ENV", str(hostile_home / "bash-env.sh"))
    monkeypatch.setenv("ENV", str(hostile_home / "sh-env.sh"))
    monkeypatch.setenv("ZDOTDIR", str(hostile_home / "zsh"))
    monkeypatch.setenv("SHELLOPTS", "xtrace")
    monkeypatch.setenv("BASH_FUNC_printf%%", "() { builtin printf inherited; }")

    probe = (
        "import json, os, sys; from pathlib import Path; "
        "print(json.dumps({'home': str(Path.home()), 'home_exists': Path.home().is_dir(), "
        "'wisp_home': os.environ['WISP_HOME'], 'wispair_home': os.environ['WISPAIR_HOME'], "
        "'optimize': sys.flags.optimize, 'optimize_env': os.environ.get('PYTHONOPTIMIZE'), "
        "'startup_env': sorted(k for k in ('BASH_ENV', 'ENV', 'ZDOTDIR', 'SHELLOPTS') "
        "if k in os.environ), "
        "'exported_functions': sorted(k for k in os.environ if k.startswith('BASH_FUNC_')), "
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
    assert payload["optimize"] == 0
    assert payload["optimize_env"] == "0"
    assert payload["startup_env"] == []
    assert payload["exported_functions"] == []


def test_python_assertions_remain_enabled_under_host_optimization(
        monkeypatch) -> None:
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    result = simqa._run(
        "fixture/assertions",
        [sys.executable, "-c", "assert False, 'must execute'"],
    )
    assert simqa._gate_status(result) == "FAIL"
    assert result.returncode == 1
    assert "AssertionError: must execute" in result.stderr


def test_bash_startup_hook_is_removed_before_child_launch(
        monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "outside-child-home.marker"
    hook = tmp_path / "host-bash-env.sh"
    hook.write_text(f"printf inherited > {marker}\n", encoding="utf-8")
    monkeypatch.setenv("BASH_ENV", str(hook))

    result = simqa._run("fixture/bash", ["bash", "-c", "printf safe"])

    assert simqa._gate_status(result) == "PASS"
    assert result.stdout == "safe"
    assert not marker.exists()


def test_exported_bash_function_is_removed_before_child_launch(
        monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "outside-child-home.marker"
    monkeypatch.setenv(
        "BASH_FUNC_printf%%",
        f'() {{ builtin printf inherited > {marker}; builtin printf "$@"; }}',
    )

    hostile = simqa._run(
        "fixture/exported-bash-function", ["/bin/bash", "-c", "printf safe"]
    )
    monkeypatch.delenv("BASH_FUNC_printf%%")
    clean = simqa._run(
        "fixture/no-exported-bash-function", ["/bin/bash", "-c", "printf safe"]
    )

    assert simqa._gate_status(hostile) == "PASS"
    assert hostile.stdout == "safe"
    assert simqa._gate_status(clean) == "PASS"
    assert clean.stdout == "safe"
    assert not marker.exists()


def _fake_git_state(*, expected: str, ending: str | None = None,
                    ending_dirty: str = ""):
    revs = iter((expected, ending or expected))
    statuses = iter(("", ending_dirty))

    def fake_git(*args: str) -> str:
        if args[:2] == ("rev-parse", "HEAD"):
            return next(revs)
        if args[:2] == ("status", "--porcelain"):
            return next(statuses)
        raise AssertionError(args)

    return fake_git


def test_report_destination_inside_candidate_is_rejected(
        monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(simqa, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "run_simulation_qa.py", "--expected-sha", "a" * 40,
        "--only-native", "--report", str(tmp_path / "report.json"),
    ])

    with pytest.raises(SystemExit, match="2"):
        simqa.main()


def test_main_fails_and_reports_when_gate_dirties_checkout(
        monkeypatch, tmp_path: Path) -> None:
    expected = "a" * 40
    report = tmp_path / "dirty-report.json"
    monkeypatch.setattr(
        simqa, "_git",
        _fake_git_state(expected=expected, ending_dirty=" M tracked.txt"),
    )
    monkeypatch.setattr(simqa, "_native_gates", lambda _build: [])
    monkeypatch.setattr(sys, "argv", [
        "run_simulation_qa.py", "--expected-sha", expected,
        "--only-native", "--report", str(report),
    ])

    assert simqa.main() == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["sha_stable"] is True
    assert payload["worktree_clean"] is False
    assert payload["ending_dirty"] == [" M tracked.txt"]


def test_main_fails_and_reports_when_head_changes(
        monkeypatch, tmp_path: Path) -> None:
    expected = "a" * 40
    ending = "b" * 40
    report = tmp_path / "sha-report.json"
    monkeypatch.setattr(
        simqa, "_git", _fake_git_state(expected=expected, ending=ending),
    )
    monkeypatch.setattr(simqa, "_native_gates", lambda _build: [])
    monkeypatch.setattr(sys, "argv", [
        "run_simulation_qa.py", "--expected-sha", expected,
        "--only-native", "--report", str(report),
    ])

    assert simqa.main() == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "FAIL"
    assert payload["sha_stable"] is False
    assert payload["ending_sha"] == ending
    assert payload["worktree_clean"] is True


def test_native_launch_error_is_reported_and_dependent_gate_is_blocked(
        monkeypatch, tmp_path: Path) -> None:
    expected = "a" * 40
    report = tmp_path / "launch-report.json"
    missing_compiler = tmp_path / "missing-compiler"
    missing_binary = tmp_path / "missing-binary"
    monkeypatch.setattr(simqa, "_git", _fake_git_state(expected=expected))
    monkeypatch.setattr(simqa, "_native_gates", lambda _build: [
        ("native/mail-db-compile", [str(missing_compiler)]),
        ("native/mail-db-contract", [str(missing_binary)]),
    ])
    monkeypatch.setattr(sys, "argv", [
        "run_simulation_qa.py", "--expected-sha", expected,
        "--only-native", "--report", str(report),
    ])

    assert simqa.main() == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["status"] == "FAIL"
    assert payload["worktree_clean"] is True
    assert payload["totals"]["failed_gates"] == 1
    assert payload["totals"]["blocked_gates"] == 1
    compile_gate, contract_gate = payload["results"]
    assert compile_gate["status"] == "FAIL"
    assert compile_gate["returncode"] is None
    assert compile_gate["launch_error"].startswith("FileNotFoundError:")
    assert contract_gate["status"] == "BLOCKED"
    assert contract_gate["blocked_by"] == "native/mail-db-compile"
    assert contract_gate["launch_error"] is None
