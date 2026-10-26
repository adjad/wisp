import AppKit
import Carbon
import Contacts
import EventKit
import SwiftUI
import UserNotifications

// The window a new user sees on Wisp's first launch: get the model, grant
// permissions, learn where Wisp lives. The model download starts first and keeps
// going in the background, because it is the longest wait; the other two steps
// happen while it runs. Engine, download and "use this model" all go through the
// shared SetupGuideModel, so the notch's setup card and the detailed setup window
// see the same state.

extension Notification.Name {
    /// Opens the first-run window again (at the step the person left) while
    /// setup is unfinished: the notch's Continue setup posts it.
    static let wispContinueFirstRun = Notification.Name("WispContinueFirstRun")
}

/// Whether to show it, and what it takes over from the old first launch.
enum FirstRunSetup {
    static let doneKey = "wisp.firstRun.done"
    /// Set when the window first opens. Quitting before it is closed leaves this
    /// without doneKey, and the next launch opens the window again.
    static let startedKey = "wisp.firstRun.started"
    static let setupGuideShownKey = "wisp.setupGuide.shown"
    static let notchTipShownKey = "wisp.notchTip.shown"
    static let permissionsRequestedKey = "wisp.didRequestPermissions"
    /// The step on screen, so a quit (or macOS reopening Wisp after Full Disk
    /// Access) comes back to it.
    static let stepKey = "wisp.firstRun.step"
    /// Launches that opened the window again by themselves.
    static let reopensKey = "wisp.firstRun.reopens"
    /// After this many, an unfinished setup stops opening at launch and is carried
    /// by the notch's quiet setup cue instead; Continue setup there still opens it.
    static let maxAutomaticReopens = 3

    /// On a Mac that has never run Wisp, and again after leaving part-way through
    /// it (a quit, a restart, or closing it before Wisp could answer), up to
    /// `maxAutomaticReopens` times. Anyone who saw the old setup card or the notch
    /// tip without ever starting this window is an existing user and never gets it.
    static func shouldShow(_ flag: (String) -> Bool, reopens: Int = 0) -> Bool {
        guard !flag(doneKey) else { return false }
        if flag(startedKey) { return reopens < maxAutomaticReopens }
        return !flag(setupGuideShownKey) && !flag(notchTipShownKey)
    }

    /// The window's height: 600 pt where it fits, else what the screen has under
    /// the menu bar and above the Dock (less a margin), never under the minimum.
    /// The steps scroll; the footer stays pinned.
    static let minimumHeight: CGFloat = 420
    static func windowHeight(room: CGFloat) -> CGFloat { max(minimumHeight, min(600, room - 60)) }

    /// Started and not finished: Continue setup opens this window again.
    static func isUnfinished(_ flag: (String) -> Bool) -> Bool { flag(startedKey) && !flag(doneKey) }

    /// Closing the window ends setup only when the person finished it or Wisp can
    /// already answer. An early close (the red button, ⌘W) before that keeps it
    /// unfinished, so it comes back instead of being silently given up.
    static func closeFinishes(finished: Bool, ready: Bool) -> Bool { finished || ready }

    /// Wisp can answer: a local model is ready, or every role runs on a remote
    /// provider (/setup/status `remote_only`). SetupStatus holds the rule.
    static func canAnswer(ready: Bool, remoteOnly: Bool) -> Bool {
        SetupStatus.canAnswer(ready: ready, remoteOnly: remoteOnly)
    }

    /// Whether the readers that make macOS ask may start on their own. While the
    /// window is up it asks row by row; after an early close they stay held, so
    /// nothing fires Calendar, Reminders, Mail and Notes prompts back to back,
    /// and the window comes back next launch to ask in context. They start once
    /// setup finished or Wisp can answer.
    static func readerGateLifts(windowUp: Bool, finished: Bool, canAnswer: Bool) -> Bool {
        !windowUp && (finished || canAnswer)
    }

    /// The notch's setup cue after one /setup/status answer, on every launch: while
    /// Wisp can't answer and no first-run window is up, the quiet cue (the `!` and
    /// a one-line row on Ask), never the full card; it goes once Wisp can answer.
    /// An open card is left to the person (it waits for Done or Later).
    static func setupCue(_ current: NotchSetupCard, canAnswer: Bool, firstRunWindowUp: Bool) -> NotchSetupCard {
        if canAnswer { return current.afterReadiness(true) }
        return !firstRunWindowUp && current == .hidden ? .later : current
    }

    /// The old first launch asked for Calendar, Reminders and Notifications as soon
    /// as the notch appeared, showed a tip before Wisp could answer, and opened the
    /// setup card. This window does all three, in order, so mark them as handled.
    static func takeOverLaunchPrompts(_ set: (String) -> Void) {
        for key in [startedKey, permissionsRequestedKey, setupGuideShownKey, notchTipShownKey] { set(key) }
    }
}

/// When Wisp offers Getting to know you on its own. The service decides whether
/// it is due (GET /profile status.offer: never finished, no recent Later, at most
/// twice); this decides whether now is a moment to show it. The flow needs only
/// the service, not the model, so it no longer waits for setup to be ready; it
/// is checked for the life of the app (service up, panel closed, wake, every
/// half hour), not by a poll that gives up.
enum KnowYouOffer {
    struct Moment: Equatable {
        var firstRunWindowUp = false
        var setupWindowUp = false
        var setupCardOpen = false
        var replying = false
        var flowOpen = false
        /// Open, or held open by typed text or a pin, or showing Search.
        var panelInUse = false
        var dismissedToMenuBar = false
        /// The person chose Tell Wisp about you this session: never offer it again.
        var chosenThisSession = false
        /// The service said About you is complete: no more checks this session.
        var profileComplete = false
    }

    /// Nothing else on screen and nothing the person is doing. Checked before
    /// asking the service, and again with its answer.
    static func quiet(_ m: Moment) -> Bool {
        !(m.firstRunWindowUp || m.setupWindowUp || m.setupCardOpen || m.replying || m.flowOpen
          || m.panelInUse || m.dismissedToMenuBar || m.chosenThisSession || m.profileComplete)
    }

