// Contract for the notch Brief's summary and row details
// (app/Sources/WispApp/BriefSummary.swift). Compiled with NotchTabChecks.swift;
// NotchDayChecks.swift calls briefSummaryChecks. Pure logic: never launches Wisp
// or contacts its service.
import Foundation

func briefSummaryChecks(_ check: (Bool, String) -> Void, dayStart: Double, now: Double) {
    // A composed brief in the service's shape (brief.py _render_brief).
    let text = """
    Good afternoon, Sam — Saturday, October 17. ☀️

    **🧭 Keeping in mind**
    - PA 2 is due tonight, and you said you want to finish CSE 20 strong.
    - Priya is in today's messages.

    **📅 Today**
    - All day – PA 2 [CSE-20-04]

    **✅ Reminders due today**
    - 11:59 PM – PA 2 (overdue)

    **📧 Inbox — 12 emails · 5 unread**
    **Worth a look**
    - Advisor: Thesis meeting moved
    - Tech Week: starts today

    ## 💬 Messages
    **👤 Priya**
    - Asked about dinner at 7.

    Let me know if you'd like me to dig into any of these or draft a reply.
    """
    let brief = ComposedBrief.parse(text)
    check(brief.sections.map(\.kind) == [.keeping, .calendar, .reminders, .mail, .messages],
          "sections by header, greeting and closing dropped: \(brief.sections.map(\.kind))")
    check(brief.keeping == ["PA 2 is due tonight, and you said you want to finish CSE 20 strong.", "Priya is in today's messages."],
          "the summary has no mail, messages or calendar prose: \(brief.keeping)")
    check(brief.intro.isEmpty, "no How Wisp works section after the first day")
    check(brief.lines(.mail) == ["Worth a look", "Advisor: Thesis meeting moved", "Tech Week: starts today"],
          "sub-headers stay in their section: \(brief.lines(.mail))")
    check(brief.lines(.messages) == ["👤 Priya", "Asked about dinner at 7."], "a # header opens Messages: \(brief.lines(.messages))")
    check(!brief.sections.flatMap(\.lines).contains { $0.contains("**") || $0.hasPrefix("- ") || $0.contains("Let me know") },
          "plain lines only")
    check(ComposedBrief.parse("Wisp is still syncing your calendar and mail.").sections.isEmpty,
          "a brief without sections gives no summary")
    check(ComposedBrief.header("**Bold** and more") == nil && ComposedBrief.header("#hashtag") == nil
          && ComposedBrief.header("### Messages") == "Messages", "only whole-line headers count")

    // Upcoming: the week's events and reminders, without today's remaining calendar.
    let hour = 3600.0
    let dayEnd = dayStart + 24 * hour
    let reply: [String: Any] = ["commitments": [
        ["id": "a", "title": "Standup", "when_ts": now + hour, "source": "calendar", "kind": "event"],
        ["id": "b", "title": "Essay outline", "when_ts": now + 2 * hour, "source": "reminders", "kind": "reminder"],
        ["id": "c", "title": "UCSC startup club", "when_ts": dayEnd + 18 * hour, "source": "calendar", "kind": "event"],
        ["id": "d", "title": "nothing", "when_ts": dayEnd + hour, "source": "reminders", "kind": "reminder"],
        ["id": "e", "title": "Far away", "when_ts": now + 8 * 86400, "source": "calendar", "kind": "event"],
        ["id": "f", "title": "Past", "when_ts": now - hour, "source": "reminders", "kind": "reminder"],
        ["id": "g", "title": "Holiday", "when_ts": dayEnd, "source": "calendar", "kind": "event", "all_day": 1],
        ["title": "no id", "when_ts": now],
    ]]
    let items = UpcomingItem.parseList(reply)
    check(items?.count == 7, "rows without an id are skipped")
    let soon = UpcomingItem.soon(items ?? [], after: now, dayEnd: dayEnd)
    check(soon.map(\.id) == ["b", "g", "c"], "SOON: reminders later today, then the coming week: \(soon.map(\.id))")
    check(soon[1].allDay && !soon[0].allDay, "all-day flag")
    check(UpcomingItem.parseList(["detail": "x"]) == nil, "an unreadable reply is unknown, not empty")
    check(UpcomingItem.soon(items ?? [], after: now, dayEnd: dayEnd, limit: 1).map(\.id) == ["b"], "limit")

    // Row details.
    let cal = BriefDetail.calendar(allDay: ["PA 2"], events: [("Club", dayStart + 15 * hour, dayStart + 16 * hour),
                                                              ("Call", dayStart + 18 * hour, nil)],
                                   unsynced: false, clock: { String(Int(($0 - dayStart) / hour)) })
    check(cal.lines.map(\.leading) == ["All day", "15–16", "18"] && cal.source == .calendar, "calendar details: all-day first")
    check(BriefDetail.calendar(allDay: [], events: [], unsynced: true, clock: { _ in "" }).lines.first?.muted == true,
          "unsynced calendar details are dim")
    let inbox = [BriefNeed(title: "Can you review my draft?", sender: "Jordan", person: true, received: now - 2 * hour),
                 BriefNeed(title: "Thesis meeting", sender: "Prof. Lee", person: true, received: now - hour),
                 BriefNeed(title: "Tech Week starts today", sender: "UCSC Events", received: now),
                 BriefNeed(title: "50% off everything", sender: "Shop", received: now),
                 BriefNeed(title: "nothing", sender: "x")]
    let mail = BriefDetail.mail(needing: inbox, ready: true)
    check(mail.lines.map(\.text) == ["Prof. Lee · Thesis meeting", "Jordan · Can you review my draft?",
                                     "UCSC Events · Tech Week starts today", "1 newsletter or promo left out"] && mail.source == .mail,
          "mail details: people first, sender and subject, then the bulk left out: \(mail.lines.map(\.text))")
    // With the composed summary: its mail section first, then only the emails it doesn't name.
    let summarized = BriefDetail.mail(needing: inbox, ready: true,
                                      summary: ["Worth a look", "Advisor: Thesis meeting moved to Tuesday", "Other mail", "3 newsletters"])
    check(summarized.lines.map(\.text) == ["Worth a look", "Advisor: Thesis meeting moved to Tuesday", "Other mail", "3 newsletters",
                                           "Needs you", "Jordan · Can you review my draft?", "UCSC Events · Tech Week starts today",
                                           "1 newsletter or promo left out"],
          "mail details: the summary, then needing emails it doesn't already name: \(summarized.lines.map(\.text))")
    check(!BriefDetail.mail(needing: inbox, ready: true, summary: ["Tech Week: starts today!"]).lines.contains { $0.text.contains("UCSC") },
          "punctuation doesn't hide a repeat")
    // A written summary replaces the summary's sender-and-subject section; the needing list follows.
    let written = BriefDetail.mail(needing: inbox, ready: true,
                                   summary: ["Worth a look", "Advisor: Thesis meeting moved to Tuesday"],
                                   prose: "Prof. Lee moved your thesis meeting to Tuesday. The rest is promos.")
    check(written.lines.first?.text == "Prof. Lee moved your thesis meeting to Tuesday. The rest is promos."
          && written.lines.first?.paragraph == true && !written.lines.contains { $0.text == "Worth a look" }
          && written.lines.contains { $0.text == "Jordan · Can you review my draft?" }
          && !written.lines.contains { $0.text == "Prof. Lee · Thesis meeting" },
          "the written summary leads, the needing emails it doesn't name follow: \(written.lines.map(\.text))")
    check(BriefDetail.mail(needing: inbox, ready: true, summary: nil, prose: "Quiet day.").lines.first?.text == "Quiet day.",
          "with a written summary there's no 'arrives later' note")
    check(BriefMail.isPrivate("Re: Private details omitted.") && BriefMail.isPrivate("Invitation: Private details omitted.")
          && BriefMail.shown([BriefNeed(title: "Re: Private details omitted.", sender: "John Morada", person: true)]).isEmpty
          && BriefMail.privateCount([BriefNeed(title: "Re: Private details omitted.")]) == 1
          && BriefMail.isPrivate("Deadline notice (private details omitted).")
          && BriefMail.isPrivate("Unverified security incident notice (details omitted)."),
          "a withheld subject behind Re: is counted, never listed")
    let wordless = [BriefNeed(title: "🎉🎉", sender: "Ann", person: true)]
    check(!BriefDetail.mail(needing: wordless, ready: true, summary: ["Worth a look"]).lines.contains { $0.text.contains("Ann") }
          && BriefMail.row(overview: "Quiet", needing: wordless) == "Nothing needs you · Quiet",
          "a subject without words is neither listed nor counted")
    check(BriefMail.row(overview: "Office hours", needing: [BriefNeed(title: "Office hours", sender: "John", person: true),
                                                           BriefNeed(title: "Canceled: Office hours", sender: "John", person: true)])
            == "Office hours · 1 needs you", "repeats count once, as the details list them")
    check(BriefMail.row(overview: "Mostly event updates and tech-week promos", needing: inbox)
            == "Mostly event updates and tech-week promos · 3 need you"
          && BriefMail.row(overview: "12 new, mostly newsletters and promos", needing: [BriefNeed(title: "50% off")])
            == "Nothing needs you · 12 new, mostly newsletters and promos"
          && BriefMail.row(overview: "Quiet", needing: [BriefNeed(title: "Private details omitted.")]) == "Quiet · 1 needs you",
          "MAIL row: the gist and how many need you; promos don't count, private emails do")
    check(BriefMail.describe(BriefNeed(title: "Lunch plans")) == "Lunch plans" && BriefMail.describe(BriefNeed(title: "Re: lunch")) == "lunch", "no sender: the subject alone, its prefix moved to the tag")
    check(BriefMail.describe(BriefNeed(title: "x", sender: "Ann")) == "Ann · x", "sender and subject")
    // Names not addresses, repeats collapsed, prefixes as tags, private subjects counted.
    let messy = [
        BriefNeed(title: "Dublin Community Office Hours", sender: "John Morada <john@example.edu>", person: true, received: now - 60),
        BriefNeed(title: "Re: Dublin Community Office Hours", sender: "John Morada <john@example.edu>", person: true, received: now - 120),
        BriefNeed(title: "Canceled: Dublin Community Office Hours", sender: "John Morada", person: true, received: now - 180),
        BriefNeed(title: "Private details omitted.", sender: "Bank", received: now),
        BriefNeed(title: "Private details omitted.", sender: "Bank", received: now - 5),
        BriefNeed(title: "Re: Re: Lunch Friday?", sender: "<sam@example.com>", person: true, received: now - 300),
    ]
    let collapsed = BriefMail.shown(messy)
    check(collapsed == [BriefMail.Shown(tag: "Canceled", text: "John Morada · Dublin Community Office Hours", count: 3,
                                        subject: "Dublin Community Office Hours"),
                        BriefMail.Shown(tag: "Re", text: "sam@example.com · Lunch Friday?", count: 1, subject: "Lunch Friday?")],
          "one line per sender and subject, no brackets, prefix as a tag: \(collapsed)")
    let messyDetail = BriefDetail.mail(needing: messy, ready: true)
    check(messyDetail.lines.map(\.text) == ["John Morada · Dublin Community Office Hours ×3", "sam@example.com · Lunch Friday?",
                                            "2 private emails not shown"] && messyDetail.lines[1].leading == "Re"
          && messyDetail.lines[0].leading == "Canceled"
          && !messyDetail.lines.contains { $0.text.contains("Private details omitted") || $0.text.contains("<") },
          "mail details: ×N, a muted tag, and private subjects counted, never shown: \(messyDetail.lines.map(\.text))")
    check(collapsed[0].rowLine == "Canceled: John Morada · Dublin Community Office Hours ×3" && collapsed[1].rowLine == collapsed[1].line,
          "a cancellation stays visible in the row, which has no tag column")
    check(BriefMail.splitPrefix("Updated invitation: Standup") == ("Updated", "Standup")
          && BriefMail.splitPrefix("Re:") == ("Re", "Re:") && BriefMail.splitPrefix("Note: bring IDs") == ("", "Note: bring IDs"),
          "only known prefixes become tags")
    check(BriefMail.displayName("\"Morada, John\" <j@x.edu>") == "Morada, John" && BriefMail.displayName("<j@x.edu>") == "j@x.edu",
          "display names")
    check(BriefDetail.mail(needing: [], ready: false).lines.map(\.text) == ["Mail hasn't been checked yet"],
          "unread mail details")
    check(BriefDetail.mail(needing: [BriefNeed(title: "50% off")], ready: true).lines.map(\.text) == ["1 newsletter or promo left out"],
          "only bulk: says so")

    let texts = [BriefNeed(title: "Priya: are we still on for dinner?", sender: "Priya", received: now - hour),
                 BriefNeed(title: "Priya: lmk!", sender: "Priya", received: now - 600),
                 BriefNeed(title: "Mom: call me", sender: "Mom", received: now - 3 * hour),
                 BriefNeed(title: "Sam: thanks!", sender: "Sam", received: now - 60, replied: true),
                 BriefNeed(title: "Ravi: see you there", received: now - 5 * hour),
                 BriefNeed(title: "nothing", sender: "Bot")]
    let waiting = BriefTexts.waiting(texts)
    check(waiting == [BriefTexts.Person(name: "Priya", count: 2, latest: now - 600),
                      BriefTexts.Person(name: "Mom", count: 1, latest: now - 3 * hour),
                      BriefTexts.Person(name: "Ravi", count: 1, latest: now - 5 * hour)],
          "one entry per person waiting, newest first; answered and placeholder texts left out: \(waiting)")
    let detail = BriefDetail.texts(needing: texts, ready: true, age: { BriefTexts.age($0, now: now) })
    check(detail.lines.map(\.text) == ["Priya · 2 texts", "Mom · 1 text", "Ravi · 1 text"] && detail.lines.map(\.leading) == ["10m", "3h", "5h"]
          && detail.source == .messages, "texts details: who, how many, how long: \(detail.lines.map(\.text))")
    // With the composed summary: its messages section without quotes or the people listed under it.
    let composedTexts = BriefDetail.texts(needing: texts, ready: true,
                                          summary: ["Priya · 2 messages, latest 1:40 PM — Priya: \u{201C}lmk!\u{201D}",
                                                    "Sam · 2 messages, latest 10:00 AM — Me: \u{201C}np\u{201D}",
                                                    "Book club · 9 messages, latest 9:00 AM — Ann: \u{201C}see \u{201D}\u{201C}you\u{201D}",
                                                    "…and 2 other conversation(s)."],
                                          age: { _ in "1h" })
    check(composedTexts.lines.map(\.text) == ["Sam · 2 messages, latest 10:00 AM", "Book club · 9 messages, latest 9:00 AM",
                                              "…and 2 other conversation(s).", "Waiting on you", "Priya · 2 texts", "Mom · 1 text",
                                              "Ravi · 1 text"]
          && !composedTexts.lines.contains { $0.text.contains("lmk") || $0.text.contains("np") || $0.text.contains("\u{201C}") },
          "texts details: the summary without quotes or waiting people, then who's waiting: \(composedTexts.lines.map(\.text))")
    check(BriefTexts.row(overview: "Mostly family check-ins", people: waiting) == "Mostly family check-ins · 3 waiting on you"
          && BriefTexts.row(overview: "Quiet", people: []) == "Quiet · nobody waiting",
          "TEXTS row: the gist and how many are waiting")
    let crowd = (1...8).map { BriefNeed(title: "P\($0): hi", sender: "P\($0)", received: now - Double($0) * 60) }
    check(BriefDetail.texts(needing: crowd, ready: true, age: { _ in "" }).lines.last?.text == "+2 more",
          "more than six waiting: the rest are counted, as the row counts them")
    // No composed summary today: the dropdowns say so first instead of silently dropping it.
    let unsummarizedMail = BriefDetail.mail(needing: inbox, ready: true, summary: nil)
    check(unsummarizedMail.lines.first?.text == BriefDetail.noSummaryYet && unsummarizedMail.lines.first?.muted == true
          && unsummarizedMail.lines.dropFirst().first?.text == "Prof. Lee · Thesis meeting", "MAIL without a summary today")
    check(BriefDetail.texts(needing: texts, ready: true, summary: nil, age: { _ in "" }).lines.first?.text == BriefDetail.noSummaryYet
          && BriefDetail.mail(needing: inbox, ready: true, summary: []).lines.first?.text != BriefDetail.noSummaryYet
          && BriefDetail.texts(needing: texts, ready: true, needsAccess: true, summary: nil, age: { _ in "" }).lines.count == 1,
          "TEXTS without a summary today; an empty section or no access says nothing about it")
    check(BriefTexts.age(now - 30, now: now) == "1m" && BriefTexts.age(now - 2 * 86400, now: now) == "2d", "ages")
    check(!detail.lines.contains { $0.text.contains("dinner") || $0.text.contains("call me") }, "a text's words are never shown")
    check(BriefTexts.row([]) == "No texts need you" && BriefTexts.row(Array(waiting.prefix(1))) == "Priya is waiting on you"
          && BriefTexts.row(waiting) == "Priya, Mom and Ravi are waiting on you"
          && BriefTexts.row(waiting + [BriefTexts.Person(name: "Ann", count: 1, latest: nil)]) == "Priya, Mom and 2 others are waiting on you",
          "TEXTS row wording")
    check(BriefDetail.texts(needing: [texts[3]], ready: true, age: { _ in "" }).lines.map(\.text) == ["No texts need you"],
          "everyone answered: no texts need you")
    check(BriefDetail.texts(needing: [], ready: false, age: { _ in "" }).lines.first?.muted == true, "unread messages details are dim")
    let locked = BriefDetail.texts(needing: texts, ready: false, needsAccess: true, age: { _ in "" })
    check(locked.lines.map(\.text) == ["Wisp can't read your texts until it has Full Disk Access."]
          && locked.action?.url.hasPrefix("x-apple.systempreferences:") == true && locked.action?.url.contains("Privacy_AllFiles") == true,
          "without Full Disk Access the details say so and link to its settings")
    check(BriefDetail.texts(needing: texts, ready: true, age: { _ in "" }).action == nil, "readable texts open Messages")
    check(BriefTexts.name(BriefNeed(title: "Me: on my way")) == "Me" && BriefTexts.waiting([BriefNeed(title: "Me: on my way")]).isEmpty,
          "your own text never waits on you")
    let week = BriefDetail.soon(soon, when: { $0.allDay ? "Sun" : "later" })
    check(week.lines.map(\.text) == ["Essay outline", "Holiday", "UCSC startup club"] && week.source == .reminders,
          "SOON details open the first item's app")
    check(BriefDetail.soon(nil, when: { _ in "" }).lines.first?.muted == true
          && BriefDetail.soon([], when: { _ in "" }).lines.map(\.text) == ["Nothing in the next week"], "SOON details when unknown or empty")
    briefTileChecks(check, dayStart: dayStart, now: now, soon: soon)
}

