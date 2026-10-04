import Foundation

enum WispClient { static let baseURL = URL(string: "http://offline.fixture/")! }

@main struct PolicyCheck {
    static func main() {
        let active: Set<String> = ["active-list"]
        precondition(RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "deleted-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: nil, reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: true, hasDueDate: true))
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: false))
        // A list removed from the published set disappears, then a restored
        // list can appear again without a title-based exception.
        precondition(!RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: [],
            completed: false, hasDueDate: true))
        precondition(RemindersWriter.eligibleForIncompleteSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true))
        // Undated reminders: only incomplete, undated rows in a current list.
        precondition(RemindersWriter.eligibleForUndatedSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: false))
        precondition(!RemindersWriter.eligibleForUndatedSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: true), "a dated reminder is not undated")
        precondition(!RemindersWriter.eligibleForUndatedSync(
            calendarID: "active-list", reminderCalendarIDs: active,
            completed: true, hasDueDate: false), "a completed reminder is never listed")
        precondition(!RemindersWriter.eligibleForUndatedSync(
            calendarID: "deleted-list", reminderCalendarIDs: active,
            completed: false, hasDueDate: false), "a removed list's rows are not listed")
        precondition(!RemindersWriter.eligibleForUndatedSync(
            calendarID: nil, reminderCalendarIDs: active,
            completed: false, hasDueDate: false))
        // The undated payload carries at most the cap plus the TRUE eligible count.
        func rows(_ n: Int) -> [[String: Any]] { (0..<n).map { ["source_id": "id\($0)", "title": "t\($0)"] } }
        for (n, sent) in [(0, 0), (1, 1), (199, 199), (200, 200), (201, 200), (500, 200)] {
            let p = RemindersWriter.undatedPayload(rows(n))
            precondition(p.items.count == sent && p.total == n,
                         "undated payload for \(n) eligible must send \(sent) and report \(n)")
        }
        precondition(RemindersWriter.undatedPayload(rows(500)).items.first?["source_id"] as? String == "id0",
                     "the cut keeps the first entries in order")
        let wire = try! JSONSerialization.data(withJSONObject: [
            "undated": RemindersWriter.undatedPayload(rows(2)).items,
            "undated_total": RemindersWriter.undatedPayload(rows(2)).total] as [String: Any])
        let decoded = try! JSONSerialization.jsonObject(with: wire) as! [String: Any]
        precondition((decoded["undated_total"] as? Int) == 2 && (decoded["undated"] as? [Any])?.count == 2,
                     "the payload must survive JSON serialisation")
        // An empty list set after a snapshot with rows is not authoritative.
        precondition(RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 0, previousRowCount: 3))
        precondition(!RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 0, previousRowCount: 0))
        precondition(!RemindersWriter.reminderListsLookTransientlyMissing(
            calendarCount: 2, previousRowCount: 3))
        accessChecks()
        print("synthetic native policy checks passed")
    }

    // Reminders access: a permission nobody has answered must stop reading as "syncing".
    static func accessChecks() {
        typealias A = RemindersWriter.Access
        func check(_ ok: Bool, _ message: String) { precondition(ok, message) }
        let g = A.promptGrace, r = A.retryInterval

        // Never asked: ask now and report a short wait.
        var report = A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: nil, now: 1000)
        check(report.syncing && report.needsRequest && !report.authorized, "first sight asks and waits")

        // Prompt up, inside the grace window: a genuine wait. Never stack a second request.
        report = A.report(status: .notDetermined, requestOutstanding: true, lastRequestAt: 1000, now: 1000 + g - 1)
        check(report.syncing && !report.needsRequest, "a visible prompt is a real wait")

        // Prompt outstanding far too long: stop claiming progress, say where to answer it.
        report = A.report(status: .notDetermined, requestOutstanding: true, lastRequestAt: 1000, now: 1000 + g + 1)
        check(!report.syncing && !report.needsRequest, "an unanswered prompt is not a sync")
        check((report.reason ?? "").contains("System Settings"), "it names where to answer")

        // The request FINISHED but macOS still has no answer (the live failure): terminal, retry later only.
        report = A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: 1000, now: 1000 + 10)
        check(!report.syncing && !report.needsRequest, "finished-undecided is terminal and does not hammer")
        check((report.reason ?? "").contains("Privacy & Security"), "it explains the next step")
        report = A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: 1000, now: 1000 + r + 1)
        check(!report.syncing && report.needsRequest, "it retries after the retry interval")

        // Denied or restricted is terminal immediately and never re-asks.
        report = A.report(status: .deniedOrRestricted, requestOutstanding: false, lastRequestAt: 1000, now: 1001)
        check(!report.syncing && !report.needsRequest && !report.authorized, "denied is terminal")
        check((report.reason ?? "").contains("Reminders"), "denied says what is off")

        // Authorized needs nothing.
        report = A.report(status: .authorized, requestOutstanding: false, lastRequestAt: 1000, now: 1001)
        check(report.authorized && !report.syncing && !report.needsRequest, "authorized proceeds")

        // The diagnostics dictionary the backend reads.
        let diagnostics = A.diagnostics(for: A.report(status: .notDetermined, requestOutstanding: false,
                                                      lastRequestAt: 1000, now: 1010), snapshotStartedAt: 5)
        check(diagnostics["authorized"] as? Bool == false, "diagnostics authorized")
        check(diagnostics["syncing"] as? Bool == false, "diagnostics syncing")
        check((diagnostics["reason"] as? String)?.isEmpty == false, "diagnostics reason")
        check(diagnostics["snapshot_started_at"] as? Double == 5, "diagnostics snapshot")
    }
}
