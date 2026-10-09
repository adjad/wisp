import Foundation

// Pure data and logic for the Wisp Chat window. Foundation only, so the
// reducer and the history mapping can be compiled and checked on their own
// (tests/ChatChecks.swift) without the app, a window, or a backend.

/// One decoded server-sent event from /agent. A plain value so this file does
/// not depend on WispClient.
struct ChatEvent {
    let type: String
    let payload: [String: Any]

    func str(_ key: String) -> String { payload[key] as? String ?? "" }
    func bool(_ key: String) -> Bool { payload[key] as? Bool ?? false }
}

// MARK: - Tools

/// What kind of app or capability a tool call touched, for the tool rows shown
/// above an answer. Derived from the tool's name, which is stable and ours.
enum ChatToolKind: Equatable {
    case mail, messages, calendar, reminders, notes, files, web, memory, other

    static func from(toolName name: String) -> ChatToolKind {
        let n = name.lowercased()
        if n.contains("email") || n.contains("mail") { return .mail }
        if n.contains("message") || n.contains("imessage") { return .messages }
        if n.contains("remind") { return .reminders }
        if n.contains("calendar") || n.contains("upcoming") || n.contains("event") || n.contains("schedule") { return .calendar }
        if n.contains("note") { return .notes }
        if n.contains("memory") || n.contains("remember") || n.contains("fact") { return .memory }
        if n.contains("browse") || n.contains("web") || n.contains("page") || n.contains("url") { return .web }
        if n.contains("file") || n.contains("dir") || n.contains("folder") || n.contains("find") || n.contains("move") || n.contains("document") { return .files }
        return .other
    }

    var label: String {
        switch self {
        case .mail: return "Mail"
        case .messages: return "Messages"
        case .calendar: return "Calendar"
        case .reminders: return "Reminders"
        case .notes: return "Notes"
        case .files: return "Files"
        case .web: return "Web"
        case .memory: return "Memory"
        case .other: return "Tool"
        }
    }

    var symbol: String {
        switch self {
        case .mail: return "envelope"
        case .messages: return "message"
        case .calendar: return "calendar"
        case .reminders: return "bell"
        case .notes: return "note.text"
        case .files: return "doc"
        case .web: return "globe"
        case .memory: return "brain"
        case .other: return "wrench.and.screwdriver"
        }
    }
}

struct ChatToolRow: Identifiable, Equatable {
    let id: String              // the server's call id; a unique value when absent
    var name: String
    var decision: String        // "allow" | "confirm" | "deny"
    var result: String = ""
    var finished = false

    var kind: ChatToolKind { ChatToolKind.from(toolName: name) }

    /// "Mail · summarize emails": the app first, then the tool in plain words.
    var title: String {
        let words = name.replacingOccurrences(of: "_", with: " ")
        return kind == .other ? words.capitalizedFirst : "\(kind.label) · \(words)"
    }

    /// One short line of what came back, or what is still happening.
    var detail: String {
        if decision == "deny" { return "blocked" }
        if !finished { return decision == "confirm" ? "waiting for you" : "running" }
        let flat = result.split(whereSeparator: { $0.isNewline }).first.map(String.init) ?? ""
        return flat.count > 60 ? String(flat.prefix(60)) + "…" : flat
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}

// MARK: - Approvals and drafts

/// A confirmation the backend is waiting on (an action Wisp will not take
/// without a yes). Mirrors the notch's card so both surfaces mean the same thing.
struct ChatApproval: Equatable {
    enum State: Equatable { case pending, allowed, denied, timedOut }
    var actionId: String
    var requestId: String
    var tool: String
    var reason: String
    var grantable: Bool
    var scopeHint: String
    var preview: String
    var state: State = .pending
}

/// A message Wisp has written but not sent. Sending stays the user's decision.
struct ChatDraft: Equatable {
    var to: String
    var text: String
    var isEditing = false
    var isSending = false
    var sent = false
    var discarded = false
    var status = ""
}

// MARK: - Metadata

struct ChatMeta: Equatable {
    var model = ""
    var role = ""
    var routeReason = ""
    var routeSource = ""
    var needsTools = false
    var tokensPerSecond = 0
    var startedAt: Date?
    var firstTokenAt: Date?
    var finishedAt: Date?
    var heartbeats = 0

