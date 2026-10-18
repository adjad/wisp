import SwiftUI
import AppKit

/// The evening check-in and the weekly goal pulse on the notch's Day tab
/// (service/assistant/checkin.py). Nothing here needs typing: each planned task
/// takes one tap (Done, Tomorrow, Drop), each of tomorrow's events one tap
/// (Keep, Move, Cancel; Move and Cancel open Ask with the request so the usual
/// draft-and-confirm path decides what is sent), and the goal pulse one tap
/// (On track, Behind, Done).

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
    let allDay: Bool

    init?(_ row: [String: Any], index: Int) {
        guard let title = row["title"] as? String else { return nil }
        id = (row["id"] as? String) ?? "event-\(index)"
        self.title = title
        whenTs = (row["when_ts"] as? NSNumber)?.doubleValue
        allDay = row["all_day"] as? Bool ?? false
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

    var body: some View {
        if model.card.showsCheckIn {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Text("Evening check-in").font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
                    Text(headline).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                }
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(.isHeader)
                if !model.card.tasksSettled {
                    ForEach(model.card.tasks) { task in taskRow(task) }
                } else if !model.card.events.isEmpty {
                    Text(eventsLine).font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                    ForEach(model.card.events) { event in eventRow(event) }
                }
                if !model.error.isEmpty {
                    Text(model.error).font(.system(size: 11)).foregroundStyle(Theme.bad)
                }
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
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
            .disabled(model.busy)
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
                chip("Move") { onAsk("Reschedule “\(event.title)”" + (event.whenTs.map { " (tomorrow \(clock($0)))" } ?? "") + " to ") }
                chip("Cancel") { onAsk("Cancel “\(event.title)”" + (event.whenTs.map { " (tomorrow \(clock($0)))" } ?? "") + " and let people know: ") }
            }
        }
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
