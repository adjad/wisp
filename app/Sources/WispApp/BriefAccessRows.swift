import AppKit
import SwiftUI

/// The quiet rows Brief and Day show for access Setup no longer asks for. Setup asks
/// for Calendar and Reminders only; the rest is asked when a task needs it:
///
///   - "Allow Wisp to read Mail": Mail & Notes Automation, asked only by this row's
///     button (the same request Setup's Mail row ran, `request(.mail)` on FirstRunPermissions),
///     never by a timer or in the background. Shown while Automation is undecided, Mail
///     is on this Mac, Google isn't covering mail and the Envelope Index can't be read.
///   - "See your texts": Full Disk Access, when Messages' history can't be opened.
///   - "See your mail and notes when they're closed": Full Disk Access again, for
///     Mail and Notes while those apps are closed and their stores can't be opened.
///
/// Texts and closed apps open the notch permission page focused on Full Disk Access
/// (Permissions.openFix, the link every Brief and Day fix uses). A row is never shown
/// when the live probe (AccessProbe) says that store reads, or can't say. Each row can
/// be put away for the rest of the day.
///
/// `decide` and the day-key helpers are pure (tests/FirstRunSetupChecks.swift); the
/// model reads the facts, the view draws them.
enum BriefAccessRows {
    enum Row: String, CaseIterable, Equatable {
        case allowMail = "allow-mail"
        case seeTexts = "see-texts"
        case seeClosed = "see-closed"
    }

    /// What the rows are decided from.
    struct Facts: Equatable {
        /// Mail & Notes Automation has no answer yet: not allowed, not refused.
        var automationUndecided = false
        /// A connected Gmail account that synced recently (MailRecache.gmailCovers).
        var gmailCovers = false
        var mailInstalled = true
        var probe: AccessProbe.State = .unknown
        var outcomes: [AccessProbe.Store: AccessProbe.Outcome] = [:]
        var mailOpen = false
        var notesOpen = false
    }

    struct Shown: Equatable {
        var allowMail = false
        var seeTexts = false
        /// Mail and/or Notes, closed and unreadable, in the order Mail, Notes.
        var closed: [AccessProbe.Store] = []

        var rows: [Row] {
            (allowMail ? [Row.allowMail] : []) + (seeTexts ? [.seeTexts] : []) + (closed.isEmpty ? [] : [.seeClosed])
        }
        var isEmpty: Bool { rows.isEmpty }
    }

    /// The stores macOS refused, from one probe. `.on` and `.unknown` refuse none.
    /// `.partial` names them; `.off` is every store that exists, which the outcomes say
    /// (all of them when they are missing). Pure.
    static func unreadable(_ state: AccessProbe.State, _ outcomes: [AccessProbe.Store: AccessProbe.Outcome]) -> Set<AccessProbe.Store> {
        switch state {
        case .on, .unknown: return []
        case .partial(let stores): return Set(stores)
        case .off:
            let refused = outcomes.filter { $0.value == .refused }.map(\.key)
            return refused.isEmpty ? Set(AccessProbe.Store.allCases) : Set(refused)
        }
    }

    /// Which rows show right now. Pure.
    static func decide(_ f: Facts) -> Shown {
        var shown = Shown()
        let indexReadable = f.outcomes[.mail] == .readable
        shown.allowMail = f.automationUndecided && !f.gmailCovers && !indexReadable && f.mailInstalled
        let unread = unreadable(f.probe, f.outcomes)
        shown.seeTexts = unread.contains(.messages)
        if unread.contains(.mail), !f.mailOpen, !f.gmailCovers { shown.closed.append(.mail) }
        if unread.contains(.notes), !f.notesOpen { shown.closed.append(.notes) }
        return shown
    }

    static let allowMailTitle = "Allow Wisp to read Mail"
    static let allowMailHelp = "Wisp asks macOS once, to read your mail and notes while those apps are open."
    static let seeTextsTitle = "See your texts"
    static let seeTextsHelp = "Wisp needs Full Disk Access to read your Messages."

