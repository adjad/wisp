import Foundation

/// What the proactive engine (service/proactive) caught up on before you asked:
/// `GET /assistant/today` → `workspace.proactive`. Ready-to-fire cards, the
/// to-do list built from every source, and one summary per mail thread or text
/// conversation.
///
/// Decoding never throws. A missing, partial or malformed `proactive` object
/// (an older service, a field the engine dropped) leaves defaults in place and
/// skips just the bad rows, so it can never take the rest of the day's plan
/// down with it.
///
/// Pure Foundation, checked by tests/ProactiveChecks.swift without the app.
struct ProactiveState: Decodable, Equatable {
    struct Progress: Decodable, Equatable {
        var done: Int = 0
        var total: Int = 0
    }

    /// "ready", "working" or "unavailable".
    var state: String = "unavailable"
    var asOf: Double? = nil
    var progress: Progress? = nil
    var accounts: [String] = []
    /// The owner's own bucket: this Mac's texts, Notes, Reminders, Calendar
    /// and Mail. Linked Google accounts only ever carry their own mail and calendar.
    var ownerID: String = "me"
    var ownerLabel: String = ""
    /// A 2–3 sentence overview of the day.
    var summary: String = ""
    /// One sentence for the top of the Brief (W1); else the summary's first.
    var lede: String = ""
    /// Why there is nothing yet, when the engine hasn't read anything.
    var reason: String = ""
    /// The sources the service says Wisp couldn't read (cycle.honest).
    var unread: [String] = []
    /// Linked Google accounts whose mail Wisp read recently: connected (not
    /// expired or failing) with a fresh sync (cycle.google_mail_read). Mail
    /// counts as read through them, and only through them.
    var mailReadBy: [String] = []
    /// Connected Google accounts Wisp couldn't check (mail_unread_by): while
    /// any is listed, mail is only partly read and never an all-clear.
    var mailUnreadBy: [String] = []
    /// Why each of those wasn't checked: "reconnect" or "syncing".
    var mailUnreadReasons: [String: String] = [:]
    var uncheckedMail: UncheckedMail { UncheckedMail(unreadBy: mailUnreadBy, reasons: mailUnreadReasons) }
    /// The work behind the cards: what Wisp read, found and drafted.
    var stats: Stats? = nil

    struct Stats: Decodable, Equatable {
        var read: Int = 0
        var found: Int = 0
        var drafted: Int = 0
        var filtered: Int = 0
        var asOf: Double? = nil
    }
    var cards: [ProactiveCard] = []
    var todos: [ProactiveTodo] = []
    var threads: [ProactiveThread] = []

    var isWorking: Bool { state == "working" }
    var isUnavailable: Bool { state != "ready" && state != "working" }
    /// "Ready" with nothing read and no account is a cycle that never read
    /// anything (no source could be opened), not an all-clear.
    var hasReadNothing: Bool {
        state == "ready" && (stats?.read ?? 0) == 0 && accounts.isEmpty && cards.isEmpty && todos.isEmpty && threads.isEmpty
    }

    // MARK: Honesty about unread sources

    /// The day's sources Wisp couldn't read, from the workspace's coverage
    /// (`source_coverage`), in reading order. Mail follows SourceAccess.mail:
    /// read through a connected Google account (`mailReadBy`) counts, and any
    /// account left unchecked (`mailUnreadBy`) makes mail only partly read.
    func unreadSources(_ coverage: [SourceAccess]) -> [SourceAccess] {
        ["calendar", "reminders", "email", "messages"].compactMap { source in
            guard var access = coverage.first(where: { $0.source == source }) else { return nil }
            if source == "email" { access = SourceAccess.mail(access, readBy: mailReadBy, unchecked: uncheckedMail) }
            return access.readable ? nil : access
        }
    }

    /// The mail-and-texts list's line when it lists nothing: an all-clear only
    /// once both were read; nil otherwise (the unread rows say why).
    static func attentionEmptyLine(unread: [SourceAccess]) -> String? {
        unread.contains { $0.source == "email" || $0.source == "messages" } ? nil
            : "Nothing in your inbox or texts needs you."
    }

