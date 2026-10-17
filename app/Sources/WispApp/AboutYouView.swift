import SwiftUI
import AppKit

/// Memory → About you: what the user told Wisp about themselves in Getting to
/// know you, editable in place. It is the same profile (`/profile`) the notch
/// flow writes and the one every local prompt, the brief and Today read, so a
/// change here is in effect at once. Erasing it removes the file.
@MainActor
final class AboutYouModel: ObservableObject {
    @Published var profile = UserProfile()
    @Published var status = ProfileStatus()
    @Published var preview: [String] = []
    @Published var loaded = false
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
        if let envelope = await client.profile() { apply(envelope) }
        loaded = true
    }

    func save() async {
        addGoal(); addStruggle(); addPerson()
        busy = true
        defer { busy = false }
        let result = await client.patchProfile(profile.fullPatch)
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
                if model.loaded {
                    if model.status.hasContent { inEffect }
                    section("What to call you") {
                        textField($model.profile.name, placeholder: "Your first name, or a nickname").frame(maxWidth: 320)
                    }
                    section("Who you are, in your own words") {
                        editor($model.profile.about, placeholder: KnowYouStep.about.placeholder)
                    }
                    section("What you're working toward", detail: "Up to five. One is kept in view each morning, taking turns.") {
                        stringList($model.profile.goals, draft: $model.goalDraft,
                                   placeholder: KnowYouStep.goals.placeholder, add: model.addGoal)
                    }
                    section("What tends to get in your way", detail: "Mentioned only when a day shows the pattern, once and gently.") {
                        stringList($model.profile.struggles, draft: $model.struggleDraft,
                                   placeholder: KnowYouStep.struggles.placeholder, add: model.addStruggle)
                    }
                    section("People who matter to you", detail: "Their mail and texts rise to the top of Needs you. This never changes what Wisp alerts you about.") {
                        peopleList
                    }
                    section("How you like to be talked to") {
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
                        editor($model.profile.boundaries, placeholder: KnowYouStep.boundaries.placeholder)
                    }
                    section("Anything else") {
                        editor($model.profile.notes, placeholder: KnowYouStep.notes.placeholder)
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
            Text("Your brief, replies and Today go back to knowing only what your sources show. Saved memories and learned routines are not touched.")
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("What you told Wisp about yourself. It shapes your brief, your replies and what rises to the top of Today, on this Mac only.")
                .font(.system(size: 13)).foregroundStyle(ChatTheme.text2)
                .fixedSize(horizontal: false, vertical: true)
            if model.loaded && model.status.stale, let days = model.status.daysSinceUpdate {
                Label("It's been \(days) days since you changed this. Still true?", systemImage: "clock")
                    .font(.system(size: 12)).foregroundStyle(ChatTheme.amber)
            }
            if model.loaded && !model.status.hasContent {
                Text("Nothing here yet. Answer the questions in the notch, or fill in whatever you like below.")
                    .font(.system(size: 12.5)).foregroundStyle(ChatTheme.text3)
            }
        }
    }

    private var inEffect: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("In effect now").font(.system(size: 11, weight: .semibold)).foregroundStyle(ChatTheme.text3).textCase(.uppercase)
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
                    .disabled(model.busy)
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
