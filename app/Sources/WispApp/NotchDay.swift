import Foundation

/// The notch's Brief and Day tabs, as pure logic: what goes on the timeline and
/// how it is styled, the badge, the Brief's sentence and lines, the Day's blurb,
/// and the plain-language rewrites of planner warnings.
///
/// Nothing here writes anywhere. Suggestions, learned windows and caught plans
/// keep the planner's `action_authority: none`; they reach the plan only through
/// the person's Keep, Move or Add in the view.
///
/// Pure Foundation, checked by tests/NotchDayChecks.swift without the app.

/// How sure Wisp is about a block, which is how its agenda row is marked.
enum DayStyle: Int, CaseIterable, Comparable {
    /// On your calendar: solid white.
    case calendar
    /// One of your own planner tasks: solid gray.
    case task
    /// Suggested by Wisp from your sources: dashed blue, with Keep and Move.
    case suggested
    /// Learned "usually busy" or "usually away": dim, with Not today.
    case usual
    /// A plan caught in your texts: amber, with Add and Ignore.
    case caught
    /// A reminder due later today: dotted.
    case needsYou
    /// Past due: red, near now.
    case overdue
    /// A to-do Wisp found in your mail, texts, notes or reminders, at its time.
    case todo

    static func < (a: DayStyle, b: DayStyle) -> Bool { a.rawValue < b.rawValue }
}

/// One planner block, as the view hands it over.
struct DayBlockInput: Equatable {
    var id: String
    var title: String
    var start: Double
    var end: Double?
    var kind: String
    var taskID: String? = nil
    var patternID: String? = nil
    var evidenceID: String? = nil
    /// For a grouped review suggestion ("Review 3 emails"): what it covers.
    var members: [DayMember] = []
}

/// One item a grouped review suggestion covers: an email, message or note.
struct DayMember: Equatable {
    var title: String
    var evidenceID: String
}

/// One "Needs you" item from the daily workspace.
struct DayNeedInput: Equatable {
    var id: String
    var title: String
    var source: String
    var due: Double?
}

/// One item of the Brief's Needs you line, as the service phrased it.
struct NeedsYouInput: Equatable {
    /// "overdue", "due", "plan", "mail" or "text".
    var id: String? = nil
    var kind: String
    var text: String
    var due: Double? = nil
    var allDay = false
    /// What the row said, as the service fingerprinted it; Clear sends it back.
    var fingerprint: String? = nil
    /// A dated reminder the service can complete (daily_workspace.needs_you).
    var canComplete = false
}

/// What a Needs you row offers behind its ×. Clear only hides the row from
/// Needs you (service/assistant/needs_you_api.py); the reminder itself stays.
/// Mark done completes a reminder through the verified native path.
enum NeedsYouRowAction: String, Equatable, CaseIterable {
    case clear, markDone

    var title: String { self == .clear ? "Clear" : "Mark done" }
    /// Said while the service works on it.
    var progress: String { self == .clear ? "Clearing…" : "Marking done…" }
    /// The service path under /assistant/needs_you/<id>/.
    var path: String { self == .clear ? "clear" : "done" }
    func accessibilityLabel(_ text: String) -> String {
        self == .clear ? "Clear \u{201C}\(text)\u{201D} from Needs you" : "Mark \u{201C}\(text)\u{201D} done in Reminders"
    }
    func help(_ text: String) -> String {
        self == .clear ? "Hide this from Needs you. It stays in Reminders, and comes back if it changes."
            : "Complete this reminder in Reminders"
    }
}

/// One scannable row in the Brief's structured Needs you section.
struct NeedsYouRow: Equatable, Identifiable {
    let id: String
    let label: String?
    let text: String
    let overdue: Bool
    var fingerprint: String? = nil
    var canComplete = false
    /// Where its answers go: <route>/<id>/clear|done. The Brief's Needs you
    /// rows by default; the proactive Brief's to-dos and threads have their own.
    var route = NeedsYouRow.needsYouRoute

    static let needsYouRoute = "assistant/needs_you"
    static let todoRoute = "assistant/proactive/todos"
    static let threadRoute = "assistant/proactive/threads"

    /// Clear for any row the service identified; Mark done for a reminder too.
    /// A row with no service id or fingerprint (an older service) offers nothing.
    var actions: [NeedsYouRowAction] {
        guard let fingerprint, !fingerprint.isEmpty, !id.hasPrefix("needs-you-") else { return [] }
        return canComplete ? [.clear, .markDone] : [.clear]
    }

    /// The tooltip for one action on this row: a thread's Clear marks nothing read.
    func help(_ action: NeedsYouRowAction) -> String {
        action == .clear && route == NeedsYouRow.threadRoute
            ? "Hide this from the Brief. Nothing is marked read; a new message brings it back."
            : action.help(text)
    }

    /// What a Clear hides: this id while it still says the same thing.
    var dismissalKey: String { id + "#" + (fingerprint ?? "") }
}

/// The items selected by the service plus its count of additional items.
struct NeedsYouList: Equatable {
    let rows: [NeedsYouRow]
    let more: Int
}

/// One to-do from the proactive engine (`workspace.proactive.todos`), as the
/// Day tab hands it over.
struct DayTodoInput: Equatable {
    var id: String
    var title: String
    /// "mail", "messages", "calendar", "reminders" or "notes".
    var source: String
    var who: String = ""
    var due: Double? = nil
    var dueLabel: String = ""
    /// The engine's kind; "event", "meeting", "call" and "plan" happen at their time.
    var kind: String = ""
    var done = false
}

/// One row of the Day tab's "Still to fit": a task the planner couldn't place,
/// or a to-do with no time today.
struct DayFitRow: Equatable, Identifiable {
    let id: String
    let title: String
    let detail: String
    /// An SF Symbol for where it came from.
    let symbol: String
    /// Set for a to-do, so the row can check it off or add it to the day.
    var todoID: String? = nil
}

/// One "Caught for you" record from the attention runner.
struct DayCaughtInput: Equatable {
    var id: String
    var title: String
    var when: Double?
    /// "shadow" (would add, preview) or "created" (added to Reminders). Others are not drawn.
    var state: String
}

/// A block on the timeline.
struct DayItem: Identifiable, Equatable {
    let id: String
    let title: String
    let detail: String
    var start: Double
    var end: Double
    let style: DayStyle
    /// What the block came from, for its actions.
    var blockID: String? = nil
    var taskID: String? = nil
    var patternID: String? = nil
    var needID: String? = nil
    var caughtID: String? = nil
    /// For a suggestion: the "Needs you" item it was made from.
    var evidenceID: String? = nil
    /// "behavior_away" for a learned away window.
    var kind: String = ""
    /// For a grouped review suggestion: the items one Keep plans together.
    var members: [DayMember] = []
    /// For a to-do: its id and where it came from ("mail", "messages", …).
    var todoID: String? = nil
    var source: String = ""
    /// Over before now, on today: drawn dim, inside the lane's two hours back.
    var past = false
    /// For a reminder or to-do due by a time: that time, which the agenda lists
    /// it at. Nil for what holds a stretch of time or happens at a time.
    var due: Double? = nil
    /// The block's own times before the lane clipped them to its hours, for the
    /// agenda's time column. `ends` is nil when the source gave no end.
    var begins: Double? = nil
    var ends: Double? = nil

    /// Whether the block is drawn as over: dim, with nothing left to act on. A
    /// block that ended keeps its controls while a cancel on it is still in its
    /// Undo window or being sent: the person tapped Cancel while it was still
    /// on, and the clock passing its end must not take Undo away mid-window
    /// (or hide the spinner while the service works). The ⋯ menu never offers a
    /// new Cancel on a past block (CheckInCancel.menuEntries), so this only
    /// keeps a cancel that began in time.
    func isOver(cancelActive: Bool) -> Bool { past && !cancelActive }
}

/// The Day tab's calendar as a list (DayTimeline.agenda). The notch is too
/// narrow for an hour grid: overlapping events squeezed side by side lost
/// their titles, so each thing gets a full-width row instead.
struct DayAgenda: Equatable {
    enum Row: Equatable, Identifiable {
        case item(DayItem)
        /// Time nothing holds, before the next thing that does.
        case free(start: Double, end: Double)

