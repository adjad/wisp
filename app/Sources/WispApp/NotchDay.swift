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
    /// A date-only reminder: `due` is that day's last minute, the Reminders
    /// reader's stand-in, not a time the person chose.
    var allDay = false
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
    /// For a reminder due on a day with no time of its own: `due` is only that
    /// day's last minute, so the agenda says "All day" and never that minute.
    var allDay = false
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

// MARK: - The week row

/// One day card in the Day tab's week row. Only a day whose plan Wisp has
/// actually read (the day on screen, with Calendar readable) carries a load;
/// every other day shows its date alone, never an invented bar or count.
/// What the Day may say about the calendar, by the Brief's rule
/// (NotchBriefView's `calendarKnown`): read once the service says Calendar is
/// ready, or when its events are on the plan anyway. Not read, Day never
/// calls the day empty, free or clear: still syncing it says it is reading;
/// not granted, unavailable or unknown (no word from the service) it says it
/// can't read the calendar.
enum DayCalendarState: Equatable {
    case read, reading, unread

    /// - Parameters:
    ///   - ready: the service's `sources.calendar.ready`, nil when it sent none.
    ///   - eventsShown: a Calendar event or a real all-day item is on the plan.
    ///   - access: `TodayPlan.access("calendar")`, nil when unknown.
    static func of(ready: Bool?, eventsShown: Bool, access: SourceAccess?) -> DayCalendarState {
        if ready == true || eventsShown { return .read }
        return access?.state == .syncing ? .reading : .unread
    }

    var isRead: Bool { self == .read }

    /// The summary's sentence when the calendar isn't read.
    var summaryLine: String? {
        switch self {
        case .read: return nil
        case .reading: return "Wisp is still reading your calendar, so this may not be your whole day."
        case .unread: return "Wisp can't read your calendar. Allow it in System Settings › Privacy & Security › Calendars."
        }
    }

    /// The Next line's words when the calendar isn't read.
    var nextLine: String? {
        switch self {
        case .read: return nil
        case .reading: return "Reading your calendar…"
        case .unread: return "Wisp can't read your calendar."
        }
    }
}

/// `GET /assistant/today/week?start=&timezone=` (service/assistant/today_api.py):
/// seven days from the calendar copy and the planner, read-only. A day whose
/// calendar isn't current has `ready` false and no numbers.
struct DayWeekReply: Equatable {
    struct Span: Equatable {
        /// Minutes from the day's local midnight.
        let start: Int
        let end: Int
        let kind: String
    }

    struct Day: Equatable {
        let day: String
        let ready: Bool
        var onClock = 0
        var minutes = 0
        var due = 0
        var spans: [Span] = []
    }

    let start: String
    let timezone: String
    let days: [Day]

    static let path = "/assistant/today/week"

    static func query(start: String, timezone: String) -> String {
        var components = URLComponents()
        components.path = path
        components.queryItems = [URLQueryItem(name: "start", value: start), URLQueryItem(name: "timezone", value: timezone)]
        return components.string ?? path
    }

    /// The reply for exactly the week asked for, or nil: not an object, another
    /// week or zone, or not seven consecutive days in order. Extra keys are
    /// ignored. A day marked ready that lacks a number or has a malformed span
    /// is read as not ready (its date alone), never guessed.
    static func decode(_ data: Data, start: String, timezone: String, days expected: [String]) -> DayWeekReply? {
        guard expected.count == 7,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              object["start"] as? String == start, object["timezone"] as? String == timezone,
              let rows = object["days"] as? [Any], rows.count == 7 else { return nil }
        var days: [Day] = []
        for (row, key) in zip(rows, expected) {
            guard let row = row as? [String: Any], row["day"] as? String == key else { return nil }
            days.append(day(row, key: key))
        }
        return DayWeekReply(start: start, timezone: timezone, days: days)
    }

    private static func int(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let double = number.doubleValue
        guard double.isFinite, double >= 0, double == double.rounded() else { return nil }
        return Int(double)
    }

    private static func day(_ row: [String: Any], key: String) -> Day {
        let notReady = Day(day: key, ready: false)
        guard let ready = row["calendar_ready"] as? NSNumber, CFGetTypeID(ready) == CFBooleanGetTypeID(),
              ready.boolValue,
              let onClock = int(row["on_clock"]), let minutes = int(row["on_clock_minutes"]), let due = int(row["due"]),
              let rawSpans = row["spans"] as? [Any] else { return notReady }
        var spans: [Span] = []
        for raw in rawSpans {
            guard let parts = raw as? [Any], parts.count == 3, let from = int(parts[0]), let to = int(parts[1]),
                  let kind = parts[2] as? String, to > from, to <= 24 * 60 + 60 else { return notReady }
            spans.append(Span(start: from, end: to, kind: kind))
        }
        guard spans.count == onClock else { return notReady }
        return Day(day: key, ready: true, onClock: onClock, minutes: minutes, due: due, spans: spans)
    }

    /// A ready day's card load, counted and drawn the way the shown day's is:
    /// the spans of real commitments (calendar events, plan and study blocks;
    /// suggestions and learned "usually busy" windows hold nothing), on a bar
    /// over the working hours `workStart`–`workEnd` (minutes) widened to whole
    /// hours around them. The count is those spans, so it can be below the
    /// service's `on_clock`, which counts every timed block. Nil when not ready.
    static func load(_ day: Day, workStart: Int, workEnd: Int) -> DayLoadBar? {
        guard day.ready else { return nil }
        let held = day.spans.filter { !["suggestion", "behavior", "behavior_away"].contains($0.kind) }
        let spans = held.map { (start: Double($0.start * 60), end: Double($0.end * 60)) }
        let span = DayWeek.span(workStart: Double(workStart * 60), workEnd: Double(workEnd * 60), spans: spans,
                                dayStart: 0, dayEnd: 86400)
        return DayWeek.load(onClock: held.count, due: day.due, spans: spans, span: span, now: nil)
    }

    /// Loads by day key, for the cards other than the shown one.
    func loads(workStart: Int, workEnd: Int) -> [String: DayLoadBar] {
        var out: [String: DayLoadBar] = [:]
        for day in days { if let load = Self.load(day, workStart: workStart, workEnd: workEnd) { out[day.day] = load } }
        return out
    }
}

struct DayWeekCard: Equatable, Identifiable {
    /// "2026-11-11", the key `GET /assistant/today?day=` takes.
    let day: String
    let date: Date
    /// "WED" and "Wednesday".
    let weekday: String
    let weekdayName: String
    let number: Int
    let isToday: Bool
    /// The day the tab shows.
    let isShown: Bool
    var load: DayLoadBar? = nil
    var id: String { day }
}

/// A day's load as its card draws it: how many things are on the clock and
/// due, and the stretches its timed things hold, as fractions of the bar.
struct DayLoadBar: Equatable {
    struct Segment: Equatable {
        let from: Double
        let to: Double
        /// Over before now, on today: drawn dim.
        let past: Bool
    }

    let onClock: Int
    let due: Int
    /// Minutes the timed things hold, overlaps counted once.
    let minutes: Int
    let segments: [Segment]
    /// Where now falls on the bar, on today while it is inside the bar's span.
    let now: Double?
}

enum DayWeek {
    static let weekdays = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    static let weekdayNames = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

    /// The seven days of the week holding `date`, in `calendar`'s own week
    /// order (its firstWeekday), each at its midnight in the calendar's zone.
    static func dates(containing date: Date, calendar: Calendar) -> [Date] {
        let day = calendar.startOfDay(for: date)
        let weekday = calendar.component(.weekday, from: day)
        let back = (weekday - calendar.firstWeekday + 7) % 7
        guard let first = calendar.date(byAdding: .day, value: -back, to: day) else { return [day] }
        return (0..<7).compactMap { calendar.date(byAdding: .day, value: $0, to: first) }
    }

    /// The week row for the week holding `shown`: `shownLoad` (the shown day's
    /// own plan) on the shown day's card, `weekLoads` (the week route's) on the
    /// others, by day key.
    static func cards(shown: Date, today: Date, calendar: Calendar, shownLoad: DayLoadBar?,
                      weekLoads: [String: DayLoadBar] = [:]) -> [DayWeekCard] {
        let key: (Date) -> String = { date in
            let parts = calendar.dateComponents([.year, .month, .day], from: date)
            return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
        }
        let shownKey = key(shown)
        let todayKey = key(today)
        return dates(containing: shown, calendar: calendar).map { date in
            let index = max(0, min(6, calendar.component(.weekday, from: date) - 1))
            let day = key(date)
            return DayWeekCard(day: day, date: date, weekday: weekdays[index], weekdayName: weekdayNames[index],
                               number: calendar.component(.day, from: date), isToday: day == todayKey,
                               isShown: day == shownKey, load: day == shownKey ? shownLoad : weekLoads[day])
        }
    }

