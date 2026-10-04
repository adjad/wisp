import Darwin
import Foundation

// H-16: the app must only act on, and only send data to, Wisp's OWN backend. `@main` like
// the other native checks; compiled together with PortGuard.swift, BackendOwnership.swift
// and BackendTrust.swift.
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
    /// Another program on Wisp's own interpreter: same executable path, different process.
    static let bystander = L(pid: 30, path: ours.path)
    static let birth = BackendOwnership.StartTime(seconds: 100, microseconds: 7)
    static let launched = BackendOwnership.Receipt(pid: 10, start: birth, executablePath: ours.path,
                                                   backendRoot: String(bundle.dropLast()), nonce: "nonce-1")
    static func ident(_ service: String = "wisp-backend", _ mode: String = "production", _ pid: Int32 = 10,
                      nonce: String? = "nonce-1") -> BackendTrust.IdentityResult {
        .answered(.init(service: service, mode: mode, pid: pid, launchNonce: nonce))
    }
    /// The kernel's view: every listed pid exists with the listed path and `birth`.
    static func kernel(_ pid: Int32) -> BackendOwnership.ProcessFacts? {
        let paths: [Int32: String] = [10: ours.path, 11: bundle + "x", 20: stranger.path, 30: bystander.path]
        return paths[pid].map { .init(pid: pid, start: birth, executablePath: $0) }
    }
    static func decide(_ listeners: [L]?, _ identity: BackendTrust.IdentityResult,
                       receipt: BackendOwnership.ReceiptState = .present(launched),
                       facts: (Int32) -> BackendOwnership.ProcessFacts? = kernel) -> BackendTrust.Verdict {
        BackendTrust.decide(listeners: listeners, identity: identity, receipt: receipt, facts: facts)
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
        // A backend this app spawned always serves /identity, so a 404 there is never evidence of anything.
        check(decide([ours], .missing) == .refused(.notWispBackend), "a 404 /identity must not be trusted, even from the launched process")

        // A stranger is refused however convincingly it identifies itself: only the process this
        // app launched (the in-memory receipt) is ever trusted.
        check(decide([stranger], ident("wisp-backend", "production", 20)) == .refused(.unproven([stranger])),
              "a stranger claiming to be Wisp must be refused")
        check(decide([stranger], .missing) == .refused(.unproven([stranger])), "a stranger with no identity is refused")
        check(decide([ours, stranger], ident()) == .refused(.unproven([stranger])),
              "a mixed set is refused and names only the stranger")
        // Where a program runs from is no evidence either way: a bystander inside Wisp's own
        // backend folder, echoing the launch nonce, is still not the launched process.
        check(decide([L(pid: 11, path: bundle + "x")], ident("wisp-backend", "production", 11))
              == .refused(.unproven([L(pid: 11, path: bundle + "x")])),
              "a program in Wisp's folder with the right nonce is not the launched process")

        // Wisp's own code is still refused when it is not the real world.
        check(decide([ours], ident("wisp-backend", "sandbox")) == .refused(.wrongMode("sandbox")), "a sandbox backend is refused")
        check(decide([ours], ident("wisp-backend", "")) == .refused(.wrongMode("")), "an empty mode is refused")
        check(decide([ours], ident("wisp-backend", "Production")) == .refused(.wrongMode("Production")), "mode is exact")
        check(decide([ours], ident("something-else")) == .refused(.notWispBackend), "the wrong service is refused")
        check(decide([ours], ident("wisp-backend", "production", 99)) == .refused(.pidMismatch),
              "the answering pid must be one of the listeners")
        // Every listener must be the launched process: a second one is not proven Wisp's.
        check(decide([ours, L(pid: 11, path: bundle + "x")], ident("wisp-backend", "production", 11))
              == .refused(.unproven([L(pid: 11, path: bundle + "x")])), "a second listener rides on the receipt")

        // A listing that disagrees with the kernel about the executable (a look-alike, a relative
        // path, an empty one) is not the process the kernel describes: fail closed.
        for path in [bundle.replacingOccurrences(of: "backend/", with: "backend-evil/") + "python",
                     "Applications/Wisp.app/Contents/Resources/backend/python", ""] {
            check(decide([L(pid: 10, path: path)], ident()) == .refused(.cannotInspect),
                  "a listing that disagrees with the kernel was trusted: \(path)")
        }

        // (i) Another program on Wisp's own interpreter is refused, whatever it answers.
        for identity in [ident("wisp-backend", "production", 30), BackendTrust.IdentityResult.missing, .failed] {
            check(decide([bystander], identity) == .refused(.unproven([bystander])),
                  "a bystander on the owned interpreter trusted: \(identity)")
        }
        // (iii) The receipt's pid with a different kernel start time is a reused pid.
        let reused: (Int32) -> BackendOwnership.ProcessFacts? = {
            .init(pid: $0, start: .init(seconds: 100, microseconds: 8), executablePath: ours.path)
        }
        check(decide([ours], ident(), facts: reused) == .refused(.unproven([ours])), "pid reuse trusted")
        check(decide([ours], .missing, facts: reused) == .refused(.unproven([ours])), "pid reuse trusted on a 404")
        // (iv) A stale receipt plus a bystander on the port: refused, no fallback.
        let stale = BackendOwnership.Receipt(pid: 777, start: birth, executablePath: ours.path,
                                             backendRoot: launched.backendRoot, nonce: "nonce-1")
        check(decide([ours], .missing, receipt: .present(stale)) == .refused(.unproven([ours])), "stale receipt trusted")
        check(decide([ours], ident(), receipt: .present(stale)) == .refused(.unproven([ours])), "stale receipt trusted (answered)")
        // No receipt at all (a backend from a build that wrote none): fail closed.
        check(decide([ours], .missing, receipt: .none) == .refused(.unproven([ours])), "no receipt + 404 trusted")
        check(decide([ours], ident(), receipt: .none) == .refused(.unproven([ours])), "no receipt + identity trusted")
        // (vi) The receipt's process must echo the receipt's nonce when it answers.
        check(decide([ours], ident(nonce: "nonce-2")) == .refused(.launchMismatch), "a different nonce trusted")
        check(decide([ours], ident(nonce: nil)) == .refused(.launchMismatch), "a missing nonce trusted")
        // (vii) A listener that vanished or cannot be read is not trusted.
        check(decide([ours], .missing, facts: { _ in nil }) == .refused(.cannotInspect), "uninspectable 404 trusted")
        // Ownership failures are refused even when identity failed (no "not yet" for them).
        check(decide([ours], .failed, receipt: .none) == .refused(.unproven([ours])), "unproven + failed identity not refused")
    }

    static func parseChecks() {
        func parse(_ text: String) -> BackendTrust.Identity? { BackendTrust.parseIdentity(Data(text.utf8)) }
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123}"#)
              == .init(service: "wisp-backend", mode: "production", pid: 123), "valid identity")
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123,"extra":1}"#) != nil, "extra keys are tolerated")
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123,"launch_nonce":"abc"}"#)
              == .init(service: "wisp-backend", mode: "production", pid: 123, launchNonce: "abc"), "nonce parsed")
        check(parse(#"{"service":"wisp-backend","mode":"production","pid":123}"#)?.launchNonce == nil, "nonce optional")
        for bad in [#"{"service":"wisp-backend","mode":"production","pid":true}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":"123"}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":-1}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":0}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":99999999999}"#,
                    #"{"service":"wisp-backend","pid":1}"#, #"{"mode":"production","pid":1}"#,
                    #"{"service":1,"mode":"production","pid":1}"#, #"[1,2]"#, "null", "", "not json",
                    #"{"service":"wisp-backend","mode":"production","pid":1,"launch_nonce":5}"#,
                    #"{"service":"wisp-backend","mode":"production","pid":1,"launch_nonce":null}"#] {
            check(parse(bad) == nil, "malformed identity accepted: \(bad)")
        }
    }

    static func wordingChecks() {
        let text = BackendTrust.describe(.refused(.unproven([L(pid: 4242, path: "/usr/local/bin/node")])))
        check(text.contains("node (pid 4242)") && text.contains("8765") && text.contains("Mail, Messages or Calendar"),
              "the refusal names the program, the port and what is protected")
        check(!text.contains("unproven") && !text.contains("refused("), "no raw enum text in what the person reads")
        check(BackendTrust.describe(.refused(.wrongMode("sandbox"))).contains("sandbox copy of Wisp"), "sandbox wording")
        check(BackendTrust.describe(.refused(.cannotInspect)).contains("couldn't check"), "cannot-inspect wording")
        let unproven = BackendTrust.describe(.refused(.unproven([L(pid: 4343, path: bundle + ".venv/bin/python3.13")])))
        check(unproven.contains("python3.13 (pid 4343)") && unproven.contains("didn't start")
              && unproven.contains("earlier copy of Wisp") && unproven.contains("Mail, Messages or Calendar")
              && !unproven.contains("unproven"), "unproven wording: \(unproven)")
        check(BackendTrust.describe(.refused(.launchMismatch)).contains("wasn't started by this copy of Wisp"),
              "launch mismatch wording")
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
        var receipt = BackendOwnership.ReceiptState.present(.init(pid: 10, start: .init(seconds: 1, microseconds: 0),
                                                                 executablePath: ours.path, backendRoot: "/r", nonce: "nonce-1"))
        var answer = ident()
        var inspections = 0
        var identities = 0
        var changeDuringIdentity: (() -> Void)?
        func inspect() -> [L]? {
            lock.lock(); defer { lock.unlock() }
            inspections += 1
            return listeners
        }
        /// The kernel: a listed pid exists with its listed path and the current `birth`.
        func facts(_ pid: Int32) -> BackendOwnership.ProcessFacts? {
            lock.lock(); defer { lock.unlock() }
            guard let birth, let path = listeners?.first(where: { $0.pid == pid })?.path else { return nil }
            return .init(pid: pid, start: birth, executablePath: path)
        }
        func currentReceipt() -> BackendOwnership.ReceiptState {
            lock.lock(); defer { lock.unlock() }
            return receipt
        }
        func identity() -> BackendTrust.IdentityResult {
            lock.lock(); defer { lock.unlock() }
            identities += 1
            changeDuringIdentity?()
            return answer
        }
        func verify(_ port: Int) async -> BackendTrust.Verdict {
            await BackendTrustGate.verify(port: port,
                                          inspect: inspect, facts: facts, receipt: currentReceipt,
                                          identity: { self.identity() })
        }
    }

    static func freshGateChecks() async {
        let previous = BackendTrustConfiguration.current
        BackendTrustConfiguration.set()
        defer { BackendTrustConfiguration.set(port: previous.port) }
        let host = Host()
        let gate = BackendTrustGate(verification: { await host.verify($0) })
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
        host.birth = .init(seconds: 1, microseconds: 0)
        host.changeDuringIdentity = { host.birth = nil }
        check(await gate.verdict() == .refused(.cannotInspect), "unreadable incarnation after identity fails closed")
        host.changeDuringIdentity = nil
        host.birth = .init(seconds: 1, microseconds: 0)
        host.answer = .missing
        check(await gate.verdict() == .refused(.notWispBackend), "a 404 /identity from the launched incarnation is not trusted")
        host.birth = .init(seconds: 3, microseconds: 0)
        var asked = host.identities
        check(await gate.verdict() == .refused(.unproven([ours])), "a 404 from a reused pid is refused")
        check(host.identities == asked, "an unproven listener is refused without being asked anything")
        host.birth = .init(seconds: 1, microseconds: 0)
        host.receipt = .none
        check(await gate.verdict() == .refused(.unproven([ours])), "no receipt: a 404 listener is refused")
        host.answer = ident()
        check(await gate.verdict() == .refused(.unproven([ours])), "no receipt: an answering listener is refused")
        check(host.identities == asked, "a receipt-less listener is never contacted")
        host.receipt = .present(.init(pid: 10, start: .init(seconds: 1, microseconds: 0), executablePath: ours.path,
                                      backendRoot: "/r", nonce: "nonce-1"))
        host.listeners = [bystander]
        check(await gate.verdict() == .refused(.unproven([bystander])) && host.identities == asked,
              "a bystander on the owned interpreter is refused without contact")
        host.listeners = [ours]
        host.answer = ident(nonce: "other")
        check(await gate.verdict() == .refused(.launchMismatch), "a different launch nonce is refused")
        asked = host.identities
        host.answer = ident()
        let swapped = host.receipt
        host.changeDuringIdentity = { host.receipt = .none }
        check(await gate.verdict() == .refused(.cannotInspect), "a receipt change during identity fails closed")
        host.changeDuringIdentity = nil
        host.receipt = swapped
        let before = host.identities
        let barrier = PairBarrier()
        let concurrentGate = BackendTrustGate(verification: { port in
            await barrier.arrive()
            return await host.verify(port)
        })
        async let first = concurrentGate.verdict()
        async let second = concurrentGate.verdict()
        let results = await [first, second]
        check(results.allSatisfy(\.isTrusted) && host.identities == before + 2,
              "concurrent requests each verify; no in-flight verdict reuse")
        host.changeDuringIdentity = { BackendTrustConfiguration.set(port: previous.port + 1) }
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
                body = {"service": "wisp-backend", "mode": mode, "pid": os.getpid()}
                if os.environ.get("WISP_LAUNCH_NONCE"): body["launch_nonce"] = os.environ["WISP_LAUNCH_NONCE"]
                return self.reply(200, json.dumps(body).encode())
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
        let nonce = UUID().uuidString
        init?(mode: String, identity: Bool) {
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-c", BackendTrustChecks.server, mode, identity ? "yes" : "no"]
            var environment = ProcessInfo.processInfo.environment
            environment[BackendOwnership.nonceEnvironmentKey] = nonce
            process.environment = environment
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
        /// A launch receipt naming this exact process, as BackendManager records one.
        func receipt(nonce: String? = nil) -> BackendOwnership.Receipt? {
            guard let start = BackendOwnership.startTime(pid: pid), let path = PortGuard.executablePath(pid: pid)
            else { return nil }
            return .init(pid: pid, start: start, executablePath: path, backendRoot: "/r", nonce: nonce ?? self.nonce)
        }
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
        check(binary.hasPrefix("/"), "could not read the test server's executable path")
        URLProtocol.registerClass(BackendTrustProtocol.self)
        var refusals: [String] = []
        let observer = NotificationCenter.default.addObserver(forName: .wispBackendRefused, object: nil, queue: nil) {
            refusals.append($0.userInfo?["message"] as? String ?? "")
        }
        defer { NotificationCenter.default.removeObserver(observer) }

        func enforce(_ server: Server) {
            BackendTrustProtocol.enforcedPort = server.port
            BackendTrustConfiguration.set(port: server.port)
        }
        // The shared store, in memory: the gate's real default verification reads it, exactly as
        // the app does after it spawns a backend. It starts empty and has no file behind it.
        func launched(_ server: Server, nonce: String? = nil) {
            guard let receipt = server.receipt(nonce: nonce) else { check(false, "no receipt for a live server"); return }
            BackendLaunchReceiptStore.shared.record(receipt)
            check(BackendLaunchReceiptStore.shared.current == .present(receipt), "receipt not recorded")
        }

        // 0. This app has launched nothing yet: no receipt, so even a genuine-looking backend is
        // someone else's. Fail closed, and nothing is sent, not even /identity.
        check(BackendLaunchReceiptStore.shared.current == .none, "the store must start empty")
        enforce(good)
        let early = await get("http://127.0.0.1:\(good.port)/hello")
        check(early.status == nil && early.error?.localizedDescription.contains("didn't start") == true,
              "with no launch recorded the real server must be refused: \(early)")
        usleep(300_000)
        check(good.requests.isEmpty, "nothing may be sent before a launch is recorded: \(good.requests)")

        // 1. Trusted: the launched process, matching incarnation and nonce, passes unharmed.
        launched(good)
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

        // 2. A stranger (the receipt names a different process) gets nothing at all, not even a question.
        launched(legacy)
        let before = good.requests.count
        let announced = refusals.count
        let blocked = await get("http://127.0.0.1:\(good.port)/hello")
        check(blocked.status == nil && blocked.error != nil, "a stranger must be refused")
        check(blocked.error?.localizedDescription.contains("didn't start") == true,
              "the failure explains itself: \(blocked.error?.localizedDescription ?? "")")
        usleep(300_000)
        check(good.requests.count == before, "NOTHING may be sent to a stranger, not even /identity: \(good.requests)")
        check(refusals.count == announced + 1 && refusals.last?.contains("Quit it and reopen Wisp") == true,
              "the person is told, once: \(refusals)")
        _ = await get("http://127.0.0.1:\(good.port)/hello")
        check(refusals.count == announced + 1, "a repeated refusal must not nag")

        // 3. Wisp's own code in a sandbox world: asked who it is, refused, no request sent.
        launched(sandboxed)
        enforce(sandboxed)
        let sandbox = await get("http://127.0.0.1:\(sandboxed.port)/hello")
        check(sandbox.status == nil, "a sandbox backend must be refused")
        usleep(300_000)
        check(sandboxed.requests == ["REQ GET /identity"], "only the identity question is sent to it: \(sandboxed.requests)")
        check(refusals.last?.contains("sandbox copy of Wisp") == true, "sandbox wording shown")

        // 4. A 404 listener that is not the launched process (the receipt names another): refused,
        // and NOTHING is sent, not even /identity.
        enforce(legacy)
        launched(good)
        let unproven = await get("http://127.0.0.1:\(legacy.port)/hello")
        check(unproven.status == nil && unproven.error?.localizedDescription.contains("didn't start") == true,
              "a 404 listener that is not the launched process must be refused: \(unproven)")
        usleep(300_000)
        check(legacy.requests.isEmpty, "nothing may be sent to an unproven listener: \(legacy.requests)")
        // Even the launched process is refused when it has no /identity (a spawned backend always has one):
        // it is asked once, and nothing else is sent.
        launched(legacy)
        let old = await get("http://127.0.0.1:\(legacy.port)/hello")
        check(old.status == nil && old.error?.localizedDescription.contains("doesn't identify itself") == true,
              "a 404 /identity from the launched process must be refused: \(old)")
        usleep(300_000)
        check(legacy.requests == ["REQ GET /identity"], "only the identity question is sent: \(legacy.requests)")

        // 4b. Another program on the same interpreter, answering as Wisp: refused unasked.
        let otherBefore = other.requests.count
        enforce(other)
        check(await get("http://127.0.0.1:\(other.port)/hello").status == nil, "a same-interpreter bystander was trusted")
        usleep(300_000)
        check(other.requests.count == otherBefore, "a same-interpreter bystander was contacted: \(other.requests)")

        // 4c. The launched process, but the receipt carries another nonce: only asked, then refused.
        launched(good, nonce: "not-this-launch")
        enforce(good)
        let goodBefore = good.requests.count
        let mismatch = await get("http://127.0.0.1:\(good.port)/hello")
        check(mismatch.status == nil && mismatch.error?.localizedDescription.contains("wasn't started by this copy") == true,
              "a nonce mismatch must be refused: \(mismatch)")
        usleep(300_000)
        check(Array(good.requests.dropFirst(goodBefore)) == ["REQ GET /identity"], "only /identity on nonce mismatch")
        launched(good)

        // 5. Nothing listening: fails promptly, nothing hangs.
        let dead = Server(mode: "production", identity: true)!
        let deadPort = dead.port
        dead.stop()
        BackendTrustProtocol.enforcedPort = deadPort
        BackendTrustConfiguration.set(port: deadPort)
        let t0 = Date()
        let none = await get("http://127.0.0.1:\(deadPort)/hello")
        check(none.status == nil && Date().timeIntervalSince(t0) < 6, "an absent backend fails promptly")

        // 6. Only the enforced port is gated: another local server passes, even while refusing a stranger.
        enforce(good)
        BackendTrustProtocol.enforcedPort = deadPort
        let free = await get("http://127.0.0.1:\(other.port)/hello")
        check(free.status == 200, "a different port must be untouched by the gate")
    }
}
