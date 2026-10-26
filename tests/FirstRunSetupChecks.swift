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
                       available: Bool = true, installed: [String] = [], roles: String = "Ling-3.0-tiny-oQ4e") -> SetupStatus {
        let installedJSON = installed.map {
            #"{"id":"\#($0)","label":"\#($0 == gemma ? realLabel : $0)","size_gb":5.5,"fit":"recommended","tested":true,"note":""}"#
        }.joined(separator: ",")
        let json = """
        {"ready":\(ready),
         "hardware":{"chip":"Apple M5 Pro","ram_gb":24,"tier":"comfortable"},
         "omlx":{"installed":true,"running":\(running),"model_dir":null,"min_macos":26,"url":"","admin_url":"","bundled":\(bundled)},
         "bundled_engine":{"available":\(available),"minimum_macos":"26.0","unavailable_reason":\(available ? "null" : "\"Wisp's engine needs macOS 26.\"")},
         "models":{"source":"engine","installed":[\(installedJSON)],"recommended":"\(gemma)","use":null,"warning":null,
                   "recommended_label":"Gemma 4 E4B · 4-bit (oQ4e) + MTP",
                   "download":{"model":"\(gemma)","label":"\(realLabel)","size_gb":5.5,"repo":"Jundot/\(gemma)","url":"","command":"","install_hf":""},
                   "embedding_download":null},
         "roles":[{"role":"general","label":"General","model":"\(roles)","endpoint":"local"}],
         "engines":[],"checks":[],"disclosure":"","configuration_token":null}
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

    static func main() {
        // Who sees the window: only a Mac that has never run Wisp.
        check(FirstRunSetup.shouldShow { _ in false }, "Fresh Mac shows it")
        for key in [FirstRunSetup.doneKey, FirstRunSetup.setupGuideShownKey, FirstRunSetup.notchTipShownKey] {
            check(!FirstRunSetup.shouldShow { $0 == key }, "\(key) means an existing user")
        }
        var set: [String] = []
        FirstRunSetup.takeOverLaunchPrompts { set.append($0) }
        check(Set(set) == [FirstRunSetup.startedKey, FirstRunSetup.permissionsRequestedKey,
                           FirstRunSetup.setupGuideShownKey, FirstRunSetup.notchTipShownKey],
              "The old launch prompts, tip and card are all taken over, and the window is marked started")
        // Quitting part-way: started and the taken-over keys are set, done is not.
        check(FirstRunSetup.shouldShow { set.contains($0) }, "Quit before finishing: the window opens again")
        check(!FirstRunSetup.shouldShow { set.contains($0) || $0 == FirstRunSetup.doneKey },
              "Closed or finished: never again")
        // Left part-way (a quit, macOS reopening Wisp after Full Disk Access, an early
        // close): it opens by itself a few times, then the notch's cue carries it.
        for reopens in 0..<FirstRunSetup.maxAutomaticReopens {
            check(FirstRunSetup.shouldShow({ set.contains($0) }, reopens: reopens), "Reopen \(reopens + 1) opens it")
        }
        check(!FirstRunSetup.shouldShow({ set.contains($0) }, reopens: FirstRunSetup.maxAutomaticReopens),
              "Past the cap it stops opening at launch")
        check(FirstRunSetup.shouldShow({ _ in false }, reopens: 99), "The cap only counts reopens of a started setup")
        check(FirstRunSetup.isUnfinished { set.contains($0) }, "Started, not done: Continue setup opens the window")
        check(!FirstRunSetup.isUnfinished { set.contains($0) || $0 == FirstRunSetup.doneKey }
              && !FirstRunSetup.isUnfinished { _ in false }, "Done, or never started: the notch card instead")
        check(!FirstRunSetup.closeFinishes(finished: false, ready: false), "An early close before Wisp can answer keeps it unfinished")
        check(FirstRunSetup.closeFinishes(finished: true, ready: false), "Start using Wisp finishes it")
        check(FirstRunSetup.closeFinishes(finished: false, ready: true), "Closing once Wisp can answer finishes it")

        // Every launch: Wisp can't answer and no window is up → the quiet cue, never the full card.
        check(FirstRunSetup.setupCue(.hidden, canAnswer: false, firstRunWindowUp: false) == .later, "Not set up: the ! cue")
        check(FirstRunSetup.setupCue(.hidden, canAnswer: false, firstRunWindowUp: true) == .hidden, "Not over the first-run window")
        check(FirstRunSetup.setupCue(.open, canAnswer: false, firstRunWindowUp: false) == .open, "An open card is left alone")
        check(FirstRunSetup.setupCue(.later, canAnswer: true, firstRunWindowUp: false) == .hidden, "Ready: the cue goes")
        check(FirstRunSetup.setupCue(.open, canAnswer: true, firstRunWindowUp: false) == .open, "Ready: an open card waits for Done")
        check(FirstRunSetup.setupCue(.hidden, canAnswer: true, firstRunWindowUp: false) == .hidden, "Ready: nothing to show")

        // While the window is up it is the only thing that asks macOS for access.
        let readers = FirstRunReaderGate.Reader.allCases
        let waiting = FirstRunReaderGate(windowUp: true) { _ in false }
        check(readers.allSatisfy { !waiting.allows($0) }, "Window up, nothing answered: no reader starts and asks")
        let noWindow = FirstRunReaderGate(windowUp: false) { _ in false }
        check(readers.allSatisfy { noWindow.allows($0) }, "No window (an existing user, or after it closes): every reader starts")
        let calendarAnswered = FirstRunReaderGate(windowUp: true) { $0 == .calendar }
        check(calendarAnswered.allows(.calendar) && !calendarAnswered.allows(.reminders) && !calendarAnswered.allows(.mail),
              "An answered row releases only its own readers")
        check(FirstRunReaderGate.readers(for: .calendar) == [.calendar]
              && FirstRunReaderGate.readers(for: .reminders) == [.reminders]
              && FirstRunReaderGate.readers(for: .mail) == [.mail, .notes]
              && FirstRunReaderGate.readers(for: .contacts) == [.contacts], "Rows map to the readers they answer for")
        check([.allowed, .notAllowed, .notOnThisMac].allSatisfy(FirstRunReaderGate.isDecided)
              && ![.notAsked, .checking].contains(where: FirstRunReaderGate.isDecided), "Decided means macOS has an answer")

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
        check(footer(status(running: false)) == .working("Starting the engine…"), "Engine starting")
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

        // Paused and failed downloads are told apart.
        if case .failed(_, _, _, let paused) = FirstRunBrain.from(status: status(), downloads: [task("cancelled")]) {
            check(paused, "Cancelled: Download paused")
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
        check(FirstRunSetupView.shortcutsLine(ask: true, search: true).hasPrefix("⌥ Space to ask"), "Both keys")
        check(!FirstRunSetupView.shortcutsLine(ask: false, search: true).hasPrefix("⌥ Space to ask")
              && FirstRunSetupView.shortcutsLine(ask: false, search: true).contains("top of the screen"),
              "Another app owns ⌥Space: point at the top of the screen instead")
        check(!FirstRunSetupView.shortcutsLine(ask: true, search: false).contains("⌘ ⇧ F"), "No ⌘⇧F when it isn't Wisp's")
        check(FirstRunSetupView.examples([:]) == ["Remind me to call the dentist tomorrow at 10"], "Nothing allowed: the reminder only")
        check(FirstRunSetupView.examples([.calendar: .allowed, .fullDiskAccess: .allowed]).count == 3, "Calendar and mail readable: all three")
        check(!FirstRunSetupView.examples([.calendar: .notAllowed, .mail: .allowed]).contains("What's on my calendar today?"),
              "No calendar question without Calendar")

        // Contacts is asked for only while undecided and once Messages can be read.
        check(ContactsReader.shouldRequest(.notDetermined, mayAsk: true), "Undecided and useful: ask")
        check(!ContactsReader.shouldRequest(.notDetermined, mayAsk: false), "Messages unreadable: not at launch")
        check(!ContactsReader.shouldRequest(.denied, mayAsk: true) && !ContactsReader.shouldRequest(.authorized, mayAsk: true),
              "Decided: never again")

        // Grant More Access: only what is off, in order, one surface per click.
        check(Permissions.missing(screenRecording: true, accessibility: true, fullDiskAccess: true).isEmpty, "All on: nothing")
        check(Permissions.missing(screenRecording: false, accessibility: true, fullDiskAccess: false) == [.screenRecording, .fullDiskAccess],
              "Only what is still off")
        check(Permissions.surface(granted: true, promptedBefore: false) == .none, "On: no surface at all")
        check(Permissions.surface(granted: false, promptedBefore: false) == .prompt, "First time: the system prompt only")
        check(Permissions.surface(granted: false, promptedBefore: true) == .settings, "After that: the Settings page only")

        // A cold start says how long it has taken, once that is worth saying.
        check(FirstRunBrain.waited(5) == nil && FirstRunBrain.waited(19) == nil, "A few seconds: nothing")
        check(FirstRunBrain.waited(45) == "45 s so far" && FirstRunBrain.waited(150) == "2 min so far", "Then the time")

        // Time left, said the way a person would.
        check(FirstRunBrain.timeLeft(nil) == nil && FirstRunBrain.timeLeft(0) == nil, "Unknown speed says nothing")
        check(FirstRunBrain.timeLeft(30) == "less than a minute left", "Seconds")
        check(FirstRunBrain.timeLeft(70) == "about a minute left", "One minute")
        check(FirstRunBrain.timeLeft(360) == "about 6 minutes left", "Minutes")
        check(FirstRunBrain.timeLeft(5400) == "about 1.5 hours left", "Hours")

        // The tour names the gesture this display actually has.
        check(FirstRunSetupView.openHint(.notch).contains("notch"), "Notch")
        check(!FirstRunSetupView.openHint(.menuBar).contains("notch") && FirstRunSetupView.openHint(.menuBar).contains("menu bar"),
              "No notch: menu bar and top edge")
        // Calendar and Reminders are separate rows that open their own Settings pages.
        check(FirstRunSetupView.settingsPane(.calendar).hasSuffix("Privacy_Calendars")
              && FirstRunSetupView.settingsPane(.reminders).hasSuffix("Privacy_Reminders"), "Each opens its own page")
        // No score: every row is optional, and a Mac without that data is not "off".
        check(FirstRunPermissions.summary([.calendar: .allowed, .reminders: .notAllowed, .mail: .notAsked,
                                           .fullDiskAccess: .notOnThisMac, .contacts: .allowed, .notifications: .allowed])
              == "All optional · 2 still off", "Counts only what could still be switched on")
        check(FirstRunPermissions.summary([.calendar: .allowed, .fullDiskAccess: .notOnThisMac]) == "All optional · nothing left off",
              "Nothing left")
        check(FirstRunSetupView.settingsPane(.fullDiskAccess) == Permissions.fullDiskAccessPane
              && Permissions.fullDiskAccessPane.hasSuffix("Privacy_AllFiles"), "Full Disk Access opens its own page")
        check(FirstRunReaderGate.readers(for: .fullDiskAccess).isEmpty, "Full Disk Access never prompts, so it gates no reader")

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

        print("PASS: first-run setup — show and resume rules, 11 step-1 states, download click, time left, tour hints, reader gate, reopen cap, Full Disk Access")
    }
}
