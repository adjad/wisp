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
// never a stranger. "Provably" is BackendOwnership's one policy: the exact process
// incarnation (pid + kernel start time) recorded in Wisp's launch receipt. An
// executable inside Wisp's backend directory is necessary but never sufficient: the
// backend runs on a generic Python interpreter that other programs can run too.
// Anything else, including a backend left by an older Wisp that wrote no receipt, is
// reported to the user and left running.
enum PortGuard {
    struct Listener: Equatable {
        let pid: Int32
        let path: String
        /// Kernel start time when listed. Re-checked before any signal: a recycled pid
        /// has a different one.
        var start: BackendOwnership.StartTime? = nil
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

    /// Pure policy: classifies listeners, signals nothing. A listener is owned only
    /// when BackendOwnership says it is Wisp's backend.
    static func verdict(listeners: [Listener]?, ownedPrefixes: [String],
                        receipt: BackendOwnership.ReceiptState,
                        facts: (Int32) -> BackendOwnership.ProcessFacts?) -> Verdict {
        guard let listeners else { return .unknown }
        if listeners.isEmpty { return .free }
        let foreign = listeners.filter {
            BackendOwnership.verdict(listener: $0, receipt: receipt, facts: facts($0.pid),
                                     ownedPrefixes: ownedPrefixes) != .wispBackend
        }
        return foreign.isEmpty ? .owned(listeners) : .conflict(foreign)
    }

    /// Whether the executable lies inside Wisp's backend directory. NECESSARY for
    /// ownership, never sufficient on its own (see BackendOwnership).
    static func executableInOwnedDirectory(_ listener: Listener, ownedPrefixes: [String]) -> Bool {
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

    static func check(port: Int, ownedPrefixes: [String], receipt: BackendOwnership.ReceiptState,
                      listeners: (Int) -> [Listener]? = PortGuard.listeners(port:),
                      facts: (Int32) -> BackendOwnership.ProcessFacts? = BackendOwnership.processFacts(pid:)) -> Verdict {
        verdict(listeners: listeners(port), ownedPrefixes: ownedPrefixes, receipt: receipt, facts: facts)
    }

    /// Terminate listeners that are still provably Wisp's own backend. Ownership is
    /// re-verified from fresh kernel facts immediately before each signal, including
    /// the start time seen at listing, so a pid that was recycled since the listing is
    /// never signalled. Returns the pids signalled.
    @discardableResult
    static func terminateOwned(_ listeners: [Listener], ownedPrefixes: [String],
                               receipt: BackendOwnership.ReceiptState,
                               facts: (Int32) -> BackendOwnership.ProcessFacts? = BackendOwnership.processFacts(pid:),
                               signal: (Int32, Int32) -> Int32 = { kill($0, $1) }) -> [Int32] {
        var signalled: [Int32] = []
        for listener in listeners where listener.pid > 1 {
            guard let listedStart = listener.start, let current = facts(listener.pid),
                  current.executablePath == listener.path, current.start == listedStart,
                  BackendOwnership.verdict(listener: listener, receipt: receipt, facts: current,
                                           ownedPrefixes: ownedPrefixes) == .wispBackend
            else { continue }
            if signal(listener.pid, SIGTERM) == 0 { signalled.append(listener.pid) }
        }
        return signalled
    }

    /// What the person is told when the port is held by something Wisp cannot prove is
    /// its own. A program running from Wisp's own folder is most likely a backend left by
    /// an earlier Wisp (one that wrote no launch receipt), so the advice says so.
    static func conflictMessage(port: Int, listeners: [Listener], ownedPrefixes: [String]) -> String {
        let named = "Another program is already using port \(port): \(describe(listeners))."
        if listeners.contains(where: { executableInOwnedDirectory($0, ownedPrefixes: ownedPrefixes) }) {
            return named + " It runs from Wisp's folder, but this Wisp didn't start it, so Wisp can't confirm "
                + "it is its own service and did not stop it. If an earlier copy of Wisp left it running, quit it "
                + "in Activity Monitor (or restart your Mac), then reopen Wisp."
        }
        return named + " Wisp did not stop it, because it isn't Wisp's. Quit that program, then reopen Wisp."
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
        return pids.sorted().map {
            Listener(pid: $0, path: executablePath(pid: $0) ?? "", start: BackendOwnership.startTime(pid: $0))
        }
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
