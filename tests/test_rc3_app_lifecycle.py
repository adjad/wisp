"""How Wisp starts when another copy is running, and how it moves itself into
Applications, read from the Swift sources.

Nothing is launched, moved or signalled here. The rules themselves are checked
in tests/PortGuardChecks.swift and tests/FirstRunSetupChecks.swift; these tests
hold the wiring around them in place.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "app" / "Sources" / "WispApp"


def _code(name: str) -> str:
    """Swift source without line comments."""
    text = (SOURCES / name).read_text(encoding="utf-8")
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


# -- a different copy of Wisp is already running (a01-app-lifecycle#4) ---------------

def test_a_different_running_copy_is_asked_about_not_handed_over_to():
    """Opening a downloaded update while the old build runs used to bring the old
    build forward and quit the new one without a word."""
    app = _code("AppDelegate.swift")
    launch = _body(app, "func applicationDidFinishLaunching(")
    decide = launch.index("SingleInstance.handOver(")
    assert launch.index("SingleInstance.shouldYield(") < decide
    # Judged on where each copy really is (a download runs from a temporary copy).
    assert "selfPath: Self.realLocation(Bundle.main.bundleURL).path" in launch
    assert "peerApps.map { $0.bundleURL.map { Self.realLocation($0).path } }" in launch
    assert "Translocation.original(of: url).original ?? url" in _body(app, "private static func realLocation(_ url: URL) -> URL")
    # A quitting copy is still waited for, not asked about.
    assert "waitsForPeerToQuit: waits" in launch
    silent = launch.index("case .silent:")
    ask = launch.index("case .ask:")
    wait = launch.index("case .waitForQuit:")
    assert decide < silent < ask < wait
    # The same app: the silent hand-over, as before.
    assert "yield(to: peerApps)" in launch[silent:ask]
    # A different one: nothing is quit before the person's click, and "no" hands over.
    asked = launch[ask:wait]
    question = asked.index("guard asksToReplaceRunningWisp(peerApps) else {")
    assert question < asked.index("yield(to: peerApps)") < asked.index("peer.terminate()")
    assert launch.count(".terminate()") == 1


def test_the_running_copy_is_only_ever_asked_to_quit():
    app = _code("AppDelegate.swift")
    # An ordinary quit, so the running copy stops its engine and service itself.
    assert "forceTerminate" not in app
    launch = _body(app, "func applicationDidFinishLaunching(")
    for forbidden in ("kill(", "SIGKILL", "SIGTERM", "Process()"):
        assert forbidden not in launch, forbidden
    # Then the bounded wait that Quit & Reopen already used; still running at the
    # deadline means the old hand-over.
    after = launch[launch.index("case .waitForQuit:"):]
    assert "FirstRunSetup.peerQuitWait" in after
    assert "if peerApps.allSatisfy(\\.isTerminated) { self.launch() } else { self.yield(to: peerApps) }" in after


def test_the_question_names_both_copies_and_defaults_to_no_change_on_escape():
    app = _code("AppDelegate.swift")
    ask = _body(app, "private func asksToReplaceRunningWisp(_ peers: [NSRunningApplication]) -> Bool")
    assert "SingleInstance.otherCopyDetail(" in ask
    assert "MoveToApplications.version(at: Bundle.main.bundleURL.path)" in ask
    assert ask.count("addButton(withTitle:") == 2
    assert 'addButton(withTitle: "Keep the Running One").keyEquivalent = "\\u{1b}"' in ask
    assert "return alert.runModal() == .alertFirstButtonReturn" in ask
    assert "terminate" not in ask
