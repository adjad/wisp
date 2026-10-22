import Foundation
import Darwin

// Source-bound synthetic retention workload. Compile this same fixture with
// baseline and candidate ChatTypes.swift, then compare its JSON. Never launches
// Wisp, uses a backend/model, writes history, or sends real tool calls.
@main
struct ChatToolMemoryChecks {
    static func main() throws {
        var messages: [ChatMessage] = []
        var inputBytes = 0
        for i in 0..<100 {
            autoreleasepool {
                // Distinct payloads, released with the event after each fold.
                // Only the reduced messages survive the scope.
                let id = String(i)
                let payload = "Synthetic result \(i) " + String(repeating: "p", count: 128)
                    + "\n" + String(repeating: Character(UnicodeScalar(65 + i % 26)!), count: 1_048_576)
                inputBytes += payload.utf8.count
                var message = ChatMessage(role: .assistant)
                _ = ChatTurnReducer.apply(ChatEvent(type: "tool_call", payload: [
                    "id": id, "name": "read_file", "decision": "allow",
                ]), to: &message)
                _ = ChatTurnReducer.apply(ChatEvent(type: "tool_result", payload: [
                    "id": id, "result": payload,
                ]), to: &message)
                messages.append(message)
            }
        }
        try withExtendedLifetime(messages) {
            var usage = rusage()
            precondition(getrusage(RUSAGE_SELF, &usage) == 0)
            let output: [String: Any] = [
                "synthetic_results": messages.count,
                "input_utf8_bytes": inputBytes,
                "retained_result_utf8_bytes": messages.reduce(0) { total, message in
                    total + message.tools.reduce(0) { $0 + $1.result.utf8.count }
                },
                "max_rss_bytes": usage.ru_maxrss,
                "visible_previews": messages.map { $0.tools[0].detail },
                "scope": "Isolated real reducer with synthetic results; not installed-app RAM or model memory.",
            ]
            let data = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
            FileHandle.standardOutput.write(data)
            print("")
        }
    }
}
