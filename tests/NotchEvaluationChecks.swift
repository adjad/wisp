// Compile with -DWISP_NOTCH_EVALUATION_CHECKS and app/Sources/WispApp/NotchEvaluation.swift,
// WispClient.swift and AgentSend.swift. Exercises the evaluation build's
// config validation, request admission, response identity, redirect refusal,
// native sink denial and hash-only receipts. Forwarded requests are answered
// by an in-process stub backend: nothing reaches a socket.
import Darwin
import Foundation

/// The stub evaluation backend behind the guard. Records what it was sent.
final class StubBackend: URLProtocol {
    nonisolated(unsafe) static var headers: [String: String] = [:]
    nonisolated(unsafe) static var seen: [URLRequest] = []
    nonisolated(unsafe) static var redirect = false

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.seen.append(request)
        let url = request.url!
        if Self.redirect {
            let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: nil,
                                           headerFields: ["Location": "https://example.com/"])!
            client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: URL(string: "https://example.com/")!),
                                redirectResponse: response)
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil,
                                       headerFields: Self.headers.merging(["Content-Type": "text/event-stream"]) { a, _ in a })!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        for line in [#"{"type":"session","id":"abc123"}"#, #"{"type":"text","text":"SYNTHETIC"}"#, #"{"type":"done"}"#] {
            client?.urlProtocol(self, didLoad: Data("data: \(line)\n\n".utf8))
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@main
struct NotchEvaluationChecks {
    nonisolated(unsafe) static var passed = 0

    static func check(_ condition: Bool, _ label: String) {
        guard condition else { print("FAIL: \(label)"); exit(1) }
        passed += 1
    }

    static func configJSON(_ overrides: [String: Any] = [:], root: String) -> Data {
        var object: [String: Any] = ["run_id": "checks-1", "owned_root": root, "listen_port": 18891,
                                     "token": String(repeating: "t", count: 40), "nonce": String(repeating: "n", count: 24),
                                     "backend_pid": 4242]
        for (key, value) in overrides { object[key] = value }
        return try! JSONSerialization.data(withJSONObject: object)
    }

    static func fetch(_ url: URL) async -> (status: Int?, body: String, error: Bool) {
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            return ((response as? HTTPURLResponse)?.statusCode, String(decoding: data, as: UTF8.self), false)
        } catch {
            return (nil, "", true)
        }
    }

    static func main() async {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-notch-eval-checks-\(getpid())")
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        // Config: only a private, owned, non-production, loopback run.
        let config = try! NotchEvaluationConfig.parse(configJSON(root: root.path))
        check(config.baseURL.absoluteString == "http://127.0.0.1:18891", "base URL is the run's loopback port")
        for (override, label) in [(["listen_port": 8765], "production backend port"),
                                  (["listen_port": 8000], "engine port"),
                                  (["token": "short"], "short token"), (["nonce": "n"], "short nonce"),
                                  (["run_id": "../x"], "unsafe run id"),
                                  (["backend_pid": Int(getpid())], "the host's own pid"),
                                  (["owned_root": "/nonexistent-wisp-root"], "missing root")] as [([String: Any], String)] {
            check((try? NotchEvaluationConfig.parse(configJSON(override, root: root.path))) == nil, "refuses \(label)")
        }
        let file = root.appendingPathComponent("config.json")
        FileManager.default.createFile(atPath: file.path, contents: configJSON(root: root.path),
                                       attributes: [.posixPermissions: 0o644])
        check((try? NotchEvaluationConfig.load(path: file.path)) == nil, "refuses a readable-by-others config")
        chmod(file.path, 0o600)
        check((try? NotchEvaluationConfig.load(path: file.path)) == config, "loads a private config")
        let link = root.appendingPathComponent("link.json")
        try? FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        check((try? NotchEvaluationConfig.load(path: link.path)) == nil, "refuses a symlinked config")

        // Admission: exact origin only; the token is added.
        for raw in ["http://127.0.0.1:8765/agent", "https://127.0.0.1:18891/agent", "http://localhost:18891/agent",
                    "http://u:p@127.0.0.1:18891/agent", "https://example.com/?q=private", "http://127.0.0.1:18892/v1/models"] {
            if case .success = EvaluationGuard.admit(URLRequest(url: URL(string: raw)!), config: config) {
                check(false, "admits \(raw)")
            } else { check(true, "refuses \(raw)") }
        }
        if case .success(let admitted) = EvaluationGuard.admit(URLRequest(url: URL(string: "http://127.0.0.1:18891/agent")!), config: config) {
            check(admitted.value(forHTTPHeaderField: EvaluationGuard.tokenHeader) == config.token, "adds the run token")
        } else { check(false, "admits the evaluation origin") }

        // Response identity.
        let url = URL(string: "http://127.0.0.1:18891/agent")!
        func response(_ status: Int, _ headers: [String: String], at target: URL = url) -> HTTPURLResponse {
            HTTPURLResponse(url: target, statusCode: status, httpVersion: nil, headerFields: headers)!
        }
        let good = [EvaluationGuard.nonceHeader: config.nonce, EvaluationGuard.pidHeader: "4242"]
        check(EvaluationGuard.accept(response(200, good), config: config) == nil, "accepts this run's backend")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.pidHeader: "4242"]), config: config) == .identity("nonce"), "no nonce")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.nonceHeader: "x" + config.nonce.dropFirst(),
                                                    EvaluationGuard.pidHeader: "4242"]), config: config) == .identity("nonce"), "wrong nonce")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.nonceHeader: config.nonce,
                                                    EvaluationGuard.pidHeader: "1"]), config: config) == .identity("pid"), "wrong pid")
        check(EvaluationGuard.accept(response(302, good), config: config) == .redirect, "a redirect")
        check(EvaluationGuard.accept(response(200, good, at: URL(string: "http://127.0.0.1:8765/agent")!), config: config)
              != nil, "a response from the production port")

        // The URLProtocol end to end, against the in-process stub backend.
        let ledger = NotchEvaluationLedger(directory: root.appendingPathComponent("evidence"))
        EvaluationGuardProtocol.config = config
        EvaluationGuardProtocol.ledger = ledger
        EvaluationGuardProtocol.forwardingProtocols = [StubBackend.self]
        check(URLProtocol.registerClass(EvaluationGuardProtocol.self), "guard installs for URLSession.shared")
        StubBackend.headers = good
        var result = await fetch(url)
        check(result.status == 200 && result.body.contains("SYNTHETIC"), "the evaluation backend's stream arrives")
        check(StubBackend.seen.last?.value(forHTTPHeaderField: EvaluationGuard.tokenHeader) == config.token,
              "the backend receives the run token")
        StubBackend.headers = [EvaluationGuard.nonceHeader: "wrong-nonce-wrong-nonce", EvaluationGuard.pidHeader: "4242"]
        result = await fetch(url)
        check(result.error && result.body.isEmpty, "a backend without this run's nonce gets no data through")
        StubBackend.headers = good
        StubBackend.redirect = true
        result = await fetch(url)
        check(result.error, "a redirect fails the request")
        StubBackend.redirect = false
        let before = StubBackend.seen.count
        for raw in ["http://127.0.0.1:8765/agent", "https://example.com/"] {
            result = await fetch(URL(string: raw)!)
            check(result.error, "\(raw) never leaves the process")
        }
        check(StubBackend.seen.count == before, "refused requests are never forwarded")

        // The real runAgent: under the checks build its base URL is the
        // production port, which the guard refuses before any byte is sent.
        let events = Box()
        await WispClient().runAgent(prompt: "SYNTHETIC check", image: nil) { events.append($0.type) }
        check(events.values == ["error"] && StubBackend.seen.count == before, "runAgent to the installed backend is refused")

        // Native sinks and hash-only receipts.
        let blocked = NotchEvaluationSinks.blocked("send_email", ledger: ledger)
        check(blocked["ok"] as? Bool == false && blocked["status"] as? String == "blocked", "a native sink refuses")
        ledger.input("SYNTHETIC-PRIVATE-PROMPT", dispatch: 7, session: "abc123")
        ledger.visible(answer: "SYNTHETIC-PRIVATE-ANSWER", dispatch: 7)
        let written = (try? String(contentsOf: root.appendingPathComponent("evidence/native-receipts.jsonl"), encoding: .utf8)) ?? ""
        check(written.contains(NotchEvaluationLedger.sha256("SYNTHETIC-PRIVATE-PROMPT")), "the input hash is recorded")
        check(!written.contains("SYNTHETIC-PRIVATE-PROMPT") && !written.contains("SYNTHETIC-PRIVATE-ANSWER"),
              "no prompt or answer text is written")
        let mode = (try? FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("evidence/native-receipts.jsonl").path))?[.posixPermissions] as? NSNumber
        check(mode?.intValue == 0o600, "receipts are private")
        check(ledger.entries.contains { $0["kind"] as? String == "refused" }, "refusals are receipted")

        print("NotchEvaluation: \(passed) checks passed")
    }
}

final class Box: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var values: [String] = []
    func append(_ value: String) { lock.lock(); values.append(value); lock.unlock() }
}