    var duration: Double? {
        guard let s = startedAt, let f = finishedAt else { return nil }
        return f.timeIntervalSince(s)
    }

    /// "Ling-3.0-tiny-oQ6e" -> "Ling-3.0-tiny": the quantization suffix is noise.
    var shortModel: String {
        var out = model
        if let slash = out.lastIndex(of: "/") { out = String(out[out.index(after: slash)...]) }
        for suffix in ["-oQ6e", "-oQ4e", "-4bit", "-3bit", "-2bit", "-6bit", "-8bit", "-nvfp4", "-MLX"] {
            if out.hasSuffix(suffix) { out = String(out.dropLast(suffix.count)) }
        }
        return out
    }

    /// The route in one friendly word, for the line under an answer.
    var roleLabel: String {
        switch role {
        case "coding": return "Coding"
        case "reasoning": return "Reasoning"
        case "vision": return "Vision"
        case "agent": return "Tools"
        case "fast": return "Quick"
        case "": return ""
        default: return "General"
        }
    }
}

// MARK: - Messages

struct ChatMessage: Identifiable, Equatable {
    enum Role: Equatable { case user, assistant }
    enum Phase: Equatable { case waiting, working, streaming, done, failed }

    let id: UUID
    var role: Role
    var text: String
    var reasoning = ""
    var tools: [ChatToolRow] = []
    var approval: ChatApproval?
    var draft: ChatDraft?
    var meta: ChatMeta?
    var status = ""
    var phase: Phase = .done
    var isError = false
    var errorDetail = ""
    var attachmentName: String?

    init(id: UUID = UUID(), role: Role, text: String = "", phase: Phase = .done) {
        self.id = id; self.role = role; self.text = text; self.phase = phase
    }

    var isLive: Bool { phase == .waiting || phase == .working || phase == .streaming }
}

// MARK: - The reducer

/// Folds the /agent event stream into one assistant message. The same event
/// vocabulary the notch panel handles (OverlayModel.handle), reduced to the
/// parts a conversation needs.
enum ChatTurnReducer {
    enum Outcome: Equatable {
        case none
        case session(String)
        case finished
        /// The request dropped before anything arrived; the caller should
        /// offer the prompt again instead of showing an error.
        case droppedEmpty
        case failed
    }

    static func apply(_ ev: ChatEvent, to msg: inout ChatMessage, now: Date = Date()) -> Outcome {
        if ev.type != "status" { msg.status = "" }
        var meta = msg.meta ?? ChatMeta()
        defer { msg.meta = meta }
        switch ev.type {
        case "session":
            return .session(ev.str("id"))
        case "status":
            msg.status = ev.str("text")
        case "routed":
            meta.model = ev.str("model")
            meta.role = ev.str("role")
            meta.routeReason = ev.str("reason")
            meta.routeSource = ev.str("route_source")
            meta.needsTools = ev.bool("needs_tools")
            msg.phase = meta.needsTools ? .working : .streaming
        case "tool_call":
            let row = ChatToolRow(id: ev.str("id").isEmpty ? UUID().uuidString : ev.str("id"),
                                  name: ev.str("name"), decision: ev.str("decision"))
            msg.tools.append(row)
            if meta.firstTokenAt == nil { meta.firstTokenAt = now }
            msg.phase = .working
        case "tool_result":
            if let i = msg.tools.lastIndex(where: { $0.id == ev.str("id") }) {
                msg.tools[i].result = ev.str("result")
                msg.tools[i].finished = true
            }
        case "clear_answer":
            // Preamble text streamed before a tool call is not the answer.
            msg.text = ""
            meta.firstTokenAt = nil
        case "confirm":
            msg.approval = ChatApproval(
                actionId: ev.str("id"), requestId: ev.str("request_id"),
                tool: ev.str("tool"), reason: ev.str("reason"),
                grantable: ev.payload["grantable"] as? Bool ?? false,
                scopeHint: ev.str("scope_hint"), preview: ev.str("preview"))
        case "confirm_timeout":
            if msg.approval?.actionId == ev.str("id") { msg.approval?.state = .timedOut }
        case "message_draft":
            msg.draft = ChatDraft(to: ev.str("to"), text: ev.str("text"))
            msg.phase = .streaming
        case "heartbeat":
            meta.heartbeats += 1
        case "reasoning":
            msg.reasoning = ev.str("text")
            if meta.firstTokenAt == nil { meta.firstTokenAt = now }
        case "delta", "text":
            if meta.firstTokenAt == nil { meta.firstTokenAt = now }
            msg.text += ev.str("text")
            msg.phase = .streaming
        case "error":
            let gotNothing = msg.text.isEmpty && msg.tools.isEmpty && meta.model.isEmpty
                && msg.approval == nil && msg.draft == nil
            if ev.bool("dropped") && gotNothing { return .droppedEmpty }
            msg.text = ev.str("message").isEmpty ? "Something went wrong." : ev.str("message")
            msg.errorDetail = ev.str("detail")
            msg.isError = true
            msg.phase = .failed
            meta.finishedAt = now
            return .failed
        case "done":
            msg.phase = .done
            meta.finishedAt = now
            return .finished
        default:
            break
        }
        return .none
    }

