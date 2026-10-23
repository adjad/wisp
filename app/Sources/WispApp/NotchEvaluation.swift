#if WISP_NOTCH_EVALUATION || WISP_NOTCH_EVALUATION_CHECKS
import AppKit
import CryptoKit
import Darwin
import Foundation
import ObjectiveC

// The contained Notch evaluation build. Compiled only with
// -DWISP_NOTCH_EVALUATION (the development host, scripts/build_notch_evaluation.sh)
// or -DWISP_NOTCH_EVALUATION_CHECKS (tests/NotchEvaluationChecks.swift). A
// production build has none of this, and nothing here can be selected at runtime.
//
// The host mounts the real OverlayPanel with the original OverlayView and
// OverlayModel, so a typed Ask goes through the normal submit, start, runAgent,
// SSE and session path. It installs no AppDelegate, BackendManager, source
// readers, native writers or assistant action stream. Every HTTP request any
// client makes passes EvaluationGuard: only the evaluation backend's exact
// origin is reachable, each request carries the run token, and a response
// without the run nonce and backend pid, or any redirect, fails the request.

/// The private run configuration written by the launcher. Read once.
struct NotchEvaluationConfig: Equatable {
    static let productionPorts: Set<Int> = [8765, 8000, 8001]

    let runID: String
    let ownedRoot: URL
    let listenPort: Int
    let token: String
    let nonce: String
    let backendPID: Int32
    /// Canonical directories inside the owned root that hold the snapshot's
    /// documents. The only places an attachment may be read from.
    let sourceRoots: [String]
    /// True only for a run the user approved with live, read-only native
    /// readers (Mail, Messages, Notes, Calendar, Reminders, Contacts).
    var liveSources: Bool = false

    var baseURL: URL { URL(string: "http://127.0.0.1:\(listenPort)")! }
    var evidence: URL { ownedRoot.appendingPathComponent("evidence/native", isDirectory: true) }

    enum Problem: Error, Equatable {
        case unreadable, notPrivate, badField(String)
    }

    /// Parse and validate. `owner` is the uid the file and root must belong to.
    static func load(path: String, owner: uid_t = getuid()) throws -> NotchEvaluationConfig {
        var info = stat()
        guard lstat(path, &info) == 0 else { throw Problem.unreadable }
        guard (info.st_mode & S_IFMT) == S_IFREG, info.st_mode & 0o077 == 0, info.st_uid == owner else {
            throw Problem.notPrivate
        }
        guard let data = FileManager.default.contents(atPath: path) else { throw Problem.unreadable }
        return try parse(data, owner: owner)
    }

    static func parse(_ data: Data, owner: uid_t = getuid()) throws -> NotchEvaluationConfig {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Problem.unreadable
        }
        guard let runID = object["run_id"] as? String, (4...64).contains(runID.count),
              runID.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || "._-".contains($0)) }) else {
            throw Problem.badField("run_id")
        }
        guard let root = object["owned_root"] as? String, root.hasPrefix("/") else { throw Problem.badField("owned_root") }
        var rootInfo = stat()
        guard lstat(root, &rootInfo) == 0, (rootInfo.st_mode & S_IFMT) == S_IFDIR, rootInfo.st_uid == owner,
              rootInfo.st_mode & 0o022 == 0, let canonicalRoot = NotchEvaluationSources.canonical(root),
              canonicalRoot == root else {
            throw Problem.badField("owned_root")
        }
        guard let port = object["listen_port"] as? Int, (1025..<65536).contains(port),
              !productionPorts.contains(port) else { throw Problem.badField("listen_port") }
        guard let token = object["token"] as? String, token.count >= 32 else { throw Problem.badField("token") }
        guard let nonce = object["nonce"] as? String, nonce.count >= 16 else { throw Problem.badField("nonce") }
        // A JSON number need not fit a pid: refuse, never trap, outside Int32.
        guard let number = object["backend_pid"] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              let pid = Int32(exactly: number.doubleValue), pid > 1, pid != getpid() else {
            throw Problem.badField("backend_pid")
        }
        var sources: [String] = []
        for raw in object["source_roots"] as? [Any] ?? [] {
            guard let text = raw as? String, let real = NotchEvaluationSources.canonical(text),
                  real.hasPrefix(root + "/"), NotchEvaluationSources.isDirectory(real) else {
                throw Problem.badField("source_roots")
            }
            sources.append(real)
        }
        let live = object["live_sources"] ?? false
        guard let liveSources = live as? Bool, CFGetTypeID(live as CFTypeRef) == CFBooleanGetTypeID() else {
            throw Problem.badField("live_sources")
        }
        return NotchEvaluationConfig(runID: runID, ownedRoot: URL(fileURLWithPath: root, isDirectory: true),
                                     listenPort: port, token: token, nonce: nonce, backendPID: pid,
                                     sourceRoots: sources, liveSources: liveSources)
    }
}

