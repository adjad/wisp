import AppKit
import Foundation

extension Notification.Name {
    /// Full Disk Access's live probe found something new. `object` is the
    /// AccessProbeMonitor; userInfo["gained"] holds the AccessProbe.Store raw values
    /// that just began to read (the readers re-post them at once, with no restart),
    /// userInfo["state"] a short word for logs: on, off, partial or unknown.
    static let wispAccessProbeChanged = Notification.Name("WispAccessProbeChanged")
}

/// Runs AccessProbe at the moments that matter and keeps its answer:
///
///   - when Wisp becomes the front app (NSApplication.didBecomeActive), which is
///     also how Wisp notices it is back from System Settings;
///   - every 2 seconds, only while a permission page is open (the notch's page or
///     the setup window's step: FirstRunPermissions.start/stop);
///   - at 1, 3 and 10 seconds after coming back from a privacy page Wisp sent the
///     person to, so a switch that lands a moment later is caught.
///
/// Read the current state with `AccessProbeMonitor.shared.state`. A change posts
/// `.wispAccessProbeChanged`. After a return from System Settings where the probe
/// still fails 10 seconds later, `reopenPromptVisible` turns on and the permission
/// page offers a Reopen button (`reopenWisp()`). Wisp never reopens on its own.
@MainActor
final class AccessProbeMonitor: ObservableObject {
    static let shared = AccessProbeMonitor()

    @Published private(set) var reading = AccessProbe.Reading.empty
    /// False until the first probe has finished.
    @Published private(set) var hasProbed = false
    @Published private(set) var reopenPromptVisible = false

    var state: AccessProbe.State { hasProbed ? reading.state : .unknown }

    /// How often the open permission page re-probes.
    static let pageInterval: UInt64 = 2_000_000_000
    /// After a return from System Settings, in seconds.
    static let returnChecks: [TimeInterval] = [1, 3, ReopenPrompt.delay + 0.5]

    private var started = false
    private var pagesOpen = 0
    private var timer: Task<Void, Never>?
    private var observers: [NSObjectProtocol] = []
    private var generation = 0
    private var applied = 0

    // The trip to System Settings: Wisp noted a privacy page (PrivacyRelaunch.notePaneOpened),
    // then stopped being the front app, then became it again.
    private var leftForSettingsFor: TimeInterval?
    private var returnHandledFor: TimeInterval?
    private var returnedAt: TimeInterval?
    private var promptDismissed = false

    /// Call once at launch.
    func start() {
        guard !started else { return }
        started = true
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSApplication.didResignActiveNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.resignedActive() }
        })
        observers.append(center.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.becameActive() }
        })
        Task { await refresh() }
    }

    /// A permission page came up or went away. The 2 s loop runs while any is open.
    func pageOpened() {
        pagesOpen += 1
        guard pagesOpen == 1, timer == nil else { return }
        timer = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: Self.pageInterval)
            }
        }
    }

    func pageClosed() {
        pagesOpen = max(0, pagesOpen - 1)
        guard pagesOpen == 0 else { return }
        timer?.cancel()
        timer = nil
    }

    private func resignedActive() {
        guard let opened = PrivacyRelaunch.paneOpenedAt(), recent(opened) else { return }
        leftForSettingsFor = opened
    }

    private func becameActive() {
        Task { await refresh() }
        // Back from the privacy page Wisp sent the person to: once per visit.
        guard let opened = PrivacyRelaunch.paneOpenedAt(), recent(opened),
              leftForSettingsFor == opened, returnHandledFor != opened else { return }
        returnHandledFor = opened
        returnedAt = Date().timeIntervalSince1970
        promptDismissed = false
        for seconds in Self.returnChecks {
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                await self?.refresh()
            }
        }
    }

    private func recent(_ opened: TimeInterval) -> Bool {
        let now = Date().timeIntervalSince1970
        return opened <= now && now - opened <= PrivacyRelaunch.paneWindow
    }

    /// One probe, off the main thread. A slower read that began first never lands
    /// over a newer one.
    func refresh() async {
        #if WISP_NOTCH_QA
        return   // renders use fixed states, never this Mac's files
        #else
        generation += 1
        let mine = generation
        let next = await Task.detached(priority: .utility) { AccessProbe.probe() }.value
        guard mine > applied else { return }
        applied = mine
        apply(next)
        #endif
    }

    private func apply(_ next: AccessProbe.Reading) {
        let before = reading
        let hadProbed = hasProbed
        hasProbed = true
        if next != before { reading = next }
        if next.state == .on { returnedAt = nil }
        updatePrompt()
        // The first probe at launch isn't a change: the readers start on their own then.
        guard hadProbed, next != before else { return }
        let gained = AccessProbe.gained(from: before.outcomes, to: next.outcomes)
        NotificationCenter.default.post(
            name: .wispAccessProbeChanged, object: self,
            userInfo: ["gained": gained.map(\.rawValue), "state": Self.word(next.state)])
    }

    private func updatePrompt() {
        let shows = ReopenPrompt.shows(returnedAt: returnedAt, now: Date().timeIntervalSince1970,
                                       access: reading.state, dismissed: promptDismissed)
        if shows != reopenPromptVisible { reopenPromptVisible = shows }
    }

    /// The prompt's second check, once its 10 s are up.
    func recheckPrompt() { updatePrompt() }

    func dismissPrompt() {
        promptDismissed = true
        updatePrompt()
    }

    /// The Reopen button: the person's own press. Wisp stops its engine and service
    /// as on any quit, and a helper opens it again once this copy has gone.
    func reopenWisp() {
        guard let arguments = WispReset.relaunchArguments(pid: ProcessInfo.processInfo.processIdentifier,
                                                          app: Bundle.main.bundleURL) else { return }
        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: WispReset.relaunchShell)
        helper.arguments = arguments
        do { try helper.run() } catch {
            NSLog("%@", "Wisp reopen: couldn't schedule the reopen (\(error)); staying open")
            return
        }
        PrivacyRelaunch.clearPaneOpened()
        NSApp.terminate(nil)
    }

    nonisolated static func word(_ state: AccessProbe.State) -> String {
        switch state {
        case .on: return "on"
        case .off: return "off"
        case .partial: return "partial"
        case .unknown: return "unknown"
        }
    }
}
