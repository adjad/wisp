"""Synthetic Chrome popup/worker/host policy checks; no browser or host install."""
from pathlib import Path
import os
import re
import subprocess
from scripts import run_simulation_qa as simqa


def test_chrome_acquisition():
    root = Path(__file__).resolve().parents[2]
    node = str(simqa.resolve_node_runtime(os.environ.get("QA_NODE_RUNTIME")))
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "tests/browser_chrome/acquisition.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=60,
        env={"PATH": simqa.TRUSTED_PATH, "LANG": "C", "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    assert re.search(r"^# tests [1-9][0-9]*$", result.stdout, re.MULTILINE)
