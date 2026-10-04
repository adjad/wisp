import Darwin
import Foundation

// Threat model. In scope: another program running as the same user that squats Wisp's
// backend port (127.0.0.1:8765), and that can create or rewrite any file Wisp can,
// including anything Wisp might keep about its own state. Out of scope: an attacker who
// can read or write Wisp's memory, attach a debugger, or inject code into it (the app is
// ad-hoc signed, so that attacker already controls Wisp outright).
//
// Is the program on Wisp's backend port Wisp's OWN backend? Two things depend on the
// answer: whether Wisp may ever signal that process (PortGuard), and whether Wisp may
// send it private requests. Both use the one pure policy below.
//
// Nothing a process looks like proves it is Wisp's. The backend runs on a generic
// Python interpreter (`python -m uvicorn service.main:app`), so any other program run
// with that interpreter has the same executable path, and argv, module, environment,
// working directory and executable location are all chosen by whoever starts a process.
// The proof is instead a launch receipt bound to one kernel process incarnation, held
// ONLY in this app's memory: when BackendManager spawns the backend it records the
// child's pid, kernel start time, executable path, backend root and a random launch
// nonce (passed to the child in its environment). A listener is Wisp's only if it is
// that exact process incarnation (a reused pid has a different start time) and, when it
// answered an identity request, it carries that launch's nonce.
//
// The receipt is never written to or read from disk: a same-user program could forge a
// receipt file naming its own process. So every app launch starts with NO receipt, and
// whatever already holds the port at startup (including a backend an earlier Wisp left
// behind after a crash or upgrade) is reported as a conflict, never adopted or signalled.
enum BackendOwnership {
    /// The child's environment variable carrying this launch's nonce.
    static let nonceEnvironmentKey = "WISP_LAUNCH_NONCE"

    /// The kernel's process start time (proc_bsdinfo pbi_start_tvsec/usec). Together
    /// with the pid it names one process incarnation: a recycled pid has a new one.
    struct StartTime: Equatable, Hashable, Sendable {
        let seconds: UInt64
        let microseconds: UInt64
    }

    struct Receipt: Equatable, Sendable {
        let pid: Int32
        let start: StartTime
        let executablePath: String
        let backendRoot: String
        let nonce: String
    }

    enum ReceiptState: Equatable, Sendable {
        /// This app instance has not spawned a backend: nothing is Wisp's.
        case none
        case present(Receipt)
    }

    /// What the kernel reports about one process. Any field it would not report is nil.
    struct ProcessFacts: Equatable, Sendable {
        let pid: Int32
        let start: StartTime?
        let executablePath: String?
    }

    /// What the backend's identity endpoint said, when it was asked.
    enum Identity: Equatable, Sendable {
        case notAsked
        /// 404: a backend without an identity endpoint.
        case missing
        case answered(nonce: String?)
    }

    enum Reason: Equatable, Sendable {
        /// pid 0, 1 or negative: never Wisp's.
        case protectedProcess
        /// The process could not be inspected, or is no longer the one listed.
        case processGone
        /// A receipt exists and this is not its process incarnation.
        case receiptMismatch
        /// The receipt's process answered the identity request without this launch's nonce.
        case nonceMismatch
        /// This app instance spawned no backend, so no process can be shown to be Wisp's.
        case noReceipt
    }

    enum Verdict: Equatable, Sendable {
        case wispBackend
        case notWisp(Reason)
    }

    /// The one policy. Pure: decides from the facts it is given, signals and sends nothing.
    /// Where the executable lives is deliberately NOT a signal (a development interpreter
    /// may resolve anywhere, and a squatter can run from anywhere): the matching in-memory
    /// receipt incarnation is the proof.
    static func verdict(listener: PortGuard.Listener, receipt: ReceiptState, facts: ProcessFacts?,
                        identity: Identity = .notAsked) -> Verdict {
        guard listener.pid > 1 else { return .notWisp(.protectedProcess) }
        guard let facts, facts.pid == listener.pid, let path = facts.executablePath, path == listener.path else {
            return .notWisp(.processGone)
        }
        guard case .present(let launched) = receipt else { return .notWisp(.noReceipt) }
        guard facts.pid == launched.pid, let start = facts.start, start == launched.start,
              path == launched.executablePath else { return .notWisp(.receiptMismatch) }
        if case .answered(let nonce) = identity {
            guard let nonce, !nonce.isEmpty, nonce == launched.nonce else { return .notWisp(.nonceMismatch) }
        }
        return .wispBackend
    }

    /// What BackendManager does after asking whether something answers on the backend port.
    enum PortDecision: Equatable, Sendable {
        /// Go on with the normal launch path (which itself refuses while a child exists).
        case proceed
        /// The responder is this manager's own live, recorded child: nothing to do.
        case ownBackendRunning
        /// Something answers that is not this manager's live, recorded child. Start no
        /// second backend, signal nothing, and report the conflict.
        case stranger
    }

    /// Pure. A healthy reply counts as Wisp's backend ONLY when this manager has a live
    /// spawned child (`liveChildPID`) whose kernel facts match the in-memory receipt.
    static func portDecision(healthy: Bool, freshRecovery: Bool, liveChildPID: Int32?,
                             receipt: ReceiptState, childFacts: ProcessFacts?) -> PortDecision {
        guard healthy else { return .proceed }
        guard let pid = liveChildPID, let childFacts, let path = childFacts.executablePath,
              verdict(listener: PortGuard.Listener(pid: pid, path: path, start: childFacts.start),
                      receipt: receipt, facts: childFacts) == .wispBackend else { return .stranger }
        return freshRecovery ? .proceed : .ownBackendRunning
    }

    // MARK: - Kernel readers

    static func processFacts(pid: Int32) -> ProcessFacts? {
        guard pid > 0, let start = startTime(pid: pid) else { return nil }
        return ProcessFacts(pid: pid, start: start, executablePath: PortGuard.executablePath(pid: pid))
    }

    static func startTime(pid: Int32) -> StartTime? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard pid > 0, proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        return StartTime(seconds: info.pbi_start_tvsec, microseconds: info.pbi_start_tvusec)
    }
}

/// The receipt for the backend THIS app instance spawned. Memory only: it starts empty on
/// every launch and is never persisted or loaded, so nothing on disk can make a process
/// Wisp's. (The name is kept for continuity with existing callers; there is no file.)
final class BackendLaunchReceiptStore: @unchecked Sendable {
    static let shared = BackendLaunchReceiptStore()

    private let lock = NSLock()
    private var state = BackendOwnership.ReceiptState.none

    init() {}

    var current: BackendOwnership.ReceiptState {
        lock.lock(); defer { lock.unlock() }
        return state
    }

    /// This app just spawned a backend: remember exactly which process incarnation it is.
    func record(_ receipt: BackendOwnership.Receipt) {
        lock.lock(); defer { lock.unlock() }
        state = .present(receipt)
    }

    /// A framework Python re-executes itself at startup, which changes the executable path
    /// the kernel reports but not the pid or start time. Re-record that path for the SAME
    /// incarnation only (never for a different pid or start time). Returns whether it changed.
    @discardableResult
    func refreshExecutablePath(pid: Int32, start: BackendOwnership.StartTime, executablePath: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard case .present(let launched) = state, launched.pid == pid, launched.start == start,
              executablePath.hasPrefix("/"), executablePath != launched.executablePath else { return false }
        state = .present(.init(pid: pid, start: start, executablePath: executablePath,
                               backendRoot: launched.backendRoot, nonce: launched.nonce))
        return true
    }
}
