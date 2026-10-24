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
    static var composer: ((ProactiveThread, _ close: @escaping () -> Void) -> AnyView)? = nil
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
            return ProactiveOutcome(ok: false, message: detail ?? "Wisp couldn't do that (\(status)).")
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
        fires[card.id] = .sending
        var body: [String: Any] = ["draft": draft(card)]
        if !action.recipient.isEmpty { body["recipient"] = action.recipient }
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
    func qaReset(account: String? = nil, openDraft: String? = nil, expanded: Set<String> = [], replying: String? = nil) {
        self.account = account
        openDrafts = openDraft.map { [$0] } ?? []
        self.expanded = expanded
        self.replying = replying
    }
    #endif
}

// MARK: - The Brief's proactive section

struct ProactiveBriefSection: View {
    let proactive: ProactiveState
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    @ObservedObject var actions: ProactiveActions = .shared
    /// The tallest the cards, to-dos and summaries get before they scroll, so
    /// the tab fits the notch; nil draws everything (the QA driver's full render).
    var scrollCap: CGFloat? = 520
    /// HOOK(W3): told when Reply is tapped on a Summaries row.
    var onReply: ((ProactiveThread) -> Void)? = BriefReplyHook.onReply

    private static let todoLimit = 6

    private var chosen: String? {
        actions.account.flatMap { picked in proactive.accounts.contains(picked) ? picked : nil }
    }

