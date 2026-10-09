import Foundation

// Synthetic contract for ChatStore: sending, streaming, approvals, recovery from
// a dropped request, list merging, and deletion, against a scripted backend.
// No window, no network, no Wisp service.

@MainActor
final class ScriptedBackend: ChatBackend {
    var chats: [ChatSummary]? = []
    /// The next this-many list requests fail, like a service that is still starting.
    var listFailures = 0
    var history: [String: [ChatMessage]] = [:]
    var script: (String) -> [ChatEvent] = { _ in [] }
    var approvals: [(actionId: String, approved: Bool, scope: String, requestId: String, session: String)] = []
    var deleted: [String] = []
    var runs: [(prompt: String, session: String)] = []
    var fullAccessValue = false
    var loadDelayMs: UInt64 = 0
    var holdNextHistoryLoad = false
    private var heldHistoryRelease: CheckedContinuation<Void, Never>?
    private var historyEntryWaiter: CheckedContinuation<Void, Never>?

    func waitForHeldHistoryLoad() async {
        if heldHistoryRelease != nil { return }
        await withCheckedContinuation { historyEntryWaiter = $0 }
    }

    func releaseHeldHistoryLoad() {
        guard let release = heldHistoryRelease else {
            preconditionFailure("Expected a held history load")
        }
        heldHistoryRelease = nil
        release.resume()
    }

    func listChats() async -> [ChatSummary]? {
        if listFailures > 0 { listFailures -= 1; return nil }
        return chats
    }
    func loadChat(id: String) async -> [ChatMessage]? {
        if holdNextHistoryLoad {
            holdNextHistoryLoad = false
            await withCheckedContinuation { release in
                precondition(heldHistoryRelease == nil, "Only one held history load")
                heldHistoryRelease = release
                let entered = historyEntryWaiter
                historyEntryWaiter = nil
                entered?.resume()
            }
        }
        if loadDelayMs > 0 { try? await Task.sleep(nanoseconds: loadDelayMs * 1_000_000) }
        return history[id]
    }
    func deleteChat(id: String) async -> Bool { deleted.append(id); return true }
    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        runs.append((prompt, sessionId))
        for event in script(prompt) { onEvent(event) }
    }
    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async {
        approvals.append((actionId, approved, scope, requestId, sessionId))
    }
    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String) { (true, "Sent") }
    func fullAccess() async -> Bool? { fullAccessValue }
    func setFullAccess(_ on: Bool) async -> Bool? { fullAccessValue = on; return on }
}

