import SwiftUI
import AppKit

struct OverlayView: View {
    @StateObject var model: OverlayModel
    @ObservedObject var researchModel: ResearchModel
    // Shared with the Wisp window's planner, so both show the same day.
    @ObservedObject var todayModel: TodayModel
    // The app's one SettingsLoader, shared with the Settings window.
    @ObservedObject var settings: SettingsLoader
    // The app's one setup model, shared with the setup window.
    @ObservedObject var setup: SetupGuideModel
    var onClose: () -> Void
    // X button: dismiss from the notch (frees the resident model) but Wisp keeps running
    // in the menu bar — NOT a full quit. Full quit is still available via the
    // menu-bar icon's right-click menu.
    var onDismiss: () -> Void = {}
    @FocusState private var focused: Bool
    @State private var breathe = false
    // Replies whose Details the person toggled away from the default (open in
    // Debug Mode, closed otherwise).
    @State private var detailsToggled: Set<UUID> = []
    // Live display layout: notch on MacBooks (physical or virtual), floating
    // capsule on monitors. Refreshes when displays change.
    @ObservedObject private var display = DisplayGeometry.shared

    var body: some View {
        Group {
            if model.collapsed { notchBar } else { expandedPanel }
        }
        .onAppear { breathe = true }
        .onChange(of: model.collapsed) { _, isCollapsed in
            if !isCollapsed {
                if !model.tab.showsDay { focused = true }
                model.refreshRecentChats()
            }
        }
        // Switching to Ask or Research (including Ask Wisp from Brief or Day) puts the cursor in the field.
        .onChange(of: model.tab) { _, tab in
            if !tab.showsDay && !model.collapsed { focused = true }
        }
    }

    private var layout: SurfaceLayout { display.layout }
    private var isNotch: Bool { layout.style == .notch }

