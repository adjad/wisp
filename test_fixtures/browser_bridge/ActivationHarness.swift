import Foundation
import Security
import Darwin

// A10 WP3 activation fixture. Synthetic only: no Keychain, browser, app launch,
// network or effect. Two modes share the actual app sources.
//
//  pipe    JSON lines on stdin/stdout. Backend control calls are written as
//          {"control": ...} lines and answered by the test process, which hosts
//          the real Python BridgeHost. No socket, signature or file is used, so
//          this mode runs inside the generic Simulation sandbox.
//  app     Real private Unix sockets. Plays the app: listens for the backend and
//          for a signed peer and prints "ready". Used by the dedicated signed
//          AF_UNIX gate, never by the generic sandbox.
//  peer    A minimal signed native peer for `app` mode.

private let ioLock = NSLock()

private func emit(_ object: [String: Any]) {
    ioLock.lock(); defer { ioLock.unlock() }
    let data = try! BrowserBridgeWire.json(object)
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}

private final class MemoryStore: BrowserBridgeCredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    var keys: [String: Data] = [:]
    var created = 0
    var removed = 0
    var sweeps = 0
    func create(_ identity: BrowserBridgeIdentity) throws -> Data {
        var key = Data(count: 32)
        _ = key.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 32, $0.baseAddress!) }
        lock.lock(); defer { lock.unlock() }
        keys[identity.credentialID] = key
        created += 1
        return key
    }
    func remove(_ identity: BrowserBridgeIdentity) throws {
        lock.lock(); defer { lock.unlock() }
        keys[identity.credentialID] = nil
        removed += 1
    }
    func removeAll() throws {
        lock.lock(); defer { lock.unlock() }
        keys.removeAll()
        sweeps += 1
    }
    var snapshot: [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return ["created": created, "removed": removed, "sweeps": sweeps, "live": keys.count]
    }
}

/// Answers backend control calls through the test process over stdin/stdout.
private final class PipeControl: BrowserBridgeControlEndpoint, @unchecked Sendable {
    var onChange: (() -> Void)?
    var started = false
    var connected = false
    private let gate = NSLock()
    var isConnected: Bool { connected }
    func start() throws { started = true }
    func stop() { started = false; connected = false }
    func call(_ message: [String: Any]) throws -> [String: Any] {
        try BrowserBridgeWire.check(connected, "Synthetic backend not connected")
        gate.lock(); defer { gate.unlock() }
        emit(["control": message])
        guard let line = readLine() else {
            throw BrowserContractViolation(message: "Synthetic backend closed", code: "bridge_unauthorized")
        }
        return try BrowserBridgeWire.parse(Data(line.utf8))
    }
}

private final class FakeListener: BrowserBridgeStoppable {
    let browser: BrowserBridgeBrowser
    let onStop: (BrowserBridgeBrowser) -> Void
    init(_ browser: BrowserBridgeBrowser, onStop: @escaping (BrowserBridgeBrowser) -> Void) {
        self.browser = browser
        self.onStop = onStop
    }
    func stop() { onStop(browser) }
}

/// Replays queued peer messages. Marker entries drive deterministic mid-session
/// events without threads: `__advance` moves the clock, `__forget` revokes the
/// profile, `__disable` flips the app's own enablement flag.
private final class ScriptedTransport: BrowserBridgePeerTransport, @unchecked Sendable {
    var script: [[String: Any]]
    var sent: [[String: Any]] = []
    var cancelled = false
    var closed = false
    let mark: ([String: Any]) -> Void
    init(_ script: [[String: Any]], mark: @escaping ([String: Any]) -> Void) {
        self.script = script
        self.mark = mark
    }
    func receive() throws -> Data {
        while true {
            try BrowserBridgeWire.check(!cancelled && !script.isEmpty, "Synthetic peer closed")
            let next = script.removeFirst()
            if next["__advance"] != nil || next["__forget"] != nil || next["__disable"] != nil { mark(next); continue }
            return try BrowserBridgeWire.json(next)
        }
    }
    func send(_ data: Data) throws { sent.append(try BrowserBridgeWire.parse(data)) }
    func cancel() { cancelled = true }
    func close() { closed = true }
}

