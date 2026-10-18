import SwiftUI
import AppKit

/// The notch's Brief tab: what's going on, read only. One sentence about the
/// day, then at most four lines (calendar, mail, texts, what's coming), each
/// opening its source. Doing things belongs to Ask and Day.
///
/// The sentence and lines come from the day's plan (`NotchDay.swift`), never a
/// model call. Refresh composes the scheduled brief now, which "Ask about this"
/// continues from.
struct NotchBriefView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel

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
                ProcessingRow(label: "Building your daily summary…")
            }
            if let plan {
                let brief = BriefContent(plan: plan, today: today)
                Text(brief.lede)
                    .font(.system(size: 16.5)).foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                if !brief.lines.isEmpty {
                    VStack(alignment: .leading, spacing: 9) {
                        ForEach(brief.lines) { line in lineRow(line) }
                    }
                }
                HStack(spacing: 8) {
                    NotchChrome.chip("Ask about this", icon: "bubble.left", help: "Continue from this in Ask") {
                        if let brief = model.brief, !brief.sessionId.isEmpty { model.askAboutBrief() }
                        else { model.askAbout(BriefContent(plan: plan, today: today).lede, reference: "") }
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

    private func lineRow(_ line: DayTimeline.BriefLine) -> some View {
        Button { NotchChrome.open(line.source) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(line.tag).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.textMuted)
                    .frame(width: 40, alignment: .leading)
                Text(line.text).font(.system(size: 15)).foregroundStyle(line.muted ? Theme.textMuted : Theme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(line.trailing).font(.system(size: 13)).foregroundStyle(Theme.textSecondary).lineLimit(1)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Open \(NotchChrome.appName(line.source))")
        .accessibilityLabel("\(line.text), \(line.trailing == "›" ? "" : line.trailing). Opens \(NotchChrome.appName(line.source))")
    }
}

/// The Brief's sentence and lines for one plan.
@MainActor
struct BriefContent {
    let lede: String
    let lines: [DayTimeline.BriefLine]

    init(plan: TodayPlan, today: TodayModel) {
        let now = today.now.timeIntervalSince1970
        let bounds = today.dayInterval
        let dayStart = bounds.start.timeIntervalSince1970, dayEnd = bounds.end.timeIntervalSince1970
        let isToday = today.isShowingCurrentDay
        let needs = (plan.workspace?.attention ?? []).filter { $0.source != "calendar" }
        let reference = isToday ? now : dayStart
        let calendarBlocks = plan.displayBlocks.filter { $0.kind == "calendar" || $0.kind == "manual" }
            .sorted { $0.start < $1.start }
        // Calendar entries only; learned away windows never make a day busy.
        let load = DayLoad.assess(events: calendarBlocks.map { DayLoad.Event(title: $0.title, start: $0.start, end: $0.end) },
                                  allDay: plan.all_day.map(\.title), from: reference, dayStart: dayStart,
                                  isToday: isToday, zone: today.timezone)
        let dueToday = needs.filter { ($0.due_ts ?? .infinity) >= reference && ($0.due_ts ?? .infinity) <= dayEnd }
            .sorted { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }.map(\.title)
        let overdue = isToday ? needs.filter { ($0.due_ts ?? .infinity) < now }.map(\.title) : []
        let next = calendarBlocks.first { $0.start > reference }.map { (title: $0.title, time: today.clock($0.start)) }
        let weekday = today.selectedDate.formatted(Date.FormatStyle(timeZone: today.timezone).weekday(.wide))
        let calendarKnown = !(plan.sources["calendar"]?.ready == false && calendarBlocks.isEmpty && plan.all_day.isEmpty)
        lede = DayTimeline.lede(weekday: weekday, isToday: isToday, load: calendarKnown ? load.verdict : nil,
                                dueToday: dueToday, overdue: overdue, next: next)

        let coverage = plan.workspace?.source_coverage.first { $0.source == "email" }
        let soon = needs.filter { ($0.due_ts ?? 0) > dayEnd }.min { ($0.due_ts ?? 0) < ($1.due_ts ?? 0) }
        lines = DayTimeline.briefLines(
            calendar: calendarKnown ? load : nil, calendarUnsynced: !calendarKnown,
            mail: coverage.map { (ready: $0.ready, needing: needs.filter { $0.source == "email" }.map(\.title)) },
            texts: needs.filter { $0.source == "messages" }.map(\.title),
            soon: soon.map { (title: $0.title, when: NotchChrome.dayClock($0.due_ts ?? 0, zone: today.timezone),
                              source: $0.source) })
    }
}