    static func shouldOffer(_ m: Moment, serviceOffers: Bool) -> Bool { serviceOffers && quiet(m) }
}

/// Which source readers may start while the first-run window is up. Each of these
/// can make macOS ask for access as soon as it starts (Calendar, Reminders and
/// Contacts request it; Mail and Notes ask about Automation when they script a
/// running app), so while the window is up it is the only thing that asks: a
/// reader starts once its access has been decided there, and every one starts
/// when the window closes. Existing users never see the window, so their readers
/// start at launch as before.
struct FirstRunReaderGate {
    enum Reader: CaseIterable { case calendar, mail, reminders, notes, contacts }

    var windowUp: Bool
    var decided: (Reader) -> Bool

    func allows(_ reader: Reader) -> Bool { !windowUp || decided(reader) }

    /// The readers a permission row answers for: they start when it is decided,
    /// and read again at once when it is allowed.
    static func readers(for item: FirstRunPermissions.Item) -> [Reader] {
        switch item {
        case .calendar: return [.calendar]
        case .reminders: return [.reminders]
        case .mail: return [.mail, .notes]
        case .contacts: return [.contacts]
        case .fullDiskAccess, .notifications: return []
        }
    }

    /// An answer the row can't change by asking again.
    static func isDecided(_ state: FirstRunPermissions.State) -> Bool {
        state == .allowed || state == .notAllowed || state == .notOnThisMac
    }

    /// What macOS has recorded, for the readers whose access it tracks itself.
    /// Mail and Notes (Automation) are known only from the window's own probe.
    static func systemDecided(_ reader: Reader) -> Bool? {
        switch reader {
        case .calendar: return EKEventStore.authorizationStatus(for: .event) != .notDetermined
        case .reminders: return EKEventStore.authorizationStatus(for: .reminder) != .notDetermined
        case .contacts: return CNContactStore.authorizationStatus(for: .contacts) != .notDetermined
        case .mail, .notes: return nil
        }
    }
}

/// Step 1's state, from the setup status and the engine's downloads. Pure, so it
/// can be checked without a service.
enum FirstRunBrain: Equatable {
    case checking
    case unavailable(String)
    case startingEngine
    case needsDownload(model: String, label: String, sizeGb: Double?)
    case downloading(model: String, label: String, fraction: Double, doneGb: Double, totalGb: Double, secondsLeft: Int?)
    /// `paused`: cancelled by the person, not failed.
    case failed(model: String, label: String, message: String, paused: Bool)
    /// The download finished but the engine doesn't list the model yet: wait,
    /// never apply. Lasting long means its files are missing (Download again).
    case waitingForEngine(model: String, label: String)
    /// Installed and not in use yet: switch Wisp over to it.
    case switching(model: String, label: String)
    case ready(label: String)

    var isReady: Bool { if case .ready = self { return true } else { return false } }

    /// Step 1 is waiting on the person's one click: Download, or Retry after a stop.
    var waitsForDownloadClick: Bool {
        switch self {
        case .needsDownload, .failed: return true
        default: return false
        }
    }

    /// The model a Download or Retry click fetches.
    var modelToFetch: String? {
        switch self {
        case .needsDownload(let model, _, _), .failed(let model, _, _, _), .waitingForEngine(let model, _): return model
        default: return nil
        }
    }

    /// The state without its numbers: a download's progress is not a new stage.
    var stage: Int {
        switch self {
        case .checking: return 0
        case .unavailable: return 1
        case .startingEngine: return 2
        case .needsDownload: return 3
        case .downloading: return 4
        case .failed: return 5
        case .waitingForEngine: return 6
        case .switching: return 7
        case .ready: return 8
        }
    }

    /// The model is on its way with nothing more to click, so the person can move on.
    var isArriving: Bool {
        switch self {
        case .downloading, .waitingForEngine, .switching: return true
        default: return false
        }
    }

    /// What the last step's footer says about the model, and the one thing it
    /// offers when the model isn't on its way. A spinner only while something is
    /// really happening, so the step never waits on nothing.
    enum Footer: Equatable {
        case ready
        case working(String)
        case download
        case retry
        case tryAgain
        case detailedSetup
    }

    /// `stalled`: the step Wisp takes by itself here stopped and needs a click (the
    /// engine didn't start, switching to the model failed, or a finished download
    /// never showed up in the engine).
    func footer(stalled: Bool) -> Footer {
        switch self {
        case .ready: return .ready
        case .checking: return .working("Starting Wisp…")
        case .startingEngine: return stalled ? .tryAgain : .working("Starting the engine…")
        case .downloading(_, _, let fraction, _, _, _): return .working("Downloading · \(Int(fraction * 100))%")
        case .switching: return stalled ? .tryAgain : .working("Finishing setup…")
        case .waitingForEngine: return stalled ? .retry : .working("Finishing setup…")
        case .needsDownload: return .download
        case .failed: return .retry
        case .unavailable: return .detailedSetup
        }
    }

