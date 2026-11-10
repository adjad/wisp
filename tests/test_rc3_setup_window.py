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


def test_a_failed_test_question_is_never_called_ready():
    """l06-first-run-e2e#2: "ready" only means the model is listed. Once setup's one
    test question has failed, the Model card, How to use, the step bar and the
    footer all say so. Done stays: it is gated on being able to answer, as before."""
    setup = _read("FirstRunSetupView.swift")
    failed = _body(setup, "private var setupTestFailed: Bool")
    assert "FirstRunBrain.testFailed(result: setup.testResult, passed: setup.testPassed, testing: setup.testing)" in failed
    # No ready copy is written straight into a view any more: each goes through the rule.
    assert '"\\(label) is ready"' not in setup.replace(
        _body(setup, "static func readyTitle(label: String, testFailed: Bool) -> String"), "")
    assert setup.count('"Model ready"') == 1 and setup.count('"Wisp can answer now. Choose Done to open it."') == 1
    card = _body(setup, "@ViewBuilder private var modelCard: some View")
    ready = card[card.index("case .ready(let label):"):]
    assert "FirstRunBrain.readyTitle(label: label, testFailed: setupTestFailed)" in ready
    assert 'Button("Try again") { Task { await setup.test() } }' in ready
    tour = _body(setup, "@ViewBuilder private var tourDownload: some View")
    assert "FirstRunBrain.readyTitle(label: label, testFailed: setupTestFailed)" in tour
    assert "FirstRunBrain.readyTourDetail(testFailed: setupTestFailed)" in tour
    assert 'if !setupTestFailed { OnboardingPill(text: "Ready") }' in tour
    assert "FirstRunBrain.readyShort(testFailed: setupTestFailed)" in _body(setup, "private var compactProgress: String?")
    assert "FirstRunBrain.readyShort(testFailed: setupTestFailed)" in _body(
        setup, "@ViewBuilder private var tourModelStatus: some View")
    assert "case .brain: return brain.isReady && !setupTestFailed" in _body(setup, "private func isDone(_ s: Step) -> Bool")
    # Done is still offered on canAnswer alone, and the ready notice is not held for the test.
    footer = _body(setup, "private var footer: some View")
    done = footer[footer.index("case .tour:"):]
    assert done.index("if setup.status?.canAnswer == true {") < done.index('Button("Done — open Wisp")')
    assert "setupTestFailed" not in done and "testPassed" not in _read("AppDelegate.swift")


def test_a_finished_setup_gets_no_setup_cue_while_the_model_warms_up():
    """a03-setup-onboarding#1: at every launch the service answers before the model
    does. For a Mac that finished setup that is warm-up, not "isn't set up": the
    readiness watch times the not-ready answers and passes that, and whether setup
    is finished, to the pure rules. The ready path is unchanged."""
    app = _read("AppDelegate.swift")
    watch = _body(app, "private func watchSetupReadiness()")
    assert "var notReadySince: Date?" in watch
    assert "if canAnswer { notReadySince = nil } else if notReadySince == nil { notReadySince = Date() }" in watch
    assert "let setupFinished = !FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) })" in watch
    cue = watch[watch.index("FirstRunSetup.setupCue("):]
    cue = cue[:cue.index("if cue != self.model.setupCard")]
    assert "canAnswer: canAnswer" in cue and "setupFinished: setupFinished, notReadyFor: notReadyFor" in cue
    assert "self.setupCanAnswer = FirstRunSetup.knownCanAnswer(canAnswer, setupFinished: setupFinished," in watch
    assert "self.setupCanAnswer = canAnswer" not in watch
    # Ready still clears the cue, settles the first Brief and tells the person, in that order.
    ready = watch[watch.index("guard canAnswer else { continue }"):]
    assert ready.index("self.settleFirstBrief(canAnswer: true)") < ready.index("self.tellReady()")
    setup = _read("FirstRunSetupView.swift")
    rule = _body(setup, "static func setupCue(_ current: NotchSetupCard, canAnswer: Bool, firstRunWindowUp: Bool,")
    assert rule.index("if canAnswer { return current.afterReadiness(true) }") < rule.index("warmingUp(")
    assert "guard !firstRunWindowUp, current == .hidden else { return current }" in rule
    assert "setupFinished && notReadyFor < warmUpGrace" in _body(
        setup, "static func warmingUp(setupFinished: Bool, notReadyFor: TimeInterval) -> Bool")

