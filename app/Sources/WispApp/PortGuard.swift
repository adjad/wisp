import Darwin
import Foundation

// Wisp's backend listens on 127.0.0.1:8765 and oMLX on 8000. They need those
// ports, and the app used to guarantee it by SIGTERM-ing whatever else was
// listening (a leftover `python -m http.server`, but also an unrelated
// development server, another app, or the previous Wisp's own healthy backend).
// That is not safe: a port says nothing about who owns the process, and killing a
// stranger risks their unsaved work. A second Wisp launch terminated the first
// one's backend and then raced against its shutdown.
//
// The policy now is: Wisp signals ONLY a process that provably is its own backend,
// never a stranger. Ownership is the kernel-reported executable path
// (proc_pidpath), which a process cannot claim by naming itself, and must lie
// inside Wisp's own backend directory. Anything else is reported to the user.
enum PortGuard {
    struct Listener: Equatable {
        let pid: Int32
        let path: String
    }

    enum Verdict: Equatable {
        /// Nothing is listening.
        case free
        /// The listeners could not be inspected. Never act on this.
        case unknown
        /// Every listener is Wisp's own backend.
        case owned([Listener])
        /// At least one listener is not Wisp's. These are NEVER terminated.
        case conflict([Listener])
    }

    /// Pure policy: classifies listeners, signals nothing.
    static func verdict(listeners: [Listener]?, ownedPrefixes: [String]) -> Verdict {
        guard let listeners else { return .unknown }
        if listeners.isEmpty { return .free }
        let foreign = listeners.filter { !isOwned($0, ownedPrefixes: ownedPrefixes) }
        return foreign.isEmpty ? .owned(listeners) : .conflict(foreign)
    }

    static func isOwned(_ listener: Listener, ownedPrefixes: [String]) -> Bool {
        // An empty prefix would match every path; a relative or unresolved path
        // proves nothing about where the executable really is.
        listener.path.hasPrefix("/") && !listener.path.contains("/../")
            && ownedPrefixes.contains { $0.hasPrefix("/") && $0.count > 1 && listener.path.hasPrefix($0) }
    }

    /// Where Wisp's own backend executable can live: the bundled backend, or the
    /// developer checkout the app falls back to when it is not bundled.
    static func ownedBackendPrefixes(bundle: Bundle = .main, devRoot: String? = nil) -> [String] {
        var prefixes: [String] = []
        if let resources = bundle.resourceURL?.resolvingSymlinksInPath().path {
            prefixes.append(resources + "/backend/")
        }
        if let devRoot { prefixes.append(devRoot.hasSuffix("/") ? devRoot : devRoot + "/") }
        return prefixes
    }

    static func check(port: Int, ownedPrefixes: [String],
                      listeners: (Int) -> [Listener]? = PortGuard.listeners(port:)) -> Verdict {
        verdict(listeners: listeners(port), ownedPrefixes: ownedPrefixes)
    }

    /// Terminate listeners that are still provably Wisp's own backend. Ownership
    /// is re-verified immediately before each signal so a pid that was recycled
    /// since the listing is never signalled. Returns the pids signalled.
    @discardableResult
    static func terminateOwned(_ listeners: [Listener], ownedPrefixes: [String],
                               pathOf: (Int32) -> String? = PortGuard.executablePath(pid:),
                               signal: (Int32, Int32) -> Int32 = { kill($0, $1) }) -> [Int32] {
        var signalled: [Int32] = []
        for listener in listeners where listener.pid > 1 {
            guard let current = pathOf(listener.pid), current == listener.path,
                  isOwned(Listener(pid: listener.pid, path: current), ownedPrefixes: ownedPrefixes)
            else { continue }
            if signal(listener.pid, SIGTERM) == 0 { signalled.append(listener.pid) }
        }
        return signalled
    }

    static func describe(_ listeners: [Listener]) -> String {
        listeners.map { listener in
            let name = (listener.path as NSString).lastPathComponent
            return "\(name.isEmpty ? "an unknown program" : name) (pid \(listener.pid))"
        }.joined(separator: ", ")
    }

    // MARK: - Inspection

    /// Processes listening on `port`, or nil when the system could not be asked.
    static func listeners(port: Int) -> [Listener]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-nP", "-iTCP:\(port)", "-sTCP:LISTEN", "-t"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
        guard !process.isRunning else { process.terminate(); return nil }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: true)
        // lsof may repeat a pid (one per socket). Any line that is not a pid means the
        // output cannot be trusted, and the answer is "unknown", not a guess.
        let parsed = lines.map { Int32($0.trimmingCharacters(in: .whitespaces)) }
        guard parsed.allSatisfy({ $0 != nil }) else { return nil }
        let pids = Set(parsed.compactMap { $0 })
        // A listener whose path cannot be read cannot be shown to be Wisp's, so it
        // is carried with an empty path and classified as foreign.
        return pids.sorted().map { Listener(pid: $0, path: executablePath(pid: $0) ?? "") }
    }

    /// The kernel's own record of the executable a process is running.
    static func executablePath(pid: Int32) -> String? {
        var buffer = [CChar](repeating: 0, count: 4096)
        let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
        return length > 0 ? String(cString: buffer) : nil
    }
}

/// A second Wisp must hand over to the first, not fight it for the backend port.
enum SingleInstance {
    static func shouldYield(bundleIdentifier: String?, selfPID: Int32, peers: [Int32]) -> Bool {
        // A development binary run outside a bundle has no identity to compare.
        guard let bundleIdentifier, !bundleIdentifier.isEmpty else { return false }
        return peers.contains { $0 != selfPID && $0 > 0 }
    }
}
