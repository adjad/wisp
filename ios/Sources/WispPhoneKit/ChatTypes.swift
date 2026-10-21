import Foundation

/// Where a conversation's turns run.
public enum EngineKind: String, Codable, CaseIterable, Sendable, Identifiable {
    /// Ling on this iPhone, via MLX. Nothing leaves the phone.
    case onDevice
    /// Full Wisp on the user's Mac, through the paired phone gateway over Tailscale.
    case mac
    /// An OpenAI-compatible provider, with the user's own API key.
    case cloud

    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .onDevice: "iPhone"
        case .mac: "Mac"
        case .cloud: "Cloud"
        }
    }
    public var symbol: String {
        switch self {
        case .onDevice: "iphone"
        case .mac: "laptopcomputer"
        case .cloud: "cloud"
        }
    }
    /// One line on where the user's words go, shown wherever the engine is picked.
    public var privacyLine: String {
        switch self {
        case .onDevice: "Runs entirely on this iPhone. Nothing is sent anywhere."
        case .mac: "Sent to Wisp on your Mac over your private Tailscale network."
        case .cloud: "Sent to the cloud provider you configured, using your API key."
        }
    }
}

public enum ChatRole: String, Codable, Sendable { case user, assistant, system }

public struct ChatMessage: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var role: ChatRole
    public var text: String
    public var createdAt: Date
    /// Short activity lines shown under an assistant answer ("Checked your calendar").
    public var activity: [String]
    /// Set when a turn ended with an error; the text then holds whatever arrived before it.
    public var failure: String?

    public init(id: UUID = UUID(), role: ChatRole, text: String, createdAt: Date = Date(),
                activity: [String] = [], failure: String? = nil) {
        self.id = id
        self.role = role
        self.text = text
        self.createdAt = createdAt
        self.activity = activity
        self.failure = failure
    }
}

/// A Mac tool asking for the user's go-ahead before it acts (send mail, add an event, ...).
public struct ApprovalRequest: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var requestId: String
    public var sessionId: String
    public var tool: String
    public var reason: String
    public var preview: String
    public var grantable: Bool

    public init(id: String, requestId: String, sessionId: String, tool: String,
                reason: String, preview: String, grantable: Bool) {
        self.id = id
        self.requestId = requestId
        self.sessionId = sessionId
        self.tool = tool
        self.reason = reason
        self.preview = preview
        self.grantable = grantable
    }
}

public enum ApprovalScope: String, Codable, Sendable { case once, always, never }

/// The engine-neutral stream every chat turn produces.
public enum ChatEvent: Equatable, Sendable {
    case session(String)
    case status(String)
    case delta(String)
    /// Replace the whole visible answer (the Mac sends a final `text` after deltas).
    case replace(String)
    case clear
    case activity(String)
    case approval(ApprovalRequest)
    case approvalExpired(String)
    case failed(String)
    case done
}

public struct Conversation: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var engine: EngineKind
    public var title: String
    public var messages: [ChatMessage]
    /// The Mac backend's session id, so a Mac conversation continues with its server-side history.
    public var remoteSessionId: String?
    public var updatedAt: Date

    public init(id: UUID = UUID(), engine: EngineKind, title: String = "New chat",
                messages: [ChatMessage] = [], remoteSessionId: String? = nil, updatedAt: Date = Date()) {
        self.id = id
        self.engine = engine
        self.title = title
        self.messages = messages
        self.remoteSessionId = remoteSessionId
        self.updatedAt = updatedAt
    }

    /// A title from the first user message: its first line, trimmed to 48 characters.
    public static func title(from prompt: String) -> String {
        let line = prompt.split(whereSeparator: \.isNewline).first.map(String.init) ?? prompt
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "New chat" }
        return trimmed.count <= 48 ? trimmed : String(trimmed.prefix(47)) + "…"
    }
}
