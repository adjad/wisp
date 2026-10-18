import Foundation

/// The composed Daily Summary (service/assistant/brief.py `_render_brief`) cut
/// into its sections, so the notch's Brief can show a short version: what Wisp
/// is keeping in mind, without the mail and messages write-ups, which belong to
/// the MAIL and TEXTS rows' details. Reads the text the service already sent;
/// never a model call.
///
/// The brief is a greeting line, then bold or `#` section headers ("**🧭 Keeping
/// in mind**", "**📅 Today**", "**✅ Reminders due today**", "**📧 Inbox — …**",
/// "**💬 Messages**"), then a closing offer. A header that isn't one of those
/// ("**Worth a look**", a conversation's name) belongs to the section above it.
struct ComposedBrief: Equatable {
    enum Kind: Equatable { case keeping, calendar, reminders, mail, messages }

    struct Section: Equatable {
        let kind: Kind
        /// Plain lines: no markdown markers, bullets or blank lines. Sub-headers
        /// inside a section are kept as their own line.
        var lines: [String]
    }

    let sections: [Section]

    func lines(_ kind: Kind) -> [String] {
        sections.filter { $0.kind == kind }.flatMap(\.lines)
    }

    /// The short summary under the Brief's lede: what Wisp is keeping in mind.
    /// Calendar shows as a clean event list instead, from the day's plan, and an
    /// unknown header stays inside the section above it, so mail and messages
    /// content can never reach the summary.
    var cutDown: [String] {
        Array(lines(.keeping).prefix(4))
    }

    static func parse(_ text: String) -> ComposedBrief {
        var sections: [Section] = []
        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            if isClosing(line) { break }
            if let header = header(line) {
                if let kind = kind(header) {
                    sections.append(Section(kind: kind, lines: []))
                    continue
                }
                // A sub-header ("Worth a look", a conversation) stays in its section.
                guard !sections.isEmpty else { continue }
                sections[sections.count - 1].lines.append(header)
                continue
            }
            // The greeting and anything else before the first header is dropped.
            guard !sections.isEmpty else { continue }
            let plain = plainLine(line)
            if !plain.isEmpty { sections[sections.count - 1].lines.append(plain) }
        }
        return ComposedBrief(sections: sections.filter { !$0.lines.isEmpty })
    }

    /// The header's text when a line is a bold-only or `#` header.
    static func header(_ line: String) -> String? {
        var text: Substring
        if line.hasPrefix("**"), line.hasSuffix("**"), line.count > 4 {
            text = line.dropFirst(2).dropLast(2)
            if text.contains("**") { return nil }
        } else if line.hasPrefix("#") {
            text = line.drop(while: { $0 == "#" })
            guard text.first == " " else { return nil }
        } else {
            return nil
        }
        let cleaned = text.trimmingCharacters(in: .whitespaces)
        return cleaned.isEmpty ? nil : cleaned
    }

    /// Which top-level section a header opens, or nil for a sub-header.
    static func kind(_ header: String) -> Kind? {
        let name = String(header.unicodeScalars.drop { !CharacterSet.alphanumerics.contains($0) })
            .lowercased()
        if name.hasPrefix("keeping in mind") { return .keeping }
        if name.hasPrefix("today") { return .calendar }
        if name.hasPrefix("reminders") { return .reminders }
        if name.hasPrefix("inbox") || name.hasPrefix("email") || name.hasPrefix("mail") { return .mail }
        if name.hasPrefix("messages") { return .messages }
        return nil
    }

    static func isClosing(_ line: String) -> Bool {
        line.hasPrefix("Let me know if you'd like me to dig") || line.hasPrefix("Let me know if you\u{2019}d like me to dig")
    }

    /// One line without markdown: bullets, bold markers and leading `#`s go.
    static func plainLine(_ line: String) -> String {
        var text = Substring(line)
        for bullet in ["- ", "• ", "* ", "– "] where text.hasPrefix(bullet) {
            text = text.dropFirst(bullet.count)
            break
        }
        return text.replacingOccurrences(of: "**", with: "").trimmingCharacters(in: .whitespaces)
    }
}

/// One upcoming item from `/assistant/upcoming`: a calendar event or a reminder
/// in the next week.
struct UpcomingItem: Equatable, Identifiable {
    let id: String
    let title: String
    let when: Double
    let allDay: Bool
    /// "calendar", "reminders", "manual" or "mail".
    let source: String
    let kind: String

    var isReminder: Bool { source == "reminders" || kind == "reminder" || kind == "assignment" }

