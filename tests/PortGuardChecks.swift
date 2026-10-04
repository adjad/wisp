import Darwin
import Foundation

// H-6: Wisp must never signal a process it does not own. `@main` like the other
// native checks; compiled together with PortGuard.swift and BackendOwnership.swift.
// Fixture files live only under FileManager.default.temporaryDirectory (the release
// pipeline links FixtureTemporaryDirectory.swift, which points it at the gate's TMPDIR).
@main
enum PortGuardChecks {
    static var checks = 0
    static func check(_ condition: Bool, _ message: String) {
        guard condition else { fatalError(message) }
        checks += 1
    }

    typealias Facts = BackendOwnership.ProcessFacts
    typealias Start = BackendOwnership.StartTime
    static let bundle = "/Applications/Wisp.app/Contents/Resources/backend/"
    static let devRoot = "/opt/dev/wisp/"
    static let hints = [bundle, devRoot]
    static let python = bundle + ".venv/bin/python3.13"
    static let backendCwd = String(bundle.dropLast())
    static let birth = Start(seconds: 100, microseconds: 7)

    static func listener(_ pid: Int32, _ path: String = python, _ start: Start? = birth)
        -> PortGuard.Listener { .init(pid: pid, path: path, start: start) }
    static func facts(_ pid: Int32, path: String = python, start: Start? = birth) -> Facts {
        Facts(pid: pid, start: start, executablePath: path)
    }
    static func receipt(_ pid: Int32, start: Start = birth, path: String = python,
                        nonce: String = "nonce-1") -> BackendOwnership.ReceiptState {
        .present(.init(pid: pid, start: start, executablePath: path, backendRoot: backendCwd, nonce: nonce))
    }

    static func main() {
        // Before anything in this process records a launch: the shared store is empty,
        // exactly as it is when the app starts.
        check(BackendLaunchReceiptStore.shared.current == .none, "the shared receipt store must start empty")
        policyChecks()
        verdictChecks()
        terminationChecks()
        wordingChecks()
        forgedReceiptChecks()
        inMemoryReceiptChecks()
        portDecisionChecks()
        kernelReaderChecks()
        singleInstanceChecks()
        realProcessChecks()
        print("\(checks) port guard checks passed")
    }

    // MARK: the one ownership policy

