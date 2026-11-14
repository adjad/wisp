import AppKit
import Darwin
import Foundation
import Security

/// The Intel preview's one-time first-launch cleanup. An earlier Wisp on the same Mac
/// shares this bundle id, so its saved state skipped onboarding, its ~/.moe held an old
/// cloud profile, and a copy still running made this one yield. On the first launch of
/// an Intel build with no bundled engine, this quits earlier Wisp apps, stops a leftover
/// Wisp backend, moves ~/.moe aside (never deletes it) and resets this app's defaults.
/// A file marker outside the defaults records that it finished, so it never repeats.
enum IntelFirstLaunch {
    /// Recorded after the reset (the reset would otherwise clear it): what the cleanup did.
    static let summaryKey = "wisp.intelFirstLaunch.summary"
    /// Set only when ~/.moe was moved aside; names the backup folder (no path).
    static let backupKey = "wisp.intelFirstLaunch.backup"
    static let markerFolder = "Library/Application Support/Wisp"
    static let markerName = "intel-first-launch-done"
    static let backupPrefix = ".moe-backup-"
    static let port = 8765

    /// Intel build, no bundled engine, not done before. Apple silicon builds and any
    /// build with an engine never run it.
    static func shouldRun(compiledForIntel: Bool, engineBundled: Bool, alreadyDone: Bool) -> Bool {
        compiledForIntel && !engineBundled && !alreadyDone
    }

    /// The folder ~/.moe moves to: `.moe-backup-yyyyMMdd-HHmmss`.
    static func backupName(for date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return backupPrefix + formatter.string(from: date)
    }

    /// An earlier Wisp's own backend: a Python inside `Wisp*.app/Contents/Resources/backend/`
    /// (also under macOS's translocation folder). Anything else is not touched.
    static func isWispBackend(executablePath path: String) -> Bool {
        guard path.hasPrefix("/"), !path.contains("/../") else { return false }
        let parts = path.split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        guard let last = parts.last, last.hasPrefix("python") else { return false }
        for index in parts.indices where parts[index].hasPrefix("Wisp") && parts[index].hasSuffix(".app") {
            let rest = Array(parts[(index + 1)...])
            if rest.count > 3, rest.starts(with: ["Contents", "Resources", "backend"]) { return true }
        }
        return false
    }

    static func markerURL(home: String = NSHomeDirectory()) -> URL {
        URL(fileURLWithPath: home).appendingPathComponent(markerFolder).appendingPathComponent(markerName)
    }

    static var engineBundled: Bool {
        guard let resources = Bundle.main.resourceURL else { return false }
        return FileManager.default.fileExists(atPath: resources.appendingPathComponent("engine").path)
    }

    /// Runs the cleanup when it is due. Synchronous; call it first in launch.
    static func runIfNeeded() {
        let done = FileManager.default.fileExists(atPath: markerURL().path)
        guard shouldRun(compiledForIntel: IntelCloudPreset.compiledForIntel,
                        engineBundled: engineBundled, alreadyDone: done) else { return }
        // State from an earlier Intel preview of this line (its cloud connection) is
        // kept: only that build writes remote_parity, so the key needn't be entered again.
        if leftoverIsFromIntelLine(configText: try? String(contentsOfFile: NSHomeDirectory() + "/.moe/config.yaml",
                                                           encoding: .utf8)) {
            writeMarker(summary: "Intel first launch: kept the earlier Intel preview's settings")
            return
        }
        run()
    }

