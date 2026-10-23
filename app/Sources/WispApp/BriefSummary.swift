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
        /// A paragraph, shown whole rather than cut to two lines.
        var paragraph = false
    }
    let lines: [Line]
    let source: DayTimeline.BriefSource
    /// A link in place of "Open <app>", for something the user must grant first.
    struct Action: Equatable {
        let label: String
        let url: String
    }
    var action: Action? = nil

    /// System Settings > Privacy & Security > Full Disk Access.
    static let fullDiskAccess = Action(label: "Open Full Disk Access settings ↗",
                                       url: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles")

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

    /// Said in place of the summary's section when today has none yet.
    static let noSummaryYet = "Today's summary arrives with your morning brief · or press Refresh"

    /// The mail section of the composed Daily Summary, then the emails that
    /// need you as "Sender · subject" (repeats collapsed ×N, people first, none
    /// the summary already names), then what was left out. The row above says
    /// the gist and the count, so neither repeats here. `summary` is nil when
    /// today has no composed summary: a muted line says so first, instead of
    /// the section silently missing.
    ///
    /// `prose` is the service's written inbox summary (mail_gists.py). When it
    /// is there it replaces the summary's section, which lists senders and
    /// subjects: the user asked for "an actual email summary not email headers".
    static func mail(needing: [BriefNeed], ready: Bool, summary: [String]? = [], prose: String? = nil) -> BriefDetail {
        guard ready else {
            return BriefDetail(lines: [Line(id: "empty", leading: "", text: "Mail hasn't been checked yet", muted: true)], source: .mail)
        }
        let written = prose?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let section = written.isEmpty ? summary ?? [] : [written]
        var lines = written.isEmpty
            ? section.prefix(8).enumerated().map { Line(id: "summary-\($0.offset)", leading: "", text: $0.element) }
            : [Line(id: "prose", leading: "", text: written, paragraph: true)]
        let said = BriefMail.words(section.joined(separator: " "))
        let shown = BriefMail.shown(needing).filter { need in
            let words = BriefMail.words(need.subject)
            return said.isEmpty || words.isEmpty || !said.contains(words)
        }
        if !shown.isEmpty, !lines.isEmpty { lines.append(Line(id: "needs", leading: "", text: "Needs you", muted: true)) }
        lines += shown.prefix(5).enumerated().map { Line(id: "need-\($0.offset)", leading: $0.element.tag, text: $0.element.line) }
        if shown.count > 5 { lines.append(Line(id: "more", leading: "", text: "+\(shown.count - 5) more", muted: true)) }
        let hidden = BriefMail.privateCount(needing)
        let bulk = BriefMail.bulkCount(needing)
        if hidden > 0 {
            lines.append(Line(id: "private", leading: "",
                              text: hidden == 1 ? "1 private email not shown" : "\(hidden) private emails not shown", muted: true))
        }
        if bulk > 0 {
            lines.append(Line(id: "bulk", leading: "",
                              text: bulk == 1 ? "1 newsletter or promo left out" : "\(bulk) newsletters and promos left out",
                              muted: true))
        }
        if lines.isEmpty { lines = [Line(id: "empty", leading: "", text: "Nothing needs a reply", muted: true)] }
        return BriefDetail(lines: noted(lines, missing: summary == nil && written.isEmpty), source: .mail)
    }

    /// The messages section of the composed Daily Summary without its quotes
    /// and without the people listed under it, then who is waiting on you and
    /// for how long. Never the texts themselves. `summary` is nil when today
    /// has no composed summary, as for mail.
    static func texts(needing: [BriefNeed], ready: Bool, needsAccess: Bool = false, summary: [String]? = [],
                      age: (Double) -> String) -> BriefDetail {
        if needsAccess {
            return BriefDetail(lines: [Line(id: "access", leading: "",
                                            text: "Wisp can't read your texts until it has Full Disk Access.", muted: true)],
                               source: .messages, action: fullDiskAccess)
        }
        guard ready else {
            return BriefDetail(lines: [Line(id: "empty", leading: "", text: "Messages haven't been checked yet", muted: true)],
                               source: .messages)
        }
        let waiting = BriefTexts.waiting(needing)
        let names = Set(waiting.map { $0.name.lowercased() })
        var lines = BriefTexts.unquoted(summary ?? [])
            .filter { !names.contains(BriefTexts.conversation($0).lowercased()) && !(waiting.isEmpty == false && BriefTexts.isQuiet($0)) }
            .prefix(6).enumerated().map { Line(id: "summary-\($0.offset)", leading: "", text: $0.element) }
        if !waiting.isEmpty, !lines.isEmpty { lines.append(Line(id: "waiting", leading: "", text: "Waiting on you", muted: true)) }
        lines += waiting.prefix(6).map { person in
            Line(id: "person-" + person.name, leading: person.latest.map(age) ?? "",
                 text: person.name + " · \(person.count) text\(person.count == 1 ? "" : "s")")
        }
        if waiting.count > 6 { lines.append(Line(id: "more", leading: "", text: "+\(waiting.count - 6) more", muted: true)) }
        if lines.isEmpty { lines = [Line(id: "empty", leading: "", text: "No texts need you", muted: true)] }
        return BriefDetail(lines: noted(lines, missing: summary == nil), source: .messages)
    }

    private static func noted(_ lines: [Line], missing: Bool) -> [Line] {
        missing ? [Line(id: "no-summary", leading: "", text: noSummaryYet, muted: true)] + lines : lines
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
    /// The sender looks like a person, not an automated sender.
    var person = false
    var received: Double? = nil
    /// You have texted that conversation since.
    var replied = false

    var who: String { sender.trimmingCharacters(in: .whitespaces) }
}

enum BriefMail {
    /// One line the Brief shows for mail: "Sender · subject" with its
    /// reply/cancel prefix as a muted tag and repeats collapsed to ×N.
    struct Shown: Equatable {
        let tag: String
        let text: String
        let count: Int
        /// The subject without its prefix, to tell whether the summary already names it.
        var subject = ""
        var line: String { count > 1 ? "\(text) ×\(count)" : text }
        /// For the row, which has no tag column: a change of plan stays visible.
        var rowLine: String { BriefMail.changeTags.contains(tag) ? "\(tag): \(line)" : line }
    }

    /// Mail worth describing: no placeholders, newsletters, promos or redacted
    /// subjects; people first, then newest.
    static func important(_ needs: [BriefNeed]) -> [BriefNeed] {
        needs.filter { !DayTimeline.isJunk($0.title) && !DayTimeline.isBulkMail($0.title) && !isPrivate($0.title) }
            .enumerated()
            .sorted { a, b in
                if a.element.person != b.element.person { return a.element.person }
                let x = a.element.received ?? -.infinity, y = b.element.received ?? -.infinity
                return x != y ? x > y : a.offset < b.offset
            }
            .map(\.element)
    }

    static func bulkCount(_ needs: [BriefNeed]) -> Int {
        needs.filter { !DayTimeline.isJunk($0.title) && DayTimeline.isBulkMail($0.title) && !isPrivate($0.title) }.count
    }

    /// Emails whose subject Wisp withheld (a code or private detail): counted, never shown as a subject.
    static func privateCount(_ needs: [BriefNeed]) -> Int {
        needs.filter { isPrivate($0.title) }.count
    }

    /// A withheld subject, also behind a Re:/Invitation: prefix ("Re: Private details omitted.").
    static func isPrivate(_ subject: String) -> Bool {
        let s = splitPrefix(subject).rest.trimmingCharacters(in: .whitespaces).lowercased()
        return s.hasPrefix("private details omitted") || s.hasPrefix("authentication details omitted") || s.contains("details omitted)")
            || s.hasPrefix("possible request context (private details omitted")
    }

    /// "John Morada", never "John Morada <john@x.com>"; a bare address when there's no name.
    static func displayName(_ sender: String) -> String {
        let text = sender.trimmingCharacters(in: .whitespaces)
        let name = text.replacingOccurrences(of: #"<[^>]*>"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: " \"'"))
        if !name.isEmpty { return name }
        if let range = text.range(of: #"<([^>]+)>"#, options: .regularExpression) {
            return String(text[range].dropFirst().dropLast())
        }
        return text
    }

    /// Lowercased words separated by single spaces, so "Tech Week: starts today"
    /// matches "Tech Week starts today". Empty for text without words.
    static func words(_ text: String) -> String {
        let parts = text.lowercased().components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }
        return parts.isEmpty ? "" : " " + parts.joined(separator: " ") + " "
    }

    /// Tags that change what the email means, so they win when repeats collapse.
    static let changeTags: Set<String> = ["Canceled", "Declined", "Updated"]

    static let prefixes: [(match: String, tag: String)] = [
        ("re", "Re"), ("fwd", "Fwd"), ("fw", "Fwd"), ("canceled", "Canceled"), ("cancelled", "Canceled"),
        ("canceled event", "Canceled"), ("updated invitation", "Updated"), ("invitation", "Invite"),
        ("accepted", "Accepted"), ("declined", "Declined"), ("tentatively accepted", "Tentative"),
    ]

    /// A subject's reply/forward/calendar prefix as a tag, and the rest: "Re: Re: Lunch" -> ("Re", "Lunch").
    static func splitPrefix(_ subject: String) -> (tag: String, rest: String) {
        var rest = subject.trimmingCharacters(in: .whitespaces)
        var tag = ""
        while let colon = rest.firstIndex(of: ":") {
            let head = rest[..<colon].trimmingCharacters(in: .whitespaces).lowercased()
            guard let known = prefixes.first(where: { $0.match == head }) else { break }
            if tag.isEmpty { tag = known.tag }
            rest = String(rest[rest.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        return rest.isEmpty ? (tag, subject) : (tag, rest)
    }

    /// The lines to show, in importance order: the same subject from the same
    /// sender collapses into one line with ×N.
    static func shown(_ needs: [BriefNeed]) -> [Shown] {
        var out: [Shown] = []
        var index: [String: Int] = [:]
        for need in important(needs) {
            let (key, line) = describing(need)
            if let at = index[key] {
                // A cancellation (or decline, or update) must not hide behind its original.
                let tag = changeTags.contains(line.tag) && !changeTags.contains(out[at].tag) ? line.tag
                    : out[at].tag.isEmpty ? line.tag : out[at].tag
                out[at] = Shown(tag: tag, text: out[at].text, count: out[at].count + 1, subject: out[at].subject)
                continue
            }
            index[key] = out.count
            out.append(line)
        }
        return out
    }

    /// "Sender · subject", or the subject alone.
    static func describe(_ need: BriefNeed) -> String {
        describing(need).line.line
    }

    /// One email's line, and the key repeats of it share.
    private static func describing(_ need: BriefNeed) -> (key: String, line: Shown) {
        let name = displayName(need.who)
        let (tag, subject) = splitPrefix(need.title)
        return (name.lowercased() + "\u{1F}" + subject.lowercased(),
                Shown(tag: tag, text: name.isEmpty ? subject : "\(name) · \(subject)", count: 1, subject: subject))
    }

    /// The MAIL row: what the inbox is about and how many emails need you
    /// ("Mostly event updates · 1 needs you", "Nothing needs you · 12 new,
    /// mostly newsletters"). Counted as the details list them (repeats once,
    /// private emails included); newsletters and promos don't count.
    static func row(overview: String, needing: [BriefNeed]) -> String {
        let n = shown(needing).count + privateCount(needing)
        let about = overview.trimmingCharacters(in: .whitespacesAndNewlines)
        if about.isEmpty { return n == 0 ? "Nothing needs you" : "\(n) need\(n == 1 ? "s" : "") you" }
        return n == 0 ? "Nothing needs you · \(about)" : "\(about) · \(n) need\(n == 1 ? "s" : "") you"
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

    /// The TEXTS row: what texts are about and how many people are waiting
    /// ("Mostly family check-ins · 2 waiting on you", "Quiet · nobody waiting").
    static func row(overview: String, people: [Person]) -> String {
        let about = overview.trimmingCharacters(in: .whitespacesAndNewlines)
        let waiting = people.isEmpty ? "nobody waiting" : "\(people.count) waiting on you"
        return about.isEmpty ? (people.isEmpty ? "Nobody waiting" : waiting.prefix(1).uppercased() + waiting.dropFirst())
            : "\(about) · \(waiting)"
    }

    /// Summary lines with what anyone wrote cut off: "Priya · 2 messages,
    /// latest 1:40 PM — Priya: “…”" becomes "Priya · 2 messages, latest 1:40 PM".
    /// A line that still carries a quote mark is dropped.
    static func unquoted(_ lines: [String]) -> [String] {
        lines.compactMap { line in
            let cut = line.range(of: " — ").map { String(line[..<$0.lowerBound]) } ?? line
            let text = cut.trimmingCharacters(in: .whitespaces)
            return text.isEmpty || text.contains("\u{201C}") || text.contains("\u{201D}") || text.contains("\"") ? nil : text
        }
    }

    /// The conversation a summary line is about: the part before " · ".
    static func conversation(_ line: String) -> String {
        line.range(of: " · ").map { String(line[..<$0.lowerBound]) } ?? ""
    }

    static func isQuiet(_ line: String) -> Bool {
        line.lowercased().hasPrefix("no important recent messages")
    }

    /// How long someone has been waiting: "12m", "3h", "2d".
    static func age(_ since: Double, now: Double) -> String {
        let seconds = max(0, now - since)
        if seconds < 3600 { return "\(max(1, Int(seconds / 60)))m" }
        if seconds < 86400 { return "\(Int(seconds / 3600))h" }
        return "\(Int(seconds / 86400))d"
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

/// The Brief's four count tiles: Today, Mail, Messages and Events. Each tile's
/// count and the list it opens come from one place, so the number on a tile
/// is always what its list holds.
enum BriefTile: String, CaseIterable, Identifiable, Equatable {
    case today, mail, messages, events

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "Today"
        case .mail: return "Mail"
        case .messages: return "Messages"
        case .events: return "Events"
        }
    }

    /// The digit that toggles it from the keyboard.
    var shortcut: Character {
        switch self {
        case .today: return "1"
        case .mail: return "2"
        case .messages: return "3"
        case .events: return "4"
        }
    }

    /// The tile `offset` places from this one, wrapping, for arrow keys.
    func moved(_ offset: Int) -> BriefTile {
        let all = Self.allCases
        let at = all.firstIndex(of: self) ?? 0
        return all[((at + offset) % all.count + all.count) % all.count]
    }
}

/// One tile: its count, nil while its source hasn't been read, and its list.
struct BriefTileContent: Equatable {
    let count: Int?
    let detail: BriefDetail
}

/// One thing to do: a reminder or deadline with a due time, or a Day task.
struct BriefTodo: Equatable {
    let id: String
    let title: String
    var due: Double? = nil
    var allDay = false
    /// A Day task's planned start, when it has one.
    var start: Double? = nil
}

enum BriefTiles {
    /// Most rows the Today list shows before "+N more".
    static let shown = 8

    /// What the user has to do today: what's overdue (today only), then what's
    /// due by the end of the day by its time, then the Day tab's tasks. A Day
    /// task made from a reminder ("Review: …" too) is listed once. Something
    /// due all day today is due today, not overdue.
    static func todo(dated: [BriefTodo], tasks: [BriefTodo], now: Double, dayStart: Double, dayEnd: Double,
                     isToday: Bool) -> [BriefTodo] {
        let usable = { (t: BriefTodo) in !DayTimeline.isJunk(t.title) }
        let late = { (t: BriefTodo) in isOverdue(t, now: now, dayStart: dayStart) }
        let overdue = isToday ? dated.filter { usable($0) && late($0) }.sorted { $0.due! < $1.due! } : []
        let reference = isToday ? now : dayStart
        let due = dated.filter { t in
            guard usable(t), let due = t.due, due <= dayEnd else { return false }
            return isToday ? !late(t) && (due >= now || (t.allDay && due >= dayStart)) : due >= reference
        }
        .sorted { $0.due! < $1.due! }
        let planned = tasks.filter(usable).sorted { ($0.start ?? .infinity) < ($1.start ?? .infinity) }
        var seen = Set<String>()
        return (overdue + due + planned).filter { seen.insert(key($0.title)).inserted }
    }

    /// The same thing to do, however it's titled: "Review: PA 2" is "pa 2".
    static func key(_ title: String) -> String {
        var text = title.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("Review: ") { text = String(text.dropFirst("Review: ".count)) }
        return text.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased()
    }

    /// Past its due time, except something due all day today.
    static func isOverdue(_ todo: BriefTodo, now: Double, dayStart: Double) -> Bool {
        guard let due = todo.due, due < now else { return false }
        return !(todo.allDay && due >= dayStart)
    }

    /// The Today tile's list: the label column says overdue, when it's due, or
    /// when a task is planned.
    static func todayDetail(_ todos: [BriefTodo], now: Double, dayStart: Double, clock: (Double) -> String) -> BriefDetail {
        guard !todos.isEmpty else {
            return BriefDetail(lines: [BriefDetail.Line(id: "empty", leading: "", text: "Nothing to do today", muted: true)],
                               source: .reminders)
        }
        var lines = todos.prefix(shown).map { todo -> BriefDetail.Line in
            let label: String
            if isOverdue(todo, now: now, dayStart: dayStart) { label = "Overdue" }
            else if let due = todo.due { label = todo.allDay ? "Today" : "Due " + clock(due) }
            else if let start = todo.start { label = clock(start) }
            else { label = "To do" }
            return BriefDetail.Line(id: "todo-" + todo.id, leading: label, text: todo.title)
        }
        if todos.count > shown {
            lines.append(BriefDetail.Line(id: "more", leading: "", text: "+\(todos.count - shown) more", muted: true))
        }
        return BriefDetail(lines: lines, source: .reminders)
    }

    /// The Events tile's list: today's calendar, then what's coming up this week.
    /// Today's part says so when it's empty, so the tile opens on a zero count.
    static func eventsDetail(today: [BriefDetail.Line], unsynced: Bool, soon: [UpcomingItem]?,
                             when: (UpcomingItem) -> String) -> BriefDetail {
        var lines = today.isEmpty
            ? [BriefDetail.Line(id: "empty", leading: "", text: unsynced ? "Calendar hasn't synced yet" : "Nothing scheduled today",
                                muted: true)]
            : today
        if let soon, !soon.isEmpty {
            lines.append(BriefDetail.Line(id: "soon-header", leading: "", text: "Coming up", muted: true))
            lines += soon.map { BriefDetail.Line(id: "soon-" + $0.id, leading: when($0), text: $0.title) }
        }
        return BriefDetail(lines: lines, source: .calendar)
    }

    /// The default list under the tiles: the three things that matter most,
    /// from the service's pick (overdue, due, planned, then mail and texts from
    /// people), then today's to-dos, then the next event, each named once.
    /// The MAIL and TEXTS lines under the Top 3: the regular summaries
    /// (DayTimeline.briefLines), with the written inbox summary in place of
    /// MAIL's one-liner when there is one. A source not read yet stays a dim
    /// line; the row style cuts each to two lines, and the tiles hold the rest.
    static func glance(_ lines: [DayTimeline.BriefLine], mailProse: String?) -> [BriefDetail.Line] {
        let prose = mailProse?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return lines.filter { $0.tag == "MAIL" || $0.tag == "TEXTS" }.map { line in
            let text = line.tag == "MAIL" && !line.muted && !prose.isEmpty ? prose
                : line.trailing.isEmpty ? line.text : "\(line.text) · \(line.trailing)"
            return BriefDetail.Line(id: "glance-" + line.tag, leading: line.tag == "MAIL" ? "Mail" : "Texts",
                                    text: text, muted: line.muted)
        }
    }

    static func top(picked: [NeedsYouRow], todos: [BriefDetail.Line], next: BriefDetail.Line?, limit: Int = 3) -> [BriefDetail.Line] {
        var seen = Set<String>()
        var out: [BriefDetail.Line] = []
        let candidates = picked.map { BriefDetail.Line(id: "top-" + $0.id, leading: $0.label ?? "", text: $0.text) }
            + todos.filter { !$0.muted } + [next].compactMap { $0 }
        for line in candidates where out.count < limit && seen.insert(key(line.text)).inserted {
            out.append(line)
        }
        return out
    }
}
