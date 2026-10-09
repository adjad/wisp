import SwiftUI

/// Everything the Chat window needs from the Wisp service. The live
/// implementation talks to the local backend; the QA harness supplies a
/// fixture so the window can be rendered without one.
@MainActor
protocol ChatBackend: AnyObject {
    func listChats() async -> [ChatSummary]?
    func loadChat(id: String) async -> [ChatMessage]?
    func deleteChat(id: String) async -> Bool
    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async
    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async
    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String)
    /// True when Wisp may act without asking; nil when the service is unreachable.
    func fullAccess() async -> Bool?
    func setFullAccess(_ on: Bool) async -> Bool?
}

/// One conversation in the sidebar and the window.
@MainActor
final class ChatConversation: ObservableObject, Identifiable {
    let id = UUID()
    /// The backend session. Empty until the first reply opens one.
    @Published var sessionId: String
    @Published var title: String
    @Published var preview: String
    @Published var lastUsed: Date
    @Published var messages: [ChatMessage] = []
    @Published var loaded: Bool
    @Published var loading = false
    @Published var busy = false
    /// Text to put back in the composer after a dropped request.
    @Published var restoredInput: String?

    // Per-run streaming state, outside the published surface.
    fileprivate var task: Task<Void, Never>?
    fileprivate var pendingDelta = ""
    fileprivate var flushScheduled = false
    fileprivate var streamStart: Date?
    fileprivate var streamChars = 0
    /// Identifies the reply being streamed, so late events from a stopped or
    /// replaced run cannot touch the next one.
    fileprivate var runID = UUID()

    init(sessionId: String = "", title: String = "New chat", preview: String = "",
         lastUsed: Date = Date(), loaded: Bool = true) {
        self.sessionId = sessionId; self.title = title; self.preview = preview
        self.lastUsed = lastUsed; self.loaded = loaded
    }

    var isEmpty: Bool { messages.isEmpty }
    var latestAssistant: ChatMessage? { messages.last(where: { $0.role == .assistant }) }
}

@MainActor
final class ChatStore: ObservableObject {
    @Published private(set) var conversations: [ChatConversation] = []
    @Published var selected: ChatConversation?
    @Published var inspectedMessage: UUID?
    @Published var showInspector = false
    @Published var search = ""
    @Published private(set) var reachable = true
    @Published private(set) var fullAccess = false
    @Published private(set) var loadingList = false

    let backend: ChatBackend
    private let debug: () -> Bool
    /// Chats deleted this session, so a list fetched before the delete finished cannot bring one back.
    private var deletedSessionIds = Set<String>()

    init(backend: ChatBackend, debug: @escaping () -> Bool = { false }) {
        self.backend = backend
        self.debug = debug
    }

    // MARK: - List

    /// Grouped for the sidebar, newest first, narrowed by the search field.
    var sections: [(title: String, items: [ChatConversation])] {
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        let visible = conversations
            .filter { !$0.isEmpty || !$0.sessionId.isEmpty || $0 === selected }
            .filter { query.isEmpty || $0.title.lowercased().contains(query) || $0.preview.lowercased().contains(query) }
            .sorted { $0.lastUsed > $1.lastUsed }
        return ChatGrouping.order.compactMap { name in
            let items = visible.filter { ChatGrouping.section(for: $0.lastUsed) == name }
            return items.isEmpty ? nil : (name, items)
        }
    }

    func refresh() async {
        loadingList = true
        defer { loadingList = false }
        async let access = backend.fullAccess()
        guard let rows = await backend.listChats() else {
            reachable = false
            return
        }
        reachable = true
        if let a = await access { fullAccess = a }
        var byId = Dictionary(conversations.compactMap { c in c.sessionId.isEmpty ? nil : (c.sessionId, c) },
                              uniquingKeysWith: { first, _ in first })
        for row in rows where !deletedSessionIds.contains(row.id) {
            if let existing = byId[row.id] {
                // A reply streaming right now owns its own title and preview.
                if !existing.busy {
                    existing.title = row.title; existing.preview = row.preview; existing.lastUsed = row.lastUsed
                }
            } else {
                let fresh = ChatConversation(sessionId: row.id, title: row.title, preview: row.preview,
                                             lastUsed: row.lastUsed, loaded: false)
                conversations.append(fresh)
                byId[row.id] = fresh
            }
        }
        // Chats the service no longer lists (deleted elsewhere) go away unless
        // they hold something the user is looking at or typing into.
        let listed = Set(rows.map(\.id))
        conversations.removeAll { c in
            !c.sessionId.isEmpty && !listed.contains(c.sessionId) && !c.busy && c.loaded && c !== selected && c.messages.isEmpty
        }
    }

    // MARK: - Selection