/// Where the native surface may read a file from: only the snapshot's
/// source roots. Pure, so the checks exercise exactly what the model enforces.
enum NotchEvaluationSources {
    static func canonical(_ path: String) -> String? {
        guard let resolved = realpath(path, nil) else { return nil }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    static func isDirectory(_ path: String) -> Bool {
        var info = stat()
        return lstat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR
    }

    /// The resolved file to read, or nil when `url` is not a regular file
    /// inside a source root (after resolving every link).
    static func admit(_ url: URL, config: NotchEvaluationConfig) -> URL? {
        guard url.isFileURL, let real = canonical(url.path) else { return nil }
        var info = stat()
        guard lstat(real, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else { return nil }
        guard config.sourceRoots.contains(where: { real.hasPrefix($0 + "/") }) else { return nil }
        return URL(fileURLWithPath: real)
    }
}

/// Per-attempt native receipts. Hashes and identifiers only: the typed text
/// and the visible answer are never written, so the ledger cannot leak them.
///
/// The receipts are mandatory evidence. The file must be a private regular
/// file this user owns; once a receipt cannot be persisted (or the retention
/// bound is reached) the ledger is unhealthy for good, and the host stops
/// sending requests and starting turns.
final class NotchEvaluationLedger {
    static let maxBytes = 64 * 1024 * 1024
    static let maxMemoryEntries = 10_000

    private let lock = NSLock()
    private var fd: Int32 = -1
    private var written = 0
    private var started: Set<UInt64> = []
    private var finalized: Set<UInt64> = []
    private(set) var entries: [[String: Any]] = []
    private var _failure: String?

    /// `directory` nil keeps receipts in memory only (the checks).
    init(directory: URL?, owner: uid_t = getuid()) {
        guard let directory else { return }
        let path = directory.path
        var info = stat()
        if lstat(path, &info) != 0 {
            guard mkdir(path, 0o700) == 0 || errno == EEXIST else {
                _failure = "the receipt directory could not be created"; return
            }
        }
        guard lstat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == owner,
              info.st_mode & 0o077 == 0 else {
            _failure = "the receipt directory is not a private directory"; return
        }
        let file = path + "/native-receipts.jsonl"
        let handle = open(file, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard handle >= 0 else { _failure = "the receipt file could not be opened"; return }
        guard fstat(handle, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == owner,
              info.st_mode & 0o077 == 0, info.st_nlink == 1 else {
            close(handle)
            _failure = "the receipt file is not a private regular file"; return
        }
        fd = handle
    }

    deinit { if fd >= 0 { close(fd) } }

    var healthy: Bool { lock.lock(); defer { lock.unlock() }; return _failure == nil }
    var failure: String? { lock.lock(); defer { lock.unlock() }; return _failure }

    static func sha256(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// An untrusted identifier kept as itself only when it is a short safe
    /// token; anything else is kept as `<key>_sha256`.
    static func identifier(_ key: String, _ value: String, into fields: inout [String: Any]) {
        let safe = (1...64).contains(value.utf8.count)
            && value.utf8.allSatisfy { $0 < 128 && (isalnum(Int32($0)) != 0 || $0 == 0x5F || $0 == 0x2D) }
        if safe { fields[key] = value } else { fields[key + "_sha256"] = sha256(value) }
    }

    /// False when the receipt was not persisted; the ledger is then unhealthy.
    @discardableResult
    func record(_ kind: String, _ fields: [String: Any]) -> Bool {
        var entry = fields
        entry["kind"] = kind
        entry["at"] = Date().timeIntervalSince1970
        lock.lock(); defer { lock.unlock() }
        guard _failure == nil else { return false }
        guard let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else {
            _failure = "a receipt could not be encoded"; return false
        }
        if entries.count < Self.maxMemoryEntries { entries.append(entry) }
        else if fd < 0 { _failure = "receipt retention is exhausted and there is no receipt file"; return false }
        guard fd >= 0 else { return true }
        let line = data + Data("\n".utf8)
        guard written + line.count <= Self.maxBytes else {
            _failure = "the receipt file reached its retention bound"; return false
        }
        let count = line.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        guard count == line.count else { _failure = "a receipt could not be persisted"; return false }
        written += count
        return true
    }

    /// The Ask field's text exactly as submitted: hashed, so the coordinator
    /// can match it against the case's original prompt hash.
    @discardableResult
    func input(_ prompt: String, dispatch: UInt64, session: String) -> Bool {
        var fields: [String: Any] = ["dispatch": dispatch, "input_sha256": Self.sha256(prompt),
                                     "input_length": prompt.count]
        if !session.isEmpty { Self.identifier("session", session, into: &fields) }
        let ok = record("input", fields)
        if ok { lock.lock(); started.insert(dispatch); lock.unlock() }
        return ok
    }

    /// Raw stream telemetry, current or late. Never a visible-result claim.
    @discardableResult
    func event(type: String, payload: [String: Any], dispatch: UInt64, current: Bool) -> Bool {
        var fields: [String: Any] = ["dispatch": dispatch, "current": current]
        Self.identifier("event", type, into: &fields)
        for key in ["trace_id", "id", "name", "decision", "request_id"] {
            if let value = payload[key] as? String { Self.identifier(key, value, into: &fields) }
        }
        if type == "text", let text = payload["text"] as? String { fields["text_length"] = text.count }
        return record("event", fields)
    }

    /// The finalized result a dispatch displayed, captured at its terminal
    /// event before the answer is cleared or a queued turn starts. Exactly one
    /// per started dispatch; a stale (no longer current) terminal is recorded
    /// as late telemetry and never labels a visible result.
    @discardableResult
    func terminal(_ outcome: String, visible: String, dispatch: UInt64, current: Bool,
                  session: String, trace: String?) -> Bool {
        var fields: [String: Any] = ["dispatch": dispatch, "outcome": outcome]
        guard current else { return record("late_terminal", fields) }
        lock.lock()
        let fresh = started.contains(dispatch) && !finalized.contains(dispatch)
        if fresh { finalized.insert(dispatch) }
        lock.unlock()
        guard fresh else { record("terminal_refused", fields); return false }
        fields["answer_sha256"] = Self.sha256(visible)
        fields["answer_length"] = visible.count
        fields["rendered"] = !visible.isEmpty
        if !session.isEmpty { Self.identifier("session", session, into: &fields) }
        if let trace, !trace.isEmpty { Self.identifier("trace_id", trace, into: &fields) }
        return record("visible", fields)
    }
}

/// Native effect sinks in the evaluation build: every one refuses and records.
enum NotchEvaluationSinks {
    static func blocked(_ what: String, ledger: NotchEvaluationLedger?) -> [String: Any] {
        var fields: [String: Any] = [:]
        NotchEvaluationLedger.identifier("sink", what.replacingOccurrences(of: ":", with: "-"), into: &fields)
        ledger?.record("blocked", fields)
        return ["ok": false, "status": "blocked", "evaluation_blocked": true,
                "error": "EVALUATION_BLOCKED: \(what) is not performed in the contained evaluation build"]
    }
}

/// Admission rules for every HTTP request and response. Pure, so the checks
/// exercise exactly what the URLProtocol enforces.
enum EvaluationGuard {
    static let tokenHeader = "x-wisp-evaluation-token"
    static let nonceHeader = "x-wisp-evaluation-nonce"
    static let pidHeader = "x-wisp-evaluation-pid"

    enum Refusal: Error, Equatable { case origin(String), identity(String), redirect, evidence, incomplete }

    /// The request to forward, with the run token, or why it is refused.
    static func admit(_ request: URLRequest, config: NotchEvaluationConfig) -> Result<URLRequest, Refusal> {
        guard let url = request.url, url.scheme == "http", url.host == "127.0.0.1",
              url.port == config.listenPort, url.user == nil, url.password == nil else {
            return .failure(.origin(request.url?.absoluteString ?? "<none>"))
        }
        var forwarded = request
        forwarded.setValue(config.token, forHTTPHeaderField: tokenHeader)
        return .success(forwarded)
    }

    /// A response is the evaluation backend's only when it carries this run's
    /// nonce and the launched backend's pid and is not a redirect.
    static func accept(_ response: HTTPURLResponse, config: NotchEvaluationConfig) -> Refusal? {
        if (300..<400).contains(response.statusCode) { return .redirect }
        guard response.url?.host == "127.0.0.1", response.url?.port == config.listenPort else {
            return .origin(response.url?.absoluteString ?? "<none>")
        }
        guard let nonce = response.value(forHTTPHeaderField: nonceHeader),
              constantTimeEqual(nonce, config.nonce) else { return .identity("nonce") }
        guard response.value(forHTTPHeaderField: pidHeader) == String(config.backendPID) else {
            return .identity("pid")
        }
        return nil
    }

    /// A refusal as a receipt: URLs are hashed, never stored.
    static func receipt(_ refusal: Refusal) -> [String: Any] {
        switch refusal {
        case .origin(let url): return ["reason": "origin", "url_sha256": NotchEvaluationLedger.sha256(url)]
        case .identity(let what): return ["reason": "identity", "what": what]
        case .redirect: return ["reason": "redirect"]
        case .evidence: return ["reason": "evidence"]
        case .incomplete: return ["reason": "incomplete"]
        }
    }

    static func constantTimeEqual(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        return zip(x, y).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }
}

/// Intercepts every URLSession.shared request (and any session using the
/// default protocol classes) and forwards only admitted ones over its own
/// ephemeral session, streaming the body through so SSE behaves as before.
///
/// Every request ends exactly once: a refusal, a connection that closes
/// before an accepted response, and a cancellation each produce one
/// sanitized failure (or none after stopLoading), and nothing reaches the
/// client after that.
final class EvaluationGuardProtocol: URLProtocol, URLSessionDataDelegate, @unchecked Sendable {
    nonisolated(unsafe) static var config: NotchEvaluationConfig?
    nonisolated(unsafe) static var ledger: NotchEvaluationLedger?
    /// Protocol classes of the forwarding session. Empty in the host (a real
    /// loopback socket); the checks answer forwarded requests in-process.
    nonisolated(unsafe) static var forwardingProtocols: [AnyClass] = []
    private static let forwardedKey = "WispEvaluationForwarded"
    private let stateLock = NSLock()
    private var session: URLSession?
    private var accepted = false
    private var finished = false

    override class func canInit(with request: URLRequest) -> Bool {
        property(forKey: forwardedKey, in: request) == nil
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    /// Marks the request finished; true only for the first caller.
    private func finish() -> Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        if finished { return false }
        finished = true
        return true
    }

    private var isLive: Bool { stateLock.lock(); defer { stateLock.unlock() }; return !finished }
    private var isAccepted: Bool { stateLock.lock(); defer { stateLock.unlock() }; return accepted && !finished }

    private func fail(_ refusal: EvaluationGuard.Refusal) {
        guard finish() else { return }
        Self.ledger?.record("refused", EvaluationGuard.receipt(refusal))
        // A fixed, content-free error: no URL, header or body reaches the caller.
        client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
        session?.invalidateAndCancel()
    }

    override func startLoading() {
        guard let config = Self.config else { return fail(.identity("unconfigured")) }
        // Without healthy evidence nothing more is sent.
        guard Self.ledger?.healthy ?? false else { return fail(.evidence) }
        switch EvaluationGuard.admit(request, config: config) {
        case .failure(let refusal):
            fail(refusal)
        case .success(let admitted):
            let marked = (admitted as NSURLRequest).mutableCopy() as! NSMutableURLRequest
            URLProtocol.setProperty(true, forKey: Self.forwardedKey, in: marked)
            guard Self.ledger?.record("request", ["method": admitted.httpMethod ?? "GET",
                                                  "path_sha256": NotchEvaluationLedger.sha256(admitted.url?.path ?? "")]) == true
            else { return fail(.evidence) }
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = Self.forwardingProtocols
            configuration.connectionProxyDictionary = [:]
            configuration.timeoutIntervalForRequest = request.timeoutInterval
            let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
            self.session = session
            session.dataTask(with: marked as URLRequest).resume()
        }
    }

    override func stopLoading() {
        // The loader cancelled: it expects no further callbacks.
        _ = finish()
        session?.invalidateAndCancel()
        session = nil
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
        fail(.redirect)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard isLive else { return completionHandler(.cancel) }
        guard let config = Self.config, let http = response as? HTTPURLResponse else {
            completionHandler(.cancel); return fail(.identity("response"))
        }
        if let refusal = EvaluationGuard.accept(http, config: config) {
            completionHandler(.cancel); return fail(refusal)
        }
        stateLock.lock(); accepted = true; stateLock.unlock()
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if isAccepted { client?.urlProtocol(self, didLoad: data) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard isAccepted else {
            // Closed, refused or failed before an accepted response: one failure.
            return fail(.incomplete)
        }
        guard finish() else { return }
        if let error {
            let code = (error as? URLError)?.code ?? .networkConnectionLost
            client?.urlProtocol(self, didFailWithError: URLError(code))
        } else {
            client?.urlProtocolDidFinishLoading(self)
        }
        session.finishTasksAndInvalidate()
    }
}

enum NotchEvaluationSandboxProbe {
    /// State-based proof that a profile confines this process: creating a
    /// file outside the owned root must be denied. Returns the problem, or
    /// nil. A probe file that does get created is removed at once.
    static func problem(directory: String = "/private/tmp") -> String? {
        let path = directory + "/wisp-notch-sandbox-probe-\(getpid())-\(UInt32.random(in: 0...UInt32.max))"
        let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        if fd >= 0 {
            close(fd)
            unlink(path)
            return "a write outside the owned root succeeded"
        }
        let code = errno
        return code == EPERM || code == EACCES ? nil : "the probe write failed with errno \(code), not a denial"
    }
}

/// The evaluation input adapter's wire format: one JSON line in, one out,
/// over a private Unix socket in the owned root. Pure, so the checks cover it.
enum NotchEvaluationInput {
    static let socketName = "app-input.sock"
    static let maxRequestBytes = 64 * 1024

    /// Why a bound input socket is unfit, or nil: it must be a socket owned
    /// by this user with mode 0600, whatever the umask did.
    static func socketProblem(_ info: stat) -> String? {
        guard (info.st_mode & S_IFMT) == S_IFSOCK else { return "not a socket" }
        guard info.st_uid == getuid() else { return "not owned by this user" }
        let mode = info.st_mode & 0o777
        return mode == 0o600 ? nil : "mode \(String(mode, radix: 8)), not 600"
    }

    static func socketProblem(path: String) -> String? {
        var info = stat()
        return lstat(path, &info) == 0 ? socketProblem(info) : "missing"
    }

    /// Binds and listens on a private socket at path; refuses one whose
    /// mode is not 0600.
    static func bindListener(path: String) throws -> Int32 {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else { throw NotchEvaluationConfig.Problem.badField("socket path") }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let listener = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else { throw NotchEvaluationConfig.Problem.unreadable }
        let old = umask(0o177)
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        umask(old)
        guard bound == 0 else {
            close(listener)
            throw NotchEvaluationConfig.Problem.badField("socket")
        }
        guard socketProblem(path: path) == nil, listen(listener, 4) == 0 else {
            close(listener)
            unlink(path)
            throw NotchEvaluationConfig.Problem.badField("socket mode")
        }
        return listener
    }
    static let maxTextLength = 8000

    struct Request: Equatable {
        let op: String
        let caseID: String
        let text: String
        let newChat: Bool
        let timeout: TimeInterval
    }

    enum Problem: Error, Equatable { case malformed, unauthorized, badField(String) }

    static func isSafeID(_ value: String) -> Bool {
        (1...32).contains(value.utf8.count)
            && value.utf8.allSatisfy { $0 < 128 && (isalnum(Int32($0)) != 0 || $0 == 0x5F || $0 == 0x2D) }
    }

    /// The request, or why it is refused. The run token is checked first.
    static func parse(_ data: Data, token: String) throws -> Request {
        guard data.count <= maxRequestBytes,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Problem.malformed
        }
        guard let given = object["token"] as? String, EvaluationGuard.constantTimeEqual(given, token) else {
            throw Problem.unauthorized
        }
        guard let op = object["op"] as? String, ["ask", "ping", "quit"].contains(op) else { throw Problem.badField("op") }
        if op != "ask" { return Request(op: op, caseID: "", text: "", newChat: false, timeout: 0) }
        guard let caseID = object["case"] as? String, isSafeID(caseID) else { throw Problem.badField("case") }
        guard let text = object["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              text.count <= maxTextLength else { throw Problem.badField("text") }
        let newChat = object["new_chat"] as? Bool ?? true
        let timeout = (object["timeout_s"] as? NSNumber)?.doubleValue ?? 240
        guard timeout.isFinite, (1...1800).contains(timeout) else { throw Problem.badField("timeout_s") }
        return Request(op: op, caseID: caseID, text: text, newChat: newChat, timeout: timeout)
    }

    static func encode(_ object: [String: Any]) -> Data {
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
            ?? Data(#"{"status":"refused","error":"unencodable reply"}"#.utf8)
        return data + Data("\n".utf8)
    }
}

/// What a live reader's AppleScript may contain. The OS profile can only
/// limit Apple Events by destination (Mail, Notes), never by verb, so every
/// script is checked here, and one that fails is refused, not run.
///
/// An ALLOWLIST built from the readers' own scripts: after AppleScript
/// strings and comments are removed, every word must be one the readers use
/// (`allowedWords`; numbers are allowed), every `tell` names application
/// "Mail" or "Notes", and every `set` assigns one of the readers' own locals
/// (`allowedLocals`): `set x to`, `set end of x to`, `set <date part> of
/// refDate to`, or the text item delimiters. Raw event codes (« »),
/// `current application` and any possessive other than `AppleScript's` are
/// refused outright, and `forbiddenWords` (never in the allowlist) name the
/// command words the review called out.
/// tests/test_notch_script_policy.py collects every script fragment in the
/// readers (multi-line and single-line literals, and the spliced-in helpers),
/// requires both sets here to equal exactly what they use, and checks each.
enum NotchEvaluationScriptPolicy {
    static let allowedWords: Set<String> = [
        "a", "account", "accountid", "accountname", "accounts", "acctid", "acctname", "address",
        "addresses", "and", "applescript", "application", "as", "ascii", "attemptedcount", "caphit",
        "capid", "capname", "case", "character", "code", "considering", "container", "contains",
        "content", "contents", "count", "current", "cutoffsecs", "date", "day", "days",
        "delimiters", "else", "email", "enabled", "end", "endidx", "epochsecs", "error",
        "errornumber", "every", "exit", "extract", "false", "fieldtext", "fieldvalue", "foldername",
        "from", "fs", "headerfields", "i", "id", "if", "in", "inbox",
        "is", "item", "lim", "linefeed", "m", "mailbox", "matches", "message",
        "messages", "messagetext", "modification", "month", "msgid", "n", "name", "nativeid",
        "not", "notes", "nt", "number", "of", "olddelims", "on", "or",
        "output", "plaintext", "problem", "r", "read", "readflag", "received", "recipients",
        "refdate", "repeat", "return", "rowfields", "rs", "s", "scanaccount", "scanacctid",
        "scanacctname", "sender", "senderaddress", "sendername", "set", "skippedcount", "startidx", "status",
        "stoppedearly", "string", "subj", "subject", "tell", "text", "themessages", "then",
        "thenotes", "time", "to", "toaddrs", "toline", "true", "try", "whose",
        "with", "year",
    ]
    static let allowedLocals: Set<String> = [
        "accountid", "accountname", "acctid", "acctname", "attemptedcount", "caphit", "capid", "capname",
        "cutoffsecs", "endidx", "epochsecs", "fieldtext", "foldername", "fs", "headerfields", "lim",
        "m", "matches", "msgid", "n", "nativeid", "nt", "olddelims", "output",
        "readflag", "refdate", "rowfields", "rs", "scanaccount", "scanacctid", "scanacctname", "senderaddress",
        "sendername", "skippedcount", "startidx", "stoppedearly", "subj", "themessages", "thenotes", "toaddrs",
        "toline",
    ]
    static let forbiddenWords: Set<String> = ["send", "delete", "make", "move", "duplicate", "copy", "save",
        "close", "quit", "open", "forward", "redirect", "reply", "bounce", "import", "export", "shell", "launch",
        "activate", "synchronize", "empty", "print", "mount", "store", "run", "use", "framework", "load",
        "script", "write", "display", "do", "perform", "check", "property", "global", "me", "my", "it"]
    static let dateParts: Set<String> = ["year", "month", "day", "time"]
    static let applications: Set<String> = ["mail", "notes"]

    /// Nil when the script may run; otherwise why it is refused.
    static func refusal(_ source: String) -> String? {
        let lowered = source.lowercased()
        // Raw event codes are refused even inside a string (fail-closed):
        // a « in an account name stops that scan rather than risk a bypass.
        if lowered.contains("\u{00AB}") || lowered.contains("\u{00BB}") { return "a raw event code" }
        var rest = Substring(lowered)
        while let range = rest.range(of: #"(?<![a-z_])tell\s+"#, options: .regularExpression) {
            let before = lowered[..<range.lowerBound]
            let after = rest[range.upperBound...]
            if !before.hasSuffix("end ") {
                guard let target = after.range(of: #"^application\s+"([a-z]+)""#, options: .regularExpression) else {
                    return "a tell that does not name Mail or Notes"
                }
                let name = after[target].split(separator: "\"").last.map(String.init) ?? ""
                guard applications.contains(name) else { return "a tell of \(name)" }
            }
            rest = after
        }
        let text = stripped(lowered)
        if text.range(of: #"current\s+application"#, options: .regularExpression) != nil {
            return "current application"
        }
        // The only possessive the readers use is AppleScript's text item
        // delimiters; any other (an AppleScriptObjC call, say) is refused.
        if text.replacingOccurrences(of: #"applescript\s*['\x{2019}]s\s+text\s+item\s+delimiters"#, with: "",
                                     options: .regularExpression)
               .range(of: #"['\x{2019}]\s*s\b"#, options: .regularExpression) != nil {
            return "a possessive other than AppleScript's text item delimiters"
        }
        let words = text.split { !($0.isLetter || $0.isNumber || $0 == "_") }.map(String.init)
        if let word = words.first(where: forbiddenWords.contains) { return "the command word \(word)" }
        if let word = words.first(where: { !allowedWords.contains($0) && !$0.allSatisfy(\.isNumber) }) {
            return "a word the readers do not use: \(word)"
        }
        var index = words.startIndex
        while let found = words[index...].firstIndex(of: "set") {
            let next = Array(words[(found + 1)...].prefix(5))
            if !allowedSet(next) { return "a set of something other than a reader's local" }
            index = found + 1
        }
        return nil
    }

    private static func allowedSet(_ w: [String]) -> Bool {
        if w.count >= 2, w[1] == "to" { return allowedLocals.contains(w[0]) }
        if w.count >= 4, w[1] == "of", w[3] == "to" {
            return (w[0] == "end" && allowedLocals.contains(w[2])) || (dateParts.contains(w[0]) && w[2] == "refdate")
        }
        // `set AppleScript's text item delimiters to …` splits as these words.
        return w.count >= 5 && w[0] == "applescript" && w[1] == "s" && w[2] == "text" && w[3] == "item"
            && w[4] == "delimiters"
    }
    /// Removes AppleScript string literals and comments.
    static func stripped(_ text: String) -> String {
        var out = "", chars = Array(text), i = 0, depth = 0
        while i < chars.count {
            let c = chars[i], next: Character? = i + 1 < chars.count ? chars[i + 1] : nil
            if depth > 0 {
                if c == "(", next == "*" { depth += 1; i += 2 } else if c == "*", next == ")" { depth -= 1; i += 2 } else { i += 1 }
            } else if c == "(", next == "*" {
                depth = 1; i += 2
            } else if (c == "-" && next == "-") || c == "#" {
                while i < chars.count, chars[i] != "\n" { i += 1 }
            } else if c == "\"" {
                i += 1
                while i < chars.count, chars[i] != "\"" { i += chars[i] == "\\" ? 2 : 1 }
                i += 1
                out += " \"\" "
            } else {
                out.append(c); i += 1
            }
        }
        return out
    }
}

/// Refuses every NSWorkspace open and launch in this process. The OS profile
/// must allow the Launch Services lookups (AppKit cannot start without them),
/// which would also let the app open URLs, files or other apps; each such
/// NSWorkspace entry point is replaced here, before NSApplication starts, with
/// one that records the call and refuses it: false, nil with an error, or the
/// completion handler called with an error. Nothing is handed to the OS.
enum NotchEvaluationWorkspaceGuard {
    /// Selector, and how its replacement refuses.
    enum Shape { case bool1, bool2, bool3Flag, void2Completion, void3Completion, void4Completion, void1,
                 launchURLOptions, openBundleIDs, launchBundleID, openURLsAppOptions,
                 fileImagePoint, iconOptions, fileOperation }
    static let guarded: [(String, Shape)] = [
        ("openURL:", .bool1),
        ("openURL:configuration:completionHandler:", .void3Completion),
        ("openApplicationAtURL:configuration:completionHandler:", .void3Completion),
        ("openURLs:withApplicationAtURL:configuration:completionHandler:", .void4Completion),
        ("launchApplication:", .bool1),
        ("openFile:", .bool1),
        ("openFile:withApplication:", .bool2),
        ("openFile:withApplication:andDeactivate:", .bool3Flag),
        ("launchApplicationAtURL:options:configuration:error:", .launchURLOptions),
        ("openURLs:withAppBundleIdentifier:options:additionalEventParamDescriptor:launchIdentifiers:", .openBundleIDs),
        ("launchAppWithBundleIdentifier:options:additionalEventParamDescriptor:launchIdentifier:", .launchBundleID),
        ("selectFile:inFileViewerRootedAtPath:", .bool2),
        ("activateFileViewerSelectingURLs:", .void1),
        ("openURL:options:configuration:error:", .launchURLOptions),
        ("openURLs:withApplicationAtURL:options:configuration:error:", .openURLsAppOptions),
        ("openTempFile:", .bool1),
        ("showSearchResultsForQueryString:", .bool1),
        ("openFile:fromImage:at:inView:", .fileImagePoint),
        ("recycleURLs:completionHandler:", .void2Completion),
        ("duplicateURLs:completionHandler:", .void2Completion),
        ("setIcon:forFile:options:", .iconOptions),
        ("performFileOperation:source:destination:files:tag:", .fileOperation),
    ]
    /// Selector name -> the refusing implementation installed for it.
    nonisolated(unsafe) private(set) static var installed: [String: IMP] = [:]
    nonisolated(unsafe) static var ledger: NotchEvaluationLedger?
    private static let lock = NSLock()

    static var isInstalled: Bool { lock.lock(); defer { lock.unlock() }; return !installed.isEmpty }

    static func refusal() -> NSError {
        NSError(domain: "WispNotchEvaluation", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "EVALUATION_BLOCKED: the contained evaluation build never opens or launches anything"])
    }

    private static func refuse(_ selector: String) {
        var fields: [String: Any] = [:]
        NotchEvaluationLedger.identifier("sink", "workspace-" + selector.split(separator: ":").first.map(String.init)!,
                                         into: &fields)
        fields["selector"] = selector
        ledger?.record("blocked", fields)
    }

    /// Replaces every guarded method NSWorkspace implements. Idempotent.
    static func install(ledger: NotchEvaluationLedger) {
        lock.lock(); defer { lock.unlock() }
        self.ledger = ledger
        guard installed.isEmpty, let cls = NSClassFromString("NSWorkspace") else { return }
        typealias Completion = @convention(block) (AnyObject?, NSError?) -> Void
        for (name, shape) in guarded {
            let selector = NSSelectorFromString(name)
            guard let method = class_getInstanceMethod(cls, selector) else { continue }
            let block: Any
            switch shape {
            case .bool1:
                block = { (_: AnyObject, _: AnyObject?) -> Bool in refuse(name); return false } as @convention(block) (AnyObject, AnyObject?) -> Bool
            case .bool2:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?) -> Bool in refuse(name); return false } as @convention(block) (AnyObject, AnyObject?, AnyObject?) -> Bool
            case .bool3Flag:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: Bool) -> Bool in refuse(name); return false } as @convention(block) (AnyObject, AnyObject?, AnyObject?, Bool) -> Bool
            case .void1:
                block = { (_: AnyObject, _: AnyObject?) in refuse(name) } as @convention(block) (AnyObject, AnyObject?) -> Void
            case .void2Completion:
                block = { (_: AnyObject, _: AnyObject?, completion: AnyObject?) in
                    refuse(name)
                    if let completion { unsafeBitCast(completion, to: Completion.self)(nil, refusal()) }
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?) -> Void
            case .void3Completion:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, completion: AnyObject?) in
                    refuse(name)
                    if let completion { unsafeBitCast(completion, to: Completion.self)(nil, refusal()) }
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, AnyObject?) -> Void
            case .void4Completion:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: AnyObject?, completion: AnyObject?) in
                    refuse(name)
                    if let completion { unsafeBitCast(completion, to: Completion.self)(nil, refusal()) }
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, AnyObject?, AnyObject?) -> Void
            case .launchURLOptions:
                block = { (_: AnyObject, _: AnyObject?, _: UInt, _: AnyObject?,
                           error: AutoreleasingUnsafeMutablePointer<NSError?>?) -> AnyObject? in
                    refuse(name); error?.pointee = refusal(); return nil
                } as @convention(block) (AnyObject, AnyObject?, UInt, AnyObject?, AutoreleasingUnsafeMutablePointer<NSError?>?) -> AnyObject?
            case .openBundleIDs:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: UInt, _: AnyObject?, _: OpaquePointer?) -> Bool in
                    refuse(name); return false
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, UInt, AnyObject?, OpaquePointer?) -> Bool
            case .launchBundleID:
                block = { (_: AnyObject, _: AnyObject?, _: UInt, _: AnyObject?, _: OpaquePointer?) -> Bool in
                    refuse(name); return false
                } as @convention(block) (AnyObject, AnyObject?, UInt, AnyObject?, OpaquePointer?) -> Bool
            case .openURLsAppOptions:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: UInt, _: AnyObject?,
                           error: AutoreleasingUnsafeMutablePointer<NSError?>?) -> AnyObject? in
                    refuse(name); error?.pointee = refusal(); return nil
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, UInt, AnyObject?, AutoreleasingUnsafeMutablePointer<NSError?>?) -> AnyObject?
            case .fileImagePoint:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: NSPoint, _: AnyObject?) -> Bool in
                    refuse(name); return false
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, NSPoint, AnyObject?) -> Bool
            case .iconOptions:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: UInt) -> Bool in
                    refuse(name); return false
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, UInt) -> Bool
            case .fileOperation:
                block = { (_: AnyObject, _: AnyObject?, _: AnyObject?, _: AnyObject?, _: AnyObject?,
                           tag: UnsafeMutablePointer<Int>?) -> Bool in
                    refuse(name); tag?.pointee = -1; return false
                } as @convention(block) (AnyObject, AnyObject?, AnyObject?, AnyObject?, AnyObject?, UnsafeMutablePointer<Int>?) -> Bool
            }
            let refusing = imp_implementationWithBlock(block)
            method_setImplementation(method, refusing)
            installed[name] = refusing
        }
    }

    /// True when the method NSWorkspace would run for this selector is ours.
    static func isGuarded(_ name: String) -> Bool {
        guard let cls = NSClassFromString("NSWorkspace"),
              let method = class_getInstanceMethod(cls, NSSelectorFromString(name)) else { return true }
        lock.lock(); defer { lock.unlock() }
        return installed[name].map { method_getImplementation(method) == $0 } ?? false
    }
}