/// The Brief's count tiles: a tile's number is what its list holds.
func briefTileChecks(_ check: (Bool, String) -> Void, dayStart: Double, now: Double, soon: [UpcomingItem]) {
    let dayEnd = dayStart + 86400
    let clock = { (t: Double) in t == now + 3600 ? "3 PM" : "9 AM" }
    // Today: overdue first, then due today by time, then Day tasks; a task
    // made from a reminder ("Review: …" too) is listed once; later due dates
    // and junk titles stay out.
    let dated = [
        BriefTodo(id: "late", title: "Return library book", due: now - 7200),
        BriefTodo(id: "pa2", title: "PA 2", due: now + 3600),
        BriefTodo(id: "allday", title: "Pay rent", due: dayStart + 1, allDay: true),
        BriefTodo(id: "next-week", title: "Essay outline", due: dayEnd + 3 * 86400),
        BriefTodo(id: "junk", title: "…", due: now + 60),
    ]
    let tasks = [BriefTodo(id: "t1", title: "Review: PA 2"), BriefTodo(id: "t2", title: "Call the bank", start: now + 3600),
                 BriefTodo(id: "t3", title: "Stretch")]
    let todos = BriefTiles.todo(dated: dated, tasks: tasks, now: now, dayStart: dayStart, dayEnd: dayEnd, isToday: true)
    check(todos.map(\.id) == ["late", "allday", "pa2", "t2", "t3"],
          "Today: overdue, then due today, then tasks, each once: \(todos.map(\.id))")
    check(BriefTiles.todo(dated: dated, tasks: [], now: now, dayStart: dayStart, dayEnd: dayEnd, isToday: false)
            .map(\.id) == ["allday", "late", "pa2"],
          "another day has nothing overdue: what's due that day, by time")
    let todayList = BriefTiles.todayDetail(todos, now: now, dayStart: dayStart, clock: clock)
    check(todayList.lines.map(\.leading) == ["Overdue", "Today", "Due 3 PM", "3 PM", "To do"]
            && todayList.lines.count == todos.count,
          "Today's list holds exactly its count, labelled: \(todayList.lines.map(\.leading))")
    let many = (0..<11).map { BriefTodo(id: "m\($0)", title: "Task \($0)") }
    let long = BriefTiles.todayDetail(many, now: now, dayStart: dayStart, clock: clock)
    check(long.lines.count == BriefTiles.shown + 1 && long.lines.last?.text == "+3 more",
          "a long Today list says how many more, adding up to its count")
    check(BriefTiles.todayDetail([], now: now, dayStart: dayStart, clock: clock).lines.map(\.text) == ["Nothing to do today"],
          "a zero Today count opens on its empty state")

    // Events: today's calendar, then what's coming up; empty days say so.
    let calendar = BriefDetail.calendar(allDay: [], events: [(title: "Standup", start: now + 3600, end: nil)],
                                        unsynced: false, clock: clock).lines
    let events = BriefTiles.eventsDetail(today: calendar, unsynced: false, soon: soon, when: { _ in "Mon" })
    check(events.lines.first?.text == "Standup" && events.lines.contains { $0.id == "soon-header" }
            && events.lines.filter { $0.id.hasPrefix("soon-") && $0.id != "soon-header" }.count == soon.count
            && events.source == .calendar,
          "Events lists today's calendar, then what's coming up")
    let empty = BriefTiles.eventsDetail(today: [], unsynced: false, soon: [], when: { _ in "" })
    check(empty.lines.map(\.text) == ["Nothing scheduled today"] && empty.lines[0].leading.isEmpty,
          "a zero Events count opens on Nothing scheduled today")
    check(BriefTiles.eventsDetail(today: [], unsynced: true, soon: nil, when: { _ in "" }).lines.map(\.text)
            == ["Reading your calendar…"], "an unsynced calendar says so instead of claiming an empty day")

    // Top 3: the service's pick first, then to-dos, then the next event, each once.
    let picked = [NeedsYouRow(id: "a", label: "OVERDUE", text: "Return library book", overdue: true),
                  NeedsYouRow(id: "b", label: "MAIL", text: "“Thesis” from Advisor", overdue: false)]
    let top = BriefTiles.top(picked: picked, todos: todayList.lines,
                             next: BriefDetail.Line(id: "next", leading: "3 PM", text: "Standup"))
    check(top.map(\.text) == ["Return library book", "“Thesis” from Advisor", "Pay rent"],
          "Top 3 names each thing once, the service's pick first: \(top.map(\.text))")
    check(top.first?.need == picked[0] && top.last?.need == nil,
          "a Top 3 line from Needs you carries its row for Clear; a to-do line doesn't")
    check(BriefTiles.top(picked: [], todos: [], next: BriefDetail.Line(id: "next", leading: "3 PM", text: "Standup"))
            .map(\.text) == ["Standup"], "a quiet day's Top 3 is the next event")
    check(BriefTiles.top(picked: [], todos: BriefTiles.todayDetail([], now: now, dayStart: dayStart, clock: clock).lines, next: nil).isEmpty,
          "an empty state is never a Top 3 item")

    // Under the Top 3: the regular MAIL and TEXTS summaries, nothing new.
    let inbox = [BriefNeed(title: "Thesis meeting", sender: "Prof. Lee", person: true, received: now - 3600)]
    let chats = [BriefNeed(title: "dinner?", sender: "Priya", person: true, received: now - 600)]
    let prose = "Prof. Lee moved your thesis meeting to Tuesday. The rest is newsletters."
    func rows(mailReady: Bool?, textsReady: Bool?, access: Bool = false, overviews: Bool = true) -> [DayTimeline.BriefLine] {
        DayTimeline.briefLines(calendar: nil, mail: mailReady.map { (ready: $0, needing: inbox) },
                               texts: textsReady.map { (ready: $0, needing: chats) }, textsNeedAccess: access,
                               mailOverview: overviews ? "Mostly event updates" : nil,
                               textsOverview: overviews ? "Mostly dinner plans" : nil, soon: .unknown)
    }
    let glance = BriefTiles.glance(rows(mailReady: true, textsReady: true), mailProse: "  " + prose + "\n")
    check(glance.map(\.leading) == ["Mail", "Texts"] && glance.map(\.id) == ["glance-MAIL", "glance-TEXTS"],
          "the default view shows MAIL then TEXTS, and no other row: \(glance.map(\.leading))")
    check(glance.first?.text == prose && glance.allSatisfy { !$0.muted && !$0.paragraph },
          "MAIL shows the written inbox summary, as a short line the row style cuts to two")
    check(glance.last?.text == BriefTexts.row(overview: "Mostly dinner plans", people: BriefTexts.waiting(chats))
            && !glance.last!.text.contains("dinner?"),
          "TEXTS is the regular overview row and never what anyone wrote: \(glance.last?.text ?? "")")
    let noProse = BriefTiles.glance(rows(mailReady: true, textsReady: true), mailProse: " ")
    check(noProse.first?.text == BriefMail.row(overview: "Mostly event updates", needing: inbox),
          "without a written summary MAIL is the overview row: \(noProse.first?.text ?? "")")
    let older = BriefTiles.glance(rows(mailReady: true, textsReady: true, overviews: false), mailProse: nil)
    check(older.count == 2 && older.allSatisfy { !$0.text.isEmpty && !$0.muted },
          "an older service with no overviews still gets a MAIL and TEXTS line: \(older.map(\.text))")
    let unread = BriefTiles.glance(rows(mailReady: nil, textsReady: false), mailProse: prose)
    check(unread.map(\.text) == ["Mail hasn't been checked yet", "Messages haven't been checked yet"]
            && unread.allSatisfy(\.muted),
          "a source not read yet is one dim line, never an old summary: \(unread.map(\.text))")
    check(BriefTiles.glance(rows(mailReady: true, textsReady: true, access: true), mailProse: nil).last?.text
            == "Texts need Full Disk Access", "TEXTS says when it needs Full Disk Access")

    // Keyboard order wraps; each tile has its own digit.
    check(BriefTile.today.moved(-1) == .events && BriefTile.events.moved(1) == .today && BriefTile.mail.moved(1) == .messages,
          "arrow keys wrap around the four tiles")
    check(BriefTile.allCases.map(\.shortcut) == ["1", "2", "3", "4"]
            && BriefTile.allCases.map(\.title) == ["Today", "Mail", "Messages", "Events"],
          "tiles in order with 1–4")

    // Unread sources: why, and the setting that fixes it, never an all-clear.
    let calendarOff = SourceAccess(source: "calendar", ready: false, state: "unavailable", blocked: "calendar_access")
    let eventsOff = BriefTiles.eventsDetail(today: [], unsynced: true, soon: nil, when: { _ in "" }, access: calendarOff)
    check(eventsOff.lines.first?.text == "Wisp can't see your calendar yet" && eventsOff.action == BriefDetail.calendarsAccess,
          "Events says the calendar can't be read and opens its setting")
    check(BriefTiles.eventsDetail(today: [], unsynced: true, soon: nil, when: { _ in "" }).lines.first?.text == "Reading your calendar…",
          "with no reason known, Events says it's reading")
    let remindersOff = SourceAccess(source: "reminders", ready: false, blocked: "reminders_access")
    let noTodos = BriefTiles.todayDetail([], now: now, dayStart: dayStart, clock: { _ in "" }, reminders: remindersOff)
    check(noTodos.lines.first?.text == "Wisp can't see your reminders yet" && noTodos.action == BriefDetail.remindersAccess,
          "Today never says \"Nothing to do\" while Reminders is unread")
    let someTodos = BriefTiles.todayDetail([BriefTodo(id: "a", title: "Essay", due: now + 60)], now: now, dayStart: dayStart,
                                           clock: { _ in "" }, reminders: remindersOff)
    check(someTodos.lines.last?.text == "Wisp can't see your reminders yet" && someTodos.action == BriefDetail.remindersAccess,
          "a Today list from what was read says Reminders is missing")
    check(BriefTiles.emptyTop(unread: []) == "Nothing needs you right now"
          && BriefTiles.emptyTop(unread: ["calendar", "email"]) == "Wisp can't see your calendar and mail yet.",
          "the Top 3's empty line names what wasn't read")
    let mailClosed = BriefDetail.mail(needing: [], ready: false, access: SourceAccess(source: "email", ready: false, blocked: "mail_closed"))
    check(mailClosed.lines.first?.text == "Wisp reads Mail while Mail is open" && mailClosed.action == BriefDetail.fullDiskAccess,
          "Mail closed says so and offers Full Disk Access")
    check(BriefDetail.texts(needing: [], ready: false, access: SourceAccess(source: "messages", ready: false, state: "syncing"),
                            age: { _ in "" }).lines.first?.text == "Reading your texts…", "texts being read say so")
    check(BriefDetail.calendar(allDay: ["🎂"], events: [(title: "1:1", start: now, end: nil)], unsynced: false, clock: { _ in "9" })
            .lines.map(\.text) == ["🎂", "1:1"], "calendar details keep letterless titles")
    firstDayChecks(check)
    briefPlateChecks(check)
}

