"""Source checks for the onboarding window's promises that a pure Swift check
can't see: Wisp never asks macOS for a permission on its own, every Wisp window
can be minimized, a Dock click brings forward one window, and Getting to know
you lives in the window rather than in a notch offer. Reads source only; no
app, service or network."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"

# Every API that makes macOS show a privacy prompt.
PROMPTING = re.compile(
    r"requestFullAccessToEvents|requestFullAccessToReminders|requestWriteOnlyAccessToEvents"
    r"|requestAccess\(to:|requestAccess\(for:|requestAuthorization\("
)


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
    assert set(hits) == {"FirstRunSetupView.swift", "ContactsReader.swift", "VoiceInput.swift"}, hits
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
    assert "client.onboardingBriefReady()" in hook
    assert hook.index("FirstRunSetup.stepKey") < hook.index("onboardingBriefReady")
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
                    ("notifications", "Notifications")], rows
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
    # No System Events or Accessibility in setup: those stay asked at use.
    assert "com.apple.systemevents" not in setup.lower()
    assert "AXIsProcessTrustedWithOptions" not in setup


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
