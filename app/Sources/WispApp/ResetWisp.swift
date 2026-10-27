import Foundation
import Security
import SwiftUI
import UserNotifications

/// /resetwisp: erase everything Wisp holds except the model, so the next launch
/// is a true first run (the onboarding window at step 1, Model).
///
/// Typed only: the notch opens a confirmation card and nothing happens until the
/// person clicks Reset Wisp (a destructive button that Return never presses).
/// The model and the agent have no way to it: it is not a tool, the service
/// route admits only this app (service/wisp_reset.py), and a starter or other
/// programmatic prompt can't open the card (OverlayModel.pick).
///
/// Order (AppDelegate.performWispReset, then finishPendingReset at the next launch):
/// 1. The service seals itself and commits the reset by writing ~/.moe/.reset-pending
///    before anything destructive; it stops its work, signs out of Google and
///    deletes its data. From the commit on, the reset always finishes.
/// 2. This app stops the engine and the service as on quit and relaunches.
/// 3. The next service start sees the marker and erases again (and the engine's
///    prompt cache, now that the engine is stopped).
/// 4. The relaunched app sees the marker before reading any setting, clears its
///    defaults domain, its provider keys, its privacy grants, its notifications and
///    caches (WispResetLaunch.run), and opens setup at step 1.
/// 5. It removes the marker last, once the service says its part is done.
/// A reset cut short anywhere after step 1 (a crash, a quit, a timeout) is finished
/// the same way by the next launch, so no launch is left with half a reset.
enum WispReset {
    /// The only bundle whose defaults and privacy grants a reset touches.
    static let bundleID = "com.wisp.assistant"
    /// The service route, and the phrase it requires (kept in WispClient).
    static var route: String { WispClient.resetRoute }
    static var confirmPhrase: String { WispClient.resetConfirmPhrase }
    /// The only 503 that means "the service already did its part" (wisp_reset.ERASED).
    static let erasedDetail = "Wisp was reset."
    /// The commit marker in Wisp's home (wisp_reset.MARKER).
    static let markerName = ".reset-pending"
    static func markerURL(home: String = NSHomeDirectory()) -> URL {
        URL(fileURLWithPath: home).appendingPathComponent(".moe").appendingPathComponent(markerName)
    }

    static let deletes = [
        "Google: every connected account is signed out (revoked with Google when online) and Wisp's tokens are deleted",
        "Mail, Messages, Notes, Calendar and Reminders copies Wisp keeps",
        "Chats, memories and facts, About you, and what Wisp learned about your routines",
        "Research, Today and Brief state, the outbox, audit log and diagnostics",
        "Permission grants, added tools and skills, cloud and provider keys",
        "Wisp's settings and its macOS permissions, so setup starts over",
    ]
    static let keeps = [
        "The model and Wisp's engine, so nothing downloads again",
        "Everything outside Wisp: your own oMLX app and its cache, copies on a Mac mini or Air node, and the iPhone app's cache",
    ]
    static let internetAccountsNote =
        "Your accounts in System Settings ▸ Internet Accounts, Mail, Calendar, Notes and Messages are not touched. Wisp only loses its access to them."

    /// Every first-run and "seen" marker this app keeps. Clearing the whole
    /// defaults domain removes them; each is also removed by name, so a domain
    /// that can't be removed as one (an unbundled build) still starts over.
    static let firstRunMarkers = [
        FirstRunSetup.doneKey, FirstRunSetup.startedKey, FirstRunSetup.stepKey, FirstRunSetup.reopensKey,
        FirstRunSetup.resumeKey, FirstRunSetup.aboutYouDecidedKey,
        FirstRunSetup.setupGuideShownKey, FirstRunSetup.notchTipShownKey,
        Permissions.screenRecordingPromptedKey, Permissions.accessibilityPromptedKey,
        "wisp.setup.ready", "WispTodayWindowDidShow", "WispIgnoredCaughtPlans",
        "wisp.contactsEnabled", "wisp.contactsRevision", "wisp.contactsPreferenceChanged",
        "wisp.browserHistoryEnabled", "wisp.browserHistoryRevision", "wisp.browserHistoryPreferenceChanged",
        "wisp.browserBridge.enabledProfiles.v1", "wisp.mailAccountLabels.identity.v1",
        "wisp.shuttingDownPID", "wisp.backendStartFailed", "wisp.backendRefused", "wisp.restartBackend",
    ]
    /// Per-account "Not now" notes in Settings ▸ Google (one key per account).
    static let markerPrefixes = ["wisp.", "Wisp"]