private final class Fixture: @unchecked Sendable {
    let defaults: UserDefaults
    let store = MemoryStore()
    let control = PipeControl()
    var clockOffset = 0.0
    var listeners = Set<String>()
    var activation: BrowserBridgeActivation!
    let suite = "wisp.a10.harness." + UUID().uuidString

    init(requirements: [String], enabled: [String]) {
        defaults = UserDefaults(suiteName: suite)!
        let enablement = BrowserBridgeDefaultsEnablement(defaults: defaults)
        for profile in enabled { enablement.setEnabled(true, profile: profile) }
        var required: [BrowserBridgeBrowser: String] = [:]
        for name in requirements { if let browser = BrowserBridgeBrowser(rawValue: name) { required[browser] = "true" } }
        let start = ProcessInfo.processInfo.systemUptime
        let contexts = BrowserBridgeNativeContext(clock: { [unowned self] in
            ProcessInfo.processInfo.systemUptime - start + self.clockOffset })
        let endpoints = BrowserBridgeEndpoints(
            control: { [unowned self] _, _ in self.control },
            peer: { [unowned self] browser, _, _, _ in
                self.listeners.insert(browser.rawValue)
                return FakeListener(browser) { self.listeners.remove($0.rawValue) }
            })
        activation = BrowserBridgeActivation(
            configuration: BrowserBridgeConfiguration(directory: "/nonexistent/wisp-a10", peerRequirements: required),
            enablement: enablement, store: store, contexts: contexts, endpoints: endpoints)
    }
    func status() -> [String: Any] {
        [ "control_started": control.started, "connected": control.connected, "listeners": listeners.sorted(),
          "keyring": activation.hub.keyring.activeProfiles().sorted(),
          "enabled": activation.hub.enablement.enabledProfiles().sorted(), "store": store.snapshot,
          "backend_path": activation.controlPathForBackend() as Any ]
    }
    func cleanup() { defaults.removePersistentDomain(forName: suite) }
}

private final class MemoryEnablement: BrowserBridgeEnablement, @unchecked Sendable {
    private let lock = NSLock()
    private var profiles: Set<String>
    init(_ profiles: [String]) { self.profiles = Set(profiles) }
    func enabledProfiles() -> Set<String> { lock.lock(); defer { lock.unlock() }; return profiles }
    func setEnabled(_ enabled: Bool, profile: String) {
        lock.lock(); defer { lock.unlock() }
        if enabled { profiles.insert(profile) } else { profiles.remove(profile) }
    }
}

@main struct ActivationHarness {
    static func main() async {
        let mode = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "pipe"
        switch mode {
        case "app": await app()
        case "peer": peer()
        default: await pipe()
        }
    }

    /// The real app side: private sockets, a real signature check on the peer and
    /// on the backend's PID. Arguments: directory requirement backend-pid profile.
    static func app() async {
        let args = CommandLine.arguments
        let store = MemoryStore()
        let enabled = args[5].isEmpty ? [] : [args[5]]
        let activation = BrowserBridgeActivation(
            configuration: BrowserBridgeConfiguration(directory: args[2], peerRequirements: [.chrome: args[3]],
                                                      peerTimeoutSeconds: 5),
            enablement: MemoryEnablement(enabled), store: store)
        activation.backendLaunched(pid: pid_t(args[4])!)
        await activation.begin()
        emit(["ready": true])
        while let line = readLine() {
            var response: [String: Any] = ["ok": true]
            do {
                let request = try BrowserBridgeWire.parse(Data(line.utf8))
                switch request["op"] as! String {
                case "status": break
                case "enable", "disable":
                    try await activation.setEnabled(request["op"] as! String == "enable", profile: request["profile"] as! String)
                case "quit": emit(response); return
                default: throw BrowserContractViolation(message: "Unknown harness op", code: "invalid_payload")
                }
                response["keyring"] = activation.hub.keyring.activeProfiles().sorted()
                response["enabled"] = activation.hub.enablement.enabledProfiles().sorted()
                response["store"] = store.snapshot
            } catch { response = ["ok": false, "error": String(describing: error)] }
            emit(response)
        }
    }