    /// The collapsed to-do row, honest about what couldn't be read.
    func todoLine(unread: [SourceAccess]) -> String {
        let open = todos.filter { !$0.done }
        if open.isEmpty && !unread.isEmpty {
            return todos.isEmpty ? "No to-dos in what Wisp could read" : "All to-dos done in what Wisp could read"
        }
        return todoLine
    }

    /// The calendar row's line when nothing else is on today.
    static func emptyDayLine(calendarUnread: Bool) -> String {
        calendarUnread ? "Wisp can't see your calendar yet" : "Nothing else on your calendar today"
    }

    init(state: String = "ready", asOf: Double? = nil, progress: Progress? = nil, accounts: [String] = [],
         summary: String = "", cards: [ProactiveCard] = [], todos: [ProactiveTodo] = [], threads: [ProactiveThread] = []) {
        self.state = state
        self.asOf = asOf
        self.progress = progress
        self.accounts = accounts
        self.summary = summary
        self.cards = cards
        self.todos = todos
        self.threads = threads
    }

    private enum Keys: String, CodingKey { case state, as_of, progress, accounts, summary, cards, todos, threads, lede, stats, owner, reason, unread, mail_read_by, mail_unread_by, mail_unread_reasons }
    private enum OwnerKeys: String, CodingKey { case id, label }
    private enum StatsKeys: String, CodingKey { case read, found, drafted, filtered, as_of }

    init(from decoder: Decoder) {
        guard let c = try? decoder.container(keyedBy: Keys.self) else { return }
        state = c.lenientString(.state) ?? "unavailable"
        asOf = c.lenient(.as_of, Double.self)
        if let p = try? c.nestedContainer(keyedBy: ProgressKeys.self, forKey: .progress) {
            progress = Progress(done: p.lenient(.done, Int.self) ?? 0, total: p.lenient(.total, Int.self) ?? 0)
        }
        accounts = c.lenient(.accounts, LossyList<String>.self)?.items.filter { !$0.isEmpty } ?? []
        lede = c.lenientString(.lede) ?? ""
        reason = c.lenientString(.reason) ?? ""
        unread = c.lenient(.unread, LossyList<String>.self)?.items.filter { !$0.isEmpty } ?? []
        mailReadBy = c.lenient(.mail_read_by, LossyList<String>.self)?.items.filter { !$0.isEmpty } ?? []
        mailUnreadBy = c.lenient(.mail_unread_by, LossyList<String>.self)?.items.filter { !$0.isEmpty } ?? []
        mailUnreadReasons = c.lenient(.mail_unread_reasons, [String: String].self) ?? [:]
        if let o = try? c.nestedContainer(keyedBy: OwnerKeys.self, forKey: .owner) {
            ownerID = o.lenientString(.id).flatMap { $0.isEmpty ? nil : $0 } ?? "me"
            ownerLabel = o.lenientString(.label) ?? ""
        }
        if let st = try? c.nestedContainer(keyedBy: StatsKeys.self, forKey: .stats) {
            stats = Stats(read: st.lenient(.read, Int.self) ?? 0, found: st.lenient(.found, Int.self) ?? 0,
                          drafted: st.lenient(.drafted, Int.self) ?? 0,
                          filtered: st.lenient(.filtered, Int.self) ?? 0, asOf: st.lenient(.as_of, Double.self))
        }
        summary = c.lenientString(.summary) ?? ""
        cards = c.lenient(.cards, LossyList<ProactiveCard>.self)?.items.filter { !$0.id.isEmpty } ?? []
        todos = c.lenient(.todos, LossyList<ProactiveTodo>.self)?.items.filter { !$0.id.isEmpty && !$0.title.isEmpty } ?? []
        threads = c.lenient(.threads, LossyList<ProactiveThread>.self)?.items.filter { !$0.id.isEmpty } ?? []
    }

    private enum ProgressKeys: String, CodingKey { case done, total }