    /// Removes this app's defaults: the whole domain when it is Wisp's own, then
    /// every marker and every key Wisp writes by name.
    static func clearDefaults(_ defaults: WispDefaultsStore, domain: String?) {
        var keys = Set(firstRunMarkers)
        if let domain, let stored = defaults.persistentDomain(forName: domain) {
            keys.formUnion(stored.keys)
            defaults.removePersistentDomain(forName: domain)
        }
        keys.formUnion(defaults.dictionaryRepresentation().keys.filter { key in
            markerPrefixes.contains { key.hasPrefix($0) }
        })
        for key in keys { defaults.removeObject(forKey: key) }
        defaults.synchronize()
    }

    /// `tccutil reset All <bundle>`, only ever for Wisp's own bundle id; nil for
    /// any other bundle (an unbundled developer build has none), so it never runs.
    static let tccutil = "/usr/bin/tccutil"
    static func tccArguments(bundleID: String?) -> [String]? {
        bundleID == Self.bundleID ? ["reset", "All", Self.bundleID] : nil
    }

    /// Waits (bounded) for this process to end, then opens Wisp again. Arguments,
    /// never interpolated into the script.
    static let relaunchShell = "/bin/sh"
    static func relaunchArguments(pid: Int32, app: URL) -> [String]? {
        guard app.pathExtension == "app", app.isFileURL else { return nil }
        let script = "i=0; while /bin/kill -0 \"$1\" 2>/dev/null && [ $i -lt 150 ]; do "
            + "/bin/sleep 0.2; i=$((i+1)); done; exec /usr/bin/open \"$2\""
        return ["-c", script, "wisp-relaunch", String(pid), app.path]
    }

    /// Cloud and local-provider keys Wisp saved (SettingsView's CloudCredentialStore:
    /// "<name>:<sha256 of the base URL>"). The same Keychain service also holds the
    /// engine's own key (BackendCredentials: local-omlx, mini-inference, mini-node),
    /// which the model needs and a reset keeps.
    static func isProviderKeyAccount(_ account: String) -> Bool {
        guard let colon = account.lastIndex(of: ":") else { return false }
        let digest = account[account.index(after: colon)...]
        return colon != account.startIndex && digest.count == 64
            && digest.unicodeScalars.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }

    /// What the service answered.
    struct ServiceReply: Equatable {
        enum Kind: Equatable {
            /// Committed and the service's files are gone: relaunch.
            case erased
            /// Committed (the marker is on disk) but not finished: relaunch, and the
            /// next start finishes it.
            case committed
            /// Refused before anything destructive ran.
            case refused
            /// No answer: whether it started is unknown.
            case unknown
        }
        let kind: Kind
        let detail: String

        /// Reset steps go ahead (quit and relaunch) only for these.
        var relaunches: Bool { kind == .erased || kind == .committed }

        static let unknownDetail = "Wisp didn't confirm the reset, so it may have started. Quit and reopen "
            + "Wisp: a reset that started finishes then."

        /// No answer at all: the marker on disk tells a committed reset from one that
        /// never started (the service writes it before anything destructive).
        static func afterNoAnswer(markerOnDisk: Bool) -> ServiceReply {
            markerOnDisk
                ? ServiceReply(kind: .committed, detail: "The reset started but Wisp didn't confirm it; "
                               + "Wisp restarts to finish it.")
                : ServiceReply(kind: .refused, detail: "Wisp's service didn't answer and no reset started. "
                               + "Nothing was reset.")
        }

