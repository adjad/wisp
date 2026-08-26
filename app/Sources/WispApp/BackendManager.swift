import Foundation

final class BackendManager {
    private var process: Process?
    private let readyURL = URL(string: "http://127.0.0.1:8765/mode")!

    func startIfNeeded() async {
        if await isHealthy() { return }
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
        proc.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONUNBUFFERED": "1",
        ]) { _, new in new }

        let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("moe-backend.log")
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
            _ = await waitUntilHealthy(timeout: 20)
        } catch {
            process = nil
        }
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
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
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