    func select(_ conversation: ChatConversation) {
        selected = conversation
        inspectedMessage = nil
        Task { await loadHistoryIfNeeded(conversation) }
    }

    /// Open a saved chat by its backend session id (the notch's hand-off).
    func open(sessionId: String) async {
        if sessionId.isEmpty { newChat(); return }
        deletedSessionIds.remove(sessionId)   // the notch is still using it, so it is not gone
        if conversations.first(where: { $0.sessionId == sessionId }) == nil { await refresh() }
        if let found = conversations.first(where: { $0.sessionId == sessionId }) {
            select(found)
        } else {
            let placeholder = ChatConversation(sessionId: sessionId, title: "Chat", loaded: false)
            conversations.append(placeholder)
            select(placeholder)
        }
    }

    func loadHistoryIfNeeded(_ conversation: ChatConversation) async {
        guard !conversation.loaded, !conversation.loading, !conversation.sessionId.isEmpty else { return }
        conversation.loading = true
        let messages = await backend.loadChat(id: conversation.sessionId)
        conversation.loading = false
        guard let messages else { return }
        // Never replace a conversation the user has already started typing into.
        if !conversation.busy && conversation.messages.isEmpty { conversation.messages = messages }
        conversation.loaded = true
        if conversation.title == "Chat", let first = messages.first(where: { $0.role == .user }) {
            conversation.title = ChatTitle.make(from: first.text)
        }
    }

    @discardableResult
    func newChat() -> ChatConversation {
        if let current = selected, current.isEmpty, current.sessionId.isEmpty { return current }
        let fresh = ChatConversation()
        conversations.insert(fresh, at: 0)
        selected = fresh
        inspectedMessage = nil
        return fresh
    }

    func delete(_ conversation: ChatConversation) {
        let wasBusy = conversation.busy
        stop(conversation)   // also denies a card that is still waiting for an answer
        let id = conversation.sessionId
        conversations.removeAll { $0 === conversation }
        if selected === conversation {
            if let next = conversations.sorted(by: { $0.lastUsed > $1.lastUsed }).first { select(next) } else { selected = nil }
        }
        guard !id.isEmpty else { return }
        deletedSessionIds.insert(id)
        Task {
            // A reply that was streaming is still being wound down by the service,
            // which saves its last turns on the way out. Wait for that to finish, so
            // the delete does not race it and leave the prompt behind as orphaned rows.
            if wasBusy { try? await Task.sleep(nanoseconds: 3_500_000_000) }
            if await backend.deleteChat(id: id) == false {
                // The chat still exists on the service, so it goes back in the list.
                deletedSessionIds.remove(id)
                await refresh()
            }
        }
    }

    // MARK: - Sending

