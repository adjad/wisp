import SwiftUI
import AppKit

/// "Getting to know you" in the notch: one question at a time, every one
/// optional, saved as you go. The copy and order live in KnowYouFlow.swift; this
/// file draws them on the notch's black surface in the panel's own idiom (chips,
/// hairlines, the orb) so it feels like Wisp asking, not a form to fill.
///
/// Comfort is a design input here, not a tone: the reason for each question is
/// shown with it and is exactly what Wisp will do with the answer; the footer
/// always says where the answers live; Back, Skip and Continue are on every
/// screen; and the last screen shows the answers already at work.
@MainActor
final class KnowYouModel: ObservableObject {
    @Published var flow = KnowYouFlow()
    @Published var profile = UserProfile()
    @Published var preview: [String] = []
    @Published var loaded = false
    @Published var busy = false
    @Published var error = ""
    // Drafts for the list questions.
    @Published var goalDraft = ""
    @Published var struggleDraft = ""
    @Published var personName = ""
    @Published var personRelation = ""

    private let client: WispClient
    /// Called when the flow ends, with a short notice for the panel or nil.
    var onClose: (String?) -> Void = { _ in }
    /// "Show my brief" on the last screen.
    var onShowBrief: () -> Void = {}

    init(client: WispClient) { self.client = client }

    func load() async {
        guard !loaded else { return }
        if let envelope = await client.profile() {
            profile = envelope.profile
            preview = envelope.preview
            flow = KnowYouFlow.resume(for: envelope.profile)
        }
        if profile.name.isEmpty {
            profile.name = UserProfile.suggestedName(fullName: NSFullUserName())
        }
        loaded = true
    }

    // MARK: Moving

    func begin() { flow.begin() }
    func back() { error = ""; flow.back() }
    /// Leave this answer unsaved and move on.
    func skip() { error = ""; flow.advance() }

    func later() async {
        busy = true
        _ = await client.dismissProfile()
        busy = false
        onClose(nil)
    }

    /// The panel moved on (a tab, the X) before Done. Nothing answered is lost;
    /// the service counts it as Later, so it offers once more in a few days.
    func leave() async {
        if profile.completedAt == nil && !flow.isDone {
            _ = await client.dismissProfile()
        }
        onClose(nil)
    }

    func saveAndContinue() async {
        commitDrafts()
        busy = true
        defer { busy = false }
        let result = await client.patchProfile(profile.patch(for: flow.step))
        if let envelope = result.envelope {
            // Keep the service's cleaned copy of what was just saved; drafts on
            // later steps are still only here.
            apply(envelope, step: flow.step)
            error = ""
            flow.advance()
        } else {
            error = result.error ?? "Couldn't save that just now. Your answer is still here; try again."
        }
    }

    func finish() async {
        busy = true
        if let envelope = await client.completeProfile() {
            profile = envelope.profile
            preview = envelope.preview
        }
        busy = false
        onClose(profile.name.isEmpty ? "Thank you. I'll put this to work."
                : "Thank you, \(profile.name). I'll put this to work.")
    }

    func showBrief() async {
        busy = true
        _ = await client.completeProfile()
        busy = false
        onShowBrief()
    }

    private func apply(_ envelope: ProfileEnvelope, step: KnowYouStep) {
        let saved = envelope.profile
        switch step {
        case .name: profile.name = saved.name
        case .about: profile.about = saved.about
        case .goals: profile.goals = saved.goals
        case .struggles: profile.struggles = saved.struggles
        case .people: profile.people = saved.people
        case .tone: profile.tone = saved.tone; profile.detail = saved.detail
        case .boundaries: profile.boundaries = saved.boundaries
        case .notes: profile.notes = saved.notes
        default: break
        }
        profile.completedAt = saved.completedAt
        profile.updatedAt = saved.updatedAt
        preview = envelope.preview
    }

    // MARK: Lists

    private func commitDrafts() {
        switch flow.step {
        case .goals: addGoal()
        case .struggles: addStruggle()
        case .people: addPerson()
        default: break
        }
    }

