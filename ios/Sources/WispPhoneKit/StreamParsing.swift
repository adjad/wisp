import Foundation

/// Decodes one SSE line from the Mac gateway (`data: {"type": ...}`) into a chat event.
/// Returns nil for comments, blank lines and event types the phone doesn't show.
public enum GatewayEventDecoder {
    public static func decode(line: String, sessionId: String) -> ChatEvent? {
        guard line.hasPrefix("data:") else { return nil }
        let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        guard let data = json.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = object["type"] as? String else { return nil }
        func str(_ key: String) -> String { object[key] as? String ?? "" }
        switch type {
        case "session":
            let id = str("id")
            return id.isEmpty ? nil : .session(id)
        case "status":
            let text = str("text")
            return text.isEmpty ? nil : .status(text)
        case "delta":
            let text = str("text")
            return text.isEmpty ? nil : .delta(text)
        case "text":
            return .replace(str("text"))
        case "clear_answer":
            return .clear
        case "tool_call":
            let name = str("name").isEmpty ? str("tool") : str("name")
            return name.isEmpty ? nil : .activity("Using \(ToolNames.friendly(name))")
        case "tool_result":
            return nil
        case "message_draft":
            let to = str("to")
            return .activity(to.isEmpty ? "Drafted a message" : "Drafted a message to \(to)")
        case "confirm":
            return .approval(ApprovalRequest(
                id: str("id"), requestId: str("request_id"), sessionId: sessionId,
                tool: str("tool"), reason: str("reason"), preview: str("preview"),
                grantable: object["grantable"] as? Bool ?? false))
        case "confirm_timeout":
            return .approvalExpired(str("id"))
        case "error":
            let message = str("message").isEmpty ? str("detail") : str("message")
            return .failed(message.isEmpty ? "Wisp on your Mac hit an error." : message)
        case "done":
            return .done
        default:
            return nil
        }
    }
}

public enum ToolNames {
    /// "get_upcoming" → "get upcoming"; good enough for an activity line.
    public static func friendly(_ name: String) -> String {
        name.replacingOccurrences(of: "_", with: " ")
    }
}

/// Decodes OpenAI-compatible streaming chunks (`data: {...}` / `data: [DONE]`).
public enum OpenAIStreamDecoder {
    public enum Piece: Equatable, Sendable {
        case content(String)
        case done
        case error(String)
    }

    public static func decode(line: String) -> Piece? {
        guard line.hasPrefix("data:") else { return nil }
        let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        if payload == "[DONE]" { return .done }
        guard let data = payload.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let error = object["error"] as? [String: Any] {
            return .error(error["message"] as? String ?? "The provider returned an error.")
        }
        guard let choices = object["choices"] as? [[String: Any]], let first = choices.first,
              let delta = first["delta"] as? [String: Any],
              let content = delta["content"] as? String, !content.isEmpty else { return nil }
        return .content(content)
    }

    /// The message of a non-2xx JSON error body, if the provider sent one.
    public static func errorMessage(from body: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return nil }
        if let error = object["error"] as? [String: Any], let message = error["message"] as? String { return message }
        if let message = object["error"] as? String { return message }
        return object["detail"] as? String
    }
}

/// Hides `<think>…</think>` reasoning from a token stream, including tags split across
/// chunks and a block that never closes.
public struct ThinkFilter: Sendable {
    private var buffer = ""
    private var thinking = false
    private static let open = "<think>"
    private static let close = "</think>"

    public init() {}

    /// Feed one chunk; returns the text that is safe to show now.
    public mutating func feed(_ chunk: String) -> String {
        buffer += chunk
        var visible = ""
        while true {
            if thinking {
                if let range = buffer.range(of: Self.close) {
                    buffer = String(buffer[range.upperBound...])
                    thinking = false
                    continue
                }
                // Keep only a possible partial closing tag.
                buffer = String(buffer.suffix(Self.close.count - 1))
                return visible
            }
            if let range = buffer.range(of: Self.open) {
                visible += buffer[..<range.lowerBound]
                buffer = String(buffer[range.upperBound...])
                thinking = true
                continue
            }
            // Hold back a trailing prefix of "<think>" in case the tag is split.
            let hold = Self.partialPrefixLength(of: buffer, tag: Self.open)
            visible += buffer.dropLast(hold)
            buffer = String(buffer.suffix(hold))
            return visible
        }
    }

    /// Whatever was held back once the stream ends (never reasoning).
    public mutating func finish() -> String {
        defer { buffer = "" }
        return thinking ? "" : buffer
    }

    private static func partialPrefixLength(of text: String, tag: String) -> Int {
        var length = min(text.count, tag.count - 1)
        while length > 0 {
            if tag.hasPrefix(String(text.suffix(length))) { return length }
            length -= 1
        }
        return 0
    }
}