    func send(_ text: String, attachmentName: String? = nil, image: String? = nil) {
        let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }
        let conversation = selected ?? newChat()
        guard !conversation.busy, !conversation.loading else { return }
        if conversation.messages.isEmpty { conversation.title = ChatTitle.make(from: prompt) }
        var user = ChatMessage(role: .user, text: prompt)
        user.attachmentName = attachmentName
        var reply = ChatMessage(role: .assistant, phase: .waiting)
        reply.meta = ChatMeta(startedAt: Date())
        conversation.messages.append(contentsOf: [user, reply])
        conversation.lastUsed = Date()
        conversation.busy = true
        conversation.pendingDelta = ""; conversation.flushScheduled = false
        conversation.streamStart = nil; conversation.streamChars = 0
        let replyID = reply.id
        let runID = UUID()
        conversation.runID = runID
        let sid = conversation.sessionId
        // The Chat window never shows debug records, and the service streams them
        // in full (about 1.6 MB a turn), so ask for none.
        let wantsDebug = false
        conversation.task = Task { [weak self, weak conversation] in
            guard let self else { return }
            await self.backend.run(prompt: prompt, image: image, sessionId: sid, debug: wantsDebug) { event in
                Task { @MainActor in
                    guard let conversation else { return }
                    self.handle(event, in: conversation, replyID: replyID, runID: runID)
                }
            }
        }
    }

    /// Stop the reply in flight. The text so far stays; a pending approval is denied.
    func stop(_ conversation: ChatConversation) {
        guard conversation.busy else { return }
        conversation.task?.cancel()
        conversation.task = nil
        flush(conversation)
        conversation.runID = UUID()
        if let i = conversation.messages.lastIndex(where: { $0.role == .assistant }) {
            if conversation.messages[i].approval?.state == .pending {
                resolve(conversation, messageID: conversation.messages[i].id, approved: false, scope: "once")
            }
            conversation.messages[i].phase = .done
            conversation.messages[i].meta?.finishedAt = Date()
        }
        conversation.busy = false
    }

    private func handle(_ event: ChatEvent, in conversation: ChatConversation, replyID: UUID, runID: UUID) {
        guard conversation.busy, conversation.runID == runID else { return }
        guard conversation.messages.contains(where: { $0.id == replyID }) else {
            conversation.busy = false     // the reply is gone; do not leave the chat stuck
            return
        }
        if event.type == "delta" {
            if conversation.streamStart == nil { conversation.streamStart = Date() }
            conversation.pendingDelta += event.str("text")
            conversation.streamChars += event.str("text").count
            scheduleFlush(conversation, replyID: replyID)
            return
        }
        flush(conversation)
        guard let i = conversation.messages.firstIndex(where: { $0.id == replyID }) else { return }
        var message = conversation.messages[i]
        let outcome = ChatTurnReducer.apply(event, to: &message)
        if let start = conversation.streamStart, message.meta != nil {
            let rate = ChatTurnReducer.tokensPerSecond(chars: conversation.streamChars, since: start)
            if rate > 0 { message.meta?.tokensPerSecond = rate }
        }
        switch outcome {
        case .none:
            conversation.messages[i] = message
        case .session(let id):
            if conversation.sessionId.isEmpty { conversation.sessionId = id }
        case .finished, .failed:
            if message.approval?.state == .pending { message.approval?.state = .timedOut }
            conversation.messages[i] = message
            conversation.busy = false
            conversation.lastUsed = Date()
            conversation.preview = message.text.split(whereSeparator: { $0.isNewline }).first.map(String.init) ?? ""
            Task { await self.refresh() }
        case .droppedEmpty:
            // Nothing arrived: take the exchange back and hand the prompt to the composer.
            if i > 0, conversation.messages[i - 1].role == .user {
                conversation.restoredInput = conversation.messages[i - 1].text
                conversation.messages.removeSubrange((i - 1)...i)
            }
            conversation.busy = false
        }
    }

    private func scheduleFlush(_ conversation: ChatConversation, replyID: UUID) {
        guard !conversation.flushScheduled else { return }
        conversation.flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self, weak conversation] in
            guard let self, let conversation else { return }
            self.flush(conversation)
        }
    }

    /// Fold buffered streamed text into the reply (at most one UI update per ~60 ms).
    private func flush(_ conversation: ChatConversation) {
        conversation.flushScheduled = false
        guard !conversation.pendingDelta.isEmpty,
              let i = conversation.messages.lastIndex(where: { $0.role == .assistant }) else { return }
        var message = conversation.messages[i]
        _ = ChatTurnReducer.apply(ChatEvent(type: "delta", payload: ["text": conversation.pendingDelta]), to: &message)
        conversation.pendingDelta = ""
        if let start = conversation.streamStart {
            let rate = ChatTurnReducer.tokensPerSecond(chars: conversation.streamChars, since: start)
            if rate > 0 { message.meta?.tokensPerSecond = rate }
        }
        conversation.messages[i] = message
    }

    // MARK: - Approvals and drafts

    func resolve(_ conversation: ChatConversation, messageID: UUID, approved: Bool, scope: String) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              conversation.busy,
              var approval = conversation.messages[i].approval, approval.state == .pending else { return }
        approval.state = approved ? .allowed : .denied
        conversation.messages[i].approval = approval
        conversation.messages[i].phase = .working
        let sid = conversation.sessionId
        Task { await backend.approve(sessionId: sid, actionId: approval.actionId, approved: approved,
                                     scope: scope, requestId: approval.requestId) }
    }

    func updateDraft(_ conversation: ChatConversation, messageID: UUID, _ change: (inout ChatDraft) -> Void) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              var draft = conversation.messages[i].draft else { return }
        change(&draft)
        conversation.messages[i].draft = draft
    }

    func sendDraft(_ conversation: ChatConversation, messageID: UUID) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              let draft = conversation.messages[i].draft, !draft.isSending, !draft.sent, !draft.discarded else { return }
        guard !draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            updateDraft(conversation, messageID: messageID) { $0.status = "The message is empty." }
            return
        }
        updateDraft(conversation, messageID: messageID) { $0.isEditing = false; $0.isSending = true; $0.status = "Sending…" }
        Task {
            let outcome = await backend.sendDraft(to: draft.to, text: draft.text)
            updateDraft(conversation, messageID: messageID) {
                $0.isSending = false; $0.sent = outcome.ok
                $0.status = outcome.ok ? "Sent"
                    : outcome.result.contains("could not reach") ? outcome.result + " It may have sent, so check Messages before trying again."
                    : outcome.result
            }
        }
    }

    // MARK: - Access

    func setFullAccess(_ on: Bool) {
        Task { if let result = await backend.setFullAccess(on) { fullAccess = result } }
    }
}
