import Foundation

@main
struct BriefTimeChecks {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ label: String) {
            checks += 1
            guard condition else { fputs("FAIL: \(label)\n", stderr); exit(1) }
        }
        let la = TimeZone(identifier: "America/Los_Angeles")!
        let utc = TimeZone(secondsFromGMT: 0)!
        let india = TimeZone(identifier: "Asia/Kolkata")!
        func instant(_ text: String) -> Date { ISO8601DateFormatter().date(from: text)! }
        func local(_ hour: Int, _ minute: Int = 0) -> BriefTimeContext {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = la
            let date = calendar.date(from: DateComponents(year: 2026, month: 10, day: 22, hour: hour, minute: minute))!
            return BriefTimeContext(date: date, timezone: la)
        }
        for (hour, minute, expected) in [
            (0, 0, BriefTimeContext.Period.night), (4, 59, .night), (5, 0, .morning),
            (11, 59, .morning), (12, 0, .afternoon), (16, 59, .afternoon),
            (17, 0, .evening), (20, 59, .evening), (21, 0, .night), (23, 59, .night)
        ] { check(local(hour, minute).period == expected, "boundary \(hour):\(minute)") }

        let sameInstant = instant("2026-10-22T07:30:00Z")
        check(BriefTimeContext(date: sameInstant, timezone: la).period == .night, "same instant: LA night")
        check(BriefTimeContext(date: sameInstant, timezone: utc).period == .morning, "same instant: UTC morning")
        check(BriefTimeContext(date: sameInstant, timezone: india).period == .afternoon, "same instant: India afternoon")
        for value in ["2026-11-01T08:30:00Z", "2026-11-01T09:30:00Z", "2026-03-08T09:59:00Z", "2026-03-08T10:00:00Z"] {
            let context = BriefTimeContext(date: instant(value), timezone: la)
            check(context.period == .night && context.beforeDawn, "DST civil-time handling \(value)")
        }

        let beforeDawn = local(0, 56)
        let lateNight = local(23, 59)
        check(beforeDawn.duePhrase(isToday: true) == "later today", "after midnight deadline wording")
        check(lateNight.duePhrase(isToday: true) == "today", "before midnight deadline wording")
        check(local(8).duePhrase(isToday: false) == "that day", "non-current selected date wording")
        check(beforeDawn.suggestions(isToday: true)[1].prompt.contains("morning of Thursday, October 22, 2026"), "before dawn plans this morning")
        check(lateNight.suggestions(isToday: true)[1].prompt.contains("morning of Friday, October 23, 2026"), "late night plans next morning")
        check(local(18).suggestions(isToday: true)[2].prompt.contains("Friday, October 23, 2026"), "evening tomorrow date")

        for hour in [8, 14, 18, 23] {
            let context = local(hour)
            let suggestions = context.suggestions(isToday: true)
            check(suggestions.count == 3 && Set(suggestions.map(\.id)).count == 3, "three stable unique suggestions at \(hour)")
            check(suggestions.allSatisfy { !$0.title.isEmpty && !$0.prompt.isEmpty }, "every suggestion has a draft at \(hour)")
            check(context.suggestions(isToday: false).isEmpty, "no current-time suggestions for another day at \(hour)")
            check(context.opening(weekday: "Thursday", isToday: false, load: .light) == nil, "neutral non-current opening at \(hour)")
            let unknown = context.opening(weekday: "Thursday", isToday: true, load: nil)!
            check(!unknown.contains("light") && !unknown.contains("clear") && !unknown.contains("quiet"), "unknown calendar makes no workload claim at \(hour)")
        }
        check(local(8).opening(weekday: "Thursday", isToday: true, load: .busy) == "A busy Thursday ahead.", "busy morning stays busy")
        check(local(8).opening(weekday: "Thursday", isToday: true, load: .light) == "A light start to Thursday.", "mockup morning copy")
        check(beforeDawn.opening(weekday: "Thursday", isToday: true, load: .packed) == "A new Thursday is getting started.", "busy new day is not described as quiet")
        let dstLate = BriefTimeContext(date: instant("2026-11-01T06:30:00Z"), timezone: la)
        check(dstLate.suggestions(isToday: true)[1].prompt.contains("morning of Sunday, November 1, 2026"), "tomorrow uses calendar day before DST fallback")
        print("Brief time context: \(checks) checks passed")
    }
}
