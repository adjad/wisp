import SwiftUI
import AppKit

/// The notch's Day tab: the rest of today on one clock. Calendar events, your
/// planned tasks, Wisp's suggestions, learned "usually busy" windows, plans
/// caught in your texts and "Needs you" reminders share one agenda in time
/// order, from now to the end of the day, under a two-line blurb.
///
/// Nothing reaches the plan on its own: a suggestion needs Keep or Move, a
/// caught plan needs Add, and "Not today" only sets aside a learned window for
/// this day (the planner's existing one-day ignore). Everything lands in Wisp's
/// planner, never in Calendar.
///
/// The full planner (task editor, pins, working hours, the daily email) stays
/// in the Wisp window, because this non-activating panel can't host sheets.
/// The app keeps the plan current in the background, so opening either tab
/// shows the latest loaded day immediately.
struct NotchDayView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    // Plans caught in texts and the stale-reminder questions come from shared
    // models (DayCards) that outlive a tab switch and load in the background
    // with the plan, so the Day tab's first frame already holds every card.
    @ObservedObject private var attention = DayCards.attention
    @State private var minutes = 45
    @State private var addingTask = false
    @FocusState private var taskFieldFocused: Bool
    @State private var ignoredCaught = IgnoredCaught.load()
    // Evening check-in and weekly goal pulse (CheckInView.swift): one tap per
    // task at the end of the day, so tomorrow's plan starts honest.
    @ObservedObject private var checkIn = CheckInModel.shared
    /// The Calendar block whose Move sub-panel is open from the timeline's ⋯ menu.
    @State private var movingEvent: CheckInEvent?
    /// The old-reminder questions show three until the person asks for all.
    @State private var showAllStale = false
    // Old undated reminders to keep or delete (StaleReminders.swift).
    @ObservedObject private var staleReminders = DayCards.staleReminders
    // The offer to move an event outside working hours (UserHoursView.swift).
    @ObservedObject private var hoursOffer = DayCards.hoursOffer
    // To-dos the proactive engine found (ProactiveCards.swift): check-offs and
    // Add to Day are shared with the Brief.
    @ObservedObject private var proactive = ProactiveActions.shared
    // "Add to calendar" on study blocks: where each add stands this session.
    @ObservedObject private var studyAdds = StudyBlockAdds.shared

    /// The agenda's time column. It grows with the Text size setting so
    /// "12:30 PM" keeps its room.
    private var timeColumn: CGFloat { 62 * Theme.textScale }
    /// From a row's bar to its title.
    private static let barInset: CGFloat = 12
    /// Things the agenda shows before Show more, so the tab fits the notch.
    private static let agendaRows = 8
    @State private var showWholeAgenda = false
    /// What ended in the last two hours, opened from its one-line summary.
    @State private var showEarlier = false
    /// Wide Later today shows one fewer row while the left half runs more than
    /// a third taller than the right (DayTimeline.trimLater), decided once per
    /// plan and layout so it never flips on its own result.
    @State private var laterTrim: (key: String, trimmed: Bool)?
    /// This page's decision, else the last one made (DayLaterTrimMemory): the
    /// Day page is built afresh on every switch to it, and starting untrimmed
    /// laid the wide Day out with every Later row, measured the halves and
    /// took a row away a pass later, so the panel's height changed course
    /// part-way through the switch.
    private var currentLaterTrim: (key: String, trimmed: Bool)? { laterTrim ?? DayLaterTrimMemory.last }
    private var laterTrimmed: Bool { halves && currentLaterTrim?.trimmed == true }
    /// The last Remove, while its Undo bar shows: the ids it sent, so Undo brings
    /// a grouped review back as one action.
    @State private var lastRemoved: (ids: [String], notice: String)?
    @State private var lastRemovedTask: Task<Void, Never>?
    /// A reminder row's Cancel (DayReminderCancel): the row asking "Delete from
    /// Reminders?", the one being deleted, and why one wasn't.
    @State private var cancelAsking: String?
    @State private var cancelBusy: String?
    @State private var cancelFailed: [String: String] = [:]
    /// How long the Undo bar stays after Remove.
    private static let removeUndoSeconds: UInt64 = 8

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    /// Which optional pieces of the tab are showing. Only these flags drive the
    /// fade, never the plan's data, so a routine refresh doesn't animate.
    private struct Showing: Equatable {
        var notice, error, stale, removed, otherDay, loading, earlier, wholeAgenda, addRow: Bool
    }

    private var showing: Showing {
        Showing(notice: model.transientNotice != nil, error: !today.error.isEmpty,
                stale: today.staleNotice != nil, removed: lastRemoved != nil,
                otherDay: !today.followsToday, loading: plan == nil,
                earlier: showEarlier, wholeAgenda: showWholeAgenda,
                addRow: addingTask || !model.todayTaskTitle.isEmpty)
    }

    /// The wide panel (OverlayView sets it from the shared two-column choice,
    /// NotchWide): the day in two halves when the panel is wide enough.
    private let wide: Bool
    /// Between the halves, the hairline in the middle (as the Brief's zones).
    private static let halfGap: CGFloat = 28

    init(model: OverlayModel, today: TodayModel, wide: Bool = false) {
        self.model = model
        self.today = today
        self.wide = wide
    }

    /// Whether the day sits in two halves: from the width the panel lays the
    /// page out at, decided in the same update as the width changes, by the
    /// Brief's rule (BriefPlate.sideBySide) so both tabs split at one width.
    private var halves: Bool {
        guard wide else { return false }
        let panel: CGFloat = DisplayGeometry.shared.layout.widened(true).expandedWidth
        return BriefPlate.sideBySide(width: panel - 2 * OverlayView.sideInset, textScale: Theme.textScale)
    }

    private var plan: TodayPlan? { today.currentPlan }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            notices
            // The summary and the week row run the panel's whole width, above the halves.
            dayHead
            // In the wide panel, two halves: the left runs in today's order up to
            // and including Old reminders, the right holds what follows. In the
            // normal panel the halves stack with the same spacing as one column.
            DayHalves(sideBySide: halves, gap: Self.halfGap, stackGap: 10) {
                VStack(alignment: .leading, spacing: 10) { leadHalf }
                    .background(GeometryReader { geo in
                        Color.clear.preference(key: DayHalfHeights.self, value: ["left": geo.size.height])
                    })
                Rectangle().fill(Theme.hairline).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 10) { restHalf }
                    .background(GeometryReader { geo in
                        Color.clear.preference(key: DayHalfHeights.self, value: ["right": geo.size.height])
                    })
            }
            if plan != nil && (addingTask || !model.todayTaskTitle.isEmpty) { addTaskRow.transition(.opacity) }
            ReportDraftCard(draft: today.report, title: "Report a problem with this plan",
                            detailsLabel: "Include planner details", save: { model.saveReport(today.report) })
            footer
        }
        // Content fades in while the panel's height follows the same spring.
        .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: showing)
        .environment(\.timeZone, today.timezone)
        .modifier(DayRefreshLoop(model: model, today: today))
        // A change anywhere (a sync, an answer from another place) refreshes the
        // stale-reminder questions now instead of at the next minute.
        .onReceive(NotificationCenter.default.publisher(for: .wispAssistantChanged)) { _ in
            Task { await DayCards.refresh(force: true); model.onResize() }
        }
        .onChange(of: model.checkInDue) { _, due in
            guard due, today.isShowingCurrentDay else { return }
            Task { await checkIn.load(day: today.day, timezone: today.timezone.identifier); model.onResize() }
        }
        .onChange(of: plan?.generated_at) { _, _ in planChanged() }
        .onPreferenceChange(DayHalfHeights.self) { heights in balanceHalves(heights) }
        // The week row's other days: read once the plan is on screen; the
        // task ends (and its read with it) when the Day closes.
        .task(id: weekReadKey) { if !weekReadKey.isEmpty { await readWeek() } }
        .onDisappear { today.weekRowClosed() }
        // A cancel the service answered: the plan is read again, so a cancelled
        // block goes and a refused one shows the fresh facts the service now has.
        .onChange(of: checkIn.cancelling) { before, now in
            guard !before.isSubset(of: now) else { return }
            Task { await today.refresh(afterMutation: true); model.onResize() }
        }
    }

    /// Above everything, the whole width: notices, the check-in.
    @ViewBuilder private var notices: some View {
            if let notice = model.transientNotice { NotchChrome.notice(notice).transition(.opacity) }
            if !today.error.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    NotchChrome.notice(today.error)
                    NotchChrome.chip("Try again", icon: "arrow.clockwise", help: "Reload your day") {
                        Task { await today.refresh() }
                    }
                    .disabled(today.busy)
                }
                .transition(.opacity)
            }
            if let stale = today.staleNotice {
                // Reads keep failing: say how old the day on screen is, and hold
                // back the buttons that would change it.
                Text(stale).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity)
            }
            if let removed = lastRemoved { removedRow(removed).transition(.opacity) }
            if !today.followsToday { otherDayRow.transition(.opacity) }
            if today.isShowingCurrentDay {
                CheckInSection(model: checkIn, day: today.day, timezone: today.timezone,
                               clock: today.clock,
                               onAsk: { model.askAbout($0) },
                               onOpenEvent: openInCalendar,
                               onChanged: { Task { await today.refresh(); model.onResize() } })
                GoalPulseSection(model: checkIn)
            }
    }

    /// The left half (or the top, in one column): the day's warnings, the
    /// agenda and Old reminders.
    @ViewBuilder private var leadHalf: some View {
            if let plan {
                dayLead(plan)
                    .disabled(today.planIsStale)
                    .transition(.opacity)
            } else if today.error.isEmpty {
                ProcessingRow(label: NotchBriefView.loadingLabel(today: today, starting: model.serviceStarting))
                    .transition(.opacity)
            }
    }

    /// The right half (or what follows, in one column): Still to fit, School
    /// this week. Empty, the left half takes the whole width.
    @ViewBuilder private var restHalf: some View {
        if let plan {
            dayRest(plan)
                .disabled(today.planIsStale)
                .transition(.opacity)
        }
    }

    /// A fresh plan for the shown day: a timeline cancel whose block changed or
    /// left is dropped before it is sent, and a Move panel for such a block is
    /// closed. No plan (a failed refresh, a day change) proves nothing and
    /// changes nothing.
    private func planChanged() {
        guard let plan else { return }
        let events = plan.displayBlocks.compactMap(\.calendarEvent)
        checkIn.reconcileTimelineCancels(events)
        if let movingEvent, !events.contains(where: { CheckInModel.sameOccurrence($0, movingEvent) }) { closeMove() }
        // A study block's add or undo from this session gives way to a newer
        // plan (an event deleted in Calendar comes back `added: false`).
        let blocks: [StudyBlockCalendar.Block] = plan.displayBlocks.map(\.calendarAddFacts)
        if blocks.contains(where: { studyAdds.ledger.phases[$0.id] != nil }) {
            studyAdds.ledger.settle(blocks, generatedAt: plan.generated_at)
        }
    }

    /// Decides, once per plan, text size and wide layout, whether Later today
    /// shows one fewer row to bring the halves closer (never in one column, nor
    /// while the whole list is open).
    private func balanceHalves(_ heights: [String: CGFloat]) {
        let left: CGFloat = heights["left"] ?? 0, right: CGFloat = heights["right"] ?? 0
        guard halves, !showWholeAgenda, today.isShowingCurrentDay, left > 0 else { return }
        let key: String = "\(plan?.generated_at ?? 0)|\(Theme.textScale)|\(today.day)|\(staleReminders.asks.count)"
        let current = currentLaterTrim
        guard current?.key != key else { return }
        let trimmed: Bool = DayTimeline.trimLater(trimmed: current?.trimmed ?? false, left: Double(left), right: Double(right),
                                                  rowAllowance: Double(44 * Theme.textScale))
        laterTrim = (key, trimmed)
        DayLaterTrimMemory.last = (key, trimmed)
        if trimmed { model.onResize() }
    }

    /// The exact occurrence when Calendar still has it; else Calendar itself.
    private func openInCalendar(_ event: CheckInEvent) {
        let url = event.sourceId.flatMap { id in
            event.whenTs.flatMap { model.showCalendarEventURL?(id, $0) }
        } ?? URL(string: "ical://")!
        NSWorkspace.shared.open(url)
    }

    /// The Calendar block behind a timeline item, as the check-in card sees an
    /// event; nil for other blocks and when the service didn't describe it.
    private func calendarEvent(for item: DayItem) -> CheckInEvent? {
        guard item.style == .calendar, let id = item.blockID else { return nil }
        return plan?.displayBlocks.first { $0.id == id }?.calendarEvent
    }

    private func openMove(_ event: CheckInEvent) {
        movingEvent = event
        model.onResize()
    }

    private func closeMove() {
        guard movingEvent != nil else { return }
        movingEvent = nil
        model.onResize()
    }

    /// Old undated reminders: keep or delete, one quiet row each. Delete runs only
    /// on its own click and goes through Reminders' verified delete.
    private var staleSection: some View {
        let asks = staleReminders.asks
        let shown = showAllStale ? asks : Array(asks.prefix(3))
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Old reminders").font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                if asks.count > 1 {
                    NotchChrome.small("Keep all") { Task { await staleReminders.keepAll(); model.onResize() } }
                        .disabled(staleReminders.answering != nil)
                        .help("Keep every reminder listed here; Wisp won't ask about them again. Nothing changes in Reminders.")
                }
            }
            Text(StaleRemindersModel.explanation).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(shown) { ask in staleRow(ask) }
            if asks.count > 3 {
                Button(showAllStale ? "Show fewer" : "Show \(asks.count - 3) more") {
                    showAllStale.toggle()
                    model.onResize()
                }
                .buttonStyle(.plain).font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            }
        }
        .padding(.top, 2)
        .accessibilityElement(children: .contain)
    }

    private func staleRow(_ ask: StaleReminderAsk) -> some View {
        let busy = staleReminders.answering != nil
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(ask.title).font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .help(ask.title)
                    Text(ask.detail).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
                Spacer(minLength: 8)
                if staleReminders.answering == ask.sourceID {
                    ProgressView().controlSize(.small)
                } else {
                    NotchChrome.small("Keep") { Task { await staleReminders.keep(ask); model.onResize() } }
                        .disabled(busy)
                        .help("Keep this reminder; Wisp won't ask about it again")
                        .accessibilityLabel("Keep \(ask.title)")
                    if !staleReminders.deleteBlocked.contains(ask.sourceID) {
                        NotchChrome.small("Delete") { Task { await staleReminders.delete(ask); model.onResize() } }
                            .disabled(busy)
                            .help("Delete this reminder from Reminders")
                            .accessibilityLabel("Delete \(ask.title) from Reminders")
                    }
                }
            }
            if let failure = staleReminders.failures[ask.sourceID] {
                Text(failure).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(ask.title), \(ask.detail)")
    }

    /// Calendar and Reminders when Wisp can't read them, and Contacts when macOS
    /// turned it down (on today): why, and the setting that fixes it. Nothing
    /// for a source that is fine.
    private func unreadAccess(_ plan: TodayPlan) -> [SourceAccess] {
        ["calendar", "reminders"].compactMap { plan.access($0) }.filter { !$0.readable }
            + [today.isShowingCurrentDay ? BriefFix.contactsAccess() : nil].compactMap { $0 }
    }

    private func accessRow(_ access: SourceAccess) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: access.symbol).font(.wisp(size: 10.5))
                .foregroundStyle(Theme.textMuted).frame(width: 15)
            VStack(alignment: .leading, spacing: 1) {
                Text(access.headline).font(.wisp(size: 12.5, weight: .medium)).foregroundStyle(Theme.textPrimary)
                Text(access.detail).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            // Day's rows are System Settings pages (Calendars, Reminders, Contacts).
            if let fix = access.fixLink, let url = URL(string: fix.url) {
                NotchChrome.small(Permissions.fixLabel(fix)) { Permissions.openFix(url) }
            }
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: The head: summary and week row

    /// The whole width above the halves: why Wisp can't read a source, the
    /// summary sentence with the all-day items at its right, then the week row.
    /// The week row is there from the first frame (dates need no plan), so the
    /// panel never jumps when the plan arrives under it.
    @ViewBuilder private var dayHead: some View {
        if let plan {
            let timeline = timeline(plan)
            // Calendar's own row would repeat the summary, which says it when
            // Wisp can't read the calendar, with the same Allow… beside it.
            let unread = unreadAccess(plan).filter { $0.source != "calendar" }
            Group {
                ForEach(unread, id: \.source) { accessRow($0) }
                summaryRow(plan, timeline: timeline, calendar: plan.calendarState)
            }
            .disabled(today.planIsStale)
            .transition(.opacity)
            weekRow(load: shownLoad(plan, timeline: timeline))
        } else {
            weekRow(load: nil)
        }
    }

    /// "Three things on the clock and two due today." with ALL DAY chips at its right.
    private func summaryRow(_ plan: TodayPlan, timeline: DayTimeline, calendar: DayCalendarState) -> some View {
        let blurb: String = DayTimeline.blurb(timeline, needs: plan.dayNeeds, isToday: today.isShowingCurrentDay,
                                              endOfDay: today.dayInterval.end.timeIntervalSince1970, clock: today.clock,
                                              dayStart: today.dayInterval.start.timeIntervalSince1970,
                                              calendar: calendar)
        // The existing fix: Wisp's Calendar permission page, as the access row had it.
        let fix: BriefDetail.Action? = calendar == .unread
            ? (plan.access("calendar")?.fixLink ?? BriefDetail.calendarsAccess) : nil
        return HStack(alignment: .center, spacing: 12) {
            // Three lines at most: beside the chip at larger text sizes two cut it.
            Text(blurb)
                .font(.wisp(size: 13.5)).foregroundStyle(Theme.textPrimary)
                .lineLimit(3).fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 10)
                .overlay(alignment: .leading) { Rectangle().fill(Theme.chipStroke).frame(width: 2) }
                .frame(maxWidth: .infinity, alignment: .leading)
            if let fix, let url = URL(string: fix.url) {
                NotchChrome.small(Permissions.fixLabel(fix)) { Permissions.openFix(url) }
            } else if !timeline.allDay.isEmpty {
                allDayChip(timeline.allDay, booked: bookedTimes(plan))
            }
        }
    }

    /// "ALL DAY  Campus Fall Fest", with "+N" for more; an assignment that
    /// already has time held reads "✓ 3:30 PM".
    private func allDayChip(_ titles: [String], booked: [String: String]) -> some View {
        let first: String = titles[0]
        let held: String? = booked[Self.looseTitle(first)]
        let shown: String = held.map { "\(first)  ✓ \($0)" } ?? first
        let spoken: String = "All day: " + titles.joined(separator: ", ")
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("ALL DAY").font(.wisp(size: 9.5, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .fixedSize()
            Text(shown).font(.wisp(size: 12, weight: .semibold))
                .foregroundStyle(held == nil ? Theme.textPrimary : Theme.textSecondary)
                .lineLimit(1).truncationMode(.tail)
            if titles.count > 1 {
                Text("+\(titles.count - 1)").font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted).fixedSize()
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 4)
        .background(Capsule().fill(DayInk.calendarFill))
        .overlay(Capsule().stroke(DayInk.calendarStroke, lineWidth: 1))
        .frame(maxWidth: 280, alignment: .trailing)
        .fixedSize(horizontal: false, vertical: true)
        .help(titles.joined(separator: "\n"))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// The shown day's card load: the summary's own counts (what is still
    /// ahead, on today) and what its calendar and plan blocks hold. Nil when
    /// Wisp can't read Calendar: the card then shows its date alone.
    private func shownLoad(_ plan: TodayPlan, timeline: DayTimeline) -> DayLoadBar? {
        guard plan.calendarState.isRead else { return nil }
        let bounds = today.dayInterval
        let dayStart: Double = bounds.start.timeIntervalSince1970
        let dayEnd: Double = bounds.end.timeIntervalSince1970
        let items: [DayItem] = timeline.items.filter { !$0.past }
        let spans = DayWeek.spans(blockInputs(plan), dayStart: dayStart, dayEnd: dayEnd)
        let span = DayWeek.span(workStart: dayStart + Double(plan.preferences.start_minute * 60),
                                workEnd: dayStart + Double(plan.preferences.end_minute * 60),
                                spans: spans, dayStart: dayStart, dayEnd: dayEnd)
        return DayWeek.load(onClock: items.filter(DayTimeline.isOnTheClock).count,
                            due: items.filter(DayTimeline.isDueBy).count, spans: spans, span: span,
                            now: today.isShowingCurrentDay ? today.now.timeIntervalSince1970 : nil)
    }

    private func blockInputs(_ plan: TodayPlan) -> [DayBlockInput] {
        plan.displayBlocks.map { DayBlockInput(id: $0.id, title: $0.title, start: $0.start, end: $0.end, kind: $0.kind,
                                               taskID: $0.task_id, evidenceID: $0.evidence_id) }
    }

    /// The person's calendar, for the week's order and its first day.
    private var weekCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        calendar.firstWeekday = Calendar.autoupdatingCurrent.firstWeekday
        return calendar
    }

    private var shownNoon: Date { today.dayInterval.start.addingTimeInterval(12 * 3600) }

    /// The week holding the shown day, in the person's calendar week order:
    /// the shown day's card from its own plan, the others from the week route
    /// once it was read, and none of them while Wisp can't read Calendar.
    private func weekCards(load: DayLoadBar?) -> [DayWeekCard] {
        let calendar: Calendar = weekCalendar
        var others: [String: DayLoadBar] = [:]
        if let plan, plan.calendarState.isRead,
           let reply = today.weekReply(start: DayWeek.start(of: shownNoon, calendar: calendar)) {
            others = reply.loads(workStart: plan.preferences.start_minute, workEnd: plan.preferences.end_minute)
        }
        return DayWeek.cards(shown: shownNoon, today: today.now, calendar: calendar, shownLoad: load, weekLoads: others)
    }

    /// The week row: full height when the week was already read as the Day
    /// opened, else compact, and kept that way while it's open (a week
    /// arriving meanwhile fills the compact cards), so the panel never jumps.
    private func weekRow(load: DayLoadBar?) -> some View {
        let cards: [DayWeekCard] = weekCards(load: load)
        let full: Bool = today.weekRowFull(start: DayWeek.start(of: shownNoon, calendar: weekCalendar),
                                           readNow: DayWeek.fullRow(cards))
        return DayWeekRow(cards: cards, full: full, pick: showDay)
    }

    /// What the week read is for: the shown week, zone and plan revision; empty
    /// (no read) before the plan is on screen and while Calendar isn't read.
    private var weekReadKey: String {
        guard let plan, plan.calendarState.isRead else { return "" }
        return DayWeek.start(of: shownNoon, calendar: weekCalendar) + "|" + today.timezone.identifier + "|\(plan.revision)"
    }

    /// One read of the week route, after the plan is on screen; the `.task` it
    /// runs in ends with the Day's view, which cancels a read in flight.
    private func readWeek() async {
        guard let plan, plan.calendarState.isRead else { return }
        let calendar: Calendar = weekCalendar
        let days: [String] = DayWeek.keys(DayWeek.dates(containing: shownNoon, calendar: calendar), calendar: calendar)
        await today.refreshWeek(start: DayWeek.start(of: shownNoon, calendar: calendar), days: days, revision: plan.revision)
    }

    /// The full week row is taller: one-column Day shows one row fewer before Show more.
    private var weekRowIsFull: Bool {
        today.weekRowLatched(start: DayWeek.start(of: shownNoon, calendar: weekCalendar)) ?? false
    }

    /// A week card's tap: the existing day selection (the planner's), then a read.
    private func showDay(_ card: DayWeekCard) {
        guard !card.isShown, !today.busy else { return }
        if card.isToday { today.returnToToday() } else { today.selectDate(card.date) }
        Task { await today.refresh(); model.onResize() }
        model.onResize()
    }

    @ViewBuilder private func dayLead(_ plan: TodayPlan) -> some View {
        let timeline = timeline(plan)
        let unread = unreadAccess(plan)
        if let warning = warningLine(plan, unread: unread) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(warning).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                if plan.displayWarnings.contains(where: { $0.hasPrefix("All-day calendar events may reserve this day") }) {
                    // The way out: plan this day anyway (a per-day preference).
                    NotchChrome.small("Plan my day anyway") {
                        Task { await today.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute,
                                                  planAllDay: true) }
                    }
                    .disabled(today.busy)
                    .help("Place your tasks around today's timed events anyway")
                }
            }
        }
        if let hours = afterHours(plan) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(hours).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                NotchChrome.small("Plan tonight") {
                    Task { await today.replan(start: plan.preferences.start_minute, end: 24 * 60) }
                }
                .disabled(today.busy)
                .help("Stretch today's working hours to midnight and place your tasks")
            }
        }
        if today.isShowingCurrentDay, let offer = hoursOffer.offer {
            HoursOfferRow(offer: offer, timezone: today.timezone, clock: today.clock,
                          isToday: { today.dayInterval.contains(Date(timeIntervalSince1970: $0)) },
                          onMove: { request in
                              model.askAbout(request)
                              Task { await hoursOffer.taken(); model.onResize() }
                          },
                          onKeep: { Task { await hoursOffer.keep(); model.onResize() } })
                .transaction { $0.animation = nil }
        }
        if let movingEvent {
            // The check-in card's Move sub-panel, for a block picked on the timeline.
            CheckInMovePanel(model: checkIn, event: movingEvent, day: today.day, timezone: today.timezone,
                             clock: today.clock, backHelp: "Back to the timeline (Esc)",
                             onAsk: { model.askAbout($0) }, onClose: closeMove)
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
                .onExitCommand { closeMove() }
                .transaction { $0.animation = nil }
        }
        // On today, the next thing on the clock leads as its own card; it
        // replaces its row below. In two halves the rest is Later today, one
        // short row each; in one column (and on another day) the agenda as before.
        // Without a read calendar there is no Next card: "next" would claim
        // nothing on the calendar comes first. The line says why, and every
        // row Wisp did read stays in the list.
        let calendar: DayCalendarState = plan.calendarState
        let next: DayItem? = calendar.isRead ? nextItem(timeline) : nil
        if today.isShowingCurrentDay { nextSection(next, timeline: timeline, calendar: calendar) }
        if halves && today.isShowingCurrentDay {
            laterSection(timeline, next: next)
        } else {
            agenda(timeline, skipping: next, calendarRead: calendar.isRead)
        }
        if today.isShowingCurrentDay && !staleReminders.asks.isEmpty {
            // A question that arrives after the first frame never animates the panel's height.
            staleSection.transaction { $0.animation = nil }
        }
    }

    /// What follows Old reminders in today's order.
    @ViewBuilder private func dayRest(_ plan: TodayPlan) -> some View {
        let fit = stillToFit(plan)
        if !fit.isEmpty { fitSection(fit) }
        let school = schoolThisWeek(plan)
        if !school.isEmpty { schoolSection(school) }
    }

    // MARK: Building the timeline

    private func timeline(_ plan: TodayPlan) -> DayTimeline {
        let bounds = today.dayInterval
        let dayStart = bounds.start.timeIntervalSince1970
        let blocks = plan.displayBlocks.map {
            DayBlockInput(id: $0.id, title: $0.title, start: $0.start, end: $0.end, kind: $0.kind,
                          taskID: $0.task_id, patternID: $0.pattern_id, evidenceID: $0.evidence_id,
                          members: ($0.members ?? []).map { DayMember(title: $0.title, evidenceID: $0.evidence_id) })
        }
        let caught = attention.items.filter { !ignoredCaught.contains($0.source_id) }.map {
            DayCaughtInput(id: $0.source_id, title: $0.title ?? "", when: $0.event_ts ?? $0.due_ts, state: $0.state)
        }
        return DayTimeline.build(
            blocks: blocks, allDay: plan.all_day.map(\.title), needs: plan.dayNeeds + plan.studyNeeds, caught: caught,
            now: today.now.timeIntervalSince1970, dayStart: dayStart, dayEnd: bounds.end.timeIntervalSince1970,
            workStart: dayStart + Double(plan.preferences.start_minute * 60),
            workEnd: dayStart + Double(plan.preferences.end_minute * 60),
            isToday: today.isShowingCurrentDay, clock: today.clock,
            dayClock: { NotchChrome.dayClock($0, zone: today.timezone, locale: today.clockLocale) },
            todos: dayTodos(plan))
    }

    /// The proactive engine's to-dos, with this Mac's confirmed check-offs. They
    /// are about today, so another day shows none.
    private func dayTodos(_ plan: TodayPlan) -> [DayTodoInput] {
        guard today.isShowingCurrentDay else { return [] }
        return (plan.workspace?.proactive?.todos ?? []).map { todo in
            var input = todo.dayInput
            input.done = proactive.isDone(todo)
            return input
        }
    }

    private func stillToFit(_ plan: TodayPlan) -> [DayFitRow] {
        let bounds = today.dayInterval
        let blocks = plan.displayBlocks.map { DayBlockInput(id: $0.id, title: $0.title, start: $0.start, end: $0.end, kind: $0.kind,
                                                            evidenceID: $0.evidence_id) }
        return DayTimeline.stillToFit(
            todos: dayTodos(plan),
            unscheduled: plan.displayUnscheduled.map { (id: $0.task_id, title: $0.title, reason: $0.reason) },
            blocks: blocks, needs: plan.dayNeeds, now: today.now.timeIntervalSince1970,
            dayStart: bounds.start.timeIntervalSince1970,
            dayEnd: bounds.end.timeIntervalSince1970, isToday: today.isShowingCurrentDay,
            dayClock: { NotchChrome.dayClock($0, zone: today.timezone, locale: today.clockLocale) })
    }

    private func proactiveTodo(_ id: String?) -> ProactiveTodo? {
        id.flatMap { id in plan?.workspace?.proactive?.todos.first { $0.id == id } }
    }

    /// "Still to fit": what the planner couldn't place and to-dos with no time
    /// today, each with where it came from.
    private func fitSection(_ rows: [DayFitRow]) -> some View {
        let shown = Array(rows.prefix(5))
        return VStack(alignment: .leading, spacing: 5) {
            Text("STILL TO FIT").font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            ForEach(shown) { row in fitRow(row) }
            if rows.count > shown.count {
                Text("+\(rows.count - shown.count) more in the Brief").font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
                    .padding(.leading, 24)
            }
        }
        .padding(.top, 2)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Still to fit")
    }

    /// Today only: school work due later this week, for reference. What is due
    /// today is already in the agenda as its deadlines, so it isn't repeated.
    private func schoolThisWeek(_ plan: TodayPlan) -> StudyUpcomingRows {
        guard today.isShowingCurrentDay, let study = plan.study else { return StudyUpcomingRows() }
        let bounds = today.dayInterval
        return study.dayUpcoming(dayStart: bounds.start.timeIntervalSince1970, dayEnd: bounds.end.timeIntervalSince1970,
                                 shownIDs: StudyDaySummary.deadlineIDs(plan.studyNeeds.map(\.id)))
    }

    /// "SCHOOL THIS WEEK": one line an assignment, then "+N more in Study ›".
    /// All of it opens Study.
    private func schoolSection(_ rows: StudyUpcomingRows) -> some View {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        return Button { model.tab = .study } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("SCHOOL THIS WEEK").font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    Spacer(minLength: 8)
                    HStack(spacing: 2) {
                        Text(rows.moreLine ?? "Study")
                        Image(systemName: "chevron.right").font(.wisp(size: 8, weight: .bold))
                    }
                    .font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary).fixedSize()
                }
                ForEach(Array(rows.items.enumerated()), id: \.offset) { _, item in
                    schoolRow(item, calendar: calendar)
                }
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.top, 2)
        .help("Your school work (⌘5)")
        .accessibilityLabel("School this week: " + StudyText.upcomingSpoken(rows, now: today.now, calendar: calendar))
        .accessibilityHint("Opens Study")
    }

    private func schoolRow(_ item: StudyDaySummary.Due, calendar: Calendar) -> some View {
        let assignment = item.assignment
        return HStack(alignment: .firstTextBaseline, spacing: 9) {
            Image(systemName: assignment.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
            Text(assignment.title).font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1).layoutPriority(1)
            // In a narrow half a squeezed course left a sliver of one glyph: it
            // shows from a few characters' room, else not at all.
            ViewThatFits(in: .horizontal) {
                Text(item.course).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
                    .frame(minWidth: 36, idealWidth: 36, alignment: .leading)
                Color.clear.frame(width: 0, height: 0)
            }
            Spacer(minLength: 8)
            Text(StudyText.dueShort(assignment, now: today.now, calendar: calendar))
                .font(.wisp(size: 11.5)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                .lineLimit(1).fixedSize()
        }
    }

    private func fitRow(_ row: DayFitRow) -> some View {
        let todo = proactiveTodo(row.todoID)
        let busy = row.todoID.map { proactive.todoBusy.contains($0) } ?? false
        return HStack(alignment: .center, spacing: 9) {
            Image(systemName: row.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted).frame(width: 15)
            fitText(row)
            Spacer(minLength: 6)
            if let todo {
                if busy {
                    ProgressView().controlSize(.small)
                } else {
                    if todo.canAddToDay {
                        if proactive.addedToDay.contains(todo.id) {
                            Text("ADDED ✓").font(.wisp(size: 9.5, design: .monospaced)).foregroundStyle(Theme.textMuted)
                        } else {
                            NotchChrome.small("Add to Day") { Task { await proactive.addToDay(todo, today: today) } }
                                .disabled(today.busy)
                                .help("Fit it into today's plan")
                        }
                    }
                    NotchChrome.small("Done") { Task { await proactive.toggleDone(todo, today: today) } }
                        .help("Check it off")
                        .accessibilityLabel("Check off \(todo.title)")
                }
            }
        }
        .accessibilityElement(children: .contain)
    }

    /// A Still to fit row's title and why it is there: side by side when both
    /// fit whole, else the reason under the title, wrapping between words. The
    /// reason was one line beside the title and was cut mid-number.
    @ViewBuilder private func fitText(_ row: DayFitRow) -> some View {
        if row.detail.isEmpty {
            fitTitle(row, lines: 2)
        } else {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .firstTextBaseline, spacing: 9) {
                    fitTitle(row, lines: 1).fixedSize()
                    fitDetail(row).fixedSize()
                }
                VStack(alignment: .leading, spacing: 1) {
                    fitTitle(row, lines: 2)
                    fitDetail(row).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func fitTitle(_ row: DayFitRow, lines: Int) -> some View {
        Text(row.title).font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
            .lineLimit(lines).fixedSize(horizontal: false, vertical: true)
            .help(row.title)
    }

    private func fitDetail(_ row: DayFitRow) -> some View {
        Text(row.detail).font(.wisp(size: 11)).foregroundStyle(Theme.textMuted)
    }

    /// The day's first planner warning, in plain words. A source Wisp can't
    /// read already has its own row; its "not current" warning isn't repeated.
    /// Placement warnings only matter while there are tasks to place.
    private func warningLine(_ plan: TodayPlan, unread: [SourceAccess] = []) -> String? {
        let hidden = unread.compactMap { ["calendar": "Calendar is not current", "reminders": "Reminders are not current"][$0.source] }
        let placing = !plan.displayUnscheduled.isEmpty
        return plan.displayWarnings.lazy
            .filter { w in !hidden.contains { w.hasPrefix($0) } }
            .filter { w in placing || !(w.hasPrefix("All-day calendar events") || w.hasPrefix("Calendar is not current")) }
            .compactMap(DayTimeline.plainWarning).first
    }

    /// "It's after your working hours (9 AM–6 PM)." when the planner held tasks
    /// back because the working day is over.
    private func afterHours(_ plan: TodayPlan) -> String? {
        guard today.isShowingCurrentDay, plan.displayUnscheduled.contains(where: { $0.reason.hasPrefix("It's after your working hours") })
        else { return nil }
        return DayTimeline.afterHoursLine(start: plan.preferences.start_minute, end: plan.preferences.end_minute,
                                          locale: today.clockLocale)
    }

    // MARK: Pieces of the day

    private var otherDayRow: some View {
        HStack(spacing: 8) {
            Text(today.selectedDate, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                .font(.wisp(size: 13, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            NotchChrome.chip("Back to today", icon: "calendar", help: "Show today instead of the day picked in the planner") {
                today.returnToToday()
                Task { await today.refresh() }
            }
            .disabled(today.busy)
        }
    }

    /// Timed Wisp blocks keyed by a loose title, so an all-day assignment that
    /// already has time held for it ("Week 4 Reading: Functions [CMP-140-04]" and
    /// the block "Week 4 Reading: Functions for CMP 140") reads as booked.
    private func bookedTimes(_ plan: TodayPlan) -> [String: String] {
        var booked: [String: String] = [:]
        for block in plan.displayBlocks where block.task_id != nil {
            let key = Self.looseTitle(block.title)
            if !key.isEmpty, booked[key] == nil {
                booked[key] = NotchChrome.dayClock(block.start, zone: today.timezone, locale: today.clockLocale)
            }
        }
        return booked
    }

    static func looseTitle(_ title: String) -> String {
        var t = title.lowercased()
        if let bracket = t.firstIndex(of: "[") { t = String(t[..<bracket]) }
        if let range = t.range(of: " for ", options: .backwards) { t = String(t[..<range.lowerBound]) }
        return t.trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
    }

    // MARK: Next up

    /// The next thing on the clock on today, or nil.
    private func nextItem(_ timeline: DayTimeline) -> DayItem? {
        guard today.isShowingCurrentDay else { return nil }
        return DayTimeline.nextUp(timeline.items, now: today.now.timeIntervalSince1970)
    }

    /// The Next card, or one line when nothing is left on the clock today.
    @ViewBuilder private func nextSection(_ next: DayItem?, timeline: DayTimeline, calendar: DayCalendarState) -> some View {
        if let next {
            nextCard(next, timeline: timeline, calendarRead: calendar.isRead)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("NEXT").font(.wisp(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textMuted)
                Text(DayTimeline.nothingNextLine(calendar)).font(.wisp(size: 13)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private func nextCard(_ item: DayItem, timeline: DayTimeline, calendarRead: Bool) -> some View {
        let now: Double = today.now.timeIntervalSince1970
        let label: String = DayTimeline.nextLabel(item, now: now)
        let free: String? = DayTimeline.nowFreeLine(timeline.items, now: now, clock: today.clock, calendarRead: calendarRead)
        let spoken: String = DayTimeline.spokenNextLabel(item, now: now) + ": " + spokenRow(item)
        return VStack(alignment: .leading, spacing: 4) {
            nextHeader(label, free: free)
            nextTitleRow(item)
            nextDetail(item)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.chipStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(spoken)
    }

    /// "NEXT · IN 40 MIN" at the left, "Now 1:20 PM · free until 2:00 PM" at the right.
    private func nextHeader(_ label: String, free: String?) -> some View {
        let lead = Text(label).font(.wisp(size: 10.5, weight: .medium, design: .monospaced)).foregroundStyle(Theme.warn)
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                lead.fixedSize()
                Spacer(minLength: 8)
                if let free { Text(free).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary).fixedSize() }
            }
            VStack(alignment: .leading, spacing: 2) {
                lead.fixedSize()
                if let free {
                    Text(free).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityHidden(true)
    }

    /// The title and the card's actions beside it, or under it when both don't fit.
    private func nextTitleRow(_ item: DayItem) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 10) {
                nextTitle(item, lines: 1).fixedSize()
                Spacer(minLength: 8)
                nextActions(item)
            }
            VStack(alignment: .leading, spacing: 6) {
                nextTitle(item, lines: 2)
                nextActions(item)
            }
        }
    }

    private func nextTitle(_ item: DayItem, lines: Int) -> some View {
        let struck: Bool = cancelStatus(item).map { !$0.failed } ?? false
        return Text(item.title).font(.wisp(size: 18, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            .lineLimit(lines).fixedSize(horizontal: false, vertical: true)
            .strikethrough(struck)
            .help(item.title)
    }

    /// Open in Calendar and Move… for a Calendar event the service described
    /// (Move only where the ⋯ menu offers it), then the row's own actions:
    /// the cancel Undo, Keep, Done or Add to calendar, and ⋯.
    private func nextActions(_ item: DayItem) -> some View {
        let event: CheckInEvent? = calendarEvent(for: item)
        let settled: Bool = event.map { checkIn.cancelled[$0.id] != nil } ?? false
        let offersMove: Bool = event.map { e in
            eventMenuEntries(item, e).contains { if case .move = $0 { return true } else { return false } }
        } ?? false
        let buttons: [DayTimeline.NextButton] = DayTimeline.nextButtons(isDescribedEvent: event != nil, menuOffersMove: offersMove,
                                                                         cancelActive: cancelActive(item) || settled)
        return HStack(spacing: 6) {
            if let event, buttons.contains(.open) {
                NotchChrome.chip("Open in Calendar ↗", icon: nil, help: "Opens this event in Calendar") { openInCalendar(event) }
            }
            if let event, buttons.contains(.move) {
                NotchChrome.chip("Move…", icon: nil, help: "Pick another time; Wisp finds free ones") { openMove(event) }
                    .accessibilityLabel("Move \(item.title)")
            }
            trailing(item)
        }
        .fixedSize()
    }

    /// The time line, then where a cancel or a calendar add stands, or the block's warning.
    @ViewBuilder private func nextDetail(_ item: DayItem) -> some View {
        Text(DayTimeline.nextTimeLine(item, clock: today.clock)).font(.wisp(size: 12.5)).foregroundStyle(Theme.textSecondary)
            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
        if let status = laterStatus(item) {
            let ink: Color = status.failed ? Theme.bad : Theme.textMuted
            Text(status.text).font(.wisp(.footnote)).foregroundStyle(ink)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
        }
    }

    /// What a cancel, a planner warning or a calendar add says about a row,
    /// beyond what it is: nil for a row with nothing to report.
    private func laterStatus(_ item: DayItem) -> (text: String, failed: Bool)? {
        if let status = cancelStatus(item) { return status }
        if let id = item.blockID, let block = plan?.displayBlocks.first(where: { $0.id == id }),
           let warning = block.warnings.lazy.compactMap(DayTimeline.plainWarning).first {
            return (warning, false)
        }
        return calendarLine(item)
    }

    // MARK: Later today

    /// The rest of today in short rows: when, a bar as long as it runs, what it
    /// is, its actions. What is due by a time waits behind one "Show N more"
    /// line that names it, as do rows past the first eight.
    @ViewBuilder private func laterSection(_ timeline: DayTimeline, next: DayItem?) -> some View {
        let now: Double = today.now.timeIntervalSince1970
        let agenda: DayAgenda = timeline.agenda(now: now, isToday: true)
        let over: [DayItem] = agenda.earlier.filter { $0.isOver(cancelActive: cancelActive($0)) }
        let live: [DayItem] = agenda.earlier.filter { !$0.isOver(cancelActive: cancelActive($0)) }
        let later = DayTimeline.laterRows(agenda, excluding: next)
        let timed: [DayItem] = live + later.timed
        // The Next card stands in for the first row, so it counts toward the
        // eight; one fewer while the left half runs far below the right.
        let limit: Int = DayTimeline.rowsBeforeMore(Self.agendaRows, hasNext: next != nil, oneFewer: laterTrimmed)
        let hidden: [DayItem] = Array(timed.dropFirst(limit)) + later.due
        let rows: [DayItem] = showWholeAgenda ? timed + later.due : Array(timed.prefix(limit))
        if !timed.isEmpty || !later.due.isEmpty || !over.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                laterHeader(over)
                if showEarlier { ForEach(over) { laterRow($0).transition(.opacity) } }
                ForEach(rows) { laterRow($0) }
                    .transition(.opacity)
                if !hidden.isEmpty { laterMore(hidden) }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Later today")
        }
    }

    /// "LATER TODAY", and at its right what ended in the last two hours, which opens.
    private func laterHeader(_ over: [DayItem]) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("LATER TODAY").font(.wisp(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 8)
            if !over.isEmpty {
                Button {
                    showEarlier.toggle()
                    model.onResize()
                } label: {
                    Text(showEarlier ? "Hide earlier" : "Earlier: " + DayTimeline.earlierLine(over))
                        .font(.wisp(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
                        .contentShape(Rectangle())
                }
                .buttonStyle(StudyRowButtonStyle())
                .help(showEarlier ? "Hide what's over" : "Show what's over")
            }
        }
        .padding(.bottom, 2)
    }

    /// "Show 2 more · due 11:59 PM: Lab 5 and Problem set 6", or Show fewer.
    private func laterMore(_ hidden: [DayItem]) -> some View {
        let label: String = showWholeAgenda ? "Show fewer" : DayTimeline.moreLine(hidden, clock: today.clock)
        return Button {
            showWholeAgenda.toggle()
            model.onResize()
        } label: {
            HStack(spacing: 4) {
                Text(label).lineLimit(1).truncationMode(.tail)
                Image(systemName: showWholeAgenda ? "chevron.up" : "chevron.down").font(.wisp(size: 8, weight: .semibold))
            }
            .font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .padding(.leading, timeColumn + 10 + Self.laterBarWidth + 10).padding(.top, 3)
        .help(label)
    }

    /// The Later row's duration bar: 30 pt for two hours or more.
    private static var laterBarWidth: CGFloat { 30 * Theme.textScale }

    private func laterRow(_ item: DayItem) -> some View {
        let over: Bool = item.isOver(cancelActive: cancelActive(item))
        // Time, bar and title share the title's first line.
        return HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(DayTimeline.laterTime(item, clock: today.clock))
                .font(.wisp(.callout, weight: .semibold)).monospacedDigit()
                .foregroundStyle(item.style == .overdue ? DayInk.now : Theme.textPrimary)
                .lineLimit(1)
                .frame(width: timeColumn, alignment: .leading)
            laterBar(item)
            VStack(alignment: .leading, spacing: 1) {
                laterLine(item, actions: !over)
                laterStatusLine(item)
            }
            .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: calendarShow(item))
            .frame(maxWidth: .infinity, alignment: .leading)
            .help(item.title)
        }
        .padding(.vertical, 4)
        .opacity(over ? 0.45 : 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(spokenRow(item))
    }

    /// How long it runs, against two hours, in its kind's ink; nothing for
    /// what is due by a time and holds no stretch.
    private func laterBar(_ item: DayItem) -> some View {
        let width: CGFloat = Self.laterBarWidth
        let fraction: Double? = DayTimeline.durationFraction(item)
        let ink: Color = DayInk.fill(item.style)
        return ZStack(alignment: .leading) {
            // What holds no stretch of time (due by a time, overdue) has no bar;
            // the column stays so the titles line up.
            if let fraction {
                Capsule().fill(Theme.chipFill).frame(width: width, height: 4)
                Capsule().fill(ink).frame(width: max(4, width * CGFloat(fraction)), height: 4)
            } else {
                Color.clear.frame(width: width, height: 4)
            }
        }
        .frame(width: width)
        .alignmentGuide(.firstTextBaseline) { d in d.height / 2 + 5 * Theme.textScale }
        .accessibilityHidden(true)
    }

    /// The title, its muted detail and the row's actions: all on one line when
    /// they fit whole; else the detail under the title; else (larger text) the
    /// actions under both, so a title is never cut to make room for a button.
    private func laterLine(_ item: DayItem, actions: Bool) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 8) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    laterTitle(item).lineLimit(1).fixedSize()
                    laterDetail(item).lineLimit(1).fixedSize()
                }
                Spacer(minLength: 8)
                if actions { trailing(item) }
            }
            HStack(alignment: .center, spacing: 8) {
                // The detail takes the room the title and actions leave (no
                // ideal width of its own, so only the title decides the fit).
                VStack(alignment: .leading, spacing: 1) {
                    laterTitle(item).lineLimit(1).fixedSize()
                    laterDetail(item).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                        .frame(minWidth: 0, idealWidth: 0, maxWidth: .infinity, alignment: .leading)
                }
                if actions { trailing(item) }
            }
            VStack(alignment: .leading, spacing: 4) {
                laterTitle(item).lineLimit(3).fixedSize(horizontal: false, vertical: true)
                laterDetail(item).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                if actions { trailing(item) }
            }
        }
    }

    private func laterTitle(_ item: DayItem) -> Text {
        let struck: Bool = cancelStatus(item).map { !$0.failed } ?? false
        return Text(item.title).font(.wisp(.callout, weight: .semibold))
            .foregroundColor(item.style == .usual ? Theme.textSecondary : Theme.textPrimary)
            .strikethrough(struck)
    }

    private func laterDetail(_ item: DayItem) -> Text {
        Text(DayTimeline.laterDetail(item, now: today.now.timeIntervalSince1970, isToday: true, clock: today.clock))
            .font(.wisp(.footnote)).foregroundColor(DayInk.detail(item.style))
    }

    /// Where a cancel or a calendar add stands, or the block's warning: one line.
    @ViewBuilder private func laterStatusLine(_ item: DayItem) -> some View {
        if let status = laterStatus(item) {
            let ink: Color = status.failed ? Theme.bad : Theme.textMuted
            Text(status.text).font(.wisp(.footnote)).foregroundStyle(ink)
                .lineLimit(1).truncationMode(.tail)
                .help(status.text)
                .transition(.opacity)
        }
    }

    // MARK: The agenda

    /// The day's calendar as a list: one dim line for what just ended, the now
    /// line, then each thing on its own full-width row in time order, with the
    /// free stretches between. A long day shows its first rows and Show more.
    @ViewBuilder private func agenda(_ timeline: DayTimeline, skipping next: DayItem? = nil,
                                     calendarRead: Bool = true) -> some View {
        let now = today.now.timeIntervalSince1970
        let isToday = today.isShowingCurrentDay
        let agenda = DayTimeline.agenda(timeline.agenda(now: now, isToday: isToday), without: next, calendarRead: calendarRead)
        // A cancel still in its Undo window keeps an ended event live, on its own row.
        let over = agenda.earlier.filter { $0.isOver(cancelActive: cancelActive($0)) }
        let live = agenda.earlier.filter { !$0.isOver(cancelActive: cancelActive($0)) }
        // The Next card stands in for the first row, and one more row waits
        // behind Show more, so the one-column Day is no taller with the card
        // and the week row than it was without them.
        let limit: Int = DayTimeline.rowsBeforeMore(Self.agendaRows - (weekRowIsFull ? 1 : 0), hasNext: next != nil,
                                                    oneFewer: next != nil)
        let ahead = showWholeAgenda ? agenda.ahead : agenda.firstRows(holding: limit)
        // An empty day has nothing to list; the blurb above already says so.
        if !agenda.earlier.isEmpty || !agenda.ahead.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                if !over.isEmpty {
                    earlierRow(over)
                    if showEarlier { ForEach(over) { agendaRow($0).transition(.opacity) } }
                }
                ForEach(live) { agendaRow($0) }
                // The Next card already says where now is; the line is for a day without one.
                if isToday && next == nil { nowRow(now) }
                ForEach(ahead) { row in
                    switch row {
                    case .item(let item): agendaRow(item)
                    case .free(let start, let end): freeRow(start, end)
                    }
                }
                .transition(.opacity)
                if agenda.thingsAhead > limit {
                    Button(showWholeAgenda ? "Show fewer" : "Show \(agenda.thingsAhead - limit) more") {
                        showWholeAgenda.toggle()
                        model.onResize()
                    }
                    .buttonStyle(.plain).font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.leading, timeColumn + 10 + Self.barInset).padding(.top, 2)
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Agenda")
        }
    }

    /// What ended in the last two hours, as one dim line that opens into rows.
    private func earlierRow(_ items: [DayItem]) -> some View {
        Button {
            showEarlier.toggle()
            model.onResize()
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                HStack(spacing: 4) {
                    Text("Earlier")
                    Image(systemName: showEarlier ? "chevron.down" : "chevron.right").font(.wisp(size: 8, weight: .semibold))
                }
                .frame(width: timeColumn, alignment: .leading)
                if !showEarlier {
                    Text(DayTimeline.earlierLine(items)).lineLimit(1).padding(.leading, Self.barInset)
                }
                Spacer(minLength: 0)
            }
            .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.bottom, 2)
        .help(showEarlier ? "Hide what's over" : "Show what's over")
        .accessibilityLabel(showEarlier ? "Hide what's over" : "Earlier: " + DayTimeline.earlierLine(items))
    }

    /// Now, as a line across the agenda between what's over and what's next.
    private func nowRow(_ now: Double) -> some View {
        HStack(spacing: 10) {
            Text(today.clock(now)).font(.wisp(.footnote, weight: .semibold)).monospacedDigit()
                .foregroundStyle(DayInk.now)
                .frame(width: timeColumn, alignment: .leading)
            HStack(spacing: 0) {
                Circle().fill(DayInk.now).frame(width: 7, height: 7)
                Rectangle().fill(DayInk.now).frame(height: 1.5)
            }
            .padding(.leading, -2)
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Now, \(today.clock(now))")
    }

    /// Time nothing holds, before the next thing that does.
    private func freeRow(_ start: Double, _ end: Double) -> some View {
        HStack(spacing: 10) {
            Color.clear.frame(width: timeColumn, height: 1)
            Text(DayTimeline.freeLine(start: start, end: end,
                                      startsNow: today.isShowingCurrentDay && start <= today.now.timeIntervalSince1970,
                                      clock: today.clock))
                .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .padding(.leading, Self.barInset)
                .overlay(alignment: .leading) { DayInk.freeBar.frame(width: 3) }
        }
        .padding(.vertical, 3)
    }

    /// One thing on the agenda: when, a bar in its style's ink, the title on up
    /// to two lines, what it is, and its actions. Nothing shares its width, so
    /// the title always shows.
    private func agendaRow(_ item: DayItem) -> some View {
        let over: Bool = item.isOver(cancelActive: cancelActive(item))
        return HStack(alignment: .top, spacing: 10) {
            agendaGutter(item)
            HStack(alignment: .top, spacing: 8) {
                agendaText(item)
                // What's over has nothing left to act on; a cancel still in its Undo
                // window (or in flight) keeps the row live until it settles.
                if !over { trailing(item) }
            }
            .padding(.leading, Self.barInset)
            .overlay(alignment: .leading) { DayInk.bar(item.style).frame(width: 3) }
        }
        .padding(.vertical, 5)
        .opacity(over ? 0.45 : 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(spokenRow(item))
    }

    /// The agenda row's time column: when, and under it the end or the day.
    @ViewBuilder private func agendaGutter(_ item: DayItem) -> some View {
        let time: (top: String, bottom: String) = DayTimeline.gutter(item, clock: today.clock)
        let ink: Color = item.style == .overdue ? DayInk.now : Theme.textPrimary
        VStack(alignment: .leading, spacing: 1) {
            Text(time.top).font(.wisp(.body, weight: .semibold)).foregroundStyle(ink)
            if !time.bottom.isEmpty {
                Text(time.bottom).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            }
        }
        .monospacedDigit()
        .padding(.top, 1)
        .frame(width: timeColumn, alignment: .leading)
    }

    /// The agenda row's words: its title, what it is, what covers it and where
    /// its calendar add stands.
    @ViewBuilder private func agendaText(_ item: DayItem) -> some View {
        let status: (text: String, failed: Bool)? = cancelStatus(item)
        let struck: Bool = status != nil && status?.failed != true
        let titleInk: Color = item.style == .usual ? Theme.textSecondary : Theme.textPrimary
        let detailInk: Color = status?.failed == true ? Theme.bad : DayInk.detail(item.style)
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                if !item.source.isEmpty {
                    Image(systemName: DayTimeline.sourceSymbol(item.source)).font(.wisp(size: 10))
                        .foregroundStyle(Theme.textSecondary)
                }
                Text(item.title).font(.wisp(.callout, weight: .semibold))
                    .foregroundStyle(titleInk)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    .strikethrough(struck)
            }
            Text(detail(item)).font(.wisp(.footnote))
                .foregroundStyle(detailInk)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            // A school deadline's or study block's "Lectures 7 and 8 cover it.": one line.
            if let covers = schoolCovers(item) {
                Text(covers).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .lineLimit(1).truncationMode(.tail)
            }
            // "Added to calendar", or why an add or undo didn't go: one line.
            if let line = calendarLine(item) {
                let lineInk: Color = line.failed ? Theme.bad : Theme.textMuted
                Text(line.text).font(.wisp(.footnote))
                    .foregroundStyle(lineInk)
                    .lineLimit(1).truncationMode(.tail)
                    .transition(.opacity)
            }
        }
        .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: calendarShow(item))
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// The agenda row in words, for VoiceOver: its title, time, detail, what
    /// covers it and its calendar line, then what it covers and whether it's over.
    private func spokenRow(_ item: DayItem) -> String {
        var parts: [String] = []
        if !item.title.isEmpty { parts.append(item.title) }
        let time: String = spokenTime(item)
        if !time.isEmpty { parts.append(time) }
        let what: String = detail(item)
        if !what.isEmpty { parts.append(what) }
        if let covers = schoolCovers(item), !covers.isEmpty { parts.append(covers) }
        if let line = calendarLine(item), !line.text.isEmpty { parts.append(line.text) }
        var spoken: String = parts.joined(separator: ", ")
        if item.members.count > 1 {
            let titles: [String] = item.members.map(\.title)
            spoken += ", covers " + titles.joined(separator: "; ")
        }
        if item.past { spoken += ", already over" }
        return spoken
    }

    /// The time column in words, for VoiceOver.
    private func spokenTime(_ item: DayItem) -> String {
        if item.style == .overdue { return "overdue" }
        if let due = item.due {
            // A date-only reminder has no time to read out.
            return item.allDay ? (today.isShowingCurrentDay ? "due today" : "due that day") : "due " + today.clock(due)
        }
        let begins = today.clock(item.begins ?? item.start)
        return item.ends.map { "\(begins) to \(today.clock($0))" } ?? "at " + begins
    }

    /// A cancel of this block that is still in its Undo window or being sent.
    private func cancelActive(_ item: DayItem) -> Bool {
        guard let event = calendarEvent(for: item) else { return false }
        return checkIn.pendingCancels[event.id] != nil || checkIn.cancelling.contains(event.id)
    }

    /// Where a timeline cancel of this block stands, for the detail line: the
    /// Undo bar's words while it can be undone, then what the service said.
    private func cancelStatus(_ item: DayItem) -> (text: String, failed: Bool)? {
        guard let event = calendarEvent(for: item) else { return nil }
        if let pending = checkIn.pendingCancels[event.id] {
            return (CheckInCancel.undoLabel(inviteeCount: pending.event.inviteeCount ?? 0), false)
        }
        if checkIn.cancelling.contains(event.id) { return ("Cancelling…", false) }
        if let done = checkIn.cancelled[event.id] { return (done, false) }
        if let failed = checkIn.cancelFailures[event.id] {
            return (failed.failure.isUncertain ? "Check Calendar: " + failed.failure.message : failed.failure.message, true)
        }
        return nil
    }

    /// A row's detail: where a cancel stands, its planner warning in plain
    /// words, or what it is.
    private func detail(_ item: DayItem) -> String {
        if let status = cancelStatus(item) { return status.text }
        guard let id = item.blockID,
              let block = plan?.displayBlocks.first(where: { $0.id == id }),
              let warning = block.warnings.lazy.compactMap(DayTimeline.plainWarning).first else {
            return DayTimeline.agendaDetail(item, now: today.now.timeIntervalSince1970,
                                            isToday: today.isShowingCurrentDay, clock: today.clock)
        }
        return warning
    }

    // MARK: Actions

    /// The block's actions, or where its timeline cancel stands: Undo while it
    /// can still be undone, a spinner while the service works, nothing once it
    /// is cancelled (the block leaves with the next plan).
    @ViewBuilder private func trailing(_ item: DayItem) -> some View {
        if let event = calendarEvent(for: item), let pending = checkIn.pendingCancels[event.id] {
            NotchChrome.small("Undo") { checkIn.undoCancel(pending.event) }
                .help("Keeps the event; nothing has been sent yet")
                .accessibilityLabel("Undo cancelling “\(CheckInSection.plainTitle(event.title))”; 5 seconds")
        } else if let event = calendarEvent(for: item), checkIn.cancelling.contains(event.id) {
            ProgressView().controlSize(.small)
        } else if let event = calendarEvent(for: item), checkIn.cancelled[event.id] != nil {
            EmptyView()
        } else {
            actions(item)
        }
    }

    /// The school deadline (service/study/day_hook.py) a row draws, if it is one.
    private func schoolDeadline(_ item: DayItem) -> TodayDeadline? {
        guard let id = item.needID else { return nil }
        return plan?.deadlines.first { $0.id == id && $0.source == "study" }
    }

    /// The covers line under a school deadline or a study block, or nil.
    private func schoolCovers(_ item: DayItem) -> String? {
        if let deadline = schoolDeadline(item) { return SchoolSlidesText.covers(deadline.covers_text) }
        guard item.kind == "study", let id = item.blockID,
              let block = plan?.displayBlocks.first(where: { $0.id == id }) else { return nil }
        return SchoolSlidesText.covers(block.covers_text)
    }

    /// A study block's "Add to calendar" state, or `.none` for every other row.
    private func calendarShow(_ item: DayItem) -> StudyBlockCalendar.Show {
        guard item.kind == "study", let id = item.blockID,
              let block = plan?.displayBlocks.first(where: { $0.id == id }) else { return .none }
        return studyAdds.ledger.show(block.calendarAddFacts, now: today.now.timeIntervalSince1970)
    }

    /// The one extra line under a study block: what went wrong, or that it's added.
    private func calendarLine(_ item: DayItem) -> (text: String, failed: Bool)? {
        switch calendarShow(item) {
        case .none: return nil
        case .add(_, let error): return error.map { ($0, true) }
        case .added(_, _, let error): return error.map { ($0, true) } ?? (StudyBlockCalendar.addedText, false)
        }
    }

    @ViewBuilder private func calendarAction(_ item: DayItem) -> some View {
        switch calendarShow(item) {
        case .none:
            EmptyView()
        case .add(let busy, _):
            if busy {
                ProgressView().controlSize(.small).accessibilityLabel("Adding \(item.title) to your calendar")
            } else {
                NotchChrome.action("Add to calendar", help: "Put this study block on your calendar",
                                   spoken: "Add \(item.title) to your calendar") { studyCalendar(.add, item) }
            }
        case .added(let undo, let busy, _):
            if busy {
                ProgressView().controlSize(.small).accessibilityLabel("Removing \(item.title) from your calendar")
            } else if undo {
                NotchChrome.action("Undo", help: "Take this study block off your calendar",
                                   spoken: "Undo adding \(item.title) to your calendar") { studyCalendar(.undo, item) }
            }
        }
    }

    /// Sends the add or undo; the service books it through the calendar bridge.
    /// Success reads the day again, so `added` comes from the service.
    private func studyCalendar(_ operation: StudyBlockCalendar.Operation, _ item: DayItem) {
        guard let id = item.blockID, studyAdds.ledger.begin(operation, id) else { return }
        model.onResize()
        Task {
            let outcome = await today.studyBlockCalendar(operation, blockID: id)
            studyAdds.ledger.finish(operation, id, outcome)
            model.onResize()
            guard case .succeeded = outcome else { return }
            await today.refresh(afterMutation: true)
            studyAdds.ledger.settle(id, snapshotAdded: today.currentPlan?.displayBlocks.first { $0.id == id }?.added,
                                    generatedAt: today.currentPlan?.generated_at)
            model.onResize()
        }
    }

    /// The block's main action as a button, and everything else in one ⋯ menu.
    /// A reminder also shows Cancel in the open, not inside the menu.
    @ViewBuilder private func actions(_ item: DayItem) -> some View {
        if let reminder = cancellableReminder(item), cancelAsking == reminder.id || cancelBusy == reminder.id {
            reminderCancelConfirm(reminder)
        } else {
            rowActions(item)
        }
    }

    @ViewBuilder private func rowActions(_ item: DayItem) -> some View {
        HStack(spacing: 4) {
            calendarAction(item)
            if let deadline = schoolDeadline(item), let review = deadline.review, review.offered {
                NotchChrome.action(review.label, help: "Make flash cards from the slides that cover this, in Study",
                                   spoken: "\(review.label) for \(deadline.title)") {
                    SchoolActions.review(review.pages, title: deadline.title, model: model)
                }
            }
            switch item.style {
            case .suggested:
                NotchChrome.small("Keep") { keep(item, laterBy: 0) }.disabled(today.busy)
            case .usual:
                NotchChrome.small("Not today") { notToday(item) }.disabled(today.busy || item.patternID == nil)
                    .help(item.kind == "behavior_away" ? "Ignore this possible time away for today"
                          : "Ignore this usually-busy time for today")
            case .caught:
                if attention.items.first(where: { $0.source_id == item.caughtID })?.state == "shadow" {
                    NotchChrome.small("Add") { addCaught(item) }.disabled(today.busy)
                }
            case .needsYou, .overdue, .todo:
                if let todo = proactiveTodo(item.todoID) {
                    if proactive.todoBusy.contains(todo.id) {
                        ProgressView().controlSize(.small)
                    } else {
                        NotchChrome.small("Done") { Task { await proactive.toggleDone(todo, today: today) } }
                            .help("Check it off")
                            .accessibilityLabel("Check off \(todo.title)")
                    }
                } else if let need = need(for: item), !planned(need) {
                    NotchChrome.small("Plan time") { planTime(item) }.disabled(today.busy)
                }
                if let reminder = cancellableReminder(item) {
                    NotchChrome.small("Cancel") {
                        cancelFailed[reminder.id] = nil
                        cancelAsking = reminder.id
                        model.onResize()
                    }
                    .disabled(today.busy || cancelBusy != nil)
                    .help(cancelFailed[reminder.id] ?? "Delete this reminder from Reminders")
                    .accessibilityLabel("Cancel \(reminder.title)")
                }
            case .calendar, .task:
                EmptyView()
            }
            moreMenu(item)
        }
    }

    private func moreMenu(_ item: DayItem) -> some View {
        Menu {
            switch item.style {
            case .suggested:
                // Only later times that still end today.
                ForEach([(30, "Keep 30 minutes later"), (60, "Keep 1 hour later"), (120, "Keep 2 hours later")]
                    .filter { item.end + Double($0.0 * 60) <= today.dayInterval.end.timeIntervalSince1970 }, id: \.0) { later in
                    Button(later.1) { keep(item, laterBy: later.0) }
                }
                // A grouped review: what it covers, each one askable on its own.
                if item.members.count > 1 {
                    Section("In this block") {
                        ForEach(Array(item.members.enumerated()), id: \.offset) { _, member in
                            Button(member.title) {
                                let reference = plan?.workspace?.attention.first { $0.id == member.evidenceID }?.reference ?? ""
                                model.askAbout(member.title, reference: reference)
                            }
                            .help("Ask Wisp about this one")
                        }
                    }
                }
                if !DayTimeline.removalIDs(item).isEmpty {
                    Button("Remove") { remove(item) }
                        .help("Takes it off your day and Wisp won't suggest it again. The email or reminder stays.")
                }
            case .task:
                if let task = task(for: item) {
                    Button("15 minutes shorter") {
                        Task { await today.edit(task, values: ["duration_minutes": max(1, task.duration_minutes - 15)]) }
                    }
                    Button("15 minutes longer") {
                        Task { await today.edit(task, values: ["duration_minutes": min(1440, task.duration_minutes + 15)]) }
                    }
                }
            case .caught:
                if let record = attention.items.first(where: { $0.source_id == item.caughtID }),
                   record.state == "created", record.undoable == true {
                    Button("Remove the reminder Wisp added") { Task { await attention.undo(record) } }
                }
                Button("Ignore") { ignore(item) }
            case .calendar:
                if let event = calendarEvent(for: item) {
                    eventEntries(item, event)
                } else {
                    Button("Open Calendar") { NotchChrome.open(.calendar) }
                }
            case .needsYou, .overdue, .todo:
                if let todo = proactiveTodo(item.todoID) {
                    if todo.canAddToDay && !proactive.addedToDay.contains(todo.id) {
                        Button("Plan time for it") { Task { await proactive.addToDay(todo, today: today) } }
                    }
                    if todo.source == "reminders" { Button("Open Reminders") { NotchChrome.open(.reminders) } }
                    if todo.source == "mail" { Button("Open Mail") { NotchChrome.open(.mail) } }
                    if todo.source == "messages" { Button("Open Messages") { NotchChrome.open(.messages) } }
                } else if let need = need(for: item), need.source == "reminders" {
                    Button("Open Reminders") { NotchChrome.open(.reminders) }
                }
            case .usual:
                EmptyView()
            }
            Button("Ask Wisp about this") { model.askAbout(item.title, reference: need(for: item)?.reference ?? "") }
        } label: {
            Image(systemName: "ellipsis").font(.wisp(size: 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
                .frame(width: 20, height: 16).contentShape(Rectangle())
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
        .disabled(today.busy)
        .help("More")
        .accessibilityLabel("More actions for \(item.title)")
    }

    /// Cancel, Move and their stand-ins for a Calendar block, by the check-in
    /// card's rules (CheckInCancel.menuEntries). Cancel starts the same 5-second
    /// Undo on the shared queue; Undo is on the block itself. After a cancel
    /// the service couldn't confirm, nothing here may send another: only Open.
    /// What the ⋯ menu offers for a Calendar block, by the check-in card's rules.
    private func eventMenuEntries(_ item: DayItem, _ event: CheckInEvent) -> [CheckInCancel.MenuEntry] {
        let failure = checkIn.cancelFailures[event.id]?.failure
        if let failure, failure.isUncertain { return [.reason(failure.message), .open] }
        return CheckInCancel.menuEntries(described: true, canCancel: event.canCancel, canMove: event.canMove,
                                         calendarWritable: event.calendarWritable, organizerIsUser: event.organizerIsUser,
                                         cancelReason: event.cancelBlocked, isPast: item.past)
    }

    @ViewBuilder private func eventEntries(_ item: DayItem, _ event: CheckInEvent) -> some View {
        let failure = checkIn.cancelFailures[event.id]?.failure
        let entries: [CheckInCancel.MenuEntry] = eventMenuEntries(item, event)
        ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
            switch entry {
            case .cancel:
                Button(CheckInCancel.cancelLabel(inviteeCount: event.inviteeCount, retry: failure?.canRetry == true)) {
                    if movingEvent?.id == event.id { closeMove() }   // not moving what is being cancelled
                    checkIn.beginCancel(event, timezone: today.timezone.identifier, fromCard: false)
                }
                .help("Removes it from your Calendar after a 5-second Undo; Calendar tells its invitees")
            case .move:
                Button("Move…") { openMove(event) }
                    .help("Pick another time; Wisp finds free ones")
            case .reason(let text):
                Text(text)
            case .decline:
                Button("Decline in Calendar ↗") { openInCalendar(event) }
                    .help(event.cancelReason)
            case .open:
                Button("Open in Calendar ↗") { openInCalendar(event) }
            }
        }
    }

    private func task(for item: DayItem) -> TodayTask? {
        item.taskID.flatMap { id in plan?.tasks.first { $0.id == id } }
    }

    private func need(for item: DayItem) -> DailyAttentionItem? {
        let id = item.needID ?? item.evidenceID
        return id.flatMap { id in plan?.workspace?.attention.first { $0.id == id } }
    }

    /// Keep a suggestion: it becomes your task, pinned at its time (or later).
    private func keep(_ item: DayItem, laterBy minutes: Int) {
        let source = need(for: item)
        let start = max(item.start + Double(minutes * 60), NotchChrome.nextFiveMinutes(after: today.now))
        let length = max(5, Int(((item.end - item.start) / 60).rounded()))
        let now = today.now.timeIntervalSince1970
        Task {
            _ = await today.add(title: item.title, kind: source?.task_kind ?? "task", minutes: length,
                                priority: source?.priority ?? 2,
                                due: source?.due_ts.flatMap { $0 > now ? Date(timeIntervalSince1970: $0) : nil },
                                pinned: Date(timeIntervalSince1970: start),
                                covers: DayTimeline.removalIDs(item))
        }
    }

    /// Plan time for a reminder: it joins the plan as a task the planner fits in.
    /// The task keeps the reminder's due time as its origin, so an old reminder
    /// planned today still ages out of the carry-forward (checkin.task_origin).
    private func planTime(_ item: DayItem) {
        guard let need = need(for: item), !planned(need) else { return }
        let now = today.now.timeIntervalSince1970
        Task {
            _ = await today.add(title: DayTimeline.plannedTitle(need.title, review: need.needs_review && need.ownership != "user"),
                                kind: need.task_kind ?? "task", minutes: need.suggested_minutes, priority: need.priority,
                                due: need.due_ts.flatMap { $0 > now ? Date(timeIntervalSince1970: $0) : nil },
                                origin: DayTimeline.planOrigin(due: need.due_ts, now: now))
        }
    }

    /// The dated Reminders reminder a row stands for, if Cancel can delete it.
    private func cancellableReminder(_ item: DayItem) -> DailyAttentionItem? {
        guard let need = need(for: item),
              DayReminderCancel.isCancellable(id: need.id, source: need.source, due: need.due_ts) else { return nil }
        return need
    }

    /// Cancel's one question, in the row: nothing is deleted until Delete.
    private func reminderCancelConfirm(_ reminder: DailyAttentionItem) -> some View {
        HStack(spacing: 6) {
            if cancelBusy == reminder.id {
                ProgressView().controlSize(.small)
                Text("Deleting…").font(.wisp(size: 10.5)).foregroundStyle(Theme.textSecondary)
            } else {
                Text(DayReminderCancel.question).font(.wisp(size: 10.5, weight: .medium)).foregroundStyle(Theme.textPrimary)
                NotchChrome.small("Keep") { cancelAsking = nil; model.onResize() }
                    .keyboardShortcut(.cancelAction)
                    .accessibilityLabel("Keep \(reminder.title)")
                Button("Delete") { cancelReminder(reminder) }
                    .buttonStyle(.plain)
                    .font(.wisp(size: 10.5, weight: .semibold)).foregroundStyle(.black)
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(Capsule().fill(Theme.bad))
                    .fixedSize()
                    .accessibilityLabel("Delete \(reminder.title) from Reminders")
            }
        }
    }

    /// Deletes the reminder through the service's verified path; the row leaves on
    /// the refresh that follows a confirmed delete.
    private func cancelReminder(_ reminder: DailyAttentionItem) {
        guard cancelBusy == nil, let due = reminder.due_ts else { return }
        cancelBusy = reminder.id
        Task {
            let failure = await DayReminderCancel.send(id: reminder.id, title: reminder.title, due: due)
            cancelBusy = nil
            cancelAsking = nil
            if let failure {
                cancelFailed[reminder.id] = failure
                NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                                     userInfo: [.announcement: "Not cancelled: \(failure)"])
            } else {
                await today.refresh(afterMutation: true)
            }
            model.onResize()
        }
    }

    /// The day already has an active task for this need, so Plan time would add it twice.
    private func planned(_ need: DailyAttentionItem) -> Bool {
        DayTimeline.alreadyPlanned(need.title, tasks: (plan?.tasks ?? []).map { (title: $0.title, active: $0.status == "active") })
    }

    /// Add a plan caught in your texts to your day, at the time the text gave.
    private func addCaught(_ item: DayItem) {
        let start = max(item.start, NotchChrome.nextFiveMinutes(after: today.now))
        Task {
            if await today.add(title: item.title, kind: "task", minutes: 60, priority: 2, due: nil,
                               pinned: Date(timeIntervalSince1970: start)) {
                ignore(item)
            }
        }
    }

    private func ignore(_ item: DayItem) {
        guard let id = item.caughtID else { return }
        ignoredCaught.insert(id)
        IgnoredCaught.add(id)
    }

    /// Remove a suggestion: its items are never suggested again (by Replan or a
    /// later plan). Only Wisp's record changes; the source stays in Needs you.
    private func remove(_ item: DayItem) {
        let ids = DayTimeline.removalIDs(item)
        guard !ids.isEmpty else { return }
        let notice = DayTimeline.removedNotice(item)
        Task {
            guard await today.mutate("/assistant/today/workspace/suggestions/remove", method: "POST",
                                     body: ["evidence_ids": ids]) else { return }
            showUndo((ids, notice))
        }
    }

    /// The Undo bar for a Remove, for removeUndoSeconds.
    private func showUndo(_ removed: (ids: [String], notice: String)) {
        lastRemoved = removed
        lastRemovedTask?.cancel()
        lastRemovedTask = Task {
            try? await Task.sleep(nanoseconds: Self.removeUndoSeconds * 1_000_000_000)
            guard !Task.isCancelled else { return }
            lastRemoved = nil
            model.onResize()
        }
        model.onResize()
    }

    /// Undo the last Remove: the same items, in one call, back into the plan.
    /// The bar stays until the service says yes; if it doesn't, the bar comes
    /// back for another try, under the day's error line.
    private func undoRemove(_ removed: (ids: [String], notice: String)) {
        lastRemovedTask?.cancel()
        Task {
            let restored = await today.mutate("/assistant/today/workspace/suggestions/restore", method: "POST",
                                              body: ["evidence_ids": removed.ids])
            if restored {
                if lastRemoved?.ids == removed.ids { lastRemoved = nil }
                model.onResize()
            } else {
                // A newer Remove's bar stays; only this Remove's own bar comes back.
                let stillShowing = lastRemoved?.ids == removed.ids
                if today.error.isEmpty { today.error = DayTimeline.undoFailedNotice(stillShowing: stillShowing) }
                if stillShowing { showUndo(removed) }
            }
        }
    }

    /// Today's removed suggestions may be suggested again (only today's: the
    /// ids the plan listed, never another day's).
    private func bringBackRemoved(_ ids: [String]) {
        lastRemovedTask?.cancel()
        lastRemoved = nil
        Task {
            let restored = await today.mutate("/assistant/today/workspace/suggestions/restore", method: "POST",
                                              body: ["evidence_ids": ids])
            if !restored && today.error.isEmpty { today.error = "Couldn't bring them back just now. Try again." }
        }
    }

    private func removedRow(_ removed: (ids: [String], notice: String)) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(removed.notice).font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            NotchChrome.small("Undo") { undoRemove(removed) }
                .disabled(today.busy)
                .help("Puts it back on your day")
                .accessibilityLabel("Undo: \(removed.notice)")
        }
    }

    private func notToday(_ item: DayItem) {
        guard let pattern = item.patternID else { return }
        Task {
            _ = await today.mutate("/assistant/today/workspace/availability", method: "POST",
                                   body: ["day": today.day, "timezone": today.timezone.identifier,
                                          "window": pattern, "ignored": true])
        }
    }

    // MARK: Adding, footer

    private var canAdd: Bool {
        !model.todayTaskTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !today.busy
    }

    // An inline row, because the notch panel can't host the planner's sheet.
    // While it holds text the panel stays open.
    private var addTaskRow: some View {
        HStack(spacing: 8) {
            TextField("Make time for something…", text: $model.todayTaskTitle)
                .textFieldStyle(.plain)
                .font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
                .focused($taskFieldFocused)
                .onSubmit(addTask)
                .onAppear { taskFieldFocused = true }
            Menu {
                ForEach([15, 30, 45, 60, 90, 120], id: \.self) { value in
                    Button("\(value) min") { minutes = value }
                }
            } label: {
                Text("\(minutes) min").font(.wisp(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 9).padding(.vertical, 5)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .help("Your estimate for how long it takes")
            NotchChrome.small("Add to day", action: addTask).disabled(!canAdd)
        }
        .disabled(plan == nil)
    }

    private func addTask() {
        guard canAdd else { return }
        let title = model.todayTaskTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            if await today.add(title: title, kind: "task", minutes: minutes, priority: 2, due: nil),
               model.todayTaskTitle.trimmingCharacters(in: .whitespacesAndNewlines) == title {
                model.todayTaskTitle = ""
                addingTask = false
            }
        }
    }

    /// "Running late" only means something once your own tasks have times.
    private var hasTimedTasksAhead: Bool {
        guard today.isShowingCurrentDay, let plan else { return false }
        let now = today.now.timeIntervalSince1970
        return plan.displayBlocks.contains { $0.task_id != nil && ($0.end ?? $0.start) > now }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            NotchChrome.chip("Make time for…", icon: "plus", help: "Add something to fit into your day") {
                addingTask.toggle()
                model.onResize()
            }
            .disabled(plan == nil)
            if let plan {
                NotchChrome.chip("Replan", icon: "arrow.triangle.2.circlepath",
                                 help: "Fit your day again within your working hours") {
                    Task { await today.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute) }
                }
                .disabled(today.busy)
            }
            Spacer()
            Menu {
                if hasTimedTasksAhead, let plan {
                    Button("Running 15 minutes late") {
                        Task {
                            await today.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute,
                                               delayMinutes: 15)
                        }
                    }
                }
                if let workspace = plan?.workspace,
                   let label = DayTimeline.bringBackLabel(blocks: workspace.removed_suggestion_blocks) {
                    Button(label) { bringBackRemoved(workspace.removed_suggestions) }
                        .help("Wisp may suggest them again, from this plan on")
                }
                Button("Report a problem with this plan") { today.reportProblem() }
            } label: {
                Image(systemName: "ellipsis").font(.wisp(size: 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
                    .frame(width: 26, height: 20)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .disabled(today.busy)
            .help("More")
            .accessibilityLabel("More day actions")
            NotchChrome.chip("Plan in window ↗", icon: nil,
                             help: "Tasks, pins, working hours and the daily email, in the Wisp window (⌘O)") {
                WispRouter.shared.open(.today(focus: .plan))
            }
        }
    }
}

// MARK: - The Day's two halves

/// Lays out the Day's halves (left, a hairline, right). Side by side: each
/// half as wide and the hairline between them as tall as the taller half; a
/// right half with nothing in it gives the left the whole width, with no
/// hairline. Stacked (the normal panel): left, then right `stackGap` below it
/// when it holds anything, and no hairline, exactly as one column.
struct DayHalves: Layout {
    let sideBySide: Bool
    var gap: CGFloat = 28
    var stackGap: CGFloat = 10

    private func columnWidth(_ width: CGFloat) -> CGFloat { max(0, (width - gap) / 2) }

    /// Whether the halves sit side by side at this width: asked for, and the
    /// right half holds something.
    private func split(_ subviews: Subviews, width: CGFloat) -> Bool {
        sideBySide && subviews[2].sizeThatFits(ProposedViewSize(width: columnWidth(width), height: nil)).height > 0
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard subviews.count == 3 else { return .zero }
        let width: CGFloat = proposal.width ?? 0
        if split(subviews, width: width) {
            let column = ProposedViewSize(width: columnWidth(width), height: nil)
            let left: CGFloat = subviews[0].sizeThatFits(column).height
            let right: CGFloat = subviews[2].sizeThatFits(column).height
            return CGSize(width: width, height: max(left, right))
        }
        let full = ProposedViewSize(width: width, height: nil)
        let left: CGFloat = subviews[0].sizeThatFits(full).height
        let right: CGFloat = sideBySide ? 0 : subviews[2].sizeThatFits(full).height
        return CGSize(width: width, height: left + (right > 0 ? stackGap + right : 0))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard subviews.count == 3 else { return }
        let width: CGFloat = bounds.width
        if split(subviews, width: width) {
            let columnW: CGFloat = columnWidth(width)
            let column = ProposedViewSize(width: columnW, height: nil)
            subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: column)
            let line = CGPoint(x: bounds.minX + columnW + gap / 2, y: bounds.minY)
            subviews[1].place(at: line, anchor: .top, proposal: ProposedViewSize(width: 1, height: bounds.height))
            subviews[2].place(at: CGPoint(x: bounds.maxX - columnW, y: bounds.minY), anchor: .topLeading, proposal: column)
            return
        }
        let full = ProposedViewSize(width: width, height: nil)
        let left: CGFloat = subviews[0].sizeThatFits(full).height
        subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: full)
        // The hairline only divides halves side by side.
        subviews[1].place(at: bounds.origin, anchor: .topLeading, proposal: ProposedViewSize(width: 0, height: 0))
        subviews[2].place(at: CGPoint(x: bounds.minX, y: bounds.minY + left + stackGap), anchor: .topLeading, proposal: full)
    }
}

