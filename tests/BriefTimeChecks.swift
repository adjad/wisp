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
        for zone in [la, utc, india] {
            let context = BriefTimeContext(date: instant("2026-10-22T07:30:37Z"), timezone: zone)
            check(context.minuteStart == instant("2026-10-22T07:30:00Z"), "schedule aligns to the minute in \(zone.identifier)")
        }
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
        check(beforeDawn.suggestions(isToday: true)[0].title == "Show today’s essentials", "before dawn retains the selected mockup")
        let tonight = lateNight.suggestions(isToday: true)[0]
        check(tonight.title == "What’s left tonight?" && tonight.prompt.contains("before midnight on Thursday, October 22, 2026"),
              "late-night first action explicitly keeps due-tonight urgency")
        check(local(21).suggestions(isToday: true)[0].id == "tonight" && local(0).suggestions(isToday: true)[0].id == "essentials",
              "midnight changes tonight into the new day's essentials")
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
        let overnight = DayLoad.Event(title: "Exam", start: beforeDawn.date.timeIntervalSince1970 + 300,
                                      end: beforeDawn.date.timeIntervalSince1970 + 3900)
        let overnightLoad = DayLoad.assess(events: [overnight], allDay: [], from: beforeDawn.date.timeIntervalSince1970,
                                           dayStart: local(0).date.timeIntervalSince1970, isToday: true, zone: la)
        check(overnightLoad.verdict == .light, "regression precondition: an overnight event can have a light workload verdict")
        check(beforeDawn.opening(weekday: "Thursday", isToday: true, load: overnightLoad.verdict, events: [overnight])
              == "A new Thursday is getting started.", "imminent overnight meeting prevents a quiet-start claim")
        let ongoing = DayLoad.Event(title: "Late call", start: beforeDawn.date.timeIntervalSince1970 - 1800,
                                    end: beforeDawn.date.timeIntervalSince1970 + 1800)
        check(beforeDawn.opening(weekday: "Thursday", isToday: true, load: .light, events: [ongoing])?.contains("quiet") == false,
              "ongoing overnight meeting prevents a quiet-start claim")
        let past = DayLoad.Event(title: "Finished call", start: beforeDawn.date.timeIntervalSince1970 - 3600,
                                 end: beforeDawn.date.timeIntervalSince1970 - 60)
        let afterDawn = DayLoad.Event(title: "Morning exam", start: local(6).date.timeIntervalSince1970,
                                      end: local(7).date.timeIntervalSince1970)
        check(beforeDawn.opening(weekday: "Thursday", isToday: true, load: .light, events: [past, afterDawn])
              == "A quiet start to Thursday.", "past events and later morning events do not occupy the overnight interval")
        let dstLate = BriefTimeContext(date: instant("2026-11-01T06:30:00Z"), timezone: la)
        check(dstLate.suggestions(isToday: true)[1].prompt.contains("morning of Sunday, November 1, 2026"), "tomorrow uses calendar day before DST fallback")

        // The time-aware opening changes only presentation; the existing composer
        // still owns due items, overdue items, next events and the empty state.
        let nightLede = DayTimeline.lede(weekday: "Thursday", isToday: true, load: .light,
                                        dueToday: ["Project outline"], overdue: ["Draft review"], next: nil,
                                        openingOverride: beforeDawn.opening(weekday: "Thursday", isToday: true, load: .light),
                                        dueReference: beforeDawn.duePhrase(isToday: true))
        check(nightLede == "A quiet start to Thursday. Project outline is due later today, and Draft review is still open.",
              "composed before-dawn sentence preserves due and overdue facts")
        let morning = local(8)
        let eventLede = DayTimeline.lede(weekday: "Thursday", isToday: true, load: nil, dueToday: [], overdue: [],
                                        next: (title: "Exam", time: "10 AM"),
                                        openingOverride: morning.opening(weekday: "Thursday", isToday: true, load: nil))
        check(eventLede == "Here's your Thursday morning. Next up: Exam at 10 AM.", "next event survives a time-aware unknown-calendar opening")
        let neutral = DayTimeline.lede(weekday: "Friday", isToday: false, load: .light, dueToday: ["Report"], overdue: [], next: nil,
                                      openingOverride: morning.opening(weekday: "Friday", isToday: false, load: .light),
                                      dueReference: morning.duePhrase(isToday: false))
        check(neutral == "A light Friday. Report is due that day.", "another selected date retains neutral factual wording")
        let attentionLede = DayTimeline.lede(weekday: "Thursday", isToday: true, load: .free, dueToday: [], overdue: [], next: nil,
                                            othersNeedYou: true,
                                            openingOverride: morning.opening(weekday: "Thursday", isToday: true, load: .free))
        check(!attentionLede.contains("Nothing needs you"), "attention rows still prevent a false empty-state claim")
        print("Brief time context: \(checks) checks passed")
    }
}
