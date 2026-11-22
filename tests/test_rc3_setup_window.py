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


def test_a_download_frozen_before_the_window_opened_is_still_timed():
    """a03-setup-onboarding#2: the No progress warning needs a first reading to time
    from. A download that was already frozen when the window opened never changes
    the state, so the window takes that reading as it appears, like the notch's row."""
    setup = _read("FirstRunSetupView.swift")
    helper = _body(setup, "private func watchProgress(_ state: FirstRunBrain)")
    assert "progressWatch.observe(doneGb: done, at: Date())" in helper and "progressWatch.reset()" in helper
    view = setup[setup.index("struct FirstRunSetupView: View"):]
    assert "watchProgress(brain)" in _body(view, ".onAppear {")
    assert "watchProgress(next)" in _body(view, ".onChange(of: brain) { _, next in")
    # One place observes, so the two paths can't drift apart.
    assert setup.count("progressWatch.observe(") == 1 and setup.count("progressWatch.reset()") == 1
    # Both cards still read the same watch.
    assert setup.count("progressWatch.stalled(at: context.date)") == 2
    notch = _read("NotchSetupDownload.swift")
    assert ".onAppear { observe(brain) }" in notch



def test_my_installed_omlx_is_greyed_out_when_there_is_none_to_run():
    """a03-setup-onboarding#4: the service saves external-omlx whether or not an
    oMLX exists, and after the restart Wisp has no engine. The menu item is offered
    only with an oMLX app, an oMLX already answering, or to someone already on it."""
    view = _read("EngineSettingsView.swift")
    rule = _body(view, "nonisolated static func externalChoiceOffered(installed: Bool, externalRunning: Bool, "
                       "onExternal: Bool) -> Bool")
    assert "installed || externalRunning || onExternal" in rule
    # The other engine's own probe, never `running` (the built-in engine's state while on it).
    helper = _body(view, "private func externalOffered(_ engine: EngineSettingsResponse) -> Bool")
    assert "installed: engine.externalOmlxInstalled" in helper
    assert "externalRunning: engine.externalEngineRunning" in helper
    assert "onExternal: engine.mode == .externalOmlx" in helper
    body = _body(view[view.index("struct EngineSettingsView: View"):], "var body: some View")
    item = body[body.index("Button(Self.externalChoiceTitle(offered: externalOffered(engine)))"):body.index("Divider()")]
    assert "selectedMode.wrappedValue = .externalOmlx" in item and ".disabled(!externalOffered(engine))" in item
    assert '"My installed oMLX (not found)"' in _body(
        view, "nonisolated static func externalChoiceTitle(offered: Bool) -> String")
    # A stale menu can't save it either, and the built-in engine's own gate is as it was.
    save = _body(view, "func setMode(_ mode: EngineMode) async")
    assert "mode != .externalOmlx || EngineSettingsView.externalChoiceOffered(" in save
    assert "mode != .bundled || engine.bundledAvailable" in save
    assert ".disabled(!engine.bundledAvailable)" in body


def test_the_voice_card_and_ready_notice_teach_only_keys_wisp_holds():
    """a03-setup-onboarding#5: the Keyboard card already left out a shortcut another
    app owns, while the Voice card above it and "Wisp is ready" still said to press
    it. Both now follow the same registered flags; the wording with the key is as it was."""
    setup = _read("FirstRunSetupView.swift")
    tour = _body(setup, "private var tourStep: some View")
    cards = tour[tour.index("ForEach(Self.helpsYourDay"):tour.index('OnboardingHeading(title: "Wisp lives at the top')]
    assert "Self.helpLine(title: item.title, line: item.line," in cards
    assert "voiceKeyRegistered: voiceKeyRegistered)" in cards and "detail: item.line" not in cards
    # The shortcut is written in one place, behind the flag.
    assert setup.count("Space and just talk") == 1
    voice = _body(setup, "nonisolated static func voiceLine(registered: Bool) -> String")
    assert "Space and just talk" in voice and "Use the mic button in Ask to talk." in voice
    assert "title == voiceTitle ? voiceLine(registered: voiceKeyRegistered) : line" in _body(
        setup, "nonisolated static func helpLine(title: String, line: String, voiceKeyRegistered: Bool) -> String")

    notice = _body(_read("OnboardingFollowUps.swift"), "enum ReadyNotice")
    assert "static let body" not in notice and "static let notchLine" not in notice
    for name in ("body", "notchLine"):
        said = _body(notice, f"static func {name}(askKeyRegistered: Bool) -> String")
        with_key, without_key = said.split("\n")[1:3]
        assert "askKeyRegistered ?" in with_key and "⌥ Space" in with_key, (name, with_key)
        assert without_key.strip().startswith(":") and "⌥" not in without_key and "top of" in without_key, (name, without_key)

    app = _read("AppDelegate.swift")
    tell = _body(app, "private func tellReady()")
    assert tell.index("let askKey = hotKey?.registered ?? false") < tell.index("Task { @MainActor [weak self] in")
    assert "body: ReadyNotice.body(askKeyRegistered: askKey)" in tell
    assert "ReadyNotice.notchLine(askKeyRegistered: askKey)" in tell
    # Opening the panel still shortens whichever wording is up, and re-shows that same one.
    seen = _body(app, "private func readyLineSeen()")
    assert "guard let line = model.transientNotice, ReadyNotice.isNotchLine(line) else { return }" in seen
    assert "model.showTransientNotice(line, duration: ReadyNotice.seenFor, info: true)" in seen


