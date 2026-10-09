import SwiftUI
import AppKit

/// What the sidebar's shortcuts open. These are the app's existing windows;
/// the Chat window launches them rather than reimplementing them.
struct ChatLaunchers {
    var today: () -> Void = {}
    var research: () -> Void = {}
    var memory: () -> Void = {}
    var settings: () -> Void = {}
    var searchEverything: () -> Void = {}
}

// MARK: - Root

struct ChatRootView: View {
    @ObservedObject var store: ChatStore
    let launchers: ChatLaunchers
    @State private var sidebarVisible = true
    @FocusState private var searchFocused: Bool

    var body: some View {
        HStack(spacing: 0) {
            if sidebarVisible {
                ChatSidebar(store: store, launchers: launchers, searchFocused: $searchFocused)
                    .frame(width: 268)
                    .transition(.move(edge: .leading).combined(with: .opacity))
                Rectangle().fill(ChatTheme.line).frame(width: 1)
            }
            ChatMainColumn(store: store, sidebarVisible: $sidebarVisible)
            if store.showInspector {
                Rectangle().fill(ChatTheme.line).frame(width: 1)
                ChatInspector(store: store)
                    .frame(width: 300)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
        .background(ChatAurora())
        .animation(.easeOut(duration: 0.22), value: sidebarVisible)
        .animation(.easeOut(duration: 0.22), value: store.showInspector)
        .background(shortcuts)
        .task { await store.refresh() }
    }

    /// Window-wide shortcuts, as invisible buttons so they work wherever focus is.
    private var shortcuts: some View {
        ZStack {
            Button("") { store.newChat() }.keyboardShortcut("n", modifiers: .command)
            Button("") { store.showInspector.toggle() }.keyboardShortcut("i", modifiers: .command)
            Button("") { sidebarVisible.toggle() }.keyboardShortcut("b", modifiers: .command)
            Button("") { sidebarVisible = true; searchFocused = true }.keyboardShortcut("k", modifiers: .command)
        }
        .opacity(0)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - Sidebar

struct ChatSidebar: View {
    @ObservedObject var store: ChatStore
    let launchers: ChatLaunchers
    var searchFocused: FocusState<Bool>.Binding

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ChatOrb(size: 22)
                Text("Wisp").font(.system(size: 15, weight: .medium)).foregroundStyle(ChatTheme.text)
            }
            .padding(.top, 44).padding(.horizontal, 20).padding(.bottom, 14)

            Button { store.newChat() } label: {
                HStack(spacing: 9) {
                    Image(systemName: "plus").font(.system(size: 12, weight: .semibold))
                    Text("New chat").font(.system(size: 13.5, weight: .medium))
                    Spacer()
                    Text("⌘N").font(.system(size: 11, design: .monospaced)).foregroundStyle(ChatTheme.text3)
                }
                .foregroundStyle(ChatTheme.text)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 10).fill(ChatTheme.glass2))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(ChatTheme.line, lineWidth: 1))
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12).padding(.bottom, 8)

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundStyle(ChatTheme.text3)
                TextField("Search chats", text: $store.search)
                    .textFieldStyle(.plain).font(.system(size: 13))
                    .foregroundStyle(ChatTheme.text)
                    .focused(searchFocused)
                if !store.search.isEmpty {
                    Button { store.search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(ChatTheme.text3) }
                        .buttonStyle(.plain).accessibilityLabel("Clear search")
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 10).fill(ChatTheme.glass))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(ChatTheme.line, lineWidth: 1))
            .padding(.horizontal, 12).padding(.bottom, 10)

            VStack(spacing: 2) {
                ChatNavRow(symbol: "sun.max", title: "Today", action: launchers.today)
                ChatNavRow(symbol: "books.vertical", title: "Research", action: launchers.research)
                ChatNavRow(symbol: "brain", title: "Memory", action: launchers.memory)
                ChatNavRow(symbol: "sparkle.magnifyingglass", title: "Search everything", hint: "⇧⌘F", action: launchers.searchEverything)
            }
            .padding(.horizontal, 8)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1, pinnedViews: []) {
                    if store.sections.isEmpty {
                        Text(store.search.isEmpty ? (store.reachable ? "No chats yet." : "Wisp isn't running.") : "No matches.")
                            .font(.system(size: 12.5)).foregroundStyle(ChatTheme.text3)
                            .padding(.horizontal, 12).padding(.top, 14)
                    }
                    ForEach(store.sections, id: \.title) { section in
                        Text(section.title.uppercased())
                            .font(.system(size: 10.5, weight: .medium)).tracking(1.0)
                            .foregroundStyle(ChatTheme.text3)
                            .padding(.horizontal, 12).padding(.top, 14).padding(.bottom, 4)
                        ForEach(section.items) { conversation in
                            ChatHistoryRow(conversation: conversation, isSelected: store.selected === conversation,
                                           select: { store.select(conversation) },
                                           delete: { store.delete(conversation) })
                        }
                    }
                }
                .padding(.horizontal, 8).padding(.top, 6).padding(.bottom, 12)
            }

            Rectangle().fill(ChatTheme.line).frame(height: 1)
            ChatSidebarFooter(store: store, launchers: launchers)
        }
        .background(ChatTheme.sidebar)
    }
}