    /// A minimal signed native peer. Arguments: socket requirement messages-json.
    static func peer() {
        do {
            let args = CommandLine.arguments
            let fd = socket(AF_UNIX, SOCK_STREAM, 0)
            var address = try TransportHarnessAddress.make(args[2])
            let status = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            try BrowserBridgeWire.check(status == 0)
            let transport = try BrowserBridgeTransport(connectedFD: fd, peerRequirement: args[3], timeoutSeconds: 5)
            let messages = try JSONSerialization.jsonObject(with: Data(args[4].utf8)) as! [[String: Any]]
            for message in messages {
                try transport.send(try BrowserBridgeWire.json(message))
                if message["op"] as? String == "disconnect" { break }
                emit(["reply": try BrowserBridgeWire.parse(try transport.receive())])
            }
            transport.close()
        } catch { emit(["denied": true]) }
    }

    static func pipe() async {
        var fixture: Fixture?
        while let line = readLine() {
            var response: [String: Any] = ["ok": true]
            do {
                let request = try BrowserBridgeWire.parse(Data(line.utf8))
                let op = request["op"] as! String
                switch op {
                case "setup":
                    fixture?.cleanup()
                    fixture = Fixture(requirements: request["requirements"] as! [String],
                                      enabled: request["enabled"] as! [String])
                case "begin":
                    await fixture!.activation.begin()
                case "connect", "disconnect":
                    fixture!.control.connected = op == "connect"
                    await fixture!.activation.controlChanged()
                case "backend_launched":
                    fixture!.activation.backendLaunched(pid: pid_t(request["pid"] as! Int))
                    response["pid"] = fixture!.activation.hub.backendPID() as Any
                case "enable", "disable":
                    do { try await fixture!.activation.setEnabled(op == "enable", profile: request["profile"] as! String) }
                    catch is BrowserBridgeUnavailable { response["error"] = "unavailable" }
                    catch { response["error"] = "denied" }
                case "forget":
                    try await fixture!.activation.forget(profile: request["profile"] as! String)
                case "status":
                    break
                case "session":
                    let f = fixture!
                    let browser = BrowserBridgeBrowser(rawValue: request["browser"] as! String)!
                    let script = request["messages"] as! [[String: Any]]
                    let transport = ScriptedTransport(script) { marker in
                        if let seconds = marker["__advance"] as? Double { f.clockOffset += seconds }
                        if let profile = marker["__forget"] as? String { f.activation.hub.closeSessions(profile: profile) }
                        if let profile = marker["__disable"] as? String {
                            f.activation.hub.enablement.setEnabled(false, profile: profile)
                        }
                    }
                    let session = BrowserBridgePeerSession(transport: transport, browser: browser, hub: f.activation.hub)
                    await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
                        Thread.detachNewThread { session.run(); done.resume() }
                    }
                    response["sent"] = transport.sent
                    response["transport_closed"] = transport.closed
                    response["cancelled"] = transport.cancelled
                case "context_of":
                    // Native-owned context as the actor would hand the client right now.
                    let profile = try BrowserBridgeProfile(request["profile"] as! String)
                    do {
                        let c = try fixture!.activation.hub.runtime(for: profile)
                        response["context"] = ["url": c.url, "enabled": c.enabled, "private": c.privateContext,
                                               "origins": c.allowedOrigins.sorted()]
                    } catch { response["context"] = NSNull() }
                default:
                    throw BrowserContractViolation(message: "Unknown harness op", code: "invalid_payload")
                }
                if let f = fixture { response["status"] = f.status() }
            } catch {
                response = ["ok": false, "error": String(describing: error)]
            }
            emit(response)
        }
        fixture?.cleanup()
    }
}

private enum TransportHarnessAddress {
    static func make(_ path: String) throws -> sockaddr_un { try BrowserBridgeSocket.address(path) }
}