    private var expandedPanel: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if let knowYou = model.knowYou {
                // Getting to know you takes the tab's place until Done or a tab.
                KnowYouView(model: knowYou)
            } else if model.showingQuickSettings {
                QuickSettingsView(model: model, settings: settings).padding(.top, 12)
            } else {
            switch model.tab {
            case .ask:
                // First run: the open setup card replaces Ask until it is done or
                // set aside, then shrinks to a line. One view in one place for both.
                if model.setupCard != .open { chatSwitcherRow.padding(.top, 8) }
                transientNotice
                if model.setupCard != .hidden { NotchSetupView(model: model, setup: setup) }
                if model.setupCard != .open {
                    if model.reportIsUnattached {
                        ReportDraftCard(draft: model.report,
                                        title: model.report.snapshot?.key == "chat:none"
                                            ? "Report a problem" : "Report a problem with the last turn",
                                        save: { model.saveReport(model.report) })
                            .padding(.top, 8)
                    }
                    if model.serviceDown { serviceDownCard }
                    MemoryReviewNotice()
                    NudgeStack(model: model)
                    promptArea
                    content
                    statusRow.padding(.top, 10)
                }
            case .brief:
                NotchBriefView(model: model, today: todayModel).padding(.top, 12)
            case .day:
                NotchDayView(model: model, today: todayModel).padding(.top, 12)
            case .research:
                if model.showingResearch {
                    ResearchView(model: researchModel, compact: true,
                                 onBack: { model.returnToChat() })
                } else {
                    transientNotice
                    promptArea
                    HStack(alignment: .firstTextBaseline) {
                        Text("Wisp drafts an editable plan first, then reads the web and writes a cited report.")
                            .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        Spacer(minLength: 12)
                        Button("Research library") { WispRouter.shared.open(.research) }
                            .buttonStyle(.plain).font(.caption).foregroundStyle(Theme.textSecondary)
                            .fixedSize()
                    }
                    statusRow.padding(.top, 10)
                }
            }
            }
        }
        .background(shortcuts)
        // Fused with the notch, the panel hangs flush from the physical top,
        // so its center is behind the camera housing — push the content
        // below it. A floating card just uses ordinary padding.
        .padding(EdgeInsets(top: layout.contentTopInset,
                            leading: 22, bottom: 16, trailing: 22))
        // Re-fit the native window whenever the CONTENT height changes. The
        // window doesn't auto-size to SwiftUI — OverlayPanel sets its height
        // from host.fittingSize, and its bottom edge hard-CLIPS anything taller
        // (see the slideView constraints). Some height changes trigger a resize
        // explicitly (SSE events call model.onResize), but others don't — this
        // measures the real laid-out height and re-fits on any change, covering
        // every case (reasoning disclosure, suggestions) without a per-state
        // onChange for each. resizeToFit no-ops when the height already matches,
        // so there's no feedback loop.
        .background(GeometryReader { geo in
            Color.clear.onChange(of: geo.size.height) { model.onResize() }
        })
        // Must match the width OverlayPanel sizes its window to — both read
        // SurfaceLayout.expandedWidth, so they cannot disagree.
        .frame(width: layout.expandedWidth)
        .background(Theme.surface)
        .clipShape(layout.panelShape)
        .shadow(color: .black.opacity(0.55), radius: 20, y: 10)
        // Pointer enter/exit is handled by the panel's AppKit tracking area
        // (HoverView), not SwiftUI .onHover — see OverlayPanel/AppDelegate.
        .onExitCommand { model.requestCollapse() }
    }

    // Collapsed: a black bar that wraps the notch (physical or virtual) on a
    // MacBook; on a monitor, a small capsule under the menu bar. Hover to open.
    private var notchBar: some View {
        ZStack(alignment: .bottom) {
            Theme.surface
            if model.isProcessing || model.phase == .streaming {
                Circle().fill(.white.opacity(0.9))
                    .frame(width: 4, height: 4)
                    .padding(.bottom, 5)
                    .opacity(breathe ? 0.3 : 1)
                    .animation(.easeInOut(duration: 1).repeatForever(autoreverses: true), value: breathe)
            } else if model.setupCard != .later, todayModel.isShowingCurrentDay, todayCount > 0 {
                // What's due today or overdue, from the day's last load (it loads only
                // while Brief or Day is open, so the bar never triggers a load).
                HStack(spacing: 6) {
                    if !isNotch { orb.scaleEffect(0.7) }
                    Text("\(todayCount)").font(.system(size: 9.5, weight: .bold)).foregroundStyle(.black)
                        .padding(.horizontal, 5).frame(minWidth: 14, minHeight: 13)
                        .background(Capsule().fill(.white))
                }
                .padding(.bottom, isNotch ? 3 : 8)
                .accessibilityLabel(todayCount == 1 ? "1 thing is due today" : "\(todayCount) things are due today")
            } else if model.setupCard == .later {
                // Setup was set aside and Wisp still can't answer.
                HStack(spacing: 6) {
                    if !isNotch { orb.scaleEffect(0.7) }
                    Text("!").font(.system(size: 10, weight: .heavy)).foregroundStyle(Color.orange)
                }
                .padding(.bottom, isNotch ? 3 : 8)
                .accessibilityLabel("Wisp isn't set up yet")
            } else if !isNotch {
                HStack(spacing: 8) {
                    orb.scaleEffect(0.7)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Theme.textMuted)
                }
                .padding(.bottom, 8)
            }
        }
        // The visible bar and the window both come from the same layout, so
        // they cannot drift apart.
        .frame(width: layout.barSize.width, height: layout.barSize.height)
        .clipShape(layout.barShape)
        .contentShape(Rectangle())
        // Hover-to-expand is driven by the panel's tracking area; tap is a
        // fallback for click-to-open.
        .onTapGesture { model.requestExpand() }
        .help("Wisp — hover to open (⌥Space)")
    }

    private var header: some View {
        HStack(spacing: 10) {
            orb
            Text("Wisp").font(.system(size: 15, weight: .medium)).foregroundStyle(Theme.textPrimary)
            tabStrip.padding(.leading, 6)
            Spacer()
            windowControls
        }
    }

    @ViewBuilder private var transientNotice: some View {
        if let notice = model.transientNotice {
            Text(notice)
                .font(.system(size: 11))
                .foregroundStyle(Theme.bad)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 8)
        }
    }

    // The Ask field (Research's prompt on that tab), its command list and the rule under it.
    @ViewBuilder private var promptArea: some View {
        inputRow.padding(.top, 10)
        if !model.commandSuggestions.isEmpty {
            commandList.padding(.top, 8)
        }
        Divider().overlay(Theme.hairline).padding(.vertical, 10)
    }

    // Due today or overdue, once the day has loaded. Undated items wait in the Brief.
    private var todayCount: Int {
        guard todayModel.isShowingCurrentDay, let plan = todayModel.currentPlan else { return 0 }
        return DayTimeline.badgeCount(plan.dayNeeds, endOfDay: todayModel.dayInterval.end.timeIntervalSince1970)
    }

    private var tabStrip: some View {
        HStack(spacing: 2) {
            ForEach(NotchTab.strip, id: \.self) { tab in tabButton(tab) }
        }
        .padding(2)
        .background(Capsule().fill(Theme.chipFill))
        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        .fixedSize()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Wisp tabs")
    }

    private func tabButton(_ tab: NotchTab) -> some View {
        let selected = model.tab.stripTab == tab
        let count = tab == .day ? todayCount : 0
        let shortcut = tab.title + (tab.shortcutDigit.map { " (⌘" + String($0) + ")" } ?? "")
        return Button { model.leaveKnowYou(); model.tab = tab } label: {
            HStack(spacing: 5) {
                Text(tab.title).font(.system(size: 11.5, weight: .medium))
                if count > 0 { countBadge(count, selected: selected) }
            }
            .foregroundStyle(selected ? Color.black : Theme.textSecondary)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(selected ? Color.white : Color.clear))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help(shortcut)
        .accessibilityLabel(count > 0 ? "Day, \(count) due today" : tab.title)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    private func countBadge(_ count: Int, selected: Bool) -> some View {
        Text(String(count))
            .font(.system(size: 9.5, weight: .bold).monospacedDigit())
            .foregroundStyle(selected ? Color.white : Color.black)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(selected ? Color.black : Color.white.opacity(0.85)))
    }

    private var windowControls: some View {
        HStack(spacing: 6) {
            if model.tab == .ask && !model.turns.isEmpty {
                ctrl("square.and.pencil", "New chat (⌘N)", { model.newChat() })
            }
            ctrl("gearshape", model.showingQuickSettings ? "Close quick settings" : "Quick settings",
                 active: model.showingQuickSettings, { model.showingQuickSettings.toggle() })
            ctrl(model.pinned ? "pin.fill" : "pin", model.pinned ? "Unpin (⌘P)" : "Keep open (⌘P)",
                 active: model.pinned, { model.pinned.toggle() })
            // The window opens on what this tab shows. For Ask it reads the saved
            // history, which the service writes only at the end of a turn, so
            // that hand-off waits for the turn to finish.
            ctrl("macwindow", windowHelp, enabled: model.tab != .ask || model.conversationSettled,
                 { model.openTabInWindow() })
            ctrl("chevron.up", "Collapse", { model.requestCollapse() })
            ctrl("xmark", "Close (stays in menu bar; frees memory)", onDismiss)
        }
    }

    private var windowHelp: String {
        switch model.tab {
        case .ask: return "Open this chat in the Wisp window (⌘O)"
        case .brief, .day: return "Open the full planner in the Wisp window (⌘O)"
        case .research: return "Open the research library (⌘O)"
        }
    }

    /// Panel-wide shortcuts, as invisible buttons so they work wherever focus is.
    /// ⌘P rather than the ⌘. in the design: ⌘. means Cancel on macOS.
    private var shortcuts: some View {
        ZStack {
            Button("") { model.newChat(); model.tab = .ask }.keyboardShortcut("n", modifiers: .command)
            Button("") { model.run(.window) }.keyboardShortcut("o", modifiers: .command)
            Button("") { model.pinned.toggle() }.keyboardShortcut("p", modifiers: .command)
            ForEach(NotchTab.strip, id: \.self) { tab in
                if let digit = tab.shortcutDigit {
                    Button("") { model.tab = tab }.keyboardShortcut(KeyEquivalent(digit), modifiers: .command)
                }
            }
        }
        .opacity(0)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    private func ctrl(_ icon: String, _ help: String, enabled: Bool = true, active: Bool = false,
                      _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(active ? Color.black : Theme.textSecondary)
                .frame(width: 19, height: 19)
                .background(Circle().fill(active ? Color.white : Theme.chipFill))
                .overlay(Circle().stroke(active ? Color.clear : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.4)
        .help(help)
        .accessibilityLabel(help)
    }

    // Which conversation is open here, and a way back into recent saved ones
    // without opening the window.
    private var chatSwitcherRow: some View {
        HStack(spacing: 8) {
            Menu {
                if model.recentChats.isEmpty {
                    Text("No other saved chats")
                } else {
                    ForEach(model.recentChats) { chat in
                        Button(chat.title) { model.reopenChat(chat) }
                    }
                }
                Divider()
                Button("New chat") { model.newChat() }
                Button("All chats in the Wisp window") { WispRouter.shared.open(.chat(sessionId: nil)) }
            } label: {
                HStack(spacing: 4) {
                    Text(model.chatTitle).lineLimit(1).truncationMode(.tail)
                    Image(systemName: "chevron.down").font(.system(size: 8, weight: .bold))
                }
                .font(.system(size: 11.5, weight: .medium))
                .foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            // A plain button style keeps the capsule label; the borderless menu
            // style redraws its own and drops it.
            .menuStyle(.button)
            .buttonStyle(.plain)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(!model.conversationSettled)
            .help(model.conversationSettled ? "Switch to a recent chat" : "Available when Wisp finishes answering")
            Spacer()
            Text("Type / for commands").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
        }
    }

    // While the Ask field holds the start of a slash command: what it can become.
    // Return runs a whole command; clicking runs any of them.
    private var commandList: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(model.commandSuggestions, id: \.self) { command in
                Button {
                    model.input = ""
                    model.run(command)
                } label: {
                    HStack(spacing: 10) {
                        Text(command.title).font(.system(size: 12, design: .monospaced))
                            .foregroundStyle(Theme.textPrimary)
                            .frame(width: 84, alignment: .leading)
                        Text(command.summary).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        Spacer()
                        if NotchCommand.match(model.input) == command {
                            Text("↩").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                        }
                    }
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(4)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private var orb: some View {
        Circle()
            .fill(Theme.orb)
            .frame(width: 26, height: 26)
            .scaleEffect(breathe ? 1.1 : 0.94)
            .opacity(breathe ? 1.0 : 0.85)
            .shadow(color: .white.opacity(0.12), radius: 5)
            .animation(.easeInOut(duration: 4).repeatForever(autoreverses: true), value: breathe)
    }

    private var statusColor: Color {
        switch model.phase {
        case .error: return Theme.bad
        case .idle, .done: return Theme.good
        default: return Theme.warn
        }
    }

    private var canSend: Bool {
        !model.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var inputRow: some View {
        HStack(spacing: 10) {
            TextField(model.researchMode ? "What would you like to research?" : "Ask anything…",
                      text: $model.input, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 19))
                .foregroundStyle(Theme.textPrimary)
                .focused($focused)
                .onSubmit { model.submit() }
                // Enlarge the click target well beyond the glyph height and make
                // the whole padded area focus the field, so a click lands in text
                // mode immediately instead of needing to hit the thin text line.
                .padding(.vertical, 10)
                .padding(.horizontal, 12)
                .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
                .contentShape(RoundedRectangle(cornerRadius: 12))
                .onTapGesture { focused = true }
            if let name = model.attachedImageName {
                Label(name, systemImage: "paperclip")
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted).lineLimit(1).frame(maxWidth: 84)
            }
            if model.queuedPromptCount > 0 {
                Text("\(model.queuedPromptCount) queued")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Theme.textMuted)
            }
            Button(action: pickImage) {
                Image(systemName: "paperclip").font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
            }
            .buttonStyle(.plain).help("Attach an image").accessibilityLabel("Attach an image")
            Button(action: { model.submit() }) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 22))
                    .foregroundStyle(canSend ? Theme.accent : Theme.textMuted)
            }
            .buttonStyle(.plain).help(model.willQueuePrompt ? "Queue prompt" : "Send")
            .accessibilityLabel(model.willQueuePrompt ? "Queue prompt" : "Send").disabled(!canSend)
        }
    }

    @ViewBuilder private var content: some View {
        if model.phase == .idle && model.turns.isEmpty {
            suggestionsRow
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(model.turns) { turn in turnView(turn) }
                        liveTurn
                        Color.clear.frame(height: 1).id("bottom")
                    }
                }
                .frame(maxHeight: 360)
                // Reopening the panel recreates this ScrollView; jump straight to
                // the latest message so an existing conversation isn't scrolled to
                // the top. Deferred a runloop so it runs after layout.
                .onAppear {
                    DispatchQueue.main.async { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: model.turns.count) {
                    withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onChange(of: model.answer) { proxy.scrollTo("bottom", anchor: .bottom) }
                // A report opened from the menu bar or /report unfolds under an
                // earlier reply; bring it into view.
                .onChange(of: model.report.snapshot?.key) { _, key in
                    guard key != nil, !model.reportIsUnattached else { return }
                    DispatchQueue.main.async { withAnimation { proxy.scrollTo("report", anchor: .bottom) } }
                }
            }
        }
    }

    // A completed exchange: user prompt as a chip, assistant reply as markdown.
    @ViewBuilder private func turnView(_ turn: OverlayModel.Turn) -> some View {
        if turn.role == "user" {
            HStack {
                Spacer(minLength: 40)
                Text(turn.text)
                    .font(.system(size: 14))
                    .foregroundStyle(Theme.textPrimary)
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                // Errors get a visible marker regardless of Debug Mode — this
                // is a UX fix, not just a debug affordance: errors used to only
                // flash in the live status color and vanish the moment the next
                // message was sent, with no trace in the transcript at all.
                if turn.isError {
                    HStack(spacing: 5) {
                        Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 11))
                        Text("Error").font(.system(size: 11, weight: .semibold))
                    }
                    .foregroundStyle(Theme.bad)
                }
                if !turn.activity.isEmpty {
                    ForEach(turn.activity.indices, id: \.self) { i in
                        Text(turn.activity[i]).font(.system(size: 12, design: .monospaced))
                            .foregroundStyle(Theme.textMuted)
                    }
                }
                if turn.nodeResultID != nil {
                    // Remote titles and bodies are presentation only. Never parse
                    // links or create ScriptRunner controls from node content.
                    Text(verbatim: turn.text).textSelection(.enabled)
                } else {
                    MarkdownView(text: turn.text)
                }
                let hasDetails = !debugHeadline(for: turn).isEmpty || !(turn.routeReason ?? "").isEmpty
                let showDetails = hasDetails && (model.debugMode != detailsToggled.contains(turn.id))
                HStack(spacing: 14) {
                    if hasDetails {
                        Button {
                            if detailsToggled.contains(turn.id) { detailsToggled.remove(turn.id) }
                            else { detailsToggled.insert(turn.id) }
                        } label: {
                            Label("Details", systemImage: showDetails ? "chevron.down" : "chevron.right")
                        }
                        .help("Model, route, timing and tool calls for this reply")
                    }
                    Button("Report a problem") { model.reportProblem(for: turn) }
                }
                .buttonStyle(.plain).font(.caption).foregroundStyle(Theme.textMuted)
                if showDetails { debugLine(for: turn) }
                if model.report.snapshot?.key == OverlayModel.reportKey(for: turn) {
                    ReportDraftCard(draft: model.report, save: { model.saveReport(model.report) })
                        .padding(.top, 4)
                        .id("report")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // Compact per-reply debug summary — model, route classification/source,
    // timing, tok/s, tool-call count. Behind each reply's Details toggle, open
    // by default in Debug Mode. Full detail
    // (raw tool args/results, reasoning text) lives in the exported log, not
    // here — this is a quick-glance strip, not the whole record.
    @ViewBuilder private func debugLine(for turn: OverlayModel.Turn) -> some View {
        let headline = debugHeadline(for: turn)
        VStack(alignment: .leading, spacing: 2) {
            Text(headline)
                .font(.system(size: 10.5, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
            if let reason = turn.routeReason, !reason.isEmpty {
                Text(reason)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(Theme.textMuted.opacity(0.75))
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 6).stroke(Theme.hairline, lineWidth: 1))
    }

    private func debugHeadline(for turn: OverlayModel.Turn) -> String {
        var parts: [String] = []
        if let m = turn.model { parts.append(m) }
        if let r = turn.routeRole {
            parts.append(r + (turn.routeSource.map { " (\($0))" } ?? ""))
        }
        if let d = turn.durationSec { parts.append(String(format: "%.1fs", d)) }
        if let ttft = turn.timeToFirstTokenSec { parts.append(String(format: "%.1fs to first token", ttft)) }
        if let tps = turn.tokPerSec { parts.append("\(tps) tok/s") }
        if !turn.toolCalls.isEmpty {
            parts.append("\(turn.toolCalls.count) tool call\(turn.toolCalls.count == 1 ? "" : "s")")
        }
        if turn.heartbeatCount > 0 { parts.append("\(turn.heartbeatCount) heartbeats") }
        return parts.joined(separator: " · ")
    }

    // The turn currently in flight: status, tool activity, reasoning, streaming text.
    @ViewBuilder private var liveTurn: some View {
        // A brief being composed shows its own progress on Brief.
        if let progress = model.dailySyncProgress, !model.briefComposing {
            SourceSyncTip(label: model.dailySyncLabel, progress: progress,
                          sources: model.sourceSyncStatuses)
        }
        if model.isProcessing && (model.dailySyncProgress == nil || model.dailySyncProgress == 1) {
            ProcessingRow(label: model.processingLabel)
        }
        if !model.activity.isEmpty {
            ForEach(model.activity.indices, id: \.self) { i in
                Text(model.activity[i]).font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
            }
        }
        if let p = model.pending { confirmCard(p) }
        if let draft = model.messageDraft { messageDraftCard(draft) }
        if !model.reasoning.isEmpty {
            DisclosureGroup(isExpanded: $model.showReasoning) {
                // Long reasoning was previously hard-clipped at 120pt with no way
                // to see the rest — a ScrollView lets it actually be read in full
                // within the same bounded height. The backend delivers reasoning
                // as one complete block (not token-by-token), so this starts at
                // the top, not auto-scrolled to the end.
                ScrollView {
                    Text(model.reasoning).font(.system(size: 12))
                        .foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 160)
            } label: {
                Text("Show reasoning").font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
            }
            .tint(Theme.textSecondary)
        }
        if !model.answer.isEmpty {
            MarkdownView(text: model.answer)
                .frame(maxWidth: .infinity, alignment: .leading)
            // Once real text starts, isProcessing (and the "Thinking…" row) go
            // away by design — the growing text itself is the signal. But if
            // generation genuinely PAUSES mid-stream (backend hiccup), that
            // signal disappears: partial text just sits there with nothing
            // indicating whether it's still alive. This stays visible for the
            // whole streaming phase — a subtle pulse normally, and if the
            // backend's stall-watchdog heartbeat fires (main.py's 8s
            // next-chunk timeout on this path), it names the silence
            // explicitly instead of leaving it ambiguous.
            if model.phase == .streaming {
                HStack(spacing: 6) {
                    Circle().fill(Theme.textMuted).frame(width: 5, height: 5)
                        .opacity(breathe ? 0.25 : 0.9)
                        .animation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true), value: breathe)
                    if model.heartbeats > 0 {
                        Text("still generating… (\(model.heartbeats * 8)s)")
                            .font(.system(size: 10)).foregroundStyle(Theme.textMuted)
                    }
                }
            }
        }
    }

    private var suggestionsRow: some View {
        HStack(spacing: 10) {
            ForEach(model.suggestions.prefix(3), id: \.self) { s in
                Button(s) {
                    model.pick(s)
                    if s == "Research a topic" { focused = true }
                }
                    .buttonStyle(.plain)
                    .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
        }
    }

    private func confirmCard(_ p: OverlayModel.Pending) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.shield").foregroundStyle(Theme.textPrimary)
                Text(p.reason).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary)
            }
            // The actual code being installed, when this is a create_tool
            // confirm. Scrollable and height-capped so a long script can be
            // read without the card swallowing the whole panel.
            if !p.preview.isEmpty {
                ScrollView {
                    Text(p.preview)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(Theme.textPrimary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(maxHeight: 220)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke, lineWidth: 1))
            }
            // What an "Always allow" would actually cover. Shown because the
            // grant is scoped (a directory, a hostname) rather than blanket,
            // and a permission you can't see the boundaries of isn't one you
            // can meaningfully consent to.
            if p.grantable && !p.scopeHint.isEmpty {
                Text("Always allow applies to \(p.tool) on \(p.scopeHint)")
                    .font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
            }
            HStack(spacing: 8) {
                Spacer()
                Button("Deny") { model.resolve(false) }.buttonStyle(.plain)
                    .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 14).padding(.vertical, 6)
                    .background(Capsule().fill(Theme.chipFill))
                if p.grantable {
                    Button("Always allow") { model.resolve(true, scope: "always") }
                        .buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                }
                Button("Allow once") { model.resolve(true) }.buttonStyle(.plain)
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(.black)
                    .padding(.horizontal, 14).padding(.vertical, 6)
                    .background(Capsule().fill(.white))
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
    }

    private func messageDraftCard(_ draft: OverlayModel.MessageDraft) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: draft.sent ? "checkmark.circle.fill" : "bubble.left")
                    .foregroundStyle(Theme.textPrimary)
                Text(draft.sent ? "Message sent" : "Message draft")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                Spacer()
                Text("To: \(draft.to)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Theme.textSecondary)
            }

            Group {
                if draft.isEditing {
                    TextEditor(text: Binding(
                        get: { model.messageDraft?.text ?? "" },
                        set: { model.setDraftText($0) }))
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(Theme.textPrimary)
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: 90, maxHeight: 220)
                        .padding(4)
                } else {
                    ScrollView {
                        Text(draft.text)
                            .font(.system(size: 12, design: .monospaced))
                            .foregroundStyle(Theme.textPrimary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                    }
                    .frame(maxHeight: 220)
                }
            }
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.chipStroke, lineWidth: 1))

            if !draft.status.isEmpty {
                Text(draft.status)
                    .font(.system(size: 11))
                    .foregroundStyle((draft.sent || draft.isSending)
                                     ? Theme.textSecondary : Theme.bad)
            }
            if !draft.sent {
                HStack(spacing: 8) {
                    Spacer()
                    Button("Discard") { model.discardMessageDraft() }
                        .buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Capsule().fill(Theme.chipFill))
                        .disabled(draft.isSending)
                    Button(draft.isEditing ? "Done" : "Edit") { model.toggleDraftEditing() }
                        .buttonStyle(.plain)
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                        .disabled(draft.isSending)
                    Button(draft.isSending ? "Sending…" : "Send") { model.sendMessageDraft() }
                        .buttonStyle(.plain)
                        .font(.system(size: 12, weight: .medium)).foregroundStyle(.black)
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Capsule().fill(.white))
                        .disabled(draft.isSending || draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    // Model/routing detail lives here, folded into the one status line at the
    // bottom of Ask.
    private var statusRow: some View {
        HStack(spacing: 6) {
            Circle().fill(model.serviceDown ? Theme.bad : statusColor).frame(width: 5, height: 5)
            if model.serviceStarting {
                Text("Starting Wisp…").font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
            } else if model.serviceDown {
                Text("Wisp service isn't running").font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                Button("Retry") { model.onRetryService() }
                    .buttonStyle(.plain).font(.system(size: 11, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                    .accessibilityLabel("Retry starting the Wisp service")
            } else if !model.modelAbbrev.isEmpty {
                if !model.routeLabel.isEmpty {
                    Text(model.routeLabel).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                    Image(systemName: "arrow.right").font(.system(size: 8, weight: .bold))
                        .foregroundStyle(Theme.textMuted)
                }
                Text(model.modelAbbrev).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                if model.tokPerSec > 0 {
                    Text("· \(model.tokPerSec) tok/s").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                }
            } else {
                Text("Running locally").font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
            }
            Spacer()
            Text("⌥ Space").font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Capsule().fill(Theme.chipFill))
        }
        .lineLimit(1)
    }

    /// Shown at the top of Ask when Wisp's own service doesn't answer, instead of
    /// a blank panel that silently fails.
    private var serviceDownCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Wisp's service isn't running").font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
            Text("Nothing you ask will be answered until it starts. Retry, or send a report if it keeps happening.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Retry") { model.onRetryService() }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .semibold)).foregroundStyle(.black)
                    .padding(.horizontal, 14).padding(.vertical, 5)
                    .background(Capsule().fill(.white))
                Button("Report") { model.reportCurrentProblem() }
                    .buttonStyle(.plain).font(.system(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 12).padding(.vertical, 5)
                    .background(Capsule().fill(Theme.chipFill))
                    .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
        .padding(.top, 8)
    }

    private func pickImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        model.pickingFile = true
        defer { model.pickingFile = false }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let ext = url.pathExtension.lowercased()
        let mime = (ext == "jpg" || ext == "jpeg") ? "jpeg" : ext
        let name = url.lastPathComponent
        // Read off the main actor: a large photo must not freeze the panel.
        Task {
            let b64 = await Task.detached(priority: .userInitiated) {
                (try? Data(contentsOf: url))?.base64EncodedString()
            }.value
            guard let b64 else { return }
            model.attach(name: name, dataURL: "data:image/\(mime);base64,\(b64)")
        }
    }
}

