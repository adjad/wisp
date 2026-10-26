import SwiftUI
import AppKit

/// "Getting to know you" in the notch: three questions, every one optional,
/// saved as you go, under a minute. The copy and order live in KnowYouFlow.swift;
/// this file draws them on the notch's black surface in the panel's own idiom
/// (chips, hairlines, the orb) so it feels like Wisp asking, not a form.
///
/// Comfort is a design input here, not a tone: the reason for each question is
/// shown with it and is exactly what Wisp will do with the answer; the header
/// always says where the answers live; Not now, Skip and Continue are on every
/// question (Back from the second on); and the last screen shows the answers
/// already at work.
@MainActor
final class KnowYouModel: ObservableObject {
    @Published var flow = KnowYouFlow()
    @Published var profile = UserProfile()
    @Published var preview: [String] = []
    @Published var status = ProfileStatus()
    @Published var loaded = false
    @Published var busy = false
    @Published var error = ""
    @Published var goalDraft = ""
    /// The service didn't answer the first read. The flow stays hidden behind a
    /// Retry, so a failed load never shows (or saves) default answers.
    @Published var loadFailed = false

    private let client: WispClient
    /// Called when the flow ends, with a short notice for the panel or nil.
    var onClose: (String?) -> Void = { _ in }
    /// "Show my brief" on the last screen.
    var onShowBrief: () -> Void = {}
    /// Set once the flow has reported its ending to the service (Later,
    /// Done, or leaving), so one gesture can never count twice.
    private var ended = false

    init(client: WispClient) { self.client = client }

    func load() async {
        guard !loaded else { return }
        loadFailed = false
        guard let envelope = await client.profile() else {
            loadFailed = true
            error = "Wisp's service isn't answering yet."
            return
        }
        error = ""
        profile = envelope.profile
        preview = envelope.preview
        status = envelope.status
        flow = KnowYouFlow.resume(for: envelope.profile)
        if profile.name.isEmpty {
            profile.name = UserProfile.suggestedName(fullName: NSFullUserName())
        }
        loaded = true
    }

    // MARK: Moving

    func back() { error = ""; flow.back() }
    /// Leave this answer unsaved and move on. On the last question that is the
    /// summary, which shows only what was saved.
    func skip() { error = ""; flow.advance() }

    /// Not now: close the questions and say where they are. Answers already
    /// saved stay saved; the service counts it as Later.
    func notNow() async {
        guard !busy, !ended else { return }
        ended = true
        busy = true
        let saved = await client.dismissProfile() != nil
        busy = false
        // Best effort: a down service just means Wisp may ask again.
        onClose(saved ? KnowYouNotice.notNow : KnowYouNotice.notNowUnsaved)
    }

    /// The panel moved on (a tab, the X) before Done. Nothing answered is lost;
    /// the service counts it as Later, so it offers once more in a few days.
    /// Reaching the last screen counts as complete even if Done wasn't pressed.
    /// It never calls onClose: the panel has already dropped this flow.
    func leave() async {
        guard !ended else { return }
        ended = true
        // Left while the profile was still loading: still a Later, so Wisp's own
        // offer waits its few days instead of coming back at the next panel
        // close. But not when the load failed: the service wasn't answering (a
        // cold launch), the person never saw a question, and each dismissal
        // spends one of the two automatic offers (profile.dismiss bumps
        // dismiss_count every time, finished profile or not).
        guard loaded else {
            if !loadFailed { _ = await client.dismissProfile() }
            return
        }
        if flow.isDone {
            _ = await client.completeProfile()
        } else if profile.completedAt == nil {
            _ = await client.dismissProfile()
        }
    }

    func saveAndContinue() async {
        guard canContinue, !busy, !ended else { return }
        if flow.step == .goals { addGoal() }
        busy = true
        defer { busy = false }
        let result = await client.patchProfile(profile.patch(for: flow.step))
        if let envelope = result.envelope {
            apply(envelope, step: flow.step)
            error = ""
            if flow.step == .goals { goalDraft = "" }
            flow.advance()
        } else {
            error = result.error ?? "Couldn't save that just now. Your answer is still here; try again."
        }
    }

