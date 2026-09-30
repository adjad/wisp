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
    private var changeObserver: NSObjectProtocol?
    private var pendingChangeSync: DispatchWorkItem?
    // Snapshot decisions and count are updated together on the main thread.
    // A late EventKit callback cannot replace a newer snapshot's count.
    struct SnapshotState {
        private(set) var latestStartedAt: TimeInterval = -.infinity
        private(set) var lastPostedReminderCount = 0

        mutating func accept(startedAt: TimeInterval, authoritativeCount: Int?) -> Bool {
            guard startedAt > latestStartedAt else { return false }
            latestStartedAt = startedAt
            if let authoritativeCount { lastPostedReminderCount = authoritativeCount }
            return true
        }
    }

    private var snapshotState = SnapshotState()
    private var consecutiveTransientReports = 0
    private static let maxTransientReports = 3

    deinit {
        if let changeObserver { NotificationCenter.default.removeObserver(changeObserver) }
        pendingChangeSync?.cancel()
        timer?.invalidate()
    }

    static func eligibleForIncompleteSync(calendarID: String?,
                                      reminderCalendarIDs: Set<String>,
                                      completed: Bool, hasDueDate: Bool) -> Bool {
        guard let calendarID else { return false }
        return reminderCalendarIDs.contains(calendarID) && !completed && hasDueDate
    }

    /// No reminder lists at all, right after a snapshot that had rows, is more
    /// likely a transient EventKit read than every list being deleted. Report
    /// it as unavailable instead of an authoritative empty set, so missing data
    /// never reads as completion or deletion.
    static func reminderListsLookTransientlyMissing(calendarCount: Int,
                                                    previousRowCount: Int) -> Bool {
        calendarCount == 0 && previousRowCount > 0
    }

    private func scheduleChangeSync() {
        // Reminders.app can emit several store changes for one edit. Coalesce
        // them, then fetch a new complete snapshot instead of reusing objects.
        pendingChangeSync?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.sync() }
        pendingChangeSync = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: work)
    }

    // These actions are called only after the service has persisted an
    // exclusive claim. The action marker lets a later read-only reconciliation
    // find a creation whose reply was lost after EventKit saved it.
    private func marker(_ actionID: String, kind: String) -> String {
        "Wisp action: \(actionID)\nWisp kind: \(kind)"
    }

    private func hasMarker(_ notes: String?, actionID: String) -> Bool {
        notes?.split(separator: "\n").first == "Wisp action: \(actionID)"
    }

    private func commitmentKind(_ reminder: EKReminder) -> String {
        guard reminder.notes?.split(separator: "\n").first?.hasPrefix("Wisp action: ") == true
        else { return "reminder" }
        let candidate = reminder.notes?.split(separator: "\n").first(where: {
            $0.hasPrefix("Wisp kind: ")
        }).map { String($0.dropFirst("Wisp kind: ".count)) }
        guard let candidate,
              ["reminder", "assignment", "exam", "meeting", "event"].contains(candidate)
        else { return "reminder" }
        return candidate
    }

    private func dueMinute(_ reminder: EKReminder) -> Int? {
        guard let components = reminder.dueDateComponents,
              let date = Calendar.current.date(from: components) else { return nil }
        return Int(date.timeIntervalSince1970 / 60)
    }

    private func failure(_ message: String, uncertain: Bool = false) -> [String: Any] {
        ["ok": false, "status": uncertain ? "unknown" : "failed", "error": message]
    }

    /// Execute one exact, claimed native action and return only values read
    /// back from EventKit. A save without matching readback stays uncertain.
    func performVerified(kind: String, actionID: String, sourceID: String = "",
                         expectedTitle: String = "", expectedDueTs: Double = 0,
                         title: String = "", dueTs: Double = 0,
                         commitmentKind: String = "reminder") -> [String: Any] {
        guard isAuthorized else { return failure("Reminders access is unavailable") }
        guard !actionID.isEmpty, actionID.count <= 128,
              actionID.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || "._:-".contains($0)) })
        else { return failure("Invalid reminder action identity") }

        if kind == "create_reminder" {
            guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  dueTs.isFinite, dueTs > 0,
                  ["reminder", "assignment", "exam", "meeting", "event"].contains(commitmentKind),
                  let list = store.defaultCalendarForNewReminders() else {
                return failure("Reminder title, due date, or destination is unavailable")
            }
            let item = EKReminder(eventStore: store)
            item.title = title
            item.calendar = list
            item.notes = marker(actionID, kind: commitmentKind)
            let due = Date(timeIntervalSince1970: dueTs)
            item.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            item.addAlarm(EKAlarm(absoluteDate: due))
            do { try store.save(item, commit: true) }
            catch { return failure("Native reminder creation outcome is unknown", uncertain: true) }
            let id = item.calendarItemIdentifier
            guard !id.isEmpty, let readback = store.calendarItem(withIdentifier: id) as? EKReminder,
                  readback.notes == marker(actionID, kind: commitmentKind), readback.title == title,
                  dueMinute(readback) == Int(dueTs / 60) else {
                return failure("Created reminder could not be read back", uncertain: true)
            }
            return ["ok": true, "status": "succeeded", "error": "", "source_id": id,
                    "title": title, "due_ts": Double(Int(dueTs / 60) * 60)]
        }

        guard ["update_reminder", "complete_reminder", "delete_reminder"].contains(kind),
              !sourceID.isEmpty, expectedDueTs.isFinite, expectedDueTs > 0,
              !expectedTitle.isEmpty else { return failure("Invalid exact reminder target") }
        guard let item = store.calendarItem(withIdentifier: sourceID) as? EKReminder else {
            return failure("Exact reminder ID was not found; nothing changed")
        }
        guard item.title == expectedTitle, dueMinute(item) == Int(expectedDueTs / 60) else {
            return failure("Reminder changed since selection; nothing changed")
        }

        if kind == "update_reminder" {
            guard !item.isCompleted else {
                return failure("Reminder was completed since selection; nothing changed")
            }
            guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  dueTs.isFinite, dueTs > 0 else { return failure("Invalid reminder update") }
            item.title = title
            let due = Date(timeIntervalSince1970: dueTs)
            item.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: due)
            for alarm in item.alarms ?? [] { item.removeAlarm(alarm) }
            item.addAlarm(EKAlarm(absoluteDate: due))
            do { try store.save(item, commit: true) }
            catch { return failure("Native reminder update outcome is unknown", uncertain: true) }
            guard let readback = store.calendarItem(withIdentifier: sourceID) as? EKReminder,
                  !readback.isCompleted, readback.title == title,
                  dueMinute(readback) == Int(dueTs / 60) else {
                return failure("Updated reminder could not be read back", uncertain: true)
            }
            return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                    "title": title, "due_ts": Double(Int(dueTs / 60) * 60)]
        }

        if kind == "complete_reminder" {
            item.isCompleted = true
            do { try store.save(item, commit: true) }
            catch { return failure("Native reminder completion outcome is unknown", uncertain: true) }
            guard let readback = store.calendarItem(withIdentifier: sourceID) as? EKReminder,
                  readback.isCompleted else {
                return failure("Completed reminder could not be read back", uncertain: true)
            }
            return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                    "is_completed": true]
        }

        do { try store.remove(item, commit: true) }
        catch { return failure("Native reminder deletion outcome is unknown", uncertain: true) }
        guard store.calendarItem(withIdentifier: sourceID) == nil else {
            return failure("Deleted reminder remains present", uncertain: true)
        }
        return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                "is_absent": true]
    }

    /// Read-only recovery for a claimed write whose native reply was lost.
    /// Creation searches the unique action marker; every other operation uses
    /// the exact EventKit ID. No negative search result authorizes a retry.
    func reconcileVerified(kind: String, actionID: String, sourceID: String = "",
                           title: String = "", dueTs: Double = 0,
                           commitmentKind expectedKind: String = "reminder") async -> [String: Any] {
        guard isAuthorized else { return failure("Reminders access is unavailable", uncertain: true) }
        if kind == "create_reminder" {
            let found: [EKReminder]? = await withCheckedContinuation { continuation in
                let predicate = store.predicateForReminders(in: nil)
                store.fetchReminders(matching: predicate) { items in continuation.resume(returning: items) }
            }
            guard let found else { return failure("Reminder reconciliation read failed", uncertain: true) }
            let matches = found.filter { hasMarker($0.notes, actionID: actionID) }
            guard matches.count == 1, let item = matches.first,
                  item.title == title, dueMinute(item) == Int(dueTs / 60),
                  commitmentKind(item) == expectedKind,
                  !item.calendarItemIdentifier.isEmpty else {
                return failure("Reminder creation remains uncertain", uncertain: true)
            }
            return ["ok": true, "status": "succeeded", "error": "",
                    "source_id": item.calendarItemIdentifier, "title": title,
                    "due_ts": Double(Int(dueTs / 60) * 60)]
        }
        guard !sourceID.isEmpty else { return failure("Exact reminder ID unavailable", uncertain: true) }
        let item = store.calendarItem(withIdentifier: sourceID) as? EKReminder
        if kind == "delete_reminder" {
            // After a crash, an absent ID could also mean EventKit changed its
            // identifier. Only the immediate remove + readback can verify it.
            return failure("Reminder deletion remains uncertain", uncertain: true)
        }
        guard let item else { return failure("Reminder readback unavailable", uncertain: true) }
        if kind == "complete_reminder" {
            guard item.isCompleted else { return failure("Reminder completion remains uncertain", uncertain: true) }
            return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                    "is_completed": true]
        }
        guard kind == "update_reminder", !item.isCompleted, item.title == title,
              dueMinute(item) == Int(dueTs / 60) else {
            return failure("Reminder update remains uncertain", uncertain: true)
        }
        return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                "title": title, "due_ts": Double(Int(dueTs / 60) * 60)]
    }

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

    // Request access, then start reading incomplete reminders back into the
    // commitment store (source="reminders" — kept separate from Calendar's
    // "calendar" source since sync_source REPLACES a source's whole active
    // set on each post; mixing the two would let one wipe the other).
    func start() {
        changeObserver = NotificationCenter.default.addObserver(
            forName: .EKEventStoreChanged, object: store, queue: .main
        ) { [weak self] _ in self?.scheduleChangeSync() }
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
        // The list buffer and snapshot fence share main-thread state, including
        // when an on-demand backend request arrives on a background queue.
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.sync() }
            return
        }
        // Capture before the asynchronous fetch: receipt order is not snapshot order.
        let snapshotStartedAt = Date().timeIntervalSince1970
        guard isAuthorized else {
            post(reminders: [], diagnostics: ["authorized": false,
                 "snapshot_started_at": snapshotStartedAt,
                 "syncing": EKEventStore.authorizationStatus(for: .reminder) == .notDetermined],
                 startedAt: snapshotStartedAt, authoritative: false)
            return
        }
        // Only incomplete reminders WITH a due date — one with no due date
        // isn't a "commitment" with a time attached, and get_upcoming's whole
        // model is time-windowed.
        // Restrict the query to calendars EventKit currently reports for the
        // reminder entity, then check membership again after the async fetch.
        // This does not classify Recently Deleted rows that retain an original
        // calendar ID; EventKit documents no deleted-state field here.
        // A partial read is reported as unavailable, never as an empty set.
        let postUnavailable: (String) -> Void = { [weak self] reason in
            self?.post(reminders: [], diagnostics: ["authorized": true, "available": false,
                 "snapshot_started_at": snapshotStartedAt, "reason": reason],
                 startedAt: snapshotStartedAt, authoritative: false)
        }
        let calendars = store.calendars(for: .reminder)
        if Self.reminderListsLookTransientlyMissing(
            calendarCount: calendars.count,
            previousRowCount: snapshotState.lastPostedReminderCount),
           consecutiveTransientReports < Self.maxTransientReports {
            consecutiveTransientReports += 1
            postUnavailable("EventKit reported no reminder lists")
            return
        }
        // Persistent zero lists (sign-out, every list deleted) become authoritative
        // only after their fetch succeeds. Keep the previous count while it is pending.
        if !calendars.isEmpty { consecutiveTransientReports = 0 }
        let reminderCalendarIDs = Set(calendars.map(\.calendarIdentifier))
        let predicate = store.predicateForIncompleteReminders(
            withDueDateStarting: nil, ending: nil, calendars: calendars)
        store.fetchReminders(matching: predicate) { [weak self] reminders in
            guard let self else { return }
            guard let reminders else {
                postUnavailable("The Reminders store did not return a result")
                return
            }
            let payload: [[String: Any]] = reminders.compactMap { r in
                guard let calendar = r.calendar,
                      Self.eligibleForIncompleteSync(
                          calendarID: calendar.calendarIdentifier,
                          reminderCalendarIDs: reminderCalendarIDs,
                          completed: r.isCompleted,
                          hasDueDate: r.dueDateComponents != nil),
                      let due = r.dueDateComponents,
                      let date = Calendar.current.date(from: due)
                else { return nil }
                return [
                    "source_id": r.calendarItemIdentifier,
                    "kind": self.commitmentKind(r),
                    "title": r.title ?? "(untitled)",
                    "context": calendar.title,
                    "when_ts": date.timeIntervalSince1970,
                    "all_day": false,
                    "location": "",
                ]
            }
            self.post(reminders: payload,
                              diagnostics: ["authorized": true, "count": payload.count,
                                            "snapshot_started_at": snapshotStartedAt],
                              startedAt: snapshotStartedAt, authoritative: true)
        }
    }

    private func post(reminders: [[String: Any]], diagnostics: [String: Any],
                              startedAt: TimeInterval, authoritative: Bool) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.snapshotState.accept(
                      startedAt: startedAt,
                      authoritativeCount: authoritative ? reminders.count : nil
                  ) else { return }
            if authoritative { self.consecutiveTransientReports = 0 }
            self.post(reminders: reminders, diagnostics: diagnostics)
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
        URLSession.shared.dataTask(with: req).resume()
    }
}