/// The first launch day: what Wisp does and when, the summary times when none
/// came yet, and the fix links for a Google sign-in and Contacts.
func firstDayChecks(_ check: (Bool, String) -> Void) {
    let first = ComposedBrief.parse("""
    Good morning — Friday, October 16. ☀️

    **🌱 How Wisp works**
    - I send a summary at 8 AM and 8 PM, ask how your day went after 5 PM, and learn a routine once I've seen it in three different weeks.

    **🧭 Keeping in mind**
    - One
    - Two
    - Three
    - Four
    - Five
    - Six

    **📅 Today**
    - Nothing on your calendar.
    """)
    check(first.intro == ["I send a summary at 8 AM and 8 PM, ask how your day went after 5 PM, and learn a routine once I've seen it in three different weeks."]
          && first.keeping.count == 6, "the first day's How Wisp works section is its own: \(first.sections.map(\.kind))")
    let short = ComposedBrief.inMind(first.keeping, showAll: false)
    check(short.shown == ["One", "Two", "Three", "Four"] && short.more == 2, "Keeping in mind shows four, then Show all")
    check(ComposedBrief.inMind(first.keeping, showAll: true) == (first.keeping, 0)
          && ComposedBrief.inMind(["One"], showAll: false) == (["One"], 0), "Show all shows every line; a short list has no Show all")

    let schedule = BriefSchedule.parse(["times": ["morning": 8, "evening": 20], "first_brief_building": true])
    check(schedule == BriefSchedule(morning: 8, evening: 20, buildingFirst: true), "schedule parses: \(String(describing: schedule))")
    check(BriefSchedule.parse(["times": ["morning": 8, "evening": 20]])?.buildingFirst == false
          && BriefSchedule.parse(["times": ["morning": 14, "evening": 20]]) == nil && BriefSchedule.parse(nil) == nil,
          "an older service says nothing about the first Brief; an hour outside its half is refused")
    let times = BriefSchedule(morning: 7, evening: 18)
    check(times.noSummaryLine(hour: 5) == "Today's summary arrives at 7 AM · or press Refresh"
          && times.noSummaryLine(hour: 12) == "Summaries arrive at 7 AM and 6 PM · or press Refresh"
          && times.noSummaryLine(hour: 5, locale: Locale(identifier: "en_GB")) == "Today's summary arrives at 07:00 · or press Refresh",
          "no summary yet: the real times, never a guess")
    check(BriefDetail.texts(needing: [], ready: true, summary: nil, noSummary: times.noSummaryLine(hour: 5), age: { _ in "" })
            .lines.first?.text == "Today's summary arrives at 7 AM · or press Refresh", "the TEXTS list names the time")

    // A Google account whose sign-in must be redone: the mail list links to Settings > Google.
    let row = SourceAccess(source: "email", ready: true)
    let reconnect = SourceAccess.mail(row, readBy: ["me@gmail.com"], unchecked: UncheckedMail(reconnect: ["work@gmail.com"]))
    let syncing = SourceAccess.mail(row, readBy: ["me@gmail.com"], unchecked: UncheckedMail(syncing: ["work@gmail.com"]))
    check(reconnect.fixLink == BriefDetail.googleSettings && syncing.fixLink == nil
          && SourceAccess(source: "calendar", ready: false, blocked: "calendar_access").fixLink == BriefDetail.calendarsAccess,
          "only a sign-in to redo links to Settings > Google; System Settings links stay as they were")
    let partly = BriefDetail.mail(needing: [BriefNeed(title: "Lunch?", sender: "Sam", person: true)], ready: true,
                                  summary: [], access: reconnect)
    check(partly.lines.first?.text == "Couldn't check work@gmail.com" && partly.action == BriefDetail.googleSettings
          && partly.lines.contains { $0.text == "Sam · Lunch?" }, "partly read mail: what was read, and the way to reconnect")
    check(BriefDetail.mail(needing: [], ready: true, summary: [], access: syncing).action == nil,
          "a check still running has nothing to fix")

    // Contacts turned down: one row like the other sources, with its setting.
    check(SourceAccess.contacts(denied: false, enabled: true) == nil && SourceAccess.contacts(denied: true, enabled: false) == nil,
          "no Contacts row before macOS said no, or with Contacts off in Wisp")
    let contacts = SourceAccess.contacts(denied: true, enabled: true)
    check(contacts?.headline == "Wisp can't see your contacts yet" && contacts?.fixLink == BriefDetail.contactsAccess
          && contacts?.detail.contains("names, not numbers") == true && contacts?.symbol == "person.crop.circle",
          "Contacts denied: why, and the Contacts setting")
}