    func finish() async {
        guard !busy, !ended else { return }
        busy = true
        defer { busy = false }
        guard let envelope = await client.completeProfile() else {
            error = "Couldn't save that just now. Your answers are still here; try Done again."
            return
        }
        ended = true
        profile = envelope.profile
        preview = envelope.preview
        onClose(KnowYouNotice.thanks(name: profile.name))
    }

    func showBrief() async {
        guard !busy, !ended else { return }
        busy = true
        defer { busy = false }
        // Same as Done: if the answers weren't saved, say so and stay here,
        // or the questionnaire would quietly come back later.
        guard let envelope = await client.completeProfile() else {
            error = "Couldn't save that just now. Your answers are still here; try again."
            return
        }
        ended = true
        profile = envelope.profile
        preview = envelope.preview
        onShowBrief()
    }

    private func apply(_ envelope: ProfileEnvelope, step: KnowYouStep) {
        let saved = envelope.profile
        switch step {
        case .name: profile.name = saved.name
        case .goals: profile.goals = saved.goals
        case .tone: profile.tone = saved.tone; profile.detail = saved.detail
        default: break
        }
        profile.completedAt = saved.completedAt
        profile.updatedAt = saved.updatedAt
        preview = envelope.preview
        status = envelope.status
    }

    // MARK: Goals

    func addGoal() {
        let text = goalDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if profile.goals.count < 5, !profile.goals.contains(where: { $0.caseInsensitiveCompare(text) == .orderedSame }) {
            profile.goals.append(text)
        }
        goalDraft = ""
    }

    func removeGoal(_ goal: String) { profile.goals.removeAll { $0 == goal } }

    var canContinue: Bool {
        switch flow.step {
        case .name: return !profile.name.trimmingCharacters(in: .whitespaces).isEmpty
        case .goals: return !profile.goals.isEmpty || !goalDraft.trimmingCharacters(in: .whitespaces).isEmpty
        case .tone: return true
        default: return false
        }
    }
}