@main
struct ChatStoreChecks {
    nonisolated(unsafe) static var count = 0
    static func check(_ condition: Bool, _ message: String) {
        precondition(condition, message)
        count += 1
    }
    static func ev(_ type: String, _ fields: [String: Any] = [:]) -> ChatEvent {
        var payload = fields; payload["type"] = type
        return ChatEvent(type: type, payload: payload)
    }
    static func settle(_ ms: UInt64 = 150) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }

    static func main() async {
        await MainActor.run { }
        await run()
        print("Chat store checks passed (\(count)).")
    }

    @MainActor static func run() async {
        // A full reply: session, route, tools, streamed text, done.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [
                ev("session", ["id": "s1"]),
                ev("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "agent", "needs_tools": true, "route_source": "rules", "reason": "mail"]),
                ev("tool_call", ["id": "c1", "name": "summarize_emails", "decision": "allow"]),
                ev("tool_result", ["id": "c1", "result": "4 messages"]),
                ev("delta", ["text": "You have "]), ev("delta", ["text": "4 messages."]),
                ev("done"),
            ] }
            let store = ChatStore(backend: backend)
            store.newChat()
            store.send("  Summarize   my mail  ")
            await settle()
            let chat = store.selected!
            check(chat.title == "Summarize my mail", "title comes from the first prompt: \(chat.title)")
            check(chat.sessionId == "s1", "the session id is adopted")
            check(chat.messages.count == 2 && chat.messages[0].role == .user && chat.messages[0].text == "Summarize   my mail", "user message is trimmed and kept")
            let reply = chat.messages[1]
            check(reply.text == "You have 4 messages.", "streamed text is joined: \(reply.text)")
            check(reply.phase == .done && !chat.busy, "the exchange finishes")
            check(reply.tools.count == 1 && reply.tools[0].finished && reply.tools[0].result == "4 messages", "tool row finished")
            check(reply.meta?.shortModel == "Ling-3.0-tiny" && reply.meta?.duration != nil, "metadata is recorded")
            check(chat.preview == "You have 4 messages.", "preview follows the reply")
            check(backend.runs.count == 1 && backend.runs[0].session == "", "first send starts a new session")
            // A follow-up continues the same session.
            store.send("and my calendar?")
            await settle()
            check(backend.runs.count == 2 && backend.runs[1].session == "s1", "follow-up reuses the session")
            check(chat.messages.count == 4, "history grows by one exchange")
            // A second send while busy is ignored.
            backend.script = { _ in [ev("routed", ["model": "m", "role": "general"]), ev("delta", ["text": "x"])] }
            store.send("one")
            await settle()
            check(chat.busy, "a reply with no done stays busy")
            let before = chat.messages.count
            store.send("two")
            check(chat.messages.count == before, "sending while busy does nothing")
            store.stop(chat)
            check(!chat.busy && chat.messages.last?.phase == .done && chat.messages.last?.text == "x", "stop keeps the text and ends the reply")
        }

        // A dropped request with nothing received is handed back to the composer.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("error", ["message": "The assistant didn't respond.", "dropped": true])] }
            let store = ChatStore(backend: backend)
            store.send("hello there")
            await settle()
            let chat = store.selected!
            check(chat.messages.isEmpty, "the failed exchange is removed")
            check(chat.restoredInput == "hello there", "the prompt is restored")
            check(!chat.busy, "dropped request frees the chat")
        }

        // A real failure keeps the exchange and shows the error.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("routed", ["model": "m", "role": "general"]), ev("error", ["message": "Out of memory", "detail": "oom"])] }
            let store = ChatStore(backend: backend)
            store.send("big question")
            await settle()
            let reply = store.selected!.messages.last!
            check(reply.isError && reply.text == "Out of memory" && reply.errorDetail == "oom", "failure is shown")
            check(!store.selected!.busy, "failure frees the chat")
        }

        // Approvals are answered through the service with the request id.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [
                ev("session", ["id": "s2"]),
                ev("routed", ["model": "m", "role": "agent", "needs_tools": true]),
                ev("confirm", ["id": "a1", "request_id": "r1", "tool": "run_shell", "reason": "Delete files", "grantable": false]),
            ] }
            let store = ChatStore(backend: backend)
            store.send("delete old downloads")
            await settle()
            let chat = store.selected!
            let reply = chat.messages.last!
            check(reply.approval?.state == .pending && chat.busy, "waiting on a yes")
            store.resolve(chat, messageID: reply.id, approved: false, scope: "once")
            await settle()
            check(chat.messages.last?.approval?.state == .denied, "card shows denied")
            check(backend.approvals.count == 1 && backend.approvals[0].approved == false && backend.approvals[0].requestId == "r1"
                  && backend.approvals[0].actionId == "a1" && backend.approvals[0].session == "s2", "service gets the denial for the right request")
            store.resolve(chat, messageID: reply.id, approved: true, scope: "once")
            await settle()
            check(backend.approvals.count == 1 && chat.messages.last?.approval?.state == .denied, "a card cannot be answered twice")
            store.stop(chat)
        }

        // A card still open when the reply ends cannot be answered afterwards.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("session", ["id": "s4"]), ev("confirm", ["id": "a2", "request_id": "r2", "tool": "t", "reason": "r"]),
                                      ev("error", ["message": "The connection dropped.", "dropped": true])] }
            let store = ChatStore(backend: backend)
            store.send("something risky")
            await settle()
            let chat = store.selected!
            check(!chat.busy, "the failed reply frees the chat")
            check(chat.messages.last?.approval?.state == .timedOut, "an unanswered card closes when the reply ends")
            store.resolve(chat, messageID: chat.messages.last!.id, approved: true, scope: "once")
            await settle()
            check(backend.approvals.isEmpty && chat.messages.last?.approval?.state == .timedOut, "a closed card cannot be allowed")
        }

        // Stopping while a card is open denies it.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("session", ["id": "s3"]), ev("confirm", ["id": "a9", "request_id": "r9", "tool": "t", "reason": "r"])] }
            let store = ChatStore(backend: backend)
            store.send("do it")
            await settle()
            store.stop(store.selected!)
            await settle()
            check(backend.approvals.count == 1 && backend.approvals[0].approved == false, "stop denies the open card")
        }

        // Drafts are sent only on request and only once.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("routed", ["model": "m", "role": "agent"]), ev("message_draft", ["to": "Priya", "text": "Count me in"]), ev("done")] }
            let store = ChatStore(backend: backend)
            store.send("tell Priya I'm in")
            await settle()
            let chat = store.selected!
            let id = chat.messages.last!.id
            check(chat.messages.last?.draft?.sent == false, "draft starts unsent")
            store.sendDraft(chat, messageID: id)
            await settle()
            check(chat.messages.last?.draft?.sent == true && chat.messages.last?.draft?.status == "Sent", "draft sends on request")
            store.updateDraft(chat, messageID: id) { $0.text = "   " }
            store.sendDraft(chat, messageID: id)
            check(chat.messages.last?.draft?.status == "Sent", "a sent draft is not sent again")
        }

        // List merging, sections, search, selection, history, deletion.
        do {
            let backend = ScriptedBackend()
            let now = Date()
            backend.chats = [
                ChatSummary(id: "a", title: "Alpha trip", preview: "pack light", lastUsed: now, turnCount: 2),
                ChatSummary(id: "b", title: "Beta lease", preview: "sixty days", lastUsed: now.addingTimeInterval(-3 * 86_400), turnCount: 2),
                ChatSummary(id: "c", title: "Gamma", preview: "", lastUsed: now.addingTimeInterval(-40 * 86_400), turnCount: 1),
            ]
            backend.history["a"] = [ChatMessage(role: .user, text: "Packing list?"), ChatMessage(role: .assistant, text: "Pack light.")]
            let store = ChatStore(backend: backend)
            await store.refresh()
            check(store.reachable && store.conversations.count == 3, "list loads")
            check(store.sections.map(\.title) == ["Today", "Previous 7 days", "Earlier"], "sections: \(store.sections.map(\.title))")
            await store.refresh()
            check(store.conversations.count == 3, "refreshing again does not duplicate")
            store.search = "lease"
            check(store.sections.flatMap(\.items).map(\.title) == ["Beta lease"], "search narrows the list")
            store.search = "pack"
            check(store.sections.flatMap(\.items).map(\.title) == ["Alpha trip"], "search also matches the preview")
            store.search = ""
            let alpha = store.conversations.first { $0.sessionId == "a" }!
            check(!alpha.loaded, "history waits until a chat is opened")
            store.select(alpha)
            await settle()
            check(alpha.loaded && alpha.messages.count == 2 && store.selected === alpha, "opening a chat loads its history")
            // Sending into a chat that is still loading its history is ignored, and the history is not overwritten.
            backend.history["b"] = [ChatMessage(role: .user, text: "Lease?"), ChatMessage(role: .assistant, text: "Sixty days.")]
            backend.holdNextHistoryLoad = true
            let beta = store.conversations.first { $0.sessionId == "b" }!
            store.select(beta)
            await backend.waitForHeldHistoryLoad()
            check(beta.loading, "history is loading")
            store.send("too early")
            check(beta.messages.isEmpty && !beta.busy, "sending while loading is ignored")
            backend.releaseHeldHistoryLoad()
            await settle(300)
            check(beta.loaded && beta.messages.count == 2, "history arrives intact")
            backend.loadDelayMs = 0
            // A chat open in the app but not yet in the list survives a refresh.
            let fresh = store.newChat()
            check(store.conversations.first === fresh && fresh.isEmpty, "new chat sits first and is empty")
            check(store.newChat() === fresh, "new chat is reused while still empty")
            await store.refresh()
            check(store.conversations.contains { $0 === fresh }, "an unsent chat survives a refresh")
            // Opening by session id (the notch's hand-off), listed or not.
            await store.open(sessionId: "b")
            check(store.selected?.sessionId == "b", "open selects a listed chat")
            backend.history["zz"] = [ChatMessage(role: .user, text: "From the notch"), ChatMessage(role: .assistant, text: "Hi")]
            await store.open(sessionId: "zz")
            await settle()
            check(store.selected?.sessionId == "zz" && store.selected?.messages.count == 2, "open loads an unlisted chat's history")
            check(store.selected?.title == "From the notch", "a placeholder takes its title from the first prompt")
            await store.open(sessionId: "")
            check(store.selected?.sessionId == "" && store.selected?.isEmpty == true, "an empty session id opens a new chat")
            // Delete.
            store.select(alpha)
            store.delete(alpha)
            await settle()
            check(store.selected != nil && store.selected !== alpha, "deleting the open chat selects another")
            await store.refresh()
            check(!store.conversations.contains { $0.sessionId == "a" }, "a deleted chat is not brought back by a stale list")
            check(backend.deleted == ["a"], "delete tells the service")
            // The service going away is reported, not hidden.
            backend.chats = nil
            await store.refresh()
            check(!store.reachable, "an unreachable service is flagged")
        }

        // Deltas that arrive faster than the UI folds them are not lost.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in
                [ev("routed", ["model": "m", "role": "general"])]
                + (0..<200).map { ev("delta", ["text": "w\($0) "]) } + [ev("done")]
            }
            let store = ChatStore(backend: backend)
            store.send("count")
            await settle(400)
            let text = store.selected!.messages.last!.text
            check(text == (0..<200).map { "w\($0) " }.joined(), "all 200 deltas arrive in order")
            check(store.selected!.messages.last!.phase == .done, "done follows the last delta")
        }

        // The window opens before the service is up: it retries until the service answers.
        do {
            let backend = ScriptedBackend()
            backend.chats = [ChatSummary(id: "late", title: "Came up late", preview: "", lastUsed: Date(), turnCount: 2)]
            backend.listFailures = 3
            let store = ChatStore(backend: backend)
            await store.refreshWhenReady(maxAttempts: 10, retryDelay: .milliseconds(10))
            check(store.reachable && store.conversations.count == 1, "the list loads once the service answers")
            let never = ScriptedBackend()
            never.listFailures = 100
            let giveUp = ChatStore(backend: never)
            await giveUp.refreshWhenReady(maxAttempts: 3, retryDelay: .milliseconds(10))
            check(!giveUp.reachable, "it stops trying when the service never answers")
            check(never.listFailures == 97, "it makes exactly the attempts it was given")
        }

        // Full access is read from and written to the service.
        do {
            let backend = ScriptedBackend()
            backend.fullAccessValue = true
            let store = ChatStore(backend: backend)
            await store.refresh()
            check(store.fullAccess, "access mode is read")
            store.setFullAccess(false)
            await settle()
            check(!store.fullAccess && !backend.fullAccessValue, "access mode is written")
        }
    }
}
