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

/// How sure Wisp is about a block, which is how it is drawn.
enum DayStyle: Int, CaseIterable, Comparable {
    /// On your calendar: solid.
    case calendar
    /// One of your own planner tasks: solid, lighter.
    case task
    /// Suggested by Wisp from your sources: dashed, with Keep and Move.
    case suggested
    /// Learned "usually busy" or "usually away": hatched, with Not today.
    case usual
    /// A plan caught in your texts: amber, with Add and Ignore.
    case caught
    /// A reminder due later today.
    case needsYou
    /// Past due: red, near now.
    case overdue
    /// A to-do Wisp found in your mail, texts, notes or reminders, at its time.
    case todo

    static func < (a: DayStyle, b: DayStyle) -> Bool { a.rawValue < b.rawValue }

    var legend: String {
        switch self {
        case .calendar: return "Calendar"
        case .task: return "Your plan"
        case .suggested: return "Suggested"
        case .usual: return "Usually busy"
        case .caught: return "Caught in texts"
        case .needsYou: return "Needs you"
        case .overdue: return "Overdue"
        case .todo: return "To-do"
        }
    }
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

    /// Clear for any row the service identified; Mark done for a reminder too.
    /// A row with no service id or fingerprint (an older service) offers nothing.
    var actions: [NeedsYouRowAction] {
        guard let fingerprint, !fingerprint.isEmpty, !id.hasPrefix("needs-you-") else { return [] }
        return canComplete ? [.clear, .markDone] : [.clear]
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
    /// Side-by-side placement when blocks overlap.
    var column = 0
    var columns = 1
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

    /// Whether the block is drawn as over: dim, with nothing left to act on. A
    /// block that ended keeps its controls while a cancel on it is still in its
    /// Undo window or being sent: the person tapped Cancel while it was still
    /// on, and the clock passing its end must not take Undo away mid-window
    /// (or hide the spinner while the service works). The ⋯ menu never offers a
    /// new Cancel on a past block (CheckInCancel.menuEntries), so this only
    /// keeps a cancel that began in time.
    func isOver(cancelActive: Bool) -> Bool { past && !cancelActive }
}

struct DayTimeline: Equatable {
    /// Hour-aligned range the lane covers.
    let from: Double
    let to: Double
    let items: [DayItem]
    let allDay: [String]

    /// Legend entries, only for the styles on screen.
    var styles: [DayStyle] { Array(Set(items.map(\.style))).sorted() }

