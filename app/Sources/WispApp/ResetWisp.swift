import Foundation
import Security
import SwiftUI

/// /resetwisp: erase everything Wisp holds except the model, so the next launch
/// is a true first run (the onboarding window at step 1, Model).
///
/// Typed only: the notch opens a confirmation card and nothing happens until the
/// person clicks Reset Wisp (a destructive button that Return never presses).
/// The model and the agent have no way to it: it is not a tool, the service
/// route admits only this app (service/wisp_reset.py), and a starter or other
/// programmatic prompt can't open the card (OverlayModel.pick).
///
/// Order (AppDelegate.performWispReset): Wisp's own activity stops; the service
/// disconnects Google and deletes its data; Wisp's provider keys leave the
/// Keychain; the engine and service stop as on quit; this app's defaults domain
/// is cleared; its macOS privacy grants are reset; Wisp relaunches.
enum WispReset {
    /// The only bundle whose defaults and privacy grants a reset touches.
    static let bundleID = "com.wisp.assistant"
    /// The service route, and the phrase it requires (service/wisp_reset.py).
    static let route = "assistant/reset_wisp"
    static let confirmPhrase = "reset wisp"
    /// A sealed service answers everything else with this (wisp_reset.ResetGate):
    /// a reset already erased it, so the rest of the reset may go ahead.
    static let sealedDetail = "Wisp is resetting."
    /// Set after the defaults are cleared, only when something couldn't be
    /// removed; the relaunched Wisp shows it once and deletes it.
    static let noticeKey = "wisp.reset.notice"

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
        "Everything outside Wisp, including your own oMLX app",
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

    /// What the service answered, in the words the card shows.
    struct ServiceReply: Equatable {
        enum Kind: Equatable { case erased, refused, noAnswer }
        let kind: Kind
        let problems: [String]
        let detail: String

        static func parse(status: Int?, body: [String: Any]?) -> ServiceReply {
            guard let status else {
                return ServiceReply(kind: .noAnswer, problems: [], detail:
                    "Wisp's service didn't answer, so nothing was reset. Try /resetwisp again.")
            }
            let detail = body?["detail"] as? String ?? ""
            if status == 200 {
                let problems = (body?["problems"] as? [[String: Any]] ?? []).compactMap { $0["detail"] as? String }
                return ServiceReply(kind: .erased, problems: problems, detail: "")
            }
            if status == 503 && detail == sealedDetail {
                // An earlier attempt erased the service and lost its answer.
                return ServiceReply(kind: .erased, problems: [], detail: "")
            }
            return ServiceReply(kind: .refused, problems: [], detail: detail.isEmpty
                ? "Wisp's service refused the reset (\(status)). Nothing was reset." : detail + " Nothing was reset.")
        }
    }

    /// Why the card can't open now, or nil.
    static func refusal(corpusBusy: Bool, replying: Bool) -> String? {
        if corpusBusy { return "Stop the corpus run before resetting Wisp." }
        if replying { return "Wait for Wisp to finish answering, then type /resetwisp again." }
        return nil
    }

    /// The short record written to the console: counts and parts, nothing personal.
    static func logLine(removedEntries: Int, providerKeys: Int, problems: Int, privacyReset: Bool) -> String {
        "Wisp reset: service removed \(removedEntries) entries, \(providerKeys) provider keys, "
            + "privacy reset \(privacyReset ? "ran" : "skipped"), \(problems) problems"
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
