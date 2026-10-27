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
@MainActor
enum BriefReplyHook {
    static var composer: ((ProactiveThread, _ close: @escaping () -> Void) -> AnyView)? = { thread, close in
        AnyView(BriefReplyComposer(replyRef: thread.replyRef, account: thread.replyAccount,
                                   onSent: {}, onDismiss: close))
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
            let outcome = try await post(today, "/assistant/proactive/cards/\(Self.segment(card.id))/fire", body: body)
            guard outcome.ok else {
                fires[card.id] = .failed(outcome.message.isEmpty ? "That didn't go through. Try again." : outcome.message)
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
            fires[card.id] = .failed(error.localizedDescription)
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
                 hero: String? = nil, openRow: String? = nil) {
        self.account = account
        heroID = hero
        self.openRow = openRow
        openDrafts = openDraft.map { [$0] } ?? []
        self.expanded = expanded
        self.replying = replying
    }
    #endif
}

// MARK: - The Brief's proactive section

/// The Brief under its one sentence: the work Wisp did, the top "Before you
/// ask" card with the rest as chips, then three one-line rows (the rest of the
/// day, the to-dos, mail and texts) that open in place.
struct ProactiveBriefSection: View {
    let proactive: ProactiveState
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    @ObservedObject var actions: ProactiveActions = .shared
    /// Clear (and Mark done for a dated reminder) behind each to-do's and
    /// thread's trailing ×; shared with the Top 3's Needs you rows.
    @ObservedObject var itemActions: NeedsYouActionsModel = NotchBriefView.needsYouActions
    /// The rest of today's calendar, for the clock row.
    var events: [(start: Double, title: String)] = []
    /// The tallest an opened row gets before it scrolls, so the tab fits the
    /// notch; nil draws everything (the QA driver's full render).
    var scrollCap: CGFloat? = 300
    /// HOOK(W3): told when Reply is tapped on a thread.
    var onReply: ((ProactiveThread) -> Void)? = BriefReplyHook.onReply
    /// Which sources Wisp could read (the workspace's source_coverage), so no
    /// row gives an all-clear for one it couldn't.
    var coverage: [SourceAccess] = []

    private var unread: [SourceAccess] { proactive.unreadSources(coverage) }

    private static let todoLimit = 6
    @State private var hoveredThread: String? = nil

    private var chosen: String? {
        actions.account.flatMap { picked in proactive.accounts.contains(picked) ? picked : nil }
    }

