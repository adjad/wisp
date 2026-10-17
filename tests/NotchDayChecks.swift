// Contract for the notch's Brief and Day logic (app/Sources/WispApp/NotchDay.swift).
// Compiled with NotchTabChecks.swift, which calls dayChecks. Pure logic: never
// launches Wisp or contacts its service.
import Foundation

func dayChecks(_ check: (Bool, String) -> Void) {
    let hour = 3600.0
    let dayStart = 86400.0 * 20_000 // a midnight, in UTC
    let dayEnd = dayStart + 24 * hour
    let now = dayStart + 13 * hour + 7 * 60
    func at(_ h: Double) -> Double { dayStart + h * hour }
    func clock(_ t: Double) -> String {
        let minutes = Int(((t - dayStart) / 60).rounded())
        return String(format: "%d:%02d", minutes / 60, minutes % 60)
    }
    func dayClock(_ t: Double) -> String { "earlier " + clock(t + 24 * hour) }

    let blocks = [
        DayBlockInput(id: "standup", title: "Standup", start: at(9), end: at(10), kind: "calendar"),
        DayBlockInput(id: "pa2", title: "Work on PA 2", start: at(13.75), end: at(15.75), kind: "suggestion",
                      evidenceID: "n1"),
        DayBlockInput(id: "club", title: "Club", start: at(15), end: at(16), kind: "calendar"),
        DayBlockInput(id: "away", title: "Possible time away", start: at(16), end: at(17.5), kind: "behavior_away",
                      patternID: "p1"),
        DayBlockInput(id: "task:t1", title: "Lab report", start: at(18), end: at(18.75), kind: "task", taskID: "t1"),
        DayBlockInput(id: "task:t2", title: "nothing", start: at(18), end: at(18.5), kind: "task", taskID: "t2"),
    ]
    let needs = [
        DayNeedInput(id: "n1", title: "PA 2", source: "reminders", due: at(23.98)),
        DayNeedInput(id: "n2", title: "Get DoorDash order", source: "reminders", due: dayStart - 5 * hour),
        DayNeedInput(id: "n3", title: "Call mom", source: "reminders", due: at(20)),
        DayNeedInput(id: "n4", title: "Nothing.", source: "reminders", due: at(21)),
        DayNeedInput(id: "n5", title: "Someday", source: "reminders", due: nil),
        DayNeedInput(id: "n6", title: "Club", source: "calendar", due: at(15)),
        DayNeedInput(id: "n7", title: "Next week's essay", source: "reminders", due: dayEnd + 50 * hour),
    ]
    let caught = [
        DayCaughtInput(id: "c1", title: "Dinner with Priya?", when: at(19), state: "shadow"),
        DayCaughtInput(id: "c2", title: "Old plan", when: dayStart - 3 * hour, state: "shadow"),
        DayCaughtInput(id: "c3", title: "Lunch", when: at(14), state: "already_on_file"),
        DayCaughtInput(id: "c4", title: "n/a", when: at(17), state: "shadow"),
    ]
    let timeline = DayTimeline.build(blocks: blocks, allDay: ["PA 2 [CSE-20-04]", "none"], needs: needs, caught: caught,
                                     now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                     isToday: true, clock: clock, dayClock: dayClock)
    let ids = timeline.items.map(\.id)
    func item(_ id: String) -> DayItem? { timeline.items.first { $0.id == id } }

    // The lane: from the hour of now to the last thing, on whole hours.
    check(timeline.from == at(13), "today's lane starts at the hour of now")
    check(timeline.to == at(20), "the lane runs to the last block's hour: \(clock(timeline.to))")
    check(timeline.hours.count == 7 && timeline.hours.first == at(13), "hour marks from 1 to 7")
    check(timeline.allDay == ["PA 2 [CSE-20-04]"], "the all-day strip drops junk titles")

    // What lands on it, and how it is drawn.
    check(!ids.contains("block:standup"), "a finished event is left off")
    check(!ids.contains("block:task:t2"), "a junk task is left off")
    check(item("block:club")?.style == .calendar && item("block:club")?.detail == "15:00 – 16:00", "calendar events are solid")
    check(item("block:pa2")?.style == .suggested && item("block:pa2")?.evidenceID == "n1", "suggestions are dashed")
    check(item("block:away")?.style == .usual && item("block:away")?.patternID == "p1"
          && item("block:away")?.detail == "Usually away · not on your calendar", "learned away time is hatched")
    check(item("block:task:t1")?.style == .task && item("block:task:t1")?.taskID == "t1", "your tasks are drawn as yours")
    check(!ids.contains("need:n1"), "a reminder with a suggested block is drawn once, as the suggestion")
    check(item("need:n2")?.style == .overdue && item("need:n2")?.start == now
          && item("need:n2")?.detail == "Overdue since earlier 19:00", "an overdue reminder sits at now, in red")
    check(item("need:n3")?.style == .needsYou && item("need:n3")?.start == at(19.5) && item("need:n3")?.end == at(20),
          "a reminder due later sits just before its due time")
    check(!ids.contains("need:n4") && !ids.contains("need:n5") && !ids.contains("need:n6") && !ids.contains("need:n7"),
          "junk, undated, calendar and later reminders stay off the clock")
    check(item("caught:c1")?.style == .caught && item("caught:c1")?.start == at(19) && item("caught:c1")?.end == at(20),
          "a plan caught in texts is amber, at the time it gave")
    check(!ids.contains("caught:c2") && !ids.contains("caught:c3") && !ids.contains("caught:c4"),
          "past, already-filed and junk caught plans are left off")
    let filed = DayTimeline.build(blocks: [], allDay: [], needs: [DayNeedInput(id: "r", title: "Dinner with Priya?", source: "reminders", due: nil)],
                                  caught: [DayCaughtInput(id: "c1", title: "Dinner with Priya?", when: at(19), state: "created"),
                                           DayCaughtInput(id: "c5", title: "Movie night", when: at(21), state: "created")],
                                  now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                  isToday: true, clock: clock, dayClock: dayClock)
    check(filed.items.map(\.id) == ["caught:c5"], "a caught plan already in Reminders is drawn once: \(filed.items.map(\.id))")
    check(timeline.styles == [.calendar, .task, .suggested, .usual, .caught, .needsYou, .overdue],
          "the legend lists only what is on screen, in order")

    // Overlaps share the width.
    check(item("block:pa2")?.columns == 2 && item("block:club")?.columns == 2
          && item("block:pa2")?.column == 0 && item("block:club")?.column == 1, "overlapping blocks sit side by side")
    check(item("block:task:t1")?.columns == 1, "a block on its own takes the full width")
    let stacked = DayTimeline.arrange([
        DayItem(id: "a", title: "a", detail: "", start: 0, end: 30, style: .calendar),
        DayItem(id: "b", title: "b", detail: "", start: 10, end: 40, style: .calendar),
        DayItem(id: "c", title: "c", detail: "", start: 20, end: 50, style: .calendar),
        DayItem(id: "d", title: "d", detail: "", start: 35, end: 60, style: .calendar),
        DayItem(id: "e", title: "e", detail: "", start: 70, end: 80, style: .calendar),
    ])
    check(stacked.map(\.column) == [0, 1, 2, 0, 0] && stacked.map(\.columns) == [3, 3, 3, 3, 1],
          "columns are reused within a cluster and reset after it: \(stacked.map(\.column)) \(stacked.map(\.columns))")

    // Another day: the lane starts at working hours, and nothing is overdue.
    let other = DayTimeline.build(blocks: blocks, allDay: [], needs: needs, caught: [], now: dayStart - 30 * hour,
                                  dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                  isToday: false, clock: clock, dayClock: dayClock)
    check(other.from == at(9) && other.items.contains { $0.id == "block:standup" }, "another day starts at working hours")
    check(!other.items.contains { $0.style == .overdue }, "nothing is overdue on another day")

    // The badge counts only what is due today or overdue.
    check(DayTimeline.badgeCount(needs, endOfDay: dayEnd) == 3, "badge: due today and overdue, not junk, undated or calendar")
    check(DayTimeline.badgeCount([], endOfDay: dayEnd) == 0, "no items, no badge")

    // Junk reminders.
    for junk in ["nothing", " Nothing. ", "N/A", "", "--", "ok", "None!", "x"] {
        check(DayTimeline.isJunk(junk), "junk: \"\(junk)\"")
    }
    for real in ["PA 2", "Call mom", "Get DoorDash order", "Dinner with Priya?"] {
        check(!DayTimeline.isJunk(real), "not junk: \(real)")
    }

    // Planner warnings in plain words.
    check(DayTimeline.plainWarning("Calendar is not current. Flexible tasks are waiting for a successful sync.")
          == "Your calendar hasn't synced yet, so I'm waiting before placing tasks.", "calendar sync warning")
    check(DayTimeline.plainWarning("Tentative · You're usually at the gym") == nil, "the hatch already says tentative")
    check(DayTimeline.plainWarning("Suggested time · duration is an estimate") == nil, "the dash already says suggested")
    check(DayTimeline.plainWarning("Overlaps Club") == "Overlaps Club.", "overlap warning")
    check(DayTimeline.plainWarning("Pinned time is in the past; mark done or move it")
          == "This time has passed. Move it or mark it done.", "past pin warning")
    check(DayTimeline.plainWarning("Something new") == "Something new", "an unknown warning passes through")
    check(DayTimeline.plainReason("Needs 90 uninterrupted minutes; largest available gap before the deadline or end of working hours is 40 minutes")
          == "no long enough gap (the longest free stretch is 40 min)", "unscheduled reason")

    // The Day blurb.
    let blurb = DayTimeline.blurb(timeline, needs: needs, isToday: true, endOfDay: dayEnd, clock: clock)
    check(blurb == "PA 2 is the main thing today. I've given it one block, around when you're usually away. Clear after 20:00.",
          "blurb: \(blurb)")
    let empty = DayTimeline.build(blocks: [], allDay: [], needs: [], caught: [], now: now, dayStart: dayStart,
                                  dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: true,
                                  clock: clock, dayClock: dayClock)
    check(DayTimeline.blurb(empty, needs: [], isToday: true, endOfDay: dayEnd, clock: clock)
          == "Nothing is planned for the rest of today.", "an empty day says so")
    check(empty.items.isEmpty && empty.styles.isEmpty && empty.to == at(18), "an empty day still shows working hours")

    // The Brief's sentence.
    check(DayTimeline.lede(weekday: "Saturday", isToday: true, eventsLeft: 1, dueToday: ["PA 2"],
                           overdue: ["Get DoorDash order"], next: nil)
          == "A light Saturday. PA 2 is due today, and Get DoorDash order is still open.", "lede with due and overdue")
    check(DayTimeline.lede(weekday: "Sunday", isToday: true, eventsLeft: 0, dueToday: [], overdue: [], next: nil)
          == "A clear Sunday. Nothing needs you right now.", "lede on a clear day")
    check(DayTimeline.lede(weekday: "Monday", isToday: true, eventsLeft: 5, dueToday: ["nothing"], overdue: ["Pay rent", "Return book"],
                           next: (title: "Club", time: "3 PM"))
          == "A full Monday. Two overdue reminders are still open.",
          "lede drops junk and counts overdue")
    check(DayTimeline.lede(weekday: "Tuesday", isToday: true, eventsLeft: 3, dueToday: [], overdue: [],
                           next: (title: "Club", time: "3 PM"))
          == "A steady Tuesday. Next up: Club at 3 PM.", "lede with only the next event")

    // The Brief's lines: at most four, each only with something to say.
    let lines = DayTimeline.briefLines(allDay: ["PA 2 [CSE-20-04]"], nextEvent: (title: "Club", time: "3 PM"),
                                       mail: (ready: true, needing: []), texts: ["nothing", "Priya asked about dinner"],
                                       soon: (title: "UCSC startup club", when: "Wed 6 PM", source: "calendar"))
    check(lines.map(\.tag) == ["CAL", "MAIL", "TEXTS", "SOON"], "four lines in order")
    check(lines[0].trailing == "All day" && lines[1].text == "Nothing needs a reply"
          && lines[2].text == "Priya asked about dinner" && lines[3].source == .calendar, "line contents")
    let sparse = DayTimeline.briefLines(allDay: [], nextEvent: nil, mail: (ready: false, needing: ["x"]), texts: [],
                                        soon: (title: "Essay", when: "Fri 5 PM", source: "reminders"))
    check(sparse.map(\.tag) == ["SOON"] && sparse[0].source == .reminders, "empty sources are hidden")
    check(DayTimeline.briefLines(allDay: [], nextEvent: (title: "Club", time: "3 PM"),
                                 mail: (ready: true, needing: ["Lab partner", "Advisor"]), texts: [], soon: nil)
          .map(\.text) == ["Club", "2 emails need you"], "the next event and a mail count")
}