private struct ChatNavRow: View {
    let symbol: String
    let title: String
    var hint: String? = nil
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: symbol).font(.system(size: 13)).frame(width: 18)
                Text(title).font(.system(size: 13.5))
                Spacer()
                if let hint { Text(hint).font(.system(size: 11, design: .monospaced)).foregroundStyle(ChatTheme.text3) }
            }
            .foregroundStyle(ChatTheme.text2)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? ChatTheme.glass2 : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

private struct ChatHistoryRow: View {
    @ObservedObject var conversation: ChatConversation
    let isSelected: Bool
    let select: () -> Void
    let delete: () -> Void
    @State private var hovering = false
    @State private var confirmDelete = false

    var body: some View {
        Button(action: select) {
            Text(conversation.title)
                .font(.system(size: 13.5)).lineLimit(1)
                .foregroundStyle(isSelected ? ChatTheme.text : ChatTheme.text2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 8).fill(isSelected ? ChatTheme.glass2 : (hovering ? ChatTheme.glass : .clear)))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .contextMenu { Button("Delete chat…", role: .destructive) { confirmDelete = true } }
        .confirmationDialog("Delete this chat?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete chat", role: .destructive, action: delete)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("\"\(conversation.title)\" and its history will be removed from this Mac.")
        }
        .accessibilityLabel(conversation.title)
    }
}

private struct ChatSidebarFooter: View {
    @ObservedObject var store: ChatStore
    let launchers: ChatLaunchers

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 7) {
                    Circle().fill(store.reachable ? ChatTheme.ok : ChatTheme.amber).frame(width: 7, height: 7)
                    Text(store.reachable ? status : "Wisp isn't running")
                        .font(.system(size: 12, design: .monospaced)).foregroundStyle(ChatTheme.text2).lineLimit(1)
                }
                Text(store.reachable ? "This Mac · all local" : "Start Wisp from the menu bar")
                    .font(.system(size: 12, design: .monospaced)).foregroundStyle(ChatTheme.text3)
            }
            Spacer()
            Button(action: launchers.settings) { Image(systemName: "slider.horizontal.3") }
                .buttonStyle(ChatIconButtonStyle()).help("Settings").accessibilityLabel("Settings")
        }
        .padding(.horizontal, 16).padding(.vertical, 11)
    }

    private var status: String {
        guard let meta = store.selected?.latestAssistant?.meta, !meta.model.isEmpty else { return "Ready" }
        let speed = meta.tokensPerSecond > 0 ? " · \(meta.tokensPerSecond) tok/s" : ""
        return meta.shortModel + speed
    }
}

struct ChatIconButtonStyle: ButtonStyle {
    var active = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13))
            .foregroundStyle(active ? ChatTheme.teal : ChatTheme.text2)
            .frame(width: 30, height: 30)
            .background(RoundedRectangle(cornerRadius: 8).fill(active ? ChatTheme.tealWash : (configuration.isPressed ? ChatTheme.glass2 : .clear)))
            .contentShape(Rectangle())
    }
}

// MARK: - Main column

struct ChatMainColumn: View {
    @ObservedObject var store: ChatStore
    @Binding var sidebarVisible: Bool

    var body: some View {
        VStack(spacing: 0) {
            topBar
            Rectangle().fill(ChatTheme.line).frame(height: 1)
            if let conversation = store.selected {
                ChatConversationView(store: store, conversation: conversation)
                    .id(conversation.id)
            } else {
                ChatWelcome(store: store)
            }
        }
        .frame(maxWidth: .infinity)
    }

    private var topBar: some View {
        HStack(spacing: 8) {
            if !sidebarVisible { Spacer().frame(width: 62) }   // clear the window's traffic lights
            Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                .buttonStyle(ChatIconButtonStyle()).help("Show or hide the sidebar (⌘B)").accessibilityLabel("Toggle sidebar")
            ChatTitleLabel(store: store)
            Spacer()
            ChatPill(text: "Local", tinted: true)
            Button { store.showInspector.toggle() } label: { Image(systemName: "sidebar.right") }
                .buttonStyle(ChatIconButtonStyle(active: store.showInspector))
                .help("Details about the answer (⌘I)").accessibilityLabel("Toggle details")
        }
        .padding(.leading, 14).padding(.trailing, 16)
        .frame(height: 52)
        .padding(.top, 0)
    }
}