    static func parse(_ obj: [String: Any]) -> UpcomingItem? {
        guard let id = obj["id"] as? String, let title = obj["title"] as? String,
              let when = (obj["when_ts"] as? NSNumber)?.doubleValue, when.isFinite else { return nil }
        let allDay = (obj["all_day"] as? NSNumber)?.boolValue ?? false
        return UpcomingItem(id: id, title: title, when: when, allDay: allDay,
                            source: obj["source"] as? String ?? "", kind: obj["kind"] as? String ?? "event")
    }

    static func parseList(_ obj: [String: Any]?) -> [UpcomingItem]? {
        guard let rows = obj?["commitments"] as? [[String: Any]] else { return nil }
        return rows.compactMap(parse)
    }

    /// What SOON lists: after `after`, within a week, without placeholders, and
    /// without the rest of today's calendar, which the CAL row already covers.
    static func soon(_ items: [UpcomingItem], after: Double, dayEnd: Double, limit: Int = 5) -> [UpcomingItem] {
        items.filter { item in
            item.when > after && item.when <= after + 7 * 86400 && !DayTimeline.isJunk(item.title)
                && (item.isReminder || item.when >= dayEnd)
        }
        .sorted { $0.when < $1.when }
        .prefix(limit).map { $0 }
    }
}

/// What opens under a Brief row: a few lines, then a link to the source app.
struct BriefDetail: Equatable {
    struct Line: Equatable, Identifiable {
        let id: String
        /// A time or day, or empty.
        let leading: String
        let text: String
        var muted = false
    }
    let lines: [Line]
    let source: DayTimeline.BriefSource

    static func calendar(allDay: [String], events: [(title: String, start: Double, end: Double?)],
                         unsynced: Bool, clock: (Double) -> String) -> BriefDetail {
        var lines: [Line] = allDay.filter { !DayTimeline.isJunk($0) }.enumerated().map {
            Line(id: "allday-\($0.offset)", leading: "All day", text: $0.element)
        }
        lines += events.filter { !DayTimeline.isJunk($0.title) }.enumerated().map { index, event in
            let span = event.end.map { "\(clock(event.start))–\(clock($0))" } ?? clock(event.start)
            return Line(id: "event-\(index)", leading: span, text: event.title)
        }
        if lines.isEmpty {
            lines = [Line(id: "empty", leading: "", text: unsynced ? "Calendar hasn't synced yet" : "Nothing on your calendar today",
                          muted: true)]
        }
        return BriefDetail(lines: lines, source: .calendar)
    }

    /// Each email that needs you, described ("Prof. Lee wants the thesis draft
    /// by Tuesday"), people first, then how much bulk was left out.
    static func mail(needing: [BriefNeed], ready: Bool) -> BriefDetail {
        guard ready else {
            return BriefDetail(lines: [Line(id: "empty", leading: "", text: "Mail hasn't been checked yet", muted: true)], source: .mail)
        }
        let real = BriefMail.important(needing)
        let bulk = BriefMail.bulkCount(needing)
        var lines = real.prefix(5).enumerated().map { Line(id: "need-\($0.offset)", leading: "", text: BriefMail.describe($0.element)) }
        if real.count > 5 { lines.append(Line(id: "more", leading: "", text: "+\(real.count - 5) more", muted: true)) }
        if bulk > 0 {
            lines.append(Line(id: "bulk", leading: "",
                              text: bulk == 1 ? "1 newsletter or promo left out" : "\(bulk) newsletters and promos left out",
                              muted: true))
        }
        if lines.isEmpty { lines = [Line(id: "empty", leading: "", text: "Nothing needs a reply", muted: true)] }
        return BriefDetail(lines: lines, source: .mail)
    }

    /// Who is waiting on you, with how many texts and when the latest came.
    /// Never the texts themselves.
    static func texts(needing: [BriefNeed], ready: Bool, when: (Double) -> String) -> BriefDetail {
        guard ready else {
            return BriefDetail(lines: [Line(id: "empty", leading: "", text: "Messages haven't been checked yet", muted: true)],
                               source: .messages)
        }
        var lines = BriefTexts.waiting(needing).prefix(6).map { person in
            Line(id: "person-" + person.name, leading: person.latest.map(when) ?? "",
                 text: person.name + " · \(person.count) text\(person.count == 1 ? "" : "s")")
        }
        if lines.isEmpty { lines = [Line(id: "empty", leading: "", text: "No texts need you", muted: true)] }
        return BriefDetail(lines: lines, source: .messages)
    }

