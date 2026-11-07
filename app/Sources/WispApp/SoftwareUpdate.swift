import AppKit
import SwiftUI
#if canImport(Sparkle)
import Sparkle
#endif

/// In-app updates through Sparkle 2 (the menu bar's Check for Updates… and
/// Settings › General › Updates).
///
/// Wisp is strictly local, so updates never reach the network on their own:
/// the build's Info.plist turns Sparkle's automatic checks and system profile
/// off, and the only requests are a check the person asks for, or the scheduled
/// checks they turn on. A request carries no profile: just the feed fetch (with
/// Sparkle's "Wisp/<build> Sparkle/<version>" user agent) and, when they accept
/// an update, its download. Every update is EdDSA-signed, the feed itself is
/// signed (SURequireSignedFeed), and Sparkle checks the archive's signature
/// before it unpacks it.
///
/// The standalone contract gates compile this file without Sparkle; there the
/// updater reports that updates are unavailable.
enum SoftwareUpdate {
    /// Sparkle's channel for pre-releases (build-support/sparkle.py writes it).
    static let betaChannel = "beta"
    /// Settings › Include beta versions. Unset means the build's own channel decides.
    static let includeBetasKey = "WispUpdatesIncludeBetas"
    static let menuTitle = "Check for Updates…"
    static let unavailableNote = "Updates aren't set up in this build."

    /// What a build's Info.plist says about updates.
    struct Configuration: Equatable {
        let feedURL: URL
        let publicEDKey: String
        /// "beta" or "stable": the channel this build was released on.
        let buildChannel: String
    }

    /// The build's update configuration, or nil when it cannot update safely: no
    /// https feed (or loopback test feed), no 32-byte EdDSA public key, or any of the privacy and signing
    /// switches not set the way the pipeline sets them.
    static func configuration(info: [String: Any]) -> Configuration? {
        guard let feed = (info["SUFeedURL"] as? String).flatMap(URL.init(string:)),
              feed.host?.isEmpty == false, feed.scheme == "https" || isLoopbackTestFeed(feed),
              let key = info["SUPublicEDKey"] as? String,
              Data(base64Encoded: key)?.count == 32,
              info["SUEnableAutomaticChecks"] as? Bool == false,
              info["SUEnableSystemProfiling"] as? Bool == false,
              info["SUAutomaticallyUpdate"] as? Bool == false,
              info["SURequireSignedFeed"] as? Bool == true,
              info["SUVerifyUpdateBeforeExtraction"] as? Bool == true
        else { return nil }
        let channel = info["WispUpdateChannel"] as? String == betaChannel ? betaChannel : "stable"
        return Configuration(feedURL: feed, publicEDKey: key, buildChannel: channel)
    }

    /// http only to this Mac's loopback address: the local end-to-end update test
    /// (docs/updates.md). The pipeline only ever writes an https feed.
    static func isLoopbackTestFeed(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1"
    }

    /// Whether beta versions are offered. A tester's beta build offers the next
    /// beta until they turn it off; a stable build offers betas only when asked.
    static func includesBetas(saved: Bool?, buildChannel: String) -> Bool {
        saved ?? (buildChannel == betaChannel)
    }

    /// Sparkle's allowed channels. Items with no channel (stable releases) are
    /// always offered.
    static func allowedChannels(includeBetas: Bool) -> Set<String> {
        includeBetas ? [betaChannel] : []
    }

    /// No system profile, ever, whatever Sparkle asks.
    static func feedParameters(sendingSystemProfile: Bool) -> [[String: String]] { [] }
}

/// The one updater for the app. Settings binds to it; the menu bar item calls it.
@MainActor
final class SoftwareUpdater: NSObject, ObservableObject {
    static let shared = SoftwareUpdater(info: Bundle.main.infoDictionary ?? [:], defaults: .standard)

    let configuration: SoftwareUpdate.Configuration?
    private let defaults: UserDefaults
    @Published private(set) var automaticallyChecks = false
    @Published private(set) var includesBetas: Bool
    @Published private(set) var canCheck = false

    #if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
    private var observation: NSKeyValueObservation?
    #endif

    init(info: [String: Any], defaults: UserDefaults) {
        configuration = SoftwareUpdate.configuration(info: info)
        self.defaults = defaults
        includesBetas = SoftwareUpdate.includesBetas(
            saved: defaults.object(forKey: SoftwareUpdate.includeBetasKey) as? Bool,
            buildChannel: configuration?.buildChannel ?? "stable")
        super.init()
    }