/// The week row's cards: equal columns across the whole width, `spacing`
/// apart, each as tall as the tallest, so cards, numbers and bars line up.
struct EqualColumns: Layout {
    var spacing: CGFloat = 6
    /// Each column's share of the width (equal when empty or mismatched).
    var weights: [Double] = []

    private func widths(_ width: CGFloat, count: Int) -> [CGFloat] {
        guard count > 0 else { return [] }
        let shares: [Double] = weights.count == count ? weights : Array(repeating: 1, count: count)
        let total: Double = max(0.0001, shares.reduce(0, +))
        let room: CGFloat = max(0, width - spacing * CGFloat(count - 1))
        return shares.map { room * CGFloat($0 / total) }
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width: CGFloat = proposal.width ?? 0
        let columns: [CGFloat] = widths(width, count: subviews.count)
        let heights: [CGFloat] = zip(subviews, columns).map { $0.sizeThatFits(ProposedViewSize(width: $1, height: nil)).height }
        return CGSize(width: width, height: heights.max() ?? 0)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let columns: [CGFloat] = widths(bounds.width, count: subviews.count)
        var x: CGFloat = bounds.minX
        for (subview, columnW) in zip(subviews, columns) {
            subview.place(at: CGPoint(x: x, y: bounds.minY), anchor: .topLeading,
                          proposal: ProposedViewSize(width: columnW, height: bounds.height))
            x += columnW + spacing
        }
    }
}