        var id: String {
            switch self {
            case .item(let item): return item.id
            case .free(let start, _): return "free:\(Int(start))"
            }
        }
    }

    /// Over before now, on today.
    let earlier: [DayItem]
    /// On now and later, or the whole day on another day.
    let ahead: [Row]

    /// How many things (not free stretches) are listed ahead.
    var thingsAhead: Int { ahead.filter { if case .item = $0 { return true } else { return false } }.count }

    /// The first rows of `ahead` holding at most `limit` things. A free stretch
    /// is never left last, where the thing that ends it would be hidden.
    func firstRows(holding limit: Int) -> [Row] {
        var rows: [Row] = []
        var things = 0
        for row in ahead {
            if case .item = row {
                if things == limit { break }
                things += 1
            }
            rows.append(row)
        }
        if case .free = rows.last { rows.removeLast() }
        return rows
    }
}

struct DayTimeline: Equatable {
    /// Hour-aligned range the lane covers.
    let from: Double
    let to: Double
    /// In order of start.
    let items: [DayItem]
    let allDay: [String]

    static let hour: Double = 3600

    /// Lays the day out on one clock.
    /// - Parameters:
    ///   - now: the current time; on today the lane runs from two hours before
    ///     its hour (never before midnight) to at least three hours after it.
    ///   - dayStart, dayEnd: the day's midnight bounds.
    ///   - workStart, workEnd: working hours, as timestamps on that day.
    ///   - isToday: whether the day shown is the current day.
    ///   - clock: formats a time ("3:30 PM"); `dayClock` formats one on another day ("Fri 5 PM").
    static func build(blocks: [DayBlockInput], allDay: [String], needs: [DayNeedInput], caught: [DayCaughtInput],
                      now: Double, dayStart: Double, dayEnd: Double, workStart: Double, workEnd: Double,
                      isToday: Bool, clock: (Double) -> String, dayClock: (Double) -> String,
                      todos: [DayTodoInput] = []) -> DayTimeline {
        // On today, two hours back so what just happened stays in view, dim.
        let earliest = isToday
            ? max(dayStart, floorHour(min(max(now, dayStart), dayEnd - hour), dayStart: dayStart) - 2 * hour) : dayStart
        var items: [DayItem] = []

        for block in blocks {
            let given = block.end.flatMap { $0 > block.start ? $0 : nil }
            let end = given ?? block.start + 30 * 60
            // A calendar event is real whatever it's called ("1:1", an emoji, no
            // title at all); only a placeholder word ("nothing", "n/a") is left off.
            let fromCalendar = block.kind == "calendar" || block.kind == "manual"
            guard end > earliest, block.start < dayEnd,
                  fromCalendar ? !isPlaceholderEvent(block.title) : !isJunk(block.title) else { continue }
            let minutes = Int(((end - block.start) / 60).rounded())
            let style: DayStyle
            let detail: String
            switch block.kind {
            case "calendar", "manual":
                style = .calendar
                detail = block.end == nil ? "From " + clock(block.start) : clock(block.start) + " – " + clock(end)
            case "suggestion":
                style = .suggested
                detail = block.members.count > 1
                    ? "Suggested · \(block.members.count) to read · about \(duration(minutes))"
                    : "Suggested · about \(duration(minutes))"
            case "behavior":
                style = .usual
                detail = "Usually busy · not on your calendar"
            case "behavior_away":
                style = .usual
                detail = "Usually away · not on your calendar"
            default:
                style = block.taskID == nil ? .calendar : .task
                detail = style == .task ? "Your plan · \(duration(minutes))" : clock(block.start) + " – " + clock(end)
            }
            items.append(DayItem(id: "block:" + block.id, title: fromCalendar ? eventTitle(block.title) : block.title, detail: detail,
                                 start: block.start, end: end, style: style, blockID: block.id,
                                 taskID: block.taskID, patternID: block.patternID,
                                 evidenceID: block.evidenceID, kind: block.kind, members: block.members,
                                 begins: block.start, ends: given))
        }

        // A reminder that already has a suggested block is drawn once, as the suggestion.
        let suggested = Set(blocks.compactMap { $0.kind == "suggestion" ? $0.evidenceID : nil })
        for need in needs where need.source != "calendar" && !isJunk(need.title) && !suggested.contains(need.id) {
            guard let due = need.due else { continue }
            if isToday && due < now {
                let start = max(earliest, now)
                items.append(DayItem(id: "need:" + need.id, title: need.title, detail: "Overdue since " + overdueLabel(due, dayStart: dayStart, clock: clock, dayClock: dayClock),
                                     start: start, end: start + 30 * 60, style: .overdue, needID: need.id))
            } else if due >= dayStart, due <= dayEnd, due > earliest {
                let start = max(earliest, due - 30 * 60)
                items.append(DayItem(id: "need:" + need.id, title: need.title, detail: "Due " + clock(due) + " · Needs you",
                                     start: start, end: max(due, start + 15 * 60), style: .needsYou, needID: need.id,
                                     due: due))
            }
        }

        // A first day can bring a long overdue backlog. Listed one by one at
        // "now" they would push the rest of the day out of view, so the agenda
        // shows the three most recent and one row for the rest.
        let overdueNeeds = items.indices.filter { items[$0].style == .overdue && items[$0].needID != nil }
        if overdueNeeds.count > maxOverdueShown {
            let start = max(earliest, now)
            let rest = overdueNeeds.count - (maxOverdueShown - 1)
            let keep = Set(overdueNeeds.prefix(maxOverdueShown - 1))
            items = items.indices.filter { !overdueNeeds.contains($0) || keep.contains($0) }.map { items[$0] }
            items.append(DayItem(id: "need:overdue-more", title: "\(rest) more overdue",
                                 detail: "Older reminders · open Reminders to see them",
                                 start: start, end: start + 30 * 60, style: .overdue, source: "reminders"))
        }

        // A plan Wisp already added to Reminders comes back as a reminder; draw it once.
        let reminderTitles = Set(needs.map { $0.title.lowercased() })
        for record in caught where !isJunk(record.title) && (record.state == "shadow" || record.state == "created")
            && !(record.state == "created" && reminderTitles.contains(record.title.lowercased())) {
            guard let when = record.when, when >= dayStart, when < dayEnd, when + hour > earliest else { continue }
            let detail = record.state == "created" ? "Caught in texts · added to Reminders" : "Caught in texts · not on your calendar"
            items.append(DayItem(id: "caught:" + record.id, title: record.title, detail: detail,
                                 start: when, end: when + hour, style: .caught, caughtID: record.id, begins: when))
        }

        // To-dos with a time today go on the clock, once: not calendar events
        // (already blocks), not what's done, nor one a block or reminder already draws.
        let drawn = Set(blocks.map { todoKey($0.title) } + needs.map { todoKey($0.title) })
        for todo in todos where !todo.done && todo.source != "calendar" && !isJunk(todo.title)
            && !drawn.contains(todoKey(todo.title)) {
            guard let due = todo.due, isTimed(due, dayStart: dayStart, dayEnd: dayEnd) else { continue }
            let from = [sourceName(todo.source), todo.who].filter { !$0.isEmpty }.joined(separator: " · ")
            if isToday && due < now {
                let start = max(earliest, now)
                items.append(DayItem(id: "todo:" + todo.id, title: todo.title,
                                     detail: "Overdue since " + clock(due) + (from.isEmpty ? "" : " · " + from),
                                     start: start, end: start + 30 * 60, style: .overdue, todoID: todo.id, source: todo.source))
            } else if due > earliest {
                let atTime = happensAtTime(todo.kind)
                let start = atTime ? due : max(earliest, due - 30 * 60)
                let end = atTime ? due + 30 * 60 : max(due, start + 15 * 60)
                let when = (atTime ? "At " : "Due ") + clock(due)
                items.append(DayItem(id: "todo:" + todo.id, title: todo.title,
                                     detail: [when, from].filter { !$0.isEmpty }.joined(separator: " · "),
                                     start: start, end: end, style: .todo, todoID: todo.id, source: todo.source,
                                     due: atTime ? nil : due, begins: atTime ? due : nil))
            }
        }

        let latest = items.map(\.end).max() ?? earliest
        var to = ceilHour(max(earliest + 4 * hour, isToday ? max(workEnd, now + 3 * hour) : workEnd, latest), dayStart: dayStart)
        to = min(to, dayEnd)
        var from = earliest
        if !isToday {
            let first = items.map(\.start).min() ?? workStart
            from = max(dayStart, floorHour(min(workStart, first), dayStart: dayStart))
        }
        if to <= from { to = min(dayEnd, from + hour) }

        for index in items.indices {
            items[index].start = max(items[index].start, from)
            items[index].end = min(max(items[index].end, items[index].start + 15 * 60), to)
            items[index].past = isToday && items[index].end <= now
        }
        items = items.filter { $0.end > $0.start }
            .sorted { ($0.start, $0.end, $0.style, $0.id) < ($1.start, $1.end, $1.style, $1.id) }
        return DayTimeline(from: from, to: to, items: items,
                           allDay: allDay.filter { !isPlaceholderEvent($0) }.map(eventTitle))
    }

