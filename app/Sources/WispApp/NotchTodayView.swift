import SwiftUI
import AppKit

/// The notch's Today tab: the brief, what needs you, and what's next, with the
/// everyday actions inline. The full planner (task editor, pins, working hours)
/// and the daily email stay in the Wisp window, because the non-activating notch
/// panel can't host their sheets.
///
/// The plan loads while this tab is on screen, never at launch or on hover of
/// the bar. The brief is composed only on schedule or with Refresh.
struct NotchTodayView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var today: TodayModel
    @State private var minutes = 45
    @State private var showingFullBrief = false

    private var plan: TodayPlan? {
        guard let plan = today.plan, plan.day == today.day,
              plan.timezone == today.timezone.identifier else { return nil }
        return plan
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            heading
            if let notice = model.transientNotice { noticeText(notice) }
            if !today.error.isEmpty { noticeText(today.error) }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    briefSection
                    if let plan {
                        needsYouSection(plan)
                        upNextSection(plan)
                    } else if today.error.isEmpty {
                        ProcessingRow(label: "Loading your day…")
                    }
                    addTaskRow
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 420)
            footer
        }
        .environment(\.timeZone, today.timezone)
        .task {
            // While the tab is on screen: follow the clock and keep the plan current.
            while !Task.isCancelled {
                today.reconcileClock()
                await today.refresh()
                model.onResize()
                do { try await Task.sleep(for: .seconds(30)) } catch { break }
            }
        }
    }

    private var heading: some View {
        HStack(spacing: 8) {
            Text(Date.now, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            Spacer()
            chipButton(model.briefComposing ? "Refreshing…" : "Refresh", icon: "arrow.clockwise",
                       help: "Compose a new brief and reload your day") {
                model.runDailySummary()
                Task { await today.refresh() }
            }
            .disabled(model.briefComposing || today.busy)
        }
    }

    // MARK: Brief

    @ViewBuilder private var briefSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            sectionTitle("Your brief", detail: model.brief.map { "Updated " + $0.receivedAt.formatted(date: .omitted, time: .shortened) })
            if model.briefComposing {
                if let progress = model.dailySyncProgress {
                    SourceSyncTip(label: model.dailySyncLabel, progress: progress, sources: model.sourceSyncStatuses)
                }
                ProcessingRow(label: "Building your daily summary…")
            } else if let brief = model.brief {
                if showingFullBrief {
                    MarkdownView(text: brief.text).frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    Text(OverlayModel.notificationBody(from: brief.text, maxLines: 4))
                        .font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                HStack(spacing: 14) {
                    Button(showingFullBrief ? "Show less" : "Show all") { showingFullBrief.toggle(); model.onResize() }
                    if !brief.sessionId.isEmpty {
                        Button("Ask about this") { model.askAboutBrief() }
                            .disabled(!model.conversationSettled)
                            .help("Continue from this brief in Ask")
                    }
                }
                .buttonStyle(.plain).font(.caption).foregroundStyle(Theme.textMuted)
            } else {
                Text("Your scheduled brief lands here. Refresh composes one now.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
        }
    }

    // MARK: Needs you

    private func needsYouSection(_ plan: TodayPlan) -> some View {
        let items = plan.workspace?.attention ?? []
        let shown = Array(items.prefix(3))
        let more = items.count - shown.count + (plan.workspace?.attention_omitted ?? 0)
        return VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Needs you" + (items.isEmpty ? "" : " · \(items.count)"), detail: nil)
            if plan.workspace == nil {
                Text(plan.workspace_error ?? "Source evidence is unavailable. Refresh to compile your sources.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            } else if items.isEmpty {
                Text("Nothing needs you in the available sources.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
            ForEach(shown) { item in attentionRow(item) }
            if more > 0 {
                Button("\(more) more in the full planner") { WispRouter.shared.open(.today(focus: .attention)) }
                    .buttonStyle(.plain).font(.caption).foregroundStyle(Theme.textMuted)
            }
        }
    }

    private func attentionRow(_ item: DailyAttentionItem) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                Text(attentionDetail(item)).font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            Spacer(minLength: 8)
            // Calendar events and Wisp's own tasks are already on the plan.
            if item.source != "calendar" && item.source != "wisp" {
                rowButton("Add as task") {
                    Task {
                        _ = await today.add(title: item.needs_review && item.ownership != "user" ? "Review: " + item.title : item.title,
                                            kind: item.task_kind ?? "task", minutes: item.suggested_minutes,
                                            priority: item.priority,
                                            due: item.due_ts.flatMap { $0 > Date().timeIntervalSince1970 ? Date(timeIntervalSince1970: $0) : nil })
                    }
                }
                .disabled(today.busy)
            }
            rowButton("Ask Wisp") { model.askAbout(item.title, reference: item.reference) }
        }
    }

    private func attentionDetail(_ item: DailyAttentionItem) -> String {
        var parts = [item.source.capitalized]
        if let due = item.due_ts { parts.append("due " + dueLabel(due)) }
        if item.needs_review { parts.append("review") }
        return parts.joined(separator: " · ")
    }

    /// "11:59 PM" today, "Mon 11:59 PM" on another day.
    private func dueLabel(_ timestamp: Double) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = today.timezone
        let date = Date(timeIntervalSince1970: timestamp)
        guard !calendar.isDate(date, inSameDayAs: Date()) else { return today.clock(timestamp) }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = today.timezone
        formatter.dateFormat = "EEE h:mm a"
        return formatter.string(from: date)
    }

    // MARK: Up next

    private func upNextSection(_ plan: TodayPlan) -> some View {
        let blocks = plan.displayBlocks
        let next = today.isShowingCurrentDay
            ? NotchUpNext.upcoming(blocks, start: \.start, end: \.end, now: Date().timeIntervalSince1970)
            : Array(blocks.prefix(3))
        return VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Up next", detail: blocks.isEmpty ? nil : "\(next.count) of \(blocks.count) blocks")
            if !plan.displayWarnings.isEmpty {
                Text(plan.displayWarnings.joined(separator: " ")).font(.system(size: 11)).foregroundStyle(Theme.warn)
            }
            if next.isEmpty {
                Text(blocks.isEmpty ? "No time blocks yet." : "Nothing else is planned today.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
            }
            ForEach(next) { block in blockRow(block, plan: plan) }
            if let first = plan.displayUnscheduled.first {
                Text("Still to fit: \(first.title) · \(first.reason)")
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(2)
            }
        }
    }

    private func blockRow(_ block: TodayBlock, plan: TodayPlan) -> some View {
        let task = block.task_id.flatMap { id in plan.tasks.first { $0.id == id } }
        return HStack(alignment: .top, spacing: 10) {
            Text(today.clock(block.start))
                .font(.system(size: 11.5).monospacedDigit()).foregroundStyle(Theme.textSecondary)
                .frame(width: 62, alignment: .leading)
            VStack(alignment: .leading, spacing: 2) {
                Text(block.title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                Text(task.map { "\($0.pinned_start == nil ? "" : "Pinned · ")\($0.duration_minutes) min" } ?? block.evidenceLabel)
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1)
                ForEach(Array(block.warnings.enumerated()), id: \.offset) { _, warning in
                    Text(warning).font(.system(size: 11)).foregroundStyle(Theme.warn)
                }
            }
            Spacer(minLength: 8)
            if let task {
                rowButton("−15") { Task { await today.edit(task, values: ["duration_minutes": max(1, task.duration_minutes - 15)]) } }
                    .disabled(today.busy)
                rowButton("+15") { Task { await today.edit(task, values: ["duration_minutes": min(1440, task.duration_minutes + 15)]) } }
                    .disabled(today.busy)
            } else if let pattern = block.pattern_id {
                rowButton("Use this time") {
                    Task {
                        _ = await today.mutate("/assistant/today/workspace/availability", method: "POST",
                                               body: ["day": today.day, "timezone": today.timezone.identifier,
                                                      "window": pattern, "ignored": true])
                    }
                }
                .help(block.inferenceIgnoreHelp).disabled(today.busy)
            }
        }
    }

    // MARK: Add a task

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
                .padding(.horizontal, 10).padding(.vertical, 7)
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
            rowButton("Add to day", action: addTask).disabled(!canAdd)
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
            }
        }
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 8) {
            if let plan {
                chipButton("Replan", icon: "arrow.triangle.2.circlepath", help: "Fit your day again within your working hours") {
                    Task { await today.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute) }
                }
                .disabled(today.busy)
                chipButton("Running 15 min late", icon: "clock", help: "Push everything still ahead back 15 minutes") {
                    Task {
                        await today.replan(start: plan.preferences.start_minute, end: plan.preferences.end_minute,
                                           delayMinutes: 15)
                    }
                }
                .disabled(today.busy || !today.isShowingCurrentDay)
            }
            Spacer()
            chipButton("Full planner", icon: "arrow.up.left.and.arrow.down.right",
                       help: "Tasks, pins, working hours and the daily email, in the Wisp window") {
                WispRouter.shared.open(.today(focus: .plan))
            }
        }
    }

    // MARK: Pieces

    private func sectionTitle(_ title: String, detail: String?) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary)
                .textCase(.uppercase)
            if let detail { Text(detail).font(.system(size: 11)).foregroundStyle(Theme.textMuted) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }

    private func noticeText(_ text: String) -> some View {
        Text(text).font(.system(size: 11)).foregroundStyle(Theme.bad)
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }

    private func rowButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    private func chipButton(_ title: String, icon: String, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: icon).font(.system(size: 10))
                Text(title).font(.system(size: 11, weight: .medium))
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(help)
        .fixedSize()
    }
}