/// The Day's week row. Compact (one line a card, the shown day twice as wide
/// with its count and a thin bar along its foot) while only the shown day has
/// a load; full height (weekday and date, a count line, a bar) once other
/// days carry one. Each form is as tall with its loads as without them, so
/// the row never moves the panel when the plan arrives.
struct DayWeekRow: View {
    let cards: [DayWeekCard]
    /// The form, chosen by the Day before its first frame; by the cards when nil.
    var full: Bool? = nil
    var pick: (DayWeekCard) -> Void = { _ in }

    var body: some View {
        let full: Bool = DayWeek.full(latched: self.full, cards: cards)
        return EqualColumns(spacing: 6, weights: DayWeek.weights(cards, full: full)) {
            ForEach(cards) { card in
                DayWeekCardView(card: card, compact: !full) { pick(card) }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("This week")
    }
}

struct DayWeekCardView: View {
    let card: DayWeekCard
    let compact: Bool
    let action: () -> Void

    private var filled: Bool { card.isShown }
    private var ink: Color { filled ? Theme.surface : Theme.textPrimary }
    private var muted: Color { filled ? Theme.surface.opacity(0.62) : Theme.textMuted }

    var body: some View {
        let stroke: Color = card.isToday && !filled ? Theme.textSecondary : Theme.chipStroke
        Button(action: action) {
            face
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: compact ? 8 : 10).fill(filled ? Theme.textPrimary : Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: compact ? 8 : 10).stroke(stroke, lineWidth: 1))
                .contentShape(RoundedRectangle(cornerRadius: compact ? 8 : 10))
        }
        // No pressed look: the white card must not flash.
        .buttonStyle(StudyRowButtonStyle())
        .help(card.isShown ? "The day shown" : card.isToday ? "Back to today" : "Show this day")
        .accessibilityLabel(DayWeek.spoken(card))
        .accessibilityHint(card.isShown ? "" : "Shows this day")
        .accessibilityAddTraits(card.isShown ? .isSelected : [])
    }

