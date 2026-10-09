#if WISP_CHAT_QA
import AppKit
import SwiftUI

// Never packaged as Wisp.app. This binary renders the real Chat window against
// a scripted fixture, installs no AppDelegate, never touches the Wisp port, and
// writes PNGs of each state in light and dark for review.
//
//   swift build -Xswiftc -DWISP_CHAT_QA && WISP_CHAT_QA_OUT=/some/dir .build/debug/WispApp

@MainActor
final class FixtureChatBackend: ChatBackend {
    private let day: TimeInterval = 86_400
    private var approvals: [String: Bool] = [:]
    private var created = 0

    func listChats() async -> [ChatSummary]? {
        let now = Date()
        return [
            ChatSummary(id: "c1", title: "Priya's offsite and Thursday", preview: "Priya sent the plan this morning.", lastUsed: now, turnCount: 2),
            ChatSummary(id: "c2", title: "Summarize this week's mail", preview: "Most of this week was quiet.", lastUsed: now.addingTimeInterval(-3_600), turnCount: 2),
            ChatSummary(id: "c3", title: "Rename the invoice PDFs", preview: "Done. I renamed 3 files.", lastUsed: now.addingTimeInterval(-day - 600), turnCount: 2),
            ChatSummary(id: "c4", title: "Why is Mail slow to sync?", preview: "Usually it's the first sync after a long sleep.", lastUsed: now.addingTimeInterval(-day - 7_200), turnCount: 2),
            ChatSummary(id: "c5", title: "Trip to Lisbon, packing list", preview: "Late October in Lisbon runs 15 to 21 °C.", lastUsed: now.addingTimeInterval(-5 * day), turnCount: 2),
            ChatSummary(id: "c6", title: "Explain the lease clause", preview: "It lets you leave before the lease ends.", lastUsed: now.addingTimeInterval(-20 * day), turnCount: 2),
        ]
    }

    func loadChat(id: String) async -> [ChatMessage]? {
        ChatHistory.messages(from: [
            ["role": "user", "content": "What did Priya send about the offsite, and am I free Thursday?"],
            ["role": "assistant", "content": "Priya sent the plan this morning. The offsite is **Oct 22 to 23 in Lisbon**, with a team dinner on Friday night and a flight she'd like you to confirm by **Friday**.\n\nThursday is mostly open: a **design review at 2:30 pm** and a 1:1 at 4:00. Everything before 2:00 is free.",
             "tool_digest": "summarize_emails, get_upcoming"],
        ])
    }

    func deleteChat(id: String) async -> Bool { true }

    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        func emit(_ type: String, _ fields: [String: Any] = [:]) {
            var payload = fields; payload["type"] = type
            onEvent(ChatEvent(type: type, payload: payload))
        }
        func pause(_ ms: UInt64) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }
        let lower = prompt.lowercased()
        if sessionId.isEmpty { created += 1 }
        emit("session", ["id": sessionId.isEmpty ? "qa-new-\(created)" : sessionId])
        await pause(120)
        if lower.contains("delete") {
            emit("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "agent", "needs_tools": true, "route_source": "rules", "reason": "File action"])
            emit("tool_call", ["id": "t1", "name": "find_files", "decision": "allow"])
            emit("tool_result", ["id": "t1", "result": "Found 14 files in ~/Downloads older than 90 days"])
            await pause(120)
            emit("tool_call", ["id": "t2", "name": "run_shell", "decision": "confirm"])
            emit("confirm", ["id": "a1", "request_id": "r1", "tool": "run_shell", "reason": "Run a command that deletes 14 files",
                             "grantable": false, "scope_hint": "", "preview": "find ~/Downloads -mtime +90 -type f -delete"])
            return
        }
        if lower.contains("reply") {
            emit("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "agent", "needs_tools": true, "route_source": "rules", "reason": "Compose"])
            emit("tool_call", ["id": "t1", "name": "read_messages", "decision": "allow"])
            emit("tool_result", ["id": "t1", "result": "1 thread"])
            await pause(120)
            emit("delta", ["text": "Here's a reply in your usual tone. Nothing has been sent."])
            emit("message_draft", ["to": "Priya Nair", "text": "Hi Priya, count me in for Lisbon. I'll confirm my flight by Friday. I can't make the Friday dinner, but I'll be there for everything else."])
            emit("done")
            return
        }
        emit("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "agent", "needs_tools": true, "route_source": "rules", "reason": "Mail and calendar lookup"])
        emit("tool_call", ["id": "t1", "name": "summarize_emails", "decision": "allow"])
        await pause(150)
        emit("tool_result", ["id": "t1", "result": "4 messages from people, 11 automated"])
        emit("tool_call", ["id": "t2", "name": "get_upcoming", "decision": "allow"])
        await pause(150)
        emit("tool_result", ["id": "t2", "result": "2 events on Thursday"])
        for chunk in ["You have **4 messages from people** and 11 automated ones. ",
                      "Priya wants your flight confirmed by Friday, and Sam moved your 1:1 to Thursday at 4:00.\n\n",
                      "- Priya Nair: offsite logistics\n- Sam Okafor: 1:1 moved\n\n",
                      "Want me to draft a reply to Priya?"] {
            emit("delta", ["text": chunk])
            await pause(250)
        }
        emit("done")
    }

    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async {
        approvals[actionId] = approved
    }
    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String) { (true, "Sent") }
    func fullAccess() async -> Bool? { false }
    func setFullAccess(_ on: Bool) async -> Bool? { on }
}

