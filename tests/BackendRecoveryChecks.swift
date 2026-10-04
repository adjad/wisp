// Compile with BackendManager.swift, BackendCredentials.swift, PortGuard.swift and BackendOwnership.swift. The quit/relaunch checks spawn only short-lived /bin/sh children of their own; no network, UI, or clipboard action occurs.
import Foundation

@main
enum BackendRecoveryChecks {
    static func main() async throws {
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

        precondition(
            BackendManager.backendEnvironment(
                base: ["WISP_BROWSER_BRIDGE_CONTROL": "/tmp/inherited.sock", "PATH": "/usr/bin"]
            )["WISP_BROWSER_BRIDGE_CONTROL"] == nil,
            "an inherited browser bridge endpoint must never reach the backend; the app sets it per launch"
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
        try await quitRelaunchChecks()
    }

    // MARK: - P2-1: quit, then a fast relaunch, must not latch a false port conflict

    private static func conflict(_ pid: Int32 = 4242) -> PortGuard.Verdict {
        .conflict([PortGuard.Listener(pid: pid, path: "/usr/bin/other")])
    }

    private static func quitRelaunchChecks() async throws {
        var checks = 0
        func expect(_ condition: Bool, _ message: String) {
            precondition(condition, message)
            checks += 1
        }

        // Startup: a holder that is the previous run's backend still shutting down goes away
        // within the window, so no conflict is latched.
        var calls = 0
        var slept: [TimeInterval] = []
        let leaving = BackendManager.settledStartupVerdict(retries: 12, interval: 0.25, check: {
            calls += 1
            return calls <= 3 ? conflict() : .free
        }, sleep: { slept.append($0) })
        expect(leaving == .free, "a holder that exits inside the window must not end as a conflict")
        expect(calls == 4 && slept == [0.25, 0.25, 0.25], "the check retries once per interval until the holder is gone")

        // A foreign holder that never exits is still a conflict once the window is spent, and the
        // window is bounded (about 3 seconds at the defaults), never an endless wait.
        calls = 0
        slept = []
        let stuck = BackendManager.settledStartupVerdict(retries: 12, interval: 0.25, check: {
            calls += 1
            return conflict()
        }, sleep: { slept.append($0) })
        expect(stuck == conflict(), "a holder that never exits must still be reported as a conflict")
        expect(calls == 13 && slept.count == 12 && slept.reduce(0, +) == 3.0, "the retry window is bounded at 3 seconds")

        // Nothing waits when the port is already settled, or when the listing cannot be read.
        for settled in [PortGuard.Verdict.free, .unknown] {
            calls = 0
            slept = []
            let verdict = BackendManager.settledStartupVerdict(check: { calls += 1; return settled },
                                                               sleep: { slept.append($0) })
            expect(verdict == settled && calls == 1 && slept.isEmpty, "a settled port is judged once, without waiting")
        }
        // The retry never changes who is blamed: the holder reported last is what the person sees.
        calls = 0
        let swapped = BackendManager.settledStartupVerdict(retries: 2, interval: 0.1, check: {
            calls += 1
            return conflict(Int32(100 + calls))
        }, sleep: { _ in })
        expect(swapped == conflict(103), "the conflict reported is the holder seen on the last look")

        // Quit: waiting for the child to exit is bounded, and says whether it did.
        var polls = 0
        let exited = await BackendManager.waitForExit(timeout: 5, interval: 0.05, isRunning: {
            polls += 1
            return polls <= 4
        }, sleep: { _ in })
        expect(exited && polls == 5, "the wait ends as soon as the child has exited")
        polls = 0
        let never = await BackendManager.waitForExit(timeout: 5, interval: 0.05, isRunning: {
            polls += 1
            return true
        }, sleep: { _ in })
        expect(!never && polls == 101, "the wait gives up after the timeout")
        let alreadyGone = await BackendManager.waitForExit(timeout: 5, interval: 0.05, isRunning: { false }, sleep: { _ in
            preconditionFailure("an exited child needs no wait")
        })
        expect(alreadyGone, "an exited child is not waited for")

        // Real children, spawned by this check (never anything else): a child that needs a moment to
        // shut down is waited for rather than abandoned, and one that ignores SIGTERM is escalated
        // to, on that Process only.
        // Shell builtins only (`read -t` waits on a pipe nobody writes to): Simulation QA's sandbox
        // allows /bin/sh but not /bin/sleep.
        var quietPipes: [Pipe] = []
        func spawn(_ script: String) -> Process? {
            let child = Process()
            child.executableURL = URL(fileURLWithPath: "/bin/sh")
            child.arguments = ["-c", script]
            let quiet = Pipe()
            quietPipes.append(quiet)
            child.standardInput = quiet
            child.standardOutput = FileHandle.nullDevice
            child.standardError = FileHandle.nullDevice
            do { try child.run() } catch { return nil }
            return child
        }
        if let slow = spawn("trap 'read -t 1 x; exit 0' TERM; while :; do read -t 1 x; done") {
            try await Task.sleep(nanoseconds: 300_000_000)
            expect(slow.isRunning, "the delayed-exit child must still be running before quit")
            let started = Date()
            let clean = await BackendManager.terminateAndWait(slow, timeout: 5)
            expect(clean && !slow.isRunning, "a child that exits after a delay is waited for until it is gone")
            expect(Date().timeIntervalSince(started) >= 0.9, "quit really waited for the delayed exit")
            expect(slow.terminationReason == .exit && slow.terminationStatus == 0, "it left on SIGTERM, not by force")
        } else {
            print("BackendRecovery: skipped the delayed-exit child (cannot spawn in this sandbox)")
        }
        if let stubborn = spawn("trap '' TERM; while :; do read -t 1 x; done") {
            try await Task.sleep(nanoseconds: 300_000_000)
            expect(stubborn.isRunning, "the stubborn child must still be running before quit")
            let started = Date()
            let clean = await BackendManager.terminateAndWait(stubborn, timeout: 0.6)
            expect(!clean && !stubborn.isRunning, "a child that ignores SIGTERM is escalated to and gone before quit completes")
            expect(Date().timeIntervalSince(started) < 4, "the escalation is bounded")
            expect(stubborn.terminationReason == .uncaughtSignal && stubborn.terminationStatus == SIGKILL,
                   "only the spawned Process was signalled, with SIGKILL")
        } else {
            print("BackendRecovery: skipped the stubborn child (cannot spawn in this sandbox)")
        }
        withExtendedLifetime(quietPipes) {}
        let gone = Process()
        expect(await BackendManager.terminateAndWait(gone, timeout: 1), "a Process that never ran is not signalled")
        print("BackendRecovery: \(checks) quit/relaunch checks passed")
    }
}
