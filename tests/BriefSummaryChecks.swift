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
    check(brief.cutDown == ["PA 2 is due tonight, and you said you want to finish CSE 20 strong.", "Priya is in today's messages."],
          "the cut-down summary has no mail, messages or calendar prose: \(brief.cutDown)")
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
}
