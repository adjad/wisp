import Foundation

// Pure checks for the in-memory archive behind Export Chat Debug Log…
// (app/Sources/WispApp/ChatDebugArchive.swift): order, the cap, reopen and
// continue, and the export's .json / .txt shape. No notch, service or disk.
@main
struct ChatDebugArchiveChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func main() {
        // Order, empty chats, and chats with no session.
        var archive = ChatDebugArchive<String>()
        check(archive.maxChats == 30 && archive.maxBytes == 64 << 20, "Defaults: 30 chats, 64 MB")
        archive.archive(sessionId: "a", startedAt: nil, turns: [], bytes: 0)
        check(archive.chats.isEmpty, "A chat with no turns is not kept")
        archive.archive(sessionId: "a", startedAt: Date(timeIntervalSince1970: 20), turns: ["a1", "a2"], bytes: 4)
        archive.archive(sessionId: "", startedAt: nil, turns: ["u1"], bytes: 2)
        archive.archive(sessionId: "", startedAt: nil, turns: ["u2"], bytes: 2)
        archive.archive(sessionId: "b", startedAt: nil, turns: ["b1"], bytes: 2)
        check(archive.chats.map(\.sessionId) == ["a", "", "", "b"], "Kept in the order left; sessionless chats stay apart")
        check(archive.take(sessionId: "") == nil && archive.chats.count == 4, "A sessionless chat can't be reopened")

        // Reopen and continue: taken out whole, archived again whole at the end.
        let reopened = archive.take(sessionId: "a")
        check(reopened?.turns == ["a1", "a2"] && !archive.chats.contains { $0.sessionId == "a" },
              "Reopening takes the chat out with its turns")
        archive.archive(sessionId: "a", startedAt: reopened?.startedAt, turns: (reopened?.turns ?? []) + ["a3"], bytes: 6)
        check(archive.chats.map(\.sessionId) == ["", "", "b", "a"] && archive.chats.last?.turns == ["a1", "a2", "a3"]
              && archive.chats.last?.startedAt == Date(timeIntervalSince1970: 20),
              "A continued chat is archived again whole, as the latest left, keeping its start")
        // Left again without being taken (the brief's chat reopened before its archive): earlier turns stay first.
        archive.archive(sessionId: "b", startedAt: Date(timeIntervalSince1970: 5), turns: ["b2"], bytes: 2)
        check(archive.chats.map(\.sessionId) == ["", "", "a", "b"] && archive.chats.last?.turns == ["b1", "b2"]
              && archive.chats.last?.bytes == 4, "One session is one chat; its earlier turns go first")

        // The chat cap drops the oldest and says how much went.
        var capped = ChatDebugArchive<String>(maxChats: 3)
        for i in 1...5 { capped.archive(sessionId: "c\(i)", startedAt: nil, turns: ["x", "y"], bytes: 1) }
        check(capped.chats.map(\.sessionId) == ["c3", "c4", "c5"], "Only the last chats are kept")
        check(capped.droppedChats == 2 && capped.droppedTurns == 4, "Dropped chats and turns are counted")

        // The size cap: oldest go first, the newest stays even alone over it.
        var sized = ChatDebugArchive<String>(maxChats: 30, maxBytes: 100)
        sized.archive(sessionId: "s1", startedAt: nil, turns: ["x"], bytes: 60)
        sized.archive(sessionId: "s2", startedAt: nil, turns: ["x"], bytes: 30)
        check(sized.chats.count == 2 && sized.droppedChats == 0, "Under the size limit both stay")
        sized.archive(sessionId: "s3", startedAt: nil, turns: ["x"], bytes: 30)
        check(sized.chats.map(\.sessionId) == ["s2", "s3"] && sized.droppedChats == 1, "Over the limit the oldest goes")
        sized.archive(sessionId: "s4", startedAt: nil, turns: ["x", "y"], bytes: 500)
        check(sized.chats.map(\.sessionId) == ["s4"] && sized.droppedChats == 3 && sized.droppedTurns == 3,
              "The newest chat stays even alone over the limit")

