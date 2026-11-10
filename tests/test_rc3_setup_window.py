"""rc3 source checks for the first-run window: what a pure Swift check can't see,
because it is wiring between the window, the app delegate and the backend
manager. Reads source only; no app, service or network."""
from itertools import takewhile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"


def _read(name: str) -> str:
    return (APP / name).read_text()


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


def test_a_failed_start_is_said_in_the_window_with_a_real_retry():
    """l06-first-run-e2e#1: during setup the notch can't be opened, so the window
    itself says why Wisp's start failed, and its Check again really starts Wisp again."""
    setup = _read("FirstRunSetupView.swift")
    # The failure is heard as it happens, and read from the app's record when the window opens.
    assert "NotificationCenter.default.publisher(for: .wispBackendStartFailed)" in setup
    assert "startFailure = recordedStartFailure()" in _body(setup, ".onAppear {")
    card = _body(setup, "@ViewBuilder private var modelCard: some View")
    checking = card[card.index("case .checking:"):card.index("case .startingEngine:")]
    assert checking.index("if let failure = startFailure {") < checking.index('title: "Starting Wisp…"')
    assert "title: FirstRunBrain.startFailedTitle, detail: failure" in checking
    # A failed start needs a click at once: Return moves to the card, as for a slow start.
    assert "FirstRunBrain.startNeedsClick(failure: startFailure" in _body(setup, "private var stalled: Bool")
    # One button, one action: start Wisp again, then ask it. Never a bare status read.
    button = _body(setup, "private func checkAgainButton(takesReturn: Bool = false)")
    assert "startAgainAndCheck()" in button and "setup.refresh()" not in button
    again = _body(setup, "private func startAgainAndCheck()")
    assert again.index("restartWisp()") < again.index("await setup.refresh()")


def test_the_window_gets_the_notchs_retry_and_never_restarts_a_wisp_that_answers():
    """The gate that holds the notch back is untouched; the window is handed the
    notch's own record and Retry. A service that answers is left alone, so the
    button can't leave the notch holding questions for a start that never comes."""
    app = _read("AppDelegate.swift")
    content = _body(app, "private func onboardingContent() -> NSView")
    assert "recordedStartFailure: { [weak self] in self?.model.serviceStartFailure }" in content
    retry = content[content.index("restartWisp: {"):content.index("closeForNow: {")]
    assert retry.index("self.backend.lastReachable != true") < retry.index("self.model.retryService()")
    gate = _body(app, "private var modelGateHolds: Bool")
    assert "FirstRunSetup.gatesUse(" in gate and "canAnswer: setupCanAnswer" in gate


def test_a_start_that_launches_nothing_says_why_in_a_fixed_sentence():
    """A port conflict, unreadable keys or missing files used to end Retry in
    silence. Each now posts the same notice a failed launch does, with a fixed
    sentence: no path from this Mac, nothing read from the Keychain."""
    manager = _read("BackendManager.swift")
    start = _body(manager, "private func startIfNeeded(freshRecovery: Bool) async")
    assert "guard !portConflict else { startBlocked(Self.portInUseReason); return }" in start
    # A start already under way, or a live child, is still left alone without a word.
    assert "guard !starting else { return }" in start
    assert "guard process == nil else { return }" in start
    assert start.count("startBlocked(Self.credentialsBlockedReason)") == 2
    assert "startBlocked(Self.missingFilesReason)" in start
    blocked = _body(manager, "private func startBlocked(_ reason: String)")
    assert "name: .wispBackendStartFailed" in blocked and '["reason": reason]' in blocked
    for name in ("portInUseReason", "credentialsBlockedReason", "missingFilesReason"):
        lines = manager[manager.index(f"nonisolated static let {name} = "):].split("\n")
        # The declaration's first line and its `+ "…"` continuation lines.
        value = lines[0].split("=", 1)[1] + "".join(takewhile(lambda line: line.strip().startswith("+ "), lines[1:]))
        assert value.count('"') >= 2, (name, value)
        assert "\\(" not in value and "NSHomeDirectory" not in value and "/" not in value, (name, value)
