import Darwin
import Foundation

// H-6: Wisp must never signal a process it does not own. `@main` like the other
// native checks; compiled together with PortGuard.swift.
@main
enum PortGuardChecks {
    static var checks = 0
    static func check(_ condition: Bool, _ message: String) {
        guard condition else { fatalError(message) }
        checks += 1
    }

    static func main() {
        let bundle = "/Applications/Wisp.app/Contents/Resources/backend/"
        let owned = [bundle, "/Users/dev/wisp/"]
        func listener(_ pid: Int32, _ path: String) -> PortGuard.Listener { .init(pid: pid, path: path) }

        // --- pure policy -----------------------------------------------------
        check(PortGuard.verdict(listeners: [], ownedPrefixes: owned) == .free, "empty should be free")
        check(PortGuard.verdict(listeners: nil, ownedPrefixes: owned) == .unknown, "failed inspection must be unknown")
        let ours = listener(10, bundle + ".venv/bin/python3.13")
        check(PortGuard.verdict(listeners: [ours], ownedPrefixes: owned) == .owned([ours]), "own backend not owned")
        check(PortGuard.verdict(listeners: [listener(11, "/Users/dev/wisp/.venv/bin/python3")], ownedPrefixes: owned)
              == .owned([listener(11, "/Users/dev/wisp/.venv/bin/python3")]), "dev backend not owned")

        let stranger = listener(20, "/usr/local/bin/node")
        check(PortGuard.verdict(listeners: [stranger], ownedPrefixes: owned) == .conflict([stranger]), "stranger not a conflict")
        // A mixed set is a conflict and names ONLY the foreign process.
        check(PortGuard.verdict(listeners: [ours, stranger], ownedPrefixes: owned) == .conflict([stranger]),
              "mixed set must report only the stranger")

        // Paths that merely resemble ownership prove nothing.
        for lookalike in [
            "/Applications/Wisp.app/Contents/Resources/backend-evil/python",   // prefix without the slash boundary
            "/Applications/Wisp.app/Contents/Resources/backendx",
            "/tmp/Applications/Wisp.app/Contents/Resources/backend/python",
            "Applications/Wisp.app/Contents/Resources/backend/python",         // relative
            "/Applications/Wisp.app/Contents/Resources/backend/../../../../tmp/evil",  // traversal
            "",                                                                // unreadable path
        ] {
            check(!PortGuard.isOwned(listener(30, lookalike), ownedPrefixes: owned), "look-alike accepted: \(lookalike)")
        }
        // A degenerate prefix list must never match everything.
        for prefixes in [[""], ["/"], ["relative/"], []] {
            check(!PortGuard.isOwned(stranger, ownedPrefixes: prefixes), "degenerate prefixes \(prefixes) matched")
        }

        // --- termination is narrow and re-verified -----------------------------
        var signalled: [(Int32, Int32)] = []
        let recorder: (Int32, Int32) -> Int32 = { pid, sig in signalled.append((pid, sig)); return 0 }
        let paths: [Int32: String] = [10: ours.path, 20: stranger.path]
        let done = PortGuard.terminateOwned([ours, stranger], ownedPrefixes: owned,
                                            pathOf: { paths[$0] }, signal: recorder)
        check(done == [10] && signalled.count == 1 && signalled[0] == (10, SIGTERM), "only the owned pid gets SIGTERM")

        signalled = []
        // The pid was recycled by a different program since the listing.
        _ = PortGuard.terminateOwned([ours], ownedPrefixes: owned,
                                     pathOf: { _ in "/usr/bin/ssh" }, signal: recorder)
        check(signalled.isEmpty, "a recycled pid was signalled")
        // The process is gone.
        _ = PortGuard.terminateOwned([ours], ownedPrefixes: owned, pathOf: { _ in nil }, signal: recorder)
        check(signalled.isEmpty, "a vanished process was signalled")
        // Critical pids are never touched even if a path claimed ownership.
        for pid: Int32 in [0, 1, -1] {
            _ = PortGuard.terminateOwned([listener(pid, ours.path)], ownedPrefixes: owned,
                                         pathOf: { _ in ours.path }, signal: recorder)
        }
        check(signalled.isEmpty, "pid 0/1/negative was signalled")
        // Nothing owned: nothing signalled.
        _ = PortGuard.terminateOwned([stranger], ownedPrefixes: [], pathOf: { _ in stranger.path }, signal: recorder)
        check(signalled.isEmpty, "signalled with no owned prefixes")

        // --- single instance ---------------------------------------------------
        check(PortGuard_yield(nil, 5, [9]) == false, "a binary with no bundle id must not yield")
        check(PortGuard_yield("app.wisp", 5, []) == false, "alone must not yield")
        check(PortGuard_yield("app.wisp", 5, [5]) == false, "seeing only itself must not yield")
        check(PortGuard_yield("app.wisp", 5, [5, 9]) == true, "a peer means yield")
        check(PortGuard_yield("app.wisp", 5, [0, -1]) == false, "bogus peer pids ignored")

        // --- real processes ----------------------------------------------------
        // Real children: a provably-owned one is terminated, a stranger is not. Skipped
        // (with a note) where the environment forbids spawning, so the policy checks
        // above remain the gate that always runs.
        if let mine = spawnSleeper(), let theirs = spawnSleeper() {
            defer { kill(mine, SIGKILL); kill(theirs, SIGKILL) }
            let sleeperPath = PortGuard.executablePath(pid: mine) ?? ""
            check(sleeperPath.hasPrefix("/"), "proc_pidpath returned no path for a real child")
            let sleeperDir = (sleeperPath as NSString).deletingLastPathComponent + "/"
            let real = [listener(mine, sleeperPath)]
            let killed = PortGuard.terminateOwned(real, ownedPrefixes: [sleeperDir])
            check(killed == [mine], "real owned child was not signalled")
            usleep(300_000)
            check(!isAlive(mine), "real owned child survived SIGTERM")
            check(isAlive(theirs), "an unlisted process died")
            // A listener whose path is outside the owned set survives, even though it is "listening".
            let strangerListing = [listener(theirs, PortGuard.executablePath(pid: theirs) ?? "")]
            check(PortGuard.verdict(listeners: strangerListing, ownedPrefixes: ["/nonexistent/"])
                  == .conflict(strangerListing), "real stranger not a conflict")
            let none = PortGuard.terminateOwned(strangerListing, ownedPrefixes: ["/nonexistent/"])
            check(none.isEmpty && isAlive(theirs), "a stranger was terminated")
        } else {
            print("note: spawning is unavailable in this sandbox; real-process checks skipped")
        }

        // A real listener on a real port is found and classified; nothing is signalled.
        if let (fd, port) = openListener() {
            defer { close(fd) }
            let found = PortGuard.listeners(port: port)
            check(found?.contains { $0.pid == getpid() } == true, "real listener not found on port \(port)")
            let verdict = PortGuard.check(port: port, ownedPrefixes: ["/nonexistent/"])
            if case .conflict(let foreign) = verdict { check(foreign.contains { $0.pid == getpid() }, "wrong conflict") }
            else { check(false, "real listener not classified as a conflict: \(verdict)") }
            let selfPath = PortGuard.executablePath(pid: getpid()) ?? ""
            let selfDir = (selfPath as NSString).deletingLastPathComponent + "/"
            if case .owned = PortGuard.check(port: port, ownedPrefixes: [selfDir]) { checks += 1 }
            else { check(false, "own listener not recognised as owned") }
        } else {
            print("note: loopback bind unavailable in this sandbox; real-listener check skipped")
        }
        check(PortGuard.check(port: 1, ownedPrefixes: owned) != .owned([]), "port 1 sanity")

        print("\(checks) port guard checks passed")
    }

    static func PortGuard_yield(_ id: String?, _ me: Int32, _ peers: [Int32]) -> Bool {
        SingleInstance.shouldYield(bundleIdentifier: id, selfPID: me, peers: peers)
    }

    static func spawnSleeper() -> Int32? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sleep")
        process.arguments = ["60"]
        do { try process.run() } catch { return nil }
        return process.processIdentifier
    }

    static func isAlive(_ pid: Int32) -> Bool {
        var status: Int32 = 0
        // Reap a terminated child so a zombie is not mistaken for a live process.
        if waitpid(pid, &status, WNOHANG) == pid { return false }
        return kill(pid, 0) == 0
    }

    static func openListener() -> (Int32, Int)? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = 0
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard bound == 0, listen(fd, 1) == 0 else { close(fd); return nil }
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &length) }
        }
        guard named == 0 else { close(fd); return nil }
        return (fd, Int(UInt16(bigEndian: address.sin_port)))
    }
}
