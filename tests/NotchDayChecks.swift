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

    // The lane: two hours before the hour of now, to the last thing, on whole hours.
    check(timeline.from == at(11), "today's lane starts two hours before the hour of now")
    check(timeline.to == at(20), "the lane runs to the last block's hour: \(clock(timeline.to))")
    check(timeline.hours.count == 9 && timeline.hours.first == at(11), "hour marks from 11 to 7")
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
    let minute = 60.0
    let stacked = DayTimeline.arrange([
        DayItem(id: "a", title: "a", detail: "", start: 0, end: 30 * minute, style: .calendar),
        DayItem(id: "b", title: "b", detail: "", start: 10 * minute, end: 40 * minute, style: .calendar),
        DayItem(id: "c", title: "c", detail: "", start: 20 * minute, end: 50 * minute, style: .calendar),
        DayItem(id: "d", title: "d", detail: "", start: 35 * minute, end: 60 * minute, style: .calendar),
        DayItem(id: "e", title: "e", detail: "", start: 70 * minute, end: 80 * minute, style: .calendar),
    ])
    check(stacked.map(\.column) == [0, 1, 2, 0, 0] && stacked.map(\.columns) == [3, 3, 3, 3, 1],
          "columns are reused within a cluster and reset after it: \(stacked.map(\.column)) \(stacked.map(\.columns))")
    // Back-to-back 15-minute blocks are drawn 30 minutes tall, so they overlap on
    // screen and must sit side by side, never on top of one another (the
    // 2026-10-19 stack of seven "Review: …" blocks at noon).
    let slivers = DayTimeline.arrange((0..<3).map {
        DayItem(id: "r\($0)", title: "Review \($0)", detail: "", start: Double($0) * 15 * minute,
                end: Double($0 + 1) * 15 * minute, style: .suggested)
    })
    // The first two overlap as drawn, so they share the width; the third starts
    // where the first's drawn 30 minutes end, so it reuses the first column.
    check(slivers.map(\.column) == [0, 1, 0] && slivers.allSatisfy { $0.columns == 2 },
          "short back-to-back blocks sit side by side: \(slivers.map(\.column)) \(slivers.map(\.columns))")
    let apart = DayTimeline.arrange([
        DayItem(id: "x", title: "x", detail: "", start: 0, end: 15 * minute, style: .suggested),
        DayItem(id: "y", title: "y", detail: "", start: 30 * minute, end: 45 * minute, style: .suggested),
    ])
    check(apart.allSatisfy { $0.columns == 1 }, "short blocks a drawn height apart keep the full width")
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
    check(!other.items.contains { $0.style == .overdue }, "nothing is overdue on another day")

    // The badge counts only what is due today or overdue.
    check(DayTimeline.badgeCount(needs, endOfDay: dayEnd) == 3, "badge: due today and overdue, not junk, undated or calendar")
    check(DayTimeline.badgeCount([], endOfDay: dayEnd) == 0, "no items, no badge")

    // Junk reminders.
    for junk in ["nothing", " Nothing. ", "N/A", "", "--", "ok", "None!", "x"] {
        check(DayTimeline.isJunk(junk), "junk: \"\(junk)\"")
    }
    for real in ["PA 2", "Call mom", "Get DoorDash order", "Dinner with Priya?", "茶", "Test", "Reminder"] {
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

    // The Brief's rows: always CAL, MAIL, TEXTS and SOON; a source Wisp can't read is dim.
    let utc = TimeZone(identifier: "UTC")!
    let light = DayLoad.assess(events: [], allDay: ["PA 2 [CSE-20-04]"], from: now, dayStart: dayStart, isToday: true, zone: utc)
    func mailOf(_ titles: [String]) -> [BriefNeed] { titles.map { BriefNeed(title: $0) } }
    let lines = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: []),
                                       texts: (ready: true, needing: mailOf(["nothing"]) + [BriefNeed(title: "Priya: dinner at 7?", sender: "Priya")]),
                                       soon: .item(title: "UCSC startup club", when: "Wed 6 PM", source: "calendar"))
    check(lines.map(\.tag) == ["CAL", "MAIL", "TEXTS", "SOON"], "four rows in order")
    check(lines[0].text == "Light day: just PA 2 [CSE-20-04] (all day)" && lines[0].trailing == ""
          && lines[1].text == "Nothing needs a reply" && lines[2].text == "Priya is waiting on you"
          && lines[3].source == .calendar && lines[3].trailing == "Wed 6 PM", "row contents: \(lines.map(\.text))")
    check(!lines.contains(where: \.muted), "synced sources are never dim")
    let quiet = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: []), texts: (ready: true, needing: mailOf(["nothing"])),
                                       soon: .nothing)
    check(quiet.map(\.text).suffix(2) == ["No texts need you", "Nothing in the next week"] && !quiet.contains(where: \.muted),
          "TEXTS and SOON stay with nothing to say")
    let unsynced = DayTimeline.briefLines(calendar: nil, calendarUnsynced: true, mail: (ready: false, needing: mailOf(["x"])),
                                          texts: (ready: false, needing: []), soon: .unknown)
    check(unsynced.map(\.tag) == ["CAL", "MAIL", "TEXTS", "SOON"] && unsynced.allSatisfy(\.muted)
          && unsynced.map(\.text) == ["Calendar hasn't synced yet", "Mail hasn't been checked yet",
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
                                       BriefNeed(title: "Priya: dinner?", sender: "Priya", received: 90)]), soon: .nothing)
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
    check(described[2].text == "Priya and Mom are waiting on you" && !described[2].text.contains("dinner"),
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
          && DayTimeline.alreadyPlanned("Reply to Priya", tasks: [(title: "Review: Reply to Priya", active: true)])
          && !DayTimeline.alreadyPlanned("Get doordash order", tasks: [(title: "Get doordash order", active: false)])
          && !DayTimeline.alreadyPlanned("Get doordash order", tasks: [(title: "Call the bank", active: true)]),
          "Plan time isn't offered twice for the same need")
    check(DayTimeline.plannedTitle("Reply to Priya", review: true) == "Review: Reply to Priya"
          && DayTimeline.plannedTitle("PA 2", review: false) == "PA 2", "planned titles")

    // Unmistakable bulk mail never counts as needing you, and the row says it was left out.
    let inbox = ["You're subscribed: The Download", "Can you review my draft?", "50% off everything",
                 "Weekly digest: CSE 20", "Lab moved to Thursday", "nothing"]
    check(DayTimeline.mailNeedingYou(inbox) == ["Can you review my draft?", "Lab moved to Thursday"],
          "newsletters, promos and placeholders are left out: \(DayTimeline.mailNeedingYou(inbox))")
    let promos = DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(inbox)), texts: nil, soon: .nothing)[1]
    check(promos.text == "Can you review my draft?" && promos.trailing == "+1 more", "the MAIL row describes real mail: \(promos.text)")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(["Can you review my draft?", "50% off everything"])),
                                 texts: nil, soon: .nothing)[1].trailing == "+1 promo", "one email: the row names the promo it left out")
    check(DayTimeline.briefLines(calendar: light, mail: (ready: true, needing: mailOf(["Can you review my draft?"])), texts: nil,
                                 soon: .nothing)[1].trailing == "", "no promos, no note")
    for personal in ["Last chance to submit your thesis form", "Webinar slides for Thursday", "Tech Week starts today",
                     "Don't miss the lab meeting", "Re: project offer", "Meeting with Priya", "This week in CSE 20"] {
        check(!DayTimeline.isBulkMail(personal), "a subject a person could write still counts: \(personal)")
    }
    check(DayTimeline.isBulkMail("Last chance: 20% off") && DayTimeline.isBulkMail("Our October newsletter")
          && DayTimeline.isBulkMail("Thanks for subscribing!"), "unmistakable bulk mail")

    briefSummaryChecks(check, dayStart: dayStart, now: now)

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
}