private struct ChatTitleLabel: View {
    @ObservedObject var store: ChatStore
    var body: some View {
        if let conversation = store.selected {
            TitleText(conversation: conversation)
        } else {
            Text("New chat").font(.system(size: 14.5, weight: .medium)).foregroundStyle(ChatTheme.text3)
        }
    }
    private struct TitleText: View {
        @ObservedObject var conversation: ChatConversation
        var body: some View {
            Text(conversation.isEmpty ? "New chat" : conversation.title)
                .font(.system(size: 14.5, weight: .medium))
                .foregroundStyle(conversation.isEmpty ? ChatTheme.text3 : ChatTheme.text)
                .lineLimit(1)
        }
    }
}

struct ChatPill: View {
    let text: String
    var tinted = false
    var body: some View {
        Text(text)
            .font(.system(size: 11.5, weight: .medium, design: .monospaced))
            .foregroundStyle(tinted ? ChatTheme.teal : ChatTheme.text2)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(tinted ? ChatTheme.tealWash : ChatTheme.glass))
            .overlay(Capsule().stroke(tinted ? ChatTheme.tealLine : ChatTheme.line, lineWidth: 1))
    }
}

// MARK: - Welcome

struct ChatWelcome: View {
    @ObservedObject var store: ChatStore
    var body: some View {
        VStack(spacing: 0) {
            ChatEmptyState { store.newChat(); store.send($0) }
            ChatComposer(store: store, busy: false, loading: false, restoredInput: .constant(nil), stop: {})
        }
    }
}

struct ChatEmptyState: View {
    let ask: (String) -> Void
    private let suggestions = ["Catch me up on today", "What's on my calendar this week?", "Summarize my unread mail",
                               "Draft a reply to my last email", "Find a file on my Mac"]
    var body: some View {
        VStack(spacing: 18) {
            Spacer(minLength: 24)
            ChatOrb(size: 76)
            Text("What are we working on?")
                .font(.system(size: 34, weight: .regular, design: .serif)).foregroundStyle(ChatTheme.text)
            Text("Everything stays on this Mac.").font(.system(size: 14)).foregroundStyle(ChatTheme.text2)
            ChatFlowLayout(spacing: 8) {
                ForEach(suggestions, id: \.self) { s in
                    Button { ask(s) } label: {
                        Text(s).font(.system(size: 13.5)).foregroundStyle(ChatTheme.text2)
                            .padding(.horizontal, 14).padding(.vertical, 7)
                            .background(Capsule().fill(ChatTheme.glass))
                            .overlay(Capsule().stroke(ChatTheme.line2, lineWidth: 1))
                    }.buttonStyle(.plain)
                }
            }
            .frame(maxWidth: 560)
            Spacer(minLength: 24)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
    }
}

/// Wrapping row of chips.
struct ChatFlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(proposal.width ?? 600, subviews).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = arrange(bounds.width, subviews)
        for (i, frame) in result.frames.enumerated() {
            subviews[i].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                              proposal: ProposedViewSize(frame.size))
        }
    }

    private func arrange(_ width: CGFloat, _ subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        var rows: [[(Int, CGSize)]] = [[]]
        var x: CGFloat = 0
        for (i, sub) in subviews.enumerated() {
            let size = sub.sizeThatFits(.unspecified)
            if x + size.width > width, !rows[rows.count - 1].isEmpty { rows.append([]); x = 0 }
            rows[rows.count - 1].append((i, size))
            x += size.width + spacing
        }
        var frames = [CGRect](repeating: .zero, count: subviews.count)
        var y: CGFloat = 0
        var maxWidth: CGFloat = 0
        for row in rows {
            let rowWidth = row.reduce(0) { $0 + $1.1.width } + spacing * CGFloat(max(0, row.count - 1))
            var cursor = (width - rowWidth) / 2      // chips are centered
            let height = row.map(\.1.height).max() ?? 0
            for (i, size) in row {
                frames[i] = CGRect(x: cursor, y: y, width: size.width, height: size.height)
                cursor += size.width + spacing
            }
            maxWidth = max(maxWidth, rowWidth)
            y += height + spacing
        }
        return (CGSize(width: max(maxWidth, 0), height: max(0, y - spacing)), frames)
    }
}

// MARK: - Conversation

struct ChatConversationView: View {
    @ObservedObject var store: ChatStore
    @ObservedObject var conversation: ChatConversation

