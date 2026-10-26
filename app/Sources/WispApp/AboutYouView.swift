import SwiftUI
import AppKit

/// Memory → About you: what the user told Wisp about themselves in Getting to
/// know you, and what Wisp now does with it, editable in place. It is the same
/// profile (`/profile`) the notch flow writes and the one every local prompt and
/// the brief read, so a change here is in effect at once. Erase removes every
/// answer and keeps only the decision, so the questions aren't offered again
/// on their own.
///
/// Nothing is shown, and nothing can be saved, until the profile has been read
/// once: a failed read must never look like an empty profile, or Save would
/// write empty answers over the real ones. Save sends only what changed.
@MainActor
final class AboutYouModel: ObservableObject {
    @Published var profile = UserProfile()
    @Published var status = ProfileStatus()
    @Published var preview: [String] = []
    @Published var loaded = false
    /// The first read failed. The form stays hidden behind Retry.
    @Published var loadFailed = false
    /// The profile as last read or saved: Save sends only what differs from it.
    private var stored = UserProfile()
    @Published var busy = false
    @Published var error = ""
    @Published var savedAt: Date?
    @Published var confirmingErase = false
    // Drafts for the list editors.
    @Published var goalDraft = ""
    @Published var struggleDraft = ""
    @Published var personName = ""
    @Published var personRelation = ""
    @Published var personNote = ""

    private let client = WispClient()

