"""The notch panel takes the keyboard (macOS 27.3: typing went to the app in front). Source checks
that OverlayPanel applies PanelKeyPolicy on every open and every press, and that "without the
keyboard" is read once per reveal and cleared there."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = (ROOT / "app/Sources/WispApp/OverlayPanel.swift").read_text()


def _body(name: str) -> str:
    start = PANEL.index(name)
    return PANEL[start:PANEL.index("\n    }\n", start)]


def test_every_open_goes_through_the_one_open_flag():
    reveal = PANEL[PANEL.index("    private func reveal(opening: Bool"):PANEL.index("    /// The next open shows the panel")]
    assert "let quiet = opening && revealsWithoutKey" in reveal
    assert "revealsWithoutKey = PanelKeyPolicy.flagAfterReveal(revealsWithoutKey)" in reveal
    calls = re.findall(r"bringForward\(([^)]*)\)", reveal)
    assert calls and all(c == "quiet: quiet" for c in calls), calls
    # The flag is cleared before any early return in reveal.
    assert reveal.index("flagAfterReveal") < reveal.index("guard let bar = barFrame()")


def test_the_flag_is_only_ever_cleared_inside_the_panel():
    sets = re.findall(r"^\s+revealsWithoutKey = (\w+)", PANEL, re.M)
    assert sets == ["PanelKeyPolicy"], sets


def test_asked_for_opens_make_key_and_activate():
    take = _body("    private func takeKeyboard()")
    assert "makeKeyAndOrderFront(nil)" in take and "NSApp.activate()" in take
    forward = _body("    private func bringForward(quiet: Bool)")
    assert "case .takeKeyboard: takeKeyboard()" in forward


def test_a_press_in_the_panel_always_takes_the_keyboard():
    send = _body("    override func sendEvent(_ event: NSEvent)")
    assert ".leftMouseDown" in send and ".rightMouseDown" in send
    assert "PanelKeyPolicy.pressTakesKeyboard" in send and "takeKeyboard()" in send
    assert send.rstrip().endswith("super.sendEvent(event)")


def test_frontmost_is_asked_of_the_workspace_not_isActive():
    assert "NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier" in PANEL