    static func policyChecks() {
        func verdict(_ l: PortGuard.Listener, _ r: BackendOwnership.ReceiptState, _ f: Facts?,
                     _ identity: BackendOwnership.Identity = .notAsked) -> BackendOwnership.Verdict {
            BackendOwnership.verdict(listener: l, receipt: r, facts: f, identity: identity)
        }
        let ours = listener(10)

        // The backend this app spawned, named by its in-memory receipt, IS Wisp.
        check(verdict(ours, receipt(10), facts(10)) == .wispBackend, "matching receipt rejected")
        check(verdict(listener(11, devRoot + ".venv/bin/python3"), receipt(11, path: devRoot + ".venv/bin/python3"),
                      facts(11, path: devRoot + ".venv/bin/python3")) == .wispBackend, "matching dev receipt rejected")

        // (iv) Dev build: the interpreter resolves OUTSIDE every Wisp folder (a framework or
        // Homebrew Python). The receipt incarnation is the proof; the folder is not a signal.
        let framework = "/Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"
        check(verdict(listener(13, framework), receipt(13, path: framework), facts(13, path: framework)) == .wispBackend,
              "a receipt-matching backend outside Wisp's folders was rejected")
        // ...while a bystander on that same interpreter, without the receipt, is not.
        check(verdict(listener(14, framework), .none, facts(14, path: framework)) == .notWisp(.noReceipt),
              "a same-interpreter bystander without a receipt was trusted")
        check(verdict(listener(14, framework), receipt(13, path: framework), facts(14, path: framework))
              == .notWisp(.receiptMismatch), "a same-interpreter bystander was trusted on another process's receipt")
        // Living inside Wisp's own folder proves nothing either.
        check(verdict(ours, .none, facts(10)) == .notWisp(.noReceipt), "a process in Wisp's folder was trusted unrecorded")

        // No receipt at all: NOTHING is proven Wisp's, however exactly it resembles the backend.
        check(verdict(ours, .none, facts(10), .missing) == .notWisp(.noReceipt), "no receipt + 404 must not prove ownership")
        check(verdict(ours, .none, facts(10), .answered(nonce: "nonce-1")) == .notWisp(.noReceipt),
              "no receipt + any nonce must not prove ownership")

        // Another program on Wisp's own interpreter (same executable path) is NOT Wisp.
        check(verdict(listener(20), receipt(10), facts(20)) == .notWisp(.receiptMismatch),
              "another process on the owned interpreter accepted")
        check(verdict(listener(20), receipt(10), facts(20), .answered(nonce: "nonce-1")) == .notWisp(.receiptMismatch),
              "another process echoing the right nonce accepted")

        // (iii) Same pid, different kernel start time: a reused pid is not the launched process.
        check(verdict(ours, receipt(10, start: Start(seconds: 100, microseconds: 8)), facts(10))
              == .notWisp(.receiptMismatch), "pid reuse (microseconds) accepted")
        check(verdict(ours, receipt(10, start: Start(seconds: 99, microseconds: 7)), facts(10))
              == .notWisp(.receiptMismatch), "pid reuse (seconds) accepted")
        check(verdict(ours, receipt(10), facts(10, start: nil)) == .notWisp(.receiptMismatch), "unknown start accepted")
        check(verdict(ours, receipt(10, path: bundle + ".venv/bin/python3.12"), facts(10)) == .notWisp(.receiptMismatch),
              "a different executable with the receipt's pid accepted")

        // A stale receipt (its process is gone) + a bystander on the port: NOT Wisp.
        check(verdict(ours, receipt(777), facts(10)) == .notWisp(.receiptMismatch), "stale receipt accepted a bystander")

        // Identity: when the backend answered, it must carry the receipt's launch nonce.
        check(verdict(ours, receipt(10), facts(10), .answered(nonce: "nonce-1")) == .wispBackend, "matching nonce rejected")
        check(verdict(ours, receipt(10), facts(10), .answered(nonce: "nonce-2")) == .notWisp(.nonceMismatch), "wrong nonce accepted")
        check(verdict(ours, receipt(10), facts(10), .answered(nonce: nil)) == .notWisp(.nonceMismatch), "missing nonce accepted")
        check(verdict(ours, receipt(10), facts(10), .answered(nonce: "")) == .notWisp(.nonceMismatch), "empty nonce accepted")
        check(verdict(ours, receipt(10, nonce: ""), facts(10), .answered(nonce: "")) == .notWisp(.nonceMismatch),
              "an empty receipt nonce must never match")
        // A 404 is trusted only from the exact launched incarnation.
        check(verdict(ours, receipt(10), facts(10), .missing) == .wispBackend, "404 from the launched incarnation rejected")
        check(verdict(ours, receipt(10, start: Start(seconds: 1, microseconds: 0)), facts(10), .missing)
              == .notWisp(.receiptMismatch), "404 from a reused pid accepted")

        // (iii) Facts must describe the listed process; missing facts mean the process is gone.
        check(verdict(ours, receipt(10), nil) == .notWisp(.processGone), "a vanished process accepted")
        check(verdict(ours, receipt(10), facts(99)) == .notWisp(.processGone), "facts for another pid accepted")
        check(verdict(ours, receipt(10), facts(10, path: bundle + "other")) == .notWisp(.processGone),
              "facts whose executable differs from the listing accepted")
        check(verdict(ours, receipt(10), Facts(pid: 10, start: birth, executablePath: nil)) == .notWisp(.processGone),
              "facts without an executable accepted")
        for pid: Int32 in [0, 1, -1] {
            check(verdict(listener(pid), receipt(pid), facts(pid)) == .notWisp(.protectedProcess), "pid \(pid) accepted")
        }
    }

    // MARK: PortGuard's listing verdict uses that policy

