#if WISP_NOTCH_EVALUATION || WISP_NOTCH_EVALUATION_CHECKS
import CryptoKit
import Darwin
import Foundation

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
        return NotchEvaluationConfig(runID: runID, ownedRoot: URL(fileURLWithPath: root, isDirectory: true),
                                     listenPort: port, token: token, nonce: nonce, backendPID: pid,
                                     sourceRoots: sources)
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

/// The evaluation input adapter's wire format: one JSON line in, one out,
/// over a private Unix socket in the owned root. Pure, so the checks cover it.
enum NotchEvaluationInput {
    static let socketName = "app-input.sock"
    static let maxRequestBytes = 64 * 1024
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
        guard let op = object["op"] as? String, ["ask", "ping"].contains(op) else { throw Problem.badField("op") }
        if op == "ping" { return Request(op: op, caseID: "", text: "", newChat: false, timeout: 0) }
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
        guard bound == 0, listen(listener, 4) == 0 else {
            close(listener)
            throw NotchEvaluationConfig.Problem.badField("socket")
        }
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

/// Mounts the real notch Ask surface against the evaluation backend.
@MainActor
enum NotchEvaluationHost {
    private static var panel: OverlayPanel?
    private static var retained: [AnyObject] = []

    static func run() {
        let config = NotchEvaluationEnvironment.config
        EvaluationGuardProtocol.config = config
        EvaluationGuardProtocol.ledger = NotchEvaluationEnvironment.ledger
        guard URLProtocol.registerClass(EvaluationGuardProtocol.self) else {
            fatalError("The evaluation network guard could not be installed")
        }
        let defaults = UserDefaults(suiteName: "com.wisp.notch-evaluation.\(config.runID)")!
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let client = WispClient()
        let model = OverlayModel(loadDebugMode: { defaults.bool(forKey: "wisp.debugMode") },
                                 saveDebugMode: { defaults.set($0, forKey: "wisp.debugMode") })
        let research = ResearchModel(client: client)
        let today = TodayModel()
        let settings = SettingsLoader()
        let setup = SetupGuideModel()
        let corpus = NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client)))
        retained = [model, research, today, settings, setup, corpus]
        // No onCorpusCommand: the /corpus surface reports itself unavailable.
        // No native calendar/reminder/research/sync callbacks: each stays nil.
        let ledger = NotchEvaluationEnvironment.ledger
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