    var body: some View {
        VStack(spacing: 0) {
            if conversation.messages.isEmpty {
                if conversation.loading {
                    VStack { Spacer(); ProgressView().controlSize(.small); Spacer() }.frame(maxWidth: .infinity)
                } else {
                    ChatEmptyState { store.send($0) }
                }
            } else {
                ChatThread(store: store, conversation: conversation)
            }
            ChatComposer(store: store, busy: conversation.busy, loading: conversation.loading,
                         restoredInput: Binding(get: { conversation.restoredInput }, set: { conversation.restoredInput = $0 }),
                         stop: { store.stop(conversation) })
        }
    }
}

struct ChatThread: View {
    @ObservedObject var store: ChatStore
    @ObservedObject var conversation: ChatConversation
    /// Whether the reader is at the bottom of the thread. The thread follows a growing
    /// answer only while this is true, so scrolling up to read is never pulled back down.
    @State private var followsBottom = true
    /// Until this time a settling scroll is our own (the animated jump to the bottom),
    /// so the reader has not moved away and following must not be switched off.
    @State private var ownScrollUntil = Date.distantPast

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 24) {
                    ForEach(conversation.messages) { message in
                        ChatMessageRow(store: store, conversation: conversation, message: message)
                            .id(message.id)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .frame(maxWidth: 740)
                .padding(.horizontal, 24).padding(.top, 26).padding(.bottom, 12)
                .frame(maxWidth: .infinity)
            }
            .modifier(ChatScrollBehavior(followsBottom: $followsBottom, ownScrollUntil: ownScrollUntil))
            .onChange(of: conversation.messages.count) { _, _ in
                // A new exchange (the reader sent something, or a chat just loaded) shows the bottom.
                followsBottom = true
                ownScrollUntil = Date().addingTimeInterval(0.6)
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onChange(of: conversation.messages.last?.text) { _, _ in follow(proxy) }
            .onChange(of: conversation.messages.last?.tools.count) { _, _ in follow(proxy) }
            .onChange(of: conversation.messages.last?.phase) { _, _ in follow(proxy) }
            .onChange(of: conversation.messages.last?.approval) { _, _ in follow(proxy) }
            .onChange(of: conversation.messages.last?.draft) { _, _ in follow(proxy) }
        }
    }

    private func follow(_ proxy: ScrollViewProxy) {
        if followsBottom { proxy.scrollTo("bottom", anchor: .bottom) }
    }
}

/// Starts the thread at the bottom and tracks whether the reader is still there.
/// Content growth alone never moves a reader who has scrolled up: from macOS 15 the
/// size-change anchoring is off and the thread follows only while `followsBottom`.
/// On macOS 14 the thread keeps the system's bottom anchoring and follows always.
private struct ChatScrollBehavior: ViewModifier {
    @Binding var followsBottom: Bool
    let ownScrollUntil: Date

    func body(content: Content) -> some View {
        if #available(macOS 15.0, *) {
            content
                .defaultScrollAnchor(.bottom, for: .initialOffset)
                .onScrollPhaseChange { _, newPhase, context in
                    switch newPhase {
                    case .tracking, .interacting, .decelerating:
                        followsBottom = false      // the reader is scrolling: stop following
                    case .idle:
                        // Settled after our own jump to the bottom: keep following.
                        if Date() < ownScrollUntil { followsBottom = true; break }
                        let g = context.geometry   // otherwise follow again only if back at the bottom
                        followsBottom = g.visibleRect.maxY >= g.contentSize.height - 48
                    default:
                        break                      // our own animated scroll
                    }
                }
        } else {
            content.defaultScrollAnchor(.bottom)
        }
    }
}

struct ChatMessageRow: View {
    @ObservedObject var store: ChatStore
    @ObservedObject var conversation: ChatConversation
    let message: ChatMessage

