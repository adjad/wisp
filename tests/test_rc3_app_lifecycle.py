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


# -- Move to Applications no longer freezes Wisp (a01-app-lifecycle#5) ---------------

MOVE_SIGNATURE = "static func move(from bundle: URL, original: URL?, target: URL = URL(fileURLWithPath: destination)) throws"


def test_the_move_runs_off_the_main_thread_under_a_progress_window():
    """Copying the bundle (about a gigabyte) and clearing its quarantine marks on the
    main thread left nothing on screen and a Dock icon that didn't answer."""
    move = _code("MoveToApplications.swift")
    offer = _body(move, "static func offerIfNeeded(")
    # Still only after the person's clicks, and the same synchronous move.
    assert offer.index("ask.runModal()") < offer.index("replace.runModal()") < offer.index("try runShowingProgress {")
    assert "try runShowingProgress { try move(from: bundle, original: original) }" in offer
    assert offer.count("move(from:") == 1
    progress = _body(move, "private static func runShowingProgress(")
    shown = progress.index("window.makeKeyAndOrderFront(nil)")
    begun = progress.index("NSApp.beginModalSession(for: window)")
    assert shown < begun < progress.index("try offMainThread(work)")
    # The window goes away however the move ends.
    closing = _body(progress, "defer")
    assert "NSApp.endModalSession(session)" in closing and "window.orderOut(nil)" in closing
    assert "NSApp.runModalSession(session)" in progress
    # A session this loop ends by itself: runModal would need a click to return.
    assert "runModal(" not in progress
    worker = _body(move, "static func offMainThread(")
    assert worker.index("DispatchQueue.global(") < worker.index("try work()")
    assert "while !result.isFinished { wait() }" in worker
    assert "if let failure = result.failure { throw failure }" in worker
    assert "DispatchQueue.main" not in worker


def test_the_progress_window_has_no_buttons_and_stays_in_view():
    move = _code("MoveToApplications.swift")
    window = _body(move, "private static func progressWindow() -> NSWindow")
    # No close button; minimizable like every Wisp window.
    assert "styleMask: [.titled, .miniaturizable]" in window
    assert "window.hidesOnDeactivate = false" in window
    assert "window.title = progressTitle" in window and "isIndeterminate = true" in window
    assert "NSButton" not in window and "addButton" not in window
    text = (SOURCES / "MoveToApplications.swift").read_text(encoding="utf-8")
    assert 'static let progressTitle = "Moving Wisp to Applications…"' in text


def test_the_move_itself_is_unchanged():
    """Copy, Trash the old one, swap in, clear quarantine, then Trash the download:
    the order, the staging cleanup and what gets copied are as they were."""
    move = _code("MoveToApplications.swift")
    body = _body(move, MOVE_SIGNATURE)
    steps = ["try fm.copyItem(at: bundle, to: staging)", "try fm.trashItem(at: target, resultingItemURL: nil)",
             "try fm.moveItem(at: staging, to: target)", 'xattr.arguments = ["-dr", "com.apple.quarantine", target.path]',
             "xattr.waitUntilExit()", "try? fm.trashItem(at: original, resultingItemURL: nil)"]
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "removeItem(at: staging)" in body and "MoveFailure(underlying: error, oldWispTrashed: oldTrashed)" in body
    # It is run from a background queue, so it must not touch the main actor or AppKit.
    for forbidden in ("NSApp", "NSAlert", "MainActor", "DispatchQueue.main", "NSWorkspace"):
        assert forbidden not in body, forbidden
    assert "@MainActor" not in move[move.index(MOVE_SIGNATURE) - 120:move.index(MOVE_SIGNATURE)]
    # The running bundle is what is copied, as before.
    offer = _body(move, "static func offerIfNeeded(")
    assert "let bundle = Bundle.main.bundleURL" in offer
    app = _code("AppDelegate.swift")
    launch = _body(app, "private func launch()")
    assert launch.index("MoveToApplications.offerIfNeeded() == .relaunching") < launch.index("WispReset.readMarker()")
