import Foundation
import Darwin

@main
struct DiagnosticReportChecks {
    static func main() throws {
        var count = 0
        func check(_ value: Bool) { precondition(value); count += 1 }
        let privateText = "PRIVATE-PROMPT"
        let details: [String: Any] = ["prompt": privateText, "api_key": "sk-secret-secret", "nested": ["authorization": "Bearer xyz-secret"],
                                      "title": "{\"token\": \"secret-secret\"}", "result": "Bearer secret-secret-secret"]
        let normal = DiagnosticReport.payload(metadata: ["surface": "chat", "client_first_token_seconds": 3.0], details: details, note: "", includeDetails: false)
        let normalData = try DiagnosticReport.encoded(normal)
        check(!String(data: normalData, encoding: .utf8)!.contains(privateText))
        check(normal["client_first_token_seconds"] as? Double == 3.0)
        let selected = DiagnosticReport.payload(metadata: [:], details: details, note: "Expected this", includeDetails: true)
        let selectedJSON = String(data: try DiagnosticReport.encoded(selected), encoding: .utf8)!
        check(selectedJSON.contains(privateText))
        check(!selectedJSON.contains("secret-secret"))
        check((selected["details"] as? [String: Any])?["title"] is String)
        check(selected["expected_result"] as? String == "Expected this")
        do {
            _ = try DiagnosticReport.encoded(["large": String(repeating: "x", count: DiagnosticReport.maximumBytes + 1)])
            preconditionFailure("Oversized report accepted")
        } catch { count += 1 }

        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["WISP_HOME"]!)
        let directory = root.appendingPathComponent("diagnostics")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let id = String(repeating: "a", count: 32)
        let file = directory.appendingPathComponent(id + ".json")
        let trace: [String: Any] = ["schema_version": 1, "trace_id": id, "kind": "agent", "status": "failed", "started_at": 1000,
                                  "PRIVATE": privateText, "events": [["stage": "error", "elapsed_ms": 10, "message": privateText]]]
        try JSONSerialization.data(withJSONObject: trace).write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        let journal = DiagnosticReport.journal(traceID: id)!
        check(!String(data: try DiagnosticReport.encoded(journal), encoding: .utf8)!.contains(privateText))
        check(journal["status"] as? String == "failed")
        check(DiagnosticReport.journal(traceID: "../bad") == nil)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: file.path)
        check(DiagnosticReport.journal(traceID: id) == nil)
        try FileManager.default.removeItem(at: file)
        let outside = root.appendingPathComponent("outside.json")
        try JSONSerialization.data(withJSONObject: trace).write(to: outside)
        try FileManager.default.createSymbolicLink(at: file, withDestinationURL: outside)
        check(DiagnosticReport.journal(traceID: id) == nil)
        print("\(count) diagnostic checks passed")
    }
}
