import Foundation

// Synthetic contract for the Wisp Chat window's pure logic: the event reducer,
// saved-history mapping, titles, and sidebar grouping. No window, no network.

@main
struct ChatChecks {
    nonisolated(unsafe) static var count = 0
    static func check(_ condition: Bool, _ message: String) {
        precondition(condition, message)
        count += 1
    }
    static func ev(_ type: String, _ fields: [String: Any] = [:]) -> ChatEvent {
        var payload = fields; payload["type"] = type
        return ChatEvent(type: type, payload: payload)
    }

    static func main() {
        let t0 = Date(timeIntervalSince1970: 1_000)
        let t1 = Date(timeIntervalSince1970: 1_003)

        // A plain streamed answer.
        var m = ChatMessage(role: .assistant, phase: .waiting)
        check(ChatTurnReducer.apply(ev("session", ["id": "abc123"]), to: &m, now: t0) == .session("abc123"), "session id surfaces")
        _ = ChatTurnReducer.apply(ev("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "general", "needs_tools": false, "route_source": "rules", "reason": "chat"]), to: &m, now: t0)
        check(m.phase == .streaming, "no-tool route streams")
        check(m.meta?.shortModel == "Ling-3.0-tiny", "quantization suffix is hidden: \(m.meta?.shortModel ?? "nil")")
        check(m.meta?.roleLabel == "General", "role label")
        _ = ChatTurnReducer.apply(ev("delta", ["text": "Hello"]), to: &m, now: t0)
        _ = ChatTurnReducer.apply(ev("delta", ["text": ", there."]), to: &m, now: t0)
        check(m.text == "Hello, there.", "deltas accumulate")
        check(m.meta?.firstTokenAt == t0, "first token time is recorded once")
        check(ChatTurnReducer.apply(ev("done"), to: &m, now: t1) == .finished, "done finishes")
        check(m.phase == .done && m.meta?.finishedAt == t1, "done stamps the finish time")

        // Tools: call, result, and preamble that is not the answer.
        var t = ChatMessage(role: .assistant, phase: .waiting)
        _ = ChatTurnReducer.apply(ev("routed", ["model": "m", "role": "agent", "needs_tools": true]), to: &t, now: t0)
        check(t.phase == .working, "tool route is working")
        _ = ChatTurnReducer.apply(ev("delta", ["text": "Let me check that"]), to: &t, now: t0)
        _ = ChatTurnReducer.apply(ev("tool_call", ["id": "c1", "name": "summarize_emails", "decision": "allow"]), to: &t, now: t0)
        _ = ChatTurnReducer.apply(ev("clear_answer"), to: &t, now: t0)
        check(t.text.isEmpty && t.meta?.firstTokenAt == nil, "preamble is cleared")
        check(t.tools.count == 1 && t.tools[0].kind == .mail && !t.tools[0].finished, "mail tool row is running")
        check(t.tools[0].title == "Mail · summarize emails", "tool title: \(t.tools[0].title)")
        check(t.tools[0].detail == "running", "running detail")
        _ = ChatTurnReducer.apply(ev("tool_result", ["id": "c1", "result": "4 messages from people\nsecond line"]), to: &t, now: t0)
        check(t.tools[0].finished && t.tools[0].detail == "4 messages from people", "result detail is the first line")
        _ = ChatTurnReducer.apply(ev("tool_result", ["id": "unknown", "result": "x"]), to: &t, now: t0)
        check(t.tools.count == 1, "a stray result adds nothing")

        // Blocked and waiting tools say so.
        var denied = ChatToolRow(id: "d", name: "delete_file", decision: "deny")
        check(denied.detail == "blocked", "denied detail")
        denied.decision = "confirm"
        check(ChatToolRow(id: "w", name: "send_email", decision: "confirm").detail == "waiting for you", "confirm detail")

        // Confirmation cards.
        var c = ChatMessage(role: .assistant, phase: .working)
        _ = ChatTurnReducer.apply(ev("confirm", ["id": "a1", "request_id": "r1", "tool": "run_shell", "reason": "Run a command", "grantable": true, "scope_hint": "~/Downloads", "preview": "ls"]), to: &c, now: t0)
        check(c.approval?.state == .pending && c.approval?.grantable == true && c.approval?.scopeHint == "~/Downloads", "approval card is pending")
        _ = ChatTurnReducer.apply(ev("confirm_timeout", ["id": "other"]), to: &c, now: t0)
        check(c.approval?.state == .pending, "a timeout for another action is ignored")
        _ = ChatTurnReducer.apply(ev("confirm_timeout", ["id": "a1"]), to: &c, now: t0)
        check(c.approval?.state == .timedOut, "matching timeout closes the card")
        var legacy = ChatMessage(role: .assistant)
        _ = ChatTurnReducer.apply(ev("confirm", ["id": "x"]), to: &legacy, now: t0)
        check(legacy.approval?.grantable == false, "older backends never offer Always allow")

        // Message drafts.
        var d = ChatMessage(role: .assistant, phase: .working)
        _ = ChatTurnReducer.apply(ev("message_draft", ["to": "Priya", "text": "Count me in"]), to: &d, now: t0)
        check(d.draft?.to == "Priya" && d.draft?.sent == false && d.phase == .streaming, "draft arrives unsent")

        // Errors.
        var e = ChatMessage(role: .assistant, phase: .working)
        check(ChatTurnReducer.apply(ev("error", ["message": "The assistant didn't respond.", "dropped": true]), to: &e, now: t0) == .droppedEmpty, "an empty dropped request is retryable")
        var e2 = ChatMessage(role: .assistant, phase: .streaming); e2.text = "partial"
        check(ChatTurnReducer.apply(ev("error", ["message": "Out of memory", "detail": "OOM", "dropped": true]), to: &e2, now: t1) == .failed, "a dropped request with content is a failure")
        check(e2.isError && e2.text == "Out of memory" && e2.errorDetail == "OOM" && e2.phase == .failed, "error text replaces the partial answer")
        var e4 = ChatMessage(role: .assistant, phase: .working)
        _ = ChatTurnReducer.apply(ev("confirm", ["id": "a1", "request_id": "r1", "tool": "t", "reason": "r"]), to: &e4, now: t0)
        check(ChatTurnReducer.apply(ev("error", ["message": "dropped", "dropped": true]), to: &e4, now: t1) == .failed, "a dropped request with an open card is a failure, not a silent retry")
        var e3 = ChatMessage(role: .assistant, phase: .working)
        _ = ChatTurnReducer.apply(ev("error", ["message": ""]), to: &e3, now: t1)
        check(e3.text == "Something went wrong.", "empty error text gets a default")

        // Status text is cleared by anything but another status.
        var s = ChatMessage(role: .assistant, phase: .waiting)
        _ = ChatTurnReducer.apply(ev("status", ["text": "Loading model…"]), to: &s, now: t0)
        check(s.status == "Loading model…", "status shows")
        _ = ChatTurnReducer.apply(ev("heartbeat"), to: &s, now: t0)
        check(s.status.isEmpty && s.meta?.heartbeats == 1, "heartbeat clears status and is counted")

        // Speed estimate matches the notch.
        check(ChatTurnReducer.tokensPerSecond(chars: 400, since: t0, now: t0.addingTimeInterval(0.2)) == 0, "too early to estimate")
        check(ChatTurnReducer.tokensPerSecond(chars: 400, since: t0, now: t0.addingTimeInterval(2)) == 50, "400 chars in 2 s is 50 tok/s")

        // Titles.
        check(ChatTitle.make(from: "  What did Priya   send\nabout the offsite? ") == "What did Priya send about the offsite?", "title collapses whitespace")
        check(ChatTitle.make(from: String(repeating: "a", count: 80)).count == 60, "title is capped at 60")
        check(ChatTitle.make(from: String(repeating: "a", count: 80)).hasSuffix("…"), "long title ends with an ellipsis")
        check(ChatTitle.make(from: "   ") == "New chat", "blank prompt")

        // List rows from the backend.
        let row: [String: Any] = ["id": "s1", "title": "Trip", "preview": "Pack light", "last_used": 1_700_000_000.5, "turn_count": 4]
        let summary = ChatSummary.parse(row)
        check(summary?.title == "Trip" && summary?.turnCount == 4 && summary?.lastUsed == Date(timeIntervalSince1970: 1_700_000_000.5), "summary parses")
        check(ChatSummary.parse(["id": "", "title": "x"]) == nil, "a row without an id is dropped")
        check(ChatSummary.parse(["id": "s2", "title": "  "])?.title == "New chat", "blank titles fall back")

        // Sidebar sections.
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "UTC")!
        let now = Date(timeIntervalSince1970: 1_700_000_000)   // 2023-11-14 22:13 UTC
        func at(_ offsetDays: Int, hours: Int = 0) -> Date { now.addingTimeInterval(Double(offsetDays) * 86_400 + Double(hours) * 3_600) }
        check(ChatGrouping.section(for: now, now: now, calendar: cal) == "Today", "now is today")
        check(ChatGrouping.section(for: at(-1), now: now, calendar: cal) == "Yesterday", "a day ago is yesterday")
        check(ChatGrouping.section(for: at(-4), now: now, calendar: cal) == "Previous 7 days", "four days ago")
        check(ChatGrouping.section(for: at(-30), now: now, calendar: cal) == "Earlier", "a month ago")

        // Saved history.
        let turns: [[String: Any]] = [
            ["role": "user", "content": "Summarize my mail", "tool_digest": ""],
            ["role": "assistant", "content": "  You have 4 messages.  ", "tool_digest": "summarize_emails, get_upcoming"],
            ["role": "system", "content": "ignored"],
            ["role": "assistant", "content": "   ", "tool_digest": ""],
        ]
        let history = ChatHistory.messages(from: turns)
        check(history.count == 2, "only non-empty user and assistant turns load")
        check(history[0].role == .user && history[1].role == .assistant, "roles map")
        check(history[1].text == "You have 4 messages.", "text is trimmed")
        check(history[1].tools.map(\.name) == ["summarize_emails", "get_upcoming"], "tool digest becomes rows")
        check(history[1].tools.allSatisfy { $0.finished }, "reloaded tool rows are not running")
        check(history[1].tools[1].kind == .calendar, "get_upcoming is calendar")

        print("Chat contract checks passed (\(count)).")
    }
}