    @ViewBuilder private var face: some View {
        if compact {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                date
                Spacer(minLength: 4)
                count
            }
            .padding(.horizontal, 8).padding(.top, 5).padding(.bottom, 7)
            // The bar runs along the card's foot, inside its padding: no height of its own.
            .overlay(alignment: .bottom) {
                bar(height: 2.5, tick: 5).padding(.horizontal, 8).padding(.bottom, 2)
            }
        } else {
            VStack(alignment: .leading, spacing: 4) {
                date
                count
                bar(height: 4, tick: 9).frame(height: 9)
            }
            .padding(.horizontal, 9).padding(.vertical, 7)
        }
    }

    /// Weekday and date; where both don't fit (larger text, a narrow card),
    /// the date alone, never cut.
    private var date: some View {
        let number = Text("\(card.number)").font(.wisp(size: compact ? 13.5 : 15, weight: .semibold)).monospacedDigit()
            .foregroundColor(ink)
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(card.weekday).font(.wisp(size: 9.5, weight: .medium, design: .monospaced)).foregroundStyle(muted)
                number
            }
            .lineLimit(1).fixedSize()
            number.lineLimit(1).fixedSize()
        }
        .layoutPriority(1)
    }

    /// "3 · 2 due"; with less room "3"; then nothing, never a cut date. A day
    /// without a load keeps the line (full form) or the room (compact) empty.
    private var count: some View {
        let full: String = card.load.map(DayWeek.countText) ?? " "
        let short: String = card.load.map(DayWeek.shortCountText) ?? " "
        return ViewThatFits(in: .horizontal) {
            Text(full)
            Text(short)
            Text(" ")
        }
        .font(.wisp(size: 10.5)).monospacedDigit().foregroundStyle(muted).lineLimit(1)
        .accessibilityHidden(true)
    }

    /// The day's held stretches on a thin bar, with now as a red tick on today.
    /// A day without a load draws nothing, never a guessed bar.
    private func bar(height: CGFloat, tick: CGFloat) -> some View {
        let track: Color = filled ? Theme.surface.opacity(0.1) : Theme.chipFill
        let mark: Color = filled ? Theme.surface : Theme.textPrimary
        return GeometryReader { geo in
            if let load = card.load {
                let width: CGFloat = geo.size.width
                ZStack(alignment: .leading) {
                    Capsule().fill(track).frame(height: height)
                    ForEach(Array(load.segments.enumerated()), id: \.offset) { _, segment in
                        Capsule().fill(mark.opacity(segment.past ? 0.35 : 1))
                            .frame(width: max(3, CGFloat(segment.to - segment.from) * width), height: height)
                            .offset(x: CGFloat(segment.from) * width)
                    }
                    if let now = load.now, card.isToday {
                        Rectangle().fill(DayInk.now).frame(width: 2, height: tick)
                            .offset(x: min(width - 2, max(0, CGFloat(now) * width - 1)))
                    }
                }
                .frame(height: tick)
            }
        }
        .frame(height: tick)
        .accessibilityHidden(true)
    }
}