        static func parse(status: Int?, body: [String: Any]?) -> ServiceReply {
            guard let status else { return ServiceReply(kind: .unknown, detail: unknownDetail) }
            let detail = body?["detail"] as? String ?? ""
            if status == 200, body?["committed"] as? Bool == true {
                return ServiceReply(kind: body?["erased"] as? Bool == true ? .erased : .committed, detail: detail)
            }
            // An earlier attempt erased the service and lost its answer.
            if status == 503 && detail == erasedDetail { return ServiceReply(kind: .erased, detail: "") }
            // The service refuses before its commit with a 4xx, or says so itself.
            if (400..<500).contains(status) || detail.hasSuffix("Nothing was reset.") {
                let said = detail.isEmpty ? "Wisp's service refused the reset (\(status))." : detail
                return ServiceReply(kind: .refused, detail: said.hasSuffix("Nothing was reset.") ? said
                                    : said + " Nothing was reset.")
            }
            return ServiceReply(kind: .unknown, detail: unknownDetail)
        }
    }

    /// The marker's contents, as the service wrote them (wisp_reset.write_marker).
    struct Marker: Equatable {
        let id: String
        /// "started", then "first-pass" after the route; only the service's restart
        /// pass (wisp_reset.resume_pending) writes "erased".
        let phase: String
        let problems: [String]
        var serviceDone: Bool { phase == "erased" }

