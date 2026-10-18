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
    let mail = BriefDetail.mail(needing: ["Can you review my draft?", "Tech Week starts today", "50% off everything"],
                                summary: ["Worth a look", "Advisor: Thesis meeting moved", "Tech Week: starts today"], ready: true)
    check(mail.lines.map(\.text) == ["Can you review my draft?", "Worth a look", "Advisor: Thesis meeting moved",
                                     "2 newsletters and promos left out"] && mail.source == .mail,
          "mail details: real mail, the summary's mail, and how much bulk was left out: \(mail.lines.map(\.text))")
    check(BriefDetail.mail(needing: [], summary: [], ready: false).lines.map(\.text) == ["Mail hasn't been checked yet"],
          "unread mail details")
    let texts = BriefDetail.texts(needing: ["Priya asked about dinner", "nothing"], summary: ["👤 Priya", "Asked about dinner at 7."],
                                  ready: true)
    check(texts.lines.map(\.text) == ["👤 Priya", "Asked about dinner at 7.", "Priya asked about dinner"] && texts.source == .messages,
          "texts details: the summary's messages, then texts needing you")
    check(BriefDetail.texts(needing: [], summary: [], ready: true).lines.map(\.text) == ["No texts need you"], "no texts")
    let week = BriefDetail.soon(soon, when: { $0.allDay ? "Sun" : "later" })
    check(week.lines.map(\.text) == ["Essay outline", "Holiday", "UCSC startup club"] && week.source == .reminders,
          "SOON details open the first item's app")
    check(BriefDetail.soon(nil, when: { _ in "" }).lines.first?.muted == true
          && BriefDetail.soon([], when: { _ in "" }).lines.map(\.text) == ["Nothing in the next week"], "SOON details when unknown or empty")
}