    // MARK: Presentation

    /// The Brief's one sentence: the engine's lede, else the summary's first
    /// sentence; nil when it sent neither.
    var headline: String? {
        let lede = lede.trimmingCharacters(in: .whitespacesAndNewlines)
        if !lede.isEmpty { return Self.firstSentence(lede) }
        let summary = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        return summary.isEmpty ? nil : Self.firstSentence(summary)
    }

    /// Up to the first sentence end (". ", "! ", "? ") that isn't inside a
    /// number like "2.5" or a clock like "4:15"; the whole text when there's none.
    static func firstSentence(_ text: String) -> String {
        let chars = Array(text)
        var i = 0
        while i < chars.count - 1 {
            if ".!?".contains(chars[i]), chars[i + 1] == " " {
                // "Dr. Lee", "St. Mary's": a title's period isn't an end.
                let before = String(chars[..<i]).split(separator: " ").last.map { $0.lowercased() } ?? ""
                let abbreviation = chars[i] == "." && ["dr", "mr", "mrs", "ms", "st", "prof", "jr", "sr", "vs", "e.g", "i.e"].contains(before)
                if !abbreviation { return String(chars[...i]) }
            }
            i += 1
        }
        return text
    }

    /// "Wisp read 132 emails and texts · skipped 120 newsletters · found 3 things · drafted 1 reply";
    /// nil without stats or when nothing was read.
    var workLine: String? {
        guard let stats, stats.read > 0 else { return nil }
        var parts = ["Wisp read \(stats.read) email\(stats.read == 1 ? "" : "s") and texts"]
        if stats.filtered > 0 { parts.append("skipped \(stats.filtered) newsletter\(stats.filtered == 1 ? "" : "s")") }
        parts.append("found \(stats.found) thing\(stats.found == 1 ? "" : "s")")
        if stats.drafted > 0 { parts.append("drafted \(stats.drafted) repl\(stats.drafted == 1 ? "y" : "ies")") }
        return parts.joined(separator: " · ")
    }

    /// The collapsed to-do row: "4 to-dos · Readings ×2, Worksheet 3". Open
    /// to-dos only; the same title twice is counted once with ×n.
    var todoLine: String {
        let open = todos.filter { !$0.done }
        guard !open.isEmpty else { return todos.isEmpty ? "No to-dos" : "All to-dos done" }
        var order: [String] = []
        var counts: [String: (title: String, n: Int)] = [:]
        for todo in open {
            let key = DayTimeline.todoKey(todo.title)
            if let seen = counts[key] { counts[key] = (seen.title, seen.n + 1) } else {
                order.append(key)
                counts[key] = (todo.title, 1)
            }
        }
        let names = order.compactMap { counts[$0] }.map { $0.n > 1 ? "\($0.title) ×\($0.n)" : $0.title }
        return "\(open.count) to-do\(open.count == 1 ? "" : "s") · " + names.joined(separator: ", ")
    }

    /// The collapsed mail-and-texts row: "Mail and texts · Ed, Mom, Devarsh, +3",
    /// people waiting on a reply first.
    var peopleLine: String {
        var names: [String] = []
        for thread in orderedThreads {
            let name = thread.who.trimmingCharacters(in: .whitespaces)
            if !name.isEmpty && !names.contains(name) { names.append(name) }
        }
        guard !names.isEmpty else { return "Mail and texts · nothing new" }
        let shown = names.prefix(3).joined(separator: ", ")
        return "Mail and texts · " + shown + (names.count > 3 ? ", +\(names.count - 3)" : "")
    }

    /// "Reading 12 of 40…" while the engine works.
    var progressLabel: String {
        guard let progress, progress.total > 0 else { return "Reading your mail and texts…" }
        return "Reading \(min(progress.done, progress.total)) of \(progress.total)…"
    }

    /// 0…1 for a progress bar, or nil when the engine hasn't said how far.
    var progressFraction: Double? {
        guard let progress, progress.total > 0 else { return nil }
        return min(1, max(0, Double(progress.done) / Double(progress.total)))
    }

