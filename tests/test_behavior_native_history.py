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


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"), reason="macOS Swift compiler required")
def test_actual_learned_values_preserve_source_uncertainty(tmp_path):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from service.behavior.evidence import pattern_evidence
    from service.behavior.predict import _contacts
    from service.behavior.types import Pattern, Window
    root = Path(__file__).resolve().parents[1]
    text = (root / "app/Sources/WispApp/LearnedView.swift").read_text()
    # Compile the actual value declarations, excluding imports and all View/UI
    # declarations. The fixture cannot create an app, window or native reader.
    values = tmp_path / "LearnedValues.swift"
    values.write_text("import Foundation\n" + text[text.index("struct LearnedPattern:"):text.index("/// Section order")])
    now = datetime(2026, 10, 15, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    rows = []
    for calls, messages in [(None, None), (0, 0), (True, "4"), (-1, -2), (1.0, float("inf")), (0, 1), (1, 0), (1, 1)]:
        p = pattern_evidence(Pattern("saved", "contact_rhythm", "Synthetic Person", "You talk every morning",
            Window((), 0, 1440), confidence=.8, stats=dict(calls=calls, messages=messages, evidence_version=2)))
        prediction = _contacts([p], now, now+3600, ZoneInfo("UTC"))[0]
        rows.append(dict(pattern=p.to_dict(), prediction=prediction.to_dict()))
    wire = tmp_path / "wire.json"
    wire.write_text(json.dumps(rows, allow_nan=False))
    executable = tmp_path / "learned-values"
    compiled = subprocess.run(["/usr/bin/swiftc", "-parse-as-library", "-swift-version", "5",
        "-module-cache-path", str(tmp_path / "modules"), str(values),
        "tests/BehaviorLearnedValuesRegression.swift", "-o", str(executable)],
        cwd=root, capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stderr
    checked = subprocess.run([str(executable), str(wire)], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr
    assert "8 source-bound Learned value checks passed" in checked.stdout
