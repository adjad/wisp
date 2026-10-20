import SwiftUI
import AppKit

/// The evening check-in and the weekly goal pulse on the notch's Day tab
/// (service/assistant/checkin.py). Nothing here needs typing: each planned task
/// takes one tap (Done, Tomorrow, Drop), each of tomorrow's events one tap
/// (Keep, Move, Cancel), and the goal pulse one tap (On track, Behind, Done).
///
/// Cancel cancels: the row becomes an Undo bar for five seconds, then the exact
/// occurrence is removed through POST /assistant/checkin/cancel_event and read
/// back (CheckInCancel.swift). The wait lives in CheckInModel.shared rather
/// than in this view, so closing the panel or switching tabs during it neither
/// drops the cancel nor hurries it; Undo is there again if the card reopens
/// inside the window. Quitting Wisp inside the window drops it: nothing is
/// cancelled. Neither Cancel nor Move is offered for an event on a read-only
/// (subscribed) calendar, for someone else's event (only its organizer may
/// change it), or when Calendar doesn't say who organizes an event with
/// invitees; the row then says why, and an invitation gets "Decline in
/// Calendar", which opens that event in Calendar. Move opens an inline sub-panel: up to three times Wisp found free
/// the way the Day tab plans (GET /assistant/checkin/move_suggestions: working
/// hours, calendar, planned tasks, learned busy and away windows; no model
/// call), or another day and time of your choosing. Picking one opens Ask with
/// the move request; nothing is moved from this card.

struct CheckInTask: Identifiable, Equatable {
    let id: String
    let title: String
    let carriedFrom: String?

    init?(_ row: [String: Any]) {
        guard let id = row["id"] as? String, let title = row["title"] as? String, !id.isEmpty else { return nil }
        self.id = id
        self.title = title
        carriedFrom = row["carried_from"] as? String
    }
}

struct CheckInEvent: Identifiable, Equatable {
    let id: String
    let title: String
    let whenTs: Double?
    let endTs: Double?
    let allDay: Bool
    let sourceId: String?
    /// Synced invitees (nil: not synced yet), who organizes it, and the service's
    /// reason a one-tap Cancel is not offered ("" when it is).
    let inviteeCount: Int?
    let organizerIsUser: Bool?
    /// false: a read-only (subscribed) calendar; nil: the app didn't say.
    let calendarWritable: Bool?
    let cancelBlocked: String
    let moveBlocked: String

    init?(_ row: [String: Any], index: Int) {
        guard let title = row["title"] as? String else { return nil }
        id = (row["id"] as? String) ?? "event-\(index)"
        self.title = title
        whenTs = (row["when_ts"] as? NSNumber)?.doubleValue
        endTs = (row["end_ts"] as? NSNumber)?.doubleValue
        allDay = row["all_day"] as? Bool ?? false
        sourceId = row["source_id"] as? String
        inviteeCount = (row["invitee_count"] as? NSNumber)?.intValue
        organizerIsUser = row["organizer_is_user"] as? Bool
        calendarWritable = row["calendar_writable"] as? Bool
        cancelBlocked = row["cancel_blocked"] as? String ?? ""
        // An older service sends no move_blocked: then Move follows Cancel's rules.
        moveBlocked = row["move_blocked"] as? String ?? (row["cancel_blocked"] as? String ?? "")
    }

    /// A Calendar block of the Day timeline as the card sees an event
    /// (TodayBlock.calendarEvent): the same fields, from the same service rule.
    init(id: String, title: String, whenTs: Double?, endTs: Double?, allDay: Bool, sourceId: String?,
         inviteeCount: Int?, organizerIsUser: Bool?, calendarWritable: Bool?, cancelBlocked: String, moveBlocked: String) {
        self.id = id
        self.title = title
        self.whenTs = whenTs
        self.endTs = endTs
        self.allDay = allDay
        self.sourceId = sourceId
        self.inviteeCount = inviteeCount
        self.organizerIsUser = organizerIsUser
        self.calendarWritable = calendarWritable
        self.cancelBlocked = cancelBlocked
        self.moveBlocked = moveBlocked
    }

    /// Move is offered only when the app would carry it out (CalendarMove.refusal):
    /// a timed event, on a writable calendar, the user's own or without invitees.
    var canMove: Bool { !allDay && moveBlocked.isEmpty && calendarWritable != false }