struct KnowYouView: View {
    @ObservedObject var model: KnowYouModel
    @FocusState private var focused: Bool
    @State private var breathe = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            progressRow
            if model.loadFailed {
                Text(model.error).font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                HStack(spacing: 10) {
                    Button("Not now") { Task { await model.notNow() } }
                        .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    Spacer()
                    primaryButton("Retry") { Task { await model.load(); focused = true } }
                }
            } else if !model.loaded {
                ProcessingRow(label: "One moment…")
            } else {
                question
                if !model.error.isEmpty {
                    Text(model.error).font(.system(size: 11)).foregroundStyle(Theme.bad)
                }
                footer
            }
        }
        .padding(.top, 12)
        .task { await model.load(); await refocus() }
        .onAppear { breathe = true }
        .onChange(of: model.flow) { _, _ in Task { await refocus() } }
        .animation(.easeInOut(duration: 0.18), value: model.flow)
    }

    /// The new field is not in the hierarchy until the step's animation lands, so
    /// hop a beat before asking for focus.
    private func refocus() async {
        try? await Task.sleep(nanoseconds: 120_000_000)
        focused = true
    }

    // MARK: Progress

    // The row keeps one geometry across steps: the label is sized by its
    // widest string (hidden underneath), so the track never changes width when
    // "Getting to know you" becomes "1 of 3"; the fill is a full-width capsule
    // scaled from the left, so nothing is recomputed from a GeometryReader
    // mid-animation; and the row opts out of the panel's step animation, so
    // only the fill moves, and only when progress changes.
    private var progressRow: some View {
        HStack(spacing: 8) {
            Text(progressLabelWidest).hidden()
                .overlay(alignment: .leading) { Text(progressLabel).lineLimit(1) }
                .font(.system(size: 11, weight: .semibold)).monospacedDigit()
                .foregroundStyle(Theme.textSecondary).textCase(.uppercase)
                .fixedSize()
                .accessibilityHidden(true)
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.chipFill)
                Capsule().fill(Color.white.opacity(0.85))
                    .scaleEffect(x: max(0.001, model.flow.progress), y: 1, anchor: .leading)
                    .animation(reduceMotion ? nil : .easeInOut(duration: 0.25), value: model.flow.progress)
            }
            .frame(height: 3)
            .frame(maxWidth: .infinity)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(progressLabel + ". Progress")
            .accessibilityValue("\(Int(model.flow.progress * 100)) percent")
            Label(model.status.whereItLives, systemImage: model.status.localOnly ? "lock.fill" : "cloud")
                .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                .fixedSize()
        }
        .transaction { $0.animation = nil }
    }

    private var progressLabel: String {
        model.flow.isDone ? "Done"
            : "Getting to know you · \(model.flow.questionNumber ?? 0) of \(KnowYouFlow.questionCount)"
    }

    /// The longest label the row can show, so the row is sized for it from the start.
    private var progressLabelWidest: String { "Getting to know you · 3 of 3" }

    // MARK: Question

    @ViewBuilder private var question: some View {
        let step = model.flow.step
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                if model.flow.isFirst || step == .done { orb.padding(.top, 2) }
                VStack(alignment: .leading, spacing: 5) {
                    Text(title(for: step)).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(step.why).font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            switch step {
            case .name: nameField
            case .goals:
                // Short displays: the footer must never be pushed off the panel.
                ScrollView(.vertical, showsIndicators: false) { goalsField }
                    .frame(maxHeight: 230)
            case .tone: toneField
            case .done: summary
            }
        }
    }

    /// The summary greets the person by the name they just gave.
    private func title(for step: KnowYouStep) -> String {
        guard step == .done else { return step.title }
        let name = model.profile.name.trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? "All set. Thank you!" : "All set, \(name). Thank you!"
    }

    private var nameField: some View {
        TextField(KnowYouStep.name.placeholder, text: $model.profile.name)
            .textFieldStyle(.plain)
            .font(.system(size: 17)).foregroundStyle(Theme.textPrimary)
            .focused($focused)
            .onSubmit { Task { await model.saveAndContinue() } }
            .padding(.vertical, 9).padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    // Select all that apply: a grid of cards, each with what it covers. A
    // picked card turns white. Past five, unpicked cards simply do not light up.
    private var goalsField: some View {
        VStack(alignment: .leading, spacing: 10) {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)],
                      alignment: .leading, spacing: 8) {
                ForEach(KnowYouChips.focusAreas) { area in focusCard(area) }
            }
            HStack(spacing: 8) {
                Text(pickedLine).font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                Spacer()
            }
            ForEach(model.profile.customGoals, id: \.self) { goal in
                listRow(goal) { model.removeGoal(goal) }
            }
            if model.profile.goals.count < KnowYouChips.maxGoals {
                HStack(spacing: 8) {
                    field($model.goalDraft, placeholder: KnowYouStep.goals.placeholder, submit: model.addGoal)
                    smallButton("Add", action: model.addGoal)
                        .disabled(model.goalDraft.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }

    private var pickedLine: String {
        let count = model.profile.goals.count
        if count == 0 { return "Tap everything that fits." }
        if count >= KnowYouChips.maxGoals { return "Five picked. Unpick one to choose another." }
        return "\(count) picked. Up to five."
    }

    private func focusCard(_ area: FocusArea) -> some View {
        let on = model.profile.hasFocus(area)
        let full = !on && model.profile.goals.count >= KnowYouChips.maxGoals
        return Button { model.profile.toggleFocus(area) } label: {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(area.title).font(.system(size: 12.5, weight: .semibold))
                    Text(area.detail).font(.system(size: 10.5)).opacity(0.75)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 4)
                Image(systemName: on ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 12, weight: .semibold))
                    .opacity(on ? 1 : 0.5)
            }
            .foregroundStyle(on ? Color.black : Theme.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 10).fill(on ? Color.white : Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: 10))
            .opacity(full ? 0.55 : 1)
        }
        .buttonStyle(.plain)
        .help(full ? "Five picked already" : area.goal)
        .accessibilityLabel("\(area.title). \(area.detail)")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
    }

    private var toneField: some View {
        VStack(alignment: .leading, spacing: 12) {
            choiceGroup("Tone", options: KnowYouChips.tones, selection: $model.profile.tone)
            choiceGroup("Length", options: KnowYouChips.details, selection: $model.profile.detail)
        }
    }

    private func choiceGroup(_ label: String, options: [(value: String, label: String, detail: String)],
                             selection: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
            HStack(spacing: 8) {
                ForEach(options, id: \.value) { option in
                    let on = selection.wrappedValue == option.value
                    Button { selection.wrappedValue = option.value } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(option.label).font(.system(size: 12.5, weight: .medium))
                            Text(option.detail).font(.system(size: 10.5)).opacity(0.75)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .foregroundStyle(on ? Color.black : Theme.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10).padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 10).fill(on ? Color.white : Theme.chipFill))
                        .overlay(RoundedRectangle(cornerRadius: 10).stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
                        .contentShape(RoundedRectangle(cornerRadius: 10))
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
                }
            }
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(model.preview.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(Theme.textSecondary)
                    Text(line).font(.system(size: 12.5)).foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 10) {
            switch model.flow.step {
            case .done:
                chipButton("Change something", icon: "chevron.left") { model.back() }
                Spacer()
                chipButton("Show my brief", icon: "sun.max") { Task { await model.showBrief() } }
                    .help("Compose a brief now, with this in it")
                primaryButton("Done") { Task { await model.finish() } }
            default:
                if model.flow.canGoBack {
                    chipButton("Back", icon: "chevron.left") { model.back() }
                }
                Button("Not now") { Task { await model.notNow() } }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .help("Close the questions; what you answered is kept. Type /about, or use the menu bar's Tell Wisp About You…, any time.")
                Spacer()
                Button("Skip") { model.skip() }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .help(model.flow.isLastQuestion ? "Leave this one unanswered and finish" : "Leave this one unanswered")
                primaryButton(model.flow.isLastQuestion ? "Finish" : "Continue") { Task { await model.saveAndContinue() } }
                    .disabled(!model.canContinue)
                    .help("Each answer is saved when you continue")
            }
        }
        .disabled(model.busy)
    }

    // MARK: Pieces

    private func field(_ text: Binding<String>, placeholder: String, submit: @escaping () -> Void) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain)
            .font(.system(size: 13)).foregroundStyle(Theme.textPrimary)
            .onSubmit(submit)
            .padding(.vertical, 7).padding(.horizontal, 10)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private func listRow(_ text: String, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            Text(text).font(.system(size: 13)).foregroundStyle(Theme.textPrimary).lineLimit(2)
            Spacer(minLength: 8)
            Button(action: remove) {
                Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                    .frame(width: 18, height: 18)
            }
            .buttonStyle(.plain).help("Remove").accessibilityLabel("Remove \(text)")
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
    }

    private func primaryButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 12.5, weight: .semibold)).foregroundStyle(Color.black)
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Capsule().fill(Color.white))
        }
        .buttonStyle(.plain)
        .keyboardShortcut(.defaultAction)
    }

    private func chipButton(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
    }

    private func smallButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
    }

    private var orb: some View {
        Circle()
            .fill(Theme.orb)
            .frame(width: 22, height: 22)
            .scaleEffect(breathe ? 1.1 : 0.94)
            .opacity(breathe ? 1.0 : 0.85)
            .shadow(color: .white.opacity(0.12), radius: 5)
            .animation(.easeInOut(duration: 4).repeatForever(autoreverses: true), value: breathe)
    }
}