    /// "See your mail and notes when they're closed". Pure.
    static func closedTitle(_ stores: [AccessProbe.Store]) -> String {
        switch (stores.contains(.mail), stores.contains(.notes)) {
        case (true, true): return "See your mail and notes when they're closed"
        case (true, false): return "See your mail when Mail is closed"
        case (false, true): return "See your notes when Notes is closed"
        case (false, false): return ""
        }
    }

    static let closedHelp = "Wisp needs Full Disk Access to read these while their apps are closed."

    static func title(_ row: Row, closed: [AccessProbe.Store] = []) -> String {
        switch row {
        case .allowMail: return allowMailTitle
        case .seeTexts: return seeTextsTitle
        case .seeClosed: return closedTitle(closed)
        }
    }

    static func symbol(_ row: Row) -> String {
        switch row {
        case .allowMail: return "envelope"
        case .seeTexts: return "message"
        case .seeClosed: return "internaldrive"
        }
    }

    // MARK: Put away for the day

    static func dismissKey(_ row: Row) -> String { "wisp.briefAccess.dismissed." + row.rawValue }

    /// "2026-11-21", in the person's calendar.
    static func dayKey(_ date: Date, calendar: Calendar = .current) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// A row put away on `stored`'s day stays away that day only. Pure.
    static func isDismissed(stored: String?, today: String) -> Bool { stored == today }

    /// Rows that remain after the ones put away today. Pure.
    static func visible(_ shown: Shown, dismissed: Set<Row>) -> Shown {
        var out = shown
        if dismissed.contains(.allowMail) { out.allowMail = false }
        if dismissed.contains(.seeTexts) { out.seeTexts = false }
        if dismissed.contains(.seeClosed) { out.closed = [] }
        return out
    }
}

/// Reads the facts and runs the one request. Nothing here asks macOS on its own: the
/// only ask is `allowMail()`, from the row's button.
@MainActor
final class BriefAccessRowsModel: ObservableObject {
    static let shared = BriefAccessRowsModel()

    @Published private(set) var shown = BriefAccessRows.Shown()
    @Published private(set) var asking = false
    /// The Setup window's own object, so the request is the very one its Mail row ran.
    private let permissions = FirstRunPermissions()
    private var live = true
    private var dismissed: Set<BriefAccessRows.Row> = []
    private var raw = BriefAccessRows.Shown()
    private var generation = 0

    init() {}

    #if WISP_NOTCH_QA
    /// A fixed set of rows for a render: reads nothing from this Mac.
    static func qaFixture(_ shown: BriefAccessRows.Shown) -> BriefAccessRowsModel {
        let model = BriefAccessRowsModel()
        model.live = false
        model.shown = shown
        return model
    }
    #endif

    /// Reads the facts again. Cheap: the probe's answer is already kept, the Automation
    /// read is `ask: false`, and Gmail is checked only when it could change a row.
    func refresh(now: Date = Date()) async {
        guard live else { return }
        generation += 1
        let mine = generation
        let monitor = AccessProbeMonitor.shared
        var facts = BriefAccessRows.Facts()
        facts.probe = monitor.state
        facts.outcomes = monitor.reading.outcomes
        facts.mailOpen = !NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.mail").isEmpty
        facts.notesOpen = !NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Notes").isEmpty
        facts.mailInstalled = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.mail") != nil
        // Mail & Notes Automation: .notAsked is "no answer yet". Reads only.
        let automation = await Task.detached { FirstRunPermissions.automationStateNow(ask: false) }.value
        facts.automationUndecided = automation == .notAsked
        // Google only matters when it would hide a mail row.
        let mailMatters = facts.automationUndecided || BriefAccessRows.unreadable(facts.probe, facts.outcomes).contains(.mail)
        if mailMatters {
            facts.gmailCovers = MailRecache.gmailCovers(await Self.gmailAccounts(), now: now)
        }
        guard mine == generation else { return }
        raw = BriefAccessRows.decide(facts)
        loadDismissed(now: now)
        publish()
    }