    /// The cancel request the service accepts for this row, or nil when Cancel
    /// must not be offered (the service said why, or the row isn't complete).
    var cancelBody: [String: Any]? {
        cancelBlocked.isEmpty ? CheckInCancel.requestBody(sourceId: sourceId, startTs: whenTs, inviteeCount: inviteeCount) : nil
    }
    var canCancel: Bool { cancelBody != nil && calendarWritable != false }
    var cancelReason: String { CheckInCancel.blockedReason(cancelBlocked) }

    /// How long it runs, for a moved copy; an hour when the calendar gave no end.
    var durationMinutes: Int {
        guard let whenTs, let endTs, endTs > whenTs else { return 60 }
        return Int((endTs - whenTs) / 60)
    }
}

/// One time Wisp found for a moved event (service/assistant/checkin.py).
struct MoveSlot: Identifiable, Equatable {
    let startTs: Double
    let endTs: Double
    let day: String
    let label: String
    let reason: String
    var id: Double { startTs }

    init?(_ row: [String: Any]) {
        guard let start = (row["start_ts"] as? NSNumber)?.doubleValue,
              let end = (row["end_ts"] as? NSNumber)?.doubleValue,
              let label = row["label"] as? String else { return nil }
        startTs = start
        endTs = end
        day = row["day"] as? String ?? ""
        self.label = label
        reason = row["reason"] as? String ?? ""
    }
}

struct GoalPulse: Equatable {
    let goal: String
    let status: String?

    init?(_ row: Any?) {
        guard let row = row as? [String: Any], let goal = row["goal"] as? String, !goal.isEmpty else { return nil }
        self.goal = goal
        status = row["status"] as? String
    }
}

struct CheckInCard: Equatable {
    var day = ""
    var due = false
    var completed = false
    var planned = 0
    var done = 0
    var tasks: [CheckInTask] = []
    var tomorrowDay = ""
    var events: [CheckInEvent] = []
    var goalPulse: GoalPulse?

    init() {}
    init?(json: [String: Any]) {
        guard let day = json["day"] as? String else { return nil }
        self.day = day
        due = json["due"] as? Bool ?? false
        completed = json["completed"] as? Bool ?? false
        planned = (json["planned"] as? NSNumber)?.intValue ?? 0
        done = (json["done"] as? NSNumber)?.intValue ?? 0
        tasks = (json["tasks"] as? [[String: Any]] ?? []).compactMap(CheckInTask.init)
        let tomorrow = json["tomorrow"] as? [String: Any] ?? [:]
        tomorrowDay = tomorrow["day"] as? String ?? ""
        events = (tomorrow["events"] as? [[String: Any]] ?? []).enumerated().compactMap { CheckInEvent($1, index: $0) }
        goalPulse = GoalPulse(json["goal_pulse"])
    }

    /// The card asks only in the evening, only once, and only when there is
    /// something to tap on.
    var showsCheckIn: Bool { due && !completed && (!tasks.isEmpty || !events.isEmpty || planned > 0) }
    var tasksSettled: Bool { tasks.isEmpty }
}

@MainActor
final class CheckInModel: ObservableObject {
    /// One card for the whole app, so the Day tab's first frame already has it
    /// after a tab switch instead of fetching again and growing the panel.
    static let shared = CheckInModel()

    @Published var card = CheckInCard()
    @Published var busy = false
    @Published var error = ""
    /// Events the person tapped Keep on, so the row reads as answered.
    @Published var kept: Set<String> = []
    @Published var goalAnswered = false

