"""Compile/run pure native mapping contracts; never instantiate native readers."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc"),
    reason="macOS Swift compiler required",
)
def test_native_history_mapping_contract(tmp_path):
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "history-contract"
    result = subprocess.run(
        [
            "/usr/bin/swiftc",
            "-parse-as-library",
            "-swift-version",
            "5",
            "-module-cache-path",
            str(tmp_path / "modules"),
            "app/Sources/WispApp/CalendarReader.swift",
            "app/Sources/WispApp/RemindersWriter.swift",
            "tests/BehaviorHistoryReaderRegression.swift",
            "-o",
            str(executable),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    checked = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=15
    )
    assert checked.returncode == 0, checked.stderr
    assert "synthetic behavior history mapping checks passed" in checked.stdout