/// The configured run, for WispClient.baseURL. A missing or invalid config
/// stops the process: an evaluation build never falls back to production.
enum NotchEvaluationEnvironment {
    static let config: NotchEvaluationConfig = {
        guard let path = ProcessInfo.processInfo.environment["WISP_NOTCH_EVALUATION_CONFIG"] else {
            fatalError("The Notch evaluation build needs WISP_NOTCH_EVALUATION_CONFIG")
        }
        do { return try NotchEvaluationConfig.load(path: path) } catch {
            fatalError("Invalid Notch evaluation config: \(error)")
        }
    }()
    static var baseURL: URL { config.baseURL }
    static let ledger = NotchEvaluationLedger(directory: config.evidence)
}
#endif

#if WISP_NOTCH_EVALUATION
import AppKit
import SwiftUI

/// A finalized turn exactly as OverlayModel displayed it.
struct NotchEvaluationTurn {
    let outcome: String
    let dispatch: UInt64
    let text: String
    let model: String?
    let routeRole: String?
    let routeSource: String?
    let routeReason: String?
    let tools: [[String: Any]]
    let responseModels: [String]
    let startedAt: Date?
    let firstTokenAt: Date?
    let finishedAt: Date?
    let session: String
    let traceID: String?
    /// What the notch displayed, in order (status lines, routing, tool calls
    /// with args, tool results, confirmation cards), and its step lines.
    var events: [[String: Any]] = []
    var activity: [String] = []
    var reasoning: String = ""
}

