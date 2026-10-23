import AppKit
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
    private var historyTask: Task<Void, Never>?
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

    /// What to tell the backend while Reminders permission is not yet granted.
    ///
    /// Live failure this exists for: macOS never recorded an answer
    /// (EKAuthorizationStatus.notDetermined) yet the app reported
    /// `syncing: true` every minute, so Reminders read "syncing" forever and
    /// nothing told the person to grant access. "Not decided" is only a wait
    /// while a prompt is plausibly on screen; after that it is a state with a
    /// next step, reported as such.
    enum Access {
        enum Status: Equatable { case notDetermined, authorized, deniedOrRestricted }

        struct Report: Equatable {
            var authorized: Bool
            var syncing: Bool
            var needsRequest: Bool
            var reason: String?
        }

        /// A prompt that has been outstanding this long is not "syncing".
        static let promptGrace: TimeInterval = 120
        /// A finished request that left the status undecided is retried this often, no faster.
        static let retryInterval: TimeInterval = 300

        static let settingsPath = "System Settings > Privacy & Security > Reminders"

        static func report(status: Status, requestOutstanding: Bool,
                           lastRequestAt: TimeInterval?, now: TimeInterval) -> Report {
            switch status {
            case .authorized:
                return Report(authorized: true, syncing: false, needsRequest: false, reason: nil)
            case .deniedOrRestricted:
                return Report(authorized: false, syncing: false, needsRequest: false,
                              reason: "Reminders access is turned off for Wisp. Turn it on in \(settingsPath).")
            case .notDetermined:
                guard let last = lastRequestAt else {
                    return Report(authorized: false, syncing: true, needsRequest: true, reason: nil)
                }
                if requestOutstanding {
                    if now - last < promptGrace {
                        return Report(authorized: false, syncing: true, needsRequest: false, reason: nil)
                    }
                    return Report(authorized: false, syncing: false, needsRequest: false,
                                  reason: "The Reminders permission prompt is still waiting for an answer. "
                                      + "Look for it, or allow Wisp in \(settingsPath).")
                }
                return Report(authorized: false, syncing: false,
                              needsRequest: now - last >= retryInterval,
                              reason: "macOS hasn't recorded an answer about Reminders for Wisp. "
                                  + "Allow it in \(settingsPath); if Wisp isn't listed there, quit and reopen Wisp.")
            }
        }

        /// The diagnostics object the backend reads (`record_sync`, sync_status).
        static func diagnostics(for report: Report, snapshotStartedAt: TimeInterval) -> [String: Any] {
            var out: [String: Any] = ["authorized": report.authorized, "syncing": report.syncing,
                                      "snapshot_started_at": snapshotStartedAt]
            if let reason = report.reason { out["reason"] = reason }
            return out
        }

        static func status(of raw: EKAuthorizationStatus) -> Status {
            if #available(macOS 14.0, *) {
                switch raw {
                case .fullAccess: return .authorized
                case .notDetermined: return .notDetermined
                default: return .deniedOrRestricted
                }
            }
            switch raw {
            case .authorized: return .authorized
            case .notDetermined: return .notDetermined
            default: return .deniedOrRestricted
            }
        }
    }

    // Main-thread state for the permission request (see Access).
    private var accessRequestOutstanding = false
    private var lastAccessRequestAt: TimeInterval?

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

    /// An incomplete reminder in a current list that has NO due date. These are not
    /// commitments (nothing is scheduled), so they ride a separate list and never
    /// enter the time-windowed store; they exist so "what is on my reminders" is
    /// not blind to the reminders a person keeps without a date.
    static func eligibleForUndatedSync(calendarID: String?,
                                       reminderCalendarIDs: Set<String>,
                                       completed: Bool, hasDueDate: Bool) -> Bool {
        guard !hasDueDate else { return false }
        return eligibleForIncompleteSync(calendarID: calendarID,
                                         reminderCalendarIDs: reminderCalendarIDs,
                                         completed: completed, hasDueDate: true)
    }

    /// Bound on how many undated titles one snapshot carries.
    static let maxUndatedReminders = 200

    /// The undated list as it travels: at most `maxUndatedReminders` entries plus the TRUE
    /// number of eligible reminders, so the backend can tell a complete list from one that
    /// was cut at the cap instead of presenting a prefix as everything.
    /// Newest first, so when the cap cuts the list it drops the oldest reminders (the ones
    /// the backend treats as stale anyway) rather than fresh ones. Entries without a
    /// creation date sort last; the backend treats their age as unknown, never as stale.
    static func undatedPayload(_ eligible: [[String: Any]]) -> (items: [[String: Any]], total: Int) {
        let ordered = eligible.enumerated().sorted { a, b in
            let x = a.element["created_ts"] as? Double, y = b.element["created_ts"] as? Double
            switch (x, y) {
            case let (x?, y?) where x != y: return x > y
            case (.some, .none): return true
            case (.none, .some): return false
            default: return a.offset < b.offset
            }
        }.map(\.element)
        return (Array(ordered.prefix(maxUndatedReminders)), eligible.count)
    }

    /// The backend stores an undated title as Python's `title.strip()[:500] or "(untitled)"`
    /// (store.parse_undated_snapshot). Sending the title already in that form, computed here,
    /// makes the backend's step a no-op, so the title it later hands back for a Delete equals
    /// `undatedTitle(item.title)` exactly. Whitespace is Python's `str.isspace` set and the cut
    /// counts Unicode scalars, as Python counts code points; the second trim keeps the result
    /// stable under Python's strip-then-cut.
    static let maxUndatedTitleScalars = 500
    private static let pythonWhitespace: Set<UInt32> = Set(
        [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680,
         0x2028, 0x2029, 0x202F, 0x205F, 0x3000] + Array(0x2000...0x200A))

    static func undatedTitle(_ raw: String?) -> String {
        func trimmed(_ scalars: ArraySlice<Unicode.Scalar>) -> ArraySlice<Unicode.Scalar> {
            var slice = scalars
            while let first = slice.first, pythonWhitespace.contains(first.value) { slice = slice.dropFirst() }
            while let last = slice.last, pythonWhitespace.contains(last.value) { slice = slice.dropLast() }
            return slice
        }
        let once = trimmed(ArraySlice((raw ?? "").unicodeScalars)).prefix(maxUndatedTitleScalars)
        let result = trimmed(once)
        guard !result.isEmpty else { return "(untitled)" }
        var view = String.UnicodeScalarView()
        view.append(contentsOf: result)
        return String(view)
    }

    /// What an exact update/complete/delete targets. A missing expected due time (JSON null)
    /// means "a reminder with no due date", which only a delete may target; any other value
    /// must be a real time. Nothing stands in for null, so a bad or missing number can never
    /// turn a dated target into an undated one.
    enum ExactTarget: Equatable { case dated(Double), allDay(String), undated }

    static func exactTarget(kind: String, expectedDueTs: Double?, expectedDueDate: String = "") -> ExactTarget? {
        guard ["update_reminder", "complete_reminder", "delete_reminder"].contains(kind) else { return nil }
        guard let due = expectedDueTs else { return kind == "delete_reminder" && expectedDueDate.isEmpty ? .undated : nil }
        // Same bound as the backend's verified_reminders._timestamp (before year 10000), so
        // `Int(due / 60)` can never trap on a huge finite value.
        guard due.isFinite && due > 0 && due < 253402300800 else { return nil }
        if !expectedDueDate.isEmpty {
            return allDayComponents(expectedDueDate) != nil ? .allDay(expectedDueDate) : nil
        }
        return .dated(due)
    }

    /// One undated entry as it travels. `created_ts` is EventKit's creationDate when it
    /// has one; the backend ages undated reminders from it to decide when to ask about them.
    static func undatedEntry(id: String, title: String?, list: String, created: Date?) -> [String: Any] {
        var entry: [String: Any] = ["source_id": id, "title": undatedTitle(title), "context": list]
        if let created, created.timeIntervalSince1970.isFinite, created.timeIntervalSince1970 > 0 {
            entry["created_ts"] = created.timeIntervalSince1970
        }
        return entry
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

    static func allDayComponents(_ value: String) -> DateComponents? {
        guard value.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", options: .regularExpression) != nil else { return nil }
        let parts = value.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, parts[0] > 0 else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let fields = DateComponents(year: parts[0], month: parts[1], day: parts[2])
        guard let date = calendar.date(from: fields) else { return nil }
        let roundtrip = calendar.dateComponents([.year, .month, .day], from: date)
        guard roundtrip.year == parts[0], roundtrip.month == parts[1], roundtrip.day == parts[2] else { return nil }
        // No hour, timezone or absolute alarm: this is a floating calendar date.
        return DateComponents(calendar: Calendar(identifier: .gregorian),
                              year: parts[0], month: parts[1], day: parts[2])
    }

    private func allDayDate(_ reminder: EKReminder) -> String? {
        guard let due = reminder.dueDateComponents, due.hour == nil, due.minute == nil,
              due.second == nil, let year = due.year, let month = due.month, let day = due.day else { return nil }
        let value = String(format: "%04d-%02d-%02d", year, month, day)
        return Self.allDayComponents(value) == nil ? nil : value
    }

    private func failure(_ message: String, uncertain: Bool = false) -> [String: Any] {
        ["ok": false, "status": uncertain ? "unknown" : "failed", "error": message]
    }

    /// Execute one exact, claimed native action and return only values read
    /// back from EventKit. A save without matching readback stays uncertain.
    func performVerified(kind: String, actionID: String, sourceID: String = "",
                         expectedTitle: String = "", expectedDueTs: Double? = nil,
                         title: String = "", dueTs: Double = 0,
                         commitmentKind: String = "reminder", dueDate: String = "", expectedDueDate: String = "") -> [String: Any] {
        #if WISP_NOTCH_EVALUATION
        // The contained evaluation build only reads Reminders (start, sync).
        _ = NotchEvaluationSinks.blocked("reminders-write", ledger: NotchEvaluationEnvironment.ledger)
        fatalError("EVALUATION_BLOCKED: the contained evaluation build never writes Reminders")
        #endif
        guard isAuthorized else { return failure("Reminders access is unavailable") }
        guard !actionID.isEmpty, actionID.count <= 128,
              actionID.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || "._:-".contains($0)) })
        else { return failure("Invalid reminder action identity") }

        if kind == "create_reminder" {
            guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  (dueDate.isEmpty ? (dueTs.isFinite && dueTs > 0) : Self.allDayComponents(dueDate) != nil),
                  ["reminder", "assignment", "exam", "meeting", "event"].contains(commitmentKind),
                  let list = store.defaultCalendarForNewReminders() else {
                return failure("Reminder title, due date, or destination is unavailable")
            }
            let item = EKReminder(eventStore: store)
            item.title = title
            item.calendar = list
            item.notes = marker(actionID, kind: commitmentKind)
            if let components = Self.allDayComponents(dueDate) {
                item.dueDateComponents = components
            } else {
                let due = Date(timeIntervalSince1970: dueTs)
                item.dueDateComponents = Calendar.current.dateComponents(
                    [.year, .month, .day, .hour, .minute], from: due)
                item.addAlarm(EKAlarm(absoluteDate: due))
            }
            do { try store.save(item, commit: true) }
            catch { return failure("Native reminder creation outcome is unknown", uncertain: true) }
            let id = item.calendarItemIdentifier
            guard !id.isEmpty, let readback = store.calendarItem(withIdentifier: id) as? EKReminder,
                  readback.notes == marker(actionID, kind: commitmentKind), readback.title == title,
                  (dueDate.isEmpty ? (dueMinute(readback) == Int(dueTs / 60) && allDayDate(readback) == nil)
                                   : (allDayDate(readback) == dueDate && (readback.alarms ?? []).isEmpty)) else {
                return failure("Created reminder could not be read back", uncertain: true)
            }
            if !dueDate.isEmpty {
                return ["ok": true, "status": "succeeded", "error": "", "source_id": id,
                        "title": readback.title ?? "", "all_day": true, "due_date": allDayDate(readback)!]
            }
            return ["ok": true, "status": "succeeded", "error": "", "source_id": id,
                    "title": title, "due_ts": Double(Int(dueTs / 60) * 60)]
        }

        // An undated target (expected due time null) is allowed only for delete_reminder and
        // only while the reminder still has no due date and is still incomplete. Its title is
        // compared in the normalised form the undated sync sent (undatedTitle).
        guard let target = Self.exactTarget(kind: kind, expectedDueTs: expectedDueTs, expectedDueDate: expectedDueDate),
              !sourceID.isEmpty, !expectedTitle.isEmpty
        else { return failure("Invalid exact reminder target") }
        guard let item = store.calendarItem(withIdentifier: sourceID) as? EKReminder else {
            return failure("Exact reminder ID was not found; nothing changed")
        }
        switch target {
        case .undated:
            guard Self.undatedTitle(item.title) == expectedTitle, item.dueDateComponents == nil,
                  !item.isCompleted else {
                return failure("Reminder changed since selection; nothing changed")
            }
        case .allDay(let expectedDate):
            guard item.title == expectedTitle, allDayDate(item) == expectedDate else {
                return failure("Reminder date or mode changed since selection; nothing changed")
            }
        case .dated(let expectedDue):
            guard item.title == expectedTitle, allDayDate(item) == nil,
                  dueMinute(item) == Int(expectedDue / 60) else {
                return failure("Reminder changed since selection; nothing changed")
            }
        }

        if kind == "update_reminder" {
            guard !item.isCompleted else {
                return failure("Reminder was completed since selection; nothing changed")
            }
            guard !title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  (dueDate.isEmpty ? (dueTs.isFinite && dueTs > 0) : Self.allDayComponents(dueDate) != nil)
            else { return failure("Invalid reminder update") }
            item.title = title
            for alarm in item.alarms ?? [] { item.removeAlarm(alarm) }
            if let components = Self.allDayComponents(dueDate) {
                item.dueDateComponents = components
            } else {
                let due = Date(timeIntervalSince1970: dueTs)
                item.dueDateComponents = Calendar.current.dateComponents(
                    [.year, .month, .day, .hour, .minute], from: due)
                item.addAlarm(EKAlarm(absoluteDate: due))
            }
            do { try store.save(item, commit: true) }
            catch { return failure("Native reminder update outcome is unknown", uncertain: true) }
            guard let readback = store.calendarItem(withIdentifier: sourceID) as? EKReminder,
                  !readback.isCompleted, readback.title == title,
                  (dueDate.isEmpty ? (dueMinute(readback) == Int(dueTs / 60) && allDayDate(readback) == nil)
                                   : (allDayDate(readback) == dueDate && (readback.alarms ?? []).isEmpty)) else {
                return failure("Updated reminder could not be read back", uncertain: true)
            }
            if !dueDate.isEmpty {
                return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                        "title": readback.title ?? "", "all_day": true, "due_date": allDayDate(readback)!]
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
                           commitmentKind expectedKind: String = "reminder", dueDate: String = "") async -> [String: Any] {
        #if WISP_NOTCH_EVALUATION
        _ = NotchEvaluationSinks.blocked("reminders-reconcile", ledger: NotchEvaluationEnvironment.ledger)
        fatalError("EVALUATION_BLOCKED: the contained evaluation build never reconciles Reminders writes")
        #endif
        guard isAuthorized else { return failure("Reminders access is unavailable", uncertain: true) }
        if kind == "create_reminder" {
            let found: [EKReminder]? = await withCheckedContinuation { continuation in
                let predicate = store.predicateForReminders(in: nil)
                store.fetchReminders(matching: predicate) { items in continuation.resume(returning: items) }
            }
            guard let found else { return failure("Reminder reconciliation read failed", uncertain: true) }
            let matches = found.filter { hasMarker($0.notes, actionID: actionID) }
            guard matches.count == 1, let item = matches.first,
                  item.title == title,
                  (dueDate.isEmpty ? (dueMinute(item) == Int(dueTs / 60) && allDayDate(item) == nil)
                                   : (allDayDate(item) == dueDate && (item.alarms ?? []).isEmpty)),
                  commitmentKind(item) == expectedKind,
                  !item.calendarItemIdentifier.isEmpty else {
                return failure("Reminder creation remains uncertain", uncertain: true)
            }
            if !dueDate.isEmpty {
                return ["ok": true, "status": "succeeded", "error": "", "source_id": item.calendarItemIdentifier,
                        "title": item.title ?? "", "all_day": true, "due_date": allDayDate(item)!]
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
              (dueDate.isEmpty ? (dueMinute(item) == Int(dueTs / 60) && allDayDate(item) == nil)
                               : (allDayDate(item) == dueDate && (item.alarms ?? []).isEmpty)) else {
            return failure("Reminder update remains uncertain", uncertain: true)
        }
        if !dueDate.isEmpty {
            return ["ok": true, "status": "succeeded", "error": "", "source_id": sourceID,
                    "title": item.title ?? "", "all_day": true, "due_date": allDayDate(item)!]
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

    /// Ask for access and remember that we did, so a request that finishes without
    /// a decision is reported as such instead of as an endless sync. A menu-bar app
    /// is not frontmost, and macOS can leave its prompt hidden behind other
    /// windows, so it is brought forward first. Main thread only.
    private func requestAccessTracked() {
        guard !accessRequestOutstanding else { return }
        accessRequestOutstanding = true
        lastAccessRequestAt = Date().timeIntervalSince1970
        NSApplication.shared.activate(ignoringOtherApps: true)
        requestAccess { [weak self] _ in
            DispatchQueue.main.async {
                self?.accessRequestOutstanding = false
                self?.sync()
            }
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
        DispatchQueue.main.async { [weak self] in self?.requestAccessTracked() }
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
            let report = Access.report(
                status: Access.status(of: EKEventStore.authorizationStatus(for: .reminder)),
                requestOutstanding: accessRequestOutstanding,
                lastRequestAt: lastAccessRequestAt, now: snapshotStartedAt)
            if report.needsRequest { requestAccessTracked() }
            post(reminders: [], diagnostics: Access.diagnostics(for: report, snapshotStartedAt: snapshotStartedAt),
                 startedAt: snapshotStartedAt, authoritative: false)
            return
        }
        // Incomplete reminders WITH a due date become time-windowed commitments (get_upcoming's
        // whole model is time-windowed). Incomplete reminders with NO due date are not
        // commitments; they travel separately as the undated list (see undatedPayload).
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
        startCompletedHistoryScan()
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
                    "when_ts": (self.allDayDate(r) != nil
                        ? (Calendar.current.date(byAdding: .day, value: 1, to: date)?.addingTimeInterval(-60) ?? date)
                        : date).timeIntervalSince1970,
                    "all_day": self.allDayDate(r) != nil,
                    "due_date": self.allDayDate(r) as Any? ?? NSNull(),
                    "location": "",
                ]
            }
            let undated: [[String: Any]] = reminders.compactMap { r in
                guard let calendar = r.calendar,
                      Self.eligibleForUndatedSync(
                          calendarID: calendar.calendarIdentifier,
                          reminderCalendarIDs: reminderCalendarIDs,
                          completed: r.isCompleted,
                          hasDueDate: r.dueDateComponents != nil)
                else { return nil }
                return Self.undatedEntry(id: r.calendarItemIdentifier, title: r.title,
                                         list: calendar.title, created: r.creationDate)
            }
            let undatedPayload = Self.undatedPayload(undated)
            self.post(reminders: payload,
                              diagnostics: ["authorized": true, "count": payload.count,
                                            "snapshot_started_at": snapshotStartedAt],
                              startedAt: snapshotStartedAt, authoritative: true,
                              undated: undatedPayload.items, undatedTotal: undatedPayload.total)
        }
    }

    /// Completed history is sent only to the behavior store. It cannot reopen
    /// reminders, replace the active feed, or create an agenda item.
    private func startCompletedHistoryScan() {
        guard historyTask == nil else { return }
        historyTask = Task { @MainActor [weak self] in
            defer { self?.historyTask = nil }
            guard self?.isAuthorized == true else { return }
            guard let job = await WispClient.behaviorNativeHistoryRequest("GET", "behavior/history/completed_reminders/next"),
                  job["state"] as? String == "scanning",
                  let start = job["page_start"] as? Double,
                  let end = job["page_end"] as? Double else { return }
            let capture: ([[String: Any]]?, String) = await Task.detached(priority: .utility) {
                let source = EKEventStore()
                guard EKEventStore.authorizationStatus(for: .reminder) == .fullAccess,
                      !source.calendars(for: .reminder).isEmpty else { return (nil,"access_unavailable") }
                let predicate = source.predicateForCompletedReminders(
                    withCompletionDateStarting: Date(timeIntervalSince1970: start),
                    ending: Date(timeIntervalSince1970: end), calendars: nil)
                let reminders: [EKReminder]? = await withCheckedContinuation { continuation in
                    source.fetchReminders(matching: predicate) { continuation.resume(returning: $0) }
                }
                guard let reminders,
                      EKEventStore.authorizationStatus(for: .reminder) == .fullAccess else { return (nil,"access_unavailable") }
                guard reminders.count <= 10000 else { return (nil,"row_limit") }
                var result: [[String: Any]] = []
                for r in reminders {
                    guard r.isCompleted, let completed = r.completionDate,
                          !r.calendarItemIdentifier.isEmpty else { return (nil,"invalid_record") }
                    let stamp = completed.timeIntervalSince1970
                    // Native bounds may be inclusive. Backend intervals are half-open.
                    guard stamp >= start && stamp < end else { continue }
                    guard let row = Self.historyCompletionRow(id: r.calendarItemIdentifier,
                        title: r.title ?? "(untitled)", completedAt: stamp, list: r.calendar?.title ?? "") else { return (nil,"invalid_record") }
                    result.append(row)
                }
                return (result,"")
            }.value
            let rows = capture.0
            _ = await WispClient.behaviorNativeHistoryBatch("completed_reminders", job: job, rows: rows ?? [],
                complete: rows != nil, reason: capture.1)
        }
    }

    static func historyCompletionRow(id: String, title: String, completedAt: Double?,
                                      list: String) -> [String: Any]? {
        guard !id.isEmpty, id.count <= 1000, let completedAt, completedAt.isFinite else { return nil }
        return ["source_id": id, "title": String((title.isEmpty ? "(untitled)" : title).prefix(1000)),
                "context": list, "completion_ts": completedAt]
    }

    private func post(reminders: [[String: Any]], diagnostics: [String: Any],
                              startedAt: TimeInterval, authoritative: Bool,
                              undated: [[String: Any]]? = nil, undatedTotal: Int? = nil) {
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  self.snapshotState.accept(
                      startedAt: startedAt,
                      authoritativeCount: authoritative ? reminders.count : nil
                  ) else { return }
            if authoritative { self.consecutiveTransientReports = 0 }
            self.post(reminders: reminders, diagnostics: diagnostics, undated: undated,
                      undatedTotal: undatedTotal)
        }
    }

    // `undated` is sent only with an authoritative read; an unavailable or empty-by-error
    // post omits the key so the backend keeps the last good list.
    private func post(reminders: [[String: Any]], diagnostics: [String: Any],
                      undated: [[String: Any]]? = nil, undatedTotal: Int? = nil) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/calendar")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [
            "source": "reminders", "events": reminders, "diagnostics": diagnostics,
        ]
        if let undated, let undatedTotal {
            body["undated"] = undated
            body["undated_total"] = undatedTotal
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()
    }
}

// The native readers need only the base URL, preserving their isolated contract
// harnesses. Transport follows the same app-wide trusted URLSession protocol.
extension WispClient {
    static func behaviorNativeHistoryRequest(_ method: String, _ path: String,
                                             body: [String: Any]? = nil) async -> [String: Any]? {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 60
        if let body {
            guard let data = try? JSONSerialization.data(withJSONObject: body) else { return nil }
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = data
        }
        guard let (data,response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    static func behaviorNativeHistoryBatch(_ source: String, job: [String: Any], rows: [[String: Any]],
                                          complete: Bool, reason: String) async -> Bool {
        guard ["calendar", "completed_reminders"].contains(source),
              let scanID = job["scan_id"] as? String, let first = job["page_start"],
              let last = job["page_end"] else { return false }
        return await behaviorNativeHistoryRequest("POST", "behavior/history/\(source)/batch", body: [
            "scan_id": scanID, "start_ts": first, "end_ts": last, "rows": rows,
            "complete": complete, "reason": reason]) != nil
    }
}
