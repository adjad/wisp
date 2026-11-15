import SwiftUI
import AppKit

// The Brief's proactive half: READY FOR YOU ("Before you ask" cards, what
// Study got ready, what was found in mail, what Wisp did for you) and YOUR
// LISTS (to-dos from every source, school work, per-thread summaries), from `workspace.proactive`
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
    /// READY FOR YOU lists every prepared thing, not only the first few.
    @Published var showAllReady = false
    /// The acting-for-you box's log (what Wisp did lately) is open.
    @Published var showActingLog = false
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

/// The proactive Brief in two zones: READY FOR YOU, what Wisp got ready before
/// you asked (the "Before you ask" card large, then one compact row for each
/// other card, what Study got ready and each thing found in mail, then the
/// acting-for-you box), set apart on a tinted panel; and YOUR LISTS (the rest
/// of today's calendar, to-dos, school work due and the mail and texts that
/// need you). Side by side when the Brief is wide enough
/// (BriefPlate.sideBySide: the wide panel), else stacked, READY FOR YOU first.
/// One layout, measured on the width it is given; both zones scroll with the
/// tab. Every button is one the Brief already had: nothing is sent or added
/// without a tap.
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
    /// This week's school work: SCHOOL in YOUR LISTS (StudyDaySummary.briefPlate).
    var schoolRows = StudyUpcomingRows()
    /// What Study got ready and the topics to look at again (StudyDaySummary.briefCards): rows in READY FOR YOU.
    var studyCards = StudyBriefCards()
    /// "Found in your mail": each suggestion is a row in READY FOR YOU.
    @ObservedObject var mailMemory: MailMemoryModel = MailMemoryCards.model
    /// "Let Wisp act for me": the acting-for-you box's state, today's count and its log.
    @ObservedObject var acting: ActForMeModel = .shared

    #if WISP_NOTCH_QA
    /// The QA driver's acting state, so a render never reads the live service.
    static var qaActing: ActForMe? = nil
    private var actingState: ActForMe? { Self.qaActing ?? acting.state }
    #else
    private var actingState: ActForMe? { acting.state }
    #endif

    private var unread: [SourceAccess] { proactive.unreadSources(coverage) }

    /// A heading row with buttons (READY FOR YOU's), one height.
    private static let controlsHeaderHeight: CGFloat = 22
    /// The gap between the zones side by side, and between them stacked.
    private static let zoneGap: CGFloat = 28
    private static let stackGap: CGFloat = 12
    /// READY FOR YOU's tag column: as wide as the widest tag its rows show,
    /// so the tags line up and the titles keep the rest.
    private var tagColumn: CGFloat {
        let shown: ProactiveState = proactive.only(chosen)
        let cards: [String] = liveCards(shown).filter { !$0.wispDidThis }.map(cardTag)
        let study: [String] = studyCards.cards.map(studyTag)
        let found: [String] = foundItems.isEmpty ? [] : [BriefReady.foundTag]
        let font = NSFont.monospacedSystemFont(ofSize: 9 * Theme.textScale, weight: .semibold)
        let advance: CGFloat = ("M" as NSString).size(withAttributes: [.font: font]).width + 0.3
        return BriefReady.tagColumn(cards + study + found, charWidth: advance)
    }

    private func cardTag(_ card: ProactiveCard) -> String {
        BriefReady.cardTag(method: card.action?.method ?? "", hasDraft: !(card.action?.draft ?? "").isEmpty)
    }

    private func studyTag(_ card: StudyBriefCard) -> String {
        if case .open = card.action { return BriefReady.studyTag(id: card.id, opensMaterial: true) }
        return BriefReady.studyTag(id: card.id, opensMaterial: false)
    }
    /// A list row's text starts here: the check circle and its gap.
    private static let listInset: CGFloat = 26
    @State private var hoveredThread: String? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// Whether the zones sit side by side: from the width the panel lays the
    /// Brief out at (its page width, OverlayView), decided in the same update
    /// as the width changes. A width measured after layout lagged a pass, so
    /// turning two columns off first drew the stacked zones with five
    /// two-line rows, then shrank to three.
    private var sideBySide: Bool {
        let panel: CGFloat = DisplayGeometry.shared.layout.widened(wide).expandedWidth
        return BriefPlate.sideBySide(width: panel - 2 * OverlayView.sideInset, textScale: Theme.textScale)
    }

    /// Stacked, YOUR LISTS still splits in two (to-dos and school | mail and
    /// texts) at the default and Large text sizes; Extra large takes one column.
    private var listsSplit: Bool { !sideBySide && Theme.textScale < 1.2 }

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
            readyZone(shown)
            Rectangle().fill(Theme.hairline).accessibilityHidden(true)
            listsZone(shown)
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

    // MARK: Ready for you

    /// What READY FOR YOU holds: the cards that ask (the large one among
    /// them), the "Wisp did this" cards (each with its Undo, in the acting
    /// box), what Study got ready and what was found in mail.
    private struct Ready {
        var asks: [ProactiveCard]
        var did: [ProactiveCard]
        var study: [StudyBriefCard]
        var found: [MailSuggestion]
        var count: Int { asks.count + study.count + found.count }
    }

    /// One compact row under the large card.
    private enum ReadyItem: Identifiable {
        case card(ProactiveCard)
        case study(StudyBriefCard)
        case found(MailSuggestion)

        var id: String {
            switch self {
            case .card(let card): return "card:" + card.id
            case .study(let card): return "study:" + card.id
            case .found(let item): return "found:\(item.id)"
            }
        }
    }

    private func ready(_ shown: ProactiveState) -> Ready {
        let cards: [ProactiveCard] = liveCards(shown)
        return Ready(asks: cards.filter { !$0.wispDidThis }, did: cards.filter(\.wispDidThis),
                     study: studyCards.cards, found: foundItems)
    }

    /// "Found in your mail" while the person has it showing ("live"): the first three, as before.
    private var foundItems: [MailSuggestion] {
        mailMemory.mode == "live" ? Array(mailMemory.suggestions.prefix(3)) : []
    }

    /// The left zone (first, stacked). Found in your mail refreshes here, as
    /// its own section did, and says its results to VoiceOver.
    private func readyZone(_ shown: ProactiveState) -> some View {
        beforeYouAsk(shown)
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .task { await mailMemory.refresh(); model.onResize() }
            .onReceive(NotificationCenter.default.publisher(for: .wispAssistantChanged)) { _ in
                Task { await mailMemory.refresh(); model.onResize() }
            }
            .onChange(of: mailMemory.confirmation) { _, text in
                if let text { Self.announce(text) }
            }
            .onChange(of: mailMemory.failures) { old, new in
                for (id, text) in new where old[id] != text { Self.announce(text) }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Ready for you")
    }

    /// Says a result aloud to VoiceOver, since these lines appear without focus moving.
    private static func announce(_ text: String) {
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }

    /// READY FOR YOU: the tinted panel, or with nothing ready one line.
    @ViewBuilder private func beforeYouAsk(_ shown: ProactiveState) -> some View {
        let held: Ready = ready(shown)
        let busy: Bool = proactive.isWorking || actions.analyzePending()
        Group {
            if held.count == 0 && held.did.isEmpty && !busy && actions.analyzeError.isEmpty {
                readyEmpty
            } else {
                readyPanel(held)
            }
        }
        // The gate's state is loaded HERE, on a view that always exists: every time the Brief
        // shows, and again whenever its cards change (a cycle finished, an Undo), so the
        // question, the box and today's count are never missing or stale. (A .task on the
        // question card itself never ran: that view is empty until the state is loaded.)
        // The QA driver never reads the live service: qaActing stands in.
        #if WISP_NOTCH_QA
        .task(id: shown.cards.map(\.id)) {}
        #else
        .task(id: shown.cards.map(\.id)) { await ActForMeModel.shared.refresh() }
        #endif
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

    /// The panel: the heading, the large card, one row per other ready
    /// thing, then the acting-for-you box.
    private func readyPanel(_ held: Ready) -> some View {
        let shape = RoundedRectangle(cornerRadius: 14, style: .continuous)
        let index: Int = held.asks.firstIndex { $0.id == actions.heroID } ?? 0
        let rest: [ReadyItem] = readyItems(held, hero: held.asks.isEmpty ? nil : index)
        return VStack(alignment: .leading, spacing: 8) {
            readyHeader(count: held.count)
            if proactive.isWorking || actions.analyzePending() { progressRow }
            if !actions.analyzeError.isEmpty {
                Text(actions.analyzeError).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
            }
            if !held.asks.isEmpty {
                cardView(held.asks[index], position: index, of: held.asks)
                    .id(held.asks[index].id)
                    .transition(.opacity)
            }
            if !rest.isEmpty || studyCards.moreLine != nil { otherCards(rest) }
            actingArea(held.did)
        }
        .padding(EdgeInsets(top: 8, leading: 12, bottom: 10, trailing: 12))
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(shape.fill(Theme.warn.opacity(0.045)))
        .overlay(shape.stroke(Theme.warn.opacity(0.24), lineWidth: 1))
    }

    /// The rows under the large card: the other cards, Study's, then mail's.
    private func readyItems(_ held: Ready, hero: Int?) -> [ReadyItem] {
        let cards: [ReadyItem] = held.asks.enumerated().filter { $0.offset != hero }.map { ReadyItem.card($0.element) }
        let study: [ReadyItem] = held.study.map { ReadyItem.study($0) }
        let found: [ReadyItem] = held.found.map { ReadyItem.found($0) }
        return cards + study + found
    }

    /// READY FOR YOU · n, what it read and when, the account menu and Analyze now.
    private func readyHeader(count: Int) -> some View {
        HStack(alignment: .center, spacing: 7) {
            Image(systemName: "sparkle").font(.wisp(size: 10, weight: .semibold)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            zoneTitle("READY FOR YOU", spoken: "Ready for you", count: count, lit: true).fixedSize()
            readSourceLine
            Spacer(minLength: 4)
            accountMenu
            analyzeControl
        }
        .frame(minHeight: Self.controlsHeaderHeight)
    }

    /// "from 49 emails and texts · 10:24 AM", from the engine's own numbers;
    /// side by side, "49 read · 10:24 AM" when the sentence would be cut, so
    /// the time always shows (BriefReady.sourceForms). The sentence is its tooltip.
    @ViewBuilder private var readSourceLine: some View {
        if let stats = proactive.stats {
            let at: String = (stats.asOf ?? proactive.asOf).map { today.clock($0) } ?? ""
            let forms: [String] = BriefReady.sourceForms(read: stats.read, at: at, sideBySide: sideBySide)
            if let full = forms.first {
                let short: String = forms.count > 1 ? forms[1] : full
                ViewThatFits(in: .horizontal) {
                    Text(full).lineLimit(1).fixedSize()
                    Text(short).lineLimit(1).fixedSize()
                    Text(short).lineLimit(1).truncationMode(.tail)
                }
                .font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted)
                .help(full)
                .accessibilityLabel(full)
            }
        }
    }

    /// Nothing ready: one line, with Analyze now beside it; "Let Wisp act for
    /// me" under it while it is asking or off, its log while it is on.
    private var readyEmpty: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "sparkle").font(.wisp(size: 10, weight: .semibold)).foregroundStyle(Theme.warn)
                    .accessibilityHidden(true)
                Text(readyEmptyLine).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let state = actingState, state.enabled { actingLogButton }
                accountMenu
                analyzeControl
            }
            if let state = actingState, state.enabled {
                if actions.showActingLog { actingLog(state) }
            } else {
                ActForMeBriefRow()
            }
        }
        .padding(.vertical, 2)
    }

    /// The one line: what Wisp checked and when, and what it did today; or,
    /// while it is reading or couldn't, the reason (never an all-clear for a
    /// source it couldn't read).
    private var readyEmptyLine: String {
        guard unread.isEmpty, !proactive.isUnavailable, !proactive.hasReadNothing,
              let stats = proactive.stats, stats.read > 0 else { return needsYouEmptyLine }
        let at: String = (stats.asOf ?? proactive.asOf).map { today.clock($0) } ?? ""
        let acted: Int? = actingState.flatMap { $0.enabled ? $0.today : nil }
        return BriefReady.emptyLine(read: stats.read, at: at, actingToday: acted)
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

    /// "Let Wisp act for me" at READY FOR YOU's foot: the question until it is
    /// answered and one line while it is off (ActForMeBriefRow, as before);
    /// while it is on, the box. What Wisp did shows with its own Undo either way.
    @ViewBuilder private func actingArea(_ did: [ProactiveCard]) -> some View {
        if let state = actingState, state.enabled {
            actingBox(state, did: did)
        } else {
            ActForMeBriefRow()
            if !did.isEmpty { actingAdded(did) }
        }
    }

    /// Acting for you: on, today's count and the log, then what Wisp added
    /// today, each with the Undo its card always had.
    private func actingBox(_ state: ActForMe, did: [ProactiveCard]) -> some View {
        let shape = RoundedRectangle(cornerRadius: 12, style: .continuous)
        let oneLine: Bool = !sideBySide && did.count == 1 && !did[0].isGroup && Theme.textScale < 1.2
        return VStack(alignment: .leading, spacing: 6) {
            if oneLine {
                // Stacked, one thing added: the state, it and its Undo, and Log share a line.
                HStack(alignment: .center, spacing: 10) {
                    actingLabel(state).fixedSize()
                    addedRow(did[0])
                    actingLogButton
                }
            } else {
                actingHead(state)
                if !did.isEmpty { actingAdded(did) }
            }
            if actions.showActingLog { actingLog(state) }
        }
        .padding(EdgeInsets(top: 6, leading: 11, bottom: 8, trailing: 8))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(shape.fill(Theme.raised))
        .overlay(shape.stroke(Theme.chipStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Acting for you")
    }

    /// "ON  Acting for you · 1 today", then Log. The switch itself stays in
    /// Settings; here it is only shown.
    private func actingHead(_ state: ActForMe) -> some View {
        HStack(alignment: .center, spacing: 7) {
            actingLabel(state)
            Spacer(minLength: 4)
            actingLogButton
        }
    }

    private func actingLabel(_ state: ActForMe) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text("ON").font(.wisp(size: 9, weight: .semibold, design: .monospaced))
                .foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(Capsule().fill(Theme.chipOn))
            Text("Acting for you").font(.wisp(size: 12.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1).fixedSize()
            Text("· \(state.today) today").font(.wisp(size: 12)).foregroundStyle(Theme.textMuted).lineLimit(1)
        }
        .help(state.does.joined(separator: "\n"))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(state.briefOn)
    }

    private var actingLogButton: some View {
        let open: Bool = actions.showActingLog
        return Button {
            actions.showActingLog.toggle()
            model.onResize()
        } label: {
            HStack(spacing: 2) {
                Text("Log")
                Image(systemName: open ? "chevron.up" : "chevron.down").font(.wisp(size: 8, weight: .bold))
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .frame(minHeight: Theme.iconButtonSize).contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .fixedSize()
        .help("What Wisp did lately. Each one also has its own Undo while it shows here.")
        .accessibilityLabel("What Wisp did lately")
        .accessibilityValue(open ? "Shown" : "Hidden")
    }

    /// What Wisp added: a grouped card's rows, or one row with its Undo.
    private func actingAdded(_ did: [ProactiveCard]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(did) { card in
                if card.isGroup { groupRows(card) } else { addedRow(card) }
            }
        }
    }

    private func addedRow(_ card: ProactiveCard) -> some View {
        let state = actions.fires[card.id]
        let line: String = card.when.isEmpty ? card.title : card.title + " · " + card.when
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .center, spacing: 8) {
                readyTag(BriefReady.cardTag(method: card.action?.method ?? "", hasDraft: false))
                addedText(card, line: line)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .help(card.why.isEmpty ? card.title : card.why)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel("Wisp added " + line)
                if let undo = card.action, !isUnconfirmed(state) { compactFire(card, undo, state) }
            }
            if case .failed(let reason) = state {
                Text(reason).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if case .unconfirmed(let note) = state {
                Text(note).font(.wisp(size: 11)).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .contain)
    }

    /// What was added: side by side, the title, then where it came from on a
    /// muted line under it, so neither is cut for the other; stacked, one line.
    @ViewBuilder private func addedText(_ card: ProactiveCard, line: String) -> some View {
        if sideBySide && !card.when.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                Text(card.title).font(.wisp(size: 12.5)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                Text(card.when).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
        } else {
            Text(line).font(.wisp(size: 12.5)).foregroundStyle(Theme.textPrimary).lineLimit(1)
        }
    }

    /// The log: what Wisp did lately, as the acting row listed it.
    @ViewBuilder private func actingLog(_ state: ActForMe) -> some View {
        if state.recent.isEmpty {
            Text("Nothing yet. When Wisp does something, it shows here with an Undo.")
                .font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(state.recent.prefix(5)) { item in actingLogRow(item) }
            }
        }
    }

    private func actingLogRow(_ item: ActForMe.Done) -> some View {
        let symbol: String = !item.check.isEmpty ? "questionmark" : item.undone ? "arrow.uturn.backward" : "checkmark"
        let text: String = !item.check.isEmpty ? "May have added “\(item.title)”: check \(item.check)"
            : item.title + (item.undone ? " (undone)" : "")
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: symbol).font(.wisp(size: 8, weight: .bold)).foregroundStyle(Theme.textMuted)
                .accessibilityHidden(true)
            Text(text).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary).lineLimit(2)
            Spacer(minLength: 4)
            Text(Date(timeIntervalSince1970: item.at), style: .relative)
                .font(.wisp(size: 10.5)).foregroundStyle(Theme.textMuted).monospacedDigit()
        }
        .accessibilityElement(children: .combine)
    }

    /// One compact row per other ready thing: a tag saying what Wisp did, the
    /// title and one muted line, its own action and its dismiss. The first
    /// few (BriefReady.rowsShown), then "+N more · Show all".
    private func otherCards(_ items: [ReadyItem]) -> some View {
        let limit: Int = BriefReady.rowsShown(sideBySide: sideBySide)
        let visible: [ReadyItem] = actions.showAllReady ? items : Array(items.prefix(limit))
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(visible) { item in readyRow(item) }
            readyMore(hidden: items.count - visible.count, canExpand: items.count > limit)
            if let confirmation = mailMemory.confirmation {
                Text(confirmation).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .padding(.leading, tagColumn + 10).padding(.top, 2)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("More things Wisp got ready")
    }

    @ViewBuilder private func readyRow(_ item: ReadyItem) -> some View {
        switch item {
        case .card(let card): cardRow(card)
        case .study(let card): studyRow(card)
        case .found(let suggestion): foundRow(suggestion)
        }
    }

    /// "+2 more · Show all" (or Show fewer), and "+N more in Study", which opens Study.
    @ViewBuilder private func readyMore(hidden: Int, canExpand: Bool) -> some View {
        let study: String? = studyCards.moreLine
        if canExpand || study != nil {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if canExpand {
                    Button(actions.showAllReady ? "Show fewer" : "+\(hidden) more · Show all") {
                        actions.showAllReady.toggle()
                        model.onResize()
                    }
                    .buttonStyle(StudyRowButtonStyle())
                }
                if canExpand && study != nil { Text("·") }
                if let study {
                    Button(study) { model.tab = .study }
                        .buttonStyle(StudyRowButtonStyle())
                        .help("Your school work (⌘5)")
                }
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .frame(minHeight: 24)
            .padding(.leading, tagColumn + 10)
            .overlay(alignment: .top) { Rectangle().fill(Theme.hairline).frame(height: 1).accessibilityHidden(true) }
        }
    }

    /// A row's frame: the tag in its column, the title and one muted line,
    /// then what it offers; a hairline above.
    private func compactRow<Trailing: View>(tag: String, title: String, detail: String, help: String,
                                            spoken: String, @ViewBuilder trailing: () -> Trailing) -> some View {
        // Side by side at Extra large, the buttons go under the words, so a title keeps its room.
        let below: Bool = sideBySide && Theme.textScale >= 1.2
        return HStack(alignment: below ? .top : .center, spacing: 8) {
            readyTag(tag).frame(width: tagColumn, alignment: .leading)
            if below {
                VStack(alignment: .leading, spacing: 3) {
                    compactText(title: title, detail: detail)
                        .help(help)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(spoken)
                    HStack(spacing: 4) { trailing() }
                }
            } else {
                compactText(title: title, detail: detail)
                    .help(help)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(spoken)
                trailing()
            }
        }
        .padding(.vertical, 5)
        .overlay(alignment: .top) { Rectangle().fill(Theme.hairline).frame(height: 1).accessibilityHidden(true) }
        .accessibilityElement(children: .contain)
    }

    /// The title and one muted line. Side by side, a title that doesn't fit
    /// on one line takes two, and the muted line gives way to it.
    @ViewBuilder private func compactText(title: String, detail: String) -> some View {
        if sideBySide {
            ViewThatFits(in: .horizontal) {
                VStack(alignment: .leading, spacing: 1) {
                    compactTitle(title).fixedSize()
                    compactDetail(detail)
                }
                compactTitle(title, lines: 2).fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            VStack(alignment: .leading, spacing: 1) {
                compactTitle(title)
                compactDetail(detail)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func compactTitle(_ title: String, lines: Int = 1) -> some View {
        Text(title).font(.wisp(size: 13, weight: .semibold)).foregroundStyle(Theme.textPrimary).lineLimit(lines)
    }

    /// One muted line that never asks for more width than it is given.
    @ViewBuilder private func compactDetail(_ detail: String) -> some View {
        if !detail.isEmpty {
            Text(detail).font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
                .frame(minWidth: 0, idealWidth: 0, maxWidth: .infinity, alignment: .leading)
        }
    }

    /// What Wisp did, in a small outlined tag: DRAFTED, QUIZ MADE, FOUND…
    private func readyTag(_ tag: String) -> some View {
        Text(tag).font(.wisp(size: 9, weight: .semibold, design: .monospaced)).tracking(0.3)
            .foregroundStyle(Theme.textSecondary).lineLimit(1).fixedSize()
            .padding(.horizontal, 5).padding(.vertical, 2)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Theme.chipStroke, lineWidth: 1))
            .accessibilityHidden(true)
    }

    /// A 24 pt icon button for a row's own action (Open, Dismiss, Add to Day).
    private func glyphButton(_ symbol: String, help: String, spoken: String,
                             action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.wisp(size: 10.5, weight: .semibold)).foregroundStyle(Theme.textMuted)
                .frame(width: Theme.iconButtonSize, height: Theme.iconButtonSize)
                .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help(help)
        .accessibilityLabel(spoken)
    }

    /// Another card: its question as the button. A reply opens as the large
    /// card with its draft showing (Send is inside the draft, as always);
    /// any other card does what its large card's button does.
    private func cardRow(_ card: ProactiveCard) -> some View {
        let tag: String = BriefReady.cardTag(method: card.action?.method ?? "", hasDraft: !(card.action?.draft ?? "").isEmpty)
        let detail: String = card.body.isEmpty ? card.metaLine.capitalized : card.body
        let state = actions.fires[card.id]
        return VStack(alignment: .leading, spacing: 2) {
            compactRow(tag: tag, title: card.chipTitle, detail: detail,
                       help: card.why.isEmpty ? card.title : card.why, spoken: card.metaLine + ". " + card.title) {
                cardRowAction(card)
                cardDismiss(card)
            }
            if case .failed(let reason) = state {
                Text(reason).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, tagColumn + 10)
            }
            if let error = actions.cardErrors[card.id] {
                Text(error).font(.wisp(size: 11)).foregroundStyle(Theme.bad).padding(.leading, tagColumn + 10)
            }
        }
    }

    /// A row's own button, small: the same fire as its large card's button.
    @ViewBuilder private func compactFire(_ card: ProactiveCard, _ step: ProactiveCard.Action,
                                          _ state: ProactiveActions.Fire?) -> some View {
        switch state {
        case .sending:
            ProgressView().controlSize(.small).accessibilityLabel("Working")
        case .done(let message):
            HStack(spacing: 4) {
                Image(systemName: "checkmark").font(.wisp(size: 9, weight: .bold))
                Text(message).lineLimit(1)
            }
            .font(.wisp(size: 11, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .fixedSize()
        default:
            let undo: Bool = step.method == "undo_add"
            let label: String = isFailed(state) ? "Try again" : undo ? step.undoLabel : buttonLabel(step)
            let help: String = undo ? "Removes exactly what Wisp added, and Wisp won't add it again"
                : (card.why.isEmpty ? card.title : card.why)
            NotchChrome.action(label, help: help, spoken: undo ? "Undo: remove what Wisp added" : label + " " + card.title) {
                Task { await actions.fire(card, today: today); model.onResize() }
            }
        }
    }

    @ViewBuilder private func cardRowAction(_ card: ProactiveCard) -> some View {
        let state = actions.fires[card.id]
        if let step = card.action, !isUnconfirmed(state) {
            if step.sendsDraft && state == nil {
                NotchChrome.action(step.primaryLabel(draftOpen: false),
                                   help: "Shows the draft first; nothing is sent until you tap Send",
                                   spoken: step.primaryLabel(draftOpen: false) + " " + card.title) {
                    withAnimation(Theme.Motion.panel(reduceMotion: reduceMotion)) {
                        actions.heroID = card.id
                    }
                    actions.openDrafts.insert(card.id)
                    model.onResize()
                }
            } else {
                compactFire(card, step, state)
            }
        }
    }

    @ViewBuilder private func cardDismiss(_ card: ProactiveCard) -> some View {
        let state = actions.fires[card.id]
        if actions.dismissing.contains(card.id) {
            ProgressView().controlSize(.mini).frame(width: Theme.iconButtonSize, height: Theme.iconButtonSize)
        } else if state != .sending, !isDone(state) {
            glyphButton("xmark", help: "Dismiss: Wisp won't bring this one back", spoken: "Dismiss " + card.title) {
                Task { await actions.dismiss(card, today: today) }
            }
        }
    }

    /// What Study got ready or a topic to look at again: Open quiz or Review slides.
    private func studyRow(_ card: StudyBriefCard) -> some View {
        let opens: Bool
        if case .open = card.action { opens = true } else { opens = false }
        let tag: String = BriefReady.studyTag(id: card.id, opensMaterial: opens)
        let slides: String = SchoolSlidesText.line(card.slides, pagesText: StudyText.slidePages) ?? ""
        let help: String = slides.isEmpty ? card.text : card.text + "\n" + slides
        let spoken: String = slides.isEmpty ? card.text : card.text + ". Slides: " + slides
        return compactRow(tag: tag, title: card.text, detail: slides, help: help, spoken: spoken) {
            if let action = card.action, let title = card.actionTitle {
                NotchChrome.action(title, help: action.help, spoken: title + ": " + card.text) {
                    SchoolActions.run(action, model: model)
                }
            }
        }
    }

    /// Found in your mail: Add (to Reminders), Open the email, Dismiss.
    private func foundRow(_ item: MailSuggestion) -> some View {
        let detail: String = item.dueLabel() + " · " + item.detail
        let spoken: String = "Found in your mail: \(item.what), \(item.dueLabel()). \(item.detail)"
        return VStack(alignment: .leading, spacing: 2) {
            compactRow(tag: BriefReady.foundTag, title: item.what, detail: detail, help: item.what + "\n" + detail,
                       spoken: spoken) {
                foundActions(item)
            }
            if let failure = mailMemory.failures[item.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, tagColumn + 10)
            }
        }
    }

    @ViewBuilder private func foundActions(_ item: MailSuggestion) -> some View {
        let busy: Bool = mailMemory.answering != nil
        if mailMemory.answering == item.id {
            ProgressView().controlSize(.small).accessibilityLabel("Adding")
        } else {
            NotchChrome.action("Add?", help: "Add this to Reminders, due \(item.dueLabel())",
                               spoken: "Add \(item.what) to Reminders") {
                Task { await mailMemory.accept(item); model.onResize() }
            }
            .disabled(busy)
            if let url = item.mailURL {
                glyphButton("arrow.up.right.square", help: "Open the email in Mail",
                            spoken: "Open the email about \(item.what)") { NSWorkspace.shared.open(url) }
            }
            glyphButton("xmark", help: "Don't suggest this again", spoken: "Dismiss \(item.what)") {
                Task { await mailMemory.dismiss(item); model.onResize() }
            }
            .disabled(busy)
        }
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

    // MARK: The large card

    private func cardView(_ card: ProactiveCard, position: Int, of cards: [ProactiveCard]) -> some View {
        let fire = actions.fires[card.id]
        let draftOpen = actions.openDrafts.contains(card.id)
        let action = card.action
        let hasDraft = !(action?.draft ?? "").isEmpty
        let quote: String? = draftOpen || action?.sendsDraft != true ? nil : BriefReady.draftQuote(action?.draft ?? "")
        return VStack(alignment: .leading, spacing: 6) {
            cardHead(card, position: position, of: cards, fire: fire)
            Text(card.title)
                .font(.wisp(size: 15.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if card.isGroup { groupRows(card) }
            if let quote {
                // The reply Wisp drafted, as it would go: the whole of it opens with the button.
                draftQuote(quote, full: action?.draft ?? "")
            } else if !card.body.isEmpty {
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
                } else if hasDraft && !isDone(fire) && !isUnconfirmed(fire) {
                    // A reply's draft opens in its editor; Send stays inside it.
                    secondary("Edit") {
                        actions.openDrafts.insert(card.id)
                        model.onResize()
                    }
                    .disabled(fire == .sending)
                    .help("Open the draft to change it before you send it")
                }
                Spacer(minLength: 4)
                if action?.sendsDraft == true && !isDone(fire) { sendsOnTap }
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
        .padding(EdgeInsets(top: 9, leading: 13, bottom: 10, trailing: 9))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.cardFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(card.metaLine). \(card.title)")
    }

    /// The card's top line: what Wisp did (DRAFTED…), when and where from,
    /// then "1 of 3 ›" and Dismiss.
    private func cardHead(_ card: ProactiveCard, position: Int, of cards: [ProactiveCard],
                          fire: ProactiveActions.Fire?) -> some View {
        let tag: String = BriefReady.cardTag(method: card.action?.method ?? "", hasDraft: !(card.action?.draft ?? "").isEmpty)
        return HStack(alignment: .center, spacing: 8) {
            readyTag(tag)
            Text(card.metaLine)
                .font(.wisp(size: 10, weight: .medium, design: .monospaced)).tracking(0.6)
                .foregroundStyle(card.wispDidThis ? Theme.textSecondary : Theme.textMuted).lineLimit(1)
                .help(card.why)
            Spacer(minLength: 4)
            if cards.count > 1 { pager(position: position, of: cards) }
            if card.isGroup {
                // A group has no Dismiss: each row has its own Undo, and it leaves with its rows.
                EmptyView()
            } else if actions.dismissing.contains(card.id) {
                ProgressView().controlSize(.mini)
            } else if fire != .sending, !isDone(fire) {
                glyphButton("xmark", help: "Dismiss: Wisp won't bring this one back", spoken: "Dismiss") {
                    Task { await actions.dismiss(card, today: today) }
                }
            }
        }
    }

    /// "1 of 3 ›": the next card, large.
    private func pager(position: Int, of cards: [ProactiveCard]) -> some View {
        Button {
            withAnimation(Theme.Motion.panel(reduceMotion: reduceMotion)) {
                actions.heroID = cards[(position + 1) % cards.count].id
            }
            model.onResize()
        } label: {
            HStack(spacing: 3) {
                Text("\(position + 1) of \(cards.count)").monospacedDigit()
                Image(systemName: "chevron.right").font(.wisp(size: 8.5, weight: .bold))
            }
            .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textMuted)
            .frame(minHeight: Theme.iconButtonSize)
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help("Next card")
        .accessibilityLabel("Card \(position + 1) of \(cards.count); next")
    }

    /// The drafted reply in one quoted line; the whole draft is in its tooltip.
    private func draftQuote(_ quote: String, full: String) -> some View {
        HStack(alignment: .top, spacing: 9) {
            RoundedRectangle(cornerRadius: 1).fill(Theme.chipStroke).frame(width: 2).accessibilityHidden(true)
            Text(quote).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary).lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .fixedSize(horizontal: false, vertical: true)
        .help(full)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Drafted reply: " + quote)
    }

    /// "🔒 Sends only when you tap", beside a reply's button.
    private var sendsOnTap: some View {
        // Never cut: the whole line, else the lock alone (the words are its tooltip and label).
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 4) {
                Image(systemName: "lock").font(.wisp(size: 9.5, weight: .semibold)).accessibilityHidden(true)
                Text(BriefReady.sendsOnTap).lineLimit(1)
            }
            .fixedSize()
            Image(systemName: "lock").font(.wisp(size: 9.5, weight: .semibold))
                .accessibilityLabel(BriefReady.sendsOnTap)
        }
        .font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
        .help("Wisp never sends a reply by itself. The draft shows first, and it goes only when you tap Send.")
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
                Text("and \(more) more in the log")
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

    // MARK: Your lists

    /// The right zone (second, stacked): YOUR LISTS, the calendar line with
    /// Day ›, then TO-DOS, SCHOOL and NEEDS ATTENTION as short lists.
    private func listsZone(_ shown: ProactiveState) -> some View {
        let todos: [ProactiveTodo] = listTodos(shown)
        let threads: [ProactiveThread] = listedThreads(shown, cards: liveCards(shown))
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                zoneTitle("YOUR LISTS", spoken: "Your lists", count: 0, lit: false).fixedSize()
                calendarLine(todosEmpty: todos.isEmpty ? shown.todoLine(unread: unread) : nil)
            }
            .frame(minHeight: Self.controlsHeaderHeight)
            listColumns(todos: todos, threads: threads)
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Your lists")
    }

    /// One column; stacked at the default and Large sizes, to-dos and school
    /// beside mail and texts until a thread is opened.
    @ViewBuilder private func listColumns(todos: [ProactiveTodo], threads: [ProactiveThread]) -> some View {
        // An opened thread (its actions, a reply being written) takes the full width.
        let opened: Bool = threads.contains { actions.expanded.contains($0.id) }
        if listsSplit && !opened && !threads.isEmpty && !(todos.isEmpty && schoolRows.items.isEmpty) {
            BriefZones(sideBySide: true, gap: 22, stackGap: 0) {
                VStack(alignment: .leading, spacing: 12) {
                    todoList(todos)
                    schoolList
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
                Rectangle().fill(Theme.hairline).accessibilityHidden(true)
                attention(threads)
            }
        } else {
            VStack(alignment: .leading, spacing: 12) {
                todoList(todos)
                schoolList
                attention(threads)
            }
        }
    }

    /// Open to-dos, most urgent first, none just cleared and none that only
    /// restates an event the calendar line names (ProactiveState.plate).
    private func listTodos(_ shown: ProactiveState) -> [ProactiveTodo] {
        let todos = shown.todos.filter { !itemActions.isHidden($0.actionRow) }
        let bounds = today.dayInterval
        let tomorrow = schoolCalendar.date(byAdding: .day, value: 1, to: bounds.end) ?? bounds.end.addingTimeInterval(86_400)
        let items: [PlateItem] = ProactiveState.plate(todos: todos, school: [], isDone: { actions.isDone($0) },
                                                      now: today.now.timeIntervalSince1970,
                                                      dayStart: bounds.start.timeIntervalSince1970,
                                                      todayEnd: bounds.end.timeIntervalSince1970,
                                                      tomorrowEnd: tomorrow.timeIntervalSince1970, named: calendarNamed)
        return items.compactMap { item -> ProactiveTodo? in
            if case .todo(let todo) = item { return todo }
            return nil
        }
    }

    /// The events the calendar line names (BriefPlate.calendarLine's first
    /// three), which the list then leaves out.
    private var calendarNamed: [String] {
        Array((allDayEvents + events.map(\.title)).prefix(3))
    }

    /// "🕘 11:45 AM Hack Day · 12 PM Hacktoberfest   Day ›", today's all-day
    /// events first; or that nothing is left on today, or nothing was on it;
    /// with no to-dos either, that goes on the same line. Opens Day.
    private func calendarLine(todosEmpty: String?) -> some View {
        let calendarUnread = unread.contains { $0.source == "calendar" }
        let upcoming: [String] = events.map { today.clock($0.start) + " " + $0.title }
        let dayLine: String = BriefPlate.calendarLine(upcoming: upcoming, allDay: allDayEvents,
                                                      earlierToday: endedEvents, calendarUnread: calendarUnread)
        let quiet: Bool = events.isEmpty && allDayEvents.isEmpty
        let line: String = todosEmpty.map { dayLine + " · " + $0 } ?? dayLine
        return Button { model.tab = .day } label: {
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Image(systemName: "clock").font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .frame(width: 17).accessibilityHidden(true)
                Text(line).font(.wisp(size: 13))
                    .foregroundStyle(quiet ? Theme.textMuted : Theme.textSecondary)
                    .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                linkLabel("Day")
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help("The rest of today, hour by hour (⌘3)")
        .accessibilityLabel(line)
        .accessibilityHint("Opens Day")
    }

    /// "Day ›", "Study ›", "All 6 ›".
    private func linkLabel(_ text: String) -> some View {
        HStack(spacing: 2) {
            Text(text)
            Image(systemName: "chevron.right").font(.wisp(size: 8, weight: .bold))
        }
        .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
        .lineLimit(1).fixedSize()
    }

    /// A list's heading ("TO-DOS · 6") and its link on the right.
    private func listHeading(_ title: String, spoken: String, link: String?, help: String,
                             action: @escaping () -> Void) -> some View {
        HStack(alignment: .center, spacing: 6) {
            Text(title).font(.wisp(size: 10, design: .monospaced)).tracking(0.4).foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .accessibilityLabel(spoken)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 6)
            if let link {
                Button(action: action) {
                    linkLabel(link).frame(minHeight: Theme.iconButtonSize).contentShape(Rectangle())
                }
                .buttonStyle(StudyRowButtonStyle())
                .help(help)
            }
        }
    }

    /// TO-DOS · n, the first few (BriefLists.todosShown), All n › for the rest.
    @ViewBuilder private func todoList(_ todos: [ProactiveTodo]) -> some View {
        if !todos.isEmpty {
            let limit: Int = BriefLists.todosShown(sideBySide: sideBySide)
            let visible: [ProactiveTodo] = actions.showAllTodos ? todos : Array(todos.prefix(limit))
            let open: Int = todos.filter { !actions.isDone($0) }.count
            let link: String? = todos.count > limit ? (actions.showAllTodos ? "Fewer" : "All \(todos.count)") : nil
            VStack(alignment: .leading, spacing: 1) {
                listHeading(BriefPlate.heading("TO-DOS", count: open), spoken: BriefPlate.heading("To-dos", count: open),
                            link: link, help: actions.showAllTodos ? "Show the first few" : "Show every to-do") {
                    actions.showAllTodos.toggle()
                    model.onResize()
                }
                ForEach(visible) { todo in todoRow(todo) }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("To-dos")
        }
    }

    /// SCHOOL · n DUE TODAY with Study ›: one line per piece of school work.
    @ViewBuilder private var schoolList: some View {
        if !schoolRows.items.isEmpty {
            let limit: Int = BriefLists.schoolShown(sideBySide: sideBySide)
            let visible: [StudyDaySummary.Due] = Array(schoolRows.items.prefix(limit))
            let bounds = today.dayInterval
            let dueToday: Int = BriefLists.dueToday(schoolRows.items.map(\.dueTS),
                                                    dayStart: bounds.start.timeIntervalSince1970,
                                                    dayEnd: bounds.end.timeIntervalSince1970)
            let total: Int = schoolRows.items.count + schoolRows.more
            VStack(alignment: .leading, spacing: 1) {
                listHeading(BriefLists.schoolHeading(dueToday: dueToday, total: total),
                            spoken: BriefLists.schoolHeading(dueToday: dueToday, total: total).capitalized,
                            link: "Study", help: "Your school work (⌘5)") { model.tab = .study }
                ForEach(Array(visible.enumerated()), id: \.offset) { _, item in schoolRow(item) }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("School work")
        }
    }

    /// Due times in the day's own time zone, as the rest of the Brief.
    private var schoolCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        return calendar
    }

    /// One piece of school work on one line: its title, the course, and when
    /// it is due at the right. The whole row opens Study, where it is done.
    private func schoolRow(_ item: StudyDaySummary.Due) -> some View {
        let calendar = schoolCalendar
        let assignment = item.assignment
        let due: String = StudyText.dueShort(assignment, now: today.now, calendar: calendar)
        let spoken: String = ["School: " + assignment.title, item.course, "due " + due].filter { !$0.isEmpty }.joined(separator: ", ")
        return Button { model.tab = .study } label: {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(assignment.title).font(.wisp(size: 13.5)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).layoutPriority(1)
                if !item.course.isEmpty { subText(item.course).lineLimit(1) }
                Spacer(minLength: 6)
                Text(due).font(.wisp(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                    .lineLimit(1).fixedSize()
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .help(assignment.title + (item.course.isEmpty ? "" : " · " + item.course) + " · " + due + "\nOpen it in Study (⌘5)")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
        .accessibilityHint("Opens Study")
    }

    // MARK: To-do

    /// One to-do on one line: the check, its title, when it is due and who
    /// it is from (muted), then Add to Day, Mark done and Clear as icons
    /// that keep their places from row to row.
    private func todoRow(_ todo: ProactiveTodo) -> some View {
        let done = actions.isDone(todo)
        let busy = actions.todoBusy.contains(todo.id)
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .center, spacing: 8) {
                checkButton(todo, done: done, busy: busy)
                todoLine(todo, done: done)
                todoIcons(todo, done: done, busy: busy)
            }
            .frame(minHeight: Theme.iconButtonSize)
            if let error = actions.todoErrors[todo.id] {
                Text(error).font(.wisp(size: 11)).foregroundStyle(Theme.bad).padding(.leading, Self.listInset)
            }
            if let failure = itemActions.failures[todo.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, Self.listInset)
            }
        }
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

    /// The title, then the due word ("Tmrw", "Overdue") and who it is from,
    /// muted; the title keeps its room longest. Everything is in the tooltip.
    private func todoLine(_ todo: ProactiveTodo, done: Bool) -> some View {
        let due: String = done ? "" : BriefLists.dueWord(todo.dueLabel)
        let from: String = done ? "" : todoSub(todo)
        let overdue: Bool = todo.dueLabel.lowercased().contains("overdue")
        let help: String = [todo.title, todo.dueLabel, todoSub(todo)].filter { !$0.isEmpty }.joined(separator: "\n")
        return HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(todoTitle(todo)).font(.wisp(size: 13.5))
                .foregroundStyle(done ? Theme.textMuted : Theme.textPrimary)
                .strikethrough(done, color: Theme.textMuted)
                .lineLimit(1).layoutPriority(1)
            if !due.isEmpty {
                Text(due).font(.wisp(size: 11.5)).foregroundStyle(overdue ? Theme.warn : Theme.textMuted)
                    .lineLimit(1).fixedSize()
            }
            if !from.isEmpty { subText(from).lineLimit(1) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(help)
    }

    /// A row's muted second part: who a to-do is from, a school row's course and time.
    private func subText(_ text: String) -> Text {
        Text(text).font(.wisp(size: 11)).foregroundColor(Theme.textMuted)
    }

    /// Add to Day, then Mark done and Clear (NeedsYouRowControls), each in
    /// its own 24 pt place so the icons line up down the list.
    @ViewBuilder private func todoIcons(_ todo: ProactiveTodo, done: Bool, busy: Bool) -> some View {
        if busy {
            ProgressView().controlSize(.small)
        } else {
            HStack(spacing: 0) {
                addToDayIcon(todo, done: done)
                rowControls(todo.actionRow, slots: true)
            }
            .fixedSize()
        }
    }

    @ViewBuilder private func addToDayIcon(_ todo: ProactiveTodo, done: Bool) -> some View {
        if !todo.canAddToDay || done {
            Color.clear.frame(width: Theme.iconButtonSize, height: Theme.iconButtonSize).accessibilityHidden(true)
        } else if actions.addedToDay.contains(todo.id) {
            Image(systemName: "calendar.badge.checkmark").font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                .frame(width: Theme.iconButtonSize, height: Theme.iconButtonSize)
                .help("On your Day")
                .accessibilityLabel("On your Day: " + todo.title)
        } else {
            glyphButton("calendar.badge.plus", help: "Add to Day: fit it into today's plan",
                        spoken: "Add to Day: " + todo.title) {
                Task { await actions.addToDay(todo, today: today) }
            }
        }
    }

    /// A row's trailing ✓ and ×: Mark done for a dated reminder, and Clear.
    private func rowControls(_ row: NeedsYouRow, slots: Bool = false) -> some View {
        NeedsYouRowControls(row: row, actions: itemActions, onResize: { model.onResize() },
                            onConfirmed: { await today.refresh(afterMutation: true) },
                            hitSize: Theme.iconButtonSize, keepsSlots: slots)
    }

    // MARK: Mail and texts

    /// NEEDS ATTENTION: what mail and texts need from you, replies first,
    /// one line each until opened; the first few, then All n ›.
    @ViewBuilder private func attention(_ threads: [ProactiveThread]) -> some View {
        if !threads.isEmpty {
            let limit: Int = BriefLists.threadsShown
            let visible: [ProactiveThread] = actions.showAllThreads ? threads : Array(threads.prefix(limit))
            let replies: Int = threads.filter(\.needsReply).count
            let link: String? = threads.count > limit ? (actions.showAllThreads ? "Fewer" : "All \(threads.count)") : nil
            VStack(alignment: .leading, spacing: 1) {
                listHeading(BriefLists.attentionHeading(replies: replies, total: threads.count),
                            spoken: BriefLists.attentionHeading(replies: replies, total: threads.count).capitalized,
                            link: link, help: actions.showAllThreads ? "Show the first few" : "Show all mail and texts") {
                    actions.showAllThreads.toggle()
                    model.onResize()
                }
                ForEach(visible) { thread in threadRow(thread) }
            }
            .frame(maxWidth: .infinity, alignment: .topLeading)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Mail and texts")
        }
    }

    /// Mail and texts as listed: none just cleared, none a reply card holds.
    private func listedThreads(_ shown: ProactiveState, cards: [ProactiveCard]) -> [ProactiveThread] {
        let open = shown.orderedThreads.filter { !itemActions.isHidden($0.actionRow) }
        return ProactiveState.threads(open, coveredBy: cards)
    }

    private func threadRow(_ thread: ProactiveThread) -> some View {
        let open = actions.expanded.contains(thread.id)
        let hot = thread.needsReply
        let hovered = hoveredThread == thread.id
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 2) {
                Button {
                    if open { actions.expanded.remove(thread.id) } else { actions.expanded.insert(thread.id) }
                    model.onResize()
                } label: {
                    threadLabel(thread, open: open)
                        .padding(.vertical, 4).padding(.leading, 6).padding(.trailing, 4)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(hovered || open ? 0.05 : 0)))
                        .contentShape(Rectangle())
                }
                .buttonStyle(StudyRowButtonStyle())
                .onHover { inside in hoveredThread = inside ? thread.id : (hoveredThread == thread.id ? nil : hoveredThread) }
                .help(open ? "Hide the actions" : (hot ? "Reply, open or ask about this" : "Open or ask about this"))
                .accessibilityValue(open ? "Expanded" : "Collapsed")
                // Beside the row, not in it: a button inside the row's button
                // would open the thread instead.
                rowControls(thread.actionRow)
            }
            if let failure = itemActions.failures[thread.id] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 30)
            }
            if open { threadDetail(thread) }
        }
    }

    /// One line until opened; opened, the whole summary under the sender.
    @ViewBuilder private func threadLabel(_ thread: ProactiveThread, open: Bool) -> some View {
        if open {
            threadLines(thread)
        } else {
            threadLine(thread)
        }
    }

    /// Who, what it's about, REPLY or FYI and the time, on one line.
    private func threadLine(_ thread: ProactiveThread) -> some View {
        let hot = thread.needsReply
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: DayTimeline.sourceSymbol(thread.source))
                .font(.wisp(size: 11)).foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted).frame(width: 15)
            Text(thread.shownWho.isEmpty ? "Someone" : thread.shownWho).font(.wisp(size: 13, weight: .semibold))
                .foregroundStyle(hot ? Theme.textPrimary : Theme.textSecondary).lineLimit(1).layoutPriority(1)
            Text(thread.line).font(.wisp(size: 13)).foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted).lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            threadBadge(hot)
            Text(timeLabel(thread.receivedTs)).font(.wisp(size: 10, design: .monospaced))
                .foregroundStyle(Theme.textMuted).lineLimit(1).fixedSize()
        }
        .help((thread.shownWho.isEmpty ? "" : thread.shownWho + ": ") + thread.line)
        .accessibilityElement(children: .combine)
    }

    private func threadLines(_ thread: ProactiveThread) -> some View {
        let hot = thread.needsReply
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: DayTimeline.sourceSymbol(thread.source))
                .font(.wisp(size: 11)).foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted)
                .frame(width: 15).padding(.top, 2)
            VStack(alignment: .leading, spacing: 1) {
                Text(thread.shownWho.isEmpty ? "Someone" : thread.shownWho).font(.wisp(size: 13, weight: .semibold))
                    .foregroundStyle(hot ? Theme.textPrimary : Theme.textSecondary).lineLimit(1)
                Text(thread.line).font(.wisp(size: 13))
                    .foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted)
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
        Text(hot ? "REPLY" : "FYI").font(.wisp(size: 9, weight: .semibold, design: .monospaced))
            .foregroundStyle(hot ? Theme.textPrimary : Theme.textMuted)
            .padding(.horizontal, 5).padding(.vertical, 2)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(hot ? Color.white.opacity(0.45) : Theme.chipStroke, lineWidth: 1))
            .fixedSize()
            .accessibilityLabel(hot ? "Needs a reply" : "For your information")
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
