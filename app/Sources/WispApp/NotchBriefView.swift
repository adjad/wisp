import SwiftUI
import AppKit

private extension Theme {
    static let tileOpenFill = Color.white.opacity(0.14)
    static let tileOpenStroke = Color.white.opacity(0.32)
    static let tileFocusStroke = Color.white.opacity(0.6)
}

/// The notch's Brief tab: what's going on, read only. One sentence about the
/// day, then four count tiles (Today, Mail, Messages, Events) over one list:
/// the Top 3 by default, or the open tile's list in its place, so opening a
/// tile never pushes the tab past one list's height. What Wisp is keeping in
/// mind closes the tab. Doing things belongs to Ask and Day.
///
/// Everything comes from the day's plan, the composed brief the service already
/// sent, and the week's upcoming items (`NotchDay.swift`, `BriefSummary.swift`),
/// never a model call. Refresh composes the scheduled brief now, which "Ask
/// about this" continues from.
struct NotchBriefView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    /// The open tile; one at a time. None shows the Top 3.
    @State var open: BriefTile? = nil
    @FocusState private var focused: BriefTile?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(model: OverlayModel, today: TodayModel, open: String? = nil) {
        self.model = model
        self.today = today
        // The QA driver names the old rows; each maps to the tile that holds it.
        _open = State(initialValue: open.flatMap { tag in
            switch tag {
            case "TODAY": return .today
            case "MAIL": return .mail
            case "TEXTS": return .messages
            case "CAL", "SOON": return .events
            default: return BriefTile(rawValue: tag)
            }
        })
    }

    private var plan: TodayPlan? { today.currentPlan }

    var body: some View {
        // Refresh presentation at minute boundaries while visible, without a
        // request or another background refresh loop. The model supplies the
        // clock so fixture dates and the Brief's timezone stay consistent.
        TimelineView(.periodic(from: BriefTimeContext(date: today.now, timezone: today.timezone).minuteStart, by: 60)) { _ in
            content
        }
        .environment(\.timeZone, today.timezone)
        .modifier(DayRefreshLoop(model: model, today: today))
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            heading
            if let notice = model.transientNotice { NotchChrome.notice(notice) }
            if !today.error.isEmpty { NotchChrome.notice(today.error) }
            if model.briefComposing {
                if let progress = model.dailySyncProgress {
                    SourceSyncTip(label: model.dailySyncLabel, progress: progress, sources: model.sourceSyncStatuses)
                }
                ProcessingRow(label: "Building your brief…")
            }
            if let plan {
                let brief = BriefContent(plan: plan, today: today, composed: composedToday)
                Text(brief.lede)
                    .font(.system(size: 16.5)).foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                tileStrip(brief)
                // The Top 3 and a tile's list take the same place, cross-fading;
                // the area clips so neither draws past it while the panel resizes.
                ZStack(alignment: .topLeading) {
                    if let tile = open, let shown = brief.tiles[tile] {
                        tileList(tile, shown.detail).id(tile.rawValue)
                            .transition(reduceMotion ? .identity : .opacity)
                    } else {
                        topList(brief.top).id("top")
                            .transition(reduceMotion ? .identity : .opacity)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .clipped()
                if !brief.suggestions.isEmpty { suggestionsRow(brief.suggestions) }
                HStack(spacing: 8) {
                    NotchChrome.chip("Ask about this", icon: "bubble.left", help: "Continue from this in Ask") {
                        // Continue the brief's saved chat only when that brief is the
                        // one on screen; otherwise ask about what is shown.
                        if let brief = model.brief, !brief.sessionId.isEmpty,
                           StoredBrief.shownIsLive(shown: composedBrief, live: (text: brief.text, at: brief.receivedAt)) {
                            model.askAboutBrief()
                        } else {
                            model.askAbout(BriefContent(plan: plan, today: today, composed: composedToday).lede, reference: "")
                        }
                    }
                    .disabled(!model.conversationSettled)
                    Spacer()
                    NotchChrome.chip("See my day →", icon: nil, help: "The rest of today, hour by hour (⌘3)") {
                        model.tab = .day
                    }
                }
                if !brief.summary.isEmpty { goalLines(brief.summary) }
            } else if today.error.isEmpty {
                ProcessingRow(label: "Loading your day…")
            }
        }
    }

    // MARK: - Tiles

    private func tileStrip(_ brief: BriefContent) -> some View {
        HStack(spacing: 8) {
            ForEach(BriefTile.allCases) { tile in
                tileButton(tile, count: brief.tiles[tile]?.count)
            }
        }
        // Arrow keys move between tiles; Tab and Shift-Tab do too, natively.
        .onKeyPress(.leftArrow) { moveFocus(-1) }
        .onKeyPress(.rightArrow) { moveFocus(1) }
        .onKeyPress(.upArrow) { moveFocus(-1) }
        .onKeyPress(.downArrow) { moveFocus(1) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Today at a glance")
    }

    private func moveFocus(_ offset: Int) -> KeyPress.Result {
        focused = (focused ?? open ?? .today).moved(offset)
        return .handled
    }

    private func tileButton(_ tile: BriefTile, count: Int?) -> some View {
        let isOpen = open == tile
        let isFocused = focused == tile
        let quiet = (count ?? 0) == 0
        return Button { toggle(tile) } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(tile.title.uppercased())
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(isOpen ? Theme.textSecondary : Theme.textMuted)
                Text(count.map(String.init) ?? "–")
                    .font(.system(size: 20, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(quiet && !isOpen ? Theme.textSecondary : Theme.textPrimary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 8).fill(isOpen ? Theme.tileOpenFill : Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(isFocused ? Theme.tileFocusStroke : isOpen ? Theme.tileOpenStroke : Theme.chipStroke,
                        lineWidth: isFocused ? 1.5 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .focusEffectDisabled()
        .focused($focused, equals: tile)
        .onKeyPress(.space) { toggle(tile); return .handled }
        .onKeyPress(.return) { toggle(tile); return .handled }
        .keyboardShortcut(KeyEquivalent(tile.shortcut), modifiers: [])
        .help(isOpen ? "Back to the Top 3 (\(tile.shortcut))" : "Show \(tile.title) (\(tile.shortcut))")
        .accessibilityLabel(count.map { "\(tile.title), \($0)" } ?? "\(tile.title), not read yet")
        .accessibilityValue(isOpen ? "Expanded" : "Collapsed")
        .accessibilityHint(isOpen ? "Shows the Top 3 again" : "Shows the \(tile.title) list in place of the Top 3")
    }

    /// Opening a tile swaps the list under the strip. A taller list grows the
    /// panel before it fades in, so it never renders clipped; a shorter one
    /// leaves the window as it is until the old list has gone
    /// (OverlayPanel.resizeToFit holds a shrink for the animation).
    private func toggle(_ tile: BriefTile) {
        let next = open == tile ? nil : tile
        if reduceMotion {
            open = next
            model.onResizeNow()
            model.onResize()
            return
        }
        withAnimation(.easeOut(duration: Self.rowAnimation)) { open = next }
        // Measure in this run-loop turn, before the first animated frame, and
        // again once SwiftUI has laid the change out.
        model.onResizeNow()
        model.onResize()
    }

    /// The list swap. Kept under OverlayPanel.shrinkSettle.
    static let rowAnimation: TimeInterval = 0.18

    // MARK: - Rows

    private func topList(_ lines: [BriefDetail.Line]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("TOP 3").font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            if lines.isEmpty {
                row(BriefDetail.Line(id: "empty", leading: "", text: "Nothing needs you right now", muted: true), width: Self.labelWidth(lines))
            } else {
                ForEach(lines) { row($0, width: Self.labelWidth(lines)) }
            }
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Top 3")
    }

    private func tileList(_ tile: BriefTile, _ detail: BriefDetail) -> some View {
        let width = Self.labelWidth(detail.lines)
        return VStack(alignment: .leading, spacing: 6) {
            Text(tile.title.uppercased()).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            ForEach(detail.lines) { row($0, width: width) }
            Button {
                if let action = detail.action, let url = URL(string: action.url) { NSWorkspace.shared.open(url) }
                else { NotchChrome.open(detail.source) }
            } label: {
                Text(detail.action?.label ?? "Open \(NotchChrome.appName(detail.source)) ↗").font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.textPrimary).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.leading, width + Self.labelGap)
            .padding(.top, 2)
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(tile.title)
    }

    /// What Wisp is keeping in mind (the day's goal among it), last in the tab.
    private func goalLines(_ lines: [String]) -> some View {
        let rows = lines.enumerated().map { BriefDetail.Line(id: "goal-\($0.offset)", leading: $0.offset == 0 ? "In mind" : "", text: $0.element) }
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(rows) { row($0, width: Self.labelWidth(rows), secondary: true) }
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Keeping in mind")
    }

    /// The tab's one row style: a mono label column, then the text. A row
    /// without a label keeps the column, so every list's text lines up.
    private func row(_ line: BriefDetail.Line, width: CGFloat, secondary: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Self.labelGap) {
            Text(line.leading.uppercased()).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .frame(width: width, alignment: .leading)
            Text(line.text).font(.system(size: line.paragraph ? 14 : 13.5))
                .foregroundStyle(line.muted ? Theme.textMuted : secondary ? Theme.textSecondary : Theme.textPrimary)
                .lineLimit(line.paragraph ? nil : 2).fixedSize(horizontal: false, vertical: true)
                .padding(.bottom, line.paragraph ? 3 : 0)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    static let labelGap: CGFloat = 10

    /// Room for "DUE 11:59 PM"; for "10:00 AM–11:30 AM" only when a row has a span.
    static func labelWidth(_ lines: [BriefDetail.Line]) -> CGFloat {
        lines.contains { $0.leading.contains("–") } ? 118 : 78
    }

    private func suggestionsRow(_ suggestions: [BriefTimeContext.Suggestion]) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { suggestionButtons(suggestions) }
            VStack(alignment: .leading, spacing: 6) { suggestionButtons(suggestions) }
        }
        .disabled(!model.conversationSettled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Suggestions for this time of day")
    }

    @ViewBuilder private func suggestionButtons(_ suggestions: [BriefTimeContext.Suggestion]) -> some View {
        ForEach(suggestions) { suggestion in
            NotchChrome.chip(suggestion.title, icon: "arrow.up.right",
                             help: "Open Ask with a prompt you can review and send") {
                model.askAbout(suggestion.prompt)
            }
        }
    }

    /// Today's composed Daily Summary, when the Brief shows today: the newer of
    /// the one this launch received (the live event or Refresh) and the one the
    /// service kept, which survives a relaunch.
    private var composedBrief: (text: String, at: Date)? {
        guard today.isShowingCurrentDay else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        let now = today.now
        return StoredBrief.newest(live: model.brief.map { (text: $0.text, at: $0.receivedAt) }, stored: today.storedBrief,
                                  sameDay: { calendar.isDate($0, inSameDayAs: now) })
    }

    private var composedToday: ComposedBrief? {
        composedBrief.map { ComposedBrief.parse($0.text) }
    }

    private var heading: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(today.selectedDate, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                .font(.system(size: 18, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            if !today.followsToday {
                NotchChrome.chip("Back to today", icon: "calendar", help: "Show today instead of the day picked in the planner") {
                    today.returnToToday()
                    Task { await today.refresh() }
                }
                .disabled(today.busy)
            }
            Spacer()
            if let updated = updatedAt {
                Text("Updated " + updated.formatted(date: .omitted, time: .shortened))
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
            NotchChrome.iconButton(model.briefComposing ? "ellipsis" : "arrow.clockwise",
                                   help: "Compose a new brief and reload your day") {
                model.runDailySummary()
                Task { await today.refresh() }
            }
            .disabled(model.briefComposing || today.busy)
        }
    }

    /// The newer of the composed brief and the day's plan.
    private var updatedAt: Date? {
        let planned = plan.map { Date(timeIntervalSince1970: $0.generated_at) }
        guard let brief = composedBrief else { return planned }
        return max(brief.at, planned ?? brief.at)
    }
}

/// The Brief's sentence, tiles, Top 3 and closing lines for one plan.
@MainActor
struct BriefContent {
    let lede: String
    let suggestions: [BriefTimeContext.Suggestion]
    /// Service-selected, structured needs-you rows, on today only: the first
    /// candidates for the Top 3.
    let needsYou: NeedsYouList?
    /// What Wisp is keeping in mind, from today's composed Daily Summary
    /// (the day's goal among it); empty without one.
    let summary: [String]
    /// Each tile's count and list, from the same lists.
    let tiles: [BriefTile: BriefTileContent]
    /// The three things that matter most, shown while no tile is open.
    let top: [BriefDetail.Line]

    init(plan: TodayPlan, today: TodayModel, composed: ComposedBrief?) {
        let now = today.now.timeIntervalSince1970
        let bounds = today.dayInterval
        let dayStart = bounds.start.timeIntervalSince1970, dayEnd = bounds.end.timeIntervalSince1970
        let isToday = today.isShowingCurrentDay
        let zone = today.timezone
        let time = BriefTimeContext(date: today.now, timezone: zone)
        suggestions = time.suggestions(isToday: isToday)
        let needs = (plan.workspace?.attention ?? []).filter { $0.source != "calendar" }
        let reference = isToday ? now : dayStart
        let calendarBlocks = plan.displayBlocks.filter {
            ($0.kind == "calendar" || $0.kind == "manual") && !DayTimeline.isJunk($0.title)
        }
            .sorted { $0.start < $1.start }
        // Calendar entries only; learned away windows never make a day busy.
        let loadEvents = calendarBlocks.map { DayLoad.Event(title: $0.title, start: $0.start, end: $0.end) }
        let load = DayLoad.assess(events: loadEvents,
                                  allDay: plan.all_day.map(\.title), from: reference, dayStart: dayStart,
                                  isToday: isToday, zone: zone, locale: today.clockLocale)
        let dueToday = needs.filter { ($0.due_ts ?? .infinity) >= reference && ($0.due_ts ?? .infinity) <= dayEnd }
            .sorted { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }.map(\.title)
        let overdue = isToday ? needs.filter { ($0.due_ts ?? .infinity) < now }.map(\.title) : []
        let next = calendarBlocks.first { $0.start > reference }.map { (title: $0.title, time: today.clock($0.start)) }
        let weekday = today.selectedDate.formatted(Date.FormatStyle(timeZone: zone).weekday(.wide))
        let calendarKnown = plan.sources["calendar"]?.ready == true || !calendarBlocks.isEmpty
            || plan.all_day.contains { !DayTimeline.isJunk($0.title) }
        // The service's pick from the same attention items as the Day tab's Needs you.
        let needsList = isToday ? plan.workspace?.needs_you.flatMap { picked in
            DayTimeline.needsYouList(picked.items.map { NeedsYouInput(id: $0.id, kind: $0.kind, text: $0.text,
                                                                      due: $0.due_ts, allDay: $0.all_day ?? false) },
                                     more: picked.more, clock: today.clock)
        } : nil
        needsYou = needsList
        lede = DayTimeline.lede(weekday: weekday, isToday: isToday, load: calendarKnown ? load.verdict : nil,
                                dueToday: dueToday, overdue: overdue, next: next, othersNeedYou: needsList != nil,
                                openingOverride: time.opening(weekday: weekday, isToday: isToday,
                                                              load: calendarKnown ? load.verdict : nil, events: loadEvents),
                                dueReference: time.duePhrase(isToday: isToday))

        func coverage(_ source: String) -> DailySourceCoverage? {
            plan.workspace?.source_coverage.first { $0.source == source }
        }
        func need(_ item: DailyAttentionItem) -> BriefNeed {
            BriefNeed(title: item.title, sender: item.sender ?? "", person: item.person ?? false,
                      received: item.received_ts, replied: item.replied ?? false)
        }
        let mailNeeds = needs.filter { $0.source == "email" }.map(need)
        let textNeeds = needs.filter { $0.source == "messages" }.map(need)
        let upcoming = UpcomingItem.parseList(today.upcomingReply).map {
            UpcomingItem.soon($0, after: reference, dayEnd: dayEnd)
        }
        func when(_ item: UpcomingItem) -> String {
            item.allDay ? Date(timeIntervalSince1970: item.when).formatted(Date.FormatStyle(timeZone: zone).weekday(.abbreviated))
                : NotchChrome.dayClock(item.when, zone: zone, locale: today.clockLocale)
        }
        // "Nothing in the next week" only once calendar and reminders are read.
        let weekRead = calendarKnown && coverage("reminders")?.ready == true
        // Today's section of the composed summary; nil (the list says none
        // came yet) only once the kept summary has been checked, so a relaunch
        // doesn't flash the note before it loads.
        func summarySection(_ kind: ComposedBrief.Kind) -> [String]? {
            guard isToday else { return [] }
            if let composed { return composed.lines(kind) }
            return today.storedBriefChecked ? nil : []
        }

        // Today: reminders and deadlines due by the end of the day (and what's
        // overdue), then the Day tab's tasks, each once.
        let reminders: [BriefTodo] = needs.filter { ["reminders", "wisp"].contains($0.source) && ($0.due_ts ?? .nan).isFinite }
            .map { BriefTodo(id: $0.id, title: $0.title, due: $0.due_ts, allDay: $0.all_day == true) }
        let deadlines: [BriefTodo] = plan.deadlines.map { BriefTodo(id: $0.id, title: $0.title, due: $0.due_ts, allDay: $0.all_day == true) }
        let dated = reminders + deadlines
        let active = plan.tasks.filter { $0.status == "active" && $0.day == plan.day }
        let datedTasks = active.filter { ($0.due_ts ?? .nan).isFinite }
            .map { BriefTodo(id: $0.id, title: $0.title, due: $0.due_ts) }
        let undated = active.filter { !($0.due_ts ?? .nan).isFinite }
            .map { BriefTodo(id: $0.id, title: $0.title, start: $0.pinned_start) }
        let todos = BriefTiles.todo(dated: dated + datedTasks, tasks: undated, now: now, dayStart: dayStart,
                                    dayEnd: dayEnd, isToday: isToday)
        let todayDetail = BriefTiles.todayDetail(todos, now: now, dayStart: dayStart, clock: today.clock)

        // Mail and Messages: the counts their lists give, and the regular
        // summaries the rows' dropdowns always showed.
        let mailReady = coverage("email")?.ready == true
        let textsBlocked = coverage("messages")?.blocked == "full_disk_access"
        let textsReady = coverage("messages")?.ready == true && !textsBlocked
        let mailDetail = BriefDetail.mail(needing: mailNeeds, ready: mailReady, summary: summarySection(.mail),
                                          prose: isToday ? plan.workspace?.mail_summary?.text : nil)
        let textsDetail = BriefDetail.texts(needing: textNeeds, ready: coverage("messages")?.ready == true,
                                            needsAccess: textsBlocked, summary: summarySection(.messages),
                                            age: { BriefTexts.age($0, now: now) })

        // Events: today's calendar, then what's coming up this week.
        let events = calendarBlocks.map { (title: $0.title, start: $0.start, end: $0.end) }
        let allDay = plan.all_day.map(\.title).filter { !DayTimeline.isJunk($0) }
        let todayEvents = allDay.isEmpty && events.isEmpty ? []
            : BriefDetail.calendar(allDay: allDay, events: events, unsynced: false, clock: today.clock).lines
        let eventsDetail = BriefTiles.eventsDetail(today: todayEvents, unsynced: !calendarKnown,
                                                   soon: upcoming.flatMap { $0.isEmpty && !weekRead ? nil : $0 }, when: when)

        tiles = [
            .today: BriefTileContent(count: todos.count, detail: todayDetail),
            .mail: BriefTileContent(count: mailReady ? BriefMail.shown(mailNeeds).count + BriefMail.privateCount(mailNeeds) : nil,
                                    detail: mailDetail),
            .messages: BriefTileContent(count: textsReady ? BriefTexts.waiting(textNeeds).count : nil, detail: textsDetail),
            .events: BriefTileContent(count: calendarKnown ? todayEvents.count : nil, detail: eventsDetail),
        ]
        let nextEvent = isToday ? calendarBlocks.first { $0.start > now }.map {
            BriefDetail.Line(id: "next-" + $0.id, leading: today.clock($0.start), text: $0.title)
        } : nil
        top = BriefTiles.top(picked: needsList?.rows ?? [], todos: todayDetail.lines, next: nextEvent)
        summary = composed?.cutDown ?? []
    }
}