    func addGoal() {
        let text = goalDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if profile.goals.count < 5, !profile.goals.contains(where: { $0.caseInsensitiveCompare(text) == .orderedSame }) {
            profile.goals.append(text)
        }
        goalDraft = ""
    }

    func removeGoal(_ goal: String) { profile.goals.removeAll { $0 == goal } }

    func hasStruggle(_ text: String) -> Bool {
        profile.struggles.contains { $0.caseInsensitiveCompare(text) == .orderedSame }
    }

    func toggleStruggle(_ text: String) {
        if hasStruggle(text) {
            profile.struggles.removeAll { $0.caseInsensitiveCompare(text) == .orderedSame }
        } else if profile.struggles.count < 6 {
            profile.struggles.append(text)
        }
    }

    func addStruggle() {
        let text = struggleDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if !hasStruggle(text) && profile.struggles.count < 6 { profile.struggles.append(text) }
        struggleDraft = ""
    }

    func addPerson() {
        let name = personName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        let relation = personRelation.trimmingCharacters(in: .whitespacesAndNewlines)
        if profile.people.count < 8, !profile.people.contains(where: { $0.name.caseInsensitiveCompare(name) == .orderedSame }) {
            profile.people.append(.init(name: name, relation: relation))
        }
        personName = ""
        personRelation = ""
    }

    func removePerson(_ person: UserProfile.Person) { profile.people.removeAll { $0 == person } }

    func appendAbout(_ phrase: String) {
        let trimmed = about.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { profile.about = phrase }
        else if !trimmed.localizedCaseInsensitiveContains(phrase) { profile.about = trimmed + ". " + phrase }
    }

    private var about: String { profile.about }

    var canContinue: Bool {
        switch flow.step {
        case .name: return !profile.name.trimmingCharacters(in: .whitespaces).isEmpty
        case .about: return !profile.about.trimmingCharacters(in: .whitespaces).isEmpty
        case .goals: return !profile.goals.isEmpty || !goalDraft.trimmingCharacters(in: .whitespaces).isEmpty
        case .struggles: return !profile.struggles.isEmpty || !struggleDraft.trimmingCharacters(in: .whitespaces).isEmpty
        case .people: return !profile.people.isEmpty || !personName.trimmingCharacters(in: .whitespaces).isEmpty
        case .tone: return true
        case .boundaries: return !profile.boundaries.trimmingCharacters(in: .whitespaces).isEmpty
        case .notes: return !profile.notes.trimmingCharacters(in: .whitespaces).isEmpty
        default: return false
        }
    }
}

struct KnowYouView: View {
    @ObservedObject var model: KnowYouModel
    @FocusState private var focused: Bool
    @State private var breathe = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            progressRow
            if !model.loaded {
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
        .task { await model.load(); focused = true }
        .onAppear { breathe = true }
        .onChange(of: model.flow) { _, _ in focused = true }
        .animation(.easeInOut(duration: 0.18), value: model.flow)
    }

    // MARK: Progress

