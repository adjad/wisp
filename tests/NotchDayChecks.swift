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
        DayCaughtInput(id: "c1", title: "Dinner with Jo?", when: at(19), state: "shadow"),
        DayCaughtInput(id: "c2", title: "Old plan", when: dayStart - 3 * hour, state: "shadow"),
        DayCaughtInput(id: "c3", title: "Lunch", when: at(14), state: "already_on_file"),
        DayCaughtInput(id: "c4", title: "n/a", when: at(17), state: "shadow"),
    ]
    let timeline = DayTimeline.build(blocks: blocks, allDay: ["PA 2 [CMP-140-04]", "none"], needs: needs, caught: caught,
                                     now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                     isToday: true, clock: clock, dayClock: dayClock)
    let ids = timeline.items.map(\.id)
    func item(_ id: String) -> DayItem? { timeline.items.first { $0.id == id } }

    // The lane: two hours before the hour of now, to the last thing, on whole hours.
    check(timeline.from == at(11), "today's lane starts two hours before the hour of now")
    check(timeline.to == at(20), "the lane runs to the last block's hour: \(clock(timeline.to))")
    check(!timeline.items.contains(where: \.past), "nothing in this fixture ended in the last two hours")

    // Two hours back, dim; at least three hours ahead; never past midnight either way.
    func window(_ blocks: [DayBlockInput], now: Double, needs: [DayNeedInput] = []) -> DayTimeline {
        DayTimeline.build(blocks: blocks, allDay: [], needs: needs, caught: [], now: now, dayStart: dayStart, dayEnd: dayEnd,
                          workStart: at(9), workEnd: at(18), isToday: true, clock: clock, dayClock: dayClock)
    }
    let recent = window([DayBlockInput(id: "coffee", title: "Coffee", start: at(11.25), end: at(11.75), kind: "calendar"),
                         DayBlockInput(id: "early", title: "Early call", start: at(10), end: at(10.9), kind: "calendar"),
                         DayBlockInput(id: "lunch", title: "Lunch", start: at(12.5), end: at(13.5), kind: "calendar"),
                         DayBlockInput(id: "late", title: "Late", start: at(15), end: at(16), kind: "calendar")], now: now)
    check(recent.items.map(\.id) == ["block:coffee", "block:lunch", "block:late"], "the back window keeps what ended in it: \(recent.items.map(\.id))")
    check(recent.items.map(\.past) == [true, false, false], "only what is over is dim; a block running now is not")
    // A block that ends while its Cancel is still in the 5-second Undo window (or
    // in flight) stays live: Undo must not vanish because the clock passed its end.
    let lunchOver = window([DayBlockInput(id: "lunch", title: "Lunch", start: at(12.5), end: at(13.5), kind: "calendar"),
                            DayBlockInput(id: "late", title: "Late", start: at(15), end: at(16), kind: "calendar")], now: at(13.6))
    let endedLunch = lunchOver.items.first { $0.id == "block:lunch" }
    let upcomingLate = lunchOver.items.first { $0.id == "block:late" }
    check(endedLunch?.past == true && upcomingLate?.past == false, "lunch ended at 13:30, late has not started")
    check(endedLunch?.isOver(cancelActive: false) == true, "an ended block with nothing pending is over: dim, no controls")
    check(endedLunch?.isOver(cancelActive: true) == false, "a cancel still undoable or in flight keeps the ended block live")
    check(upcomingLate?.isOver(cancelActive: false) == false && upcomingLate?.isOver(cancelActive: true) == false,
          "a block that has not ended is never over")
    check(DayTimeline.blurb(window([DayBlockInput(id: "coffee", title: "Coffee", start: at(11.25), end: at(11.75), kind: "calendar")], now: now),
                            needs: [], isToday: true, endOfDay: dayEnd, clock: clock) == "Nothing is planned for the rest of today.",
          "the blurb ignores what is already over")
    let dawn = window([], now: at(1.5))
    check(dawn.from == dayStart && dawn.to == at(18), "before 2 AM the lane starts at midnight: \(clock(dawn.from))–\(clock(dawn.to))")
    let dusk = window([], now: at(19.25))
    check(dusk.from == at(17) && dusk.to == at(23), "three hours ahead past working hours: \(clock(dusk.from))–\(clock(dusk.to))")
    let midnight = window([DayBlockInput(id: "show", title: "Show", start: at(21.5), end: at(22.5), kind: "calendar")], now: at(23.5))
    check(midnight.from == at(21) && midnight.to == dayEnd && midnight.items.first?.past == true,
          "near midnight: two hours back, ending at midnight: \(clock(midnight.from))–\(clock(midnight.to))")
    check(timeline.allDay == ["PA 2 [CMP-140-04]"], "the all-day strip drops placeholder words")

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
    let filed = DayTimeline.build(blocks: [], allDay: [], needs: [DayNeedInput(id: "r", title: "Dinner with Jo?", source: "reminders", due: nil)],
                                  caught: [DayCaughtInput(id: "c1", title: "Dinner with Jo?", when: at(19), state: "created"),
                                           DayCaughtInput(id: "c5", title: "Movie night", when: at(21), state: "created")],
                                  now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                  isToday: true, clock: clock, dayClock: dayClock)
    check(filed.items.map(\.id) == ["caught:c5"], "a caught plan already in Reminders is drawn once: \(filed.items.map(\.id))")
    check(Set(timeline.items.map(\.style)) == [.calendar, .task, .suggested, .usual, .caught, .needsYou, .overdue],
          "every kind of block lands on the day")

    // The agenda: each thing on its own row in time order, with the free stretches.
    func rows(_ agenda: DayAgenda) -> [String] { rows(agenda.ahead) }
    func rows(_ list: [DayAgenda.Row]) -> [String] {
        list.map {
            switch $0 {
            case .item(let item): return item.id
            case .free(let start, let end): return "free \(clock(start))–\(clock(end))"
            }
        }
    }
    let agenda = timeline.agenda(now: now, isToday: true)
    check(agenda.earlier.isEmpty, "nothing in this fixture is over")
    // Overdue leads at now; Call mom is listed at its 20:00 due time, after Dinner;
    // 17:30–18:00 is free, 18:45–19:00 too short to name; overlapping Club still
    // gets its own row after PA 2.
    check(rows(agenda) == ["need:n2", "free 13:07–13:45", "block:pa2", "block:club", "block:away",
                           "free 17:30–18:00", "block:task:t1", "caught:c1", "need:n3"],
          "the agenda lists the day in order: \(rows(agenda))")
    check(agenda.ahead.map(\.id).count == Set(agenda.ahead.map(\.id)).count, "agenda rows have distinct ids")
    // Show more counts things, not free stretches, and never ends on one.
    check(agenda.thingsAhead == 7, "seven things ahead: \(agenda.thingsAhead)")
    check(rows(agenda.firstRows(holding: 3)) == ["need:n2", "free 13:07–13:45", "block:pa2", "block:club"],
          "the first three things, with the free stretch between them")
    check(rows(agenda.firstRows(holding: 4)) == ["need:n2", "free 13:07–13:45", "block:pa2", "block:club", "block:away"],
          "a free stretch whose next thing is hidden is left off: \(rows(agenda.firstRows(holding: 4)))")
    check(agenda.firstRows(holding: 9) == agenda.ahead, "a short day shows all of it")
    let clubItem = item("block:club")!
    check(DayTimeline.gutter(clubItem, clock: clock) == ("15:00", "16:00"), "an event's column shows its start over its end")
    check(DayTimeline.gutter(item("need:n2")!, clock: clock) == ("Overdue", ""), "an overdue one says so in red")
    check(DayTimeline.gutter(item("need:n3")!, clock: clock) == ("20:00", "Due"), "a reminder is listed at its due time")
    check(DayTimeline.gutter(item("caught:c1")!, clock: clock) == ("19:00", ""), "a caught plan has no end to show")
    check(DayTimeline.agendaDetail(clubItem, now: now, isToday: true, clock: clock) == "Calendar · 1 h",
          "an event says it's from Calendar and how long it runs")
    check(DayTimeline.agendaDetail(clubItem, now: at(15.5), isToday: true, clock: clock) == "Calendar · 30 min left",
          "an event on now says how long is left")
    check(DayTimeline.agendaDetail(item("need:n2")!, now: now, isToday: true, clock: clock) == "Since earlier 19:00",
          "the column already says overdue")
    check(DayTimeline.agendaDetail(item("need:n3")!, now: now, isToday: true, clock: clock) == "Needs you",
          "the column already says when it's due")
    check(DayTimeline.agendaDetail(item("block:pa2")!, now: now, isToday: true, clock: clock) == item("block:pa2")!.detail,
          "a suggestion keeps its own words")

    // A reminder with a date and no time arrives with its day's last minute as
    // `due`. The agenda never shows that minute as if the person had picked it.
    let lastMinute = dayEnd - 60
    let dateOnlyToday = window([], now: now, needs: [DayNeedInput(id: "rent", title: "Pay rent", source: "reminders",
                                                                  due: lastMinute, allDay: true)])
    let dateOnlyRow = dateOnlyToday.items.first { $0.id == "need:rent" }
    check(dateOnlyRow?.style == .needsYou && dateOnlyRow?.allDay == true && dateOnlyRow?.detail == "Due today · Needs you",
          "a date-only reminder is due today, not at a time: \(dateOnlyRow?.detail ?? "missing")")
    check(dateOnlyRow?.due == lastMinute && dateOnlyRow?.start == lastMinute - 30 * 60,
          "it keeps its place at the end of the day")
    let dateOnlyLate = window([], now: now, needs: [DayNeedInput(id: "rent", title: "Pay rent", source: "reminders",
                                                                 due: dayStart - 60, allDay: true)])
    let dateOnlyLateRow = dateOnlyLate.items.first { $0.id == "need:rent" }
    check(dateOnlyLateRow?.style == .overdue && dateOnlyLateRow?.start == now
          && dateOnlyLateRow?.detail == "Overdue since earlier",
          "yesterday's date-only reminder is overdue since that day, with no time: \(dateOnlyLateRow?.detail ?? "missing")")
    var dateOnlyWords: [String] = []
    for row in [dateOnlyRow, dateOnlyLateRow] {
        guard let row else { check(false, "a date-only reminder is on the agenda"); continue }
        let column = DayTimeline.gutter(row, clock: clock)
        dateOnlyWords += [row.detail, column.top, column.bottom,
                          DayTimeline.agendaDetail(row, now: now, isToday: true, clock: clock)]
    }
    check(dateOnlyWords == ["Due today · Needs you", "All day", "Due", "Needs you",
                            "Overdue since earlier", "Overdue", "", "Since earlier"],
          "a date-only reminder reads All day over Due, then overdue since its day: \(dateOnlyWords)")
    check(!dateOnlyWords.contains { $0.contains("23:59") }, "the stand-in last minute is never shown: \(dateOnlyWords)")
    let dateOnlyLastMinute = window([], now: dayEnd - 30, needs: [DayNeedInput(id: "rent", title: "Pay rent", source: "reminders",
                                                                               due: lastMinute, allDay: true)])
    let dateOnlyLastMinuteRow = dateOnlyLastMinute.items.first { $0.id == "need:rent" }
    check(dateOnlyLastMinuteRow?.detail == "Overdue since today",
          "in the day's last minute it still names the day, not the minute")
    let dateOnlyOtherDay = DayTimeline.build(blocks: [], allDay: [],
                                             needs: [DayNeedInput(id: "rent", title: "Pay rent", source: "reminders",
                                                                  due: lastMinute, allDay: true)],
                                             caught: [], now: dayStart - 5 * hour, dayStart: dayStart, dayEnd: dayEnd,
                                             workStart: at(9), workEnd: at(18), isToday: false, clock: clock, dayClock: dayClock)
    let dateOnlyOtherDayRow = dateOnlyOtherDay.items.first { $0.id == "need:rent" }
    check(dateOnlyOtherDayRow?.detail == "Due that day · Needs you", "on another day it is due that day")
    // A reminder with a time of its own is unchanged, even at the same minute.
    check(item("need:n3")?.detail == "Due 20:00 · Needs you" && item("need:n3")?.allDay == false,
          "a timed reminder keeps its time")
    let timedLastMinute = window([], now: now, needs: [DayNeedInput(id: "late", title: "Submit form", source: "reminders",
                                                                    due: lastMinute)])
    let timedRow = timedLastMinute.items.first { $0.id == "need:late" }
    let timedColumn = timedRow.map { DayTimeline.gutter($0, clock: clock) }
    check(timedRow?.detail == "Due 23:59 · Needs you" && timedRow?.allDay == false, "a reminder set for 11:59 PM on purpose keeps its time")
    check(timedColumn?.top == "23:59" && timedColumn?.bottom == "Due", "and its column still shows 11:59 PM over Due")
    // The badge and Still to fit count a date-only reminder exactly as before.
    check(DayTimeline.badgeCount([DayNeedInput(id: "rent", title: "Pay rent", source: "reminders", due: lastMinute, allDay: true)],
                                 endOfDay: dayEnd) == 1, "a date-only reminder due today still counts on the badge")
    check(DayTimeline.freeLine(start: now, end: at(13.75), startsNow: true, clock: clock) == "Free until 13:45 · 38 min",
          "free time from now says until when")
    check(DayTimeline.freeLine(start: at(17.5), end: at(18), startsNow: false, clock: clock) == "Free 17:30 – 18:00 · 30 min",
          "free time later says when it starts, too")
    check(DayTimeline.length(25) == "25 min" && DayTimeline.length(60) == "1 h" && DayTimeline.length(135) == "2 h 15 min",
          "lengths read as hours and minutes")
    // What just ended folds into one line; what runs now leads, listed at its real start.
    let recentAgenda = recent.agenda(now: now, isToday: true)
    check(recentAgenda.earlier.map(\.id) == ["block:coffee"] && DayTimeline.earlierLine(recentAgenda.earlier) == "Coffee",
          "what ended in the last two hours is earlier")
    check(rows(recentAgenda) == ["block:lunch", "free 13:30–15:00", "block:late"],
          "a block on now comes first, and free time starts when it ends: \(rows(recentAgenda))")
    let workshop = window([DayBlockInput(id: "workshop", title: "Workshop", start: at(10), end: at(14), kind: "calendar")],
                          now: now).items.first!
    check(workshop.start == at(11) && DayTimeline.gutter(workshop, clock: clock) == ("10:00", "14:00"),
          "the column shows an event's own start, not where the lane cut it")
    check(DayTimeline.agendaDetail(workshop, now: now, isToday: true, clock: clock) == "Calendar · 53 min left",
          "minutes left round up")
    let openEnded = window([DayBlockInput(id: "call", title: "Call", start: at(15), end: nil, kind: "calendar")], now: now).items.first!
    check(DayTimeline.gutter(openEnded, clock: clock) == ("15:00", "")
          && DayTimeline.agendaDetail(openEnded, now: now, isToday: true, clock: clock) == "Calendar",
          "an event with no end shows none")
    // Free time from now counts from 15 minutes; between things, from 30.
    let soon = window([DayBlockInput(id: "a", title: "A", start: at(13.5), end: at(14), kind: "calendar"),
                       DayBlockInput(id: "b", title: "B", start: at(14.4), end: at(15), kind: "calendar")], now: now)
    check(rows(soon.agenda(now: now, isToday: true)) == ["free 13:07–13:30", "block:a", "block:b"],
          "23 minutes from now is named; a 24-minute gap later is not")
    // A to-do that happens at its time holds it; one due by a time doesn't.
    let withCall = DayTimeline.build(
        blocks: [DayBlockInput(id: "lunch", title: "Lunch", start: at(12), end: at(13.5), kind: "calendar"),
                 DayBlockInput(id: "dinner", title: "Dinner", start: at(18), end: at(19), kind: "calendar")],
        allDay: [], needs: [], caught: [], now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
        isToday: true, clock: clock, dayClock: dayClock,
        todos: [DayTodoInput(id: "call", title: "Call Jo", source: "messages", who: "Jo", due: at(16.25), kind: "call"),
                DayTodoInput(id: "form", title: "Send the form", source: "mail", who: "", due: at(17), kind: "task")])
    check(rows(withCall.agenda(now: now, isToday: true))
          == ["block:lunch", "free 13:30–16:15", "todo:call", "free 16:45–18:00", "todo:form", "block:dinner"],
          "a call splits the free time; a form due at 17:00 doesn't: \(rows(withCall.agenda(now: now, isToday: true)))")
    let grouped = window([DayBlockInput(id: "reviews", title: "Review 3 emails · Devarsh, Maya +1", start: at(14),
                                        end: at(14.5), kind: "suggestion", evidenceID: "e1",
                                        members: [DayMember(title: "Week 4", evidenceID: "e1"),
                                                  DayMember(title: "Lab", evidenceID: "e2"),
                                                  DayMember(title: "Budget", evidenceID: "e3")])], now: now)
    check(grouped.items.first?.members.count == 3 && grouped.items.first?.detail == "Suggested · 3 to read · about 30 min",
          "a grouped review says how many it covers: \(grouped.items.first?.detail ?? "none")")

    // Another day: the lane starts at working hours, and nothing is overdue.
    let other = DayTimeline.build(blocks: blocks, allDay: [], needs: needs, caught: [], now: dayStart - 30 * hour,
                                  dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                  isToday: false, clock: clock, dayClock: dayClock)
    check(other.from == at(9) && other.items.contains { $0.id == "block:standup" }, "another day starts at working hours")
    let otherAgenda = other.agenda(now: dayStart - 30 * hour, isToday: false)
    check(otherAgenda.earlier.isEmpty && rows(otherAgenda) == ["block:standup", "free 10:00–13:45", "block:pa2", "block:club",
                                                              "block:away", "free 17:30–18:00", "block:task:t1", "need:n3"],
          "another day lists all of it, free time counted from the first thing: \(rows(otherAgenda))")
    check(!other.items.contains { $0.style == .overdue }, "nothing is overdue on another day")

    // The badge counts only what is due today or overdue.
    check(DayTimeline.badgeCount(needs, endOfDay: dayEnd) == 3, "badge: due today and overdue, not junk, undated or calendar")
    check(DayTimeline.badgeCount([], endOfDay: dayEnd) == 0, "no items, no badge")

    // Junk reminders.
    for junk in ["nothing", " Nothing. ", "N/A", "", "--", "ok", "None!", "x"] {
        check(DayTimeline.isJunk(junk), "junk: \"\(junk)\"")
    }
    for real in ["PA 2", "Call mom", "Get DoorDash order", "Dinner with Jo?", "茶", "Test", "Reminder"] {
        check(!DayTimeline.isJunk(real), "not junk: \(real)")
    }

    // Planner warnings in plain words.
    check(DayTimeline.plainWarning("Calendar is not current. Flexible tasks are waiting for a successful sync.")
          == "Wisp is still reading your calendar, so I'm waiting before placing tasks.", "calendar sync warning")
    check(DayTimeline.plainWarning("Tentative · You're usually at the gym") == nil, "the hatch already says tentative")
    check(DayTimeline.plainWarning("Suggested time · duration is an estimate") == nil, "the dash already says suggested")
    check(DayTimeline.plainWarning("Overlaps Club") == "Overlaps Club.", "overlap warning")
    check(DayTimeline.plainWarning("Pinned time is in the past; mark done or move it")
          == "This time has passed. Move it or mark it done.", "past pin warning")
    check(DayTimeline.plainWarning("Something new") == "Something new", "an unknown warning passes through")
    check(DayTimeline.plainReason("Needs 90 uninterrupted minutes; largest available gap before the deadline or end of working hours is 40 minutes")
          == "no gap long enough (the longest is 40\u{00A0}min)", "unscheduled reason: short, the number kept with its unit")

    // The Day blurb.
    let blurb = DayTimeline.blurb(timeline, needs: needs, isToday: true, endOfDay: dayEnd, clock: clock)
    check(blurb == "PA 2 is the main thing today. I've given it one block, around when you're usually away. Clear after 20:00.",
          "blurb: \(blurb)")
    let empty = DayTimeline.build(blocks: [], allDay: [], needs: [], caught: [], now: now, dayStart: dayStart,
                                  dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: true,
                                  clock: clock, dayClock: dayClock)
    check(DayTimeline.blurb(empty, needs: [], isToday: true, endOfDay: dayEnd, clock: clock)
          == "Nothing is planned for the rest of today.", "an empty day says so")
    check(empty.items.isEmpty && empty.to == at(18), "an empty day still shows working hours")
    check(empty.agenda(now: now, isToday: true) == DayAgenda(earlier: [], ahead: []), "an empty day lists nothing")

    // The Brief's sentence.
    check(DayTimeline.lede(weekday: "Saturday", isToday: true, load: .light, dueToday: ["PA 2"],
                           overdue: ["Get DoorDash order"], next: nil)
          == "A light Saturday. PA 2 is due today, and Get DoorDash order is still open.", "lede with due and overdue")
    check(DayTimeline.lede(weekday: "Sunday", isToday: true, load: .free, dueToday: [], overdue: [], next: nil)
          == "A free Sunday. Nothing needs you right now.", "lede on a free day")
    check(DayTimeline.lede(weekday: "Monday", isToday: true, load: .packed, dueToday: ["nothing"], overdue: ["Pay rent", "Return book"],
                           next: (title: "Club", time: "3 PM"))
          == "A packed Monday. Two overdue reminders are still open.",
          "lede drops junk and counts overdue")
    check(DayTimeline.lede(weekday: "Tuesday", isToday: true, load: .steady, dueToday: [], overdue: [],
                           next: (title: "Club", time: "3 PM"))
          == "A steady Tuesday. Next up: Club at 3 PM.", "lede with only the next event")
    check(DayTimeline.lede(weekday: "Sunday", isToday: true, load: .free, dueToday: [], overdue: [], next: nil,
                           othersNeedYou: true) == "A free Sunday.",
          "lede never says nothing needs you while the Needs you line lists something")

    // The Brief's Needs you line under the lede (daily_workspace.needs_you picks the items).
    let picked = [
        NeedsYouInput(kind: "overdue", text: "Pay rent", due: dayStart - 5 * hour),
        NeedsYouInput(kind: "due", text: "PA 2", due: at(23.5)),
        NeedsYouInput(kind: "due", text: "Library book", due: at(0), allDay: true),
        NeedsYouInput(kind: "mail", text: "a deadline notice from Dr. Patel"),
    ]
    check(DayTimeline.needsYou(picked, more: 2, clock: clock)
          == "Needs you today: Pay rent (overdue), PA 2 (due 23:30), Library book (due today), a deadline notice from Dr. Patel and 2 more.",
          "needs-you line lists items with their due words and the rest as a count")
    check(DayTimeline.needsYou([NeedsYouInput(kind: "text", text: "a text from Mom")], more: 0, clock: clock)
          == "Needs you today: a text from Mom.", "needs-you line with one item")
    check(DayTimeline.needsYou([NeedsYouInput(kind: "plan", text: "Finish slides"), NeedsYouInput(kind: "mail", text: "“Lab feedback” from Dr. Patel")],
                               more: 0, clock: clock)
          == "Needs you today: Finish slides and “Lab feedback” from Dr. Patel.", "needs-you line joins two with and")
    check(DayTimeline.needsYou([], more: 0, clock: clock) == nil, "nothing to list: no line, not an empty header")
    check(DayTimeline.needsYou([NeedsYouInput(kind: "due", text: "nothing", due: at(15))], more: 0, clock: clock) == nil,
          "a junk title alone makes no line")

    let structuredNeeds = DayTimeline.needsYouList([
        NeedsYouInput(id: "late", kind: "overdue", text: "Pay rent", due: dayStart - 5 * hour),
        NeedsYouInput(id: "timed", kind: "due", text: "PA 2", due: at(23.5)),
        NeedsYouInput(id: "all-day", kind: "due", text: "Library book", due: at(0), allDay: true),
        NeedsYouInput(id: "no-date", kind: "due", text: "Date withheld"),
        NeedsYouInput(id: "task", kind: "plan", text: "Finish slides"),
        NeedsYouInput(id: "mail", kind: "mail", text: "“Lab feedback” from Dr. Patel"),
        NeedsYouInput(id: "text", kind: "text", text: "a text from Mom"),
    ], more: 2, clock: clock)
    check(structuredNeeds?.rows.map(\.id) == ["late", "timed", "all-day", "no-date", "task", "mail", "text"],
          "structured needs-you keeps service item identities and order")
    check(structuredNeeds?.rows.map(\.label) == ["OVERDUE", "DUE 23:30", "DUE TODAY", "DUE", "PLAN", "MAIL", "TEXT"],
          "structured needs-you uses readable labels and leaves missing dates unspecified")
    check(structuredNeeds?.rows.map(\.text) == ["Pay rent", "PA 2", "Library book", "Date withheld", "Finish slides",
                                                "“Lab feedback” from Dr. Patel", "a text from Mom"],
          "structured needs-you preserves each selected item as a separate row")
    check(structuredNeeds?.rows.map(\.overdue) == [true, false, false, false, false, false, false],
          "only overdue rows receive overdue emphasis")
    check(structuredNeeds?.more == 2, "structured needs-you retains the service's additional-item count")
    check(DayTimeline.needsYouList([NeedsYouInput(kind: "due", text: "Date withheld")], more: -1, clock: clock)?.more == 0,
          "structured needs-you never displays a negative additional-item count")
    check(DayTimeline.needsYouList([NeedsYouInput(kind: "due", text: "nothing")], more: 4, clock: clock) == nil,
          "junk-only selected items do not create a needs-you section")

    // Each row's actions: Clear for any row the service fingerprinted, Mark done
    // only for a reminder it can complete; an older service's rows offer nothing.
    let actionable = DayTimeline.needsYouList([
        NeedsYouInput(id: "reminders:r", kind: "overdue", text: "Wisp verification timed 2113", due: dayStart - 5 * hour,
                      fingerprint: "f1", canComplete: true),
        NeedsYouInput(id: "email:m", kind: "mail", text: "“Lab feedback” from Dr. Patel", fingerprint: "f2"),
        NeedsYouInput(id: "wisp:t", kind: "plan", text: "Finish slides", fingerprint: "f3"),
        NeedsYouInput(id: "reminders:old", kind: "due", text: "No fingerprint", due: at(20), canComplete: true),
        NeedsYouInput(kind: "text", text: "a text from Mom", fingerprint: "f5"),
    ], more: 0, clock: clock)
    check(actionable?.rows.map(\.actions) == [[.clear, .markDone], [.clear], [.clear], [], []],
          "row actions: \(actionable?.rows.map(\.actions) ?? [])")
    check(actionable?.rows.first?.fingerprint == "f1" && actionable?.rows.first?.dismissalKey == "reminders:r#f1",
          "a row keeps the service's fingerprint for Clear")
    check(NeedsYouRowAction.clear.path == "clear" && NeedsYouRowAction.markDone.path == "done",
          "the two service paths")
    check(NeedsYouRowAction.clear.accessibilityLabel("Pay rent") == "Clear “Pay rent” from Needs you"
          && NeedsYouRowAction.markDone.accessibilityLabel("Pay rent") == "Mark “Pay rent” done in Reminders",
          "each action names its row for VoiceOver")
    check(NeedsYouRowAction.clear.help("x").contains("stays in Reminders"), "Clear says it never touches the reminder")

    // The Brief's rows: always CAL, MAIL, TEXTS and SOON; a source Wisp can't read is dim.
    let utc = TimeZone(identifier: "UTC")!
    let light = DayLoad.assess(events: [], allDay: ["PA 2 [CMP-140-04]"], from: now, dayStart: dayStart, isToday: true, zone: utc)
    func mailOf(_ titles: [String]) -> [BriefNeed] { titles.map { BriefNeed(title: $0) } }
    let lines = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: []),
                                       texts: (ready: true, needing: mailOf(["nothing"]) + [BriefNeed(title: "Jo: dinner at 7?", sender: "Jo")]),
                                       soon: .item(title: "UCSC startup club", when: "Wed 6 PM", source: "calendar"))
    check(lines.map(\.tag) == ["CAL", "MAIL", "TEXTS", "SOON"], "four rows in order")
    check(lines[0].text == "Light day: just PA 2 [CMP-140-04] (all day)" && lines[0].trailing == ""
          && lines[1].text == "Nothing needs a reply" && lines[2].text == "Jo is waiting on you"
          && lines[3].source == .calendar && lines[3].trailing == "Wed 6 PM", "row contents: \(lines.map(\.text))")
    check(!lines.contains(where: \.muted), "synced sources are never dim")
    let quiet = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: []), texts: (ready: true, needing: mailOf(["nothing"])),
                                       soon: .nothing)
    check(quiet.map(\.text).suffix(2) == ["No texts need you", "Nothing in the next week"] && !quiet.contains(where: \.muted),
          "TEXTS and SOON stay with nothing to say")
    let unsynced = DayTimeline.briefLines(calendar: nil, calendarUnsynced: true, mail: (ready: false, needing: mailOf(["x"])),
                                          texts: (ready: false, needing: []), soon: .unknown)
    check(unsynced.map(\.tag) == ["CAL", "MAIL", "TEXTS", "SOON"] && unsynced.allSatisfy(\.muted)
          && unsynced.map(\.text) == ["Reading your calendar…", "Mail hasn't been checked yet",
                                      "Messages haven't been checked yet", "The week ahead hasn't loaded yet"],
          "unread sources keep the four rows, dim: \(unsynced.map(\.text))")
    let noAccess = DayTimeline.briefLines(calendar: light, mail: nil, texts: (ready: false, needing: []), textsNeedAccess: true,
                                          soon: .nothing)[2]
    check(noAccess.text == "Texts need Full Disk Access" && !noAccess.muted, "missing Full Disk Access is said plainly, not 'not checked yet'")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: [
        BriefNeed(title: "Office hours", sender: "John", received: 2), BriefNeed(title: "Canceled: Office hours", sender: "John", received: 1)]),
        texts: nil, soon: .nothing)[1].text == "Canceled: John · Office hours ×2", "a cancellation isn't hidden by its original")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: [BriefNeed(title: "Private details omitted.")]),
                                 texts: nil, soon: .nothing)[1].text == "1 private email not shown", "only private mail: counted")
    let unread = DayTimeline.briefLines(calendar: nil, mail: nil, texts: nil, soon: .nothing)
    check(unread.map(\.text) == ["Nothing on your calendar", "Mail hasn't been checked yet", "Messages haven't been checked yet",
                                 "Nothing in the next week"] && unread[1].muted && unread[2].muted,
          "no coverage (the workspace couldn't be read) is unknown, never off")
    // MAIL says what the top email is about; TEXTS says who is waiting, never what they wrote.
    let described = DayTimeline.briefLines(calendar: nil, mail: (ready: true, needing: [
        BriefNeed(title: "Lab report", sender: "Lab partner", person: true, received: 100),
        BriefNeed(title: "Thesis meeting", sender: "Prof. Lee", person: true, received: 200),
        BriefNeed(title: "Tech Week starts today", sender: "UCSC Events", received: 300)]),
        texts: (ready: true, needing: [BriefNeed(title: "Mom: call me", sender: "Mom", received: 50),
                                       BriefNeed(title: "Jo: dinner?", sender: "Jo", received: 90)]), soon: .nothing)
    check(described[1].text == "Prof. Lee · Thesis meeting" && described[1].trailing == "+2 more",
          "MAIL from an older service leads with the newest person's email: \(described[1].text) \(described[1].trailing)")
    // With overviews: the gist and how many need you / are waiting, with nothing trailing.
    let overviewed = DayTimeline.briefLines(calendar: nil, mail: (ready: true, needing: [
        BriefNeed(title: "Lab report", sender: "Lab partner", person: true, received: 100),
        BriefNeed(title: "50% off everything", sender: "Shop", received: 300)]),
        texts: (ready: true, needing: [BriefNeed(title: "Mom: call me", sender: "Mom", received: 50)]),
        mailOverview: "Mostly lab work and promos", textsOverview: "Mostly family check-ins", soon: .nothing)
    check(overviewed[1].text == "Mostly lab work and promos · 1 needs you" && overviewed[1].trailing.isEmpty
          && overviewed[2].text == "Mostly family check-ins · 1 waiting on you",
          "MAIL and TEXTS rows with overviews: \(overviewed[1].text) / \(overviewed[2].text)")
    check(DayTimeline.briefLines(calendar: nil, mail: nil, texts: (ready: false, needing: []), textsNeedAccess: true,
                                 textsOverview: "Quiet", soon: .nothing)[2].text == "Texts need Full Disk Access",
          "Full Disk Access wins over an overview")
    check(described[2].text == "Jo and Mom are waiting on you" && !described[2].text.contains("dinner"),
          "TEXTS names who is waiting, newest first: \(described[2].text)")
    check(DayTimeline.briefLines(calendar: light, mail: nil, texts: nil, soon: .item(title: "nothing", when: "Fri", source: "reminders"))[3]
          .text == "Nothing in the next week", "a placeholder is never what's coming")

    // Plan time on an overdue reminder (the "Get doordash order" case): the task
    // keeps the reminder's due time as its origin, and a second tap adds nothing.
    let doordashDue = now - 16 * 86400
    check(DayTimeline.planOrigin(due: doordashDue, now: now) == doordashDue, "an overdue reminder's task starts at its due time")
    check(DayTimeline.planOrigin(due: now + 3600, now: now) == nil && DayTimeline.planOrigin(due: nil, now: now) == nil,
          "a reminder not yet due, or undated, gives no origin")
    check(DayTimeline.alreadyPlanned("Get doordash order", tasks: [(title: "get  DoorDash order", active: true)])
          && DayTimeline.alreadyPlanned("Reply to Jo", tasks: [(title: "Review: Reply to Jo", active: true)])
          && !DayTimeline.alreadyPlanned("Get doordash order", tasks: [(title: "Get doordash order", active: false)])
          && !DayTimeline.alreadyPlanned("Get doordash order", tasks: [(title: "Call the bank", active: true)]),
          "Plan time isn't offered twice for the same need")
    // Remove on a suggested block sends the item it came from, or each item a review covers.
    let removeOne = DayItem(id: "block:s", title: "PA 2", detail: "", start: at(14), end: at(15), style: .suggested,
                         evidenceID: "reminders:aaa")
    check(DayTimeline.removalIDs(removeOne) == ["reminders:aaa"], "Remove sends the suggestion's item")
    let removeGroup = DayItem(id: "block:g", title: "Review 2 emails", detail: "", start: at(14), end: at(15), style: .suggested,
                          evidenceID: "email:a", members: [DayMember(title: "A", evidenceID: "email:a"),
                                                           DayMember(title: "B", evidenceID: "email:b")])
    check(DayTimeline.removalIDs(removeGroup) == ["email:a", "email:b"], "Remove on a grouped review sends every email in it")
    check(DayTimeline.removalIDs(DayItem(id: "block:c", title: "Standup", detail: "", start: at(9), end: at(10),
                                         style: .calendar, evidenceID: "calendar:x")).isEmpty,
          "a calendar block is never removed as a suggestion")
    check(DayTimeline.removedNotice(removeOne) == "Removed “PA 2” from your day", "Undo bar names what Remove took")
    check(DayTimeline.removedNotice(removeGroup) == "Removed 2 emails from your day",
          "Undo bar on a grouped review counts every email it brings back")
    check(DayTimeline.bringBackLabel(blocks: 0) == nil, "no Bring back entry when nothing today was removed")
    check(DayTimeline.bringBackLabel(blocks: 1) == "Bring back today's removed suggestion"
          && DayTimeline.bringBackLabel(blocks: 3) == "Bring back today's 3 removed suggestions",
          "Bring back counts today's removed blocks, which is all it restores")
    check(DayTimeline.undoFailedNotice(stillShowing: true).contains("Try Undo again")
          && DayTimeline.undoFailedNotice(stillShowing: false).contains("Bring it back"),
          "a failed Undo of an older Remove doesn't promise its gone Undo bar")
    check(DayTimeline.plannedTitle("Reply to Jo", review: true) == "Review: Reply to Jo"
          && DayTimeline.plannedTitle("PA 2", review: false) == "PA 2", "planned titles")

    // Unmistakable bulk mail never counts as needing you, and the row says it was left out.
    let inbox = ["You're subscribed: The Download", "Can you review my draft?", "50% off everything",
                 "Weekly digest: CMP 140", "Lab moved to Thursday", "nothing"]
    check(DayTimeline.mailNeedingYou(inbox) == ["Can you review my draft?", "Lab moved to Thursday"],
          "newsletters, promos and placeholders are left out: \(DayTimeline.mailNeedingYou(inbox))")
    let promos = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(inbox)), texts: nil, soon: .nothing)[1]
    check(promos.text == "Can you review my draft?" && promos.trailing == "+1 more", "the MAIL row describes real mail: \(promos.text)")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(["Can you review my draft?", "50% off everything"])),
                                 texts: nil, soon: .nothing)[1].trailing == "+1 promo", "one email: the row names the promo it left out")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(["Can you review my draft?"])), texts: nil,
                                 soon: .nothing)[1].trailing == "", "no promos, no note")
    for personal in ["Last chance to submit your thesis form", "Webinar slides for Thursday", "Tech Week starts today",
                     "Don't miss the lab meeting", "Re: project offer", "Meeting with Jo", "This week in CMP 140"] {
        check(!DayTimeline.isBulkMail(personal), "a subject a person could write still counts: \(personal)")
    }
    check(DayTimeline.isBulkMail("Last chance: 20% off") && DayTimeline.isBulkMail("Our October newsletter")
          && DayTimeline.isBulkMail("Thanks for subscribing!"), "unmistakable bulk mail")

    briefSummaryChecks(check, dayStart: dayStart, now: now)

    dayTodoChecks(check, dayStart: dayStart, now: now, clock: clock, dayClock: dayClock)
    dayWeekChecks(check, dayStart: dayStart, now: now)
    weekRouteChecks(check)
    dayNextChecks(check, dayStart: dayStart, clock: clock)

    // How busy the calendar is: one verdict for the CAL line and the lede.
    func event(_ title: String, _ start: Double, _ end: Double?) -> DayLoad.Event {
        DayLoad.Event(title: title, start: at(start), end: end.map(at))
    }
    func load(_ events: [DayLoad.Event], allDay: [String] = [], from: Double = at(8), isToday: Bool = true) -> DayLoad {
        DayLoad.assess(events: events, allDay: allDay, from: from, dayStart: dayStart, isToday: isToday, zone: utc)
    }
    let free = load([], isToday: false)
    check(free.verdict == .free && free.summary == "Free all day", "free: \(free.summary)")
    check(load([], from: at(14)).summary == "Free for the rest of today", "free for the rest of today")
    check(light.verdict == .light, "an all-day entry alone is a light day")
    let one = load([event("Club", 15, 16)])
    check(one.verdict == .light && one.summary == "Light day: just Club at 3 PM" && one.next == "3 PM", "one meeting: \(one.summary)")
    let afternoon = load([event("Lab", 13, 14), event("Office hours", 14.5, 15.5), event("Review", 16, 17)])
    check(afternoon.verdict == .busy && afternoon.summary == "Busy afternoon: 3 meetings, 1–5 PM",
          "busy afternoon: \(afternoon.summary) (\(afternoon.occupiedHours) h)")
    let packed = load([event("Lab", 10, 11), event("Office hours", 11, 12), event("Review", 12.1, 13), event("Sync", 13, 14),
                       event("Design", 14, 15), event("Retro", 15.05, 16)])
    check(packed.verdict == .packed && packed.summary == "Packed: back-to-back 10 AM–4 PM" && packed.backToBack == 5,
          "packed: \(packed.summary) pairs \(packed.backToBack)")
    let guessed = load([event("Lab", 13, 14), event("Office hours", 14, nil), event("Review", 15, 17)])
    check(guessed.uncertain && guessed.summary == "Busy afternoon: 3 meetings, about 1–5 PM", "unknown end reads about: \(guessed.summary)")
    let steady = load([event("Lab", 9, 10), event("Office hours", 12, 13), event("Review", 16.5, 17)])
    check(steady.verdict == .steady && steady.summary == "Steady: 3 meetings, longest free stretch 3.5 h"
          && steady.largestGapHours == 3.5, "steady: \(steady.summary)")
    let over = load([event("Standup", 9, 10), event("Club", 15, 16)], from: at(13))
    check(over.meetings == 1 && over.verdict == .light, "finished meetings don't count")
    let evening = load([event("Dinner", 19, 21), event("Show", 21, 23), event("Late", 23, 23.5), event("Call", 18, 19)])
    check(evening.occupiedHours == 0 && evening.verdict == .busy && evening.summary.hasPrefix("Busy evening:"),
          "evening meetings fall outside 9–6 but still count: \(evening.summary)")
    let allDayLong = load([event("Conference", 9, 17)])
    check(allDayLong.summary == "Packed: 1 meeting, 8 h booked", "one long event: \(allDayLong.summary)")
    let late = load([event("Dinner", 19, 20), event("Show", 20.5, 22), event("Call", 22.5, 23)], from: at(18.5))
    check(late.verdict == .steady && late.summary == "Steady: 3 meetings from 7 PM", "after 6 PM: \(late.summary)")
    let lateAfternoon = load([event("Dinner", 19, 20), event("Show", 20.5, 22), event("Call", 22.5, 23)], from: at(17.5))
    check(lateAfternoon.summary == "Steady: 3 meetings from 7 PM", "between 5 and 6 PM: \(lateAfternoon.summary)")
    let backwards = load([event("Lab", 13, 12)])
    check(backwards.uncertain && backwards.meetings == 1, "an end before the start counts as an hour and reads about")
    let nested = load([event("Workshop", 13, 16), event("Talk", 13.5, 14)])
    check(nested.summary.hasSuffix("1–4 PM"), "a nested meeting doesn't reverse the span: \(nested.summary)")
    check(DayTimeline.lede(weekday: "Friday", isToday: true, load: nil, dueToday: [], overdue: [], next: nil)
          == "It's Friday. Nothing needs you right now.", "no calendar, no verdict")
    check(DayTimeline.lede(weekday: "Sunday", isToday: false, load: free.verdict, dueToday: [], overdue: [], next: nil)
          .hasPrefix("A free Sunday.") && free.summary.hasPrefix("Free"), "the lede and the line share the word")
    let la = TimeZone(identifier: "America/Los_Angeles")!
    var cal = Calendar(identifier: .gregorian)
    cal.timeZone = la
    let springForward = cal.date(from: DateComponents(year: 2027, month: 3, day: 14))!.timeIntervalSince1970
    check(DayLoad.at(9, springForward, la) - springForward == 8 * 3600, "9 AM on the spring-forward day is 8 hours after midnight")
    check(DayLoad.union([(1, 3), (2, 5), (7, 8), (8, 9)]).map { [$0.0, $0.1] } == [[1, 5], [7, 9]], "union merges touching spans")
    check(DayLoad.span(at(10), at(16), zone: utc) == "10 AM–4 PM" && DayLoad.span(at(13), at(17.5), zone: utc) == "1–5:30 PM",
          "spans")
    let gb = Locale(identifier: "en_GB")
    check(DayLoad.span(at(13), at(17.5), zone: utc, locale: gb) == "13:00–17:30" && DayLoad.clock(at(9), zone: utc, locale: gb) == "09:00",
          "a 24-hour locale drops AM/PM")
    check(WispClock.hour(15, locale: gb) == "15:00" && WispClock.hour(15, locale: WispClock.fixedLocale) == "3 PM"
          && WispClock.hour(0, locale: WispClock.fixedLocale) == "12 AM", "hour labels follow the locale")
    check(WispClock.time(at(15.5), zone: utc, locale: gb) == "15:30" && WispClock.time(at(15.5), zone: utc, locale: WispClock.fixedLocale) == "3:30 PM",
          "clock times follow the locale")

    // Study (docs/STUDY_TAB.md): a study block is drawn as your own plan, never as
    // a Calendar event; a school deadline is due at its time; SCHOOL follows CAL.
    let studied = window([DayBlockInput(id: "study-block:canvas:1:2", title: "Study: Lab 5", start: at(15), end: at(15.75),
                                        kind: "study")],
                         now: now, needs: [DayNeedInput(id: "study:canvas:1:2", title: "Lab 5 · CS 101", source: "study",
                                                        due: at(23 + 59.0 / 60))])
    let studyBlock = studied.items.first { $0.id == "block:study-block:canvas:1:2" }
    check(studyBlock?.style == .task && studyBlock?.detail == "Study block · 45 min" && studyBlock?.taskID == nil,
          "a study block is a plan row, not a calendar event: \(studyBlock?.detail ?? "missing")")
    check(studyBlock.map { DayTimeline.agendaDetail($0, now: now, isToday: true, clock: clock) } == "Study block · 45 min",
          "a study block's agenda line names it")
    let studyDue = studied.items.first { $0.id == "need:study:canvas:1:2" }
    check(studyDue?.style == .needsYou && studyDue?.detail == "Due 23:59 · Needs you" && studyDue?.due == at(23 + 59.0 / 60),
          "a school deadline is due at its time: \(studyDue?.detail ?? "missing")")
    let schoolLines = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: []), texts: (ready: true, needing: []),
                                             soon: .nothing, school: " 2 due today: Lab 5 (CS 101), Essay draft ")
    check(schoolLines.map(\.tag) == ["CAL", "SCHOOL", "MAIL", "TEXTS", "SOON"]
          && schoolLines[1].text == "2 due today: Lab 5 (CS 101), Essay draft" && !schoolLines[1].muted,
          "SCHOOL follows CAL when school work is due: \(schoolLines.map(\.tag))")
    check(DayTimeline.briefLines(calendar: light, mail: nil, texts: nil, soon: .nothing, school: "  ").map(\.tag)
            == ["CAL", "MAIL", "TEXTS", "SOON"], "no school line when nothing is due")
    let schoolGlance = BriefTiles.glance(schoolLines, mailProse: nil)
    check(schoolGlance.map(\.leading) == ["School", "Mail", "Texts"] && schoolGlance.first?.id == "glance-SCHOOL",
          "the default Brief view lists School over Mail and Texts: \(schoolGlance.map(\.leading))")

    // The `study` object of /assistant/today, tolerant of missing and odd fields.
    let studyJSON = #"""
    {"due_today": [{"id": "canvas:1:2", "title": "Lab 5", "course_name": "CS 101", "due_ts": 1794700740.0,
                    "all_day": false, "kind": "assignment", "status": "upcoming"},
                   {"id": "x", "title": "Essay draft", "course_name": null, "due_ts": null}],
     "due_tomorrow_count": 1, "due_week_count": 4, "missing_count": 0,
     "next_test": {"id": "canvas:1:9", "title": "Midterm", "course_name": "CS 101", "due_ts": 1794900000.0},
     "line": "2 due today: Lab 5 (CS 101), Essay draft", "sample": false, "brand_new": [1, 2]}
    """#
    let summary = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(studyJSON.utf8))
    check(summary?.dueToday.map(\.title) == ["Lab 5", "Essay draft"] && summary?.dueToday.first?.courseName == "CS 101"
          && summary?.dueToday.first?.dueTS == 1794700740.0 && summary?.dueToday.last?.courseName == ""
          && summary?.dueToday.last?.dueTS == nil, "study due items decode, nulls as defaults")
    check(summary?.dueTomorrowCount == 1 && summary?.dueWeekCount == 4 && summary?.missingCount == 0
          && summary?.nextTest?.title == "Midterm" && summary?.briefLine == "2 due today: Lab 5 (CS 101), Essay draft"
          && summary?.sample == false, "the study summary's counts, next test and line")
    let bare = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(#"{"line": "", "next_test": null}"#.utf8))
    check(bare == StudyDaySummary() && bare?.briefLine == nil, "an empty study summary says nothing")

    // This week's school work for Brief and Day (`upcoming`, `upcoming_more`).
    // An older service sends neither key: no rows, the line as before.
    check(summary?.upcoming == [] && summary?.upcomingMore == 0 && summary?.briefUpcoming.isEmpty == true
          && summary?.briefHeadline == "2 due today: Lab 5 (CS 101), Essay draft"
          && summary?.dayUpcoming(dayStart: dayStart, dayEnd: dayEnd).isEmpty == true,
          "an older study summary without upcoming decodes to no rows")
    check(bare?.briefHeadline == "" && bare?.briefUpcoming.isEmpty == true, "no line and nothing upcoming hides SCHOOL")
    let weekJSON = """
    {"line": "2 due today: Lab 5 (CS 101), Essay draft", "sample": false,
     "due_today": [{"id": "lab5", "title": "Lab 5", "course_name": "CS 101", "due_ts": \(at(17))}],
     "upcoming": [
       {"id": "lab5", "title": "Lab 5", "course_name": "Intro to Programming", "course_label": "CS 101", "due_ts": \(at(17))},
       {"id": "essay", "title": "Essay draft", "course_name": "Writing and Rhetoric: Voices and Global Perspectives", "course_label": "", "due_ts": \(at(23.98))},
       {"id": "quiz3", "title": "Quiz 3", "course_name": "CS 101", "course_label": "CS 101", "due_ts": \(at(34)), "kind": "quiz"},
       {"id": "pset7", "title": "  ", "course_name": "MATH 110", "course_label": null, "due_ts": \(at(24 * 3)), "all_day": true},
       {"id": "proj", "title": "Project proposal", "course_name": "CS 101", "course_label": "CS 101", "due_ts": \(at(24 * 5 + 12))},
       {"id": "read", "title": "Reading 8", "course_name": "WRIT 100", "course_label": "WRIT 100", "due_ts": \(at(24 * 6 + 9))}],
     "upcoming_more": 2}
    """
    let week = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(weekJSON.utf8))
    check(week?.upcoming.count == 6 && week?.upcomingMore == 2 && week?.sample == false,
          "upcoming and upcoming_more decode, the sample flag unchanged")
    check(week?.upcoming.map(\.course) == ["CS 101", "Writing and Rhetoric: Voices and Global Perspectives", "CS 101", "MATH 110", "CS 101", "WRIT 100"],
          "a row names the course's short label, else its name: \(week?.upcoming.map(\.course) ?? [])")
    check(week?.upcoming[3].shownTitle == "Untitled" && week?.upcoming[3].allDay == true && week?.upcoming[2].kind == "quiz",
          "a blank title shows as Untitled; all-day and kind decode")
    let briefRows = week?.briefUpcoming
    check(briefRows?.items.map(\.id) == ["lab5", "essay", "quiz3"] && briefRows?.more == 5
          && briefRows?.moreLine == "+5 more in Study",
          "the Brief lists three, and the more line counts the hidden rows and upcoming_more: \(briefRows?.more ?? -1)")
    let dayRows = week?.dayUpcoming(dayStart: dayStart, dayEnd: dayEnd, shownIDs: StudyDaySummary.deadlineIDs(["study:lab5", "pa2"]))
    check(dayRows?.items.map(\.id) == ["quiz3", "pset7", "proj", "read"] && dayRows?.more == 2,
          "Day's School this week skips what is due on the day shown, four then the rest: \(dayRows?.items.map(\.id) ?? [])")
    check(StudyDaySummary.deadlineIDs(["study:lab5", "pa2", "study:canvas:1:2"]) == ["lab5", "canvas:1:2"],
          "the Day's school deadline ids name their assignments")
    let byID = StudyDaySummary(upcoming: [StudyDaySummary.Due(id: "late", dueTS: at(30)), StudyDaySummary.Due(id: "next", dueTS: at(40))])
    check(byID.dayUpcoming(dayStart: dayStart, dayEnd: dayEnd, shownIDs: ["late"]).items.map(\.id) == ["next"],
          "an item the day already lists by id is not repeated")
    let onlyToday = StudyDaySummary(line: "1 due today: Lab 5 (CS 101)",
                                    upcoming: [StudyDaySummary.Due(id: "lab5", title: "Lab 5", dueTS: at(17))])
    check(onlyToday.briefUpcoming.isEmpty && onlyToday.briefHeadline == "1 due today: Lab 5 (CS 101)"
          && onlyToday.dayUpcoming(dayStart: dayStart, dayEnd: dayEnd).isEmpty,
          "one item due today and nothing else: the headline alone, and nothing extra on Day")
    check(onlyToday.briefPlate.items.map(\.id) == ["lab5"] && onlyToday.briefPlate.more == 0,
          "the proactive Brief's list has no headline, so the one item due today is a row there")
    let quietWeek = StudyDaySummary(upcoming: [StudyDaySummary.Due(id: "proj", title: "Project", dueTS: at(24 * 4))])
    check(quietWeek.briefHeadline == StudyDaySummary.quietHeadline && quietWeek.briefUpcoming.items.map(\.id) == ["proj"]
          && quietWeek.briefUpcoming.moreLine == nil,
          "nothing due today or tomorrow but work later this week: a quiet headline over its row")
    let threeExactly = StudyDaySummary(line: "x", upcoming: (0..<3).map { StudyDaySummary.Due(id: "a\($0)", dueTS: at(30 + Double($0))) })
    check(threeExactly.briefUpcoming.items.count == 3 && threeExactly.briefUpcoming.moreLine == nil, "three fit with no more line")
    let capped = StudyDaySummary(line: "x", upcoming: (0..<8).map { StudyDaySummary.Due(id: "a\($0)", dueTS: at(30 + Double($0))) },
                                 upcomingMore: -4)
    check(capped.upcomingMore == 0 && capped.dayUpcoming(dayStart: dayStart, dayEnd: dayEnd).more == 4,
          "Day shows four of eight; a negative upcoming_more counts as none")

    // Slides as pages (study.ready / study.weak): an older service sends neither,
    // and the Brief shows exactly what it showed before.
    check(summary?.ready.isEmpty == true && summary?.weak.isEmpty == true && summary?.briefCards == StudyBriefCards()
          && week?.briefCards.isEmpty == true && week?.briefCards.moreLine == nil,
          "no ready or weak keys: no study cards")
    let readyJSON = #"""
    {"line": "x", "ready": [
      {"id": "r1", "kind": "quiz", "assignment_id": "a1", "course": "CS 101", "title": "Quiz 3: Loops", "due_ts": 1700000000,
       "text": " CS 101 quiz Thursday. I made a 10-question quiz from weeks 5 and 6. ", "material_id": "m1",
       "slides": [{"source_id": "s5", "deck": "Week 5", "pages": [2, 3]}]},
      {"id": "r2", "kind": "assignment", "title": "PA 3", "text": "PA 3 is due Friday. Lectures 7 and 8 cover it.",
       "material_id": null,
       "slides": [{"source_id": "s7", "deck": "Lecture 7: Pointers", "pages": [4, 5, 5, "x", 0, 6]},
                  {"source_id": "s8", "deck": "Lecture 8", "pages": [2]}, {"source_id": "s9", "deck": "Lecture 9", "pages": []}]},
      {"id": "r3", "kind": "seminar", "text": "Something new.", "material_id": null, "slides": []},
      {"id": "r4", "kind": "quiz", "text": "   "},
      "not an object"],
     "weak": [{"id": "w1", "course": "CS 101", "topic": "Pointers", "count": 3,
               "text": "You missed 3 on pointers; here are those two slides again.",
               "slides": [{"source_id": "s7", "deck": "Lecture 7: Pointers", "pages": [6, 8]}]},
              {"id": "w2", "topic": "Limits", "count": "two", "text": "You missed some on limits.", "slides": "bad"}]}
    """#
    let withReady = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(readyJSON.utf8))
    check(withReady?.line == "x" && withReady?.ready.map(\.id) == ["r1", "r2", "r3", "r4"] && withReady?.weak.count == 2,
          "ready and weak decode; a bad row is skipped: \(withReady?.ready.map(\.id) ?? [])")
    check(withReady?.ready[1].slides.first?.pages == [4, 5, 6] && withReady?.ready[1].materialID == nil
          && withReady?.ready[2].kind == "seminar" && withReady?.weak[1].count == 0 && withReady?.weak[1].slides.isEmpty == true,
          "pages drop repeats and bad values; null material; an unknown kind and odd fields are tolerated")
    let cards = withReady?.briefCards ?? StudyBriefCards()
    // r4 has no line, so three ready cards: two shown; one weak shown of two.
    check(cards.cards.map(\.id) == ["ready:r1", "ready:r2", "weak:w1"] && cards.more == 2
          && cards.moreLine == "+2 more in Study", "two ready, one weak, then +N: \(cards.cards.map(\.id)) \(cards.more)")
    check(cards.cards[0].text == "CS 101 quiz Thursday. I made a 10-question quiz from weeks 5 and 6."
          && cards.cards[0].action == .open(materialID: "m1") && cards.cards[0].actionTitle == "Open quiz",
          "a ready quiz opens what Study made")
    check(cards.cards[1].action == .review(pages: [SchoolPage(sourceID: "s7", page: 4), SchoolPage(sourceID: "s7", page: 5),
                                                   SchoolPage(sourceID: "s7", page: 6), SchoolPage(sourceID: "s8", page: 2)],
                                           title: "PA 3")
          && cards.cards[1].actionTitle == "Review slides",
          "no material: review the slides, flattened in order")
    check(cards.cards[2].action == .review(pages: [SchoolPage(sourceID: "s7", page: 6), SchoolPage(sourceID: "s7", page: 8)],
                                           title: "Pointers"),
          "a weak topic reviews its slides, titled by topic")
    check(StudyBriefCard(ready: StudyDaySummary.Ready(id: "r", text: "Lecture tomorrow."))?.action == nil
          && StudyBriefCard(weak: StudyDaySummary.Weak(id: "w", text: "You missed 2."))?.actionTitle == nil,
          "nothing to open and no slides: the line, with no action")
    let pagesText: ([Int]) -> String = { $0.count == 1 ? "p. \($0[0])" : "pp. \($0.first!)–\($0.last!)" }
    check(SchoolSlidesText.line(withReady?.ready[1].slides ?? [], pagesText: pagesText)
          == "Lecture 7: Pointers · pp. 4–6, Lecture 8 · p. 2 +1",
          "two decks named, then +N: \(SchoolSlidesText.line(withReady?.ready[1].slides ?? [], pagesText: pagesText) ?? "nil")")
    check(SchoolSlidesText.line([], pagesText: pagesText) == nil
          && SchoolSlidesText.line([SchoolSlides(sourceID: "s", deck: "", pages: [])], pagesText: pagesText) == "Slides",
          "no slides, no line; an unnamed deck reads Slides")
    let many = [SchoolSlides(sourceID: "a", deck: "A", pages: Array(1...30)),
                SchoolSlides(sourceID: "b", deck: "B", pages: Array(1...30)), SchoolSlides(sourceID: "", deck: "C", pages: [1])]
    let flat = SchoolSlidesText.pages(many)
    check(flat.count == 40 && flat.first == SchoolPage(sourceID: "a", page: 1) && flat.last == SchoolPage(sourceID: "b", page: 10),
          "slide refs flatten in order and stop at 40")
    check(SchoolSlidesText.covers("  Lectures 7 and 8 cover it. ") == "Lectures 7 and 8 cover it."
          && SchoolSlidesText.covers("  ") == nil && SchoolSlidesText.covers(nil) == nil, "the covers line, or none")
    let review = try? JSONDecoder().decode(SchoolReview.self, from: Data(#"""
    {"minutes": 20, "pages": [{"source_id": "s7", "page": 4}, {"source_id": "s7", "page": 4}, {"source_id": "s7"}, {"page": 2}]}
    """#.utf8))
    check(review?.label == "Review (20 min)" && review?.pages == [SchoolPage(sourceID: "s7", page: 4)] && review?.offered == true,
          "a review names its minutes; bad and repeated pages drop")
    let oddReview = try? JSONDecoder().decode(SchoolReview.self, from: Data(#"{"minutes": "soon", "pages": []}"#.utf8))
    check(oddReview?.label == "Review (15 min)" && oddReview?.offered == false, "odd minutes read as 15; no pages, no review")
    studyBlockCalendarChecks(check)
}

/// "Add to calendar" on a study block: which rows offer it, the request, and the state machine.
func studyBlockCalendarChecks(_ check: (Bool, String) -> Void) {
    typealias C = StudyBlockCalendar
    let now = 1_000_000.0
    func block(id: String = "study-block:canvas:12", start: Double = 1_003_600, added: Bool?, source: String? = nil,
               kind: String = "study") -> C.Block {
        C.Block(id: id, kind: kind, start: start, added: added, eventSourceID: source)
    }
    // The path: the whole id is one segment.
    check(C.pathSegment("study-block:canvas:12/3") == "study-block%3Acanvas%3A12%2F3",
          "the id encodes as one segment: \(C.pathSegment("study-block:canvas:12/3"))")
    check(C.path(.add, blockID: "study-block:canvas:12/3") == "/study/blocks/study-block%3Acanvas%3A12%2F3/add"
          && C.path(.undo, blockID: "study-block:a b?#%") == "/study/blocks/study-block%3Aa%20b%3F%23%25/undo",
          "add and undo paths")
    check(C.body(.add, day: "2026-11-14", timezone: "America/Los_Angeles")
            == ["day": "2026-11-14", "timezone": "America/Los_Angeles"] && C.body(.undo, day: "x", timezone: "y").isEmpty,
          "add sends the shown day and zone; undo sends {}")
    // Which rows offer it.
    var ledger = C.Ledger()
    check(ledger.show(block(added: nil), now: now) == .none, "old service (no added key): no action, row as before")
    check(ledger.show(block(added: false), now: now) == .add(busy: false, error: nil), "added false, later today: offer the add")
    check(ledger.show(block(start: now - 60, added: false), now: now) == .none
          && ledger.show(block(start: now, added: false), now: now) == .none, "a block already started offers nothing")
    check(ledger.show(block(id: "block:x", added: false), now: now) == .none
          && ledger.show(block(added: false, kind: "task"), now: now) == .none, "only study-block: rows of kind study")
    check(ledger.show(block(added: true, source: "ev1"), now: now) == .added(undo: true, busy: false, error: nil)
          && ledger.show(block(added: true), now: now) == .added(undo: false, busy: false, error: nil),
          "added: Undo only with an event_source_id")
    // Replies.
    func data(_ json: String) -> Data { Data(json.utf8) }
    check(C.outcome(.add, status: 200, data: data(#"{"ok": true, "status": "succeeded", "block_id": "b", "source_id": "ev9"}"#))
            == .succeeded(sourceID: "ev9"), "ok true succeeds")
    check(C.outcome(.add, status: 200, data: data(#"{"ok": false, "status": "conflict", "error": "That time is taken now."}"#))
            == .failed("That time is taken now."), "ok false shows the service's error")
    check(C.outcome(.add, status: 404, data: data(#"{"detail": "Not Found"}"#)) == .routeMissing
          && C.outcome(.undo, status: 404, data: Data()) == .routeMissing, "a bare 404 is a missing route")
    check(C.outcome(.add, status: 404, data: data(#"{"ok": false, "status": "not_found", "error": "That block left your plan."}"#))
            == .failed("That block left your plan."), "a 404 from the route itself is a failure with its reason")
    check(C.outcome(.add, status: nil, data: nil) == .failed(C.fallbackError)
          && C.outcome(.add, status: 500, data: data("oops")) == .failed(C.fallbackError)
          && C.outcome(.add, status: 200, data: data(#"{"ok": false, "error": "  "}"#)) == .failed(C.fallbackError),
          "transport error, 5xx and an empty reason read the fixed fallback")
    let long = String(repeating: "x", count: 300)
    check(C.errorLine("one\ntwo") == "one two" && C.errorLine(long).count == 160 && C.errorLine(long).hasSuffix("…"),
          "the error is one line of at most 160 characters")
    // idle → adding → added → (snapshot) idle → undoing → undone → (snapshot) idle.
    let id = "study-block:canvas:12"
    check(ledger.begin(.add, id) && !ledger.begin(.add, id), "one request at a time per block")
    check(ledger.show(block(added: false), now: now) == .add(busy: true, error: nil), "adding: the action is busy")
    ledger.finish(.add, id, .succeeded(sourceID: "ev1"))
    check(ledger.show(block(added: false), now: now) == .added(undo: true, busy: false, error: nil),
          "added this session shows before the snapshot catches up")
    ledger.settle(id, snapshotAdded: false)
    check(ledger.phase(id) == .added(sourceID: "ev1"), "a snapshot that hasn't caught up keeps the add")
    ledger.settle(id, snapshotAdded: true)
    check(ledger.phase(id) == .idle, "a snapshot that agrees takes over")
    check(ledger.begin(.undo, id)
          && ledger.show(block(added: true, source: "ev1"), now: now) == .added(undo: true, busy: true, error: nil), "undoing: busy")
    ledger.finish(.undo, id, .failed("Calendar said no."))
    check(ledger.show(block(added: true, source: "ev1"), now: now) == .added(undo: true, busy: false, error: "Calendar said no."),
          "a failed undo keeps Undo and shows why")
    _ = ledger.begin(.undo, id)
    ledger.finish(.undo, id, .succeeded(sourceID: nil))
    check(ledger.show(block(added: true, source: "ev1"), now: now) == .add(busy: false, error: nil), "undone: the add is back")
    ledger.settle(id, snapshotAdded: false)
    check(ledger.phase(id) == .idle, "the snapshot agrees with the undo")
    // A failed add keeps the action and shows why, nothing else.
    _ = ledger.begin(.add, id)
    ledger.finish(.add, id, .failed(C.fallbackError))
    check(ledger.show(block(added: false), now: now) == .add(busy: false, error: C.fallbackError), "failed add: Add stays, one error line")
    // A 404 hides the action for the session and shows no error.
    _ = ledger.begin(.add, id)
    ledger.finish(.add, id, .routeMissing)
    check(ledger.routeMissing && ledger.show(block(added: false), now: now) == .none
          && ledger.show(block(id: "study-block:other", added: false), now: now) == .none
          && ledger.show(block(added: true, source: "ev1"), now: now) == .added(undo: false, busy: false, error: nil)
          && !ledger.begin(.add, "study-block:other"), "after a 404: no action anywhere, no error line")
    studyBlockUndoReplyChecks(check)
}

/// Undo's "changed" and "gone" replies, and a newer snapshot over this session's add.
func studyBlockUndoReplyChecks(_ check: (Bool, String) -> Void) {
    typealias C = StudyBlockCalendar
    let now = 1_000_000.0
    let id = "study-block:qnt210:4"
    let ours = C.Block(id: id, kind: "study", start: now + 3600, added: true, eventSourceID: "ev2")
    func data(_ json: String) -> Data { Data(json.utf8) }
    let sentence = "That event was changed in Calendar after Wisp added it, so it was left alone."
    let changed = C.outcome(.undo, status: 409, data: data(#"{"ok": false, "status": "changed", "error": "\#(sentence)"}"#))
    check(changed == .failed(sentence), "undo changed: the service's sentence: \(changed)")
    check(C.outcome(.undo, status: 200, data: data(#"{"ok": false, "status": "changed"}"#)) == .failed(C.changedFallback),
          "undo changed with no sentence: a fixed one about Calendar")
    var ledger = C.Ledger()
    _ = ledger.begin(.undo, id)
    ledger.finish(.undo, id, changed, at: now)
    check(ledger.show(ours, now: now) == .added(undo: true, busy: false, error: sentence),
          "changed: the row stays Added to calendar, Undo still offered, the sentence under it")
    ledger.settle([ours], generatedAt: now + 5)
    check(ledger.show(ours, now: now) == .added(undo: true, busy: false, error: sentence),
          "a newer snapshot that still has it added keeps the sentence")
    for status in [200, 404] {
        let gone = C.outcome(.undo, status: status, data: data(#"{"ok": true, "status": "gone", "block_id": "\#(id)"}"#))
        check(gone == .succeeded(sourceID: nil), "undo gone (\(status)): a successful undo: \(gone)")
    }
    _ = ledger.begin(.undo, id)
    ledger.finish(.undo, id, .succeeded(sourceID: nil), at: now + 10)
    check(ledger.show(C.Block(id: id, kind: "study", start: now + 3600, added: false, eventSourceID: nil), now: now)
            == .add(busy: false, error: nil), "gone: Add to calendar again")
    // Added this session, then the event deleted in Calendar: the next plan says
    // added false and wins over this session's "added".
    var added = C.Ledger()
    _ = added.begin(.add, id)
    added.finish(.add, id, .succeeded(sourceID: "ev2"), at: now)
    let deleted = C.Block(id: id, kind: "study", start: now + 3600, added: false, eventSourceID: nil)
    added.settle([deleted], generatedAt: now - 30)
    check(added.show(deleted, now: now) == .added(undo: true, busy: false, error: nil),
          "a plan made before the add finished keeps this session's add")
    added.settle([deleted], generatedAt: now + 60)
    check(added.phase(id) == .idle && added.show(deleted, now: now) == .add(busy: false, error: nil),
          "a newer plan with added false shows Add to calendar again")
    // A changed-undo whose event is then deleted in Calendar: the error goes, Add is back.
    var kept = C.Ledger()
    _ = kept.begin(.undo, id)
    kept.finish(.undo, id, changed, at: now)
    kept.settle([deleted], generatedAt: now + 60)
    check(kept.show(deleted, now: now) == .add(busy: false, error: nil), "deleted after a changed undo: Add again, no stale sentence")
    // A failed add that a newer plan says went through after all.
    var late = C.Ledger()
    _ = late.begin(.add, id)
    late.finish(.add, id, .failed(C.fallbackError), at: now)
    late.settle([ours], generatedAt: now + 60)
    check(late.show(ours, now: now) == .added(undo: true, busy: false, error: nil), "a newer plan with it added: Added, no error")
    // Settling never touches blocks this session did not act on, nor one in flight.
    var busy = C.Ledger()
    _ = busy.begin(.add, id)
    busy.settle([deleted, C.Block(id: "study-block:cs101:1", kind: "study", start: now + 60, added: true, eventSourceID: nil)],
                generatedAt: now + 60)
    check(busy.phase(id) == .adding && busy.phases["study-block:cs101:1"] == nil, "an add in flight stays in flight")
}

/// To-dos from the proactive engine on the Day tab: timed ones on the hour
/// lane, untimed ones in "Still to fit", each once and with its source.
func dayTodoChecks(_ check: (Bool, String) -> Void, dayStart: Double, now: Double,
                   clock: (Double) -> String, dayClock: (Double) -> String) {
    let hour = 3600.0
    let dayEnd = dayStart + 24 * hour
    func at(_ h: Double) -> Double { dayStart + h * hour }
    let blocks = [
        DayBlockInput(id: "lecture", title: "CMP 140 lecture", start: at(14), end: at(15.25), kind: "calendar"),
        DayBlockInput(id: "club", title: "Startup club", start: at(18), end: at(19), kind: "calendar"),
    ]
    let needs = [DayNeedInput(id: "pa2", title: "PA 2", source: "reminders", due: at(23.5))]
    let todos = [
        DayTodoInput(id: "jo", title: "Call Jo", source: "messages", who: "Jo", due: at(16.25), kind: "call"),
        DayTodoInput(id: "tj", title: "Go to Trader Joe's", source: "reminders", due: at(17.5), kind: "errand"),
        DayTodoInput(id: "lecture", title: "CMP 140 lecture", source: "calendar", due: at(14), kind: "event"),
        DayTodoInput(id: "dana", title: "Reply to Dana", source: "mail", who: "Dana Lee", due: nil, dueLabel: "This week"),
        DayTodoInput(id: "mom", title: "Call Mom", source: "messages", who: "Mom", due: at(24), dueLabel: "Tomorrow"),
        DayTodoInput(id: "late", title: "Return the form", source: "mail", due: at(10)),
        DayTodoInput(id: "done", title: "Pay rent", source: "reminders", due: at(16), done: true),
        DayTodoInput(id: "dup", title: "pa 2!", source: "reminders", due: at(20)),
        DayTodoInput(id: "dateonly", title: "Pick up package", source: "notes", due: dayStart, dueLabel: ""),
        DayTodoInput(id: "eod", title: "Submit timesheet", source: "mail", due: dayEnd - 30, dueLabel: "Today"),
        DayTodoInput(id: "old", title: "Renew library book", source: "reminders", due: dayStart - 30 * hour),
        DayTodoInput(id: "junk", title: "n/a", source: "notes"),
    ]
    let timeline = DayTimeline.build(blocks: blocks, allDay: [], needs: needs, caught: [], now: now,
                                     dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18),
                                     isToday: true, clock: clock, dayClock: dayClock, todos: todos)
    func item(_ id: String) -> DayItem? { timeline.items.first { $0.id == "todo:" + id } }

    let jo = item("jo")
    check(jo?.style == .todo && jo?.start == at(16.25) && jo?.end == at(16.75),
          "a call happens at its time, for half an hour: \(String(describing: jo))")
    check(jo?.source == "messages" && jo?.todoID == "jo" && jo?.detail == "At 16:15 · Texts · Jo",
          "the block keeps its source and says when and from whom: \(jo?.detail ?? "")")
    let tj = item("tj")
    check(tj?.style == .todo && tj?.end == at(17.5) && tj?.start == at(17) && tj?.detail == "Due 17:30 · Reminders",
          "an errand is due by its time, so its block ends there: \(String(describing: tj))")
    check(item("lecture") == nil && timeline.items.filter { $0.title == "CMP 140 lecture" }.count == 1,
          "a calendar to-do is the calendar block, drawn once")
    check(item("dana") == nil && item("mom") == nil, "undated and tomorrow's to-dos stay off today's clock")
    let late = item("late")
    check(late?.style == .overdue && late?.start == now && late?.detail.hasPrefix("Overdue since 10:00") == true,
          "a to-do due earlier today and not done sits at now as overdue: \(late?.detail ?? "")")
    check(item("done") == nil, "a done to-do leaves the clock")
    check(item("dup") == nil, "a to-do a reminder already draws is not drawn twice")
    check(item("dateonly") == nil && item("eod") == nil, "midnight and end-of-day times are date-only, not times")
    check(item("junk") == nil, "placeholder titles never draw")
    check(DayTimeline.gutter(tj!, clock: clock) == ("17:30", "Due")
          && DayTimeline.agendaDetail(tj!, now: now, isToday: true, clock: clock) == "Reminders",
          "an errand is listed at its due time, with where it came from")
    check(DayTimeline.gutter(jo!, clock: clock) == ("16:15", "")
          && DayTimeline.agendaDetail(jo!, now: now, isToday: true, clock: clock) == "Texts · Jo",
          "a call is listed at its time, with who it's with")
    check(timeline.to >= at(19), "the lane still reaches the last block")

    // Not today: a to-do still draws at its time, without an overdue.
    let other = DayTimeline.build(blocks: [], allDay: [], needs: [], caught: [], now: now, dayStart: dayStart,
                                  dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: false,
                                  clock: clock, dayClock: dayClock, todos: [todos[5]])
    check(other.items.first?.style == .todo && other.items.first?.start == at(9.5),
          "on another day, a to-do due at 10 is drawn by 10, not as overdue")
    // No to-dos: the build is what it was.
    let none = DayTimeline.build(blocks: blocks, allDay: [], needs: needs, caught: [], now: now, dayStart: dayStart,
                                 dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: true,
                                 clock: clock, dayClock: dayClock)
    check(!none.items.contains { $0.style == .todo }, "without to-dos nothing new is drawn")

    // Still to fit: the planner's leftovers, then to-dos with no time today.
    let fit = DayTimeline.stillToFit(
        todos: todos, unscheduled: [(id: "t1", title: "Essay outline", reason: "Needs 45 min; the largest gap is 30 min"),
                                    (id: "t2", title: "nothing", reason: "")],
        blocks: blocks, needs: needs, now: now, dayStart: dayStart, dayEnd: dayEnd, isToday: true, dayClock: dayClock)
    check(fit.map(\.id) == ["task:t1", "todo:dana", "todo:dateonly", "todo:eod", "todo:old"],
          "still to fit, in order: \(fit.map(\.id))")
    check(fit[0].symbol == "clock" && fit[0].todoID == nil && fit[0].detail.hasPrefix("No gap long enough"),
          "a planner task says why it didn't fit: \(fit[0].detail)")
    check(fit[1].symbol == "envelope" && fit[1].detail == "Dana Lee · This week" && fit[1].todoID == "dana",
          "an undated to-do shows its source, who and when: \(fit[1].detail)")
    check(fit[2].symbol == "note.text" && fit[2].detail == "Today", "a date-only to-do today reads Today: \(fit[2].detail)")
    check(fit[4].symbol == "checklist" && fit[4].detail.hasPrefix("Overdue since earlier"),
          "an earlier day's to-do is overdue: \(fit[4].detail)")
    check(!fit.contains { ["todo:mom", "todo:jo", "todo:lecture", "todo:done", "todo:dup", "todo:junk"].contains($0.id) },
          "tomorrow's, timed, calendar, done, duplicate and placeholder to-dos aren't still to fit")
    let elsewhere = DayTimeline.stillToFit(todos: todos, unscheduled: [], blocks: [], now: now, dayStart: dayStart,
                                           dayEnd: dayEnd, isToday: false, dayClock: dayClock)
    check(!elsewhere.contains { $0.id == "todo:old" }, "another day shows no overdue to-dos")

    // An overdue reminder the agenda draws, with its Plan time, isn't also still to fit.
    let backlog = (0..<5).map { DayNeedInput(id: "old\($0)", title: "Backlog \($0)", source: "reminders", due: dayStart - Double($0 + 1) * hour) }
    let asTodos = backlog.map { DayTodoInput(id: "t" + $0.id, title: $0.title, source: "reminders", due: $0.due) }
    let drawnOnce = DayTimeline.stillToFit(todos: Array(asTodos.prefix(3)), unscheduled: [], blocks: [], needs: Array(backlog.prefix(3)),
                                           now: now, dayStart: dayStart, dayEnd: dayEnd, isToday: true, dayClock: dayClock)
    check(drawnOnce.isEmpty, "overdue reminders listed by name aren't repeated: \(drawnOnce.map(\.id))")
    let folded = DayTimeline.stillToFit(todos: asTodos, unscheduled: [], blocks: [], needs: backlog, now: now, dayStart: dayStart,
                                        dayEnd: dayEnd, isToday: true, dayClock: dayClock)
    check(folded.map(\.id) == ["todo:told2", "todo:told3", "todo:told4"],
          "those folded into \"N more overdue\" keep their row: \(folded.map(\.id))")
    let anotherDay = DayTimeline.stillToFit(todos: [], unscheduled: [(id: "t", title: "Backlog 0", reason: "")], blocks: [], needs: backlog,
                                            now: now, dayStart: dayStart, dayEnd: dayEnd, isToday: false, dayClock: dayClock)
    check(anotherDay.map(\.id) == ["task:t"], "another day's agenda draws no overdue, so nothing is taken out")

    // The 2026-11-04 Day: an event and a kept review block, no backlog or repeat left, so
    // the summary counts two and ends at the block.
    let settled = DayTimeline.build(blocks: [DayBlockInput(id: "a", title: "Attendance", start: now - 15 * 60, end: nil, kind: "calendar"),
                                             DayBlockInput(id: "r", title: "Review 8 emails · Ana, Ben +1", start: now + 60, end: now + 60 + 45 * 60,
                                                           kind: "task", taskID: "r")],
                                    allDay: [], needs: [], caught: [], now: now, dayStart: dayStart, dayEnd: dayEnd,
                                    workStart: at(9), workEnd: at(18), isToday: true, clock: clock, dayClock: dayClock)
    let settledBlurb = DayTimeline.blurb(settled, needs: [], isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
    check(settledBlurb.contains("on the clock") && !settledBlurb.contains("overdue") && !settledBlurb.contains("suggested")
          && settledBlurb.contains("Clear after " + clock(now + 60 + 45 * 60)), "summary after both fixes: \(settledBlurb)")

    // The summary counts what the agenda draws: four things at a time (an event,
    // a study block, a plan caught in texts, a to-do at its time) and two due by
    // a time (school work due tonight, a to-do due by then). It once counted
    // only the event and the block and said "nothing else planned".
    let schoolDue = DayNeedInput(id: "cs101-hw", title: "CS 101 problem set", source: "study", due: now + 5 * 3600)
    let counted = DayTimeline.build(
        blocks: [DayBlockInput(id: "e", title: "Office hours", start: now + 1800, end: now + 3600, kind: "calendar"),
                 DayBlockInput(id: "study-block:cs101:1", title: "Study: CS 101", start: now + 2 * 3600, end: now + 3 * 3600, kind: "study")],
        allDay: [], needs: [schoolDue],
        caught: [DayCaughtInput(id: "c", title: "Dinner with Sam Rivera", when: now + 4 * 3600, state: "shadow")],
        now: now, dayStart: dayStart, dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: true,
        clock: clock, dayClock: dayClock,
        todos: [DayTodoInput(id: "call", title: "Call the lab", source: "mail", due: now + 3 * 3600 + 1800, kind: "call"),
                DayTodoInput(id: "form", title: "Send the QNT 210 form", source: "mail", due: now + 4 * 3600 + 1800)])
    check(counted.items.filter(DayTimeline.isOnTheClock).count == 4 && counted.items.filter(DayTimeline.isDueBy).count == 2,
          "the agenda draws four at a time and two due: \(counted.items.map { ($0.id, $0.style) })")
    let countedBlurb = DayTimeline.blurb(counted, needs: [], isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
    check(countedBlurb.hasPrefix("Four things on the clock and two due today."), "the summary counts the agenda: \(countedBlurb)")
    check(DayTimeline.clockAndDue(0, 1, isToday: false) == "one thing due that day, nothing on the clock"
          && DayTimeline.clockAndDue(1, 3, isToday: true) == "one thing on the clock and three due today",
          "the count's words")

    // Sources and kinds.
    check(DayTimeline.sourceSymbol("mail") == "envelope" && DayTimeline.sourceSymbol("messages") == "message"
          && DayTimeline.sourceSymbol("calendar") == "calendar" && DayTimeline.sourceSymbol("reminders") == "checklist"
          && DayTimeline.sourceSymbol("notes") == "note.text" && DayTimeline.sourceSymbol("other") == "circle.dashed",
          "one SF Symbol per source")
    check(DayTimeline.happensAtTime("Meeting") && !DayTimeline.happensAtTime("errand"), "which kinds start at their time")
    check(DayTimeline.todoKey("Go to Trader Joe's!") == DayTimeline.todoKey("go to trader joes"), "titles compare loosely")
    check(DayTimeline.isTimed(at(9), dayStart: dayStart, dayEnd: dayEnd) && !DayTimeline.isTimed(dayStart, dayStart: dayStart, dayEnd: dayEnd)
          && !DayTimeline.isTimed(dayEnd - 59, dayStart: dayStart, dayEnd: dayEnd), "timed versus date-only")
    firstDayChecks(check, dayStart: dayStart, dayEnd: dayEnd, now: now, clock: clock, dayClock: dayClock)
}

/// A new user's first Brief and Day: unreadable sources say so with the setting
/// that fixes it, letterless calendar titles stay, a day-one backlog never
/// leads, and the planner's after-hours and all-day reasons read plainly.
func firstDayChecks(_ check: (Bool, String) -> Void, dayStart: Double, dayEnd: Double, now: Double,
                    clock: (Double) -> String, dayClock: (Double) -> String) {
    let hour = 3600.0
    func at(_ h: Double) -> Double { dayStart + h * hour }
    func build(_ blocks: [DayBlockInput], needs: [DayNeedInput] = [], allDay: [String] = []) -> DayTimeline {
        DayTimeline.build(blocks: blocks, allDay: allDay, needs: needs, caught: [], now: now, dayStart: dayStart,
                          dayEnd: dayEnd, workStart: at(9), workEnd: at(18), isToday: true, clock: clock, dayClock: dayClock)
    }

    // Calendar events are real whatever they're called; Wisp's own blocks can be placeholders.
    let titled = build([DayBlockInput(id: "one", title: "1:1", start: at(14), end: at(14.5), kind: "calendar"),
                        DayBlockInput(id: "emoji", title: "☕️", start: at(15), end: at(15.5), kind: "calendar"),
                        DayBlockInput(id: "blank", title: "  ", start: at(16), end: at(16.5), kind: "manual"),
                        DayBlockInput(id: "task:j", title: "nothing", start: at(17), end: at(17.5), kind: "task", taskID: "j"),
                        DayBlockInput(id: "word", title: "n/a", start: at(17.5), end: at(18), kind: "calendar")],
                       allDay: ["🎂", ""])
    check(titled.items.map(\.title) == ["1:1", "☕️", "(untitled)"],
          "letterless calendar titles stay, blank reads (untitled), placeholder words go: \(titled.items.map(\.title))")
    check(titled.allDay == ["🎂", "(untitled)"], "all-day calendar titles are never dropped")
    let load = DayLoad.assess(events: [.init(title: "1:1", start: at(14), end: at(14.5))], allDay: [], from: now,
                              dayStart: dayStart, isToday: true, zone: TimeZone(identifier: "UTC")!)
    check(load.meetings == 1 && load.summary.contains("1:1"), "DayLoad counts a \"1:1\": \(load.summary)")

    // A day-one overdue backlog: three on the lane at most, the rest as one block.
    let backlog = (0..<6).map { DayNeedInput(id: "o\($0)", title: "Old thing \($0)", source: "reminders", due: dayStart - Double($0 + 1) * 86400) }
    let crowded = build([], needs: backlog)
    let overdue = crowded.items.filter { $0.style == .overdue }
    check(overdue.count == DayTimeline.maxOverdueShown, "at most three overdue blocks: \(overdue.map(\.title))")
    check(overdue.contains { $0.id == "need:overdue-more" && $0.title == "4 more overdue" }, "the rest as one block: \(overdue.map(\.title))")
    check(DayTimeline.blurb(crowded, needs: backlog, isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
            .hasPrefix("Six overdue reminders to look at"),
          "a backlog is counted, never \"the main thing today\": \(DayTimeline.blurb(crowded, needs: backlog, isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart))")
    let mixed = backlog + [DayNeedInput(id: "t", title: "Essay", source: "reminders", due: at(20))]
    check(DayTimeline.blurb(build([], needs: mixed), needs: mixed, isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
            .hasPrefix("Essay is the main thing today."), "what's due today leads, not the oldest overdue")

    // An unread calendar: never "Nothing is planned".
    let empty = build([])
    let cantRead = "Wisp can't read your calendar. Allow it in System Settings › Privacy & Security › Calendars."
    check(DayTimeline.blurb(empty, needs: [], isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart, calendar: .unread)
            == cantRead, "an unread calendar is named")
    check(DayTimeline.blurb(empty, needs: [], isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
            == "Nothing is planned for the rest of today.", "a read, empty calendar is an honest all-clear")
    noCalendarChecks(check, build: { build($0, needs: $1, allDay: $2) }, at: at, now: now, dayStart: dayStart, dayEnd: dayEnd, clock: clock, cantRead: cantRead)

    // The lede gives an all-clear only for what was read.
    check(DayTimeline.lede(weekday: "Monday", isToday: true, load: nil, dueToday: [], overdue: [], next: nil,
                           unread: ["calendar", "reminders"]) == "It's Monday. Wisp can't see your calendar and reminders yet.",
          "the lede names unread sources")
    check(DayTimeline.lede(weekday: "Monday", isToday: true, load: .free, dueToday: [], overdue: [], next: nil)
            == "A free Monday. Nothing needs you right now.", "the all-clear stays when everything was read")

    // Which source, and the one setting that fixes it.
    let calendarOff = SourceAccess(source: "calendar", ready: false, state: "unavailable", blocked: "calendar_access")
    check(!calendarOff.readable && calendarOff.headline == "Wisp can't see your calendar yet", "a blocked calendar's line")
    check(calendarOff.fix == BriefDetail.calendarsAccess && calendarOff.fix?.url.hasSuffix("Privacy_Calendars") == true,
          "Calendar opens Privacy > Calendars")
    check(SourceAccess(source: "reminders", ready: false, blocked: "reminders_access").fix?.url.hasSuffix("Privacy_Reminders") == true,
          "Reminders opens Privacy > Reminders")
    check(SourceAccess(source: "messages", ready: false, blocked: "full_disk_access").fix == BriefDetail.fullDiskAccess,
          "Messages opens Full Disk Access")
    check(SourceAccess(source: "email", ready: false, blocked: "mail_automation").fix?.url.hasSuffix("Privacy_Automation") == true,
          "Mail automation opens Automation")
    check(SourceAccess(source: "email", ready: false, blocked: "mail_not_configured").headline == "No Mail accounts on this Mac"
          && SourceAccess(source: "email", ready: false, blocked: "mail_not_configured").fix == BriefDetail.internetAccounts,
          "no Mail account says so, with Internet Accounts")
    let reading = SourceAccess(source: "calendar", ready: false, state: "syncing")
    check(reading.state == .syncing && reading.headline == "Reading your calendar…" && reading.fix == nil,
          "a read in progress has no setting to open")
    check(SourceAccess(source: "calendar", ready: false).state == .unavailable, "not ready and silent is never an all-clear")
    check(SourceAccess(source: "calendar", ready: true, state: "unavailable", blocked: "calendar_access").readable,
          "ready wins")
    check(SourceAccess(source: "calendar", ready: false, blocked: "calendar_access", reason: "Calendar access is off for Wisp.").detail
            == "Calendar access is off for Wisp.", "the service's sentence is the detail")
    check(SourceAccess.cantSee(["email", "messages", "calendar"]) == "Wisp can't see your mail, texts and calendar yet.",
          "unread sources in one sentence")
    let cal = DayTimeline.briefLines(calendar: nil, calendarAccess: calendarOff, mail: nil, texts: nil,
                                     mailAccess: SourceAccess(source: "email", ready: false, blocked: "mail_closed"), soon: .unknown)
    check(cal[0].text == "Wisp can't see your calendar yet" && cal[0].muted, "the CAL row says why: \(cal[0].text)")
    check(cal[1].text == "Wisp reads Mail while Mail is open", "the MAIL row says why: \(cal[1].text)")

    // Planner reasons in plain words; no "Pin one" on the notch.
    check(DayTimeline.plainReason("It's after your working hours") == "it's after your working hours", "after hours")
    check(DayTimeline.plainWarning("All-day calendar events may reserve this day. Flexible tasks stay unscheduled; choose to plan the day anyway or pin a task.")?
            .contains("Pin") == false, "the all-day warning never asks the notch user to pin")
    check(DayTimeline.afterHoursLine(start: 540, end: 1080) == "It's after your working hours (9 AM–6 PM), so nothing new is placed today.",
          "after-hours line names the hours")
}

/// The Day's week row (C3): the week's dates in the person's week order, the
/// shown day's load and count, and nothing invented for any other day.
func dayWeekChecks(_ check: (Bool, String) -> Void, dayStart: Double, now: Double) {
    let hour = 3600.0
    func at(_ h: Double) -> Double { dayStart + h * hour }
    var utc = Calendar(identifier: .gregorian)
    utc.timeZone = TimeZone(identifier: "UTC")!
    // 2026-11-14 is a Saturday.
    let saturday = utc.date(from: DateComponents(year: 2026, month: 11, day: 14, hour: 13))!
    var monday = utc
    monday.firstWeekday = 2
    let mondayWeek = DayWeek.cards(shown: saturday, today: saturday, calendar: monday, shownLoad: nil)
    check(mondayWeek.map(\.weekday) == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
          && mondayWeek.map(\.number) == [9, 10, 11, 12, 13, 14, 15],
          "a Monday-first week runs Mon 9 to Sun 15: \(mondayWeek.map(\.weekday)) \(mondayWeek.map(\.number))")
    check(mondayWeek.map(\.day).first == "2026-11-09" && mondayWeek.map(\.day).last == "2026-11-15",
          "each card keys its day as the snapshot route takes it")
    check(mondayWeek.filter(\.isToday).map(\.weekday) == ["SAT"] && mondayWeek.filter(\.isShown).map(\.weekday) == ["SAT"],
          "today is the one shown")
    var sunday = utc
    sunday.firstWeekday = 1
    let sundayWeek = DayWeek.cards(shown: saturday, today: saturday, calendar: sunday, shownLoad: nil)
    check(sundayWeek.map(\.weekday) == ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
          && sundayWeek.first?.number == 8 && sundayWeek.last?.number == 14,
          "a Sunday-first week ends on Saturday 14: \(sundayWeek.map(\.number))")
    let sundayShown = utc.date(from: DateComponents(year: 2026, month: 11, day: 15, hour: 9))!
    check(DayWeek.dates(containing: sundayShown, calendar: monday).count == 7
          && DayWeek.cards(shown: sundayShown, today: saturday, calendar: monday, shownLoad: nil).last?.isShown == true,
          "a Sunday in a Monday-first week is its last day")
    let monthEnd = utc.date(from: DateComponents(year: 2026, month: 12, day: 31, hour: 12))!
    check(DayWeek.cards(shown: monthEnd, today: saturday, calendar: monday, shownLoad: nil).map(\.number) == [28, 29, 30, 31, 1, 2, 3]
          && DayWeek.cards(shown: monthEnd, today: saturday, calendar: monday, shownLoad: nil).allSatisfy { !$0.isToday },
          "a week across the year's end; today is in another week")

    // Load: commitments only, clipped to the day, overlaps once.
    let blocks = [
        DayBlockInput(id: "a", title: "Lecture", start: at(9), end: at(10.5), kind: "calendar"),
        DayBlockInput(id: "b", title: "Lab", start: at(10), end: at(11), kind: "calendar"),
        DayBlockInput(id: "s", title: "Study: Lab 5", start: at(16), end: at(16.75), kind: "study"),
        DayBlockInput(id: "x", title: "Work on PA 2", start: at(20), end: at(21.5), kind: "suggestion"),
        DayBlockInput(id: "u", title: "Usually busy", start: at(12), end: at(13), kind: "behavior"),
        DayBlockInput(id: "n", title: "n/a", start: at(14), end: at(15), kind: "calendar"),
        DayBlockInput(id: "late", title: "Late show", start: at(23), end: at(25), kind: "calendar"),
        DayBlockInput(id: "open", title: "Open end", start: at(18), end: nil, kind: "calendar"),
    ]
    let spans = DayWeek.spans(blocks, dayStart: dayStart, dayEnd: at(24))
    check(spans.count == 5 && !spans.contains { $0.start == at(20) || $0.start == at(12) || $0.start == at(14) },
          "suggestions, usual windows and placeholder events add no load: \(spans.count)")
    check(spans.contains { $0.start == at(23) && $0.end == at(24) } && spans.contains { $0.start == at(18) && $0.end == at(18.5) },
          "a late event is clipped at midnight; one without an end holds half an hour")
    let span = DayWeek.span(workStart: at(9), workEnd: at(17), spans: spans, dayStart: dayStart, dayEnd: at(24))
    check(span.start == at(9) && span.end == at(24), "the bar spans working hours, widened to what is held: \(span)")
    let narrowSpan = DayWeek.span(workStart: at(9.5), workEnd: at(17.25), spans: [], dayStart: dayStart, dayEnd: at(24))
    check(narrowSpan.start == at(9) && narrowSpan.end == at(18), "to whole hours")
    let load = DayWeek.load(onClock: 3, due: 2, spans: spans, span: span, now: at(12))
    check(load.minutes == 120 + 45 + 30 + 60, "overlapping events count once: \(load.minutes) min")
    check(load.segments.count == 4 && load.segments[0].from == 0 && abs(load.segments[0].to - 2.0 / 15) < 1e-9
          && load.segments[0].past && !load.segments[1].past,
          "segments merge and sit on the span; what ended before now is past: \(load.segments)")
    check(load.now.map { abs($0 - 3.0 / 15) < 1e-9 } == true, "now on the bar")
    check(DayWeek.load(onClock: 1, due: 0, spans: spans, span: span, now: at(7)).now == nil
          && DayWeek.load(onClock: 1, due: 0, spans: spans, span: span, now: nil).segments.allSatisfy { !$0.past },
          "no now tick outside the span, nor on another day; nothing past there")

    // Counts and words.
    func bar(_ n: Int, _ d: Int) -> DayLoadBar { DayLoadBar(onClock: n, due: d, minutes: 0, segments: [], now: nil) }
    check(DayWeek.countText(bar(3, 2)) == "3 · 2 due" && DayWeek.countText(bar(3, 0)) == "3"
          && DayWeek.countText(bar(0, 2)) == "2 due" && DayWeek.countText(bar(0, 0)) == "free",
          "card counts: 3 · 2 due, 3, 2 due, free")
    check(DayWeek.shortCountText(bar(3, 2)) == "3" && DayWeek.shortCountText(bar(0, 2)) == "2 due",
          "with less room, the things on the clock alone")
    var cards = DayWeek.cards(shown: saturday, today: saturday, calendar: monday, shownLoad: bar(3, 2))
    check(cards.filter { $0.load != nil }.map(\.weekday) == ["SAT"], "only the shown day carries a load")
    check(DayWeek.spoken(cards[5]) == "Saturday 14, today, 3 things and 2 due", DayWeek.spoken(cards[5]))
    check(DayWeek.spoken(cards[2]) == "Wednesday 11", "a day Wisp hasn't read is its date alone: \(DayWeek.spoken(cards[2]))")
    cards = DayWeek.cards(shown: utc.date(byAdding: .day, value: -3, to: saturday)!, today: saturday, calendar: monday,
                          shownLoad: bar(4, 0))
    check(DayWeek.spoken(cards[2]) == "Wednesday 11, shown, 4 things" && DayWeek.spoken(cards[5]) == "Saturday 14, today",
          "another day shown: its load; today's date alone: \(DayWeek.spoken(cards[2])) / \(DayWeek.spoken(cards[5]))")
    check(DayWeek.spoken(DayWeek.cards(shown: saturday, today: saturday, calendar: monday, shownLoad: bar(0, 0))[5])
          == "Saturday 14, today, free", "free")
    // Compact until other days carry a load; the shown day twice as wide there.
    let compactWeek = DayWeek.cards(shown: saturday, today: saturday, calendar: monday, shownLoad: bar(3, 2))
    check(!DayWeek.fullRow(compactWeek) && !DayWeek.fullRow(DayWeek.cards(shown: saturday, today: saturday, calendar: monday,
                                                                          shownLoad: nil)),
          "one day's load (or none): the compact row")
    var weekLoad = compactWeek
    weekLoad[2].load = bar(4, 0)
    check(DayWeek.fullRow(weekLoad), "a load for another day too: the full row")
    check(DayWeek.weights(compactWeek, full: false) == [1, 1, 1, 1, 1, 2, 1]
          && DayWeek.weights(DayWeek.cards(shown: saturday, today: saturday, calendar: monday, shownLoad: nil), full: false)
              == [1, 1, 1, 1, 1, 2, 1]
          && DayWeek.weights(weekLoad, full: true) == Array(repeating: 1, count: 7),
          "compact: the shown day twice as wide, before and after its load; full: equal")
    _ = now
}

/// The Next card and Later today (C3): which thing is next (ties, all-day and
/// due rows), its words, the rows after it, the Show more line, the bars and
/// the card's buttons.
func dayNextChecks(_ check: (Bool, String) -> Void, dayStart: Double, clock: (Double) -> String) {
    let hour = 3600.0
    func at(_ h: Double) -> Double { dayStart + h * hour }
    let now = at(13 + 20.0 / 60)
    let blocks = [
        DayBlockInput(id: "lunch", title: "Lunch with Sam Rivera", start: at(12), end: at(13), kind: "calendar"),
        DayBlockInput(id: "review", title: "STAT 110 review session", start: at(14), end: at(15.5), kind: "calendar"),
        DayBlockInput(id: "plan", title: "Plan block at two", start: at(14), end: at(14.5), kind: "task", taskID: "t1"),
        DayBlockInput(id: "pset", title: "Study: Problem set 6", start: at(15.75), end: at(16.5), kind: "study"),
        DayBlockInput(id: "notes", title: "Review lecture 9 notes", start: at(13.5), end: at(14.25), kind: "suggestion",
                      evidenceID: "n9"),
        DayBlockInput(id: "practice", title: "Ultimate practice", start: at(18.5), end: at(20), kind: "calendar"),
    ]
    let needs = [
        DayNeedInput(id: "lab", title: "CS 101 Lab 4", source: "reminders", due: at(23.98)),
        DayNeedInput(id: "ps", title: "Problem set 6 · MATH 110", source: "study", due: at(23.98)),
        DayNeedInput(id: "n9", title: "Review lecture 9 notes", source: "reminders", due: nil),
    ]
    let timeline = DayTimeline.build(blocks: blocks, allDay: ["Campus Fall Fest"], needs: needs, caught: [], now: now,
                                     dayStart: dayStart, dayEnd: at(24), workStart: at(9), workEnd: at(22),
                                     isToday: true, clock: clock, dayClock: clock)
    let next = DayTimeline.nextUp(timeline.items, now: now)
    check(next?.id == "block:review", "a Calendar event beats a plan block at the same time; a suggestion is never next: \(next?.id ?? "nil")")
    check(next.map { DayTimeline.nextLabel($0, now: now) } == "NEXT · IN 40 MIN", next.map { DayTimeline.nextLabel($0, now: now) } ?? "")
    check(next.map { DayTimeline.spokenNextLabel($0, now: now) } == "Next, in 40 min", "said as words")
    check(next.map { DayTimeline.nextTimeLine($0, clock: clock) } == "14:00 – 15:30 · 1 h 30 min",
          next.map { DayTimeline.nextTimeLine($0, clock: clock) } ?? "")
    // The suggestion at 1:30 holds the time, so free runs only until it.
    check(DayTimeline.nowFreeLine(timeline.items, now: now, clock: clock) == "Now 13:20 · free until 13:30",
          DayTimeline.nowFreeLine(timeline.items, now: now, clock: clock) ?? "nil")
    check(DayTimeline.nowFreeLine(timeline.items, now: at(13.6), clock: clock) == nil, "not free while something holds now")
    check(DayTimeline.nowFreeLine(timeline.items, now: now, clock: clock, calendarRead: false) == nil
          && DayTimeline.nothingNextLine(.unread) == "Wisp can't read your calendar."
          && DayTimeline.nothingNextLine(.read) == "Nothing else on the clock today.",
          "no free time or all-clear claimed while Calendar can't be read")
    let onNow = DayTimeline.nextUp(timeline.items, now: at(14.5))
    check(onNow?.id == "block:review" && onNow.map { DayTimeline.nextLabel($0, now: at(14.5)) } == "NOW · 1 H LEFT",
          "one already on stays next, with the time left: \(onNow.map { DayTimeline.nextLabel($0, now: at(14.5)) } ?? "nil")")
    check(DayTimeline.nextUp(timeline.items, now: at(13.995)).map { DayTimeline.nextLabel($0, now: at(13.995)) } == "NEXT · IN 1 MIN"
          && DayTimeline.nextUp(timeline.items, now: at(14)).map { DayTimeline.nextLabel($0, now: at(14)) } == "NOW · 1 H 30 MIN LEFT",
          "a minute away, then on")
    let far = DayTimeline.build(blocks: [DayBlockInput(id: "e", title: "Evening", start: at(18), end: nil, kind: "calendar")],
                                allDay: [], needs: [], caught: [], now: at(16.9), dayStart: dayStart, dayEnd: at(24),
                                workStart: at(9), workEnd: at(17), isToday: true, clock: clock, dayClock: clock)
    let farNext = DayTimeline.nextUp(far.items, now: at(16.9))
    check(farNext.map { DayTimeline.nextLabel($0, now: at(16.9)) } == "NEXT · IN 1 H 6 MIN"
          && farNext.map { DayTimeline.nextTimeLine($0, clock: clock) } == "From 18:00",
          "hours and minutes; an event with no end reads From")
    // Only due rows and an all-day item left: nothing is next.
    let late = DayTimeline.build(blocks: blocks, allDay: ["Campus Fall Fest"], needs: needs, caught: [], now: at(21),
                                 dayStart: dayStart, dayEnd: at(24), workStart: at(9), workEnd: at(22),
                                 isToday: true, clock: clock, dayClock: clock)
    check(DayTimeline.nextUp(late.items, now: at(21)) == nil, "due rows and all-day items are never next")
    let studyNext = DayTimeline.nextUp(timeline.items, now: at(15.6))
    check(studyNext.map { DayTimeline.nextTimeLine($0, clock: clock) } == "15:45 – 16:30 · 45 min · Study block",
          "a study block says what it is: \(studyNext.map { DayTimeline.nextTimeLine($0, clock: clock) } ?? "nil")")

    // Later today: the rest after the card's item, timed rows and due rows apart.
    let agenda = timeline.agenda(now: now, isToday: true)
    let later = DayTimeline.laterRows(agenda, excluding: next)
    check(later.timed.map(\.id) == ["block:notes", "block:plan", "block:pset", "block:practice"],
          "timed rows after the card, in agenda order: \(later.timed.map(\.id))")
    check(later.due.map(\.id) == ["need:lab", "need:ps"], "due rows apart: \(later.due.map(\.id))")
    let narrow = DayTimeline.agenda(agenda, without: next)
    check(!narrow.ahead.contains { $0.id == "block:review" } && narrow.thingsAhead == agenda.thingsAhead - 1
          && !narrow.ahead.contains { if case .free(_, let end) = $0 { return end == at(14) } else { return false } },
          "the one-column agenda drops the card's row and the free stretch up to it")
    check(DayTimeline.agenda(agenda, without: nil) == agenda, "no card, the agenda as it was")
    check(DayTimeline.moreLine(later.due, clock: clock) == "Show 2 more · due 23:59: CS 101 Lab 4 and Problem set 6 · MATH 110",
          DayTimeline.moreLine(later.due, clock: clock))
    check(DayTimeline.moreLine(Array(later.timed.suffix(2)) + later.due, clock: clock)
          == "Show 4 more: Study: Problem set 6, Ultimate practice and 2 more",
          DayTimeline.moreLine(Array(later.timed.suffix(2)) + later.due, clock: clock))
    check(DayTimeline.moreLine([], clock: clock).isEmpty, "nothing hidden, no line")
    check(later.timed.map { DayTimeline.laterTime($0, clock: clock) } == ["13:30", "14:00", "15:45", "18:30"]
          && later.due.map { DayTimeline.laterTime($0, clock: clock) } == ["23:59", "23:59"], "times in a column")
    check(later.timed.map { DayTimeline.laterDetail($0, now: now, isToday: true, clock: clock) }
          == ["Suggested · about 45 min", "Your plan · 30 min", "45 min", "1 h 30 min"],
          "details: \(later.timed.map { DayTimeline.laterDetail($0, now: now, isToday: true, clock: clock) })")
    // Bars: two hours fills it, a short one keeps a sliver, due rows hold none.
    let fractions = later.timed.map { DayTimeline.durationFraction($0) }
    check(fractions == [0.375, 0.25, 0.375, 0.75], "duration bars against two hours: \(fractions)")
    check(later.due.allSatisfy { DayTimeline.durationFraction($0) == nil }, "due rows draw a dot, not a bar")
    let long = DayItem(id: "x", title: "Long", detail: "", start: at(9), end: at(13), style: .calendar, begins: at(9), ends: at(13))
    let short = DayItem(id: "y", title: "Short", detail: "", start: at(9), end: at(9.05), style: .calendar, begins: at(9), ends: at(9.05))
    check(DayTimeline.durationFraction(long) == 1 && DayTimeline.durationFraction(short) == 0.12, "capped, and never invisible")

    // Rows before Show more, and the halves' balance.
    check(DayTimeline.rowsBeforeMore(hasNext: false, oneFewer: false) == 8 && DayTimeline.rowsBeforeMore(hasNext: true, oneFewer: false) == 7
          && DayTimeline.rowsBeforeMore(hasNext: true, oneFewer: true) == 6 && DayTimeline.rowsBeforeMore(1, hasNext: true, oneFewer: true) == 1,
          "eight, less the Next card, less one when asked, never none")
    check(DayTimeline.trimLater(trimmed: false, left: 1400, right: 1000, rowAllowance: 44)
          && !DayTimeline.trimLater(trimmed: false, left: 1300, right: 1000, rowAllowance: 44),
          "trim when the left is more than a third taller")
    check(DayTimeline.trimLater(trimmed: true, left: 1310, right: 1000, rowAllowance: 44)
          && !DayTimeline.trimLater(trimmed: true, left: 1280, right: 1000, rowAllowance: 44),
          "trimmed stays trimmed unless a row back would still be within the third")
    check(!DayTimeline.trimLater(trimmed: false, left: 1400, right: 0, rowAllowance: 44), "no right half, no trim")

    // The card's buttons: only for a described Calendar event; Move only where the menu has it.
    check(DayTimeline.nextButtons(isDescribedEvent: true, menuOffersMove: true, cancelActive: false) == [.open, .move]
          && DayTimeline.nextButtons(isDescribedEvent: true, menuOffersMove: false, cancelActive: false) == [.open]
          && DayTimeline.nextButtons(isDescribedEvent: false, menuOffersMove: true, cancelActive: false).isEmpty
          && DayTimeline.nextButtons(isDescribedEvent: true, menuOffersMove: true, cancelActive: true).isEmpty,
          "Open and Move only where they work today; none during a cancel")
}

/// Calendar not granted, unknown or still syncing: the summary, the Next
/// line, the agenda and the week card say only what Wisp read, never that
/// the day or the calendar is empty, free or clear.
func noCalendarChecks(_ check: (Bool, String) -> Void, build: ([DayBlockInput], [DayNeedInput], [String]) -> DayTimeline, at: (Double) -> Double,
                      now: Double, dayStart: Double, dayEnd: Double, clock: (Double) -> String, cantRead: String) {
    let denied = SourceAccess(source: "calendar", ready: false, state: "unavailable", blocked: "calendar_access")
    let syncing = SourceAccess(source: "calendar", ready: false, state: "syncing")
    // Granted (the service says ready), shown events, not granted, unknown, syncing.
    check(DayCalendarState.of(ready: true, eventsShown: false, access: SourceAccess(source: "calendar", ready: true)) == .read
          && DayCalendarState.of(ready: false, eventsShown: true, access: denied) == .read
          && DayCalendarState.of(ready: false, eventsShown: false, access: denied) == .unread
          && DayCalendarState.of(ready: nil, eventsShown: false, access: nil) == .unread
          && DayCalendarState.of(ready: false, eventsShown: false, access: syncing) == .reading,
          "the Brief's rule: read when ready or its events show; unknown counts as unread")
    let allClear = ["Nothing is planned", "nothing else", "Nothing else", "Clear after", "free", "nothing on the clock"]
    func claims(_ text: String) -> [String] { allClear.filter { text.contains($0) } }
    // Without other items, and with a reminder, school work and a plan caught in texts.
    let bare = build([], [], [])
    let needs = [DayNeedInput(id: "r", title: "Readings", source: "reminders", due: at(17)),
                 DayNeedInput(id: "s", title: "Lab 5 · CS 101", source: "study", due: at(23.9))]
    let busy = build([DayBlockInput(id: "task:call", title: "Call Sam Rivera", start: at(19), end: at(20), kind: "task", taskID: "call"),
                      DayBlockInput(id: "pa2-a", title: "Work on PA 2", start: at(20), end: at(21.5), kind: "suggestion")],
                     needs, [])
    for (state, line) in [(DayCalendarState.unread, cantRead),
                          (.reading, "Wisp is still reading your calendar, so this may not be your whole day.")] {
        let alone = DayTimeline.blurb(bare, needs: [], isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart,
                                      calendar: state)
        let others = DayTimeline.blurb(busy, needs: needs, isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart,
                                       calendar: state)
        check(alone == line, "\(state) alone: only the calendar line: \(alone)")
        check(others.hasPrefix(line) && claims(others).isEmpty && !others.contains("on the clock"),
              "\(state) with other items: the line, then only what was read: \(others)")
        check(DayTimeline.nothingNextLine(state) == state.nextLine && claims(DayTimeline.nothingNextLine(state)).isEmpty,
              "\(state): the Next line is the calendar line alone")
        // The agenda lists every row it read, and no free stretch between them.
        let rows = DayTimeline.agenda(busy.agenda(now: now, isToday: true), without: nil, calendarRead: false).ahead
        check(!rows.contains { if case .free = $0 { return true }; return false }
              && rows.count == busy.agenda(now: now, isToday: true).ahead.filter { if case .item = $0 { return true }; return false }.count,
              "\(state): no free stretch in the agenda, every item kept")
    }
    let read = DayTimeline.blurb(busy, needs: needs, isToday: true, endOfDay: dayEnd, clock: clock, dayStart: dayStart)
    check(!read.contains("calendar"), "granted: the summary as before: \(read)")
    let readRows = DayTimeline.agenda(busy.agenda(now: now, isToday: true), without: nil).ahead
    check(readRows.contains { if case .free = $0 { return true }; return false }, "granted: free stretches stay")
    // The week card: no load (no count, no bar, never "free") without a read calendar.
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "UTC")!
    let cards = DayWeek.cards(shown: Date(timeIntervalSince1970: now), today: Date(timeIntervalSince1970: now),
                              calendar: calendar, shownLoad: nil)
    check(cards.allSatisfy { $0.load == nil } && !DayWeek.fullRow(cards)
          && cards.allSatisfy { !DayWeek.spoken($0).contains("free") },
          "no calendar: every card is its date alone, today too")
}

/// The week route's reply (GET /assistant/today/week), decoded defensively and
/// reduced to card loads, with invented data only.
func weekRouteChecks(_ check: (Bool, String) -> Void) {
    var utc = Calendar(identifier: .gregorian)
    utc.timeZone = TimeZone(identifier: "UTC")!
    utc.firstWeekday = 1
    let wednesday = utc.date(from: DateComponents(year: 2030, month: 3, day: 13, hour: 12))!
    let dates = DayWeek.dates(containing: wednesday, calendar: utc)
    let keys = DayWeek.keys(dates, calendar: utc)
    let start = DayWeek.start(of: wednesday, calendar: utc)
    check(start == "2030-03-10" && keys.first == start && keys.last == "2030-03-16" && keys.count == 7,
          "the week starts on the calendar's first weekday: \(start) \(keys)")
    utc.firstWeekday = 2
    check(DayWeek.start(of: wednesday, calendar: utc) == "2030-03-11", "a Monday-first calendar starts on Monday")
    utc.firstWeekday = 1
    check(DayWeekReply.query(start: start, timezone: "Area/City") == "/assistant/today/week?start=2030-03-10&timezone=Area/City",
          DayWeekReply.query(start: start, timezone: "Area/City"))

    func ready(_ key: String, _ spans: [[Any]], due: Int) -> [String: Any] {
        ["day": key, "calendar_ready": true, "on_clock": spans.count, "on_clock_minutes": 60, "due": due,
         "all_day": 0, "spans": spans]
    }
    var days: [[String: Any]] = keys.map { ready($0, [[540, 600, "calendar"]], due: 0) }
    days[1] = ready(keys[1], [[540, 600, "calendar"], [600, 660, "task"], [780, 900, "study"], [900, 960, "suggestion"]], due: 2)
    days[2] = ready(keys[2], [], due: 0)
    days[3] = ["day": keys[3], "calendar_ready": false]
    days[4] = ready(keys[4], [], due: 3)
    func data(_ object: [String: Any]) -> Data { try! JSONSerialization.data(withJSONObject: object) }
    let good: [String: Any] = ["start": start, "timezone": "Area/City", "days": days, "extra": "ignored"]
    let reply = DayWeekReply.decode(data(good), start: start, timezone: "Area/City", days: keys)
    check(reply?.days.count == 7 && reply?.days[3].ready == false && reply?.days[1].spans.count == 4,
          "a whole week decodes; extra keys are ignored")
    let loads = reply?.loads(workStart: 540, workEnd: 1080) ?? [:]
    // Suggestions hold nothing: three things, not four; the bar spans working hours.
    check(loads[keys[1]].map(DayWeek.countText) == "3 · 2 due", "busy: \(loads[keys[1]].map(DayWeek.countText) ?? "nil")")
    check(loads[keys[1]]?.segments.count == 2 && loads[keys[1]]?.now == nil,
          "overlapping or touching spans merge; no now line on another day")
    check(loads[keys[2]].map(DayWeek.countText) == "free", "free only when ready with nothing on the clock or due")
    check(loads[keys[3]] == nil, "a day whose calendar isn't current: its date alone")
    check(loads[keys[4]].map(DayWeek.countText) == "3 due", "nothing on the clock but due: not free")

    // Wrong shapes are no data, never a guess.
    var short = good; short["days"] = Array(days.prefix(6))
    var shifted = good; shifted["days"] = Array(days.dropFirst()) + [days[0]]
    var otherWeek = good; otherWeek["start"] = "2030-03-17"
    var otherZone = good; otherZone["timezone"] = "Elsewhere/Town"
    for (name, object) in [("six days", short), ("days out of order", shifted), ("another week", otherWeek),
                           ("another zone", otherZone)] {
        check(DayWeekReply.decode(data(object), start: start, timezone: "Area/City", days: keys) == nil, "\(name): no data")
    }
    check(DayWeekReply.decode(Data("[]".utf8), start: start, timezone: "Area/City", days: keys) == nil
          && DayWeekReply.decode(Data("not json".utf8), start: start, timezone: "Area/City", days: keys) == nil,
          "not an object: no data")
    // A ready day missing a number, with a bool for a count, or a bad span: its date alone.
    var missing = days; missing[0].removeValue(forKey: "due")
    var boolean = days; boolean[0]["on_clock"] = true
    var badSpan = days; badSpan[0]["spans"] = [[600, 540, "calendar"]]
    var miscount = days; miscount[0]["on_clock"] = 5
    var stringReady = days; stringReady[0]["calendar_ready"] = "true"
    for (name, list) in [("missing due", missing), ("a bool count", boolean), ("a backwards span", badSpan),
                         ("a count the spans don't match", miscount), ("ready as a string", stringReady)] {
        var object = good; object["days"] = list
        let day = DayWeekReply.decode(data(object), start: start, timezone: "Area/City", days: keys)?.days[0]
        check(day?.ready == false, "\(name): that day has its date alone")
    }

    // Today agrees: the route's today counts as the shown day's own card does
    // (six on the clock and four due in the Day fixture; the suggestion holds nothing).
    let todaySpans: [[Any]] = [[705, 840, "calendar"], [720, 780, "calendar"], [930, 975, "study"], [990, 1035, "study"],
                               [1080, 1140, "calendar"], [1140, 1200, "caught"], [1200, 1290, "suggestion"]]
    var withToday = days; withToday[0] = ready(keys[0], todaySpans, due: 4)
    var todayObject = good; todayObject["days"] = withToday
    let todayLoad = DayWeekReply.decode(data(todayObject), start: start, timezone: "Area/City", days: keys)
        .flatMap { DayWeekReply.load($0.days[0], workStart: 540, workEnd: 1320) }
    check(todayLoad?.onClock == 6 && todayLoad?.due == 4, "the fixture's today agrees with the shown day: \(String(describing: todayLoad))")

    // The row's form: from what was read before its first frame, then kept.
    // Wednesday is the not-ready day and is shown with no load of its own.
    let cards = DayWeek.cards(shown: wednesday, today: wednesday, calendar: utc, shownLoad: nil, weekLoads: loads)
    check(cards.filter { $0.load != nil }.count == 6 && cards[3].load == nil, "every ready day's card has its load")
    check(DayWeek.full(latched: nil, cards: cards) && !DayWeek.full(latched: false, cards: cards)
          && DayWeek.full(latched: true, cards: []), "a latched form wins over the data that arrived since")
    let noWeek = DayWeek.cards(shown: wednesday, today: wednesday, calendar: utc, shownLoad: loads[keys[0]])
    check(!DayWeek.fullRow(noWeek), "without the week (pending, failed, 404): compact")
}

