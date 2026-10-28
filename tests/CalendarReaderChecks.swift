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
        paceChecks()
        print("synthetic calendar reader checks passed")
    }

    static func check(_ ok: Bool, _ message: String) { precondition(ok, message) }

    // Whole words only: these titles used to read as exams and assignments.
    static func classifyChecks() {
        typealias P = CalendarEventPolicy
        for title in ["Latest numbers review", "Contest viewing", "Lunch with Ashwin",
                      "Southwest flight 221", "Hwy 101 road trip", "Overdue library books chat",
                      "Quizlet study group", "Examine the budget", "Team sync", "Finalize the slides",
                      "Testing the new oven", "Collaboration chat", "Paperwork pickup", "Problem solving club",
                      "Set the table", ""] {
            check(P.classify(title) == "event", "\(title) is an ordinary event")
        }
        for title in ["CS 101 Exam", "Midterm", "midterms week", "Pop QUIZ", "Quiz2",
                      "Bio exam: chapters 3-5", "Two quizzes today", "Exams", "CS 161 Final",
                      "Chem test", "Finals week", "Driving test"] {
            check(P.classify(title) == "exam", "\(title) is an exam")
        }
        for title in ["Essay due", "HW3", "Homework 4", "Assignment: lab report",
                      "Math hw", "Rent DUE", "assignments review", "Problem set 2 (due Friday)",
                      "Project deadline", "Term paper", "Problem set 3", "Problem Sets", "Chem lab",
                      "Lab3", "pset 4", "Pset4", "History essay", "Grant submission", "Deadline"] {
            check(P.classify(title) == "assignment", "\(title) is an assignment")
        }
        // An exam word wins over an assignment word, as before.
        check(P.classify("Exam prep homework") == "exam", "exam beats assignment")
        check(P.classify("Final project") == "exam", "an exam word beats an assignment word, as before")
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
        check(!P.include(cancelled: true, attendees: []), "a cancelled event with no attendees is dropped")
        // The person organizes it: EventKit lists them as an accepted attendee.
        // Every invitee declining doesn't take it off the organizer's day.
        let organizer = P.Attendee(isCurrentUser: true, declined: false)
        check(P.include(cancelled: false, attendees: [organizer, otherDeclined, otherDeclined]),
              "an organizer's event stays when the invitees decline")
        check(!P.include(cancelled: true, attendees: [organizer]), "an organizer's cancelled event is dropped")
    }

    // Calendar is read when EventKit says it changed and on a heartbeat. The heartbeat
    // has to beat the backend's freshness windows or the planner calls Calendar
    // "not current" between reads (service/assistant/today.py source_health: 180 s;
    // the behavior receipt in service/assistant/scheduler.py: 1200 s).
    static func paceChecks() {
        typealias P = NativeSyncPace
        check(P.heartbeat == 150, "the heartbeat is two and a half minutes")
        check(P.heartbeat + 30 <= 180, "a heartbeat plus its read and post stays inside the planner's 180 s window")
        check(P.heartbeat < 1200, "and inside the behavior receipt's 1200 s window")
        check(P.heartbeat > 60, "it is slower than the old one-minute timer")
        check(P.heartbeatInterval(authorized: true) == P.heartbeat, "with access Calendar waits the heartbeat")
        check(P.heartbeatInterval(authorized: false) == 60,
              "without access the cheap status post keeps its one-minute pace, so a new grant is seen")
        // A change waits for the burst to settle, and reads are at least a minute apart.
        check(P.changeDelay(lastSyncStartedAt: nil, now: 1000) == P.changeDebounce, "the first change waits only the debounce")
        check(P.changeDelay(lastSyncStartedAt: 100, now: 1000) == P.changeDebounce, "a long-quiet store reads after the debounce")
        check(P.changeDelay(lastSyncStartedAt: 1000, now: 1000) == P.changeFloor, "a read that just began holds the next one a minute")
        check(P.changeDelay(lastSyncStartedAt: 1000, now: 1010) == P.changeFloor - 10, "the floor counts down")
        check(P.changeDelay(lastSyncStartedAt: 1000, now: 1000 + P.changeFloor - 1) == P.changeDebounce,
              "the debounce is the least wait")
        check(P.changeDelay(lastSyncStartedAt: 2000, now: 1000) == P.changeDebounce,
              "a clock that moved back does not stall reads")
        check(P.changeDebounce > 0 && P.changeFloor >= 60, "never faster than the old timer")
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
        // Re-asks back off and stop: about three per launch, with growing waits.
        let waits = A.reaskDelays
        check(waits.count == 3 && waits[0] < waits[1] && waits[1] < waits[2], "growing, capped re-asks")
        for (made, wait) in zip(1..., waits) {
            check(!A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: 1000,
                            now: 1000 + wait - 1, source: .calendar, requestsMade: made).needsRequest,
                  "re-ask \(made) waits its full interval")
            check(A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: 1000,
                           now: 1000 + wait, source: .calendar, requestsMade: made).needsRequest,
                  "re-ask \(made) is due after its interval")
        }
        let spent = A.report(status: .notDetermined, requestOutstanding: false, lastRequestAt: 1000,
                             now: 1000 + 86400 * 30, source: .calendar, requestsMade: waits.count + 1)
        check(!spent.needsRequest && spent.reasonKind == "calendar_access" && !spent.syncing,
              "after the last re-ask Wisp stops asking and the reason stays")
        check(A.shouldActivate(requestsMade: 0), "the first ask brings Wisp forward")
        for made in 1...5 { check(!A.shouldActivate(requestsMade: made), "a re-ask never takes focus") }

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