    /// Whether this build can update at all (Sparkle linked and configured).
    var isAvailable: Bool {
        #if canImport(Sparkle)
        return configuration != nil
        #else
        return false
        #endif
    }

    /// Starts Sparkle. Starting reads only local state; with automatic checks
    /// off (the default) it schedules nothing.
    func start() {
        #if canImport(Sparkle)
        guard isAvailable, controller == nil else { return }
        let controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: self,
                                                      userDriverDelegate: self)
        self.controller = controller
        do {
            try controller.updater.start()
        } catch {
            NSLog("Wisp updates unavailable: %@", error.localizedDescription)
            self.controller = nil
            return
        }
        automaticallyChecks = controller.updater.automaticallyChecksForUpdates
        observation = controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
            let can = updater.canCheckForUpdates
            Task { @MainActor in self?.canCheck = can }
        }
        #endif
    }

    /// Check for Updates…: the person asked, so Sparkle shows its window either way.
    @objc func checkForUpdates(_ sender: Any?) {
        #if canImport(Sparkle)
        start()
        controller?.checkForUpdates(sender)
        #endif
    }

    func setAutomaticallyChecks(_ on: Bool) {
        #if canImport(Sparkle)
        guard let updater = controller?.updater else { return }
        updater.automaticallyChecksForUpdates = on
        automaticallyChecks = updater.automaticallyChecksForUpdates
        #endif
    }

    func setIncludesBetas(_ on: Bool) {
        defaults.set(on, forKey: SoftwareUpdate.includeBetasKey)
        includesBetas = on
        #if canImport(Sparkle)
        // A channel change re-reads the feed on the next check, not from a cache.
        controller?.updater.resetUpdateCycleAfterShortDelay()
        #endif
    }
}

extension SoftwareUpdater: NSMenuItemValidation {
    nonisolated func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        MainActor.assumeIsolated {
            guard menuItem.action == #selector(checkForUpdates(_:)) else { return true }
            #if canImport(Sparkle)
            return isAvailable && (controller == nil || canCheck)
            #else
            return false
            #endif
        }
    }
}

#if canImport(Sparkle)
extension SoftwareUpdater: SPUUpdaterDelegate, SPUStandardUserDriverDelegate {
    nonisolated func allowedChannels(for updater: SPUUpdater) -> Set<String> {
        MainActor.assumeIsolated { SoftwareUpdate.allowedChannels(includeBetas: includesBetas) }
    }

    nonisolated func feedParameters(for updater: SPUUpdater, sendingSystemProfile: Bool) -> [[String: String]] {
        SoftwareUpdate.feedParameters(sendingSystemProfile: sendingSystemProfile)
    }

    /// Wisp lives in the menu bar: a scheduled check's alert must not jump in
    /// front of what the person is doing. Sparkle then shows it without stealing focus.
    nonisolated var supportsGentleScheduledUpdateReminders: Bool { true }
}
#endif

/// Settings › General › Updates.
struct SoftwareUpdateSettings: View {
    @ObservedObject var updater: SoftwareUpdater

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Updates").font(.title3.weight(.medium))
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Software updates")
                            .font(.system(size: 14, weight: .medium))
                        Text(updater.isAvailable
                             ? "Wisp contacts its update feed on GitHub only when you check, or on a schedule "
                               + "if you turn that on. It sends no information about you or your Mac. "
                               + "Every update is signed and checked before it installs."
                             : SoftwareUpdate.unavailableNote)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Button(SoftwareUpdate.menuTitle) { updater.checkForUpdates(nil) }
                        .disabled(!updater.isAvailable)
                }
                Toggle(isOn: Binding(get: { updater.automaticallyChecks },
                                     set: { updater.setAutomaticallyChecks($0) })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Check for updates automatically")
                            .font(.system(size: 14, weight: .medium))
                        Text("Off: Wisp checks only when you choose Check for Updates….")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.switch)
                .disabled(!updater.isAvailable)
                Toggle(isOn: Binding(get: { updater.includesBetas },
                                     set: { updater.setIncludesBetas($0) })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Include beta versions")
                            .font(.system(size: 14, weight: .medium))
                        Text("Beta versions arrive sooner and may have rough edges.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.switch)
                .disabled(!updater.isAvailable)
            }
        }
        .onAppear { updater.start() }
    }
}
