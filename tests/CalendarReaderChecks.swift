// Calendar's pure rules: which events are kept, what kind each one is, and what
// an unanswered or refused Calendar permission reports. No EventKit store, no
// Calendar access and no network. Compile with CalendarReader.swift and
// its real siblings (the same list as tests/test_behavior_native_history.py);
// RemindersWriter.swift holds the shared permission rules.
import Foundation

@main struct CalendarReaderChecks {
    static func main() {
        classifyChecks()
        includeChecks()
        accessChecks()
        print("synthetic calendar reader checks passed")
    }

    static func check(_ ok: Bool, _ message: String) { precondition(ok, message) }

    // Whole words only: these titles used to read as exams and assignments.
    static func classifyChecks() {
        typealias P = CalendarEventPolicy
        for title in ["Latest numbers review", "Contest finals viewing", "Lunch with Ashwin",
                      "Southwest flight 221", "Hwy 101 road trip", "Overdue library books chat",
                      "Quizlet study group", "Examine the budget", "Team sync", ""] {
            check(P.classify(title) == "event", "\(title) is an ordinary event")
        }
        for title in ["CS 101 Exam", "Midterm", "midterms week", "Pop QUIZ", "Quiz2",
                      "Bio exam: chapters 3-5", "Two quizzes today", "Exams"] {
            check(P.classify(title) == "exam", "\(title) is an exam")
        }
        for title in ["Essay due", "HW3", "Homework 4", "Assignment: lab report",
                      "Math hw", "Rent DUE", "assignments review", "Problem set 2 (due Friday)"] {
            check(P.classify(title) == "assignment", "\(title) is an assignment")
        }
        // An exam word wins over an assignment word, as before.
        check(P.classify("Exam prep homework") == "exam", "exam beats assignment")
    }

    // Declined and cancelled events are not on the day.
    static func includeChecks() {
        typealias P = CalendarEventPolicy
        let me = P.Attendee(isCurrentUser: true, declined: false)
        let meDeclined = P.Attendee(isCurrentUser: true, declined: true)
        let otherDeclined = P.Attendee(isCurrentUser: false, declined: true)
        check(P.include(cancelled: false, attendees: []), "a plain event is kept")
        check(P.include(cancelled: false, attendees: [me, otherDeclined]),
              "someone else declining does not drop the event")
        check(!P.include(cancelled: false, attendees: [otherDeclined, meDeclined]),
              "an event the person declined is dropped")
        check(!P.include(cancelled: true, attendees: [me]), "a cancelled event is dropped")
    }

    // Calendar uses Reminders' permission rules, with its own name, Settings page
    // and reason_kind.
    static func accessChecks() {
        typealias A = RemindersWriter.Access
        let g = A.promptGrace, r = A.retryInterval
        func report(_ status: A.Status, outstanding: Bool = false, last: TimeInterval?, now: TimeInterval) -> A.Report {
            A.report(status: status, requestOutstanding: outstanding, lastRequestAt: last, now: now, source: .calendar)
        }

        var x = report(.notDetermined, last: nil, now: 1000)
        check(x.syncing && x.needsRequest && x.reasonKind == nil, "first sight asks and waits, with no reason yet")

        x = report(.notDetermined, outstanding: true, last: 1000, now: 1000 + g - 1)
        check(x.syncing && !x.needsRequest && x.reason == nil, "a visible prompt is a real wait")

        x = report(.notDetermined, outstanding: true, last: 1000, now: 1000 + g + 1)
        check(!x.syncing && !x.needsRequest && x.reasonKind == "calendar_access", "an unanswered prompt is not a sync")
        check((x.reason ?? "").contains("Calendar permission prompt"), "it says which prompt")
        check((x.reason ?? "").contains("System Settings > Privacy & Security > Calendars"), "it names the Calendars page")

        x = report(.notDetermined, last: 1000, now: 1010)
        check(!x.syncing && !x.needsRequest && x.reasonKind == "calendar_access", "finished-undecided is terminal")
        x = report(.notDetermined, last: 1000, now: 1000 + r + 1)
        check(!x.syncing && x.needsRequest, "it asks again after the retry interval")

        x = report(.deniedOrRestricted, last: nil, now: 1000)
        check(!x.syncing && !x.needsRequest && !x.authorized, "denied is terminal and never re-asks")
        check(x.reason == "Calendar access is turned off for Wisp. Turn it on in "
              + "System Settings > Privacy & Security > Calendars.", "denied copy")
        check(x.reasonKind == "calendar_access", "denied carries calendar_access")

        x = report(.authorized, last: 1000, now: 1001)
        check(x.authorized && x.reason == nil && x.reasonKind == nil, "authorized needs nothing")

        let diagnostics = A.diagnostics(for: report(.deniedOrRestricted, last: nil, now: 5), snapshotStartedAt: 5)
        check(diagnostics["authorized"] as? Bool == false && diagnostics["syncing"] as? Bool == false,
              "denied diagnostics are unavailable, not syncing")
        check(diagnostics["reason_kind"] as? String == "calendar_access", "diagnostics carry reason_kind")
        check((diagnostics["reason"] as? String)?.isEmpty == false, "diagnostics carry the reason")
        let waiting = A.diagnostics(for: report(.notDetermined, last: nil, now: 5), snapshotStartedAt: 5)
        check(waiting["reason_kind"] == nil && waiting["reason"] == nil, "a real wait carries no reason")
    }
}
