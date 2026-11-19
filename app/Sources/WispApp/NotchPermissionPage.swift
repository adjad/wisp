import AppKit

/// The permission page in the notch: what each macOS permission is, why Wisp
/// wants it, where it stands and the one thing to do next. A Brief, Day or Ask
/// row about a source Wisp can't read opens it on that permission
/// (Permissions.openFix); Settings' Mac permissions row opens the whole list.
///
/// The notch panel sits above normal windows (SurfaceLayout.windowLevel), so
/// anything macOS shows for a permission would open under it. For a question macOS
/// asks in a dialog the panel stays open and drops to a normal window's level until
/// the answer (AppDelegate.lowerForQuestion); for a System Settings page it tucks
/// away first (Permissions.stepAside) and comes back to show the answer. The setup window's Permissions step stays for setup and Settings ›
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
        case .accessibility: return PrivacyNames.accessibility
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
    /// `calendarAddOnly` is macOS's answer for Calendar (FirstRunPermissions.isAddOnly):
    /// nil reads it from this Mac.
    static func next(_ item: Item, _ state: State, waiting: Bool = false, reset: Bool = false,
                     unanswered: Bool = false, notReached: Bool = false, contactsWait: Bool = false,
                     calendarAddOnly: Bool? = nil) -> String {
        if item == .contacts && contactsWait {
            return "It's useful once Full Disk Access lets Wisp read Messages, so it waits for that."
        }
        switch state {
        case .checking: return ""
        case .allowed: return "Wisp has this. There's nothing to do."
        case .notOnThisMac: return "Nothing on this Mac needs it yet."
        case .notAsked:
            if waiting { return FirstRunPermissions.automationWaitingNote }
            if notReached { return FirstRunPermissions.automationNotReachedNote(item) }
            if reset { return FirstRunPermissions.automationResetNote }
            if unanswered { return "macOS didn't show its question. Quit and reopen Wisp, then click Allow again." }
            switch item {
            case .fullDiskAccess:
                return "macOS has no question for this. In the page that opens, switch on Wisp. " + relaunchNote
            case .screenRecording:
                return "Wisp tucks away so you can answer macOS. " + relaunchNote
            case .mail:
                return "macOS asks above this page. Mail and Notes open to ask, then Notes closes. "
                    + "If you allow it, Mail stays open in the background so Wisp can read it."
            case .messages:
                return "macOS asks above this page. Messages opens for a moment to ask, then closes."
            default:
                return "macOS asks above this page, once."
            }
        case .notAllowed:
            if FirstRunPermissions.isAddOnly(item, state, calendarAddOnly: calendarAddOnly) { return calendarAddOnlyNote }
            // On macOS 27 the pane has a new name; say both so it is recognised.
            let page = item == .accessibility && PrivacyNames.renamesAccessibility
                ? "Wisp is off in System Settings, under \(PrivacyNames.accessibilityWithFormerName). "
                    + "In the page that opens, switch on Wisp."
                : "Wisp is off in System Settings. In the page that opens, switch on Wisp."
            if canStartOver(item, state) { return page + " " + startOverNote }
            return item == .fullDiskAccess || item == .screenRecording ? page + " " + relaunchNote : page
        }
    }

    /// Screen Recording and Accessibility, once asked and still not there: System
    /// Settings may already show Wisp switched on, saved for an older copy of Wisp
    /// (Permissions.startOver), and then there is no switch left to flip.
    static func canStartOver(_ item: Item, _ state: State) -> Bool {
        state == .notAllowed && (item == .screenRecording || item == .accessibility)
    }

    static let startOverTitle = "Start over"
    static let startOverNote = "If Wisp is already switched on there, macOS saved that for an older copy of Wisp. "
        + "Press Start over, then Allow, and macOS asks again for this copy."
    static let startOverFailed = "macOS didn't forget the old answer. In System Settings, remove Wisp from the list "
        + "with the minus button, then add it again."

    /// Calendar set to Add Events Only: Wisp is already on in that page, so there is
    /// no switch to flip. The button is still Open System Settings.
    static let calendarAddOnlyNote = "Wisp can only add events, not read your calendar. In the page that opens, "
        + "click Options next to Wisp and choose Full Access."

    /// Full Disk Access and Screen Recording, on the page: said before the Settings
    /// page opens, because Quit & Reopen would otherwise look like a crash.
    static let relaunchNote = "macOS may ask Wisp to quit and reopen. That's expected, and this page comes back."

    /// Which row shows its explanation; only one does at a time.
    enum RowChoice: Equatable {
        /// Nobody has clicked a row yet: openRow decides.
        case automatic
        /// A click. Nil is every row closed.
        case chosen(Item?)
    }

    /// The open row. Unprompted: the permission the page opened on, then one with
    /// something to report (macOS never showed its question), then Full Disk Access
    /// while it's off, whose explanation is what makes it make sense. Pure.
    static func openRow(_ choice: RowChoice, focus: Item?, reporting: Item?, fullDiskAccess: State) -> Item? {
        if case .chosen(let item) = choice { return item }
        if let focus { return focus }
        if let reporting { return reporting }
        return fullDiskAccess == .notAsked || fullDiskAccess == .notAllowed ? .fullDiskAccess : nil
    }

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
    /// Here rather than in the view, so a row stays open while the panel is tucked aside.
    @Published private(set) var rowChoice = NotchPermissionPage.RowChoice.automatic
    /// Shows the page again, once there is an answer to show (AppDelegate).
    var comeBack: () -> Void = {}

    /// What the page waits for while the panel is tucked away. It lasts until the
    /// panel has opened again, not until the answer: macOS can answer while the
    /// panel is still rolling up, and that close must not take the page with it.
    struct Waiting {
        let token = UUID()
        let item: Item
        let before: NotchPermissionPage.State
        let inPlace: Bool
        let since: Date
    }
    private var waiting: Waiting?
    private var appObserver: NSObjectProtocol?

    /// A close under way or still to come is for one of the page's own actions, so the page stays.
    var keepsThroughClose: Bool { waiting != nil }

    /// A click waiting on macOS's own question (Calendar, Reminders and the rest
    /// answered in a dialog): the panel stays open under it instead of tucking away.
    var waitsInPlace: Bool { waiting?.inPlace == true }

    func stillWaiting(at date: Date) -> Bool {
        waiting.map { NotchPermissionPage.stillWaiting(since: $0.since, at: date) } ?? false
    }

    func opened(focus: Item?) {
        self.focus = focus
        rowChoice = .automatic
        endWaiting()
    }

    /// A click on a row: it opens, or closes if it was the open one.
    func toggleRow(_ item: Item, open: Item?) {
        rowChoice = .chosen(open == item ? nil : item)
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
        // Whatever macOS answers, the page comes back showing this row's next step.
        rowChoice = .chosen(item)
        let state = access.state(item)
        let wait = beginWaiting(item, state: state)
        Task { [weak self, access] in
            if state == .notAllowed && !FirstRunPermissions.settingsThroughRequest(item) {
                Permissions.openSettingsPane(FirstRunSetupView.settingsPane(item))
                await access.refresh()
            } else {
                await access.request(item)
            }
            self?.answered(wait)
        }
    }

    /// Start over for Screen Recording or Accessibility: forget macOS's saved answer,
    /// so the row goes back to Allow and macOS asks again for this copy of Wisp.
    @Published private(set) var startOverFailed: Item?

    func startOver(_ item: Item) {
        let extra: Permissions.Extra
        switch item {
        case .screenRecording: extra = .screenRecording
        case .accessibility: extra = .accessibility
        default: return
        }
        rowChoice = .chosen(item)
        startOverFailed = Permissions.startOver(extra) ? nil : item
        Task { [access] in await access.refresh() }
    }

    /// The click, before anything is asked: the page now stays through the close.
    func beginWaiting(_ item: Item, state: NotchPermissionPage.State) -> Waiting {
        let wait = Waiting(item: item, before: state,
                           inPlace: NotchPermissionPage.answersInPlace(item, state), since: Date())
        waiting = wait
        if wait.inPlace {
            NotchPermissionPage.clearResume()
        } else {
            NotchPermissionPage.noteResume(item)
            watchForLeavingSystemSettings()
        }
        return wait
    }

    /// macOS has answered the question this click asked.
    func answered(_ wait: Waiting) {
        guard wait.inPlace, waiting?.token == wait.token else { return }
        comeBack()
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
        // The switch can land a moment after the person leaves System Settings (a
        // password or Touch ID sheet closing), so it is read a few times, a second
        // apart, until it shows.
        Task { [weak self, access] in
            for attempt in 0..<4 {
                if attempt > 0 { try? await Task.sleep(nanoseconds: 1_000_000_000) }
                await access.refresh()
                guard let self, self.waiting?.token == wait.token else { return }
                if NotchPermissionPage.comesBack(before: wait.before, now: access.state(wait.item)) {
                    self.comeBack()
                    return
                }
            }
        }
    }

    private func endWaiting() {
        waiting = nil
        if let appObserver { NSWorkspace.shared.notificationCenter.removeObserver(appObserver) }
        appObserver = nil
    }
}