/// What OverlayModel tells the input adapter, on the main actor.
@MainActor
protocol NotchEvaluationTurnObserver: AnyObject {
    func evaluationStarted(dispatch: UInt64, inputSHA256: String)
    func evaluationConfirmation(tool: String, grantable: Bool)
    func evaluationTerminal(_ turn: NotchEvaluationTurn)
}

/// Types each corpus turn into the real Ask field. The driver sends one
/// request; the adapter sets OverlayModel.input, calls the normal submit,
/// declines any confirmation card through the card's own resolve path, and
/// answers with the finalized turn once the terminal event has been shown.
/// Text only: the rendered answer is the receipt; nothing captures the screen.
@MainActor
final class NotchEvaluationInputAdapter: NotchEvaluationTurnObserver {
    private struct Waiter {
        let request: NotchEvaluationInput.Request
        let reply: (Data) -> Void
        var started: (dispatch: UInt64, inputSHA256: String)?
        var confirmation: [String: Any]?
        var timeout: DispatchWorkItem?
    }

    private let model: OverlayModel
    private let config: NotchEvaluationConfig
    private let ledger: NotchEvaluationLedger
    private var waiter: Waiter?

    init(model: OverlayModel, config: NotchEvaluationConfig, ledger: NotchEvaluationLedger) {
        self.model = model
        self.config = config
        self.ledger = ledger
    }

