import AppKit

// Quits every other Dock-visible app — used only by Super Model mode (see
// OverlayModel.enableSuperModel), which the user triggers deliberately to
// free RAM for a large local model. Terminal, oMLX, and Wisp itself are
// exempt. Uses the graceful `.terminate()` Apple Event (same as ⌘Q), not
// forceTerminate — an app can still prompt "save changes?" or refuse to quit.
enum AppQuitter {
    private static let exemptBundleIDs: Set<String> = [
        Bundle.main.bundleIdentifier ?? "com.wisp.assistant",
        "app.omlx",             // oMLX — the model server this feature needs running
        "com.apple.Terminal",
    ]

    // Apps that never show up in the .regular loop below but still cost real
    // RAM. com.apple.campo is Siri AI.app — LSUIElement is true, so AppKit
    // reports it as an accessory app despite it sitting resident at ~150-200MB.
    private static let forcedQuitBundleIDs: Set<String> = [
        "com.apple.campo",   // Siri AI
    ]

    private static var watchdog: Timer?
    private static var watchdogTicksRemaining = 0

    // How many follow-up passes to run after the initial quit, and how far
    // apart. This only needs to cover the immediate post-toggle race window
    // (Finder respawning off a Dock/Control Center/disk-mount Apple Event
    // that was already in flight) — not the whole Super Model session, since
    // the user is expected to reopen apps deliberately while it's active and
    // those shouldn't be quit back out from under them.
    private static let watchdogTickCount = 3
    private static let watchdogTickInterval: TimeInterval = 5

    static func quitOtherApps() {
        for app in NSWorkspace.shared.runningApplications {
            if let bid = app.bundleIdentifier, forcedQuitBundleIDs.contains(bid) {
                app.terminate()
                continue
            }
            // .regular = Dock-visible apps; skips menu-bar-only agents/daemons
            // (Bluetooth helpers, iCloud, etc.) that weren't the intended target
            // and use negligible memory anyway.
            guard app.activationPolicy == .regular else { continue }
            guard let bid = app.bundleIdentifier, !exemptBundleIDs.contains(bid) else { continue }
            app.terminate()
        }
    }

    // A single quitOtherApps() pass at toggle time doesn't always stick:
    // Finder in particular can get relaunched by an Apple Event from the
    // Dock, Control Center, or a disk mount that was already in flight when
    // this ran. Re-run the pass a few more times just after toggling to
    // catch that race, then stop — this must NOT keep running for the
    // duration of Super Model, or it quits apps the user reopens on purpose
    // while the model is active, which defeats the point of reopening them.
    static func startWatchdog() {
        stopWatchdog()
        watchdogTicksRemaining = watchdogTickCount
        watchdog = Timer.scheduledTimer(withTimeInterval: watchdogTickInterval, repeats: true) { _ in
            quitOtherApps()
            watchdogTicksRemaining -= 1
            if watchdogTicksRemaining <= 0 { stopWatchdog() }
        }
    }

    static func stopWatchdog() {
        watchdog?.invalidate()
        watchdog = nil
        watchdogTicksRemaining = 0
    }
}
