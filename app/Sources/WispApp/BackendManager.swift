import AppKit
import Foundation
import CryptoKit

/// Recovery reservations are synchronous: one monitor tick consumes the pending
/// launch before any health check or credential read can suspend the manager.
struct BackendRecoveryState {
    private(set) var launchedGeneration: String?
    private(set) var blocked = false
    private var rejectedGenerations: Set<String> = []
    private var reservedGeneration: String?

    mutating func observe(_ current: String?) -> Bool {
        let valid = current.map { $0 == "absent" || BackendCredentials.valid($0) } ?? false
        let expected = launchedGeneration ?? reservedGeneration
        if !valid || (expected != nil && expected != current) {
            quarantine()
        }
        return valid && !blocked
    }

    mutating func quarantine() {
        if let expected = launchedGeneration ?? reservedGeneration {
            rejectedGenerations.insert(expected)
        }
        blocked = true
    }

    mutating func reserveRecovery(current: String?, processRunning: Bool, starting: Bool) -> Bool {
        guard blocked, !processRunning, !starting, let current,
              BackendCredentials.valid(current), !rejectedGenerations.contains(current) else { return false }
        launchedGeneration = nil
        reservedGeneration = current
        blocked = false
        return true
    }

    mutating func didLaunch(generation: String) {
        launchedGeneration = generation
        reservedGeneration = nil
    }
}

@MainActor
final class BackendManager {
    private var monitor: Task<Void, Never>?
    private var credentialState = BackendRecoveryState()
    private var starting = false
    private var lifecycle = UUID()

    private func enforceCredentialState() -> Bool {
        let allowed = credentialState.observe(try? BackendCredentials.generation())
        if !allowed, let process, process.isRunning { process.terminate() }
        return allowed
    }

