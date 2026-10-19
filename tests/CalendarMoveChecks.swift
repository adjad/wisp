import Foundation

/// Offline checks for CalendarMove (app/Sources/WispApp/CalendarMove.swift): which
/// events may be moved, the new end, and the read-back comparison that decides
/// whether a saved move counts as verified.
@main struct CalendarMoveCheck {
    static func main() {
        let event = CalendarEventFacts(
            title: "Dentist", start: 1_900_000_000, end: 1_900_003_600, isAllDay: false,
            calendarID: "cal-home", timeZone: "America/Los_Angeles", notes: "Bring card",
            url: "https://example.invalid/visit", location: "Main St", structuredLocation: "Main St|37.0,-122.0",
            attendees: [], alarms: ["relative:-900.0"], availability: 0)
        func refusal(_ facts: CalendarEventFacts = event, title: String = "Dentist",
                     start: Double = 1_900_000_000, writable: Bool = true,
                     invitees: Int = 0, expected: Int? = nil, organizer: Bool? = nil,
                     taken: Bool = false) -> String? {
            CalendarMove.refusal(facts, expectedTitle: title, expectedStart: start,
                                 calendarWritable: writable, invitees: invitees,
                                 expectedInvitees: expected ?? invitees, organizerIsUser: organizer,
                                 startTaken: taken)
        }
        precondition(refusal() == nil)
        precondition(refusal(title: "  Dentist ") == nil, "whitespace is how the backend stores titles")
        precondition(refusal(title: "Dentist appt") != nil, "a renamed event is not the one asked about")
        precondition(refusal(start: 1_900_000_060) != nil, "an event that moved since the last read")
        precondition(refusal(writable: false)?.contains("can't be changed") == true)
        // Invitees: the organizer may move it; a non-organizer or an unknown organizer may not,
        // and a count that differs from what the card stated is a changed event.
        precondition(refusal(invitees: 3, organizer: true) == nil, "the organizer moves it; invitees are notified")
        precondition(refusal(invitees: 3, organizer: false)?.contains("Only the organizer") == true)
        precondition(refusal(invitees: 3, organizer: nil)?.contains("who organizes") == true)
        precondition(refusal(invitees: 0, organizer: nil) == nil, "no invitees: no organizer needed")
        precondition(refusal(invitees: 4, expected: 3, organizer: true)?.contains("changed") == true)
        precondition(refusal(taken: true)?.contains("already starts") == true, "the read-back must be unambiguous")
        var allDay = event; allDay.isAllDay = true
        precondition(refusal(allDay) != nil)
        var noStart = event; noStart.start = .nan
        precondition(refusal(noStart) != nil, "an unknown start never matches")

        // Length: kept unless one is asked for.
        precondition(CalendarMove.newEnd(event, newStart: 1_900_086_400, durationMin: nil) == 1_900_090_000)
        precondition(CalendarMove.newEnd(event, newStart: 1_900_086_400, durationMin: 30) == 1_900_088_200)

        // Read-back: exactly the move is verified; any other change is not.
        let newStart = 1_900_086_400.0, newEnd = 1_900_090_000.0
        var moved = event; moved.start = newStart; moved.end = newEnd
        precondition(CalendarMove.mismatch(before: event, after: moved, newStart: newStart, newEnd: newEnd) == nil)
        let ok = CalendarMove.savedResult(before: event, after: moved, newStart: newStart, newEnd: newEnd)
        precondition(ok["ok"] as? Bool == true && ok["when_ts"] as? Double == newStart && ok["end_ts"] as? Double == newEnd)
        precondition(Set(ok.keys) == ["ok", "when_ts", "end_ts"])
        for (field, change) in [("notes", { (f: inout CalendarEventFacts) in f.notes = "" }),
                                ("URL", { $0.url = "<none>" }), ("location", { $0.location = "Elm St" }),
                                ("time zone", { $0.timeZone = "floating" }),
                                ("structured location", { $0.structuredLocation = "" }),
                                ("attendees", { $0.attendees = ["mailto:a@example.invalid"] }),
                                ("alerts", { $0.alarms = [] }), ("calendar", { $0.calendarID = "cal-work" }),
                                ("availability", { $0.availability = 1 }), ("title", { $0.title = "Dentist!" })] {
            var after = moved
            change(&after)
            let result = CalendarMove.savedResult(before: event, after: after, newStart: newStart, newEnd: newEnd)
            precondition(result["ok"] as? Bool == false && result["status"] as? String == "unknown")
            precondition((result["error"] as? String ?? "").contains(field), field)
        }
        var wrongTime = moved; wrongTime.start = newStart + 3600
        precondition(CalendarMove.mismatch(before: event, after: wrongTime, newStart: newStart, newEnd: newEnd)?.contains("start") == true)
        let unread = CalendarMove.savedResult(before: event, after: nil, newStart: newStart, newEnd: newEnd)
        precondition(unread["status"] as? String == "unknown")
        print("CalendarMove: refusal, length and read-back checks passed")
    }
}
