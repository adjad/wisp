import Foundation

/// The chats left since Wisp opened (New chat, opening another chat, the brief's
/// chat), with every turn's full debug detail, so one Export Chat Debug Log… covers
/// them all and not only the chat on screen. Memory only: nothing here is written
/// to disk except by that explicit export, and it is gone when Wisp quits.
///
/// Generic over the turn so the cap and reopen rules are checked without the
/// notch's model (tests/ChatDebugArchiveChecks.swift).
struct ChatDebugArchive<Turn> {
    struct Chat {
        let sessionId: String
        var startedAt: Date?
        var turns: [Turn]
        var bytes: Int
    }

    /// The last 30 chats, and at most 32 MB of turn text and model I/O between them
    /// (the export is built on the main actor when it is asked for).
    static var defaultMaxChats: Int { 30 }
    static var defaultMaxBytes: Int { 32 << 20 }

    let maxChats: Int
    let maxBytes: Int
    /// Oldest first, in the order they were left.
    private(set) var chats: [Chat] = []
    /// Older chats dropped by the cap, said in the export so a gap is never silent.
    private(set) var droppedChats = 0
    private(set) var droppedTurns = 0

    init(maxChats: Int = defaultMaxChats, maxBytes: Int = defaultMaxBytes) {
        self.maxChats = max(1, maxChats)
        self.maxBytes = max(0, maxBytes)
    }

    var totalBytes: Int { chats.reduce(0) { $0 + $1.bytes } }

    /// Keep a chat being left. A chat left before under the same session (and not
    /// taken back on reopen) keeps its earlier turns ahead of these. A chat that
    /// never got a session is kept too; it just can't be reopened.
    mutating func archive(sessionId: String, startedAt: Date?, turns: [Turn], bytes: Int) {
        guard !turns.isEmpty else { return }
        var chat = Chat(sessionId: sessionId, startedAt: startedAt, turns: turns, bytes: max(0, bytes))
        if !sessionId.isEmpty, let index = chats.firstIndex(where: { $0.sessionId == sessionId }) {
            let earlier = chats.remove(at: index)
            chat.turns = earlier.turns + chat.turns
            chat.bytes += earlier.bytes
            chat.startedAt = [earlier.startedAt, startedAt].compactMap { $0 }.min()
        }
        chats.append(chat)
        // The newest chat always stays, even alone over the size limit.
        while chats.count > maxChats || (chats.count > 1 && totalBytes > maxBytes) {
            let dropped = chats.removeFirst()
            droppedChats += 1
            droppedTurns += dropped.turns.count
        }
    }

    /// Take a chat back out to continue it (reopened from Recent Chats, or the brief's
    /// chat asked about again); it is archived again, whole, when it is left.
    mutating func take(sessionId: String) -> Chat? {
        guard !sessionId.isEmpty, let index = chats.firstIndex(where: { $0.sessionId == sessionId }) else { return nil }
        return chats.remove(at: index)
    }

    /// A saved chat was deleted: its archived copy goes too.
    mutating func remove(sessionId: String) {
        guard !sessionId.isEmpty else { return }
        chats.removeAll { $0.sessionId == sessionId }
    }

    /// Something Wisp read or learned was forgotten, with no one chat to match:
    /// every archived chat goes.
    mutating func removeAll() { chats.removeAll() }
}


/// Assembles the export's .json payload and .txt transcript from chats whose turns
/// the notch has already rendered, so the shape is checked without the notch.
enum ChatDebugExport {
    struct Chat {
        let sessionId: String
        let startedAt: Date?
        let current: Bool
        let turns: [[String: Any]]
        /// The chat's turns as readable text, without a header.
        let transcript: String
    }

    static func header(index: Int, of count: Int, chat: Chat, iso: ISO8601DateFormatter) -> String {
        var line = "CHAT \(index) of \(count)" + (chat.current ? " — open now" : "")
        line += "\nSession: " + (chat.sessionId.isEmpty ? "(not saved)" : chat.sessionId)
        if let started = chat.startedAt { line += "\nStarted: \(iso.string(from: started))" }
        line += "\nTurns: \(chat.turns.count)"
        return line
    }

    /// `chats` are oldest first and end with the open chat when it has turns. The
    /// top-level session_id / turn_count / turns stay the open chat's, as before,
    /// for the tools that already read them.
    static func payload(chats: [Chat], currentSessionId: String, droppedChats: Int, droppedTurns: Int,
                        exportedAt: Date, iso: ISO8601DateFormatter) -> [String: Any] {
        let current = chats.last(where: { $0.current })
        var payload: [String: Any] = [
            "exported_at": iso.string(from: exportedAt),
            "session_id": currentSessionId,
            "turn_count": current?.turns.count ?? 0,
            "turns": current?.turns ?? [],
            "scope": "since_launch",
            "chat_count": chats.count,
            "chats": chats.map { chat -> [String: Any] in
                var row: [String: Any] = ["session_id": chat.sessionId, "turn_count": chat.turns.count,
                                          "turns": chat.turns, "current": chat.current]
                row["started_at"] = chat.startedAt.map { iso.string(from: $0) as Any } ?? NSNull()
                return row
            },
        ]
        if droppedChats > 0 {
            payload["dropped_chats"] = droppedChats
            payload["dropped_turns"] = droppedTurns
        }
        return payload
    }

    static func text(chats: [Chat], droppedChats: Int, droppedTurns: Int,
                     exportedAt: Date, iso: ISO8601DateFormatter) -> String {
        var text = "Wisp debug export\n"
        text += "Exported: \(iso.string(from: exportedAt))\n"
        text += "Chats since Wisp opened: \(chats.count)\n"
        if droppedChats > 0 {
            text += "Older chats not kept: \(droppedChats) (\(droppedTurns) turns), over the in-memory limit\n"
        }
        text += String(repeating: "=", count: 70) + "\n\n"
        for (i, chat) in chats.enumerated() {
            text += String(repeating: "#", count: 70) + "\n"
            text += header(index: i + 1, of: chats.count, chat: chat, iso: iso) + "\n"
            text += String(repeating: "#", count: 70) + "\n\n"
            text += chat.transcript
        }
        return text
    }
}
