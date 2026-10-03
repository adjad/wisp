import Darwin
import Foundation

// Is the program on Wisp's backend port Wisp's OWN backend? Two things depend on the
// answer: whether Wisp may ever signal that process (PortGuard), and whether Wisp may
// send it private requests (BackendTrust). Both use the one pure policy below.
//
// Nothing a process looks like proves it is Wisp's. The backend runs on a generic
// Python interpreter (`python -m uvicorn service.main:app`), so any other program run
// with that interpreter has the same executable path, and argv, module, environment
// and working directory are all chosen by whoever starts a process. The proof is
// instead a launch receipt bound to one kernel process incarnation: when
// BackendManager starts the backend it records the child's pid, kernel start time,
// executable path, backend root and a random launch nonce (passed to the child in its
// environment and echoed by /identity). A listener is Wisp's only if it is that exact
// process incarnation (a reused pid has a different start time) and, when /identity
// answered, it carries that launch's nonce.
//
// No receipt, a stale receipt, or an unreadable one: NOT Wisp's. That includes a
// backend left running by an older Wisp that wrote no receipt; it is reported as a
// conflict (the person quits it), never signalled, and nothing private is sent to it.
enum BackendOwnership {
    /// The child's environment variable carrying this launch's nonce. Echoed by /identity.
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
        /// No receipt file at all (never launched by a receipt-writing Wisp): nothing is Wisp's.
        case none
        /// A receipt file exists but cannot be used (corrupt, partial, wrong owner or
        /// mode, unreadable), or the store was never configured. Nothing is Wisp's.
        case unusable
        case present(Receipt)
    }

    /// What the kernel reports about one process. Any field it would not report is nil.
    struct ProcessFacts: Equatable, Sendable {
        let pid: Int32
        let start: StartTime?
        let executablePath: String?
    }

    /// What /identity said, when it was asked.
    enum Identity: Equatable, Sendable {
        case notAsked
        /// 404: a backend without /identity.
        case missing
        case answered(nonce: String?)
    }

    enum Reason: Equatable, Sendable {
        /// pid 0, 1 or negative: never Wisp's.
        case protectedProcess
        /// The process could not be inspected, or is no longer the one listed.
        case processGone
        /// The executable is not inside Wisp's backend directory.
        case foreignExecutable
        case receiptUnusable
        /// A receipt exists and this is not its process incarnation.
        case receiptMismatch
        /// The receipt's process answered /identity without this launch's nonce.
        case nonceMismatch
        /// No receipt exists, so no process can be shown to be Wisp's backend.
        case noReceipt
    }

    enum Verdict: Equatable, Sendable {
        case wispBackend
        case notWisp(Reason)
    }

    /// The one policy. Pure: decides from the facts it is given, signals and sends nothing.
    static func verdict(listener: PortGuard.Listener, receipt: ReceiptState, facts: ProcessFacts?,
                        identity: Identity = .notAsked, ownedPrefixes: [String]) -> Verdict {
        guard listener.pid > 1 else { return .notWisp(.protectedProcess) }
        guard let facts, facts.pid == listener.pid, let path = facts.executablePath, path == listener.path else {
            return .notWisp(.processGone)
        }
        // Necessary, never sufficient.
        guard PortGuard.executableInOwnedDirectory(listener, ownedPrefixes: ownedPrefixes) else {
            return .notWisp(.foreignExecutable)
        }
        switch receipt {
        case .unusable:
            return .notWisp(.receiptUnusable)
        case .present(let launched):
            guard facts.pid == launched.pid, let start = facts.start, start == launched.start,
                  path == launched.executablePath else { return .notWisp(.receiptMismatch) }
            if case .answered(let nonce) = identity {
                guard let nonce, !nonce.isEmpty, nonce == launched.nonce else { return .notWisp(.nonceMismatch) }
            }
            return .wispBackend
        case .none:
            return .notWisp(.noReceipt)
        }
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

/// The launch receipt, in memory and persisted in the app's own support directory (not
/// the user's ~/.moe data). One file, written atomically (temporary + rename), mode 0600.
final class BackendLaunchReceiptStore: @unchecked Sendable {
    static let fileName = "backend-launch-receipt.json"
    static let shared = BackendLaunchReceiptStore()
    private static let schemaVersion = 1
    private static let maximumSize = 64 * 1024

    private let lock = NSLock()
    private var directory: URL?
    /// Fail closed until configured: an unconfigured store proves nothing.
    private var state = BackendOwnership.ReceiptState.unusable

    init() {}

    /// Where the app keeps its own state (the same Application Support/Wisp directory
    /// other app state uses).
    static func defaultDirectory() -> URL? {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("Wisp", isDirectory: true)
    }

    /// Load the persisted receipt (the previous launch's backend) and keep it in memory.
    @discardableResult
    func configure(directory: URL) -> BackendOwnership.ReceiptState {
        let loaded = Self.read(directory: directory)
        lock.lock(); defer { lock.unlock() }
        self.directory = directory
        state = loaded
        return loaded
    }

    var current: BackendOwnership.ReceiptState {
        lock.lock(); defer { lock.unlock() }
        return state
    }

    /// A new backend was launched. Memory is updated first, so this run always knows its
    /// own backend; if persisting fails the older file is removed (a later launch then
    /// sees no receipt rather than a misleading one) and the error is reported.
    func record(_ receipt: BackendOwnership.Receipt) throws {
        lock.lock()
        state = .present(receipt)
        let directory = self.directory
        lock.unlock()
        guard let directory else { throw CocoaError(.fileNoSuchFile) }
        do {
            try Self.write(receipt, directory: directory)
        } catch {
            unlink(directory.appendingPathComponent(Self.fileName).path)
            throw error
        }
    }

    static func read(directory: URL) -> BackendOwnership.ReceiptState {
        let path = directory.appendingPathComponent(fileName).path
        let fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        if fd < 0 { return errno == ENOENT ? .none : .unusable }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(),
              info.st_nlink == 1, info.st_mode & 0o7777 == 0o600,
              info.st_size > 0, info.st_size <= maximumSize else { return .unusable }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: false)
        guard let data = try? handle.readToEnd(), data.count == Int(info.st_size),
              let receipt = decode(data) else { return .unusable }
        return .present(receipt)
    }

    static func write(_ receipt: BackendOwnership.Receipt, directory: URL) throws {
        var info = stat()
        if lstat(directory.path, &info) != 0 {
            guard errno == ENOENT else { throw CocoaError(.fileWriteUnknown) }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            guard lstat(directory.path, &info) == 0 else { throw CocoaError(.fileWriteUnknown) }
        }
        guard info.st_mode & S_IFMT == S_IFDIR, info.st_uid == getuid() else { throw CocoaError(.fileWriteNoPermission) }
        let data = try encode(receipt)
        let target = directory.appendingPathComponent(fileName).path
        let temporary = directory.appendingPathComponent(".\(fileName).\(UUID().uuidString)").path
        let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw CocoaError(.fileWriteUnknown) }
        defer { unlink(temporary) }   // a no-op once renamed
        let written = data.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        let ok = written == data.count && fchmod(fd, 0o600) == 0 && fsync(fd) == 0
        close(fd)
        guard ok, rename(temporary, target) == 0 else { throw CocoaError(.fileWriteUnknown) }
    }

    static func encode(_ receipt: BackendOwnership.Receipt) throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "schema_version": schemaVersion,
            "pid": Int(receipt.pid),
            "start_seconds": receipt.start.seconds,
            "start_microseconds": receipt.start.microseconds,
            "executable_path": receipt.executablePath,
            "backend_root": receipt.backendRoot,
            "nonce": receipt.nonce,
        ], options: [.sortedKeys])
    }

    /// Strict: anything but the exact documented shape is not a receipt.
    static func decode(_ data: Data) -> BackendOwnership.Receipt? {
        func integer(_ value: Any?) -> Int64? {
            guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  CFNumberIsFloatType(number) == false else { return nil }
            return number.int64Value
        }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              integer(object["schema_version"]) == Int64(schemaVersion),
              let pid = integer(object["pid"]), pid > 1, pid <= Int64(Int32.max),
              let seconds = integer(object["start_seconds"]), seconds >= 0,
              let micros = integer(object["start_microseconds"]), micros >= 0, micros < 1_000_000,
              let path = object["executable_path"] as? String, path.hasPrefix("/"),
              let root = object["backend_root"] as? String, root.hasPrefix("/"),
              let nonce = object["nonce"] as? String, !nonce.isEmpty else { return nil }
        return BackendOwnership.Receipt(pid: Int32(pid), start: .init(seconds: UInt64(seconds), microseconds: UInt64(micros)),
                                        executablePath: path, backendRoot: root, nonce: nonce)
    }
}
