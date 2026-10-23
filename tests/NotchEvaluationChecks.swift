// Compile with -DWISP_NOTCH_EVALUATION_CHECKS and app/Sources/WispApp/NotchEvaluation.swift,
// WispClient.swift and AgentSend.swift. Exercises the evaluation build's
// config validation, source admission, request admission, response identity,
// redirect refusal, single-shot failure, native sink denial, receipt health
// and hash-only receipts. Forwarded requests are answered by an in-process
// stub backend: nothing reaches a socket.
import Darwin
import Foundation

/// The stub evaluation backend behind the guard. Records what it was sent.
final class StubBackend: URLProtocol {
    nonisolated(unsafe) static var headers: [String: String] = [:]
    nonisolated(unsafe) static var seen: [URLRequest] = []
    nonisolated(unsafe) static var redirect = false
    enum Mode { case stream, closeBeforeHeaders, hang }
    nonisolated(unsafe) static var mode = Mode.stream

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.seen.append(request)
        let url = request.url!
        if Self.mode == .closeBeforeHeaders {
            client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            return
        }
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
        if Self.mode == .hang {
            client?.urlProtocol(self, didLoad: Data("data: {\"type\":\"status\"}\n\n".utf8))
            return  // never finishes; the caller cancels
        }
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
                                     "backend_pid": 4242, "source_roots": [root + "/home"]]
        for (key, value) in overrides { object[key] = value }
        return try! JSONSerialization.data(withJSONObject: object)
    }

    static func refusals(_ ledger: NotchEvaluationLedger, _ reason: String) -> Int {
        ledger.entries.filter { $0["kind"] as? String == "refused" && $0["reason"] as? String == reason }.count
    }

    static func fetch(_ url: URL) async -> (status: Int?, body: String, error: Bool) {
        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            let (data, response) = try await URLSession.shared.data(for: request)
            return ((response as? HTTPURLResponse)?.statusCode, String(decoding: data, as: UTF8.self), false)
        } catch {
            return (nil, "", true)
        }
    }

    static func main() async {
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-notch-eval-checks-\(getpid())")
        try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }
        // The launcher passes canonical paths; so do the checks.
        let root = URL(fileURLWithPath: NotchEvaluationSources.canonical(scratch.path)!, isDirectory: true)
        let home = root.appendingPathComponent("home")
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)

        // Config: only a private, owned, non-production, loopback run.
        let config = try! NotchEvaluationConfig.parse(configJSON(root: root.path))
        check(config.baseURL.absoluteString == "http://127.0.0.1:18891", "base URL is the run's loopback port")
        for (override, label) in [(["listen_port": 8765], "production backend port"),
                                  (["listen_port": 8000], "engine port"),
                                  (["token": "short"], "short token"), (["nonce": "n"], "short nonce"),
                                  (["run_id": "../x"], "unsafe run id"),
                                  (["backend_pid": Int(getpid())], "the host's own pid"),
                                  (["owned_root": "/nonexistent-wisp-root"], "missing root"),
                                  (["owned_root": scratch.path + "/../" + scratch.lastPathComponent], "a non-canonical root"),
                                  // Must refuse, never trap converting to Int32.
                                  (["backend_pid": Int(Int32.max) + 1], "a pid above Int32.max"),
                                  (["backend_pid": 1 << 40], "a pid far above Int32.max"),
                                  (["backend_pid": -5], "a negative pid"),
                                  (["backend_pid": 3.5], "a fractional pid"),
                                  (["backend_pid": true], "a boolean pid"),
                                  (["backend_pid": "4242"], "a string pid"),
                                  (["source_roots": ["/etc"]], "a source root outside the owned root"),
                                  (["source_roots": [root.path + "/missing"]], "a missing source root")]
                                  as [([String: Any], String)] {
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
        check(config.sourceRoots == [home.path] && config.backendPID == 4242, "source roots and pid parse")

        // Attachments: only regular files inside a source root, links resolved.
        let inside = home.appendingPathComponent("photo.png")
        FileManager.default.createFile(atPath: inside.path, contents: Data("SYNTHETIC".utf8))
        let outside = root.appendingPathComponent("outside.png")
        FileManager.default.createFile(atPath: outside.path, contents: Data("SYNTHETIC".utf8))
        let escape = home.appendingPathComponent("escape.png")
        try? FileManager.default.createSymbolicLink(at: escape, withDestinationURL: outside)
        check(NotchEvaluationSources.admit(inside, config: config)?.path == inside.path, "admits a source-root file")
        check(NotchEvaluationSources.admit(outside, config: config) == nil, "refuses a file outside the source roots")
        check(NotchEvaluationSources.admit(escape, config: config) == nil, "refuses a link that escapes the source roots")
        check(NotchEvaluationSources.admit(home, config: config) == nil, "refuses a directory")
        check(NotchEvaluationSources.admit(URL(fileURLWithPath: "/etc/hosts"), config: config) == nil, "refuses a system file")
        check(NotchEvaluationSources.admit(URL(string: "https://example.com/a.png")!, config: config) == nil,
              "refuses a remote URL")

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
        // A connection that closes before any response ends exactly once, promptly.
        var incomplete = refusals(ledger, "incomplete")
        StubBackend.mode = .closeBeforeHeaders
        var began = Date()
        result = await fetch(url)
        check(result.error && result.body.isEmpty && Date().timeIntervalSince(began) < 4,
              "a close before headers fails promptly with no body")
        check(refusals(ledger, "incomplete") == incomplete + 1, "a close before headers ends exactly once")
        // A cancelled request stops silently: no refusal and no callback after it.
        StubBackend.mode = .hang
        incomplete = refusals(ledger, "incomplete")
        let refusedBefore = ledger.entries.filter { $0["kind"] as? String == "refused" }.count
        began = Date()
        let pending = Task { await fetch(url) }
        try? await Task.sleep(nanoseconds: 200_000_000)
        pending.cancel()
        result = await pending.value
        check(result.error && Date().timeIntervalSince(began) < 4, "a cancelled stream ends promptly")
        try? await Task.sleep(nanoseconds: 200_000_000)
        check(ledger.entries.filter { $0["kind"] as? String == "refused" }.count == refusedBefore,
              "a cancellation is not reported as a second failure")
        StubBackend.mode = .stream
        let before = StubBackend.seen.count
        for raw in ["http://127.0.0.1:8765/agent", "https://example.com/?q=PRIVATE-URL-CANARY"] {
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
        check(ledger.input("SYNTHETIC-PRIVATE-PROMPT", dispatch: 7, session: "abc123"), "an input receipt persists")
        ledger.event(type: "tool_call", payload: ["id": "PRIVATE-EVENT-CANARY value", "name": "view_emails",
                                                  "trace_id": "trace_1"], dispatch: 7, current: true)

        // Finalized visible receipts: one per dispatch, the exact displayed
        // text, captured at the terminal event. These drive the ledger the way
        // OverlayModel.handle does (flush, receipt, then clear and dequeue);
        // the real model path is covered by native UI qualification.
        func receipts(_ kind: String, _ dispatch: UInt64) -> [[String: Any]] {
            ledger.entries.filter { $0["kind"] as? String == kind && ($0["dispatch"] as? UInt64) == dispatch }
        }
        var answer = "SYNTHETIC-PRIVATE-"
        let buffered = "ANSWER"
        answer += buffered  // done flushes the coalesced delta first
        check(ledger.terminal("done", visible: answer, dispatch: 7, current: true, session: "abc123", trace: "trace_1"),
              "the finalized answer is receipted")
        check(receipts("visible", 7).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("SYNTHETIC-PRIVATE-ANSWER"),
              "the receipt hashes the complete flushed answer, not an empty buffer")
        check(!ledger.terminal("done", visible: "", dispatch: 7, current: true, session: "", trace: nil)
              && receipts("visible", 7).count == 1, "a second terminal cannot relabel a dispatch")
        check(!ledger.terminal("done", visible: "x", dispatch: 99, current: true, session: "", trace: nil),
              "a dispatch with no input receipt claims no visible result")
        // Queued: the second dispatch starts after the first's receipt.
        ledger.input("SYNTHETIC second", dispatch: 8, session: "abc123")
        ledger.terminal("error", visible: "Something went wrong.", dispatch: 8, current: true, session: "abc123", trace: nil)
        ledger.input("SYNTHETIC queued", dispatch: 9, session: "abc123")
        ledger.terminal("done", visible: "SYNTHETIC third", dispatch: 9, current: true, session: "abc123", trace: nil)
        check(receipts("visible", 8).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("Something went wrong.")
              && receipts("visible", 8).first?["outcome"] as? String == "error", "an error receipt matches the error turn")
        check(receipts("visible", 9).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("SYNTHETIC third"),
              "a queued turn's answer is bound to its own dispatch")
        // Stale: a cancelled dispatch's late terminal is telemetry only.
        ledger.input("SYNTHETIC cancelled", dispatch: 10, session: "abc123")
        ledger.terminal("done", visible: "", dispatch: 10, current: false, session: "abc123", trace: nil)
        check(receipts("visible", 10).isEmpty && receipts("late_terminal", 10).count == 1
              && receipts("late_terminal", 10).first?["answer_sha256"] == nil, "a stale terminal labels no visible result")
        check(ledger.terminal("done", visible: "", dispatch: 10, current: true, session: "", trace: nil)
              && receipts("visible", 10).first?["rendered"] as? Bool == false, "an empty reply is receipted as not rendered")

        let written = (try? String(contentsOf: root.appendingPathComponent("evidence/native-receipts.jsonl"), encoding: .utf8)) ?? ""
        check(written.contains(NotchEvaluationLedger.sha256("SYNTHETIC-PRIVATE-PROMPT")), "the input hash is recorded")
        check(!written.contains("SYNTHETIC-PRIVATE-PROMPT") && !written.contains("SYNTHETIC-PRIVATE-ANSWER"),
              "no prompt or answer text is written")
        check(!written.contains("PRIVATE-EVENT-CANARY") && written.contains("id_sha256")
              && written.contains("\"trace_id\":\"trace_1\""), "untrusted event ids are hashed; safe ids kept")
        check(!written.contains("PRIVATE-URL-CANARY") && written.contains("url_sha256"), "refused URLs are hashed")
        let mode = (try? FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("evidence/native-receipts.jsonl").path))?[.posixPermissions] as? NSNumber
        check(mode?.intValue == 0o600, "receipts are private")
        check(ledger.entries.contains { $0["kind"] as? String == "refused" }, "refusals are receipted")
        check(ledger.healthy, "the healthy ledger stayed healthy")

        // Receipt destinations: only a private regular file in a private directory.
        let bad1 = root.appendingPathComponent("bad1")
        try? FileManager.default.createDirectory(at: bad1.appendingPathComponent("native-receipts.jsonl"),
                                                 withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        chmod(bad1.path, 0o700)
        check(!NotchEvaluationLedger(directory: bad1).healthy, "refuses a receipt path that is a directory")
        let bad2 = root.appendingPathComponent("bad2")
        try? FileManager.default.createDirectory(at: bad2, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? FileManager.default.createSymbolicLink(at: bad2.appendingPathComponent("native-receipts.jsonl"),
                                                   withDestinationURL: outside)
        check(!NotchEvaluationLedger(directory: bad2).healthy, "refuses a linked receipt file")
        let bad3 = root.appendingPathComponent("bad3")
        try? FileManager.default.createDirectory(at: bad3, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        FileManager.default.createFile(atPath: bad3.appendingPathComponent("native-receipts.jsonl").path, contents: nil,
                                       attributes: [.posixPermissions: 0o644])
        check(!NotchEvaluationLedger(directory: bad3).healthy, "refuses a receipt file others can read")
        let bad4 = root.appendingPathComponent("bad4")
        try? FileManager.default.createDirectory(at: bad4, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        chmod(bad4.path, 0o755)
        check(!NotchEvaluationLedger(directory: bad4).healthy, "refuses a receipt directory others can read")

        // An unhealthy ledger stops everything: no request is forwarded and no turn input is accepted.
        let broken = NotchEvaluationLedger(directory: bad1)
        EvaluationGuardProtocol.ledger = broken
        let forwarded = StubBackend.seen.count
        result = await fetch(url)
        check(result.error && StubBackend.seen.count == forwarded, "no request leaves while evidence is unavailable")
        check(!broken.input("SYNTHETIC", dispatch: 1, session: ""), "no turn starts while evidence is unavailable")
        EvaluationGuardProtocol.ledger = ledger

        // A write that fails is sticky: the file size limit makes write(2) fail (EFBIG).
        let fsz = root.appendingPathComponent("fsz")
        let limited = NotchEvaluationLedger(directory: fsz)
        check(limited.record("probe", [:]), "a fresh ledger writes")
        signal(SIGXFSZ, SIG_IGN)
        var saved = rlimit()
        getrlimit(RLIMIT_FSIZE, &saved)
        var tiny = saved
        tiny.rlim_cur = 16
        setrlimit(RLIMIT_FSIZE, &tiny)
        let wrote = limited.record("probe", ["pad": String(repeating: "x", count: 64)])
        setrlimit(RLIMIT_FSIZE, &saved)
        check(!wrote && !limited.healthy, "a failed receipt write makes the ledger unhealthy")
        check(!limited.record("probe", [:]) && !limited.healthy, "the failure is sticky")

        // Without a file, exhausting in-memory retention is a failure, not silent loss.
        let memory = NotchEvaluationLedger(directory: nil)
        for i in 0..<NotchEvaluationLedger.maxMemoryEntries { memory.record("fill", ["i": i]) }
        check(memory.healthy && !memory.record("over", [:]) && !memory.healthy, "exhausted retention is a sticky failure")

        print("NotchEvaluation: \(passed) checks passed")
    }
}

final class Box: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var values: [String] = []
    func append(_ value: String) { lock.lock(); values.append(value); lock.unlock() }
}