    /// The most overdue reminders the agenda lists one by one.
    static let maxOverdueShown = 3

    /// A calendar entry that is only a placeholder word ("nothing", "n/a", "tbd").
    /// Unlike isJunk, a title with no letters ("1:1", an emoji) or no title at
    /// all is a real event.
    static func isPlaceholderEvent(_ title: String) -> Bool {
        let word = title.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters)).lowercased()
        return !word.isEmpty && word != "untitled" && junkTitles.contains(word)
    }

    /// A calendar event's title as shown: "(untitled)" when blank.
    static func eventTitle(_ title: String) -> String {
        title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "(untitled)" : title
    }

    // MARK: To-dos

    /// A to-do's due time names a moment on this day, not just the day: date-only
    /// to-dos come as midnight or the day's last minute.
    static func isTimed(_ due: Double, dayStart: Double, dayEnd: Double) -> Bool {
        due > dayStart && due < dayEnd - 60
    }

    /// Kinds that happen at their time (start there) rather than being due by it.
    static func happensAtTime(_ kind: String) -> Bool {
        ["event", "meeting", "call", "plan", "appointment"].contains(kind.lowercased())
    }

    /// One spelling for comparing titles: lowercased letters and digits only.
    static func todoKey(_ title: String) -> String {
        String(title.lowercased().unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) })
    }

    /// The SF Symbol for a to-do's or summary's source.
    static func sourceSymbol(_ source: String) -> String {
        switch source.lowercased() {
        case "mail", "email": return "envelope"
        case "messages", "text", "texts": return "message"
        case "calendar": return "calendar"
        case "reminders": return "checklist"
        case "notes": return "note.text"
        case "wisp", "plan": return "clock"
        default: return "circle.dashed"
        }
    }

    /// Where a to-do came from, in a word.
    static func sourceName(_ source: String) -> String {
        switch source.lowercased() {
        case "mail", "email": return "Mail"
        case "messages", "text", "texts": return "Texts"
        case "calendar": return "Calendar"
        case "reminders": return "Reminders"
        case "notes": return "Notes"
        default: return ""
        }
    }

    /// The Day tab's "Still to fit": tasks the planner couldn't place, then
    /// to-dos with no time today (undated, dated today without a time, or past
    /// due from an earlier day), each once. Calendar events, done to-dos and
    /// to-dos for a later day are left out; so is anything already on the day.
    static func stillToFit(todos: [DayTodoInput], unscheduled: [(id: String, title: String, reason: String)],
                           blocks: [DayBlockInput], needs: [DayNeedInput] = [], now: Double, dayStart: Double,
                           dayEnd: Double, isToday: Bool, dayClock: (Double) -> String) -> [DayFitRow] {
        var rows: [DayFitRow] = []
        // A reminder the lane already draws as Needs you isn't still to fit.
        var seen = Set(blocks.map { todoKey($0.title) }
                       + needs.filter { $0.due.map { $0 >= dayStart && $0 < dayEnd } ?? false }.map { todoKey($0.title) })
        for task in unscheduled where !isJunk(task.title) && !seen.contains(todoKey(task.title)) {
            seen.insert(todoKey(task.title))
            rows.append(DayFitRow(id: "task:" + task.id, title: task.title, detail: plainReason(task.reason).capitalizedFirst,
                                  symbol: sourceSymbol("wisp")))
        }
        for todo in todos where !todo.done && todo.source != "calendar" && !isJunk(todo.title)
            && !seen.contains(todoKey(todo.title)) {
            var due = ""
            if let at = todo.due {
                if isTimed(at, dayStart: dayStart, dayEnd: dayEnd) { continue }   // on the clock
                if at >= dayEnd { continue }                                         // a later day's
                if at < dayStart {
                    guard isToday else { continue }
                    due = "Overdue since " + dayClock(at)
                } else {
                    due = todo.dueLabel.isEmpty ? (isToday ? "Today" : "That day") : todo.dueLabel
                }
            } else {
                due = todo.dueLabel
            }
            seen.insert(todoKey(todo.title))
            rows.append(DayFitRow(id: "todo:" + todo.id, title: todo.title,
                                  detail: [todo.who, due].filter { !$0.isEmpty }.joined(separator: " · "),
                                  symbol: sourceSymbol(todo.source), todoID: todo.id))
        }
        return rows
    }

    // MARK: Agenda

    /// The shortest free stretch the agenda names. One that starts now counts
    /// from 15 minutes: "free until 2 PM" is what you act on next.
    static let shortestFree: Double = 30 * 60
    static let shortestFreeNow: Double = 15 * 60

    /// Whether a row holds a stretch of the day, so the time it covers isn't
    /// free. Overdue and due-by rows are asks pinned to a moment; a to-do that
    /// happens at its time (a call, a meeting) holds its half hour.
    static func holdsTime(_ item: DayItem) -> Bool {
        switch item.style {
        case .calendar, .task, .suggested, .usual, .caught: return true
        case .todo: return item.due == nil && item.begins != nil
        case .needsYou, .overdue: return false
        }
    }

    /// The day as a list: what's over, then what's on now and next in time
    /// order, each on its own row, with the free stretches between. Something
    /// due by a time is listed at that time; an overdue one at now.
    func agenda(now: Double, isToday: Bool) -> DayAgenda {
        let earlier = isToday ? items.filter(\.past) : []
        let ahead = isToday ? items.filter { !$0.past } : items
        var rows: [(at: Double, free: Bool, row: DayAgenda.Row)] = ahead.map { ($0.due ?? $0.start, false, .item($0)) }
        // Free time counts from now on today, from the first thing on another day.
        let opening = isToday ? max(now, from) : nil
        var cursor = opening
        for item in ahead where Self.holdsTime(item) {
            if let start = cursor, item.start - start >= (start == opening ? Self.shortestFreeNow : Self.shortestFree) {
                rows.append((start, true, .free(start: start, end: item.start)))
            }
            cursor = max(cursor ?? item.end, item.end)
        }
        // A free stretch follows what's listed at its start; ties keep their order.
        let ordered = rows.enumerated().sorted {
            ($0.element.at, $0.element.free ? 1 : 0, $0.offset) < ($1.element.at, $1.element.free ? 1 : 0, $1.offset)
        }
        return DayAgenda(earlier: earlier, ahead: ordered.map(\.element.row))
    }

    /// The agenda's time column for a row, as two short lines: "6:30 PM" over
    /// "7:30 PM", "8:00 PM" over "Due", or "Overdue".
    static func gutter(_ item: DayItem, clock: (Double) -> String) -> (top: String, bottom: String) {
        if item.style == .overdue { return ("Overdue", "") }
        if let due = item.due { return (clock(due), "Due") }
        return (clock(item.begins ?? item.start), item.ends.map(clock) ?? "")
    }

    /// A row's second line in the agenda, without the time its column shows:
    /// what kind of thing it is, and for an event on now, how long is left.
    static func agendaDetail(_ item: DayItem, now: Double, isToday: Bool, clock: (Double) -> String) -> String {
        switch item.style {
        case .calendar:
            guard let ends = item.ends else { return "Calendar" }
            let begins = item.begins ?? item.start
            if isToday && begins <= now && now < ends {
                return "Calendar · " + length(Int(((ends - now) / 60).rounded(.up))) + " left"
            }
            return "Calendar · " + length(Int(((ends - begins) / 60).rounded()))
        case .overdue:
            let lead = "Overdue since "
            return item.detail.hasPrefix(lead) ? "Since " + item.detail.dropFirst(lead.count) : item.detail
        case .needsYou:
            return "Needs you"
        case .todo:
            // "Due 3:00 PM · Mail · Jordan" keeps where it came from.
            guard let at = item.due ?? item.begins else { return item.detail }
            for lead in ["Due ", "At "] where item.detail.hasPrefix(lead + clock(at)) {
                let rest = item.detail.dropFirst((lead + clock(at)).count)
                return rest.hasPrefix(" · ") ? String(rest.dropFirst(3)) : "To-do"
            }
            return item.detail
        case .task, .suggested, .usual, .caught:
            return item.detail
        }
    }

    /// "Free until 6:30 PM · 25 min" from now; "Free 2:00 PM – 6:00 PM · 4 h"
    /// later, since the row above may end before the stretch starts.
    static func freeLine(start: Double, end: Double, startsNow: Bool, clock: (Double) -> String) -> String {
        (startsNow ? "Free until " + clock(end) : "Free " + clock(start) + " – " + clock(end))
            + " · " + length(Int(((end - start) / 60).rounded()))
    }

    /// What ended in the last two hours, in one line: "Standup and 2 more".
    static func earlierLine(_ items: [DayItem]) -> String { list(items.map(\.title)) }

    /// "25 min", "1 h", "2 h 15 min".
    static func length(_ minutes: Int) -> String {
        minutes < 60 ? "\(minutes) min" : "\(minutes / 60) h" + (minutes % 60 == 0 ? "" : " \(minutes % 60) min")
    }

    static func floorHour(_ t: Double, dayStart: Double) -> Double {
        dayStart + ((t - dayStart) / hour).rounded(.down) * hour
    }

    static func ceilHour(_ t: Double, dayStart: Double) -> Double {
        dayStart + ((t - dayStart) / hour).rounded(.up) * hour
    }

    static func duration(_ minutes: Int) -> String {
        if minutes < 60 { return "\(minutes) min" }
        let hours = Double(minutes) / 60
        return hours == hours.rounded() ? "\(Int(hours)) h" : String(format: "%.1f h", hours)
    }

    private static func overdueLabel(_ due: Double, dayStart: Double, clock: (Double) -> String,
                                     dayClock: (Double) -> String) -> String {
        due >= dayStart ? clock(due) : dayClock(due)
    }

    // MARK: Calm rules

    /// Titles that aren't a thing to do: empty, punctuation, or a placeholder
    /// like "nothing" that a reminder or a parsed text sometimes holds.
    static func isJunk(_ title: String) -> Bool {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters)).lowercased()
        // One Latin letter is a stray keystroke; one CJK character can be a word.
        guard trimmed.contains(where: \.isLetter), trimmed.count >= 2 || !trimmed.allSatisfy(\.isASCII) else { return true }
        return junkTitles.contains(trimmed)
    }

    static let junkTitles: Set<String> = [
        "nothing", "none", "n/a", "na", "null", "nil", "no", "yes", "ok", "okay",
        "untitled", "new reminder", "tbd", "todo", "to do", "asdf", "idk", "nothing really",
        "nothing much", "not sure", "no plans",
    ]

    /// The badge: "Needs you" items due today or already past due. Calendar
    /// events aren't due, and undated items wait in the Brief.
    static func badgeCount(_ needs: [DayNeedInput], endOfDay: Double) -> Int {
        needs.filter { $0.source != "calendar" && !isJunk($0.title) && ($0.due.map { $0 <= endOfDay } ?? false) }.count
    }

    /// A planner warning in plain words, or nil for one the timeline already
    /// says by how it draws the block.
    static func plainWarning(_ warning: String) -> String? {
        let w = warning.trimmingCharacters(in: .whitespaces)
        if w.isEmpty || w.hasPrefix("Tentative") || w.hasPrefix("Suggested time") { return nil }
        if w.hasPrefix("Calendar is not current") {
            return "Wisp is still reading your calendar, so I'm waiting before placing tasks."
        }
        if w.hasPrefix("Older calendar events have unknown end times") {
            return "A few earlier events have no end time, so these times are rough."
        }
        if w.hasPrefix("Some fixed events have no end time") {
            return "Some events have no end time, so I'm waiting before placing tasks."
        }
        if w.hasPrefix("All-day calendar events may reserve this day") {
            return "An all-day event looks like time away, so I haven't placed your tasks."
        }
        if w.hasPrefix("Reminders are not current") {
            return "Reminders haven't synced, so a deadline could be missing."
        }
        if w.hasPrefix("Pinned time is in the past") { return "This time has passed. Move it or mark it done." }
        if w.hasPrefix("Pinned outside working hours") { return "Outside your working hours." }
        if w.hasPrefix("Pinned block ends after its deadline") { return "Ends after it's due." }
        if w.hasPrefix("Pinned on a day with an all-day event") { return "There's an all-day event today. Check you're free." }
        if w.hasPrefix("Fixed event has no end time") { return "No end time on your calendar." }
        if w.hasPrefix("Continues from the previous day") { return "Started yesterday." }
        if w.hasPrefix("Overlaps ") { return "Overlaps " + w.dropFirst("Overlaps ".count) + "." }
        return w
    }

    /// "It's after your working hours (9 AM–6 PM)."
    static func afterHoursLine(start: Int, end: Int, locale: Locale = WispClock.fixedLocale) -> String {
        func label(_ minute: Int) -> String {
            let hour = minute / 60, rest = minute % 60
            if WispClock.uses24Hour(locale) { return String(format: "%02d:%02d", hour % 24, rest) }
            let shown = hour % 12 == 0 ? 12 : hour % 12
            return (rest == 0 ? "\(shown)" : String(format: "%d:%02d", shown, rest)) + (hour % 24 < 12 ? " AM" : " PM")
        }
        return "It's after your working hours (\(label(start))–\(label(end))), so nothing new is placed today."
    }

    /// Why a task couldn't be placed, in plain words.
    static func plainReason(_ reason: String) -> String {
        if reason.hasPrefix("All-day event needs") { return "an all-day event looks like time away" }
        if reason.hasPrefix("It's after your working hours") { return "it's after your working hours" }
        if reason.hasPrefix("Waiting for a current calendar") { return "waiting until Wisp can read your calendar" }
        if reason.hasPrefix("Needs "), let gap = reason.range(of: "is ")?.upperBound {
            let largest = reason[gap...].split(separator: " ").first.map(String.init) ?? ""
            return "no long enough gap" + (largest.isEmpty ? "" : " (the longest free stretch is \(largest) min)")
        }
        return reason
    }

    // MARK: Words

    static func count(_ n: Int) -> String {
        ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"].indices.contains(n)
            ? ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"][n] : String(n)
    }

    private static func list(_ titles: [String]) -> String {
        switch titles.count {
        case 0: return ""
        case 1: return titles[0]
        case 2: return titles[0] + " and " + titles[1]
        default: return titles[0] + " and \(titles.count - 1) more"
        }
    }

    /// The Day tab's blurb, at most two short sentences and a tail: what's up
    /// today and why the day is laid out the way it is.
    static func blurb(_ timeline: DayTimeline, needs: [DayNeedInput], isToday: Bool, endOfDay: Double,
                      clock: (Double) -> String, dayStart: Double? = nil, calendarRead: Bool = true) -> String {
        // What's already over doesn't shape the rest of the day.
        let items = timeline.items.filter { !$0.past }
        let start = dayStart ?? endOfDay - 86400
        let open = needs.filter { n in
            n.source != "calendar" && !isJunk(n.title) && (n.due.map { $0 <= endOfDay } ?? false)
        }
        // What's due today leads; a backlog from earlier days is counted, never
        // called "the main thing".
        let dueToday = open.filter { ($0.due ?? 0) >= start }
        let backlog = open.count - dueToday.count
        let suggestions = items.filter { $0.style == .suggested }
        let usual = items.filter { $0.style == .usual }
        let events = items.filter { $0.style == .calendar || $0.style == .task }
        var first: String
        if !calendarRead && events.isEmpty && dueToday.isEmpty && suggestions.isEmpty {
            // Not "nothing is planned": Wisp hasn't seen the calendar.
            first = "Wisp can't see your calendar yet, so this may not be your whole day."
            if backlog > 0 { first += " \(count(backlog).capitalizedFirst) overdue reminder\(backlog == 1 ? "" : "s") to look at." }
            return first
        }
        if let main = dueToday.first {
            let forMain = suggestions.filter { $0.evidenceID == main.id }.count
            first = "\(main.title) is the main thing \(isToday ? "today" : "that day")."
            if forMain > 0 {
                first += " I've given it \(count(forMain)) block\(forMain == 1 ? "" : "s")"
                if let window = usual.first {
                    first += window.kind == "behavior_away" ? ", around when you're usually away." : ", around when you're usually busy."
                } else {
                    first += "."
                }
            } else if !suggestions.isEmpty {
                first += " I've suggested time for \(list(suggestions.map(\.title)))."
            }
        } else if backlog > 0 {
            first = "\(count(backlog).capitalizedFirst) overdue reminder\(backlog == 1 ? "" : "s") to look at"
                + (events.isEmpty ? "." : ", and \(count(events.count)) thing\(events.count == 1 ? "" : "s") on the clock.")
        } else if !suggestions.isEmpty {
            first = "Nothing is due. I've suggested \(count(suggestions.count)) block\(suggestions.count == 1 ? "" : "s") around your calendar."
        } else {
            first = events.isEmpty
                ? (isToday ? "Nothing is planned for the rest of today." : "Nothing is planned that day.")
                : "\(count(events.count).capitalizedFirst) thing\(events.count == 1 ? "" : "s") on the clock\(isToday ? " for the rest of today" : ""), nothing else planned."
        }
        if let last = items.map(\.end).max(), last < endOfDay - 2 * hour {
            first += " Clear after " + clock(last) + "."
        }
        return first
    }

    // MARK: Brief

    /// The Brief's one sentence, from the day itself. The default opening shares
    /// the CAL verdict. A current-day caller can supply a time-aware opening and
    /// due reference without rewriting its factual clauses or empty state.
    /// `othersNeedYou`: the Needs you line under it lists something (a person's
    /// email or text, a plan task), so the lede never says nothing does.
    static func lede(weekday: String, isToday: Bool, load: DayLoad.Verdict?, dueToday: [String], overdue: [String],
                     next: (title: String, time: String)?, othersNeedYou: Bool = false,
                     openingOverride: String? = nil, dueReference: String? = nil,
                     unread: [String] = [], uncheckedMail: UncheckedMail = UncheckedMail()) -> String {
        var parts: [String] = []
        let due = dueToday.filter { !isJunk($0) }
        let late = overdue.filter { !isJunk($0) }
        if !due.isEmpty {
            parts.append(list(due) + (due.count == 1 ? " is due " : " are due ")
                         + (dueReference ?? (isToday ? "today" : "that day")))
        }
        if !late.isEmpty {
            parts.append(late.count == 1 ? late[0] + " is still open" : "\(count(late.count)) overdue reminders are still open")
        }
        // Without a current calendar there's no verdict to give.
        let opening = openingOverride ?? (load.map { $0.article + " " + weekday + "." } ?? "It's \(weekday).")
        if parts.isEmpty {
            if let next { return opening + " Next up: \(next.title) at \(next.time)." }
            if othersNeedYou { return opening }
            // An all-clear only from sources Wisp has read.
            if !unread.isEmpty { return opening + " " + SourceAccess.cantSee(unread, uncheckedMail: uncheckedMail) }
            return opening + (isToday ? " Nothing needs you right now." : " Nothing needs you that day.")
        }
        return opening + " " + parts.joined(separator: ", and ").capitalizedFirst + "."
    }

    /// The Brief's line under the lede: today's work that needs the user, as the
    /// service picked it (daily_workspace.needs_you, from the same attention
    /// items the Day tab's Needs you reads). "Needs you today: Pay rent
    /// (overdue), PA 2 (due 11:59 PM), a text from Mom and 2 more." Nil when
    /// nothing is listed: the lede already says so, and an empty header says nothing.
    static func needsYou(_ items: [NeedsYouInput], more: Int, clock: (Double) -> String) -> String? {
        let shown = items.filter { !isJunk($0.text) }
        guard !shown.isEmpty else { return nil }
        var phrases = shown.map { item -> String in
            switch item.kind {
            case "overdue": return item.text + " (overdue)"
            case "due":
                guard let due = item.due else { return item.text }
                return item.text + (item.allDay ? " (due today)" : " (due \(clock(due)))")
            default: return item.text
            }
        }
        if more > 0 { phrases.append("\(more) more") }
        let joined = phrases.count == 1 ? phrases[0]
            : phrases.dropLast().joined(separator: ", ") + " and " + phrases[phrases.count - 1]
        return "Needs you today: " + joined + "."
    }

    /// Keeps the service-picked items separate for the Brief's one-item-per-row
    /// presentation. Missing due times stay undated; the label says only "DUE".
    static func needsYouList(_ items: [NeedsYouInput], more: Int, clock: (Double) -> String) -> NeedsYouList? {
        let shown = items.enumerated().filter { !isJunk($0.element.text) }
        guard !shown.isEmpty else { return nil }

        let rows = shown.map { index, item -> NeedsYouRow in
            let due = item.due.flatMap { $0.isFinite ? $0 : nil }
            let label: String?
            switch item.kind {
            case "overdue": label = "OVERDUE"
            case "due":
                if let due {
                    label = item.allDay ? "DUE TODAY" : "DUE " + clock(due)
                } else {
                    label = "DUE"
                }
            case "alert": label = "ALERT"
            case "plan": label = "PLAN"
            case "mail": label = "MAIL"
            case "text": label = "TEXT"
            default: label = nil
            }
            return NeedsYouRow(id: item.id ?? "needs-you-\(index)", label: label,
                               text: item.text, overdue: item.kind == "overdue" || item.kind == "alert",
                               fingerprint: item.fingerprint, canComplete: item.canComplete)
        }
        return NeedsYouList(rows: rows, more: max(0, more))
    }

    enum BriefSource: Equatable { case calendar, mail, messages, reminders }

    struct BriefLine: Equatable, Identifiable {
        let tag: String
        let text: String
        let trailing: String
        let source: BriefSource
        /// A source Wisp can't read yet: shown dim, so the Brief keeps its shape
        /// and says why a row has nothing in it.
        var muted = false
        var id: String { tag }
    }

    /// What the SOON row knows about the coming week.
    enum Soon: Equatable {
        case item(title: String, when: String, source: String)
        /// The week was read and holds nothing.
        case nothing
        /// The week hasn't been read (or couldn't be).
        case unknown
    }

    static func briefSource(_ source: String) -> BriefSource {
        switch source {
        case "calendar", "manual": return .calendar
        case "email", "mail": return .mail
        case "messages": return .messages
        default: return .reminders
        }
    }

    /// Always four rows, CAL, MAIL, TEXTS and SOON, so the Brief keeps its shape.
    /// A source Wisp can't read yet shows a dim row saying so instead of disappearing.
    static func briefLines(calendar: DayLoad?, calendarUnsynced: Bool = false, calendarAccess: SourceAccess? = nil,
                           mail: (ready: Bool, needing: [BriefNeed])?,
                           texts: (ready: Bool, needing: [BriefNeed])?,
                           textsNeedAccess: Bool = false,
                           mailAccess: SourceAccess? = nil, textsAccess: SourceAccess? = nil,
                           mailOverview: String? = nil, textsOverview: String? = nil,
                           soon: Soon) -> [BriefLine] {
        var lines: [BriefLine] = []
        if let calendar {
            lines.append(BriefLine(tag: "CAL", text: calendar.summary, trailing: calendar.next ?? "", source: .calendar))
        } else {
            let unread = calendarAccess.flatMap { $0.readable ? nil : $0 }
            lines.append(BriefLine(tag: "CAL", text: unread?.headline ?? (calendarUnsynced ? "Reading your calendar…" : "Nothing on your calendar"),
                                   trailing: "", source: .calendar, muted: calendarUnsynced || unread != nil))
        }
        // No coverage entry means the workspace couldn't be read: unknown, not off.
        if let mail, mail.ready, let mailOverview {
            // What the inbox is about, and how many emails need you.
            lines.append(BriefLine(tag: "MAIL", text: BriefMail.row(overview: mailOverview, needing: mail.needing),
                                   trailing: "", source: .mail))
        } else if let mail, mail.ready {
            // An older service sends no overview: what the top email is about.
            let shown = BriefMail.shown(mail.needing)
            let promos = BriefMail.bulkCount(mail.needing)
            let hidden = BriefMail.privateCount(mail.needing)
            let text = shown.first?.rowLine
                ?? (hidden > 0 ? "\(hidden) private email\(hidden == 1 ? "" : "s") not shown" : "Nothing needs a reply")
            // Say when more mail or promos were left out, so a hidden email is never silent.
            let trailing = shown.count > 1 ? "+\(shown.count - 1) more"
                : promos > 0 ? "+\(promos) promo\(promos == 1 ? "" : "s")" : ""
            lines.append(BriefLine(tag: "MAIL", text: text, trailing: trailing, source: .mail))
        } else {
            let why = mailAccess.flatMap { $0.readable ? nil : $0.headline } ?? "Mail hasn't been checked yet"
            lines.append(BriefLine(tag: "MAIL", text: why, trailing: "", source: .mail, muted: true))
        }
        if textsNeedAccess {
            // Not "haven't been checked yet": Wisp can't read them until the user grants access.
            lines.append(BriefLine(tag: "TEXTS", text: "Texts need Full Disk Access", trailing: "", source: .messages))
        } else if let texts, texts.ready, let textsOverview {
            // What texts are about and how many people are waiting, never what they wrote.
            lines.append(BriefLine(tag: "TEXTS", text: BriefTexts.row(overview: textsOverview, people: BriefTexts.waiting(texts.needing)),
                                   trailing: "", source: .messages))
        } else if let texts, texts.ready {
            // Who is waiting on you, never what they wrote.
            lines.append(BriefLine(tag: "TEXTS", text: BriefTexts.row(BriefTexts.waiting(texts.needing)), trailing: "",
                                   source: .messages))
        } else {
            let why = textsAccess.flatMap { $0.readable ? nil : $0.headline } ?? "Messages haven't been checked yet"
            lines.append(BriefLine(tag: "TEXTS", text: why, trailing: "", source: .messages, muted: true))
        }
        switch soon {
        case let .item(title, when, source) where !isJunk(title):
            lines.append(BriefLine(tag: "SOON", text: title, trailing: when, source: briefSource(source)))
        case .item, .nothing:
            lines.append(BriefLine(tag: "SOON", text: "Nothing in the next week", trailing: "", source: .reminders))
        case .unknown:
            lines.append(BriefLine(tag: "SOON", text: "The week ahead hasn't loaded yet", trailing: "", source: .reminders, muted: true))
        }
        return lines
    }

    /// What Remove sends for a suggested block: every item a grouped review
    /// covers, or the one item it came from. Nothing for any other block.
    static func removalIDs(_ item: DayItem) -> [String] {
        guard item.style == .suggested else { return [] }
        let members = item.members.map(\.evidenceID)
        return members.isEmpty ? (item.evidenceID.map { [$0] } ?? []) : members
    }

    /// The task Plan time makes from a need: "Review: …" for mail and texts.
    static func plannedTitle(_ title: String, review: Bool) -> String {
        review ? "Review: " + title : title
    }

    /// When a task made from a reminder first mattered: the reminder's due time
    /// once it has passed. A reminder due two weeks ago makes a task two weeks
    /// old on its first day, so the morning carry-forward leaves it behind
    /// instead of carrying it as new. Nil for one not yet due or undated.
    static func planOrigin(due: Double?, now: Double) -> Double? {
        guard let due, due.isFinite, due > 0, due <= now else { return nil }
        return due
    }

    /// Whether the day already has an active task for this need (its title, or
    /// "Review: " and its title), so a second Plan time can't add a duplicate.
    static func alreadyPlanned(_ title: String, tasks: [(title: String, active: Bool)]) -> Bool {
        let key = { (s: String) in s.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
        let wanted: Set<String> = [key(title), key(plannedTitle(title, review: true))]
        return tasks.contains { $0.active && wanted.contains(key($0.title)) }
    }

    /// Mail worth the MAIL row's count: no placeholders, newsletters or promos.
    static func mailNeedingYou(_ subjects: [String]) -> [String] {
        subjects.filter { !isJunk($0) && !isBulkMail($0) }
    }

    /// An unmistakable newsletter, promotion or subscription notice, by its
    /// subject. Deliberately narrow: "Last chance to submit your form" or
    /// "Webinar slides" from a person must still count, so only phrases a person
    /// wouldn't write are here.
    static func isBulkMail(_ subject: String) -> Bool {
        let s = subject.lowercased()
        return bulkMailPhrases.contains { s.contains($0) }
            || s.range(of: #"\b\d{1,2}% off\b"#, options: .regularExpression) != nil
    }

    static let bulkMailPhrases = [
        "you're subscribed", "you\u{2019}re subscribed", "you are subscribed", "subscription confirmed",
        "thanks for subscribing", "thank you for subscribing", "unsubscribe", "newsletter",
        "weekly digest", "daily digest", "flash sale", "free shipping", "promo code",
    ]
}

/// How busy the rest of a day's calendar is, in plain words: "Light day: just
/// PA 2 (all day)", "Busy afternoon: 3 meetings, 1–5 PM", "Packed: back-to-back
/// 10 AM–4 PM", "Free all day".
///
/// Calendar entries only, never learned away windows. The math follows the
/// weekly report (service/behavior/weekly.py): busy time is the union of events
/// clipped to 9 AM–6 PM, and two meetings are back to back when the next starts
/// between 5 minutes before and 10 minutes after the last ends. An event with
/// no end counts as an hour, and makes the times read "about".
struct DayLoad: Equatable {
    enum Verdict: String, Equatable {
        case free, light, steady, busy, packed

        /// The lede's opening: "A light Saturday."
        var article: String {
            switch self {
            case .free: return "A free"
            case .light: return "A light"
            case .steady: return "A steady"
            case .busy: return "A busy"
            case .packed: return "A packed"
            }
        }
    }

    struct Event: Equatable {
        var title: String
        var start: Double
        var end: Double?
    }

    let verdict: Verdict
    /// The CAL line.
    let summary: String
    /// The next event's time, for the line's trailing label.
    let next: String?
    /// Booked hours left in the 9–6 window.
    let occupiedHours: Double
    let meetings: Int
    let backToBack: Int
    /// Longest free stretch left in the 9–6 window, in hours.
    let largestGapHours: Double
    let uncertain: Bool

    static let assumedLength: Double = 3600

    /// - Parameters:
    ///   - from: now on the current day, the day's start otherwise; earlier events are over.
    ///   - dayStart: the day's midnight, for the 9–6 window.
    static func assess(events: [Event], allDay: [String], from: Double, dayStart: Double, isToday: Bool,
                       zone: TimeZone, locale: Locale = WispClock.fixedLocale) -> DayLoad {
        func clock(_ t: Double, zone: TimeZone) -> String { DayLoad.clock(t, zone: zone, locale: locale) }
        func span(_ a: Double, _ b: Double, zone: TimeZone) -> String { DayLoad.span(a, b, zone: zone, locale: locale) }
        let windowStart = at(9, dayStart, zone), windowEnd = at(18, dayStart, zone)
        // An event with no end, or an end before its start, counts as an hour and reads "about".
        func end(_ e: Event) -> Double { e.end.flatMap { $0 > e.start ? $0 : nil } ?? e.start + assumedLength }
        func guessed(_ e: Event) -> Bool { e.end.map { $0 <= e.start } ?? true }
        let left = events.filter { !DayTimeline.isPlaceholderEvent($0.title) && end($0) > from }
            .map { Event(title: DayTimeline.eventTitle($0.title), start: $0.start, end: $0.end) }
            .sorted { $0.start < $1.start }
        let allDayTitles = allDay.filter { !DayTimeline.isPlaceholderEvent($0) }.map(DayTimeline.eventTitle)
        let uncertain = left.contains(where: guessed)

        let lo = max(from, windowStart)
        let busy = union(left.compactMap { e -> (Double, Double)? in
            let a = max(lo, e.start), b = min(windowEnd, end(e))
            return b > a ? (a, b) : nil
        })
        let occupied = busy.reduce(0) { $0 + $1.1 - $1.0 } / 3600
        var gap = 0.0, cursor = lo
        for (a, b) in busy { gap = max(gap, a - cursor); cursor = max(cursor, b) }
        if windowEnd > cursor { gap = max(gap, windowEnd - cursor) }
        let largestGap = max(0, gap) / 3600

        // Back-to-back chains: runs of meetings that follow each other. The
        // longest one (the first, on a tie) is the "back-to-back" span.
        var pairs = 0
        var chains: [(start: Double, end: Double, length: Int)] = []
        for event in left {
            if let last = chains.last, (-300...600).contains(event.start - last.end) {
                pairs += 1
                chains[chains.count - 1] = (last.start, max(last.end, end(event)), last.length + 1)
            } else {
                chains.append((event.start, end(event), 1))
            }
        }
        let longest = chains.reduce(nil as (start: Double, end: Double, length: Int)?) { best, chain in
            chain.length > (best?.length ?? 0) ? chain : best
        }
        let bestLength = longest?.length ?? 0

        let about = uncertain ? "about " : ""
        let next = left.first { $0.start > from }.map { clock($0.start, zone: zone) }
        let verdict: Verdict
        let summary: String
        let n = left.count
        let meetings = "\(n) meeting\(n == 1 ? "" : "s")"
        let lastEnd = left.map(end).max() ?? 0
        if n == 0 {
            if let first = allDayTitles.first {
                verdict = .light
                summary = "Light day: just \(first) (all day)" + (allDayTitles.count > 1 ? " and \(allDayTitles.count - 1) more" : "")
            } else {
                verdict = .free
                summary = "Free " + (isToday && from > windowStart ? "for the rest of today" : "all day")
            }
        } else if occupied >= 5 || (bestLength >= 3 && largestGap < 1) {
            verdict = .packed
            if let chain = longest, chain.length >= 3 {
                summary = "Packed: back-to-back " + about + span(chain.start, chain.end, zone: zone)
            } else {
                summary = "Packed: \(meetings), " + about + hours(occupied) + " booked"
            }
        } else if occupied >= 3 || n >= 4 {
            verdict = .busy
            summary = "Busy \(part(left.map(\.start), left.map(end), dayStart: dayStart, zone: zone)): \(meetings), "
                + about + span(left[0].start, lastEnd, zone: zone)
        } else if occupied < 1.5 && n <= 2 {
            verdict = .light
            summary = n == 1
                ? "Light day: just \(left[0].title) at " + clock(left[0].start, zone: zone) + (uncertain ? ", end time unknown" : "")
                : "Light day: 2 meetings, " + about + span(left[0].start, lastEnd, zone: zone)
        } else {
            verdict = .steady
            // Nothing booked in what's left of 9-6 (late in the day, or only
            // evening meetings): say when they start rather than "0 h booked".
            summary = "Steady: \(meetings)" + (lo >= windowEnd || occupied < 0.25
                ? " from " + clock(max(from, left[0].start), zone: zone)
                : largestGap >= 1 ? ", longest free stretch " + about + hours(largestGap)
                : ", " + about + hours(occupied) + " booked")
        }
        return DayLoad(verdict: verdict, summary: summary, next: next, occupiedHours: occupied, meetings: n,
                       backToBack: pairs, largestGapHours: largestGap, uncertain: uncertain)
    }

    static func union(_ intervals: [(Double, Double)]) -> [(Double, Double)] {
        var merged: [(Double, Double)] = []
        for (a, b) in intervals.sorted(by: { $0.0 < $1.0 }) where b > a {
            if let last = merged.last, a <= last.1 { merged[merged.count - 1].1 = max(b, last.1) }
            else { merged.append((a, b)) }
        }
        return merged
    }

    /// "morning", "afternoon", "evening" when every remaining meeting falls in one, else "day".
    static func part(_ starts: [Double], _ ends: [Double], dayStart: Double, zone: TimeZone) -> String {
        let noon = at(12, dayStart, zone), five = at(17, dayStart, zone)
        if ends.allSatisfy({ $0 <= noon }) { return "morning" }
        if starts.allSatisfy({ $0 >= five }) { return "evening" }
        if starts.allSatisfy({ $0 >= noon }) { return "afternoon" }
        return "day"
    }

    /// That hour of the day on the wall clock, so a daylight-saving day is right.
    static func at(_ hour: Int, _ dayStart: Double, _ zone: TimeZone) -> Double {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let day = Date(timeIntervalSince1970: dayStart + 12 * 3600)
        return calendar.date(bySettingHour: hour, minute: 0, second: 0, of: day)?.timeIntervalSince1970
            ?? dayStart + Double(hour) * 3600
    }

    static func hours(_ h: Double) -> String {
        let rounded = (h * 2).rounded() / 2
        return rounded == rounded.rounded() ? "\(Int(rounded)) h" : String(format: "%.1f h", rounded)
    }

    private static func parts(_ t: Double, zone: TimeZone) -> (hour: Int, minute: Int) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let c = calendar.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: t))
        return (c.hour ?? 0, c.minute ?? 0)
    }

    private static func bare(_ t: Double, zone: TimeZone) -> (text: String, pm: Bool) {
        let (h, m) = parts(t, zone: zone)
        let shown = h % 12 == 0 ? 12 : h % 12
        return (m == 0 ? "\(shown)" : String(format: "%d:%02d", shown, m), h >= 12)
    }

    /// "3 PM", "3:30 PM"; "15:00", "15:30" in a 24-hour locale.
    static func clock(_ t: Double, zone: TimeZone, locale: Locale = WispClock.fixedLocale) -> String {
        if WispClock.uses24Hour(locale) {
            let (h, m) = parts(t, zone: zone)
            return String(format: "%02d:%02d", h, m)
        }
        let b = bare(t, zone: zone)
        return b.text + (b.pm ? " PM" : " AM")
    }

    /// "1–5 PM", "10 AM–4 PM"; "13:00–17:00" in a 24-hour locale.
    static func span(_ a: Double, _ b: Double, zone: TimeZone, locale: Locale = WispClock.fixedLocale) -> String {
        if WispClock.uses24Hour(locale) { return clock(a, zone: zone, locale: locale) + "–" + clock(b, zone: zone, locale: locale) }
        let x = bare(a, zone: zone), y = bare(b, zone: zone)
        return x.pm == y.pm ? x.text + "–" + y.text + (y.pm ? " PM" : " AM")
            : clock(a, zone: zone, locale: locale) + "–" + clock(b, zone: zone, locale: locale)
    }
}

