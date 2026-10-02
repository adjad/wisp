import Darwin
import Foundation

// H-16: the app must only act on, and only send data to, Wisp's OWN backend. `@main` like
// the other native checks; compiled together with PortGuard.swift and BackendTrust.swift.
@main
enum BackendTrustChecks {
    static var checks = 0
    static func check(_ condition: Bool, _ message: String) {
        guard condition else { fatalError(message) }
        checks += 1
    }

    typealias L = PortGuard.Listener
    static let bundle = "/Applications/Wisp.app/Contents/Resources/backend/"
    static let ours = L(pid: 10, path: bundle + ".venv/bin/python3.13")
    static let stranger = L(pid: 20, path: "/usr/local/bin/node")
    static func ident(_ service: String = "wisp-backend", _ mode: String = "production", _ pid: Int32 = 10)
        -> BackendTrust.IdentityResult { .answered(.init(service: service, mode: mode, pid: pid)) }
    static func decide(_ listeners: [L]?, _ identity: BackendTrust.IdentityResult) -> BackendTrust.Verdict {
        BackendTrust.decide(listeners: listeners, ownedPrefixes: [bundle], identity: identity)
    }

    static func main() async {
        pureChecks()
        parseChecks()
        wordingChecks()
        gateChecks()
        await freshGateChecks()
        if !CommandLine.arguments.contains("--pure") { await liveChecks() }
        print("\(checks) backend trust checks passed")
    }

    // MARK: pure policy

    static func pureChecks() {
        check(decide(nil, ident()) == .refused(.cannotInspect), "an uninspectable port must fail closed")
        check(decide([], ident()) == .unreachable, "nothing listening is unreachable, not trusted")
        check(decide([ours], ident()) == .trusted(pids: [10]), "the real backend, production, matching pid")
        check(decide([ours], .failed) == .unreachable, "own listener that did not answer is not yet trusted")
        check(decide([ours], .missing) == .trusted(pids: [10]), "a backend older than /identity is trusted on the kernel proof")

        // A stranger is refused however convincingly it identifies itself.
        check(decide([stranger], ident("wisp-backend", "production", 20)) == .refused(.foreignListener([stranger])),
              "a stranger claiming to be Wisp must be refused")
        check(decide([stranger], .missing) == .refused(.foreignListener([stranger])), "a stranger with no identity is refused")
        check(decide([ours, stranger], ident()) == .refused(.foreignListener([stranger])),
              "a mixed set is refused and names only the stranger")

        // Wisp's own code is still refused when it is not the real world.
        check(decide([ours], ident("wisp-backend", "sandbox")) == .refused(.wrongMode("sandbox")), "a sandbox backend is refused")
        check(decide([ours], ident("wisp-backend", "")) == .refused(.wrongMode("")), "an empty mode is refused")
        check(decide([ours], ident("wisp-backend", "Production")) == .refused(.wrongMode("Production")), "mode is exact")
        check(decide([ours], ident("something-else")) == .refused(.notWispBackend), "the wrong service is refused")
        check(decide([ours], ident("wisp-backend", "production", 99)) == .refused(.pidMismatch),
              "the answering pid must be one of the listeners")
        check(decide([ours, L(pid: 11, path: bundle + "x")], ident("wisp-backend", "production", 11)) == .trusted(pids: [10, 11]),
              "several owned listeners are fine when the pid matches one")

        // Look-alike paths are not ownership (same rules as PortGuard).
        for path in [bundle.replacingOccurrences(of: "backend/", with: "backend-evil/") + "python",
                     "Applications/Wisp.app/Contents/Resources/backend/python", ""] {
            check(decide([L(pid: 10, path: path)], ident()) == .refused(.foreignListener([L(pid: 10, path: path)])),
                  "look-alike path trusted: \(path)")
        }
        check(BackendTrust.decide(listeners: [ours], ownedPrefixes: [], identity: ident()) == .refused(.foreignListener([ours])),
              "no owned prefixes must trust nothing")
    }

