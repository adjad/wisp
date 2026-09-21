// Compile with BackendManager.swift and BackendCredentials.swift. No process, network, UI, or clipboard action occurs.
import Foundation

@main
enum BackendRecoveryChecks {
    static func main() throws {
        let epoch = String(repeating: "a", count: 64)
        let recoveredEpoch = String(repeating: "b", count: 64)
        precondition(BackendManager.acceptableRuntimeGeneration("absent"))
        precondition(BackendManager.acceptableRuntimeGeneration(epoch))
        precondition(!BackendManager.acceptableRuntimeGeneration("invalid"))
        precondition(BackendManager.listenerPIDs(Data("13796\n".utf8)) == [13796])
        precondition(BackendManager.listenerPIDs(Data("13796\n13800\n".utf8)) == [13796, 13800])
        precondition(BackendManager.listenerPIDs(Data()) == [])
        precondition(BackendManager.listenerPIDs(Data("p13796\nf3\n".utf8)) == nil)
        let home = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-start-recovery-\(UUID().uuidString)")
        let directory = home.appendingPathComponent(".moe")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: home) }
        let marker = directory.appendingPathComponent(".helper-transaction.json")
        let generation = directory.appendingPathComponent(".credential-generation")
        func writeGeneration(_ value: String) throws {
            try value.write(to: generation, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: generation.path)
        }
        func probe() -> String? { try? BackendCredentials.generation(home: home.path) }
        var state = BackendRecoveryState()
        var launches = 0
        var running = false
        var starting = false
        func tick() {
            _ = state.observe(probe())
            if state.reserveRecovery(current: probe(), processRunning: running, starting: starting) {
                starting = true
                // Production consumes the same reservation before its first await.
                precondition(!state.reserveRecovery(current: probe(), processRunning: false, starting: true))
                let snapshot = try! BackendCredentials.loadForBackend(reader: { _ in nil }, state: {
                    try BackendCredentials.generation(home: home.path)
                })
                precondition(state.observe(snapshot.generation))
                launches += 1
                running = true
                state.didLaunch(generation: snapshot.generation)
                starting = false
            }
        }
        try writeGeneration(epoch)
        // A secret-bearing marker is never read or propagated into diagnostics.
        let secret = "synthetic-marker-value-do-not-log"
        try secret.write(to: marker, atomically: true, encoding: .utf8)
        precondition(!state.observe(probe()) && state.blocked && state.launchedGeneration == nil)
        for _ in 0..<10 { tick() }
        precondition(launches == 0)
        try FileManager.default.removeItem(at: marker)
        try FileManager.default.removeItem(at: generation)
        tick() // Missing generation must not clear blocked startup.
        try writeGeneration("invalid-" + secret)
        tick()
        precondition(launches == 0 && state.blocked)
        try writeGeneration(recoveredEpoch)
        tick()
        for _ in 0..<10 { tick() }
        precondition(launches == 1 && !state.blocked && state.launchedGeneration == recoveredEpoch)
        try secret.write(to: marker, atomically: true, encoding: .utf8)
        tick()
        precondition(state.blocked && launches == 1)
        try FileManager.default.removeItem(at: marker)
        for _ in 0..<10 { tick() } // Neither a running old process nor its stale epoch can recover.
        running = false
        tick()
        precondition(launches == 1 && state.blocked)
        try writeGeneration(String(repeating: "c", count: 64))
        starting = true
        tick()
        precondition(launches == 1)
        starting = false
        tick()
        for _ in 0..<10 { tick() }
        precondition(launches == 2 && !state.blocked)
        // A marker/generation race after reservation must re-arm quarantine.
        var raced = BackendRecoveryState()
        precondition(!raced.observe(nil))
        precondition(raced.reserveRecovery(current: epoch, processRunning: false, starting: false))
        precondition(!raced.observe(nil))
        precondition(!raced.reserveRecovery(current: epoch, processRunning: false, starting: false))
        precondition(raced.reserveRecovery(current: recoveredEpoch, processRunning: false, starting: false))
        precondition(!raced.observe(epoch))
        precondition(raced.blocked)
        var transient = BackendRecoveryState()
        precondition(!transient.observe(nil))
        precondition(transient.reserveRecovery(current: epoch, processRunning: false, starting: false))
        // The credential loader observed quarantine even if a later probe no longer does.
        transient.quarantine()
        precondition(!transient.observe(epoch))
        precondition(!transient.reserveRecovery(current: epoch, processRunning: false, starting: false))
        precondition(transient.reserveRecovery(current: recoveredEpoch, processRunning: false, starting: false))
        var initialRace = BackendRecoveryState()
        precondition(initialRace.observe(epoch))
        initialRace.quarantine() // Snapshot mismatch before first launch must arm retry.
        precondition(initialRace.reserveRecovery(current: recoveredEpoch, processRunning: false, starting: false))
        precondition(BackendManager.assistantStoreRecoveryDiagnostic(in: secret) == nil)
        let bound = BackendManager.backendEnvironment(base: ["WISP_CREDENTIAL_GENERATION": "inherited"], generation: epoch)
        precondition(bound["WISP_CREDENTIAL_GENERATION"] == epoch)
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
        // Bytecode written beside the packaged sources adds unsealed files to
        // Wisp.app and breaks its code signature on first launch.
        let env = BackendManager.backendEnvironment(
            base: ["PATH": "/usr/bin"], home: "/tmp/wisp-home-fixture"
        )
        precondition(
            env["PYTHONPYCACHEPREFIX"] == "/tmp/wisp-home-fixture/.moe/cache/pycache",
            "bytecode must be cached outside the app bundle"
        )
        precondition(env["PYTHONUNBUFFERED"] == "1", "backend logs must stay unbuffered")
        precondition(env["PATH"] == "/usr/bin", "inherited environment must be preserved")
        precondition(
            BackendManager.backendEnvironment(
                base: ["PYTHONPYCACHEPREFIX": "/inside/Wisp.app"], home: "/tmp/wisp-home-fixture"
            )["PYTHONPYCACHEPREFIX"] == "/tmp/wisp-home-fixture/.moe/cache/pycache",
            "an inherited cache prefix must not redirect bytecode into the bundle"
        )

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
        print("BackendRecovery: 14 regression checks passed")
    }
}
