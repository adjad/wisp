import Foundation

/// When a prompt to `/agent` provably never reached the turn runner, so it is
/// safe to send it again by itself, and what the user is told when it fails.
///
/// Rules:
/// - IMPORTANT: retry only before a 200 stream exists. Once the stream starts,
///   the server may have run a tool ("delete …"), so a drop then is never resent.
/// - Retryable: the connection never opened, or it was lost before any response,
///   or a 5xx came back instead of the stream. /agent writes its 200 headers
///   before routing or running anything, so none of these follow a tool run.
///   A 4xx is the request itself, so sending it again can't help; a timeout
///   or a cancel is never resent.
enum AgentSend {
    static let retryDelay: UInt64 = 800_000_000

    static func retryable(status: Int) -> Bool { (500...599).contains(status) }

    static func retryable(error: Error) -> Bool {
        guard let code = (error as? URLError)?.code else { return false }
        return [.cannotConnectToHost, .cannotFindHost, .networkConnectionLost,
                .notConnectedToInternet, .dnsLookupFailed].contains(code)
    }

    /// The terminal event for a prompt the server never ran: it goes back in the box.
    static func unsent(status: Int) -> [String: Any] {
        ["message": "Wisp couldn't take that message (it returned \(status)).", "dropped": true, "unsent": true]
    }

    static func unsent(error: Error) -> [String: Any] {
        let reason = (error as? URLError)?.code == .cannotConnectToHost ? "Wisp isn't running right now."
            : "Couldn't reach Wisp."
        return ["message": reason, "dropped": true, "unsent": true]
    }

    /// Where a dropped prompt went.
    enum Place { case box, queued, setAside }

    /// What the Ask tab says when a prompt didn't go through: why (when the
    /// server never ran it), where the prompt is now, and what waits for it.
    /// `setAside` is how many set-aside prompts there are now, this one included.
    static func notice(message: String, unsent: Bool, queued: Int, place: Place = .box,
                       setAside: Int = 1) -> String {
        let why = unsent && !message.isEmpty ? message : "Wisp didn't answer."
        switch place {
        case .queued:
            return why + " That message is first in line; it sends after the one in the box (or press Return on an empty box)."
        case .setAside:
            return why + " It may have started on that message, so it won't be sent again by itself."
                + " Press Return on an empty box to bring it back."
                + (setAside > 1 ? " \(setAside - 1) earlier set-aside message\(setAside == 2 ? " stays" : "s stay") behind it." : "")
        case .box: break
        }
        var text = why + " Your message is back in the box. Press Return to send it again."
        if queued > 0 { text += " \(queued) queued message\(queued == 1 ? "" : "s") will follow it." }
        return text
    }
}

/// A turn that failed only because Wisp's model isn't ready yet: nothing ran
/// (no tool, no text), so the question is put back, never shown as an error.
/// The service says so with an error `code` (`setup_needed`, `model_loading`,
/// `engine_starting`); older services only with their message, which is read
/// as a fallback.
enum AskNotReady: Equatable {
    /// No model is downloaded or set up yet: the setup card is the way forward.
    case setup
    /// The engine or model is still coming up: try again in a moment.
    case starting

    static func classify(code: String, message: String) -> AskNotReady? {
        switch code {
        case "setup_needed": return .setup
        case "model_loading", "engine_starting": return .starting
        default: break
        }
        let text = message.lowercased()
        if text.contains("isn't downloaded") || text.contains("finish setup") { return .setup }
        if text.contains("trying to restart it") || text.contains("did not become ready")
            || text.contains("still loading") || text.contains("still starting") { return .starting }
        return nil
    }

    /// One wait before the single automatic retry of a `.starting` question.
    static let retryDelay: UInt64 = 8_000_000_000

    /// What Ask says, plainly, instead of a red error.
    func notice(willRetry: Bool) -> String {
        switch self {
        case .setup:
            return "Wisp's AI model isn't set up yet. Finish setup above, then press Return: your question is waiting in the box."
        case .starting:
            return willRetry
                ? "Wisp's AI is still starting up. Your question is in the box, and I'll send it again in a few seconds."
                : "Wisp's AI is still starting up. Your question is back in the box; press Return in a moment."
        }
    }
}

/// The tappable questions under an empty Ask: ones a fresh install can answer
/// even before any source is allowed. A question about a source Wisp can't
/// read (Calendar or Mail denied or turned off) is left out, so a first tap
/// never ends in "I can't read that".
enum AskStarters {
    static let limit = 4

    /// `unreadable`: source ids ("calendar", "email") Wisp can't read now.
    static func prompts(unreadable: Set<String>) -> [String] {
        var out = ["What can you do?"]
        if !unreadable.contains("calendar") { out.append("What's on my calendar today?") }
        if !unreadable.contains("email") { out.append("Any emails I need to reply to?") }
        out.append("Is my data private?")
        if out.count < limit { out.append("How do I use Wisp?") }
        return Array(out.prefix(limit))
    }

    /// Source states that mean Wisp can't read the source on this Mac.
    static func unreadable(states: [String: String]) -> Set<String> {
        Set(states.filter { $0.value == "unavailable" || $0.value == "disabled" }.keys)
    }
}