    /// A Cancel waiting out its Undo window: the row as it was when Cancel was
    /// tapped (what the request will say), and where to reload afterwards.
    struct PendingCancel: Equatable {
        let event: CheckInEvent
        let startedAt: Date
        let timezone: String
    }
    struct CancelFailure: Equatable {
        let title: String
        let failure: CheckInCancel.Failure
    }
    /// Cancels inside their Undo window, by event id. They live on the shared
    /// model, not the card, so a closed panel or a tab switch neither drops
    /// nor hurries them, and Undo is back if the card reopens in time. One
    /// window per event, so a second Cancel never hurries the first.
    @Published private(set) var pendingCancels: [String: PendingCancel] = [:]
    /// Cancels sent and awaiting the service's verified answer.
    @Published private(set) var cancelling: Set<String> = []
    /// Verified cancels, by event id: the line the card shows for them.
    @Published private(set) var cancelled: [String: String] = [:]
    @Published private(set) var cancelFailures: [String: CancelFailure] = [:]
    private var cancelDetails: [String: PendingCancel] = [:]
    /// Where each cancel was tapped, by event id: the card (true) or the Day
    /// timeline's ⋯ menu (false). One queue serves both, so no cancel is hurried
    /// or dropped by a tab switch; but each surface shows, keeps itself up for,
    /// and reconciles only its own.
    @Published private(set) var cancelFromCard: [String: Bool] = [:]
    private lazy var cancelQueue = CheckInCancelQueue { [weak self] id in await self?.sendCancel(id) }

    init() {
        cancelQueue.onChange = { [weak self] in self?.syncCancelState() }
    }

    /// A window is open or a request is out: the card must stay where the
    /// person can see Undo and the outcome.
    var hasCancelActivity: Bool { !pendingCancels.isEmpty || !cancelling.isEmpty }
    var hasCancelOutcomes: Bool { !cancelled.isEmpty || !cancelFailures.isEmpty }
    /// The card's own cancels (an unknown origin is the card's, as before the timeline had any).
    func isCardCancel(_ id: String) -> Bool { cancelFromCard[id] ?? true }
    var hasCardCancelActivity: Bool {
        pendingCancels.keys.contains(where: isCardCancel) || cancelling.contains(where: isCardCancel)
    }
    var hasCardCancelOutcomes: Bool {
        cancelled.keys.contains(where: isCardCancel) || cancelFailures.keys.contains(where: isCardCancel)
    }
    /// Origins of cancels that are over and whose outcome has been cleared.
    private func forgetSettledOrigins() {
        cancelFromCard = cancelFromCard.filter {
            pendingCancels[$0.key] != nil || cancelling.contains($0.key) || cancelled[$0.key] != nil || cancelFailures[$0.key] != nil
        }
    }