    /// Hour marks from `from` up to, not including, `to`.
    var hours: [Double] { stride(from: from, to: to, by: 3600).map { $0 } }

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
            let end = block.end.flatMap { $0 > block.start ? $0 : nil } ?? block.start + 30 * 60
            guard end > earliest, block.start < dayEnd, !isJunk(block.title) else { continue }
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
            items.append(DayItem(id: "block:" + block.id, title: block.title, detail: detail,
                                 start: block.start, end: end, style: style, blockID: block.id,
                                 taskID: block.taskID, patternID: block.patternID,
                                 evidenceID: block.evidenceID, kind: block.kind, members: block.members))
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
                                     start: start, end: max(due, start + 15 * 60), style: .needsYou, needID: need.id))
            }
        }

        // A plan Wisp already added to Reminders comes back as a reminder; draw it once.
        let reminderTitles = Set(needs.map { $0.title.lowercased() })
        for record in caught where !isJunk(record.title) && (record.state == "shadow" || record.state == "created")
            && !(record.state == "created" && reminderTitles.contains(record.title.lowercased())) {
            guard let when = record.when, when >= dayStart, when < dayEnd, when + hour > earliest else { continue }
            let detail = record.state == "created" ? "Caught in texts · added to Reminders" : "Caught in texts · not on your calendar"
            items.append(DayItem(id: "caught:" + record.id, title: record.title, detail: detail,
                                 start: when, end: when + hour, style: .caught, caughtID: record.id))
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
                                     start: start, end: end, style: .todo, todoID: todo.id, source: todo.source))
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
        return DayTimeline(from: from, to: to, items: arrange(items), allDay: allDay.filter { !isJunk($0) })
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

    /// The shortest time a block is drawn as: the lane never draws one under 22
    /// points at 44 points an hour, so a 15-minute block covers 30 minutes on
    /// screen. Overlap is judged by that, or back-to-back short blocks would draw
    /// on top of one another, their titles and borders running through each other.
    static let minimumDrawn: Double = 30 * 60

    /// Side by side where blocks overlap as drawn: each cluster of overlapping
    /// blocks shares out columns, first free column first.
    static func arrange(_ input: [DayItem]) -> [DayItem] {
        var items = input.sorted { ($0.start, $0.end, $0.style, $0.id) < ($1.start, $1.end, $1.style, $1.id) }
        let drawnEnd = { (item: DayItem) in max(item.end, item.start + minimumDrawn) }
        var cluster: [Int] = []
        var columnEnds: [Double] = []
        var clusterEnd = -Double.infinity
        func close() {
            for index in cluster { items[index].columns = max(1, columnEnds.count) }
            cluster = []
            columnEnds = []
        }
        for index in items.indices {
            let end = drawnEnd(items[index])
            if items[index].start >= clusterEnd { close() }
            if let free = columnEnds.firstIndex(where: { $0 <= items[index].start }) {
                items[index].column = free
                columnEnds[free] = end
            } else {
                items[index].column = columnEnds.count
                columnEnds.append(end)
            }
            cluster.append(index)
            clusterEnd = max(clusterEnd, end)
        }
        close()
        return items
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
            return "Your calendar hasn't synced yet, so I'm waiting before placing tasks."
        }
        if w.hasPrefix("Older calendar events have unknown end times") {
            return "A few earlier events have no end time, so these times are rough."
        }
        if w.hasPrefix("Some fixed events have no end time") {
            return "Some events have no end time, so I'm waiting before placing tasks."
        }
        if w.hasPrefix("All-day calendar events may reserve this day") {
            return "An all-day event may fill today, so I haven't placed tasks. Pin one to choose its time."
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

    /// Why a task couldn't be placed, in plain words.
    static func plainReason(_ reason: String) -> String {
        if reason.hasPrefix("All-day event needs") { return "an all-day event is on, so pick its time" }
        if reason.hasPrefix("Waiting for a current calendar") { return "waiting for your calendar to sync" }
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
                      clock: (Double) -> String) -> String {
        // What's already over doesn't shape the rest of the day.
        let items = timeline.items.filter { !$0.past }
        let dueToday = needs.filter { n in
            n.source != "calendar" && !isJunk(n.title) && (n.due.map { $0 <= endOfDay } ?? false)
        }
        let suggestions = items.filter { $0.style == .suggested }
        let usual = items.filter { $0.style == .usual }
        var first: String
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
        } else if !suggestions.isEmpty {
            first = "Nothing is due. I've suggested \(count(suggestions.count)) block\(suggestions.count == 1 ? "" : "s") around your calendar."
        } else {
            let events = items.filter { $0.style == .calendar || $0.style == .task }
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
                     openingOverride: String? = nil, dueReference: String? = nil) -> String {
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
    static func briefLines(calendar: DayLoad?, calendarUnsynced: Bool = false,
                           mail: (ready: Bool, needing: [BriefNeed])?,
                           texts: (ready: Bool, needing: [BriefNeed])?,
                           textsNeedAccess: Bool = false,
                           mailOverview: String? = nil, textsOverview: String? = nil,
                           soon: Soon) -> [BriefLine] {
        var lines: [BriefLine] = []
        if let calendar {
            lines.append(BriefLine(tag: "CAL", text: calendar.summary, trailing: calendar.next ?? "", source: .calendar))
        } else {
            lines.append(BriefLine(tag: "CAL", text: calendarUnsynced ? "Calendar hasn't synced yet" : "Nothing on your calendar",
                                   trailing: "", source: .calendar, muted: calendarUnsynced))
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
            lines.append(BriefLine(tag: "MAIL", text: "Mail hasn't been checked yet", trailing: "", source: .mail, muted: true))
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
            lines.append(BriefLine(tag: "TEXTS", text: "Messages haven't been checked yet", trailing: "", source: .messages, muted: true))
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
        let left = events.filter { !DayTimeline.isJunk($0.title) && end($0) > from }.sorted { $0.start < $1.start }
        let allDayTitles = allDay.filter { !DayTimeline.isJunk($0) }
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
