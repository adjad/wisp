import Foundation

final class BackendManager {
    private var process: Process?
    // /ping rather than /mode: it needs no API key, so a first-ever launch can
    // tell "the backend is up" from "the key file doesn't exist yet" instead of
    // reading a 401 as a dead server.
    private let readyURL = WispConfig.backendBaseURL.appendingPathComponent("ping")

    /// Why the backend isn't running, when it isn't. `nil` means healthy.
    /// AppDelegate surfaces this — a silently dead backend was the single most
    /// confusing first-run failure, since the UI comes up looking fine and then
    /// every request fails with a connection error.
    private(set) var lastError: String?

    @discardableResult
    func startIfNeeded() async -> Bool {
        if await isHealthy() { lastError = nil; return true }
        guard process == nil else { return false }

        guard let root = backendRoot() else {
            lastError = """
            Couldn't find Wisp's backend. A packaged Wisp.app carries its own \
            copy; running from a checkout needs `service/main.py` reachable — \
            set WISP_DEV_ROOT to the checkout directory.
            """
            return false
        }
        guard let python = pythonPath(in: root) else {
            lastError = """
            Couldn't find a Python interpreter to run the backend. Run \
            scripts/setup.sh in \(root.path) to create one, or set \
            WISP_PYTHON to an interpreter that has Wisp's dependencies.
            """
            return false
        }

        let proc = Process()
        proc.executableURL = python
        proc.currentDirectoryURL = root
        proc.arguments = [
            "-m", "uvicorn",
            "service.main:app",
            "--host", WispConfig.backendHost,
            "--port", "\(WispConfig.backendPort)",
        ]
        proc.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONUNBUFFERED": "1",
        ]) { _, new in new }

        let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-backend.log")
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
            try proc.run()
            process = proc
        } catch {
            process = nil
            lastError = "Couldn't launch the backend: \(error.localizedDescription)"
            return false
        }

        if await waitUntilHealthy(timeout: 20) {
            lastError = nil
            return true
        }

        // It launched but never answered. Almost always a Python-side import
        // error, and the traceback is already in the log — point at it rather
        // than making the user go looking.
        lastError = """
        The backend started but never became ready on port \
        \(WispConfig.backendPort). See \(logURL.path) for what it printed.
        """
        return false
    }

    func stop() {
        guard let process else { return }
        if process.isRunning {
            process.terminate()
        }
        self.process = nil
    }

    private func waitUntilHealthy(timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await isHealthy() { return true }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return false
    }

    private func isHealthy() async -> Bool {
        var req = URLRequest(url: readyURL)
        req.timeoutInterval = 1
        do {
            let (_, resp) = try await WispSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }

    private func backendRoot() -> URL? {
        // A packaged Wisp.app carries its own copy of the backend and should
        // always use it — this is the normal case.
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("backend"),
           hasBackend(bundled) {
            return bundled
        }

        // Otherwise we're running from a checkout (`swift run`, or the binary
        // straight out of .build). WispConfig offers WISP_DEV_ROOT first, then
        // each directory above the executable — so a checkout is found by
        // where it actually is, not by a path baked in at author time.
        return WispConfig.devRootCandidates.first(where: hasBackend)
    }

    private func hasBackend(_ root: URL) -> Bool {
        FileManager.default.fileExists(
            atPath: root.appendingPathComponent("service/main.py").path
        )
    }

    private func pythonPath(in root: URL) -> URL? {
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["WISP_PYTHON"],
           !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        candidates.append(root.appendingPathComponent(".venv/bin/python"))

        // Deliberately no /usr/bin/python3 fallback. macOS ships a 3.9 command
        // line tools stub there; it exists on every Mac and has none of Wisp's
        // dependencies, so falling back to it just converted "no interpreter"
        // into "backend dies on import" — the same dead backend, one step
        // further from the cause. Better to report that setup hasn't been run.
        return candidates.first { canRunBackend($0) }
    }

    /// An interpreter is only useful if it can actually import the server.
    /// One cheap subprocess here turns a whole class of silent runtime failure
    /// into a specific message at launch.
    private func canRunBackend(_ python: URL) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: python.path) else { return false }
        let p = Process()
        p.executableURL = python
        p.arguments = ["-c", "import uvicorn, fastapi"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }
}
