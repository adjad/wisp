"""Export Chat Debug Log keeps the notch live.

runModal on the export's alert locked the notch until it was answered. From the
menu bar, which never activates Wisp, macOS also bounced the Dock icon and could
open the alert behind other apps, so the only way out was clicking the icon. The
behavior is exercised by scripts/test_overlay_transition.sh, which CI does not
run, so this holds the shape in CI. Reads source only.
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app/Sources/WispApp"


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    # The next declaration at the same indent ends it.
    match = re.compile(r"^    (?:///|//|private |func |@objc |static |nonisolated )", re.M).search(
        source, source.index("\n    }\n", start))
    return source[start:match.start() if match else len(source)]


def test_export_and_its_notice_open_no_modal_session():
    delegate = (APP / "AppDelegate.swift").read_text()
    for signature in ("private func exportDebugLog() async {", "private func showDebugLogNotice("):
        body = _body(delegate, signature)
        assert "runModal(" not in body and "NSAlert()" not in body, signature
    alert = (APP / "NonModalAlert.swift").read_text()
    assert "runModal(" not in alert and "beginSheetModal(" not in alert


def test_export_builds_and_writes_off_the_main_actor():
    model = (APP / "OverlayModel.swift").read_text()
    export = _body(model, "func exportDebugLog(to directory: URL? = nil) async -> DebugLogExport {")
    assert "Task.detached" in export and "writeDebugLog(snapshot" in export
    for signature in ("nonisolated static func writeDebugLog(", "nonisolated static func writePrivate(",
                      "nonisolated static func debugExportFiles("):
        assert signature in model
