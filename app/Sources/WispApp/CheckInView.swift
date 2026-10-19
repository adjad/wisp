import SwiftUI
import AppKit

/// The evening check-in and the weekly goal pulse on the notch's Day tab
/// (service/assistant/checkin.py). Nothing here needs typing: each planned task
/// takes one tap (Done, Tomorrow, Drop), each of tomorrow's events one tap
/// (Keep, Move, Cancel), and the goal pulse one tap (On track, Behind, Done).
///
/// Cancel opens Ask with the request naming the exact event, so the agent's
/// own confirmation stays the gate (cancelling can notify attendees). Move
/// opens an inline sub-panel: up to three times Wisp found free the way the
/// Day tab plans (GET /assistant/checkin/move_suggestions: working hours,
/// calendar, planned tasks, learned busy and away windows; no model call), or
/// another day and time of your choosing. Picking one opens Ask with the move
/// request; nothing is changed from this card.

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

    init?(_ row: [String: Any], index: Int) {
        guard let title = row["title"] as? String else { return nil }
        id = (row["id"] as? String) ?? "event-\(index)"
        self.title = title
        whenTs = (row["when_ts"] as? NSNumber)?.doubleValue
        endTs = (row["end_ts"] as? NSNumber)?.doubleValue
        allDay = row["all_day"] as? Bool ?? false
        sourceId = row["source_id"] as? String
    }

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
        if next.day != card.day { kept = []; goalAnswered = false }
        card = next
    }

    func decide(_ task: CheckInTask, _ decision: String, day: String, timezone: String) async {
        await post("assistant/checkin/tasks/\(task.id)", ["decision": decision, "day": day, "timezone": timezone])
    }

    func complete(day: String, timezone: String) async {
        await post("assistant/checkin/complete", ["day": day, "timezone": timezone])
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
    var onChanged: () -> Void

    /// The event whose Move sub-panel is open, if any.
    @State private var moving: CheckInEvent?
    @State private var slots: [MoveSlot] = []
    @State private var loadingSlots = false
    @State private var choosingOther = false
    @State private var otherDate = Date()

    var body: some View {
        if model.card.showsCheckIn {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text("Evening check-in").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
                    Text(headline).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                if let moving, model.card.tasksSettled {
                    movePanel(moving)
                } else if !model.card.tasksSettled {
                    ForEach(model.card.tasks) { task in taskRow(task) }
                } else if let moving {
                    movePanel(moving)
                } else if !model.card.events.isEmpty {
                    Text(eventsLine).font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                    ForEach(model.card.events) { event in eventRow(event) }
                }
                if !model.error.isEmpty {
                    Text(model.error).font(.system(size: 11)).foregroundStyle(Theme.bad)
                }
                if moving == nil || !model.card.tasksSettled {
                    HStack {
                        Text(model.card.tasksSettled ? "That's the day. Carried tasks are on tomorrow's plan."
                             : "One tap each. Tomorrow carries it; Drop forgets it.")
                            .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                        Spacer()
                        Button(model.card.tasksSettled ? "Done for today" : "Skip tonight") {
                            Task { await model.complete(day: day, timezone: timezone.identifier); onChanged() }
                        }
                        .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                    }
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
            .disabled(model.busy)
            .onExitCommand { if moving != nil { closeMove() } }
            // The card may arrive after the Day tab painted; it must never
            // animate the panel's height, so the only motion is the sub-panel's.
            .transaction { $0.animation = nil }
            .animation(.easeInOut(duration: 0.18), value: moving?.id)
            .onChange(of: model.card.events) { _, events in
                // The event left tomorrow's list (reload, cancelled elsewhere): the panel is stale.
                if let moving, !events.contains(where: { $0.id == moving.id }) { closeMove() }
            }
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

    private func taskRow(_ task: CheckInTask) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(task.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(2)
                if let from = task.carriedFrom {
                    Text("Carried from \(from)").font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                }
            }
            Spacer(minLength: 8)
            chip("Done", primary: true) { Task { await model.decide(task, "done", day: day, timezone: timezone.identifier); onChanged() } }
            chip("Tomorrow") { Task { await model.decide(task, "tomorrow", day: day, timezone: timezone.identifier); onChanged() } }
            chip("Drop") { Task { await model.decide(task, "drop", day: day, timezone: timezone.identifier); onChanged() } }
        }
    }

    private func eventRow(_ event: CheckInEvent) -> some View {
        let kept = model.kept.contains(event.id)
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(event.allDay ? "All day" : event.whenTs.map(clock) ?? "")
                .font(.system(size: 11.5).monospacedDigit()).foregroundStyle(Theme.textSecondary)
                .frame(width: 62, alignment: .leading)
            Text(event.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(1)
            Spacer(minLength: 8)
            if kept {
                Text("Kept").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            } else {
                chip("Keep", primary: true) { model.keep(event) }
                if !event.allDay {               // an all-day event has no time to move to
                    chip("Move") { openMove(event) }
                        .help("Pick another time; Wisp finds free ones")
                }
                chip("Cancel") { onAsk(cancelRequest(event)) }
                    .help("Opens Ask with the cancel request; Wisp confirms before anything changes")
            }
        }
    }

    // MARK: Cancel and Move hand off to Ask

    /// The event, named precisely enough for the agent's calendar tools and
    /// unambiguous to a reader: title, day and time, and the calendar id. The
    /// title came from a calendar invite, so it is flattened to one short line
    /// and the instruction never depends on it.
    private func describe(_ event: CheckInEvent) -> String {
        var text = "“\(Self.plainTitle(event.title))”"
        if let when = event.whenTs {
            text += event.allDay ? " on \(dayLabel(when)) (all day)" : " on \(dayLabel(when)) at \(clock(when))"
        }
        let ref = event.sourceId ?? event.id
        if !ref.isEmpty { text += " [calendar event \(ref)]" }
        return text
    }

    static func plainTitle(_ raw: String) -> String {
        let flat = raw.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
            .replacingOccurrences(of: "“", with: "'").replacingOccurrences(of: "”", with: "'")
            .replacingOccurrences(of: "\"", with: "'")
        return flat.count > 80 ? String(flat.prefix(79)) + "…" : flat
    }

    private func cancelRequest(_ event: CheckInEvent) -> String {
        "Cancel this calendar event and let anyone invited know: \(describe(event))"
    }

    private func moveRequest(_ event: CheckInEvent, to start: Double) -> String {
        "Move this calendar event to \(dayLabel(start)) at \(clock(start)), keeping it \(event.durationMinutes) minutes: \(describe(event))"
    }

    private func dayLabel(_ ts: Double) -> String {
        Date(timeIntervalSince1970: ts).formatted(Date.FormatStyle(timeZone: timezone).weekday(.wide).month(.abbreviated).day())
    }

    private func openMove(_ event: CheckInEvent) {
        moving = event
        slots = []
        choosingOther = false
        otherDate = Date(timeIntervalSince1970: (event.whenTs ?? Date().timeIntervalSince1970) + 86400)
        loadingSlots = true
        Task {
            let found = await model.moveSuggestions(for: event, day: model.card.tomorrowDay.isEmpty ? day : model.card.tomorrowDay,
                                                    timezone: timezone.identifier)
            if moving?.id == event.id {
                slots = found
                loadingSlots = false
                choosingOther = found.isEmpty
            }
        }
    }

    private func closeMove() {
        moving = nil
        slots = []
        choosingOther = false
    }

    /// "Move to…": Wisp's suggested times, then another day and time of your own.
    private func movePanel(_ event: CheckInEvent) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Button { closeMove() } label: {
                    Label("Back", systemImage: "chevron.left").labelStyle(.titleAndIcon)
                        .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                }
                .buttonStyle(.plain).help("Back to tomorrow's events (Esc)")
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
                ForEach(slots) { slot in slotRow(event, slot) }
            }
            if choosingOther {
                otherTimeRow(event)
            } else {
                chip("Other time…") { choosingOther = true }
                    .help("Choose a day and time yourself")
            }
            Text("Picking a time opens Ask with the request. Wisp confirms before the calendar changes.")
                .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
            Text("Moving events arrives in the next update; until then Wisp will say so.")
                .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Move \(event.title)")
    }

    private func slotRow(_ event: CheckInEvent, _ slot: MoveSlot) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(slot.label).font(.system(size: 12.5, weight: .medium).monospacedDigit()).foregroundStyle(Theme.textPrimary)
                .frame(width: 150, alignment: .leading)
            Text(slot.reason).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            Spacer(minLength: 8)
            chip("Move here", primary: true) { onAsk(moveRequest(event, to: slot.startTs)); closeMove() }
        }
    }

    private func otherTimeRow(_ event: CheckInEvent) -> some View {
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
            chip("Move here", primary: true) { onAsk(moveRequest(event, to: otherDate.timeIntervalSince1970)); closeMove() }
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