    func load(day: String, timezone: String) async {
        var components = URLComponents(url: WispClient.baseURL.appendingPathComponent("assistant/checkin"), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "day", value: day), URLQueryItem(name: "timezone", value: timezone)]
        guard let url = components.url else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? false,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let next = CheckInCard(json: object) else { return }
        if next.day != card.day { kept = []; goalAnswered = false; cancelled = [:]; cancelFailures = [:]; forgetSettledOrigins() }
        card = next
        error = ""
        reconcilePendingCancels()
    }

    /// A reloaded row must still be the one Cancel was tapped on. If its start
    /// or invitee count changed, or it left the list, that cancel is dropped
    /// before it is sent and the card says so; the service would refuse it
    /// anyway (404 or not_current), but the person shouldn't wait to learn that.
    private func reconcilePendingCancels() {
        for (id, pending) in pendingCancels where isCardCancel(id) {
            let row = card.events.first { $0.id == id }
            let same = row.map { $0.canCancel && $0.sourceId == pending.event.sourceId
                && $0.whenTs == pending.event.whenTs && $0.inviteeCount == pending.event.inviteeCount } ?? false
            if same { continue }
            undoCancel(pending.event)
            let title = CheckInSection.plainTitle(pending.event.title)
            cancelFailures[id] = CancelFailure(title: title, failure: row == nil
                ? CheckInCancel.Failure(outcome: "changed", message: "“\(title)” is no longer on tomorrow's list; nothing was cancelled.")
                : CheckInCancel.changed(title: title))
        }
    }

    /// The Day timeline's counterpart of reconcilePendingCancels, with the Calendar
    /// blocks the Day tab shows now (TodayBlock.calendarEvent): a block that changed
    /// under a pending cancel, or left the plan, drops that cancel before it is sent.
    func reconcileTimelineCancels(_ events: [CheckInEvent]) {
        for (id, pending) in pendingCancels where !isCardCancel(id) {
            let row = events.first { $0.id == id }
            let same = row.map { $0.canCancel && $0.sourceId == pending.event.sourceId
                && $0.whenTs == pending.event.whenTs && $0.inviteeCount == pending.event.inviteeCount } ?? false
            if same { continue }
            undoCancel(pending.event)
            let title = CheckInSection.plainTitle(pending.event.title)
            cancelFromCard[id] = false
            cancelFailures[id] = CancelFailure(title: title, failure: row == nil
                ? CheckInCancel.Failure(outcome: "changed", message: "“\(title)” is no longer on the timeline; nothing was cancelled.")
                : CheckInCancel.changed(title: title))
        }
    }

    // MARK: Cancel with Undo

    /// Starts the Undo window. The cancel is sent only when it runs out untouched.
    /// `fromCard` is false for the Day timeline's ⋯ menu (same queue, own surface).
    func beginCancel(_ event: CheckInEvent, timezone: String, fromCard: Bool = true) {
        guard event.canCancel, !cancelQueue.isActive(event.id) else { return }
        cancelFailures[event.id] = nil
        cancelFromCard[event.id] = fromCard
        cancelDetails[event.id] = PendingCancel(event: event, startedAt: Date(), timezone: timezone)
        cancelQueue.begin(event.id, now: cancelDetails[event.id]!.startedAt)
    }

    func undoCancel(_ event: CheckInEvent) {
        if cancelQueue.undo(event.id) { cancelDetails[event.id] = nil; cancelFromCard[event.id] = nil }
    }

    private func syncCancelState() {
        let open = cancelQueue.pending
        pendingCancels = cancelDetails.filter { open[$0.key] != nil }
        cancelling = cancelQueue.inFlight
    }

    /// POST /assistant/checkin/cancel_event for one occurrence, then reload
    /// the card so the row goes (verified) or shows fresh facts (refused).
    private func sendCancel(_ id: String) async {
        guard let pending = cancelDetails[id] else { return }
        defer { cancelDetails[id] = nil }
        let event = pending.event
        guard let body = event.cancelBody else { return }
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("assistant/checkin/cancel_event"))
        request.httpMethod = "POST"
        request.timeoutInterval = CheckInCancel.requestTimeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        var status: Int?
        var object: [String: Any]?
        var transport: URLError.Code?
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            status = (response as? HTTPURLResponse)?.statusCode
            object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        } catch let error as URLError {
            transport = error.code
        } catch {
            transport = .unknown
        }
        let title = CheckInSection.plainTitle(event.title)
        if let failure = CheckInCancel.failure(status: status, body: object, transport: transport) {
            cancelFailures[id] = CancelFailure(title: title, failure: failure)
            announce(failure.isUncertain ? "Cancel of “\(title)” unconfirmed. \(failure.message)" : "Not cancelled: \(failure.message)")
        } else {
            let count = CheckInCancel.notifiedCount(body: object, rowCount: event.inviteeCount)
            cancelled[id] = CheckInCancel.doneLabel(title: title, inviteeCount: count)
            announce(cancelled[id]!)
        }
        // The day the card shows now, not the one Cancel was tapped on: a
        // window that crosses midnight must not flip the card back a day.
        await load(day: card.day, timezone: pending.timezone)
    }

    private func announce(_ text: String) {
        AccessibilityNotification.Announcement(text).post()
    }

    func decide(_ task: CheckInTask, _ decision: String, day: String, timezone: String) async {
        await post("assistant/checkin/tasks/\(task.id)", ["decision": decision, "day": day, "timezone": timezone])
    }

    func complete(day: String, timezone: String) async {
        await post("assistant/checkin/complete", ["day": day, "timezone": timezone])
        if error.isEmpty {
            // The card's outcomes have been read; the timeline's stay on their blocks.
            cancelled = cancelled.filter { !isCardCancel($0.key) }
            cancelFailures = cancelFailures.filter { !isCardCancel($0.key) }
            forgetSettledOrigins()
        }
    }

    func keep(_ event: CheckInEvent) { kept.insert(event.id) }

    /// Other times for `event`, from the planner. Empty when nothing fits or
    /// the service is away; the card then offers only "Other time".
    func moveSuggestions(for event: CheckInEvent, day: String, timezone: String) async -> [MoveSlot] {
        var components = URLComponents(url: WispClient.baseURL.appendingPathComponent("assistant/checkin/move_suggestions"),
                                       resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "event_id", value: event.id),
                                 URLQueryItem(name: "day", value: day),
                                 URLQueryItem(name: "timezone", value: timezone)]
        guard let url = components.url else { return [] }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? false,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return [] }
        return (object["suggestions"] as? [[String: Any]] ?? []).compactMap(MoveSlot.init)
    }

    func answerGoal(_ goal: String, _ status: String) async {
        busy = true
        defer { busy = false }
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("assistant/checkin/goal"))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["goal": goal, "status": status])
        if let (_, response) = try? await URLSession.shared.data(for: request),
           (response as? HTTPURLResponse).map({ (200..<300).contains($0.statusCode) }) ?? false {
            goalAnswered = true
            card.goalPulse = nil
            error = ""
        } else {
            error = "Couldn't save that. Is Wisp's service running?"
        }
    }

    private func post(_ path: String, _ body: [String: Any]) async {
        busy = true
        defer { busy = false }
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            error = "Wisp's service didn't answer."
            return
        }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (200..<300).contains(http.statusCode), let object, let next = CheckInCard(json: object) else {
            error = object?["detail"] as? String ?? "Couldn't save that."
            return
        }
        error = ""
        card = next
    }
}

