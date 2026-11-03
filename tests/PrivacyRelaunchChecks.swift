// Contract for macOS's Quit & Reopen after a privacy switch
// (app/Sources/WispApp/PrivacyRelaunch.swift): which quits make Wisp reopen
// itself once it has gone. Pure logic: never quits, launches or opens anything.
import Foundation

@main
struct PrivacyRelaunchChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }
        typealias Quit = PrivacyRelaunch.Quit
        let now: TimeInterval = 1_000_000
        let recent = now - 60, stale = now - PrivacyRelaunch.paneWindow - 1
        func relaunch(_ quit: Quit, _ opened: TimeInterval? = nil) -> Bool {
            PrivacyRelaunch.shouldRelaunch(quit, paneOpenedAt: opened, now: now)
        }
        let settings = Quit.appleEvent(senderBundleID: "com.apple.systempreferences",
                                       senderPath: "/System/Applications/System Settings.app/Contents/MacOS/System Settings")
        let privacyPage = Quit.appleEvent(senderBundleID: "com.apple.settings.PrivacySecurity.extension", senderPath: nil)
        let privacyPath = Quit.appleEvent(senderBundleID: nil, senderPath:
            "/System/Library/ExtensionKit/Extensions/SecurityPrivacyExtension.appex/Contents/MacOS/SecurityPrivacyExtension")
        let dock = Quit.appleEvent(senderBundleID: "com.apple.dock", senderPath: "/System/Library/CoreServices/Dock.app/Contents/MacOS/Dock")
        let logout = Quit.appleEvent(senderBundleID: nil,
                                     senderPath: "/System/Library/CoreServices/loginwindow.app/Contents/MacOS/loginwindow")
        let finder = Quit.appleEvent(senderBundleID: "com.apple.finder", senderPath: nil)
        let script = Quit.appleEvent(senderBundleID: nil, senderPath: "/usr/bin/osascript")
        let otherApp = Quit.appleEvent(senderBundleID: "com.example.cleaner", senderPath: "/Applications/Cleaner.app/Contents/MacOS/Cleaner")
        let unknown = Quit.appleEvent(senderBundleID: nil, senderPath: nil)
        let macOSAgent = Quit.appleEvent(senderBundleID: nil, senderPath: "/System/Library/PrivateFrameworks/Example.framework/agent")

        // The reported bug: System Settings' Quit & Reopen, from setup or from Settings.
        check(relaunch(settings), "System Settings' Quit & Reopen: Wisp reopens itself")
        check(relaunch(settings, stale), "however long ago Wisp opened a privacy page (or never)")
        check(relaunch(privacyPage) && relaunch(privacyPath),
              "the Privacy & Security page's own process counts as System Settings, by bundle id or by path")
        // A person's quit, or another program's, never brings Wisp back.
        check(!relaunch(.notAppleEvent) && !relaunch(.notAppleEvent, recent),
              "Wisp's own Quit or ⌘Q: stays quit, even right after a privacy page")
        check(!relaunch(dock, recent) && !relaunch(logout, recent) && !relaunch(finder, recent),
              "the Dock, a logout or restart, the Finder: stays quit")
        check(!relaunch(script, recent) && !relaunch(otherApp, recent), "a script or another app: stays quit")
        // Sender unknown, or part of macOS: only right after Wisp sent the person there.
        check(relaunch(unknown, recent) && relaunch(macOSAgent, recent),
              "an unknown or macOS sender just after a privacy page: reopens")
        check(!relaunch(unknown) && !relaunch(unknown, stale) && !relaunch(macOSAgent, stale),
              "without a recent privacy page: stays quit")
        check(!relaunch(unknown, now + 120), "a privacy-page time in the future (clock change) is ignored")
        check(PrivacyRelaunch.paneWindow >= 5 * 60 && PrivacyRelaunch.paneWindow <= 30 * 60,
              "the privacy-page window is minutes, not hours")
        check(PrivacyRelaunch.isSystemSettings(bundleID: nil, path: "/System/Applications/System Settings.app/Contents/MacOS/System Settings")
              && !PrivacyRelaunch.isSystemSettings(bundleID: "com.apple.settingsx", path: "/Applications/Settings Helper.app/x"),
              "System Settings is told by its bundle id or its path, never a look-alike")
        // The marker.
        let defaults = UserDefaults(suiteName: "wisp.privacy-relaunch-checks.\(getpid())")!
        defer { defaults.removePersistentDomain(forName: "wisp.privacy-relaunch-checks.\(getpid())") }
        check(PrivacyRelaunch.paneOpenedAt(defaults) == nil, "no privacy page opened yet")
        PrivacyRelaunch.notePaneOpened(defaults, now: Date(timeIntervalSince1970: recent))
        check(PrivacyRelaunch.paneOpenedAt(defaults) == recent, "opening one records when")
        check(PrivacyRelaunch.paneOpenedKey.hasPrefix("wisp."), "the marker is one /resetwisp clears")
        print("PrivacyRelaunchChecks: \(count) checks passed")
    }
}
