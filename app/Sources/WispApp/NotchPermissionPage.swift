import AppKit

/// The permission page in the notch: what each macOS permission is, why Wisp
/// wants it, where it stands and the one thing to do next. A Brief, Day or Ask
/// row about a source Wisp can't read opens it on that permission
/// (Permissions.openFix); Settings' Mac permissions row opens the whole list.
///
/// The notch panel sits above normal windows (SurfaceLayout.windowLevel), so
/// anything macOS shows for a permission would open under it. The page's actions
/// tuck the panel away first (Permissions.stepAside) and bring it back to show the
/// answer. The setup window's Permissions step stays for setup and Settings ›
/// Privacy; both use FirstRunPermissions, so the rules live in one place.
enum NotchPermissionPage {
    typealias Item = FirstRunPermissions.Item
    typealias State = FirstRunPermissions.State

    /// Which permission a fix link's System Settings page is about. Nil for a page
    /// that isn't a permission (Internet Accounts), which opens as it always did. Pure.
    static func item(forFix url: String) -> Item? {
        guard url.hasPrefix("x-apple.systempreferences:") else { return nil }
        if url.hasSuffix("Privacy_Calendars") { return .calendar }
        if url.hasSuffix("Privacy_Reminders") { return .reminders }
        if url.hasSuffix("Privacy_Automation") { return .mail }
        if url.hasSuffix("Privacy_AllFiles") { return .fullDiskAccess }
        if url.hasSuffix("Privacy_Contacts") { return .contacts }
        if url.hasSuffix("Privacy_ScreenCapture") { return .screenRecording }
        if url.hasSuffix("Privacy_Accessibility") { return .accessibility }
        return nil
    }

    static func title(_ item: Item) -> String {
        switch item {
        case .calendar: return "Calendar"
        case .reminders: return "Reminders"
        case .mail: return "Mail & Notes"
        case .messages: return "Messages"
        case .fullDiskAccess: return "Full Disk Access"
        case .contacts: return "Contacts"
        case .notifications: return "Notifications"
        case .screenRecording: return "Screen Recording"
        case .accessibility: return "Accessibility"
        }
    }

    static func symbol(_ item: Item) -> String {
        switch item {
        case .calendar: return "calendar"
        case .reminders: return "checklist"
        case .mail: return "envelope"
        case .messages: return "message"
        case .fullDiskAccess: return "internaldrive"
        case .contacts: return "person.crop.circle"
        case .notifications: return "bell"
        case .screenRecording: return "rectangle.dashed.badge.record"
        case .accessibility: return "accessibility"
        }
    }

    /// What it is and why Wisp wants it.
    static func purpose(_ item: Item) -> String {
        switch item {
        case .calendar: return "Your meetings and events, for your day plan."
        case .reminders: return "What's due, and the reminders Wisp sets for you."
        case .mail: return "Emails that need a reply, and your notes, while those apps are open."
        case .messages: return "Sends the texts you approve in Ask."
        case .fullDiskAccess: return "So Wisp can read Messages, and Mail and Notes when those apps are closed."
        case .contacts: return "Names instead of phone numbers in Messages."
        case .notifications: return "Alerts for reminders you set, and when Wisp is ready."
        case .screenRecording: return "So Wisp can look at your screen when you ask about it."
        case .accessibility: return "So Wisp can read the window you're asking about."
        }
    }

    /// The button a row offers, if any.
    enum Action: Equatable { case allow, askAgain, openSettings }

    /// What a click does on screen: macOS's question, which Wisp waits on, or a
    /// surface that stays up on its own until the person leaves it.
    static func action(_ item: Item, _ state: State, waiting: Bool = false, contactsWait: Bool = false) -> Action? {
        if item == .contacts && contactsWait { return nil }
        switch state {
        case .notAsked:
            if item == .fullDiskAccess { return .openSettings }
            return waiting ? .askAgain : .allow
        case .notAllowed: return .openSettings
        case .checking, .allowed, .notOnThisMac: return nil
        }
    }