    var body: some View {
        let shown = proactive.only(chosen)
        VStack(alignment: .leading, spacing: 12) {
            if !proactive.summary.isEmpty {
                Text(proactive.summary)
                    .font(.system(size: 13.5)).foregroundStyle(Theme.textSecondary)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            if proactive.accounts.count > 1 { accountChips }
            CappedScroll(cap: scrollCap) {
                VStack(alignment: .leading, spacing: 18) {
                    beforeYouAsk(shown)
                    todoSection(shown)
                    summaries(shown)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
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

    // MARK: Accounts

    private var accountChips: some View {
        HStack(spacing: 6) {
            accountChip("All", picked: chosen == nil) { actions.account = nil }
            ForEach(proactive.accounts, id: \.self) { account in
                accountChip(proactive.chipName(account), picked: chosen == account) { actions.account = account }
                    .help(account)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Accounts")
    }

    private func accountChip(_ title: String, picked: Bool, action: @escaping () -> Void) -> some View {
        Button {
            action()
            model.onResize()
        } label: {
            Text(title).font(.system(size: 11.5, weight: picked ? .semibold : .medium))
                .foregroundStyle(picked ? Theme.textPrimary : Theme.textSecondary)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Capsule().fill(picked ? Theme.chipOn : Theme.chipFill))
                .overlay(Capsule().stroke(picked ? Color.white.opacity(0.32) : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .fixedSize()
        .accessibilityValue(picked ? "Selected" : "")
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
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 8) {
                header("BEFORE YOU ASK", trailing: cards.isEmpty ? "" : "· \(cards.count)")
                Spacer()
                analyzeControl
            }
            if proactive.isWorking || actions.analyzePending() { progressRow }
            if !actions.analyzeError.isEmpty {
                Text(actions.analyzeError).font(.system(size: 11)).foregroundStyle(Theme.bad)
            }
            if cards.isEmpty {
                Text(emptyCardsLine)
                    .font(.system(size: 13)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ForEach(cards) { card in cardView(card).transition(.opacity) }
            }
        }
    }

    private var emptyCardsLine: String {
        if proactive.isWorking { return "Cards land here as Wisp finishes reading." }
        if proactive.isUnavailable { return "Wisp hasn't caught up on your mail and texts yet. Analyze now reads them on this Mac." }
        return "Nothing to get ahead of right now. Wisp will bring the next thing here."
    }

    private var analyzeControl: some View {
        let busy = proactive.isWorking || actions.analyzePending()
        return Button {
            Task { await actions.analyze(today: today, account: chosen); model.onResize() }
        } label: {
            HStack(spacing: 4) {
                Image(systemName: busy ? "ellipsis" : "sparkles").font(.system(size: 10))
                Text(busy ? "Reading…" : "Analyze now").font(.system(size: 11.5, weight: .medium))
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .fixedSize()
        .disabled(busy)
        .help(chosen.map { "Read \($0)'s mail and texts now and catch up" } ?? "Read your mail, texts, calendar and reminders now and catch up")
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

    // MARK: A card

    private func cardView(_ card: ProactiveCard) -> some View {
        let fire = actions.fires[card.id]
        let draftOpen = actions.openDrafts.contains(card.id)
        let action = card.action
        let hasDraft = !(action?.draft ?? "").isEmpty
        return VStack(alignment: .leading, spacing: 7) {
            Text(card.metaLine)
                .font(.system(size: 10, weight: .medium, design: .monospaced)).tracking(0.6)
                .foregroundStyle(Theme.textMuted)
                .help(card.why)
            Text(card.title)
                .font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if !card.body.isEmpty {
                Text(card.body)
                    .font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                    .lineSpacing(1.5)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if draftOpen, let action { draftEditor(card, action) }
            HStack(alignment: .center, spacing: 12) {
                if let action { fireButton(card, action, fire) }
                if hasDraft {
                    Button {
                        if draftOpen { actions.openDrafts.remove(card.id) } else { actions.openDrafts.insert(card.id) }
                        model.onResize()
                    } label: {
                        HStack(spacing: 3) {
                            Text(draftOpen ? "Hide the draft" : "Read the draft")
                            Image(systemName: draftOpen ? "chevron.up" : "chevron.down").font(.system(size: 8.5, weight: .bold))
                        }
                        .font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(fire == .sending)
                }
                Spacer(minLength: 4)
                if actions.dismissing.contains(card.id) {
                    ProgressView().controlSize(.small)
                } else if fire != .sending, !isDone(fire) {
                    Button("Dismiss") { Task { await actions.dismiss(card, today: today) } }
                        .buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .help("Not useful; Wisp won't bring this one back")
                }
            }
            .padding(.top, 2)
            if case .failed(let reason) = fire {
                Text(reason).font(.system(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = actions.cardErrors[card.id] {
                Text(error).font(.system(size: 11)).foregroundStyle(Theme.bad)
            }
        }
        .padding(EdgeInsets(top: 11, leading: 13, bottom: 11, trailing: 13))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.cardFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.cardStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(card.metaLine). \(card.title)")
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
            Button { Task { await actions.fire(card, today: today); model.onResize() } } label: {
                Text(isFailed(fire) ? "Try again" : action.label)
                    .font(.system(size: 12.5, weight: .semibold)).foregroundStyle(Color.black)
                    .lineLimit(1)
                    .padding(.horizontal, 13).padding(.vertical, 5)
                    .background(Capsule().fill(Color.white.opacity(0.92)))
                    .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .fixedSize()
            .help(action.sendsDraft ? "Sends exactly the draft below, after you've read it" : card.why)
        }
    }

    private func isFailed(_ fire: ProactiveActions.Fire?) -> Bool {
        if case .failed = fire { return true }
        return false
    }

    private func draftEditor(_ card: ProactiveCard, _ action: ProactiveCard.Action) -> some View {
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
        }
        .padding(.top, 2)
    }

    /// Room for the draft's lines at the editor's width, about 70 characters a line.
    private func draftHeight(_ text: String) -> CGFloat {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
            .reduce(0) { $0 + max(1, Int(ceil(Double($1.count) / 72))) }
        return min(150, max(64, CGFloat(lines) * 17 + 14))
    }

    // MARK: To-do

    @ViewBuilder private func todoSection(_ shown: ProactiveState) -> some View {
        let todos = shown.orderedTodos
        let open = todos.filter { !actions.isDone($0) }.count
        if !todos.isEmpty {
            let visible = actions.showAllTodos ? todos : Array(todos.prefix(Self.todoLimit))
            VStack(alignment: .leading, spacing: 4) {
                header("TO-DO", trailing: open == 0 ? "· all done" : "· \(open) left")
                    .padding(.bottom, 3)
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
            }
            .padding(.vertical, 3)
            if let error = actions.todoErrors[todo.id] {
                Text(error).font(.system(size: 11)).foregroundStyle(Theme.bad).padding(.leading, 51)
            }
        }
        .accessibilityElement(children: .contain)
    }

    // MARK: Summaries

    @ViewBuilder private func summaries(_ shown: ProactiveState) -> some View {
        let threads = shown.orderedThreads
        if !threads.isEmpty {
            let waiting = threads.filter(\.needsReply).count
            VStack(alignment: .leading, spacing: 2) {
                header("SUMMARIES", trailing: waiting == 0 ? "" : "· \(waiting) need\(waiting == 1 ? "s" : "") a reply")
                    .padding(.bottom, 4)
                ForEach(threads) { thread in threadRow(thread) }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Summaries")
        }
    }

    private func threadRow(_ thread: ProactiveThread) -> some View {
        let open = actions.expanded.contains(thread.id)
        return VStack(alignment: .leading, spacing: 6) {
            Button {
                if open { actions.expanded.remove(thread.id) } else { actions.expanded.insert(thread.id) }
                model.onResize()
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 9) {
                    Image(systemName: DayTimeline.sourceSymbol(thread.source))
                        .font(.system(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
                    (Text(thread.who.isEmpty ? "Someone" : thread.who).font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Theme.textPrimary)
                     + Text("  " + thread.line).font(.system(size: 13)).foregroundColor(Theme.textSecondary))
                        .lineLimit(open ? nil : 1)
                        .fixedSize(horizontal: false, vertical: open)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if thread.needsReply {
                        Text("NEEDS REPLY").font(.system(size: 9, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Theme.textPrimary)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .overlay(Capsule().stroke(Color.white.opacity(0.4), lineWidth: 1))
                            .fixedSize()
                    }
                    Text(timeLabel(thread.receivedTs)).font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(Theme.textMuted).fixedSize()
                    Image(systemName: open ? "chevron.up" : "chevron.down")
                        .font(.system(size: 8, weight: .bold)).foregroundStyle(Theme.textMuted)
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(open ? "Expanded" : "Collapsed")
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
        .padding(.leading, 24).padding(.bottom, 6)
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
        !account.isEmpty && account != proactive.accounts.first && account.lowercased().hasSuffix("gmail.com")
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
