import SwiftUI

struct ChatView: View {
    @Environment(AppModel.self) private var model
    @State private var draft = ""
    @State private var showConversations = false
    @State private var showSettings = false
    @FocusState private var composerFocused: Bool

    var body: some View {
        NavigationStack {
            ZStack {
                AuroraBackground()
                VStack(spacing: 0) {
                    if let messages = model.current?.messages, !messages.isEmpty {
                        transcript(messages)
                    } else {
                        EmptyChat { suggestion in model.send(suggestion) }
                    }
                    if let request = model.pendingApproval {
                        ApprovalCard(request: request)
                            .padding(.horizontal, 16)
                            .padding(.bottom, 8)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                    composer
                }
                .animation(.spring(duration: 0.35), value: model.pendingApproval)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showConversations = true } label: { Image(systemName: "text.bubble") }
                        .accessibilityLabel("Conversations")
                }
                ToolbarItem(placement: .principal) { EngineSwitcher() }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button { model.newChat() } label: { Image(systemName: "square.and.pencil") }
                        .accessibilityLabel("New chat")
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
            }
            .toolbarBackground(.hidden, for: .navigationBar)
            .sheet(isPresented: $showConversations) { ConversationsView() }
            .sheet(isPresented: $showSettings) { SettingsView() }
        }
    }

    private func transcript(_ messages: [ChatMessage]) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    ForEach(messages) { message in
                        MessageRow(message: message,
                                   isStreaming: model.isSending && message.id == messages.last?.id,
                                   status: model.statusText)
                            .id(message.id)
                    }
                    Color.clear.frame(height: 4).id("bottom")
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: messages.last?.text) { proxy.scrollTo("bottom", anchor: .bottom) }
            .onChange(of: messages.count) { withAnimation { proxy.scrollTo("bottom", anchor: .bottom) } }
            .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }

    private var composer: some View {
        VStack(spacing: 6) {
            if let readiness = model.readiness {
                Text(readiness)
                    .font(.footnote)
                    .foregroundStyle(.white.opacity(0.75))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Ask Wisp", text: $draft, axis: .vertical)
                    .lineLimit(1...6)
                    .focused($composerFocused)
                    .submitLabel(.send)
                    .onSubmit(submit)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 11)
                    .glassCard(cornerRadius: 22)
                Button {
                    if model.isSending { model.stop() } else { submit() }
                } label: {
                    Image(systemName: model.isSending ? "stop.fill" : "arrow.up")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(Theme.night)
                        .frame(width: 42, height: 42)
                        .background(Circle().fill(sendEnabled || model.isSending ? Theme.teal : .white.opacity(0.25)))
                }
                .disabled(!sendEnabled && !model.isSending)
                .accessibilityLabel(model.isSending ? "Stop" : "Send")
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
        }
        .padding(.top, 6)
    }

    private var sendEnabled: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func submit() {
        guard sendEnabled, !model.isSending else { return }
        model.send(draft)
        draft = ""
    }
}

private struct EmptyChat: View {
    @Environment(AppModel.self) private var model
    var onSuggestion: (String) -> Void

    private var suggestions: [String] {
        switch model.engine {
        case .mac: ["What's on my calendar today?", "Catch me up on unread email", "Remind me to call Mom at 6"]
        case .cloud, .onDevice: ["Help me plan a focused afternoon", "Explain this simply: compound interest", "Draft a polite reschedule note"]
        }
    }

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            BreathingOrb(active: model.isSending, diameter: 120)
            Text("Hi, I'm Wisp")
                .font(.title2.weight(.semibold))
            Label(model.engine.privacyLine, systemImage: model.engine.symbol)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.7))
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            VStack(spacing: 10) {
                ForEach(suggestions, id: \.self) { suggestion in
                    Button { onSuggestion(suggestion) } label: {
                        Text(suggestion)
                            .font(.callout)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 12)
                            .glassCard(cornerRadius: 16)
                    }
                    .buttonStyle(.plain)
                    .disabled(model.readiness != nil)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 8)
            Spacer()
        }
    }
}

private struct MessageRow: View {
    let message: ChatMessage
    let isStreaming: Bool
    let status: String

    var body: some View {
        if message.role == .user {
            HStack {
                Spacer(minLength: 48)
                Text(message.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(LinearGradient(colors: [Theme.indigo.opacity(0.85), Theme.violet.opacity(0.75)],
                                             startPoint: .topLeading, endPoint: .bottomTrailing)))
            }
        } else {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(message.activity, id: \.self) { line in
                    Label(line, systemImage: "sparkle")
                        .font(.caption)
                        .foregroundStyle(Theme.teal.opacity(0.9))
                }
                if message.text.isEmpty && isStreaming {
                    HStack(spacing: 10) {
                        BreathingOrb(active: true, diameter: 26)
                        Text(status.isEmpty ? "Thinking…" : status)
                            .font(.callout)
                            .foregroundStyle(.white.opacity(0.7))
                    }
                } else if !message.text.isEmpty {
                    Text(Self.markdown(message.text))
                        .textSelection(.enabled)
                }
                if let failure = message.failure {
                    Label(failure, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(Theme.danger)
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassCard(cornerRadius: 18)
        }
    }

    private static func markdown(_ text: String) -> AttributedString {
        (try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }
}

private struct ApprovalCard: View {
    @Environment(AppModel.self) private var model
    let request: ApprovalRequest

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Wisp wants to \(ToolNames.friendly(request.tool))", systemImage: "hand.raised")
                .font(.headline)
            if !request.reason.isEmpty {
                Text(request.reason).font(.callout)
            }
            if !request.preview.isEmpty {
                ScrollView {
                    Text(request.preview)
                        .font(.callout.monospaced())
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(maxHeight: 140)
                .padding(10)
                .background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
            }
            HStack {
                Button("Deny", role: .cancel) { model.answer(request, approved: false) }
                    .buttonStyle(.bordered)
                Spacer()
                if request.grantable {
                    Button("Always allow") { model.answer(request, approved: true, scope: .always) }
                        .buttonStyle(.bordered)
                }
                Button("Allow") { model.answer(request, approved: true) }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.teal)
            }
        }
        .padding(16)
        .glassCard(cornerRadius: 20, tint: Theme.teal)
    }
}

struct EngineSwitcher: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Menu {
            ForEach(EngineKind.allCases) { kind in
                Button { model.select(kind) } label: {
                    Label(kind.title, systemImage: kind == model.engine ? "checkmark" : kind.symbol)
                }
            }
        } label: {
            HStack(spacing: 6) {
                Circle().fill(dotColor).frame(width: 7, height: 7)
                Image(systemName: model.engine.symbol).font(.footnote)
                Text(model.engine.title).font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down").font(.caption2.weight(.bold)).opacity(0.6)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .glassCard(cornerRadius: 14)
        }
        .accessibilityLabel("Wisp runs on \(model.engine.title)")
    }

    private var dotColor: Color {
        if model.readiness != nil { return Theme.danger }
        if model.engine == .mac, case .checking = model.macStatus { return .yellow }
        return Theme.teal
    }
}