    var body: some View {
        switch message.role {
        case .user: userBubble
        case .assistant: assistant
        }
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 80)
            VStack(alignment: .trailing, spacing: 4) {
                if let name = message.attachmentName {
                    Label(name, systemImage: "photo").font(.system(size: 12, design: .monospaced)).foregroundStyle(ChatTheme.text2)
                }
                Text(message.text)
                    .font(.system(size: 15)).foregroundStyle(ChatTheme.text)
                    .textSelection(.enabled)
            }
            .padding(.horizontal, 15).padding(.vertical, 9)
            .background(UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: 18, bottomTrailingRadius: 5, topTrailingRadius: 18).fill(ChatTheme.bubble))
            .overlay(UnevenRoundedRectangle(topLeadingRadius: 18, bottomLeadingRadius: 18, bottomTrailingRadius: 5, topTrailingRadius: 18).stroke(ChatTheme.line, lineWidth: 1))
        }
    }

    private var assistant: some View {
        HStack(alignment: .top, spacing: 14) {
            ChatOrb(size: 20, thinking: message.isLive).padding(.top, 2)
            VStack(alignment: .leading, spacing: 12) {
                if message.isLive && message.text.isEmpty && message.tools.isEmpty && message.approval == nil && message.draft == nil {
                    ChatThinkingRow(label: message.status.isEmpty ? "Thinking…" : message.status)
                }
                if !message.tools.isEmpty { ChatTraceView(message: message) }
                if !message.reasoning.isEmpty { ChatReasoningView(text: message.reasoning) }
                if !message.text.isEmpty {
                    if message.isError {
                        Text(message.text).font(.system(size: 14.5)).foregroundStyle(ChatTheme.danger)
                            .textSelection(.enabled)
                    } else {
                        MarkdownView(text: message.text)
                            .environment(\.markdownPalette, ChatTheme.markdown)
                    }
                }
                if let approval = message.approval {
                    ChatApprovalCard(approval: approval) { approved, scope in
                        store.resolve(conversation, messageID: message.id, approved: approved, scope: scope)
                    }
                }
                if message.draft != nil {
                    ChatDraftCard(message: message, store: store, conversation: conversation)
                }
                if message.phase == .streaming && message.status.isEmpty == false {
                    Text(message.status).font(.system(size: 12)).foregroundStyle(ChatTheme.text3)
                }
                if message.phase == .done || message.phase == .failed, let meta = message.meta, !meta.model.isEmpty {
                    ChatMetaLine(meta: meta, selected: store.inspectedMessage == message.id) {
                        store.inspectedMessage = message.id
                        store.showInspector = true
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct ChatThinkingRow: View {
    let label: String
    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(label).font(.system(size: 13.5)).foregroundStyle(ChatTheme.text3)
        }
    }
}

private struct ChatMetaLine: View {
    let meta: ChatMeta
    let selected: Bool
    let action: () -> Void

    private var text: String {
        var parts: [String] = []
        if !meta.roleLabel.isEmpty { parts.append(meta.roleLabel) }
        parts.append(meta.shortModel)
        if let d = meta.duration { parts.append(String(format: "%.1f s", d)) }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Text(text).font(.system(size: 11.5, weight: .medium, design: .monospaced))
                Image(systemName: "info.circle").font(.system(size: 10.5))
            }
            .foregroundStyle(selected ? ChatTheme.teal : ChatTheme.text3)
        }
        .buttonStyle(.plain)
        .help("Show details for this answer")
    }
}

// MARK: - Tool trace

struct ChatTraceView: View {
    let message: ChatMessage
    @State private var expanded = false

    private var isOpen: Bool { expanded || message.isLive }
    private var title: String {
        let n = message.tools.count
        let running = message.tools.contains { !$0.finished && $0.decision != "deny" }
        if running { return "Working with \(n) tool\(n == 1 ? "" : "s")…" }
        return "Used \(n) tool\(n == 1 ? "" : "s")"
    }

    var body: some View {
        VStack(spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 8) {
                    Image(systemName: "sparkle").font(.system(size: 12))
                    Text(title).font(.system(size: 13))
                    Spacer()
                    if let d = message.meta?.duration, !message.isLive {
                        Text(String(format: "%.1f s", d)).font(.system(size: 11.5, design: .monospaced)).foregroundStyle(ChatTheme.text3)
                    }
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                        .rotationEffect(.degrees(isOpen ? 90 : 0)).foregroundStyle(ChatTheme.text3)
                }
                .foregroundStyle(ChatTheme.text2)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if isOpen {
                ForEach(message.tools) { tool in
                    Rectangle().fill(ChatTheme.line).frame(height: 1)
                    HStack(spacing: 10) {
                        Image(systemName: tool.kind.symbol).font(.system(size: 12)).foregroundStyle(ChatTheme.blue).frame(width: 16)
                        Text(tool.title).font(.system(size: 13)).foregroundStyle(ChatTheme.text2).lineLimit(1)
                        Spacer(minLength: 8)
                        Text(tool.detail).font(.system(size: 11.5, design: .monospaced))
                            .foregroundStyle(tool.decision == "deny" ? ChatTheme.danger : ChatTheme.text3).lineLimit(1)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 12).fill(ChatTheme.glass))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(ChatTheme.line, lineWidth: 1))
        .animation(.easeOut(duration: 0.18), value: isOpen)
    }
}

private struct ChatReasoningView: View {
    let text: String
    @State private var open = false
    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            ScrollView {
                Text(text).font(.system(size: 12.5)).foregroundStyle(ChatTheme.text3)
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 180)
        } label: {
            Text("Show reasoning").font(.system(size: 12.5)).foregroundStyle(ChatTheme.text2)
        }
        .tint(ChatTheme.text2)
    }
}

