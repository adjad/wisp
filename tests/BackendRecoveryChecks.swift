// Compile with BackendManager.swift. No process, network, UI, or clipboard action occurs.
import Foundation

@main
enum BackendRecoveryChecks {
    static func main() throws {
        let first = "AssistantStore startup stopped: older stale diagnostic"
        let expected = "AssistantStore startup stopped: live and legacy rows conflict. "
            + "Data was retained. Inspect with an exact command."
        let log = "unrelated warning\n\(first)\nTraceback line\nRuntimeError: \(expected)\nmore noise"

        precondition(
            BackendManager.assistantStoreRecoveryDiagnostic(in: log) == expected,
            "the newest diagnostic should be extracted without traceback text"
        )
        precondition(
            BackendManager.assistantStoreRecoveryDiagnostic(in: "RuntimeError: unrelated") == nil,
            "unrelated startup failures must fail closed"
        )
        precondition(
            BackendManager.assistantStoreRecoveryDiagnostic(
                in: "prefix \(expected)\nprivate traceback details"
            ) == expected,
            "extraction must stop at the diagnostic line"
        )

        let logURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-backend-recovery-\(UUID().uuidString).log")
        defer { try? FileManager.default.removeItem(at: logURL) }
        try "\(first)\n".write(to: logURL, atomically: true, encoding: .utf8)
        let offset = BackendManager.fileSize(at: logURL)
        let handle = try FileHandle(forWritingTo: logURL)
        try handle.seekToEnd()
        try handle.write(contentsOf: Data("RuntimeError: unrelated current failure\n".utf8))
        try handle.close()
        let currentLaunchLog = BackendManager.logText(at: logURL, after: offset) ?? ""
        precondition(
            BackendManager.assistantStoreRecoveryDiagnostic(in: currentLaunchLog) == nil,
            "an old recovery marker must not leak into an unrelated current failure"
        )
        print("BackendRecovery: 4 regression checks passed")
    }
}
