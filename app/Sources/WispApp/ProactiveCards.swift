import SwiftUI
import AppKit

// The Brief's proactive half: "Before you ask" cards, the to-do list built from
// every source, and per-thread summaries, from `workspace.proactive`
// (ProactiveModels.swift). Nothing here fires on its own: every card, check-off
// and Add to Day is one tap, and a card leaves only once the service says its
// action succeeded.

private extension Theme {
    static let cardFill = Color.white.opacity(0.055)
    static let cardStroke = Color.white.opacity(0.13)
    static let chipOn = Color.white.opacity(0.16)
    static let editorFill = Color.white.opacity(0.07)
}

// MARK: - Integration hook for the reply composer (W3)

/// HOOK(W3): the inline reply composer for a Summaries row.
///
/// `BriefReplyComposer` (W3) is hosted here. Until it is wired, the row shows a
/// placeholder pointing at Mail. To wire it, replace the placeholder branch in
/// `ProactiveBriefSection.replyArea` with the composer, or set once at launch:
///
///     BriefReplyHook.composer = { thread, close in
///         AnyView(BriefReplyComposer(replyRef: thread.replyRef, account: thread.replyAccount,
///                                    onSent: {}, onDismiss: close))
///     }
///
/// `onReply` is told whenever Reply is tapped, for whatever else wants to know.
///
/// The default composer shows the reply `ProactiveActions.shared` keeps for the
/// thread, so the notch tucking away or a tab change never drafts it again.
@MainActor
enum BriefReplyHook {
    static var composer: ((ProactiveThread, _ close: @escaping () -> Void) -> AnyView)? = { thread, close in
        AnyView(BriefReplyComposer(model: ProactiveActions.shared.replyModel(for: thread), onDismiss: close))
    }
    static var onReply: ((ProactiveThread) -> Void)? = nil
}

// MARK: - Actions

/// What the person did to cards and to-dos, shared by Brief and Day so a tab
/// switch never loses a fire in flight or an edited draft.
@MainActor
final class ProactiveActions: ObservableObject {
    static let shared = ProactiveActions()

    init() {}

    enum Fire: Equatable {
        case sending
        case done(String)
        case failed(String)
        /// Nothing was sent: Mail reported other recipients than the card
        /// showed. The card shows them now, and Send waits for a fresh tap.
        case confirm(String)
        /// A reply that may have gone out: the card says where to check and
        /// offers no second Send.
        case unconfirmed(String)
    }

    @Published var fires: [String: Fire] = [:]
    /// Drafts the person edited, by card id.
    @Published var drafts: [String: String] = [:]
    @Published var openDrafts: Set<String> = []
    /// Cards the service confirmed fired or dismissed; gone until it stops sending them.
    @Published var hidden: Set<String> = []
    @Published var dismissing: Set<String> = []
    @Published var cardErrors: [String: String] = [:]
    @Published var todoBusy: Set<String> = []
    /// Local check-offs the service confirmed, ahead of the next plan.
    @Published var todoDone: [String: Bool] = [:]
    @Published var addedToDay: Set<String> = []
    @Published var todoErrors: [String: String] = [:]
    /// Analyze now was tapped and the engine hasn't reported working yet.
    @Published var analyzeRequested: Date? = nil
    @Published var analyzeError = ""
    /// The account chip; nil is All.
    @Published var account: String? = nil
    @Published var expanded: Set<String> = []
    @Published var replying: String? = nil
    @Published var showAllTodos = false
    @Published var showAllThreads = false
    /// The card shown large; nil is the first.
    @Published var heroID: String? = nil
    /// The one of the Brief's rows that is open: "todos" or "threads".
    @Published var openRow: String? = nil
    /// Replies being written, by thread id. Kept here, not in the composer's
    /// view, so the notch tucking away or a tab change keeps the typed text and
    /// the result of a send. Not published: each composer observes its own.
    private(set) var replyModels: [String: BriefReplyModel] = [:]

    /// The reply for a thread: made on the first ask, the same one afterwards.
    func replyModel(for thread: ProactiveThread, transport: BriefReplyModel.Transport? = nil) -> BriefReplyModel {
        if let kept = replyModels[thread.id] { return kept }
        let made = BriefReplyModel(replyRef: thread.replyRef, account: thread.replyAccount,
                                   baseURL: BriefReplyModel.defaultBaseURL,
                                   transport: transport ?? BriefReplyModel.defaultTransport)
        replyModels[thread.id] = made
        return made
    }

    /// Cancel, Done or Close reply. The reply is forgotten unless its send is
    /// still out: that one is kept, so opening Reply again shows what happened
    /// to it instead of a new draft with a live Send.
    func closeReply(_ id: String) {
        if replying == id { replying = nil }
        if let kept = replyModels[id], kept.phase != .sending { replyModels[id] = nil }
    }

    /// Whether the open reply is unfinished: being drafted, holding text that
    /// hasn't been sent, or sending. The panel stays open for it. False when no
    /// reply is open, its row is closed, or it is sent.
    var replyInProgress: Bool {
        guard let id = replying, expanded.contains(id) else { return false }
        return replyModels[id]?.holdsPanelOpen == true
    }

    /// Forget replies to threads the Brief no longer lists, so one that can't
    /// be reached never keeps the panel open. A send still out is kept.
    func pruneReplies(keeping listed: Set<String>) {
        if let id = replying, !listed.contains(id) { replying = nil }
        for (id, kept) in replyModels where !listed.contains(id) && kept.phase != .sending {
            replyModels[id] = nil
        }
    }

    func draft(_ card: ProactiveCard) -> String { drafts[card.id] ?? card.action?.draft ?? "" }

    func isDone(_ todo: ProactiveTodo) -> Bool { todoDone[todo.id] ?? todo.done }

    /// A POST through the Brief's own transport (so fixtures answer it), with
    /// room for a send that takes a while. A non-2xx reply is a failed outcome.
    func post(_ today: TodayModel, _ path: String, body: [String: Any], timeout: TimeInterval = 60) async throws -> ProactiveOutcome {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await today.transport(request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let detail = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["detail"] as? String
            // The service's own sentence when it sent one; never a bare status code.
            return ProactiveOutcome(ok: false, message: detail ?? "Wisp couldn't do that. Try again.")
        }
        return ProactiveOutcome.parse(data)
    }

    static func segment(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#"))) ?? id
    }

    /// Fire a card with the (possibly edited) draft. The card stays until the
    /// service reports success; a failure keeps it, with the reason.
    func fire(_ card: ProactiveCard, today: TodayModel) async {
        guard fires[card.id] != .sending, let action = card.action else { return }
        // A reply is sent only from its shown draft: a tap while the draft is
        // closed opens it and sends nothing.
        if action.primaryStep(draftOpen: openDrafts.contains(card.id)) == .openDraft {
            openDrafts.insert(card.id)
            return
        }
        fires[card.id] = .sending
        var body: [String: Any] = ["draft": draft(card)]
        if !action.recipient.isEmpty { body["recipient"] = action.recipient }
        // The service books the slot it stored on the card and rechecks it; the
        // request never carries one (the fire route refuses unknown fields).
        do {
            let outcome = try await post(today, "/assistant/proactive/cards/\(Self.segment(card.id))/fire", body: body,
                                         timeout: action.fireTimeout)
            guard outcome.ok else {
                // A reply the service didn't send may have changed its card: other
                // recipients after a confirm, or a lock after an unconfirmed send.
                // Read the plan again so the card shows, and the next tap repeats,
                // the recipient the service now holds. The draft stays open and
                // keeps the person's edits: both live here, not in the plan.
                switch outcome.refusal(method: action.method) {
                case .confirm(let line):
                    // Still "Sending…" until the card shows the new recipients,
                    // so no tap can repeat the old ones.
                    await today.refresh(afterMutation: true)
                    fires[card.id] = .confirm(line)
                case .unconfirmed(let line):
                    fires[card.id] = .unconfirmed(line)
                    await today.refresh(afterMutation: true)
                case .retry(let line):
                    fires[card.id] = .failed(line)
                    if action.sendsDraft { await today.refresh(afterMutation: true) }
                }
                return
            }
            if let url = URL(string: outcome.url), !outcome.url.isEmpty {
                NSWorkspace.shared.open(url)
            } else if outcome.handoff == "open_in_calendar", let url = URL(string: "ical://") {
                NSWorkspace.shared.open(url)
            }
            let handedBack = !outcome.handoff.isEmpty || !outcome.url.isEmpty
            fires[card.id] = .done(outcome.message.isEmpty ? (handedBack ? "Opened to finish" : "Done") : outcome.message)
            guard !handedBack else { return }
            try? await Task.sleep(for: .milliseconds(1400))
            withAnimation(.easeOut(duration: 0.2)) { _ = hidden.insert(card.id) }
            openDrafts.remove(card.id)
            await today.refresh(afterMutation: true)
        } catch {
            guard action.sendsDraft, !ProactiveOutcome.neverReached(error) else {
                fires[card.id] = .failed(error.localizedDescription)
                return
            }
            // No answer is not a failure: the service may still be sending, and
            // the reply can go out after this. Never "Try again"; say where to
            // check, then show what the service holds (gone once sent, locked
            // when Mail or Messages didn't confirm).
            fires[card.id] = .unconfirmed(ProactiveOutcome.unconfirmedLine(method: action.method))
            await today.refresh(afterMutation: true)
        }
    }

