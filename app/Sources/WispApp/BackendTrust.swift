import Foundation
import os
import Darwin

// The app talks to its backend on a fixed port (8765) and used to trust whatever
// answered there. Every "send this email / create this event / read my mail" exchange
// then ran against the user's real Mail, Messages and Calendar, so a sandbox backend
// started on that port while the real app was open, a sandbox server acting as a second
// app on its event stream, or any program that took the port, could make the real app
// act or could receive the data it syncs.
//
// This file decides whether the program on the port is Wisp's OWN backend. The proof
// comes from the kernel, not from the program's own words:
//   1. every process listening on the port must be running an executable inside Wisp's
//      own backend directory (proc_pidpath, the same ownership test PortGuard uses to
//      decide what Wisp may ever signal); and
//   2. that backend must report mode "production" from /identity, which also refuses
//      Wisp's own code running against a sandbox world, and must be one of the listeners.
// A backend that predates /identity (it answers 404) is accepted on the strength of (1)
// alone, so updating the app never strands a still-running older backend.
//
// Pure policy here (testable without a network); BackendTrustGate and
// BackendTrustProtocol below apply it to every request the app makes to that port.
enum BackendTrust {
    static let productionPort = 8765

    struct Identity: Equatable {
        let service: String
        let mode: String
        let pid: Int32
    }

    /// What asking the backend "who are you?" produced.
    enum IdentityResult: Equatable {
        case answered(Identity)
        /// 404: an older backend with no /identity endpoint.
        case missing
        /// No usable answer (connection error, timeout, malformed body).
        case failed
    }

    enum Refusal: Equatable {
        case cannotInspect
        case foreignListener([PortGuard.Listener])
        case notWispBackend
        case wrongMode(String)
        case pidMismatch
    }

    enum Verdict: Equatable {
        case trusted(pids: [Int32])
        /// Nothing is listening yet (or it did not answer): nothing to refuse, nothing to trust.
        case unreachable
        case refused(Refusal)

        var isTrusted: Bool { if case .trusted = self { return true } else { return false } }
    }

    /// Pure policy. Fails closed: anything not positively proven is not trusted.
    static func decide(listeners: [PortGuard.Listener]?, ownedPrefixes: [String],
                       identity: IdentityResult) -> Verdict {
        guard let listeners else { return .refused(.cannotInspect) }
        if listeners.isEmpty { return .unreachable }
        let foreign = listeners.filter { !PortGuard.executableInOwnedDirectory($0, ownedPrefixes: ownedPrefixes) }
        if !foreign.isEmpty { return .refused(.foreignListener(foreign)) }
        let pids = listeners.map(\.pid)
        switch identity {
        case .failed:
            return .unreachable
        case .missing:
            return .trusted(pids: pids)
        case .answered(let answer):
            guard answer.service == "wisp-backend" else { return .refused(.notWispBackend) }
            guard answer.mode == "production" else { return .refused(.wrongMode(answer.mode)) }
            guard pids.contains(answer.pid) else { return .refused(.pidMismatch) }
            return .trusted(pids: pids)
        }
    }

    /// Strict: anything that is not exactly the documented shape is "no identity".
    static func parseIdentity(_ data: Data) -> Identity? {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let service = object["service"] as? String, let mode = object["mode"] as? String,
              let pid = (object["pid"] as? NSNumber), CFGetTypeID(pid) != CFBooleanGetTypeID(),
              pid.int64Value > 0, pid.int64Value <= Int64(Int32.max) else { return nil }
        return Identity(service: service, mode: mode, pid: Int32(pid.int64Value))
    }

