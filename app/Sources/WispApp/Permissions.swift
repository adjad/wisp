import AppKit
import EventKit
import UserNotifications
import CoreGraphics
import ApplicationServices

// Triggers the macOS privacy/security prompts Wisp uses, so the person is never
// buried in system dialogs.
//
// What asks for what, and when:
//   - A new user's first-run window asks for Calendar, Reminders, Mail and Notes
//     Automation, Contacts and Notifications row by row, and opens Full Disk
//     Access's page; while it is up nothing else asks (FirstRunReaderGate).
//   - At launch, `bootstrap` asks once for Notifications, Calendar and Reminders
//     on a Mac that never had the first-run window (the window takes it over).
//   - The readers ask for their own access when they start: Calendar
//     (CalendarReader), Reminders (RemindersWriter), Contacts once Messages can be
//     read (ContactsReader), Mail and Notes Automation when they script a running
//     app (MailReader, NotesReader).
//   - Screen Recording and Accessibility: macOS asks on first real use
//     (screen_capture, page reading); Grant More Access asks earlier, one at a time.
//   - Full Disk Access has no prompt: its Settings page opens instead.
//
// Wisp never asks for what it doesn't use: Camera, Photos, Location, Input
// Monitoring (the hotkeys use Carbon, which needs none).
enum Permissions {
    /// Once, on a Mac that never had the first-run window.
    static func bootstrap() {
        let key = FirstRunSetup.permissionsRequestedKey
        if UserDefaults.standard.bool(forKey: key) { return }
        UserDefaults.standard.set(true, forKey: key)
        requestFirstRun()
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

    // MARK: Grant More Access

    /// The access Wisp can use beyond its first launch, each asked for on its own.
    enum Extra: CaseIterable, Equatable {
        case screenRecording, accessibility, fullDiskAccess

        var title: String {
            switch self {
            case .screenRecording: return "Screen Recording"
            case .accessibility: return "Accessibility"
            case .fullDiskAccess: return "Full Disk Access"
            }
        }

        var reason: String {
            switch self {
            case .screenRecording: return "so Wisp can look at your screen when you ask about it"
            case .accessibility: return "so Wisp can read the text of other apps' windows, like Smart Search does"
            case .fullDiskAccess: return "so Wisp can read Messages, Mail and Notes when those apps are closed, and your files"
            }
        }
    }

    /// What is still off, in the order Grant More Access offers it. Pure.
    static func missing(screenRecording: Bool, accessibility: Bool, fullDiskAccess: Bool) -> [Extra] {
        Extra.allCases.filter {
            switch $0 {
            case .screenRecording: return !screenRecording
            case .accessibility: return !accessibility
            case .fullDiskAccess: return !fullDiskAccess
            }
        }
    }

    static func missingExtras() -> [Extra] {
        missing(screenRecording: CGPreflightScreenCaptureAccess(), accessibility: AXIsProcessTrusted(),
                fullDiskAccess: hasFullDiskAccess())
    }

    /// One system surface for one extra: its prompt the first time, its Settings
    /// page after that (macOS shows each prompt only once), and nothing when it is
    /// already on.
    static func request(_ extra: Extra) {
        switch extra {
        case .screenRecording: requestScreenRecording()
        case .accessibility: requestAccessibility()
        case .fullDiskAccess: if !hasFullDiskAccess() { openFullDiskAccessPane() }
        }
    }

    enum Surface: Equatable { case none, prompt, settings }

    /// What one click shows for a promptable permission. Pure.
    static func surface(granted: Bool, promptedBefore: Bool) -> Surface {
        granted ? .none : (promptedBefore ? .settings : .prompt)
    }

    static let screenRecordingPromptedKey = "wisp.screenRecordingPrompted"
    static let accessibilityPromptedKey = "wisp.accessibilityPrompted"

    // Screen Recording: needed for screen_capture (the `screencapture` CLI wrapper).
    static func requestScreenRecording() {
        let defaults = UserDefaults.standard
        switch surface(granted: CGPreflightScreenCaptureAccess(),
                       promptedBefore: defaults.bool(forKey: screenRecordingPromptedKey)) {
        case .none: break
        case .prompt:
            defaults.set(true, forKey: screenRecordingPromptedKey)
            CGRequestScreenCaptureAccess()
        case .settings:
            openSettingsPane("com.apple.preference.security?Privacy_ScreenCapture")
        }
    }

    // Accessibility: UI automation and page reading. The first time, the system
    // prompt (it has its own Open System Settings button); after that, the page.
    // Never both at once.
    static func requestAccessibility() {
        let defaults = UserDefaults.standard
        switch surface(granted: AXIsProcessTrusted(),
                       promptedBefore: defaults.bool(forKey: accessibilityPromptedKey)) {
        case .none: break
        case .prompt:
            defaults.set(true, forKey: accessibilityPromptedKey)
            let axOpts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(axOpts)
        case .settings:
            openSettingsPane("com.apple.preference.security?Privacy_Accessibility")
        }
    }

    // Not promptable: the user must toggle it by hand. Needed by the file/shell
    // tools, Messages, and Mail and Notes while those apps are closed.
    static let fullDiskAccessPane = "com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"

    /// Whether Full Disk Access is on: the system's own privacy database opens
    /// only with it, and it exists on every Mac. Messages, Mail or Notes data that
    /// reads also proves it.
    static func hasFullDiskAccess() -> Bool {
        let fd = open("/Library/Application Support/com.apple.TCC/TCC.db", O_RDONLY)
        if fd >= 0 { close(fd); return true }
        return FirstRunPermissions.fullDiskAccessState() == .allowed
    }

    static func openFullDiskAccessPane() {
        openSettingsPane(fullDiskAccessPane)
    }

    static func openSettingsPane(_ path: String) {
        if let url = URL(string: "x-apple.systempreferences:\(path)") {
            NSWorkspace.shared.open(url)
        }
    }
}
