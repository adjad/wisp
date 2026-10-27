import Foundation

// /resetwisp's app side (app/Sources/WispApp/ResetWisp.swift and its callers).
// Compiled with every app source by scripts/test_first_run_setup.sh and run from
// FirstRunSetupChecks. Never launches Wisp, calls its service, runs tccutil or
// writes any preferences: defaults checks use an in-memory store.
enum WispResetChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), "resetwisp: " + message)
    }

    static func run() {
        firstRunStartsOver()
        privacyResetIsWispOnly()
        relaunch()
        keychain()
        serviceReplies()
        sourceContracts()
        print("PASS: /resetwisp — first run starts over at step 1, defaults cleared, tccutil only for Wisp, "
              + "relaunch, provider keys only, typed-only destructive card")
    }

    /// An in-memory defaults domain: the app's persistent domain plus a global
    /// one, like UserDefaults.standard, without writing a preferences file.
    final class Store: WispDefaultsStore {
        let name: String
        var domain: [String: Any] = [:]
        var global: [String: Any] = ["AppleLocale": "en_US"]
        init(name: String) { self.name = name }
        func persistentDomain(forName domainName: String) -> [String: Any]? { domainName == name ? domain : nil }
        func removePersistentDomain(forName domainName: String) { if domainName == name { domain = [:] } }
        func dictionaryRepresentation() -> [String: Any] { global.merging(domain) { _, app in app } }
        func removeObject(forKey defaultName: String) { domain[defaultName] = nil }
        func synchronize() -> Bool { true }
        func object(forKey key: String) -> Any? { domain[key] ?? global[key] }
        func bool(forKey key: String) -> Bool { object(forKey: key) as? Bool ?? false }
        func integer(forKey key: String) -> Int { object(forKey: key) as? Int ?? 0 }
        func set(_ value: Any, forKey key: String) { domain[key] = value }
    }

    /// A fully onboarded Mac: every first-run and "seen" marker set.
    static func seedOnboarded(_ defaults: Store) {
        for key in WispReset.firstRunMarkers { defaults.set(true, forKey: key) }
        defaults.set(OnboardingStep.tour.rawValue, forKey: FirstRunSetup.stepKey)
        defaults.set(2, forKey: FirstRunSetup.reopensKey)
        defaults.set("Not now", forKey: "WispGoogleNoteDismissed.someone@example.com")
        defaults.set("google", forKey: "WispSettingsPane")
        defaults.set("frame", forKey: "NSWindow Frame Welcome to Wisp")
    }

    static func firstRunStartsOver() {
        // With Wisp's own bundle id the whole domain goes; an unbundled build
        // (no bundle id) still loses every marker and every Wisp key by name.
        for useDomain in [true, false] {
            let defaults = Store(name: WispReset.bundleID)
            seedOnboarded(defaults)
            let flag: (String) -> Bool = { defaults.bool(forKey: $0) }
            check(!FirstRunSetup.shouldShow(flag, reopens: defaults.integer(forKey: FirstRunSetup.reopensKey)),
                  "an onboarded Mac does not see setup before the reset")
            check(OnboardingStep.restored(defaults.object(forKey: FirstRunSetup.stepKey) as? Int) == .tour,
                  "the seeded step is the last one")

            WispReset.clearDefaults(defaults, domain: useDomain ? WispReset.bundleID : nil)

            for key in WispReset.firstRunMarkers {
                check(defaults.object(forKey: key) == nil, "\(key) is gone (domain \(useDomain))")
            }
            check(defaults.object(forKey: "WispGoogleNoteDismissed.someone@example.com") == nil,
                  "per-account notes are gone")
            check(defaults.object(forKey: "WispSettingsPane") == nil, "window and pane choices are gone")
            check(useDomain ? defaults.domain.isEmpty : defaults.domain.keys.sorted() == ["NSWindow Frame Welcome to Wisp"],
                  "the whole domain goes when it is Wisp's own")
            check(defaults.global["AppleLocale"] != nil, "the global domain is untouched")
            check(FirstRunSetup.shouldShow(flag, reopens: defaults.integer(forKey: FirstRunSetup.reopensKey)),
                  "setup shows again after a reset")
            check(OnboardingStep.restored(defaults.object(forKey: FirstRunSetup.stepKey) as? Int) == .brain,
                  "setup opens at step 1, Model")
            check(!FirstRunSetup.launchCountsTowardCap(flag), "the relaunch is a fresh Mac, not a reopen")
            check(!FirstRunSetup.isUnfinished(flag) && !FirstRunSetup.aboutYouDecidedInWindow(flag),
                  "About you and Getting to know you are offered again")
            check(FirstRunSetup.drivesModel(setupDone: flag(FirstRunSetup.doneKey)),
                  "the window may switch Wisp to the kept model again")
            check(!flag(Permissions.screenRecordingPromptedKey) && !flag(Permissions.accessibilityPromptedKey),
                  "permission prompts are asked again")
        }
        for key in [FirstRunSetup.doneKey, FirstRunSetup.startedKey, FirstRunSetup.stepKey, FirstRunSetup.reopensKey,
                    FirstRunSetup.resumeKey, FirstRunSetup.aboutYouDecidedKey, FirstRunSetup.setupGuideShownKey,
                    FirstRunSetup.notchTipShownKey] {
            check(WispReset.firstRunMarkers.contains(key), "\(key) is a listed first-run marker")
        }
    }

    static func privacyResetIsWispOnly() {
        check(WispReset.tccutil == "/usr/bin/tccutil", "the system tccutil")
        check(WispReset.tccArguments(bundleID: "com.wisp.assistant") == ["reset", "All", "com.wisp.assistant"],
              "resets every service for Wisp's own bundle")
        for other in [nil, "", "com.apple.Terminal", "com.wisp.assistant.safari-extension", "com.wisp.assistant ",
                      "COM.WISP.ASSISTANT", "com.wisp"] as [String?] {
            check(WispReset.tccArguments(bundleID: other) == nil, "never for \(other ?? "nil")")
        }
    }

    static func relaunch() {
        let app = URL(fileURLWithPath: "/Applications/Wisp Test.app")
        guard let arguments = WispReset.relaunchArguments(pid: 4242, app: app) else {
            check(false, "an .app relaunches"); return
        }
        check(arguments.count == 5 && arguments[0] == "-c" && arguments[3] == "4242" && arguments[4] == app.path,
              "pid and path are arguments")
        check(!arguments[1].contains("Wisp Test") && arguments[1].contains("/usr/bin/open \"$2\""),
              "the script never interpolates the path")
        check(WispReset.relaunchArguments(pid: 1, app: URL(fileURLWithPath: "/tmp/WispApp")) == nil,
              "an unbundled build does not relaunch")
    }

    static func keychain() {
        let digest = String(repeating: "ab", count: 32)
        check(WispReset.isProviderKeyAccount("openai:" + digest), "a provider key goes")
        check(WispReset.isProviderKeyAccount("my:provider:" + digest), "names may hold a colon")
        for kept in ["local-omlx", "mini-inference", "mini-node", ":" + digest, "openai:" + digest.uppercased(),
                     "openai:" + digest.dropLast(), "openai"] {
            check(!WispReset.isProviderKeyAccount(kept), "\(kept) is not a provider key")
        }
    }

    static func serviceReplies() {
        typealias R = WispReset.ServiceReply
        let erased = R.parse(status: 200, body: ["committed": true, "erased": true, "problems": [[String: Any]]()])
        check(erased.kind == .erased && erased.relaunches, "200 committed and erased relaunches")
        let unfinished = R.parse(status: 200, body: ["committed": true, "erased": false,
                                                     "detail": "Google was signed out, but its files were not deleted."])
        check(unfinished.kind == .committed && unfinished.relaunches,
              "committed but unfinished still relaunches: the next start finishes it")
        check(R.parse(status: 503, body: ["detail": WispReset.erasedDetail]).kind == .erased,
              "only the distinct 'Wisp was reset.' 503 means the service's part is done")
        for other in ["Wisp is resetting.", "Credential recovery", ""] {
            let reply = R.parse(status: 503, body: ["detail": other])
            check(!reply.relaunches && reply.kind == .unknown, "503 '\(other)' is not a finished reset")
        }
        for status in [403, 409, 415, 422] {
            let reply = R.parse(status: status, body: ["detail": "No."])
            check(reply.kind == .refused && !reply.relaunches && reply.detail.hasSuffix("Nothing was reset."),
                  "\(status) refused before anything ran")
        }
        let notWritable = R.parse(status: 500, body: ["detail": "Couldn't start the reset (Wisp's folder isn't writable). Nothing was reset."])
        check(notWritable.kind == .refused, "the service's own pre-commit refusal")
        for unknown in [R.parse(status: 500, body: nil), R.parse(status: nil, body: nil),
                        R.parse(status: 200, body: ["ok": true])] {
            check(unknown.kind == .unknown && !unknown.relaunches && !unknown.detail.contains("Nothing was reset"),
                  "no answer, a crash or an uncommitted 200 never claims nothing was reset")
        }
        check(WispReset.refusal(corpusBusy: true, replying: false) != nil, "not during a corpus run")
        check(WispReset.refusal(corpusBusy: false, replying: true) != nil, "not while Wisp is answering")
        check(WispReset.refusal(corpusBusy: false, replying: false) == nil, "otherwise the card opens")

        // No answer: the marker on disk tells a committed reset from one that never started.
        let committed = R.afterNoAnswer(markerOnDisk: true)
        check(committed.kind == .committed && committed.relaunches && !committed.detail.contains("Nothing was reset"),
              "a marker on disk means the reset committed: Wisp restarts to finish it")
        let neverStarted = R.afterNoAnswer(markerOnDisk: false)
        check(neverStarted.kind == .refused && !neverStarted.relaunches
              && neverStarted.detail.contains("no reset started"), "no marker means no reset started")

        // The marker the service writes, and the notice the relaunch shows.
        typealias M = WispReset.Marker
        check(M.parse(Data(#"{"version":1,"id":"ab12","phase":"erased","problems":["Couldn't reach Google."]}"#.utf8))
              == M(id: "ab12", phase: "erased", problems: ["Couldn't reach Google."]), "an erased marker")
        check(M.parse(Data(#"{"version":1,"id":"ab12","phase":"first-pass"}"#.utf8))?.serviceDone == false,
              "the route's first pass is not done: only the restart pass writes erased")
        for junk in [Data(), Data("{".utf8), Data(#"{"phase":"erased","id":"a"}"#.utf8),
                     Data(#"{"version":2,"phase":"erased","id":"a"}"#.utf8),
                     Data(#"{"version":1,"phase":"done","id":"a"}"#.utf8),
                     Data(#"{"version":1,"phase":"erased","id":"../a"}"#.utf8),
                     Data(#"{"version":1,"phase":"erased"}"#.utf8)] {
            check(M.parse(junk) == nil, "junk is not a marker: \(String(decoding: junk, as: UTF8.self))")
        }
        check(WispReset.launchNotice(marker: M(id: "a", phase: "erased", problems: []), appProblems: []) == nil,
              "a clean reset says nothing")
        check(WispReset.launchNotice(marker: M(id: "a", phase: "erased", problems: ["A."]), appProblems: ["B."])
              == "Wisp was reset, but: A. B.", "problems from both parts are said once")
        let unfinishedText = WispReset.launchNotice(marker: M(id: "a", phase: "first-pass", problems: []), appProblems: [])
        check(unfinishedText?.hasPrefix(WispReset.unfinishedNotice) == true
              && unfinishedText?.contains("delete ~/.moe/.reset-pending") == true
              && unfinishedText?.contains("tries again at the next launch") == false,
              "an unfinished reset gives the way out instead of promising a retry")
        sharedMarkerCases()
        check(WispReset.Marker.parse(Data([0xEF, 0xBB, 0xBF]) + Data(#"{"version":1,"id":"a","phase":"erased"}"#.utf8))
              == nil, "a byte-order mark is refused, as the service refuses it")
        check(WispReset.appPartRan(markerID: "m1", record: nil, defaultsID: "m1"),
              "the defaults fallback alone means the app's part ran (its record couldn't be written)")
        check(WispReset.appPartRan(markerID: "m1", record: WispReset.AppDone(id: "m1", problems: []), defaultsID: nil),
              "the record alone means it ran")
        check(!WispReset.appPartRan(markerID: "m2", record: WispReset.AppDone(id: "m1", problems: []), defaultsID: "m1"),
              "a new reset runs the app's part again")
        let tcc = ["macOS didn't reset Wisp's permissions."]
        check(WispReset.appPartProblems(markerID: "m1", record: nil, defaultsID: "m1", defaultsProblems: tcc) == tcc,
              "when only the defaults prove the part ran, its problems come from them too")
        check(WispReset.appPartProblems(markerID: "m1", record: WispReset.AppDone(id: "m1", problems: ["R."]),
                                        defaultsID: "m1", defaultsProblems: tcc) == ["R."], "the record wins")
        check(WispReset.appPartProblems(markerID: "m2", record: WispReset.AppDone(id: "m1", problems: ["R."]),
                                        defaultsID: "m1", defaultsProblems: tcc).isEmpty,
              "another reset's problems are never said")
        check(WispReset.markerURL(home: "/Users/someone").path == "/Users/someone/.moe/.reset-pending",
              "the marker lives in ~/.moe")
        check(WispReset.appDoneURL(home: "/Users/someone").path == "/Users/someone/.moe/.reset-pending.app",
              "the app's record sits beside it (wisp_reset.APP_DONE)")
        markerFiles()

        // Wisp's own app files outside ~/.moe, and only for Wisp's own bundle.
        check(WispReset.appFiles(home: "/Users/someone", bundleID: WispReset.bundleID) == [
            "/Users/someone/Library/Caches/com.wisp.assistant",
            "/Users/someone/Library/HTTPStorages/com.wisp.assistant",
            "/Users/someone/Library/Application Support/Wisp/battery_history.json"], "the app's caches")
        check(WispReset.appFiles(home: "/Users/someone", bundleID: nil).isEmpty
              && WispReset.appFiles(home: "/Users/someone", bundleID: "com.apple.Terminal").isEmpty,
              "never another bundle's files")
        check(WispReset.route == "assistant/reset_wisp" && WispReset.confirmPhrase == "reset wisp",
              "the route and phrase match service/wisp_reset.py")
    }

    /// N4: the same cases service/wisp_reset.py checks (tests/test_wisp_reset.py),
    /// so the app never acts on, or waits for, a marker the service ignores.
    static func sharedMarkerCases() {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        guard let data = try? Data(contentsOf: root.appendingPathComponent("fixtures/reset_markers.json")),
              let doc = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let cases = doc["cases"] as? [[String: Any]], cases.count >= 49 else {
            check(false, "read tests/fixtures/reset_markers.json"); return
        }
        for item in cases {
            guard let name = item["name"] as? String, let text = item["text"] as? String,
                  let valid = item["valid"] as? Bool else { check(false, "a fixture case"); continue }
            check((WispReset.Marker.parse(Data(text.utf8)) != nil) == valid,
                  "the app judges the shared marker case \"\(name)\" like the service")
        }
    }

    /// The marker and the app's record read from real files: only a regular file
    /// this user owns counts; a link, a folder or junk is ignored.
    static func markerFiles() {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-reset-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let marker = dir.appendingPathComponent(".reset-pending").path
        check(WispReset.readMarker(marker) == .absent, "no marker")
        try? Data(#"{"version":1,"id":"c0ffee","phase":"first-pass","problems":[]}"#.utf8)
            .write(to: URL(fileURLWithPath: marker))
        check(WispReset.readMarker(marker) == .valid(WispReset.Marker(id: "c0ffee", phase: "first-pass", problems: [])),
              "a marker Wisp wrote")
        try? Data("junk".utf8).write(to: URL(fileURLWithPath: marker))
        if case .invalid = WispReset.readMarker(marker) {} else { check(false, "junk content is ignored") }
        try? FileManager.default.removeItem(atPath: marker)
        let target = dir.appendingPathComponent("elsewhere.json").path
        try? Data(#"{"version":1,"id":"c0ffee","phase":"erased"}"#.utf8).write(to: URL(fileURLWithPath: target))
        try? FileManager.default.createSymbolicLink(atPath: marker, withDestinationPath: target)
        if case .invalid = WispReset.readMarker(marker) {} else { check(false, "a link is ignored") }
        check(WispReset.removeOwnedFile(marker) == false, "a link is not removed, and says so")
        check(FileManager.default.fileExists(atPath: target), "removing never follows a link")
        try? FileManager.default.removeItem(atPath: marker)
        try? FileManager.default.createDirectory(atPath: marker, withIntermediateDirectories: false)
        if case .invalid = WispReset.readMarker(marker) {} else { check(false, "a folder is ignored") }
        try? FileManager.default.removeItem(atPath: marker)

        // The app part runs once per marker: its record holds the marker's id.
        let record = dir.appendingPathComponent(".reset-pending.app").path
        check(WispReset.readAppDone(record) == nil, "no record yet")
        check(WispReset.writeAppDone(WispReset.AppDone(id: "c0ffee", problems: ["P."]), to: record),
              "the record is written")
        check(WispReset.readAppDone(record) == WispReset.AppDone(id: "c0ffee", problems: ["P."]), "the record")
        check(WispReset.removeOwnedFile(record), "removing reports success")
        check(!FileManager.default.fileExists(atPath: record), "the record is removed")
        check(WispReset.removeOwnedFile(record), "an absent file counts as removed")
        check(!WispReset.writeAppDone(WispReset.AppDone(id: "c0ffee", problems: []),
                                      to: dir.appendingPathComponent("missing/folder/record").path),
              "a record that can't be written says so")
    }

    static func source(_ path: String) -> String {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        guard let text = try? String(contentsOf: root.appendingPathComponent(path), encoding: .utf8) else {
            check(false, "read \(path)"); return ""
        }
        return text
    }

    static func sourceContracts() {
        let card = source("app/Sources/WispApp/ResetWisp.swift")
        let destructive = "Button(\"Reset Wisp\", role: .destructive) { model.confirmReset() }"
        check(card.contains(destructive), "Reset Wisp is a destructive button")
        let after = card.components(separatedBy: destructive)[1].prefix(200)
        check(!after.contains("keyboardShortcut"), "Reset Wisp has no keyboard shortcut")
        check(!card.contains(".defaultAction") && !card.contains(".return"), "Return never confirms the card")
        check(card.contains("WispReset.clearDefaults(UserDefaults.standard, domain: bundleID)"),
              "the launch step clears Wisp's defaults domain")
        check(card.contains("WispReset.tccArguments(bundleID: bundleID)")
              && card.components(separatedBy: "Process()").count == 2
              && card.components(separatedBy: "URL(fileURLWithPath: WispReset.tccutil)").count == 2,
              "tccutil runs only with WispReset's Wisp-only arguments")
        check(card.contains("removeAllDeliveredNotifications()"), "delivered notifications go")

        let model = source("app/Sources/WispApp/OverlayModel.swift")
        check(model.contains("case .resetwisp: openResetCard()"), "/resetwisp opens the card, nothing more")
        check(model.contains("guard resetCard == .confirm, let onResetWisp else { return }"),
              "the reset runs only from an open confirmation")
        check(model.contains("guard resetCard != .running else { return }"), "a running reset keeps its card")
        check(model.contains("guard !NotchCommand.isReset(s) else { return }"), "a starter can never open the card")

        for name in ["AppDelegate.swift", "OverlayView.swift", "WispClient.swift", "QuickSettingsView.swift",
                     "NotchCommand.swift"] {
            check(!source("app/Sources/WispApp/" + name).contains("confirmReset()"),
                  "only the card calls confirmReset (\(name))")
        }
        let delegate = source("app/Sources/WispApp/AppDelegate.swift")
        check(!delegate.contains("tccutil") && !delegate.contains("clearDefaults"),
              "the delegate resets nothing itself; the marker-guarded launch step does")
        check(delegate.contains("case .valid(let marker):\n            resetPending = true\n")
              && delegate.contains("            if !marker.serviceDone && !WispReset.appPartRan(\n"
                                + "                markerID: marker.id, record: WispReset.readAppDone(),\n"
                                + "                defaultsID: UserDefaults.standard.string(forKey: WispReset.appPartDoneKey)) {\n"
                                + "                let problems = WispResetLaunch.run(bundleID: Bundle.main.bundleIdentifier)\n"
                                + "                UserDefaults.standard.set(marker.id, forKey: WispReset.appPartDoneKey)\n"
                                + "                UserDefaults.standard.set(problems, forKey: WispReset.appPartProblemsKey)\n"
                                + "                WispReset.writeAppDone(WispReset.AppDone(id: marker.id, problems: problems))"),
              "a launch with a valid marker runs the app's part once per marker, before reading settings")
        check(delegate.contains("if model.debugMode { model.debugMode = false }"),
              "a reset never keeps Debug Mode on (the model read it before the defaults were cleared)")
        check(delegate.contains("case .invalid(let reason):") && delegate.contains("WispReset.ignoredMarkerReportedKey"),
              "an invalid marker is ignored and reported once")
        let launchStart = delegate.components(separatedBy: "    private func launch() {")[1]
        check(launchStart.range(of: "WispResetLaunch.run")!.lowerBound
              < launchStart.range(of: "FirstRunSetup.shouldShow")!.lowerBound,
              "the defaults are cleared before setup decides whether to show")
        check(delegate.contains("case .valid(let marker) where marker.serviceDone:\n"
                                + "                    let appProblems = Self.resetAppProblems(marker)\n"),
              "the marker is removed only once the restart pass says erased")
        let finish = delegate.components(separatedBy: "case .valid(let marker) where marker.serviceDone:")[1]
        check(finish.contains("let markerGone = WispReset.removeOwnedFile(WispReset.markerURL().path)\n"
                              + "                    if markerGone, !WispReset.removeOwnedFile(WispReset.appDoneURL().path) {"),
              "the marker goes first; the app's record only after it is gone")
        check(finish.contains("WispReset.markerStuckNotice"), "a marker that won't delete is said")
        check(finish.contains("UserDefaults.standard.removeObject(forKey: WispReset.appPartDoneKey)")
              && finish.contains("UserDefaults.standard.removeObject(forKey: WispReset.appPartProblemsKey)"),
              "the defaults fallback goes with the marker")
        check(delegate.contains("UserDefaults.standard.set(problems, forKey: WispReset.appPartProblemsKey)"),
              "the app part's problems are kept in the defaults fallback too")
        check(!delegate.contains("appProblems: WispReset.readAppDone()?.problems")
              && !delegate.contains("let appProblems = WispReset.readAppDone()?.problems"),
              "every notice reads the app part's problems through the fallback")
        check(!card.contains("try? data.write(to: URL(fileURLWithPath: path)"), "a failed record write is logged")
        check(delegate.contains("reply = .afterNoAnswer(markerOnDisk: true)"),
              "no answer: the marker on disk decides what Wisp says")
        check(delegate.contains("guard reply.relaunches else { return reply.detail }"),
              "only a committed reset quits and relaunches")
        check(delegate.contains("return await self.performWispReset()")
              && delegate.components(separatedBy: "performWispReset()").count == 3,
              "the reset is reached only from the card's callback")
    }
}
