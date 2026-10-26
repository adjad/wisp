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

/// Whether to show it, and what it takes over from the old first launch.
enum FirstRunSetup {
    static let doneKey = "wisp.firstRun.done"
    /// Set when the window first opens. Quitting before it is closed leaves this
    /// without doneKey, and the next launch opens the window again.
    static let startedKey = "wisp.firstRun.started"
    static let setupGuideShownKey = "wisp.setupGuide.shown"
    static let notchTipShownKey = "wisp.notchTip.shown"
    static let permissionsRequestedKey = "wisp.didRequestPermissions"

    /// On a Mac that has never run Wisp, and again after quitting part-way through
    /// it. Anyone who saw the old setup card or the notch tip without ever starting
    /// this window is an existing user and never gets it.
    static func shouldShow(_ flag: (String) -> Bool) -> Bool {
        guard !flag(doneKey) else { return false }
        return flag(startedKey) || (!flag(setupGuideShownKey) && !flag(notchTipShownKey))
    }

    /// The old first launch asked for Calendar, Reminders and Notifications as soon
    /// as the notch appeared, showed a tip before Wisp could answer, and opened the
    /// setup card. This window does all three, in order, so mark them as handled.
    static func takeOverLaunchPrompts(_ set: (String) -> Void) {
        for key in [startedKey, permissionsRequestedKey, setupGuideShownKey, notchTipShownKey] { set(key) }
    }
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
        case .calendar: return [.calendar, .reminders]
        case .mail: return [.mail, .notes]
        case .contacts: return [.contacts]
        case .messages, .notifications: return []
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
    case failed(model: String, label: String, message: String)
    case finishing(model: String, label: String)
    case ready(label: String)

    var isReady: Bool { if case .ready = self { return true } else { return false } }

    /// Step 1 is waiting on the person's one click: Download, or Retry after a stop.
    var waitsForDownloadClick: Bool {
        switch self {
        case .needsDownload, .failed: return true
        default: return false
        }
    }

    static func from(status: SetupStatus?, downloads: [SetupGuideModel.DownloadTask]) -> FirstRunBrain {
        guard let status else { return .checking }
        if status.ready {
            let model = status.roles.first(where: { $0.endpoint == "local" })?.model ?? status.models.recommended
            let label = status.models.installed.first(where: { $0.id == model })?.label ?? model
            return .ready(label: label)
        }
        guard status.omlx.bundled == true else {
            return .unavailable("Wisp is set to use another app for its model. Use the detailed setup to finish.")
        }
        if let engine = status.bundledEngine, !engine.available {
            return .unavailable(engine.unavailableReason ?? "Wisp's built-in engine can't run on this Mac.")
        }
        if !status.omlx.running { return .startingEngine }
        let model = status.models.download?.model ?? status.models.recommended
        let label = status.models.download?.label ?? status.models.recommendedLabel ?? model
        if status.models.installed.contains(where: { $0.id == model }) {
            return .finishing(model: model, label: label)
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
                return .finishing(model: model, label: label)
            default:
                return .failed(model: model, label: label,
                               message: task.message ?? "The download stopped. Choose Retry to pick up where it left off.")
            }
        }
        return .needsDownload(model: model, label: label, sizeGb: status.models.download?.sizeGb)
    }

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

// MARK: - Permissions

@MainActor
final class FirstRunPermissions: ObservableObject {
    enum Item: String, CaseIterable, Identifiable {
        case calendar, mail, messages, contacts, notifications
        var id: String { rawValue }
    }
    enum State: Equatable { case checking, notAsked, allowed, notAllowed, notOnThisMac }

    @Published private(set) var states: [Item: State] = [:]
    @Published private(set) var asking: Item?
    private var activeObserver: NSObjectProtocol?
    private var poll: Task<Void, Never>?

    func state(_ item: Item) -> State { states[item] ?? .checking }
    var allowedCount: Int { Item.allCases.filter { state($0) == .allowed }.count }

