import SwiftUI
import AppKit

/// The notch's Ideate tab: type an idea, a coding agent builds a small project in its
/// own folder on a local model, the tab streams what it does in a few short rows, and
/// asks inline before anything risky.
///
/// Everything stays inline (the panel is non-activating: no sheets, alerts or popovers).
/// Calm and monochrome: colour only for status. Amber marks the one thing that needs
/// you (an approval); soft red marks a failure; "Thinking…" is neutral.
struct NotchIdeateView: View {
    @ObservedObject var ideate: IdeateModel
    var wide = false
    /// The tab scrolls inside this height when it is given.
    var cap: CGFloat?
    /// Told when what shows changes, so the panel can refit.
    var onResize: () -> Void = {}

    @State private var prompt = ""
    @State private var followUp = ""
    @FocusState private var promptFocused: Bool
    @FocusState private var followUpFocused: Bool

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    init(ideate: IdeateModel, wide: Bool = false, cap: CGFloat? = nil, onResize: @escaping () -> Void = {}) {
        self.ideate = ideate
        self.wide = wide
        self.cap = cap
        self.onResize = onResize
    }

    private var kind: IdeateKind { ideate.current?.kind ?? ideate.kind }

    /// Which branches show. Never the row text itself: that changes every second.
    private struct Branches: Equatable {
        var mode: Int
        var rows: Int
        var earlier: Bool
        var approvals: Int
        var files: Int
        var message: Bool
    }

    private var branches: Branches {
        Branches(mode: mode.rawValue, rows: ideate.timeline.rows.count, earlier: ideate.timeline.earlier > 0,
                 approvals: ideate.approvals.count, files: ideate.files.count, message: ideate.message != nil)
    }

    private enum Mode: Int { case unavailable, home, running, done, failed }

    private var mode: Mode {
        if ideate.current != nil {
            switch ideate.status {
            case .starting, .running, .waiting: return .running
            case .done: return .done
            case .failed, .cancelled: return .failed
            case .idle: break
            }
        }
        if ideate.availabilityKnown && !ideate.availability.available { return .unavailable }
        return .home
    }

    var body: some View {
        CappedScroll(cap: cap, reveals: true) {
            VStack(alignment: .leading, spacing: 12) {
                switch mode {
                case .unavailable: unavailable.transition(.opacity)
                case .home: home.transition(.opacity)
                case .running: running.transition(.opacity)
                case .done: done.transition(.opacity)
                case .failed: failed.transition(.opacity)
                }
                if let message = ideate.message {
                    Text(message).font(.wisp(.footnote)).foregroundStyle(Theme.warn)
                        .fixedSize(horizontal: false, vertical: true)
                        .transition(.opacity)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 8)
        }
        .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: branches)
        .onChange(of: branches) { _, _ in onResize() }
        .onAppear { ideate.appeared() }
        .onDisappear { ideate.disappeared() }
    }

    // MARK: Empty

