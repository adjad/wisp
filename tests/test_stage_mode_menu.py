"""Demo mode in the menu bar's drop-down, read from the Swift sources.

No process is started and nothing is compiled: the sources are read as text with
line comments stripped.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "Sources" / "WispApp"


def _code(text: str) -> str:
    """Swift source without line comments."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _stage() -> str:
    return _code((APP / "StageMode.swift").read_text(encoding="utf-8"))


def _delegate() -> str:
    return _code((APP / "AppDelegate.swift").read_text(encoding="utf-8"))


def _function(source: str, name: str) -> str:
    match = re.search(r"func " + re.escape(name) + r"\(.*?\n    \}\n", source, re.DOTALL)
    assert match, name
    return match.group(0)


def test_the_menu_has_a_demo_mode_item_that_toggles_it():
    assert 'static let menuTitle = "Demo mode"' in _stage()
    menu = _function(_delegate(), "buildMenu")
    assert re.search(r"addItem\(withTitle: StageMode\.menuTitle,\s*action: #selector\(toggleDemoModeFromMenu\)", menu)
    assert "item.target = self" in menu
    toggle = _function(_delegate(), "toggleDemoModeFromMenu")
    assert "StageMode.shared.set(!StageMode.shared.isOn)" in toggle


def test_the_checkmark_and_note_are_refreshed_each_time_the_menu_opens():
    refresh = _function(_delegate(), "refreshDemoModeMenuItems")
    assert "state = stage.isOn ? .on : .off" in refresh
    assert "StageMode.menuNote(" in refresh
    opens = _function(_delegate(), "toggle")
    assert opens.index("refreshDemoModeMenuItems()") < opens.index("statusItem.button?.performClick(nil)")


def test_it_starts_off_and_is_never_persisted():
    stage = _stage()
    assert re.search(r"private\(set\) var isOn = false\b", stage)
    for saved in ("UserDefaults", "FileManager", ".write(", "~/.moe", "@AppStorage"):
        assert saved not in stage, saved


def test_it_ends_after_four_hours_by_the_apps_own_timer():
    stage = _stage()
    assert "static let maxSeconds: TimeInterval = 4 * 60 * 60" in stage
    auto_off = _function(stage, "startAutoOff")
    assert "Self.maxSeconds" in auto_off and "self.set(false)" in auto_off


def test_it_keeps_the_mac_awake_only_while_on():
    stage = _stage()
    awake = _function(stage, "holdAwake")
    assert "ProcessInfo.processInfo.beginActivity(" in awake
    assert ".idleSystemSleepDisabled" in awake and ".idleDisplaySleepDisabled" in awake
    assert "ProcessInfo.processInfo.endActivity(" in _function(stage, "stopLocally")


def test_the_backend_up_hook_pushes_it_again():
    delegate = _delegate()
    hook = delegate[delegate.index("backend.onReachabilityChange"):]
    up = hook[:hook.index("return\n")]
    assert "self.resyncAllReaders()" in up and "StageMode.shared.pushIfOn()" in up


def test_it_puts_to_the_stage_mode_route_and_a_refusal_turns_it_off():
    stage = _stage()
    assert 'private static let path = "stage_mode"' in stage
    push = _function(stage, "push")
    assert 'sendTold("PUT", Self.path, body: ["enabled": on]' in push
    assert "self.stopLocally()" in push and "self.problem =" in push
    assert "X-Forwarded" not in stage


def test_the_menu_note_wording():
    stage = _stage()
    assert "Fastest answers for a live demo. Uses more power." in stage
    assert "Turns off by itself in" in stage
    assert "—" not in stage


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_stage_mode_menu.py"' in manifest