    /// Bind the private socket and accept on a background thread.
    func start() throws {
        let path = config.ownedRoot.appendingPathComponent(NotchEvaluationInput.socketName).path
        let listener = try NotchEvaluationInput.bindListener(path: path)
        ledger.record("input_adapter_ready", [:])
        let thread = Thread { [weak self] in
            while true {
                let client = accept(listener, nil, nil)
                if client < 0 { continue }
                var data = Data()
                var buffer = [UInt8](repeating: 0, count: 4096)
                while data.count <= NotchEvaluationInput.maxRequestBytes {
                    let count = read(client, &buffer, buffer.count)
                    if count <= 0 { break }
                    data.append(buffer, count: count)
                    if buffer[count - 1] == 0x0A { break }
                }
                let reply: @Sendable (Data) -> Void = { payload in
                    _ = payload.withUnsafeBytes { write(client, $0.baseAddress, $0.count) }
                    close(client)
                }
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { self?.handle(data, reply: reply) ?? reply(NotchEvaluationInput.encode(["status": "refused"])) }
                }
            }
        }
        thread.name = "wisp-evaluation-input"
        thread.start()
    }

    private func handle(_ data: Data, reply: @escaping (Data) -> Void) {
        let request: NotchEvaluationInput.Request
        do {
            request = try NotchEvaluationInput.parse(data, token: config.token)
        } catch {
            ledger.record("input_refused", ["reason": "\(error)"])
            return reply(NotchEvaluationInput.encode(["status": "refused", "error": "\(error)"]))
        }
        if request.op == "quit" {
            // The supervisor's token-checked stop: the app ends itself. Nothing
            // ever signals it, since a Launch Services launch is not a child.
            ledger.record("quit_requested", [:])
            reply(NotchEvaluationInput.encode(["status": "quitting"]))
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { NSApp.terminate(nil) }
            return
        }
        if request.op == "ping" {
            return reply(NotchEvaluationInput.encode([
                "status": ledger.healthy ? "ready" : "unhealthy", "settled": model.conversationSettled]))
        }
        guard ledger.healthy else { return reply(NotchEvaluationInput.encode(["status": "unhealthy"])) }
        guard waiter == nil else { return reply(NotchEvaluationInput.encode(["status": "busy"])) }
        if request.newChat {
            model.newChat()
        } else if !model.conversationSettled {
            return reply(NotchEvaluationInput.encode(["status": "busy"]))
        }
        model.tab = .ask
        waiter = Waiter(request: request, reply: reply)
        model.input = request.text
        model.submit()
        guard waiter?.started != nil else {
            waiter = nil
            return reply(NotchEvaluationInput.encode(["status": "not_submitted", "case": request.caseID]))
        }
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let current = self.waiter, current.request.caseID == request.caseID else { return }
            self.waiter = nil
            current.reply(NotchEvaluationInput.encode([
                "status": "timeout", "case": request.caseID,
                "dispatch": current.started?.dispatch ?? 0, "partial_text": self.model.answer]))
        }
        waiter?.timeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + request.timeout, execute: timeout)
    }

    func evaluationStarted(dispatch: UInt64, inputSHA256: String) {
        if waiter != nil, waiter?.started == nil { waiter?.started = (dispatch, inputSHA256) }
    }

    func evaluationConfirmation(tool: String, grantable: Bool) {
        guard waiter != nil else { return }
        waiter?.confirmation = ["tool": tool, "grantable": grantable, "declined": true]
        ledger.record("confirmation_declined", [:])
        // Declined once through the card's own path, after it has rendered.
        DispatchQueue.main.async { [weak self] in self?.model.resolve(false) }
    }

    func evaluationTerminal(_ turn: NotchEvaluationTurn) {
        guard let current = waiter, current.started?.dispatch == turn.dispatch else { return }
        current.timeout?.cancel()
        waiter = nil
        // Let the finished turn render before it is reported.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self else { return }
            var result: [String: Any] = [
                "status": "terminal", "case": current.request.caseID, "dispatch": turn.dispatch,
                "outcome": turn.outcome, "text": turn.text, "tools": turn.tools,
                "response_models": turn.responseModels, "session": turn.session,
                "events": turn.events, "activity": turn.activity, "reasoning": turn.reasoning,
                "input_sha256": current.started?.inputSHA256 ?? "",
            ]
            for (key, value) in [("model", turn.model), ("route_role", turn.routeRole), ("route_source", turn.routeSource),
                                 ("route_reason", turn.routeReason), ("trace_id", turn.traceID)] {
                if let value { result[key] = value }
            }
            for (key, value) in [("started_at", turn.startedAt), ("first_token_at", turn.firstTokenAt),
                                 ("finished_at", turn.finishedAt)] {
                if let value { result[key] = value.timeIntervalSince1970 }
            }
            if let confirmation = current.confirmation { result["confirmation"] = confirmation }
            current.reply(NotchEvaluationInput.encode(result))
        }
    }
}

