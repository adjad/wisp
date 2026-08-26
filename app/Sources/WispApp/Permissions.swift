import AppKit
import EventKit
import UserNotifications
import CoreGraphics
import ApplicationServices
import IOKit.hid

// Proactively triggers every macOS privacy/security prompt Wisp actually uses,
// so the user grants them up front instead of hunting through System Settings.
//
// Two classes of permission exist:
//   • API-promptable — calling the request shows the system dialog. We fire all
//     of these that Wisp genuinely uses.
//   • Not promptable (no API) — Full Disk Access, Developer Tools, App
//     Management. For these we open the exact System Settings pane so the user
//     can flip the toggle; macOS offers no way to prompt for them.
//
// We deliberately do NOT request permissions Wisp doesn't use (Camera, Photos,
// Location, Contacts) — requesting unused access is bad practice and just spams
// the user with irrelevant dialogs.
enum Permissions {
    // Run the full bootstrap once (guarded), or force=true from the menu.
    static func bootstrap(force: Bool = false) {
        let key = "wisp.didRequestPermissions"
        if !force && UserDefaults.standard.bool(forKey: key) { return }
        UserDefaults.standard.set(true, forKey: key)
        requestAll()
    }

    static func requestAll() {
        // 1. Notifications — reminder alerts.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }

        // 2. Calendar + Reminders — upcoming events / countdown chip, and
        //    mirroring Wisp reminders into the macOS Reminders app.
        let store = EKEventStore()
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents { _, _ in }
            store.requestFullAccessToReminders { _, _ in }
        } else {
            store.requestAccess(to: .event) { _, _ in }
            store.requestAccess(to: .reminder) { _, _ in }
        }

        // 3. Screen Recording — needed for screen_capture (the screenshot tool,
        //    service/tools/system_extras.py's `screencapture` CLI wrapper).
        //    Calling this shows the prompt on first use.
        CGRequestScreenCaptureAccess()

        // 4. Accessibility — reliable global hotkey + UI automation. The prompt
        //    variant opens a dialog with an "Open System Settings" button.
        let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(axOpts)

        // 5. Input Monitoring — global key events.
        _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)

        // 6. Not promptable — open the panes the user must toggle by hand.
        //    Full Disk Access is the important one (file/shell tools). Staggered
        //    so it doesn't fight the dialogs above for focus.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            openSettingsPane("com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles")
        }
        // Automation (controlling Music/Safari/etc.) can't be pre-requested — it
        // prompts per target app on first use by the app_control tools.
    }

    static func openSettingsPane(_ path: String) {
        if let url = URL(string: "x-apple.systempreferences:\(path)") {
            NSWorkspace.shared.open(url)
        }
    }
}