    /// Whether ~/.moe/config.yaml was written by an Intel preview of this line: it
    /// turns on the cloud model for everything (`remote_parity: true`), which no older
    /// Wisp wrote. Pure.
    static func leftoverIsFromIntelLine(configText: String?) -> Bool {
        guard let configText else { return false }
        return configText.split(whereSeparator: \.isNewline).contains { line in
            let parts = line.split(separator: ":", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
            return parts.count == 2 && parts[0] == "remote_parity"
                && parts[1].split(separator: "#").first?.trimmingCharacters(in: .whitespaces) == "true"
        }
    }

    static func writeMarker(summary: String, home: String = NSHomeDirectory()) {
        let marker = markerURL(home: home)
        try? FileManager.default.createDirectory(at: marker.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? Data(summary.utf8).write(to: marker, options: .atomic)
        NSLog("%@", summary)
    }

    static func run(home: String = NSHomeDirectory(), now: Date = Date()) {
        let fm = FileManager.default
        let bundleID = Bundle.main.bundleIdentifier ?? ""
        let selfPID = ProcessInfo.processInfo.processIdentifier

        // a. Other running Wisp apps (this bundle id only): polite quit, then forced.
        var quit = 0
        if !bundleID.isEmpty {
            let others = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
                .filter { $0.processIdentifier != selfPID }
            for app in others where !app.isTerminated { app.terminate(); quit += 1 }
            let deadline = Date().addingTimeInterval(5)
            while others.contains(where: { !$0.isTerminated }), Date() < deadline {
                RunLoop.current.run(until: Date().addingTimeInterval(0.1))
            }
            for app in others where !app.isTerminated { app.forceTerminate() }
            let forced = Date().addingTimeInterval(2)
            while others.contains(where: { !$0.isTerminated }), Date() < forced {
                RunLoop.current.run(until: Date().addingTimeInterval(0.1))
            }
        }

        // b. A leftover Wisp backend on the port. Strangers are left alone.
        var stoppedBackend = false
        for listener in PortGuard.listeners(port: port) ?? []
        where listener.pid != selfPID && isWispBackend(executablePath: listener.path) {
            if kill(listener.pid, SIGTERM) == 0 { stoppedBackend = true }
        }
        if stoppedBackend {
            let deadline = Date().addingTimeInterval(5)
            while !((PortGuard.listeners(port: port) ?? []).filter({ isWispBackend(executablePath: $0.path) }).isEmpty),
                  Date() < deadline {
                Thread.sleep(forTimeInterval: 0.2)
            }
        }

        // c. ~/.moe moves aside, never deleted.
        var backup: String?
        var moveFailed = false
        let moe = URL(fileURLWithPath: home).appendingPathComponent(".moe")
        if fm.fileExists(atPath: moe.path) {
            let name = backupName(for: now)
            do {
                try fm.moveItem(at: moe, to: URL(fileURLWithPath: home).appendingPathComponent(name))
                backup = name
            } catch {
                moveFailed = true
            }
        }

        // d. This app's defaults start over (the same clearing /resetwisp does). Keychain untouched.
        if bundleID == WispReset.bundleID {
            WispReset.clearDefaults(UserDefaults.standard, domain: bundleID)
        } else if !bundleID.isEmpty {
            UserDefaults.standard.removePersistentDomain(forName: bundleID)
            UserDefaults.standard.synchronize()
        }

        // e. Summary, then the marker (last, so an interrupted cleanup runs again).
        var parts = ["\(quit) earlier Wisp app\(quit == 1 ? "" : "s") quit",
                     stoppedBackend ? "old backend stopped" : "no old backend"]
        if let backup { parts.append("old data moved to ~/\(backup)") }
        else if moveFailed { parts.append("old data could not be moved and was left in place") }
        else { parts.append("no old data") }
        let summary = "Intel first launch: " + parts.joined(separator: ", ")
        let defaults = UserDefaults.standard
        defaults.set(summary, forKey: summaryKey)
        if let backup { defaults.set(backup, forKey: backupKey) }
        defaults.synchronize()
        NSLog("%@", summary)
        let marker = markerURL(home: home)
        try? fm.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? Data(summary.utf8).write(to: marker, options: .atomic)
    }

    // MARK: Permissions, once per signing identity

    /// macOS keeps privacy permissions (TCC) for the app's designated requirement. An
    /// ad-hoc build's requirement is its code hash, so to TCC every such build is a new
    /// app and the grants or denials an earlier one left under the same bundle id stay
    /// behind: System Settings shows Wisp switched on while macOS refuses this copy, and
    /// a remembered "no" keeps a question (Calendar's) from appearing. Intel previews are
    /// now signed with one stable identity ("Wisp Dev"), so the requirement stays the
    /// same from update to update and grants carry over. Wisp's own permissions (this
    /// bundle id only) are reset only when the requirement changes: once when moving
    /// to the stable identity, and on every update of an ad-hoc build.
    static let permissionsMarkerName = "intel-permissions-requirement"

    /// This running app's designated requirement as text; nil when it can't be read.
    static func signingRequirement() -> String? {
        var code: SecCode?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code else { return nil }
        var staticCode: SecStaticCode?
        guard SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode else { return nil }
        var requirement: SecRequirement?
        guard SecCodeCopyDesignatedRequirement(staticCode, [], &requirement) == errSecSuccess,
              let requirement else { return nil }
        var text: CFString?
        guard SecRequirementCopyString(requirement, [], &text) == errSecSuccess, let text else { return nil }
        return text as String
    }

    /// Intel build, no bundled engine, a known requirement that differs from the recorded one. Pure.
    static func permissionsResetDue(compiledForIntel: Bool, engineBundled: Bool,
                                    recorded: String?, current: String?) -> Bool {
        guard compiledForIntel, !engineBundled, let current, !current.isEmpty else { return false }
        return recorded?.trimmingCharacters(in: .whitespacesAndNewlines) != current
    }

    /// Saved answers that would show a permission as given after the reset. Pure.
    static func staleAnswerKeys(in keys: [String]) -> [String] {
        keys.filter {
            $0 == Permissions.screenRecordingPromptedKey || $0 == Permissions.accessibilityPromptedKey
                || $0.hasPrefix("wisp.automation.answer.")
        }
    }

    static func resetPermissionsIfNewBuild(home: String = NSHomeDirectory()) {
        let marker = URL(fileURLWithPath: home).appendingPathComponent(markerFolder)
            .appendingPathComponent(permissionsMarkerName)
        let recorded = try? String(contentsOf: marker, encoding: .utf8)
        let current = signingRequirement()
        guard permissionsResetDue(compiledForIntel: IntelCloudPreset.compiledForIntel, engineBundled: engineBundled,
                                  recorded: recorded, current: current), let current,
              let arguments = WispReset.tccArguments(bundleID: Bundle.main.bundleIdentifier) else { return }
        let tcc = Process()
        tcc.executableURL = URL(fileURLWithPath: WispReset.tccutil)
        tcc.arguments = arguments
        tcc.standardOutput = FileHandle.nullDevice
        tcc.standardError = FileHandle.nullDevice
        guard (try? tcc.run()) != nil else {
            NSLog("Intel permissions: tccutil could not be started; earlier grants left as they were")
            return
        }
        // Bounded: launch never waits on a slow tccd for more than a few seconds.
        let deadline = Date().addingTimeInterval(10)
        while tcc.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if tcc.isRunning {
            tcc.terminate()
            NSLog("Intel permissions: tccutil reset took too long; it runs again next launch")
            return
        }
        // A failed reset is tried again on the next launch: the marker is written only after success.
        guard tcc.terminationStatus == 0 else {
            NSLog("Intel permissions: tccutil reset failed (status %d)", tcc.terminationStatus)
            return
        }
        let defaults = UserDefaults.standard
        for key in staleAnswerKeys(in: Array(defaults.dictionaryRepresentation().keys)) {
            defaults.removeObject(forKey: key)
        }
        defaults.synchronize()
        try? FileManager.default.createDirectory(at: marker.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        try? Data(current.utf8).write(to: marker, options: .atomic)
        NSLog("Intel permissions: reset Wisp's own grants for a new signature")
    }

    /// The first step's line when data was moved aside; nil otherwise.
    static func onboardingNote(backup: String?) -> String? {
        guard let backup, backup.hasPrefix(backupPrefix) else { return nil }
        return "Wisp moved the data from an earlier Wisp to ~/\(backup) so it starts fresh."
    }
}