    /// The stretches of the day that real commitments hold: calendar events and
    /// planned or study blocks, clipped to the day. Suggestions and learned
    /// "usually busy" windows are not commitments, so they never add load.
    static func spans(_ blocks: [DayBlockInput], dayStart: Double, dayEnd: Double) -> [(start: Double, end: Double)] {
        blocks.compactMap { block in
            guard !["suggestion", "behavior", "behavior_away"].contains(block.kind) else { return nil }
            let fromCalendar = block.kind == "calendar" || block.kind == "manual"
            guard fromCalendar ? !DayTimeline.isPlaceholderEvent(block.title) : !DayTimeline.isJunk(block.title) else { return nil }
            let end = block.end.flatMap { $0 > block.start ? $0 : nil } ?? block.start + 30 * 60
            let from = max(block.start, dayStart), to = min(end, dayEnd)
            return to > from ? (from, to) : nil
        }
    }

    /// The bar's span: working hours, widened to whole hours around anything
    /// held outside them, never past the day.
    static func span(workStart: Double, workEnd: Double, spans: [(start: Double, end: Double)],
                     dayStart: Double, dayEnd: Double) -> (start: Double, end: Double) {
        let first = min(workStart, spans.map(\.start).min() ?? workStart)
        let last = max(workEnd, spans.map(\.end).max() ?? workEnd)
        let start = max(dayStart, DayTimeline.floorHour(first, dayStart: dayStart))
        let end = min(dayEnd, DayTimeline.ceilHour(last, dayStart: dayStart))
        return end > start ? (start, end) : (dayStart, dayEnd)
    }

    /// The card's load: counts as the summary gives them, and `spans` merged
    /// and placed on the bar's span. `now` only on today.
    static func load(onClock: Int, due: Int, spans: [(start: Double, end: Double)],
                     span: (start: Double, end: Double), now: Double?) -> DayLoadBar {
        var merged: [(start: Double, end: Double)] = []
        for s in spans.sorted(by: { ($0.start, $0.end) < ($1.start, $1.end) }) {
            if let last = merged.last, s.start <= last.end {
                merged[merged.count - 1].end = max(last.end, s.end)
            } else {
                merged.append(s)
            }
        }
        let width = max(1, span.end - span.start)
        let fraction: (Double) -> Double = { min(1, max(0, ($0 - span.start) / width)) }
        let segments = merged.compactMap { s -> DayLoadBar.Segment? in
            let from = fraction(s.start), to = fraction(s.end)
            guard to > from else { return nil }
            return DayLoadBar.Segment(from: from, to: to, past: now.map { s.end <= $0 } ?? false)
        }
        let minutes = Int((merged.reduce(0) { $0 + ($1.end - $1.start) } / 60).rounded())
        let nowAt = now.flatMap { $0 >= span.start && $0 <= span.end ? fraction($0) : nil }
        return DayLoadBar(onClock: onClock, due: due, minutes: minutes, segments: segments, now: nowAt)
    }

    /// The full-height row (a count line and a bar on every card) is for a
    /// week where more than the shown day carries a load: a `weekLoad` for
    /// the other days. With the shown day's alone, the compact row.
    static func fullRow(_ cards: [DayWeekCard]) -> Bool {
        cards.filter { $0.load != nil }.count > 1
    }

    /// The form a week row keeps while the panel is open: chosen before its
    /// first frame from what was already read (`latched`), so a week arriving
    /// while it's open fills the compact cards in place and the height never
    /// jumps; the full row waits for the next open.
    static func full(latched: Bool?, cards: [DayWeekCard]) -> Bool {
        latched ?? fullRow(cards)
    }

