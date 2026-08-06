import Foundation
import EventKit

// Mirrors Wisp reminders INTO the macOS Reminders app (EKReminder) so they
// also appear on the user's other devices, and reads reminders back OUT —
// items the user created directly in Reminders.app (via Siri, iPhone, typing
// in the app itself) were previously invisible to get_upcoming, since
// CalendarReader only ever reads EKEvent. Separate TCC grant from Calendar;
// the backend can't touch Reminders directly, so writes go over SSE and reads
// get pushed here, same split as Mail/Messages/Calendar.
final class RemindersWriter {
    private let store = EKEventStore()
    private var timer: Timer?

    func requestAccess(_ done: @escaping (Bool) -> Void) {
        if #available(macOS 14.0, *) {
            store.requestFullAccessToReminders { granted, _ in done(granted) }
        } else {
            store.requestAccess(to: .reminder) { granted, _ in done(granted) }
        }
    }

    var isAuthorized: Bool {
        let s = EKEventStore.authorizationStatus(for: .reminder)
        if #available(macOS 14.0, *) { return s == .fullAccess }
        return s == .authorized
    }

    func create(title: String, dueTs: Double) {
        guard isAuthorized, let list = store.defaultCalendarForNewReminders() else { return }
        let r = EKReminder(eventStore: store)
        r.title = title
        r.calendar = list
        let due = Date(timeIntervalSince1970: dueTs)
        r.dueDateComponents = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute], from: due)
        // A concrete alarm so Reminders actually notifies at the due time.
        r.addAlarm(EKAlarm(absoluteDate: due))
        try? store.save(r, commit: true)
    }

    // Request access, then start reading incomplete reminders back into the
    // commitment store (source="reminders" — kept separate from Calendar's
    // "calendar" source since sync_source REPLACES a source's whole active
    // set on each post; mixing the two would let one wipe the other).
    func start() {
        requestAccess { [weak self] _ in
            DispatchQueue.main.async { self?.sync() }
        }
        for delay in [1.0, 3.0, 6.0, 12.0, 25.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.sync()
            }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func sync() {
        guard isAuthorized else {
            post(reminders: [], diagnostics: ["authorized": false])
            return
        }
        // Only incomplete reminders WITH a due date — one with no due date
        // isn't a "commitment" with a time attached, and get_upcoming's whole
        // model is time-windowed.
        let predicate = store.predicateForIncompleteReminders(
            withDueDateStarting: nil, ending: nil, calendars: nil)
        store.fetchReminders(matching: predicate) { [weak self] reminders in
            guard let self else { return }
            let payload: [[String: Any]] = (reminders ?? []).compactMap { r in
                guard let due = r.dueDateComponents, let date = Calendar.current.date(from: due)
                else { return nil }
                return [
                    "source_id": r.calendarItemIdentifier,
                    "kind": "reminder",
                    "title": r.title ?? "(untitled)",
                    "context": r.calendar?.title ?? "",
                    "when_ts": date.timeIntervalSince1970,
                    "all_day": false,
                    "location": "",
                ]
            }
            DispatchQueue.main.async {
                self.post(reminders: payload, diagnostics: ["authorized": true, "count": payload.count])
            }
        }
    }

    private func post(reminders: [[String: Any]], diagnostics: [String: Any]) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/calendar")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "source": "reminders", "events": reminders, "diagnostics": diagnostics,
        ])
        WispSession.shared.dataTask(with: req).resume()
    }
}
