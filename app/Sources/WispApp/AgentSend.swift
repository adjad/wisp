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
/// never ends in "I can't read that", and so is every source question until
/// the service has said what it can read.
enum AskStarters {
    static let limit = 4

    /// The sources a starter can be about, asked of the service as the panel opens.
    static let sources = ["calendar", "email", "messages"]

    /// What Wisp already holds on this Mac that makes a starter the person's
    /// own. Nothing is read for the starters alone; an empty field keeps the
    /// general starter in its place.
    struct Personal: Equatable {
        /// The next timed calendar event's title (nextEvent).
        var nextEvent = ""
        /// A person whose email or text waits for an answer (waitingSender), and
        /// where it came: "email" or "messages".
        var sender = ""
        var senderSource = ""
        /// The first goal from Getting to know you.
        var goal = ""
    }

    /// The goal starter puts what the person said in Getting to know you into
    /// the prompt, and Wisp promises that never goes to a cloud model. So it is
    /// offered only once the service said no cloud model gets Ask's prompts
    /// (Super Model or a cloud role); unknown keeps it back.
    static func goalStarterAllowed(cloudInUse: Bool?) -> Bool { cloudInUse == false }

    /// `unreadable`: source ids ("calendar", "email") Wisp can't read now; nil
    /// while that isn't known yet, which leaves out every source starter.
    static func prompts(unreadable: Set<String>?, personal: Personal = Personal()) -> [String] {
        let blocked = unreadable ?? Set(sources)
        var out = ["What can you do?"]
        if !blocked.contains("calendar") {
            out.append(personal.nextEvent.isEmpty ? "What's on my calendar today?"
                       : "What should I prep for \(clipped(personal.nextEvent))?")
        }
        if !personal.sender.isEmpty && !blocked.contains(personal.senderSource) {
            out.append("Anything from \(clipped(personal.sender)) I need to answer?")
        } else if !blocked.contains("email") {
            out.append("Any emails I need to reply to?")
        }
        if !personal.goal.isEmpty { out.append("Help me with my goal: \(clipped(personal.goal))") }
        out.append("Is my data private?")
        if out.count < limit { out.append("How do I use Wisp?") }
        return Array(out.prefix(limit))
    }

    /// The soonest timed event that hasn't started, within `window` seconds, by
    /// its title. All-day entries, blank titles and events on someone else's
    /// calendar (`own` false: subscribed, or shared read-only) never make a
    /// "prep for" starter.
    static func nextEvent(_ events: [(title: String, start: Double, allDay: Bool, own: Bool)], now: Double,
                          window: Double = 86_400) -> String {
        events.filter { $0.own && !$0.allDay && $0.start > now && $0.start <= now + window
                        && !$0.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            .min { $0.start < $1.start }?.title.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    /// The first real person still waiting: not an automated sender, not a
    /// conversation already answered, and known by a name, not an address or
    /// number ("Anything from +1 555…" reads oddly). In the order given (the Brief's own).
    static func waitingSender(_ needs: [(sender: String, source: String, person: Bool, replied: Bool)])
        -> (name: String, source: String)? {
        guard let first = needs.first(where: {
            $0.person && !$0.replied && !$0.sender.trimmingCharacters(in: .whitespaces).isEmpty
                && !isHandle($0.sender)
        }) else { return nil }
        return (first.sender.trimmingCharacters(in: .whitespaces), first.source)
    }

    /// An email address or a phone number rather than a name.
    private static func isHandle(_ sender: String) -> Bool {
        let text = sender.trimmingCharacters(in: .whitespaces)
        return text.contains("@")
            || (text.contains(where: \.isNumber) && text.allSatisfy { $0.isNumber || "+-(). ".contains($0) })
    }

    /// The greeting over the starters when the person told Wisp their name.
    static func greeting(name: String) -> String? {
        guard let first = name.split(whereSeparator: \.isWhitespace).first else { return nil }
        return "Hi, \(first)."
    }

    /// A name or title short enough for a starter chip.
    static func clipped(_ text: String, max: Int = 28) -> String {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count > max else { return text }
        return text.prefix(max - 1).trimmingCharacters(in: .whitespaces) + "…"
    }

    /// The starters not yet asked in this chat, in their original order. A starter
    /// counts as asked when a user turn says it (ignoring case and surrounding
    /// whitespace), so the set resets with a new chat and survives reopening the panel.
    static func remaining(_ all: [String], askedTexts: [String]) -> [String] {
        let asked = Set(askedTexts.map(normalized))
        return all.filter { !asked.contains(normalized($0)) }
    }

    private static func normalized(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    /// The chat window's own starters, shown on an empty chat and, once it has
    /// begun, as "Try another" for the ones not yet asked.
    static let chatWindow = ["What can you do?", "What's on my calendar this week?", "Any emails I need to reply to?",
                             "Draft a reply to my last email", "Is my data private?"]

    /// The source each chat-window starter reads.
    private static let chatWindowSource = ["What's on my calendar this week?": "calendar",
                                           "Any emails I need to reply to?": "email",
                                           "Draft a reply to my last email": "email"]

    /// The chat window's starters Wisp can answer now, by the same rule as Ask:
    /// none about a source it can't read, or before that is known (nil).
    static func chatStarters(unreadable: Set<String>?) -> [String] {
        let blocked = unreadable ?? Set(sources)
        return chatWindow.filter { chatWindowSource[$0].map { !blocked.contains($0) } ?? true }
    }

    /// Source states that mean Wisp can't read the source on this Mac.
    static func unreadable(states: [String: String]) -> Set<String> {
        Set(states.filter { $0.value == "unavailable" || $0.value == "disabled" }.keys)
    }
}

/// Ask's view of /inference/cloud for the goal starter (goalStarterAllowed).
/// Each panel open forgets the last answer, so a cloud model turned on since
/// never lets the goal starter show on a stale "no"; an answer to an older
/// open's request is dropped.
struct StarterCloudUse: Equatable {
    private(set) var inUse: Bool?
    private var asked = 0

    /// A panel opened: unknown again until this request's answer lands.
    mutating func opened() -> Int {
        inUse = nil
        asked += 1
        return asked
    }

    mutating func answered(_ value: Bool?, for request: Int) {
        if request == asked { inUse = value }
    }
}