    private func startMonitor() {
        guard monitor == nil else { return }
        monitor = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 500_000_000)
                guard !Task.isCancelled, let self else { return }
                _ = self.enforceCredentialState()
                if self.credentialState.reserveRecovery(
                    current: try? BackendCredentials.generation(),
                    processRunning: self.process?.isRunning == true, starting: self.starting
                ) {
                    self.process = nil
                    await self.startIfNeeded(freshRecovery: true)
                }
            }
        }
    }
    private var process: Process?
    private var recoveryAlertShown = false
    private let readyURL = URL(string: "http://127.0.0.1:8765/mode")!

    func startIfNeeded() async {
        await startIfNeeded(freshRecovery: false)
    }

    private func startIfNeeded(freshRecovery: Bool) async {
        startMonitor()
        guard !starting, enforceCredentialState() else { return }
        starting = true
        defer { starting = false }
        let lifecycle = self.lifecycle
        let healthy = await isHealthy()
        guard lifecycle == self.lifecycle, enforceCredentialState() else { return }
        if healthy && !freshRecovery { return }
        guard process == nil, let root = backendRoot(), let python = pythonPath(in: root) else {
            return
        }

        let proc = Process()
        proc.executableURL = python
        proc.currentDirectoryURL = root
        proc.arguments = [
            "-m", "uvicorn",
            "service.main:app",
            "--host", "127.0.0.1",
            "--port", "8765",
        ]
        let configDigest = Self.configurationDigest()
        let snapshot: BackendCredentials.Snapshot
        let credentialPipe = Pipe()
        defer {
            try? credentialPipe.fileHandleForReading.close()
            try? credentialPipe.fileHandleForWriting.close()
        }
        do {
            snapshot = try BackendCredentials.loadForBackend()
            proc.environment = Self.backendEnvironment(credentials: snapshot.credentials,
                                                       generation: snapshot.generation)
            proc.environment?["WISP_CREDENTIAL_PIPE"] = try BackendCredentials.pipeMetadata(credentialPipe)
            proc.standardInput = credentialPipe
        } catch {
            // Re-arm recovery if the marker appeared during the credential read.
            // Denied/malformed Keychain data remains closed without logging values.
            if case BackendCredentials.Failure.quarantined = error {
                credentialState.quarantine()
            }
            _ = enforceCredentialState()
            return
        }

        let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("moe-backend.log")
        let logStartOffset = Self.fileSize(at: logURL)
        if FileManager.default.fileExists(atPath: logURL.path),
           let handle = try? FileHandle(forWritingTo: logURL) {
            do {
                try handle.seekToEnd()
            } catch {}
            proc.standardOutput = handle
            proc.standardError = handle
        } else if FileManager.default.createFile(atPath: logURL.path, contents: nil),
                  let handle = try? FileHandle(forWritingTo: logURL) {
            proc.standardOutput = handle
            proc.standardError = handle
        }

        do {
            guard enforceCredentialState(),
                  try BackendCredentials.generation() == snapshot.generation else {
                credentialState.quarantine()
                _ = enforceCredentialState()
                return
            }
            try proc.run()
            do {
                try credentialPipe.fileHandleForReading.close()
                try BackendCredentials.writePipe(credentialPipe, credentials: snapshot.credentials,
                    generation: snapshot.generation, role: "primary", pid: proc.processIdentifier)
            } catch {
                if proc.isRunning { proc.terminate() }
                return
            }
            process = proc
            credentialState.didLaunch(generation: snapshot.generation)
            let healthy = await waitUntilHealthy(timeout: 20, process: proc)
            if healthy, let configDigest, proc.isRunning, enforceCredentialState() {
                do {
                    try Self.publishRuntimeReceipt(generation: snapshot.generation, pid: proc.processIdentifier,
                                                   configDigest: configDigest)
                } catch {
                    credentialState.quarantine()
                    proc.terminate()
                }
            }
            if !healthy {
                if !proc.isRunning { process = nil }
                if let text = Self.logText(at: logURL, after: logStartOffset),
                   let diagnostic = Self.assistantStoreRecoveryDiagnostic(in: text) {
                    await presentRecoveryDiagnostic(diagnostic)
                }
            }
        } catch {
            process = nil
            if case BackendCredentials.Failure.quarantined = error {
                credentialState.quarantine()
            }
            _ = enforceCredentialState()
        }
    }

    func stop() {
        lifecycle = UUID()
        monitor?.cancel()
        monitor = nil
        credentialState = BackendRecoveryState()
        guard let process else { return }
        if process.isRunning {
            process.terminate()
        }
        self.process = nil
    }

    private func waitUntilHealthy(timeout: TimeInterval, process: Process) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            guard enforceCredentialState() else { return false }
            let healthy = await isHealthy()
            guard enforceCredentialState() else { return false }
            if !process.isRunning { return false }
            if healthy { return true }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return false
    }

    /// Receipt attests only a fresh owned process, its listener, and unchanged
    /// overlay bytes. It never attests model health or a different process.
    nonisolated static func acceptableRuntimeGeneration(_ generation: String) -> Bool {
        generation == "absent" || BackendCredentials.valid(generation)
    }

    nonisolated static func configurationDigest(home: String = NSHomeDirectory()) -> String? {
        let path = home + "/.moe/config.yaml"
        let fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1,
              info.st_mode & 0o022 == 0, info.st_size >= 0, info.st_size <= 1048576 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        guard let data = try? handle.readToEnd(), data.count == info.st_size else { return nil }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    nonisolated static func publishRuntimeReceipt(generation: String, pid: Int32, configDigest: String) throws {
        guard acceptableRuntimeGeneration(generation), pid > 0,
              configurationDigest() == configDigest,
              try BackendCredentials.generation() == generation else { throw BackendCredentials.Failure.quarantined }
        let process = Process()
        let output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-nP", "-a", "-iTCP:8765", "-sTCP:LISTEN", "-Fp"]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
        guard !process.isRunning else {
            process.terminate()
            throw BackendCredentials.Failure.unavailable
        }
        let lines = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .split(separator: "\n").map(String.init) ?? []
        guard process.terminationStatus == 0, lines == ["p\(pid)"],
              configurationDigest() == configDigest,
              try BackendCredentials.generation() == generation else { throw BackendCredentials.Failure.quarantined }
        let receipt = URL(fileURLWithPath: NSHomeDirectory() + "/.moe/backend-runtime.json")
        let data = try JSONSerialization.data(withJSONObject: ["schema_version": 1, "generation": generation,
                "pid": pid, "config_sha256": configDigest], options: [.sortedKeys])
        var info = stat()
        if lstat(receipt.path, &info) == 0 {
            guard info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(),
                  info.st_nlink == 1, info.st_mode & 0o7777 == 0o600 else {
                throw BackendCredentials.Failure.quarantined
            }
        } else if errno != ENOENT { throw BackendCredentials.Failure.quarantined }
        let temporary = receipt.path + "." + UUID().uuidString
        let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw BackendCredentials.Failure.unavailable }
        defer { close(fd); unlink(temporary) }
        let count = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        guard count == data.count, fsync(fd) == 0,
              try BackendCredentials.generation() == generation,
              rename(temporary, receipt.path) == 0 else { throw BackendCredentials.Failure.quarantined }
    }

    private func isHealthy() async -> Bool {
        var req = URLRequest(url: readyURL)
        req.timeoutInterval = 1
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    /// Return only the actionable AssistantStore message from a new log
    /// segment. Tracebacks and unrelated backend failures must never be shown
    /// as migration instructions.
    nonisolated static func assistantStoreRecoveryDiagnostic(in logText: String) -> String? {
        let marker = "AssistantStore startup stopped:"
        guard let markerRange = logText.range(of: marker, options: .backwards) else {
            return nil
        }
        let suffix = logText[markerRange.lowerBound...]
        guard let line = suffix.split(whereSeparator: { $0.isNewline }).first else {
            return nil
        }
        return String(line).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Environment for the packaged backend.
    ///
    /// Bytecode must be cached outside the bundle: writing .pyc beside the
    /// packaged sources adds unsealed files to Wisp.app and invalidates its
    /// code signature on first launch. Caching outside preserves import speed.
    nonisolated static func backendEnvironment(
        base: [String: String] = ProcessInfo.processInfo.environment,
        home: String = NSHomeDirectory(),
        credentials: [String: String] = [:],
        generation: String = "absent"
    ) -> [String: String] {
        BackendCredentials.injecting(credentials, into: base).merging([
            "WISP_CREDENTIAL_GENERATION": generation,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPYCACHEPREFIX": (home as NSString)
                .appendingPathComponent(".moe/cache/pycache"),
        ]) { _, new in new }
    }

    nonisolated static func fileSize(at url: URL) -> UInt64 {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attributes[.size] as? NSNumber else { return 0 }
        return size.uint64Value
    }

    nonisolated static func logText(at url: URL, after offset: UInt64) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        do {
            try handle.seek(toOffset: offset)
            guard let data = try handle.readToEnd(), !data.isEmpty else { return nil }
            return String(data: data, encoding: .utf8)
        } catch {
            return nil
        }
    }

    private func presentRecoveryDiagnostic(_ diagnostic: String) async {
        guard !recoveryAlertShown else { return }
        recoveryAlertShown = true
        await MainActor.run {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Wisp needs help recovering assistant data"
            alert.informativeText = diagnostic
            alert.addButton(withTitle: "Copy recovery instructions")
            alert.addButton(withTitle: "Close")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(diagnostic, forType: .string)
            }
        }
    }

    private func backendRoot() -> URL? {
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("backend"),
           FileManager.default.fileExists(atPath: bundled.appendingPathComponent("service/main.py").path) {
            return bundled
        }

        let devRoot = URL(fileURLWithPath: "/Users/adijain/Desktop/MOE_Project")
        if FileManager.default.fileExists(atPath: devRoot.appendingPathComponent("service/main.py").path) {
            return devRoot
        }

        return nil
    }

    private func pythonPath(in root: URL) -> URL? {
        let bundledPython = root.appendingPathComponent(".venv/bin/python")
        if FileManager.default.fileExists(atPath: bundledPython.path) {
            return bundledPython
        }

        let systemPython = URL(fileURLWithPath: "/usr/bin/python3")
        if FileManager.default.fileExists(atPath: systemPython.path) {
            return systemPython
        }

        return nil
    }
}
