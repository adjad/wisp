import Foundation

// oMLX (port 8000, a separate menu-bar app) and the Wisp backend (port 8765)
// need their ports uncontested at launch. If something else has claimed one,
// oMLX/uvicorn fail to bind and the user hits a cryptic "port in use" error.
//
// This used to SIGTERM whatever was holding the port. That is fine on the one
// machine where the only thing ever listening on :8000 is oMLX, and hostile
// everywhere else: :8000 is the default for `python -m http.server`, Django,
// and a dozen other dev servers, and killing a stranger's work to launch a
// menu-bar app is not a trade Wisp gets to make on their behalf. So this type
// now only *reports*. Terminating is a separate call, and AppDelegate only
// makes it after the user has said so in a dialog.
enum PortGuard {
    struct Conflict: Equatable {
        let pid: pid_t
        let executable: String
        let port: Int

        /// Last path component, for display — full paths are long and the
        /// interesting part ("Python", "node") is at the end.
        var processName: String {
            (executable as NSString).lastPathComponent
        }
    }

    /// Everything listening on `port` that isn't a legitimate owner.
    /// `expectedOwnerPrefixes` are executable-path prefixes to ignore — e.g.
    /// oMLX's own bundle, which is *supposed* to be the one on :8000.
    static func conflicts(port: Int, expectedOwnerPrefixes: [String]) -> [Conflict] {
        listeningPIDs(port: port).compactMap { pid in
            guard let exe = executablePath(pid: pid) else { return nil }
            if expectedOwnerPrefixes.contains(where: exe.hasPrefix) { return nil }
            return Conflict(pid: pid, executable: exe, port: port)
        }
    }

    /// Ask a process to quit. Only ever called with explicit user consent —
    /// SIGTERM, not SIGKILL, so it still gets to shut down cleanly.
    static func terminate(_ conflict: Conflict) {
        kill(conflict.pid, SIGTERM)
    }

    private static func listeningPIDs(port: Int) -> [pid_t] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        p.arguments = ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-t"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return [] }
        // Read before waiting: waitUntilExit() first can deadlock if the child
        // fills the pipe buffer and blocks on write while we block on exit.
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { pid_t($0.trimmingCharacters(in: .whitespaces)) }
    }

    private static func executablePath(pid: pid_t) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/ps")
        p.arguments = ["-p", "\(pid)", "-o", "comm="]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (text?.isEmpty ?? true) ? nil : text
    }
}