// MARK: - Approvals and drafts

struct ChatApprovalCard: View {
    let approval: ChatApproval
    let resolve: (_ approved: Bool, _ scope: String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: header.symbol)
                Text(header.text).font(.system(size: 13, weight: .medium)).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .foregroundStyle(header.color)
            .padding(.horizontal, 14).padding(.vertical, 10)
            Rectangle().fill(border).frame(height: 1)
            if !approval.preview.isEmpty {
                ScrollView {
                    Text(approval.preview).font(.system(size: 12, design: .monospaced)).foregroundStyle(ChatTheme.text)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(10)
                }
                .frame(maxHeight: 220)
                .background(RoundedRectangle(cornerRadius: 8).fill(ChatTheme.glass2))
                .padding(12)
            }
            if approval.state == .pending {
                if approval.grantable && !approval.scopeHint.isEmpty {
                    Text("Always allow applies to \(approval.tool) on \(approval.scopeHint)")
                        .font(.system(size: 11.5)).foregroundStyle(ChatTheme.text2).padding(.horizontal, 14).padding(.top, approval.preview.isEmpty ? 12 : 0)
                }
                HStack(spacing: 8) {
                    Spacer()
                    ChatCardButton(title: "Deny") { resolve(false, "once") }
                    if approval.grantable { ChatCardButton(title: "Always allow") { resolve(true, "always") } }
                    ChatCardButton(title: "Allow once", primary: true) { resolve(true, "once") }
                }
                .padding(12)
            }
        }
        .background(RoundedRectangle(cornerRadius: 14).fill(approval.state == .pending ? ChatTheme.amberWash : ChatTheme.glass))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(border, lineWidth: 1))
        .opacity(approval.state == .denied || approval.state == .timedOut ? 0.7 : 1)
    }

    private var border: Color { approval.state == .pending ? ChatTheme.amberLine : ChatTheme.line }
    private var header: (symbol: String, text: String, color: Color) {
        switch approval.state {
        case .pending: return ("exclamationmark.shield", approval.reason, ChatTheme.amber)
        case .allowed: return ("checkmark.circle", "Allowed · \(approval.reason)", ChatTheme.ok)
        case .denied: return ("xmark.circle", "Denied · nothing was changed", ChatTheme.text3)
        case .timedOut: return ("clock", "Timed out · denied automatically", ChatTheme.text3)
        }
    }
}

struct ChatCardButton: View {
    let title: String
    var primary = false
    var enabled = true
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 13, weight: primary ? .semibold : .medium))
                .foregroundStyle(primary ? Color(nsColor: ChatTheme.amberButtonText) : ChatTheme.text2)
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Capsule().fill(primary ? ChatTheme.amberButton : ChatTheme.glass))
                .overlay(Capsule().stroke(primary ? Color.clear : ChatTheme.line2, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.5)
    }
}

struct ChatDraftCard: View {
    let message: ChatMessage
    @ObservedObject var store: ChatStore
    @ObservedObject var conversation: ChatConversation

    private var draft: ChatDraft { message.draft ?? ChatDraft(to: "", text: "") }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: draft.sent ? "checkmark.circle" : "bubble.left")
                Text(draft.sent ? "Message sent" : "Message draft, not sent yet").font(.system(size: 13, weight: .medium))
                Spacer()
                Text("To: \(draft.to)").font(.system(size: 11.5, design: .monospaced)).foregroundStyle(ChatTheme.text2)
            }
            .foregroundStyle(draft.sent ? ChatTheme.ok : ChatTheme.amber)
            .padding(.horizontal, 14).padding(.vertical, 10)
            Rectangle().fill(border).frame(height: 1)
            Group {
                if draft.isEditing {
                    TextEditor(text: Binding(get: { draft.text },
                                             set: { new in store.updateDraft(conversation, messageID: message.id) { $0.text = new } }))
                        .font(.system(size: 14)).foregroundStyle(ChatTheme.text)
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: 96, maxHeight: 220)
                } else {
                    Text(draft.text).font(.system(size: 14)).foregroundStyle(ChatTheme.text)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(14)
            if !draft.status.isEmpty {
                Text(draft.status).font(.system(size: 11.5))
                    .foregroundStyle(draft.sent || draft.isSending ? ChatTheme.text2 : ChatTheme.danger)
                    .padding(.horizontal, 14).padding(.bottom, 10)
            }
            if !draft.sent && !draft.discarded {
                HStack(spacing: 8) {
                    Spacer()
                    ChatCardButton(title: "Discard", enabled: !draft.isSending) {
                        store.updateDraft(conversation, messageID: message.id) { $0.discarded = true; $0.status = "Discarded" }
                    }
                    ChatCardButton(title: draft.isEditing ? "Done" : "Edit", enabled: !draft.isSending) {
                        store.updateDraft(conversation, messageID: message.id) { $0.isEditing.toggle() }
                    }
                    ChatCardButton(title: draft.isSending ? "Sending…" : "Send", primary: true,
                                   enabled: !draft.isSending && !draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                        store.sendDraft(conversation, messageID: message.id)
                    }
                }
                .padding(12)
                .background(ChatTheme.glass2)
            }
        }
        .background(RoundedRectangle(cornerRadius: 14).fill(draft.sent || draft.discarded ? ChatTheme.glass : ChatTheme.amberWash))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(border, lineWidth: 1))
        .opacity(draft.discarded ? 0.6 : 1)
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private var border: Color { draft.sent || draft.discarded ? ChatTheme.line : ChatTheme.amberLine }
}