    /// Whether an item tagged `account` belongs to the chosen account. No choice
    /// shows everything; an untagged item (this Mac's Messages, Reminders,
    /// Notes) belongs to the owner's own bucket.
    func belongs(_ account: String, to chosen: String?) -> Bool {
        guard let chosen else { return true }
        let owner = account.isEmpty ? ownerID : account
        return owner.caseInsensitiveCompare(chosen) == .orderedSame
    }

    /// The same state with only the chosen account's cards, to-dos and threads.
    func only(_ chosen: String?) -> ProactiveState {
        guard chosen != nil else { return self }
        var copy = self
        copy.cards = cards.filter { belongs($0.account, to: chosen) }
        copy.todos = todos.filter { belongs($0.account, to: chosen) }
        copy.threads = threads.filter { belongs($0.account, to: chosen) }
        return copy
    }

    /// To-dos in the order to do them: open first, soonest due first, undated
    /// last; done ones keep their place at the bottom.
    var orderedTodos: [ProactiveTodo] {
        todos.enumerated().sorted { a, b in
            if a.element.done != b.element.done { return !a.element.done }
            let x = a.element.dueTs ?? .infinity, y = b.element.dueTs ?? .infinity
            return x != y ? x < y : a.offset < b.offset
        }.map(\.element)
    }

    /// Threads in the order they need you: a reply first, then people before
    /// organizations, then newest first.
    var orderedThreads: [ProactiveThread] {
        threads.enumerated().sorted { a, b in
            if a.element.needsReply != b.element.needsReply { return a.element.needsReply }
            if a.element.isOrganization != b.element.isOrganization { return !a.element.isOrganization }
            let x = a.element.receivedTs ?? 0, y = b.element.receivedTs ?? 0
            return x != y ? x > y : a.offset < b.offset
        }.map(\.element)
    }

    /// A short chip name for an account: the part before the @, unless two
    /// accounts share it.
    func chipName(_ account: String) -> String {
        if account == ownerID { return ownerLabel.isEmpty ? "You" : ownerLabel }
        let local = account.split(separator: "@").first.map(String.init) ?? account
        let clashes = accounts.filter { ($0.split(separator: "@").first.map(String.init) ?? $0) == local }.count > 1
        return clashes || local.isEmpty ? account : local
    }
}

/// One ready-to-fire card: "Before you ask".
struct ProactiveCard: Decodable, Equatable, Identifiable {
    struct Source: Decodable, Equatable {
        var kind = "", ref = "", who = "", quote = ""
        private enum Keys: String, CodingKey { case kind, ref, who, quote }
        init(kind: String = "", ref: String = "", who: String = "", quote: String = "") {
            self.kind = kind; self.ref = ref; self.who = who; self.quote = quote
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            kind = c.lenientString(.kind) ?? ""
            ref = c.lenientString(.ref) ?? ""
            who = c.lenientString(.who) ?? ""
            quote = c.lenientString(.quote) ?? ""
        }
    }

    struct Action: Decodable, Equatable {
        /// "email_reply", "text_reply", "reminder", "calendar_event", "add_to_day", "block_time", "read",
        /// or "undo_add": a "Wisp did this" card (meta "ADDED · CALENDAR"), whose button removes exactly
        /// what Wisp added (service/proactive/autoadd.py). It fires through the same card route.
        var method = ""
        /// The one question-style button: "Reply & add to calendar?".
        var button = ""
        /// The verbatim draft the person can read and edit before firing.
        var draft = ""
        var recipient = ""
        /// For "block_time": the hour Wisp would hold.
        var slot: Slot? = nil
        struct Slot: Decodable, Equatable { var start: Double; var end: Double }
        private enum Keys: String, CodingKey { case method, button, draft, recipient, slot }
        private enum SlotKeys: String, CodingKey { case start, end }
        init(method: String = "", button: String = "", draft: String = "", recipient: String = "", slot: Slot? = nil) {
            self.method = method; self.button = button; self.draft = draft; self.recipient = recipient; self.slot = slot
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: Keys.self)
            method = c.lenientString(.method) ?? ""
            button = c.lenientString(.button) ?? ""
            draft = c.lenientString(.draft) ?? ""
            recipient = c.lenientString(.recipient) ?? ""
            if let sc = try? c.nestedContainer(keyedBy: SlotKeys.self, forKey: .slot),
               let start = sc.lenient(.start, Double.self), let end = sc.lenient(.end, Double.self), end > start {
                slot = Slot(start: start, end: end)
            }
        }