    static func actionTitle(_ action: Action, asking: Bool = false) -> String {
        if asking { return "Asking…" }
        switch action {
        case .allow: return "Allow"
        case .askAgain: return "Ask again"
        case .openSettings: return "Open System Settings"
        }
    }

    /// True when the click is answered through macOS's question and Wisp hears the
    /// answer, so the panel comes back by itself. A System Settings page has no
    /// answer to wait for, and neither do Screen Recording's and Accessibility's
    /// questions: their request returns at once and their way on is that same page.
    static func answersInPlace(_ item: Item, _ state: State) -> Bool {
        guard state == .notAsked else { return false }
        switch item {
        case .calendar, .reminders, .contacts, .notifications, .mail, .messages: return true
        case .fullDiskAccess, .screenRecording, .accessibility: return false
        }
    }

    /// After a System Settings page: the panel comes back by itself only for a
    /// new yes, once the person has left that page. Anything else waits for them
    /// to open the panel, rather than dropping over whatever they moved on to.
    static func comesBack(before: State, now: State) -> Bool { now == .allowed && before != .allowed }

    /// The sentence under a row's purpose: what happens next, in the state it is in.
    static func next(_ item: Item, _ state: State, waiting: Bool = false, reset: Bool = false,
                     unanswered: Bool = false, contactsWait: Bool = false) -> String {
        if item == .contacts && contactsWait {
            return "It's useful once Full Disk Access lets Wisp read Messages, so it waits for that."
        }
        switch state {
        case .checking: return ""
        case .allowed: return "Wisp has this. There's nothing to do."
        case .notOnThisMac: return "Nothing on this Mac needs it yet."
        case .notAsked:
            if waiting { return FirstRunPermissions.automationWaitingNote }
            if reset { return FirstRunPermissions.automationResetNote }
            if unanswered { return "macOS didn't show its question. Quit and reopen Wisp, then click Allow again." }
            switch item {
            case .fullDiskAccess:
                return "macOS has no question for this. In the page that opens, switch on Wisp. " + relaunchNote
            case .screenRecording:
                return "Wisp tucks away so you can answer macOS. " + relaunchNote
            case .mail:
                return "Wisp tucks away so you can answer macOS. Mail and Notes open for a moment to ask, then close."
            case .messages:
                return "Wisp tucks away so you can answer macOS. Messages opens for a moment to ask, then closes."
            default:
                return "Wisp tucks away so you can answer macOS. It asks once."
            }
        case .notAllowed:
            let page = "Wisp is off in System Settings. In the page that opens, switch on Wisp."
            return item == .fullDiskAccess || item == .screenRecording ? page + " " + relaunchNote : page
        }
    }

    /// Full Disk Access and Screen Recording, on the page: said before the Settings
    /// page opens, because Quit & Reopen would otherwise look like a crash.
    static let relaunchNote = "macOS may ask Wisp to quit and reopen. That's expected, and this page comes back."

    /// The order the page lists them in: the one it opened on first, then the
    /// setup window's order.
    static func order(focus: Item?) -> [Item] {
        guard let focus else { return Item.allCases }
        return [focus] + Item.allCases.filter { $0 != focus }
    }

    /// How long a page set aside for System Settings waits, as a tab does when
    /// the panel closes (NotchTabMemory). Later, the panel opens as usual.
    static let staysFor = NotchTabMemory.hoverWindow

    static func stillWaiting(since: Date, at date: Date) -> Bool { date.timeIntervalSince(since) <= staysFor }

    // MARK: Quit & Reopen

    /// Which permission's System Settings page the person went to, so the page
    /// comes back if macOS's Quit & Reopen ends Wisp meanwhile.
    static let resumeItemKey = "wisp.permissionPage.resumeItem"
    static let resumeAtKey = "wisp.permissionPage.resumeAt"

    static func noteResume(_ item: Item, defaults: UserDefaults = .standard, now: Date = Date()) {
        defaults.set(item.rawValue, forKey: resumeItemKey)
        defaults.set(now.timeIntervalSince1970, forKey: resumeAtKey)
    }

    static func clearResume(defaults: UserDefaults = .standard) {
        defaults.removeObject(forKey: resumeItemKey)
        defaults.removeObject(forKey: resumeAtKey)
    }