        /// Nil unless it is a marker Wisp wrote: version 1, a known phase, an id.
        static func parse(_ data: Data) -> Marker? {
            guard let doc = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  doc["version"] as? Int == 1,
                  let phase = doc["phase"] as? String, ["started", "first-pass", "erased"].contains(phase),
                  let id = doc["id"] as? String, !id.isEmpty, id.count <= 64,
                  id.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) && $0.isASCII }) else {
                return nil
            }
            return Marker(id: id, phase: phase,
                          problems: (doc["problems"] as? [Any] ?? []).compactMap { $0 as? String })
        }
    }

    /// A file in Wisp's home read the way the service reads it: only a regular file
    /// this user owns, never through a link.
    enum OwnedFile: Equatable {
        case absent
        case odd(String)
        case data(Data)
    }

    static func readOwnedFile(_ path: String) -> OwnedFile {
        var info = stat()
        guard lstat(path, &info) == 0 else { return errno == ENOENT ? .absent : .odd("unreadable") }
        guard info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid() else {
            return .odd("not a regular file owned by this user")
        }
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { return .odd("unreadable") }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        guard let data = try? handle.read(upToCount: 65536) else { return .odd("unreadable") }
        return .data(data)
    }

    enum MarkerState: Equatable {
        case absent
        /// Not a marker Wisp wrote (a link, a folder, someone else's file, junk):
        /// ignored, never acted on, and reported once.
        case invalid(String)
        case valid(Marker)
    }

    static func readMarker(_ path: String = markerURL().path) -> MarkerState {
        switch readOwnedFile(path) {
        case .absent: return .absent
        case .odd(let reason): return .invalid(reason)
        case .data(let data):
            guard let marker = Marker.parse(data) else { return .invalid("not a Wisp reset marker") }
            return .valid(marker)
        }
    }

    /// This app's record that it ran its part for one marker (wisp_reset.APP_DONE):
    /// the marker's id and what didn't work. The part runs once per marker.
    static func appDoneURL(home: String = NSHomeDirectory()) -> URL {
        URL(fileURLWithPath: home).appendingPathComponent(".moe").appendingPathComponent(markerName + ".app")
    }

    struct AppDone: Equatable {
        let id: String
        let problems: [String]
    }

    static func readAppDone(_ path: String = appDoneURL().path) -> AppDone? {
        guard case .data(let data) = readOwnedFile(path),
              let doc = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = doc["id"] as? String else { return nil }
        return AppDone(id: id, problems: (doc["problems"] as? [Any] ?? []).compactMap { $0 as? String })
    }

    /// False (and logged) when the record couldn't be written: the app's part then
    /// runs again at the next launch, until the service's restart pass says "erased".
    @discardableResult
    static func writeAppDone(_ done: AppDone, to path: String = appDoneURL().path) -> Bool {
        do {
            let data = try JSONSerialization.data(withJSONObject: ["id": done.id, "problems": done.problems])
            try data.write(to: URL(fileURLWithPath: path), options: [.atomic])
            return true
        } catch {
            NSLog("%@", "Wisp reset: couldn't record the app's part (\((error as NSError).domain) \((error as NSError).code))")
            return false
        }
    }

    /// Deletes a file only when it is a regular file this user owns. True when it
    /// is gone (or was never there); false when it is still there.
    @discardableResult
    static func removeOwnedFile(_ path: String) -> Bool {
        switch readOwnedFile(path) {
        case .absent: return true
        case .odd: return false
        case .data: return unlink(path) == 0 || errno == ENOENT
        }
    }

    static let markerStuckNotice = "Wisp couldn't remove its reset marker (~/.moe/.reset-pending); "
        + "delete it to stop this message."

    /// The notice the relaunched Wisp shows once, or nil when all went well.
    static func launchNotice(marker: Marker, appProblems: [String]) -> String? {
        let problems = marker.problems + appProblems
        if !marker.serviceDone {
            return "Wisp couldn't finish deleting its data and tries again at the next launch."
                + (problems.isEmpty ? "" : " " + problems.joined(separator: " "))
        }
        return problems.isEmpty ? nil : "Wisp was reset, but: " + problems.joined(separator: " ")
    }

    static let ignoredMarkerNotice = "Wisp found a reset marker it didn't write (~/.moe/.reset-pending) and ignored it."
    /// Set once an ignored marker was reported, so it is said only once.
    static let ignoredMarkerReportedKey = "wisp.reset.ignoredMarkerReported"

    /// Wisp's own app files outside ~/.moe that a reset removes: its URL cache and
    /// cookies, and the battery graph. Only for Wisp's own bundle id.
    static func appFiles(home: String, bundleID: String?) -> [String] {
        guard bundleID == Self.bundleID else { return [] }
        return ["Library/Caches/\(Self.bundleID)", "Library/HTTPStorages/\(Self.bundleID)",
                "Library/Application Support/Wisp/battery_history.json"]
            .map { (home as NSString).appendingPathComponent($0) }
    }

    /// Why the card can't open now, or nil.
    static func refusal(corpusBusy: Bool, replying: Bool) -> String? {
        if corpusBusy { return "Stop the corpus run before resetting Wisp." }
        if replying { return "Wait for Wisp to finish answering, then type /resetwisp again." }
        return nil
    }

}

/// What clearDefaults needs from UserDefaults, so its checks can run on an
/// in-memory store instead of writing a preferences file.
protocol WispDefaultsStore: AnyObject {
    func persistentDomain(forName domainName: String) -> [String: Any]?
    func removePersistentDomain(forName domainName: String)
    func dictionaryRepresentation() -> [String: Any]
    func removeObject(forKey defaultName: String)
    @discardableResult func synchronize() -> Bool
}

extension UserDefaults: WispDefaultsStore {}

