import Foundation

/// The rules for moving one Calendar event, kept free of EventKit so they can be
/// checked without Calendar access (tests/CalendarMoveChecks.swift).
/// CalendarReader.moveEvent gathers these facts from the EKEvent before and after
/// the save and applies them.
///
/// A move changes only the start and end of one exact occurrence. Everything else
/// the user put on the event must read back unchanged, or the move is reported as
/// not verified instead of done.
struct CalendarEventFacts: Equatable {
    var title: String
    var start: Double
    var end: Double
    var isAllDay: Bool
    var calendarID: String
    var notes: String
    var url: String
    var location: String
    /// Participant URLs, sorted, so order never reads as a change.
    var attendees: [String]
    /// Each alarm as "relative:<seconds>" or "absolute:<ts>", sorted.
    var alarms: [String]
    var availability: Int
}

enum CalendarMove {
    /// Titles compared as the backend stores them: whitespace-collapsed.
    static func sameTitle(_ a: String, _ b: String) -> Bool {
        let fold = { (s: String) in s.split(whereSeparator: \.isWhitespace).joined(separator: " ") }
        return fold(a) == fold(b)
    }

    /// Why this event must not be moved, or nil when it may. Checked before any write.
    static func refusal(_ facts: CalendarEventFacts, expectedTitle: String, expectedStart: Double,
                        calendarWritable: Bool, hasAttendees: Bool, organizerIsUser: Bool) -> String? {
        if !(abs(facts.start - expectedStart) < 1) || !sameTitle(facts.title, expectedTitle) {
            return "That event changed since Wisp last read Calendar; nothing was moved"
        }
        if !calendarWritable { return "That event's calendar can't be changed from this Mac" }
        if hasAttendees && !organizerIsUser {
            return "Only the organizer can move an event with invitees; ask them or move it in Calendar"
        }
        if facts.isAllDay { return "Wisp doesn't move all-day events; move it in Calendar" }
        return nil
    }

    /// The new end: the requested length, or the event's own length when none is given.
    static func newEnd(_ facts: CalendarEventFacts, newStart: Double, durationMin: Int?) -> Double {
        if let minutes = durationMin { return newStart + Double(minutes) * 60 }
        let length = facts.end - facts.start
        return newStart + (length.isFinite ? max(0, length) : 0)
    }

    /// Nil when the read-back shows exactly the move: the new start and end, and every
    /// other field as it was. Otherwise the fields that differ.
    static func mismatch(before: CalendarEventFacts, after: CalendarEventFacts,
                         newStart: Double, newEnd: Double) -> String? {
        var changed: [String] = []
        if !(abs(after.start - newStart) < 1) { changed.append("start") }
        if !(abs(after.end - newEnd) < 1) { changed.append("end") }
        var expected = before
        expected.start = after.start
        expected.end = after.end
        let pairs: [(String, Bool)] = [
            ("title", expected.title == after.title), ("all-day", expected.isAllDay == after.isAllDay),
            ("calendar", expected.calendarID == after.calendarID), ("notes", expected.notes == after.notes),
            ("URL", expected.url == after.url), ("location", expected.location == after.location),
            ("attendees", expected.attendees == after.attendees), ("alerts", expected.alarms == after.alarms),
            ("availability", expected.availability == after.availability),
        ]
        changed += pairs.filter { !$0.1 }.map { $0.0 }
        return changed.isEmpty ? nil : changed.joined(separator: ", ")
    }

    /// The native result for a save that EventKit accepted, from its read-back.
    static func savedResult(before: CalendarEventFacts, after: CalendarEventFacts?,
                            newStart: Double, newEnd: Double) -> [String: Any] {
        guard let after else {
            return ["ok": false, "status": "unknown",
                    "error": "Calendar accepted the move but Wisp couldn't read the event back; the outcome is unknown. Check Calendar before trying again"]
        }
        if let fields = mismatch(before: before, after: after, newStart: newStart, newEnd: newEnd) {
            return ["ok": false, "status": "unknown",
                    "error": "Calendar saved the move but its read-back differs (\(fields)); the outcome is unknown. Check Calendar before trying again"]
        }
        return ["ok": true, "when_ts": after.start, "end_ts": after.end]
    }
}