    static func verdictChecks() {
        let ours = listener(10)
        let bystander = listener(20)            // the SAME interpreter, a different process
        let stranger = listener(30, "/usr/local/bin/node")
        let table: [Int32: Facts] = [10: facts(10), 20: facts(20), 30: facts(30, path: stranger.path)]
        func verdict(_ ls: [PortGuard.Listener]?, _ r: BackendOwnership.ReceiptState = receipt(10)) -> PortGuard.Verdict {
            PortGuard.verdict(listeners: ls, receipt: r, facts: { table[$0] })
        }
        check(verdict([]) == .free, "empty should be free")
        check(verdict(nil) == .unknown, "failed inspection must be unknown")
        check(verdict([ours]) == .owned([ours]), "receipt-proven backend not owned")
        check(verdict([ours], .none) == .conflict([ours]), "a receipt-less backend must be a conflict (fail closed)")
        check(verdict([bystander]) == .conflict([bystander]), "a bystander on the owned interpreter was owned")
        check(verdict([ours, bystander]) == .conflict([bystander]), "mixed set must report only the bystander")
        check(verdict([stranger]) == .conflict([stranger]), "stranger not a conflict")
        check(verdict([ours, stranger]) == .conflict([stranger]), "mixed set must report only the stranger")
        check(verdict([ours], receipt(777)) == .conflict([ours]), "stale receipt must make the listener a conflict")
        check(PortGuard.verdict(listeners: [ours], receipt: receipt(10), facts: { _ in nil })
              == .conflict([ours]), "an uninspectable listener must be a conflict")
        // At startup the app judges the port with NO receipt: every holder is a conflict.
        check(verdict([ours, bystander, stranger], .none) == .conflict([ours, bystander, stranger]),
              "a startup listing with no receipt must make every holder a conflict")

        // The folder hint used for wording only: look-alikes never match it, and a
        // look-alike without a receipt is a conflict like anything else.
        for lookalike in [
            "/Applications/Wisp.app/Contents/Resources/backend-evil/python",
            "/Applications/Wisp.app/Contents/Resources/backendx",
            "/tmp/Applications/Wisp.app/Contents/Resources/backend/python",
            "Applications/Wisp.app/Contents/Resources/backend/python",
            "/Applications/Wisp.app/Contents/Resources/backend/../../../../tmp/evil",
            "",
        ] {
            check(!PortGuard.runsFromWispFolder(listener(31, lookalike), folders: hints),
                  "look-alike accepted: \(lookalike)")
            let l = listener(31, lookalike)
            check(PortGuard.verdict(listeners: [l], receipt: .none, facts: { _ in facts(31, path: lookalike) })
                  == .conflict([l]), "look-alike owned: \(lookalike)")
        }
        check(PortGuard.runsFromWispFolder(ours, folders: hints), "the bundled interpreter not recognised for wording")
        for folders in [[""], ["/"], ["relative/"], []] {
            check(!PortGuard.runsFromWispFolder(stranger, folders: folders), "degenerate folders \(folders) matched")
        }
    }

    // MARK: termination is narrow and re-verified immediately before the signal

