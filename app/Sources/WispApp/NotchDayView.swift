import SwiftUI
import AppKit

/// The notch's Day tab: the rest of today on one clock. Calendar events, your
/// planned tasks, Wisp's suggestions, learned "usually busy" windows, plans
/// caught in your texts and "Needs you" reminders share an hour-by-hour lane
/// from now to the end of the day, under a two-line blurb.
///
/// Nothing reaches the plan on its own: a suggestion needs Keep or Move, a
/// caught plan needs Add, and "Not today" only sets aside a learned window for
/// this day (the planner's existing one-day ignore). Everything lands in Wisp's
/// planner, never in Calendar.
///
/// The full planner (task editor, pins, working hours, the daily email) stays
/// in the Wisp window, because this non-activating panel can't host sheets.
/// The plan loads while this tab or Brief is on screen, never at launch or on
/// hover of the bar.
struct NotchDayView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    // Plans caught in texts and the stale-reminder questions come from shared
    // models (DayCards) that outlive a tab switch and load with the plan on
    // Brief, so the Day tab's first frame already holds every card.
    @ObservedObject private var attention = DayCards.attention
    @State private var minutes = 45
    @State private var addingTask = false
    @State private var ignoredCaught = IgnoredCaught.load()
    // Evening check-in and weekly goal pulse (CheckInView.swift): one tap per
    // task at the end of the day, so tomorrow's plan starts honest.
    @ObservedObject private var checkIn = CheckInModel.shared
    /// The Calendar block whose Move sub-panel is open from the timeline's ⋯ menu.
    @State private var movingEvent: CheckInEvent?
    // Old undated reminders to keep or delete (StaleReminders.swift).
    @ObservedObject private var staleReminders = DayCards.staleReminders

    /// One hour of the lane.
    private let hourHeight: CGFloat = 44
    /// Tallest the lane gets before it scrolls, so the tab fits the notch.
    private let laneLimit: CGFloat = 280


    init(model: OverlayModel, today: TodayModel) {
        self.model = model
        self.today = today
    }

    private var plan: TodayPlan? { today.currentPlan }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let notice = model.transientNotice { NotchChrome.notice(notice) }
            if !today.error.isEmpty { NotchChrome.notice(today.error) }
            if !today.followsToday { otherDayRow }
            if today.isShowingCurrentDay {
                CheckInSection(model: checkIn, day: today.day, timezone: today.timezone,
                               clock: today.clock,
                               onAsk: { model.askAbout($0) },
                               onOpenEvent: openInCalendar,
                               onChanged: { Task { await today.refresh(); model.onResize() } })
                GoalPulseSection(model: checkIn)
            }
            if let plan {
                dayContent(plan)
            } else if today.error.isEmpty {
                ProcessingRow(label: "Loading your day…")
            }
            ReportDraftCard(draft: today.report, title: "Report a problem with this plan",
                            detailsLabel: "Include planner details", save: { model.saveReport(today.report) })
            footer
        }
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
        // A fresh plan for the shown day: a timeline cancel whose block changed or
        // left is dropped before it is sent, and a Move panel for such a block is
        // closed. No plan (a failed refresh, a day change) proves nothing and
        // changes nothing.
        .onChange(of: plan?.generated_at) { _, _ in
            guard let plan else { return }
            let events = plan.displayBlocks.compactMap(\.calendarEvent)
            checkIn.reconcileTimelineCancels(events)
            if let movingEvent, !events.contains(where: { CheckInModel.sameOccurrence($0, movingEvent) }) { closeMove() }
        }
        // A cancel the service answered: the plan is read again, so a cancelled
        // block goes and a refused one shows the fresh facts the service now has.
        .onChange(of: checkIn.cancelling) { before, now in
            guard !before.isSubset(of: now) else { return }
            Task { await today.refresh(afterMutation: true); model.onResize() }
        }
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
        let shown = Array(staleReminders.asks.prefix(3))
        return VStack(alignment: .leading, spacing: 6) {
            Text("Old reminders with no date").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                .accessibilityAddTraits(.isHeader)
            ForEach(shown) { ask in staleRow(ask) }
            if staleReminders.asks.count > shown.count {
                Text("+\(staleReminders.asks.count - shown.count) more").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
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
                    Text(ask.title).font(.system(size: 13)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                    Text(ask.detail).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
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
                Text(failure).font(.system(size: 11)).foregroundStyle(Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(ask.title), \(ask.detail)")
    }

    @ViewBuilder private func dayContent(_ plan: TodayPlan) -> some View {
        let timeline = timeline(plan)
        Text(DayTimeline.blurb(timeline, needs: plan.dayNeeds, isToday: today.isShowingCurrentDay,
                               endOfDay: today.dayInterval.end.timeIntervalSince1970, clock: today.clock))
            .font(.system(size: 13.5)).foregroundStyle(Theme.textPrimary)
            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            .padding(.leading, 10)
            .overlay(alignment: .leading) { Rectangle().fill(Theme.chipStroke).frame(width: 2) }
            .frame(maxWidth: .infinity, alignment: .leading)
        if !timeline.allDay.isEmpty { allDayStrip(timeline.allDay) }
        if let warning = warningLine(plan) {
            Text(warning).font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
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
        lane(timeline)
        if !timeline.styles.isEmpty { legend(timeline.styles) }
        if today.isShowingCurrentDay && !staleReminders.asks.isEmpty {
            // A question that arrives after the first frame never animates the panel's height.
            staleSection.transaction { $0.animation = nil }
        }
        if let first = plan.displayUnscheduled.first(where: { !DayTimeline.isJunk($0.title) }) {
            Text("Still to fit: \(first.title), \(DayTimeline.plainReason(first.reason)).")
                .font(.system(size: 11.5)).foregroundStyle(Theme.textMuted).lineLimit(1)
        }
        if addingTask || !model.todayTaskTitle.isEmpty { addTaskRow }
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
            blocks: blocks, allDay: plan.all_day.map(\.title), needs: plan.dayNeeds, caught: caught,
            now: today.now.timeIntervalSince1970, dayStart: dayStart, dayEnd: bounds.end.timeIntervalSince1970,
            workStart: dayStart + Double(plan.preferences.start_minute * 60),
            workEnd: dayStart + Double(plan.preferences.end_minute * 60),
            isToday: today.isShowingCurrentDay, clock: today.clock,
            dayClock: { NotchChrome.dayClock($0, zone: today.timezone) })
    }

    /// The day's first planner warning, in plain words.
    private func warningLine(_ plan: TodayPlan) -> String? {
        plan.displayWarnings.lazy.compactMap(DayTimeline.plainWarning).first
    }

    // MARK: Pieces of the day

    private var otherDayRow: some View {
        HStack(spacing: 8) {
            Text(today.selectedDate, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            NotchChrome.chip("Back to today", icon: "calendar", help: "Show today instead of the day picked in the planner") {
                today.returnToToday()
                Task { await today.refresh() }
            }
            .disabled(today.busy)
        }
    }

    private func allDayStrip(_ titles: [String]) -> some View {
        HStack(spacing: 8) {
            Text("All day").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            ForEach(Array(titles.prefix(3).enumerated()), id: \.offset) { _, title in
                Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(RoundedRectangle(cornerRadius: 6).fill(DayInk.calendarFill))
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(DayInk.calendarStroke, lineWidth: 1))
            }
            if titles.count > 3 {
                Text("+\(titles.count - 3)").font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            }
        }
    }

    private func lane(_ timeline: DayTimeline) -> some View {
        let hours: CGFloat = CGFloat((timeline.to - timeline.from) / 3600)
        let height: CGFloat = max(hourHeight, hours * hourHeight)
        return ScrollView(.vertical) {
            HStack(alignment: .top, spacing: 0) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(timeline.hours, id: \.self) { hour in
                        Text(hourLabel(hour, first: hour == timeline.from))
                            .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(Theme.textMuted)
                            .frame(height: hourHeight, alignment: .topLeading)
                            .offset(y: -6)
                    }
                }
                .frame(width: 46, alignment: .leading)
                .overlay(alignment: .topLeading) { nowLabel(timeline) }
                GeometryReader { geo in
                    laneBody(timeline, width: geo.size.width, height: height)
                }
                .frame(height: height)
                .overlay(alignment: .leading) { Rectangle().fill(Theme.hairline).frame(width: 1) }
            }
            .padding(.top, 6)
        }
        .scrollIndicators(.automatic)
        .frame(height: min(height + 6, laneLimit))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Timeline, \(today.clock(timeline.from)) to \(today.clock(timeline.to))")
    }

    private func laneBody(_ timeline: DayTimeline, width: CGFloat, height: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(timeline.hours, id: \.self) { hour in
                Rectangle().fill(Theme.hairline.opacity(0.6)).frame(height: 1)
                    .offset(y: y(hour, timeline))
            }
            ForEach(timeline.items) { item in positioned(item, timeline, width: width) }
            nowMarker(timeline)
        }
        .frame(width: width, height: height, alignment: .topLeading)
    }

    private func positioned(_ item: DayItem, _ timeline: DayTimeline, width: CGFloat) -> some View {
        let column: CGFloat = (width - 10) / CGFloat(item.columns)
        let top: CGFloat = y(item.start, timeline)
        // The same shortest size DayTimeline.arrange lays blocks out by, less the gap.
        let shortest = CGFloat(DayTimeline.minimumDrawn / DayTimeline.hour) * hourHeight
        let tall: CGFloat = max(shortest, y(item.end, timeline) - top - 3)
        let wide: CGFloat = max(40, column - 4)
        let left: CGFloat = 8 + CGFloat(item.column) * column
        return blockView(item, compact: tall < 32).frame(width: wide, height: tall).offset(x: left, y: top)
    }

    /// The time of now, in the hour column beside its line, as in Calendar.
    @ViewBuilder private func nowLabel(_ timeline: DayTimeline) -> some View {
        let now = today.now.timeIntervalSince1970
        if today.isShowingCurrentDay && now >= timeline.from && now <= timeline.to {
            Text(today.clock(now)).font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(DayInk.now)
                .frame(width: 46, alignment: .leading)
                .background(Theme.surface)
                .offset(y: y(now, timeline) - 6)
                .accessibilityHidden(true)
        }
    }

    @ViewBuilder private func nowMarker(_ timeline: DayTimeline) -> some View {
        let now = today.now.timeIntervalSince1970
        if today.isShowingCurrentDay && now >= timeline.from && now <= timeline.to {
            nowLine(now).offset(y: y(now, timeline))
        }
    }

    private func y(_ t: Double, _ timeline: DayTimeline) -> CGFloat {
        CGFloat((t - timeline.from) / 3600) * hourHeight
    }

    /// "1 PM" on the first and noon rows, the bare hour on the rest.
    private func hourLabel(_ t: Double, first: Bool) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        let hour = calendar.component(.hour, from: Date(timeIntervalSince1970: t))
        let shown = hour % 12 == 0 ? 12 : hour % 12
        return first || hour == 12 || hour == 0 ? "\(shown) \(hour < 12 ? "AM" : "PM")" : "\(shown)"
    }

    private func nowLine(_ now: Double) -> some View {
        HStack(spacing: 0) {
            Circle().fill(DayInk.now).frame(width: 7, height: 7).offset(x: -4)
            Rectangle().fill(DayInk.now).frame(height: 1.5)
        }
        .offset(y: -3.5)
        .allowsHitTesting(false)
        .accessibilityLabel("Now, \(today.clock(now))")
    }

    /// A short block keeps only its title; its detail is in the VoiceOver label.
    private func blockView(_ item: DayItem, compact: Bool) -> some View {
        HStack(alignment: compact ? .center : .top, spacing: 6) {
            let status = cancelStatus(item)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.title).font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(item.style == .usual ? Theme.textSecondary : Theme.textPrimary)
                    .lineLimit(1)
                    .strikethrough(status != nil && !status!.failed)
                if !compact {
                    Text(detail(item)).font(.system(size: 10.5))
                        .foregroundStyle(status?.failed == true ? Theme.bad : DayInk.detail(item.style)).lineLimit(1)
                }
            }
            Spacer(minLength: 2)
            // What's over has nothing left to act on.
            if !item.past { trailing(item) }
        }
        .padding(.horizontal, 8).padding(.vertical, compact ? 1 : 4)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(DayInk.fill(item.style))
        .overlay(DayInk.border(item.style))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .opacity(item.past ? 0.45 : 1)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(item.title), \(detail(item)), \(today.clock(item.start)) to \(today.clock(item.end))"
                            + (item.members.count > 1 ? ", covers " + item.members.map(\.title).joined(separator: "; ") : "")
                            + (item.past ? ", already over" : ""))
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

    /// A block's detail, with its planner warning in plain words when it has one.
    private func detail(_ item: DayItem) -> String {
        if let status = cancelStatus(item) { return status.text }
        guard let id = item.blockID,
              let block = plan?.displayBlocks.first(where: { $0.id == id }),
              let warning = block.warnings.lazy.compactMap(DayTimeline.plainWarning).first else { return item.detail }
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

    /// The block's main action as a button, and everything else in one ⋯ menu.
    @ViewBuilder private func actions(_ item: DayItem) -> some View {
        HStack(spacing: 4) {
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
            case .needsYou, .overdue:
                if let need = need(for: item), !planned(need) {
                    NotchChrome.small("Plan time") { planTime(item) }.disabled(today.busy)
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
            case .needsYou, .overdue:
                if let need = need(for: item), need.source == "reminders" {
                    Button("Open Reminders") { NotchChrome.open(.reminders) }
                }
            case .usual:
                EmptyView()
            }
            Button("Ask Wisp about this") { model.askAbout(item.title, reference: need(for: item)?.reference ?? "") }
        } label: {
            Image(systemName: "ellipsis").font(.system(size: 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
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
    @ViewBuilder private func eventEntries(_ item: DayItem, _ event: CheckInEvent) -> some View {
        let failure = checkIn.cancelFailures[event.id]?.failure
        let entries: [CheckInCancel.MenuEntry] = failure?.isUncertain == true
            ? [.reason(failure!.message), .open]
            : CheckInCancel.menuEntries(described: true, canCancel: event.canCancel, canMove: event.canMove,
                                        calendarWritable: event.calendarWritable, organizerIsUser: event.organizerIsUser,
                                        cancelReason: event.cancelBlocked, isPast: item.past)
        ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
            switch entry {
            case .cancel:
                Button(failure?.canRetry == true ? "Try cancelling again" : "Cancel event") {
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
                                pinned: Date(timeIntervalSince1970: start))
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

    private func notToday(_ item: DayItem) {
        guard let pattern = item.patternID else { return }
        Task {
            _ = await today.mutate("/assistant/today/workspace/availability", method: "POST",
                                   body: ["day": today.day, "timezone": today.timezone.identifier,
                                          "window": pattern, "ignored": true])
        }
    }

    // MARK: Legend, adding, footer

    private func legend(_ styles: [DayStyle]) -> some View {
        HStack(spacing: 12) {
            ForEach(styles, id: \.self) { style in
                HStack(spacing: 5) {
                    ZStack {
                        DayInk.fill(style)
                        DayInk.border(style)
                    }
                    .frame(width: 12, height: 9).clipShape(RoundedRectangle(cornerRadius: 3))
                    Text(style.legend).font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var canAdd: Bool {
        !model.todayTaskTitle.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !today.busy
    }

    // An inline row, because the notch panel can't host the planner's sheet.
    // While it holds text the panel stays open.
    private var addTaskRow: some View {
        HStack(spacing: 8) {
            TextField("Make time for something…", text: $model.todayTaskTitle)
                .textFieldStyle(.plain)
                .font(.system(size: 13)).foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
                .onSubmit(addTask)
            Menu {
                ForEach([15, 30, 45, 60, 90, 120], id: \.self) { value in
                    Button("\(value) min") { minutes = value }
                }
            } label: {
                Text("\(minutes) min").font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
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
                Button("Report a problem with this plan") { today.reportProblem() }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
                    .frame(width: 26, height: 20)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
            .disabled(today.busy)
            .help("More")
            .accessibilityLabel("More day actions")
            NotchChrome.chip("Full week ↗", icon: nil,
                             help: "Tasks, pins, working hours and the daily email, in the Wisp window (⌘O)") {
                WispRouter.shared.open(.today(focus: .plan))
            }
        }
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
        (workspace?.attention ?? []).map { DayNeedInput(id: $0.id, title: $0.title, source: $0.source, due: $0.due_ts) }
    }
}

/// While Brief or Day is on screen: follow the clock and keep the plan current.
struct DayRefreshLoop: ViewModifier {
    let model: OverlayModel
    let today: TodayModel

    func body(content: Content) -> some View {
        content.task {
            while !Task.isCancelled {
                today.reconcileClock()
                await today.refresh()
                // The evening check-in loads with the plan, on Brief as well as
                // Day, so the Day tab's first frame already holds it and the
                // panel is sized once (CheckInView.swift).
                if today.isShowingCurrentDay {
                    await CheckInModel.shared.load(day: today.day, timezone: today.timezone.identifier)
                    if model.checkInDue { model.checkInDue = false }
                }
                // The Day tab's cards load here too, on Brief as well as Day,
                // before the panel is sized.
                await DayCards.refresh()
                model.onResize()
                do { try await Task.sleep(for: .seconds(30)) } catch { break }
            }
        }
    }
}

/// The Day tab's card sources: plans caught in texts and the stale-reminder
/// questions. One instance each for the whole app, so switching tabs keeps
/// what was loaded instead of starting empty and refetching. DayRefreshLoop
/// refreshes them with the plan, while Brief or Day is on screen.
@MainActor
enum DayCards {
    static var attention = AttentionModel()
    static var staleReminders = StaleRemindersModel(baseURL: WispClient.baseURL)
    private static var refreshedAt: Date?

    /// At most once a minute (the old per-tab cadence) unless forced: the loop
    /// ticks every 30 s, and a tab switch restarts it.
    static func refresh(force: Bool = false, now: Date = Date()) async {
        if !force, let last = refreshedAt, now.timeIntervalSince(last) < 60 { return }
        refreshedAt = now
        await attention.refresh()
        await staleReminders.refresh()
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

/// The Day's few colors. Everything else is the notch's monochrome Theme.
enum DayInk {
    static let calendarFill = Color(white: 0.11)
    static let calendarStroke = Color(white: 0.24)
    static let suggested = Color(red: 0.56, green: 0.72, blue: 1.0)
    static let caught = Color(red: 0.91, green: 0.76, blue: 0.48)
    static let now = Color(red: 1.0, green: 0.42, blue: 0.37)

    @ViewBuilder static func fill(_ style: DayStyle) -> some View {
        switch style {
        case .calendar: calendarFill
        case .task: Color(white: 0.08)
        case .suggested: suggested.opacity(0.08)
        case .usual: Hatch()
        case .caught: caught.opacity(0.08)
        case .needsYou: Color.white.opacity(0.05)
        case .overdue: now.opacity(0.08)
        }
    }

    @ViewBuilder static func border(_ style: DayStyle) -> some View {
        let shape = RoundedRectangle(cornerRadius: 8)
        switch style {
        case .calendar: shape.stroke(calendarStroke, lineWidth: 1)
        case .task: shape.stroke(Color(white: 0.2), lineWidth: 1)
        case .suggested: shape.stroke(suggested, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
        case .usual: shape.stroke(Color(white: 0.17), lineWidth: 1)
        case .caught: shape.stroke(caught, lineWidth: 1)
        case .needsYou: shape.stroke(Color.white.opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [1.5, 2.5]))
        case .overdue: shape.stroke(now, lineWidth: 1)
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

    /// Diagonal stripes for a learned window: there, but not a commitment.
    struct Hatch: View {
        var body: some View {
            Canvas { context, size in
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Color(white: 0.05)))
                var path = Path()
                var x: CGFloat = -size.height
                while x < size.width {
                    path.move(to: CGPoint(x: x, y: size.height))
                    path.addLine(to: CGPoint(x: x + size.height, y: 0))
                    x += 9
                }
                context.stroke(path, with: .color(Color(white: 0.13)), lineWidth: 4)
            }
        }
    }
}

/// Small controls shared by the notch's Brief and Day tabs.
enum NotchChrome {
    static func notice(_ text: String) -> some View {
        Text(text).font(.system(size: 11)).foregroundStyle(Theme.bad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }

    static func small(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.system(size: 10.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(Capsule().fill(Color.black.opacity(0.35)))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    static func chip(_ title: String, icon: String?, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                if let icon { Image(systemName: icon).font(.system(size: 10)) }
                Text(title).font(.system(size: 11.5, weight: .medium))
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

    static func iconButton(_ icon: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.system(size: 9, weight: .bold)).foregroundStyle(Theme.textSecondary)
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

    /// "Wed 6 PM" style, for a time on another day.
    static func dayClock(_ timestamp: Double, zone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = zone
        formatter.dateFormat = "EEE h:mm a"
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