    static func from(status: SetupStatus?, downloads: [SetupGuideModel.DownloadTask]) -> FirstRunBrain {
        guard let status else { return .checking }
        if status.ready {
            let model = status.roles.first(where: { $0.endpoint == "local" })?.model ?? status.models.recommended
            let label = status.models.installed.first(where: { $0.id == model })?.label ?? model
            return .ready(label: plainLabel(label))
        }
        // The engine's own reason first: a Mac whose macOS is too old for it also
        // falls back to external mode, and "set to use another app" would be untrue.
        if let engine = status.bundledEngine, !engine.available {
            return .unavailable(engine.unavailableReason ?? "Wisp's built-in engine can't run on this Mac.")
        }
        guard status.omlx.bundled == true else {
            return .unavailable("Wisp is set to use another app for its model. Use the detailed setup to finish.")
        }
        // No model Wisp can download fits this Mac's memory (or it couldn't be
        // read): the engine would refuse to load one, so offer no Download.
        if status.models.fits == false, status.models.installed.isEmpty, status.models.download == nil {
            return .unavailable(status.models.warning ?? "No model Wisp can download fits this Mac.")
        }
        if !status.omlx.running { return .startingEngine }
        let model = status.models.download?.model ?? status.models.recommended
        let label = plainLabel(status.models.download?.label ?? status.models.recommendedLabel ?? model)
        if status.models.installed.contains(where: { $0.id == model }) {
            return .switching(model: model, label: label)
        }
        if let task = downloads.first(where: { $0.model == model }) {
            switch task.state {
            case "pending", "downloading":
                let total = task.totalBytes > 0 ? task.totalBytes : (status.models.download?.sizeGb ?? 0) * 1e9
                let fraction = total > 0 ? min(1, task.downloadedBytes / total) : task.progress / 100
                let left = task.bytesPerSecond > 0 && total > task.downloadedBytes
                    ? Int((total - task.downloadedBytes) / task.bytesPerSecond) : nil
                return .downloading(model: model, label: label, fraction: fraction,
                                    doneGb: task.downloadedBytes / 1e9, totalGb: total / 1e9, secondsLeft: left)
            case "completed":
                return .waitingForEngine(model: model, label: label)
            default:
                return .failed(model: model, label: label,
                               message: task.message ?? "Choose Retry to pick up where it left off.",
                               paused: task.state == "cancelled")
            }
        }
        return .needsDownload(model: model, label: label, sizeGb: status.models.download?.sizeGb)
    }

    /// The model's name without its build details: "Gemma 4 E4B", not
    /// "Gemma 4 E4B · 4-bit (oQ4e) + MTP". The detailed setup keeps the full label.
    static func plainLabel(_ label: String) -> String {
        label.components(separatedBy: " · ").first.map { $0.trimmingCharacters(in: .whitespaces) } ?? label
    }

    /// Space a download needs while it runs: the model, plus room for the engine
    /// to unpack it. About 8 GB for Gemma 4 E4B.
    static func spaceNeeded(sizeGb: Double?) -> Double { max(8, (sizeGb ?? 5.5) * 1.45) }

    /// Said before Download when the Mac is short of space, instead of failing
    /// part-way through with a generic message.
    static func diskWarning(freeBytes: Int64?, sizeGb: Double?) -> String? {
        guard let freeBytes else { return nil }
        let free = Double(freeBytes) / 1e9, needed = spaceNeeded(sizeGb: sizeGb)
        guard free < needed else { return nil }
        return String(format: "This Mac has %.1f GB free; the download needs about %.0f GB. Free up some space first.",
                      free, needed)
    }

    /// How long a cold start has taken, once it is long enough to be worth saying.
    static func waited(_ seconds: Int) -> String? {
        guard seconds >= 20 else { return nil }
        return seconds < 120 ? "\(seconds) s so far" : "\(seconds / 60) min so far"
    }

    /// A transfer that hasn't moved in this long gets "No progress".
    static let noProgressAfter: TimeInterval = 30

    /// "about 6 minutes left", rounded the way a person would say it.
    static func timeLeft(_ seconds: Int?) -> String? {
        guard let seconds, seconds > 0 else { return nil }
        if seconds < 60 { return "less than a minute left" }
        let minutes = Int((Double(seconds) / 60).rounded())
        if minutes < 60 { return minutes == 1 ? "about a minute left" : "about \(minutes) minutes left" }
        let hours = Double(minutes) / 60
        return String(format: "about %.1f hours left", hours)
    }
}

/// Whether a download's bytes are still moving. Pure, so it can be checked.
struct DownloadProgressWatch: Equatable {
    private(set) var bytes: Double = -1
    private(set) var movedAt: Date?

    mutating func observe(doneGb: Double, at date: Date) {
        if doneGb != bytes { bytes = doneGb; movedAt = date }
    }

    mutating func reset() { self = DownloadProgressWatch() }

    func stalled(at date: Date) -> Bool {
        guard let movedAt else { return false }
        return date.timeIntervalSince(movedAt) >= FirstRunBrain.noProgressAfter
    }
}

// MARK: - Permissions

@MainActor
final class FirstRunPermissions: ObservableObject {
    /// `mail` is Automation for Mail and Notes, which only helps while those apps
    /// are open; `fullDiskAccess` is what reads Messages, and Mail and Notes when
    /// they are closed.
    enum Item: String, CaseIterable, Identifiable {
        case calendar, reminders, mail, fullDiskAccess, contacts, notifications
        var id: String { rawValue }
    }
    enum State: Equatable { case checking, notAsked, allowed, notAllowed, notOnThisMac }

    @Published private(set) var states: [Item: State] = [:]
    @Published private(set) var asking: Item?
    private var activeObserver: NSObjectProtocol?
    private var poll: Task<Void, Never>?

    func state(_ item: Item) -> State { states[item] ?? .checking }
    /// The step's footer: every row is optional, so no "N of M" score, only how
    /// many could still be switched on (a Mac without Messages data is not one).
    static func summary(_ states: [Item: State]) -> String {
        let off = Item.allCases.filter { states[$0] == .notAsked || states[$0] == .notAllowed }.count
        return off == 0 ? "All optional · nothing left off" : "All optional · \(off) still off"
    }
    /// Every one has an answer from macOS, whichever it was.
    var allAnswered: Bool { Item.allCases.allSatisfy { FirstRunReaderGate.isDecided(state($0)) } }

    /// Re-read while the step is visible: grants made in System Settings arrive
    /// when the person comes back to Wisp.
    func start() {
        guard poll == nil else { return }
        activeObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { _ = Task { await self?.refresh() } }
        }
        // Ends with its owner too, not only on stop(): a view torn down without
        // onDisappear must not leave a 2 s loop running for the life of the app.
        poll = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    func stop() {
        poll?.cancel()
        poll = nil
        if let activeObserver { NotificationCenter.default.removeObserver(activeObserver) }
        activeObserver = nil
    }