/// R6: the Brief's two zones, and its "On your plate" list in one urgency order.
func briefPlateChecks(_ check: (Bool, String) -> Void) {
    let day0: Double = 1_700_000_000, now = day0 + 10 * 3600
    let end = day0 + 86400, tomorrowEnd = day0 + 2 * 86400
    func urgency(_ entry: BriefPlate.Entry) -> BriefPlate.Urgency {
        BriefPlate.urgency(entry, now: now, dayStart: day0, todayEnd: end, tomorrowEnd: tomorrowEnd)
    }
    check(urgency(.init(key: "a", title: "A", due: now - 60)) == .overdue, "past its time: overdue")
    check(urgency(.init(key: "a", title: "A", due: day0, allDay: true)) == .today, "all-day today is today, not overdue")
    check(urgency(.init(key: "a", title: "A", due: day0 - 86400, allDay: true)) == .overdue, "all-day yesterday is overdue")
    check(urgency(.init(key: "a", title: "A", due: now + 3600, overdue: true)) == .overdue, "the service's overdue wins")
    check(urgency(.init(key: "a", title: "A", due: end + 60)) == .tomorrow, "tomorrow")
    check(urgency(.init(key: "a", title: "A", due: tomorrowEnd + 60)) == .later, "later")
    check(urgency(.init(key: "a", title: "A", due: nil)) == .undated, "undated")
    check(urgency(.init(key: "a", title: "A", due: now - 60, done: true)) == .done, "done goes last")
    let ordered = BriefPlate.order([
        .init(key: "undated", title: "Zed", due: nil),
        .init(key: "b-today", title: "Beta", due: now + 7200),
        .init(key: "a-today", title: "Alpha", due: now + 7200),
        .init(key: "early", title: "Early", due: now + 600),
        .init(key: "late", title: "Late", due: now - 3600),
        .init(key: "done", title: "Done", due: now - 7200, done: true),
        .init(key: "tom", title: "Tom", due: end + 60),
    ], now: now, dayStart: day0, todayEnd: end, tomorrowEnd: tomorrowEnd)
    check(ordered.map(\.key) == ["late", "early", "a-today", "b-today", "tom", "undated", "done"],
          "overdue, today by time then title, tomorrow, undated, done: \(ordered.map(\.key))")
    check(BriefPlate.moreParts(hiddenTodos: 3, hiddenSchool: 5) == ["+3 to-dos", "+5 in Study"], "both parts")
    check(BriefPlate.moreParts(hiddenTodos: 1, hiddenSchool: 0) == ["+1 to-do"], "one to-do, no Study part")
    check(BriefPlate.moreParts(hiddenTodos: 0, hiddenSchool: 0).isEmpty, "nothing hidden, no line")
    check(!BriefPlate.sideBySide(width: 596, textScale: 1), "the 640 pt panel (596 pt of content) stacks the zones")
    check(BriefPlate.sideBySide(width: 796, textScale: 1), "the 840 pt wide panel puts them side by side")
    check(BriefPlate.sideBySide(width: 796, textScale: 1.3) && !BriefPlate.sideBySide(width: 700, textScale: 1.3),
          "larger text needs more room before the zones sit side by side")
    check(BriefPlate.shown(sideBySide: true) == 5 && BriefPlate.shown(sideBySide: false) == 2,
          "five rows side by side, two stacked, before Show all")
    check(BriefPlate.heading("NEEDS YOU", count: 4) == "NEEDS YOU · 4" && BriefPlate.heading("NEEDS YOU", count: 0) == "NEEDS YOU",
          "a zone heading counts only when there is something")
    briefWordingChecks(check)
}

