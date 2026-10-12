import Foundation

/// Deterministic source inspector. This does not claim natural-language or workflow20 semantic completion.
public struct WorkflowEngine: Sendable {
    public init() {}
    public func evaluate(_ request: WorkflowRequest, in workspace: Workspace) -> WorkflowResult {
        let family = request.family
        let filter = request.filter.trimmingCharacters(in: .whitespacesAndNewlines)
        let needed: [SourceKind]
        switch family {
        case .overview: needed = [.email, .messages, .calendar]
        case .daily: needed = [.email, .messages, .calendar, .reminders]
        case .schedule, .event, .fuzzyEvent: needed = [.calendar]
        case .priorities: needed = [.reminders, .calendar]
        case .contact, .thread, .unread: needed = [.email, .messages]
        case .groceries: needed = [.notes, .reminders]
        case .weather: needed = [.weather]
        case .knowledge: needed = [.memory, .notes]
        case .pending: needed = [.queue]
        case .create: return clarification(family, ["kind", "title", "date", "timezone"], "Native event/reminder writes are not connected. No item was saved.")
        case .meeting: return clarification(family, ["contact", "start", "duration", "timezone"], "Meeting creation and invitations are not connected. No meeting was created.")
        case .communication, .availability: return clarification(family, ["recipient", "channel", "content", "send_time"], "Draft/send and scheduled delivery are not connected. No message was sent or queued.")
        case .trip: return unavailable(family, "Live location, traffic and Maps handoff are not connected. No trip was scheduled.")
        case .unsubscribe: return unavailable(family, "Mailbox history and provider unsubscribe are not connected. No subscription was changed.")
        case .files: return unavailable(family, "Text imports do not grant directory access. No files were moved.")
        }
        if [.contact, .thread, .event, .fuzzyEvent, .weather].contains(family), filter.isEmpty {
            return clarification(family, [family == .contact ? "contact" : family == .thread ? "thread" : family == .weather ? "city" : "event"], "Enter an explicit search term. No matching scope has been assumed.")
        }
        var result = WorkflowResult(status: .answered, title: family.title)
        if workspace.origin == .synthetic { result.limitations.append("Synthetic fixture data; no live account was read.") }
        else { result.limitations.append("Only user-imported text is available; this is not a full inbox or account summary.") }
        var usable = 0
        for source in needed {
            guard let snapshot = workspace.sources[source] else {
                result.limitations.append("\(source.rawValue): not connected."); continue
            }
            guard snapshot.state == .ready || snapshot.state == .partial else {
                result.limitations.append("\(source.rawValue): \(snapshot.state.rawValue); records excluded."); continue
            }
            usable += 1; result.reads.append(source)
            if !snapshot.complete || snapshot.state == .partial { result.limitations.append("\(source.rawValue): partial coverage.") }
            var records = snapshot.records
            if family == .unread {
                records = records.filter { $0.unread == true }
                if snapshot.records.contains(where: { $0.unread == nil }) { result.limitations.append("\(source.rawValue): unread state is unknown for some records.") }
            }
            if family == .daily || family == .schedule {
                if source == .calendar {
                    var calendar = Calendar(identifier: .gregorian); calendar.timeZone = workspace.timezone
                    let today = calendar.dateInterval(of: .day, for: workspace.clock)!
                    records = records.filter { record in
                        guard let start = record.start else { return false }
                        if let end = record.end, end > start { return start < today.end && end > today.start }
                        return today.contains(start) && start < today.end
                    }
                    if snapshot.records.contains(where: { $0.start == nil }) {
                        result.limitations.append("Calendar records without structured start times are excluded.")
                    }
                    result.limitations.append("Calendar view is today in \(workspace.timezone.identifier); week/month planning is not connected.")
                }
            }
            if !filter.isEmpty {
                // Explicit literal matching avoids pretending a model resolved an ambiguous identity or event.
                records = records.filter { record in
                    let candidate = family == .contact ? record.sender ?? "" : family == .thread ? record.thread ?? "" : record.text
                    return candidate.localizedCaseInsensitiveContains(filter)
                }
            }
            for record in records.prefix(20) {
                result.evidence.append(Evidence(id: record.id, source: source, quote: record.text))
            }
            if records.count > 20 { result.limitations.append("\(source.rawValue): showing the first 20 matches.") }
        }
        if usable == 0 { result.status = .unsupported }
        else if result.evidence.isEmpty {
            result.status = .notCompleted
            result.limitations.append("No matching records in the available scope; this does not establish that no matching information exists.")
        }
        if family == .priorities { result.limitations.append("These are source excerpts, not an inferred priority ranking.") }
        if family == .fuzzyEvent { result.limitations.append("Literal event search only; semantic fuzzy matching is not validated yet.") }
        return result
    }
    private func unavailable(_ family: WorkflowFamily, _ limitation: String) -> WorkflowResult {
        WorkflowResult(status: .unsupported, title: family.title, limitations: [limitation])
    }
    private func clarification(_ family: WorkflowFamily, _ fields: [String], _ limitation: String) -> WorkflowResult {
        WorkflowResult(status: .clarify, title: family.title, limitations: [limitation], clarificationFields: fields)
    }
}

public enum DemoWorkspace {
    public static func make() -> Workspace {
        let clock = ISO8601DateFormatter().date(from: "2026-10-10T09:00:00-07:00")!
        return Workspace(origin: .synthetic, clock: clock, timezone: TimeZone(identifier: "America/Los_Angeles")!, sources: [
            .email: .init(state: .ready, complete: true, records: [.init(id: "demo-mail-1", text: "Design review: bring the prototype to the 2 PM meeting.", sender: "Alex Example", thread: "Design review", unread: true)]),
            .messages: .init(state: .ready, complete: true, records: [.init(id: "demo-message-1", text: "I can make it to the Quad tomorrow at 6 PM.", sender: "Sam Example", thread: "Study group", unread: true)]),
            .calendar: .init(state: .ready, complete: true, records: [.init(id: "demo-event-1", text: "Design review, October 10, 2–3 PM Pacific", start: clock.addingTimeInterval(5*3600), end: clock.addingTimeInterval(6*3600))]),
            .reminders: .init(state: .ready, complete: true, records: [.init(id: "demo-reminder-1", text: "Buy oat milk and apples")]),
            .notes: .init(state: .ready, complete: true, records: [.init(id: "demo-note-1", text: "Shopping list: bread and rice")]),
            .weather: .init(state: .ready, complete: false, records: [.init(id: "demo-weather-1", text: "San Francisco demo forecast: 62°F, cloudy. Synthetic; not current weather.")]),
            .queue: .init(state: .ready, complete: true, records: []),
            .memory: .init(state: .ready, complete: true, records: [.init(id: "demo-memory-1", text: "Project note: the prototype review needs a source-grounded demo.")])
        ])
    }
}