/// The card on Today. `onAsk` opens Ask with a request about an event.
struct CheckInSection: View {
    @ObservedObject var model: CheckInModel
    let day: String
    let timezone: TimeZone
    var clock: (Double) -> String
    var onAsk: (String) -> Void
    /// Opens the event in Calendar (Decline / Open in Calendar).
    var onOpenEvent: (CheckInEvent) -> Void
    var onChanged: () -> Void

    /// The event whose Move sub-panel is open, if any.
    @State private var moving: CheckInEvent?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // The shared model may still hold an earlier day's card for a moment;
        // only today's card is ever shown or tapped.
        // A cancel in its window or in flight, or an outcome not yet read,
        // keeps the card up: the person must be able to see Undo and the result.
        if model.card.day == day, model.card.showsCheckIn || model.hasCardCancelActivity
            || (model.hasCardCancelOutcomes && model.card.due && !model.card.completed) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text("Evening check-in").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
                    Text(headline).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                if let moving, model.card.tasksSettled {
                    CheckInMovePanel(model: model, event: moving,
                                     day: model.card.tomorrowDay.isEmpty ? day : model.card.tomorrowDay,
                                     timezone: timezone, clock: clock, backHelp: "Back to tomorrow's events (Esc)",
                                     onAsk: onAsk, onClose: closeMove)
                } else if !model.card.tasksSettled {
                    ForEach(model.card.tasks) { task in taskRow(task) }
                } else if !model.card.events.isEmpty {
                    Text(eventsLine).font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                    ForEach(model.card.events) { event in eventRow(event) }
                }
                // Outcomes for rows that have already left tomorrow's list.
                ForEach(outcomeLines) { line in
                    Text(line.text).font(.system(size: 11.5)).foregroundStyle(line.failed ? Theme.bad : Theme.textMuted)
                }
                if !model.error.isEmpty {
                    Text(model.error).font(.system(size: 11)).foregroundStyle(Theme.bad)
                }
                if moving == nil || !model.card.tasksSettled {
                    HStack {
                        Text(footerLine).font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                        Spacer()
                        Button(model.card.tasksSettled ? "Done for today" : "Skip tonight") {
                            Task { await model.complete(day: day, timezone: timezone.identifier); onChanged() }
                        }
                        .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                        // Closing the card would hide Undo and the outcome.
                        .disabled(model.busy || model.hasCardCancelActivity)
                        .help(model.hasCardCancelActivity ? "Wait for the cancel to finish first" : "Closes tonight's check-in")
                    }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
            .onExitCommand { if moving != nil { closeMove() } }
            // The card may arrive after the Day tab painted; it must never
            // animate the panel's height. The sub-panel's own animation sits
            // inside (nearer the content), so it survives the outer nil.
            .animation(.easeInOut(duration: 0.18), value: moving?.id)
            .transaction { $0.animation = nil }
            .onChange(of: model.card.events) { _, events in
                // The event left tomorrow's list (reload, cancelled elsewhere): the panel is stale.
                if let moving, !events.contains(where: { $0.id == moving.id }) { closeMove() }
            }
            .onDisappear { closeMove() }     // a hidden card never comes back with a stale panel
        }
    }

    private var headline: String {
        let card = model.card
        if card.planned == 0 { return "Nothing was planned today." }
        var text = "You planned \(card.planned) thing\(card.planned == 1 ? "" : "s") today"
        if card.done > 0 { text += ", \(card.done) done" }
        return text + "."
    }

    private var eventsLine: String {
        let count = model.card.events.count
        return "Tomorrow has \(count) event\(count == 1 ? "" : "s"). Anything to move?"
    }

    private var footerLine: String {
        if model.hasCardCancelActivity { return "A cancel is under way. Undo is on its row." }
        return model.card.tasksSettled ? "That's the day. Carried tasks are on tomorrow's plan."
            : "One tap each. Tomorrow carries it; Drop forgets it."
    }

    private struct OutcomeLine: Identifiable { let id: String; let text: String; let failed: Bool }

    private var outcomeLines: [OutcomeLine] {
        // The card's own cancels only; a timeline cancel's outcome stays on its block.
        let shown = Set(model.card.events.map(\.id))
        let done = model.cancelled.filter { !shown.contains($0.key) && model.isCardCancel($0.key) }.sorted { $0.key < $1.key }
            .map { OutcomeLine(id: $0.key, text: $0.value, failed: false) }
        let failed = model.cancelFailures.filter { !shown.contains($0.key) && model.isCardCancel($0.key) }.sorted { $0.key < $1.key }
            .map { OutcomeLine(id: $0.key, text: "“\($0.value.title)”: \($0.value.failure.message)", failed: true) }
        return done + failed
    }

    private func taskRow(_ task: CheckInTask) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(task.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(2)
                if let from = task.carriedFrom {
                    Text("Carried from \(from)").font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                }
            }
            Spacer(minLength: 8)
            Group {
                chip("Done", primary: true) { Task { await model.decide(task, "done", day: day, timezone: timezone.identifier); onChanged() } }
                chip("Tomorrow") { Task { await model.decide(task, "tomorrow", day: day, timezone: timezone.identifier); onChanged() } }
                chip("Drop") { Task { await model.decide(task, "drop", day: day, timezone: timezone.identifier); onChanged() } }
            }
            .disabled(model.busy)
        }
    }

    private func eventRow(_ event: CheckInEvent) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(event.allDay ? "All day" : event.whenTs.map(clock) ?? "")
                    .font(.system(size: 11.5).monospacedDigit()).foregroundStyle(Theme.textSecondary)
                    .frame(width: 62, alignment: .leading)
                Text(event.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                    .strikethrough(model.pendingCancels[event.id] != nil || model.cancelled[event.id] != nil)
                Spacer(minLength: 8)
                if let pending = model.pendingCancels[event.id] {
                    undoBar(pending)
                } else if model.cancelling.contains(event.id) {
                    Text("Cancelling…").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                } else if let done = model.cancelled[event.id] {
                    Text(done).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
                } else if model.cancelFailures[event.id]?.failure.isUncertain == true {
                    // It may be cancelled; no tap here may send another request.
                    Text("Check Calendar").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                        .help(model.cancelFailures[event.id]?.failure.message ?? "")
                } else if model.kept.contains(event.id) {
                    Text("Kept").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                } else {
                    Group {
                        chip("Keep", primary: true) { model.keep(event) }
                        if event.canMove {               // never for all-day, read-only or someone else's events
                            chip("Move") { openMove(event) }
                                .help(model.hasCardCancelActivity ? "Wait for the cancel to finish first" : "Pick another time; Wisp finds free ones")
                                .disabled(model.hasCardCancelActivity)   // the panel would hide the Undo bar
                        }
                        cancelControl(event)
                    }
                    .disabled(model.busy)
                }
            }
            if let pending = model.pendingCancels[event.id] {
                countdown(pending)
            }
            if let failed = model.cancelFailures[event.id] {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(failed.failure.message).font(.system(size: 11)).foregroundStyle(Theme.bad)
                    if failed.failure.canRetry, event.canCancel {
                        chip("Try again") { model.beginCancel(event, timezone: timezone.identifier) }
                            .help("Starts the 5-second Undo again, then cancels")
                            .disabled(model.busy)
                    }
                }
            }
        }
    }

    // MARK: Cancel cancels, after Undo

    /// Cancel when the service will accept it. Otherwise what the person can do
    /// instead: nothing from here on a read-only calendar (said plainly), or a
    /// real button that opens the event in Calendar, "Decline" for an
    /// invitation. The service's full reason is on hover and for VoiceOver.
    @ViewBuilder private func cancelControl(_ event: CheckInEvent) -> some View {
        switch CheckInCancel.blockedControl(canCancel: event.canCancel, calendarWritable: event.calendarWritable,
                                            organizerIsUser: event.organizerIsUser) {
        case .cancel:
            chip("Cancel") { model.beginCancel(event, timezone: timezone.identifier) }
                .help("Removes it from your Calendar after a 5-second Undo; Calendar tells its invitees")
        case .readOnly:
            Text("On a read-only calendar").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                .help(event.cancelReason)
                .accessibilityLabel(event.cancelReason)
        case .decline:
            chip("Decline in Calendar ↗") { onOpenEvent(event) }
                .help(event.cancelReason)
                .accessibilityLabel("Decline in Calendar. \(event.cancelReason)")
        case .open:
            chip("Open in Calendar ↗") { onOpenEvent(event) }
                .help(event.cancelReason)
                .accessibilityLabel("Open in Calendar. \(event.cancelReason)")
        }
    }

    /// "Cancelled · its 3 invitees will be notified · Undo". The count is the
    /// one captured at the tap, which is also what the request will send.
    private func undoBar(_ pending: CheckInModel.PendingCancel) -> some View {
        HStack(spacing: 6) {
            Text(CheckInCancel.undoLabel(inviteeCount: pending.event.inviteeCount ?? 0) + " ·")
                .font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            chip("Undo", primary: true) { model.undoCancel(pending.event) }
                .help("Keeps the event; nothing has been sent yet")
                .accessibilityLabel("Undo cancelling “\(Self.plainTitle(pending.event.title))”; 5 seconds")
        }
    }

    /// The seconds left before the cancel is sent, as a shrinking line.
    private func countdown(_ pending: CheckInModel.PendingCancel) -> some View {
        TimelineView(reduceMotion ? .periodic(from: .now, by: 0.5) : .periodic(from: .now, by: 1.0 / 30)) { context in
            let left = max(0, 1 - context.date.timeIntervalSince(pending.startedAt) / CheckInCancel.undoWindow)
            Capsule().fill(Theme.textMuted).frame(height: 2)
                .scaleEffect(x: max(0.001, left), y: 1, anchor: .leading)
        }
        .frame(height: 2)
        .accessibilityHidden(true)       // the Undo button's label carries the window
    }

    static func plainTitle(_ raw: String) -> String {
        let flat = raw.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
            .replacingOccurrences(of: "“", with: "'").replacingOccurrences(of: "”", with: "'")
            .replacingOccurrences(of: "\"", with: "'")
        return flat.count > 80 ? String(flat.prefix(79)) + "…" : flat
    }

    private func openMove(_ event: CheckInEvent) { moving = event }

    private func closeMove() { moving = nil }

    private func chip(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(primary ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// "Move to…": Wisp's suggested times, then another day and time of your own.
/// Shared by the check-in card and the Day timeline's ⋯ menu (NotchDayView.swift).
/// Picking a time hands the request to Ask; the agent's confirmation card
/// ("its N invitees will be notified") gates the native, verified move.
struct CheckInMovePanel: View {
    @ObservedObject var model: CheckInModel
    let event: CheckInEvent
    /// The day the free times are looked for from: the event's own day.
    let day: String
    let timezone: TimeZone
    var clock: (Double) -> String
    var backHelp: String
    var onAsk: (String) -> Void
    var onClose: () -> Void

    @State private var slots: [MoveSlot] = []
    @State private var loadingSlots = true
    @State private var choosingOther = false
    @State private var otherDate = Date()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Button { onClose() } label: {
                    Label("Back", systemImage: "chevron.left").labelStyle(.titleAndIcon)
                        .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                }
                .buttonStyle(.plain).help(backHelp)
                Text("Move “\(event.title)” to…").font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                Spacer()
                if let when = event.whenTs {
                    Text("Now \(dayLabel(when)) \(clock(when))").font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                }
            }
            if loadingSlots {
                ProcessingRow(label: "Looking at your days…")
            } else if slots.isEmpty {
                Text("Nothing free fits in the next few days' working hours. Pick a time yourself.")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            } else {
                ForEach(slots) { slot in slotRow(slot) }
            }
            if choosingOther {
                otherTimeRow
            } else {
                chip("Other time…") { choosingOther = true }
                    .help("Choose a day and time yourself")
            }
            Text("Picking a time opens Ask with the request. Wisp confirms before the calendar changes.")
                .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Move \(event.title)")
        .task(id: event.id) {
            slots = []
            choosingOther = false
            loadingSlots = true
            otherDate = Date(timeIntervalSince1970: (event.whenTs ?? Date().timeIntervalSince1970) + 86400)
            let found = await model.moveSuggestions(for: event, day: day, timezone: timezone.identifier)
            guard !Task.isCancelled else { return }
            slots = found
            loadingSlots = false
            choosingOther = found.isEmpty
        }
    }

    /// The event, named precisely enough for the agent's calendar tools and
    /// unambiguous to a reader: title, day and time, and the calendar id. The
    /// title came from a calendar invite, so it is flattened to one short line
    /// and the instruction never depends on it.
    private func describe() -> String {
        var text = "“\(CheckInSection.plainTitle(event.title))”"
        if let when = event.whenTs {
            text += event.allDay ? " on \(dayLabel(when)) (all day)" : " on \(dayLabel(when)) at \(clock(when))"
        }
        let ref = event.sourceId ?? event.id
        if !ref.isEmpty { text += " [calendar event \(ref)]" }
        return text
    }

    private func moveRequest(to start: Double) -> String {
        "Move this calendar event to \(dayLabel(start)) at \(clock(start)), keeping it \(event.durationMinutes) minutes: \(describe())"
    }

    private func dayLabel(_ ts: Double) -> String {
        Date(timeIntervalSince1970: ts).formatted(Date.FormatStyle(timeZone: timezone).weekday(.wide).month(.abbreviated).day())
    }

    private func slotRow(_ slot: MoveSlot) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(slot.label).font(.system(size: 12.5, weight: .medium).monospacedDigit()).foregroundStyle(Theme.textPrimary)
                .frame(width: 150, alignment: .leading)
            Text(slot.reason).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            Spacer(minLength: 8)
            chip("Move here", primary: true) { onAsk(moveRequest(to: slot.startTs)); onClose() }
        }
    }

    private var otherTimeRow: some View {
        HStack(spacing: 8) {
            DatePicker("Day", selection: $otherDate, in: Date()..., displayedComponents: .date)
                .datePickerStyle(.stepperField).labelsHidden()
                .environment(\.timeZone, timezone)
                .font(.system(size: 11.5))
            DatePicker("Time", selection: $otherDate, in: Date()..., displayedComponents: .hourAndMinute)
                .datePickerStyle(.stepperField).labelsHidden()
                .environment(\.timeZone, timezone)
                .font(.system(size: 11.5))
            Spacer(minLength: 8)
            chip("Move here", primary: true) { onAsk(moveRequest(to: otherDate.timeIntervalSince1970)); onClose() }
                .disabled(event.whenTs.map { abs(otherDate.timeIntervalSince1970 - $0) < 60 } ?? false)
        }
        .accessibilityLabel("Another day and time")
    }

    private func chip(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(primary ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// "Ship Wisp 1.5: on track?" One tap, once a week.
struct GoalPulseSection: View {
    @ObservedObject var model: CheckInModel

    var body: some View {
        if let pulse = model.card.goalPulse, !model.goalAnswered {
            VStack(alignment: .leading, spacing: 8) {
                Text("This week").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(pulse.goal).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(2)
                    Text(pulse.status == "behind" ? "You said behind last time." : "On track?")
                        .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    Spacer(minLength: 8)
                    answer("On track", "on_track", primary: true, goal: pulse.goal)
                    answer("Behind", "behind", goal: pulse.goal)
                    answer("Done", "done", goal: pulse.goal)
                }
                Text("Behind makes it the goal your brief leads with. Done retires it.")
                    .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
            .disabled(model.busy)
        }
    }

    private func answer(_ title: String, _ status: String, primary: Bool = false, goal: String) -> some View {
        Button { Task { await model.answerGoal(goal, status) } } label: {
            Text(title).font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(primary ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}
