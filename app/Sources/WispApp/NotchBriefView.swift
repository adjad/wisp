import SwiftUI
import AppKit

/// The notch's Brief tab: what's going on, read only. One sentence about the
/// day, a short version of today's composed Daily Summary (what Wisp is keeping
/// in mind and today's events, without the mail and messages write-ups), then
/// four rows: CAL, MAIL, TEXTS and SOON. Clicking a row opens its details in
/// place, ending with a link to the app. Doing things belongs to Ask and Day.
///
/// Everything comes from the day's plan, the composed brief the service already
/// sent, and the week's upcoming items (`NotchDay.swift`, `BriefSummary.swift`),
/// never a model call. Refresh composes the scheduled brief now, which "Ask
/// about this" continues from.
struct NotchBriefView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    /// The row whose details are open; one at a time.
    @State var open: String? = nil
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var plan: TodayPlan? { today.currentPlan }

    var body: some View {
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
                if !brief.summary.isEmpty || !brief.agenda.isEmpty { summary(brief) }
                VStack(alignment: .leading, spacing: 9) {
                    ForEach(brief.lines) { line in
                        lineRow(line, isOpen: open == line.tag)
                        if open == line.tag, let detail = brief.details[line.tag] {
                            detailView(detail)
                                .transition(reduceMotion ? .identity : .opacity.combined(with: .move(edge: .top)))
                        }
                    }
                }
                HStack(spacing: 8) {
                    NotchChrome.chip("Ask about this", icon: "bubble.left", help: "Continue from this in Ask") {
                        if let brief = model.brief, !brief.sessionId.isEmpty { model.askAboutBrief() }
                        else { model.askAbout(BriefContent(plan: plan, today: today, composed: composedToday).lede, reference: "") }
                    }
                    .disabled(!model.conversationSettled)
                    Spacer()
                    NotchChrome.chip("See my day →", icon: nil, help: "The rest of today, hour by hour (⌘3)") {
                        model.tab = .day
                    }
                }
            } else if today.error.isEmpty {
                ProcessingRow(label: "Loading your day…")
            }
        }
        .environment(\.timeZone, today.timezone)
        .modifier(DayRefreshLoop(model: model, today: today))
        .task {
            // The week ahead changes slowly; SOON reads it while the Brief is open.
            while !Task.isCancelled {
                await today.refreshUpcoming()
                do { try await Task.sleep(for: .seconds(120)) } catch { break }
            }
        }
    }

    /// Today's composed Daily Summary, when one arrived today and the Brief shows today.
    private var composedToday: ComposedBrief? {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        guard let brief = model.brief, today.isShowingCurrentDay,
              calendar.isDate(brief.receivedAt, inSameDayAs: today.now) else { return nil }
        return ComposedBrief.parse(brief.text)
    }

    /// The cut-down Daily Summary: what Wisp keeps in mind, then today's events.
    private func summary(_ brief: BriefContent) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(brief.summary.enumerated()), id: \.offset) { _, line in
                Text(line).font(.system(size: 13.5)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(brief.agenda) { line in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(line.leading).font(.system(size: 12.5)).foregroundStyle(Theme.textMuted).monospacedDigit()
                        .frame(width: Self.timeWidth(brief.agenda), alignment: .leading)
                    Text(line.text).font(.system(size: 13.5)).foregroundStyle(line.muted ? Theme.textMuted : Theme.textSecondary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.leading, 10)
        .overlay(alignment: .leading) { Rectangle().fill(Theme.hairline).frame(width: 2) }
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Daily summary")
    }

    /// Wide enough for "10:00 AM–11:30 AM" only when a line has a span.
    private static func timeWidth(_ lines: [BriefDetail.Line]) -> CGFloat {
        lines.contains { $0.leading.contains("–") } ? 118 : 64
    }

    /// Opening grows the panel first so the details fade into room; closing
    /// shrinks it once they have gone, so it never jumps under them.
    private func toggle(_ tag: String) {
        let next = open == tag ? nil : tag
        let growing = next != nil
        if reduceMotion {
            open = next
            DispatchQueue.main.async { model.onResize() }
            return
        }
        if growing { DispatchQueue.main.async { model.onResize() } }
        withAnimation(.easeOut(duration: 0.18)) { open = next } completion: {
            if !growing { model.onResize() }
        }
    }

    private func detailView(_ detail: BriefDetail) -> some View {
        let timed = detail.lines.contains { !$0.leading.isEmpty }
        return VStack(alignment: .leading, spacing: 5) {
            ForEach(detail.lines) { line in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if timed {
                        Text(line.leading).font(.system(size: 12.5)).foregroundStyle(Theme.textMuted).monospacedDigit()
                            .frame(width: Self.timeWidth(detail.lines), alignment: .leading)
                    }
                    Text(line.text).font(.system(size: 13.5))
                        .foregroundStyle(line.muted ? Theme.textMuted : Theme.textSecondary)
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                }
            }
            Button {
                if let action = detail.action, let url = URL(string: action.url) { NSWorkspace.shared.open(url) }
                else { NotchChrome.open(detail.source) }
            } label: {
                Text(detail.action?.label ?? "Open \(NotchChrome.appName(detail.source)) ↗").font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(Theme.textPrimary).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 2)
        }
        .padding(.leading, 50).padding(.bottom, 4)
        .textSelection(.enabled)
        .accessibilityElement(children: .contain)
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
        guard let brief = model.brief, today.isShowingCurrentDay,
              Calendar.current.isDateInToday(brief.receivedAt) else { return planned }
        return max(brief.receivedAt, planned ?? brief.receivedAt)
    }

    private func lineRow(_ line: DayTimeline.BriefLine, isOpen: Bool) -> some View {
        Button { toggle(line.tag) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(line.tag).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    .frame(width: 40, alignment: .leading)
                Text(line.text).font(.system(size: 15)).foregroundStyle(line.muted ? Theme.textMuted : Theme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                if !line.trailing.isEmpty {
                    Text(line.trailing).font(.system(size: 13)).foregroundStyle(Theme.textSecondary).lineLimit(1)
                }
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Theme.textMuted)
                    .rotationEffect(.degrees(isOpen ? 90 : 0))
                    .frame(width: 12)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(isOpen ? "Hide details" : "Show details")
        .accessibilityLabel(line.trailing.isEmpty ? "\(line.tag), \(line.text)" : "\(line.tag), \(line.text), \(line.trailing)")
        .accessibilityValue(isOpen ? "Expanded" : "Collapsed")
        .accessibilityHint(isOpen ? "Hides the details" : "Shows the details")
    }
}

/// The Brief's sentence, summary, rows and row details for one plan.
@MainActor
struct BriefContent {
    let lede: String
    /// The composed Daily Summary's short version; empty without one from today.
    let summary: [String]
    /// The rest of today's events under the summary; empty without a composed brief.
    let agenda: [BriefDetail.Line]
    let lines: [DayTimeline.BriefLine]
    /// What opens under each row, by its tag.
    let details: [String: BriefDetail]

    init(plan: TodayPlan, today: TodayModel, composed: ComposedBrief?) {
        let now = today.now.timeIntervalSince1970
        let bounds = today.dayInterval
        let dayStart = bounds.start.timeIntervalSince1970, dayEnd = bounds.end.timeIntervalSince1970
        let isToday = today.isShowingCurrentDay
        let zone = today.timezone
        let needs = (plan.workspace?.attention ?? []).filter { $0.source != "calendar" }
        let reference = isToday ? now : dayStart
        let calendarBlocks = plan.displayBlocks.filter { $0.kind == "calendar" || $0.kind == "manual" }
            .sorted { $0.start < $1.start }
        // Calendar entries only; learned away windows never make a day busy.
        let load = DayLoad.assess(events: calendarBlocks.map { DayLoad.Event(title: $0.title, start: $0.start, end: $0.end) },
                                  allDay: plan.all_day.map(\.title), from: reference, dayStart: dayStart,
                                  isToday: isToday, zone: zone, locale: today.clockLocale)
        let dueToday = needs.filter { ($0.due_ts ?? .infinity) >= reference && ($0.due_ts ?? .infinity) <= dayEnd }
            .sorted { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }.map(\.title)
        let overdue = isToday ? needs.filter { ($0.due_ts ?? .infinity) < now }.map(\.title) : []
        let next = calendarBlocks.first { $0.start > reference }.map { (title: $0.title, time: today.clock($0.start)) }
        let weekday = today.selectedDate.formatted(Date.FormatStyle(timeZone: zone).weekday(.wide))
        let calendarKnown = !(plan.sources["calendar"]?.ready == false && calendarBlocks.isEmpty && plan.all_day.isEmpty)
        lede = DayTimeline.lede(weekday: weekday, isToday: isToday, load: calendarKnown ? load.verdict : nil,
                                dueToday: dueToday, overdue: overdue, next: next)

        func coverage(_ source: String) -> DailySourceCoverage? {
            plan.workspace?.source_coverage.first { $0.source == source }
        }
        func need(_ item: DailyAttentionItem) -> BriefNeed {
            BriefNeed(title: item.title, sender: item.sender ?? "", gist: item.gist ?? "", person: item.person ?? false,
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
        let soon: DayTimeline.Soon
        if let upcoming {
            soon = upcoming.first.map { .item(title: $0.title, when: when($0), source: $0.isReminder ? "reminders" : $0.source) }
                ?? (weekRead ? .nothing : .unknown)
        } else if let need = needs.filter({ ($0.due_ts ?? 0) > dayEnd }).min(by: { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }) {
            // Before the week has been read, a dated need after today still counts.
            soon = .item(title: need.title, when: NotchChrome.dayClock(need.due_ts ?? 0, zone: zone, locale: today.clockLocale), source: need.source)
        } else {
            soon = .unknown
        }
        lines = DayTimeline.briefLines(
            calendar: calendarKnown ? load : nil, calendarUnsynced: !calendarKnown,
            mail: coverage("email").map { (ready: $0.ready, needing: mailNeeds) },
            texts: coverage("messages").map { (ready: $0.ready, needing: textNeeds) },
            textsNeedAccess: coverage("messages")?.blocked == "full_disk_access",
            soon: soon)

        let events = calendarBlocks.map { (title: $0.title, start: $0.start, end: $0.end) }
        let allDay = plan.all_day.map(\.title)
        let calendarDetail = BriefDetail.calendar(allDay: allDay, events: events, unsynced: !calendarKnown, clock: today.clock)
        details = [
            "CAL": calendarDetail,
            "MAIL": BriefDetail.mail(needing: mailNeeds, ready: coverage("email")?.ready == true),
            "TEXTS": BriefDetail.texts(needing: textNeeds, ready: coverage("messages")?.ready == true,
                                       needsAccess: coverage("messages")?.blocked == "full_disk_access",
                                       when: { $0 >= dayStart ? today.clock($0) : NotchChrome.dayClock($0, zone: zone) }),
            "SOON": BriefDetail.soon(upcoming.flatMap { $0.isEmpty && !weekRead ? nil : $0 }, when: when),
        ]

        summary = composed?.cutDown ?? []
        if composed != nil, calendarKnown {
            // The rest of today: all-day first, then what hasn't ended.
            let left = BriefDetail.calendar(allDay: allDay,
                                            events: events.filter { !isToday || ($0.end ?? $0.start + 3600) > now },
                                            unsynced: false, clock: today.clock).lines
            agenda = left.count > 4
                ? Array(left.prefix(3)) + [BriefDetail.Line(id: "more", leading: "", text: "+\(left.count - 3) more today", muted: true)]
                : left
        } else {
            agenda = []
        }
    }
}