    /// What to tell the person. Plain language, names the program, never says "refused: 4".
    static func describe(_ verdict: Verdict) -> String {
        switch verdict {
        case .trusted: return "Wisp's service is running."
        case .unreachable: return "Wisp's service isn't running yet."
        case .refused(let refusal):
            let why: String
            switch refusal {
            case .cannotInspect:
                why = "Wisp couldn't check which program is using it."
            case .foreignListener(let listeners):
                why = "It is being used by \(PortGuard.describe(listeners)), which isn't Wisp's own service."
            case .notWispBackend:
                why = "The program there doesn't identify itself as Wisp's service."
            case .wrongMode(let mode):
                why = "The service there is a \(mode) copy of Wisp, not the one for your real data."
            case .pidMismatch:
                why = "The program that answered isn't the one listening on the port."
            }
            return "Wisp stopped talking to the program on port \(productionPort), so it can't act on "
                + "your Mail, Messages or Calendar through it. \(why) Quit it and reopen Wisp."
        }
    }
}

extension Notification.Name {
    /// Posted (once per change) when the app starts refusing the program on the backend
    /// port. `userInfo["message"]` is the plain-language explanation.
    static let wispBackendRefused = Notification.Name("wisp.backendRefused")
}

/// Which executables count as Wisp's own backend, and which port is enforced. Set
/// synchronously at launch (a lock, not an actor) so no request can be judged before it.
enum BackendTrustConfiguration {
    struct Value: Equatable {
        var ownedPrefixes: [String] = []
        var port = BackendTrust.productionPort
    }
    static let storage = OSAllocatedUnfairLock(initialState: Value())

    static func set(ownedPrefixes: [String], port: Int = BackendTrust.productionPort) {
        storage.withLock { $0 = Value(ownedPrefixes: ownedPrefixes, port: port) }
    }
    static var current: Value { storage.withLock { $0 } }
}

/// Verifies the backend anew for each request. Everything that talks to the
/// backend goes through `BackendTrustProtocol`, which asks this first.
actor BackendTrustGate {
    static let shared = BackendTrustGate()

    private var lastAnnounced: BackendTrust.Verdict?
    private let verification: @Sendable (Int, [String]) async -> BackendTrust.Verdict

    init(verification: @escaping @Sendable (Int, [String]) async -> BackendTrust.Verdict = {
        await BackendTrustGate.verify(port: $0, ownedPrefixes: $1)
    }) {
        self.verification = verification
    }

    func verdict() async -> BackendTrust.Verdict {
        let configured = BackendTrustConfiguration.current
        let verification = self.verification
        // Neither a timed verdict nor another request's in-flight check proves who
        // owns the port for this request. Only notification wording is deduplicated.
        let task = Task.detached { await verification(configured.port, configured.ownedPrefixes) }
        let result = await task.value
        guard configured == BackendTrustConfiguration.current else { return .refused(.cannotInspect) }
        announce(result)
        return result
    }

    private func announce(_ verdict: BackendTrust.Verdict) {
        guard verdict != lastAnnounced else { return }
        lastAnnounced = verdict
        guard case .refused = verdict else { return }
        NotificationCenter.default.post(name: .wispBackendRefused, object: nil,
                                        userInfo: ["message": BackendTrust.describe(verdict)])
    }

    /// One full check against the live system.
    struct Incarnation: Equatable {
        let seconds: UInt64
        let microseconds: UInt64
    }

    static func incarnation(pid: Int32) -> Incarnation? {
        var info = proc_bsdinfo()
        let size = MemoryLayout<proc_bsdinfo>.size
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(size)) == Int32(size) else { return nil }
        return Incarnation(seconds: info.pbi_start_tvsec, microseconds: info.pbi_start_tvusec)
    }

    static func verify(port: Int, ownedPrefixes: [String],
                       inspect: (() -> [PortGuard.Listener]?)? = nil,
                       processIncarnation: (Int32) -> Incarnation? = incarnation,
                       identity: (() async -> BackendTrust.IdentityResult)? = nil) async -> BackendTrust.Verdict {
        let inspect = inspect ?? { PortGuard.listeners(port: port) }
        var listeners = inspect()
        if listeners == nil { listeners = inspect() }   // one retry under load
        guard let found = listeners, !found.isEmpty else {
            return BackendTrust.decide(listeners: listeners, ownedPrefixes: ownedPrefixes, identity: .failed)
        }
        // A stranger is refused without being asked anything: nothing is sent to it.
        if found.contains(where: { !PortGuard.executableInOwnedDirectory($0, ownedPrefixes: ownedPrefixes) }) {
            return BackendTrust.decide(listeners: found, ownedPrefixes: ownedPrefixes, identity: .failed)
        }
        let incarnations = found.map { processIncarnation($0.pid) }
        guard incarnations.allSatisfy({ $0 != nil }) else { return .refused(.cannotInspect) }
        let answer = await (identity ?? { await fetchIdentity(port: port) })()
        // Identity is asynchronous. Inspect again before allowing the private request,
        // including kernel start times: a reused pid is not the same process.
        let current = inspect()
        let verdict = BackendTrust.decide(listeners: current, ownedPrefixes: ownedPrefixes, identity: answer)
        guard verdict.isTrusted else { return verdict }
        guard current == found, found.map({ processIncarnation($0.pid) }) == incarnations else {
            return .refused(.cannotInspect)
        }
        return verdict
    }

    static func fetchIdentity(port: Int) async -> BackendTrust.IdentityResult {
        guard let url = URL(string: "http://127.0.0.1:\(port)/identity") else { return .failed }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        request.cachePolicy = .reloadIgnoringLocalCacheData
        // This request IS the verification, so it must not wait on itself.
        let marked = (request as NSURLRequest).mutableCopy() as! NSMutableURLRequest
        URLProtocol.setProperty(true, forKey: BackendTrustProtocol.bypassKey, in: marked)
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        guard let (data, response) = try? await session.data(for: marked as URLRequest),
              let http = response as? HTTPURLResponse else { return .failed }
        if http.statusCode == 404 { return .missing }
        guard http.statusCode == 200, let identity = BackendTrust.parseIdentity(data) else { return .failed }
        return .answered(identity)
    }
}