    var body: some View {
        let shown = proactive.only(chosen)
        VStack(alignment: .leading, spacing: 12) {
            if !unread.isEmpty { accessRows(unread) }
            if let work = proactive.workLine {
                HStack(spacing: 6) {
                    Image(systemName: "sparkles").font(.system(size: 10)).foregroundStyle(Theme.textSecondary)
                    Text(work).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                .padding(.top, -4)
                .accessibilityElement(children: .combine)
            }
            beforeYouAsk(shown)
            attention(shown)
            VStack(alignment: .leading, spacing: 0) {
                Rectangle().fill(Theme.hairline).frame(height: 1)
                dayRow
                Rectangle().fill(Theme.hairline).frame(height: 1)
                expandingRow("todos", icon: "checklist", text: shown.todoLine(unread: unread),
                             empty: shown.todos.isEmpty) { todoList(shown) }
            }
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

    // MARK: Sources Wisp couldn't read

    /// One quiet row per source Wisp couldn't read: what's wrong, and the
    /// setting that fixes it (Full Disk Access for texts, Calendars, …).
    private func accessRows(_ rows: [SourceAccess]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(rows, id: \.source) { access in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: DayTimeline.sourceSymbol(access.source)).font(.system(size: 10.5))
                        .foregroundStyle(Theme.textMuted).frame(width: 15)
                    Text(access.headline).font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .help(access.detail)
                    Spacer(minLength: 6)
                    if let fix = access.fix, let url = URL(string: fix.url) {
                        NotchChrome.small(Permissions.fixLabel(fix)) { Permissions.openFix(url) }
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
                    Image(systemName: "chevron.down").font(.system(size: 7, weight: .bold))
                }
                .font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Capsule().fill(chosen == nil ? Theme.chipFill : Theme.chipOn))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .help("Show one account's cards, to-dos and mail")
            .accessibilityLabel("Account: \(chosen ?? "all")")
        }
    }

    // MARK: Before you ask

    private func header(_ title: String, trailing: String = "") -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            if !trailing.isEmpty {
                Text(trailing).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted.opacity(0.8))
            }
        }
    }

    @ViewBuilder private func beforeYouAsk(_ shown: ProactiveState) -> some View {
        let cards = shown.cards.filter { !actions.hidden.contains($0.id) }
        let index = cards.firstIndex { $0.id == actions.heroID } ?? 0
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 6) {
                header("BEFORE YOU ASK")
                Spacer()
                accountMenu
                analyzeControl
            }
            if proactive.isWorking || actions.analyzePending() { progressRow }
            if !actions.analyzeError.isEmpty {
                Text(actions.analyzeError).font(.system(size: 11)).foregroundStyle(Theme.bad)
            }
            if cards.isEmpty {
                Text(emptyCardsLine).font(.system(size: 12.5)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                cardView(cards[index], position: index, of: cards)
                    .id(cards[index].id)
                    .transition(.opacity)
                let rest = cards.enumerated().filter { $0.offset != index }.map(\.element)
                if !rest.isEmpty { otherCards(Array(rest.prefix(3))) }
            }
        }
    }

    /// The other cards, by title; one tap makes it the one shown.
    private func otherCards(_ cards: [ProactiveCard]) -> some View {
        HStack(spacing: 6) {
            ForEach(cards) { card in
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { actions.heroID = card.id }
                    model.onResize()
                } label: {
                    Text(card.chipTitle).font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
                        .lineLimit(1).truncationMode(.tail)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .help(card.title)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("More things Wisp caught")
    }

    private var emptyCardsLine: String {
        if proactive.isWorking { return "Cards land here as Wisp reads." }
        if proactive.isUnavailable || proactive.hasReadNothing {
            let reason = proactive.reason.trimmingCharacters(in: .whitespacesAndNewlines)
            return reason.isEmpty ? "Not caught up yet. Tap ✦ to read your mail and texts." : reason
        }
        if !unread.isEmpty { return "Nothing to get ahead of in what Wisp could read." }
        return "Nothing to get ahead of right now."
    }

    private var analyzeControl: some View {
        let busy = proactive.isWorking || actions.analyzePending()
        return Button {
            Task { await actions.analyze(today: today, account: chosen); model.onResize() }
        } label: {
            Image(systemName: busy ? "ellipsis" : "sparkles").font(.system(size: 10, weight: .semibold))
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
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary).monospacedDigit()
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
                Text(card.metaLine)
                    .font(.system(size: 10, weight: .medium, design: .monospaced)).tracking(0.6)
                    .foregroundStyle(Theme.textMuted).lineLimit(1)
                    .help(card.why)
                Spacer(minLength: 4)
                if actions.dismissing.contains(card.id) {
                    ProgressView().controlSize(.mini)
                } else if fire != .sending, !isDone(fire) {
                    Button { Task { await actions.dismiss(card, today: today) } } label: {
                        Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                            .frame(width: 16, height: 16).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Dismiss: Wisp won't bring this one back")
                    .accessibilityLabel("Dismiss")
                }
            }
            Text(card.title)
                .font(.system(size: 15.5, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if !card.body.isEmpty {
                Text(card.body)
                    .font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                    .lineSpacing(1.5).lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // A reply's Send sits inside its shown draft; the card's own button
            // only opens the draft (ProactiveCard.Action.primaryStep).
            let sendInDraft = draftOpen && action?.sendsDraft == true
            if draftOpen, let action { draftEditor(card, action, fire: fire, withSend: sendInDraft) }
            HStack(alignment: .center, spacing: 12) {
                if let action, !sendInDraft { fireButton(card, action, fire) }
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
                            Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .bold))
                        }
                        .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textMuted)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Next card")
                    .accessibilityLabel("Card \(position + 1) of \(cards.count); next")
                }
            }
            .padding(.top, 3)
            if case .failed(let reason) = fire {
                Text(reason).font(.system(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = actions.cardErrors[card.id] {
                Text(error).font(.system(size: 11)).foregroundStyle(Theme.bad)
            }
        }
        .padding(EdgeInsets(top: 11, leading: 13, bottom: 11, trailing: 11))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.cardFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(card.metaLine). \(card.title)")
    }

    private func secondary(_ title: String, chevron: String? = nil, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Text(title)
                if let chevron { Image(systemName: chevron).font(.system(size: 8.5, weight: .bold)) }
            }
            .font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
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
                Text(action.sendsDraft ? "Sending…" : "Working…").font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.textSecondary)
            }
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(Capsule().fill(Theme.chipFill))
        case .done(let message):
            HStack(spacing: 5) {
                Image(systemName: "checkmark").font(.system(size: 10.5, weight: .bold))
                Text(message).font(.system(size: 12.5, weight: .medium)).lineLimit(1)
            }
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(Capsule().fill(Theme.chipOn))
        default:
            let draftOpen = actions.openDrafts.contains(card.id)
            Button { Task { await actions.fire(card, today: today); model.onResize() } } label: {
                Text(isFailed(fire) && (draftOpen || !action.sendsDraft) ? "Try again"
                     : action.sendsDraft ? action.primaryLabel(draftOpen: draftOpen) : buttonLabel(action))
                    .font(.system(size: 12.5, weight: .semibold)).foregroundStyle(Color.black)
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

    private func isFailed(_ fire: ProactiveActions.Fire?) -> Bool {
        if case .failed = fire { return true }
        return false
    }

    private func draftEditor(_ card: ProactiveCard, _ action: ProactiveCard.Action, fire: ProactiveActions.Fire? = nil,
                             withSend: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            if let source = card.sources.first(where: { !$0.quote.isEmpty }) {
                Text((source.who.isEmpty ? "" : source.who + ": ") + "“" + source.quote + "”")
                    .font(.system(size: 11.5)).italic().foregroundStyle(Theme.textMuted)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            if !action.recipient.isEmpty {
                Text("TO  " + action.recipient).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    .lineLimit(1)
            }
            TextEditor(text: Binding(get: { actions.draft(card) }, set: { actions.drafts[card.id] = $0 }))
                .font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                .scrollContentBackground(.hidden)
                .padding(.horizontal, 5).padding(.vertical, 6)
                .frame(minHeight: 64, idealHeight: draftHeight(actions.draft(card)), maxHeight: 150)
                .fixedSize(horizontal: false, vertical: true)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.editorFill))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke, lineWidth: 1))
                .disabled(actions.fires[card.id] == .sending)
                .accessibilityLabel("Draft; edit before sending")
            if actions.drafts[card.id].map({ $0 != action.draft }) == true {
                Text("Edited — Wisp sends your version.").font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
            }
            if withSend {
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

    // MARK: The three rows

    /// "11:45 AM Hack Day · 12 PM Hacktoberfest", then Day ›.
    private var dayRow: some View {
        let line = events.isEmpty ? ProactiveState.emptyDayLine(calendarUnread: unread.contains { $0.source == "calendar" })
            : events.prefix(3).map { today.clock($0.start) + " " + $0.title }.joined(separator: " · ")
        return Button { model.tab = .day } label: {
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Image(systemName: "clock").font(.system(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
                Text(line).font(.system(size: 13)).foregroundStyle(events.isEmpty ? Theme.textMuted : Theme.textPrimary)
                    .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: 2) {
                    Text("Day")
                    Image(systemName: "chevron.right").font(.system(size: 8, weight: .bold))
                }
                .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary).fixedSize()
            }
            .padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("The rest of today, hour by hour (⌘3)")
    }

    /// A one-line summary that opens to its list in place; one open at a time.
    private func expandingRow<Content: View>(_ id: String, icon: String, text: String, empty: Bool,
                                             @ViewBuilder content: () -> Content) -> some View {
        let open = actions.openRow == id && !empty
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                guard !empty else { return }
                withAnimation(.easeOut(duration: 0.15)) { actions.openRow = open ? nil : id }
                model.onResize()
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 9) {
                    Image(systemName: icon).font(.system(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
                    Text(text).font(.system(size: 13)).foregroundStyle(empty ? Theme.textMuted : Theme.textPrimary)
                        .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                    if !empty {
                        Image(systemName: open ? "chevron.up" : "chevron.down")
                            .font(.system(size: 8.5, weight: .bold)).foregroundStyle(Theme.textMuted)
                    }
                }
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                CappedScroll(cap: scrollCap) { content().frame(maxWidth: .infinity, alignment: .leading) }
                    .padding(.bottom, 8)
            }
        }
    }

    // MARK: To-do

    @ViewBuilder private func todoList(_ shown: ProactiveState) -> some View {
        // A row the service just confirmed cleared or done leaves at once.
        let todos = shown.orderedTodos.filter { !itemActions.isHidden($0.actionRow) }
        let visible = actions.showAllTodos ? todos : Array(todos.prefix(Self.todoLimit))
        VStack(alignment: .leading, spacing: 2) {
            ForEach(visible) { todo in todoRow(todo) }
            if todos.count > Self.todoLimit {
                Button(actions.showAllTodos ? "Show fewer" : "Show all \(todos.count)") {
                    actions.showAllTodos.toggle()
                    model.onResize()
                }
                .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.leading, 26).padding(.top, 2)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("To-do")
    }

    private func todoRow(_ todo: ProactiveTodo) -> some View {
        let done = actions.isDone(todo)
        let busy = actions.todoBusy.contains(todo.id)
        let sub = [todo.who, todo.quote.isEmpty ? "" : "“" + todo.quote + "”"].filter { !$0.isEmpty }.joined(separator: " · ")
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .center, spacing: 9) {
                Button { Task { await actions.toggleDone(todo, today: today) } } label: {
                    Image(systemName: done ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 15, weight: .light))
                        .foregroundStyle(done ? Theme.textSecondary : Theme.textPrimary.opacity(0.85))
                        .frame(width: 18, height: 18).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .help(done ? "Mark as not done" : "Check it off")
                .accessibilityLabel(done ? "Done: \(todo.title)" : "Check off \(todo.title)")
                Image(systemName: DayTimeline.sourceSymbol(todo.source))
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    .frame(width: 15)
                    .help(DayTimeline.sourceName(todo.source))
                VStack(alignment: .leading, spacing: 1) {
                    Text(todo.title).font(.system(size: 13.5))
                        .foregroundStyle(done ? Theme.textMuted : Theme.textPrimary)
                        .strikethrough(done, color: Theme.textMuted)
                        .lineLimit(1)
                    if !sub.isEmpty && !done {
                        Text(sub).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
                    }
                }
                Spacer(minLength: 6)
                if !todo.dueLabel.isEmpty && !done {
                    Text(todo.dueLabel.uppercased())
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(todo.dueLabel.lowercased().contains("overdue") ? Theme.textPrimary : Theme.textSecondary)
                        .lineLimit(1).fixedSize()
                }
                if busy {
                    ProgressView().controlSize(.small)
                } else if todo.canAddToDay && !done {
                    if actions.addedToDay.contains(todo.id) {
                        Text("ON DAY ✓").font(.system(size: 9.5, design: .monospaced)).foregroundStyle(Theme.textMuted).fixedSize()
                    } else {
                        NotchChrome.small("Add to Day") { Task { await actions.addToDay(todo, today: today) } }
                            .help("Fit it into today's plan")
                    }
                }
                if !busy { rowControls(todo.actionRow) }
            }
            .padding(.vertical, 3)
            if let error = actions.todoErrors[todo.id] {
                Text(error).font(.system(size: 11)).foregroundStyle(Theme.bad).padding(.leading, 51)
            }
            if let failure = itemActions.failures[todo.id] {
                Text(failure).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 51)
            }
        }
        .accessibilityElement(children: .contain)
    }

    /// A row's trailing ×: Clear, and Mark done for a dated reminder.
    private func rowControls(_ row: NeedsYouRow) -> some View {
        NeedsYouRowControls(row: row, actions: itemActions, onResize: { model.onResize() },
                            onConfirmed: { await today.refresh(afterMutation: true) })
    }

    // MARK: Summaries

    // MARK: Needs attention · mail & texts

    private static let attentionLimit = 4

    /// What mail and texts need from you, always open: who and what, two lines
    /// each, replies first and brighter.
    @ViewBuilder private func attention(_ shown: ProactiveState) -> some View {
        let threads = shown.orderedThreads.filter { !itemActions.isHidden($0.actionRow) }
        let waiting = threads.filter(\.needsReply).count
        let visible = actions.showAllThreads ? threads : Array(threads.prefix(Self.attentionLimit))
        VStack(alignment: .leading, spacing: 2) {
            header("NEEDS ATTENTION · MAIL & TEXTS",
                   trailing: waiting == 0 ? "" : "· \(waiting) need\(waiting == 1 ? "s" : "") a reply")
                .padding(.bottom, 4)
            if threads.isEmpty {
                // An all-clear only when both were read; the rows above say why not.
                if let line = ProactiveState.attentionEmptyLine(unread: unread) {
                    Text(line).font(.system(size: 12.5)).foregroundStyle(Theme.textMuted)
                } else {
                    Text("No mail or texts to show from what Wisp could read.")
                        .font(.system(size: 12.5)).foregroundStyle(Theme.textMuted)
                }
            } else {
                ForEach(visible) { thread in threadRow(thread) }
                if threads.count > Self.attentionLimit {
                    Button(actions.showAllThreads ? "Show fewer"
                           : "+\(threads.count - Self.attentionLimit) more · Show all") {
                        actions.showAllThreads.toggle()
                        model.onResize()
                    }
                    .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.leading, 26).padding(.top, 3)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Needs attention, mail and texts")
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
                    HStack(alignment: .top, spacing: 9) {
                        Image(systemName: DayTimeline.sourceSymbol(thread.source))
                            .font(.system(size: 11)).foregroundStyle(hot ? Theme.textSecondary : Theme.textMuted)
                            .frame(width: 15).padding(.top, 2)
                        (Text(thread.who.isEmpty ? "Someone" : thread.who).font(.system(size: 13, weight: .semibold))
                            .foregroundColor(hot ? Theme.textPrimary : Theme.textSecondary)
                         + Text("  " + thread.line).font(.system(size: 13))
                            .foregroundColor(hot ? Theme.textSecondary : Theme.textMuted))
                            .lineLimit(open ? nil : 2)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        VStack(alignment: .trailing, spacing: 3) {
                            Text(hot ? "NEEDS REPLY" : "FYI").font(.system(size: 9, weight: .semibold, design: .monospaced))
                                .foregroundStyle(hot ? Theme.textPrimary : Theme.textMuted)
                                .padding(.horizontal, 6).padding(.vertical, 2)
                                .overlay(Capsule().stroke(hot ? Color.white.opacity(0.45) : Theme.chipStroke, lineWidth: 1))
                                .fixedSize()
                            Text(timeLabel(thread.receivedTs)).font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(Theme.textMuted).fixedSize()
                        }
                    }
                    .padding(.vertical, 6).padding(.leading, 8).padding(.trailing, 6)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(hovered || open ? 0.05 : hot ? 0.03 : 0)))
                    .overlay(alignment: .leading) {
                        // Replies owed get a quiet accent, so they read first.
                        if hot { RoundedRectangle(cornerRadius: 1).fill(Color.white.opacity(0.6)).frame(width: 2).padding(.vertical, 6) }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .onHover { inside in hoveredThread = inside ? thread.id : (hoveredThread == thread.id ? nil : hoveredThread) }
                .help(open ? "Hide the actions" : "Reply, open or ask about this")
                .accessibilityValue(open ? "Expanded" : "Collapsed")
                // Beside the row, not in it: a button inside the row's button
                // would open the thread instead.
                rowControls(thread.actionRow).padding(.top, 6)
            }
            if let failure = itemActions.failures[thread.id] {
                Text(failure).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, 32)
            }
            if open { threadDetail(thread) }
        }
    }

    private func threadDetail(_ thread: ProactiveThread) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !thread.title.isEmpty && thread.title != thread.line {
                Text(thread.title).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            HStack(spacing: 6) {
                if thread.canReply {
                    NotchChrome.small(actions.replying == thread.id ? "Close reply" : "Reply") {
                        if actions.replying == thread.id { actions.replying = nil } else {
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
            if actions.replying == thread.id { replyArea(thread) }
        }
        .padding(.leading, 32).padding(.bottom, 6)
    }

    /// HOOK(W3): the reply composer, or a placeholder until it is wired.
    @ViewBuilder private func replyArea(_ thread: ProactiveThread) -> some View {
        if let composer = BriefReplyHook.composer {
            composer(thread) { actions.replying = nil; model.onResize() }
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Replying from the Brief isn't set up in this build. Open the thread in Mail to answer it.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
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

// MARK: - A scroll area as tall as its content, up to a cap

struct CappedScroll<Content: View>: View {
    let cap: CGFloat?
    @ViewBuilder var content: Content
    @State private var height: CGFloat = 0

    var body: some View {
        if let cap {
            ScrollView(.vertical) {
                content
                    .background(GeometryReader { geo in
                        Color.clear.preference(key: CappedScrollHeight.self, value: geo.size.height)
                    })
            }
            .scrollIndicators(.automatic)
            .frame(height: min(max(height, 1), cap))
            .onPreferenceChange(CappedScrollHeight.self) { height = $0 }
        } else {
            content
        }
    }
}

private struct CappedScrollHeight: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}
