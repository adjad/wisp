// Contract for working and quiet hours (app/Sources/WispApp/UserHours.swift).
// Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct UserHoursChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        // Defaults match service/user_hours.py.
        let plain = UserHours()
        check(plain.working == .init(start: 540, end: 1020), "working hours default to 9 to 5")
        check(plain.workingDays == [0, 1, 2, 3, 4], "working days default to Monday through Friday")
        check(plain.quiet == .init(start: 1320, end: 480), "quiet hours default to 10 PM to 8 AM, wrapping midnight")
        check(plain.offerMoves && !plain.isSet && plain.canSave, "unset, on, and saveable as it stands")

        // Parsing is tolerant: what the service doesn't send, or sends wrong, is the default.
        check(UserHours(json: [:]) == UserHours(), "an empty reply reads as the defaults")
        let bad = UserHours(json: ["working": ["start_minute": "nine", "end_minute": 1020, "days": []],
                                   "quiet": ["start_minute": 99999, "end_minute": 8], "offer_moves": "yes"])
        check(bad == UserHours(), "bad values read as the defaults, not a crash")
        let wrapped = UserHours(json: ["working": ["start_minute": 1320, "end_minute": 360, "days": [6, 0]],
                                       "quiet": ["start_minute": 600, "end_minute": 840],
                                       "offer_moves": false, "is_set": true])
        check(wrapped.working == .init(start: 1320, end: 360) && wrapped.workingDays == [0, 6], "a night shift parses")
        check(wrapped.quiet == .init(start: 600, end: 840) && !wrapped.offerMoves && wrapped.isSet, "the rest parses")

        // The body sent back is what the service takes.
        let patch = wrapped.patch(timezone: "America/New_York")
        let working = patch["working"] as? [String: Any]
        check(working?["start_minute"] as? Int == 1320 && working?["days"] as? [Int] == [0, 6], "patch carries working hours and days")
        check((patch["quiet"] as? [String: Any])?["end_minute"] as? Int == 840, "patch carries quiet hours")
        check(patch["offer_moves"] as? Bool == false && patch["timezone"] as? String == "America/New_York", "patch carries the switch and zone")

        // What can't be saved.
        var hours = UserHours()
        hours.working = .init(start: 600, end: 600)
        check(!hours.canSave, "working hours need a start and end that differ")
        hours = UserHours()
        hours.workingDays = []
        check(!hours.canSave, "at least one working day")
        hours = UserHours()
        hours.quiet = .init(start: 0, end: 0)
        check(hours.canSave && hours.quietIsOff, "no quiet hours is allowed")

        // Pickers keep a time saved between the half hours.
        let times = UserHours.options(including: 555)
        check(times.contains(555) && times == times.sorted(), "an off-grid saved time stays selectable, in order")
        check(UserHours.options(including: 540).count == 48, "every half hour of the day")
        check(UserHours.options(including: 1440, upTo: 1440).last == 1440, "midnight can end working hours")
        check(!UserHours.options(including: 1440).contains(1440), "but not start them")

        // Clock labels.
        check(UserHours.clock(540) == "9 AM" && UserHours.clock(1050) == "5:30 PM", "plain clock labels")
        check(UserHours.clock(0) == "Midnight" && UserHours.clock(1440) == "Midnight" && UserHours.clock(720) == "Noon", "named times")
        check(UserHours.clock(725) == "12:05 PM" && UserHours.clock(5) == "12:05 AM", "around noon and midnight")
        check(UserHours().summaryLine == "I'll plan around 9 AM to 5 PM, and stay quiet from 10 PM to 8 AM.", "the summary says what they do")
        hours = UserHours()
        hours.quiet = .init(start: 0, end: 0)
        check(hours.summaryLine == "I'll plan around 9 AM to 5 PM.", "the summary drops quiet hours when there are none")

        // The move offer: parsed strictly, because a click acts on it.
        let row: [String: Any] = ["id": "abc", "title": "Thesis\n“review”", "source_id": "EK-1", "when_ts": 1_000.0,
                                  "duration_minutes": 45, "side": "after",
                                  "slot": ["start_ts": 2_000.0, "label": "Tue 9:00 AM", "reason": "First free slot Tue"]]
        guard let offer = HoursMoveOffer(json: row) else { check(false, "a whole offer parses"); return }
        check(!offer.isBefore, "after the working day")
        check(HoursMoveOffer(json: row.merging(["slot": [:]]) { _, new in new }) == nil, "no named slot, no offer")
        check(HoursMoveOffer(json: row.filter { $0.key != "source_id" }) == nil, "no event id, no offer")
        check(HoursMoveOffer(json: row.merging(["side": "before"]) { _, new in new })?.isBefore == true, "before the working day")
        let line = offer.line(eventTime: "7:30 PM")
        check(line == "“Thesis 'review'” is at 7:30 PM, after your working hours. Tue 9:00 AM is free.", "the line names the event, why, and the slot: \(line)")

        // Taking the offer is the Move panel's request, with the event named exactly.
        let request = offer.moveRequest(dayLabel: { $0 == 2_000 ? "Tuesday, Nov 10" : "Monday, Nov 9" },
                                        clock: { $0 == 2_000 ? "9:00 AM" : "7:30 PM" })
        check(request == "Move this calendar event to Tuesday, Nov 10 at 9:00 AM, keeping it 45 minutes: "
              + "“Thesis 'review'” on Monday, Nov 9 at 7:30 PM [calendar event EK-1]", "the Ask request: \(request)")

        // Chosen Brief times: nil is the default day, and bad rows never count.
        check(UserHours(json: [:]).briefTimes == nil, "no chosen times by default")
        check(UserHours(json: ["brief_times": NSNull()]).briefTimes == nil, "null is the default day")
        check(UserHours(json: ["brief_times": [540, 9999, 540, 60]]).briefTimes == [60, 540], "chosen times parse in order")
        check(UserHours(json: ["brief_times": [Int]()]).briefTimes == nil, "none chosen is the default day")
        check(UserHours().suggestedBriefTimes == [540, 720, 840, 1020], "the default day to the nearest hour: \(UserHours().suggestedBriefTimes)")
        var chosenHours = UserHours()
        chosenHours.briefTimes = [540]
        check(chosenHours.briefTimes(toggling: 540) == nil, "the last time can't be switched off")
        check(chosenHours.briefTimes(toggling: 600) == [540, 600], "a time switches on")
        chosenHours.briefTimes = [60, 120, 180, 240, 300, 360, 420, 480]
        check(chosenHours.briefTimes(toggling: 600) == nil, "no more than the limit")
        check(chosenHours.briefTimes(toggling: 60)?.count == 7, "one off at the limit")
        check(chosenHours.patch(timezone: "UTC")["brief_times"] == nil, "saving the hours never touches the chosen times")
        check(UserHours.clockList([540]) == "9 AM", "one time")
        check(UserHours.clockList([540, 780]) == "9 AM and 1 PM", "two times")
        check(UserHours.clockList([540, 780, 1020]) == "9 AM, 1 PM and 5 PM", "three times")

        print("UserHoursChecks: \(count) checks passed")
    }
}