/// The Day halves' drawn heights, for balancing Later today.
struct DayHalfHeights: PreferenceKey {
    static let defaultValue: [String: CGFloat] = [:]
    static func reduce(value: inout [String: CGFloat], nextValue: () -> [String: CGFloat]) {
        value.merge(nextValue()) { $1 }
    }
}

// MARK: - Shared by Brief and Day

extension TodayModel {
    /// The plan, only when it is for the day and time zone shown.
    var currentPlan: TodayPlan? {
        guard let plan, plan.day == day, plan.timezone == timezone.identifier else { return nil }
        return plan
    }
}

extension TodayPlan {
    /// The workspace's "Needs you" items, for the timeline and the badge.
    var dayNeeds: [DayNeedInput] {
        (workspace?.attention ?? []).map {
            DayNeedInput(id: $0.id, title: $0.title, source: $0.source, due: $0.due_ts, allDay: $0.all_day == true)
        }
    }

    /// School work due that day (deadlines from service/study/day_hook.py), for
    /// the agenda only: "Due 11:59 PM". Not counted in the badge.
    var studyNeeds: [DayNeedInput] {
        deadlines.filter { $0.source == "study" }.map {
            DayNeedInput(id: $0.id, title: $0.title, source: "study", due: $0.due_ts, allDay: $0.all_day == true)
        }
    }
}

