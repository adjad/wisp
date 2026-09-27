"""Run the synthetic JS boundary suite in the repository's regression gate."""
from pathlib import Path
import os
import subprocess
import re
from scripts import run_simulation_qa as simqa


def _node_runtime():
    # Simulation QA intentionally supplies a system-only PATH. Do not broaden
    # it or accept a page/catalog/environment-provided fallback executable.
    return str(simqa.resolve_node_runtime(os.environ.get("QA_NODE_RUNTIME")))


def test_synthetic_page_extractor():
    root = Path(__file__).resolve().parents[2]
    node = _node_runtime()
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "tests/browser_dom/page-extractor.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=60,
        env={"PATH": simqa.TRUSTED_PATH, "LANG": "C", "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    assert re.search(r"^# tests [1-9][0-9]*$", result.stdout, re.MULTILINE)


def test_node_absence_is_a_failure(monkeypatch):
    import pytest
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    with pytest.raises(RuntimeError, match="Node is required"):
        _node_runtime()


def test_node_runtime_setting_can_pin_but_cannot_select_an_arbitrary_path(monkeypatch):
    import pytest
    selected = str(simqa.resolve_node_runtime())
    monkeypatch.setenv("QA_NODE_RUNTIME", selected)
    assert _node_runtime() == selected
    monkeypatch.setenv("QA_NODE_RUNTIME", "/bin/sh")
    with pytest.raises(RuntimeError, match="validated selection"):
        _node_runtime()


def test_direct_wrapper_does_not_execute_host_node_preload(monkeypatch, tmp_path):
    import json
    marker = tmp_path / "preload-ran"
    hook = tmp_path / "preload.cjs"
    hook.write_text("require('node:fs').writeFileSync(" + json.dumps(str(marker)) + ", 'unsafe'); throw Error('preloaded');")
    monkeypatch.setenv("NODE_OPTIONS", "--require=" + str(hook))
    monkeypatch.setenv("NODE_PATH", str(tmp_path))
    test_synthetic_page_extractor()
    assert not marker.exists()