/// The app's part of a committed reset, run first thing at launch while the marker
/// is on disk (AppDelegate.launch), before any setting is read. Every step is
/// idempotent, so a launch cut short just runs them again.
enum WispResetLaunch {
    /// Returns sentences for the notice about what didn't work, if anything.
    static func run(bundleID: String?) -> [String] {
        var problems: [String] = []
        // Every first-run marker goes, so setup opens at step 1.
        WispReset.clearDefaults(UserDefaults.standard, domain: bundleID)
        _ = WispResetKeychain.removeProviderKeys()
        try? BrowserBridgeCredentials.removeAll()
        // Wisp's own macOS privacy grants, for its own bundle id only.
        if let arguments = WispReset.tccArguments(bundleID: bundleID) {
            let tcc = Process()
            tcc.executableURL = URL(fileURLWithPath: WispReset.tccutil)
            tcc.arguments = arguments
            tcc.standardOutput = FileHandle.nullDevice
            tcc.standardError = FileHandle.nullDevice
            if (try? tcc.run()) != nil {
                tcc.waitUntilExit()
                if tcc.terminationStatus != 0 {
                    problems.append("macOS didn't reset Wisp's permissions; remove Wisp under System Settings ▸ Privacy & Security to start them over.")
                }
            } else {
                problems.append("macOS didn't reset Wisp's permissions; remove Wisp under System Settings ▸ Privacy & Security to start them over.")
            }
        }
        if bundleID != nil {
            // Wisp's own notifications: delivered ones and any still scheduled.
            let center = UNUserNotificationCenter.current()
            center.removeAllDeliveredNotifications()
            center.removeAllPendingNotificationRequests()
        }
        for path in WispReset.appFiles(home: NSHomeDirectory(), bundleID: bundleID) {
            // removeItem deletes a link itself, never what it points at.
            if (try? FileManager.default.attributesOfItem(atPath: path)) != nil {
                do { try FileManager.default.removeItem(atPath: path) } catch {
                    problems.append("Couldn't delete \((path as NSString).lastPathComponent).")
                }
            }
        }
        NSLog("%@", "Wisp reset: app part done at launch, \(problems.count) problems")
        return problems
    }
}

/// Keychain items that are Wisp's own and go in a reset: cloud/provider keys only.
enum WispResetKeychain {
    static let inferenceService = "com.wisp.inference"

    /// Deletes every provider key Wisp saved; returns how many went.
    static func removeProviderKeys() -> Int {
        #if WISP_SETTINGS_QA
        return 0
        #else
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: inferenceService,
                                    kSecMatchLimit as String: kSecMatchLimitAll,
                                    kSecReturnAttributes as String: true]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let rows = result as? [[String: Any]] else { return 0 }
        var removed = 0
        for account in rows.compactMap({ $0[kSecAttrAccount as String] as? String })
        where WispReset.isProviderKeyAccount(account) {
            let item: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                       kSecAttrService as String: inferenceService,
                                       kSecAttrAccount as String: account]
            if SecItemDelete(item as CFDictionary) == errSecSuccess { removed += 1 }
        }
        return removed
        #endif
    }
}

/// The card /resetwisp opens in the notch.
enum ResetCardState: Equatable {
    case confirm
    case running
    case failed(String)
}

struct ResetWispCard: View {
    @ObservedObject var model: OverlayModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Reset Wisp?").font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            switch model.resetCard {
            case .running:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Resetting… Wisp restarts into setup when it's done.")
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                }
            case .failed(let message):
                Text(message).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack { Spacer(); Button("Close") { model.cancelReset() }.keyboardShortcut(.cancelAction) }
            default:
                confirm
            }
        }
        .padding(.top, 12)
    }

    private var confirm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("This deletes everything Wisp has, then restarts it as if it were the first time.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            list("Deletes", WispReset.deletes)
            list("Keeps", WispReset.keeps)
            Text(WispReset.internetAccountsNote).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Spacer()
                Button("Cancel") { model.cancelReset() }
                    .keyboardShortcut(.cancelAction)
                // Destructive, and deliberately without a keyboard shortcut: Return never resets.
                Button("Reset Wisp", role: .destructive) { model.confirmReset() }
                    .buttonStyle(.borderedProminent).tint(.red)
            }
            .padding(.top, 4)
        }
    }

    private func list(_ title: String, _ rows: [String]) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textMuted)
            ForEach(rows, id: \.self) { row in
                Text("• " + row).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