/// Data refresh belongs to the app. These views only size the panel when the
/// shared plan changes; their disappearance never cancels a network request.
struct DayRefreshLoop: ViewModifier {
    let model: OverlayModel
    @ObservedObject var today: TodayModel

    func body(content: Content) -> some View {
        content.onReceive(today.$plan) { _ in
            model.onResize()
        }
    }
}

/// The wide Day's last Later-today decision (NotchDayView.balanceHalves), kept
/// for the app's life so a Day page built afresh by a tab switch is laid out
/// trimmed or not from its first frame, as the last one was.
@MainActor
enum DayLaterTrimMemory {
    static var last: (key: String, trimmed: Bool)?
}

/// The Day tab's card sources: plans caught in texts and the stale-reminder
/// questions. One instance each for the whole app, so switching tabs keeps
/// what was loaded instead of starting empty and refetching. The app refreshes
/// them with the plan even while the notch is collapsed.
@MainActor
enum DayCards {
    static var attention = AttentionModel()
    static var staleReminders = StaleRemindersModel(baseURL: WispClient.baseURL)
    static let hoursOffer = HoursOfferModel()
    private static var refreshedAt: Date?

    /// At most once a minute (the old per-tab cadence) unless forced: the loop
    /// ticks every 30 s, and a tab switch restarts it.
    static func refresh(force: Bool = false, now: Date = Date()) async {
        if !force, let last = refreshedAt, now.timeIntervalSince(last) < 60 { return }
        refreshedAt = now
        await attention.refresh()
        await staleReminders.refresh()
        #if !WISP_NOTCH_QA
        await hoursOffer.refresh()
        #endif
    }

