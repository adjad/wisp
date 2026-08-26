import SwiftUI
import AppKit

struct OverlayView: View {
    @StateObject var model: OverlayModel
    var onClose: () -> Void
    // X button: dismiss from the notch (frees the resident model) but Wisp keeps running
    // in the menu bar — NOT a full quit. Full quit is still available via the
    // menu-bar icon's right-click menu.
    var onDismiss: () -> Void = {}
    @FocusState private var focused: Bool
    @State private var breathe = false

    var body: some View {
        Group {
            if model.collapsed { notchBar } else { expandedPanel }
        }
        .onAppear { breathe = true }
        .onChange(of: model.collapsed) { _, isCollapsed in
            if !isCollapsed { focused = true }
        }
    }

    private var hasNotch: Bool { model.notchInset > 0 }

    private var expandedPanel: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            inputRow.padding(.top, 10)
            Divider().overlay(Theme.hairline).padding(.vertical, 10)
            content
            statusRow.padding(.top, 10)
        }
        // Fused with the notch, the panel hangs flush from the physical top,
        // so its center is behind the camera housing — push the content
        // below it.
        .padding(EdgeInsets(top: hasNotch ? model.notchInset + 8 : 16,
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
        // Must match OverlayPanel.frame(compact:false)'s `w` exactly — the
        // native window frame is sized from THAT constant, not from this view.
        .frame(width: 640)
        .background(Theme.surface)
        .clipShape(Theme.notchCorners)
        .shadow(color: .black.opacity(0.55), radius: 20, y: 10)
        // Pointer enter/exit is handled by the panel's AppKit tracking area
        // (HoverView), not SwiftUI .onHover — see OverlayPanel/AppDelegate.
        .onExitCommand { model.requestCollapse() }
    }

    // Collapsed: a black bar that wraps the physical notch (hover to expand).
    // On non-notched displays it falls back to a small visible pill instead.
    private var notchBar: some View {
        ZStack(alignment: .bottom) {
            Theme.surface
            if model.isProcessing || model.phase == .streaming {
                Circle().fill(.white.opacity(0.9))
                    .frame(width: 4, height: 4)
                    .padding(.bottom, 5)
                    .opacity(breathe ? 0.3 : 1)
                    .animation(.easeInOut(duration: 1).repeatForever(autoreverses: true), value: breathe)
            } else if !hasNotch {
                HStack(spacing: 8) {
                    orb.scaleEffect(0.7)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Theme.textMuted)
                }
                .padding(.bottom, 8)
            }
        }
        // References OverlayPanel.barScale directly (rather than a duplicated
        // literal) so the visible bar and the actual window (hover hitbox)
        // can never drift out of sync again — they did, silently, the last
        // time this ratio was tuned only here and not there.
        .frame(width: hasNotch ? (model.notchWidth + 28) * OverlayPanel.barScale : 160,
               height: hasNotch ? (model.notchInset + 8) * OverlayPanel.barScale : 40)
        .clipShape(UnevenRoundedRectangle(topLeadingRadius: 0, bottomLeadingRadius: 14,
                                          bottomTrailingRadius: 14, topTrailingRadius: 0))
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
            Spacer()
            researchControl
            superModelControl
            dailySummaryControl
            windowControls
        }
    }

    private var researchControl: some View {
        Button(action: { model.researchMode.toggle() }) {
            HStack(spacing: 4) {
                Image(systemName: "binoculars.fill").font(.system(size: 11))
                Text("Research").font(.system(size: 11, weight: .medium))
            }
            .foregroundStyle(model.researchMode ? .black : Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(model.researchMode ? Color.white : Theme.chipFill))
            .overlay(Capsule().stroke(model.researchMode ? Color.clear : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(model.researchMode
              ? "Research mode is on — the next prompt opens an editable research plan"
              : "Create a multi-source, cited research report")
    }

    // Always visible (not just while active) — a persistent toggle right in
    // the main panel, same tier as the thinking-level/daily-summary chips.
    // Filled orange when engaged (since it silently forces every message onto
    // one model — needs a hard-to-miss "this is still on" state), outline
    // chip like the others when off. Tap either way goes through the Touch
    // ID + VRAM raise/restore flow — see OverlayModel.enableSuperModel /
    // disableSuperModel. The right-click menu item is a second entry point to
    // the same toggle, kept for parity.
    private var superModelControl: some View {
        Button(action: { model.toggleSuperModel() }) {
            HStack(spacing: 4) {
                Image(systemName: "bolt.fill").font(.system(size: 11))
                Text("Super").font(.system(size: 11, weight: .medium))
            }
            .foregroundStyle(model.superModelActive ? .black : Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(model.superModelActive ? Color.orange : Theme.chipFill))
            .overlay(
                Capsule().stroke(model.superModelActive ? Color.clear : Theme.chipStroke, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .disabled(model.superModelBusy)
        .help(model.superModelActive
              ? "Super Model is forcing every request onto \(model.superModelName) — click to turn off"
              : "Super Model — force every request onto \(model.superModelName.isEmpty ? "one model" : model.superModelName), quitting other apps to make room")
    }

    // "Daily Summary" button + an AM/PM toggle for when the scheduled brief
    // fires (8am vs 8pm). Tapping the button builds the combined calendar+email
    // brief on demand and drops it into the transcript.
    private var dailySummaryControl: some View {
        HStack(spacing: 6) {
            Button(action: { model.runDailySummary() }) {
                HStack(spacing: 4) {
                    Image(systemName: "sun.max").font(.system(size: 11))
                    Text("Daily Summary").font(.system(size: 11, weight: .medium))
                }
                .foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            }
            .buttonStyle(.plain).help("Summarize today's calendar + email now")

            // AM / PM segmented toggle for the scheduled brief time.
            HStack(spacing: 0) {
                ForEach(["AM", "PM"], id: \.self) { p in
                    Button(action: { model.setSummaryPeriod(p) }) {
                        Text(p).font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(model.summaryPeriod == p ? Color.black : Theme.textMuted)
                            .frame(width: 22, height: 18)
                            .background(model.summaryPeriod == p ? Theme.accent : Color.clear)
                    }
                    .buttonStyle(.plain)
                }
            }
            .background(Capsule().fill(Theme.chipFill))
            .clipShape(Capsule())
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .help("When the daily brief is delivered: 8 AM or 8 PM")
        }
    }

    private var windowControls: some View {
        HStack(spacing: 6) {
            if !model.turns.isEmpty {
                ctrl("square.and.pencil", "New chat", { model.newChat() })
            }
            ctrl("chevron.up", "Collapse", { model.requestCollapse() })
            ctrl("xmark", "Close (stays in menu bar; frees memory)", onDismiss)
        }
    }

    private func ctrl(_ icon: String, _ help: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(Theme.textSecondary)
                .frame(width: 19, height: 19)
                .background(Circle().fill(Theme.chipFill))
                .overlay(Circle().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(help)
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
        !model.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !model.isProcessing
    }

    private var inputRow: some View {
        HStack(spacing: 10) {
            TextField("Ask anything…", text: $model.input, axis: .vertical)
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
            Button(action: pickImage) {
                Image(systemName: "paperclip").font(.system(size: 16)).foregroundStyle(Theme.textSecondary)
            }
            .buttonStyle(.plain).help("Attach a file")
            Button(action: { model.submit() }) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 22))
                    .foregroundStyle(canSend ? Theme.accent : Theme.textMuted)
            }
            .buttonStyle(.plain).help("Send").disabled(!canSend)
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
                if model.debugMode { debugLine(for: turn) }
                MarkdownView(text: turn.text)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // Compact per-reply debug summary — model, route classification/source,
    // timing, tok/s, tool-call count. Shown only in Debug Mode. Full detail
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
        if model.isProcessing { ProcessingRow(label: model.processingLabel) }
        if !model.activity.isEmpty {
            ForEach(model.activity.indices, id: \.self) { i in
                Text(model.activity[i]).font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.textMuted)
            }
        }
        if let p = model.pending { confirmCard(p) }
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
                Button(s) { model.pick(s) }
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

    // Model/routing detail used to be its own wrapping pill up in the header
    // (the main source of clutter) — it lives here now, folded into the one
    // status line that was already at the bottom, so nothing is lost, it's
    // just no longer competing for space with Daily Summary / AM-PM / window
    // controls up top.
    private var statusRow: some View {
        HStack(spacing: 6) {
            Circle().fill(statusColor).frame(width: 5, height: 5)
            if !model.modelAbbrev.isEmpty {
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

    private func pickImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        model.pickingFile = true
        defer { model.pickingFile = false }
        if panel.runModal() == .OK, let url = panel.url, let data = try? Data(contentsOf: url) {
            let b64 = data.base64EncodedString()
            let ext = url.pathExtension.lowercased()
            model.attach(name: url.lastPathComponent, dataURL: "data:image/\(ext);base64,\(b64)")
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
