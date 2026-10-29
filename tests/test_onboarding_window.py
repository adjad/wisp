"""Source checks for the onboarding window's promises that a pure Swift check
can't see: Wisp never asks macOS for a permission on its own, every Wisp window
can be minimized, a Dock click brings forward one window, and Getting to know
you lives in the window rather than in a notch offer. Reads source only; no
app, service or network."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"

# Every API that makes macOS show a privacy prompt. Screen Recording's request and
# Accessibility's prompt option count too: either one shows macOS's question.
PROMPTING = re.compile(
    r"requestFullAccessToEvents|requestFullAccessToReminders|requestWriteOnlyAccessToEvents"
    r"|requestAccess\(to:|requestAccess\(for:|requestAuthorization\("
    r"|CGRequestScreenCaptureAccess\(|AXIsProcessTrustedWithOptions\(|kAXTrustedCheckOptionPrompt"
)

# Permissions' click-time requests for Screen Recording and Accessibility.
EXTRA_REQUESTS = re.compile(r"(?<![\w.])(?<!func )(?:Permissions\.)?request(?:ScreenRecording|Accessibility)\(\)")


def _sources() -> dict[str, str]:
    return {p.relative_to(APP).as_posix(): p.read_text() for p in APP.rglob("*.swift")}


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


def test_prompts_only_follow_a_click():
    """A prompt may only come from the setup window's row button
    (FirstRunPermissions.request) or ContactsReader's guarded request, which
    the app starts with mayAsk false."""
    hits = {}
    for name, text in _sources().items():
        for match in PROMPTING.finditer(text):
            hits.setdefault(name, []).append(text.count("\n", 0, match.start()) + 1)
    assert set(hits) == {"FirstRunSetupView.swift", "ContactsReader.swift", "VoiceInput.swift",
                         "Permissions.swift", "PageReader.swift"}, hits
    # The microphone is asked for only by pressing the mic (or ⌥⇧Space): the one
    # prompt is inside start(), reached only through toggle(), which only the
    # mic button and the voice hotkey call (OverlayModel.toggleVoice).
    voice = _sources()["VoiceInput.swift"]
    assert len(PROMPTING.findall(_body(voice, "func start()"))) == len(PROMPTING.findall(voice)) == 1
    voice_input = _body(voice, "final class VoiceInput")
    assert len(re.findall(r"(?<![\w.])start\(\)", voice_input)) == 2 and "case .idle, .failed: start()" in voice
    overlay = _sources()["OverlayModel.swift"]
    assert overlay.count("voice.toggle()") == 1 and "voice.toggle()" in _body(overlay, "func toggleVoice()")
    setup = _sources()["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    assert len(PROMPTING.findall(request)) == len(PROMPTING.findall(setup)), \
        "every prompt in the setup window is inside the row's request()"
    # Automation is asked (ask: true) only from that same click: once for Mail
    # and Notes, once for Messages, and nowhere else in the app.
    assert setup.count("automationState(ask: true)") == 1 and "automationState(ask: true)" in request
    assert setup.count("messagesAutomationState(ask: true)") == 1 and "messagesAutomationState(ask: true)" in request
    for name, text in _sources().items():
        if name != "FirstRunSetupView.swift":
            assert "ask: true)" not in text or "AEDeterminePermissionToAutomateTarget" not in text, name


def test_screen_recording_and_accessibility_prompt_only_from_a_click():
    """Screen Recording and Accessibility show macOS's question only inside
    Permissions' request functions, once (then the Settings page), and those are
    called only from a click: the setup window's row, More Access…'s button, and
    the search panel's button. PageReader's own prompt is never called."""
    sources = _sources()
    perms = sources["Permissions.swift"]
    screen = _body(perms, "static func requestScreenRecording()")
    access = _body(perms, "static func requestAccessibility()")
    assert len(PROMPTING.findall(screen)) + len(PROMPTING.findall(access)) == len(PROMPTING.findall(perms)), \
        "every prompt in Permissions.swift is inside its two request functions"
    for body, key in ((screen, "screenRecordingPromptedKey"), (access, "accessibilityPromptedKey")):
        prompt = body[body.index("case .prompt:"):body.index("case .settings:")]
        assert PROMPTING.search(prompt) and f"defaults.set(true, forKey: {key})" in prompt
        assert not PROMPTING.search(body[:body.index("case .prompt:")] + body[body.index("case .settings:"):])
    reader = sources["PageReader.swift"]
    assert len(PROMPTING.findall(reader)) == len(PROMPTING.findall(_body(reader, "static func requestAccessibility()")))
    for name, text in sources.items():
        assert "PageReader.requestAccessibility(" not in text, name
    # Who may call Permissions' two requests (or request(_ extra), which wraps them).
    callers = {name: len(EXTRA_REQUESTS.findall(text)) for name, text in sources.items() if EXTRA_REQUESTS.search(text)}
    assert set(callers) == {"Permissions.swift", "FirstRunSetupView.swift", "SearchPanel.swift"}, callers
    assert len(EXTRA_REQUESTS.findall(_body(perms, "static func request(_ extra: Extra)"))) == callers["Permissions.swift"] == 2
    setup = sources["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    assert len(EXTRA_REQUESTS.findall(request)) == callers["FirstRunSetupView.swift"] == 2
    panel = sources["SearchPanel.swift"]
    assert callers["SearchPanel.swift"] == 1
    assert len(EXTRA_REQUESTS.findall(_body(panel, 'Button("Open Accessibility Settings…")'))) == 1
    wraps = {name for name, text in sources.items() if "Permissions.request(" in text}
    assert wraps == {"AppDelegate.swift"}, wraps
    more = _body(sources["AppDelegate.swift"], "private func grantMoreAccess()")
    assert "if alert.runModal() == .alertFirstButtonReturn, let next = missing.first { Permissions.request(next) }" in more
    # The setup window only reads them: CGPreflight and AXIsProcessTrusted never ask.
    refresh = _body(setup, "func refresh() async")
    assert "CGPreflightScreenCaptureAccess()" in refresh and "AXIsProcessTrusted()" in refresh
    assert not PROMPTING.search(refresh) and not EXTRA_REQUESTS.search(refresh)


def test_screen_recording_says_it_needs_a_reopen_and_marks_resume_first():
    setup = _sources()["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    screen = request[request.index("case .screenRecording:"):request.index("case .accessibility:")]
    assert screen.index("FirstRunSetup.expectRelaunch(") < screen.index("Permissions.requestScreenRecording()")
    detail = _body(setup, "static func screenRecordingDetail(_ state: State) -> String")
    assert detail.count("screenRecordingRelaunchNote") == 2 and "NSAlert" not in detail


def test_nothing_asks_at_launch():
    sources = _sources()
    joined = "\n".join(sources.values())
    for gone in ("Permissions.bootstrap", "requestFirstRun", "didRequestPermissions", "didRequestExtraPermissions"):
        assert gone not in joined, gone
    app = sources["AppDelegate.swift"]
    assert "ContactsReader(mayAsk: { false })" in app
    # Calendar and Reminders readers report instead of requesting.
    for reader in ("CalendarReader.swift", "RemindersWriter.swift"):
        text = sources[reader]
        assert "Access.passive(" in text and "requestAccessTracked" not in text, reader
    # Mail and Notes skip scripted reads until Automation has an answer, asking nothing.
    for reader in ("MailReader.swift", "NotesReader.swift"):
        text = sources[reader]
        helper = _body(text, "static func automationDecided(")
        assert "AEEventID(typeWildCard), false)" in helper, reader
        assert "Self.automationDecided(" in text, reader


def test_every_wisp_window_can_be_minimized():
    for name, text in _sources().items():
        if name in {"main.swift", "NotchQA.swift", "Chat/ChatQA.swift"}:
            continue  # QA harness windows, never shown to a person
        for match in re.finditer(r"styleMask:\s*(\[[^\]]*\]|Self\.onboardingStyle)", text):
            mask = match.group(1)
            if mask == "Self.onboardingStyle":
                mask = re.search(r"static let onboardingStyle: NSWindow.StyleMask = (\[[^\]]*\])", text).group(1)
            if ".titled" in mask:
                assert ".miniaturizable" in mask, f"{name}: {mask}"
    app = _sources()["AppDelegate.swift"]
    assert '"Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"' in app


def test_dock_click_brings_forward_one_window():
    app = _sources()["AppDelegate.swift"]
    body = _body(app, "func applicationShouldHandleReopen(")
    assert "WispReopen.choose(" in body
    # Returning true would let AppKit bring back a minimized window as well.
    assert re.search(r"return false\s*}\s*$", body) and "return true" not in body


def test_know_you_is_a_window_step_not_a_notch_offer():
    setup = _sources()["FirstRunSetupView.swift"]
    assert "KnowYouView(model: knowYou, inWindow: true)" in setup
    quiet = _body(setup, "static func quiet(_ m: Moment)")
    assert "m.aboutYouDecidedInWindow" in quiet
    app = _sources()["AppDelegate.swift"]
    assert "aboutYouDecidedInWindow: FirstRunSetup.aboutYouDecidedInWindow" in app
    # The key is set only by the step's own Done or Skip for now, never by showing it.
    assert app.count("FirstRunSetup.aboutYouDecidedKey") == 1
    assert setup.count("aboutYouDecided()") == 2
    # Finishing setup no longer opens Getting to know you in the notch.
    finish = _body(app, "private func finishFirstRun(")
    assert "startKnowYou" not in finish and "tellWispAboutYou" not in finish


def test_reopened_window_leaves_the_model_alone():
    """Opened again from Permissions… or a Brief link, the window must not
    switch the model or run its test by itself."""
    setup = _sources()["FirstRunSetupView.swift"]
    drive = _body(setup, "private func drive(_ state: FirstRunBrain)")
    guard = "guard FirstRunSetup.drivesModel(setupDone: setupDone) else { return }"
    assert guard in drive
    # Only the connect timer runs before the guard (so Try again appears after
    # setup is finished); starting the person's oMLX is gated on drivesModel too.
    before, after = drive.split(guard, 1)
    assert "setup.apply(" not in before and "setup.apply(" in after
    assert "setup.startExistingOmlxOnce()" not in after
    assert "if steps.startsOmlx { setup.startExistingOmlxOnce() }" in before
    gate = _body(setup, "static func whileConnecting(setupDone: Bool, connecting: Bool)")
    assert "connecting && FirstRunSetup.drivesModel(setupDone: setupDone)" in gate
    app = _sources()["AppDelegate.swift"]
    assert "setupDone: !FirstRunSetup.isUnfinished { defaults.bool(forKey: $0) }" in app


def test_fix_links_say_where_they_go():
    """Every Brief/Day fix link opens through openFix and is labelled by fixLabel."""
    for name in ("NotchBriefView.swift", "NotchDayView.swift", "ProactiveCards.swift"):
        text = _sources()[name]
        assert "Permissions.openFix(url)" in text and "Permissions.fixLabel" in text, name
        assert "fix.label)" not in text, name


def test_only_the_explicit_finish_marks_setup_done():
    """A quit (macOS's Quit & Reopen after Full Disk Access included) or an
    early close must never end setup, even when the model can already answer:
    the window has steps after the model."""
    app = _sources()["AppDelegate.swift"]
    setup = _sources()["FirstRunSetupView.swift"]
    assert "closeFinishes" not in app and "closeFinishes" not in setup
    assert app.count("forKey: FirstRunSetup.doneKey)") == 1
    closed = _body(app, "private func firstRunClosed()")
    assert "FirstRunSetup.ending(finishing: firstRunFinishing, terminating: appTerminating)" in closed
    done_branch = _body(closed, "if FirstRunSetup.marksDone(ending)")
    assert "defaults.set(true, forKey: FirstRunSetup.doneKey)" in done_branch
    assert "canAnswer" not in done_branch, "a ready model no longer ends setup"
    assert "guard ending == .closedEarly else { return }" in closed
    marks = _body(setup, "static func marksDone(_ ending: Ending)")
    assert marks.strip("{} \n") == "ending == .finished"
    # Done — open Wisp and the last step's prompts are the only callers of finish.
    assert setup.count("finish(nil)") == 1 and setup.count("finish(prompt)") == 1


def test_termination_is_noted_before_any_window_closes():
    app = _sources()["AppDelegate.swift"]
    should = _body(app, "func applicationShouldTerminate(")
    assert should.lstrip("{ \n").startswith("noteTerminating()")
    for signature in ("private func beginShutdown()", "private func terminateWithoutShutdown()"):
        assert _body(app, signature).lstrip("{ \n").startswith("noteTerminating()"), signature
    note = _body(app, "private func noteTerminating()")
    assert "appTerminating = true" in note
    assert "firstRunWindow != nil" in note and "forKey: FirstRunSetup.resumeKey" in note


def test_setup_resumes_at_its_step_after_a_quit():
    app = _sources()["AppDelegate.swift"]
    setup = _sources()["FirstRunSetupView.swift"]
    # Every step change is saved, and the window opens at the saved step.
    assert "stepChanged: { [weak self] step in" in app
    assert "UserDefaults.standard.set(step.rawValue, forKey: FirstRunSetup.stepKey)" in app
    # The last step lets the service compose the first Brief, only while setup is unfinished
    # and the model answers (a Brief made without it would stand as the day's).
    hook = app[app.index("stepChanged: { [weak self] step in"):]
    hook = hook[:hook.index("askKeyRegistered:")]
    assert "if step == .tour, FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) })," in hook
    assert "self?.setupModel.status?.canAnswer == true {" in hook
    assert "self?.requestFirstBrief()" in hook
    assert hook.index("FirstRunSetup.stepKey") < hook.index("requestFirstBrief")
    # The request un-claims the Brief when the service didn't take it, so it's asked again.
    request = _body(app, "private func requestFirstBrief()")
    assert "client.onboardingBriefReady() == false" in request
    assert "removeObject(forKey: FirstBrief.requestedKey)" in request
    assert "initialStep: OnboardingStep.restored(defaults.object(forKey: FirstRunSetup.stepKey) as? Int)" in app
    on_step = setup[setup.index(".onChange(of: step)"):]
    assert "stepChanged(next)" in _body(on_step, ".onChange(of: step)")
    # The step is cleared only when setup is finished.
    assert app.count("removeObject(forKey: FirstRunSetup.stepKey)") == 1
    # Launch: the resume mark is consumed, and only a reopen after an early close counts.
    launch = _body(app, "private func launch()")
    assert "FirstRunSetup.launchCountsTowardCap(flag)" in launch
    assert "defaults.removeObject(forKey: FirstRunSetup.resumeKey)" in launch
    assert "if countsTowardCap {" in launch
    show = _body(setup, "static func shouldShow(")
    assert show.lstrip("{ \n").startswith("guard !flag(doneKey) else { return false }")


def test_full_disk_access_says_it_may_quit_and_marks_resume_first():
    setup = _sources()["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    fda = request[request.index("case .fullDiskAccess:"):]
    assert fda.index("FirstRunSetup.expectRelaunch(") < fda.index("Permissions.openFullDiskAccessPane()")
    detail = _body(setup, "private var fullDiskAccessDetail: String")
    assert "FirstRunPermissions.fullDiskAccessRelaunchNote" in detail
    assert "NSAlert" not in detail


def test_messages_has_its_own_permission_row():
    """Sending a text scripts Messages (OutboundSender.sendMessage), so its
    Automation prompt is a row of its own, after Mail & Notes, asked only by a
    click, on a target list apart from Mail's."""
    setup = _sources()["FirstRunSetupView.swift"]
    step = _body(setup, "private var permissionsStep: some View")
    rows = re.findall(r'permissionRow\(\.(\w+), icon: "[^"]+", title: "([^"]+)"', step)
    assert rows == [("calendar", "Calendar"), ("reminders", "Reminders"), ("mail", "Mail & Notes"),
                    ("messages", "Messages"), ("fullDiskAccess", "Full Disk Access"),
                    ("notifications", "Notifications"), ("screenRecording", "Screen Recording"),
                    ("accessibility", "Accessibility")], rows
    assert ('detail: "Sends the texts you approve in Ask. Opens Messages for a moment to ask, then closes it.")' in step)
    mail = step[step.index("permissionRow(.mail"):step.index("permissionRow(.messages")]
    assert "Opens Mail and Notes for a moment to ask, then closes them." in mail
    assert 'nonisolated static let automationTargets = ["com.apple.mail", "com.apple.Notes"]' in setup
    assert 'nonisolated static let messagesAutomationTargets = ["com.apple.MobileSMS"]' in setup
    request = _body(setup, "func request(_ item: Item) async")
    messages = request[request.index("case .messages:"):request.index("case .fullDiskAccess:")]
    assert "Self.messagesAutomationTargets" in messages and "Self.automationTargets" not in messages
    assert "launchHidden(id)" in messages and "messagesAutomationState(ask: true)" in messages
    assert "Self.quitAfterAsking(launched)" in messages
    assert "Self.quitAfterAsking(launched)" in request[request.index("case .mail:"):request.index("case .messages:")]
    refresh = _body(setup, "func refresh() async")
    # The last definite answer is remembered in UserDefaults (never in memory only):
    # a closed Mail, Notes or Messages reads "can't say", not "permission removed".
    assert "next[.messages] = await Self.messagesAutomationState(ask: false)" in refresh
    assert "next[.mail] = await Self.automationState(ask: false)" in refresh
    assert "private var answered" not in setup and "keepingAnswer" not in setup
    assert "defaults.set(answer.rawValue, forKey: automationKey(item))" in setup
    # Only a target Wisp started itself is closed again, politely.
    launch = _body(setup, "private static func launchHidden(_ bundleID: String) async -> NSRunningApplication?")
    assert "runningApplications.contains" in launch and "config.addsToRecentItems = false" in launch
    quit = _body(setup, "private static func quitAfterAsking(_ launched: [NSRunningApplication])")
    assert "app.terminate()" in quit and "forceTerminate" not in setup
    # No System Events in setup: it stays asked at use. Accessibility is a row, but
    # asked through Permissions.requestAccessibility, never prompted here directly.
    assert "com.apple.systemevents" not in setup.lower()
    assert "AXIsProcessTrustedWithOptions" not in setup and "CGRequestScreenCaptureAccess" not in setup
    footnote = step[step.index('Text("Skipped one?'):]
    assert "microphone" in footnote and "asked for later" not in footnote
    # Screen Recording and Accessibility aren't needed to set up: they sit in an
    # optional disclosure after the core rows and don't count toward "still off".
    more = step[step.index("DisclosureGroup(isExpanded: $showMoreAccess)"):]
    assert step.index("permissionRow(.notifications") < step.index("DisclosureGroup(isExpanded: $showMoreAccess)")
    assert "permissionRow(.screenRecording" in more and "permissionRow(.accessibility" in more
    assert 'Text("More access (optional)")' in more
    for name in ("static func summary(", "static func allAnswered("):
        assert "Item.core." in _body(setup, name) and "Item.allCases" not in _body(setup, name), name


def test_relaunch_waits_for_a_quitting_copy_instead_of_yielding_to_it():
    """macOS's Quit & Reopen can start a new Wisp while the old one is still
    stopping its engine. Handing over to that dying copy left nothing running."""
    app = _sources()["AppDelegate.swift"]
    launch = _body(app, "func applicationDidFinishLaunching(")
    assert "SingleInstance.shouldYield(" in launch
    assert "FirstRunSetup.waitsForPeerToQuit(" in launch
    assert "defaults.bool(forKey: FirstRunSetup.resumeKey)" in launch
    assert "Self.shuttingDownPIDKey" in launch
    # Bounded, polled with an async sleep (never a blocking wait on the main thread).
    assert "FirstRunSetup.peerQuitWait" in launch and "Task.sleep" in launch
    for blocking in ("Thread.sleep", "usleep(", "waitUntilExit", "semaphore", "DispatchGroup"):
        assert blocking not in launch, blocking
    # Gone: launch. Still alive at the deadline: the old hand-over.
    assert "if peerApps.allSatisfy(\\.isTerminated) { self.launch() } else { self.yield(to: peerApps) }" in launch
    assert launch.rstrip("} \n").endswith("launch()")
    yield_body = _body(app, "private func yield(to peers: [NSRunningApplication])")
    assert ".activate(options: [])" in yield_body and "terminateWithoutShutdown()" in yield_body
    # The quitting copy says so before anything else, and only a launched copy does.
    note = _body(app, "private func noteTerminating()")
    assert note.index("guard launched else { return }") < note.index("forKey: Self.shuttingDownPIDKey")
    # While waiting, a Dock click or a quit touches nothing of Wisp's own.
    assert _body(app, "func applicationShouldHandleReopen(").lstrip("{ \n").startswith("guard launched else { return false }")
    assert "if !launched { shutdownFinished = true; return .terminateNow }" in _body(app, "func applicationShouldTerminate(")
    assert _body(app, "private func launch()").lstrip("{ \n").startswith("launched = true")


def test_an_early_close_clears_a_stale_resume_mark():
    app = _sources()["AppDelegate.swift"]
    closed = _body(app, "private func firstRunClosed()")
    tail = closed[closed.index("else if FirstRunSetup.resumesAtNextLaunch(ending)"):]
    early = _body(tail, "} else")
    assert "defaults.removeObject(forKey: FirstRunSetup.resumeKey)" in early


def test_a_pending_question_disables_only_its_own_row():
    """2026-10-27: "clicking Allow on Reminders does nothing". The Mail & Notes check waits
    until it is answered, and one shared lock disabled every other Allow button meanwhile."""
    setup = _sources()["FirstRunSetupView.swift"]
    assert "var asking: Set<Item>" in setup
    assert "guard !asking.contains(item)" in setup
    assert ".disabled(permissions.asking.contains(item))" in setup
    assert "asking != nil" not in setup and "asking == nil" not in setup


def test_open_at_login_is_opt_in_and_only_the_switch_changes_it():
    """Wisp never adds itself to Login Items: register()/unregister() live in
    LaunchAtLogin.set, and only the last step's switch calls that."""
    sources = _sources()
    setup = sources["FirstRunSetupView.swift"]
    set_body = _body(setup, "    func set(_ on: Bool) {")
    for call in ("SMAppService.mainApp.register()", "SMAppService.mainApp.unregister()"):
        assert setup.count(call) == 1 and call in set_body, call
        for name, text in sources.items():
            if name != "FirstRunSetupView.swift":
                assert call not in text, name
    assert setup.count("launchAtLogin.set(") == 1
    assert "set: { launchAtLogin.set($0) }" in _body(setup, "private var launchAtLoginRow: some View")
    # Read, never assumed: the switch's value is macOS's status.
    assert "get: { LaunchAtLogin.isOn(status) }" in setup
    assert "SMAppService.openSystemSettingsLoginItems()" in setup


def test_try_asking_waits_for_a_model_that_can_answer():
    """A prompt clicked before Wisp can answer used to finish setup and leave an
    unsendable question in Ask."""
    setup = _sources()["FirstRunSetupView.swift"]
    tour = _body(setup, "private var tourStep: some View")
    assert "let canAnswer = setup.status?.canAnswer == true" in tour
    assert tour.index("guard canAnswer else { return }") < tour.index("finish(prompt)")
    assert ".disabled(!canAnswer)" in tour and "These work once the model is ready." in tour


def test_last_step_listens_for_the_ask_shortcut():
    setup = _sources()["FirstRunSetupView.swift"]
    assert "NotificationCenter.default.publisher(for: .wispAskHotkeyPressed)" in setup
    assert "Self.shortcutTry(registered: askKeyRegistered, pressed: askKeyPressed)" in setup


def test_onboarding_copy_never_teaches_retired_features_or_jargon():
    """Smart Search (⌘⇧F) and Research are retired; the window speaks plainly."""
    for name in ("FirstRunSetupView.swift", "OnboardingViews.swift", "Onboarding.swift"):
        text = _sources()[name]
        literals = re.findall(r'"((?:[^"\\\n]|\\.)*)"', text)
        for literal in literals:
            assert "⌘ ⇧ F" not in literal and "⌘⇧F" not in literal, (name, literal)
            assert "Smart Search" not in literal and "Research" not in literal, (name, literal)
            low = literal.lower()
            assert "engine" not in low and "backend" not in low and "bundled" not in low, (name, literal)
            assert "service" not in low and "server" not in low, (name, literal)
            # oMLX only as the name of the person's own app, never as Wisp's machinery.
            if "omlx" in low:
                assert "your omlx" in low or "omlx app" in low, (name, literal)


def test_window_progress_and_return_follow_the_card():
    setup = _sources()["FirstRunSetupView.swift"]
    # The step bar tells a stop from a failure, as the card and VoiceOver do.
    progress = _body(setup, "private var compactProgress: String?")
    assert 'paused ? "Download stopped" : "Download failed"' in progress
    # A slow service and a stalled start give Return to the card's own button.
    assert "checkAgainButton(takesReturn: true)" in setup
    assert _body(setup, "private func checkAgainButton(takesReturn: Bool = false)").count(".keyboardShortcut(.defaultAction)") == 1
    # A window that opens on a model already ready still records it.
    assert "record(brain)" in _body(setup, ".onAppear {")


def test_download_paths_share_one_start_and_one_disk_warning():
    setup = _sources()["FirstRunSetupView.swift"]
    # Every Download, Retry, Resume and Download again goes through startDownload.
    assert setup.count("await setup.download(model)") == 1
    assert "await setup.download(model)" in _body(setup, "private func startDownload(_ model: String)")
    assert "OnboardingTimeline.record(\"download started\")" in _body(setup, "private func startDownload(_ model: String)")
    # The disk warning is drawn under step 1's card and on the last step, never only before the first Download.
    assert setup.count("diskWarningLine") == 3
    assert "brain.footer(stalled: stalled).fetches" in _body(setup, "private var diskWarning: String?")


def test_an_unanswered_automation_question_lets_its_row_go():
    """2026-10-27: an Automation question nobody answers left Mail & Notes on "Asking…"
    for good, since macOS waits for the answer with no time limit. After a short wait the
    row says where the question is and can ask again, and the first ask finishes
    (closing what it opened) whenever the person answers."""
    setup = _sources()["FirstRunSetupView.swift"]
    request = _body(setup, "func request(_ item: Item) async")
    for case, ask in (("case .mail:", "Self.automationState(ask: true)"),
                      ("case .messages:", "Self.messagesAutomationState(ask: true)")):
        end = "case .messages:" if case == "case .mail:" else "case .fullDiskAccess:"
        body = request[request.index(case):request.index(end)]
        order = ["let patience = letGoIfUnanswered(item, token: token)", ask, "patience.cancel()",
                 "waiting.remove(item)", "Self.quitAfterAsking(launched)"]
        at = [body.index(part) for part in order]
        assert at == sorted(at), case
    # Only the click that still owns the row clears it: an ask that was let go
    # returns later and must not end a newer click's "Asking…".
    assert "defer { if askTokens[item] == token { asking.remove(item); askTokens[item] = nil } }" in request
    let_go = _body(setup, "private func letGoIfUnanswered(_ item: Item, token: UUID) -> Task<Void, Never>")
    assert "self.askTokens[item] == token" in let_go and "self.waiting.insert(item)" in let_go
    refresh = _body(setup, "func refresh() async")
    assert "waiting = waiting.filter { !Self.isDecided(next[$0] ?? .checking) }" in refresh
    row = _body(setup, "private func permissionRow(")
    assert "permissions.waiting.contains(item)" in row and "FirstRunPermissions.automationWaitingNote" in row


def test_readers_post_again_when_the_service_comes_up():
    """2026-10-29 fresh-install test: Reminders, allowed in setup, was missing after
    macOS's Quit & Reopen for Full Disk Access. The service keeps each source's last
    sync in memory only, and the readers' launch-time posts went out before the cold
    service listened; the reachability handler refreshed Today without re-posting."""
    app = _sources()["AppDelegate.swift"]
    handler = app[app.index("backend.onReachabilityChange = {"):]
    up = _body(handler, "if up")
    assert "self.resyncAllReaders()" in up
    assert up.index("self.resyncAllReaders()") < up.index("await self.todayModel.refresh()")
    # And Today looks again once the posts have landed.
    assert up.count("await self.todayModel.refresh()") == 2
    assert "NativeSyncPace.reachableRefreshDelay" in up
    resync = _body(app, "private func resyncAllReaders()")
    assert "Self.resyncedWhenReachable" in resync and "resyncReaders(after: item)" in resync
    # Reading only: no reader asks macOS for anything (test_prompts_only_follow_a_click).
    assert not PROMPTING.search(resync)


def test_notch_and_windows_wait_for_the_model_during_setup():
    """dev8: until the model has arrived, a click on the notch, the menu bar icon,
    a shortcut or a Wisp window route brings setup forward and says why."""
    app = _sources()["AppDelegate.swift"]
    hold = _body(app, "private func holdUntilModelReady() -> Bool")
    assert "guard modelGateHolds else { return false }" in hold
    assert "bringSetupForward()" in hold and ".wispHeldUntilModelReady" in hold
    gate = _body(app, "private var modelGateHolds: Bool")
    assert "FirstRunSetup.gatesUse(" in gate and "canAnswer: setupCanAnswer" in gate
    expand = _body(app, "private func expand()")
    assert expand.index("if holdUntilModelReady() { return }") < expand.index("requestPresentation(.chat)")
    assert "if holdUntilModelReady() { return }" in _body(app, "private func askByVoice()")
    assert "!modelGateHolds" in _body(app, "private var canHoverOpen: Bool")
    route = _body(app, "private func open(_ route: WispRoute)")
    assert "case .settings, .setup: break" in route
    assert route.index("if holdUntilModelReady() { return }") < route.index("wispWindow.show")
    setup = _sources()["FirstRunSetupView.swift"]
    footer = _body(setup, "private var footer: some View")
    tour = footer[footer.index("case .tour:"):]
    assert tour.index("if setup.status?.canAnswer == true {") < tour.index('Button("Done — open Wisp")')
    assert 'Button("Close for now")' in tour and "closeForNow()" in tour


def test_open_at_login_sits_with_the_permissions():
    """dev8: the switch moved from How to use to the Permissions step (and Settings › Privacy)."""
    sources = _sources()
    setup = sources["FirstRunSetupView.swift"]
    assert "launchAtLoginRow" in _body(setup, "private var permissionsStep: some View")
    assert "launchAtLoginRow" not in _body(setup, "private var tourStep: some View")
    settings = sources["SettingsView.swift"]
    assert "launchAtLogin.set($0)" in settings and "LaunchAtLogin.isOn(launchAtLogin.status)" in settings


def test_how_to_use_starts_with_the_download():
    setup = _sources()["FirstRunSetupView.swift"]
    tour = _body(setup, "private var tourStep: some View")
    assert tour.lstrip("{ \n").split("\n", 1)[1].lstrip().startswith("// The download first") or \
        tour.index("tourDownload") < tour.index("OnboardingHeading(")
    assert tour.index("tourDownload") < tour.index("Self.helpsYourDay")
    download = _body(setup, "@ViewBuilder private var tourDownload: some View")
    assert "FirstRunBrain.progressLine(" in download and "speedText" in download


def test_move_to_applications_only_on_a_click():
    """dev8: offered at the top of launch; nothing moves or is removed without the
    person's click, an existing Wisp goes to the Trash, and no password is asked."""
    sources = _sources()
    move = sources["MoveToApplications.swift"]
    offer = _body(move, "static func offerIfNeeded(")
    assert offer.index("ask.runModal()") < offer.index("try move(from:")
    assert "guard answer == .alertFirstButtonReturn" in offer
    assert offer.index("replace.runModal()") < offer.index("try move(from:")
    body = _body(move, "static func move(from bundle: URL, original: URL?, target: URL = URL(fileURLWithPath: destination)) throws")
    # The person's things only ever go to the Trash; removeItem is for Wisp's own staging copy.
    assert "trashItem(at: target" in body and "trashItem(at: original" in body
    assert body.count("removeItem(") == 1 and "removeItem(at: staging)" in body
    # A copy that stops part-way is cleaned up too (Release Auditor, 6c3d68d4).
    assert body.index("do {") < body.index("try fm.copyItem(at: bundle, to: staging)")
    assert "MoveFailure(underlying: error, oldWispTrashed: oldTrashed)" in body
    assert body.index("moveItem(at: staging, to: target)") < body.index("trashItem(at: original")
    for forbidden in ("with administrator privileges", "AuthorizationExecuteWithPrivileges", "sudo "):
        assert forbidden not in move, forbidden
    app = sources["AppDelegate.swift"]
    launch = _body(app, "private func launch()")
    assert launch.index("MoveToApplications.offerIfNeeded()") < launch.index("WispReset.readMarker()")
    # Only a real download: a quarantine mark, never a preview build (Release Auditor, 6c3d68d4).
    offer_call = offer[offer.index("guard offers("):offer.index("else { return .stayed }")]
    assert "quarantined: isQuarantined(bundle)" in offer_call and '"WispPreviewBuild"' in offer_call
