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


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc"),
    reason="macOS compiler required",
)
def test_signal_settings_async_order(tmp_path):
    """Actual model, private actual backend snapshots, controlled transport only."""
    import json

    from service.behavior.engine import BehaviorEngine

    root = Path(__file__).resolve().parents[1]
    def scenario(name, changes):
        engine = BehaviorEngine(tmp_path / f"{name}.db", tz="UTC", clock=lambda: 1_792_051_200)
        try:
            initial = engine.signal_settings()
            replies = [engine.configure_signals(change, 1_792_051_200 + i)
                       for i, change in enumerate(changes)]
            return {"initial": initial, "changes": changes, "replies": replies}
        finally:
            engine.close()

    names = ["activity", "websites", "email", "messages", "notes", "calendar_context",
             "reminders_context", "interpretation", "remote_analysis"]
    cases = {
        "cross": scenario("cross", [{"activity": True}, {"notes": True}]),
        "same": scenario("same", [{"notes": True}, {"notes": False}]),
        "bounded": scenario("bounded", [{"activity": True}] + [{name: False} for name in names]),
    }
    wire = tmp_path / "wire.json"
    wire.write_text(json.dumps({"cases": cases, "names": names}))
    source = (root / "app/Sources/WispApp/LearnedView.swift").read_text()
    # Exact model/value declarations; exclude every view/window and real client.
    pure = source[source.index("struct LearnedPattern:"):source.index("struct LearnedView: View")]
    assert "NSApplication" not in pure and "NSWindow" not in pure
    model = tmp_path / "LearnedModel.swift"
    model.write_text("import Foundation\nimport Combine\nimport CoreFoundation\n" + pure)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "WISP_BEHAVIOR": "0"}
    for key, leaf in [("HOME", "home"), ("WISP_HOME", "state"), ("TMPDIR", "tmp")]:
        folder = tmp_path / leaf
        folder.mkdir()
        env[key] = str(folder)
    env["WISP_ENGINE_ROOT"] = str(tmp_path / "absent-engine")
    executable = tmp_path / "signal-checks"
    compiled = subprocess.run(
        ["/usr/bin/swiftc", "-parse-as-library", "-swift-version", "5",
         "-module-cache-path", str(tmp_path / "modules"), str(model),
         str(root / "tests/BehaviorSignalSettingsRegression.swift"), "-o", str(executable)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert compiled.returncode == 0, compiled.stderr
    checked = subprocess.run(
        [str(executable), str(wire)], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=15,
    )
    (tmp_path / "signal-checks.stdout").write_text(checked.stdout)
    (tmp_path / "signal-checks.stderr").write_text(checked.stderr)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "synthetic signal settings checks passed" in checked.stdout


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("swiftc"),
    reason="macOS compiler required",
)
def test_global_learning_settings_confirmation(tmp_path):
    """Actual consent state/methods/copy, with manually ordered HTTP outcomes."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/Sources/WispApp/SettingsView.swift").read_text()
    properties = source[source.index("    @Published var behaviorEnabled"):
                        source.index("    @Published var behaviorForgetStatus")]
    methods = source[source.index("    func refreshBehavior() async"):
                     source.index("    /// Deletes everything Wisp learned")]
    copy_start = source.index("    private var behaviorExplanation: String")
    copy_end = min(source.index(marker, copy_start) for marker in
                   ("    private var panes:", "    private var behaviorLearningLabel:")
                   if marker in source[copy_start:])
    copy = source[copy_start:copy_end]
    client_source = (root / "app/Sources/WispApp/WispClient.swift").read_text()
    client_methods = client_source[client_source.index("    func setBehaviorEnabled("):
                                   client_source.index("    // DELETE /behavior/data")]
    for segment in (properties, methods, copy, client_methods):
        assert "URLSession" not in segment and "NSApplication" not in segment
    extracted = tmp_path / "SettingsConsent.swift"
    extracted.write_text(
        "import Foundation\nimport Combine\n"
        "@MainActor final class SettingsLoader: ObservableObject {\n"
        "private let client = WispClient()\n" + properties + methods + "\n}\n"
        "@MainActor struct BehaviorPrivacyCopy {\nlet loader: SettingsLoader\n" + copy
        + "\nvar text: String { behaviorExplanation }\n}\n"
        + "extension WispClient {\n" + client_methods + "\n}\n"
    )
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "WISP_BEHAVIOR": "0"}
    for key, leaf in (("HOME", "home"), ("WISP_HOME", "state"), ("TMPDIR", "tmp")):
        folder = tmp_path / leaf
        folder.mkdir()
        env[key] = str(folder)
    env["WISP_ENGINE_ROOT"] = str(tmp_path / "absent-engine")
    executable = tmp_path / "consent-checks"
    compiled = subprocess.run(
        ["/usr/bin/swiftc", "-parse-as-library", "-swift-version", "5",
         "-module-cache-path", str(tmp_path / "modules"), str(extracted),
         str(root / "tests/BehaviorGlobalSettingsRegression.swift"), "-o", str(executable)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert compiled.returncode == 0, compiled.stderr
    checked = subprocess.run(
        [str(executable)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=15
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "synthetic global consent checks passed" in checked.stdout
