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
        guard lstat(root, &rootInfo) == 0, (rootInfo.st_mode & S_IFMT) == S_IFDIR, rootInfo.st_uid == owner else {
            throw Problem.badField("owned_root")
        }
        guard let port = object["listen_port"] as? Int, (1025..<65536).contains(port),
              !productionPorts.contains(port) else { throw Problem.badField("listen_port") }
        guard let token = object["token"] as? String, token.count >= 32 else { throw Problem.badField("token") }
        guard let nonce = object["nonce"] as? String, nonce.count >= 16 else { throw Problem.badField("nonce") }
        guard let pid = object["backend_pid"] as? Int, pid > 1, pid != Int(getpid()) else {
            throw Problem.badField("backend_pid")
        }
        return NotchEvaluationConfig(runID: runID, ownedRoot: URL(fileURLWithPath: root, isDirectory: true),
                                     listenPort: port, token: token, nonce: nonce, backendPID: Int32(pid))
    }
}

/// Per-attempt native receipts. Hashes and identifiers only: the typed text
/// and the visible answer are never written, so the ledger cannot leak them.
final class NotchEvaluationLedger {
    private let url: URL?
    private let lock = NSLock()
    private(set) var entries: [[String: Any]] = []

    init(directory: URL?) {
        if let directory {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700])
            url = directory.appendingPathComponent("native-receipts.jsonl")
        } else {
            url = nil
        }
    }

    static func sha256(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    func record(_ kind: String, _ fields: [String: Any]) {
        var entry = fields
        entry["kind"] = kind
        entry["at"] = Date().timeIntervalSince1970
        lock.lock(); defer { lock.unlock() }
        entries.append(entry)
        guard let url, let data = try? JSONSerialization.data(withJSONObject: entry, options: [.sortedKeys]) else { return }
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: data + Data("\n".utf8))
    }

    /// The Ask field's text exactly as submitted: hashed, so the coordinator
    /// can match it against the case's original prompt hash.
    func input(_ prompt: String, dispatch: UInt64, session: String) {
        record("input", ["dispatch": dispatch, "session": session, "input_sha256": Self.sha256(prompt),
                         "input_length": prompt.count])
    }

    func event(type: String, payload: [String: Any], dispatch: UInt64) {
        var fields: [String: Any] = ["dispatch": dispatch, "event": type]
        for key in ["trace_id", "id", "name", "decision", "request_id"] {
            if let value = payload[key] as? String { fields[key] = value }
        }
        if type == "text", let text = payload["text"] as? String { fields["text_length"] = text.count }
        record("event", fields)
    }

    func visible(answer: String, dispatch: UInt64) {
        record("visible", ["dispatch": dispatch, "answer_sha256": Self.sha256(answer), "answer_length": answer.count])
    }
}

/// Native effect sinks in the evaluation build: every one refuses and records.
enum NotchEvaluationSinks {
    static func blocked(_ what: String, ledger: NotchEvaluationLedger?) -> [String: Any] {
        ledger?.record("blocked", ["sink": what])
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

    enum Refusal: Error, Equatable { case origin(String), identity(String), redirect }

    /// The request to forward, with the run token, or why it is refused.
    static func admit(_ request: URLRequest, config: NotchEvaluationConfig) -> Result<URLRequest, Refusal> {
        guard let url = request.url, url.scheme == "http", url.host == "127.0.0.1",
              url.port == config.listenPort, url.user == nil, url.password == nil else {
            return .failure(.origin(request.url?.absoluteString.prefix(120).description ?? "<none>"))
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

    static func constantTimeEqual(_ a: String, _ b: String) -> Bool {
        let x = Array(a.utf8), y = Array(b.utf8)
        guard x.count == y.count else { return false }
        return zip(x, y).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }
}

/// Intercepts every URLSession.shared request (and any session using the
/// default protocol classes) and forwards only admitted ones over its own
/// ephemeral session, streaming the body through so SSE behaves as before.
final class EvaluationGuardProtocol: URLProtocol, URLSessionDataDelegate, @unchecked Sendable {
    nonisolated(unsafe) static var config: NotchEvaluationConfig?
    nonisolated(unsafe) static var ledger: NotchEvaluationLedger?
    /// Protocol classes of the forwarding session. Empty in the host (a real
    /// loopback socket); the checks answer forwarded requests in-process.
    nonisolated(unsafe) static var forwardingProtocols: [AnyClass] = []
    private static let forwardedKey = "WispEvaluationForwarded"
    private var session: URLSession?
    private var accepted = false

    override class func canInit(with request: URLRequest) -> Bool {
        property(forKey: forwardedKey, in: request) == nil
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    private func fail(_ refusal: EvaluationGuard.Refusal) {
        Self.ledger?.record("refused", ["reason": "\(refusal)"])
        client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
    }

    override func startLoading() {
        guard let config = Self.config else { return fail(.identity("unconfigured")) }
        switch EvaluationGuard.admit(request, config: config) {
        case .failure(let refusal):
            fail(refusal)
        case .success(let admitted):
            let marked = (admitted as NSURLRequest).mutableCopy() as! NSMutableURLRequest
            URLProtocol.setProperty(true, forKey: Self.forwardedKey, in: marked)
            Self.ledger?.record("request", ["method": admitted.httpMethod ?? "GET",
                                            "path": admitted.url?.path ?? ""])
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
        session?.invalidateAndCancel()
        session = nil
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
        fail(.redirect)
        task.cancel()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let config = Self.config, let http = response as? HTTPURLResponse else {
            completionHandler(.cancel); return fail(.identity("response"))
        }
        if let refusal = EvaluationGuard.accept(http, config: config) {
            completionHandler(.cancel); return fail(refusal)
        }
        accepted = true
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if accepted { client?.urlProtocol(self, didLoad: data) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard accepted else { return }
        if let error { client?.urlProtocol(self, didFailWithError: error) }
        else { client?.urlProtocolDidFinishLoading(self) }
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
        let panel = OverlayPanel {
            OverlayView(model: model, corpus: corpus, researchModel: research, todayModel: today,
                        settings: settings, setup: setup, onClose: {}, onDismiss: {})
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
        NotchEvaluationEnvironment.ledger.record("host_ready", [
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