    static func terminationChecks() {
        var signalled: [(Int32, Int32)] = []
        let recorder: (Int32, Int32) -> Int32 = { pid, sig in signalled.append((pid, sig)); return 0 }
        let ours = listener(10)
        let bystander = listener(20)
        let stranger = listener(30, "/usr/local/bin/node")
        let table: [Int32: Facts] = [10: facts(10), 20: facts(20), 30: facts(30, path: stranger.path)]
        func terminate(_ ls: [PortGuard.Listener], _ r: BackendOwnership.ReceiptState,
                       _ f: @escaping (Int32) -> Facts? = { table[$0] }) -> [Int32] {
            PortGuard.terminateOwned(ls, receipt: r, facts: f, signal: recorder)
        }

        // The receipt-proven backend is signalled; nobody else is.
        check(terminate([ours, bystander, stranger], receipt(10)) == [10] && signalled.count == 1
              && signalled[0] == (10, SIGTERM), "only the receipt-proven backend gets SIGTERM")

        // Another process on the owned interpreter is never signalled, whatever the receipt.
        signalled = []
        for state in [BackendOwnership.ReceiptState.none, receipt(10), receipt(777)] {
            _ = terminate([bystander], state)
        }
        check(signalled.isEmpty, "a bystander on the owned interpreter was signalled")
        // No receipt (nothing spawned by this app): never signalled.
        _ = terminate([ours, bystander, stranger], .none)
        check(signalled.isEmpty, "a receipt-less listener was signalled")
        // Stale receipt.
        _ = terminate([ours], receipt(777))
        check(signalled.isEmpty, "stale receipt: a listener was signalled")
        // Receipt pid matches but the kernel start time differs.
        _ = terminate([ours], receipt(10, start: Start(seconds: 1, microseconds: 0)))
        check(signalled.isEmpty, "a reused pid (receipt start time) was signalled")

        // Re-verification just before the signal: the pid was recycled since the listing.
        _ = terminate([ours], receipt(10)) { _ in facts(10, start: Start(seconds: 200, microseconds: 0)) }
        check(signalled.isEmpty, "a pid recycled since the listing (new start time) was signalled")
        _ = terminate([ours], receipt(10)) { _ in facts(10, path: "/usr/bin/ssh") }
        check(signalled.isEmpty, "a pid recycled by a different program was signalled")
        _ = terminate([ours], receipt(10)) { _ in nil }
        check(signalled.isEmpty, "a vanished process was signalled")
        // The listing and the receipt agree with each other but not with the live process.
        _ = terminate([listener(10, python, Start(seconds: 5, microseconds: 5))], receipt(10, start: Start(seconds: 5, microseconds: 5)))
        check(signalled.isEmpty, "a listing whose start time is no longer the live one was signalled")
        // A listing without a start time cannot be re-verified, so it is never signalled.
        _ = terminate([listener(10, python, nil)], receipt(10))
        check(signalled.isEmpty, "a listing with no start time was signalled")
        for pid: Int32 in [0, 1, -1] {
            _ = terminate([listener(pid)], receipt(pid)) { facts($0) }
        }
        check(signalled.isEmpty, "pid 0/1/negative was signalled")
    }

    static func wordingChecks() {
        // Diagnostic only: a holder running from Wisp's folder may be an earlier Wisp's leftover.
        let older = PortGuard.conflictMessage(port: 8765, listeners: [listener(4242)], folderHints: hints)
        check(older.contains("python3.13 (pid 4242)") && older.contains("8765") && older.contains("earlier copy of Wisp")
              && older.contains("did not stop it") && older.contains("Activity Monitor"), "leftover backend wording: \(older)")
        let stranger = PortGuard.conflictMessage(port: 8765, listeners: [listener(7, "/usr/local/bin/node")],
                                                 folderHints: hints)
        check(stranger.contains("node (pid 7)") && stranger.contains("didn't start it") && !stranger.contains("earlier copy"),
              "stranger wording: \(stranger)")
        // A responder the listing could not name still gets an actionable message.
        let unnamed = PortGuard.conflictMessage(port: 8765, listeners: [], folderHints: hints)
        check(unnamed.contains("port 8765") && unnamed.contains("did not stop it") && !unnamed.contains("()")
              && !unnamed.contains(": ."), "unnamed responder wording: \(unnamed)")
    }

    // MARK: (i) NATIVE-A1: a forged receipt file proves nothing