        /// The button's words, never empty.
        var label: String {
            let trimmed = button.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { return trimmed }
            switch method {
            case "email_reply", "text_reply": return "Send the reply?"
            case "reminder": return "Add the reminder?"
            case "calendar_event": return "Add to calendar?"
            case "add_to_day": return "Add to my day?"
            case "block_time": return "Block the time?"
            case "undo_add": return "Undo?"
            default: return "Got it"
            }
        }

        /// Whether the draft is something the person sends, so it opens editable.
        var sendsDraft: Bool { method == "email_reply" || method == "text_reply" }

        /// What the card's main button does now. A reply never sends a draft the
        /// person hasn't seen: its first tap opens the draft, and Send sits
        /// inside the shown draft.
        enum Step: Equatable { case openDraft, fire }
        func primaryStep(draftOpen: Bool) -> Step { sendsDraft && !draftOpen ? .openDraft : .fire }

        /// The main button's words for that step: the card's question until the
        /// draft is open, then "Send".
        func primaryLabel(draftOpen: Bool) -> String {
            guard sendsDraft else { return label }
            return draftOpen ? "Send" : label
        }
    }

    var id = ""
    /// The caps line: "OVERDUE · 3 DAYS · MAIL".
    var meta = ""
    var title = ""
    /// What Wisp did: "I checked… I've drafted…".
    var body = ""
    var why = ""
    var account = ""
    var sources: [Source] = []
    var action: Action? = nil
    /// For a mail card: the thread's `{message_id, account, account_id}`, when the engine sent one.
    var replyRef: [String: String] = [:]

    private enum Keys: String, CodingKey { case id, meta, title, body, why, account, sources, action, reply_ref }

    init(id: String, meta: String = "", title: String = "", body: String = "", why: String = "",
         account: String = "", sources: [Source] = [], action: Action? = nil) {
        self.id = id; self.meta = meta; self.title = title; self.body = body; self.why = why
        self.account = account; self.sources = sources; self.action = action
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        id = c.lenientString(.id) ?? ""
        meta = c.lenientString(.meta) ?? ""
        title = c.lenientString(.title) ?? ""
        body = c.lenientString(.body) ?? ""
        why = c.lenientString(.why) ?? ""
        account = c.lenientString(.account) ?? ""
        sources = c.lenient(.sources, LossyList<Source>.self)?.items ?? []
        action = c.lenient(.action, Action.self)
        replyRef = c.lenientRef(.reply_ref)
    }

    /// The card's name on a chip under the one shown.
    var chipTitle: String { title.isEmpty ? metaLine.capitalized : title }

    /// The meta line, uppercased; built from the first source when the engine sent none.
    var metaLine: String {
        let trimmed = meta.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed.uppercased() }
        let kind = sources.first.map { DayTimeline.sourceName($0.kind) } ?? ""
        return kind.isEmpty ? "BEFORE YOU ASK" : kind.uppercased()
    }
}

/// One to-do, from any source.
struct ProactiveTodo: Decodable, Equatable, Identifiable {
    var id = ""
    var title = ""
    /// "mail", "messages", "calendar", "reminders" or "notes".
    var source = ""
    var account = ""
    var who = ""
    var quote = ""
    var dueTs: Double? = nil
    var dueLabel = ""
    var kind = ""
    var done = false
    var cardID: String? = nil
    /// What Clear sends back (cycle.item_fingerprint); empty from an older service.
    var fingerprint = ""
    /// A dated reminder Mark done can complete in Reminders.
    var canComplete = false

