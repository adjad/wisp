import SwiftUI
import AppKit
import Contacts

private extension Theme {
    static let tileOpenFill = Color.white.opacity(0.14)
    static let tileOpenStroke = Color.white.opacity(0.32)
    static let tileFocusStroke = Color.white.opacity(0.6)
}

/// The notch's Brief tab: everything to know about the day. One sentence about
/// the day, then, when the proactive engine has run (`workspace.proactive`), its
/// overview, "Before you ask" cards, the to-dos from every source and each
/// thread's summary (ProactiveCards.swift). Without it, four count tiles (Today, Mail, Messages, Events) over one list:
/// the Top 3 by default, or the open tile's list in its place, so opening a
/// tile never pushes the tab past one list's height. What Wisp is keeping in
/// mind sits right under the sentence (on the first launch day, under what Wisp
/// does and when). Doing things belongs to Ask and Day.
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
    /// The focus ring shows only once the keyboard is in use, so the tile that
    /// takes focus when the tab opens doesn't look selected.
    @State private var keyboardNav = false
    /// "Keeping in mind" shows four lines until the person asks for all.
    @State private var showAllInMind = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The QA driver's full render: the proactive section without its scroll cap.
    private let uncapped: Bool
    /// Card, to-do and thread state; the app's shared one, or a QA scene's own.
    private let actions: ProactiveActions
    /// Clear and Mark done on the Top 3's Needs you rows.
    @ObservedObject private var needActions: NeedsYouActionsModel = NotchBriefView.needsYouActions
    static let needsYouActions = NeedsYouActionsModel(baseURL: WispClient.baseURL)

    @MainActor
    init(model: OverlayModel, today: TodayModel, open: String? = nil, uncapped: Bool = false,
         actions: ProactiveActions? = nil) {
        self.model = model
        self.today = today
        self.uncapped = uncapped
        self.actions = actions ?? .shared
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
        // While today has no summary: when it comes, and whether the first
        // one is being built (read again every 20 seconds while it is, so the
        // row goes if its retries run out; else every two minutes).
        .task(id: today.isShowingCurrentDay && composedBrief == nil) {
            guard today.isShowingCurrentDay, composedBrief == nil else { return }
            while !Task.isCancelled {
                await today.refreshBriefSchedule()
                try? await Task.sleep(for: .seconds(today.briefSchedule?.buildingFirst == true ? 20 : 120))
            }
        }
        .onChange(of: buildingFirstBrief) { _, _ in model.onResize() }
    }

    /// The first launch day's first summary is on its way and none is on screen.
    private var buildingFirstBrief: Bool {
        today.isShowingCurrentDay && composedBrief == nil && today.briefSchedule?.buildingFirst == true
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
            } else if buildingFirstBrief {
                ProcessingRow(label: "Building your first Brief…")
            }
            if let plan {
                let brief = BriefContent(plan: plan, today: today, composed: composedToday)
                if today.isShowingCurrentDay, let proactive = plan.workspace?.proactive {
                    // The proactive engine's Brief: one sentence, the work behind
                    // it, the top card, then three rows that open in place. It
                    // replaces the tiles and the Top 3.
                    lede(proactive.headline ?? brief.lede)
                    underLede(brief)
                    ProactiveBriefSection(proactive: proactive, model: model, today: today, actions: actions,
                                          events: restOfToday(plan),
                                          scrollCap: uncapped ? nil : Self.proactiveCap,
                                          coverage: Self.coverage(plan))
                    MailMemorySection(memory: MailMemoryCards.model, onResize: { model.onResize() })
                    HStack {
                        askChip(plan)
                        Spacer()
                    }
                } else {
                    lede(brief.lede)
                    underLede(brief)
                    legacy(plan, brief)
                }
            } else if today.error.isEmpty {
                // Nothing loaded yet, or the service is starting or busy: the
                // read retries itself, so this is a wait, never an error.
                ProcessingRow(label: Self.loadingLabel(today: today, starting: model.serviceStarting))
            } else {
                NotchChrome.chip("Try again", icon: "arrow.clockwise", help: "Reload your day") {
                    Task { await today.refresh() }
                }
                .disabled(today.busy)
            }
        }
    }

    /// The loading row's words: Wisp starting up on a cold launch, then a
    /// patient note if it takes a while; never a raw connection error.
    @MainActor static func loadingLabel(today: TodayModel, starting: Bool) -> String {
        today.outageNotice ?? (starting ? "Starting Wisp…" : "Loading your day…")
    }

    /// Every source's readability for the proactive Brief: Calendar and
    /// Reminders from the plan's own health, the rest from source_coverage.
    static func coverage(_ plan: TodayPlan) -> [SourceAccess] {
        ["calendar", "reminders", "email", "messages"].compactMap { plan.access($0) }
    }

    /// The tallest an opened row (to-dos, mail and texts) gets before it scrolls.
    static let proactiveCap: CGFloat = 300

    private func lede(_ text: String) -> some View {
        Text(text)
            .font(.wisp(size: 16.5)).foregroundStyle(Theme.textPrimary)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }

    /// Calendar events still to come or under way today, soonest first.
    private func restOfToday(_ plan: TodayPlan) -> [(start: Double, title: String)] {
        let now = today.now.timeIntervalSince1970
        return plan.displayBlocks
            .filter { ($0.kind == "calendar" || $0.kind == "manual") && !DayTimeline.isPlaceholderEvent($0.title)
                && ($0.end ?? $0.start + 1800) > now }
            .sorted { $0.start < $1.start }
            .map { (start: $0.start, title: DayTimeline.eventTitle($0.title)) }
    }

    /// Ask about this, and the way to the Day tab.
    private func footerChips(_ plan: TodayPlan) -> some View {
        HStack(spacing: 8) {
            askChip(plan)
            Spacer()
            NotchChrome.chip("See my day →", icon: nil, help: "The rest of today, hour by hour (⌘3)") {
                model.tab = .day
            }
        }
    }

    private func askChip(_ plan: TodayPlan) -> some View {
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
    }

    /// The Brief without the proactive engine: four tiles over the Top 3.
    @ViewBuilder private func legacy(_ plan: TodayPlan, _ brief: BriefContent) -> some View {
        tileStrip(brief)
        // The Top 3 and a tile's list take the same place, cross-fading;
        // the area clips so neither draws past it while the panel resizes.
        ZStack(alignment: .topLeading) {
            if let tile = open, let shown = brief.tiles[tile] {
                tileList(tile, shown.detail).id(tile.rawValue)
                    .transition(reduceMotion ? .identity : .opacity)
            } else {
                topList(brief.top, glance: brief.glance, unread: brief.unread, uncheckedMail: brief.uncheckedMail).id("top")
                    .transition(reduceMotion ? .identity : .opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .clipped()
        if !brief.suggestions.isEmpty { suggestionsRow(brief.suggestions) }
        MailMemorySection(memory: MailMemoryCards.model, onResize: { model.onResize() })
        footerChips(plan)
    }

    /// Right under the sentence, in both layouts: Contacts when macOS turned it
    /// down, what Wisp does and when (first launch day), and what it is keeping
    /// in mind (the day's goal among it).
    @ViewBuilder private func underLede(_ brief: BriefContent) -> some View {
        if today.isShowingCurrentDay, let contacts = BriefFix.contactsAccess() { accessRow(contacts) }
        if !brief.intro.isEmpty { introLines(brief.intro) }
        if !brief.summary.isEmpty { goalLines(brief.summary) }
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
        keyboardNav = true
        focused = (focused ?? open ?? .today).moved(offset)
        return .handled
    }

    private func tileButton(_ tile: BriefTile, count: Int?) -> some View {
        let isOpen = open == tile
        let isFocused = focused == tile && keyboardNav
        let quiet = (count ?? 0) == 0
        let hint: String = isOpen ? "Back to the Top 3 (\(tile.shortcut))" : "Show \(tile.title) (\(tile.shortcut))"
        return Button { keyboardNav = false; toggle(tile) } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(tile.title.uppercased())
                    .font(.wisp(size: 10, design: .monospaced))
                    .foregroundStyle(isOpen ? Theme.textSecondary : Theme.textMuted)
                Text(count.map(String.init) ?? "–")
                    .font(.wisp(size: 20, weight: .semibold)).monospacedDigit()
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
        .onKeyPress(.space) { keyboardNav = true; toggle(tile); return .handled }
        .onKeyPress(.return) { keyboardNav = true; toggle(tile); return .handled }
        .onKeyPress(.tab) { keyboardNav = true; return .ignored }
        .keyboardShortcut(KeyEquivalent(tile.shortcut), modifiers: [])
        .help(hint)
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

    /// The default view: the Top 3, then what mail and texts are about.
    private func topList(_ all: [BriefDetail.Line], glance: [BriefDetail.Line], unread: [String] = [],
                         uncheckedMail: UncheckedMail = UncheckedMail()) -> some View {
        // A row the service just confirmed cleared or done leaves at once; the
        // refreshed plan then fills its place.
        let lines = all.filter { line in line.need.map { !needActions.isHidden($0) } ?? true }
        let width = Self.labelWidth(lines + glance)
        return VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text("TOP 3").font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    .accessibilityAddTraits(.isHeader)
                if lines.isEmpty {
                    row(BriefDetail.Line(id: "empty", leading: "", text: BriefTiles.emptyTop(unread: unread, uncheckedMail: uncheckedMail), muted: true), width: width)
                } else {
                    ForEach(lines) { row($0, width: width) }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Top 3")
            if !glance.isEmpty {
                // A hairline keeps a Top 3 mail item from reading as the summary's first line.
                Rectangle().fill(Theme.hairline).frame(height: 1).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(glance) { row($0, width: width) }
                }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Mail and texts")
            }
        }
        .textSelection(.enabled)
    }

    private func tileList(_ tile: BriefTile, _ detail: BriefDetail) -> some View {
        let width = Self.labelWidth(detail.lines)
        return VStack(alignment: .leading, spacing: 6) {
            Text(tile.title.uppercased()).font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            ForEach(detail.lines) { row($0, width: width) }
            Button {
                if let action = detail.action { BriefFix.open(action) }
                else { NotchChrome.open(detail.source) }
            } label: {
                Text(detail.action.map(Permissions.fixLabel) ?? "Open \(NotchChrome.appName(detail.source)) ↗").font(.wisp(size: 12.5, weight: .medium))
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

    /// What Wisp is keeping in mind (the day's goal among it): four lines,
    /// then "Show all" for the rest.
    private func goalLines(_ lines: [String]) -> some View {
        let (shown, more) = ComposedBrief.inMind(lines, showAll: showAllInMind)
        let rows = shown.enumerated().map { BriefDetail.Line(id: "goal-\($0.offset)", leading: $0.offset == 0 ? "In mind" : "", text: $0.element) }
        let width = Self.labelWidth(rows)
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(rows) { row($0, width: width, secondary: true) }
            if more > 0 || showAllInMind && lines.count > ComposedBrief.keepingShown {
                Button(showAllInMind ? "Show fewer" : "Show all \(lines.count)") {
                    showAllInMind.toggle()
                    model.onResize()
                }
                .buttonStyle(.plain).font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.leading, width + Self.labelGap)
                .accessibilityHint(showAllInMind ? "Shows the first four" : "Shows \(more) more")
            }
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Keeping in mind")
    }

    /// The first launch day's line about what Wisp does on its own and when.
    private func introLines(_ lines: [String]) -> some View {
        let rows = lines.enumerated().map { BriefDetail.Line(id: "intro-\($0.offset)", leading: $0.offset == 0 ? "How it works" : "", text: $0.element) }
        return VStack(alignment: .leading, spacing: 4) {
            ForEach(rows) { row($0, width: Self.labelWidth(rows), secondary: true) }
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("How Wisp works")
    }

    /// One source Wisp can't read and the setting that fixes it, as the
    /// proactive Brief's and the Day tab's rows show theirs.
    private func accessRow(_ access: SourceAccess) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: access.symbol).font(.wisp(size: 10.5))
                .foregroundStyle(Theme.textMuted).frame(width: 15)
            Text(access.headline).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .help(access.detail)
            Spacer(minLength: 6)
            if let fix = access.fixLink {
                NotchChrome.small(Permissions.fixLabel(fix)) { BriefFix.open(fix) }
                    .help(access.detail)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// The tab's one row style: a mono label column, then the text. A row
    /// without a label keeps the column, so every list's text lines up. A Needs
    /// you row adds its trailing × (Clear, and Mark done for a reminder).
    @ViewBuilder private func row(_ line: BriefDetail.Line, width: CGFloat, secondary: Bool = false) -> some View {
        if let need = line.need, !needActions.actions(for: need).isEmpty {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    plainRow(line, width: width, secondary: secondary)
                    needControls(need)
                }
                if let failure = needActions.failures[need.id] {
                    Text(failure).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.leading, width + Self.labelGap)
                }
            }
            .accessibilityElement(children: .contain)
        } else {
            plainRow(line, width: width, secondary: secondary)
        }
    }

    private func needControls(_ need: NeedsYouRow) -> some View {
        NeedsYouRowControls(row: need, actions: needActions, onResize: { model.onResize() },
                            onConfirmed: { await today.refresh(afterMutation: true) })
    }

    private func plainRow(_ line: BriefDetail.Line, width: CGFloat, secondary: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Self.labelGap) {
            // An empty label still sets a baseline; an empty Text doesn't, and
            // throws the row's first-baseline alignment off.
            Text(line.leading.isEmpty ? " " : line.leading.uppercased())
                .font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .frame(width: width, alignment: .leading)
            Text(line.text).font(.wisp(size: line.paragraph ? 14 : 13.5))
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
                .font(.wisp(size: 18, weight: .semibold)).foregroundStyle(Theme.textPrimary)
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
                    .font(.wisp(size: 12)).foregroundStyle(Theme.textMuted)
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
    /// What Wisp does on its own and when, from the first launch day's summary.
    let intro: [String]
    /// Each tile's count and list, from the same lists.
    let tiles: [BriefTile: BriefTileContent]
    /// The three things that matter most, shown while no tile is open.
    let top: [BriefDetail.Line]
    /// What mail and texts are about, under the Top 3.
    let glance: [BriefDetail.Line]
    /// Today's sources Wisp couldn't read ("calendar", "reminders", "email",
    /// "messages"): the lede and the Top 3 never give an all-clear for them.
    let unread: [String]
    /// Google accounts Wisp couldn't check, named wherever mail is unread.
    let uncheckedMail: UncheckedMail

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
        // Calendar entries, whatever they're called ("1:1", an emoji, no title).
        let calendarBlocks = plan.displayBlocks.filter {
            ($0.kind == "calendar" || $0.kind == "manual") && !DayTimeline.isPlaceholderEvent($0.title)
        }
            .sorted { $0.start < $1.start }
        // Calendar entries only; learned away windows never make a day busy.
        let loadEvents = calendarBlocks.map { DayLoad.Event(title: DayTimeline.eventTitle($0.title), start: $0.start, end: $0.end) }
        let load = DayLoad.assess(events: loadEvents,
                                  allDay: plan.all_day.map(\.title), from: reference, dayStart: dayStart,
                                  isToday: isToday, zone: zone, locale: today.clockLocale)
        let dueToday = needs.filter { ($0.due_ts ?? .infinity) >= reference && ($0.due_ts ?? .infinity) <= dayEnd }
            .sorted { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }.map(\.title)
        let overdue = isToday ? needs.filter { ($0.due_ts ?? .infinity) < now }.map(\.title) : []
        let next = calendarBlocks.first { $0.start > reference }.map { (title: DayTimeline.eventTitle($0.title), time: today.clock($0.start)) }
        let weekday = today.selectedDate.formatted(Date.FormatStyle(timeZone: zone).weekday(.wide))
        let calendarKnown = plan.sources["calendar"]?.ready == true || !calendarBlocks.isEmpty
            || plan.all_day.contains { !DayTimeline.isPlaceholderEvent($0.title) }
        // What Wisp couldn't read, so nothing here reads as an all-clear for it.
        // A calendar whose events are on screen anyway isn't called unseen.
        let calendarAccess = calendarKnown ? nil : plan.access("calendar")
        let remindersAccess = plan.access("reminders")
        unread = isToday ? plan.unreadSources.filter { $0 != "calendar" || !calendarKnown } : []
        uncheckedMail = isToday ? plan.uncheckedMail : UncheckedMail()
        // The service's pick from the same attention items as the Day tab's Needs you.
        let needsList = isToday ? plan.workspace?.needs_you.flatMap { picked in
            DayTimeline.needsYouList(picked.items.map { NeedsYouInput(id: $0.id, kind: $0.kind, text: $0.text,
                                                                      due: $0.due_ts, allDay: $0.all_day ?? false,
                                                                      fingerprint: $0.fingerprint,
                                                                      canComplete: $0.can_complete ?? false) },
                                     more: picked.more, clock: today.clock)
        } : nil
        needsYou = needsList
        lede = DayTimeline.lede(weekday: weekday, isToday: isToday, load: calendarKnown ? load.verdict : nil,
                                dueToday: dueToday, overdue: overdue, next: next, othersNeedYou: needsList != nil,
                                openingOverride: time.opening(weekday: weekday, isToday: isToday,
                                                              load: calendarKnown ? load.verdict : nil, events: loadEvents),
                                dueReference: time.duePhrase(isToday: isToday), unread: unread,
                                uncheckedMail: uncheckedMail)

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
        // Said when none came yet: the real summary times once they're read.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let noSummary = today.briefSchedule.map {
            $0.noSummaryLine(hour: calendar.component(.hour, from: today.now), locale: today.clockLocale)
        } ?? BriefDetail.noSummaryYet

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
        let todayDetail = BriefTiles.todayDetail(todos, now: now, dayStart: dayStart, clock: today.clock,
                                                 reminders: isToday ? remindersAccess : nil)

        // Mail and Messages: the counts their lists give, and the regular
        // summaries the rows' dropdowns always showed.
        // Mail as access("email") sees it: read through Google counts as read.
        let mailTile = plan.mailTile
        let mailReady = mailTile?.ready == true
        let textsBlocked = coverage("messages")?.blocked == "full_disk_access"
        let textsReady = coverage("messages")?.ready == true && !textsBlocked
        let mailDetail = BriefDetail.mail(needing: mailNeeds, ready: mailReady, summary: summarySection(.mail),
                                          prose: isToday ? plan.workspace?.mail_summary?.text : nil,
                                          access: mailTile?.access, noSummary: noSummary)
        let textsDetail = BriefDetail.texts(needing: textNeeds, ready: coverage("messages")?.ready == true,
                                            needsAccess: textsBlocked, summary: summarySection(.messages),
                                            access: coverage("messages")?.access, noSummary: noSummary,
                                            age: { BriefTexts.age($0, now: now) })

        // Events: today's calendar, then what's coming up this week.
        let events = calendarBlocks.map { (title: $0.title, start: $0.start, end: $0.end) }
        let allDay = plan.all_day.filter { !DayTimeline.isPlaceholderEvent($0.title) }.map { DayTimeline.eventTitle($0.title) }
        let todayEvents = allDay.isEmpty && events.isEmpty ? []
            : BriefDetail.calendar(allDay: allDay, events: events, unsynced: false, clock: today.clock).lines
        let eventsDetail = BriefTiles.eventsDetail(today: todayEvents, unsynced: !calendarKnown,
                                                   soon: upcoming.flatMap { $0.isEmpty && !weekRead ? nil : $0 }, when: when,
                                                   access: calendarAccess)

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
        let rows = DayTimeline.briefLines(calendar: nil,
                                          // Partly read mail shows its headline here, not "Nothing needs a reply".
                                          mail: mailTile.map { (ready: $0.ready && $0.access?.readable != false, needing: mailNeeds) },
                                          texts: coverage("messages").map { (ready: $0.ready, needing: textNeeds) },
                                          textsNeedAccess: textsBlocked,
                                          mailAccess: mailTile?.access, textsAccess: coverage("messages")?.access,
                                          mailOverview: plan.workspace?.mail_overview?.text,
                                          textsOverview: plan.workspace?.texts_overview?.text, soon: .unknown)
        glance = BriefTiles.glance(rows, mailProse: isToday ? plan.workspace?.mail_summary?.text : nil)
        summary = composed?.keeping ?? []
        intro = composed?.intro ?? []
    }
}

/// Opens a Brief or Day fix link. Wisp's own Settings > Google (a Google
/// account to reconnect) opens on its Google pane, through the notification
/// AppDelegate observes (.wispOpenSettings); everything else is a System Settings page
/// (Permissions.openFix, which asks in Wisp first when macOS never has).
enum BriefFix {
    @MainActor static func open(_ action: BriefDetail.Action) {
        if action == BriefDetail.googleSettings {
            // SettingsView's @AppStorage pane, so an open Settings window switches too.
            UserDefaults.standard.set("google", forKey: "WispSettingsPane")
            NotificationCenter.default.post(name: .wispOpenSettings, object: nil)
            return
        }
        if let url = URL(string: action.url) { Permissions.openFix(url) }
    }

    /// Contacts as Brief and Day show it: a row only once macOS said no and
    /// Contacts is on in Wisp's Settings (ContactsReader.enabledKey). Reads the
    /// status only; never asks.
    static func contactsAccess() -> SourceAccess? {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        let enabled = UserDefaults.standard.object(forKey: ContactsReader.enabledKey) as? Bool ?? true
        return SourceAccess.contacts(denied: status == .denied || status == .restricted, enabled: enabled)
    }
}

/// A Brief row's trailing control (the Top 3's Needs you rows, and the proactive
/// Brief's to-dos and mail/text threads): a dim × that clears the row at once,
/// with no confirm step, plus a ✓ for Mark done when the row is a reminder.
/// Never a sheet or alert (the panel doesn't take focus). While the service
/// works it says so; the row leaves only on its confirmation.
struct NeedsYouRowControls: View {
    let row: NeedsYouRow
    @ObservedObject var actions: NeedsYouActionsModel
    var onResize: () -> Void = {}
    /// After the service confirmed: read the day again.
    var onConfirmed: () async -> Void = {}

    var body: some View {
        let offered = actions.actions(for: row)
        if offered.isEmpty {
            EmptyView()
        } else if let working = actions.busy[row.id] {
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text(working.progress).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
            }
            .fixedSize()
            .accessibilityElement(children: .combine)
            .accessibilityLabel(working.progress)
        } else {
            HStack(spacing: 4) {
                if offered.contains(.markDone) {
                    icon("checkmark", action: .markDone)
                }
                if offered.contains(.clear) {
                    icon("xmark", action: .clear)
                }
            }
            .fixedSize()
        }
    }

    private func icon(_ symbol: String, action: NeedsYouRowAction) -> some View {
        Button { answer(action) } label: {
            Image(systemName: symbol).font(.wisp(size: 9, weight: .semibold))
                .foregroundStyle(Theme.textMuted).frame(width: 16, height: 16).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!actions.busy.isEmpty)
        .help(row.help(action))
        .accessibilityLabel(action.accessibilityLabel(row.text))
    }

    private func answer(_ action: NeedsYouRowAction) {
        Task {
            let confirmed = await actions.perform(action, on: row)
            onResize()
            if confirmed { await onConfirmed() }
        }
    }
}