/// The live, read-only native readers of an approved real-data run.
@MainActor
final class NotchEvaluationReaders {
    let calendar = CalendarReader()
    let mail = MailReader()
    let messages = MessagesReader()
    let reminders = RemindersWriter()
    let notes = NotesReader()
    let contacts = ContactsReader()

    private let ledger: NotchEvaluationLedger
    private var stream: Task<Void, Never>?

    /// True (and recorded) when a reader's AppleScript fails the write check.
    /// Called from the readers' own background queues.
    nonisolated static func refuseScript(_ source: String, reader: String) -> Bool {
        guard let reason = NotchEvaluationScriptPolicy.refusal(source) else { return false }
        var fields: [String: Any] = ["reason": reason, "script_sha256": NotchEvaluationLedger.sha256(source)]
        NotchEvaluationLedger.identifier("reader", reader, into: &fields)
        NotchEvaluationEnvironment.ledger.record("script_refused", fields)
        return true
    }

    init(ledger: NotchEvaluationLedger) { self.ledger = ledger }

    func start() {
        calendar.start()
        mail.start()
        messages.start()
        reminders.start()
        notes.start()
        contacts.start()
        stream = Task { [weak self] in await self?.followReReadRequests() }
    }

    /// Re-reads the named sources. The backend admits exactly this request on
    /// the event stream; anything else that arrives is ignored and recorded.
    func sync(_ sources: [String]) {
        ledger.record("live_resync", ["sources": sources.sorted().joined(separator: ",")])
        for source in Set(sources) {
            switch source {
            case "calendar": calendar.sync()
            case "reminders": reminders.sync()
            case "email": mail.sync()
            case "messages": messages.sync()
            case "notes": notes.sync()
            default: break
            }
        }
    }

