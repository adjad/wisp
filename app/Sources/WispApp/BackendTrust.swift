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
// comes from the kernel and from what THIS running app did, not from the program's words
// and not from anything on disk (a same-user program can write any file Wisp can):
//   1. it must be exactly the process this app instance spawned, recorded in memory by
//      BackendManager (pid, kernel start time, executable path), the same BackendOwnership
//      policy PortGuard and BackendManager use. A listener that fails this is refused
//      WITHOUT being asked anything. With nothing recorded (the app has spawned nothing yet,
//      or the backend is left over from an earlier launch or run by hand) it fails closed;
//      where a program runs from is no evidence either way; and
//   2. that backend must report mode "production" from /identity, which also refuses
//      Wisp's own code running against a sandbox world, must be one of the listeners, and
//      must echo the launch nonce this launch recorded. A 404 from /identity is NOT
//      accepted: a backend this app spawned always serves it.
//
// WHAT THIS GUARANTEES: at the moment of each check, every listener on the port is the
// process this app spawned, and a request is never SENT to anything else that was already
// there (a squatter, a sandbox backend, a leftover). It is re-checked for every request.
//
// WHAT IT DOES NOT GUARANTEE (audit N7, accepted residual; NATIVE-A2 is NOT closed here):
// the check inspects the listener, but the connection that then carries the request is a
// separate one that is not itself authenticated. The listener is inspected again after the
// identity answer, which narrows the window, but a same-user program that takes the port
// after the final inspection and before the request connects is not detected, and a program
// that already holds the port before this app's backend binds it is only noticed, never
// evicted. Closing that needs an authenticated transport (a different channel or a verified
// peer on the connection itself), which is a separate change.
//
// Pure policy here (testable without a network); BackendTrustGate and
// BackendTrustProtocol below apply it to every request the app makes to that port.
enum BackendTrust {
    static let productionPort = 8765

    struct Identity: Equatable {
        let service: String
        let mode: String
        let pid: Int32
        /// The nonce this backend was launched with (absent from older backends).
        var launchNonce: String? = nil
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
        case notWispBackend
        case wrongMode(String)
        case pidMismatch
        /// Not the process this app spawned (or this app has spawned nothing). Whatever it is,
        /// a stranger, a leftover or a sandbox, it is never contacted.
        case unproven([PortGuard.Listener])
        /// The launched process answered without this launch's nonce.
        case launchMismatch
    }

    enum Verdict: Equatable {
        case trusted(pids: [Int32])
        /// Nothing is listening yet (or it did not answer): nothing to refuse, nothing to trust.
        case unreachable
        case refused(Refusal)

        var isTrusted: Bool { if case .trusted = self { return true } else { return false } }
    }