// MARK: - Composer

struct ChatComposer: View {
    @ObservedObject var store: ChatStore
    let busy: Bool
    let loading: Bool
    @Binding var restoredInput: String?
    let stop: () -> Void
    @State private var text = ""
    @State private var image: (name: String, dataURL: String)?
    @State private var attachNote = ""
    @FocusState private var focused: Bool

    private var canSend: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !busy && !loading }

    var body: some View {
        VStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 8) {
                if let image {
                    HStack(spacing: 8) {
                        Image(systemName: "photo").font(.system(size: 12))
                        Text(image.name).font(.system(size: 12, design: .monospaced)).lineLimit(1)
                        Button { self.image = nil; attachNote = "" } label: { Image(systemName: "xmark").font(.system(size: 10, weight: .semibold)) }
                            .buttonStyle(.plain).accessibilityLabel("Remove image")
                    }
                    .foregroundStyle(ChatTheme.text2)
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(RoundedRectangle(cornerRadius: 9).fill(ChatTheme.glass))
                    .overlay(RoundedRectangle(cornerRadius: 9).stroke(ChatTheme.line, lineWidth: 1))
                }
                TextField("Message Wisp", text: $text, axis: .vertical)
                    .textFieldStyle(.plain)
                    .font(.system(size: 15))
                    .foregroundStyle(ChatTheme.text)
                    .lineLimit(1...8)
                    .focused($focused)
                    .onSubmit(send)
                HStack(spacing: 6) {
                    Button(action: pickImage) { Image(systemName: "paperclip") }
                        .buttonStyle(ChatIconButtonStyle()).help("Attach an image").accessibilityLabel("Attach an image")
                    Menu {
                        Button { store.setFullAccess(false) } label: { Label("Ask before acting", systemImage: store.fullAccess ? "" : "checkmark") }
                        Button { store.setFullAccess(true) } label: { Label("Full access", systemImage: store.fullAccess ? "checkmark" : "") }
                    } label: {
                        ChatPill(text: store.fullAccess ? "Full access" : "Ask before acting")
                    }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .help("Whether Wisp asks before it acts. Sending and bulk deletes always ask.")
                    Spacer()
                    if busy {
                        Button(action: stop) {
                            Image(systemName: "stop.fill").font(.system(size: 11))
                                .foregroundStyle(ChatTheme.sendIcon).frame(width: 32, height: 32)
                                .background(Circle().fill(ChatTheme.sendFill))
                        }
                        .buttonStyle(.plain).help("Stop").accessibilityLabel("Stop")
                    } else {
                        Button(action: send) {
                            Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                                .foregroundStyle(canSend ? ChatTheme.sendIcon : ChatTheme.text3)
                                .frame(width: 32, height: 32)
                                .background(Circle().fill(canSend ? ChatTheme.sendFill : ChatTheme.glass2))
                        }
                        .buttonStyle(.plain).disabled(!canSend).accessibilityLabel("Send")
                    }
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 10)
            .background(RoundedRectangle(cornerRadius: 18).fill(ChatTheme.field))
            .overlay(RoundedRectangle(cornerRadius: 18).stroke(focused ? ChatTheme.tealLine : ChatTheme.line2, lineWidth: 1))
            .frame(maxWidth: 740)
            Text(attachNote.isEmpty ? "Wisp runs on this Mac and can be wrong. Check anything important." : attachNote)
                .font(.system(size: 11)).foregroundStyle(attachNote.isEmpty ? ChatTheme.text3 : ChatTheme.danger)
        }
        .padding(.horizontal, 24).padding(.bottom, 14).padding(.top, 4)
        .frame(maxWidth: .infinity)
        .onAppear {
            focused = true
            if let restored = restoredInput { text = restored; restoredInput = nil }
        }
        .onChange(of: restoredInput) { _, restored in
            guard let restored else { return }
            text = restored
            restoredInput = nil
        }
    }

    private static let maxImageBytes = 10_000_000

    private func send() {
        guard canSend else { return }
        let prompt = text
        let attached = image
        text = ""
        image = nil
        attachNote = ""
        store.send(prompt, attachmentName: attached?.name, image: attached?.dataURL)
    }

    private func pickImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        guard size <= Self.maxImageBytes else {
            attachNote = "That image is over 10 MB. Choose a smaller one."
            return
        }
        guard let data = try? Data(contentsOf: url) else { return }
        attachNote = ""
        let ext = url.pathExtension.lowercased()
        image = (url.lastPathComponent, "data:image/\(ext);base64,\(data.base64EncodedString())")
    }
}

