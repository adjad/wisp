import Foundation

/// The person's working hours and quiet hours, as the service holds them
/// (service/user_hours.py, GET and PATCH /assistant/hours), and the offer to move an
/// event that falls outside them. Pure logic, checked by tests/UserHoursChecks.swift
/// without the app; UserHoursView.swift draws it.
///
/// Times are local minutes after midnight. A window may wrap midnight. Quiet hours
/// with the same start and end mean the person has none.
struct UserHours: Equatable {
    struct Window: Equatable {
        var start: Int
        var end: Int

        init(start: Int, end: Int) {
            self.start = start
            self.end = end
        }

        init?(json: Any?) {
            guard let row = json as? [String: Any],
                  let start = Self.minute(row["start_minute"]),
                  let end = Self.minute(row["end_minute"]) else { return nil }
            self.init(start: start, end: end)
        }

        private static func minute(_ value: Any?) -> Int? {
            guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  (0...1440).contains(number.intValue) else { return nil }
            return number.intValue
        }

        var json: [String: Any] { ["start_minute": start, "end_minute": end] }
    }

    static let defaultWorking = Window(start: 9 * 60, end: 17 * 60)
    static let defaultQuiet = Window(start: 22 * 60, end: 8 * 60)

    var working = UserHours.defaultWorking
    /// Monday is 0, the service's numbering.
    var workingDays: Set<Int> = [0, 1, 2, 3, 4]
    var quiet = UserHours.defaultQuiet
    var offerMoves = true
    /// The person answered, rather than these being the defaults.
    var isSet = false
    /// The times chosen for the Brief to update, minutes after midnight in clock
    /// order; nil is the default day across the working hours. PATCHed on its own
    /// (WispClient.saveBriefTimes), so saving the hours never touches it.
    var briefTimes: [Int]?
    var maxBriefTimes = 8

    init() {}

    /// Parsed tolerantly: what the service doesn't send reads as the default.
    init(json: [String: Any]) {
        if let window = Window(json: json["working"]) { working = window }
        if let days = (json["working"] as? [String: Any])?["days"] as? [Any] {
            let parsed = days.compactMap { ($0 as? NSNumber)?.intValue }.filter { (0...6).contains($0) }
            if !parsed.isEmpty { workingDays = Set(parsed) }
        }
        if let window = Window(json: json["quiet"]) { quiet = window }
        offerMoves = json["offer_moves"] as? Bool ?? true
        isSet = json["is_set"] as? Bool ?? false
        if let chosen = json["brief_times"] as? [Any] {
            let minutes = chosen.compactMap { ($0 as? NSNumber)?.intValue }.filter { (0..<1440).contains($0) }
            if !minutes.isEmpty { briefTimes = Array(Set(minutes)).sorted() }
        }
        if let most = (json["max_brief_times"] as? NSNumber)?.intValue, most > 0 { maxBriefTimes = most }
    }

    /// The whole hours to start from on choosing times: the default day
    /// (service/brief_pace.py, brief_times) to the nearest hour.
    var suggestedBriefTimes: [Int] {
        let span = ((working.end - working.start) % 1440 + 1440) % 1440
        let builds = 4
        let minutes = (0..<builds).map { step -> Int in
            let minute = Double(working.start) + Double(span) * Double(step) / Double(builds - 1)
            return (Int((minute / 60).rounded()) * 60) % 1440
        }
        return Array(Set(minutes)).sorted()
    }

    /// One hour's button on or off, never leaving none or passing the limit.
    func briefTimes(toggling minute: Int) -> [Int]? {
        var chosen = Set(briefTimes ?? [])
        if chosen.contains(minute) {
            guard chosen.count > 1 else { return nil }
            chosen.remove(minute)
        } else {
            guard chosen.count < maxBriefTimes else { return nil }
            chosen.insert(minute)
        }
        return chosen.sorted()
    }

    /// "9 AM, 1 PM and 5 PM".
    static func clockList(_ minutes: [Int]) -> String {
        let clocks = minutes.map(clock)
        guard let last = clocks.last else { return "" }
        return clocks.count == 1 ? last : clocks.dropLast().joined(separator: ", ") + " and " + last
    }

    /// Whether this can be saved: the service refuses anything else.
    var canSave: Bool { working.start != working.end && !workingDays.isEmpty }

    var quietIsOff: Bool { quiet.start == quiet.end }

