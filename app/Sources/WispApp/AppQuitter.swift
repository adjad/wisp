import AppKit

// Quits every other Dock-visible app — used only by Super Model mode (see
// OverlayModel.enableSuperModel), which the user triggers deliberately to
// free RAM for a large local model. Terminal, oMLX, and Wisp itself are
// exempt. Uses the graceful `.terminate()` Apple Event (same as ⌘Q), not
// forceTerminate — an app can still prompt "save changes?" or refuse to quit.
//
// OPT-IN, and off by default. "Engaging a bigger model closes everything else
// you had open" is a reasonable trade to make for yourself on your own machine
// and a genuinely shocking one to inflict on someone who just installed this
// and pressed the interesting-looking button. Super Model works without it —
// the wired-memory ceiling and the model swap are the substance; this only
// buys additional headroom. Settings › Advanced turns it on.
enum AppQuitter {
    static let defaultsKey = "SuperModelQuitsOtherApps"

    /// Defaults to false: `UserDefaults.bool(forKey:)` returns false for an
    /// unset key, which is the behaviour we want on a fresh install.
    static var isEnabled: Bool {
        get { UserDefaults.standard.bool(forKey: defaultsKey) }
        set { UserDefaults.standard.set(newValue, forKey: defaultsKey) }
    }

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

    static func quitOtherApps() {
        guard isEnabled else { return }
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

    // A single quitOtherApps() pass at toggle time doesn't stick: Finder in
    // particular gets relaunched by Apple Events from the Dock, Control
    // Center, and disk mounts regardless of anything this app does, and Mail
    // can be reopened by the user or Spotlight. Re-run the same pass on an
    // interval for as long as Super Model stays engaged so anything that
    // creeps back gets quit again within one tick.
    static func startWatchdog() {
        stopWatchdog()
        guard isEnabled else { return }
        watchdog = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { _ in
            quitOtherApps()
        }
    }

    static func stopWatchdog() {
        watchdog?.invalidate()
        watchdog = nil
    }
}