    /// Pure policy. Fails closed: anything not positively proven is not trusted.
    /// `identity` is `.failed` when nothing was (or could be) asked.
    static func decide(listeners: [PortGuard.Listener]?,
                       identity: IdentityResult, receipt: BackendOwnership.ReceiptState,
                       facts: (Int32) -> BackendOwnership.ProcessFacts?) -> Verdict {
        guard let listeners else { return .refused(.cannotInspect) }
        if listeners.isEmpty { return .unreachable }
        let evidence: BackendOwnership.Identity
        switch identity {
        case .failed: evidence = .notAsked
        case .missing: evidence = .missing
        case .answered(let answer): evidence = .answered(nonce: answer.launchNonce)
        }
        var unproven: [PortGuard.Listener] = []
        for listener in listeners {
            switch BackendOwnership.verdict(listener: listener, receipt: receipt, facts: facts(listener.pid),
                                            identity: evidence) {
            case .wispBackend: continue
            case .notWisp(.processGone): return .refused(.cannotInspect)
            case .notWisp(.nonceMismatch): return .refused(.launchMismatch)
            case .notWisp: unproven.append(listener)
            }
        }
        if !unproven.isEmpty { return .refused(.unproven(unproven)) }
        let pids = listeners.map(\.pid)
        switch identity {
        case .failed:
            return .unreachable
        case .missing:
            // A backend this app spawned always serves /identity; a 404 proves nothing, so it is
            // not trusted even when the listener is the exact launched incarnation.
            return .refused(.notWispBackend)
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
        let nonce = object["launch_nonce"]
        guard nonce == nil || nonce is String else { return nil }
        return Identity(service: service, mode: mode, pid: Int32(pid.int64Value), launchNonce: nonce as? String)
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
            case .notWispBackend:
                why = "The program there doesn't identify itself as Wisp's service."
            case .wrongMode(let mode):
                why = "The service there is a \(mode) copy of Wisp, not the one for your real data."
            case .pidMismatch:
                why = "The program that answered isn't the one listening on the port."
            case .unproven(let listeners):
                why = "It is being used by \(PortGuard.describe(listeners)), which this Wisp didn't start, so Wisp "
                    + "can't confirm it is its own service. It may be another program, or left over from an earlier copy of Wisp."
            case .launchMismatch:
                why = "The service there wasn't started by this copy of Wisp."
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

/// Which port is enforced. Set synchronously at launch (a lock, not an actor) so no
/// request can be judged before it. Who counts as Wisp's backend is not configured here:
/// it is whatever BackendManager recorded in memory when it spawned one.
enum BackendTrustConfiguration {
    struct Value: Equatable {
        var port = BackendTrust.productionPort
    }
    static let storage = OSAllocatedUnfairLock(initialState: Value())

    static func set(port: Int = BackendTrust.productionPort) {
        storage.withLock { $0 = Value(port: port) }
    }
    static var current: Value { storage.withLock { $0 } }
}

/// Verifies the backend anew for each request. Everything that talks to the
/// backend goes through `BackendTrustProtocol`, which asks this first.
actor BackendTrustGate {
    static let shared = BackendTrustGate()

    private var lastAnnounced: BackendTrust.Verdict?
    private let verification: @Sendable (Int) async -> BackendTrust.Verdict

    init(verification: @escaping @Sendable (Int) async -> BackendTrust.Verdict = {
        await BackendTrustGate.verify(port: $0)
    }) {
        self.verification = verification
    }

    func verdict() async -> BackendTrust.Verdict {
        let configured = BackendTrustConfiguration.current
        let verification = self.verification
        // Neither a timed verdict nor another request's in-flight check proves who
        // owns the port for this request. Only notification wording is deduplicated.
        let task = Task.detached { await verification(configured.port) }
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

    /// A process incarnation: the kernel start time (see BackendOwnership.StartTime).
    typealias Incarnation = BackendOwnership.StartTime

    static func incarnation(pid: Int32) -> Incarnation? { BackendOwnership.startTime(pid: pid) }

    /// One full check against the live system.
    static func verify(port: Int,
                       inspect: (() -> [PortGuard.Listener]?)? = nil,
                       facts: (Int32) -> BackendOwnership.ProcessFacts? = BackendOwnership.processFacts(pid:),
                       receipt: () -> BackendOwnership.ReceiptState = { BackendLaunchReceiptStore.shared.current },
                       identity: (() async -> BackendTrust.IdentityResult)? = nil) async -> BackendTrust.Verdict {
        let inspect = inspect ?? { PortGuard.listeners(port: port) }
        var listeners = inspect()
        if listeners == nil { listeners = inspect() }   // one retry under load
        let launched = receipt()
        func decide(_ listeners: [PortGuard.Listener]?, _ answer: BackendTrust.IdentityResult) -> BackendTrust.Verdict {
            BackendTrust.decide(listeners: listeners, identity: answer, receipt: launched, facts: facts)
        }
        guard let found = listeners, !found.isEmpty else { return decide(listeners, .failed) }
        // A stranger, or anything that is not exactly the launched process, is refused
        // without being asked anything: nothing is sent to it.
        let unasked = decide(found, .failed)
        if case .refused = unasked { return unasked }
        let incarnations = found.map { facts($0.pid)?.start }
        guard incarnations.allSatisfy({ $0 != nil }) else { return .refused(.cannotInspect) }
        let answer = await (identity ?? { await fetchIdentity(port: port) })()
        // Identity is asynchronous. Inspect again before allowing the private request,
        // including kernel start times (a reused pid is not the same process) and the
        // receipt itself (a launch meanwhile names a different process).
        let current = inspect()
        guard current == found, found.map({ facts($0.pid)?.start }) == incarnations, receipt() == launched else {
            if let current, !current.isEmpty, current != found, case .refused(let why) = decide(current, .failed) {
                return .refused(why)   // name a replacement stranger plainly
            }
            return .refused(.cannotInspect)
        }
        return decide(current, answer)
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

/// Sits in front of every request the app makes to its backend port. A request is sent
/// only after the program on the port was checked, just before, to be the process this app
/// spawned; otherwise it fails here and nothing leaves the app. The check is not bound to
/// the connection that then carries the request, so the N7 race described at the top of
/// this file remains: this narrows who can receive a request, it does not authenticate the
/// connection.
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