    private var progressRow: some View {
        HStack(spacing: 8) {
            Text(model.flow.isWelcome ? "Getting to know you"
                 : model.flow.isDone ? "All set"
                 : "\(model.flow.questionNumber ?? 0) of \(KnowYouFlow.questionCount)")
                .font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.textSecondary).textCase(.uppercase)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.chipFill)
                    Capsule().fill(Color.white.opacity(0.85))
                        .frame(width: max(0, geo.size.width * model.flow.progress))
                        .animation(.easeInOut(duration: 0.25), value: model.flow.progress)
                }
            }
            .frame(height: 3)
            .accessibilityLabel("Progress")
            .accessibilityValue("\(Int(model.flow.progress * 100)) percent")
            Label("Stays on this Mac", systemImage: "lock.fill")
                .font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                .fixedSize()
        }
    }

    // MARK: Question

    @ViewBuilder private var question: some View {
        let step = model.flow.step
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                if step == .welcome || step == .done { orb.padding(.top, 2) }
                VStack(alignment: .leading, spacing: 5) {
                    Text(step.title).font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(step.why).font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            switch step {
            case .welcome: EmptyView()
            case .name: nameField
            case .about: aboutField
            case .goals: goalsField
            case .struggles: strugglesField
            case .people: peopleField
            case .tone: toneField
            case .boundaries: longField($model.profile.boundaries, placeholder: step.placeholder)
            case .notes: longField($model.profile.notes, placeholder: step.placeholder)
            case .done: summary
            }
        }
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

    private var aboutField: some View {
        VStack(alignment: .leading, spacing: 8) {
            longField($model.profile.about, placeholder: KnowYouStep.about.placeholder)
            chipRow(KnowYouChips.about, selected: { model.profile.about.localizedCaseInsensitiveContains($0) }) {
                model.appendAbout($0)
            }
        }
    }

    private var goalsField: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(model.profile.goals, id: \.self) { goal in
                listRow(goal) { model.removeGoal(goal) }
            }
            if model.profile.goals.count < 5 {
                addRow($model.goalDraft, placeholder: KnowYouStep.goals.placeholder, add: model.addGoal)
            }
        }
    }

    private var strugglesField: some View {
        VStack(alignment: .leading, spacing: 8) {
            chipRow(KnowYouChips.struggles, selected: model.hasStruggle) { model.toggleStruggle($0) }
            ForEach(model.profile.struggles.filter { !KnowYouChips.struggles.contains($0) }, id: \.self) { text in
                listRow(text) { model.toggleStruggle(text) }
            }
            if model.profile.struggles.count < 6 {
                addRow($model.struggleDraft, placeholder: KnowYouStep.struggles.placeholder, add: model.addStruggle)
            }
        }
    }

    private var peopleField: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(model.profile.people, id: \.name) { person in
                listRow(person.label) { model.removePerson(person) }
            }
            if model.profile.people.count < 8 {
                HStack(spacing: 8) {
                    field($model.personName, placeholder: "Name", submit: model.addPerson)
                        .frame(maxWidth: 170)
                    field($model.personRelation, placeholder: "Who they are to you", submit: model.addPerson)
                    smallButton("Add", action: model.addPerson)
                        .disabled(model.personName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                chipRow(KnowYouChips.relations, selected: { model.personRelation.caseInsensitiveCompare($0) == .orderedSame }) {
                    model.personRelation = $0
                }
            }
        }
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
            case .welcome:
                Button("Later") { Task { await model.later() } }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .help("Ask me another time. The menu bar's Tell Wisp About You… is always here.")
                Spacer()
                primaryButton("Let's begin", action: model.begin)
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
                Button("Skip") { model.skip() }
                    .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .help("Leave this one unanswered")
                Spacer()
                Text("Saved as you go").font(.system(size: 10.5)).foregroundStyle(Theme.textMuted)
                primaryButton(model.flow.isLastQuestion ? "Finish" : "Continue") { Task { await model.saveAndContinue() } }
                    .disabled(!model.canContinue)
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

    private func longField(_ text: Binding<String>, placeholder: String) -> some View {
        TextField(placeholder, text: text, axis: .vertical)
            .textFieldStyle(.plain)
            .lineLimit(2...5)
            .font(.system(size: 14)).foregroundStyle(Theme.textPrimary)
            .focused($focused)
            .padding(.vertical, 9).padding(.horizontal, 12)
            .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private func addRow(_ text: Binding<String>, placeholder: String, add: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            field(text, placeholder: placeholder, submit: add).focused($focused)
            smallButton("Add", action: add)
                .disabled(text.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty)
        }
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

    private func chipRow(_ options: [String], selected: @escaping (String) -> Bool,
                         tap: @escaping (String) -> Void) -> some View {
        FlowRow(spacing: 6) {
            ForEach(options, id: \.self) { option in
                let on = selected(option)
                Button { tap(option) } label: {
                    Text(option).font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(on ? Color.black : Theme.textSecondary)
                        .padding(.horizontal, 10).padding(.vertical, 4)
                        .background(Capsule().fill(on ? Color.white : Theme.chipFill))
                        .overlay(Capsule().stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            }
        }
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

/// Chips that wrap onto new lines inside the panel's fixed width.
struct FlowRow: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 600
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: width, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX { x = bounds.minX; y += rowHeight + spacing; rowHeight = 0 }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