    private func followReReadRequests() async {
        let url = WispClient.baseURL.appendingPathComponent("assistant/events")
        while !Task.isCancelled {
            if let (bytes, _) = try? await URLSession.shared.bytes(from: url) {
                do {
                    for try await line in bytes.lines where line.hasPrefix("data: ") {
                        guard let object = try? JSONSerialization.jsonObject(with: Data(line.dropFirst(6).utf8))
                                as? [String: Any], let kind = object["type"] as? String else { continue }
                        if kind == "sync_assistant_sources_now", let sources = object["sources"] as? [String] {
                            sync(sources)
                        } else if kind != "heartbeat" && kind != "hello" {
                            var fields: [String: Any] = [:]
                            NotchEvaluationLedger.identifier("event", kind, into: &fields)
                            ledger.record("event_ignored", fields)
                        }
                    }
                } catch {}
            }
            try? await Task.sleep(nanoseconds: 3_000_000_000)
        }
    }
}

/// Ends the app when its backend stops proving this run's identity, so a
/// Launch Services launch never outlives the supervised backend. Runs on its
/// own queue and exits directly, so a wedged main thread cannot keep the app
/// alive (a token-checked quit needs the main thread; this does not).
final class NotchEvaluationIdentityWatch: @unchecked Sendable {
    static let interval: TimeInterval = 10
    static let allowedFailures = 3
    private let queue = DispatchQueue(label: "wisp-evaluation-identity", qos: .utility)
    private var timer: DispatchSourceTimer?
    private var failures = 0
    private let ledger: NotchEvaluationLedger

    init(ledger: NotchEvaluationLedger) { self.ledger = ledger }