/// Sits in front of every request the app makes to its backend port. A request goes out
/// only while the program on the port is proven to be Wisp's own backend; otherwise it
/// fails here and nothing leaves the app.
final class BackendTrustProtocol: URLProtocol, URLSessionDataDelegate {
    static let bypassKey = "wisp.backendtrust.bypass"
    /// The port that is enforced. Anything else (the Settings QA stub, other apps) is untouched.
    nonisolated(unsafe) static var enforcedPort = BackendTrust.productionPort

    private var gateTask: Task<Void, Never>?
    private var inner: URLSessionDataTask?
    private var session: URLSession?

    override class func canInit(with request: URLRequest) -> Bool {
        guard let url = request.url, url.scheme == "http", url.host == "127.0.0.1",
              url.port == enforcedPort,
              URLProtocol.property(forKey: bypassKey, in: request) == nil else { return false }
        return true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        gateTask = Task { [weak self] in
            let verdict = await BackendTrustGate.shared.verdict()
            guard let self, !Task.isCancelled else { return }
            guard verdict.isTrusted else {
                let message = BackendTrust.describe(verdict)
                self.client?.urlProtocol(self, didFailWithError: URLError(
                    .cannotConnectToHost, userInfo: [NSLocalizedDescriptionKey: message]))
                return
            }
            self.forward()
        }
    }

    private func forward() {
        let marked = (request as NSURLRequest).mutableCopy() as! NSMutableURLRequest
        URLProtocol.setProperty(true, forKey: Self.bypassKey, in: marked)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = max(request.timeoutInterval, 1)
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session
        let task = session.dataTask(with: marked as URLRequest)
        inner = task
        task.resume()
    }

    override func stopLoading() {
        gateTask?.cancel()
        inner?.cancel()
        session?.invalidateAndCancel()
        inner = nil
        session = nil
    }

    // MARK: URLSessionDataDelegate: relay the real response to the original caller.

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        client?.urlProtocol(self, didLoad: data)   // streamed as it arrives, so SSE stays live
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            // A cancel we caused (stopLoading) must not be reported as a failure.
            if (error as? URLError)?.code != .cancelled { client?.urlProtocol(self, didFailWithError: error) }
        } else {
            client?.urlProtocolDidFinishLoading(self)
        }
        session.finishTasksAndInvalidate()
    }

    /// The backend never redirects. Following one would leave the verified origin.
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}