    private var home: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(kind.subtitle).font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            promptField
            HStack(spacing: 8) {
                ForEach(kind.starters, id: \.self) { starter in
                    Button { prompt = starter; promptFocused = true } label: {
                        Text(starter).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                            .lineLimit(1)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Capsule().fill(Theme.chipFill))
                            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Use the idea: \(starter)")
                }
            }
            if ideate.swapsModel { modelNote }
            if !ideate.projects.isEmpty { recent }
            Text("Runs on Inkling 2 Mini on this Mac · builds in its own folder")
                .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
        }
    }

    private var promptField: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(kind.placeholder, text: $prompt, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(kind.pastesLog ? 4...8 : 1...3)
                .font(kind.pastesLog ? .wisp(size: 12, design: .monospaced) : .wisp(size: 13))
                .foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .stroke(promptFocused ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
                .focused($promptFocused)
                .onSubmit { if !kind.pastesLog { begin() } }
                .accessibilityLabel("Your idea")
            if kind.pastesLog || !prompt.isEmpty {
                pill("Start", icon: "arrow.up", filled: true) { begin() }
                    .disabled(prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .transition(.opacity)
            }
        }
    }

    private func begin() {
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        ideate.start(prompt: text, kind: kind)
        prompt = ""
    }

    private var modelNote: some View {
        Text("Starting loads Inkling 2 Mini (about 13 GB); Ask's model steps aside until Ideate is done.")
            .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var recent: some View {
        VStack(alignment: .leading, spacing: 4) {
            label("Recent")
            ForEach(ideate.projects.prefix(3)) { project in
                Button { ideate.open(project) } label: {
                    HStack(alignment: .firstTextBaseline) {
                        Text(project.name).font(.wisp(.body)).foregroundStyle(Theme.textPrimary).lineLimit(1)
                        Spacer(minLength: 12)
                        Text(project.status.text).font(.wisp(.caption))
                            .foregroundStyle(project.status == .failed ? Theme.bad : Theme.textMuted)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: Running

    private var running: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            timelineRows
            ForEach(ideate.approvals) { approval in
                approvalCard(approval).transition(.opacity)
            }
            if ideate.swapsModel || ideate.status == .starting, !ideate.availability.modelLoaded, ideate.availabilityKnown {
                modelNote
            }
            pill("Stop", icon: "stop.circle", filled: false) { ideate.cancel() }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(ideate.current?.name ?? "New idea").font(.wisp(.headline, weight: .semibold))
                .foregroundStyle(Theme.textPrimary).lineLimit(1)
            Text(ideate.status.text).font(.wisp(.footnote))
                .foregroundStyle(ideate.status == .waiting ? Theme.warn : Theme.textSecondary)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    private var timelineRows: some View {
        VStack(alignment: .leading, spacing: 5) {
            if ideate.timeline.earlier > 0 {
                Text("\(ideate.timeline.earlier) earlier steps").font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
            ForEach(ideate.timeline.rows) { row in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: icon(for: row)).font(.wisp(size: 10))
                        .foregroundStyle(tint(for: row)).frame(width: 14)
                        .accessibilityHidden(true)
                    Text(IdeateTimeline.display(row)).font(.wisp(.body))
                        .foregroundStyle(tint(for: row))
                        .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .transition(.opacity)
            }
            if ideate.timeline.rows.isEmpty {
                Text("Getting started…").font(.wisp(.body)).foregroundStyle(Theme.textMuted)
            }
        }
    }

    private func icon(for row: IdeateRow) -> String {
        switch row.style {
        case .thinking: return "ellipsis"
        case .edit, .file: return "doc"
        case .run: return "terminal"
        case .read: return "eye"
        case .approval: return "hand.raised"
        case .done: return "checkmark"
        case .error: return "exclamationmark.circle"
        case .text: return "text.alignleft"
        }
    }

    private func tint(for row: IdeateRow) -> Color {
        switch row.style {
        case .thinking: return Theme.textMuted
        case .approval: return Theme.warn
        case .error: return Theme.bad
        default: return row.state == "failed" ? Theme.bad : Theme.textSecondary
        }
    }

    private func approvalCard(_ approval: IdeateApproval) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(approval.title).font(.wisp(.callout, weight: .medium)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Text("It waits for you.").font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
            HStack(spacing: 8) {
                pill("Allow once", icon: "checkmark", filled: true) { ideate.answer(approvalId: approval.id, allow: true) }
                pill("Don't allow", icon: "xmark", filled: false) { ideate.answer(approvalId: approval.id, allow: false) }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.warn.opacity(0.7), lineWidth: 1))
        .accessibilityElement(children: .contain)
    }

    // MARK: Done

    private var done: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(ideate.current?.name ?? "Done").font(.wisp(.headline, weight: .semibold))
                .foregroundStyle(Theme.textPrimary).lineLimit(1)
            label(ideate.files.isEmpty ? "Done" : "Made")
            ForEach(ideate.files.prefix(5)) { file in
                HStack(spacing: 8) {
                    Image(systemName: "doc").font(.wisp(size: 10)).foregroundStyle(Theme.textMuted).frame(width: 14)
                        .accessibilityHidden(true)
                    Text(file.path).font(.wisp(.body)).foregroundStyle(Theme.textSecondary).lineLimit(1)
                }
            }
            if ideate.files.count > 5 {
                Text("and \(ideate.files.count - 5) more").font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
            if kind.showsTests, let test = IdeateTimeline.lastTestRun(ideate.timeline.rows) {
                Text(IdeateTimeline.display(test)).font(.wisp(.footnote))
                    .foregroundStyle(test.state == "failed" ? Theme.bad : Theme.textSecondary)
            }
            HStack(spacing: 8) {
                pill("Open folder", icon: "folder", filled: true) { ideate.openFolder() }
                if kind.showsPreview && ideate.hasPreview {
                    pill("Open preview", icon: "safari", filled: false) { ideate.openPreview() }
                }
                if kind.showsTests {
                    pill("Run tests", icon: "checkmark.circle", filled: false) {
                        ideate.send("Run the tests and tell me what passed and what failed.")
                    }
                }
                if kind.runsSample {
                    pill("Run sample", icon: "play", filled: false) {
                        ideate.send("Run it on the sample input and show me the result.")
                    }
                }
                pill("New idea", icon: "plus", filled: false) { ideate.newProject() }
            }
            followUpField
        }
    }

    private var followUpField: some View {
        TextField("Change something…", text: $followUp)
            .textFieldStyle(.plain)
            .font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(followUpFocused ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
            .focused($followUpFocused)
            .onSubmit {
                let text = followUp.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { return }
                ideate.send(text)
                followUp = ""
            }
            .accessibilityLabel("Ask Ideate to change something")
    }

    // MARK: Failed and unavailable

    private var failed: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(ideate.current?.name ?? "Ideate").font(.wisp(.headline, weight: .semibold))
                .foregroundStyle(Theme.textPrimary).lineLimit(1)
            Text(ideate.status == .cancelled ? "Stopped. What was made so far is kept in its folder."
                                             : (ideate.failure ?? "Something went wrong."))
                .font(.wisp(.body)).foregroundStyle(ideate.status == .cancelled ? Theme.textSecondary : Theme.bad)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                pill("Try again", icon: "arrow.clockwise", filled: true) {
                    ideate.send("Try again from where you stopped.")
                }
                pill("New idea", icon: "plus", filled: false) { ideate.newProject() }
            }
        }
    }

    private var unavailable: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Ideate isn't ready").font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            Text(ideate.availability.reason.isEmpty ? "Ideate can't start on this Mac yet." : ideate.availability.reason)
                .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            pill("Check again", icon: "arrow.clockwise", filled: false) { Task { await ideate.refresh() } }
        }
    }

    // MARK: Pieces

    private func label(_ text: String) -> some View {
        Text(text).font(.wisp(.footnote, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            .accessibilityAddTraits(.isHeader)
    }

    private func pill(_ title: String, icon: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.wisp(.footnote, weight: .medium))
                .foregroundStyle(filled ? Color.black : Theme.textPrimary)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Capsule().fill(filled ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(filled ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}