    func start() {
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + Self.interval, repeating: Self.interval)
        timer.setEventHandler { [weak self] in self?.check() }
        self.timer = timer
        timer.resume()
    }

    private func check() {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("evaluation/identity"))
        request.timeoutInterval = 5
        let done = DispatchSemaphore(value: 0)
        var ok = false
        // The guard protocol accepts a reply only with this run's nonce and pid.
        URLSession.shared.dataTask(with: request) { _, response, error in
            ok = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
            done.signal()
        }.resume()
        if done.wait(timeout: .now() + 8) == .timedOut { ok = false }
        failures = ok ? 0 : failures + 1
        guard failures >= Self.allowedFailures else { return }
        timer?.cancel()
        ledger.record("identity_lost_exit", ["failures": failures])
        exit(3)
    }
}

/// Confines this process with the launcher's profile. A Launch Services
/// launch (needed so macOS attributes permission prompts to this app, not to
/// a sandbox-exec wrapper) cannot be wrapped, so the app applies the same
/// owned-root profile to itself before it reads anything. Fails closed.
enum NotchEvaluationSelfSandbox {
    private typealias SandboxInit = @convention(c) (
        UnsafePointer<CChar>, UInt64, UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> Int32

    static let launchFlag = "WISP_NOTCH_EVALUATION_LAUNCH"
    private(set) static var applied = false

    /// A Launch Services launch must confine itself; nothing else may skip it.
    static func required(config: NotchEvaluationConfig, environment: [String: String]) -> Bool {
        config.liveSources || environment[launchFlag] != nil
    }

    static func apply(profileAt url: URL) {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { fatalError("The evaluation profile is missing") }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
              info.st_mode & 0o077 == 0, info.st_size > 0, info.st_size < 1 << 20 else {
            close(fd); fatalError("The evaluation profile is not a private regular file")
        }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        guard let text = String(data: handle.readDataToEndOfFile(), encoding: .utf8),
              let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "sandbox_init") else {
            fatalError("The evaluation profile could not be applied")
        }
        let sandboxInit = unsafeBitCast(symbol, to: SandboxInit.self)
        var error: UnsafeMutablePointer<CChar>?
        guard sandboxInit(text, 0, &error) == 0 else {
            fatalError("The evaluation profile was refused: \(error.map { String(cString: $0) } ?? "unknown")")
        }
        applied = true
    }
}

/// Mounts the real notch Ask surface against the evaluation backend.
@MainActor
enum NotchEvaluationHost {
    private static var panel: OverlayPanel?
    private static var retained: [AnyObject] = []

    static func run() {
        let config = NotchEvaluationEnvironment.config
        let environment = ProcessInfo.processInfo.environment
        if NotchEvaluationSelfSandbox.required(config: config, environment: environment) {
            NotchEvaluationSelfSandbox.apply(profileAt: config.ownedRoot.appendingPathComponent("app.sb"))
        }
        // Launched through Launch Services means no sandbox-exec wrapper: never
        // run unconfined.
        if environment[NotchEvaluationSelfSandbox.launchFlag] != nil, !NotchEvaluationSelfSandbox.applied {
            fatalError("A Launch Services launch must apply its evaluation profile")
        }
        // Either way (sandbox-exec or self-applied) the profile must be in
        // force: prove it by a write it denies, not by a flag.
        if let problem = NotchEvaluationSandboxProbe.problem() {
            fatalError("The evaluation profile is not in force: \(problem)")
        }
        // Before NSApplication: the profile allows the Launch Services lookups
        // AppKit needs, so every NSWorkspace open/launch is refused in-app.
        NotchEvaluationWorkspaceGuard.install(ledger: NotchEvaluationEnvironment.ledger)
        guard NotchEvaluationWorkspaceGuard.isInstalled,
              NotchEvaluationWorkspaceGuard.guarded.allSatisfy({ NotchEvaluationWorkspaceGuard.isGuarded($0.0) }) else {
            fatalError("The evaluation workspace guard is not installed")
        }
        EvaluationGuardProtocol.config = config
        EvaluationGuardProtocol.ledger = NotchEvaluationEnvironment.ledger
        guard URLProtocol.registerClass(EvaluationGuardProtocol.self) else {
            fatalError("The evaluation network guard could not be installed")
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let client = WispClient()
        // Debug mode on: the backend then reports each completion's raw reply,
        // so every case records the model id the engine actually served.
        let model = OverlayModel(loadDebugMode: { true }, saveDebugMode: { _ in })
        let research = ResearchModel(client: client)
        let today = TodayModel()
        let settings = SettingsLoader()
        let setup = SetupGuideModel()
        let corpus = NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client)))
        retained = [model, research, today, settings, setup, corpus]
        // No onCorpusCommand: the /corpus surface reports itself unavailable.
        // No native calendar/reminder/research/sync callbacks: each stays nil.
        let ledger = NotchEvaluationEnvironment.ledger
        if config.liveSources {
            // The app's own readers, read-only: each opens its store read-only
            // or runs a fixed read script, and pushes this launch's read to the
            // evaluation backend. Every write path stays refused (OverlayModel
            // seams and the backend boundary); Browser History and Activity are
            // not sources of this run.
            let readers = NotchEvaluationReaders(ledger: ledger)
            readers.start()
            retained.append(readers)
            ledger.record("live_readers_started", [:])
        }
        let panel = OverlayPanel {
            OverlayView(model: model, corpus: corpus, researchModel: research, todayModel: today,
                        settings: settings, setup: setup, onClose: {}, onDismiss: {})
                // No link leaves the surface: Markdown links and any openURL
                // action are refused and recorded, never handed to the OS.
                .environment(\.openURL, OpenURLAction { _ in
                    _ = NotchEvaluationSinks.blocked("open_url", ledger: ledger)
                    return .discarded
                })
        }
        panel.isCompact = { model.collapsed }
        model.onResize = { DispatchQueue.main.async { panel.resizeToFit() } }
        model.onResizeNow = { panel.resizeToFit() }
        model.requestExpand = { model.collapsed = false; panel.resizeToFit() }
        model.requestCollapse = {}
        model.tab = .ask
        model.collapsed = false
        model.closing = false
        Self.panel = panel
        let adapter = NotchEvaluationInputAdapter(model: model, config: config, ledger: ledger)
        model.evaluationObserver = adapter
        do { try adapter.start() } catch { fatalError("The evaluation input adapter could not start: \(error)") }
        retained.append(adapter)
        let watch = NotchEvaluationIdentityWatch(ledger: ledger)
        watch.start()
        retained.append(watch)
        guard ledger.healthy else {
            fatalError("Notch evaluation receipts are unavailable: \(ledger.failure ?? "unknown")")
        }
        ledger.record("host_ready", [
            "run_id": config.runID, "pid": Int(getpid()), "backend_pid": Int(config.backendPID),
            "listen_port": config.listenPort, "surface": "OverlayPanel+OverlayView+OverlayModel",
        ])
        panel.present()
        panel.dropOpen()
        panel.makeKeyAndOrderFront(nil)
        app.activate(ignoringOtherApps: true)
        app.run()
    }
}
#endif