        // The export's shape.
        let iso = ISO8601DateFormatter()
        let now = Date(timeIntervalSince1970: 1_000)
        let chats = [
            ChatDebugExport.Chat(sessionId: "", startedAt: nil, current: false,
                                 turns: [["role": "user", "text": "hi"]], transcript: "── USER ──\nhi\n\n"),
            ChatDebugExport.Chat(sessionId: "old", startedAt: Date(timeIntervalSince1970: 10), current: false,
                                 turns: [["role": "user", "text": "q"], ["role": "assistant", "text": "a"]],
                                 transcript: "── USER ──\nq\n\n"),
            ChatDebugExport.Chat(sessionId: "open", startedAt: nil, current: true,
                                 turns: [["role": "user", "text": "now"]], transcript: "── USER ──\nnow\n\n"),
        ]
        let payload = ChatDebugExport.payload(chats: chats, currentSessionId: "open", droppedChats: 2, droppedTurns: 7,
                                              exportedAt: now, iso: iso)
        check(JSONSerialization.isValidJSONObject(payload), "The payload serializes")
        check(payload["session_id"] as? String == "open" && payload["turn_count"] as? Int == 1
              && (payload["turns"] as? [[String: Any]])?.first?["text"] as? String == "now",
              "Top-level fields stay the open chat's, for tools that already read them")
        let rows = payload["chats"] as? [[String: Any]] ?? []
        check(rows.map { $0["session_id"] as? String } == ["", "old", "open"] && payload["chat_count"] as? Int == 3,
              "chats lists every chat, oldest first")
        check(rows.map { $0["turn_count"] as? Int } == [1, 2, 1] && rows.map { $0["current"] as? Bool } == [false, false, true],
              "Each chat has its turn count and whether it is open")
        check(rows[1]["started_at"] as? String == iso.string(from: Date(timeIntervalSince1970: 10))
              && rows[0]["started_at"] is NSNull, "started_at is the chat's first request, or null")
        check(payload["dropped_chats"] as? Int == 2 && payload["dropped_turns"] as? Int == 7
              && payload["scope"] as? String == "since_launch", "Dropped chats are marked")

        // After New chat with nothing typed yet: archived chats only, empty top-level turns.
        let leftOnly = ChatDebugExport.payload(chats: Array(chats.prefix(2)), currentSessionId: "", droppedChats: 0,
                                               droppedTurns: 0, exportedAt: now, iso: iso)
        check(leftOnly["session_id"] as? String == "" && leftOnly["turn_count"] as? Int == 0
              && (leftOnly["turns"] as? [Any])?.isEmpty == true && leftOnly["dropped_chats"] == nil,
              "With no open chat the top-level turns are empty and nothing is marked dropped")

        let text = ChatDebugExport.text(chats: chats, droppedChats: 2, droppedTurns: 7, exportedAt: now, iso: iso)
        check(text.hasPrefix("Wisp debug export\n") && text.contains("Chats since Wisp opened: 3"), "The transcript says its scope")
        check(text.contains("Older chats not kept: 2 (7 turns)"), "The transcript says what was dropped")
        check(text.contains("CHAT 1 of 3\nSession: (not saved)") && text.contains("CHAT 2 of 3\nSession: old\nStarted: ")
              && text.contains("CHAT 3 of 3 — open now\nSession: open"), "One headed section per chat")
        let order = ["\nhi\n", "\nq\n", "\nnow\n"].compactMap { text.range(of: $0)?.lowerBound }
        check(order.count == 3 && order == order.sorted(), "Sections in chat order")
        check(!ChatDebugExport.text(chats: chats, droppedChats: 0, droppedTurns: 0, exportedAt: now, iso: iso)
                .contains("Older chats"), "Nothing dropped, nothing said")

        print("PASS: chat debug archive keeps chats in order, caps them, reopens whole, and exports every chat")
    }
}
