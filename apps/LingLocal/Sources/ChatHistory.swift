import Foundation

struct ChatHistory {
    static let maximumMessages = 64

    struct AppendResult {
        let messages: [ChatTurn]
        let removedTurns: [ChatTurn]

        var removedExchange: Bool { !removedTurns.isEmpty }
    }

    static func appendPendingUser(_ turn: ChatTurn, to history: [ChatTurn]) -> AppendResult {
        precondition(turn.role == "user", "A pending chat turn must be a user message")
        var messages = history
        var removed: [ChatTurn] = []
        while messages.count + 1 > maximumMessages {
            let exchange = removeOldestCompleteExchange(from: &messages)
            guard !exchange.isEmpty else { break }
            removed.append(contentsOf: exchange)
        }
        messages.append(turn)
        return AppendResult(messages: messages, removedTurns: removed)
    }

    static func appendAssistant(_ turn: ChatTurn, to history: [ChatTurn]) -> AppendResult {
        precondition(turn.role == "assistant", "A completed chat turn must be an assistant message")
        var messages = history
        var removed: [ChatTurn] = []
        while messages.count + 1 > maximumMessages {
            let exchange = removeOldestCompleteExchange(from: &messages)
            guard !exchange.isEmpty else { break }
            removed.append(contentsOf: exchange)
        }
        messages.append(turn)
        return AppendResult(messages: messages, removedTurns: removed)
    }

    static func rollbackPendingUser(_ id: UUID, from history: [ChatTurn]) -> [ChatTurn] {
        guard let last = history.last, last.id == id, last.role == "user" else { return history }
        return Array(history.dropLast())
    }

    static func reset() -> [ChatTurn] { [] }

    private static func removeOldestCompleteExchange(from messages: inout [ChatTurn]) -> [ChatTurn] {
        guard messages.count >= 2 else { return [] }
        for index in 0..<(messages.count - 1) {
            if messages[index].role == "user", messages[index + 1].role == "assistant" {
                let pair = Array(messages[index...index + 1])
                messages.removeSubrange(index...index + 1)
                return pair
            }
        }
        return []
    }
}