    func dismiss(_ card: ProactiveCard, today: TodayModel) async {
        guard !dismissing.contains(card.id) else { return }
        dismissing.insert(card.id)
        defer { dismissing.remove(card.id) }
        do {
            let outcome = try await post(today, "/assistant/proactive/cards/\(Self.segment(card.id))/dismiss", body: [:], timeout: 15)
            if outcome.ok {
                cardErrors[card.id] = nil
                withAnimation(.easeOut(duration: 0.2)) { _ = hidden.insert(card.id) }
                await today.refresh(afterMutation: true)
            } else {
                cardErrors[card.id] = outcome.message.isEmpty ? "Couldn't dismiss it." : outcome.message
            }
        } catch { cardErrors[card.id] = error.localizedDescription }
    }

    /// Check a to-do off (or back on).
    func toggleDone(_ todo: ProactiveTodo, today: TodayModel) async {
        await changeTodo(todo, body: ["done": !isDone(todo)], today: today) { [self] in todoDone[todo.id] = !isDone(todo) }
    }

    func addToDay(_ todo: ProactiveTodo, today: TodayModel) async {
        await changeTodo(todo, body: ["add_to_day": true], today: today) { [self] in _ = addedToDay.insert(todo.id) }
    }

    private func changeTodo(_ todo: ProactiveTodo, body: [String: Any], today: TodayModel, onSuccess: () -> Void) async {
        guard !todoBusy.contains(todo.id) else { return }
        todoBusy.insert(todo.id)
        defer { todoBusy.remove(todo.id) }
        do {
            let outcome = try await post(today, "/assistant/proactive/todos/\(Self.segment(todo.id))", body: body, timeout: 20)
            if outcome.ok {
                todoErrors[todo.id] = nil
                onSuccess()
                await today.refresh(afterMutation: true)
            } else {
                todoErrors[todo.id] = outcome.message.isEmpty ? "That didn't save." : outcome.message
            }
        } catch { todoErrors[todo.id] = error.localizedDescription }
    }

    /// Analyze now: one cycle over everything (or one account), then the Brief
    /// polls while the engine reports progress.
    func analyze(today: TodayModel, account: String?) async {
        guard !analyzePending() else { return }
        analyzeRequested = Date()
        analyzeError = ""
        var body: [String: Any] = [:]
        if let account { body["account"] = account }
        do {
            let outcome = try await post(today, "/assistant/proactive/analyze", body: body, timeout: 20)
            if !outcome.ok {
                analyzeError = outcome.message.isEmpty ? "Wisp couldn't start reading. Try again." : outcome.message
                analyzeRequested = nil
                return
            }
            await today.refresh(afterMutation: true)
        } catch {
            analyzeError = error.localizedDescription
            analyzeRequested = nil
        }
    }

    /// Analyze now was tapped in the last 30 seconds and the engine hasn't
    /// picked it up yet.
    func analyzePending(now: Date = Date()) -> Bool {
        analyzeRequested.map { now.timeIntervalSince($0) < 30 } ?? false
    }

    /// Whether the Brief should poll: the engine is working, or Analyze now
    /// was just tapped and it hasn't started yet.
    func shouldPoll(_ state: ProactiveState, now: Date = Date()) -> Bool {
        state.isWorking || analyzePending(now: now)
    }

    /// The engine took the request (it reports working), or it has stayed
    /// ready long enough after the tap that the cycle must have been quick.
    func settleAnalyze(_ state: ProactiveState, now: Date = Date()) {
        guard let asked = analyzeRequested else { return }
        let quick = state.state == "ready" && now.timeIntervalSince(asked) > 8
        if state.isWorking || quick || !analyzePending(now: now) { analyzeRequested = nil }
    }

    #if WISP_NOTCH_QA
    func qaReset(account: String? = nil, openDraft: String? = nil, expanded: Set<String> = [], replying: String? = nil,
                 hero: String? = nil, openRow: String? = nil, showAll: Bool = false) {
        self.account = account
        heroID = hero
        self.openRow = openRow
        showAllTodos = showAll
        openDrafts = openDraft.map { [$0] } ?? []
        self.expanded = expanded
        self.replying = replying
        replyModels = [:]
    }
    #endif
}

// MARK: - The Brief's proactive section

/// The proactive Brief in two zones (R6): NEEDS YOU (the "Before you ask"
/// card and the other cards as pills, then mail and texts, then what Wisp is
/// doing on its own) and ON YOUR PLATE (the rest of today's calendar, school
/// work and to-dos in one list by urgency, what Study got ready, and what was
/// found in mail). Side by side when the Brief is wide enough
/// (BriefPlate.sideBySide: the wide panel), else stacked, NEEDS YOU first. One
/// layout, measured on the width it is given; both zones scroll with the tab.
struct ProactiveBriefSection: View {
    let proactive: ProactiveState
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    @ObservedObject var actions: ProactiveActions = .shared
    /// Clear (and Mark done for a dated reminder) behind each to-do's and
    /// thread's trailing ×; shared with the Top 3's Needs you rows.
    @ObservedObject var itemActions: NeedsYouActionsModel = NotchBriefView.needsYouActions
    /// The rest of today's calendar, for the calendar line.
    var events: [(start: Double, title: String)] = []
    /// Today's all-day events, and how many timed ones have already ended, so
    /// the calendar line reads the same day the cards do (BriefPlate.calendarLine).
    var allDayEvents: [String] = []
    var endedEvents = 0
    /// Kept for callers; the zones scroll with the tab (OverlayView.tabScroll).
    var scrollCap: CGFloat? = 300
    /// HOOK(W3): told when Reply is tapped on a thread.
    var onReply: ((ProactiveThread) -> Void)? = BriefReplyHook.onReply
    /// Which sources Wisp could read (the workspace's source_coverage), so no
    /// row gives an all-clear for one it couldn't.
    var coverage: [SourceAccess] = []
    /// The wider panel. The zones follow the width they are given, so this
    /// only says the panel was widened.
    var wide = false
    /// This week's school work, one row each in ON YOUR PLATE (StudyDaySummary.briefPlate).
    var schoolRows = StudyUpcomingRows()
    /// What Study got ready and the topics to look at again (StudyDaySummary.briefCards), under the list.
    var studyCards = StudyBriefCards()
    /// "Found in your mail", at the foot of ON YOUR PLATE.
    var mailMemory: MailMemoryModel? = nil

    private var unread: [SourceAccess] { proactive.unreadSources(coverage) }

    /// A heading row with buttons (NEEDS YOU's, ON YOUR PLATE's), one height.
    private static let controlsHeaderHeight: CGFloat = 22
    /// The gap between the zones side by side, and between them stacked.
    private static let zoneGap: CGFloat = 28
    private static let stackGap: CGFloat = 12
    /// A row's text starts here: the check circle, the source icon and their gaps.
    private static let rowInset: CGFloat = 51
    @State private var hoveredThread: String? = nil
    /// Whether the zones sit side by side: from the width the panel lays the
    /// Brief out at (its page width, OverlayView), decided in the same update
    /// as the width changes. A width measured after layout lagged a pass, so
    /// turning two columns off first drew the stacked zones with five
    /// two-line rows, then shrank to three.
    private var sideBySide: Bool {
        let panel: CGFloat = DisplayGeometry.shared.layout.widened(wide).expandedWidth
        return BriefPlate.sideBySide(width: panel - 2 * OverlayView.sideInset, textScale: Theme.textScale)
    }

    private var chosen: String? {
        actions.account.flatMap { picked in proactive.accounts.contains(picked) ? picked : nil }
    }

    var body: some View {
        let shown = proactive.only(chosen)
        zones(shown)
        // Every account's threads, not only the chosen chip's.
        .onChange(of: proactive.threads.map(\.id), initial: true) { _, listed in
            actions.pruneReplies(keeping: Set(listed))
        }
        .task(id: actions.shouldPoll(proactive)) {
            // While the engine reads, follow its progress; stop once it's done.
            // The loop reads the latest plan: this view's copy is from its start.
            @MainActor func latest() -> ProactiveState { today.currentPlan?.workspace?.proactive ?? proactive }
            while !Task.isCancelled, actions.shouldPoll(latest()) {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled else { return }
                await today.refresh()
                actions.settleAnalyze(latest())
            }
            actions.settleAnalyze(latest())
        }
    }

