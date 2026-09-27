"""Synthetic-only A13 suite; no browser, network, user data, or approval authority."""
from pathlib import Path
import re
import subprocess

from scripts import run_simulation_qa as simqa


def test_synthetic_action_executor():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(simqa.resolve_node_runtime()), "--test", "--test-reporter=tap",
         "tests/browser_actions/action-executor.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=60,
        env={"PATH": simqa.TRUSTED_PATH, "LANG": "C", "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    assert re.search(r"^# tests [1-9][0-9]*$", result.stdout, re.MULTILINE)


def test_host_node_preloads_are_not_executed(monkeypatch, tmp_path):
    import json

    marker = tmp_path / "preload-ran"
    hook = tmp_path / "preload.cjs"
    hook.write_text("require('node:fs').writeFileSync(" + json.dumps(str(marker)) + ", 'unsafe');")
    monkeypatch.setenv("NODE_OPTIONS", "--require=" + str(hook))
    monkeypatch.setenv("NODE_PATH", str(tmp_path))
    test_synthetic_action_executor()
    assert not marker.exists()