    /// A same-user program writes a syntactically valid 0600 receipt (the exact format the
    /// earlier persisted store accepted) naming a live process it controls. To make the
    /// forgery as believable as possible it names THIS test process's real pid, kernel
    /// start time and executable. A restarted Wisp must not trust it or signal anything.
    static func forgedReceiptChecks() {
        let fm = FileManager.default
        let me = getpid()
        guard let real = BackendOwnership.processFacts(pid: me), let start = real.start,
              let path = real.executablePath else { return check(false, "no kernel facts for the test process") }
        let dir = fm.temporaryDirectory.appendingPathComponent("wisp-forgery-\(UUID().uuidString)", isDirectory: true)
        defer { try? fm.removeItem(at: dir) }
        do {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        } catch { return check(false, "cannot create the forgery directory: \(error)") }
        let forged = try? JSONSerialization.data(withJSONObject: [
            "schema_version": 1, "pid": Int(me), "start_seconds": start.seconds,
            "start_microseconds": start.microseconds, "executable_path": path,
            "backend_root": (path as NSString).deletingLastPathComponent, "nonce": "forged-nonce",
        ], options: [.sortedKeys])
        check(forged != nil, "forged receipt not encodable")
        let file = dir.appendingPathComponent("backend-launch-receipt.json").path
        let fd = open(file, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        check(fd >= 0, "cannot plant the forged receipt (errno \(errno))")
        let data = forged ?? Data()
        let written = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
        var info = stat()
        check(written == data.count && lstat(file, &info) == 0 && info.st_mode & S_IFMT == S_IFREG
              && info.st_mode & 0o7777 == 0o600 && info.st_uid == getuid(), "the forged receipt is not a valid 0600 file")

        // A freshly created registry (what a restarted app has) is EMPTY: nothing reads disk.
        let restarted = BackendLaunchReceiptStore()
        check(restarted.current == .none, "a new receipt store is not empty")
        check(BackendLaunchReceiptStore.shared.current == .none, "the shared store picked up a receipt")

        let squatter = PortGuard.Listener(pid: me, path: path, start: start)
        check(BackendOwnership.verdict(listener: squatter, receipt: restarted.current, facts: real)
              == .notWisp(.noReceipt), "a forged receipt made its process Wisp's")
        check(BackendOwnership.verdict(listener: squatter, receipt: restarted.current, facts: real,
                                       identity: .answered(nonce: "forged-nonce")) == .notWisp(.noReceipt),
              "a forged receipt plus its own nonce made its process Wisp's")
        // PortGuard.check at startup (no receipt) reports a conflict, never owned. The
        // listing is injected: no real port is touched.
        check(PortGuard.check(port: 1, receipt: restarted.current, listeners: { _ in [squatter] })
              == .conflict([squatter]), "the forged receipt's process was not a conflict")
        check(PortGuard.check(port: 1, receipt: .none, listeners: { _ in [squatter] })
              == .conflict([squatter]), "a startup check was not a conflict")
        var signals = 0
        let signalled = PortGuard.terminateOwned([squatter], receipt: restarted.current,
                                                 signal: { _, _ in signals += 1; return 0 })
        check(signalled.isEmpty && signals == 0, "the forged receipt's process was signalled")
        // The startup decision treats its healthy reply as a stranger.
        check(BackendOwnership.portDecision(healthy: true, freshRecovery: false, liveChildPID: nil,
                                            receipt: restarted.current, childFacts: nil) == .stranger,
              "a healthy squatter was taken for Wisp's backend")
        check(fm.fileExists(atPath: file), "the forged file vanished (the check above proved nothing)")
    }

    // MARK: (ii) the backend this app spawned, recorded in memory

    static func inMemoryReceiptChecks() {
        let me = getpid()
        guard let real = BackendOwnership.processFacts(pid: me), let start = real.start,
              let path = real.executablePath else { return check(false, "no kernel facts for the test process") }
        let store = BackendLaunchReceiptStore()
        let spawned = BackendOwnership.Receipt(pid: me, start: start, executablePath: path,
                                               backendRoot: (path as NSString).deletingLastPathComponent, nonce: "n")
        store.record(spawned)
        check(store.current == .present(spawned), "record did not keep the receipt in memory")
        check(BackendLaunchReceiptStore.shared.current == .none, "recording in one store leaked into the shared one")
        let mine = PortGuard.Listener(pid: me, path: path, start: start)
        check(BackendOwnership.verdict(listener: mine, receipt: store.current, facts: real) == .wispBackend,
              "the recorded incarnation was not Wisp's")
        check(PortGuard.check(port: 1, receipt: store.current, listeners: { _ in [mine] }) == .owned([mine]),
              "the recorded incarnation was not owned")
        var signals: [(Int32, Int32)] = []
        check(PortGuard.terminateOwned([mine], receipt: store.current,
                                       signal: { signals.append(($0, $1)); return 0 }) == [me]
              && signals.count == 1 && signals[0] == (me, SIGTERM), "the recorded incarnation was not the one signalled")
        // A different start time (an earlier incarnation of this pid) is not it.
        let earlier = Start(seconds: start.seconds, microseconds: (start.microseconds + 1) % 1_000_000)
        signals = []
        check(PortGuard.terminateOwned([PortGuard.Listener(pid: me, path: path, start: earlier)], receipt: store.current,
                                       signal: { signals.append(($0, $1)); return 0 }).isEmpty && signals.isEmpty,
              "a listing from an earlier incarnation was signalled")

        // Framework Python re-exec: same pid and start time, new executable path. Only the
        // SAME incarnation may have its path refreshed.
        let reexec = BackendLaunchReceiptStore()
        let launched = BackendOwnership.Receipt(pid: me, start: start, executablePath: "/opt/python/bin/python3",
                                                backendRoot: "/opt/wisp", nonce: "n2")
        reexec.record(launched)
        check(BackendOwnership.verdict(listener: mine, receipt: reexec.current, facts: real) == .notWisp(.receiptMismatch),
              "the pre-re-exec path matched the post-re-exec process")
        check(!reexec.refreshExecutablePath(pid: me, start: earlier, executablePath: path)
              && reexec.current == .present(launched), "a different incarnation refreshed the receipt")
        check(!reexec.refreshExecutablePath(pid: me + 1, start: start, executablePath: path)
              && reexec.current == .present(launched), "a different pid refreshed the receipt")
        check(!reexec.refreshExecutablePath(pid: me, start: start, executablePath: "relative/python")
              && reexec.current == .present(launched), "a relative path refreshed the receipt")
        check(!BackendLaunchReceiptStore().refreshExecutablePath(pid: me, start: start, executablePath: path),
              "an empty store invented a receipt on refresh")
        check(reexec.refreshExecutablePath(pid: me, start: start, executablePath: path), "same-incarnation refresh refused")
        check(reexec.current == .present(.init(pid: me, start: start, executablePath: path, backendRoot: "/opt/wisp",
                                               nonce: "n2")), "refresh changed more than the executable path")
        check(BackendOwnership.verdict(listener: mine, receipt: reexec.current, facts: real) == .wispBackend,
              "the refreshed receipt does not match the re-executed process")
        check(!reexec.refreshExecutablePath(pid: me, start: start, executablePath: path), "a no-op refresh reported a change")
    }

    // MARK: (v) startIfNeeded: a healthy reply is ours only from our live, recorded child

    static func portDecisionChecks() {
        let me = getpid()
        guard let real = BackendOwnership.processFacts(pid: me), let start = real.start,
              let path = real.executablePath else { return check(false, "no kernel facts for the test process") }
        let ours = BackendOwnership.ReceiptState.present(.init(pid: me, start: start, executablePath: path,
                                                                backendRoot: "/opt/wisp", nonce: "n"))
        func decide(healthy: Bool, fresh: Bool = false, child: Int32?, _ r: BackendOwnership.ReceiptState = ours,
                    _ f: Facts? = real) -> BackendOwnership.PortDecision {
            BackendOwnership.portDecision(healthy: healthy, freshRecovery: fresh, liveChildPID: child, receipt: r, childFacts: f)
        }
        // Nothing answers: go on to launch.
        check(decide(healthy: false, child: nil, .none, nil) == .proceed, "a silent port did not proceed to launch")
        check(decide(healthy: false, fresh: true, child: nil, .none, nil) == .proceed, "a silent port did not proceed (recovery)")
        // Something answers and this manager has no live child: a stranger, never "already running".
        check(decide(healthy: true, child: nil, .none, nil) == .stranger, "a healthy stranger was taken for Wisp's backend")
        check(decide(healthy: true, fresh: true, child: nil, .none, nil) == .stranger, "a healthy stranger during recovery")
        check(decide(healthy: true, child: nil) == .stranger, "a receipt with no live child made a responder Wisp's")
        // Our live child, exactly the recorded incarnation: it is our backend.
        check(decide(healthy: true, child: me) == .ownBackendRunning, "our own healthy child was not recognised")
        // A fresh recovery keeps its old behaviour for our own child (go on; the launch path
        // itself refuses while a child exists).
        check(decide(healthy: true, fresh: true, child: me) == .proceed, "fresh recovery of our own child changed")
        // A live child that is not the recorded incarnation, or cannot be inspected: stranger.
        check(decide(healthy: true, child: me, .none) == .stranger, "a live child with no receipt was trusted")
        check(decide(healthy: true, child: me, ours, nil) == .stranger, "a child without facts was trusted")
        check(decide(healthy: true, child: me, ours, Facts(pid: me, start: Start(seconds: start.seconds + 1,
                     microseconds: start.microseconds), executablePath: path)) == .stranger,
              "a child with another start time was trusted")
        check(decide(healthy: true, child: me, ours, Facts(pid: me, start: start, executablePath: "/usr/bin/nc"))
              == .stranger, "a child running another executable was trusted")
        check(decide(healthy: true, child: me, ours, Facts(pid: me + 1, start: start, executablePath: path)) == .stranger,
              "facts for another pid were trusted")
        check(decide(healthy: true, child: me + 1, ours, Facts(pid: me + 1, start: start, executablePath: path))
              == .stranger, "a child other than the recorded pid was trusted")
        check(decide(healthy: true, child: 1, .present(.init(pid: 1, start: start, executablePath: path,
                     backendRoot: "/", nonce: "n")), Facts(pid: 1, start: start, executablePath: path)) == .stranger,
              "pid 1 was trusted")
    }

    // MARK: the kernel readers, on this very process (no spawning)

    static func kernelReaderChecks() {
        let me = getpid()
        let facts = BackendOwnership.processFacts(pid: me)
        check(facts != nil, "facts for the running test process unavailable")
        guard let facts else { return }
        check(facts.pid == me, "facts describe another pid")
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().path
        check(facts.executablePath == PortGuard.executablePath(pid: me) && facts.executablePath.map {
                URL(fileURLWithPath: $0).resolvingSymlinksInPath().path } == exe,
              "executable \(String(describing: facts.executablePath)) != \(exe)")
        // The start time agrees with an independent kernel interface and is in the past.
        var kp = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, me]
        check(sysctl(&mib, 4, &kp, &size, nil, 0) == 0, "kinfo_proc unavailable")
        let independent = kp.kp_proc.p_un.__p_starttime
        check(facts.start == Start(seconds: UInt64(independent.tv_sec), microseconds: UInt64(independent.tv_usec)),
              "start time \(String(describing: facts.start)) disagrees with kinfo_proc")
        check(UInt64(Date().timeIntervalSince1970) >= facts.start!.seconds, "start time in the future")
        check(BackendOwnership.startTime(pid: me) == facts.start, "startTime(pid:) disagrees with facts")
        check(BackendOwnership.processFacts(pid: 0x7fff_fff0) == nil && BackendOwnership.startTime(pid: 0x7fff_fff0) == nil,
              "readers invented data for a nonexistent pid")

        // This process: not Wisp's without a receipt; Wisp's with a receipt naming its exact
        // incarnation; not with a receipt carrying a different start time.
        let mine = PortGuard.Listener(pid: me, path: facts.executablePath ?? "", start: facts.start)
        let selfDir = ((facts.executablePath ?? "") as NSString).deletingLastPathComponent + "/"
        check(BackendOwnership.verdict(listener: mine, receipt: .none, facts: facts)
              == .notWisp(.noReceipt), "the test process was proven Wisp's without a receipt")
        let selfReceipt = BackendOwnership.Receipt(pid: me, start: facts.start!, executablePath: facts.executablePath!,
                                                   backendRoot: selfDir, nonce: "self")
        check(BackendOwnership.verdict(listener: mine, receipt: .present(selfReceipt), facts: facts)
              == .wispBackend, "a receipt naming this process exactly was rejected")
        let reused = BackendOwnership.Receipt(pid: me, start: Start(seconds: facts.start!.seconds, microseconds:
                                                (facts.start!.microseconds + 1) % 1_000_000),
                                              executablePath: facts.executablePath!, backendRoot: selfDir, nonce: "self")
        check(BackendOwnership.verdict(listener: mine, receipt: .present(reused), facts: facts)
              == .notWisp(.receiptMismatch), "a receipt for an earlier incarnation of this pid was accepted")
    }

