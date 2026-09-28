"""Run the synthetic D1 live-capture, private-filter and ModelView JS suites.

Synthetic DOM fixtures on reserved .invalid hosts only: no browser, network,
native host, model or user state. Discovered by the regression gate.
"""
from pathlib import Path
import os
import re
import subprocess
from scripts import run_simulation_qa as simqa

SUITES = (
    "tests/browser_dom/live-acquisition.test.cjs",
    "tests/browser_dom/live-private-filter.test.cjs",
    "tests/browser_dom/model-view.test.cjs",
)


def test_synthetic_live_capture_and_model_view():
    root = Path(__file__).resolve().parents[2]
    # Validated fixed-path Node only; PATH stays system-only and preload
    # variables are not inherited.
    node = str(simqa.resolve_node_runtime(os.environ.get("QA_NODE_RUNTIME")))
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", *SUITES],
        cwd=root, capture_output=True, text=True, timeout=120,
        env={"PATH": simqa.TRUSTED_PATH, "LANG": "C", "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    match = re.search(r"^# tests ([0-9]+)$", result.stdout, re.MULTILINE)
    assert match and int(match.group(1)) >= 30, result.stdout[-2000:]