    private func loadDismissed(now: Date) {
        let today = BriefAccessRows.dayKey(now)
        dismissed = Set(BriefAccessRows.Row.allCases.filter {
            BriefAccessRows.isDismissed(stored: UserDefaults.standard.string(forKey: BriefAccessRows.dismissKey($0)), today: today)
        })
    }

    private func publish() {
        let next = BriefAccessRows.visible(raw, dismissed: dismissed)
        if next != shown { shown = next }
    }

    /// Put a row away until tomorrow.
    func dismiss(_ row: BriefAccessRows.Row, now: Date = Date()) {
        UserDefaults.standard.set(BriefAccessRows.dayKey(now), forKey: BriefAccessRows.dismissKey(row))
        dismissed.insert(row)
        publish()
    }

    /// The row's button: the same Mail & Notes Automation request Setup's row made.
    func allowMail() {
        guard live, !asking else { return }
        asking = true
        Task {
            await permissions.request(.mail)
            // request() tucked the notch away for macOS's question: bring it back.
            Permissions.stepBack()
            asking = false
            await refresh()
        }
    }

    /// Texts, and Mail and Notes while closed: the notch permission page, on Full Disk Access.
    func openFullDiskAccess() {
        guard live, let url = URL(string: BriefDetail.fullDiskAccess.url) else { return }
        Permissions.openFix(url)
    }

    private static func gmailAccounts() async -> [MailRecache.GmailAccount] {
        var req = URLRequest(url: WispClient.baseURL.appendingPathComponent("assistant/gmail/status"))
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return [] }
        let rows = json["accounts"] as? [[String: Any]] ?? []
        return rows.compactMap(GoogleStatus.Account.parse).map {
            MailRecache.GmailAccount(connected: $0.state == .connected, lastSync: $0.lastSync)
        }
    }
}

/// The rows, one line each, in Brief's and Day's access-row style.
struct BriefAccessRowsView: View {
    @ObservedObject private var model: BriefAccessRowsModel
    @ObservedObject private var probe = AccessProbeMonitor.shared

    init(model: BriefAccessRowsModel? = nil) {
        self.model = model ?? BriefAccessRowsModel.shared
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(model.shown.rows, id: \.rawValue) { row in line(row) }
        }
        .task { await model.refresh() }
        .onChange(of: probe.reading) { _, _ in Task { await model.refresh() } }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await model.refresh() }
        }
    }

    private func line(_ row: BriefAccessRows.Row) -> some View {
        let title = BriefAccessRows.title(row, closed: model.shown.closed)
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: BriefAccessRows.symbol(row)).font(.wisp(size: 10.5))
                .foregroundStyle(Theme.textMuted).frame(width: 15)
            Text(title).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary)
                .lineLimit(1).truncationMode(.tail)
                .help(help(row))
            Spacer(minLength: 6)
            if row == .allowMail {
                if model.asking {
                    ProgressView().controlSize(.small)
                } else {
                    NotchChrome.small(Permissions.allowLabel) { model.allowMail() }.help(help(row))
                }
            } else {
                NotchChrome.small(Permissions.allowLabel) { model.openFullDiskAccess() }.help(help(row))
            }
            Button { model.dismiss(row) } label: {
                Image(systemName: "xmark").font(.wisp(size: 9, weight: .medium)).foregroundStyle(Theme.textMuted)
            }
            .buttonStyle(.plain)
            .help("Hide until tomorrow")
            .accessibilityLabel("Hide \(title) until tomorrow")
        }
        .accessibilityElement(children: .contain)
    }

    private func help(_ row: BriefAccessRows.Row) -> String {
        switch row {
        case .allowMail: return BriefAccessRows.allowMailHelp
        case .seeTexts: return BriefAccessRows.seeTextsHelp
        case .seeClosed: return BriefAccessRows.closedHelp
        }
    }
}