    /// Streamed characters per second, as the notch estimates it: about four
    /// characters to a token, ignored until a third of a second has passed.
    static func tokensPerSecond(chars: Int, since start: Date, now: Date = Date()) -> Int {
        let elapsed = now.timeIntervalSince(start)
        guard elapsed > 0.3 else { return 0 }
        return Int(Double(chars) / 4.0 / elapsed)
    }
}

// MARK: - The conversation list

struct ChatSummary: Identifiable, Equatable {
    let id: String              // the backend session id
    var title: String
    var preview: String
    var lastUsed: Date
    var turnCount: Int

    static func parse(_ row: [String: Any]) -> ChatSummary? {
        guard let id = row["id"] as? String, !id.isEmpty else { return nil }
        let used = (row["last_used"] as? Double) ?? Double(row["last_used"] as? Int ?? 0)
        let title = (row["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return ChatSummary(id: id, title: title.isEmpty ? "New chat" : title,
                           preview: row["preview"] as? String ?? "",
                           lastUsed: Date(timeIntervalSince1970: used),
                           turnCount: row["turn_count"] as? Int ?? 0)
    }
}

enum ChatTitle {
    /// A chat's name from its first prompt: one line, at most 60 characters.
    static func make(from prompt: String, limit: Int = 60) -> String {
        let flat = prompt.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
        if flat.isEmpty { return "New chat" }
        return flat.count > limit ? String(flat.prefix(limit - 1)) + "…" : flat
    }
}

enum ChatGrouping {
    /// The sidebar section a chat belongs in.
    static func section(for date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        if calendar.isDate(date, inSameDayAs: now) { return "Today" }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) { return "Yesterday" }
        if let week = calendar.date(byAdding: .day, value: -7, to: now), date > week { return "Previous 7 days" }
        return "Earlier"
    }

    static let order = ["Today", "Yesterday", "Previous 7 days", "Earlier"]
}

/// Maps the rows of GET /sessions/{id} (role, content, tool_digest) back into
/// messages for a saved chat. The saved history keeps which tools an answer
/// used but not their results or timing, so reloaded rows show the tool only.
enum ChatHistory {
    static func messages(from turns: [[String: Any]]) -> [ChatMessage] {
        var out: [ChatMessage] = []
        for turn in turns {
            let role = turn["role"] as? String ?? ""
            let text = (turn["content"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard (role == "user" || role == "assistant"), !text.isEmpty else { continue }
            var message = ChatMessage(role: role == "user" ? .user : .assistant, text: text)
            if role == "assistant", let digest = turn["tool_digest"] as? String, !digest.isEmpty {
                message.tools = digest.split(separator: ",").map { part in
                    ChatToolRow(id: UUID().uuidString, name: part.trimmingCharacters(in: .whitespaces),
                                decision: "allow", finished: true)
                }
            }
            out.append(message)
        }
        return out
    }
}
