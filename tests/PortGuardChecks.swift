import Darwin
import Foundation

// H-6: Wisp must never signal a process it does not own. `@main` like the other
// native checks; compiled together with PortGuard.swift and BackendOwnership.swift.
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
    static let devRoot = "/Users/dev/wisp/"
    static let owned = [bundle, devRoot]
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
        policyChecks()
        verdictChecks()
        terminationChecks()
        wordingChecks()
        receiptStoreChecks()
        kernelReaderChecks()
        singleInstanceChecks()
        realProcessChecks()
        print("\(checks) port guard checks passed")
    }

    // MARK: the one ownership policy

    static func policyChecks() {
        func verdict(_ l: PortGuard.Listener, _ r: BackendOwnership.ReceiptState, _ f: Facts?,
                     _ identity: BackendOwnership.Identity = .notAsked) -> BackendOwnership.Verdict {
            BackendOwnership.verdict(listener: l, receipt: r, facts: f, identity: identity, ownedPrefixes: owned)
        }
        let ours = listener(10)

        // (ii) The real backend with a matching receipt IS Wisp.
        check(verdict(ours, receipt(10), facts(10)) == .wispBackend, "matching receipt rejected")
        check(verdict(listener(11, devRoot + ".venv/bin/python3"), receipt(11, path: devRoot + ".venv/bin/python3"),
                      facts(11, path: devRoot + ".venv/bin/python3")) == .wispBackend, "matching dev receipt rejected")

        // No receipt at all: NOTHING is proven Wisp's, however exactly it resembles the
        // backend (the upgrade case with an older, receipt-less backend fails closed).
        check(verdict(ours, .none, facts(10)) == .notWisp(.noReceipt), "no receipt must never prove ownership")
        check(verdict(ours, .none, facts(10), .missing) == .notWisp(.noReceipt), "no receipt + 404 must not prove ownership")
        check(verdict(ours, .none, facts(10), .answered(nonce: "nonce-1")) == .notWisp(.noReceipt),
              "no receipt + any nonce must not prove ownership")

        // (i) Another program on Wisp's own interpreter (same executable path) is NOT Wisp.
        check(verdict(listener(20), receipt(10), facts(20)) == .notWisp(.receiptMismatch),
              "another process on the owned interpreter accepted")
        check(verdict(listener(20), receipt(10), facts(20), .answered(nonce: "nonce-1")) == .notWisp(.receiptMismatch),
              "another process echoing the right nonce accepted")
        // The executable must still be Wisp's own, whatever the receipt says.
        let node = listener(12, "/usr/local/bin/node")
        check(verdict(node, receipt(12, path: node.path), facts(12, path: node.path)) == .notWisp(.foreignExecutable),
              "a receipt never makes an outside executable Wisp's")

        // (iii) Same pid, different kernel start time: a reused pid is not the launched process.
        check(verdict(ours, receipt(10, start: Start(seconds: 100, microseconds: 8)), facts(10))
              == .notWisp(.receiptMismatch), "pid reuse (microseconds) accepted")
        check(verdict(ours, receipt(10, start: Start(seconds: 99, microseconds: 7)), facts(10))
              == .notWisp(.receiptMismatch), "pid reuse (seconds) accepted")
        check(verdict(ours, receipt(10), facts(10, start: nil)) == .notWisp(.receiptMismatch), "unknown start accepted")
        check(verdict(ours, receipt(10, path: bundle + ".venv/bin/python3.12"), facts(10)) == .notWisp(.receiptMismatch),
              "a different executable with the receipt's pid accepted")

        // (iv) A stale receipt (its process is gone) + a bystander on the port: NOT Wisp.
        check(verdict(ours, receipt(777), facts(10)) == .notWisp(.receiptMismatch), "stale receipt accepted a bystander")
        // (viii) A receipt that exists but cannot be used: NOT Wisp.
        check(verdict(ours, .unusable, facts(10)) == .notWisp(.receiptUnusable), "unusable receipt accepted")

        // (vi) Identity: when the backend answered, it must carry the receipt's launch nonce.
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

        // Facts must describe the listed process; anything else is not inspectable.
        check(verdict(ours, receipt(10), nil) == .notWisp(.processGone), "a vanished process accepted")
        check(verdict(ours, receipt(10), facts(99)) == .notWisp(.processGone), "facts for another pid accepted")
        check(verdict(ours, receipt(10), facts(10, path: bundle + "other")) == .notWisp(.processGone),
              "facts whose executable differs from the listing accepted")
        for pid: Int32 in [0, 1, -1] {
            check(verdict(listener(pid), receipt(pid), facts(pid)) == .notWisp(.protectedProcess), "pid \(pid) accepted")
        }
        for prefixes in [[""], ["/"], ["relative/"], []] {
            check(BackendOwnership.verdict(listener: ours, receipt: receipt(10), facts: facts(10),
                                           identity: .notAsked, ownedPrefixes: prefixes) == .notWisp(.foreignExecutable),
                  "degenerate prefixes \(prefixes) matched")
        }
    }

    // MARK: PortGuard's listing verdict uses that policy

    static func verdictChecks() {
        let ours = listener(10)
        let bystander = listener(20)            // the SAME interpreter, a different process
        let stranger = listener(30, "/usr/local/bin/node")
        let table: [Int32: Facts] = [10: facts(10), 20: facts(20), 30: facts(30, path: stranger.path)]
        func verdict(_ ls: [PortGuard.Listener]?, _ r: BackendOwnership.ReceiptState = receipt(10)) -> PortGuard.Verdict {
            PortGuard.verdict(listeners: ls, ownedPrefixes: owned, receipt: r, facts: { table[$0] })
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
        check(verdict([ours], .unusable) == .conflict([ours]), "unusable receipt must make the listener a conflict")
        check(PortGuard.verdict(listeners: [ours], ownedPrefixes: owned, receipt: receipt(10), facts: { _ in nil })
              == .conflict([ours]), "an uninspectable listener must be a conflict")

        // Paths that merely resemble ownership prove nothing (the executable directory is
        // still necessary, just never sufficient).
        for lookalike in [
            "/Applications/Wisp.app/Contents/Resources/backend-evil/python",
            "/Applications/Wisp.app/Contents/Resources/backendx",
            "/tmp/Applications/Wisp.app/Contents/Resources/backend/python",
            "Applications/Wisp.app/Contents/Resources/backend/python",
            "/Applications/Wisp.app/Contents/Resources/backend/../../../../tmp/evil",
            "",
        ] {
            check(!PortGuard.executableInOwnedDirectory(listener(31, lookalike), ownedPrefixes: owned),
                  "look-alike accepted: \(lookalike)")
            let l = listener(31, lookalike)
            check(PortGuard.verdict(listeners: [l], ownedPrefixes: owned, receipt: receipt(31, path: lookalike),
                                    facts: { _ in facts(31, path: lookalike) }) == .conflict([l]),
                  "look-alike owned: \(lookalike)")
        }
        for prefixes in [[""], ["/"], ["relative/"], []] {
            check(!PortGuard.executableInOwnedDirectory(stranger, ownedPrefixes: prefixes), "degenerate prefixes \(prefixes) matched")
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
            PortGuard.terminateOwned(ls, ownedPrefixes: owned, receipt: r, facts: f, signal: recorder)
        }

        // (ii) The receipt-proven backend is signalled; nobody else is.
        check(terminate([ours, bystander, stranger], receipt(10)) == [10] && signalled.count == 1
              && signalled[0] == (10, SIGTERM), "only the receipt-proven backend gets SIGTERM")

        // (i) another process on the owned interpreter is never signalled, whatever the receipt.
        signalled = []
        for state in [BackendOwnership.ReceiptState.none, receipt(10), receipt(777), .unusable] {
            _ = terminate([bystander], state)
        }
        check(signalled.isEmpty, "a bystander on the owned interpreter was signalled")
        // No receipt (e.g. an older Wisp's backend): never signalled.
        _ = terminate([ours], .none)
        check(signalled.isEmpty, "a receipt-less listener was signalled")
        // (iv) stale receipt.
        _ = terminate([ours], receipt(777))
        check(signalled.isEmpty, "stale receipt: a listener was signalled")
        _ = terminate([ours], .unusable)
        check(signalled.isEmpty, "unusable receipt: a listener was signalled")
        // (iii) receipt pid matches but the kernel start time differs.
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
        _ = PortGuard.terminateOwned([ours], ownedPrefixes: [], receipt: receipt(10), facts: { table[$0] }, signal: recorder)
        check(signalled.isEmpty, "signalled with no owned prefixes")
    }

    static func wordingChecks() {
        let older = PortGuard.conflictMessage(port: 8765, listeners: [listener(4242)], ownedPrefixes: owned)
        check(older.contains("python3.13 (pid 4242)") && older.contains("8765") && older.contains("earlier copy of Wisp")
              && older.contains("did not stop it"), "receipt-less backend wording: \(older)")
        let stranger = PortGuard.conflictMessage(port: 8765, listeners: [listener(7, "/usr/local/bin/node")],
                                                 ownedPrefixes: owned)
        check(stranger.contains("node (pid 7)") && stranger.contains("isn't Wisp's") && !stranger.contains("earlier copy"),
              "stranger wording: \(stranger)")
    }

    // MARK: the launch receipt on disk

    static func receiptStoreChecks() {
        let fm = FileManager.default
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("wisp-receipt-\(UUID().uuidString)", isDirectory: true)
        defer { try? fm.removeItem(at: dir) }
        let file = dir.appendingPathComponent(BackendLaunchReceiptStore.fileName)
        let sample = BackendOwnership.Receipt(pid: 4242, start: Start(seconds: 1_790_000_000, microseconds: 123_456),
                                              executablePath: python, backendRoot: backendCwd, nonce: UUID().uuidString)

        // Missing directory or file: no receipt (the upgrade case), not "unusable".
        check(BackendLaunchReceiptStore.read(directory: dir) == .none, "missing receipt must read as none")

        // Atomic write, 0600, no temporary left behind, round-trips exactly.
        do { try BackendLaunchReceiptStore.write(sample, directory: dir) } catch { check(false, "write failed: \(error)") }
        var info = stat()
        check(lstat(file.path, &info) == 0 && info.st_mode & S_IFMT == S_IFREG, "receipt not a regular file")
        check(info.st_mode & 0o7777 == 0o600, "receipt mode is \(String(info.st_mode & 0o7777, radix: 8)), not 600")
        check(BackendLaunchReceiptStore.read(directory: dir) == .present(sample), "receipt did not round-trip")
        let leftovers = ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0 != BackendLaunchReceiptStore.fileName }
        check(leftovers.isEmpty, "temporary files left behind: \(leftovers)")
        // Replacing keeps exactly one file and the new content.
        let second = BackendOwnership.Receipt(pid: 4343, start: Start(seconds: 1, microseconds: 2), executablePath: python,
                                              backendRoot: backendCwd, nonce: "n2")
        do { try BackendLaunchReceiptStore.write(second, directory: dir) } catch { check(false, "rewrite failed: \(error)") }
        check(BackendLaunchReceiptStore.read(directory: dir) == .present(second), "receipt not replaced")
        check(((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []) == [BackendLaunchReceiptStore.fileName],
              "rewrite left extra files")

        // A receipt that exists but cannot be trusted is UNUSABLE, never "none": that would
        // silently re-enable the weaker legacy proof.
        let good = (try? Data(contentsOf: file)) ?? Data()
        func plant(_ data: Data, mode: mode_t = 0o600) {
            unlink(file.path)
            let fd = open(file.path, O_WRONLY | O_CREAT | O_EXCL, mode)
            _ = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
            fchmod(fd, mode)
            close(fd)
        }
        for corrupt in [Data(), Data("{".utf8), good.prefix(good.count / 2), Data("null".utf8), Data("[]".utf8),
                        Data(#"{"schema_version":1}"#.utf8), Data("garbage \u{0}".utf8),
                        Data(String(decoding: good, as: UTF8.self).replacingOccurrences(of: "\"schema_version\":1",
                                                                                         with: "\"schema_version\":2").utf8),
                        Data(String(decoding: good, as: UTF8.self).replacingOccurrences(of: "4343", with: "0").utf8)] {
            plant(corrupt)
            check(BackendLaunchReceiptStore.read(directory: dir) == .unusable,
                  "corrupt receipt not unusable: \(String(decoding: corrupt, as: UTF8.self))")
        }
        plant(good, mode: 0o644)
        check(BackendLaunchReceiptStore.read(directory: dir) == .unusable, "a group/world-readable receipt was used")
        plant(good)
        check(BackendLaunchReceiptStore.read(directory: dir) == .present(second), "re-planted good receipt not read")
        unlink(file.path)
        let elsewhere = dir.appendingPathComponent("target.json")
        fm.createFile(atPath: elsewhere.path, contents: good, attributes: [.posixPermissions: 0o600])
        symlink(elsewhere.path, file.path)
        check(BackendLaunchReceiptStore.read(directory: dir) == .unusable, "a symlinked receipt was followed")
        unlink(file.path)
        unlink(elsewhere.path)

        // The store: fail closed until configured, then memory follows disk and launches.
        let store = BackendLaunchReceiptStore()
        check(store.current == .unusable, "an unconfigured store must not report 'no receipt'")
        check(store.configure(directory: dir) == .none && store.current == .none, "configure must load the disk state")
        do { try store.record(sample) } catch { check(false, "record failed: \(error)") }
        check(store.current == .present(sample) && BackendLaunchReceiptStore.read(directory: dir) == .present(sample),
              "record must update memory and disk")
        let reopened = BackendLaunchReceiptStore()
        check(reopened.configure(directory: dir) == .present(sample), "a new store must load the persisted receipt")
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

        // This process, with its own executable directory owned: not Wisp's without a
        // receipt; Wisp's with a receipt naming its exact incarnation; not with a receipt
        // carrying a different start time.
        let mine = PortGuard.Listener(pid: me, path: facts.executablePath ?? "", start: facts.start)
        let selfDir = ((facts.executablePath ?? "") as NSString).deletingLastPathComponent + "/"
        check(BackendOwnership.verdict(listener: mine, receipt: .none, facts: facts, ownedPrefixes: [selfDir])
              == .notWisp(.noReceipt), "the test process was proven Wisp's without a receipt")
        let selfReceipt = BackendOwnership.Receipt(pid: me, start: facts.start!, executablePath: facts.executablePath!,
                                                   backendRoot: selfDir, nonce: "self")
        check(BackendOwnership.verdict(listener: mine, receipt: .present(selfReceipt), facts: facts, ownedPrefixes: [selfDir])
              == .wispBackend, "a receipt naming this process exactly was rejected")
        let reused = BackendOwnership.Receipt(pid: me, start: Start(seconds: facts.start!.seconds, microseconds:
                                                (facts.start!.microseconds + 1) % 1_000_000),
                                              executablePath: facts.executablePath!, backendRoot: selfDir, nonce: "self")
        check(BackendOwnership.verdict(listener: mine, receipt: .present(reused), facts: facts, ownedPrefixes: [selfDir])
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
            let sleeperDir = (sleeperPath as NSString).deletingLastPathComponent + "/"
            let start = BackendOwnership.startTime(pid: mine)
            let real = [PortGuard.Listener(pid: mine, path: sleeperPath, start: start)]
            // Same executable directory, no receipt: never signalled.
            check(PortGuard.terminateOwned(real, ownedPrefixes: [sleeperDir], receipt: .none).isEmpty && isAlive(mine),
                  "a real receipt-less child on an owned executable was signalled")
            // A receipt naming this exact child: it is signalled.
            let receipt = BackendOwnership.ReceiptState.present(.init(pid: mine, start: start!, executablePath: sleeperPath,
                                                                     backendRoot: sleeperDir, nonce: "n"))
            check(PortGuard.terminateOwned(real, ownedPrefixes: [sleeperDir], receipt: receipt) == [mine],
                  "real receipt-proven child was not signalled")
            usleep(300_000)
            check(!isAlive(mine), "real owned child survived SIGTERM")
            check(isAlive(theirs), "an unlisted process died")
            // The other child, same executable, same receipt store: never signalled.
            let other = [PortGuard.Listener(pid: theirs, path: PortGuard.executablePath(pid: theirs) ?? "",
                                            start: BackendOwnership.startTime(pid: theirs))]
            check(PortGuard.terminateOwned(other, ownedPrefixes: [sleeperDir], receipt: receipt).isEmpty && isAlive(theirs),
                  "a stale receipt let a bystander be signalled")
        } else {
            print("note: spawning is unavailable in this sandbox; real-process checks skipped")
        }

        if let (fd, port) = openListener() {
            defer { close(fd) }
            let found = PortGuard.listeners(port: port)
            check(found?.contains { $0.pid == getpid() && $0.start != nil } == true,
                  "real listener (with start time) not found on port \(port)")
            let selfPath = PortGuard.executablePath(pid: getpid()) ?? ""
            let selfDir = (selfPath as NSString).deletingLastPathComponent + "/"
            if case .conflict(let foreign) = PortGuard.check(port: port, ownedPrefixes: [selfDir], receipt: .none) {
                check(foreign.contains { $0.pid == getpid() }, "wrong conflict")
            } else { check(false, "a listener on an owned executable without a receipt was not a conflict") }
            let me = BackendOwnership.Receipt(pid: getpid(), start: BackendOwnership.startTime(pid: getpid())!,
                                              executablePath: selfPath, backendRoot: selfDir, nonce: "n")
            if case .owned = PortGuard.check(port: port, ownedPrefixes: [selfDir], receipt: .present(me)) { checks += 1 }
            else { check(false, "own receipt-proven listener not recognised as owned") }
        } else {
            print("note: loopback bind unavailable in this sandbox; real-listener check skipped")
        }
        check(PortGuard.check(port: 1, ownedPrefixes: owned, receipt: .none) != .owned([]), "port 1 sanity")
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