    func refresh() async {
        var next: [Item: State] = [:]
        next[.calendar] = Self.eventKitState(.event)
        next[.reminders] = Self.eventKitState(.reminder)
        next[.contacts] = Self.contactsState()
        next[.fullDiskAccess] = Self.fullDiskAccessState()
        next[.notifications] = await Self.notificationsState()
        next[.mail] = await Self.automationState(ask: false)
        states = next
    }

    func request(_ item: Item) async {
        guard asking == nil else { return }
        asking = item
        defer { asking = nil }
        // A prompt for an app that isn't frontmost can open behind whatever the
        // person clicked into during the download (the RemindersWriter pattern).
        NSApp.activate(ignoringOtherApps: true)
        switch item {
        case .calendar:
            _ = try? await EKEventStore().requestFullAccessToEvents()
        case .reminders:
            _ = try? await EKEventStore().requestFullAccessToReminders()
        case .contacts:
            _ = try? await CNContactStore().requestAccess(for: .contacts)
        case .notifications:
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        case .mail:
            // macOS asks about Automation only for an app that is running, so start
            // Mail and Notes in the background first.
            for id in Self.automationTargets { await Self.launchHidden(id) }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            NSApp.activate(ignoringOtherApps: true)
            _ = await Self.automationState(ask: true)
        case .fullDiskAccess:
            // Full Disk Access can't be asked for: open its page and let the person
            // switch Wisp on. The poll notices when they come back.
            Permissions.openFullDiskAccessPane()
        }
        await refresh()
    }