    /// Re-read while the step is visible: grants made in System Settings arrive
    /// when the person comes back to Wisp.
    func start() {
        guard poll == nil else { return }
        activeObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { Task { await self?.refresh() } }
        }
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
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
        next[.calendar] = Self.calendarState()
        next[.contacts] = Self.contactsState()
        next[.messages] = Self.messagesState()
        next[.notifications] = await Self.notificationsState()
        next[.mail] = await Self.automationState(ask: false)
        states = next
    }

    func request(_ item: Item) async {
        guard asking == nil else { return }
        asking = item
        defer { asking = nil }
        switch item {
        case .calendar:
            let store = EKEventStore()
            _ = try? await store.requestFullAccessToEvents()
            _ = try? await store.requestFullAccessToReminders()
        case .contacts:
            _ = try? await CNContactStore().requestAccess(for: .contacts)
        case .notifications:
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
        case .mail:
            // macOS asks about Automation only for an app that is running, so start
            // Mail and Notes in the background first.
            for id in Self.automationTargets { await Self.launchHidden(id) }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            _ = await Self.automationState(ask: true)
        case .messages:
            // Full Disk Access can't be asked for: open its page and let the person
            // switch Wisp on. The poll notices when they come back.
            Permissions.openFullDiskAccessPane()
        }
        await refresh()
    }

    private static func calendarState() -> State {
        let events = EKEventStore.authorizationStatus(for: .event)
        let reminders = EKEventStore.authorizationStatus(for: .reminder)
        if events == .fullAccess && reminders == .fullAccess { return .allowed }
        if events == .notDetermined || reminders == .notDetermined { return .notAsked }
        return .notAllowed
    }

    private static func contactsState() -> State {
        switch CNContactStore.authorizationStatus(for: .contacts) {
        case .authorized: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    /// Full Disk Access shows only in whether chat.db can be opened. A missing file
    /// means Messages isn't used on this Mac, which is not something to fix.
    nonisolated static func messagesState(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> State {
        let path = home.appendingPathComponent("Library/Messages/chat.db").path
        let fd = open(path, O_RDONLY)
        if fd >= 0 { close(fd); return .allowed }
        return errno == ENOENT ? .notOnThisMac : .notAllowed
    }

    private static func notificationsState() async -> State {
        switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
        case .authorized, .provisional: return .allowed
        case .notDetermined: return .notAsked
        default: return .notAllowed
        }
    }

    static let automationTargets = ["com.apple.mail", "com.apple.Notes"]

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
    @State private var step: Step = .brain
    @State private var engineStartTried = false
    @State private var appliedModel: String?
    var openDetailedSetup: () -> Void
    var finish: (_ tellAboutYou: Bool) -> Void
    /// Every permission read, so the readers waiting on a row can start.
    var permissionsChanged: ([FirstRunPermissions.Item: FirstRunPermissions.State]) -> Void = { _ in }

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
        .frame(width: 600, height: 600)
        .background(Color(white: 0.08))
        .foregroundStyle(Theme.textPrimary)
        .preferredColorScheme(.dark)
        .onChange(of: brain) { _, next in drive(next) }
        .onAppear {
            drive(brain)
            // One read up front, so access answered before a quit lets its readers start.
            Task { await permissions.refresh() }
        }
        .onReceive(permissions.$states) { states in if !states.isEmpty { permissionsChanged(states) } }
        .onChange(of: step) { _, next in
            if next == .permissions { permissions.start() } else { permissions.stop() }
        }
        .onDisappear { permissions.stop() }
    }

    /// The parts of step 1 that need no click: start the engine once, and switch
    /// the model on as soon as it has arrived.
    private func drive(_ state: FirstRunBrain) {
        switch state {
        case .startingEngine where !engineStartTried && setup.busy == nil:
            engineStartTried = true
            Task { await setup.startEngine() }
        case .finishing(let model, _) where appliedModel != model && setup.busy == nil:
            appliedModel = model
            Task { await setup.apply(model) }
        default:
            break
        }
    }

    // MARK: Chrome

    private var stepBar: some View {
        HStack(spacing: 6) {
            ForEach(Step.allCases, id: \.self) { s in
                let done = s.rawValue < step.rawValue || (s == .brain && brain.isReady && step != .brain)
                Text(done ? "✓ \(title(s))" : "\(s.rawValue + 1)  \(title(s))")
                    .font(.system(size: 11.5, weight: s == step ? .semibold : .regular))
                    .foregroundStyle(s == step ? Theme.textPrimary : (done ? Color.green.opacity(0.8) : Theme.textMuted))
                    .padding(.horizontal, 9).padding(.vertical, 3)
                    .background(Capsule().fill(s == step ? Theme.chipFill : .clear))
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
        case .finishing: return "Finishing setup…"
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
                Text(brain.isReady ? "Everything runs on this Mac." : "You can keep going while this downloads.")
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
                Text("\(permissions.allowedCount) of \(FirstRunPermissions.Item.allCases.count) allowed")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                Spacer()
                Button("Back") { step = .brain }.buttonStyle(FirstRunButtonStyle())
                Button("Continue") { step = .tour }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
            case .tour:
                if brain.isReady {
                    Circle().fill(Theme.orb).frame(width: 9, height: 9)
                    Text("Model ready").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                } else if brain.waitsForDownloadClick {
                    Image(systemName: "arrow.down.circle").foregroundStyle(Theme.textMuted)
                    Text("Wisp needs its model before it can answer")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                } else {
                    ProgressView().controlSize(.small)
                    Text((compactProgress ?? "Getting the model ready…") + " · we'll let you know")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                }
                Spacer()
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

    // MARK: Step 1

    private var brainStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Welcome to Wisp",
                    "Wisp runs a private AI model on this Mac. Nothing you ask leaves your computer. "
                        + "First it needs to download that model once.")
            if let hw = setup.status?.hardware {
                card(icon: "cpu", title: "Picked for your Mac",
                     detail: "\(hw.chip) · \(hw.ramGb > 0 ? "\(hw.ramGb) GB memory" : "memory unknown")") {
                    pill("Recommended")
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
            card(icon: "magnifyingglass", title: "Checking this Mac…", detail: "This takes a few seconds on first launch.") {
                ProgressView().controlSize(.small)
            }
        case .startingEngine:
            card(icon: "bolt", title: "Starting Wisp's engine…", detail: "It's built into Wisp, so there's nothing else to install.") {
                if setup.busy == nil && engineStartTried {
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
                Button("Download") { Task { await setup.download(model) } }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(setup.busy != nil)
            }
        case .downloading(let model, let label, let fraction, let done, let total, let left):
            VStack(alignment: .leading, spacing: 0) {
                card(icon: "arrow.down.circle", title: label + String(format: " · %.1f GB", total),
                     detail: "Fast and private. The engine is built into Wisp, so there's nothing else to install.") {
                    EmptyView()
                }
                ProgressView(value: fraction).tint(Color(red: 0.56, green: 0.56, blue: 0.96)).padding(.top, 2)
                HStack {
                    Text(String(format: "%.1f of %.1f GB", done, total)
                         + (FirstRunBrain.timeLeft(left).map { " · \($0)" } ?? ""))
                    Spacer()
                    Button("Cancel") { Task { await setup.download(model, cancel: true) } }
                        .buttonStyle(.plain).foregroundStyle(Theme.textMuted)
                }
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 6)
            }
        case .failed(let model, let label, let message):
            card(icon: "arrow.clockwise.circle", title: label, detail: message) {
                Button("Retry") { Task { await setup.download(model) } }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(setup.busy != nil)
            }
        case .finishing(_, let label):
            card(icon: "checkmark.circle", title: label, detail: "Downloaded. Switching Wisp over to it…") {
                ProgressView().controlSize(.small)
            }
        case .ready(let label):
            card(icon: "checkmark.circle.fill", title: "\(label) is ready", detail: "Wisp can answer now.") {
                pill("Ready")
            }
        }
        if setup.messageIsError, let message = setup.message {
            Text(message).font(.system(size: 12)).foregroundStyle(Theme.bad).padding(.top, 6)
        } else if let error = setup.downloadError ?? setup.refreshError {
            Text(error).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).padding(.top, 6)
        }
    }

    // MARK: Step 2

    private var permissionsStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Let Wisp see your day",
                    "Each one is optional. Wisp only reads; it never sends or changes anything without your tap.")
            permissionRow(.calendar, icon: "calendar", title: "Calendar and Reminders",
                          detail: "Your day plan, and reminders it sets for you")
            permissionRow(.mail, icon: "envelope", title: "Mail and Notes",
                          detail: "Catches emails that need a reply. Opens Mail and Notes in the background to ask.")
            permissionRow(.messages, icon: "message", title: "Messages",
                          detail: messagesDetail)
            permissionRow(.contacts, icon: "person.crop.circle", title: "Contacts",
                          detail: "Names instead of phone numbers")
            permissionRow(.notifications, icon: "bell", title: "Notifications", detail: "Reminder alerts")
            Text("Screen reading and Accessibility are asked later, the first time a feature needs them.")
                .font(.system(size: 12)).foregroundStyle(Theme.textMuted).padding(.top, 8)
        }
    }

    private var messagesDetail: String {
        switch permissions.state(.messages) {
        case .notAllowed:
            return "Needs Full Disk Access. In the page that opens, switch on Wisp, then come back here."
        case .notOnThisMac: return "Messages isn't set up on this Mac."
        default: return "Texts that need a reply"
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
                Text("Not used").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            case .notAllowed:
                Button(item == .messages ? "Show me how" : "Open Settings") {
                    if item == .messages { Task { await permissions.request(.messages) } }
                    else { Permissions.openSettingsPane(Self.settingsPane(item)) }
                }
                .buttonStyle(FirstRunButtonStyle())
            case .notAsked:
                Button(permissions.asking == item ? "Asking…" : "Allow") { Task { await permissions.request(item) } }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .disabled(permissions.asking != nil)
            }
        }
    }

    /// Where to change a permission that was turned down.
    static func settingsPane(_ item: FirstRunPermissions.Item) -> String {
        let base = "com.apple.preference.security?"
        switch item {
        case .calendar: return base + "Privacy_Calendars"
        case .mail: return base + "Privacy_Automation"
        case .messages: return base + "Privacy_AllFiles"
        case .contacts: return base + "Privacy_Contacts"
        case .notifications: return "com.apple.preference.notifications"
        }
    }

    // MARK: Step 3

    private var tourStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            heading("Wisp lives at the top of your screen", "No window to find. It's always one gesture away.")
            tourPicture.padding(.bottom, 14)
            card(icon: "keyboard", title: "Open from anywhere",
                 detail: "⌥ Space to ask  ·  ⌘ ⇧ F to search everything") { EmptyView() }
            card(icon: "sparkles", title: "Try asking",
                 detail: "“What's on my calendar today?”  ·  “Any emails I need to reply to?”  ·  "
                    + "“Remind me to call the dentist tomorrow at 10”") { EmptyView() }
        }
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