    /// The sources Wisp couldn't read, then the two zones.
    private func zones(_ shown: ProactiveState) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if !unread.isEmpty { accessRows(unread) }
            zonePair(shown)
        }
    }

    private func zonePair(_ shown: ProactiveState) -> some View {
        BriefZones(sideBySide: sideBySide, gap: Self.zoneGap, stackGap: Self.stackGap) {
            needsYouZone(shown)
            Rectangle().fill(Theme.hairline).accessibilityHidden(true)
            plateZone(shown)
        }
    }

    /// A zone's heading: the name, and its count in a ring while there is one.
    private func zoneTitle(_ title: String, spoken: String, count: Int, lit: Bool) -> some View {
        let color = lit && count > 0 ? Theme.warn : Theme.textMuted
        return HStack(alignment: .center, spacing: 7) {
            Text(title).font(.wisp(size: 10, design: .monospaced)).foregroundStyle(color)
            if count > 0 {
                Text("\(count)").font(.wisp(size: 10, weight: .semibold, design: .monospaced)).monospacedDigit()
                    .foregroundStyle(color)
                    .padding(.horizontal, 6).padding(.vertical, 1)
                    .overlay(Capsule().stroke(color, lineWidth: 1))
                    .fixedSize()
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(BriefPlate.heading(spoken, count: count))
        .accessibilityAddTraits(.isHeader)
    }

    // MARK: Sources Wisp couldn't read

    /// One quiet row per source Wisp couldn't read: what's wrong, and the
    /// setting that fixes it (Full Disk Access for texts, Calendars, …).
    private func accessRows(_ rows: [SourceAccess]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(rows, id: \.source) { access in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: DayTimeline.sourceSymbol(access.source)).font(.wisp(size: 10.5))
                        .foregroundStyle(Theme.textMuted).frame(width: 15)
                    Text(access.headline).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .help(access.detail)
                    Spacer(minLength: 6)
                    // fixLink: a Google account to reconnect opens Wisp's own Settings.
                    if let fix = access.fixLink, let url = URL(string: fix.url) {
                        NotchChrome.small(Permissions.fixLabel(fix)) {
                            if fix == BriefDetail.googleSettings { BriefFix.open(fix) } else { Permissions.openFix(url) }
                        }
                        .help(access.detail)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Sources Wisp can't read yet")
    }

    // MARK: Accounts

    /// All, or one linked account, as one small menu beside Analyze now.
    @ViewBuilder private var accountMenu: some View {
        if proactive.accounts.count > 1 {
            Menu {
                Button("All accounts") { actions.account = nil; model.onResize() }
                ForEach(proactive.accounts, id: \.self) { account in
                    Button(proactive.chipName(account)) { actions.account = account; model.onResize() }
                }
            } label: {
                HStack(spacing: 3) {
                    Text(chosen.map(proactive.chipName) ?? "All")
                    Image(systemName: "chevron.down").font(.wisp(size: 7, weight: .bold))
                }
                .font(.wisp(size: 11, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Capsule().fill(chosen == nil ? Theme.chipFill : Theme.chipOn))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .help("Show one account's cards, to-dos and mail")
            .accessibilityLabel("Account: \(chosen ?? "all")")
        }
    }

    // MARK: Needs you

    /// Wisp's cards: what it got ready before you asked, the first one large
    /// and the rest as pills under it.
    @ViewBuilder private func beforeYouAsk(_ shown: ProactiveState) -> some View {
        let cards = liveCards(shown)
        let index = cards.firstIndex { $0.id == actions.heroID } ?? 0
        VStack(alignment: .leading, spacing: 8) {
            if proactive.isWorking || actions.analyzePending() { progressRow }
            if !actions.analyzeError.isEmpty {
                Text(actions.analyzeError).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
            }
            if !cards.isEmpty {
                cardView(cards[index], position: index, of: cards)
                    .id(cards[index].id)
                    .transition(.opacity)
                let rest = cards.enumerated().filter { $0.offset != index }.map(\.element)
                if !rest.isEmpty { otherCards(Array(rest.prefix(3))) }
            }
        }
        // The gate's state is loaded HERE, on a view that always exists: every time the Brief
        // shows, and again whenever its cards change (a cycle finished, an Undo), so the
        // question, the line and today's count are never missing or stale. (A .task on the
        // question card itself never ran: that view is empty until the state is loaded.)
        .task(id: shown.cards.map(\.id)) { await ActForMeModel.shared.refresh() }
    }

    /// The cards still to show: one the service confirmed fired or dismissed
    /// has gone, and a grouped "Wisp did this" card goes once every one of its
    /// rows has been undone.
    private func liveCards(_ shown: ProactiveState) -> [ProactiveCard] {
        shown.cards.filter { card in
            !actions.hidden.contains(card.id)
                && !(card.isGroup && card.notes.isEmpty && card.members.allSatisfy { actions.hidden.contains($0.id) })
        }
    }

    /// The left zone: its heading with the account menu and Analyze now, the
    /// cards, mail and texts (a thread a reply card holds shows only as that
    /// card), then "Let Wisp act for me" (ActForMe.swift) at its foot. With
    /// nothing in it, one muted line.
    private func needsYouZone(_ shown: ProactiveState) -> some View {
        let cards = liveCards(shown)
        let threads = listedThreads(shown, cards: cards)
        let waiting = threads.filter(\.needsReply).count
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 6) {
                zoneTitle("NEEDS YOU", spoken: "Needs you", count: cards.count + waiting, lit: true)
                Spacer()
                accountMenu
                analyzeControl
            }
            .frame(height: Self.controlsHeaderHeight)
            beforeYouAsk(shown)
            if !threads.isEmpty {
                attention(threads)
            } else if cards.isEmpty {
                Text(needsYouEmptyLine).font(.wisp(size: 12.5)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // "Let Wisp act for me", always in the Brief: the question, or one line.
            ActForMeBriefRow()
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Needs you")
    }

    /// Mail and texts as listed: none just cleared, none a reply card holds.
    private func listedThreads(_ shown: ProactiveState, cards: [ProactiveCard]) -> [ProactiveThread] {
        let open = shown.orderedThreads.filter { !itemActions.isHidden($0.actionRow) }
        return ProactiveState.threads(open, coveredBy: cards)
    }

    /// NEEDS YOU's one line when it holds nothing: Wisp still reading, why it
    /// couldn't, or an all-clear only for what it could read.
    private var needsYouEmptyLine: String {
        if proactive.isWorking { return "Cards land here as Wisp reads." }
        if proactive.isUnavailable || proactive.hasReadNothing {
            let reason = proactive.reason.trimmingCharacters(in: .whitespacesAndNewlines)
            return reason.isEmpty ? "Not caught up yet. Tap ✦ to read your mail and texts." : reason
        }
        if let clear = ProactiveState.attentionEmptyLine(unread: unread) { return clear }
        return "Nothing to get ahead of in what Wisp could read."
    }

    /// The other cards, by title; one tap makes it the one shown. They share a
    /// row when every title fits whole; otherwise each takes its own line and
    /// wraps, so a title is never cut.
    private func otherCards(_ cards: [ProactiveCard]) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 6) {
                ForEach(cards) { card in otherCard(card).fixedSize() }
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 6) {
                ForEach(cards) { card in otherCard(card) }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("More things Wisp caught")
    }

    private func otherCard(_ card: ProactiveCard) -> some View {
        let shape = RoundedRectangle(cornerRadius: 12, style: .circular)
        return Button {
            withAnimation(.easeOut(duration: 0.15)) { actions.heroID = card.id }
            model.onResize()
        } label: {
            Text(card.chipTitle).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(shape.fill(Theme.chipFill))
                .overlay(shape.stroke(Theme.chipStroke, lineWidth: 1))
                .contentShape(shape)
        }
        .buttonStyle(.plain)
        .help(card.title)
        .accessibilityLabel(card.title)
    }

    private var analyzeControl: some View {
        let busy = proactive.isWorking || actions.analyzePending()
        return Button {
            Task { await actions.analyze(today: today, account: chosen); model.onResize() }
        } label: {
            Image(systemName: busy ? "ellipsis" : "sparkles").font(.wisp(size: 10, weight: .semibold))
                .foregroundStyle(Theme.textSecondary)
                .frame(width: 22, height: 20)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(busy)
        .help(chosen.map { "Analyze now: read \($0)'s mail and texts and catch up" }
              ?? "Analyze now: read your mail, texts, calendar and reminders and catch up")
        .accessibilityLabel("Analyze now")
    }

    private var progressRow: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(proactive.isWorking ? proactive.progressLabel : "Starting…")
                .font(.wisp(size: 12)).foregroundStyle(Theme.textSecondary).monospacedDigit()
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.08))
                    Capsule().fill(Color.white.opacity(0.7))
                        .frame(width: max(4, geo.size.width * CGFloat(proactive.progressFraction ?? 0.04)))
                }
            }
            .frame(height: 3)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: The hero card

    private func cardView(_ card: ProactiveCard, position: Int, of cards: [ProactiveCard]) -> some View {
        let fire = actions.fires[card.id]
        let draftOpen = actions.openDrafts.contains(card.id)
        let action = card.action
        let hasDraft = !(action?.draft ?? "").isEmpty
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                if card.wispDidThis {
                    // Already done by Wisp: a tick and brighter ink set it apart from cards that ask.
                    Image(systemName: "checkmark.circle.fill").font(.wisp(size: 10.5, weight: .semibold))
                        .foregroundStyle(Theme.textSecondary).accessibilityHidden(true)
                }
                Text(card.metaLine)
                    .font(.wisp(size: 10, weight: .medium, design: .monospaced)).tracking(0.6)
                    .foregroundStyle(card.wispDidThis ? Theme.textSecondary : Theme.textMuted).lineLimit(1)
                    .help(card.why)
                Spacer(minLength: 4)
                if card.isGroup {
                    // A group has no Dismiss: each row has its own Undo, and it leaves with its rows.
                    EmptyView()
                } else if actions.dismissing.contains(card.id) {
                    ProgressView().controlSize(.mini)
                } else if fire != .sending, !isDone(fire) {
                    Button { Task { await actions.dismiss(card, today: today) } } label: {
                        Image(systemName: "xmark").font(.wisp(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                            .frame(width: 16, height: 16).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Dismiss: Wisp won't bring this one back")
                    .accessibilityLabel("Dismiss")
                }
            }
            Text(card.title)
                .font(.wisp(size: 15.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if card.isGroup { groupRows(card) }
            if !card.body.isEmpty {
                // Two lines keep the card short; More opens the rest in place,
                // so it isn't reachable only by hovering for the tooltip.
                ClampedText(text: card.body, onToggle: { model.onResize() })
            }
            // A reply's Send sits inside its shown draft; the card's own button
            // only opens the draft (ProactiveCard.Action.primaryStep).
            let sendInDraft = draftOpen && action?.sendsDraft == true
            if draftOpen, let action {
                draftEditor(card, action, fire: fire, withSend: sendInDraft).revealInPanel("draft-\(card.id)")
            }
            HStack(alignment: .center, spacing: 12) {
                if let action, !card.isGroup, !sendInDraft, !isUnconfirmed(fire) { fireButton(card, action, fire) }
                if action?.method == "block_time" {
                    secondary("Pick another time") { model.tab = .day }
                        .help("Open Day to choose a different time")
                } else if hasDraft && (draftOpen || action?.sendsDraft != true) {
                    secondary(draftOpen ? "Hide the draft" : "Read the draft",
                              chevron: draftOpen ? "chevron.up" : "chevron.down") {
                        if draftOpen { actions.openDrafts.remove(card.id) } else { actions.openDrafts.insert(card.id) }
                        model.onResize()
                    }
                    .disabled(fire == .sending)
                }
                Spacer(minLength: 4)
                if cards.count > 1 {
                    Button {
                        withAnimation(.easeOut(duration: 0.15)) { actions.heroID = cards[(position + 1) % cards.count].id }
                        model.onResize()
                    } label: {
                        HStack(spacing: 3) {
                            Text("\(position + 1) of \(cards.count)").monospacedDigit()
                            Image(systemName: "chevron.right").font(.wisp(size: 8.5, weight: .bold))
                        }
                        .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textMuted)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Next card")
                    .accessibilityLabel("Card \(position + 1) of \(cards.count); next")
                }
            }
            .padding(.top, 3)
            if case .failed(let reason) = fire {
                Text(reason).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if case .confirm(let line) = fire {
                // Not a failure: nothing was sent, and Send waits for a fresh tap.
                Text(line).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if case .unconfirmed(let line) = fire {
                Text(line).font(.wisp(size: 11)).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = actions.cardErrors[card.id] {
                Text(error).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
            }
        }
        .padding(EdgeInsets(top: 11, leading: 13, bottom: 11, trailing: 11))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.cardFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(card.metaLine). \(card.title)")
    }

    /// The rows of a grouped "Wisp did this" card: what was done, the day and time it is for, and
    /// that thing's own Undo (the member card's, fired exactly as a single card's is). Up to
    /// `ProactiveCard.groupRows` rows, then how many more there are.
    @ViewBuilder private func groupRows(_ card: ProactiveCard) -> some View {
        let members = card.members.filter { !actions.hidden.contains($0.id) }
        let shownMembers = Array(members.prefix(ProactiveCard.groupRows))
        let shownNotes = Array(card.notes.prefix(ProactiveCard.groupRows - shownMembers.count))
        let more = members.count + card.notes.count - shownMembers.count - shownNotes.count
        VStack(alignment: .leading, spacing: 7) {
            ForEach(shownMembers) { member in
                let fire = actions.fires[member.id]
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .center, spacing: 8) {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(member.title).font(.wisp(size: 12.5, weight: .medium)).foregroundStyle(Theme.textPrimary)
                                .fixedSize(horizontal: false, vertical: true)
                            if !member.when.isEmpty {
                                Text(member.when).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                            }
                        }
                        Spacer(minLength: 6)
                        if let action = member.action, !isUnconfirmed(fire) { fireButton(member, action, fire) }
                    }
                    if case .failed(let reason) = fire {
                        Text(reason).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if case .unconfirmed(let line) = fire {
                        Text(line).font(.wisp(size: 11)).foregroundStyle(Theme.warn)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .accessibilityElement(children: .contain)
            }
            ForEach(shownNotes, id: \.self) { note in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "questionmark").font(.wisp(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                        .accessibilityHidden(true)
                    Text(note).font(.wisp(size: 12)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .help("Wisp started this and was interrupted before it could confirm. If it is there and you don't want it, remove it there.")
            }
            if more > 0 {
                Text("and \(more) more, listed under “Wisp is acting for you” below")
                    .font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.top, 1)
    }

    private func secondary(_ title: String, chevron: String? = nil, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Text(title)
                if let chevron { Image(systemName: chevron).font(.wisp(size: 8.5, weight: .bold)) }
            }
            .font(.wisp(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }

    /// The button's words; a block-time card without any names its hour.
    private func buttonLabel(_ action: ProactiveCard.Action) -> String {
        if action.method == "block_time", action.button.trimmingCharacters(in: .whitespaces).isEmpty, let slot = action.slot {
            return "Block " + DayLoad.span(slot.start, slot.end, zone: today.timezone, locale: today.clockLocale) + "?"
        }
        return action.label
    }

    private func isDone(_ fire: ProactiveActions.Fire?) -> Bool {
        if case .done = fire { return true }
        return false
    }

    @ViewBuilder private func fireButton(_ card: ProactiveCard, _ action: ProactiveCard.Action, _ fire: ProactiveActions.Fire?) -> some View {
        switch fire {
        case .sending:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(action.sendsDraft ? "Sending…" : "Working…").font(.wisp(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.textSecondary)
            }
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(Capsule().fill(Theme.chipFill))
        case .done(let message):
            HStack(spacing: 5) {
                Image(systemName: "checkmark").font(.wisp(size: 10.5, weight: .bold))
                Text(message).font(.wisp(size: 12.5, weight: .medium)).lineLimit(1)
            }
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(Capsule().fill(Theme.chipOn))
        default:
            if action.method == "undo_add" {
                // The add already happened, so Undo is the quiet button: an outline, not the white pill.
                Button { Task { await actions.fire(card, today: today); model.onResize() } } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.uturn.backward").font(.wisp(size: 10, weight: .bold))
                        Text(isFailed(fire) ? "Try again" : action.undoLabel).lineLimit(1)
                    }
                    .font(.wisp(size: 12.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .fixedSize()
                .help("Removes exactly what Wisp added, and Wisp won't add it again")
                .accessibilityLabel("Undo: remove what Wisp added")
            } else {
            let draftOpen = actions.openDrafts.contains(card.id)
            Button { Task { await actions.fire(card, today: today); model.onResize() } } label: {
                Text(isFailed(fire) && (draftOpen || !action.sendsDraft) ? "Try again"
                     : action.sendsDraft ? action.primaryLabel(draftOpen: draftOpen) : buttonLabel(action))
                    .font(.wisp(size: 12.5, weight: .semibold)).foregroundStyle(Color.black)
                    .lineLimit(1)
                    .padding(.horizontal, 13).padding(.vertical, 5)
                    .background(Capsule().fill(Color.white.opacity(0.92)))
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .fixedSize()
            .help(action.sendsDraft ? (draftOpen ? "Sends exactly the draft above, as it reads now"
                                                 : "Shows the draft first; nothing is sent until you tap Send")
                  : card.why)
            }
        }
    }

    private func isFailed(_ fire: ProactiveActions.Fire?) -> Bool {
        if case .failed = fire { return true }
        return false
    }

    /// The reply may have gone out: the card shows no Send and no Try again.
    private func isUnconfirmed(_ fire: ProactiveActions.Fire?) -> Bool {
        if case .unconfirmed = fire { return true }
        return false
    }

    private func draftEditor(_ card: ProactiveCard, _ action: ProactiveCard.Action, fire: ProactiveActions.Fire? = nil,
                             withSend: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            if let source = card.sources.first(where: { !$0.quote.isEmpty }) {
                Text((source.who.isEmpty ? "" : source.who + ": ") + "“" + source.quote + "”")
                    .font(.wisp(size: 11.5)).italic().foregroundStyle(Theme.textMuted)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            if !action.recipient.isEmpty {
                // Never truncated: these are the people Send sends the draft to.
                Text("TO  " + action.recipient).font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                    .accessibilityLabel("To: " + action.recipient)
            }
            TextEditor(text: Binding(get: { actions.draft(card) }, set: { actions.drafts[card.id] = $0 }))
                .font(.wisp(size: 12.5)).foregroundStyle(Theme.textPrimary)
                .scrollContentBackground(.hidden)
                .padding(.horizontal, 5).padding(.vertical, 6)
                .frame(minHeight: 64, idealHeight: draftHeight(actions.draft(card)), maxHeight: 150)
                .fixedSize(horizontal: false, vertical: true)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.editorFill))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke, lineWidth: 1))
                .disabled(actions.fires[card.id] == .sending)
                .accessibilityLabel("Draft; edit before sending")
            if actions.drafts[card.id].map({ $0 != action.draft }) == true {
                Text("Edited — Wisp sends your version.").font(.wisp(size: 10.5)).foregroundStyle(Theme.textMuted)
            }
            if withSend, !isUnconfirmed(fire) {
                // Send, under the words it sends and the person it sends them to.
                fireButton(card, action, fire).padding(.top, 2)
            }
        }
        .padding(.top, 2)
    }

    /// Room for the draft's lines at the editor's width, about 70 characters a line.
    private func draftHeight(_ text: String) -> CGFloat {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
            .reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / 72))) }
        return min(150, max(64, CGFloat(lines) * 17 + 14))
    }

    // MARK: On your plate

    /// The right zone: its heading with Day ›, the rest of today's calendar,
    /// school work and to-dos in one list by urgency, what Study got ready,
    /// then "Found in your mail".
    private func plateZone(_ shown: ProactiveState) -> some View {
        let items = plateItems(shown)
        let open: Int = items.filter { (item: PlateItem) -> Bool in
            if case .todo(let todo) = item { return !actions.isDone(todo) }
            return true
        }.count
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 6) {
                zoneTitle("ON YOUR PLATE", spoken: "On your plate", count: open, lit: false)
                Spacer()
                dayLink
            }
            .frame(height: Self.controlsHeaderHeight)
            calendarLine(todosEmpty: items.isEmpty ? shown.todoLine(unread: unread) : nil)
            if !items.isEmpty { plateList(items) }
            if !studyCards.isEmpty {
                StudyBriefCardsView(cards: studyCards, model: model, leading: 0, showsMore: schoolRows.more == 0)
                    .padding(.top, 2)
            }
            if let mailMemory {
                MailMemorySection(memory: mailMemory, onResize: { model.onResize() }, buttonsBelow: sideBySide)
                    .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("On your plate")
    }

    /// School work and to-dos as one list (ProactiveState.plate): none just
    /// cleared or done by the service.
    private func plateItems(_ shown: ProactiveState) -> [PlateItem] {
        let todos = shown.todos.filter { !itemActions.isHidden($0.actionRow) }
        let bounds = today.dayInterval
        let dayStart = bounds.start.timeIntervalSince1970
        let todayEnd = bounds.end.timeIntervalSince1970
        let tomorrow = schoolCalendar.date(byAdding: .day, value: 1, to: bounds.end) ?? bounds.end.addingTimeInterval(86_400)
        return ProactiveState.plate(todos: todos, school: schoolRows.items, isDone: { actions.isDone($0) },
                                    now: today.now.timeIntervalSince1970, dayStart: dayStart,
                                    todayEnd: todayEnd, tomorrowEnd: tomorrow.timeIntervalSince1970)
    }

    private var dayLink: some View {
        Button { model.tab = .day } label: {
            HStack(spacing: 2) {
                Text("Day")
                Image(systemName: "chevron.right").font(.wisp(size: 8, weight: .bold))
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("The rest of today, hour by hour (⌘3)")
    }

    /// "11:45 AM Hack Day · 12 PM Hacktoberfest", today's all-day events
    /// first; or that nothing is left on today, or nothing was on it; with no
    /// to-dos either, that goes on the same line. Opens Day.
    private func calendarLine(todosEmpty: String?) -> some View {
        let calendarUnread = unread.contains { $0.source == "calendar" }
        let upcoming: [String] = events.map { today.clock($0.start) + " " + $0.title }
        let dayLine: String = BriefPlate.calendarLine(upcoming: upcoming, allDay: allDayEvents,
                                                      earlierToday: endedEvents, calendarUnread: calendarUnread)
        let quiet: Bool = events.isEmpty && allDayEvents.isEmpty
        let line: String = todosEmpty.map { dayLine + " · " + $0 } ?? dayLine
        return Button { model.tab = .day } label: {
            Text(line).font(.wisp(size: 13))
                .foregroundStyle(quiet ? Theme.textMuted : Theme.textSecondary)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help("The rest of today, hour by hour (⌘3)")
        .accessibilityLabel(line)
        .accessibilityHint("Opens Day")
    }

    /// The first five (three stacked), then "+N to-dos · +M in Study · Show all".
    private func plateList(_ items: [PlateItem]) -> some View {
        let limit: Int = BriefPlate.shown(sideBySide: sideBySide)
        let visible: [PlateItem] = actions.showAllTodos ? items : Array(items.prefix(limit))
        let hidden = items.dropFirst(visible.count)
        let hiddenSchool = hidden.filter(\.isSchool).count + schoolRows.more
        let hiddenTodos = hidden.count - hidden.filter(\.isSchool).count
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(visible) { item in plateRow(item) }
            plateMore(hiddenTodos: hiddenTodos, hiddenSchool: hiddenSchool, canExpand: items.count > limit)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("School work and to-dos")
    }

    @ViewBuilder private func plateRow(_ item: PlateItem) -> some View {
        switch item {
        case .todo(let todo): todoRow(todo)
        case .school(let due): schoolRow(due)
        }
    }

    /// "+3 to-dos · +5 in Study · Show all": the parts that aren't zero; the
    /// Study part opens Study, Show all opens the list in place.
    @ViewBuilder private func plateMore(hiddenTodos: Int, hiddenSchool: Int, canExpand: Bool) -> some View {
        let todosPart: String? = hiddenTodos > 0 ? BriefPlate.moreParts(hiddenTodos: hiddenTodos, hiddenSchool: 0).first : nil
        let studyPart: String? = hiddenSchool > 0 ? BriefPlate.moreParts(hiddenTodos: 0, hiddenSchool: hiddenSchool).first : nil
        if todosPart != nil || studyPart != nil || canExpand {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if let todosPart { Text(todosPart) }
                if let studyPart {
                    if todosPart != nil { Text("·") }
                    Button(studyPart) { model.tab = .study }
                        .buttonStyle(.plain)
                        .help("Your school work (⌘5)")
                }
                if canExpand {
                    if todosPart != nil || studyPart != nil { Text("·") }
                    Button(actions.showAllTodos ? "Show fewer" : "Show all") {
                        actions.showAllTodos.toggle()
                        model.onResize()
                    }
                    .buttonStyle(.plain)
                }
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .padding(.leading, Self.rowInset).padding(.top, 2)
        }
    }

    /// Due times in the day's own time zone, as the rest of the Brief.
    private var schoolCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        return calendar
    }

    /// One piece of school work: its title in full, the course and time under
    /// it, then when it is due. The whole row opens Study, where it is done.
    private func schoolRow(_ item: StudyDaySummary.Due) -> some View {
        let calendar = schoolCalendar
        let assignment = item.assignment
        let due = StudyText.dueShort(assignment, now: today.now, calendar: calendar)
        let time: String = assignment.allDay ? "" : assignment.dueDate.map { StudyText.time($0, calendar: calendar) } ?? ""
        let chip: String = time.isEmpty ? due : due.replacingOccurrences(of: " " + time, with: "")
        let under: String = [item.course, time].filter { !$0.isEmpty }.joined(separator: " · ")
        let spoken: String = ["School: " + assignment.title, under, "due " + due].filter { !$0.isEmpty }.joined(separator: ", ")
        return Button { model.tab = .study } label: {
            schoolLines(assignment.title, under: under, chip: chip)
                .padding(.vertical, 3)
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help("Open it in Study (⌘5)")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .accessibilityHint("Opens Study")
    }

    /// Side by side: the title and course, then when it is due on a second
    /// line. Stacked (full width), when it is due sits at the right of the title.
    @ViewBuilder private func schoolLines(_ title: String, under: String, chip: String) -> some View {
        if sideBySide {
            VStack(alignment: .leading, spacing: 3) {
                schoolHead(title, under: under)
                schoolDue(chip)
            }
        } else {
            HStack(alignment: .top, spacing: 9) {
                schoolHead(title, under: under)
                schoolChip(chip).padding(.top, 3)
            }
        }
    }

    private func schoolHead(_ title: String, under: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            // The check column stays empty: school work is checked off in Study.
            Color.clear.frame(width: 18, height: 18)
            Image(systemName: "graduationcap").font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                .frame(width: 15).padding(.top, 2)
            schoolText(title, under: under)
        }
    }

    private func schoolChip(_ chip: String) -> some View {
        Text(chip.uppercased()).font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textSecondary)
            .lineLimit(1).fixedSize()
    }

    private func schoolText(_ title: String, under: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title).font(.wisp(size: 13.5)).foregroundStyle(Theme.textPrimary)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            if !under.isEmpty {
                // A long course name wraps rather than being cut with room beside it.
                Text(under).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func schoolDue(_ chip: String) -> some View {
        HStack(alignment: .center, spacing: 8) {
            schoolChip(chip)
            Spacer(minLength: 6)
            HStack(spacing: 2) {
                Text("Study")
                Image(systemName: "chevron.right").font(.wisp(size: 8, weight: .bold))
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary).fixedSize()
        }
        .padding(.leading, Self.rowInset)
    }

    // MARK: To-do

    /// One to-do: the check, its source and its whole title (two lines when
    /// it needs them); then on a second line when it is due, Add to Day and
    /// its ✓ / ×, so they never squeeze the title.
    private func todoRow(_ todo: ProactiveTodo) -> some View {
        let done = actions.isDone(todo)
        let busy = actions.todoBusy.contains(todo.id)
        return VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .top, spacing: 9) {
                checkButton(todo, done: done, busy: busy)
                Image(systemName: DayTimeline.sourceSymbol(todo.source))
                    .font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .frame(width: 15).padding(.top, 2)
                    .help(DayTimeline.sourceName(todo.source))
                todoText(todo, done: done)
                // Stacked (full width): the due time and actions fit beside the title.
                if !sideBySide { todoActions(todo, done: done, busy: busy, below: false) }
            }
            if sideBySide { todoActions(todo, done: done, busy: busy, below: true) }
            if let error = actions.todoErrors[todo.id] {
                Text(error).font(.wisp(size: 11)).foregroundStyle(Theme.bad).padding(.leading, Self.rowInset)
            }
            if let failure = itemActions.failures[todo.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, Self.rowInset)
            }
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .contain)
    }

    private func checkButton(_ todo: ProactiveTodo, done: Bool, busy: Bool) -> some View {
        Button { Task { await actions.toggleDone(todo, today: today) } } label: {
            Image(systemName: done ? "checkmark.circle.fill" : "circle")
                .font(.wisp(size: 15, weight: .light))
                .foregroundStyle(done ? Theme.textSecondary : Theme.textPrimary.opacity(0.85))
                .frame(width: 18, height: 18).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(busy)
        .help(done ? "Mark as not done" : "Check it off")
        .accessibilityLabel(done ? "Done: \(todo.title)" : "Check off \(todo.title)")
    }

    /// Who it is from and what they said: "Jo Tanaka · “let's talk tmrw”".
    /// With no sender, the course a title ended in ("Readings [CS 101]"); a
    /// quote that only repeats the title is left out (BriefPlate.quoteRepeats).
    private func todoSub(_ todo: ProactiveTodo) -> String {
        let split = BriefPlate.splitCourse(todo.title)
        let from: String = todo.shownWho.isEmpty ? split.course : todo.shownWho
        let quote: String = BriefPlate.quoteRepeats(todo.quote, title: split.title) ? "" : "“" + todo.quote + "”"
        return [from, quote].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The title as the row shows it: without a course code in brackets at its end.
    private func todoTitle(_ todo: ProactiveTodo) -> String { BriefPlate.splitCourse(todo.title).title }

    /// The title; under it who it is from, unless that goes on the second line.
    private func todoText(_ todo: ProactiveTodo, done: Bool) -> some View {
        let sub: String = sideBySide ? "" : todoSub(todo)
        return VStack(alignment: .leading, spacing: 1) {
            Text(todoTitle(todo)).font(.wisp(size: 13.5))
                .foregroundStyle(done ? Theme.textMuted : Theme.textPrimary)
                .strikethrough(done, color: Theme.textMuted)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .help(todo.title)
            if !sub.isEmpty && !done {
                Text(sub).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// When it is due, then Add to Day and ✓ / ×: the to-do's second line
    /// (`below`), or at the right of its title.
    private func todoActions(_ todo: ProactiveTodo, done: Bool, busy: Bool, below: Bool) -> some View {
        let overdue = todo.dueLabel.lowercased().contains("overdue")
        return HStack(alignment: .center, spacing: 8) {
            if !todo.dueLabel.isEmpty && !done {
                Text(todo.dueLabel.uppercased())
                    .font(.wisp(size: 10, design: .monospaced))
                    .foregroundStyle(overdue ? Theme.textPrimary : Theme.textSecondary)
                    .lineLimit(1).fixedSize()
            }
            // Side by side, who it is from shares the second line with the due time.
            if below && !done && !todoSub(todo).isEmpty {
                Text(todoSub(todo)).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            if below { Spacer(minLength: 6) }
            if busy {
                ProgressView().controlSize(.small)
            } else if todo.canAddToDay && !done {
                if actions.addedToDay.contains(todo.id) {
                    Text("ON DAY ✓").font(.wisp(size: 9.5, design: .monospaced)).foregroundStyle(Theme.textMuted).fixedSize()
                } else {
                    NotchChrome.small("Add to Day") { Task { await actions.addToDay(todo, today: today) } }
                        .help("Fit it into today's plan")
                }
            }
            if !busy { rowControls(todo.actionRow) }
        }
        .fixedSize(horizontal: !below, vertical: false)
        .padding(.leading, below ? Self.rowInset : 0)
    }

    /// A row's trailing ×: Clear, and Mark done for a dated reminder.
    private func rowControls(_ row: NeedsYouRow) -> some View {
        NeedsYouRowControls(row: row, actions: itemActions, onResize: { model.onResize() },
                            onConfirmed: { await today.refresh(afterMutation: true) })
    }

    // MARK: Mail and texts

    private static let attentionLimit = 4

    /// What mail and texts need from you, in NEEDS YOU under the cards:
    /// replies first, then FYI; the sender on one line and what it is about
    /// on the next.
    private func attention(_ threads: [ProactiveThread]) -> some View {
        // Stacked, one fewer, so the normal panel stays short.
        let limit: Int = sideBySide ? Self.attentionLimit : Self.attentionLimit - 1
        let visible: [ProactiveThread] = actions.showAllThreads ? threads : Array(threads.prefix(limit))
        return VStack(alignment: .leading, spacing: 2) {
            ForEach(visible) { thread in threadRow(thread) }
            if threads.count > limit {
                Button(actions.showAllThreads ? "Show fewer"
                       : "+\(threads.count - limit) more · Show all") {
                    actions.showAllThreads.toggle()
                    model.onResize()
                }
                .buttonStyle(.plain).font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.leading, 32).padding(.top, 3)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Mail and texts")
    }

    private func threadRow(_ thread: ProactiveThread) -> some View {
        let open = actions.expanded.contains(thread.id)
        let hot = thread.needsReply
        let hovered = hoveredThread == thread.id
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 4) {
                Button {
                    if open { actions.expanded.remove(thread.id) } else { actions.expanded.insert(thread.id) }
                    model.onResize()
                } label: {
                    threadLabel(thread, open: open)
                        .padding(.vertical, 6).padding(.leading, 8).padding(.trailing, 6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(hovered || open ? 0.05 : hot ? 0.03 : 0)))
                        .overlay(alignment: .leading) {
                            // Replies owed get a quiet accent, so they read first.
                            if hot { RoundedRectangle(cornerRadius: 1).fill(Color.white.opacity(0.6)).frame(width: 2).padding(.vertical, 6) }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(StudyRowButtonStyle())
                .onHover { inside in hoveredThread = inside ? thread.id : (hoveredThread == thread.id ? nil : hoveredThread) }
                .help(open ? "Hide the actions" : "Reply, open or ask about this")
                .accessibilityValue(open ? "Expanded" : "Collapsed")
                // Beside the row, not in it: a button inside the row's button
                // would open the thread instead.
                rowControls(thread.actionRow).padding(.top, 6)
            }
            if let failure = itemActions.failures[thread.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 32)
            }
            if open { threadDetail(thread) }
        }
    }

    /// The sender on one line, what it's about on the next (all of it once
    /// opened); NEEDS REPLY or FYI and the time on the right.
    @ViewBuilder private func threadLabel(_ thread: ProactiveThread, open: Bool) -> some View {
        // Stacked (full width), an FYI is one line until it is opened.
        if !sideBySide && !thread.needsReply && !open {
            threadLine(thread)
        } else {
            threadLines(thread, open: open)
        }
    }

    /// An FYI on one line: who, what it's about, the label and the time.
    private func threadLine(_ thread: ProactiveThread) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: DayTimeline.sourceSymbol(thread.source))
                .font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
            Text(thread.shownWho.isEmpty ? "Someone" : thread.shownWho).font(.wisp(size: 13, weight: .semibold))
                .foregroundStyle(Theme.textSecondary).lineLimit(1).fixedSize()
            Text(thread.line).font(.wisp(size: 13)).foregroundStyle(Theme.textMuted).lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            threadBadge(false)
            Text(timeLabel(thread.receivedTs)).font(.wisp(size: 10, design: .monospaced))
                .foregroundStyle(Theme.textMuted).fixedSize()
        }
        .help(thread.line)
        .accessibilityElement(children: .combine)
    }

    private func threadLines(_ thread: ProactiveThread, open: Bool) -> some View {
        let hot = thread.needsReply
        return HStack(alignment: .top, spacing: 9) {
            Image(systemName: DayTimeline.sourceSymbol(thread.source))
                .font(.wisp(size: 11)).foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted)
                .frame(width: 15).padding(.top, 2)
            VStack(alignment: .leading, spacing: 1) {
                Text(thread.shownWho.isEmpty ? "Someone" : thread.shownWho).font(.wisp(size: 13, weight: .semibold))
                    .foregroundStyle(hot ? Theme.textPrimary : Theme.textSecondary).lineLimit(1)
                Text(thread.line).font(.wisp(size: 13))
                    .foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted)
                    .lineLimit(open ? nil : 1)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            VStack(alignment: .trailing, spacing: 3) {
                threadBadge(hot)
                Text(timeLabel(thread.receivedTs)).font(.wisp(size: 10, design: .monospaced))
                    .foregroundStyle(Theme.textMuted).fixedSize()
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func threadBadge(_ hot: Bool) -> some View {
        Text(hot ? "NEEDS REPLY" : "FYI").font(.wisp(size: 9, weight: .semibold, design: .monospaced))
            .foregroundStyle(hot ? Theme.textPrimary : Theme.textMuted)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .overlay(Capsule().stroke(hot ? Color.white.opacity(0.45) : Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    private func threadDetail(_ thread: ProactiveThread) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !thread.title.isEmpty && thread.title != thread.line {
                Text(thread.title).font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            HStack(spacing: 6) {
                if thread.canReply {
                    NotchChrome.small(actions.replying == thread.id ? "Close reply" : "Reply") {
                        if actions.replying == thread.id { actions.closeReply(thread.id) } else {
                            actions.replying = thread.id
                            onReply?(thread)   // HOOK(W3)
                        }
                        model.onResize()
                    }
                    .help("Wisp drafts a reply you edit before sending")
                }
                NotchChrome.small("Ask about this") {
                    model.askAbout("\(thread.who): \(thread.line)", reference: thread.reference)
                }
                .disabled(!model.conversationSettled)
                NotchChrome.small("Open \(appName(thread)) ↗") { openSource(thread) }
            }
            if actions.replying == thread.id { replyArea(thread).revealInPanel("reply-\(thread.id)") }
        }
        .padding(.leading, 32).padding(.bottom, 6)
    }

    /// HOOK(W3): the reply composer, or a placeholder until it is wired.
    @ViewBuilder private func replyArea(_ thread: ProactiveThread) -> some View {
        if let composer = BriefReplyHook.composer {
            composer(thread) { actions.closeReply(thread.id); model.onResize() }
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Replying from the Brief isn't set up in this build. Open the thread in Mail to answer it.")
                    .font(.wisp(size: 12)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(10)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.editorFill))
        }
    }

    private func appName(_ thread: ProactiveThread) -> String {
        if thread.source == "messages" { return "Messages" }
        return isLinkedGmail(thread.account) ? "Gmail" : "Mail"
    }

    /// A linked Google account other than the person's own reads in Gmail, not Mail.
    private func isLinkedGmail(_ account: String) -> Bool {
        !account.isEmpty && account != proactive.ownerID
    }

    private func openSource(_ thread: ProactiveThread) {
        if thread.source == "messages" { NotchChrome.open(.messages); return }
        if isLinkedGmail(thread.account),
           let url = URL(string: "https://mail.google.com/mail/u/?authuser=" + (thread.account.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")) {
            NSWorkspace.shared.open(url)
            return
        }
        NotchChrome.open(.mail)
    }

    /// "9:12 AM" today, "Wed" this week, "Oct 12" before that.
    private func timeLabel(_ ts: Double?) -> String {
        guard let ts else { return "" }
        let start = today.dayInterval.start.timeIntervalSince1970
        if ts >= start { return today.clock(ts) }
        let date = Date(timeIntervalSince1970: ts)
        let style = start - ts < 6 * 86400
            ? Date.FormatStyle(timeZone: today.timezone).weekday(.abbreviated)
            : Date.FormatStyle(timeZone: today.timezone).month(.abbreviated).day()
        return date.formatted(style)
    }
}

// MARK: - The Brief's two zones

/// Lays out the Brief's zones (left zone, a hairline, right zone) side by
/// side, each half as wide and the hairline between them as tall as the
/// taller zone, or stacked in that order with the hairline across. Which one
/// is BriefPlate.sideBySide's call, made once for the Brief's width.
struct BriefZones: Layout {
    let sideBySide: Bool
    var gap: CGFloat = 28
    var stackGap: CGFloat = 18

    private func columnWidth(_ width: CGFloat) -> CGFloat { max(0, (width - gap) / 2) }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard subviews.count == 3 else { return .zero }
        let width: CGFloat = proposal.width ?? 0
        if sideBySide {
            let column = ProposedViewSize(width: columnWidth(width), height: nil)
            let left = subviews[0].sizeThatFits(column).height
            let right = subviews[2].sizeThatFits(column).height
            return CGSize(width: width, height: max(left, right))
        }
        let full = ProposedViewSize(width: width, height: nil)
        let left = subviews[0].sizeThatFits(full).height
        let right = subviews[2].sizeThatFits(full).height
        return CGSize(width: width, height: left + right + 1 + 2 * stackGap)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard subviews.count == 3 else { return }
        let width = bounds.width
        if sideBySide {
            let columnW = columnWidth(width)
            let column = ProposedViewSize(width: columnW, height: nil)
            subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: column)
            let line = CGPoint(x: bounds.minX + columnW + gap / 2, y: bounds.minY)
            subviews[1].place(at: line, anchor: .top, proposal: ProposedViewSize(width: 1, height: bounds.height))
            subviews[2].place(at: CGPoint(x: bounds.maxX - columnW, y: bounds.minY), anchor: .topLeading, proposal: column)
            return
        }
        let full = ProposedViewSize(width: width, height: nil)
        let left = subviews[0].sizeThatFits(full).height
        subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: full)
        let lineY = bounds.minY + left + stackGap
        subviews[1].place(at: CGPoint(x: bounds.minX, y: lineY), anchor: .topLeading,
                          proposal: ProposedViewSize(width: width, height: 1))
        subviews[2].place(at: CGPoint(x: bounds.minX, y: lineY + 1 + stackGap), anchor: .topLeading, proposal: full)
    }
}

// MARK: - A scroll area as tall as its content, up to a cap

/// As tall as its content until the content passes `cap`, then that tall and
/// scrolling. While the content fits it looks like no scroll view at all: no
/// indicator and no bounce. Used for an opened Brief row, and for the whole
/// Brief and Day tabs under the panel's header (OverlayView.tabScroll), where
/// `reveals` lets a view inside ask to be scrolled into sight (`revealInPanel`).
///
/// Its height is measured in the same layout pass as everything else
/// (CappedHeight), not learned a pass later: a page that arrives with a slide
/// is its real height from its first frame, so the panel moves once, not twice.
/// `remembers` keeps the scroll position under that key while the panel is
/// open (PanelScrollMemory). `showsMore` (Study) makes an overflow obvious: the
/// indicator always shows and flashes on arrival, and a soft fade sits over the
/// bottom edge, with room under the content so the last line clears it.
struct CappedScroll<Content: View>: View {
    let cap: CGFloat?
    var reveals = false
    var remembers: String? = nil
    var showsMore = false
    @ViewBuilder var content: Content
    /// The content's height, for the scroll indicator only.
    @State private var height: CGFloat = 0
    /// `showsMore`: bumped once, when the content is first measured past the cap,
    /// so the indicator flashes as the page arrives overflowing and never again
    /// as something inside it opens or closes across the cap.
    @State private var flashes = 0
    @State private var measured = false
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride

    var body: some View {
        if let cap {
            CappedHeight(cap: cap) {
                ScrollViewReader { proxy in
                    ScrollView(.vertical) {
                        content
                            .background(GeometryReader { geo in
                                Color.clear.preference(key: CappedScrollHeight.self, value: geo.size.height)
                            })
                            .padding(.bottom, more(cap) ? Self.fade : 0)
                            // The overflow is learned a pass after the change that
                            // caused it, outside its animation: ease what it adds.
                            .animation(moreMotion, value: more(cap))
                    }
                    .scrollIndicators(height > cap ? (showsMore ? .visible : .automatic) : .never)
                    .scrollIndicatorsFlash(trigger: flashes)
                    .scrollBounceBehavior(.basedOnSize)
                    .overlay(alignment: .bottom) {
                        // Always there for showsMore, faded in and out with the overflow
                        // on the panel's spring rather than popping in at full strength.
                        if showsMore {
                            LinearGradient(colors: [Theme.surface.opacity(0), Theme.surface],
                                           startPoint: .top, endPoint: .bottom)
                                .frame(height: Self.fade)
                                .opacity(more(cap) ? 1 : 0)
                                .animation(moreMotion, value: more(cap))
                                .allowsHitTesting(false)
                                .accessibilityHidden(true)
                        }
                    }
                    .modifier(RemembersScroll(key: remembers))
                    .environment(\.panelReveal, reveals ? PanelReveal { id in
                        // After the panel has grown to fit what just opened. Never
                        // animated, so Reduce Motion needs no special case.
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { proxy.scrollTo(id) }
                    } : nil)
                }
            }
            .onPreferenceChange(CappedScrollHeight.self) { new in
                // Crossing the cap: the fade's room is added in the same animated
                // transaction, not as a 24 pt jump in the first frame.
                if showsMore, measured, (new > cap) != (height > cap) {
                    withAnimation(moreMotion) { height = new }
                } else {
                    height = new
                }
                guard showsMore, !measured, new > 0 else { return }
                measured = true
                if new > cap { flashes += 1 }
            }
            // A CappedScroll inside this one reports its own content; keep it
            // from reaching (and inflating) an outer one.
            .transformPreference(CappedScrollHeight.self) { $0 = CappedScrollHeight.defaultValue }
        } else {
            content
        }
    }

    /// The bottom fade's height (`showsMore`).
    private static var fade: CGFloat { 24 }

    /// `showsMore` and the content passes the cap.
    private func more(_ cap: CGFloat) -> Bool { showsMore && height > cap }

    /// The fade and its room following the overflow: the panel's spring, none with Reduce Motion.
    private var moreMotion: Animation? { Theme.Motion.panel(reduceMotion: reduceMotionOverride ?? systemReduceMotion) }
}

/// Lays out one scroll view as tall as its content, up to `cap`. A vertical
/// ScrollView asked for its ideal height answers with its content's height.
struct CappedHeight: Layout {
    let cap: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let scroll = subviews.first else { return .zero }
        let ideal = scroll.sizeThatFits(ProposedViewSize(width: proposal.width, height: nil))
        return CGSize(width: proposal.width ?? ideal.width, height: max(min(ideal.height, cap), 1))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: bounds.origin, anchor: .topLeading, proposal: ProposedViewSize(bounds.size))
    }
}

/// Where each tab's scroll was left, while the panel stays open. Going back to
/// a tab finds it there; closing the panel forgets them all, so it reopens on
/// each tab's top as before. `version` lets a view refuse a position that no
/// longer fits what it shows (Ask: a conversation that has moved on).
@MainActor
enum PanelScrollMemory {
    private static var offsets: [String: (y: CGFloat, version: Int)] = [:]

    static func key(_ tab: NotchTab) -> String { "tab.\(tab.rawValue)" }

    static func note(_ key: String, y: CGFloat, version: Int = 0) { offsets[key] = (y, version) }

    /// The remembered offset, if one was kept for this version.
    static func offset(_ key: String, version: Int = 0) -> CGFloat? {
        guard let kept = offsets[key], kept.version == version else { return nil }
        return kept.y
    }

    static func forget(_ key: String) { offsets[key] = nil }

    static func forgetAll() { offsets = [:] }
}

/// Keeps a scroll view's position under `key` (PanelScrollMemory). Needs
/// macOS 15's scroll geometry; on macOS 14 a tab comes back at its top.
struct RemembersScroll: ViewModifier {
    let key: String?
    var version = 0

    func body(content: Content) -> some View {
        if #available(macOS 15, *), let key {
            content.modifier(RemembersScrollPosition(key: key, version: version))
        } else {
            content
        }
    }
}

@available(macOS 15, *)
private struct RemembersScrollPosition: ViewModifier {
    let key: String
    let version: Int
    @State private var position: ScrollPosition

    init(key: String, version: Int) {
        self.key = key
        self.version = version
        let y = PanelScrollMemory.offset(key, version: version)
        _position = State(initialValue: y.map { ScrollPosition(point: CGPoint(x: 0, y: $0)) } ?? ScrollPosition())
    }

    func body(content: Content) -> some View {
        content
            .scrollPosition($position)
            .onScrollGeometryChange(for: CGFloat.self, of: { $0.contentOffset.y + $0.contentInsets.top }) { _, y in
                PanelScrollMemory.note(key, y: max(y, 0), version: version)
            }
    }
}

/// Scrolls the enclosing tab scroll the least it takes to show one view.
struct PanelReveal {
    let scroll: (String) -> Void
}

private struct PanelRevealKey: EnvironmentKey {
    static let defaultValue: PanelReveal? = nil
}

extension EnvironmentValues {
    var panelReveal: PanelReveal? {
        get { self[PanelRevealKey.self] }
        set { self[PanelRevealKey.self] = newValue }
    }
}

private struct RevealInPanel: ViewModifier {
    let id: String
    @Environment(\.panelReveal) private var reveal
    @State private var appeared = Date.distantPast
    /// A reply composer grows when its draft arrives; follow it that long.
    static var settle: TimeInterval { 10 }

    func body(content: Content) -> some View {
        content.id(id)
            .background(GeometryReader { geo in
                Color.clear.onChange(of: geo.size.height) { old, new in
                    if new > old, Date().timeIntervalSince(appeared) < Self.settle { reveal?.scroll(id) }
                }
            })
            .onAppear {
                appeared = Date()
                reveal?.scroll(id)
            }
    }
}

extension View {
    /// When this appears inside a scrolling tab (a reply composer, an opened
    /// draft), scroll it into sight; elsewhere it does nothing.
    func revealInPanel(_ id: String) -> some View { modifier(RevealInPanel(id: id)) }
}

private struct CappedScrollHeight: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// A card body held to `lines` lines, with More and Less when it's longer, so
/// the rest is a click (or VoiceOver's press) away rather than hover-only. The
/// full text is measured hidden behind the clamped one to know whether it was cut.
struct ClampedText: View {
    let text: String
    var lines = 2
    /// Told when the text opens or closes, to refit the panel.
    var onToggle: () -> Void = {}
    @State private var open = false
    @State private var clampedHeight: CGFloat = 0
    @State private var fullHeight: CGFloat = 0

    private var cut: Bool { fullHeight > clampedHeight + 1 }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            styled(Text(text))
                .lineLimit(open ? nil : lines)
                .fixedSize(horizontal: false, vertical: true)
                .background(GeometryReader { geo in
                    Color.clear
                        .onAppear { clampedHeight = geo.size.height }
                        .onChange(of: geo.size.height) { clampedHeight = geo.size.height }
                })
                .background(alignment: .topLeading) {
                    styled(Text(text))
                        .fixedSize(horizontal: false, vertical: true)
                        .hidden()
                        .background(GeometryReader { geo in
                            Color.clear
                                .onAppear { fullHeight = geo.size.height }
                                .onChange(of: geo.size.height) { fullHeight = geo.size.height }
                        })
                        .accessibilityHidden(true)
                }
            if open || cut {
                Button(open ? "Less" : "More") {
                    open.toggle()
                    onToggle()
                }
                .buttonStyle(.plain)
                .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textMuted)
                .accessibilityHint(open ? "Shows two lines" : "Shows the whole note")
            }
        }
    }

    private func styled(_ t: Text) -> some View {
        t.font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary).lineSpacing(1.5)
    }
}