@MainActor
enum ChatQADriver {
    static func run() {
        let outDir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["WISP_CHAT_QA_OUT"]
                         ?? FileManager.default.currentDirectoryPath, isDirectory: true)
        try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let store = ChatStore(backend: FixtureChatBackend())
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1180, height: 760),
                              styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.contentView = NSHostingView(rootView: ChatRootView(store: store, launchers: ChatLaunchers()))
        window.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        Task { @MainActor in
            await drive(store: store, window: window, outDir: outDir)
            app.terminate(nil)
        }
        app.run()
    }

    private static func drive(store: ChatStore, window: NSWindow, outDir: URL) async {
        func settle(_ seconds: Double = 0.6) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)) }
        func shoot(_ name: String) async {
            for (suffix, appearance) in [("light", NSAppearance.Name.aqua), ("dark", NSAppearance.Name.darkAqua)] {
                window.appearance = NSAppearance(named: appearance)
                await settle(0.5)
                guard let view = window.contentView else { continue }
                view.layoutSubtreeIfNeeded()
                guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: outDir.appendingPathComponent("\(name)-\(suffix).png"))
            }
        }
        func waitIdle(_ conversation: ChatConversation) async {
            for _ in 0..<80 where conversation.busy { await settle(0.1) }
        }

        await store.refresh()
        store.newChat()
        await settle()
        await shoot("1-welcome")

        // A saved chat loaded from history, tool row collapsed.
        if let saved = store.conversations.first(where: { $0.sessionId == "c1" }) {
            store.select(saved)
            await settle(0.8)
            await shoot("2-saved-chat")
        }

        // A live exchange with tools and streamed markdown, with the inspector open.
        store.newChat()
        store.send("Catch me up on today's mail and calendar")
        await settle(0.5)
        await shoot("3-working")
        if let live = store.selected { await waitIdle(live) }
        await settle()
        store.showInspector = true
        await shoot("4-answer-inspector")
        store.showInspector = false

        // An approval card waiting for a yes.
        store.newChat()
        store.send("Delete my old downloads")
        await settle(1.0)
        await shoot("5-approval")
        if let c = store.selected, let m = c.messages.last(where: { $0.role == .assistant }) {
            store.resolve(c, messageID: m.id, approved: false, scope: "once")
            await settle(0.4)
            await shoot("6-approval-denied")
            c.busy = false
        }

        // An unsent message draft.
        store.newChat()
        store.send("Draft a reply to Priya")
        await settle(1.2)
        await shoot("7-draft")
        print("Chat QA wrote screenshots to \(outDir.path)")
    }
}
#endif
