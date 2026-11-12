import SwiftUI

// Settings › Privacy › "Write reply drafts in my style" (service/personal_style.py).
// The person presses Learn; Wisp reads the emails they sent in Apple Mail and trains a
// small adapter on this Mac. The adapter writes reply drafts only: text in an editable
// field, never a tool call, never a send. Unknown state shows "Check", never a guess.

struct WritingStyle: Equatable {
    let available: Bool
    /// False on a Mac with less memory than learning and styled drafts need.
    let enoughMemory: Bool
    let memoryNeededGB: Int
    let enabled: Bool
    let phase: String
    let step: Int
    let steps: Int
    let trained: Bool
    let current: Bool
    let examples: Int
    let error: String

    init?(_ json: [String: Any]) {
        guard let available = json["available"] as? Bool, let enabled = json["enabled"] as? Bool,
              let trained = json["trained"] as? Bool else { return nil }
        self.available = available
        enoughMemory = json["enough_memory"] as? Bool ?? true
        memoryNeededGB = json["memory_needed_gb"] as? Int ?? 24
        self.enabled = enabled
        self.trained = trained
        phase = json["phase"] as? String ?? "idle"
        step = json["step"] as? Int ?? 0
        steps = json["steps"] as? Int ?? 0
        current = json["current"] as? Bool ?? false
        examples = json["examples"] as? Int ?? 0
        error = json["error"] as? String ?? ""
    }

    var busy: Bool { phase != "idle" }

    /// The one line under the title. Never claims more than the service said.
    var line: String {
        if !enoughMemory {
            return "This needs a Mac with \(memoryNeededGB) GB of memory or more. Reply drafts use the usual model on this Mac."
        }
        if !available { return "This needs the oMLX app on this Mac. Wisp couldn't find its Python." }
        if phase == "reading" { return step > 0 ? "Reading your sent mail… \(step) emails so far." : "Reading your sent mail…" }
        if phase == "training" { return steps > 0 ? "Learning… step \(step) of \(steps). You can keep using Wisp." : "Learning…" }
        if !error.isEmpty { return error }
        if trained && !current { return "The chat model changed since Wisp learned your style. Learn again to use it." }
        if trained { return "Learned from \(examples) emails you sent. Used only for reply drafts you edit and send yourself." }
        return "Wisp reads the emails you sent in Apple Mail and learns how you write, on this Mac. Nothing leaves your Mac."
    }
}

@MainActor
final class WritingStyleModel: ObservableObject {
    @Published private(set) var state: WritingStyle?
    @Published private(set) var pending = false
    private var poll: Task<Void, Never>?

    func refresh() async {
        if case .ok(let json) = await MeetingClient.send("GET", "personal_style") { apply(json) }
    }

    func setEnabled(_ on: Bool) { send("PUT", "personal_style", ["enabled": on]) }
    func learn() { send("POST", "personal_style/train", [:]) }
    func stop() { send("POST", "personal_style/stop", [:]) }
    func forget() { send("POST", "personal_style/forget", [:]) }

    private func send(_ method: String, _ path: String, _ body: [String: Any]) {
        guard !pending else { return }
        pending = true
        Task {
            // A refused or lost reply is followed by a fresh read, so the row shows what is true.
            if case .ok(let json) = await MeetingClient.send(method, path, body: body) { apply(json) }
            else { await refresh() }
            pending = false
        }
    }

    private func apply(_ json: [String: Any]) {
        state = WritingStyle(json)
        poll?.cancel()
        guard state?.busy == true else { return }
        poll = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard !Task.isCancelled else { return }
            await self?.refresh()
        }
    }
}

struct WritingStyleSettingsRow: View {
    @StateObject private var model = WritingStyleModel()
    @State private var confirmForget = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let state = model.state {
                if state.trained && state.enoughMemory {
                    Toggle(isOn: Binding(get: { state.enabled }, set: { model.setEnabled($0) })) { label(state) }
                        .toggleStyle(.switch)
                        .disabled(model.pending || state.busy)
                        .accessibilityLabel("Write reply drafts in my style")
                } else {
                    label(state)
                }
                HStack(spacing: 8) {
                    if state.busy {
                        ProgressView(value: Double(state.step), total: Double(max(state.steps, 1)))
                            .frame(width: 140)
                            .opacity(state.phase == "training" ? 1 : 0)
                        Button("Stop") { model.stop() }
                    } else {
                        Button(state.trained ? "Learn again" : "Learn my writing style") { model.learn() }
                            .disabled(!state.available || !state.enoughMemory)
                        if state.trained {
                            Button("Forget…") { confirmForget = true }
                        }
                    }
                }
                .controlSize(.small)
                .disabled(model.pending)
            } else {
                Text("Write reply drafts in my style").font(.system(size: 14, weight: .medium))
                Button("Check") { Task { await model.refresh() } }
                    .controlSize(.small)
                    .accessibilityLabel("Check whether Wisp has learned your writing style")
            }
        }
        .task { await model.refresh() }
        .confirmationDialog("Forget your writing style?", isPresented: $confirmForget, titleVisibility: .visible) {
            Button("Forget", role: .destructive) { model.forget() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This erases what Wisp learned from the emails you sent. Reply drafts go back to the usual model.")
        }
    }

    private func label(_ state: WritingStyle) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Write reply drafts in my style").font(.system(size: 14, weight: .medium))
            Text(state.line).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