    static func soon(_ items: [UpcomingItem]?, when: (UpcomingItem) -> String) -> BriefDetail {
        guard let items else {
            return BriefDetail(lines: [Line(id: "empty", leading: "", text: "The week ahead hasn't loaded yet", muted: true)],
                               source: .reminders)
        }
        let lines = items.isEmpty ? [Line(id: "empty", leading: "", text: "Nothing in the next week", muted: true)]
            : items.map { Line(id: $0.id, leading: when($0), text: $0.title) }
        let source = items.first.map { DayTimeline.briefSource($0.isReminder ? "reminders" : $0.source) } ?? .reminders
        return BriefDetail(lines: lines, source: source)
    }
}

/// One email or text the Brief describes, from a workspace attention item
/// (service/assistant/daily_workspace.py).
struct BriefNeed: Equatable {
    /// An email's subject, or the text itself. A text is never shown.
    let title: String
    /// Who it's from: a name, or "Priya in Family" for a group text. Empty from
    /// an older service.
    var sender = ""
    /// A one-line, model-written description of an email
    /// (service/assistant/mail_gists.py), or empty until one is written.
    var gist = ""
    /// The sender looks like a person, not an automated sender.
    var person = false
    var received: Double? = nil
    /// You have texted that conversation since.
    var replied = false

    var who: String { sender.trimmingCharacters(in: .whitespaces) }
}

enum BriefMail {
    /// Mail worth describing: no placeholders, newsletters or promos; people
    /// first, then newest, the order the gists are written in.
    static func important(_ needs: [BriefNeed]) -> [BriefNeed] {
        needs.filter { !DayTimeline.isJunk($0.title) && !DayTimeline.isBulkMail($0.title) }
            .enumerated()
            .sorted { a, b in
                if a.element.person != b.element.person { return a.element.person }
                let x = a.element.received ?? -.infinity, y = b.element.received ?? -.infinity
                return x != y ? x > y : a.offset < b.offset
            }
            .map(\.element)
    }

    static func bulkCount(_ needs: [BriefNeed]) -> Int {
        needs.filter { !DayTimeline.isJunk($0.title) && DayTimeline.isBulkMail($0.title) }.count
    }

    /// The gist when one was written, else "Sender: subject", else the subject.
    static func describe(_ need: BriefNeed) -> String {
        let gist = need.gist.trimmingCharacters(in: .whitespacesAndNewlines)
        if !gist.isEmpty { return gist }
        return need.who.isEmpty ? need.title : need.who + ": " + need.title
    }
}

enum BriefTexts {
    struct Person: Equatable {
        let name: String
        let count: Int
        let latest: Double?
    }

    /// Who sent a text: the service's sender, else the text's "Name: …" prefix
    /// (an older service sent only the text).
    static func name(_ need: BriefNeed) -> String {
        if !need.who.isEmpty { return need.who }
        let title = need.title
        if let range = title.range(of: ": "), title.distance(from: title.startIndex, to: range.lowerBound) <= 40 {
            return String(title[..<range.lowerBound])
        }
        return "Someone"
    }

    /// One entry per person still waiting, the most recent first. A
    /// conversation you've texted since is not waiting.
    static func waiting(_ needs: [BriefNeed]) -> [Person] {
        var order: [String] = []
        var people: [String: Person] = [:]
        let open = needs.filter { !$0.replied && !DayTimeline.isJunk($0.title) && name($0) != "Me" }
            .sorted { ($0.received ?? -.infinity) > ($1.received ?? -.infinity) }
        for need in open {
            let name = name(need)
            if let known = people[name] {
                people[name] = Person(name: name, count: known.count + 1, latest: known.latest ?? need.received)
            } else {
                order.append(name)
                people[name] = Person(name: name, count: 1, latest: need.received)
            }
        }
        return order.compactMap { people[$0] }
    }

    /// "Trishe is waiting on you", "Trishe and Mom are waiting on you",
    /// "Trishe, Mom and 2 others are waiting on you".
    static func row(_ people: [Person]) -> String {
        let names = people.map(\.name)
        switch names.count {
        case 0: return "No texts need you"
        case 1: return "\(names[0]) is waiting on you"
        case 2: return "\(names[0]) and \(names[1]) are waiting on you"
        case 3: return "\(names[0]), \(names[1]) and \(names[2]) are waiting on you"
        default: return "\(names[0]), \(names[1]) and \(names.count - 2) others are waiting on you"
        }
    }
}