/// Whether Wisp could read one of the day's sources, and the one thing that
/// fixes it when it couldn't. The Brief and Day say "Wisp can't see your
/// calendar yet" with that setting, never a bare "nothing" when the real reason
/// is that a source can't be read.
///
/// From the plan's `sources` (Calendar, Reminders: service/assistant/today.py
/// source_access) or the workspace's `source_coverage` (every source).
struct SourceAccess: Equatable {
    enum State: String, Equatable { case ready, syncing, unavailable }

    /// "calendar", "reminders", "email", "messages" or "notes".
    let source: String
    let state: State
    /// What the person must grant, when the service knows: "calendar_access",
    /// "reminders_access", "full_disk_access", "mail_automation",
    /// "mail_not_configured", "no_accounts", "mail_closed", "notes_closed".
    var blocked: String? = nil
    /// The service's one sentence about it, when it sent one.
    var reason: String = ""

    /// Connected Google accounts Wisp couldn't check (blocked "google_mail").
    var unchecked = UncheckedMail()

    var readable: Bool { state == .ready }

    /// - Parameters:
    ///   - ready: the service's readiness flag.
    ///   - state: its "syncing" or "unavailable", when it sent one; a source
    ///     that isn't ready and says nothing more is treated as unavailable, so
    ///     it can never read as an all-clear.
    init(source: String, ready: Bool, state: String? = nil, blocked: String? = nil, reason: String? = nil) {
        self.source = source
        self.state = ready ? .ready : (state == "syncing" ? .syncing : .unavailable)
        self.blocked = blocked.flatMap { $0.isEmpty ? nil : $0 }
        self.reason = ready ? "" : (reason ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// How the source is named in a sentence: "calendar", "reminders", "mail", "texts", "notes".
    static func noun(_ source: String) -> String {
        switch source {
        case "calendar": return "calendar"
        case "reminders": return "reminders"
        case "email", "mail": return "mail"
        case "messages", "texts": return "texts"
        case "notes": return "notes"
        default: return source
        }
    }

    var noun: String { Self.noun(source) }

    /// Mail as the Brief shows it, by one rule everywhere (TodayPlan.access,
    /// ProactiveState.unreadSources): fully read only when no connected Google
    /// account was left unchecked (`unreadBy`, the service's mail_unread_by)
    /// and either Mail.app's row is ready or a Google account read it
    /// (`readBy`, mail_read_by). One account unchecked: partly read, so not
    /// readable, and its headline names the account.
    static func mail(_ row: SourceAccess, readBy: [String], unchecked: UncheckedMail) -> SourceAccess {
        if !unchecked.isEmpty {
            var partly = SourceAccess(source: "email", ready: false, state: "unavailable", blocked: "google_mail",
                                      reason: unchecked.detail)
            partly.unchecked = unchecked
            return partly
        }
        if !row.readable && !readBy.isEmpty { return SourceAccess(source: "email", ready: true) }
        return row
    }

    /// The top line for unread sources, naming Google accounts Wisp couldn't
    /// check when mail is only partly read.
    static func cantSee(_ sources: [String], uncheckedMail: UncheckedMail) -> String {
        guard !uncheckedMail.isEmpty, sources.contains("email") else { return cantSee(sources) }
        let others = cantSee(sources.filter { $0 != "email" })
        return (others.isEmpty ? "" : others + " ") + uncheckedMail.sentence
    }

    /// The short line a row shows.
    var headline: String {
        switch state {
        case .ready: return ""
        case .syncing: return "Reading your \(noun)…"
        case .unavailable:
            switch blocked {
            case "mail_not_configured", "no_accounts": return "No Mail accounts on this Mac"
            case "mail_closed": return "Wisp reads Mail while Mail is open"
            case "notes_closed": return "Wisp reads Notes while Notes is open"
            case "google_mail": return unchecked.headline
            case .some: return "Wisp can't see your \(noun) yet"
            case .none: return "Wisp couldn't read your \(noun) just now"
            }
        }
    }

    /// The sentence under it: the service's reason, else what to do.
    var detail: String {
        if !reason.isEmpty { return reason }
        switch state {
        case .ready: return ""
        case .syncing: return "This takes a few seconds after Wisp starts."
        case .unavailable:
            switch blocked {
            case "calendar_access": return "Turn on Calendars for Wisp in System Settings > Privacy & Security."
            case "reminders_access": return "Turn on Reminders for Wisp in System Settings > Privacy & Security."
            case "full_disk_access": return "Wisp needs Full Disk Access to read this when the app is closed."
            case "mail_automation": return "Allow Wisp to control Mail in System Settings > Privacy & Security > Automation."
            case "mail_not_configured", "no_accounts": return "Add an account in System Settings > Internet Accounts."
            case "mail_closed": return "Open Mail, or give Wisp Full Disk Access to read it when Mail is closed."
            case "notes_closed": return "Open Notes, or give Wisp Full Disk Access to read it when Notes is closed."
            default: return "Wisp will try again in a minute."
            }
        }
    }

    /// The setting that fixes it, as a link; nil when there's nothing to open.
    var fix: BriefDetail.Action? {
        guard state == .unavailable else { return nil }
        return Self.fix(for: blocked)
    }

    static func fix(for blocked: String?) -> BriefDetail.Action? {
        switch blocked {
        case "calendar_access": return BriefDetail.calendarsAccess
        case "reminders_access": return BriefDetail.remindersAccess
        case "full_disk_access", "mail_closed", "notes_closed": return BriefDetail.fullDiskAccess
        case "mail_automation": return BriefDetail.automationAccess
        case "mail_not_configured", "no_accounts": return BriefDetail.internetAccounts
        default: return nil
        }
    }

    /// "Wisp can't see your calendar and reminders yet."
    static func cantSee(_ sources: [String]) -> String {
        var nouns: [String] = []
        for source in sources where !nouns.contains(noun(source)) { nouns.append(noun(source)) }
        let joined: String
        switch nouns.count {
        case 0: return ""
        case 1: joined = nouns[0]
        case 2: joined = nouns[0] + " and " + nouns[1]
        default: joined = nouns.dropLast().joined(separator: ", ") + " and " + nouns[nouns.count - 1]
        }
        return "Wisp can't see your \(joined) yet."
    }
}

/// Connected Google accounts whose mail Wisp couldn't check, by why
/// (the service's mail_unread_by and mail_unread_reasons). "reconnect" only
/// when the person has to act on the sign-in; anything else is a check Wisp
/// hasn't finished (a sync restored at launch, or one running late). Either
/// way mail is only partly read, never an all-clear.
struct UncheckedMail: Equatable {
    var reconnect: [String] = []
    var syncing: [String] = []

    init(reconnect: [String] = [], syncing: [String] = []) {
        self.reconnect = reconnect
        self.syncing = syncing
    }

    init(unreadBy: [String], reasons: [String: String]) {
        reconnect = unreadBy.filter { reasons[$0] == "reconnect" }
        syncing = unreadBy.filter { reasons[$0] != "reconnect" }
    }

    var isEmpty: Bool { reconnect.isEmpty && syncing.isEmpty }

    /// The row's short line.
    var headline: String {
        reconnect.isEmpty ? "Wisp hasn't finished checking \(Self.list(syncing)) yet" : "Couldn't check " + Self.list(reconnect)
    }

    /// The sentence under it.
    var detail: String {
        guard !reconnect.isEmpty else { return "Wisp will show that mail once it has checked." }
        let fix = "Reconnect \(reconnect.count == 1 ? "it" : "them") in Wisp's Settings > Google so Wisp can read that mail."
        return syncing.isEmpty ? fix : fix + " Wisp hasn't finished checking \(Self.list(syncing)) yet."
    }

    /// The top line's words for it.
    var sentence: String {
        var parts: [String] = []
        if !reconnect.isEmpty { parts.append("Wisp couldn't check \(Self.list(reconnect)). Reconnect in Settings.") }
        if !syncing.isEmpty { parts.append("Wisp hasn't finished checking \(Self.list(syncing)) yet.") }
        return parts.joined(separator: " ")
    }

    /// "work@gmail.com", "a@x.com and b@y.com", "a, b and c".
    static func list(_ accounts: [String]) -> String {
        switch accounts.count {
        case 0: return "an account"
        case 1: return accounts[0]
        case 2: return accounts[0] + " and " + accounts[1]
        default: return accounts.dropLast().joined(separator: ", ") + " and " + accounts[accounts.count - 1]
        }
    }
}

/// Whether the person's locale shows 12- or 24-hour time. Views pass
/// `.autoupdatingCurrent`; checks keep the fixed en_US default so they are
/// deterministic on any host.
enum WispClock {
    static let fixedLocale = Locale(identifier: "en_US")

    /// Asks the locale's own hour pattern: "H"/"k" is a 24-hour clock. (Testing
    /// for the "a" marker misreads locales whose day period is "B".)
    static func uses24Hour(_ locale: Locale) -> Bool {
        let format = DateFormatter.dateFormat(fromTemplate: "j", options: 0, locale: locale) ?? "h a"
        let unquoted = format.split(separator: "'", omittingEmptySubsequences: false)
            .enumerated().filter { $0.offset % 2 == 0 }.map(\.element).joined()
        return unquoted.contains("H") || unquoted.contains("k")
    }

    /// An hour of the day as a whole-hour label: "3 PM" or "15:00".
    static func hour(_ hour: Int, locale: Locale) -> String {
        let h = ((hour % 24) + 24) % 24
        if uses24Hour(locale) { return String(format: "%02d:00", h) }
        return "\(h % 12 == 0 ? 12 : h % 12) \(h < 12 ? "AM" : "PM")"
    }

    /// A time of day: "3:05 PM" or "15:05".
    static func time(_ t: Double, zone: TimeZone, locale: Locale) -> String {
        let f = DateFormatter()
        let h24 = uses24Hour(locale)
        f.locale = Locale(identifier: h24 ? "en_GB" : "en_US_POSIX")
        f.timeZone = zone
        f.dateFormat = h24 ? "HH:mm" : "h:mm a"
        return f.string(from: Date(timeIntervalSince1970: t))
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