    #if WISP_NOTCH_QA
    static func qaUse(attention: AttentionModel, staleReminders: StaleRemindersModel) {
        self.attention = attention
        self.staleReminders = staleReminders
        refreshedAt = nil
    }
    #endif
}

/// Plans caught in texts that the person chose to ignore here. Kept on this Mac
/// only, oldest first, and only the newest 200.
enum IgnoredCaught {
    private static let key = "WispIgnoredCaughtPlans"

    static func load() -> Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: key) ?? [])
    }

    static func add(_ id: String) {
        var ids = (UserDefaults.standard.stringArray(forKey: key) ?? []).filter { $0 != id }
        ids.append(id)
        UserDefaults.standard.set(Array(ids.suffix(200)), forKey: key)
    }
}

/// The Day's few colors, taken from WispInk. Everything else is the notch's monochrome Theme.
enum DayInk {
    static let calendarFill = Color(white: 0.11)
    static let calendarStroke = Color(white: 0.24)
    static let suggested = Color(nsColor: WispInk.rgb(WispInk.blue.dark))
    static let caught = Theme.warn
    static let now = Theme.bad

    /// A row's mark in the agenda: its style's ink as a 3-point bar, dashed for
    /// a suggestion and dotted for a reminder due later.
    @ViewBuilder static func bar(_ style: DayStyle) -> some View {
        switch style {
        case .calendar: Capsule().fill(Theme.textPrimary)
        case .task: Capsule().fill(Color(white: 0.6))
        case .suggested: Rule().stroke(suggested, style: StrokeStyle(lineWidth: 3, lineCap: .round, dash: [4, 4]))
        case .usual: Capsule().fill(Color(white: 0.3))
        case .caught: Capsule().fill(caught)
        case .needsYou: Rule().stroke(Color.white.opacity(0.55), style: StrokeStyle(lineWidth: 3, lineCap: .round, dash: [0, 5]))
        case .overdue: Capsule().fill(now)
        case .todo: Capsule().fill(Color.white.opacity(0.45))
        }
    }

