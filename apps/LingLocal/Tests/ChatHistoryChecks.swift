import Foundation

@main
enum ChatHistoryChecks {
    static func main() {
        var original: [ChatTurn] = []
        for index in 0..<32 {
            original.append(ChatTurn(role: "user", content: "question-\(index)"))
            original.append(ChatTurn(role: "assistant", content: "answer-\(index)"))
        }

        let pending = ChatTurn(role: "user", content: "question-32")
        let append = ChatHistory.appendPendingUser(pending, to: original)
        require(append.messages.count == 63, "pending request must stay below the 64-message maximum")
        require(append.removedExchange, "adding the 33rd exchange must report trimming")
        require(append.removedTurns.map(\.content) == ["question-0", "answer-0"], "trim must remove one oldest complete exchange")
        require(append.messages.first?.content == "question-1", "retained history must start at the next complete exchange")

        let completed = ChatHistory.appendAssistant(ChatTurn(role: "assistant", content: "answer-32"), to: append.messages)
        require(completed.messages.count == 64, "completed request history must remain within the API limit")
        require(completed.messages.suffix(2).map(\.role) == ["user", "assistant"], "new exchange must remain complete")

        let rolledBack = ChatHistory.rollbackPendingUser(pending.id, from: append.messages)
        require(rolledBack == Array(append.messages.dropLast()), "failed send must remove only its pending user turn")
        require(ChatHistory.reset().isEmpty, "new chat must clear all conversation history")
        print("ChatHistory checks passed")
    }

    private static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            fputs("ChatHistory check failed: \(message)\n", stderr)
            exit(1)
        }
    }
}
