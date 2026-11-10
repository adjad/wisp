"""A legacy script module must run every test it defines.

Modules in the gate's LEGACY_SCRIPT_TESTS run as ``python file.py``, so only
what their own ``__main__`` path calls is executed. A pytest-style test added
to one of them is skipped silently while the module's other counts keep the
gate green.
"""
from __future__ import annotations

import sys

from scripts import test_replay_failure_fixes as gate


def test_every_legacy_script_names_each_test_it_defines() -> None:
    never_run = {
        relative: missing
        for relative in sorted(gate.LEGACY_SCRIPT_TESTS)
        if (missing := gate._unreferenced_legacy_tests(gate.ROOT / relative))
    }

    assert never_run == {}, (
        "these tests never run in the gate; call them from the script or move "
        f"them to a pytest module: {never_run}"
    )


def test_scan_reports_only_the_tests_a_script_never_names(tmp_path) -> None:
    path = tmp_path / "test_sample.py"
    path.write_text(
        "import unittest\n"
        "def helper(): pass\n"
        "def test_called(): pass\n"
        "def test_listed(): pass\n"
        "def test_forgotten(monkeypatch): assert True\n"
        "async def test_forgotten_async(): assert True\n"
        "class Cases(unittest.TestCase):\n"
        "    def test_method(self): pass\n"
        "def main():\n"
        "    test_called()\n"
        "    for case in (test_listed,):\n"
        "        case()\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
        "def test_after_the_entry_point(tmp_path): assert True\n",
        encoding="utf-8",
    )

    assert gate._unreferenced_legacy_tests(path) == [
        "test_forgotten", "test_forgotten_async", "test_after_the_entry_point",
    ]


def test_scan_does_not_count_a_definition_as_a_call(tmp_path) -> None:
    path = tmp_path / "test_only_defined.py"
    path.write_text(
        "def test_alone(): pass\n"
        "test_alone.__doc__ = 'named, so the scan accepts it'\n"
        "def test_never_named(): pass\n",
        encoding="utf-8",
    )

    assert gate._unreferenced_legacy_tests(path) == ["test_never_named"]


def test_web_search_module_runs_under_pytest() -> None:
    # Its three cloud-evidence tests are module-level async functions, which
    # unittest.main() ignores. Under pytest they run beside the unittest cases.
    relative = "tests/test_broad_web_search.py"

    assert relative not in gate.LEGACY_SCRIPT_TESTS
    assert gate._command(gate.ROOT / relative)[:3] == [sys.executable, "-m", "pytest"]
