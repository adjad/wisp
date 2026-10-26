import AppKit
import EventKit
import UserNotifications
import CoreGraphics
import ApplicationServices
import IOKit.hid

// Triggers the macOS privacy/security prompts Wisp uses, in two stages so the
// user is not buried in system dialogs over the setup card on first launch.
//
// Two classes of permission exist:
//   - API-promptable: calling the request shows the system dialog.
//   - Not promptable (no API): Full Disk Access, Developer Tools, App
//     Management. For these we open the exact System Settings pane so the user
//     can flip the toggle.
//
// We deliberately do NOT request permissions Wisp doesn't use (Camera, Photos,
// Location, Contacts).
enum Permissions {
    // First launch asks only for what the first-run experience needs
    // (notifications; calendar and reminders for Brief / Day). The rest are
    // deferred:
    //   - Screen Recording, Accessibility: macOS prompts on first real use
    //     (screen_capture, page reading); `requestExtras` asks earlier.
    //   - Input Monitoring: nothing in Wisp needs it today (the hotkey uses
    //     Carbon), so it is asked only by `requestExtras`.
    //   - Full Disk Access cannot be prompted; `requestExtras` opens its pane.
    // The menu bar's Grant More Access… calls `requestExtras(force: true)`.
    static func bootstrap(force: Bool = false) {
        let key = "wisp.didRequestPermissions"
        if !force && UserDefaults.standard.bool(forKey: key) { return }
        UserDefaults.standard.set(true, forKey: key)
        if force { requestAll() } else { requestFirstRun() }
    }

    /// The later, clearly explained step. Call from a user-initiated place (a
    /// setup step or a feature's first use). Runs once unless force is true.
    static func requestExtras(force: Bool = false) {
        let key = "wisp.didRequestExtraPermissions"
        if !force && UserDefaults.standard.bool(forKey: key) { return }
        UserDefaults.standard.set(true, forKey: key)
        requestScreenRecording()
        requestAccessibility()
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
        // Staggered so it doesn't fight the dialogs above for focus.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { openFullDiskAccessPane() }
    }

    static func requestAll() {
        requestFirstRun()
        requestExtras(force: true)
    }

    static func requestFirstRun() {
        // 1. Notifications: reminder alerts.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }

        // 2. Calendar + Reminders: upcoming events / countdown chip, and
        //    mirroring Wisp reminders into the macOS Reminders app.
        let store = EKEventStore()
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents { _, _ in }
            store.requestFullAccessToReminders { _, _ in }
        } else {
            store.requestAccess(to: .event) { _, _ in }
            store.requestAccess(to: .reminder) { _, _ in }
        }
    }

    // Screen Recording: needed for screen_capture (the `screencapture` CLI wrapper).
    static func requestScreenRecording() {
        if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
    }

    // Accessibility: UI automation and page reading. The prompt variant opens a
    // dialog with an "Open System Settings" button.
    static func requestAccessibility() {
        let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(axOpts)
    }

    // Not promptable: the user must toggle it by hand. Needed by the file/shell
    // tools, Messages, and Mail and Notes while those apps are closed.
    static let fullDiskAccessPane = "com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"

    static func openFullDiskAccessPane() {
        openSettingsPane(fullDiskAccessPane)
    }

    static func openSettingsPane(_ path: String) {
        if let url = URL(string: "x-apple.systempreferences:\(path)") {
            NSWorkspace.shared.open(url)
        }
    }
}