    // MARK: single instance

    static func singleInstanceChecks() {
        func yield(_ id: String?, _ me: Int32, _ peers: [Int32]) -> Bool {
            SingleInstance.shouldYield(bundleIdentifier: id, selfPID: me, peers: peers)
        }
        check(yield(nil, 5, [9]) == false, "a binary with no bundle id must not yield")
        check(yield("app.wisp", 5, []) == false, "alone must not yield")
        check(yield("app.wisp", 5, [5]) == false, "seeing only itself must not yield")
        check(yield("app.wisp", 5, [5, 9]) == true, "a peer means yield")
        check(yield("app.wisp", 5, [0, -1]) == false, "bogus peer pids ignored")
    }

    // MARK: real processes and a real listener (skipped where the sandbox forbids them)

    static func realProcessChecks() {
        if let mine = spawnSleeper(), let theirs = spawnSleeper() {
            defer { kill(mine, SIGKILL); kill(theirs, SIGKILL) }
            let sleeperPath = PortGuard.executablePath(pid: mine) ?? ""
            check(sleeperPath.hasPrefix("/"), "proc_pidpath returned no path for a real child")
            let start = BackendOwnership.startTime(pid: mine)
            let real = [PortGuard.Listener(pid: mine, path: sleeperPath, start: start)]
            // No receipt: never signalled.
            check(PortGuard.terminateOwned(real, receipt: .none).isEmpty && isAlive(mine),
                  "a real receipt-less child was signalled")
            // A receipt naming this exact child: it is signalled.
            let receipt = BackendOwnership.ReceiptState.present(.init(pid: mine, start: start!, executablePath: sleeperPath,
                                                                     backendRoot: "/", nonce: "n"))
            check(PortGuard.terminateOwned(real, receipt: receipt) == [mine], "real receipt-proven child was not signalled")
            usleep(300_000)
            check(!isAlive(mine), "real owned child survived SIGTERM")
            check(isAlive(theirs), "an unlisted process died")
            // The other child, same executable: never signalled.
            let other = [PortGuard.Listener(pid: theirs, path: PortGuard.executablePath(pid: theirs) ?? "",
                                            start: BackendOwnership.startTime(pid: theirs))]
            check(PortGuard.terminateOwned(other, receipt: receipt).isEmpty && isAlive(theirs),
                  "a stale receipt let a bystander be signalled")
        } else {
            print("note: spawning is unavailable in this sandbox; real-process checks skipped")
        }

        if let (fd, port) = openListener() {
            defer { close(fd) }
            let found = PortGuard.listeners(port: port)
            if let found {
                check(found.contains { $0.pid == getpid() && $0.start != nil },
                      "real listener (with start time) not found on port \(port)")
                let selfPath = PortGuard.executablePath(pid: getpid()) ?? ""
                if case .conflict(let foreign) = PortGuard.check(port: port, receipt: .none) {
                    check(foreign.contains { $0.pid == getpid() }, "wrong conflict")
                } else { check(false, "a real listener without a receipt was not a conflict") }
                let me = BackendOwnership.Receipt(pid: getpid(), start: BackendOwnership.startTime(pid: getpid())!,
                                                  executablePath: selfPath, backendRoot: "/", nonce: "n")
                if case .owned = PortGuard.check(port: port, receipt: .present(me)) { checks += 1 }
                else { check(false, "own receipt-proven listener not recognised as owned") }
            } else {
                print("note: listing listeners is unavailable in this sandbox; real-listener check skipped")
            }
        } else {
            print("note: loopback bind unavailable in this sandbox; real-listener check skipped")
        }
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