// MARK: - Inspector

struct ChatInspector: View {
    @ObservedObject var store: ChatStore

    var body: some View {
        if let conversation = store.selected {
            InspectorBody(store: store, conversation: conversation)
        } else {
            InspectorShell { Text("Send a message to see how Wisp answered it.").font(.system(size: 13)).foregroundStyle(ChatTheme.text3) }
        }
    }

    private struct InspectorBody: View {
        @ObservedObject var store: ChatStore
        @ObservedObject var conversation: ChatConversation

        private var message: ChatMessage? {
            if let id = store.inspectedMessage, let m = conversation.messages.first(where: { $0.id == id }), m.role == .assistant { return m }
            return conversation.messages.last(where: { $0.role == .assistant && $0.meta != nil && !($0.meta?.model.isEmpty ?? true) })
        }

        var body: some View {
            InspectorShell {
                if let message, let meta = message.meta, !meta.model.isEmpty {
                    section("This answer") {
                        row("Route", meta.roleLabel.isEmpty ? "—" : meta.roleLabel)
                        row("Model", meta.shortModel)
                        row("Routed by", Self.routedBy(meta.routeSource))
                        if meta.tokensPerSecond > 0 { row("Speed", "\(meta.tokensPerSecond) tok/s") }
                        if let d = meta.duration { row("Time", String(format: "%.1f s", d)) }
                        row("Ran on", "This Mac", color: ChatTheme.ok)
                    }
                    if !meta.routeReason.isEmpty {
                        section("Why this route") {
                            Text(meta.routeReason).font(.system(size: 12.5)).foregroundStyle(ChatTheme.text2)
                        }
                    }
                    section("Tools used") {
                        if message.tools.isEmpty {
                            Text("None. Answered from the model alone.").font(.system(size: 12.5)).foregroundStyle(ChatTheme.text3)
                        } else {
                            ForEach(message.tools) { tool in
                                VStack(alignment: .leading, spacing: 2) {
                                    HStack {
                                        Image(systemName: tool.kind.symbol).font(.system(size: 11)).foregroundStyle(ChatTheme.blue)
                                        Text(tool.name).font(.system(size: 12, design: .monospaced)).foregroundStyle(ChatTheme.text2).lineLimit(1)
                                    }
                                    Text(tool.detail).font(.system(size: 11.5)).foregroundStyle(ChatTheme.text3).lineLimit(2)
                                }
                            }
                        }
                    }
                } else {
                    Text("Send a message to see how Wisp answered it: which model, which tools, and how long it took.")
                        .font(.system(size: 13)).foregroundStyle(ChatTheme.text3)
                }
            }
        }

        private static func routedBy(_ source: String) -> String {
            switch source {
            case "air_router": return "Air router"
            case "local_router": return "Local router"
            case "fallback": return "Local fallback"
            case "rules": return "Local rules"
            case "default": return "Default"
            default: return source.isEmpty ? "—" : source
            }
        }

        private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
            VStack(alignment: .leading, spacing: 8) {
                Text(title.uppercased()).font(.system(size: 10.5, weight: .medium)).tracking(1.0).foregroundStyle(ChatTheme.text3)
                content()
            }
        }

        private func row(_ key: String, _ value: String, color: Color = ChatTheme.text2) -> some View {
            HStack(alignment: .firstTextBaseline) {
                Text(key).font(.system(size: 13)).foregroundStyle(ChatTheme.text3)
                Spacer(minLength: 12)
                Text(value).font(.system(size: 12, design: .monospaced)).foregroundStyle(color).multilineTextAlignment(.trailing)
            }
        }
    }

    private struct InspectorShell<Content: View>: View {
        @ViewBuilder var content: Content
        var body: some View {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) { content }
                    .padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.top, 52)
            .background(ChatTheme.inspector)
        }
    }
}
