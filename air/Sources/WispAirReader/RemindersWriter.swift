import Foundation
import EventKit

// Creates EKReminders for the to-dos the periodic runs extract.
//
// This is the piece that makes the whole architecture cheap: an EKReminder
// written here syncs through iCloud to the Pro *and* the user's iPhone for
// free. There is deliberately NO custom to-do sync protocol between the
// machines — only the text summaries travel over the network, because they
// have nowhere else to live.
//
// Ported from the Pro's RemindersWriter, reduced to create-only. The Pro also
// reads reminders back to feed its commitment store; that job stays on the Pro,
// and duplicating it here would mean two machines writing the same store.
final class RemindersWriter {
    private let store = EKEventStore()
    private var timer: Timer?

    // No pre-macOS-14 fallback: this package targets macOS 14+, and the Air is
    // on a far newer OS than that. The Pro's copy still carries the old
    // `requestAccess(to:)` branch, which is dead code here.
    func requestAccess(_ done: @escaping (Bool) -> Void) {
        store.requestFullAccessToReminders { granted, _ in done(granted) }
    }

    var isAuthorized: Bool {
        EKEventStore.authorizationStatus(for: .reminder) == .fullAccess
    }

    func start() {
        requestAccess { granted in
            if !granted {
                NSLog("[WispAirReader] Reminders access NOT granted — to-dos will queue unwritten")
            }
        }
        // Polls a durable queue rather than subscribing to a push stream. The
        // Pro uses SSE for this, which has to handle reconnects and missed
        // events; here a to-do can perfectly well wait 30 seconds, and if this
        // app is down or unauthorized the queue simply doesn't drain — nothing
        // is lost, and it drains on its own once the grant arrives.
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in
                self?.drain()
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in self?.drain() }
    }

    func drain() {
        guard isAuthorized else { return }
        Config.get("reminders/pending") { [weak self] json in
            guard let self,
                  let list = json?["reminders"] as? [[String: Any]], !list.isEmpty
            else { return }

            var created: [Int] = []
            for item in list {
                guard let id = item["id"] as? Int,
                      let title = item["title"] as? String else { continue }
                let due = item["due_ts"] as? Double
                if self.create(title: title, dueTs: due) { created.append(id) }
            }
            // Ack ONLY what actually saved. An unacked row stays queued and is
            // retried next poll; the service's hash ledger is separate and
            // permanent, so a retry can never produce a duplicate reminder.
            if !created.isEmpty {
                Config.post("reminders/ack", body: ["ids": created])
                NSLog("[WispAirReader] created \(created.count) reminder(s)")
            }
        }
    }

    @discardableResult
    func create(title: String, dueTs: Double?) -> Bool {
        guard isAuthorized, let list = store.defaultCalendarForNewReminders() else { return false }
        let r = EKReminder(eventStore: store)
        r.title = title
        r.calendar = list
        if let dueTs {
            let due = Date(timeIntervalSince1970: dueTs)
            r.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            // A concrete alarm, so Reminders actually notifies at the due time
            // rather than only showing up in a list.
            r.addAlarm(EKAlarm(absoluteDate: due))
        }
        do {
            try store.save(r, commit: true)
            return true
        } catch {
            NSLog("[WispAirReader] failed to save reminder '\(title)': \(error.localizedDescription)")
            return false
        }
    }
}