/// The Brief's study cards, under its school line (both Briefs): Study's line
/// on up to two lines, the slides it is about in one muted line, and one small
/// action; then "+N more in Study". Display only.
struct StudyBriefCardsView: View {
    let cards: StudyBriefCards
    let model: OverlayModel
    /// Lines the cards up under the school line's text.
    var leading: CGFloat = 24
    /// False when the school rows above already end in "+N more in Study", so
    /// the same line doesn't show twice in a row.
    var showsMore = true

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(cards.cards) { card in row(card) }
            if showsMore, let more = cards.moreLine {
                Text(more).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.leading, leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Ready in Study")
    }

    private func row(_ card: StudyBriefCard) -> some View {
        let slides = SchoolSlidesText.line(card.slides, pagesText: StudyText.slidePages)
        // One line: Study's sentence, then the slides muted after it; the tail
        // truncates and the whole line is in the tooltip.
        let line = Text(card.text).font(.wisp(size: 12.5)).foregroundColor(Theme.textPrimary)
            + Text(slides.map { "  " + $0 } ?? "").font(.wisp(size: 11)).foregroundColor(Theme.textMuted)
        return HStack(alignment: .center, spacing: 8) {
            line
                .lineLimit(1).truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .help(card.text + (slides.map { "\n" + $0 } ?? ""))
                .accessibilityLabel(card.text + (slides.map { ". Slides: " + $0 } ?? ""))
            if let action = card.action, let title = card.actionTitle {
                NotchChrome.action(title, help: action.help, spoken: title + ": " + card.text) {
                    SchoolActions.run(action, model: model)
                }
            }
        }
        .frame(minHeight: 24)
    }
}

private extension StudyBriefCard.Action {
    var help: String {
        switch self {
        case .open: return "Open it in Study (⌘5)"
        case .review: return "Make flash cards from these slides, in Study (⌘5)"
        }
    }
}