    /// The permission to show at launch, once: only a mark as recent as the
    /// privacy page Wisp sent the person to (PrivacyRelaunch.paneWindow).
    static func takeResume(defaults: UserDefaults = .standard, now: Date = Date()) -> Item? {
        defer { clearResume(defaults: defaults) }
        guard let raw = defaults.string(forKey: resumeItemKey), let item = Item(rawValue: raw) else { return nil }
        let at = defaults.double(forKey: resumeAtKey)
        guard at <= now.timeIntervalSince1970, now.timeIntervalSince1970 - at <= PrivacyRelaunch.paneWindow else { return nil }
        return item
    }
}

/// The page's state. It lives on OverlayModel, not in the view, because the view
/// goes away while the panel is tucked aside and a question may still be up.
@MainActor
final class NotchPermissionPageModel: ObservableObject {
    typealias Item = NotchPermissionPage.Item

    let access = FirstRunPermissions()
    /// The permission the page opened on; nil for the whole list.
    @Published private(set) var focus: Item?
    /// Shows the page again, once there is an answer to show (AppDelegate).
    var comeBack: () -> Void = {}

    /// What the page waits for while the panel is tucked away.
    private struct Waiting {
        let token = UUID()
        let item: Item
        let before: NotchPermissionPage.State
        let inPlace: Bool
        let since: Date
    }
    private var waiting: Waiting?
    private var appObserver: NSObjectProtocol?

    /// The panel is closing for one of the page's own actions, so the page stays.
    var keepsThroughClose: Bool { waiting != nil }

    func stillWaiting(at date: Date) -> Bool {
        waiting.map { NotchPermissionPage.stillWaiting(since: $0.since, at: date) } ?? false
    }

    func opened(focus: Item?) {
        self.focus = focus
        endWaiting()
    }

    /// The person left the page: Done, another tab, or the panel closing.
    func closed() {
        endWaiting()
        NotchPermissionPage.clearResume()
    }

    /// The panel is open again, by whatever route: there is no more waiting to do.
    func panelOpened() { endWaiting() }

    /// A row's button. Calendar, Reminders and the rest show macOS's question
    /// once; once it has an answer, a click opens that permission's page in
    /// System Settings (the setup window's permissionRow does the same).
    func act(_ item: Item) {
        let state = access.state(item)
        let wait = Waiting(item: item, before: state,
                           inPlace: NotchPermissionPage.answersInPlace(item, state), since: Date())
        waiting = wait
        if wait.inPlace {
            NotchPermissionPage.clearResume()
        } else {
            NotchPermissionPage.noteResume(item)
            watchForLeavingSystemSettings()
        }
        Task { [weak self, access] in
            if state == .notAllowed && !FirstRunPermissions.settingsThroughRequest(item) {
                Permissions.openSettingsPane(FirstRunSetupView.settingsPane(item))
                await access.refresh()
            } else {
                await access.request(item)
            }
            guard let self, wait.inPlace, self.waiting?.token == wait.token else { return }
            self.endWaiting()
            self.comeBack()
        }
    }

    /// A grant made in System Settings shows when the person leaves it. A switch
    /// that makes macOS offer Quit & Reopen is answered there first, so the panel
    /// never drops over that page while it is in front.
    private func watchForLeavingSystemSettings() {
        guard appObserver == nil else { return }
        appObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            let id = app?.bundleIdentifier
            MainActor.assumeIsolated { self?.appCameForward(id) }
        }
    }

    private func appCameForward(_ bundleID: String?) {
        guard let wait = waiting, !wait.inPlace, !PrivacyRelaunch.isSystemSettings(bundleID: bundleID, path: nil) else { return }
        Task { [weak self, access] in
            await access.refresh()
            guard let self, self.waiting?.token == wait.token,
                  NotchPermissionPage.comesBack(before: wait.before, now: access.state(wait.item)) else { return }
            self.endWaiting()
            self.comeBack()
        }
    }

    private func endWaiting() {
        waiting = nil
        if let appObserver { NSWorkspace.shared.notificationCenter.removeObserver(appObserver) }
        appObserver = nil
    }
}
