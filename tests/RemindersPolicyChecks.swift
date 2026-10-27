import Foundation

enum WispClient { static let baseURL = URL(string: "http://offline.fixture/")! }

@main struct PolicyCheck {
    static func main() {
        let due = RemindersWriter.allDayComponents("2026-10-20")!
        precondition(due.year == 2026 && due.month == 10 && due.day == 20)
        precondition(due.hour == nil && due.minute == nil && due.second == nil && due.timeZone == nil)
        for invalid in ["2026-02-30", "2026-13-01", "2026-00-20", "2026-10-20T00:00", "0000-01-01"] {
            precondition(RemindersWriter.allDayComponents(invalid) == nil)
        }
        precondition(RemindersWriter.exactTarget(kind: "update_reminder", expectedDueTs: 2e9,
            expectedDueDate: "2026-10-20") == .allDay("2026-10-20"))
        precondition(RemindersWriter.exactTarget(kind: "delete_reminder", expectedDueTs: 2e9,
            expectedDueDate: "2026-02-30") == nil)
        precondition(RemindersWriter.exactTarget(kind: "delete_reminder", expectedDueTs: nil,
            expectedDueDate: "2026-10-20") == nil)
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
        // Newest first when creation dates are known, so the cap drops the oldest
        // (stale) reminders; undated-by-creation entries sort last, in input order.
        let aged: [[String: Any]] = [["source_id": "old", "created_ts": 100.0],
                                     ["source_id": "unknown"],
                                     ["source_id": "new", "created_ts": 900.0],
                                     ["source_id": "mid", "created_ts": 500.0]]
        precondition(RemindersWriter.undatedPayload(aged).items.compactMap { $0["source_id"] as? String }
                     == ["new", "mid", "old", "unknown"], "undated payload sorts newest first")
        let entry = RemindersWriter.undatedEntry(id: "x", title: nil, list: "L",
                                                 created: Date(timeIntervalSince1970: 1234))
        precondition(entry["created_ts"] as? Double == 1234 && entry["title"] as? String == "(untitled)",
                     "an undated entry carries its creation date")
        precondition(RemindersWriter.undatedEntry(id: "x", title: "t", list: "L", created: nil)["created_ts"] == nil,
                     "no creation date is sent as absent, never as zero")
        // Undated titles travel already in the backend's stored form, so a Delete's
        // expected title matches undatedTitle(item.title). The same cases are pinned
        // in tests/test_stale_reminders.py (SWIFT_UNDATED_TITLES), which checks the backend leaves
        // each expected value unchanged.
        let longA = String(repeating: "a", count: 600)
        let titleCases: [(String?, String)] = [
            ("  Buy milk \n", "Buy milk"), ("", "(untitled)"), (nil, "(untitled)"),
            ("\u{3000}\u{1C}Idea\u{2009}", "Idea"), (" \u{85}\u{A0} ", "(untitled)"),
            (longA, String(repeating: "a", count: 500)),
            (String(repeating: "a", count: 499) + " " + String(repeating: "b", count: 100),
             String(repeating: "a", count: 499)),
            (String(repeating: "e\u{301}", count: 300), String(repeating: "e\u{301}", count: 250)),
        ]
        for (raw, expected) in titleCases {
            let got = RemindersWriter.undatedTitle(raw)
            precondition(got == expected, "undatedTitle(\(String(describing: raw))) = \(got)")
            precondition(RemindersWriter.undatedTitle(got) == got || got == "(untitled)",
                         "undatedTitle is idempotent")
        }
        precondition(RemindersWriter.undatedEntry(id: "x", title: "  padded  ", list: "L",
                                                  created: nil)["title"] as? String == "padded",
                     "the undated entry carries the normalised title")
        // An exact target: null means undated, and only a delete may use it; 0, NaN and
        // negative times are invalid rather than stand-ins for null.
        typealias T = RemindersWriter.ExactTarget
        precondition(RemindersWriter.exactTarget(kind: "delete_reminder", expectedDueTs: nil) == T.undated)
        precondition(RemindersWriter.exactTarget(kind: "delete_reminder", expectedDueTs: 2e9) == T.dated(2e9))
        for kind in ["update_reminder", "complete_reminder"] {
            precondition(RemindersWriter.exactTarget(kind: kind, expectedDueTs: nil) == nil,
                         "\(kind) never targets an undated reminder")
        }
        for bad in [0.0, -1.0, Double.nan, Double.infinity, 1e300, 253402300800] {
            precondition(RemindersWriter.exactTarget(kind: "delete_reminder", expectedDueTs: bad) == nil,
                         "\(bad) is not a due time and not a stand-in for undated")
        }
        precondition(RemindersWriter.exactTarget(kind: "create_reminder", expectedDueTs: nil) == nil)
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

        // reason_kind is what the Brief keys its Settings button on: present
        // with every reason, absent while a prompt is a real wait.
        check(diagnostics["reason_kind"] as? String == "reminders_access", "diagnostics reason_kind")
        for status in [A.Status.deniedOrRestricted, .notDetermined] {
            let blocked = A.report(status: status, requestOutstanding: false, lastRequestAt: 1000, now: 1001)
            check(blocked.reasonKind == "reminders_access" && blocked.reason != nil, "every reason has its kind")
        }
        let waiting = A.diagnostics(for: A.report(status: .notDetermined, requestOutstanding: false,
                                                  lastRequestAt: nil, now: 1000), snapshotStartedAt: 5)
        check(waiting["reason_kind"] == nil, "a real wait has no reason_kind")
        let granted = A.diagnostics(for: A.report(status: .authorized, requestOutstanding: false,
                                                  lastRequestAt: 1000, now: 1001), snapshotStartedAt: 5)
        check(granted["reason_kind"] == nil && granted["reason"] == nil, "authorized has neither")
        // Re-asks after a finished, undecided request back off and stop; only
        // the first ask brings Wisp forward.
        check(A.reaskDue(requestsMade: 1, lastRequestAt: 0, now: A.reaskDelays[0]), "first re-ask")
        check(!A.reaskDue(requestsMade: 2, lastRequestAt: 0, now: A.reaskDelays[0]), "second re-ask waits longer")
        check(A.reaskDue(requestsMade: 3, lastRequestAt: 0, now: A.reaskDelays[2]), "third re-ask")
        check(!A.reaskDue(requestsMade: 4, lastRequestAt: 0, now: 1e9), "no fourth re-ask this launch")
        check(!A.reaskDue(requestsMade: 0, lastRequestAt: 0, now: 1e9), "nothing to re-ask before a first ask")
        check(A.shouldActivate(requestsMade: 0) && !A.shouldActivate(requestsMade: 1), "only the first ask activates")
        check(A.report(status: .deniedOrRestricted, requestOutstanding: false, lastRequestAt: nil, now: 1)
              .reason == "Reminders access is turned off for Wisp. Turn it on in "
                + "System Settings > Privacy & Security > Reminders.", "Reminders copy is unchanged")
    }
}