/// B1 to B4: who a row is from, a quote that repeats its title, the calendar
/// line, and a course code in brackets.
func briefWordingChecks(_ check: (Bool, String) -> Void) {
    // B1: an unnamed group lost its "Group" word on the way; a group with no name left "in" bare.
    check(BriefPlate.who("Sam Rivera in of 3 (Dana, Lee, Sam Rivera)") == "Sam Rivera in a group of 3 (Dana, Lee, Sam Rivera)",
          "an unnamed group reads as a group of 3: \(BriefPlate.who("Sam Rivera in of 3 (Dana, Lee, Sam Rivera)"))")
    check(BriefPlate.who("of 4 (Dana, Lee, Sam, Jo)") == "A group of 4 (Dana, Lee, Sam, Jo)", "the group alone")
    check(BriefPlate.who("Dana Okafor in ") == "Dana Okafor" && BriefPlate.who("Dana Okafor in") == "Dana Okafor",
          "no group name: the in clause goes, not a hole")
    check(BriefPlate.who("Dana Okafor in Study Buddies") == "Dana Okafor in Study Buddies", "a named group stays")
    check(BriefPlate.who("Dana Okafor in Group \"Study Buddies\"") == "Dana Okafor in Study Buddies", "a leftover Group word goes")
    check(BriefPlate.who("Sam Rivera") == "Sam Rivera" && BriefPlate.who("") == "", "a person stays as sent")
    check(BriefPlate.who("Office of 3 (Main)") == "Office of 3 (Main)", "a name with \"of\" in it is left alone")
    // B3: the quote under a to-do only when it says more than the title.
    check(BriefPlate.quoteRepeats("finish the CS 101 lab writeup!", title: "Finish the CS 101 lab writeup"),
          "the same words, case and punctuation aside")
    check(BriefPlate.quoteRepeats("\"Midterm review\"", title: "Midterm review"), "quote marks aside")
    check(BriefPlate.quoteRepeats("midterm review", title: "Midterm review, QNT 210"), "the title holds the quote")
    check(BriefPlate.quoteRepeats("do the midterm review", title: "Midterm review"), "two words more")
    check(!BriefPlate.quoteRepeats("can you send the midterm review slides by Friday?", title: "Midterm review"),
          "a quote that says more stays")
    check(!BriefPlate.quoteRepeats("let's talk tmrw", title: "Call Jo"), "a different quote stays")
    check(BriefPlate.quoteRepeats("", title: "Call Jo"), "no quote, nothing to show")
    // B2: the calendar line reads the same day the cards do.
    check(BriefPlate.calendarLine(upcoming: [], allDay: [], earlierToday: 0, calendarUnread: false) == "Nothing on your calendar today",
          "nothing on today at all")
    check(BriefPlate.calendarLine(upcoming: [], allDay: [], earlierToday: 2, calendarUnread: false) == "Nothing left on your calendar today",
          "everything today has ended")
    check(BriefPlate.calendarLine(upcoming: [], allDay: ["Career fair"], earlierToday: 0, calendarUnread: false) == "All day: Career fair",
          "an all-day event is named, not \"nothing\"")
    check(BriefPlate.calendarLine(upcoming: ["9 AM Lab", "1 PM Lecture", "3 PM Club"], allDay: ["Career fair"],
                                  earlierToday: 0, calendarUnread: false) == "All day: Career fair · 9 AM Lab · 1 PM Lecture",
          "all-day first, three at most")
    check(BriefPlate.calendarLine(upcoming: [], allDay: [], earlierToday: 0, calendarUnread: true) == "Wisp can't see your calendar yet",
          "an unread calendar is never an all-clear")
    for line in [BriefPlate.calendarLine(upcoming: [], allDay: [], earlierToday: 0, calendarUnread: false),
                 BriefPlate.calendarLine(upcoming: [], allDay: [], earlierToday: 3, calendarUnread: false)] {
        check(!line.contains("else"), "never \"else\" with nothing above it: \(line)")
    }
    // B4: a course code in brackets after a to-do's title goes where the course shows.
    check(BriefPlate.splitCourse("Chapter 4 reading [CS 101]") == ("Chapter 4 reading", "CS 101"), "square brackets")
    check(BriefPlate.splitCourse("Readings (QNT 210)") == ("Readings", "QNT 210"), "round brackets")
    check(BriefPlate.splitCourse("Lab writeup [CS101]") == ("Lab writeup", "CS101"), "a code in one word")
    check(BriefPlate.splitCourse("Dinner (with Sam)") == ("Dinner (with Sam)", ""), "an ordinary aside stays")
    check(BriefPlate.splitCourse("Call back (2 PM)") == ("Call back (2 PM)", ""), "a time stays")
    check(BriefPlate.splitCourse("[CS 101]") == ("[CS 101]", ""), "a code alone stays the title")
    // Option 2: READY FOR YOU's tags say what Wisp did, from what the item really is.
    check(BriefReady.cardTag(method: "email_reply", hasDraft: true) == "DRAFTED"
          && BriefReady.cardTag(method: "text_reply", hasDraft: false) == "DRAFTED", "a reply Wisp wrote is DRAFTED")
    check(BriefReady.cardTag(method: "block_time", hasDraft: false) == "FREE TIME", "a free hour it found")
    check(BriefReady.cardTag(method: "undo_add", hasDraft: false) == "ADDED" && BriefReady.cardTag(method: "group", hasDraft: false) == "ADDED",
          "what it added by itself, alone or grouped")
    check(BriefReady.cardTag(method: "reminder", hasDraft: false) == "SPOTTED"
          && BriefReady.cardTag(method: "calendar_event", hasDraft: false) == "SPOTTED"
          && BriefReady.cardTag(method: "", hasDraft: false) == "SPOTTED", "anything else it spotted")
    check(BriefReady.cardTag(method: "read", hasDraft: true) == "DRAFTED", "a card that carries a draft")
    check(BriefReady.studyTag(id: "ready:qa:quiz3", opensMaterial: true) == "QUIZ MADE", "Study made a quiz")
    check(BriefReady.studyTag(id: "ready:qa:pa3", opensMaterial: false) == "SLIDES", "Study found the slides")
    check(BriefReady.studyTag(id: "weak:qa:pointers", opensMaterial: false) == "WEAK SPOT", "a topic to look at again")
    dueSoonChecks(check)
    check(BriefReady.foundTag == "FOUND", "found in mail")
    check(BriefReady.rowsShown(sideBySide: true) == 3 && BriefReady.rowsShown(sideBySide: false) == 3, "rows before Show all")
    check(BriefReady.source(read: 49, at: "10:24 AM") == "from 49 emails and texts · 10:24 AM", "what it read and when")
    check(BriefReady.source(read: 1, at: "") == "from 1 email and texts" && BriefReady.source(read: 0, at: "9 AM") == nil,
          "one email; nothing read, no line")
    check(BriefReady.sourceShort(read: 49, at: "10:24 AM") == "49 read · 10:24 AM"
          && BriefReady.sourceShort(read: 3, at: "") == "3 read" && BriefReady.sourceShort(read: 0, at: "9 AM") == nil,
          "the short read line keeps the time")
    check(BriefReady.sourceForms(read: 49, at: "10:24 AM", sideBySide: true)
          == ["from 49 emails and texts · 10:24 AM", "49 read · 10:24 AM"], "side by side: the sentence, then the short form")
    check(BriefReady.sourceForms(read: 49, at: "10:24 AM", sideBySide: false) == ["from 49 emails and texts · 10:24 AM"],
          "stacked: the sentence only")
    check(BriefReady.sourceForms(read: 0, at: "10:24 AM", sideBySide: true).isEmpty, "nothing read: no line")
    check(BriefReady.tagColumn(["FOUND", "QUIZ MADE"], charWidth: 6) == 66 && BriefReady.tagColumn(["FOUND"], charWidth: 5.5) == 40,
          "the tag column fits the widest tag shown")
    check(BriefReady.tagColumn([], charWidth: 6) == 12, "no tags: just the padding")
    check(BriefReady.emptyLine(read: 49, at: "10:24 AM", actingToday: 1)
          == "Nothing needs you yet. Wisp checked 49 emails and texts at 10:24 AM · Acting for you · 1 today",
          "the one line with nothing ready, acting on")
    check(BriefReady.emptyLine(read: 3, at: "", actingToday: nil) == "Nothing needs you yet. Wisp checked 3 emails and texts",
          "acting off or unknown: not mentioned")
    check(BriefReady.draftQuote("Hi Dana,\n\nYes, Tuesday works.\n\nBest,\nAlex") == "“Hi Dana, Yes, Tuesday works. Best, Alex”",
          "a draft as one quoted line")
    check(BriefReady.draftQuote(" \n ") == nil, "no draft, no quote")
    // YOUR LISTS' headings and short lists.
    check(BriefLists.schoolHeading(dueToday: 1, total: 4) == "SCHOOL · 1 DUE TODAY", "school due today")
    check(BriefLists.schoolHeading(dueToday: 0, total: 4) == "SCHOOL · 4", "nothing due today: how many")
    check(BriefLists.attentionHeading(replies: 2, total: 6) == "NEEDS ATTENTION · 2 REPLIES"
          && BriefLists.attentionHeading(replies: 1, total: 6) == "NEEDS ATTENTION · 1 REPLY", "replies owed")
    check(BriefLists.attentionHeading(replies: 0, total: 3) == "NEEDS ATTENTION · 3", "no reply owed: how many")
    let day0: Double = 1_700_000_000
    check(BriefLists.dueToday([day0 + 3600, day0 + 90_000, nil, .infinity], dayStart: day0, dayEnd: day0 + 86_400) == 1,
          "only today's due times count")
    check(BriefLists.dueWord("Tomorrow") == "Tmrw" && BriefLists.dueWord("Tomorrow 5 PM") == "Tmrw 5 PM"
          && BriefLists.dueWord(" 5:30 PM ") == "5:30 PM" && BriefLists.dueWord("Overdue · 1 day") == "Overdue · 1 day",
          "the due word, short")
    check(BriefLists.todosShown(sideBySide: true) == 3 && BriefLists.todosShown(sideBySide: false) == 2
          && BriefLists.schoolShown(sideBySide: true) == 3 && BriefLists.threadsShown == 4, "rows before All n")
}


