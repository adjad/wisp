"""Run the synthetic JS boundary suite in the repository's regression gate."""
from pathlib import Path
import shutil
import subprocess
import re


def test_synthetic_page_extractor():
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    assert node, "Node is required; missing runtime is not a passing privacy check"
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "tests/browser_dom/page-extractor.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# fail 0" in result.stdout
    assert re.search(r"^# tests [1-9][0-9]*$", result.stdout, re.MULTILINE)
