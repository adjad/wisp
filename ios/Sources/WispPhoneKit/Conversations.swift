import Foundation

/// The system prompt for the engines that don't run Wisp's own backend (iPhone and Cloud).
public enum PhonePrompt {
    public static func system(engine: EngineKind, now: Date = Date(), timeZone: TimeZone = .current) -> String {
        let formatter = DateFormatter()
        formatter.timeZone = timeZone
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "EEEE, MMMM d, yyyy 'at' h:mm a zzz"
        let place = engine == .onDevice ? "running entirely on the user's iPhone" : "running on a cloud model the user chose"
        return """
        You are Wisp, a calm, concise personal assistant \(place).
        Current date and time: \(formatter.string(from: now)).

        IMPORTANT rules:
        - Answer directly. Short paragraphs or a few bullets; no filler or preamble.
        - You have no tools in this mode: you cannot read the user's mail, messages, calendar, reminders or files, and you cannot send, schedule or change anything.
        - Never claim you did something you can't do. When a request needs those, say it works in Mac mode, where Wisp has those tools.
        - If you don't know, say so. Don't invent facts, names, dates or numbers.
        """
    }

    /// OpenAI-style messages: system prompt plus as much recent history as fits `characterBudget`.
    public static func messages(system: String, history: [ChatMessage],
                                characterBudget: Int = 24_000) -> [[String: String]] {
        var kept: [[String: String]] = []
        var used = 0
        for message in history.reversed() where message.role != .system {
            let text = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if used + text.count > characterBudget, !kept.isEmpty { break }
            used += text.count
            kept.append(["role": message.role.rawValue, "content": text])
        }
        // A window must start with the user's turn, not a dangling answer.
        while kept.last?["role"] == ChatRole.assistant.rawValue { kept.removeLast() }
        return [["role": "system", "content": system]] + kept.reversed()
    }
}

/// Saves conversations as one JSON file in the app's protected Application Support directory.
public struct ConversationStore: Sendable {
    public let fileURL: URL

    public init(fileURL: URL) { self.fileURL = fileURL }

    public static func standard() throws -> ConversationStore {
        let directory = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                    appropriateFor: nil, create: true)
            .appendingPathComponent("Wisp", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return ConversationStore(fileURL: directory.appendingPathComponent("conversations.json"))
    }

    public func load() -> [Conversation] {
        guard let data = try? Data(contentsOf: fileURL) else { return [] }
        return (try? JSONDecoder().decode([Conversation].self, from: data)) ?? []
    }

    public func save(_ conversations: [Conversation]) throws {
        // Default date encoding: exact round trip (ISO 8601 would drop fractional seconds).
        let data = try JSONEncoder().encode(conversations)
        #if os(iOS)
        try data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: fileURL, options: .atomic)
        #endif
    }
}