    /// The first day of the person's calendar week holding `date`, as the
    /// week route's `start`.
    static func start(of date: Date, calendar: Calendar) -> String {
        let first = dates(containing: date, calendar: calendar).first ?? date
        let parts = calendar.dateComponents([.year, .month, .day], from: first)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// The week's seven day keys from `start`, as the route answers them.
    static func keys(_ dates: [Date], calendar: Calendar) -> [String] {
        dates.map { date in
            let parts = calendar.dateComponents([.year, .month, .day], from: date)
            return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
        }
    }

    /// Each card's share of the row: equal in the full row; in the compact row
    /// the shown day twice as wide, so its count fits beside its date. From
    /// which day is shown only, never from whether its load has arrived.
    static func weights(_ cards: [DayWeekCard], full: Bool) -> [Double] {
        cards.map { full || !$0.isShown ? 1 : 2 }
    }

    /// The card's count: "3", "3 · 2 due", "2 due", "free".
    static func countText(_ load: DayLoadBar) -> String {
        switch (load.onClock, load.due) {
        case (0, 0): return "free"
        case (let n, 0): return "\(n)"
        case (0, let d): return "\(d) due"
        case (let n, let d): return "\(n) · \(d) due"
        }
    }

    /// The count when the full one doesn't fit: the things on the clock alone.
    static func shortCountText(_ load: DayLoadBar) -> String {
        load.onClock > 0 ? "\(load.onClock)" : countText(load)
    }

    /// The card for VoiceOver: "Wednesday 11, 4 things", "Saturday 14, today,
    /// 3 things and 2 due", "Sunday 15, free", or the date alone without a load.
    static func spoken(_ card: DayWeekCard) -> String {
        var parts = ["\(card.weekdayName) \(card.number)"]
        if card.isToday { parts.append("today") }
        if card.isShown && !card.isToday { parts.append("shown") }
        if let load = card.load {
            let things = "\(load.onClock) thing\(load.onClock == 1 ? "" : "s")"
            switch (load.onClock, load.due) {
            case (0, 0): parts.append("free")
            case (_, 0): parts.append(things)
            case (0, let d): parts.append("\(d) due")
            case (_, let d): parts.append(things + " and \(d) due")
            }
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Next up and Later today

extension DayTimeline {
    /// What the Next card shows: the first thing still on the clock, by when it
    /// begins; at the same time a calendar event before a plan block before a
    /// caught plan, then the shorter, then by id. Suggestions, usual windows,
    /// due-by rows and all-day items are never "next".
    static func nextUp(_ items: [DayItem], now: Double) -> DayItem? {
        items.filter { !$0.past && isOnTheClock($0) && !$0.allDay && $0.end > now }
            .min { a, b in
                let ka = (a.begins ?? a.start, a.style.rawValue, a.ends ?? a.end, a.id)
                let kb = (b.begins ?? b.start, b.style.rawValue, b.ends ?? b.end, b.id)
                return ka < kb
            }
    }

    /// The Next line when there is no Next card: "Nothing else on the clock
    /// today." only once Calendar was read; otherwise the calendar line is the
    /// only thing it says.
    static func nothingNextLine(_ calendar: DayCalendarState) -> String {
        calendar.nextLine ?? "Nothing else on the clock today."
    }

    /// Whether the next thing is already on.
    static func isOnNow(_ item: DayItem, now: Double) -> Bool {
        (item.begins ?? item.start) <= now && now < (item.ends ?? item.end)
    }

    /// "NEXT · IN 40 MIN", "NEXT · IN 1 H 5 MIN", "NEXT · STARTING NOW", or for
    /// one already on "NOW · 25 MIN LEFT".
    static func nextLabel(_ item: DayItem, now: Double) -> String {
        if isOnNow(item, now: now) {
            let left = Int((((item.ends ?? item.end) - now) / 60).rounded(.up))
            return "NOW · " + length(max(1, left)).uppercased() + " LEFT"
        }
        let minutes = Int((((item.begins ?? item.start) - now) / 60).rounded(.up))
        return minutes <= 0 ? "NEXT · STARTING NOW" : "NEXT · IN " + length(minutes).uppercased()
    }

    /// The same, for VoiceOver: "Next, in 40 min", "Now, 25 min left".
    static func spokenNextLabel(_ item: DayItem, now: Double) -> String {
        let label = nextLabel(item, now: now).lowercased()
        return label.prefix(1).uppercased() + label.dropFirst().replacingOccurrences(of: " · ", with: ", ")
    }

    /// "Now 1:20 PM · free until 2:00 PM": nothing holds the time from now
    /// until the next thing that does, a minute or more away. Nil otherwise,
    /// and always nil when Wisp can't read Calendar: it can't know you're free.
    static func nowFreeLine(_ items: [DayItem], now: Double, clock: (Double) -> String,
                            calendarRead: Bool = true) -> String? {
        guard calendarRead else { return nil }
        let holding = items.filter { !$0.past && holdsTime($0) }
        if holding.contains(where: { ($0.begins ?? $0.start) <= now && now < $0.end }) { return nil }
        guard let until = holding.map({ $0.begins ?? $0.start }).filter({ $0 > now }).min(), until - now >= 60 else { return nil }
        return "Now " + clock(now) + " · free until " + clock(until)
    }

    /// The Next card's time line: "11:45 AM – 2:00 PM · 2 h 15 min", then what
    /// it is when it isn't a Calendar event ("· Study block", "· Your plan",
    /// "· Caught in texts"); "From 6:00 PM" for an event the source gave no
    /// end, "At 7:00 PM" for a to-do or a caught plan.
    static func nextTimeLine(_ item: DayItem, clock: (Double) -> String) -> String {
        let begins = item.begins ?? item.start
        var line: String
        if let ends = item.ends {
            line = clock(begins) + " – " + clock(ends) + " · " + length(Int(((ends - begins) / 60).rounded()))
        } else {
            line = (item.style == .calendar ? "From " : "At ") + clock(begins)
        }
        switch item.style {
        case .task: line += item.kind == "study" ? " · Study block" : " · Your plan"
        case .caught: line += " · Caught in texts"
        case .todo where !sourceName(item.source).isEmpty: line += " · " + sourceName(item.source)
        default: break
        }
        return line
    }

    /// The Next card's item taken out of the agenda (the card replaces its
    /// row), with the free stretch that ran up to it: the card says that.
    /// Without a read calendar (`calendarRead` false) there are no free
    /// stretches at all: Wisp can't know the time is free.
    static func agenda(_ agenda: DayAgenda, without next: DayItem?, calendarRead: Bool = true) -> DayAgenda {
        guard next != nil || !calendarRead else { return agenda }
        let ahead = agenda.ahead.filter { row in
            switch row {
            case .item(let item): return item.id != next?.id
            case .free(_, let end): return calendarRead && end != next?.start
            }
        }
        return DayAgenda(earlier: agenda.earlier, ahead: ahead)
    }

    /// Later today: what the agenda still lists after the Next card's item,
    /// as rows at a time (`timed`, overdue first as the agenda has them) and
    /// what is due by a time (`due`), each in agenda order.
    static func laterRows(_ agenda: DayAgenda, excluding next: DayItem?) -> (timed: [DayItem], due: [DayItem]) {
        let items: [DayItem] = agenda.ahead.compactMap {
            if case .item(let item) = $0, item.id != next?.id { return item }
            return nil
        }
        return (items.filter { !isDueBy($0) }, items.filter(isDueBy))
    }

    /// The "Show N more" line under Later today, naming what it hides:
    /// "Show 2 more · due 11:59 PM: Lab 4 and Problem set 6" when all of it is
    /// due at one time, else "Show 3 more: Lab 4, Club and 1 more".
    static func moreLine(_ hidden: [DayItem], clock: (Double) -> String) -> String {
        guard !hidden.isEmpty else { return "" }
        let titles = hidden.map(\.title)
        let named: String
        switch titles.count {
        case 1: named = titles[0]
        case 2: named = titles[0] + " and " + titles[1]
        default: named = titles[0] + ", " + titles[1] + " and \(titles.count - 2) more"
        }
        let times = Set(hidden.map { item -> String in
            guard isDueBy(item), let due = item.due else { return "" }
            return item.allDay ? "today" : clock(due)
        })
        let lead = "Show \(hidden.count) more"
        if times.count == 1, let time = times.first, !time.isEmpty { return lead + " · due " + time + ": " + named }
        return lead + ": " + named
    }

    /// A Later row's time: its start, "Overdue", its due time, or "All day".
    static func laterTime(_ item: DayItem, clock: (Double) -> String) -> String {
        gutter(item, clock: clock).top
    }

    /// How much of a Later row's duration bar is filled: its length against
    /// two hours (a longer one fills it), and never less than a sliver. Nil
    /// for what is due by a time or overdue: those hold no stretch of time.
    static func durationFraction(_ item: DayItem) -> Double? {
        guard holdsTime(item) else { return nil }
        let minutes = ((item.ends ?? item.end) - (item.begins ?? item.start)) / 60
        return min(1, max(0.12, minutes / 120))
    }

    /// A Later row's muted detail: how long it runs ("1 h 30 min"), "Suggested ·
    /// about 45 min", "Usually busy · …", "Needs you", or a to-do's source.
    static func laterDetail(_ item: DayItem, now: Double, isToday: Bool, clock: (Double) -> String) -> String {
        switch item.style {
        case .calendar:
            guard let ends = item.ends else { return "Calendar" }
            let begins = item.begins ?? item.start
            if isToday && begins <= now && now < ends { return length(Int(((ends - now) / 60).rounded(.up))) + " left" }
            return length(Int(((ends - begins) / 60).rounded()))
        case .task where item.kind == "study":
            return length(Int(((item.end - item.start) / 60).rounded()))
        default:
            return agendaDetail(item, now: now, isToday: isToday, clock: clock)
        }
    }

    /// How many things a list shows before its "Show N more": eight, less one
    /// for the Next card that stands in for the first, and less one more when
    /// asked (`oneFewer`: the wide halves out of balance, or the narrow Day
    /// held to its height from before the card).
    static func rowsBeforeMore(_ base: Int = 8, hasNext: Bool, oneFewer: Bool) -> Int {
        max(1, base - (hasNext ? 1 : 0) - (oneFewer ? 1 : 0))
    }

    /// Whether wide Later today shows one fewer row, from the halves' drawn
    /// heights: when the left runs more than a third taller than the right.
    /// Once trimmed it stays trimmed unless the left, with a row's worth
    /// (`rowAllowance`) added back, would still be within the third, so the
    /// choice can't flip back and forth on its own result. No right half, no trim.
    static func trimLater(trimmed: Bool, left: Double, right: Double, rowAllowance: Double) -> Bool {
        guard right > 0, left > 0 else { return false }
        let limit = right * 4 / 3
        return trimmed ? left + rowAllowance > limit : left > limit
    }

    /// The Next card's two buttons beside its ⋯ menu, for a Calendar event the
    /// service described: Open in Calendar, and Move… only where the timeline's
    /// own menu offers Move. Nothing for any other kind of row, and nothing
    /// while a cancel of it is in its Undo window or being sent.
    enum NextButton: Equatable { case open, move }

    static func nextButtons(isDescribedEvent: Bool, menuOffersMove: Bool, cancelActive: Bool) -> [NextButton] {
        guard isDescribedEvent, !cancelActive else { return [] }
        return menuOffersMove ? [.open, .move] : [.open]
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
            case "study":
                // A study block (service/study/day_hook.py): drawn like your own
                // plan, never as a Calendar event, so it offers no Open in
                // Calendar, Move or Cancel.
                style = .task
                detail = "Study block · \(duration(minutes))"
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
                // A date-only reminder names its day, never the stand-in last minute.
                let since = need.allDay ? overdueDay(due, dayStart: dayStart, dayClock: dayClock)
                    : overdueLabel(due, dayStart: dayStart, clock: clock, dayClock: dayClock)
                items.append(DayItem(id: "need:" + need.id, title: need.title, detail: "Overdue since " + since,
                                     start: start, end: start + 30 * 60, style: .overdue, needID: need.id))
            } else if due >= dayStart, due <= dayEnd, due > earliest {
                let start = max(earliest, due - 30 * 60)
                let when = need.allDay ? (isToday ? "today" : "that day") : clock(due)
                items.append(DayItem(id: "need:" + need.id, title: need.title, detail: "Due " + when + " · Needs you",
                                     start: start, end: max(due, start + 15 * 60), style: .needsYou, needID: need.id,
                                     due: due, allDay: need.allDay))
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
        // A reminder the lane already draws, as Needs you or Overdue, isn't still to fit.
        var seen = Set(blocks.map { todoKey($0.title) }
                       + needs.filter { $0.due.map { $0 >= dayStart && $0 < dayEnd } ?? false }.map { todoKey($0.title) }
                       + drawnOverdue(needs, blocks: blocks, now: now, isToday: isToday).map { todoKey($0.title) })
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

    /// The overdue reminders the agenda lists by name. Past `maxOverdueShown` the rest
    /// fold into one "N more overdue" row, so those keep their place in Still to fit.
    private static func drawnOverdue(_ needs: [DayNeedInput], blocks: [DayBlockInput], now: Double,
                                     isToday: Bool) -> [DayNeedInput] {
        guard isToday else { return [] }
        let suggested = Set(blocks.compactMap { $0.kind == "suggestion" ? $0.evidenceID : nil })
        let overdue = needs.filter { need in
            need.source != "calendar" && !isJunk(need.title) && !suggested.contains(need.id)
                && (need.due.map { $0 < now } ?? false)
        }
        return overdue.count > maxOverdueShown ? Array(overdue.prefix(maxOverdueShown - 1)) : overdue
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
    /// "7:30 PM", "8:00 PM" over "Due", "All day" over "Due" for a reminder
    /// with a date and no time, or "Overdue".
    static func gutter(_ item: DayItem, clock: (Double) -> String) -> (top: String, bottom: String) {
        if item.style == .overdue { return ("Overdue", "") }
        if let due = item.due { return (item.allDay ? "All day" : clock(due), "Due") }
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

    /// The day a date-only reminder was due, without a clock time: "today", or
    /// the day's name alone ("Mon"), which `dayClock` leads with.
    private static func overdueDay(_ due: Double, dayStart: Double, dayClock: (Double) -> String) -> String {
        if due >= dayStart { return "today" }
        let label = dayClock(due)
        return label.split(separator: " ").first.map { String($0) } ?? label
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
            // Short, and the number and its unit never part: the row's reason
            // was cut right at the number ("…stretch is 6…").
            return "no gap long enough" + (largest.isEmpty ? "" : " (the longest is \(largest)\u{00A0}min)")
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
                      clock: (Double) -> String, dayStart: Double? = nil, calendar: DayCalendarState = .read) -> String {
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
        // Counted from the list the agenda draws: what happens at a time
        // (calendar events, plan and study blocks, plans caught in texts, to-dos
        // at their time), and apart from it what is due by a time (reminders,
        // school work, to-dos due by then). Only calendar and plan blocks were
        // counted, and the due rows not at all ("nothing else planned").
        let events = items.filter(isOnTheClock)
        let due = items.filter(isDueBy).count
        var first: String
        if let line = calendar.summaryLine {
            // Not "nothing is planned", "nothing else" or "clear after": Wisp
            // hasn't read the calendar. Only what it did read is counted: what's
            // due, and the overdue backlog.
            first = line
            if due > 0 { first += " \(things(due).capitalizedFirst) due \(isToday ? "today" : "that day")." }
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
            if events.isEmpty && due == 0 {
                first += "."
            } else if due == 0 {
                first += ", and \(things(events.count)) on the clock."
            } else {
                first += ", " + clockAndDue(events.count, due, isToday: isToday) + "."
            }
        } else if !suggestions.isEmpty {
            first = "Nothing is due. I've suggested \(count(suggestions.count)) block\(suggestions.count == 1 ? "" : "s") around your calendar."
        } else {
            if events.isEmpty && due == 0 {
                first = isToday ? "Nothing is planned for the rest of today." : "Nothing is planned that day."
            } else if due == 0 {
                first = "\(things(events.count).capitalizedFirst) on the clock\(isToday ? " for the rest of today" : ""), nothing else planned."
            } else {
                first = clockAndDue(events.count, due, isToday: isToday).capitalizedFirst + "."
            }
        }
        if let last = items.map(\.end).max(), last < endOfDay - 2 * hour {
            first += " Clear after " + clock(last) + "."
        }
        return first
    }

    /// On the agenda at a time of its own: a calendar event, a plan or study
    /// block, a plan caught in texts, a to-do that happens at its time.
    static func isOnTheClock(_ item: DayItem) -> Bool {
        switch item.style {
        case .calendar, .task, .caught: return true
        case .todo: return item.due == nil
        default: return false
        }
    }

    /// On the agenda as due by a time, still ahead: a reminder, school work,
    /// a to-do due by then. Overdue rows are the backlog, counted apart.
    static func isDueBy(_ item: DayItem) -> Bool {
        item.style == .needsYou || (item.style == .todo && item.due != nil)
    }

    /// "one thing", "four things".
    static func things(_ n: Int) -> String { "\(count(n)) thing\(n == 1 ? "" : "s")" }

    /// "four things on the clock and two due today", "two things due today".
    static func clockAndDue(_ events: Int, _ due: Int, isToday: Bool) -> String {
        let when = isToday ? "today" : "that day"
        if events == 0 { return "\(things(due)) due \(when), nothing on the clock" }
        return "\(things(events)) on the clock and \(count(due)) due \(when)"
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

    /// Always four rows, CAL, MAIL, TEXTS and SOON, so the Brief keeps its shape,
    /// and SCHOOL after CAL when school work is due today or tomorrow.
    /// A source Wisp can't read yet shows a dim row saying so instead of disappearing.
    static func briefLines(calendar: DayLoad?, calendarUnsynced: Bool = false, calendarAccess: SourceAccess? = nil,
                           mail: (ready: Bool, needing: [BriefNeed])?,
                           texts: (ready: Bool, needing: [BriefNeed])?,
                           textsNeedAccess: Bool = false,
                           mailAccess: SourceAccess? = nil, textsAccess: SourceAccess? = nil,
                           mailOverview: String? = nil, textsOverview: String? = nil,
                           soon: Soon, school: String = "") -> [BriefLine] {
        var lines: [BriefLine] = []
        if let calendar {
            lines.append(BriefLine(tag: "CAL", text: calendar.summary, trailing: calendar.next ?? "", source: .calendar))
        } else {
            let unread = calendarAccess.flatMap { $0.readable ? nil : $0 }
            lines.append(BriefLine(tag: "CAL", text: unread?.headline ?? (calendarUnsynced ? "Reading your calendar…" : "Nothing on your calendar"),
                                   trailing: "", source: .calendar, muted: calendarUnsynced || unread != nil))
        }
        // School work due today or tomorrow (StudyDaySummary.line), only when
        // there is some: Study is optional, so the row never says "nothing due".
        let school = school.trimmingCharacters(in: .whitespacesAndNewlines)
        if !school.isEmpty {
            lines.append(BriefLine(tag: "SCHOOL", text: school, trailing: "", source: .reminders))
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

    /// What Remove and Keep send for a suggested block: every item a grouped
    /// review covers, or the one item it came from. Nothing for any other block.
    static func removalIDs(_ item: DayItem) -> [String] {
        guard item.style == .suggested else { return [] }
        let members = item.members.map(\.evidenceID)
        return members.isEmpty ? (item.evidenceID.map { [$0] } ?? []) : members
    }

    /// The Undo bar's words after Remove. A grouped review says how many emails
    /// went, since Undo brings them all back together.
    static func removedNotice(_ item: DayItem) -> String {
        item.members.count > 1 ? "Removed \(item.members.count) emails from your day"
                               : "Removed “\(item.title)” from your day"
    }

    /// The ⋯ entry that brings back today's removed suggestions, or nil when
    /// there are none. It names only today's, which is all it restores.
    /// It counts removed blocks, so a grouped review of five emails is one.
    static func bringBackLabel(blocks: Int) -> String? {
        switch blocks {
        case ..<1: return nil
        case 1: return "Bring back today's removed suggestion"
        default: return "Bring back today's \(blocks) removed suggestions"
        }
    }

    /// What a failed Undo says. Its bar comes back only while it is still the
    /// newest Remove's; an older one points at Bring back instead.
    static func undoFailedNotice(stillShowing: Bool) -> String {
        stillShowing ? "Couldn't undo that just now. Try Undo again."
                     : "Couldn't undo the earlier Remove. Bring it back from ⋯ below."
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
        cache.lock.lock(); defer { cache.lock.unlock() }
        if let known = cache.h24[locale.identifier] { return known }
        let format = DateFormatter.dateFormat(fromTemplate: "j", options: 0, locale: locale) ?? "h a"
        let unquoted = format.split(separator: "'", omittingEmptySubsequences: false)
            .enumerated().filter { $0.offset % 2 == 0 }.map(\.element).joined()
        let h24 = unquoted.contains("H") || unquoted.contains("k")
        cache.h24[locale.identifier] = h24
        return h24
    }

    /// The 24-hour answer and the time formatters, kept. Building a formatter
    /// (or asking a locale for its hour pattern) costs far more than using one,
    /// and Day and the Brief format dozens of times per update: rebuilt each
    /// time, they held a tab switch's first frame back. Emptied when the Mac's
    /// locale changes, a 12/24-hour switch included.
    private final class Cache: @unchecked Sendable {
        let lock = NSLock()
        var h24: [String: Bool] = [:]
        var formatters: [String: DateFormatter] = [:]
        init() {
            NotificationCenter.default.addObserver(forName: NSLocale.currentLocaleDidChangeNotification,
                                                   object: nil, queue: nil) { [weak self] _ in
                guard let self else { return }
                self.lock.lock(); self.h24 = [:]; self.formatters = [:]; self.lock.unlock()
            }
        }
    }
    private static let cache = Cache()

    /// An hour of the day as a whole-hour label: "3 PM" or "15:00".
    static func hour(_ hour: Int, locale: Locale) -> String {
        let h = ((hour % 24) + 24) % 24
        if uses24Hour(locale) { return String(format: "%02d:00", h) }
        return "\(h % 12 == 0 ? 12 : h % 12) \(h < 12 ? "AM" : "PM")"
    }

    /// A time of day: "3:05 PM" or "15:05".
    static func time(_ t: Double, zone: TimeZone, locale: Locale) -> String {
        let h24 = uses24Hour(locale)
        cache.lock.lock(); defer { cache.lock.unlock() }
        let key = (h24 ? "24|" : "12|") + zone.identifier
        let f = cache.formatters[key] ?? {
            let f = DateFormatter()
            f.locale = Locale(identifier: h24 ? "en_GB" : "en_US_POSIX")
            f.timeZone = zone
            f.dateFormat = h24 ? "HH:mm" : "h:mm a"
            cache.formatters[key] = f
            return f
        }()
        return f.string(from: Date(timeIntervalSince1970: t))
    }
}

/// The `study` object of `/assistant/today` (service/study/day_hook.py): what
/// school work is due, for the Brief's SCHOOL line. Every field is optional on
/// the wire, so an older or partial service never blanks the Brief.
struct StudyDaySummary: Decodable, Equatable {
    /// An open assignment: one due today (`due_today`), or one due from today
    /// through a week out (`upcoming`, which also carries `course_label`).
    struct Due: Decodable, Equatable, Identifiable {
        var id = ""
        var title = ""
        var courseName = ""
        /// The course's short code ("CS 101"); empty when it has none.
        var courseLabel = ""
        var dueTS: Double? = nil
        var allDay = false
        var kind = ""
        var status = ""
        /// The assignment's own page, already checked by the service and again
        /// here (StudyLinkRule); nil when there is none or it must not be offered.
        var url: String? = nil
        /// "assignment" or "quiz_page" (a quiz's landing page, never its attempt); nil without a url.
        var urlKind: String? = nil
        /// `due_soon` only: "today" or "tomorrow"; empty elsewhere.
        var day = ""
        /// `due_soon` only: the guide or other material made for it, or the course's guide.
        var material: Material? = nil

        /// What Study made that a due-soon row opens: one made for this
        /// assignment ("assignment"), or the course's newest guide ("course").
        struct Material: Decodable, Equatable {
            var id = ""
            var kind = ""
            var scope = ""
            var title = ""

            private enum CodingKeys: String, CodingKey { case id, kind, scope, title }

            init(id: String, kind: String, scope: String, title: String = "") {
                self.id = id; self.kind = kind; self.scope = scope; self.title = title
            }

            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
                kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? ""
                scope = (try? c.decodeIfPresent(String.self, forKey: .scope)) ?? ""
                title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
            }

            /// Only a known scope with an id is something to open.
            var usable: Bool { !id.isEmpty && (scope == "assignment" || scope == "course") }
        }

        private enum CodingKeys: String, CodingKey {
            case id, title, kind, status, url, day, material
            case courseName = "course_name", courseLabel = "course_label", dueTS = "due_ts", allDay = "all_day"
            case urlKind = "url_kind"
        }

        init(id: String = "", title: String = "", courseName: String = "", courseLabel: String = "",
             dueTS: Double? = nil, allDay: Bool = false, kind: String = "", status: String = "",
             url: String? = nil, urlKind: String? = nil, day: String = "", material: Material? = nil) {
            self.id = id; self.title = title; self.courseName = courseName; self.courseLabel = courseLabel
            self.dueTS = dueTS; self.allDay = allDay; self.kind = kind; self.status = status
            (self.url, self.urlKind) = Self.link(url, kind: urlKind)
            self.day = day; self.material = material?.usable == true ? material : nil
        }

        /// The url and its kind when both pass: a known kind, and the client's own
        /// check of the address (a stale or tampered payload cannot widen it).
        static func link(_ url: String?, kind: String?) -> (String?, String?) {
            guard let url, let kind, kind == "assignment" || kind == "quiz_page",
                  StudyLinkRule.check(url) != nil else { return (nil, nil) }
            return (url, kind)
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
            title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
            courseName = (try? c.decodeIfPresent(String.self, forKey: .courseName)) ?? ""
            courseLabel = (try? c.decodeIfPresent(String.self, forKey: .courseLabel)) ?? ""
            dueTS = try? c.decodeIfPresent(Double.self, forKey: .dueTS)
            allDay = (try? c.decodeIfPresent(Bool.self, forKey: .allDay)) ?? false
            kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? ""
            status = (try? c.decodeIfPresent(String.self, forKey: .status)) ?? ""
            (url, urlKind) = Self.link(try? c.decodeIfPresent(String.self, forKey: .url),
                                       kind: try? c.decodeIfPresent(String.self, forKey: .urlKind))
            day = (try? c.decodeIfPresent(String.self, forKey: .day)) ?? ""
            let made = try? c.decodeIfPresent(Material.self, forKey: .material)
            material = made?.usable == true ? made : nil
        }

        /// The course as a one-line row names it: the short label, else the
        /// course's name (the row truncates a long one).
        var course: String {
            let label = courseLabel.trimmingCharacters(in: .whitespacesAndNewlines)
            return label.isEmpty ? courseName.trimmingCharacters(in: .whitespacesAndNewlines) : label
        }

        /// The title a row shows; "Untitled" for a blank one.
        var shownTitle: String {
            let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
            return t.isEmpty ? "Untitled" : t
        }
    }

    /// The next quiz or exam within a week.
    struct Test: Decodable, Equatable {
        var id = ""
        var title = ""
        var courseName = ""
        var dueTS: Double? = nil

        private enum CodingKeys: String, CodingKey {
            case id, title
            case courseName = "course_name", dueTS = "due_ts"
        }

        init(id: String = "", title: String = "", courseName: String = "", dueTS: Double? = nil) {
            self.id = id; self.title = title; self.courseName = courseName; self.dueTS = dueTS
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? ""
            title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
            courseName = (try? c.decodeIfPresent(String.self, forKey: .courseName)) ?? ""
            dueTS = try? c.decodeIfPresent(Double.self, forKey: .dueTS)
        }
    }

    var dueToday: [Due] = []
    var dueTomorrowCount = 0
    var dueWeekCount = 0
    var missingCount = 0
    var nextTest: Test? = nil
    /// "2 due today: Lab 5 (CS 101), Essay draft"; empty when nothing is due today or tomorrow.
    var line = ""
    var sample = false
    /// Up to eight open assignments due from the start of today through the
    /// end of the seventh day after it, soonest first (today's among them).
    var upcoming: [Due] = []
    /// How many more the service left out of `upcoming`.
    var upcomingMore = 0
    /// Work Study got ready for what's coming (a quiz made, the slides that
    /// cover an assignment); absent from an older service.
    var ready: [Ready] = []
    /// Topics recent quizzes missed, with the slides behind them; absent from an older service.
    var weak: [Weak] = []
    /// Open work due today, then tomorrow (at most four), each with its link and
    /// the guide made for it: READY FOR YOU's first rows. Absent from an older service.
    var dueSoon: [Due] = []
    var dueSoonMore = 0

    private enum CodingKeys: String, CodingKey {
        case line, sample, upcoming, ready, weak
        case dueSoon = "due_soon", dueSoonMore = "due_soon_more"
        case dueToday = "due_today", dueTomorrowCount = "due_tomorrow_count", dueWeekCount = "due_week_count"
        case missingCount = "missing_count", nextTest = "next_test", upcomingMore = "upcoming_more"
    }

    init(dueToday: [Due] = [], dueTomorrowCount: Int = 0, dueWeekCount: Int = 0, missingCount: Int = 0,
         nextTest: Test? = nil, line: String = "", sample: Bool = false,
         upcoming: [Due] = [], upcomingMore: Int = 0, ready: [Ready] = [], weak: [Weak] = [],
         dueSoon: [Due] = [], dueSoonMore: Int = 0) {
        self.dueToday = dueToday; self.dueTomorrowCount = dueTomorrowCount; self.dueWeekCount = dueWeekCount
        self.missingCount = missingCount; self.nextTest = nextTest; self.line = line; self.sample = sample
        self.upcoming = upcoming; self.upcomingMore = max(0, upcomingMore)
        self.ready = ready; self.weak = weak
        self.dueSoon = dueSoon.filter { $0.day == "today" || $0.day == "tomorrow" }
        self.dueSoonMore = max(0, dueSoonMore)
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Each field decodes on its own; a bad one falls back to its default.
        dueToday = (try? c.decodeIfPresent([Due].self, forKey: .dueToday)) ?? []
        dueTomorrowCount = (try? c.decodeIfPresent(Int.self, forKey: .dueTomorrowCount)) ?? 0
        dueWeekCount = (try? c.decodeIfPresent(Int.self, forKey: .dueWeekCount)) ?? 0
        missingCount = (try? c.decodeIfPresent(Int.self, forKey: .missingCount)) ?? 0
        nextTest = try? c.decodeIfPresent(Test.self, forKey: .nextTest)
        line = ((try? c.decodeIfPresent(String.self, forKey: .line)) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        sample = (try? c.decodeIfPresent(Bool.self, forKey: .sample)) ?? false
        // An older service sends neither: no rows, and the SCHOOL line as before.
        upcoming = (try? c.decodeIfPresent([Due].self, forKey: .upcoming)) ?? []
        upcomingMore = max(0, (try? c.decodeIfPresent(Int.self, forKey: .upcomingMore)) ?? 0)
        // Newer still: what Study got ready, and the topics to look at again. A row
        // that doesn't decode is skipped; an older service sends neither.
        ready = c.schoolValue(.ready, SchoolLossy<Ready>.self)?.items ?? []
        weak = c.schoolValue(.weak, SchoolLossy<Weak>.self)?.items ?? []
        // Newest: what is due today and tomorrow. A row for another day is dropped.
        dueSoon = (c.schoolValue(.dueSoon, SchoolLossy<Due>.self)?.items ?? [])
            .filter { $0.day == "today" || $0.day == "tomorrow" }
        dueSoonMore = max(0, (try? c.decodeIfPresent(Int.self, forKey: .dueSoonMore)) ?? 0)
    }

    /// The Brief's SCHOOL line, or nil when there is nothing to say.
    var briefLine: String? { line.isEmpty ? nil : line }

    /// The Brief's SCHOOL rows show this many before "+N more in Study".
    static let briefUpcomingLimit = 3
    /// The Day tab's "School this week" shows this many before "+N more in Study".
    static let dayUpcomingLimit = 4
    /// The SCHOOL headline when nothing is due today or tomorrow but work is due later this week.
    static let quietHeadline = "Nothing due today or tomorrow"

    /// The Brief's SCHOOL headline: the service's line; with none, but work due
    /// later this week, says nothing is due sooner (the rows below name it).
    /// Empty hides the row, as before.
    var briefHeadline: String {
        if !line.isEmpty { return line }
        return upcoming.isEmpty ? "" : Self.quietHeadline
    }

    /// The rows under the Brief's SCHOOL headline: the soonest three and how
    /// many more Study holds. None when the one item due this week is the one
    /// the headline already names.
    var briefUpcoming: StudyUpcomingRows {
        if upcoming.count + upcomingMore <= 1 && !line.isEmpty { return StudyUpcomingRows() }
        return Self.rows(upcoming, extra: upcomingMore, limit: Self.briefUpcomingLimit)
    }

    /// The proactive Brief's school rows (R6's "On your plate"): the soonest
    /// three of this week's work, today's among them, and how many more Study
    /// holds. With no headline above the list, the one item due today shows too.
    var briefPlate: StudyUpcomingRows {
        Self.rows(upcoming, extra: upcomingMore, limit: Self.briefUpcomingLimit)
    }

    /// The Day tab's "School this week" for the day shown: what is due this
    /// week but not on that day (those are its own deadlines, by time or by
    /// `shownIDs`), the soonest four, and how many more Study holds.
    func dayUpcoming(dayStart: Double, dayEnd: Double, shownIDs: Set<String> = []) -> StudyUpcomingRows {
        let rest = upcoming.filter { item in
            if !item.id.isEmpty && shownIDs.contains(item.id) { return false }
            guard let due = item.dueTS, due.isFinite else { return true }
            return !(due >= dayStart && due < dayEnd)
        }
        return Self.rows(rest, extra: upcomingMore, limit: Self.dayUpcomingLimit)
    }

    /// The Study assignment ids behind the Day's school deadlines ("study:<id>").
    static func deadlineIDs(_ ids: [String]) -> Set<String> {
        Set(ids.compactMap { $0.hasPrefix("study:") ? String($0.dropFirst("study:".count)) : nil })
    }

    static func rows(_ items: [Due], extra: Int, limit: Int) -> StudyUpcomingRows {
        let shown = Array(items.prefix(max(0, limit)))
        return StudyUpcomingRows(items: shown, more: items.count - shown.count + max(0, extra))
    }

    /// Something Study got ready for an upcoming quiz, exam, lecture or
    /// assignment (`study.ready`): the line to show, the quiz it made (if
    /// any) and the slides that cover it.
    struct Ready: Decodable, Equatable, Identifiable {
        var id = ""
        /// "quiz", "exam", "lecture" or "assignment"; any other kind is kept as sent.
        var kind = ""
        var assignmentID = ""
        var course = ""
        var title = ""
        var dueTS: Double? = nil
        var text = ""
        var materialID: String? = nil
        var slides: [SchoolSlides] = []

        private enum CodingKeys: String, CodingKey {
            case id, kind, course, title, text, slides
            case assignmentID = "assignment_id", dueTS = "due_ts", materialID = "material_id"
        }

        init(id: String = "", kind: String = "", assignmentID: String = "", course: String = "", title: String = "",
             dueTS: Double? = nil, text: String = "", materialID: String? = nil, slides: [SchoolSlides] = []) {
            self.id = id; self.kind = kind; self.assignmentID = assignmentID; self.course = course; self.title = title
            self.dueTS = dueTS; self.text = text; self.materialID = materialID; self.slides = slides
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = c.schoolID(.id) ?? ""
            kind = c.schoolValue(.kind, String.self) ?? ""
            assignmentID = c.schoolID(.assignmentID) ?? ""
            course = c.schoolValue(.course, String.self) ?? ""
            title = c.schoolValue(.title, String.self) ?? ""
            dueTS = c.schoolValue(.dueTS, Double.self)
            text = c.schoolValue(.text, String.self) ?? ""
            materialID = c.schoolID(.materialID).flatMap { $0.trimmingCharacters(in: .whitespaces).isEmpty ? nil : $0 }
            slides = c.schoolValue(.slides, SchoolLossy<SchoolSlides>.self)?.items ?? []
        }
    }

    /// A topic recent quizzes missed (`study.weak`), and its slides.
    struct Weak: Decodable, Equatable, Identifiable {
        var id = ""
        var course = ""
        var topic = ""
        var count = 0
        var text = ""
        var slides: [SchoolSlides] = []

        private enum CodingKeys: String, CodingKey { case id, course, topic, count, text, slides }

        init(id: String = "", course: String = "", topic: String = "", count: Int = 0, text: String = "",
             slides: [SchoolSlides] = []) {
            self.id = id; self.course = course; self.topic = topic; self.count = count; self.text = text; self.slides = slides
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = c.schoolID(.id) ?? ""
            course = c.schoolValue(.course, String.self) ?? ""
            topic = c.schoolValue(.topic, String.self) ?? ""
            count = max(0, c.schoolValue(.count, Int.self) ?? 0)
            text = c.schoolValue(.text, String.self) ?? ""
            slides = c.schoolValue(.slides, SchoolLossy<SchoolSlides>.self)?.items ?? []
        }
    }

    /// The Brief shows this many ready items, then this many weak topics, before "+N more in Study".
    static let briefReadyLimit = 2
    static let briefWeakLimit = 1

    /// The Brief's study cards (today only): up to two ready items, then one
    /// weak topic, and how many more are only in Study. An item with no line
    /// to show is left out.
    var briefCards: StudyBriefCards {
        let readyCards = ready.compactMap(StudyBriefCard.init(ready:))
        let weakCards = weak.compactMap(StudyBriefCard.init(weak:))
        let shownReady = Array(readyCards.prefix(Self.briefReadyLimit))
        let shownWeak = Array(weakCards.prefix(Self.briefWeakLimit))
        return StudyBriefCards(cards: shownReady + shownWeak,
                               more: readyCards.count - shownReady.count + weakCards.count - shownWeak.count)
    }
}

/// Slides of one deck a Study item points at (`{"source_id","deck","pages"}`).
/// The Day and Brief's own wire shape: this file is compiled without Study.swift
/// (scripts/test_today_contract.sh), so the views turn it into StudySlideRef.
struct SchoolSlides: Decodable, Equatable, Hashable {
    var sourceID = ""
    var deck = ""
    /// 1-based page numbers, in order, without repeats.
    var pages: [Int] = []

    private enum CodingKeys: String, CodingKey { case sourceID = "source_id", deck, pages }

    init(sourceID: String = "", deck: String = "", pages: [Int] = []) {
        self.sourceID = sourceID; self.deck = deck; self.pages = pages
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sourceID = c.schoolID(.sourceID) ?? ""
        deck = (c.schoolValue(.deck, String.self) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        var seen = Set<Int>()
        pages = (c.schoolValue(.pages, SchoolLossy<Int>.self)?.items ?? []).filter { $0 > 0 && seen.insert($0).inserted }
    }
}

/// One slide (`{"source_id","page"}`), as a review or a make-from-pages request names it.
struct SchoolPage: Decodable, Equatable, Hashable {
    var sourceID: String
    var page: Int

    private enum CodingKeys: String, CodingKey { case sourceID = "source_id", page }

    init(sourceID: String, page: Int) { self.sourceID = sourceID; self.page = page }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = c.schoolID(.sourceID), !id.isEmpty, let page = c.schoolValue(.page, Int.self), page > 0 else {
            throw DecodingError.dataCorruptedError(forKey: .page, in: c, debugDescription: "not a slide")
        }
        sourceID = id; self.page = page
    }
}

/// A school deadline's "Review (15 min)" (`deadline.review`): how long, and which slides.
struct SchoolReview: Decodable, Equatable {
    var minutes = 15
    var pages: [SchoolPage] = []

    private enum CodingKeys: String, CodingKey { case minutes, pages }

    init(minutes: Int = 15, pages: [SchoolPage] = []) { self.minutes = minutes; self.pages = pages }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let given = c.schoolValue(.minutes, Int.self) ?? 15
        minutes = given > 0 && given <= 240 ? given : 15
        var seen = Set<SchoolPage>()
        pages = Array((c.schoolValue(.pages, SchoolLossy<SchoolPage>.self)?.items ?? [])
            .filter { seen.insert($0).inserted }.prefix(SchoolSlidesText.pageCap))
    }

    /// "Review (15 min)".
    var label: String { "Review (\(minutes) min)" }
    /// Only a review with slides to make cards from is offered.
    var offered: Bool { !pages.isEmpty }
}

/// The words and page lists for slides on Brief cards and Day rows.
enum SchoolSlidesText {
    /// /study/generate takes at most this many pages.
    static let pageCap = 40
    /// A card or row names this many decks before "+N".
    static let deckLimit = 2

    /// Every page of every deck, in order, once each, at most `pageCap`.
    static func pages(_ slides: [SchoolSlides]) -> [SchoolPage] {
        var seen = Set<SchoolPage>()
        var out: [SchoolPage] = []
        for set in slides where !set.sourceID.isEmpty {
            for page in set.pages where page > 0 {
                let ref = SchoolPage(sourceID: set.sourceID, page: page)
                if seen.insert(ref).inserted { out.append(ref) }
                if out.count == pageCap { return out }
            }
        }
        return out
    }

    /// "Lecture 7 · pp. 4–9, Lecture 8 · p. 2 +1": two decks, each with its
    /// pages (`pagesText` is StudyText.slidePages), then how many more decks.
    /// Nil with no slides.
    static func line(_ slides: [SchoolSlides], pagesText: ([Int]) -> String) -> String? {
        let decks = slides.filter { !$0.sourceID.isEmpty || !$0.deck.isEmpty }
        guard !decks.isEmpty else { return nil }
        let named = decks.prefix(deckLimit).map { set -> String in
            let name = set.deck.isEmpty ? "Slides" : set.deck
            return set.pages.isEmpty ? name : name + " · " + pagesText(set.pages)
        }
        let more = decks.count - named.count
        return named.joined(separator: ", ") + (more > 0 ? " +\(more)" : "")
    }

    /// A school deadline's or study block's "Lectures 7 and 8 cover it.", or nil.
    static func covers(_ text: String?) -> String? {
        let t = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }
}

/// One study card in the Brief: Study's line, the slides it is about, and one action.
struct StudyBriefCard: Equatable, Identifiable {
    enum Action: Equatable {
        /// Open the material Study already made.
        case open(materialID: String)
        /// Make flash cards from these slides (at most 40), titled `title`.
        case review(pages: [SchoolPage], title: String)
    }

    let id: String
    let text: String
    let slides: [SchoolSlides]
    let action: Action?

    /// Flash cards a "Review slides" or "Review (15 min)" makes.
    static let reviewCount = 8

    /// "Open quiz" or "Review slides"; nil with nothing to do.
    var actionTitle: String? {
        switch action {
        case .open: return "Open quiz"
        case .review: return "Review slides"
        case nil: return nil
        }
    }

    init(id: String, text: String, slides: [SchoolSlides] = [], action: Action? = nil) {
        self.id = id; self.text = text; self.slides = slides; self.action = action
    }

    /// A ready item: Open quiz when Study made one, else Review slides when it
    /// names slides. Nil when it has no line to show.
    init?(ready: StudyDaySummary.Ready) {
        let text = ready.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let pages = SchoolSlidesText.pages(ready.slides)
        let title = ready.title.trimmingCharacters(in: .whitespacesAndNewlines)
        let action: Action? = ready.materialID.map { .open(materialID: $0) }
            ?? (pages.isEmpty ? nil : .review(pages: pages, title: title.isEmpty ? "Review" : title))
        self.init(id: "ready:" + (ready.id.isEmpty ? text : ready.id), text: text, slides: ready.slides, action: action)
    }

    /// A weak topic: Review slides when it names slides. Nil when it has no line to show.
    init?(weak: StudyDaySummary.Weak) {
        let text = weak.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let pages = SchoolSlidesText.pages(weak.slides)
        let topic = weak.topic.trimmingCharacters(in: .whitespacesAndNewlines)
        self.init(id: "weak:" + (weak.id.isEmpty ? text : weak.id), text: text, slides: weak.slides,
                  action: pages.isEmpty ? nil : .review(pages: pages, title: topic.isEmpty ? "Review" : topic))
    }
}

/// The Brief's study cards and how many more are only in Study.
struct StudyBriefCards: Equatable {
    var cards: [StudyBriefCard] = []
    var more = 0

    var isEmpty: Bool { cards.isEmpty }
    /// "+2 more in Study", or nil.
    var moreLine: String? { more > 0 ? "+\(more) more in Study" : nil }
}

/// School work a Brief or Day row lists, and how many more are only in Study.
struct StudyUpcomingRows: Equatable {
    var items: [StudyDaySummary.Due] = []
    var more = 0

    var isEmpty: Bool { items.isEmpty && more == 0 }
    /// "+3 more in Study", or nil.
    var moreLine: String? { more > 0 ? "+\(more) more in Study" : nil }
}

// This file is compiled on its own by the pure checks, without
// ProactiveModels.swift's LossyList and lenient, so the school fields carry
// their own tolerant reading.

/// An array that keeps the rows that decode and skips the rest.
struct SchoolLossy<T: Decodable>: Decodable {
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
    fileprivate func schoolValue<T: Decodable>(_ key: Key, _ type: T.Type) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }

    /// A string, or a whole number written as one; nil otherwise.
    fileprivate func schoolID(_ key: Key) -> String? {
        if let s = schoolValue(key, String.self) { return s }
        if let i = schoolValue(key, Int.self) { return String(i) }
        return nil
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

/// "Add to calendar" on a Day study block: what a row offers, the request, and
/// where each block's add stands. The service rebuilds the block from its own
/// plan and books it through the verified calendar bridge
/// (`POST /study/blocks/{id}/add`, `.../undo`); the app never writes Calendar
/// here. A block whose JSON has no `added` key comes from a service without
/// that flow and offers nothing, exactly as before.
enum StudyBlockCalendar {
    static let idPrefix = "study-block:"
    static let fallbackError = "Couldn't add that to your calendar."
    static let undoFallbackError = "Couldn't undo that."
    /// Undo's "changed" reply without a sentence of its own.
    static let changedFallback = "The event was changed in Calendar, so Wisp left it there."
    static let addedText = "Added to calendar"
    static let errorCap = 160
    static let timeout: TimeInterval = 60

    enum Operation: Equatable { case add, undo }

    /// The facts of one study block the row needs.
    struct Block: Equatable {
        var id: String
        var kind: String
        var start: Double
        /// Nil: the service has no add flow (the key is missing).
        var added: Bool?
        var eventSourceID: String?
    }

    /// The block id as ONE path segment: everything but unreserved characters
    /// is percent-encoded, ":" and "/" included.
    static func pathSegment(_ id: String) -> String {
        var allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        allowed.insert(charactersIn: "-._~")
        return id.addingPercentEncoding(withAllowedCharacters: allowed) ?? id
    }

    static func path(_ operation: Operation, blockID: String) -> String {
        "/study/blocks/" + pathSegment(blockID) + (operation == .add ? "/add" : "/undo")
    }

    /// Add sends the day and zone the Day reads `/assistant/today` with; undo sends `{}`.
    static func body(_ operation: Operation, day: String, timezone: String) -> [String: String] {
        operation == .add ? ["day": day, "timezone": timezone] : [:]
    }

    /// The service's reason on one line, at most 160 characters, or the fallback.
    static func errorLine(_ text: String?, fallback: String = fallbackError) -> String {
        let flat = (text ?? "").split(whereSeparator: \.isNewline).joined(separator: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !flat.isEmpty else { return fallback }
        return flat.count > errorCap ? String(flat.prefix(errorCap - 1)) + "…" : flat
    }

    /// `{"ok", "status", "error"?, "block_id"?, "source_id"?}`, read leniently.
    struct Reply: Decodable {
        var ok: Bool?
        var status: String?
        var error: String?
        var source_id: String?
        private enum CodingKeys: String, CodingKey { case ok, status, error, source_id }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? nil
            status = (try? c.decodeIfPresent(String.self, forKey: .status)) ?? nil
            error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
            source_id = (try? c.decodeIfPresent(String.self, forKey: .source_id)) ?? nil
        }
    }

    enum Outcome: Equatable {
        case succeeded(sourceID: String?)
        case failed(String)
        /// The route is not there (an older service): hide the action, say nothing.
        case routeMissing
    }

    /// What a reply means. `status` nil is a transport error (no reply). A 404
    /// whose body is not this route's reply is a missing route; a 404 carrying
    /// `ok` is the route saying it couldn't find the block.
    static func outcome(_ operation: Operation, status: Int?, data: Data?) -> Outcome {
        let fallback = operation == .add ? fallbackError : undoFallbackError
        guard let status else { return .failed(fallback) }
        let reply = data.flatMap { try? JSONDecoder().decode(Reply.self, from: $0) }
        if status == 404, reply?.ok == nil { return .routeMissing }
        // Undo of an event already deleted in Calendar ("gone"): it is off the
        // calendar, which is what Undo asked for.
        if operation == .undo, reply?.ok == true, reply?.status == "gone" { return .succeeded(sourceID: nil) }
        if (200..<300).contains(status), reply?.ok == true { return .succeeded(sourceID: reply?.source_id) }
        // Undo of an event changed in Calendar ("changed"): the service left it
        // alone and says so; the row stays added, with Undo (Ledger.show).
        let changed = operation == .undo && reply?.status == "changed"
        return .failed(errorLine(reply?.error, fallback: changed ? changedFallback : fallback))
    }

    enum Phase: Equatable {
        case idle
        case adding
        /// Added this session, until a snapshot says so too.
        case added(sourceID: String?)
        case addFailed(String)
        case undoing
        /// Undone this session, until a snapshot says so too.
        case undone
        case undoFailed(String)
    }

    /// What the row shows.
    enum Show: Equatable {
        case none
        /// "Add to calendar"; `busy` while the request runs; `error` under the row.
        case add(busy: Bool, error: String?)
        /// "Added to calendar", with Undo when it can be undone.
        case added(undo: Bool, busy: Bool, error: String?)
    }

    /// Every block's add this session, and whether the routes exist.
    struct Ledger: Equatable {
        var phases: [String: Phase] = [:]
        /// When each block's last request finished (seconds since 1970): a
        /// snapshot made after it is the truth, whatever this session did.
        var finishedAt: [String: Double] = [:]
        /// A 404 from an older service hides the action for the session.
        var routeMissing = false

        func phase(_ id: String) -> Phase { phases[id] ?? .idle }

        func show(_ block: Block, now: Double) -> Show {
            guard block.kind == "study", block.id.hasPrefix(StudyBlockCalendar.idPrefix),
                  let added = block.added else { return .none }
            let canAct = !routeMissing
            let future = block.start > now
            switch phase(block.id) {
            case .adding: return .add(busy: true, error: nil)
            case .addFailed(let error): return canAct && future ? .add(busy: false, error: error) : .none
            case .added(let source):
                return .added(undo: canAct && (source != nil || block.eventSourceID != nil), busy: false, error: nil)
            case .undoing: return .added(undo: true, busy: true, error: nil)
            case .undoFailed(let error): return .added(undo: canAct, busy: false, error: error)
            case .undone: return canAct && future ? .add(busy: false, error: nil) : .none
            case .idle:
                if added { return .added(undo: canAct && block.eventSourceID != nil, busy: false, error: nil) }
                return canAct && future ? .add(busy: false, error: nil) : .none
            }
        }

        /// Starts a request; false when one is already running for the block or the route is gone.
        mutating func begin(_ operation: Operation, _ id: String) -> Bool {
            guard !routeMissing, phase(id) != .adding, phase(id) != .undoing else { return false }
            phases[id] = operation == .add ? .adding : .undoing
            return true
        }

        mutating func finish(_ operation: Operation, _ id: String, _ outcome: Outcome,
                             at time: Double = Date().timeIntervalSince1970) {
            finishedAt[id] = time
            switch (operation, outcome) {
            case (_, .routeMissing):
                routeMissing = true
                phases[id] = .idle
            case (.add, .succeeded(let source)): phases[id] = .added(sourceID: source)
            case (.add, .failed(let error)): phases[id] = .addFailed(error)
            case (.undo, .succeeded): phases[id] = .undone
            case (.undo, .failed(let error)): phases[id] = .undoFailed(error)
            }
        }

        /// A snapshot of the block: once it agrees with what this session did,
        /// it is the truth. One made after the last request finished
        /// (`generatedAt`) is the truth even when it disagrees: an event added
        /// here and then deleted in Calendar comes back `added: false`, so the
        /// row offers Add again rather than keeping "Added to calendar". An
        /// error line stays while the snapshot agrees with it.
        mutating func settle(_ id: String, snapshotAdded: Bool?, generatedAt: Double? = nil) {
            let fresh: Bool = snapshotAdded != nil
                && generatedAt.map { made in finishedAt[id].map { made >= $0 } ?? true } == true
            switch phase(id) {
            case .added where snapshotAdded == true, .undone where snapshotAdded == false: phases[id] = .idle
            case .added where fresh, .undone where fresh: phases[id] = .idle
            case .addFailed where fresh && snapshotAdded == true: phases[id] = .idle
            case .undoFailed where fresh && snapshotAdded == false: phases[id] = .idle
            default: break
            }
        }

        /// A fresh plan: every block this session acted on, against the plan's facts.
        mutating func settle(_ blocks: [Block], generatedAt: Double) {
            for block in blocks where phases[block.id] != nil {
                settle(block.id, snapshotAdded: block.added, generatedAt: generatedAt)
            }
        }
    }
}


/// The rule an assignment link must pass before the Brief offers it or opens
/// it, mirroring the service's (service/study/plan.py `link`): the person clicks
/// it, so a quiz's landing page is allowed, but nothing that starts, takes,
/// previews, submits or launches something, signs in or out, or carries
/// credentials, a one-time key or a fragment. Pure; checked in BriefSummaryChecks.
enum StudyLinkRule {
    /// A path segment, query name or value made of (or run together from) one of these is refused.
    static let refusedWords: Set<String> = ["take", "attempt", "attempts", "preview", "start", "history",
                                            "speedgrader", "externaltools", "lti", "launch", "login", "logout",
                                            "signin", "signout", "sso", "saml", "oauth"]
    /// A query name with one of these parts (split at _ and -) is a signed or one-time link.
    static let secretKeyParts: Set<String> = ["token", "sig", "signature", "auth", "apikey", "key", "session",
                                              "sid", "otp", "code", "nonce", "jwt", "ticket", "reset", "verify",
                                              "confirm", "login", "magic", "hash", "secret", "password"]
    static let quizSegments: Set<String> = ["quizzes", "quiz", "exams", "exam", "assessments"]

    private static func words(_ text: String) -> Set<String> {
        let lower = text.lowercased()
        let isWord: (Character) -> Bool = { $0.isASCII && ($0.isLetter || $0.isNumber) }
        var out = Set(lower.split(whereSeparator: { !isWord($0) }).map(String.init))
        out.insert(String(lower.filter(isWord)))
        return out
    }

    /// The url and "assignment" or "quiz_page", or nil when it must not be opened.
    static func check(_ raw: String) -> (url: URL, kind: String)? {
        guard raw.hasPrefix("https://"), raw.count <= 2048 else { return nil }
        // Printable ASCII only: no spaces, controls, backslash or fragment.
        for scalar in raw.unicodeScalars {
            if scalar.value < 0x21 || scalar.value > 0x7e || scalar == "\\" || scalar == "#" { return nil }
        }
        guard let parts = URLComponents(string: raw), parts.scheme == "https",
              let host = parts.host, !host.isEmpty, host.allSatisfy(\.isASCII),
              parts.user == nil, parts.password == nil, parts.port == nil, parts.fragment == nil,
              !(parts.percentEncodedHost ?? "").contains("%"),
              !host.lowercased().split(separator: ".").contains(where: { $0.hasPrefix("xn--") }),
              let url = parts.url else { return nil }
        let path: String = parts.path
        let query: String = parts.query ?? ""
        if path.contains("%") || query.contains("%") { return nil }     // encoded twice: refused
        let segments: [String] = path.lowercased().split(separator: "/").map(String.init)
        if segments.contains(where: { !words($0).isDisjoint(with: refusedWords) }) { return nil }
        if zip(segments, segments.dropFirst()).contains(where: { $0 == "submissions" && $1 == "new" }) { return nil }
        if !query.isEmpty {
            if !words(query).isDisjoint(with: refusedWords) { return nil }
            for item in parts.queryItems ?? [] {
                let name = item.name.lowercased()
                if !words(name).isDisjoint(with: refusedWords) || !words(item.value ?? "").isDisjoint(with: refusedWords) {
                    return nil
                }
                let pieces = Set(name.split(whereSeparator: { $0 == "_" || $0 == "-" }).map(String.init))
                if !pieces.isDisjoint(with: secretKeyParts) { return nil }
            }
        }
        return (url, quizSegments.isDisjoint(with: segments) ? "assignment" : "quiz_page")
    }
}


/// Cancel on a Day reminder row (2026-11-18, the owner: "give an option to cancel
/// overdue reminders and any reminder - don't hide it behind the menu"). One inline
/// question, then `POST /assistant/needs_you/<id>/cancel`, which deletes the exact
/// reminder in Reminders and reads back that it is gone (needs_you_api.cancel).
enum DayReminderCancel {
    static let question = "Delete from Reminders?"

    /// A dated Reminders reminder, named by the id the service's Needs you route takes.
    static func isCancellable(id: String, source: String, due: Double?) -> Bool {
        source == "reminders" && id.hasPrefix("reminders:") && (due?.isFinite ?? false)
    }

    /// Pure: what a reply means for the row.
    static func failure(status: Int, body: Data) -> String? {
        if status == 200 { return nil }
        let detail = ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["detail"] as? String
        return detail ?? "Reminders didn't confirm the delete (\(status))."
    }
}