    private enum Keys: String, CodingKey {
        case id, title, source, account, who, quote, due_ts, due_label, kind, done, card_id, fingerprint, can_complete
    }

    init(id: String, title: String, source: String, account: String = "", who: String = "", quote: String = "",
         dueTs: Double? = nil, dueLabel: String = "", kind: String = "", done: Bool = false, cardID: String? = nil) {
        self.id = id; self.title = title; self.source = source; self.account = account; self.who = who
        self.quote = quote; self.dueTs = dueTs; self.dueLabel = dueLabel; self.kind = kind; self.done = done
        self.cardID = cardID
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        id = c.lenientString(.id) ?? ""
        title = c.lenientString(.title) ?? ""
        source = c.lenientString(.source) ?? ""
        account = c.lenientString(.account) ?? ""
        who = c.lenientString(.who) ?? ""
        quote = c.lenientString(.quote) ?? ""
        dueTs = c.lenient(.due_ts, Double.self).flatMap { $0.isFinite ? $0 : nil }
        dueLabel = c.lenientString(.due_label) ?? ""
        kind = c.lenientString(.kind) ?? ""
        done = c.lenient(.done, Bool.self) ?? false
        cardID = c.lenientString(.card_id).flatMap { $0.isEmpty ? nil : $0 }
        fingerprint = c.lenientString(.fingerprint) ?? ""
        canComplete = c.lenient(.can_complete, Bool.self) ?? false
    }

    /// Its trailing × (NeedsYouActionsModel): Clear for every to-do, Mark done
    /// only for an open dated reminder.
    var actionRow: NeedsYouRow {
        NeedsYouRow(id: id, label: nil, text: title, overdue: false,
                    fingerprint: fingerprint.isEmpty ? nil : fingerprint,
                    canComplete: canComplete && !done && source == "reminders", route: NeedsYouRow.todoRoute)
    }

    /// As the Day tab's timeline takes it.
    var dayInput: DayTodoInput {
        DayTodoInput(id: id, title: title, source: source, who: who, due: dueTs, dueLabel: dueLabel, kind: kind, done: done)
    }

    /// A calendar event is already on the Day; anything else can be added to it.
    var canAddToDay: Bool { source != "calendar" }
}

/// One mail thread or text conversation, summarized.
struct ProactiveThread: Decodable, Equatable, Identifiable {
    var id = ""
    /// "mail" or "messages".
    var source = ""
    var account = ""
    var who = ""
    var title = ""
    var summary = ""
    var needsReply = false
    var receivedTs: Double? = nil
    /// What the reply composer (BriefReplyComposer) drafts from:
    /// `{message_id, account, account_id}`, each optional.
    var replyRef: [String: String] = [:]
    /// What Clear sends back (cycle.item_fingerprint); empty from an older service.
    var fingerprint = ""

    private enum Keys: String, CodingKey {
        case id, source, account, who, title, summary, needs_reply, received_ts, reply_ref, fingerprint
    }

    init(id: String, source: String, account: String = "", who: String = "", title: String = "", summary: String = "",
         needsReply: Bool = false, receivedTs: Double? = nil, replyRef: [String: String] = [:]) {
        self.id = id; self.source = source; self.account = account; self.who = who; self.title = title
        self.summary = summary; self.needsReply = needsReply; self.receivedTs = receivedTs; self.replyRef = replyRef
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        id = c.lenientString(.id) ?? ""
        source = c.lenientString(.source) ?? ""
        account = c.lenientString(.account) ?? ""
        who = c.lenientString(.who) ?? ""
        title = c.lenientString(.title) ?? ""
        summary = c.lenientString(.summary) ?? ""
        needsReply = c.lenient(.needs_reply, Bool.self) ?? false
        receivedTs = c.lenient(.received_ts, Double.self).flatMap { $0.isFinite ? $0 : nil }
        replyRef = c.lenientRef(.reply_ref)
        fingerprint = c.lenientString(.fingerprint) ?? ""
    }

