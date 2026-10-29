import EventKit
import Foundation

// Pure checks for the first-run window: when it shows, and what step 1 says for
// every engine, model and download state. No window, service or engine.
@main
struct FirstRunSetupChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static let gemma = "gemma-4-E4B-it-oQ4e-mtp"
    /// What the service really sends (setup/guide.py: label · quant).
    static let realLabel = "Gemma 4 E4B · 4-bit (oQ4e) + MTP"

    static func status(ready: Bool = false, bundled: Bool = true, running: Bool = true,
                       available: Bool = true, installed: [String] = [], roles: String = "Ling-3.0-tiny-oQ4e",
                       fits: Bool? = nil, download: Bool = true, warning: String? = nil,
                       modelAction: String? = nil, remoteOnly: Bool = false,
                       roleEndpoint: String = "local") -> SetupStatus {
        let installedJSON = installed.map {
            #"{"id":"\#($0)","label":"\#($0 == gemma ? realLabel : $0)","size_gb":5.5,"fit":"recommended","tested":true,"note":""}"#
        }.joined(separator: ",")
        let json = """
        {"ready":\(ready),"remote_only":\(remoteOnly),
         "hardware":{"chip":"Apple M5 Pro","ram_gb":24,"tier":"comfortable"},
         "omlx":{"installed":true,"running":\(running),"model_dir":null,"min_macos":26,"url":"","admin_url":"","bundled":\(bundled)},
         "bundled_engine":{"available":\(available),"minimum_macos":"26.0","unavailable_reason":\(available ? "null" : "\"Wisp's engine needs macOS 26.\"")},
         "models":{"source":"engine","installed":[\(installedJSON)],"recommended":"\(gemma)","use":null,
                   "warning":\(warning.map { "\"\($0)\"" } ?? "null"),
                   \(fits.map { "\"fits\":\($0)," } ?? "")
                   "recommended_label":"Gemma 4 E4B · 4-bit (oQ4e) + MTP",
                   "download":\(download ? #"{"model":"\#(gemma)","label":"\#(realLabel)","size_gb":5.5,"repo":"Jundot/\#(gemma)","url":"","command":"","install_hf":""}"# : "null"),
                   "embedding_download":null},
         "roles":[{"role":"general","label":"General","model":"\(roles)","endpoint":"\(roleEndpoint)"}],
         "engines":[],
         "checks":[\(modelAction.map { #"{"id":"model","state":"todo","title":"","detail":"\#(warning ?? "")","action":"\#($0)","url":null}"# } ?? "")],
         "disclosure":"","configuration_token":null}
        """
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(SetupStatus.self, from: Data(json.utf8))
    }

    static func task(_ state: String, done: Double = 2.5e9, total: Double = 5.5e9, bps: Double = 10e6) -> SetupGuideModel.DownloadTask {
        let json = #"{"model":"\#(gemma)","state":"\#(state)","progress":45,"downloaded_bytes":\#(done),"total_bytes":\#(total),"bytes_per_second":\#(bps),"message":null}"#
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try! decoder.decode(SetupGuideModel.DownloadTask.self, from: Data(json.utf8))
    }

    static func footerOf(_ s: SetupStatus, _ tasks: [SetupGuideModel.DownloadTask], stalled: Bool = false) -> FirstRunBrain.Footer {
        FirstRunBrain.from(status: s, downloads: tasks).footer(stalled: stalled)
    }

    static func main() {
        // Who sees the window: only a Mac that has never run Wisp.
        check(FirstRunSetup.shouldShow { _ in false }, "Fresh Mac shows it")
        for key in [FirstRunSetup.doneKey, FirstRunSetup.setupGuideShownKey, FirstRunSetup.notchTipShownKey] {
            check(!FirstRunSetup.shouldShow { $0 == key }, "\(key) means an existing user")
        }
        var set: [String] = []
        FirstRunSetup.takeOverLaunchPrompts { set.append($0) }
        check(Set(set) == [FirstRunSetup.startedKey, FirstRunSetup.setupGuideShownKey, FirstRunSetup.notchTipShownKey],
              "The old launch tip and card are taken over, and the window is marked started")
        // The notch's offer of Getting to know you stops only once the window's
        // About you was finished or skipped; starting the window doesn't count.
        check(!FirstRunSetup.aboutYouDecidedInWindow { set.contains($0) },
              "Started the window, never reached About you: the notch may still offer it")
        check(FirstRunSetup.aboutYouDecidedInWindow { $0 == FirstRunSetup.aboutYouDecidedKey },
              "Finished or skipped About you: never in the notch")
        // Opened again after setup (Permissions…, a Brief link): hands off the model.
        check(FirstRunSetup.drivesModel(setupDone: false) && !FirstRunSetup.drivesModel(setupDone: true),
              "Only first-run setup switches the model or tests it by itself")
        // The window's setupDone is "not unfinished" (AppDelegate.onboardingContent).
        func drives(_ keys: Set<String>) -> Bool {
            FirstRunSetup.drivesModel(setupDone: !FirstRunSetup.isUnfinished { keys.contains($0) })
        }
        check(!drives([]), "Upgraded from a build without the window (neither key): Permissions… leaves the model alone")
        check(drives([FirstRunSetup.startedKey]), "Fresh install or mid-setup (started, not done): drives the model")
        check(!drives([FirstRunSetup.startedKey, FirstRunSetup.doneKey]), "Finished setup: leaves the model alone")
        // A fresh install marks the window started before it opens, so it drives from the first frame.
        var fresh: Set<String> = []
        FirstRunSetup.takeOverLaunchPrompts { fresh.insert($0) }
        check(drives(fresh), "Fresh install: the launch marks it started, so the window drives the model")
        // Quitting part-way: started and the taken-over keys are set, done is not.
        check(FirstRunSetup.shouldShow { set.contains($0) }, "Quit before finishing: the window opens again")
        check(!FirstRunSetup.shouldShow { set.contains($0) || $0 == FirstRunSetup.doneKey },
              "Closed or finished: never again")
        // Closed early: it opens by itself a few times, then Continue setup and the
        // Dock carry it.
        for reopens in 0..<FirstRunSetup.maxAutomaticReopens {
            check(FirstRunSetup.shouldShow({ set.contains($0) }, reopens: reopens), "Reopen \(reopens + 1) opens it")
        }
        check(!FirstRunSetup.shouldShow({ set.contains($0) }, reopens: FirstRunSetup.maxAutomaticReopens),
              "Past the cap it stops opening at launch")
        check(FirstRunSetup.shouldShow({ _ in false }, reopens: 99), "The cap only counts reopens of a started setup")
        check(FirstRunSetup.isUnfinished { set.contains($0) }, "Started, not done: Continue setup opens the window")
        check(!FirstRunSetup.isUnfinished { set.contains($0) || $0 == FirstRunSetup.doneKey }
              && !FirstRunSetup.isUnfinished { _ in false }, "Done, or never started: the notch card instead")
        // Only the explicit finish ends setup: never a quit, never an early close,
        // whether or not the model is ready (it plays no part any more).
        check(FirstRunSetup.ending(finishing: true, terminating: false) == .finished, "Done — open Wisp finishes it")
        check(FirstRunSetup.ending(finishing: true, terminating: true) == .finished,
              "Done at the same moment as a quit: the explicit finish wins")
        check(FirstRunSetup.ending(finishing: false, terminating: true) == .appQuit,
              "A close while Wisp is ending (Quit & Reopen after Full Disk Access) is the quit")
        check(FirstRunSetup.ending(finishing: false, terminating: false) == .closedEarly, "The red button or ⌘W")
        check(FirstRunSetup.marksDone(.finished), "Finishing marks setup done")
        check(!FirstRunSetup.marksDone(.appQuit), "A quit never marks setup done")
        check(!FirstRunSetup.marksDone(.closedEarly), "An early close never marks setup done, even with Wisp able to answer")
        check(FirstRunSetup.resumesAtNextLaunch(.appQuit) && !FirstRunSetup.resumesAtNextLaunch(.closedEarly)
              && !FirstRunSetup.resumesAtNextLaunch(.finished), "Only a quit under the window resumes uncounted")

        // The relaunch after macOS's Quit & Reopen (or any quit with the window up).
        let quitMidSetup = set + [FirstRunSetup.resumeKey]
        check(FirstRunSetup.shouldShow({ quitMidSetup.contains($0) }, reopens: 0), "Quit with the window up: it comes back")
        check(FirstRunSetup.shouldShow({ quitMidSetup.contains($0) }, reopens: FirstRunSetup.maxAutomaticReopens + 5),
              "Wisp ending under the window comes back even past the cap")
        check(!FirstRunSetup.launchCountsTowardCap { quitMidSetup.contains($0) }, "and never uses up a reopen")
        check(FirstRunSetup.launchCountsTowardCap { set.contains($0) }, "Reopening after an early close counts")
        check(!FirstRunSetup.launchCountsTowardCap { _ in false }, "A fresh Mac's first window isn't a reopen")
        check(!FirstRunSetup.shouldShow { quitMidSetup.contains($0) || $0 == FirstRunSetup.doneKey },
              "Finished users are never pulled back in, even with a leftover resume mark")
        check(!FirstRunSetup.shouldShow { $0 == FirstRunSetup.resumeKey || $0 == FirstRunSetup.notchTipShownKey },
              "A resume mark without a started window (an upgrader) shows nothing")
        // A copy macOS relaunches while the old one is still quitting waits for it
        // instead of handing over to it; a live copy still gets the hand-over.
        check(!FirstRunSetup.waitsForPeerToQuit(resumePending: false, peerShuttingDown: false),
              "A live copy, nothing pending: hand over at once, as before")
        check(FirstRunSetup.waitsForPeerToQuit(resumePending: false, peerShuttingDown: true),
              "The running copy said it is shutting down: wait, then launch")
        check(FirstRunSetup.waitsForPeerToQuit(resumePending: true, peerShuttingDown: false),
              "Setup expects a relaunch (Quit & Reopen after Full Disk Access): wait, then launch")
        check(FirstRunSetup.peerQuitWait > 20 && FirstRunSetup.peerQuitWait <= 30,
              "The wait outlasts the old copy's 20 s shutdown bound, and stays bounded")
        // Before System Settings opens for Full Disk Access.
        var marked: [String] = []
        FirstRunSetup.expectRelaunch({ set.contains($0) }, { marked.append($0) })
        check(marked == [FirstRunSetup.resumeKey], "Mid-setup: opening Full Disk Access marks setup to come back")
        marked = []
        FirstRunSetup.expectRelaunch({ set.contains($0) || $0 == FirstRunSetup.doneKey }, { marked.append($0) })
        FirstRunSetup.expectRelaunch({ _ in false }, { marked.append($0) })
        check(marked.isEmpty, "Finished, or an upgrader opening Permissions…: nothing marked")
        check(FirstRunPermissions.fullDiskAccessRelaunchNote.contains("quit and reopen")
              && FirstRunPermissions.fullDiskAccessRelaunchNote.contains("picks up right here"),
              "The Full Disk Access row says macOS may quit and reopen Wisp, and setup picks up here")
        check(FirstRunSetup.canAnswer(ready: false, remoteOnly: true), "Remote-only (Cloud Everything) counts as able to answer")
        check(FirstRunSetup.canAnswer(ready: true, remoteOnly: false) && !FirstRunSetup.canAnswer(ready: false, remoteOnly: false),
              "Otherwise only a ready local model can answer")
        // No Wisp notification while the setup window is on screen.
        check(!FirstRunSetup.notifiesReady(promised: true, windowUp: true, windowMinimized: false),
              "Window up, even behind System Settings or the browser: no notification")
        check(FirstRunSetup.notifiesReady(promised: true, windowUp: true, windowMinimized: true),
              "Window minimized: notify")
        check(FirstRunSetup.notifiesReady(promised: true, windowUp: false, windowMinimized: false),
              "Window closed or finished: notify")
        check(!FirstRunSetup.notifiesReady(promised: false, windowUp: false, windowMinimized: false),
              "Never promised: never")

        // Every launch: Wisp can't answer and no window is up → the quiet cue, never the full card.
        check(FirstRunSetup.setupCue(.hidden, canAnswer: false, firstRunWindowUp: false) == .later, "Not set up: the ! cue")
        check(FirstRunSetup.setupCue(.hidden, canAnswer: false, firstRunWindowUp: true) == .hidden, "Not over the first-run window")
        check(FirstRunSetup.setupCue(.open, canAnswer: false, firstRunWindowUp: false) == .open, "An open card is left alone")
        check(FirstRunSetup.setupCue(.later, canAnswer: true, firstRunWindowUp: false) == .hidden, "Ready: the cue goes")
        check(FirstRunSetup.setupCue(.open, canAnswer: true, firstRunWindowUp: false) == .open, "Ready: an open card waits for Done")
        check(FirstRunSetup.setupCue(.hidden, canAnswer: true, firstRunWindowUp: false) == .hidden, "Ready: nothing to show")
        check(FirstRunSetup.setupCue(.ready, canAnswer: true, firstRunWindowUp: false) == .ready,
              "Ready: the ready dot stays until the panel opens")
        check(FirstRunSetup.setupCue(.ready, canAnswer: false, firstRunWindowUp: false) == .later
              && FirstRunSetup.setupCue(.ready, canAnswer: false, firstRunWindowUp: true) == .hidden,
              "Can't answer again: the ready dot gives way to the ! cue (none over the window)")

        // The menu bar icon's Finish Setup…: only while setup is under way.
        func menuSetup(_ canAnswer: Bool?, _ card: NotchSetupCard, window: Bool = false, unfinished: Bool = false) -> Bool {
            FirstRunSetup.menuOffersSetup(canAnswer: canAnswer, card: card, firstRunWindowUp: window, unfinished: unfinished)
        }
        check(menuSetup(false, .hidden), "Wisp can't answer: Finish Setup… shows")
        check(menuSetup(nil, .later) && menuSetup(true, .open), "A setup cue or an open card: it shows")
        check(menuSetup(nil, .hidden, window: true) && menuSetup(true, .hidden, unfinished: true),
              "The first-run window is up, or was left unfinished: it shows")
        check(!menuSetup(true, .hidden), "Set up: it hides (Settings › Models changes the model)")
        check(!menuSetup(true, .ready), "The ready dot is not setup under way")
        check(!menuSetup(nil, .hidden), "Before the service first answers: nothing shown by itself")

        // Permission rows: their state in a word, and Contacts only once it helps.
        check(FirstRunPermissions.label(.calendar, .notAsked) == "Not set"
              && FirstRunPermissions.label(.calendar, .allowed) == "Allowed"
              && FirstRunPermissions.label(.calendar, .notAllowed) == "Denied"
              && FirstRunPermissions.label(.fullDiskAccess, .notAllowed) == "Needs System Settings",
              "Not set, Allowed, Denied, Needs System Settings")
        check([.allowed, .notAllowed, .notOnThisMac].allSatisfy(FirstRunPermissions.isDecided)
              && ![.notAsked, .checking].contains(where: FirstRunPermissions.isDecided), "Decided means macOS has an answer")
        check(!FirstRunPermissions.contactsUseful(messagesReadable: false) && FirstRunPermissions.contactsUseful(messagesReadable: true),
              "Contacts waits for Messages to be readable")

        // The Calendar and Reminders buttons: macOS asks once, so a turned-down row opens its page instead.
        check(FirstRunPermissions.eventKitClick(.notDetermined) == .ask
              && FirstRunPermissions.eventKitClick(.fullAccess) == .alreadyAllowed
              && [.denied, .restricted, .writeOnly].allSatisfy { FirstRunPermissions.eventKitClick($0) == .openSettings },
              "Undecided: ask macOS; allowed: nothing; denied, restricted or write-only: System Settings")
        check(FirstRunPermissions.eventKitUnanswered(before: .notDetermined, after: .notDetermined)
              && !FirstRunPermissions.eventKitUnanswered(before: .notDetermined, after: .fullAccess)
              && !FirstRunPermissions.eventKitUnanswered(before: .notDetermined, after: .denied)
              && !FirstRunPermissions.eventKitUnanswered(before: .denied, after: .denied),
              "An ask that leaves macOS with no answer is reported; a Don't Allow is an answer")
        // An Automation question nobody answers: "Asking…" while it is new, then Ask again.
        check(FirstRunPermissions.allowTitle(.mail, asking: true, waiting: false) == "Asking…"
              && FirstRunPermissions.allowTitle(.mail, asking: true, waiting: true) == "Asking…"
              && FirstRunPermissions.allowTitle(.mail, asking: false, waiting: true) == "Ask again"
              && FirstRunPermissions.allowTitle(.messages, asking: false, waiting: true) == "Ask again"
              && FirstRunPermissions.allowTitle(.mail, asking: false, waiting: false) == "Allow"
              && FirstRunPermissions.allowTitle(.fullDiskAccess, asking: false, waiting: false) == "Open System Settings",
              "A question still up after the wait reads Ask again; a new one reads Asking…")
        check(FirstRunPermissions.automationPatience >= .seconds(5) && FirstRunPermissions.automationPatience <= .seconds(30)
              && FirstRunPermissions.automationWaitingNote.contains("Ask again"),
              "The row is let go after a short wait and says how to get the question back")
        let remindersBase = "What's due."
        let denied = FirstRunPermissions.eventKitDetail(.reminders, base: remindersBase, state: .notAllowed, unanswered: false)
        let silent = FirstRunPermissions.eventKitDetail(.reminders, base: remindersBase, state: .notAsked, unanswered: true)
        check(denied.contains("Privacy & Security > Reminders") && denied.contains("Open System Settings")
              && FirstRunPermissions.eventKitDetail(.calendar, base: "", state: .notAllowed, unanswered: false)
                  .contains("Privacy & Security > Calendars")
              && silent.contains("didn't show its question") && silent.contains("Quit and reopen Wisp")
              && FirstRunPermissions.eventKitDetail(.reminders, base: remindersBase, state: .notAsked, unanswered: false) == remindersBase
              && FirstRunPermissions.eventKitDetail(.reminders, base: remindersBase, state: .allowed, unanswered: true) == remindersBase,
              "Reminders row: denied says where to switch it on, a silent ask says what to do, otherwise its purpose")
        check(FirstRunSetupView.settingsPane(.reminders).hasSuffix("Privacy_Reminders")
              && FirstRunSetupView.settingsPane(.calendar).hasSuffix("Privacy_Calendars"),
              "Each row opens its own privacy page")
        // Readers never ask: an undecided Calendar or Reminders is a reason, never a request.
        for source in [RemindersWriter.Access.Source.calendar, .reminders] {
            let unasked = RemindersWriter.Access.passive(status: .notDetermined, source: source)
            check(!unasked.needsRequest && !unasked.syncing && !unasked.authorized
                  && unasked.reasonKind == source.reasonKind && unasked.reason?.contains("Permissions…") == true,
                  "\(source.name) undecided: says where to allow it, asks nothing")
            check(RemindersWriter.Access.passive(status: .authorized, source: source).authorized, "\(source.name) allowed reads")
            let denied = RemindersWriter.Access.passive(status: .deniedOrRestricted, source: source)
            check(!denied.needsRequest && denied.reason?.contains("turned off") == true, "\(source.name) denied: Settings")
        }
        // A Brief or Day link for a permission macOS never asked about opens Wisp's own row.
        check(Permissions.needsAskingInWisp(BriefDetail.calendarsAccess.url, calendarUndecided: true, remindersUndecided: false)
              && !Permissions.needsAskingInWisp(BriefDetail.calendarsAccess.url, calendarUndecided: false, remindersUndecided: true)
              && Permissions.needsAskingInWisp(BriefDetail.remindersAccess.url, calendarUndecided: false, remindersUndecided: true)
              && !Permissions.needsAskingInWisp(BriefDetail.fullDiskAccess.url, calendarUndecided: true, remindersUndecided: true),
              "Undecided Calendar or Reminders: Wisp's Permissions step; anything else: System Settings")
        check(Permissions.needsAskingInWisp(BriefDetail.automationAccess.url, calendarUndecided: false,
                                            remindersUndecided: false, automationUndecided: true)
              && !Permissions.needsAskingInWisp(BriefDetail.automationAccess.url, calendarUndecided: true,
                                                remindersUndecided: true, automationUndecided: false),
              "Undecided Automation: Wisp's Mail & Notes row; decided: System Settings")
        // Mail or Notes open with Automation never decided: point at Wisp's row, not "Open Mail".
        let closed = (kind: "full_disk_access", reason: "Wisp reads Mail while Mail is open. Open Mail, or …")
        let unasked = Permissions.unaskedAutomation(app: "Mail", appRunning: true, automationDecided: false, failure: closed)
        check(unasked.kind == "mail_automation" && unasked.reason.contains("Permissions…")
              && unasked.reason.contains("Mail & Notes") && !unasked.reason.contains("Open Mail"),
              "Open but never asked: the Permissions row")
        check(Permissions.unaskedAutomation(app: "Mail", appRunning: false, automationDecided: false, failure: closed) == closed
              && Permissions.unaskedAutomation(app: "Notes", appRunning: true, automationDecided: true, failure: closed) == closed,
              "Closed, or Automation decided: the on-disk reason as before")
        // The Google step: an outage is not a sign-in problem; Gmail unticked is said plainly.
        check(OnboardingGoogleCopy.signInProblem(GoogleAccountModel.serviceDown) == nil
              && OnboardingGoogleCopy.signInProblem("Account connected.") == nil
              && OnboardingGoogleCopy.signInProblem("") == nil, "Outage, success, nothing: no sign-in problem")
        let builtIn = OnboardingGoogleCopy.connectDetail(GoogleStatus(clientSource: "builtin",
                                                                      consentNotice: GoogleConsentCopy.unverified))
        check(builtIn.contains("tester list") && builtIn.contains("isn't verified") && builtIn.contains("7 days")
              && builtIn.contains("reconnect") && builtIn.components(separatedBy: "verified").count == 2,
              "Built-in Google sign-in: tester list, the unverified warning once, and 7 days")
        check(OnboardingGoogleCopy.connectDetail(GoogleStatus(clientSource: "user"))
                == "Opens Google's sign-in page in your browser.", "The person's own Google client: no testing notes")
        let noNotice = OnboardingGoogleCopy.connectDetail(GoogleStatus(clientSource: "builtin"))
        check(noNotice.contains("tester list") && noNotice.contains(GoogleConsentCopy.unverified) && noNotice.contains("7 days"),
              "Built-in, no notice from the service: Wisp's own unverified warning")
        let ownNotice = OnboardingGoogleCopy.connectDetail(GoogleStatus(clientSource: "builtin", consentNotice: "Google says X."))
        check(ownNotice.contains("Google says X.") && !ownNotice.contains(GoogleConsentCopy.unverified)
              && ownNotice.contains("tester list") && ownNotice.contains("7 days"),
              "Built-in with the service's own notice: its words, plus the tester list and 7 days")
        check(OnboardingGoogleCopy.signInProblem("Wisp could not connect Gmail: Gmail read access wasn't granted — on "
                  + "Google's consent screen, tick the box to view your email messages, then try again")
              == GoogleGrantNames.gmailNotGrantedMessage, "Gmail unticked: the tick-box message")

        // Getting to know you, offered by Wisp itself: due by the service's word,
        // shown only in a quiet moment, and never waiting on the model.
        let calm = KnowYouOffer.Moment()
        check(KnowYouOffer.shouldOffer(calm, serviceOffers: true), "Quiet and due: offer it, even mid-download")
        check(!KnowYouOffer.shouldOffer(calm, serviceOffers: false), "Not due (finished, Later recently, twice already): never")
        let busy: [(String, WritableKeyPath<KnowYouOffer.Moment, Bool>)] = [
            ("the first-run window", \.firstRunWindowUp), ("the setup window", \.setupWindowUp),
            ("the setup card", \.setupCardOpen), ("a reply in progress", \.replying),
            ("the flow already open", \.flowOpen), ("an open panel", \.panelInUse),
            ("the notch dismissed to the menu bar", \.dismissedToMenuBar),
            ("Tell Wisp about you chosen this session", \.chosenThisSession),
            ("a completed About you (no more GET /profile)", \.profileComplete),
            ("About you finished or skipped in the onboarding window", \.aboutYouDecidedInWindow),
        ]
        for (name, key) in busy {
            var moment = calm
            moment[keyPath: key] = true
            check(!KnowYouOffer.quiet(moment) && !KnowYouOffer.shouldOffer(moment, serviceOffers: true), "Not over \(name)")
        }

        // Step 1 states.
        check(FirstRunBrain.from(status: nil, downloads: []) == .checking, "No status yet")
        if case .unavailable = FirstRunBrain.from(status: status(available: false), downloads: []) {} else {
            check(false, "Engine that can't run here")
        }
        if case .unavailable = FirstRunBrain.from(status: status(bundled: false), downloads: []) {} else {
            check(false, "External engine sends people to the detailed setup")
        }
        // A macOS too old for the engine also falls back to external mode: the
        // engine's own reason, not "set to use another app".
        check(FirstRunBrain.from(status: status(bundled: false, available: false), downloads: [])
                == .unavailable("Wisp's engine needs macOS 26."), "Unsupported macOS says why")
        check(FirstRunBrain.from(status: status(running: false), downloads: []) == .startingEngine, "Engine not running yet")
        // The person's own oMLX (external mode) before its server answers: connecting,
        // never the "can't set this up" card, however long the first answer takes.
        let connecting = FirstRunBrain.from(status: status(bundled: false, running: false), downloads: [])
        check(connecting == .startingEngine, "External mode, not ready yet, is not unavailable")
        check(connecting.footer(stalled: false) == .working("Getting Wisp ready…"), "Connecting shows a spinner, not a failure")
        check(FirstRunBrain.from(status: status(bundled: false, running: false, available: false), downloads: [])
                == .startingEngine, "A Mac that can't run the built-in engine still connects to its own oMLX")
        if case .unavailable(let why) = FirstRunBrain.from(status: status(bundled: false), downloads: []) {
            check(why.hasPrefix("Your oMLX app is running but doesn't have Gemma 4 E4B"),
                  "A running oMLX without Wisp's model is the stuck case, and says so")
        } else { check(false, "A running oMLX without the model goes to the detailed setup") }
        // Right after Wisp starts their oMLX its server answers before it lists any model: that is
        // still the search, not the stuck case, until the search window has passed.
        let running = status(bundled: false)
        check(running.usesOwnOmlx && running.existingOmlx == nil, "A running oMLX without the model is still their oMLX")
        check(FirstRunBrain.from(status: running, downloads: [], stillLooking: true) == .startingEngine,
              "Inside the search window a running oMLX without the model is still being looked for")
        check(FirstRunBrain.from(status: running, downloads: [], stillLooking: true).footer(stalled: false)
                == .working("Getting Wisp ready…"), "...with a spinner, no failure copy and no Try again")
        check(FirstRunBrain.from(status: status(bundled: false, available: false), downloads: [], stillLooking: true)
                == .startingEngine, "...even on a Mac that can't run the built-in engine")
        if case .unavailable = FirstRunBrain.from(status: running, downloads: [], stillLooking: false) {} else {
            check(false, "Past the search window the stuck case reaches the detailed setup")
        }
        if case .unavailable = FirstRunBrain.from(status: running, downloads: []) {} else {
            check(false, "The window is off by default: the notch's card reads it as before")
        }
        if case .unavailable = FirstRunBrain.from(status: status(available: false), downloads: [], stillLooking: true) {} else {
            check(false, "The window never hides the built-in engine's own failure")
        }
        check(FirstRunBrain.from(status: status(ready: true, bundled: false), downloads: [], stillLooking: true).isReady,
              "Ready wins over the search window")
        check(FirstRunBrain.stillLooking(waited: nil) && FirstRunBrain.stillLooking(waited: 0)
              && FirstRunBrain.stillLooking(waited: FirstRunBrain.connectGrace - 0.1)
              && !FirstRunBrain.stillLooking(waited: FirstRunBrain.connectGrace),
              "The search lasts exactly connectGrace, and starts when it is first seen")
        check(FirstRunBrain.connectGrace >= 30, "A cold oMLX gets a long search before anything is called wrong")
        check(FirstRunBrain.startingTitle(ownOmlx: true) == "Looking for your oMLX…"
              && FirstRunBrain.startingTitle(ownOmlx: false) == "Getting Wisp ready…",
              "Their oMLX is looked for, the built-in engine is started")
        // A model the person's oMLX serves that would replace a choice: one click, never automatic.
        let offered = FirstRunBrain.offered(model: gemma, label: "Gemma 4 E4B")
        check(offered.modelToUse == gemma && offered.footer(stalled: false) == .tryAgain
              && !offered.isArriving && !offered.waitsForDownloadClick && offered.modelToFetch == nil,
              "An offered model waits for its Use click and is never fetched")
        // Connecting to the person's oMLX always reaches a Try again, even with no automatic start.
        check(!FirstRunBrain.startStalled(autoStarted: false, failed: false, ownOmlx: true,
                                          waited: FirstRunBrain.connectGrace - 1),
              "Looking for their oMLX is not stalled inside the grace period")
        check(FirstRunBrain.startStalled(autoStarted: false, failed: false, ownOmlx: true,
                                         waited: FirstRunBrain.connectGrace),
              "Looking for their oMLX past the grace period offers Try again without an automatic start")
        check(!FirstRunBrain.startStalled(autoStarted: true, failed: false, ownOmlx: true, waited: 1)
              && !FirstRunBrain.startStalled(autoStarted: false, failed: true, ownOmlx: true, waited: 1),
              "An early or failed automatic start of their oMLX waits out the search before Try again")
        check(!FirstRunBrain.startStalled(autoStarted: false, failed: false, ownOmlx: false, waited: 600),
              "The built-in engine keeps its own rule: stalled only after Wisp's start or a failure")
        check(FirstRunBrain.startStalled(autoStarted: true, failed: false, ownOmlx: false, waited: 0)
              && FirstRunBrain.startStalled(autoStarted: false, failed: true, ownOmlx: false, waited: 0),
              "An automatic start that left nothing running, or a failure, offers Try again")
        // S1: setup finished, reopened from Permissions…, the person's oMLX off. The wait is
        // still timed so Try again appears and says oMLX isn't running; Wisp never starts it.
        let reopened = FirstRunBrain.whileConnecting(setupDone: true, connecting: true)
        check(!reopened.startsOmlx && reopened.timesGrace, "Finished setup: Try again after the grace, no automatic start")
        check(FirstRunBrain.whileConnecting(setupDone: false, connecting: true) == (true, true)
              && FirstRunBrain.whileConnecting(setupDone: false, connecting: false) == (false, false),
              "Only an unfinished first run starts the person's oMLX by itself")
        check(FirstRunBrain.from(status: status(bundled: false, running: false), downloads: []).footer(
                stalled: FirstRunBrain.startStalled(autoStarted: false, failed: false, ownOmlx: true,
                                                    waited: FirstRunBrain.connectGrace)) == .tryAgain
              && FirstRunBrain.connectingDetail(setupDone: true, stalled: true).hasPrefix("Your oMLX isn't running")
              && !FirstRunBrain.connectingDetail(setupDone: true, stalled: false).contains("isn't running"),
              "Finished setup, oMLX off: Try again and a plain line once the grace has passed")
        check(FirstRunBrain.from(status: status(), downloads: []) == .needsDownload(model: gemma, label: "Gemma 4 E4B", sizeGb: 5.5),
              "Nothing installed: offer the download")
        guard case .downloading(_, _, let fraction, let done, let total, let left) =
                FirstRunBrain.from(status: status(), downloads: [task("downloading")]) else {
            return check(false, "Downloading")
        }
        check(abs(fraction - 2.5 / 5.5) < 0.001 && abs(done - 2.5) < 0.001 && abs(total - 5.5) < 0.001, "Progress in GB")
        check(left == 300, "3 GB at 10 MB/s is five minutes")
        if case .failed = FirstRunBrain.from(status: status(), downloads: [task("failed")]) {} else { check(false, "Failed") }
        if case .failed = FirstRunBrain.from(status: status(), downloads: [task("cancelled")]) {} else { check(false, "Cancelled") }
        check(FirstRunBrain.from(status: status(), downloads: [task("completed")]) == .waitingForEngine(model: gemma, label: "Gemma 4 E4B"),
              "Completed but not listed yet: wait for the engine, never apply")
        check(FirstRunBrain.from(status: status(installed: [gemma]), downloads: []) == .switching(model: gemma, label: "Gemma 4 E4B"),
              "Installed but not in use: switch it on")
        check(FirstRunBrain.from(status: status(ready: true, installed: [gemma], roles: gemma), downloads: []) == .ready(label: "Gemma 4 E4B"),
              "Ready names the model in use")
        check(FirstRunBrain.from(status: status(ready: true), downloads: []).isReady, "Ready wins over everything")

        // Every role on a remote provider (Cloud Everything): Wisp already answers, no download.
        let remote = FirstRunBrain.from(status: status(roles: "claude-sonnet", remoteOnly: true, roleEndpoint: "anthropic"),
                                        downloads: [])
        check(remote == .ready(label: "claude-sonnet") && remote.footer(stalled: false) == .ready
              && remote.modelToFetch == nil, "Remote-only skips the model download")
        check(FirstRunBrain.from(status: status(running: false, remoteOnly: true), downloads: []).isReady,
              "Remote-only answers even with no local engine running")
        check(NotchModelOffer.from(status: status(remoteOnly: true), downloads: []) == .none,
              "Remote-only: the notch offers no download either")

        // Wisp's service that never comes up: after the notch's 3 minutes, Check again.
        check(FirstRunBrain.serviceStartLimit == 180, "The same 3 minutes AppDelegate allows a cold first launch")
        check(!FirstRunBrain.serviceSlow(waited: 179) && FirstRunBrain.serviceSlow(waited: 180), "Slow from 3 minutes")
        check(FirstRunBrain.checking.footer(stalled: false) == .working("Starting Wisp…")
              && FirstRunBrain.checking.footer(stalled: true) == .checkAgain, "The last step's footer offers Check again too")

        // A stop is a stop: its words name the card's own button.
        if case .failed(_, _, let message, true) = FirstRunBrain.from(status: status(), downloads: [task("cancelled")]) {
            check(message == FirstRunBrain.stoppedMessage && message.contains("Resume") && !message.contains("Retry"),
                  "Stopped: the message names Resume, the button it sits beside")
        } else { check(false, "Cancelled reads as stopped") }
        if case .failed(_, _, let message, false) = FirstRunBrain.from(status: status(), downloads: [task("failed")]) {
            check(message.contains("Retry") && !message.contains("Resume"), "Failed: the message names Retry")
        } else { check(false, "Failed reads as failed") }

        // The disk warning goes with every click that starts a download.
        check(FirstRunBrain.Footer.download.fetches && FirstRunBrain.Footer.retry.fetches
              && !FirstRunBrain.Footer.tryAgain.fetches && !FirstRunBrain.Footer.ready.fetches
              && !FirstRunBrain.Footer.working("Downloading · 45%").fetches, "Download, Retry, Resume, Download again")
        check(footerOf(status(), [task("cancelled")]).fetches && footerOf(status(), [task("completed")], stalled: true).fetches
              && !footerOf(status(), [task("completed")]).fetches && !footerOf(status(), [task("downloading")]).fetches,
              "Resume and a stuck Download again warn; a download under way or finishing doesn't")

        // Exactly one default action per state: the card's when it waits on a click.
        let returnCases: [(SetupStatus, [SetupGuideModel.DownloadTask], Bool, Bool)] = [
            (status(), [], false, true),
            (status(), [task("failed")], false, true),
            (status(), [task("cancelled")], false, true),
            (status(), [task("completed")], false, false),
            (status(), [task("completed")], true, true),
            (status(installed: [gemma]), [], false, false),
            (status(installed: [gemma]), [], true, true),
            (status(), [task("downloading")], false, false),
            (status(ready: true), [], false, false),
            (status(available: false), [], false, false),
        ]
        for (s, tasks, stalled, card) in returnCases {
            check(FirstRunBrain.from(status: s, downloads: tasks).cardTakesReturn(stalled: stalled) == card,
                  "Return goes to \(card ? "the card" : "Continue") for \(FirstRunBrain.from(status: s, downloads: tasks))")
        }
        check(FirstRunBrain.offered(model: gemma, label: "Gemma 4 E4B").cardTakesReturn(stalled: false),
              "An offered model: Return is its Use button, not Continue")
        check(!FirstRunBrain.checking.cardTakesReturn(stalled: false) && FirstRunBrain.checking.cardTakesReturn(stalled: true),
              "A slow service: Return is Check again, never a skip past the step")
        check(!FirstRunBrain.startingEngine.cardTakesReturn(stalled: false)
              && FirstRunBrain.startingEngine.cardTakesReturn(stalled: true),
              "A start that stalled: Return is the card's Try again, never a skip past the step")

        // Remote-only: step 1 says there is nothing to download, and nothing about a local model.
        let remoteHeading = FirstRunBrain.welcomeSubtitle(remoteOnly: true)
        check(remoteHeading.contains("nothing to download") && !remoteHeading.contains("runs on this Mac")
              && !remoteHeading.contains("download that model"), "Remote-only: no download, no local-model claim")
        check(FirstRunBrain.welcomeSubtitle(remoteOnly: false).contains("download that model once"),
              "A local setup still hears about its one download")

        // Return on step 1 belongs to Download/Retry only while one is waiting.
        check(FirstRunBrain.from(status: status(), downloads: []).waitsForDownloadClick, "Download waits for its click")
        check(FirstRunBrain.from(status: status(), downloads: [task("failed")]).waitsForDownloadClick, "Retry waits for its click")
        for state in ["downloading", "completed"] {
            check(!FirstRunBrain.from(status: status(), downloads: [task(state)]).waitsForDownloadClick, "\(state): Continue")
        }
        check(!FirstRunBrain.from(status: status(ready: true), downloads: []).waitsForDownloadClick, "Ready: Continue")

        // The last step's footer never spins on nothing: each state either shows
        // something under way or offers the click that gets it going.
        func footer(_ s: SetupStatus?, _ tasks: [SetupGuideModel.DownloadTask] = [], failed: Bool = false) -> FirstRunBrain.Footer {
            FirstRunBrain.from(status: s, downloads: tasks).footer(stalled: failed)
        }
        check(footer(nil) == .working("Starting Wisp…"), "Service still starting")
        check(footer(status(running: false)) == .working("Getting Wisp ready…"), "Engine starting")
        check(footer(status(running: false), failed: true) == .tryAgain, "Engine start failed: Try again")
        check(footer(status()) == .download, "Never downloaded: Download")
        check(footer(status(), [task("failed")]) == .retry && footer(status(), [task("cancelled")]) == .retry, "Stopped: Retry")
        check(footer(status(available: false)) == .detailedSetup, "Can't run here: the detailed setup")
        check(footer(status(), [task("downloading")]) == .working("Downloading · 45%"), "Downloading shows progress")
        check(footer(status(), [task("completed")]) == .working("Finishing setup…"), "Finishing")
        check(footer(status(), [task("completed")], failed: true) == .retry, "Never listed by the engine: Download again")
        check(footer(status(installed: [gemma])) == .working("Finishing setup…")
              && footer(status(installed: [gemma]), failed: true) == .tryAgain, "Switching over failed: Try again")
        check(FirstRunBrain.from(status: status(), downloads: [task("completed")]).modelToFetch == gemma, "Download again fetches it")
        let progress = [task("downloading", done: 1e9), task("downloading", done: 2e9)]
            .map { FirstRunBrain.from(status: status(), downloads: [$0]) }
        check(progress[0] != progress[1] && progress[0].stage == progress[1].stage, "Progress is not a new stage")
        check(footer(status(ready: true)) == .ready, "Ready")
        check(FirstRunBrain.from(status: status(), downloads: [task("downloading")]).isArriving
              && FirstRunBrain.from(status: status(), downloads: [task("completed")]).isArriving
              && !FirstRunBrain.from(status: status(), downloads: []).isArriving
              && !FirstRunBrain.from(status: status(), downloads: [task("failed")]).isArriving,
              "\"Keep going while this downloads\" only while it does")
        check(FirstRunBrain.from(status: status(), downloads: [task("failed")]).modelToFetch == gemma
              && FirstRunBrain.from(status: status(), downloads: []).modelToFetch == gemma
              && FirstRunBrain.from(status: status(), downloads: [task("downloading")]).modelToFetch == nil,
              "Download and Retry fetch the recommended model")

        // A Mac nothing fits (8 GB, or memory unread): no Download the engine would
        // refuse; the reason, with the detailed setup's other routes.
        let tooSmall = "Wisp's models need a Mac with at least 16 GB of memory, and this one has 8 GB."
        check(FirstRunBrain.from(status: status(fits: false, download: false, warning: tooSmall), downloads: [])
                == .unavailable(tooSmall), "Nothing fits: say why, never Download")
        check(FirstRunBrain.from(status: status(fits: false, download: false, warning: tooSmall), downloads: []).footer(stalled: false)
                == .detailedSetup, "Nothing fits: the footer offers the detailed setup")
        check(FirstRunBrain.from(status: status(fits: true), downloads: []) == .needsDownload(model: gemma, label: "Gemma 4 E4B", sizeGb: 5.5),
              "It fits: Download")
        check(FirstRunBrain.from(status: status(), downloads: []) == .needsDownload(model: gemma, label: "Gemma 4 E4B", sizeGb: 5.5),
              "An older service that never says fits: Download as before")

        // The notch card's model step offers what the window does, in place.
        func offer(_ s: SetupStatus, _ tasks: [SetupGuideModel.DownloadTask] = []) -> NotchModelOffer {
            NotchModelOffer.from(status: s, downloads: tasks)
        }
        check(offer(status()) == .download(.needsDownload(model: gemma, label: "Gemma 4 E4B", sizeGb: 5.5)),
              "Notch: Download in the card, not a window")
        if case .download(.downloading) = offer(status(), [task("downloading")]) {} else { check(false, "Notch: progress in the card") }
        if case .download(.downloading) = offer(status(), [task("pending", done: 0, total: 0, bps: 0)]) {} else {
            check(false, "Notch: a pending transfer shows as starting")
        }
        if case .download(.failed(_, _, _, true)) = offer(status(), [task("cancelled")]) {} else { check(false, "Notch: paused") }
        if case .download(.failed(_, _, _, false)) = offer(status(), [task("failed")]) {} else { check(false, "Notch: failed, Try again") }
        check(offer(status(), [task("completed")]) == .download(.waitingForEngine(model: gemma, label: "Gemma 4 E4B")),
              "Notch: finishing")
        check(offer(status(running: false)) == .startEngineFirst, "Notch: the engine step comes first")
        check(offer(status(bundled: false)) == .elsewhere("Download a model  ⤢"), "Notch: an external oMLX keeps ⤢")
        check(offer(status(download: false, modelAction: "add_local_model")) == .elsewhere("Add a model  ⤢"),
              "Notch: no public download keeps ⤢")
        check(offer(status(download: false)) == .elsewhere("Add a model  ⤢"), "Notch: nothing to fetch keeps ⤢")
        check(offer(status(fits: false, download: false, warning: tooSmall, modelAction: "no_local_fit")) == .noFit(tooSmall),
              "Notch: nothing fits says why")
        check(offer(status(ready: true, installed: [gemma], roles: gemma)) == .none, "Notch: ready has nothing to offer")
        check(NotchModelOffer.progressLine(doneGb: 0, totalGb: 5.5, speed: nil, secondsLeft: nil, stalled: false)
                == "Starting download…", "Notch: starting")
        check(NotchModelOffer.progressLine(doneGb: 2.5, totalGb: 5.5, speed: "10 MB/s", secondsLeft: 300, stalled: false)
                == "2.5 of 5.5 GB · 10 MB/s · about 5 minutes left", "Notch: size, speed and time left")
        check(NotchModelOffer.progressLine(doneGb: 2.5, totalGb: 5.5, speed: "10 MB/s", secondsLeft: 300, stalled: true)
                == "2.5 of 5.5 GB · No progress — check your connection", "Notch: stalled after 30 s")
        check(NotchModelOffer.stoppedDetail(paused: true, engineMessage: "x") == "Resume starts it again."
              && NotchModelOffer.stoppedDetail(paused: false, engineMessage: nil) == "Try again starts it again."
              && NotchModelOffer.stoppedDetail(paused: false, engineMessage: "Disk full.")
                 == "Disk full. Try again starts it again.", "Notch: a stop says what its button does")
        check(NotchModelOffer.listingWait == 30, "Notch: Download again after the same 30 s as the window")

        // Paused and failed downloads are told apart.
        if case .failed(_, _, _, let paused) = FirstRunBrain.from(status: status(), downloads: [task("cancelled")]) {
            check(paused, "Cancelled: Download stopped")
        }
        if case .failed(_, _, _, let paused) = FirstRunBrain.from(status: status(), downloads: [task("failed")]) {
            check(!paused, "Failed: Download failed")
        }
        // Disk space is checked before Download, not discovered part-way.
        check(FirstRunBrain.spaceNeeded(sizeGb: 5.5) == 8, "Gemma needs about 8 GB")
        check(FirstRunBrain.diskWarning(freeBytes: 50_000_000_000, sizeGb: 5.5) == nil, "Plenty of room: nothing")
        check(FirstRunBrain.diskWarning(freeBytes: 6_200_000_000, sizeGb: 5.5)?.hasPrefix("This Mac has 6.2 GB free") == true,
              "Short of room: say so before Download")
        check(FirstRunBrain.diskWarning(freeBytes: nil, sizeGb: 5.5) == nil, "Unknown: nothing")
        // A transfer whose bytes stop moving says so after 30 s.
        var watch = DownloadProgressWatch()
        let t0 = Date(timeIntervalSince1970: 1000)
        check(!watch.stalled(at: t0), "Nothing seen yet")
        watch.observe(doneGb: 1.0, at: t0)
        watch.observe(doneGb: 1.0, at: t0.addingTimeInterval(20))
        check(!watch.stalled(at: t0.addingTimeInterval(25)), "Under 30 s")
        check(watch.stalled(at: t0.addingTimeInterval(31)), "30 s without a byte: No progress")
        watch.observe(doneGb: 1.2, at: t0.addingTimeInterval(32))
        check(!watch.stalled(at: t0.addingTimeInterval(40)), "Moving again")

        // The window names the model plainly; build details stay in the detailed setup.
        check(FirstRunBrain.plainLabel(realLabel) == "Gemma 4 E4B" && FirstRunBrain.plainLabel("Ling") == "Ling",
              "Plain model name")

        // The tour teaches only shortcuts Wisp really holds, and examples that can work.
        check(FirstRunSetupView.shortcutsLine(ask: true, voice: true) == "⌥ Space to ask  ·  ⌥ ⇧ Space to ask by voice",
              "Both keys: ask, and ask by voice")
        check(!FirstRunSetupView.shortcutsLine(ask: false, voice: true).hasPrefix("⌥ Space to ask")
              && FirstRunSetupView.shortcutsLine(ask: false, voice: true).contains("top of the screen"),
              "Another app owns ⌥Space: point at the top of the screen instead")
        check(!FirstRunSetupView.shortcutsLine(ask: true, voice: false).contains("voice"), "No voice line when it isn't Wisp's")
        for (ask, voice) in [(true, true), (true, false), (false, true), (false, false)] {
            let line = FirstRunSetupView.shortcutsLine(ask: ask, voice: voice)
            check(!line.contains("⌘ ⇧ F") && !line.lowercased().contains("search"), "Smart Search is retired: never taught")
        }
        // The live check on the last step: invite, then confirm once ⌥Space really opened Ask.
        check(FirstRunSetupView.shortcutTry(registered: true, pressed: false) == "Try it now: press ⌥ Space."
              && FirstRunSetupView.shortcutTry(registered: true, pressed: true) == "That's it — ⌥ Space works."
              && FirstRunSetupView.shortcutTry(registered: false, pressed: false) == nil
              && FirstRunSetupView.shortcutTry(registered: false, pressed: true) == nil,
              "Try it now, then That's it; nothing when another app owns ⌥Space")
        // Open Wisp when you log in: on only once macOS holds the request; approval explained.
        check(!LaunchAtLogin.isOn(.notRegistered) && !LaunchAtLogin.isOn(.notFound)
              && LaunchAtLogin.isOn(.enabled) && LaunchAtLogin.isOn(.requiresApproval), "The switch follows macOS's status")
        check(LaunchAtLogin.note(.requiresApproval)?.contains("System Settings › General › Login Items") == true
              && LaunchAtLogin.note(.enabled) == nil && LaunchAtLogin.note(.notRegistered) == nil,
              "Waiting for approval says where; otherwise nothing")
        check(LaunchAtLogin.problem(turningOn: true).contains("Login Items")
              && LaunchAtLogin.problem(turningOn: false).contains("Login Items"), "A failure says where to do it by hand")
        check(FirstRunSetupView.tabLines.map(\.tab) == ["Ask", "Brief", "Day"], "The tabs in the notch's order")
        for style in [SurfaceLayout.Style.notch, .menuBar, .floating] {
            check(FirstRunSetupView.findLine(style).contains("menu bar"), "Every display names the menu bar icon")
        }
        check(FirstRunSetupView.findLine(.notch).contains("notch") && !FirstRunSetupView.findLine(.menuBar).contains("notch"),
              "Only a notch Mac is told about a notch")

        // Contacts' reader asks only when told it may; the app never tells it so.
        check(ContactsReader.shouldRequest(.notDetermined, mayAsk: true), "Undecided and allowed to ask: ask")
        check(!ContactsReader.shouldRequest(.notDetermined, mayAsk: false), "Not allowed to ask: never")
        check(!ContactsReader.shouldRequest(.denied, mayAsk: true) && !ContactsReader.shouldRequest(.authorized, mayAsk: true),
              "Decided: never again")

        // Grant More Access: only what is off, in order, one surface per click.
        check(Permissions.missing(screenRecording: true, accessibility: true, fullDiskAccess: true).isEmpty, "All on: nothing")
        check(Permissions.missing(screenRecording: false, accessibility: true, fullDiskAccess: false) == [.screenRecording, .fullDiskAccess],
              "Only what is still off")
        check(Permissions.surface(granted: true, promptedBefore: false) == .none, "On: no surface at all")
        check(Permissions.surface(granted: false, promptedBefore: false) == .prompt, "First time: the system prompt only")
        check(Permissions.surface(granted: false, promptedBefore: true) == .settings, "After that: the Settings page only")

        // The window fits a small display; its footer is never off screen.
        check(FirstRunSetup.windowHeight(room: 1000) == 600, "Room to spare: 600 pt")
        check(FirstRunSetup.windowHeight(room: 600) == 540, "A 13-inch at Larger Text: what fits")
        check(FirstRunSetup.windowHeight(room: 300) == FirstRunSetup.minimumHeight, "Never below the minimum")

        // A cold start says how long it has taken, once that is worth saying.
        check(FirstRunBrain.waited(5) == nil && FirstRunBrain.waited(19) == nil, "A few seconds: nothing")
        check(FirstRunBrain.waited(45) == "45 s so far" && FirstRunBrain.waited(150) == "2 min so far", "Then the time")

        // Time left, said the way a person would.
        check(FirstRunBrain.timeLeft(nil) == nil && FirstRunBrain.timeLeft(0) == nil, "Unknown speed says nothing")
        check(FirstRunBrain.timeLeft(30) == "less than a minute left", "Seconds")
        check(FirstRunBrain.timeLeft(70) == "about a minute left", "One minute")
        check(FirstRunBrain.timeLeft(360) == "about 6 minutes left", "Minutes")
        check(FirstRunBrain.timeLeft(5400) == "about 1.5 hours left", "Hours")

        // The picture's label says the gesture this display actually has.
        check(FirstRunSetupView.openHint(.notch) == "Wisp lives here — hover or click", "Notch")
        check(!FirstRunSetupView.openHint(.menuBar).contains("notch") && FirstRunSetupView.openHint(.menuBar).contains("top"),
              "No notch: the top edge")
        // Calendar and Reminders are separate rows that open their own Settings pages.
        check(FirstRunSetupView.settingsPane(.calendar).hasSuffix("Privacy_Calendars")
              && FirstRunSetupView.settingsPane(.reminders).hasSuffix("Privacy_Reminders"), "Each opens its own page")
        // Messages: its own Automation row, so the first send from Ask isn't the
        // moment macOS asks. Mail & Notes keeps its own targets and its own answer.
        check(FirstRunPermissions.Item.allCases == [.calendar, .reminders, .mail, .messages, .fullDiskAccess,
                                                    .contacts, .notifications, .screenRecording, .accessibility],
              "Messages sits after Mail & Notes; Screen Recording and Accessibility come last")
        check(FirstRunPermissions.messagesAutomationTargets == ["com.apple.MobileSMS"], "Messages asks about Messages only")
        check(FirstRunPermissions.automationTargets == ["com.apple.mail", "com.apple.Notes"]
              && Set(FirstRunPermissions.automationTargets).isDisjoint(with: FirstRunPermissions.messagesAutomationTargets),
              "Mail & Notes' targets are unchanged and never include Messages")
        // Asking opens the apps only for a moment: closed again unless the person was handed them.
        check(FirstRunPermissions.quitsAfterAsking(launchedByWisp: true, isHidden: true, isActive: false),
              "Opened by Wisp, still hidden and not in use: closed after asking")
        check(!FirstRunPermissions.quitsAfterAsking(launchedByWisp: false, isHidden: true, isActive: false),
              "Already open before Wisp asked: never closed")
        check(!FirstRunPermissions.quitsAfterAsking(launchedByWisp: true, isHidden: false, isActive: false)
              && !FirstRunPermissions.quitsAfterAsking(launchedByWisp: true, isHidden: true, isActive: true),
              "Shown or brought forward while the prompt was up: left open")
        // A closed Mail or Notes (procNotFound) is "can't say": it keeps the last
        // definite answer and never reads as a removed permission.
        let noProc = OSStatus(procNotFound)
        check(FirstRunPermissions.automationRead([noProc, noProc]) == .unknown
              && FirstRunPermissions.automationRead([noErr, noProc]) == .unknown
              && FirstRunPermissions.automationRead([]) == .unknown, "A closed app can't be read")
        check(FirstRunPermissions.automationRead([noErr, noErr]) == .granted
              && FirstRunPermissions.automationRead([noErr]) == .granted, "Every target answering allowed is granted")
        check(FirstRunPermissions.automationRead([noErr, OSStatus(errAEEventNotPermitted)]) == .denied
              && FirstRunPermissions.automationRead([noProc, OSStatus(errAEEventNotPermitted)]) == .denied,
              "A running target refusing (-1743) is denied, closed or not for the other")
        check(FirstRunPermissions.automationRead([noErr, OSStatus(errAEEventWouldRequireUserConsent)]) == .notAsked,
              "A running target with no answer (-1744) is not asked")
        for stored in [FirstRunPermissions.AutomationAnswer.allowed, .denied, .reset, nil] {
            let kept = FirstRunPermissions.resolveAutomation(.unknown, stored: stored)
            check(kept.stored == stored, "procNotFound never changes what is remembered")
        }
        check(FirstRunPermissions.resolveAutomation(.unknown, stored: .allowed).state == .allowed
              && FirstRunPermissions.resolveAutomation(.unknown, stored: .denied).state == .notAllowed
              && FirstRunPermissions.resolveAutomation(.unknown, stored: nil).state == .notAsked
              && FirstRunPermissions.resolveAutomation(.unknown, stored: .reset).state == .notAsked,
              "A closed app shows the last answer, Not set when there was none")
        check(FirstRunPermissions.resolveAutomation(.denied, stored: .allowed) == (.notAllowed, .denied),
              "-1743 flips the remembered answer to denied")
        check(FirstRunPermissions.resolveAutomation(.granted, stored: .denied) == (.allowed, .allowed)
              && FirstRunPermissions.resolveAutomation(.granted, stored: nil) == (.allowed, .allowed),
              "A successful probe flips it to granted")
        check(FirstRunPermissions.resolveAutomation(.notAsked, stored: .allowed) == (.notAsked, .reset)
              && FirstRunPermissions.resolveAutomation(.notAsked, stored: .reset) == (.notAsked, .reset)
              && FirstRunPermissions.resolveAutomation(.notAsked, stored: nil) == (.notAsked, nil),
              "A grant macOS dropped reads Not set and is remembered as reset, so the row can say why")
        // Persisted: survives the window closing and Wisp relaunching (a fresh read of the same defaults).
        // An in-memory store: a UserDefaults suite would leave a .plist in ~/Library/Preferences.
        final class Answers: AutomationAnswerStore {
            var values: [String: Any] = [:]
            func string(forKey defaultName: String) -> String? { values[defaultName] as? String }
            func set(_ value: Any?, forKey defaultName: String) { values[defaultName] = value }
            func removeObject(forKey defaultName: String) { values[defaultName] = nil }
        }
        let suite = Answers()
        check(FirstRunPermissions.recordAutomation(.granted, item: .mail, defaults: suite) == .allowed, "Granted")
        check(FirstRunPermissions.recordAutomation(.unknown, item: .mail, defaults: suite) == .allowed
              && FirstRunPermissions.storedAutomation(.mail, defaults: suite) == .allowed,
              "Mail closed afterwards: still Allowed, still remembered")
        check(FirstRunPermissions.storedAutomation(.messages, defaults: suite) == nil
              && FirstRunPermissions.recordAutomation(.unknown, item: .messages, defaults: suite) == .notAsked,
              "Messages keeps its own answer")
        check(FirstRunPermissions.recordAutomation(.notAsked, item: .mail, defaults: suite) == .notAsked
              && FirstRunPermissions.storedAutomation(.mail, defaults: suite) == .reset,
              "macOS dropping the grant is remembered as reset")
        check(FirstRunPermissions.recordAutomation(.denied, item: .mail, defaults: suite) == .notAllowed
              && FirstRunPermissions.storedAutomation(.mail, defaults: suite) == .denied, "Then refused")
        let before = suite.values
        _ = FirstRunPermissions.recordAutomation(.unknown, item: .mail, defaults: suite)
        check(suite.values.count == before.count && suite.string(forKey: FirstRunPermissions.automationKey(.mail)) == "denied",
              "A read that can't say (Mail closed) never writes, so a poll can't undo an answer just recorded")
        check(FirstRunPermissions.automationKey(.mail).hasPrefix("wisp."),
              "Remembered under wisp.*, so /resetwisp clears it with the other markers")
        check(FirstRunPermissions.label(.messages, .notAsked) == "Not set"
              && FirstRunPermissions.label(.messages, .allowed) == "Allowed"
              && FirstRunPermissions.label(.messages, .notAllowed) == "Denied", "Messages: Not set, Allowed, Denied")
        check(FirstRunSetupView.settingsPane(.messages).hasSuffix("Privacy_Automation"), "Denied Messages opens Automation")
        // No score: every row is optional, and a Mac without that data is not "off".
        check(FirstRunPermissions.summary([.calendar: .allowed, .reminders: .notAllowed, .mail: .notAsked,
                                           .fullDiskAccess: .notOnThisMac, .contacts: .allowed, .notifications: .allowed])
              == "All optional · 2 still off", "Counts only what could still be switched on")
        check(FirstRunPermissions.summary([.calendar: .allowed, .fullDiskAccess: .notOnThisMac]) == "All optional · nothing left off",
              "Nothing left")
        // Contacts and Full Disk Access: no dead end on a Mac with nothing for Full Disk Access to read.
        let everyOther: [FirstRunPermissions.Item: FirstRunPermissions.State] = [
            .calendar: .allowed, .reminders: .allowed, .mail: .allowed, .messages: .allowed, .notifications: .allowed,
            .screenRecording: .allowed, .accessibility: .allowed]
        var noData = everyOther
        noData[.fullDiskAccess] = .notOnThisMac
        noData[.contacts] = .notAsked
        check(!FirstRunPermissions.contactsWaits(noData, messagesReadable: false),
              "Full Disk Access not needed here: Contacts has its own Allow, never After Full Disk Access")
        check(FirstRunPermissions.summary(noData, messagesReadable: false) == "All optional · 1 still off"
              && !FirstRunPermissions.allAnswered(noData, messagesReadable: false),
              "...and until it is answered it is the one still off")
        noData[.contacts] = .allowed
        check(FirstRunPermissions.allAnswered(noData, messagesReadable: false)
              && FirstRunPermissions.summary(noData, messagesReadable: false) == "All optional · nothing left off",
              "Contacts allowed on its own: the step ticks and nothing is left off")
        var fdaOff = everyOther
        fdaOff[.fullDiskAccess] = .notAllowed
        fdaOff[.contacts] = .notAsked
        check(FirstRunPermissions.contactsWaits(fdaOff, messagesReadable: false),
              "Full Disk Access still off with Messages unread: Contacts waits for it")
        check(FirstRunPermissions.summary(fdaOff, messagesReadable: false) == "All optional · 1 still off",
              "A waiting Contacts isn't counted on top of Full Disk Access")
        check(FirstRunPermissions.allAnswered(fdaOff, messagesReadable: false),
              "A waiting Contacts follows Full Disk Access's answer, so the step can still tick")
        var fdaOn = fdaOff
        fdaOn[.fullDiskAccess] = .allowed
        check(!FirstRunPermissions.contactsWaits(fdaOn, messagesReadable: false)
              && !FirstRunPermissions.contactsWaits(fdaOff, messagesReadable: true),
              "Full Disk Access on (even with no Messages), or Messages readable: Contacts is asked on its own")
        check(!FirstRunPermissions.contactsWaits([.contacts: .allowed, .fullDiskAccess: .notAllowed], messagesReadable: false),
              "Contacts already answered never waits")
        check(FirstRunSetupView.settingsPane(.fullDiskAccess) == Permissions.fullDiskAccessPane
              && Permissions.fullDiskAccessPane.hasSuffix("Privacy_AllFiles"), "Full Disk Access opens its own page")

        // Screen Recording and Accessibility: rows of their own, asked once, then System Settings.
        check(FirstRunPermissions.promptOnceState(granted: true, promptedBefore: false) == .allowed
              && FirstRunPermissions.promptOnceState(granted: true, promptedBefore: true) == .allowed,
              "Trusted by macOS: Allowed, asked here or not")
        check(FirstRunPermissions.promptOnceState(granted: false, promptedBefore: false) == .notAsked,
              "Never asked: Not set, with Allow")
        check(FirstRunPermissions.promptOnceState(granted: false, promptedBefore: true) == .notAllowed
              && FirstRunPermissions.label(.screenRecording, .notAllowed) == "Needs System Settings"
              && FirstRunPermissions.label(.accessibility, .notAllowed) == "Needs System Settings",
              "Asked and still off: Needs System Settings (macOS asks only once)")
        check(FirstRunPermissions.label(.screenRecording, .notAsked) == "Not set"
              && FirstRunPermissions.label(.accessibility, .allowed) == "Allowed", "Not set and Allowed as elsewhere")
        check([.fullDiskAccess, .screenRecording, .accessibility].allSatisfy(FirstRunPermissions.settingsThroughRequest)
              && ![.calendar, .reminders, .mail, .messages, .contacts, .notifications]
                    .contains(where: FirstRunPermissions.settingsThroughRequest),
              "Open System Settings goes through the row's request only where macOS can't ask again")
        check(FirstRunSetupView.settingsPane(.screenRecording).hasSuffix("Privacy_ScreenCapture")
              && FirstRunSetupView.settingsPane(.accessibility).hasSuffix("Privacy_Accessibility"),
              "Each opens its own page")
        let screenBase = "So Wisp can look at your screen when you ask about it."
        let screenRelaunch = FirstRunPermissions.screenRecordingRelaunchNote
        let screenDenied = FirstRunPermissions.screenRecordingDetail(.notAllowed)
        check(FirstRunPermissions.screenRecordingDetail(.notAsked) == screenBase + " " + screenRelaunch
              && screenDenied.hasSuffix(screenRelaunch) && screenDenied.contains("switch on Wisp")
              && FirstRunPermissions.screenRecordingDetail(.allowed) == screenBase,
              "Screen Recording says it works once Wisp quits and reopens, until it's on")
        check(FirstRunPermissions.screenRecordingRelaunchNote.contains("quits and reopens")
              && FirstRunPermissions.screenRecordingRelaunchNote.contains("picks up right here"),
              "...and that setup picks up here")
        check(FirstRunPermissions.accessibilityDetail(.notAsked) == "So Wisp can read the window you're asking about."
              && FirstRunPermissions.accessibilityDetail(.notAllowed).contains("switch on Wisp")
              && !FirstRunPermissions.accessibilityDetail(.notAllowed).contains("Smart Search"),
              "Accessibility: what it's for, and where to switch it on")
        var extrasOff = everyOther
        extrasOff[.fullDiskAccess] = .allowed
        extrasOff[.contacts] = .allowed
        extrasOff[.screenRecording] = .notAsked
        extrasOff[.accessibility] = .notAllowed
        check(FirstRunPermissions.summary(extrasOff, messagesReadable: true) == "All optional · nothing left off"
              && FirstRunPermissions.allAnswered(extrasOff, messagesReadable: true),
              "Screen Recording and Accessibility sit under More access: never counted, never hold the step open")
        check(FirstRunPermissions.Item.core.allSatisfy { !$0.isExtra }
              && FirstRunPermissions.Item.allCases.filter(\.isExtra) == [.screenRecording, .accessibility],
              "Only Screen Recording and Accessibility are extras")

        // Full Disk Access: a row of its own, on whether Messages, Mail or Notes data
        // really reads (Automation alone only helps while those apps are open).
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        check(FirstRunPermissions.fullDiskAccessState(home: scratch) == .notOnThisMac, "No data yet: nothing needs it")
        let mail = scratch.appendingPathComponent("Library/Mail")
        try! FileManager.default.createDirectory(at: mail.appendingPathComponent("V10"), withIntermediateDirectories: true)
        try! FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: mail.path)
        check(FirstRunPermissions.fullDiskAccessState(home: scratch) == .notAllowed, "Mail data that won't read: not allowed")
        try! FileManager.default.createDirectory(at: scratch.appendingPathComponent("Library/Messages"), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: scratch.appendingPathComponent("Library/Messages/chat.db").path, contents: Data())
        check(FirstRunPermissions.fullDiskAccessState(home: scratch) == .allowed, "Readable chat.db")
        try! FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: mail.path)
        try! FileManager.default.removeItem(at: scratch.appendingPathComponent("Library/Messages"))
        check(FirstRunPermissions.fullDiskAccessState(home: scratch) == .allowed, "Readable Mail folder")
        try? FileManager.default.removeItem(at: scratch)

        dev8OnboardingChecks()

        // /resetwisp brings all of the above back to a fresh Mac (tests/WispResetChecks.swift).
        WispResetChecks.run()
        print("PASS: first-run setup — model gate, Move to Applications, readers re-post when the service comes up, show and resume rules, 11 step-1 states, download click, time left, tour hints, readers never ask, ready notification, remote-only, reopen cap, Full Disk Access, Contacts on its own, slow service, one Return per state, open at login")
    }

    /// dev8 (2026-10-29 fresh-install test): the model gate, How to use's download
    /// line, Move to Applications, and the readers that re-post when the service comes up.
    static func dev8OnboardingChecks() {
        // Wisp waits for its model only while setup is under way.
        check(FirstRunSetup.gatesUse(setupUnfinished: true, canAnswer: nil), "Service not answered yet: held")
        check(FirstRunSetup.gatesUse(setupUnfinished: true, canAnswer: false), "Downloading: held")
        check(!FirstRunSetup.gatesUse(setupUnfinished: true, canAnswer: true), "Model ready: open")
        check(!FirstRunSetup.gatesUse(setupUnfinished: false, canAnswer: false),
              "Someone already set up is never held back, even if the model stops answering")

        // How to use's line: only what is known.
        check(FirstRunBrain.progressLine(doneGb: 2.5, totalGb: 5.5, speed: "10 MB/s", secondsLeft: 360)
              == "2.5 of 5.5 GB · 10 MB/s · about 6 minutes left", "Size, speed and time left")
        check(FirstRunBrain.progressLine(doneGb: 0, totalGb: 5.5, speed: nil, secondsLeft: nil) == "0.0 of 5.5 GB",
              "Nothing moving yet: no speed, no guess")
        check(FirstRunSetup.notDownloadingTitle(.download) == "The model hasn't started downloading"
              && FirstRunSetup.notDownloadingTitle(.working("Starting Wisp…")) == "Starting Wisp…",
              "How to use names what the model is doing when it isn't downloading")
        check(FirstRunSetupView.helpsYourDay.map(\.title).prefix(3) == ["Brief", "Day", "Ask"],
              "What Wisp is for starts with the three tabs")
        check(!FirstRunSetupView.helpsYourDay.contains { $0.line.contains("never leave") },
              "No absolute local claim: a cloud model is the person's choice")

        // Move to Applications: asked only where a download lands.
        let home = "/Users/sam"
        typealias Place = MoveToApplications.Place
        let places: [(String, Bool, Place)] = [
            ("/Applications/Wisp.app", false, .applications),
            ("/Users/sam/Applications/Wisp.app", false, .applications),
            ("/Users/sam/Downloads/Wisp.app", false, .downloaded),
            ("/Users/sam/Downloads/Wisp-0.5.0/Wisp.app", false, .downloaded),
            ("/Users/sam/Desktop/Wisp.app", false, .downloaded),
            ("/Volumes/Wisp/Wisp.app", false, .downloaded),
            ("/private/var/folders/x/AppTranslocation/ABC/d/Wisp.app", false, .translocated),
            ("/Users/sam/Downloads/Wisp.app", true, .translocated),
            ("/Users/sam/code/wisp/.build/Wisp.app", false, .elsewhere),
        ]
        for (path, translocated, want) in places {
            check(MoveToApplications.place(bundlePath: path, home: home, translocated: translocated) == want, path)
        }
        check(MoveToApplications.place(bundlePath: "/Users/sam/Downloads/Wisp.app", home: "/Users/sam/", translocated: false)
              == .downloaded, "A home with a trailing slash")
        check(MoveToApplications.offers(place: .downloaded, declined: false, isAppBundle: true)
              && MoveToApplications.offers(place: .translocated, declined: false, isAppBundle: true),
              "Downloads and macOS's temporary copy are asked")
        check(!MoveToApplications.offers(place: .downloaded, declined: true, isAppBundle: true), "Don't ask again holds")
        check(!MoveToApplications.offers(place: .applications, declined: false, isAppBundle: true)
              && !MoveToApplications.offers(place: .elsewhere, declined: false, isAppBundle: true)
              && !MoveToApplications.offers(place: .downloaded, declined: false, isAppBundle: false),
              "Already in Applications, a build folder, or not an app bundle: never asked")
        let replace = MoveToApplications.replaceDetail(existing: "0.5.0 (3075)", this: "0.5.0 (3120)")
        check(replace.contains("The one there is 0.5.0 (3075); this one is 0.5.0 (3120).")
              && replace.contains("goes to the Trash") && replace.contains("stay as they are"),
              "Replacing says both versions, the Trash, and that data stays")
        check(MoveToApplications.replaceDetail(existing: nil, this: nil).hasPrefix("Replace it"), "Unknown versions")
        check(MoveToApplications.needsAdmin(NSError(domain: NSCocoaErrorDomain, code: NSFileWriteNoPermissionError))
              && MoveToApplications.needsAdmin(NSError(domain: NSCocoaErrorDomain, code: NSFileWriteUnknownError,
                    userInfo: [NSUnderlyingErrorKey: NSError(domain: NSPOSIXErrorDomain, code: Int(EACCES))]))
              && !MoveToApplications.needsAdmin(NSError(domain: NSCocoaErrorDomain, code: NSFileWriteOutOfSpaceError)),
              "Only a permission refusal reads as needing an administrator")

        // Reminders after macOS's Quit & Reopen: every reading source re-posts when the
        // service comes up, since it keeps their last sync in memory only.
        check(Set(AppDelegate.resyncedWhenReachable).isSuperset(of: [.calendar, .reminders, .fullDiskAccess]),
              "Calendar, Reminders and the Full Disk Access readers re-post on reconnect")
        check(!AppDelegate.resyncedWhenReachable.contains(where: \.isExtra),
              "Nothing that only sends or looks at the screen")
    }
}