    func load() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        loadFailed = false
        guard let envelope = await client.profile() else {
            // Keep whatever was shown; only a first read that fails hides the form.
            if !loaded { loadFailed = true }
            return
        }
        apply(envelope)
        error = ""
        loaded = true
    }

    /// Whether Save has anything to send.
    var hasChanges: Bool {
        var draft = profile
        draft.goals += pending(goalDraft, in: draft.goals)
        draft.struggles += pending(struggleDraft, in: draft.struggles)
        return !draft.changes(since: stored).isEmpty
            || !personName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func pending(_ draft: String, in list: [String]) -> [String] {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty || list.contains(text) ? [] : [text]
    }

    func save() async {
        guard loaded, !busy else { return }
        addGoal(); addStruggle(); addPerson()
        let patch = profile.changes(since: stored)
        guard !patch.isEmpty else { return }
        busy = true
        defer { busy = false }
        let result = await client.patchProfile(patch)
        if let envelope = result.envelope {
            apply(envelope)
            error = ""
            savedAt = Date()
        } else {
            error = result.error ?? "Couldn't save. Is Wisp's service running?"
        }
    }

    func erase() async {
        busy = true
        defer { busy = false }
        if let envelope = await client.eraseProfile() {
            apply(envelope)
            error = ""
            savedAt = nil
        } else {
            error = "Couldn't erase. Is Wisp's service running?"
        }
        confirmingErase = false
    }

    private func apply(_ envelope: ProfileEnvelope) {
        profile = envelope.profile
        stored = envelope.profile
        status = envelope.status
        preview = envelope.preview
    }

    func addGoal() {
        let text = goalDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty, profile.goals.count < 5, !profile.goals.contains(text) { profile.goals.append(text) }
        goalDraft = ""
    }

    func addStruggle() {
        let text = struggleDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty, profile.struggles.count < 6, !profile.struggles.contains(text) { profile.struggles.append(text) }
        struggleDraft = ""
    }

    func addPerson() {
        let name = personName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        if profile.people.count < 8, !profile.people.contains(where: { $0.name.caseInsensitiveCompare(name) == .orderedSame }) {
            profile.people.append(.init(name: name,
                                        relation: personRelation.trimmingCharacters(in: .whitespacesAndNewlines),
                                        note: personNote.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        personName = ""; personRelation = ""; personNote = ""
    }
}

struct AboutYouView: View {
    @StateObject private var model = AboutYouModel()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                intro
                if model.loadFailed {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Wisp's service didn't answer, so your answers can't be shown yet. Nothing has changed.")
                            .font(.system(size: 13)).foregroundStyle(ChatTheme.text2)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Try again") { Task { await model.load() } }.disabled(model.busy)
                    }
                } else if model.loaded {
                    if model.status.hasContent { inEffect }
                    section("What to call you") {
                        textField($model.profile.name, placeholder: "Your first name, or a nickname").frame(maxWidth: 320)
                    }
                    section("Who you are, in your own words") {
                        editor($model.profile.about, placeholder: "A student finishing a thesis. A founder with two kids. Between jobs and figuring it out…")
                    }
                    section("What you're working toward", detail: "Up to five. One is kept in view each morning, taking turns.") {
                        stringList($model.profile.goals, draft: $model.goalDraft,
                                   placeholder: KnowYouStep.goals.placeholder, add: model.addGoal)
                    }
                    section("What tends to get in your way", detail: "In your own words. When a day shows the pattern, your brief mentions it once, gently, with a small next step.") {
                        stringList($model.profile.struggles, draft: $model.struggleDraft,
                                   placeholder: "Add one in your own words", add: model.addStruggle)
                    }
                    section("People who matter to you", detail: "When they show up on your calendar or in your mail and texts, your morning brief points it out. This never changes what Wisp alerts you about.") {
                        peopleList
                    }
                    section("How you like to be talked to", detail: "Upbeat with full answers is how Wisp talks unless you choose otherwise.") {
                        HStack(spacing: 18) {
                            Picker("Tone", selection: $model.profile.tone) {
                                ForEach(KnowYouChips.tones, id: \.value) { Text($0.label).tag($0.value) }
                            }
                            Picker("Length", selection: $model.profile.detail) {
                                ForEach(KnowYouChips.details, id: \.value) { Text($0.label).tag($0.value) }
                            }
                        }
                        .pickerStyle(.menu).fixedSize()
                    }
                    section("What Wisp should be careful about") {
                        editor($model.profile.boundaries, placeholder: "Please don't bring up… / Never… / On a bad day, just…")
                    }
                    section("Anything else") {
                        editor($model.profile.notes, placeholder: "Anything at all")
                    }
                    actions
                } else {
                    Text("Loading…").font(.system(size: 13)).foregroundStyle(ChatTheme.text3)
                }
            }
            .padding(.vertical, 6)
            .frame(maxWidth: 720, alignment: .leading)
        }
        .task { await model.load() }
        .confirmationDialog("Erase everything Wisp knows about you from these questions?",
                            isPresented: $model.confirmingErase, titleVisibility: .visible) {
            Button("Erase", role: .destructive) { Task { await model.erase() } }
            Button("Keep", role: .cancel) {}
        } message: {
            Text("Your brief and replies go back to knowing only what your sources show, and Wisp won't offer the questions again on its own. Facts and Routines are not touched.")
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What you told Wisp about yourself, and how Wisp uses it. It shapes your replies and your morning brief. Change anything below, or erase it all.")
                .font(.system(size: 13)).foregroundStyle(ChatTheme.text2)
                .fixedSize(horizontal: false, vertical: true)
            if model.loaded {
                Label(model.status.whereItLives, systemImage: model.status.localOnly ? "lock.fill" : "cloud")
                    .font(.system(size: 11.5)).foregroundStyle(ChatTheme.text3)
            }
            if model.loaded && model.status.stale, let days = model.status.daysSinceUpdate {
                Label("It's been \(days) days since you changed this. Still true?", systemImage: "clock")
                    .font(.system(size: 12)).foregroundStyle(ChatTheme.amber)
            }
            if model.loaded && !model.status.hasContent {
                Text("Nothing here yet. Answer the three questions in the notch, or fill in whatever you like below.")
                    .font(.system(size: 12.5)).foregroundStyle(ChatTheme.text3)
            }
        }
    }

    private var inEffect: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What Wisp does with this").font(.system(size: 11, weight: .semibold)).foregroundStyle(ChatTheme.text3).textCase(.uppercase)
            ForEach(Array(model.preview.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(ChatTheme.teal)
                    Text(line).font(.system(size: 12.5)).foregroundStyle(ChatTheme.text)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(ChatTheme.glass))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(ChatTheme.line, lineWidth: 1))
    }

    private var actions: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Button("Save changes") { Task { await model.save() } }
                    .keyboardShortcut("s", modifiers: .command)
                    .disabled(model.busy || !model.loaded || !model.hasChanges)
                Button("Revisit the questions in the notch") { WispRouter.shared.open(.knowYou) }
                    .disabled(model.busy)
                Spacer()
                Button("Erase everything", role: .destructive) { model.confirmingErase = true }
                    .disabled(model.busy || !model.status.hasContent)
            }
            if !model.error.isEmpty {
                Text(model.error).font(.system(size: 12)).foregroundStyle(ChatTheme.danger)
            } else if let savedAt = model.savedAt {
                Text("Saved " + savedAt.formatted(date: .omitted, time: .shortened) + ". In effect for your next reply and brief.")
                    .font(.system(size: 12)).foregroundStyle(ChatTheme.text3)
            }
            Text("Things you asked Wisp to remember are under Facts. Habits Wisp noticed on its own are under Routines.")
                .font(.system(size: 11.5)).foregroundStyle(ChatTheme.text3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Pieces

    private func section<Content: View>(_ title: String, detail: String? = nil,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(ChatTheme.text)
                if let detail {
                    Text(detail).font(.system(size: 11.5)).foregroundStyle(ChatTheme.text3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            content()
        }
    }

    private func textField(_ text: Binding<String>, placeholder: String, submit: @escaping () -> Void = {}) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain)
            .font(.system(size: 13)).foregroundStyle(ChatTheme.text)
            .onSubmit(submit)
            .padding(.vertical, 7).padding(.horizontal, 10)
            .background(RoundedRectangle(cornerRadius: 9).fill(ChatTheme.field))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(ChatTheme.line, lineWidth: 1))
    }

    private func editor(_ text: Binding<String>, placeholder: String) -> some View {
        TextField(placeholder, text: text, axis: .vertical)
            .textFieldStyle(.plain)
            .lineLimit(2...8)
            .font(.system(size: 13)).foregroundStyle(ChatTheme.text)
            .padding(.vertical, 8).padding(.horizontal, 10)
            .background(RoundedRectangle(cornerRadius: 9).fill(ChatTheme.field))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(ChatTheme.line, lineWidth: 1))
    }

    private func stringList(_ items: Binding<[String]>, draft: Binding<String>, placeholder: String,
                            add: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(items.wrappedValue, id: \.self) { item in
                HStack(spacing: 8) {
                    Text(item).font(.system(size: 13)).foregroundStyle(ChatTheme.text)
                    Spacer()
                    Button { items.wrappedValue.removeAll { $0 == item } } label: { Image(systemName: "xmark") }
                        .buttonStyle(ChatIconButtonStyle()).help("Remove").accessibilityLabel("Remove \(item)")
                }
                .padding(.leading, 10).padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 9).fill(ChatTheme.glass))
            }
            HStack(spacing: 8) {
                textField(draft, placeholder: placeholder, submit: add)
                Button("Add", action: add).disabled(draft.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }

    private var peopleList: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(model.profile.people, id: \.name) { person in
                HStack(spacing: 8) {
                    Text(person.name).font(.system(size: 13, weight: .medium)).foregroundStyle(ChatTheme.text)
                    if !person.relation.isEmpty { Text(person.relation).font(.system(size: 12)).foregroundStyle(ChatTheme.text2) }
                    if !person.note.isEmpty { Text(person.note).font(.system(size: 12)).foregroundStyle(ChatTheme.text3).lineLimit(1) }
                    Spacer()
                    Button { model.profile.people.removeAll { $0 == person } } label: { Image(systemName: "xmark") }
                        .buttonStyle(ChatIconButtonStyle()).help("Remove").accessibilityLabel("Remove \(person.name)")
                }
                .padding(.leading, 10).padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: 9).fill(ChatTheme.glass))
            }
            if model.profile.people.count < 8 {
                HStack(spacing: 8) {
                    textField($model.personName, placeholder: "Name", submit: model.addPerson).frame(maxWidth: 160)
                    textField($model.personRelation, placeholder: "Who they are to you", submit: model.addPerson).frame(maxWidth: 180)
                    textField($model.personNote, placeholder: "A note, if it helps", submit: model.addPerson)
                    Button("Add", action: model.addPerson)
                        .disabled(model.personName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}