    /// Its trailing ×: Clear only. Nothing is marked read or answered.
    var actionRow: NeedsYouRow {
        NeedsYouRow(id: id, label: nil, text: who.isEmpty ? title : who + ": " + title, overdue: false,
                    fingerprint: fingerprint.isEmpty ? nil : fingerprint, route: NeedsYouRow.threadRoute)
    }

    /// From a team, company or mailer rather than a person, by its name.
    var isOrganization: Bool {
        let name = who.lowercased()
        guard !name.isEmpty else { return false }
        let words = ["team", "inc", "llc", "noreply", "no-reply", "support", "newsletter", "news", "notification",
                     "events", "office", "admissions", "department", "university", "store", "billing", "digest", "updates"]
        return words.contains { name.contains($0) }
    }

    /// Mail threads with a reference can be answered from the Brief.
    var canReply: Bool { (source == "mail" || source == "email") && !(replyRef["message_id"] ?? "").isEmpty }
    /// The account the reply goes out from: the reference's, else the thread's.
    var replyAccount: String { replyRef["account"].flatMap { $0.isEmpty ? nil : $0 } ?? account }
    /// A plain reference for Ask: the message id.
    var reference: String { replyRef["message_id"] ?? "" }
    /// The line shown collapsed: the summary, else the subject.
    var line: String { summary.isEmpty ? title : summary }
}

/// What the service said about a fire, dismiss or to-do change.
struct ProactiveOutcome: Equatable {
    var ok: Bool
    var message: String
    /// "open_in_calendar", "open_in_gmail", … when the engine handed the step back.
    var handoff: String = ""
    var url: String = ""

    /// A 2xx reply is a success unless it says `ok: false` (or names an error).
    static func parse(_ data: Data) -> ProactiveOutcome {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let error = (obj["error"] as? String) ?? ""
        let ok = (obj["ok"] as? Bool) ?? error.isEmpty
        let message = [obj["detail"], obj["message"], obj["error"]].compactMap { $0 as? String }.first { !$0.isEmpty } ?? ""
        let handoff = [obj["result"], obj["handoff"]].compactMap { $0 as? String }.first { $0.hasPrefix("open_") } ?? ""
        let url = [obj["open_url"], obj["url"]].compactMap { $0 as? String }.first { !$0.isEmpty } ?? ""
        return ProactiveOutcome(ok: ok, message: message, handoff: handoff, url: url)
    }
}

// MARK: - Lenient decoding

/// An array that keeps the rows that decode and skips the rest.
struct LossyList<T: Decodable>: Decodable {
    var items: [T] = []
    private struct Skip: Decodable { init(from decoder: Decoder) throws {} }
    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        while !c.isAtEnd {
            if let item = try? c.decode(T.self) { items.append(item) }
            else if (try? c.decode(Skip.self)) == nil { break }
        }
    }
}

extension KeyedDecodingContainer {
    /// The value, or nil when it is missing, null or the wrong type.
    func lenient<T: Decodable>(_ key: Key, _ type: T.Type) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }

    /// A `{message_id, account, account_id}` reference: the string values of an
    /// object, or a bare string taken as the message id; empty otherwise.
    func lenientRef(_ key: Key) -> [String: String] {
        if let s = lenient(key, String.self) { return s.isEmpty ? [:] : ["message_id": s] }
        guard let nested = try? nestedContainer(keyedBy: AnyKey.self, forKey: key) else { return [:] }
        var out: [String: String] = [:]
        for k in nested.allKeys { if let v = nested.lenientString(k) { out[k.stringValue] = v } }
        return out
    }

    /// A string, or a number written as one; nil otherwise.
    func lenientString(_ key: Key) -> String? {
        if let s = lenient(key, String.self) { return s }
        if let i = lenient(key, Int.self) { return String(i) }
        return nil
    }
}

/// Any key, for reading an object whose keys aren't fixed.
struct AnyKey: CodingKey {
    var stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
