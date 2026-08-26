import Foundation

// oMLX (port 8000, a separate menu-bar app) and the MOE backend (port 8765)
// need their ports uncontested at launch. If some other process claims one
// first — e.g. a leftover `python -m http.server` left running from a
// terminal session — oMLX/uvicorn fail to bind and the user hits a cryptic
// "port in use" dialog. Called before either server starts so MOE's ports
// always win the race; anything squatting there that isn't the legitimate
// owner gets killed.
enum PortGuard {
    static func reserve(port: Int, exemptExecutablePrefixes: [String]) {
        for pid in listeningPIDs(port: port) {
            guard let exe = executablePath(pid: pid) else { continue }
            if exemptExecutablePrefixes.contains(where: exe.hasPrefix) { continue }
            kill(pid, SIGTERM)
        }
    }

    private static func listeningPIDs(port: Int) -> [pid_t] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        p.arguments = ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-t"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return [] }
        p.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
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
        p.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (text?.isEmpty ?? true) ? nil : text
    }
}
