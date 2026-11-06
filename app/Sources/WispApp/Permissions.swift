import AppKit
import EventKit
import UserNotifications
import CoreGraphics
import ApplicationServices
import Contacts

// Every macOS privacy prompt Wisp can cause, and when. The rule: Wisp never
// asks on its own. A prompt only ever follows the person's click on a button
// that says what it is for.
//
//   - Calendar, Reminders, Mail and Notes (Automation), Contacts and
//     Notifications: their rows in the setup window's Permissions step and on
//     the notch's permission page (FirstRunPermissions.request), one click per
//     row. A Brief, Day or Ask row about a source Wisp can't read opens that page
//     (`openFix`, NotchPermissionPage). The window's step stays for setup,
//     Settings › Privacy's Permissions… and the menu bar.
//   - The readers never ask: Calendar and Reminders report "not allowed yet"
//     (RemindersWriter.Access.passive), Contacts is started with mayAsk false,
//     and Mail and Notes skip their scripted reads until Automation has an
//     answer (they read the on-disk copy instead, with Full Disk Access).
//   - Screen Recording and Accessibility: Settings › Privacy's More Access… or
//     Smart Search's own button, one surface per click.
//   - Full Disk Access has no prompt: its Settings page opens instead.
//
// Wisp never asks for what it doesn't use: Camera, Photos, Location, Input
// Monitoring (the hotkeys use Carbon, which needs none).
extension Notification.Name {
    /// Opens the setup window on its Permissions step.
    static let wispOpenPermissions = Notification.Name("WispOpenPermissions")
    /// Opens the notch on its permission page, on the Item rawValue in `object`
    /// (the whole list when nil).
    static let wispOpenPermissionPage = Notification.Name("WispOpenPermissionPage")
    /// Something macOS shows is about to open: the notch panel, which sits above
    /// normal windows, tucks away so it can't open underneath.
    static let wispStepAside = Notification.Name("WispStepAside")
    /// What the panel tucked away for has been answered: it can come back.
    static let wispStepBack = Notification.Name("WispStepBack")
    /// Asks for the next extra that is still off (Settings › Privacy's More Access…).
    static let wispGrantMoreAccess = Notification.Name("WispGrantMoreAccess")
    /// Opens Settings in the Wisp window, on the pane `WispSettingsPane` names
    /// (the Brief's Gmail reconnect link, BriefFix.open).
    static let wispOpenSettings = Notification.Name("WispOpenSettings")
}

enum Permissions {
    /// Before anything macOS shows on its own: a question, a System Settings page.
    static func stepAside() { NotificationCenter.default.post(name: .wispStepAside, object: nil) }

    /// After the question it stepped aside for has an answer.
    static func stepBack() { NotificationCenter.default.post(name: .wispStepBack, object: nil) }

    /// A Brief, Day or Ask fix link. A permission opens its page in the notch,
    /// which says what it is for and where it stands before anything else opens.
    /// Any other System Settings page (Internet Accounts) opens directly.
    static func openFix(_ url: URL) {
        if let item = NotchPermissionPage.item(forFix: url.absoluteString) {
            NotificationCenter.default.post(name: .wispOpenPermissionPage, object: item.rawValue)
        } else {
            if url.scheme == "x-apple.systempreferences" { PrivacyRelaunch.notePaneOpened() }
            stepAside()
            NSWorkspace.shared.open(url)
        }
    }

    /// The link's words: a permission's link opens Wisp's own page, not the
    /// System Settings page its label names.
    static func fixLabel(_ action: BriefDetail.Action) -> String {
        NotchPermissionPage.item(forFix: action.url) == nil ? action.label : allowLabel
    }

    static let allowLabel = "Allow…"

    // MARK: Ask's fix buttons

    /// System Settings > Privacy & Security > Contacts.
    static let contactsAccess = BriefDetail.Action(
        label: "Open Contacts settings ↗",
        url: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Contacts")

    /// The buttons under an Ask reply whose tools weren't allowed to read a
    /// source (`needs_access` kinds, AskAccess), as things stand now: a fix
    /// macOS already reports done is dropped, and Contacts points at Full Disk
    /// Access while the setup window still waits on it (its contactsRow).
    /// Status reads only: it never asks.
    @MainActor static func askFixes(_ kinds: [String]) -> [BriefDetail.Action] {
        // The setup window's own rule (contactsWaits), so a Full Disk Access it
        // calls "Not needed" never becomes Ask's Contacts fix.
        let contactsWait = kinds.contains("contacts_access")
            && CNContactStore.authorizationStatus(for: .contacts) == .notDetermined
            && FirstRunPermissions.contactsWaits(
                [.contacts: .notAsked, .fullDiskAccess: FirstRunPermissions.fullDiskAccessState()],
                messagesReadable: FirstRunPermissions.messagesReadable())
        return askFixes(kinds.filter { !askFixDone($0) }, contactsWaitForFullDiskAccess: contactsWait)
    }

    /// The Brief and Day's own link for each kind, plus Contacts, each link once. Pure.
    static func askFixes(_ kinds: [String], contactsWaitForFullDiskAccess: Bool) -> [BriefDetail.Action] {
        var out: [BriefDetail.Action] = []
        for kind in kinds {
            let kind = kind == "contacts_access" && contactsWaitForFullDiskAccess ? "full_disk_access" : kind
            guard let fix = kind == "contacts_access" ? contactsAccess : SourceAccess.fix(for: kind),
                  !out.contains(where: { $0.url == fix.url }) else { continue }
            out.append(fix)
        }
        return out
    }

    /// macOS now says yes, so the reply's old button would do nothing.
    private static func askFixDone(_ kind: String) -> Bool {
        switch kind {
        case "calendar_access": return EKEventStore.authorizationStatus(for: .event) == .fullAccess
        case "reminders_access": return EKEventStore.authorizationStatus(for: .reminder) == .fullAccess
        case "contacts_access": return CNContactStore.authorizationStatus(for: .contacts) == .authorized
        default: return false
        }
    }

    /// What a Mail or Notes read says when its on-disk copy failed. When the app
    /// is open but Automation was never decided, Wisp skipped reading it through
    /// the app (asking would prompt), so "open Mail" would be wrong: the reason
    /// points at the Mail & Notes row instead, and its Brief link (Automation's
    /// kind) opens that row through openFix. Pure.
    static func unaskedAutomation(app: String, appRunning: Bool, automationDecided: Bool,
                                  failure: (kind: String, reason: String)) -> (kind: String, reason: String) {
        guard appRunning, !automationDecided else { return failure }
        return ("mail_automation", "\(app) is open, but Wisp hasn't been allowed to read it yet. Open Wisp's "
                + "Settings, choose Permissions… under Privacy, and click Allow next to Mail & Notes.")
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
            case .accessibility: return "so Wisp can read the window you're asking about"
            case .fullDiskAccess: return "so Wisp can read Messages, Mail and Notes when those apps are closed, and your files"
            }
        }
    }

    /// What is still off, in the order More Access… offers it. Pure.
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
            PrivacyRelaunch.notePaneOpened()
            stepAside()
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
            PrivacyRelaunch.notePaneOpened()
            stepAside()
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

    /// Every privacy page Wisp opens. Switching Wisp on there can make macOS
    /// offer Quit & Reopen, so Wisp notes it first (PrivacyRelaunch).
    static func openSettingsPane(_ path: String) {
        if let url = URL(string: "x-apple.systempreferences:\(path)") {
            PrivacyRelaunch.notePaneOpened()
            stepAside()
            NSWorkspace.shared.open(url)
        }
    }
}
