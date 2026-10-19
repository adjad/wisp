import Foundation

/// The rules for moving one Calendar event, kept free of EventKit so they can be
/// checked without Calendar access (tests/CalendarMoveChecks.swift).
/// CalendarReader.moveEvent gathers these facts from the EKEvent before and after
/// the save and applies them.
///
/// A move changes only the start and end of one exact occurrence. The fields below
/// (title, all-day, calendar, time zone, notes, URL, location, structured location,
/// attendees, alerts, availability) must read back unchanged, or the move is reported
/// as not verified instead of done.
struct CalendarEventFacts: Equatable {
    var title: String
    var start: Double
    var end: Double
    var isAllDay: Bool
    var calendarID: String
    /// The event's time zone identifier, or "floating".
    var timeZone: String
    var notes: String
    /// The URL, or "<none>" so a missing URL never reads the same as an empty one.
    var url: String
    var location: String
    /// The structured location's title and coordinates, or "".
    var structuredLocation: String
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
    /// `startTaken`: another occurrence of the same event already begins at the new start
    /// (a repeating series), so the read-back could not tell the two apart.
    /// `invitees` is the event's attendee count now and `expectedInvitees` the count the
    /// confirmation card stated ("its N invitees will be notified"); `organizerIsUser` is
    /// nil when Calendar doesn't say who organizes it.
    static func refusal(_ facts: CalendarEventFacts, expectedTitle: String, expectedStart: Double,
                        calendarWritable: Bool, invitees: Int, expectedInvitees: Int,
                        organizerIsUser: Bool?, startTaken: Bool) -> String? {
        if !(abs(facts.start - expectedStart) < 1) || !sameTitle(facts.title, expectedTitle)
            || invitees != expectedInvitees {
            return "That event changed since Wisp last read Calendar; nothing was moved"
        }
        if !calendarWritable { return "That event's calendar can't be changed from this Mac" }
        // Someone else's event is never moved, even when its attendee list shows only the
        // user (Exchange 1:1 invitations), so this check doesn't depend on the count. With
        // invitees, an unknown organizer is refused too, never assumed.
        if organizerIsUser == false {
            return "Only the organizer can move this event; ask them or propose a new time in Calendar"
        }
        if invitees > 0 && organizerIsUser == nil {
            return "Calendar doesn't say who organizes this event, so Wisp won't move it; move it in Calendar"
        }
        if startTaken { return "Another occurrence of this event already starts then; nothing was moved" }
        if facts.isAllDay { return "Wisp doesn't move all-day events; move it in Calendar" }
        return nil
    }

    /// Why this occurrence must not be cancelled, or nil when it may. The same identity
    /// and organizer rules as a move: it must still be the event the card showed, on a
    /// writable calendar, and an event with invitees is cancelled only by its organizer
    /// (Calendar then tells them). All-day events may be cancelled.
    /// Calendar's own deep link to an event: ical://ekevent/<calendarItemIdentifier>?method=show&options=more.
    /// nil for an empty identifier. The identifier is one percent-encoded path segment, so a
    /// slash, question mark or hash inside it can't change the URL's shape.
    static func showURL(calendarItemIdentifier: String) -> URL? {
        guard !calendarItemIdentifier.isEmpty else { return nil }
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#;")
        guard let segment = calendarItemIdentifier.addingPercentEncoding(withAllowedCharacters: allowed) else { return nil }
        var parts = URLComponents()
        parts.scheme = "ical"
        parts.host = "ekevent"
        parts.percentEncodedPath = "/" + segment
        parts.queryItems = [URLQueryItem(name: "method", value: "show"), URLQueryItem(name: "options", value: "more")]
        return parts.url
    }

    static func cancelRefusal(_ facts: CalendarEventFacts, expectedTitle: String, expectedStart: Double,
                              calendarWritable: Bool, invitees: Int, expectedInvitees: Int,
                              organizerIsUser: Bool?) -> String? {
        if !(abs(facts.start - expectedStart) < 1) || !sameTitle(facts.title, expectedTitle)
            || invitees != expectedInvitees {
            return "That event changed since Wisp last read Calendar; nothing was cancelled"
        }
        if !calendarWritable { return "That event's calendar can't be changed from this Mac" }
        if organizerIsUser == false {
            return "Only the organizer can cancel this event; decline it in Calendar instead"
        }
        if invitees > 0 && organizerIsUser == nil {
            return "Calendar doesn't say who organizes this event, so Wisp won't cancel it; cancel it in Calendar"
        }
        return nil
    }

    /// The native result for a removal EventKit accepted, from whether the occurrence can
    /// still be found at its start.
    static func cancelledResult(stillThere: Bool) -> [String: Any] {
        stillThere
            ? ["ok": false, "status": "unknown",
               "error": "Calendar accepted the cancel but the event still reads back; the outcome is unknown. Check Calendar before trying again"]
            : ["ok": true, "is_absent": true]
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
            ("calendar", expected.calendarID == after.calendarID), ("time zone", expected.timeZone == after.timeZone),
            ("notes", expected.notes == after.notes),
            ("URL", expected.url == after.url), ("location", expected.location == after.location),
            ("structured location", expected.structuredLocation == after.structuredLocation),
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