    /// A free stretch's mark: a faint dotted bar.
    static var freeBar: some View {
        Rule().stroke(Theme.textMuted.opacity(0.6), style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [0, 4]))
    }

    /// A Later today row's duration bar, in its style's ink (as `bar`).
    static func fill(_ style: DayStyle) -> Color {
        switch style {
        case .calendar: return Theme.textPrimary
        case .task: return Color(white: 0.6)
        case .suggested: return suggested
        case .usual: return Color(white: 0.3)
        case .caught: return caught
        case .needsYou: return Color.white.opacity(0.55)
        case .overdue: return now
        case .todo: return Color.white.opacity(0.45)
        }
    }

    static func detail(_ style: DayStyle) -> Color {
        switch style {
        case .suggested: return suggested
        case .caught: return caught
        case .overdue: return now
        default: return Theme.textMuted
        }
    }

    /// A vertical line down the middle of its frame, inset for round caps.
    struct Rule: Shape {
        func path(in rect: CGRect) -> Path {
            var path = Path()
            path.move(to: CGPoint(x: rect.midX, y: rect.minY + 1.5))
            path.addLine(to: CGPoint(x: rect.midX, y: max(rect.minY + 1.5, rect.maxY - 1.5)))
            return path
        }
    }
}

/// What a Brief study card or a Day "Review (15 min)" does: switch to Study,
/// then open the quiz Study made or make flash cards from the slides. Display
/// only: nothing here reaches a prompt, a log or a notification.
@MainActor
enum SchoolActions {
    static func refs(_ pages: [SchoolPage]) -> [StudySlideRef] {
        pages.prefix(SchoolSlidesText.pageCap).map { StudySlideRef(sourceID: $0.sourceID, page: $0.page) }
    }

    static func run(_ action: StudyBriefCard.Action, model: OverlayModel) {
        switch action {
        case .open(let id):
            model.tab = .study
            Task { await StudyModel.shared.open(id) }
        case .review(let pages, let title):
            review(pages, title: title, model: model)
        }
    }

    /// The Brief's "Make a guide": switches to Study and starts Study's own
    /// path for that assignment, as its Guide button does (the key card
    /// opens instead when the key isn't ready). Without the assignment in
    /// Study's list, Study just opens.
    static func makeGuide(assignmentID: String, model: OverlayModel) {
        model.tab = .study
        Task {
            let study = StudyModel.shared
            if study.state == nil { await study.refresh() }
            guard let state = study.state,
                  let assignment = state.assignments.first(where: { $0.id == assignmentID }) else { return }
            guard state.connector.ready else {
                study.showingConnector = true
                return
            }
            await study.make(.guide, course: state.course(assignment.courseID), assignment: assignment, topic: nil)
        }
    }

    static func review(_ pages: [SchoolPage], title: String, model: OverlayModel) {
        let chosen = Self.refs(pages)
        guard !chosen.isEmpty else { return }
        model.tab = .study
        Task {
            await StudyModel.shared.makeFromPages(.flashcards, pages: chosen, count: StudyBriefCard.reviewCount, title: title)
        }
    }
}

/// Where each study block's "Add to calendar" stands, for the whole session,
/// so a tab switch never loses an add in flight or a missing route.
@MainActor
final class StudyBlockAdds: ObservableObject {
    static let shared = StudyBlockAdds()
    @Published var ledger = StudyBlockCalendar.Ledger()
}

/// Small controls shared by the notch's Brief and Day tabs.
enum NotchChrome {
    static func notice(_ text: String) -> some View {
        Text(text).font(.wisp(size: 11)).foregroundStyle(Theme.bad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }

    static func small(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.wisp(size: 10.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(Color.black.opacity(0.35)))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    static func chip(_ title: String, icon: String?, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if let icon { Image(systemName: icon).font(.wisp(size: 10)) }
                Text(title).font(.wisp(size: 11.5, weight: .medium))
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(help)
        .fixedSize()
    }

    /// A small capsule action with a 24 pt tall hit target: the capsule looks
    /// like `small`; the clear band around it still takes the click.
    static func action(_ title: String, help: String, spoken: String? = nil,
                       action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.wisp(size: 10.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .lineLimit(1)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Capsule().fill(Color.black.opacity(0.35)))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                .frame(minHeight: 24)
                .contentShape(Rectangle())
        }
        // No pressed look: a click must not flash the capsule.
        .buttonStyle(StudyRowButtonStyle())
        .help(help)
        .accessibilityLabel(spoken ?? title)
        .fixedSize()
    }

    static func iconButton(_ icon: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.wisp(size: 9, weight: .bold)).foregroundStyle(Theme.textSecondary)
                .frame(width: 19, height: 19)
                .background(Circle().fill(Theme.chipFill))
                .overlay(Circle().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(help)
    }

    /// The next whole five minutes, so a kept block never starts in the past.
    static func nextFiveMinutes(after date: Date) -> Double {
        (date.timeIntervalSince1970 / 300).rounded(.up) * 300
    }

    /// "Wed 6 PM" style (or "Wed 18:00"), for a time on another day.
    static func dayClock(_ timestamp: Double, zone: TimeZone, locale: Locale = WispClock.fixedLocale) -> String {
        let formatter = DateFormatter()
        let h24 = WispClock.uses24Hour(locale)
        formatter.locale = Locale(identifier: h24 ? "en_GB" : "en_US_POSIX")
        formatter.timeZone = zone
        formatter.dateFormat = h24 ? "EEE HH:mm" : "EEE h:mm a"
        return formatter.string(from: Date(timeIntervalSince1970: timestamp))
            .replacingOccurrences(of: ":00 ", with: " ")
    }

    static func appName(_ source: DayTimeline.BriefSource) -> String {
        switch source {
        case .calendar: return "Calendar"
        case .mail: return "Mail"
        case .messages: return "Messages"
        case .reminders: return "Reminders"
        }
    }

    /// Opens the app a Brief line or a block came from.
    static func open(_ source: DayTimeline.BriefSource) {
        let id: String
        switch source {
        case .calendar: id = "com.apple.iCal"
        case .mail: id = "com.apple.mail"
        case .messages: id = "com.apple.MobileSMS"
        case .reminders: id = "com.apple.reminders"
        }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: id) else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
}


extension DayReminderCancel {
    /// Nil once Reminders confirmed the delete, else what to say.
    static func send(id: String, title: String, due: Double) async -> String? {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("assistant/needs_you")
            .appendingPathComponent(id).appendingPathComponent("cancel"))
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["title": title, "due_ts": due])
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            return failure(status: status, body: data)
        } catch {
            return "Wisp couldn't reach its service. Nothing was deleted."
        }
    }
}