// Animated "working" indicator shown between submit and first output.
struct ProcessingRow: View {
    let label: String
    @State private var animate = false

    var body: some View {
        HStack(spacing: 8) {
            HStack(spacing: 4) {
                ForEach(0..<3) { i in
                    Circle()
                        .fill(Theme.textSecondary)
                        .frame(width: 6, height: 6)
                        .opacity(animate ? 0.25 : 1.0)
                        .animation(.easeInOut(duration: 0.6).repeatForever().delay(Double(i) * 0.18), value: animate)
                }
            }
            Text(label).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
        }
        .onAppear { animate = true }
    }
}

// Keep sync peripheral to the conversation: one line, with per-source
// percentages and freshness caveats available on hover and to VoiceOver.
struct SourceSyncTip: View {
    let label: String
    let progress: Double
    let sources: [WispClient.SourceSyncStatus]

    private var details: String {
        sources.map { source in
            "\(source.label): \(source.percentageLabel). "
                + (source.warning.isEmpty ? source.detail : source.warning)
        }.joined(separator: "\n")
    }

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: progress < 1 ? "arrow.triangle.2.circlepath" : "info.circle")
                .accessibilityHidden(true)
            Text(label).monospacedDigit()
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .font(.system(size: 11))
        .foregroundStyle(Theme.textMuted)
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(details.isEmpty ? label : details)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(details)
    }
}