def test_calendar_set_to_add_events_only_is_not_called_denied():
    """l04-known-issues#5: with Add Events Only, Wisp is already listed and on in
    System Settings > Calendars, so "Denied" and "switch Wisp on" pointed at a switch
    that isn't off. The row stays not allowed (Wisp can't read events) and its button
    still opens System Settings; only its words change, and only for Calendar."""
    setup = _read("FirstRunSetupView.swift")
    live = _body(setup, "nonisolated static func calendarAddOnly() -> Bool")
    assert "EKEventStore.authorizationStatus(for: .event) == .writeOnly" in live
    assert ".reminder" not in live and "request" not in live      # Calendar only; reads, never asks
    rule = _body(setup, "nonisolated static func isAddOnly(_ item: Item, _ state: State, calendarAddOnly: Bool?) -> Bool")
    assert "item == .calendar && state == .notAllowed && (calendarAddOnly ?? Self.calendarAddOnly())" in rule

    label = _body(setup, "static func label(_ item: Item, _ state: State, calendarAddOnly: Bool? = nil) -> String")
    denied = label[label.index("case .notAllowed:"):label.index("case .notOnThisMac:")]
    assert denied.index('if isAddOnly(item, state, calendarAddOnly: calendarAddOnly) { return "Add only" }') \
        < denied.index('"Denied"')
    detail = _body(setup, "static func eventKitDetail(_ item: Item, base: String, state: State, unanswered: Bool,")
    add_only = detail[detail.index("case .notAllowed where isAddOnly(item, state, calendarAddOnly: calendarAddOnly):"):]
    add_only, off = add_only.split("case .notAllowed:", 1)
    assert "choose Full Access" in add_only and "Options next to Wisp" in add_only and "switch Wisp on" not in add_only
    assert "switch Wisp on under" in off                          # a Calendar that is really off reads as before

    page = _read("NotchPermissionPage.swift")
    nxt = _body(page, "static func next(_ item: Item, _ state: State, waiting: Bool = false, reset: Bool = false,")
    off = nxt[nxt.index("case .notAllowed:"):]
    assert off.index("if FirstRunPermissions.isAddOnly(item, state, calendarAddOnly: calendarAddOnly) "
                     "{ return calendarAddOnlyNote }") < off.index("switch on Wisp")
    start = page.index("static let calendarAddOnlyNote = ")
    note = page[start:page.index("\n\n", start)]
    assert "only add events" in note and "Options next to Wisp" in note and "Full Access" in note

    # Still not allowed, and still System Settings: no state, button or request changed.
    state = _body(setup, "private static func eventKitState(_ entity: EKEntityType) -> State")
    assert "case .fullAccess: return .allowed" in state and "default: return .notAllowed" in state
    assert "writeOnly" not in state
    click = _body(setup, "static func eventKitClick(_ status: EKAuthorizationStatus) -> EventKitClick")
    assert "default: return .openSettings" in click
    assert "case .notAllowed: return .openSettings" in _body(
        page, "static func action(_ item: Item, _ state: State, waiting: Bool = false, contactsWait: Bool = false) -> Action?")