    static func parseChecks() {
        func parse(_ text: String) -> BackendTrust.Identity? { BackendTrust.parseIdentity(Data(text.utf8)) }
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123}"#)
              == .init(service: "wisp-backend", mode: "production", pid: 123), "valid identity")
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123,"extra":1}"#) != nil, "extra keys are tolerated")
        for bad in [#"{"service":"wisp-backend","mode":"production","pid":true}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":"123"}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":-1}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":0}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":99999999999}"#,
                    #"{"service":"wisp-backend","pid":1}"#, #"{"mode":"production","pid":1}"#,
                    #"{"service":1,"mode":"production","pid":1}"#, #"[1,2]"#, "null", "", "not json"] {
            check(parse(bad) == nil, "malformed identity accepted: \(bad)")
        }
    }

    static func wordingChecks() {
        let text = BackendTrust.describe(.refused(.foreignListener([L(pid: 4242, path: "/usr/local/bin/node")])))
        check(text.contains("node (pid 4242)") && text.contains("8765") && text.contains("Mail, Messages or Calendar"),
              "the refusal names the program, the port and what is protected")
        check(!text.contains("foreignListener") && !text.contains("refused("), "no raw enum text in what the person reads")
        check(BackendTrust.describe(.refused(.wrongMode("sandbox"))).contains("sandbox copy of Wisp"), "sandbox wording")
        check(BackendTrust.describe(.refused(.cannotInspect)).contains("couldn't check"), "cannot-inspect wording")
    }

    // MARK: the protocol's scope

    static func gateChecks() {
        BackendTrustProtocol.enforcedPort = 8765
        func can(_ url: String, bypass: Bool = false) -> Bool {
            let marked = NSMutableURLRequest(url: URL(string: url)!)
            if bypass { URLProtocol.setProperty(true, forKey: BackendTrustProtocol.bypassKey, in: marked) }
            return BackendTrustProtocol.canInit(with: marked as URLRequest)
        }
        check(can("http://127.0.0.1:8765/agent"), "the backend origin is gated")
        check(can("http://127.0.0.1:8765/assistant/events"), "the event stream is gated")
        check(!can("http://127.0.0.1:8775/agent"), "another local port is not touched")
        check(!can("http://127.0.0.1:8000/v1/models"), "the engine port is not touched")
        check(!can("https://127.0.0.1:8765/x"), "only plain http to the backend")
        check(!can("http://localhost:8765/x"), "the app only ever uses 127.0.0.1")
        check(!can("https://example.com/x"), "the internet is not touched")
        check(!can("http://127.0.0.1:8765/identity", bypass: true), "the verification request is not gated by itself")
    }

    // MARK: live: the real protocol against real local servers

    final class Host: @unchecked Sendable {
        let lock = NSLock()
        var listeners: [L]? = [ours]
        var birth: BackendTrustGate.Incarnation? = .init(seconds: 1, microseconds: 0)
        var answer = ident()
        var inspections = 0
        var identities = 0
        var changeDuringIdentity: (() -> Void)?
        func inspect() -> [L]? {
            lock.lock(); defer { lock.unlock() }
            inspections += 1
            return listeners
        }
        func incarnation(_ pid: Int32) -> BackendTrustGate.Incarnation? {
            lock.lock(); defer { lock.unlock() }
            return birth
        }
        func identity() -> BackendTrust.IdentityResult {
            lock.lock(); defer { lock.unlock() }
            identities += 1
            changeDuringIdentity?()
            return answer
        }
        func verify(_ port: Int, _ prefixes: [String]) async -> BackendTrust.Verdict {
            await BackendTrustGate.verify(port: port, ownedPrefixes: prefixes,
                                          inspect: inspect, processIncarnation: incarnation,
                                          identity: { self.identity() })
        }
    }

    static func freshGateChecks() async {
        let previous = BackendTrustConfiguration.current
        BackendTrustConfiguration.set(ownedPrefixes: [bundle])
        defer { BackendTrustConfiguration.set(ownedPrefixes: previous.ownedPrefixes, port: previous.port) }
        let host = Host()
        let gate = BackendTrustGate(verification: { await host.verify($0, $1) })
        check(await gate.verdict() == .trusted(pids: [10]), "healthy fresh gate")
        host.listeners = [stranger]
        check(!(await gate.verdict()).isTrusted, "a foreign replacement cannot reuse a timed verdict")
        check(host.inspections == 3 && host.identities == 1, "foreign request inspects anew without contacting it")
        host.listeners = nil
        check(await gate.verdict() == .refused(.cannotInspect), "unreadable replacement fails closed")
        host.listeners = [ours]
        host.answer = ident("wisp-backend", "sandbox")
        check(!(await gate.verdict()).isTrusted, "fresh mode identity is required for every request")
        host.answer = ident()
        host.changeDuringIdentity = { host.listeners = [stranger] }
        check(!(await gate.verdict()).isTrusted, "listener replacement during identity cannot receive a private request")
        host.listeners = [ours]
        host.changeDuringIdentity = { host.birth = .init(seconds: 2, microseconds: 0) }
        check(await gate.verdict() == .refused(.cannotInspect), "same pid/path with new kernel start time is refused")
        host.changeDuringIdentity = { host.birth = nil }
        check(await gate.verdict() == .refused(.cannotInspect), "unreadable incarnation after identity fails closed")
        host.changeDuringIdentity = nil
        host.birth = .init(seconds: 3, microseconds: 0)
        host.answer = .missing
        check((await gate.verdict()).isTrusted, "legacy 404 still requires stable owned process")
        host.answer = ident()
        let before = host.identities
        let barrier = PairBarrier()
        let concurrentGate = BackendTrustGate(verification: { port, prefixes in
            await barrier.arrive()
            return await host.verify(port, prefixes)
        })
        async let first = concurrentGate.verdict()
        async let second = concurrentGate.verdict()
        let results = await [first, second]
        check(results.allSatisfy(\.isTrusted) && host.identities == before + 2,
              "concurrent requests each verify; no in-flight verdict reuse")
        host.changeDuringIdentity = { BackendTrustConfiguration.set(ownedPrefixes: []) }
        check(await gate.verdict() == .refused(.cannotInspect), "configuration changes during verification fail closed")
    }

    actor PairBarrier {
        private var waiting: CheckedContinuation<Void, Never>?
        func arrive() async {
            if let waiting {
                self.waiting = nil
                waiting.resume()
            } else {
                await withCheckedContinuation { waiting = $0 }
            }
        }
    }

    static let server = """
    import sys, os, json, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    mode, identity = sys.argv[1], sys.argv[2] == "yes"
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def reply(self, code, body, ctype="application/json"):
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            print("REQ GET", self.path, flush=True)
            if self.path == "/identity":
                if not identity: return self.reply(404, b"{}")
                return self.reply(200, json.dumps({"service": "wisp-backend", "mode": mode, "pid": os.getpid()}).encode())
            if self.path == "/sse":
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close"); self.end_headers()
                for n in range(4):
                    self.wfile.write(("data: event %d\\n\\n" % n).encode()); self.wfile.flush(); time.sleep(0.4)
                self.close_connection = True; return
            self.reply(200, b"ok", "text/plain")
        def do_POST(self):
            print("REQ POST", self.path, flush=True)
            n = int(self.headers.get("Content-Length", "0"))
            self.reply(200, self.rfile.read(n))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    print("PORT", srv.server_address[1], flush=True)
    srv.serve_forever()
    """

    final class Server {
        let process = Process()
        let port: Int
        let pid: Int32
        private let lock = NSLock()
        private var lines: [String] = []
        init?(mode: String, identity: Bool) {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-c", BackendTrustChecks.server, mode, identity ? "yes" : "no"]
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            do { try process.run() } catch { return nil }
            var found: Int?
            var buffered = ""
            let deadline = Date().addingTimeInterval(8)
            while found == nil, Date() < deadline {
                let data = pipe.fileHandleForReading.availableData
                if data.isEmpty { usleep(50_000); continue }
                buffered += String(decoding: data, as: UTF8.self)
                for line in buffered.split(separator: "\n") where line.hasPrefix("PORT ") { found = Int(line.dropFirst(5)) }
            }
            guard let found else { process.terminate(); return nil }
            port = found
            pid = process.processIdentifier
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let text = String(decoding: handle.availableData, as: UTF8.self)
                self?.lock.lock(); self?.lines += text.split(separator: "\n").map(String.init); self?.lock.unlock()
            }
        }
        var requests: [String] { lock.lock(); defer { lock.unlock() }; return lines.filter { $0.hasPrefix("REQ ") } }
        func stop() { process.terminate(); process.waitUntilExit() }
    }

    static func get(_ url: String) async -> (status: Int?, body: String, error: URLError?) {
        do {
            var request = URLRequest(url: URL(string: url)!)
            request.timeoutInterval = 8
            let (data, response) = try await URLSession.shared.data(for: request)
            return ((response as? HTTPURLResponse)?.statusCode, String(decoding: data, as: UTF8.self), nil)
        } catch { return (nil, "", error as? URLError) }
    }

    static func liveChecks() async {
        guard let good = Server(mode: "production", identity: true) else {
            print("note: a local server cannot be started in this sandbox; live protocol checks skipped")
            return
        }
        defer { good.stop() }
        guard let sandboxed = Server(mode: "sandbox", identity: true),
              let legacy = Server(mode: "production", identity: false),
              let other = Server(mode: "production", identity: true) else { return }
        defer { sandboxed.stop(); legacy.stop(); other.stop() }
        let binary = PortGuard.executablePath(pid: good.pid) ?? ""
        let owned = [((binary as NSString).deletingLastPathComponent) + "/"]
        check(binary.hasPrefix("/"), "could not read the test server's executable path")
        URLProtocol.registerClass(BackendTrustProtocol.self)
        var refusals: [String] = []
        let observer = NotificationCenter.default.addObserver(forName: .wispBackendRefused, object: nil, queue: nil) {
            refusals.append($0.userInfo?["message"] as? String ?? "")
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        func enforce(_ server: Server, owning prefixes: [String]) {
            BackendTrustProtocol.enforcedPort = server.port
            BackendTrustConfiguration.set(ownedPrefixes: prefixes, port: server.port)
        }

        // 1. Trusted: everything passes through unharmed.
        enforce(good, owning: owned)
        let hello = await get("http://127.0.0.1:\(good.port)/hello")
        check(hello.status == 200 && hello.body == "ok", "a trusted backend must work normally: \(String(describing: hello.error))")
        var post = URLRequest(url: URL(string: "http://127.0.0.1:\(good.port)/echo")!)
        post.httpMethod = "POST"
        let payload = #"{"events":[{"title":"Lunch","when":1790000000}],"note":"héllo ✓"}"#
        post.httpBody = Data(payload.utf8)
        post.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let echoed = try? await URLSession.shared.data(for: post)
        check(echoed.map { String(decoding: $0.0, as: UTF8.self) } == payload, "a POST body must arrive intact through the gate")
        let started = Date()
        var arrivals: [TimeInterval] = []
        if let (bytes, response) = try? await URLSession.shared.bytes(from: URL(string: "http://127.0.0.1:\(good.port)/sse")!) {
            check((response as? HTTPURLResponse)?.statusCode == 200, "SSE status")
            do { for try await line in bytes.lines where line.hasPrefix("data:") { arrivals.append(Date().timeIntervalSince(started)) } } catch {}
        }
        check(arrivals.count == 4, "all 4 SSE events must arrive: \(arrivals)")
        check(arrivals[0] < 0.9 && arrivals[3] - arrivals[0] > 0.9, "SSE must stream as it arrives, not buffer: \(arrivals)")

        // 2. A stranger (path is outside the owned set) gets nothing at all, not even a question.
        let before = good.requests.count
        enforce(good, owning: ["/nonexistent/"])
        let blocked = await get("http://127.0.0.1:\(good.port)/hello")
        check(blocked.status == nil && blocked.error != nil, "a stranger must be refused")
        check(blocked.error?.localizedDescription.contains("isn't Wisp's own service") == true,
              "the failure explains itself: \(blocked.error?.localizedDescription ?? "")")
        usleep(300_000)
        check(good.requests.count == before, "NOTHING may be sent to a stranger, not even /identity: \(good.requests)")
        check(refusals.count == 1 && refusals[0].contains("Quit it and reopen Wisp"), "the person is told, once: \(refusals)")
        _ = await get("http://127.0.0.1:\(good.port)/hello")
        check(refusals.count == 1, "a repeated refusal must not nag")

        // 3. Wisp's own code in a sandbox world: asked who it is, refused, no request sent.
        enforce(sandboxed, owning: owned)
        let sandbox = await get("http://127.0.0.1:\(sandboxed.port)/hello")
        check(sandbox.status == nil, "a sandbox backend must be refused")
        usleep(300_000)
        check(sandboxed.requests == ["REQ GET /identity"], "only the identity question is sent to it: \(sandboxed.requests)")
        check(refusals.last?.contains("sandbox copy of Wisp") == true, "sandbox wording shown")

        // 4. A backend older than /identity (404) is trusted on the kernel proof.
        enforce(legacy, owning: owned)
        let old = await get("http://127.0.0.1:\(legacy.port)/hello")
        check(old.status == 200 && old.body == "ok", "a legacy backend must keep working")

        // 5. Nothing listening: fails promptly, nothing hangs.
        let dead = Server(mode: "production", identity: true)!
        let deadPort = dead.port
        dead.stop()
        enforce(good, owning: owned)
        BackendTrustProtocol.enforcedPort = deadPort
        BackendTrustConfiguration.set(ownedPrefixes: owned, port: deadPort)
        let t0 = Date()
        let none = await get("http://127.0.0.1:\(deadPort)/hello")
        check(none.status == nil && Date().timeIntervalSince(t0) < 6, "an absent backend fails promptly")

        // 6. Only the enforced port is gated: another local server passes, even while refusing a stranger.
        enforce(good, owning: ["/nonexistent/"])
        let free = await get("http://127.0.0.1:\(other.port)/hello")
        check(free.status == 200, "a different port must be untouched by the gate")
    }
}
