"""Pure interval checks, no native read or collector construction."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc"),
    reason="macOS compiler required",
)
def test_native_activity_intervals(tmp_path):
    root = Path(__file__).resolve().parents[1]
    executable = tmp_path / "activity-checks"
    result = subprocess.run(
        [
            "/usr/bin/swiftc",
            "-parse-as-library",
            "-swift-version",
            "5",
            "-module-cache-path",
            str(tmp_path / "modules"),
            "app/Sources/WispApp/ActivityReader.swift",
            "tests/BehaviorActivityChecks.swift",
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
    assert "synthetic activity interval checks passed" in checked.stdout
