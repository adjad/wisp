import AppKit
import EventKit
import UserNotifications
import CoreGraphics
import ApplicationServices

// Every macOS privacy prompt Wisp can cause, and when. The rule: Wisp never
// asks on its own. A prompt only ever follows the person's click on a button
// that says what it is for.
//
//   - Calendar, Reminders, Mail and Notes (Automation), Contacts and
//     Notifications: their rows in the setup window's Permissions step
//     (FirstRunPermissions.request), one click per row. The same step opens
//     again from the menu bar icon's Permissions…, and from a Brief or Day row
//     that says a source isn't allowed yet (`openFix`).
//   - The readers never ask: Calendar and Reminders report "not allowed yet"
//     (RemindersWriter.Access.passive), Contacts is started with mayAsk false,
//     and Mail and Notes skip their scripted reads until Automation has an
//     answer (they read the on-disk copy instead, with Full Disk Access).
//   - Screen Recording and Accessibility: Grant More Access… or Smart Search's
//     own button, one surface per click.
//   - Full Disk Access has no prompt: its Settings page opens instead.
//
// Wisp never asks for what it doesn't use: Camera, Photos, Location, Input
// Monitoring (the hotkeys use Carbon, which needs none).
extension Notification.Name {
    /// Opens the setup window on its Permissions step.
    static let wispOpenPermissions = Notification.Name("WispOpenPermissions")
}

enum Permissions {
    /// A Brief or Day "Open … settings" link. For Calendar or Reminders that
    /// macOS has never been asked about, System Settings doesn't list Wisp yet,
    /// so the link opens Wisp's own Permissions step instead, where one click on
    /// Allow asks macOS. Every other link opens as before.
    static func openFix(_ url: URL) {
        if needsAskingInWisp(url.absoluteString,
                             calendarUndecided: EKEventStore.authorizationStatus(for: .event) == .notDetermined,
                             remindersUndecided: EKEventStore.authorizationStatus(for: .reminder) == .notDetermined,
                             automationUndecided: FirstRunPermissions.automationStateNow(ask: false) == .notAsked) {
            NotificationCenter.default.post(name: .wispOpenPermissions, object: nil)
        } else {
            NSWorkspace.shared.open(url)
        }
    }

    /// Pure: whether a settings link is for a permission macOS hasn't asked about yet.
    static func needsAskingInWisp(_ url: String, calendarUndecided: Bool, remindersUndecided: Bool,
                                  automationUndecided: Bool = false) -> Bool {
        (url.hasSuffix("Privacy_Calendars") && calendarUndecided)
            || (url.hasSuffix("Privacy_Reminders") && remindersUndecided)
            || (url.hasSuffix("Privacy_Automation") && automationUndecided)
    }

    /// What a Mail or Notes read says when its on-disk copy failed. When the app
    /// is open but Automation was never decided, Wisp skipped reading it through
    /// the app (asking would prompt), so "open Mail" would be wrong: the reason
    /// points at the Mail & Notes row instead, and its Brief link (Automation's
    /// kind) opens that row through openFix. Pure.
    static func unaskedAutomation(app: String, appRunning: Bool, automationDecided: Bool,
                                  failure: (kind: String, reason: String)) -> (kind: String, reason: String) {
        guard appRunning, !automationDecided else { return failure }
        return ("mail_automation", "\(app) is open, but Wisp hasn't been allowed to read it yet. Right-click Wisp's "
                + "menu bar icon, choose Permissions…, and click Allow next to Mail & Notes.")
    }

    // MARK: Grant More Access

    /// The access Wisp can use beyond the setup window's rows, each asked for on its own.
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