    /// The PATCH body, with the zone so today's plan picks the hours up at once.
    func patch(timezone: String) -> [String: Any] {
        var working = working.json
        working["days"] = workingDays.sorted()
        return ["working": working, "quiet": quiet.json, "offer_moves": offerMoves, "timezone": timezone]
    }

    /// The times a picker offers: every half hour, plus a saved time between them,
    /// so opening the picker never changes what was saved. `upTo` is 1440 where
    /// midnight can be an end.
    static func options(including minute: Int, upTo last: Int = 1410) -> [Int] {
        var times = Array(stride(from: 0, through: last, by: 30))
        if (0...last).contains(minute), !times.contains(minute) { times.append(minute); times.sort() }
        return times
    }

    /// "9 AM", "5:30 PM", "Noon", "Midnight".
    static func clock(_ minute: Int) -> String {
        let wrapped = ((minute % 1440) + 1440) % 1440
        if wrapped == 0 { return "Midnight" }
        if wrapped == 720 { return "Noon" }
        let hour = wrapped / 60, rest = wrapped % 60
        let face = hour % 12 == 0 ? 12 : hour % 12
        let suffix = hour < 12 ? "AM" : "PM"
        return rest == 0 ? "\(face) \(suffix)" : "\(face):\(String(format: "%02d", rest)) \(suffix)"
    }

    /// One letter a day, Monday first, with the service's day number.
    static let dayLetters: [(day: Int, letter: String, name: String)] = [
        (0, "M", "Monday"), (1, "T", "Tuesday"), (2, "W", "Wednesday"), (3, "T", "Thursday"),
        (4, "F", "Friday"), (5, "S", "Saturday"), (6, "S", "Sunday"),
    ]

    /// Getting to know you's last screen and About you say what these now do.
    var summaryLine: String {
        let plan = "I'll plan around \(Self.clock(working.start)) to \(Self.clock(working.end))"
        return quietIsOff ? "\(plan)." : "\(plan), and stay quiet from \(Self.clock(quiet.start)) to \(Self.clock(quiet.end))."
    }
}

/// "This event is outside your working hours; here's a free time inside them." The
/// service picks it (service/assistant/hours_offers.py) and changes nothing: taking
/// it opens Ask with the same request as the Move panel, and Ask's confirmation card
/// gates the native, verified move.
struct HoursMoveOffer: Equatable, Identifiable {
    let id: String
    let title: String
    let sourceId: String
    let whenTs: Double
    let durationMinutes: Int
    /// True when the event is before the working day, false when after it.
    let isBefore: Bool
    let slotStartTs: Double
    let slotLabel: String

    init?(json: [String: Any]) {
        guard let id = json["id"] as? String, !id.isEmpty,
              let title = json["title"] as? String, !title.isEmpty,
              let sourceId = json["source_id"] as? String, !sourceId.isEmpty,
              let whenTs = (json["when_ts"] as? NSNumber)?.doubleValue,
              let minutes = (json["duration_minutes"] as? NSNumber)?.intValue, minutes > 0,
              let slot = json["slot"] as? [String: Any],
              let slotStart = (slot["start_ts"] as? NSNumber)?.doubleValue,
              let label = slot["label"] as? String, !label.isEmpty else { return nil }
        self.id = id
        self.title = title
        self.sourceId = sourceId
        self.whenTs = whenTs
        durationMinutes = minutes
        isBefore = json["side"] as? String == "before"
        slotStartTs = slotStart
        slotLabel = label
    }

    /// The title came
    /// from a calendar, so it is flattened and can't reshape the text around it.
    private var plainTitle: String {
        let flat = title.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
            .replacingOccurrences(of: "“", with: "'").replacingOccurrences(of: "”", with: "'")
            .replacingOccurrences(of: "\"", with: "'")
        return flat.count > 80 ? String(flat.prefix(79)) + "…" : flat
    }

    func line(eventTime: String) -> String {
        "“\(plainTitle)” is at \(eventTime), \(isBefore ? "before" : "after") your working hours. \(slotLabel) is free."
    }

    /// What Ask receives: the Move panel's wording (CheckInMovePanel.moveRequest),
    /// naming the event precisely. Sending this changes nothing by itself.
    func moveRequest(dayLabel: (Double) -> String, clock: (Double) -> String) -> String {
        "Move this calendar event to \(dayLabel(slotStartTs)) at \(clock(slotStartTs)), keeping it \(durationMinutes) minutes: "
            + "“\(plainTitle)” on \(dayLabel(whenTs)) at \(clock(whenTs)) [calendar event \(sourceId)]"
    }
}
