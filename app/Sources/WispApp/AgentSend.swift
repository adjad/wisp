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
                + (setAside > 1 ? " \(setAside - 1) earlier set-aside message\(setAside == 2 ? "" : "s") stay behind it." : "")
        case .box: break
        }
        var text = why + " Your message is back in the box. Press Return to send it again."
        if queued > 0 { text += " \(queued) queued message\(queued == 1 ? "" : "s") will follow it." }
        return text
    }
}