/// READY FOR YOU's due rows and the school link rule (StudyLinkRule, mirroring
/// service/study/plan.py `link`). Invented addresses only.
func dueSoonChecks(_ check: (Bool, String) -> Void) {
    check(BriefReady.dueTag(day: "today") == "DUE TODAY" && BriefReady.dueTag(day: "tomorrow") == "DUE TMRW"
          && BriefReady.dueTag(day: "friday") == nil && BriefReady.dueTag(day: "") == nil, "due tags: today, tomorrow, nothing else")
    check(BriefReady.tagColumn(["DUE TODAY", "DUE TMRW", "SLIDES"], charWidth: 6) == BriefReady.tagColumn(["QUIZ MADE"], charWidth: 6),
          "DUE TODAY is no wider than QUIZ MADE")
    // The four cases, and the kinds.
    check(BriefReady.dueMaterial(id: "m1", kind: "guide", scope: "assignment", canMake: true) == .open(id: "m1", label: "Open guide"),
          "url + assignment guide: Open guide")
    check(BriefReady.dueMaterial(id: "m2", kind: "guide", scope: "course", canMake: true) == .courseGuide(id: "m2"),
          "url + course guide: Course guide")
    check(BriefReady.dueMaterial(id: nil, kind: "", scope: "", canMake: true) == .makeGuide, "no guide: Make a guide")
    check(BriefReady.dueMaterial(id: nil, kind: "", scope: "", canMake: false) == .none, "no guide, Study can't make one: no button")
    check(BriefReady.dueMaterial(id: "m3", kind: "lesson", scope: "assignment", canMake: false) == .open(id: "m3", label: "Open lesson")
          && BriefReady.dueMaterial(id: "m4", kind: "flashcards", scope: "assignment", canMake: false) == .open(id: "m4", label: "Open cards")
          && BriefReady.dueMaterial(id: "m5", kind: "quiz", scope: "assignment", canMake: false) == .open(id: "m5", label: "Open quiz"),
          "lesson, cards, quiz")
    check(BriefReady.dueMaterial(id: "m6", kind: "flashcards", scope: "course", canMake: true) == .makeGuide
          && BriefReady.dueMaterial(id: "", kind: "guide", scope: "assignment", canMake: true) == .makeGuide
          && BriefReady.dueMaterial(id: "m7", kind: "poster", scope: "assignment", canMake: false) == .none,
          "a course's cards, an empty id or an unknown kind open nothing")
    check(BriefReady.dueLink(urlKind: "assignment")?.label == "Open ↗"
          && BriefReady.dueLink(urlKind: "quiz_page")?.label == "Open quiz page ↗"
          && BriefReady.dueLink(urlKind: "quiz_page")?.help == "Opens the quiz's page. It does not start it."
          && BriefReady.dueLink(urlKind: nil) == nil && BriefReady.dueLink(urlKind: "attempt") == nil, "link labels by kind")

    // The rule, table-driven: (address, kind or nil when refused).
    let table: [(String, String?)] = [
        ("https://canvas.example.edu/courses/12/assignments/34", "assignment"),
        ("https://canvas.example.edu/courses/12/assignments/34/", "assignment"),
        ("https://CANVAS.Example.edu/courses/12/assignments/34", "assignment"),
        ("https://canvas.example.edu/courses/12/assignments/34?module_item_id=56", "assignment"),
        ("https://canvas.example.edu/courses/12/quizzes/78", "quiz_page"),
        ("https://canvas.example.edu/courses/12/quizzes/78/", "quiz_page"),
        ("https://canvas.example.edu/courses/12/quizzes/78/take", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78/Take/", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78/take?user_id=1", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78?preview=1", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78/history?quiz_submission_id=9", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78/attempts/2", nil),
        ("https://canvas.example.edu/courses/12/quizzes/78/start", nil),
        ("https://canvas.example.edu/courses/12/assignments/34/submissions/new", nil),
        ("https://canvas.example.edu/courses/12/gradebook/speed_grader?assignment_id=34", nil),
        ("https://canvas.example.edu/courses/12/external_tools/90", nil),
        ("https://canvas.example.edu/courses/12/assignments/34?display=lti_launch", nil),
        ("https://canvas.example.edu/login/oauth2/auth", nil),
        ("https://canvas.example.edu/logout", nil),
        ("https://canvas.example.edu/courses/12/assignments/34?token=abc", nil),
        ("https://canvas.example.edu/courses/12/assignments/34#comments", nil),
        ("https://canvas.example.edu/courses/12/%2574ake", nil),
        ("https://canvas.example.edu:8443/courses/12/assignments/34", nil),
        ("https://student:pw@canvas.example.edu/courses/12/assignments/34", nil),
        ("https://student@canvas.example.edu/courses/12/assignments/34", nil),
        ("http://canvas.example.edu/courses/12/assignments/34", nil),
        ("HTTPS://canvas.example.edu/courses/12/assignments/34", nil),
        ("javascript:alert(1)", nil),
        ("https://canvas.xn--exmple-cua.edu/courses/12/assignments/34", nil),
        ("https://canvas.example.edu/courses/12/assignments/34 x", nil),
        ("https://canvas.example.edu/courses/12/assignments/34\tx", nil),
        ("https://canvas.example.edu/courses/12/assignments/caf\u{e9}", nil),
        ("https://canvas%2eexample.edu/courses/12/assignments/34", nil),
        ("https://canvas.example.edu\\@evil.example.com/courses/12", nil),
        ("", nil),
    ]
    for (raw, kind) in table {
        let got: String? = StudyLinkRule.check(raw)?.kind
        check(got == kind, "link rule: \(raw) gives \(got ?? "refused"), wanted \(kind ?? "refused")")
    }

    // Decoding: a refused or unknown-kind url is dropped; a row for another day is dropped.
    let json = #"""
    {"due_soon": [
      {"id": "a1", "title": "Homework 5", "day": "today", "url": "https://canvas.example.edu/courses/105/assignments/5005",
       "url_kind": "assignment", "material": {"id": "m9", "kind": "guide", "scope": "assignment", "title": "HW5 guide"}},
      {"id": "a2", "title": "Quiz 2", "day": "tomorrow", "url": "https://canvas.example.edu/courses/105/quizzes/22/take",
       "url_kind": "quiz_page", "material": {"id": "m8", "kind": "guide", "scope": "elsewhere"}},
      {"id": "a3", "title": "Lab 1", "day": "today", "url": "https://canvas.example.edu/courses/105/assignments/11",
       "url_kind": "launch_page", "material": null},
      {"id": "a4", "title": "Old", "day": "monday"}
    ], "due_soon_more": 2}
    """#
    let summary = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(json.utf8))
    let soon = summary?.dueSoon ?? []
    check(soon.map(\.id) == ["a1", "a2", "a3"] && summary?.dueSoonMore == 2, "due_soon decodes, another day dropped")
    check(soon.first?.url == "https://canvas.example.edu/courses/105/assignments/5005" && soon.first?.urlKind == "assignment"
          && soon.first?.material?.id == "m9", "a good link and its guide")
    check(soon.count > 2 && soon[1].url == nil && soon[1].urlKind == nil && soon[1].material == nil,
          "an attempt url and an unknown scope are dropped on the client")
    check(soon.count > 2 && soon[2].url == nil, "an unknown url kind gives no link")
    let old = try? JSONDecoder().decode(StudyDaySummary.self, from: Data(#"{"line": "x"}"#.utf8))
    check(old?.dueSoon == [] && old?.dueSoonMore == 0, "an older service: no due rows")
}
