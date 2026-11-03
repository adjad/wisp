import AppKit
import Foundation

/// macOS's Quit & Reopen, after a privacy switch (Full Disk Access, Screen
/// Recording, Accessibility…) is turned on for Wisp in System Settings.
///
/// System Settings sends Wisp a quit Apple Event and reopens it only if it
/// quits promptly. Wisp's quit waits for its engine and service to stop (about
/// 7 s, up to 20 s), and macOS then never reopened it: seen in the unified log
/// on rc1-beta1 (2026-11-02 21:32, "Handling Quit AppleEvent", NSTerminateLater,
/// exit 7 s later, no launch after). So when that quit came from System
/// Settings, Wisp reopens itself once it has gone (WispReset.relaunchArguments).
/// A second open while macOS's own reopen is running only brings that one forward.
enum PrivacyRelaunch {
    /// When Wisp last opened a privacy page of System Settings, or asked macOS
    /// for a privacy permission (seconds since 1970).
    static let paneOpenedKey = "wisp.privacyPaneOpenedAt"
    /// How long that counts: long enough to find the switch and type a password.
    static let paneWindow: TimeInterval = 15 * 60

    /// What asked Wisp to quit.
    enum Quit: Equatable {
        /// Wisp's own Quit, ⌘Q or a quit it makes itself: no Apple Event.
        case notAppleEvent
        /// A quit Apple Event, with what is known about the program that sent it
        /// and its quit reason (`kAEQuitReason`, 'why?') as a four-character code.
        case appleEvent(senderBundleID: String?, senderPath: String?, reason: String? = nil)
    }

    /// Quit reasons that mean the Mac is logging out, restarting, shutting down
    /// or sleeping: kAELogOut, kAEReallyLogOut, kAEShowRestartDialog,
    /// kAEShowShutdownDialog, kAERestart, kAEShutDown, kAEShowPowerDialog, kAESleep.
    static let shutdownReasons: Set<String> = ["logo", "rlgo", "rrst", "rsdn", "rest", "shut", "pwrd", "slep"]

    /// System Settings itself, or its Privacy & Security page (a separate process).
    static func isSystemSettings(bundleID: String?, path: String?) -> Bool {
        if let bundleID {
            if bundleID == "com.apple.systempreferences" || bundleID.hasPrefix("com.apple.settings.") { return true }
        }
        guard let path else { return false }
        return path.contains("/System Settings.app/") || path.contains("/System Preferences.app/")
            || path.contains("/SecurityPrivacyExtension.appex/")
    }

    /// Where a person's own quit comes from: the Dock, a logout or restart, the Finder.
    static func isPersonQuit(bundleID: String?, path: String?) -> Bool {
        let people = ["com.apple.dock", "com.apple.loginwindow", "com.apple.finder"]
        if let bundleID, people.contains(bundleID) { return true }
        guard let path else { return false }
        return ["/Dock.app/", "/loginwindow.app/", "/Finder.app/"].contains { path.contains($0) }
    }

    /// Whether Wisp reopens itself after this quit. Always when System Settings
    /// asked. Otherwise only right after Wisp sent the person to a privacy page,
    /// and only when the sender can't be told or is part of macOS itself, never
    /// for the Dock, a logout or another program's quit. Never when the quit
    /// gives a logout, restart, shutdown or sleep reason, whoever sent it. Pure.
    static func shouldRelaunch(_ quit: Quit, paneOpenedAt: TimeInterval?, now: TimeInterval) -> Bool {
        guard case let .appleEvent(bundleID, path, reason) = quit else { return false }
        if let reason, shutdownReasons.contains(reason) { return false }
        if isSystemSettings(bundleID: bundleID, path: path) { return true }
        // Any other reason from a sender that isn't System Settings: not a plain quit.
        if reason != nil { return false }
        if isPersonQuit(bundleID: bundleID, path: path) { return false }
        guard let paneOpenedAt, paneOpenedAt <= now, now - paneOpenedAt <= paneWindow else { return false }
        let unknown = bundleID == nil && path == nil
        let partOfMacOS = path.map { $0.hasPrefix("/System/Library/") || $0.hasPrefix("/usr/libexec/") } ?? false
        return unknown || partOfMacOS
    }

    /// Before Wisp opens a privacy page or asks for a privacy permission.
    static func notePaneOpened(_ defaults: UserDefaults = .standard, now: Date = Date()) {
        defaults.set(now.timeIntervalSince1970, forKey: paneOpenedKey)
    }

    /// Once a reopen is scheduled: the page has done its part.
    static func clearPaneOpened(_ defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: paneOpenedKey)
    }

    static func paneOpenedAt(_ defaults: UserDefaults = .standard) -> TimeInterval? {
        defaults.object(forKey: paneOpenedKey) == nil ? nil : defaults.double(forKey: paneOpenedKey)
    }
}

extension PrivacyRelaunch.Quit {
    /// The quit Apple Event AppKit is handling right now, and who sent it: the
    /// sender's bundle id where Launch Services knows it, and its program's path.
    static func current() -> Self {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              event.eventClass == AEEventClass(kCoreEventClass),
              event.eventID == AEEventID(kAEQuitApplication) else { return .notAppleEvent }
        let why = AEKeyword(kAEQuitReason)
        let reason = (event.attributeDescriptor(forKeyword: why) ?? event.paramDescriptor(forKeyword: why))
            .map { fourCharCode($0.typeCodeValue != 0 ? $0.typeCodeValue : $0.enumCodeValue) }
        let pid = event.attributeDescriptor(forKeyword: AEKeyword(keySenderPIDAttr))?.int32Value ?? 0
        guard pid > 0 else { return .appleEvent(senderBundleID: nil, senderPath: nil, reason: reason) }
        var buffer = [CChar](repeating: 0, count: 4096)
        let path = proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 ? String(cString: buffer) : nil
        return .appleEvent(senderBundleID: NSRunningApplication(processIdentifier: pid)?.bundleIdentifier,
                           senderPath: path, reason: reason)
    }

    /// 'logo' from its OSType.
    static func fourCharCode(_ code: OSType) -> String {
        String(bytes: [24, 16, 8, 0].map { UInt8((code >> $0) & 0xFF) }, encoding: .macOSRoman) ?? String(code)
    }
}