    /// Calendar and Reminders are two permissions with two Settings pages, so two
    /// rows: one row for both sent a person who denied only Reminders to Calendars.
    private static func eventKitState(_ entity: EKEntityType) -> State {
        switch EKEventStore.authorizationStatus(for: entity) {
        case .fullAccess: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    private static func contactsState() -> State {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    /// What Wisp reads with Full Disk Access: Messages always, Mail and Notes when
    /// those apps are closed (MailDBReader, NotesDBReader).
    nonisolated static let fullDiskAccessPaths = [
        "Library/Messages/chat.db",
        "Library/Mail",
        "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite",
    ]

    /// Whether Messages' history really opens (TCC, not file modes: access(2)
    /// says yes either way).
    nonisolated static func messagesReadable(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        let fd = open(home.appendingPathComponent("Library/Messages/chat.db").path, O_RDONLY)
        if fd >= 0 { close(fd); return true }
        return false
    }

    /// Full Disk Access shows only in whether those files really read: any one that
    /// does means it is on. None of them existing means nothing here needs it yet.
    nonisolated static func fullDiskAccessState(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> State {
        var refused = false
        for relative in fullDiskAccessPaths {
            let url = home.appendingPathComponent(relative)
            var isDirectory: ObjCBool = false
            if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), isDirectory.boolValue {
                // A folder is read by listing it: that is what the privacy check guards.
                do { _ = try FileManager.default.contentsOfDirectory(atPath: url.path); return .allowed }
                catch { refused = true; continue }
            }
            let fd = open(url.path, O_RDONLY)
            if fd >= 0 { close(fd); return .allowed }
            if errno != ENOENT { refused = true }
        }
        return refused ? .notAllowed : .notOnThisMac
    }

    private static func notificationsState() async -> State {
        switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
        case .authorized, .provisional: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    nonisolated static let automationTargets = ["com.apple.mail", "com.apple.Notes"]

    /// Allowed only when both Mail and Notes are. A target that isn't running can't
    /// be asked about, which reads as not asked yet.
    private static func automationState(ask: Bool) async -> State {
        await Task.detached {
            var states: [OSStatus] = []
            for id in automationTargets {
                var target = AEAddressDesc()
                let created = id.withCString { pointer in
                    AECreateDesc(DescType(typeApplicationBundleID), pointer, strlen(pointer), &target)
                }
                guard created == noErr else { states.append(OSStatus(procNotFound)); continue }
                states.append(AEDeterminePermissionToAutomateTarget(&target, AEEventClass(typeWildCard),
                                                                    AEEventID(typeWildCard), ask))
                AEDisposeDesc(&target)
            }
            if states.allSatisfy({ $0 == noErr }) { return State.allowed }
            if states.contains(OSStatus(errAEEventNotPermitted)) { return State.notAllowed }
            return State.notAsked
        }.value
    }

    private static func launchHidden(_ bundleID: String) async {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else { return }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = false
        config.hides = true
        _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: config)
    }
}

// MARK: - View

struct FirstRunSetupView: View {
    enum Step: Int, CaseIterable { case brain, permissions, tour }

    @ObservedObject var setup: SetupGuideModel
    @StateObject private var permissions = FirstRunPermissions()
    @ObservedObject private var display = DisplayGeometry.shared
    @State private var step: Step
    @State private var appliedModel: String?
    @State private var openedAt = Date()
    @State private var progressWatch = DownloadProgressWatch()
    /// A finished download the engine still hadn't listed after 30 s.
    @State private var stuckDownload: String?
    /// The one automatic test once Wisp is ready (it also loads the model).
    @State private var testedOnce = false
    var openDetailedSetup: () -> Void
    var finish: (_ tellAboutYou: Bool) -> Void
    /// Every permission read, so the readers waiting on a row can start.
    var permissionsChanged: ([FirstRunPermissions.Item: FirstRunPermissions.State]) -> Void
    /// Where the person is, saved so setup picks up there after a quit.
    var stepChanged: (Step) -> Void
    /// Finishes setup and asks Wisp this, from the tour's examples.
    var tryAsking: (String) -> Void
    /// Whether ⌥Space and ⌘⇧F really belong to Wisp on this Mac.
    var askKeyRegistered: Bool
    var searchKeyRegistered: Bool

    init(setup: SetupGuideModel, initialStep: Step = .brain,
         openDetailedSetup: @escaping () -> Void,
         finish: @escaping (_ tellAboutYou: Bool) -> Void,
         permissionsChanged: @escaping ([FirstRunPermissions.Item: FirstRunPermissions.State]) -> Void = { _ in },
         stepChanged: @escaping (Step) -> Void = { _ in },
         tryAsking: @escaping (String) -> Void = { _ in },
         askKeyRegistered: Bool = true, searchKeyRegistered: Bool = true) {
        self.setup = setup
        _step = State(initialValue: initialStep)
        self.openDetailedSetup = openDetailedSetup
        self.finish = finish
        self.permissionsChanged = permissionsChanged
        self.stepChanged = stepChanged
        self.tryAsking = tryAsking
        self.askKeyRegistered = askKeyRegistered
        self.searchKeyRegistered = searchKeyRegistered
    }

    private var brain: FirstRunBrain { FirstRunBrain.from(status: setup.status, downloads: setup.downloads) }

    var body: some View {
        VStack(spacing: 0) {
            stepBar
            Divider().overlay(Theme.hairline)
            ScrollView {
                Group {
                    switch step {
                    case .brain: brainStep
                    case .permissions: permissionsStep
                    case .tour: tourStep
                    }
                }
                .padding(.horizontal, 28).padding(.vertical, 24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider().overlay(Theme.hairline)
            footer
        }
        .frame(width: 600)
        .frame(minHeight: FirstRunSetup.minimumHeight, maxHeight: .infinity)
        .background(Color(white: 0.08))
        .foregroundStyle(Theme.textPrimary)
        .preferredColorScheme(.dark)
        .onChange(of: brain) { _, next in
            drive(next)
            if case .downloading(_, _, _, let done, _, _) = next {
                progressWatch.observe(doneGb: done, at: Date())
            } else {
                progressWatch.reset()
            }
        }
        // An error belongs to the stage it happened in: a red line from a failed
        // start or download must not sit under the next one.
        .onChange(of: brain.stage) { _, _ in
            announce(brain)
            if setup.busy == nil, setup.messageIsError {
                setup.message = nil
                setup.messageIsError = false
            }
        }
        .onAppear {
            drive(brain)
            // One read up front, so access answered before a quit lets its readers start.
            Task { await permissions.refresh() }
            if step == .permissions { permissions.start() }
        }
        .onReceive(permissions.$states) { states in if !states.isEmpty { permissionsChanged(states) } }
        .onChange(of: step) { _, next in
            if next == .permissions { permissions.start() } else { permissions.stop() }
            stepChanged(next)
        }
        .onDisappear { permissions.stop() }
    }

    /// The parts of step 1 that need no click: switch the model on as soon as it
    /// has arrived, and notice a finished download the engine never lists. Starting
    /// the engine is SetupGuideModel's own (autoAdvanceFirstSetup), once a session.
    private func drive(_ state: FirstRunBrain) {
        switch state {
        case .switching(let model, _) where appliedModel != model && setup.busy == nil:
            appliedModel = model
            Task { await setup.apply(model) }
        case .ready where !testedOnce && setup.busy == nil && setup.statusIsCurrent:
            // "Ready" only means the engine lists the model. One short question
            // proves it answers, and loads it while the person reads the rest, so
            // the first real question doesn't pay the cold load.
            testedOnce = true
            Task { await setup.test() }
        case .waitingForEngine(let model, _):
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                if case .waitingForEngine(let current, _) = brain, current == model { stuckDownload = model }
            }
        default:
            break
        }
    }

    // MARK: Chrome

    /// A step is ticked for what is true, not for having been passed: the model
    /// once Wisp can answer with it, permissions once every one has an answer.
    private func isDone(_ s: Step) -> Bool {
        switch s {
        case .brain: return brain.isReady
        case .permissions: return permissions.allAnswered
        case .tour: return false
        }
    }

    private var stepBar: some View {
        HStack(spacing: 6) {
            ForEach(Step.allCases, id: \.self) { s in
                let done = isDone(s)
                Button { step = s } label: {
                    Text(done ? "✓ \(title(s))" : "\(s.rawValue + 1)  \(title(s))")
                        .font(.system(size: 11.5, weight: s == step ? .semibold : .regular))
                        .foregroundStyle(s == step ? Theme.textPrimary : (done ? Color.green.opacity(0.8) : Theme.textMuted))
                        .padding(.horizontal, 9).padding(.vertical, 3)
                        .background(Capsule().fill(s == step ? Theme.chipFill : .clear))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Step \(s.rawValue + 1) of \(Step.allCases.count), \(title(s))")
                .accessibilityValue(done ? "Done" : "")
                .accessibilityAddTraits(s == step ? [.isSelected, .isHeader] : [])
            }
            Spacer()
            if step != .brain, let progress = compactProgress {
                Text(progress).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private func title(_ s: Step) -> String {
        switch s {
        case .brain: return "Model"
        case .permissions: return "Permissions"
        case .tour: return "How to use"
        }
    }

    /// The download, in a word or two, on the steps after the first.
    private var compactProgress: String? {
        switch brain {
        case .downloading(_, _, let fraction, _, _, _): return "Downloading · \(Int(fraction * 100))%"
        case .waitingForEngine, .switching: return "Finishing setup…"
        case .ready: return "Model ready"
        case .failed: return "Download stopped"
        default: return nil
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            switch step {
            case .brain:
                Image(systemName: "lock").foregroundStyle(Theme.textMuted)
                Text(brain.isArriving ? "You can keep going while this downloads." : "The AI model runs on this Mac.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                Spacer()
                if brain.waitsForDownloadClick {
                    // Return belongs to the card's Download (or Retry) here, so it can't
                    // skip past the one step Wisp can't answer without.
                    Button("Skip for now") { step = .permissions }
                        .buttonStyle(FirstRunButtonStyle())
                } else {
                    Button("Continue") { step = .permissions }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                }
            case .permissions:
                Text(FirstRunPermissions.summary(permissions.states))
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                Spacer()
                Button("Back") { step = .brain }.buttonStyle(FirstRunButtonStyle())
                Button("Continue") { step = .tour }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
            case .tour:
                Button("Back") { step = .permissions }.buttonStyle(FirstRunButtonStyle())
                tourModelStatus
                Spacer(minLength: 6)
                // Neither waits for the model: Getting to know you only saves to
                // Wisp's service, and the notch's setup card carries the download
                // from here, with a notification once Wisp can answer.
                Button("Tell Wisp about you") { finish(true) }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(setup.status == nil)
                Button("Start using Wisp") { finish(false) }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 22).padding(.vertical, 12)
    }

    /// A missed poll, said for this window: it has no Check again button (the
    /// shared model's wording names one), and it does try again by itself. While
    /// nothing has answered yet, the Starting Wisp card already says so.
    private var transientNote: String? {
        if setup.downloadError != nil {
            return "Wisp couldn't check the download just now. It tries again by itself."
        }
        if setup.refreshError != nil, setup.status != nil {
            return "Wisp's service didn't answer just now. This updates by itself."
        }
        return nil
    }

    /// Wisp's own step for this state stopped and needs the person's click.
    private var stalled: Bool {
        guard setup.busy == nil else { return false }
        switch brain {
        case .startingEngine: return setup.autoStartedEngine || setup.messageIsError
        case .switching(let model, _): return appliedModel == model && setup.messageIsError
        case .waitingForEngine(let model, _): return stuckDownload == model
        default: return false
        }
    }

    /// The model, on the last step: a spinner only while it is really on its way,
    /// otherwise the one click that gets it going, so the step never waits on nothing.
    @ViewBuilder private var tourModelStatus: some View {
        switch brain.footer(stalled: stalled) {
        case .ready:
            Circle().fill(Theme.orb).frame(width: 9, height: 9)
            Text("Model ready").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
        case .working(let text):
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.textMuted).lineLimit(1)
        case .download:
            fetchButton("Download the model")
        case .retry:
            if case .waitingForEngine = brain { fetchButton("Download again") } else { fetchButton("Retry download") }
        case .tryAgain:
            if case .switching(let model, _) = brain {
                Button("Switch to the model") { Task { await setup.apply(model) } }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(setup.busy != nil)
            } else {
                Button("Start the engine") { Task { await setup.startEngine() } }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(setup.busy != nil)
            }
        case .detailedSetup:
            Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle())
        }
    }

    /// Step 1's Download or Retry. While the request to start it is in flight
    /// (up to a few minutes when the engine is starting too) it shows that,
    /// instead of a button that has merely gone grey.
    @ViewBuilder private func downloadButton(_ title: String, model: String) -> some View {
        if setup.busy == "Starting download…" {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Starting download…").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
        } else {
            Button(title) { Task { await setup.download(model) } }
                .buttonStyle(FirstRunButtonStyle(primary: true))
                .keyboardShortcut(.defaultAction)
                .disabled(setup.busy != nil)
        }
    }

    /// Free space on the volume the engine keeps its models on (the home folder's).
    static func freeBytes() -> Int64? {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return try? home.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
            .volumeAvailableCapacityForImportantUsage
    }

    @ViewBuilder private func fetchButton(_ title: String) -> some View {
        if let model = brain.modelToFetch {
            Button(title) { Task { await setup.download(model) } }
                .buttonStyle(FirstRunButtonStyle())
                .disabled(setup.busy != nil)
        }
    }

    // MARK: Step 1

    private var brainStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Welcome to Wisp",
                    "The AI model runs on this Mac. Your mail, calendar and messages stay here; when you ask "
                        + "for something from the web, only the search terms are sent. First Wisp needs to "
                        + "download that model once.")
            if let hw = setup.status?.hardware {
                card(icon: "cpu", title: "Picked for your Mac",
                     detail: "\(hw.chip) · \(hw.ramGb > 0 ? "\(hw.ramGb) GB memory" : "memory unknown")") {
                    EmptyView()
                }
            }
            modelCard
            if let warning = setup.status?.models.warning {
                Text(warning).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.top, 4)
            }
            if case .unavailable = brain {} else {
                Button { openDetailedSetup() } label: {
                    Text("Using a different app like Ollama or LM Studio? ")
                        .foregroundStyle(Theme.textMuted)
                    + Text("Connect it instead").foregroundStyle(Color(red: 0.73, green: 0.74, blue: 0.96))
                }
                .buttonStyle(.plain).font(.system(size: 12)).padding(.top, 12)
            }
        }
    }

    @ViewBuilder private var modelCard: some View {
        switch brain {
        case .checking:
            // Wisp's service is still starting: a cold first launch (bundle checks,
            // first imports) can take minutes, so say so and show the time.
            TimelineView(.periodic(from: openedAt, by: 5)) { context in
                let waited = FirstRunBrain.waited(Int(context.date.timeIntervalSince(openedAt)))
                card(icon: "hourglass", title: "Starting Wisp…",
                     detail: "The first launch can take a few minutes." + (waited.map { " (\($0))" } ?? "")) {
                    ProgressView().controlSize(.small)
                }
            }
        case .startingEngine:
            card(icon: "bolt", title: "Starting Wisp's engine…", detail: "It's built into Wisp, so there's nothing else to install.") {
                if stalled {
                    Button("Try again") { Task { await setup.startEngine() } }.buttonStyle(FirstRunButtonStyle())
                } else {
                    ProgressView().controlSize(.small)
                }
            }
        case .unavailable(let reason):
            card(icon: "exclamationmark.triangle", title: "Wisp can't set this up on its own", detail: reason) {
                Button("Open detailed setup") { openDetailedSetup() }.buttonStyle(FirstRunButtonStyle(primary: true))
            }
        case .needsDownload(let model, let label, let size):
            card(icon: "arrow.down.circle", title: label + (size.map { String(format: " · %.1f GB", $0) } ?? ""),
                 detail: "Fast and private. The engine is built into Wisp, so there's nothing else to install.") {
                HStack(spacing: 8) {
                    pill("Recommended")
                    downloadButton("Download", model: model)
                }
            }
            if let warning = FirstRunBrain.diskWarning(freeBytes: Self.freeBytes(), sizeGb: size) {
                Text(warning).font(.system(size: 12)).foregroundStyle(Color.orange).padding(.bottom, 4)
            }
        case .downloading(let model, let label, let fraction, let done, let total, let left):
            VStack(alignment: .leading, spacing: 0) {
                card(icon: "arrow.down.circle", title: label + String(format: " · %.1f GB", total),
                     detail: "Fast and private. The engine is built into Wisp, so there's nothing else to install.") {
                    EmptyView()
                }
                ProgressView(value: fraction).tint(Color(red: 0.56, green: 0.56, blue: 0.96)).padding(.top, 2)
                    .accessibilityLabel("Model download")
                    .accessibilityValue("\(Int(fraction * 100)) percent")
                HStack {
                    TimelineView(.periodic(from: .now, by: 5)) { context in
                        if progressWatch.stalled(at: context.date) {
                            Text(String(format: "%.1f of %.1f GB", done, total) + " · No progress — check your connection")
                                .foregroundStyle(Color.orange)
                        } else {
                            Text(String(format: "%.1f of %.1f GB", done, total)
                                 + (FirstRunBrain.timeLeft(left).map { " · \($0)" } ?? ""))
                        }
                    }
                    Spacer()
                    Button("Cancel") { Task { await setup.download(model, cancel: true) } }
                        .buttonStyle(.plain).foregroundStyle(Theme.textMuted)
                }
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 6)
            }
        case .failed(let model, let label, let message, let paused):
            card(icon: "arrow.clockwise.circle", title: (paused ? "Download paused" : "Download failed") + " · " + label,
                 detail: message) {
                downloadButton("Retry", model: model)
            }
        case .waitingForEngine(let model, let label):
            if stuckDownload == model {
                card(icon: "exclamationmark.triangle", title: label,
                     detail: "Downloaded, but Wisp's engine can't find its files. Download it again to fix that.") {
                    Button("Download again") { Task { await setup.download(model) } }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                        .disabled(setup.busy != nil)
                }
            } else {
                card(icon: "checkmark.circle", title: label, detail: "Downloaded. Waiting for Wisp's engine to load it…") {
                    ProgressView().controlSize(.small)
                }
            }
        case .switching(let model, let label):
            if stalled {
                card(icon: "exclamationmark.triangle", title: label,
                     detail: "Downloaded, but Wisp couldn't switch over to it.") {
                    Button("Try again") { Task { await setup.apply(model) } }
                        .buttonStyle(FirstRunButtonStyle(primary: true))
                        .keyboardShortcut(.defaultAction)
                        .disabled(setup.busy != nil)
                }
            } else {
                card(icon: "checkmark.circle", title: label, detail: "Downloaded. Switching Wisp over to it…") {
                    ProgressView().controlSize(.small)
                }
            }
        case .ready(let label):
            card(icon: "checkmark.circle.fill", title: "\(label) is ready",
                 detail: setup.testing ? "Checking it answers…" : (setup.testResult ?? "Wisp can answer now.")) {
                if setup.testing {
                    ProgressView().controlSize(.small)
                } else if setup.testResult != nil && !setup.testPassed {
                    Button("Try again") { Task { await setup.test() } }.buttonStyle(FirstRunButtonStyle())
                } else {
                    pill("Ready")
                }
            }
        }
        if setup.messageIsError, let message = setup.message {
            Text(message).font(.system(size: 12)).foregroundStyle(Theme.bad).padding(.top, 6)
        } else if let note = transientNote {
            Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.top, 6)
        }
    }

    // MARK: Step 2

    private var permissionsStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Let Wisp see your day",
                    "Each one is optional. Wisp reads these to help; it asks before sending or changing anything.")
            permissionRow(.calendar, icon: "calendar", title: "Calendar",
                          detail: "Your day plan: meetings and events")
            permissionRow(.reminders, icon: "checklist", title: "Reminders",
                          detail: "What's due, and reminders it sets for you")
            permissionRow(.mail, icon: "envelope", title: "Mail and Notes",
                          detail: "While they're open: emails that need a reply, and your notes. "
                              + "Opens Mail and Notes in the background to ask.")
            permissionRow(.fullDiskAccess, icon: "internaldrive", title: "Full Disk Access",
                          detail: fullDiskAccessDetail)
            permissionRow(.contacts, icon: "person.crop.circle", title: "Contacts",
                          detail: "Names instead of phone numbers")
            permissionRow(.notifications, icon: "bell", title: "Notifications", detail: "Reminder alerts")
            Text("Screen reading and Accessibility are asked later, the first time a feature needs them.")
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 8)
        }
    }

    /// Said before the Settings page opens: switching it on can make macOS reopen
    /// Wisp, and setup comes back at this step when it does.
    private var fullDiskAccessDetail: String {
        switch permissions.state(.fullDiskAccess) {
        case .notAllowed:
            return "So Wisp can read Messages, and Mail and Notes when those apps are closed. In the page "
                + "that opens, switch on Wisp. macOS may ask to reopen Wisp; setup picks up where you left off."
        case .notOnThisMac: return "Nothing here needs it yet: no Messages, Mail or Notes data on this Mac."
        default: return "So Wisp can read Messages, and Mail and Notes when those apps are closed."
        }
    }

    private func permissionRow(_ item: FirstRunPermissions.Item, icon: String, title: String, detail: String) -> some View {
        card(icon: icon, title: title, detail: detail) {
            switch permissions.state(item) {
            case .allowed:
                Label("Allowed", systemImage: "checkmark").font(.system(size: 12)).foregroundStyle(Color.green.opacity(0.85))
            case .checking:
                ProgressView().controlSize(.small)
            case .notOnThisMac:
                Text(item == .fullDiskAccess ? "Not needed" : "Not used").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            case .notAllowed:
                Button(item == .fullDiskAccess ? "Show me how" : "Open Settings") {
                    if item == .fullDiskAccess { Task { await permissions.request(.fullDiskAccess) } }
                    else { Permissions.openSettingsPane(Self.settingsPane(item)) }
                }
                .buttonStyle(FirstRunButtonStyle())
                .accessibilityLabel(item == .fullDiskAccess ? "Show how to turn on \(title)" : "Open Settings for \(title)")
            case .notAsked:
                Button(permissions.asking == item ? "Asking…" : "Allow") { Task { await permissions.request(item) } }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .disabled(permissions.asking != nil)
                    .accessibilityLabel("Allow \(title)")
            }
        }
    }

    /// VoiceOver hears the moments that matter without hunting for them: the
    /// model ready, or its download stopping.
    private func announce(_ state: FirstRunBrain) {
        let text: String
        switch state {
        case .ready(let label): text = "\(label) is ready"
        case .failed(_, _, _, let paused): text = paused ? "Download paused" : "Download failed"
        case .unavailable: text = "Wisp can't set this up on its own"
        default: return
        }
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    /// Where to change a permission that was turned down.
    static func settingsPane(_ item: FirstRunPermissions.Item) -> String {
        let base = "com.apple.preference.security?"
        switch item {
        case .calendar: return base + "Privacy_Calendars"
        case .reminders: return base + "Privacy_Reminders"
        case .mail: return base + "Privacy_Automation"
        case .fullDiskAccess: return Permissions.fullDiskAccessPane
        case .contacts: return base + "Privacy_Contacts"
        case .notifications: return "com.apple.preference.notifications"
        }
    }

    // MARK: Step 3

    private var tourStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Wisp lives at the top of your screen", "No window to find. It's always one gesture away.")
            tourPicture.padding(.bottom, 14)
            if case .working = brain.footer(stalled: stalled) {
                Text("You can start now. Wisp lets you know when its model is ready.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.bottom, 10)
            }
            card(icon: "keyboard", title: "Open from anywhere",
                 detail: Self.shortcutsLine(ask: askKeyRegistered, search: searchKeyRegistered)) { EmptyView() }
            VStack(alignment: .leading, spacing: 6) {
                Text("Try asking").font(.system(size: 13, weight: .medium))
                ForEach(Self.examples(permissions.states), id: \.self) { example in
                    Button { tryAsking(example) } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "arrow.up.circle").accessibilityHidden(true)
                            Text("“\(example)”")
                            Spacer(minLength: 0)
                        }
                        .font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.04)))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(brain.isReady ? "Start using Wisp and ask this" : "Start using Wisp; this waits in Ask until the model is ready")
                }
            }
        }
    }

    /// The shortcuts this Mac really has. A combination another app owns does
    /// nothing in Wisp, so it is not taught; the top of the screen always works.
    static func shortcutsLine(ask: Bool, search: Bool) -> String {
        switch (ask, search) {
        case (true, true): return "⌥ Space to ask  ·  ⌘ ⇧ F to search everything"
        case (true, false): return "⌥ Space to ask"
        case (false, true): return "Another app uses ⌥ Space, so open Wisp from the top of the screen  ·  ⌘ ⇧ F to search everything"
        case (false, false): return "Another app uses Wisp's shortcuts, so open it from the top of the screen or the menu bar"
        }
    }

    /// Examples that work with what was allowed: no calendar question without
    /// Calendar, no email one without a way to read Mail.
    static func examples(_ states: [FirstRunPermissions.Item: FirstRunPermissions.State]) -> [String] {
        var out: [String] = []
        if states[.calendar] == .allowed { out.append("What's on my calendar today?") }
        if states[.mail] == .allowed || states[.fullDiskAccess] == .allowed { out.append("Any emails I need to reply to?") }
        out.append("Remind me to call the dentist tomorrow at 10")
        return out
    }

    /// How to open Wisp on this display, in words that match what's drawn there.
    static func openHint(_ style: SurfaceLayout.Style) -> String {
        switch style {
        case .notch: return "Hover the notch to open"
        case .menuBar: return "Push the pointer to the top centre, or click the Wisp icon in the menu bar"
        case .floating: return "Hover the bar at the top of the screen to open"
        }
    }

    private var tourPicture: some View {
        let style = display.layout.style
        return ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 10).fill(Color(white: 0.05))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
            if style == .notch {
                UnevenRoundedRectangle(bottomLeadingRadius: 9, bottomTrailingRadius: 9)
                    .fill(Color.black).frame(width: 120, height: 20)
            }
            VStack(spacing: 10) {
                Label(Self.openHint(style), systemImage: "arrow.up")
                    .font(.system(size: 11.5)).foregroundStyle(Color(red: 0.73, green: 0.74, blue: 0.96))
                HStack(spacing: 6) { pill("Ask"); pill("Brief"); pill("Day") }
            }
            .padding(.top, style == .notch ? 30 : 18)
        }
        .frame(height: 100)
    }

    // MARK: Pieces

    private func heading(_ title: String, _ subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 20, weight: .semibold))
            Text(subtitle).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 18)
    }

    private func card<Trailing: View>(icon: String, title: String, detail: String,
                                      @ViewBuilder trailing: () -> Trailing) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.system(size: 15))
                .foregroundStyle(Color(red: 0.73, green: 0.74, blue: 0.96))
                .frame(width: 30, height: 30)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium))
                Text(detail).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            trailing()
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
        .padding(.bottom, 9)
    }

    private func pill(_ text: String) -> some View {
        Text(text).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke))
    }
}

struct FirstRunButtonStyle: ButtonStyle {
    var primary = false
    @Environment(\.isEnabled) private var enabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(primary ? Color.white : Theme.textPrimary)
            .padding(.horizontal, 13).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 7)
                .fill(primary ? Color(red: 0.43, green: 0.42, blue: 0.94) : Color.white.opacity(0.06)))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(primary ? .clear : Theme.chipStroke))
            .opacity(enabled ? (configuration.isPressed ? 0.8 : 1) : 0.4)
    }
}
