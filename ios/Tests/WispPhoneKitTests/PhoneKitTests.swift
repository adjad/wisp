import Foundation
import Testing
@testable import WispPhoneKit

// MARK: Think filter

private func filtered(_ chunks: [String]) -> String {
    var filter = ThinkFilter()
    return chunks.map { filter.feed($0) }.joined() + filter.finish()
}

@Test func thinkBlocksAreHidden() {
    #expect(filtered(["Hi <think>secret</think>there"]) == "Hi there")
}
@Test func thinkTagsSplitAcrossChunksAreHidden() {
    #expect(filtered(["Hel", "lo <th", "ink>pla", "n</th", "ink> world"]) == "Hello  world")
}
@Test func unclosedThinkNeverLeaks() {
    #expect(filtered(["Answer.", "<think>still reasoning and never closes"]) == "Answer.")
}
@Test func lookalikeTextIsKept() {
    #expect(filtered(["a < b and <thin", "g>"]) == "a < b and <thing>")
}

// MARK: Gateway SSE

@Test func gatewayEventsDecode() {
    func d(_ s: String) -> ChatEvent? { GatewayEventDecoder.decode(line: s, sessionId: "s1") }
    #expect(d(#"data: {"type":"session","id":"abc"}"#) == .session("abc"))
    #expect(d(#"data: {"type":"delta","text":"Hel"}"#) == .delta("Hel"))
    #expect(d(#"data: {"type":"text","text":"Hello"}"#) == .replace("Hello"))
    #expect(d(#"data: {"type":"tool_call","name":"get_upcoming"}"#) == .activity("Using get upcoming"))
    #expect(d(#"data: {"type":"error","message":"boom"}"#) == .failed("boom"))
    #expect(d(#"data: {"type":"done"}"#) == .done)
    #expect(d(#"data: {"type":"heartbeat"}"#) == nil)
    #expect(d(": keepalive") == nil)
    #expect(d("data: not json") == nil)
}
@Test func confirmCarriesSessionAndRequest() {
    let line = #"data: {"type":"confirm","id":"a1","request_id":"r9","tool":"send_email","reason":"Send to Sam","preview":"Hi Sam","grantable":false}"#
    guard case .approval(let request)? = GatewayEventDecoder.decode(line: line, sessionId: "s42") else {
        Issue.record("not an approval"); return
    }
    #expect(request == ApprovalRequest(id: "a1", requestId: "r9", sessionId: "s42", tool: "send_email",
                                       reason: "Send to Sam", preview: "Hi Sam", grantable: false))
}
@Test func confirmCarriesGrantScope() {
    let line = #"data: {"type":"confirm","id":"a2","tool":"web_fetch","grantable":true,"scope_hint":"example.com"}"#
    guard case .approval(let request)? = GatewayEventDecoder.decode(line: line, sessionId: "s1") else {
        Issue.record("not an approval"); return
    }
    #expect(request.grantable && request.scopeHint == "example.com")
}

// MARK: OpenAI stream

@Test func openAIChunksDecode() {
    #expect(OpenAIStreamDecoder.decode(line: #"data: {"choices":[{"delta":{"content":"Hi"}}]}"#) == .content("Hi"))
    #expect(OpenAIStreamDecoder.decode(line: #"data: {"choices":[{"delta":{"role":"assistant"}}]}"#) == nil)
    #expect(OpenAIStreamDecoder.decode(line: "data: [DONE]") == .done)
    #expect(OpenAIStreamDecoder.decode(line: #"data: {"error":{"message":"rate limited"}}"#) == .error("rate limited"))
    #expect(OpenAIStreamDecoder.decode(line: ": OPENROUTER PROCESSING") == nil)
    #expect(OpenAIStreamDecoder.errorMessage(from: Data(#"{"error":{"message":"bad key"}}"#.utf8)) == "bad key")
}

// MARK: Pairing

private let token = String(repeating: "A", count: 43)

@Test func pairingLinkParses() throws {
    let link = "wisp-pair://v1?host=adis-mbp.tail1234.ts.net&ip=100.101.1.2&port=8799&token=\(token)&mac=Adi%27s%20MacBook"
    let pairing = try MacPairing.parse(link)
    #expect(pairing.host == "adis-mbp.tail1234.ts.net")
    #expect(pairing.ip == "100.101.1.2")
    #expect(pairing.port == 8799)
    #expect(pairing.macName == "Adi's MacBook")
    #expect(pairing.baseURLs.map(\.absoluteString) == ["http://adis-mbp.tail1234.ts.net:8799", "http://100.101.1.2:8799"])
}
@Test func pairingRefusesNonTailnetAddresses() {
    #expect(throws: MacPairing.ParseError.unsafeAddress) {
        try MacPairing.parse("wisp-pair://v1?host=evil.example.com&port=8799&token=\(token)")
    }
    #expect(throws: MacPairing.ParseError.unsafeAddress) {
        try MacPairing.parse("wisp-pair://v1?ip=192.168.1.5&port=8799&token=\(token)")
    }
}
@Test func pairingRejectsBadInput() {
    #expect(throws: MacPairing.ParseError.notAPairingLink) { try MacPairing.parse("https://example.com") }
    #expect(throws: MacPairing.ParseError.missingToken) { try MacPairing.parse("wisp-pair://v1?ip=100.64.0.1&port=8799&token=short") }
    #expect(throws: MacPairing.ParseError.missingAddress) { try MacPairing.parse("wisp-pair://v1?port=8799&token=\(token)") }
    #expect(throws: MacPairing.ParseError.badPort) { try MacPairing.parse("wisp-pair://v1?ip=100.64.0.1&port=99999&token=\(token)") }
}
@Test func tailscaleRange() {
    #expect(MacPairing.isTailscaleIPv4("100.64.0.1"))
    #expect(MacPairing.isTailscaleIPv4("100.127.255.254"))
    #expect(!MacPairing.isTailscaleIPv4("100.128.0.1"))
    #expect(!MacPairing.isTailscaleIPv4("10.0.0.1"))
    #expect(!MacPairing.isTailscaleIPv4("100.64.0"))
}

// MARK: Cloud settings

@Test func cloudURLMustBeEncrypted() throws {
    #expect(try CloudSettings(provider: .openRouter).completionsURL().absoluteString
            == "https://openrouter.ai/api/v1/chat/completions")
    #expect(throws: CloudSettings.Problem.insecureURL) {
        try CloudSettings(provider: .custom, baseURL: "http://example.com/v1", model: "m").completionsURL()
    }
    #expect(try CloudSettings(provider: .custom, baseURL: "http://box.tail1.ts.net:8000/v1", model: "m")
        .completionsURL().absoluteString == "http://box.tail1.ts.net:8000/v1/chat/completions")
    #expect(throws: CloudSettings.Problem.missingModel) {
        try CloudSettings(provider: .custom, baseURL: "https://x.dev/v1", model: " ").completionsURL()
    }
}
@Test func cloudRequestCarriesKeyAndStreams() throws {
    let client = CloudClient(settings: CloudSettings(provider: .openAI, model: "gpt-x"), apiKey: "sk-test")
    let request = try client.makeRequest(messages: [["role": "user", "content": "hi"]])
    #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer sk-test")
    let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
    #expect(body["stream"] as? Bool == true)
    #expect(body["model"] as? String == "gpt-x")
    #expect(throws: CloudSettings.Problem.missingKey) {
        try CloudClient(settings: CloudSettings(), apiKey: "").makeRequest(messages: [])
    }
}

// MARK: Prompt window and storage

@Test func historyWindowStartsWithUserAndRespectsBudget() {
    let history = [
        ChatMessage(role: .assistant, text: "orphan answer"),
        ChatMessage(role: .user, text: String(repeating: "x", count: 50)),
        ChatMessage(role: .assistant, text: "a1"),
        ChatMessage(role: .user, text: "q2"),
    ]
    let all = PhonePrompt.messages(system: "S", history: history)
    #expect(all.map { $0["role"]! } == ["system", "user", "assistant", "user"])
    let tight = PhonePrompt.messages(system: "S", history: history, characterBudget: 10)
    #expect(tight.map { $0["content"]! } == ["S", "q2"])
}
@Test func systemPromptStatesNoTools() {
    let prompt = PhonePrompt.system(engine: .onDevice, now: Date(timeIntervalSince1970: 0),
                                    timeZone: TimeZone(identifier: "UTC")!)
    #expect(prompt.contains("entirely on the user's iPhone"))
    #expect(prompt.contains("Thursday, January 1, 1970"))
    #expect(prompt.contains("Mac mode"))
}
@Test func conversationsRoundTrip() throws {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-\(UUID().uuidString).json")
    defer { try? FileManager.default.removeItem(at: url) }
    let store = ConversationStore(fileURL: url)
    #expect(store.load().isEmpty)
    let conversation = Conversation(engine: .mac, title: "T", messages: [ChatMessage(role: .user, text: "hi")],
                                    remoteSessionId: "s1", updatedAt: Date(timeIntervalSince1970: 1_000))
    try store.save([conversation])
    #expect(store.load() == [conversation])
}
@Test func titlesComeFromFirstLine() {
    #expect(Conversation.title(from: "  What's on today?\nmore") == "What's on today?")
    #expect(Conversation.title(from: String(repeating: "a", count: 60)).count == 48)
    #expect(Conversation.title(from: "   ") == "New chat")
}

// MARK: Audit regressions (PR 336)

@Test(arguments: ["203.0.113.5%2F.ts.net", "evil.com%23.ts.net", "evil.com%3F.ts.net", "x@evil.com.ts.net",
                  "a..b.ts.net", "-bad.tail.ts.net", "ts.net", ".ts.net", "host%20x.tail.ts.net"])
func pairingRejectsSmuggledHosts(host: String) {
    #expect(throws: MacPairing.ParseError.unsafeAddress) {
        try MacPairing.parse("wisp-pair://v1?host=\(host)&port=8799&token=\(token)")
    }
}
@Test func modelPathsStayOneSegmentDeep() {
    // A not-yet-existing child under a symlinked parent (/var → /private/var on device) still passes.
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("Models-\(UUID().uuidString)")
    let bundle = root.appendingPathComponent("ling-3.0-tiny", isDirectory: true)
    #expect(ModelBundle.isDirectChild(bundle, of: root))
    #expect(ModelBundle.isDirectChild(bundle.appendingPathComponent("model.safetensors"), of: bundle))
    #expect(!ModelBundle.isDirectChild(root.appendingPathComponent(".."), of: root))
    #expect(!ModelBundle.isDirectChild(bundle.appendingPathComponent("x/y.json"), of: bundle))
    #expect(!ModelBundle.isDirectChild(bundle.appendingPathComponent("../../escape.json"), of: bundle))
}
@Test func tailscaleIPv4RejectsLeadingZeros() {
    #expect(!MacPairing.isTailscaleIPv4("100.064.0.1"))
    #expect(!MacPairing.isTailscaleIPv4("100.64.00.1"))
    #expect(MacPairing.isTailscaleIPv4("100.64.0.1"))
}
@Test(arguments: ["100.64.0.1%2F", "100.+64.0.1", "100.64.0.01x", "100.064.0.1.5"])
func pairingRejectsMalformedIPs(ip: String) {
    #expect(throws: MacPairing.ParseError.unsafeAddress) {
        try MacPairing.parse("wisp-pair://v1?ip=\(ip)&port=8799&token=\(token)")
    }
}
@Test func pairingURLsCarryNoPath() throws {
    let pairing = try MacPairing.parse("wisp-pair://v1?host=mac.tail1.ts.net&ip=100.64.0.1&port=8799&token=\(token)")
    for url in pairing.baseURLs {
        #expect(url.path.isEmpty && url.query == nil && url.fragment == nil && url.port == 8799)
    }
    #expect(pairing.addressDescription == "mac.tail1.ts.net:8799")
    // A pairing stored before validation tightened still never yields an unsafe URL.
    let stale = MacPairing(host: "1.2.3.4/.ts.net", ip: "8.8.8.8", port: 80, macName: "x", token: token)
    #expect(stale.baseURLs.isEmpty)
}
@Test func modelBundleNamesAreValidated() {
    func bundle(id: String, name: String) -> ModelBundle {
        ModelBundle(id: id, modelType: "bailing_hybrid", totalBytes: 1,
                    files: [.init(name: name, size: 1, sha256: String(repeating: "a", count: 64))])
    }
    #expect(bundle(id: "ling-3.0-tiny-oq4e", name: "model.safetensors").isSafe)
    #expect(!bundle(id: "..", name: "model.safetensors").isSafe)
    #expect(!bundle(id: "a/b", name: "model.safetensors").isSafe)
    #expect(!bundle(id: "ling", name: "../../Wisp/conversations.json").isSafe)
    #expect(!bundle(id: "ling", name: ".hidden.json").isSafe)
    #expect(!bundle(id: "ling", name: "modeling.py").isSafe)
    #expect(!bundle(id: "ling", name: "a%2Fb.json").isSafe)
    #expect(!ModelBundle(id: "ling", modelType: nil, totalBytes: 1,
                         files: [.init(name: "a.json", size: 1, sha256: "zz")]).isSafe)
}

// MARK: Ping challenge (gateway v2)

struct StubReply: Sendable {
    var status: Int
    var headers: [String: String] = [:]
    var body = Data()
    /// Drop the connection after the body, as a flaky network would mid-download.
    var dropAfterBody = false
    /// Never finish after the body, like a slow multi-GB transfer.
    var hangAfterBody = false
}

/// Answers requests for one port from a per-test handler, so parallel tests never share state.
final class StubProtocol: URLProtocol, @unchecked Sendable {
    typealias Handler = @Sendable (URLRequest) -> (Int, Data)?
    typealias ReplyHandler = @Sendable (URLRequest) -> StubReply?
    nonisolated(unsafe) private static var handlers: [Int: ReplyHandler] = [:]
    nonisolated(unsafe) private static var seen: [Int: [URLRequest]] = [:]
    private static let lock = NSLock()

    static func session(port: Int, handler: @escaping Handler) -> URLSession {
        URLSession(configuration: configuration(port: port) { request in
            handler(request).map { StubReply(status: $0.0, body: $0.1) }
        })
    }
    static func configuration(port: Int, reply: @escaping ReplyHandler) -> URLSessionConfiguration {
        lock.withLock { handlers[port] = reply; seen[port] = seen[port] ?? [] }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return config
    }
    static func requests(port: Int) -> [URLRequest] { lock.withLock { seen[port] ?? [] } }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        let port = request.url?.port ?? 0
        let handler = Self.lock.withLock { Self.seen[port, default: []].append(request); return Self.handlers[port] }
        guard let url = request.url, let reply = handler?(request) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost)); return
        }
        // A Content-Type, as the gateway sends, so URLSession doesn't hold the response back to sniff it.
        let headers = ["Content-Type": "application/octet-stream"].merging(reply.headers) { $1 }
        let response = HTTPURLResponse(url: url, statusCode: reply.status, httpVersion: nil, headerFields: headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if !reply.body.isEmpty { client?.urlProtocol(self, didLoad: reply.body) }
        if reply.hangAfterBody { return }
        if reply.dropAfterBody {
            // Let the body reach the delegate first, as it would on a real connection.
            let client = client
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.05) {
                client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            }
        } else {
            client?.urlProtocolDidFinishLoading(self)
        }
    }
}

private func nonce(of request: URLRequest) -> String {
    URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
        .queryItems?.first { $0.name == "nonce" }?.value ?? ""
}

private func pingAnswer(_ proofs: [String]) -> Data {
    try! JSONSerialization.data(withJSONObject: ["wisp_gateway": 2, "proofs": proofs])
}

private func helloBody() -> Data {
    Data(#"{"gateway_version":2,"mac_name":"Mac","device":{"id":"d1","name":"Phone"},"backend":{"reachable":true}}"#.utf8)
}

@Test func pingProofMatchesGatewayVector() {
    // Same vector as tests/test_phone_gateway.py::test_ping_proof_known_vector.
    #expect(GatewayProof.proof(token: token, nonce: "00112233445566778899aabbccddeeff")
            == "2dd110115354b568036ae2d48dec99d6f038e6398e9e184e8a1c7c381c7f32ea")
}
@Test func pingNoncesAreFreshHex() {
    let a = GatewayProof.newNonce(), b = GatewayProof.newNonce()
    #expect(a.count == 64 && a.allSatisfy { $0.isHexDigit && !$0.isUppercase } && a != b)
}
@Test func pingVerificationNeedsTheRightTokenAndNonce() {
    let nonce = GatewayProof.newNonce()
    let good = GatewayProof.proof(token: token, nonce: nonce)
    #expect(GatewayProof.verify(pingResponse: pingAnswer(["00", good]), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer([good]), token: token + "x", nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer([good]), token: token, nonce: GatewayProof.newNonce()))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer([]), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: Data(#"{"wisp_gateway":1}"#.utf8), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer([String(good.dropLast(2))]), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: Data("not json".utf8), token: token, nonce: nonce))
}
@Test func gatewaySendsTokenOnlyAfterProof() async throws {
    let port = 18_801
    let session = StubProtocol.session(port: port) { request in
        if request.url!.path == "/phone/v1/ping" {
            return (200, pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))]))
        }
        return (200, helloBody())
    }
    let pairing = MacPairing(host: "mac.tail1.ts.net", ip: nil, port: port, macName: "Mac", token: token)
    let hello = try await GatewayClient(pairing: pairing, session: session).hello()
    #expect(hello.macName == "Mac")
    let seen = StubProtocol.requests(port: port)
    #expect(seen.map { $0.url!.path } == ["/phone/v1/ping", "/phone/v1/hello"])
    #expect(seen[0].value(forHTTPHeaderField: "Authorization") == nil)
    #expect(nonce(of: seen[0]).count == 64)
    #expect(seen[1].value(forHTTPHeaderField: "Authorization") == "Bearer \(token)")
}
@Test(arguments: ["plain", "wrongToken", "replayed"])
func gatewayWithholdsTokenFromImpostor(kind: String) async {
    let port = 18_802 + ["plain", "wrongToken", "replayed"].firstIndex(of: kind)!
    let stale = GatewayProof.proof(token: token, nonce: GatewayProof.newNonce())
    let session = StubProtocol.session(port: port) { request in
        switch kind {
        case "plain": return (200, Data(#"{"wisp_gateway":1}"#.utf8))  // a v1 ping, or anyone answering 200
        case "wrongToken": return (200, pingAnswer([GatewayProof.proof(token: "other", nonce: nonce(of: request))]))
        default: return (200, pingAnswer([stale]))  // a proof captured for some other nonce
        }
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    await #expect(throws: GatewayError.unverified) {
        try await GatewayClient(pairing: pairing, session: session).hello()
    }
    let seen = StubProtocol.requests(port: port)
    #expect(seen.allSatisfy { $0.url!.path == "/phone/v1/ping" && $0.value(forHTTPHeaderField: "Authorization") == nil })
}
@Test func gatewayFallsBackToTheAddressThatProves() async throws {
    // MagicDNS answers without a proof; the IP proves. Same port, so route by host.
    let port = 18_810
    let session = StubProtocol.session(port: port) { request in
        let url = request.url!
        guard url.path == "/phone/v1/ping" else { return url.host == "100.64.0.1" ? (200, helloBody()) : nil }
        if url.host == "mac.tail1.ts.net" { return (200, pingAnswer([])) }
        return (200, pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))]))
    }
    let pairing = MacPairing(host: "mac.tail1.ts.net", ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    _ = try await GatewayClient(pairing: pairing, session: session).hello()
    let authed = StubProtocol.requests(port: port).filter { $0.value(forHTTPHeaderField: "Authorization") != nil }
    #expect(authed.map { $0.url!.host } == ["100.64.0.1"])
}
@Test func gatewayUnreachableWhenNothingAnswers() async {
    let session = StubProtocol.session(port: 18_811) { _ in nil }
    let pairing = MacPairing(host: "mac.tail1.ts.net", ip: nil, port: 18_811, macName: "Mac", token: token)
    await #expect(throws: GatewayError.unreachable) {
        try await GatewayClient(pairing: pairing, session: session).hello()
    }
}
@Test func cloudPlainHTTPOnlyToMagicDNSNames() throws {
    for base in ["http://100.64.0.1:8000/v1", "http://100.100.1.2/v1", "http://127.0.0.1:8000/v1",
                 "http://evil.com#.ts.net/v1"] {
        #expect(throws: CloudSettings.Problem.self) {
            try CloudSettings(provider: .custom, baseURL: base, model: "m").completionsURL()
        }
    }
    #expect(throws: CloudSettings.Problem.insecureURL) {
        try CloudSettings(provider: .custom, baseURL: "http://100.64.0.1:8000/v1", model: "m").completionsURL()
    }
    _ = try CloudSettings(provider: .custom, baseURL: "http://Box.Tail1.ts.net/v1", model: "m").completionsURL()
    #expect(try CloudSettings(provider: .custom, baseURL: "https://100.64.0.1/v1", model: "m")
        .completionsURL().scheme == "https")
}

/// A mutable value tests can flip while a client holds a reference.
final class Box<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: T
    init(_ value: T) { stored = value }
    var value: T {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

@Test func verifiedBaseExpiresAndIsProvedAgain() async throws {
    // The Mac proves, then Tailscale drops and a stranger answers at the same address.
    let port = 18_812
    let impostor = Box(false), clock = Box(ContinuousClock.now)
    let session = StubProtocol.session(port: port) { request in
        if request.url!.path == "/phone/v1/ping" {
            return impostor.value ? (200, Data(#"{"wisp_gateway":2,"proofs":[]}"#.utf8))
                : (200, pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))]))
        }
        return (200, helloBody())
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: session, now: { clock.value })
    _ = try await client.hello()
    _ = try await client.hello()  // within the lifetime: no second ping
    #expect(StubProtocol.requests(port: port).map { $0.url!.path }
            == ["/phone/v1/ping", "/phone/v1/hello", "/phone/v1/hello"])
    impostor.value = true
    clock.value = clock.value.advanced(by: .seconds(GatewayClient.proofLifetime + 1))
    await #expect(throws: GatewayError.unverified) { try await client.hello() }
    #expect(StubProtocol.requests(port: port).last?.value(forHTTPHeaderField: "Authorization") == nil)
}
@Test func httpErrorForgetsTheVerifiedBase() async throws {
    let port = 18_813
    let failHello = Box(true)
    let session = StubProtocol.session(port: port) { request in
        if request.url!.path == "/phone/v1/ping" {
            return (200, pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))]))
        }
        return failHello.value ? (503, Data()) : (200, helloBody())
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: session)
    await #expect(throws: GatewayError.http(503, "")) { try await client.hello() }
    failHello.value = false
    _ = try await client.hello()
    #expect(StubProtocol.requests(port: port).map { $0.url!.path }
            == ["/phone/v1/ping", "/phone/v1/hello", "/phone/v1/ping", "/phone/v1/hello"])
}
@Test func malformedProofsAreRejected() {
    let nonce = GatewayProof.newNonce()
    let good = GatewayProof.proof(token: token, nonce: nonce)
    #expect(!GatewayProof.verify(pingResponse: pingAnswer([good + "0"]), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer(["zz" + good.dropFirst(2)]), token: token, nonce: nonce))
    #expect(!GatewayProof.verify(pingResponse: pingAnswer(Array(repeating: "00", count: 1_024) + [good]),
                                 token: token, nonce: nonce))
}

// MARK: Model downloads prove the Mac before every attempt (#341 audit P2)

private let filePath = "/phone/v1/models/ling/files/model.safetensors"

private func proved(_ request: URLRequest) -> StubReply {
    StubReply(status: 200, body: pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))]))
}

private func download(_ client: GatewayClient, port: Int, maxBytes: Int64 = 11,
                      reply: @escaping StubProtocol.ReplyHandler) async throws -> String {
    let url = try await VerifiedFileDownloader.download(
        gateway: client, model: "ling", file: "model.safetensors", maxBytes: maxBytes,
        configuration: StubProtocol.configuration(port: port, reply: reply), backoff: { _ in }, progress: { _, _ in })
    defer { try? FileManager.default.removeItem(at: url) }
    return try String(contentsOf: url, encoding: .utf8)
}

@Test func downloadRetryProvesAgainAndResumesOnTheSameBase() async throws {
    // The proof from hello() is still inside its window, and the retry still pings again.
    let port = 18_820
    let clock = Box(ContinuousClock.now), fileCalls = Box(0)
    let reply: StubProtocol.ReplyHandler = { request in
        switch request.url!.path {
        case "/phone/v1/ping": return proved(request)
        case "/phone/v1/hello": return StubReply(status: 200, body: helloBody())
        default:
            fileCalls.value += 1
            if fileCalls.value == 1 { return StubReply(status: 200, body: Data("hello ".utf8), dropAfterBody: true) }
            guard request.value(forHTTPHeaderField: "Range") == "bytes=6-" else { return StubReply(status: 400) }
            return StubReply(status: 206, headers: ["Content-Range": "bytes 6-10/11"], body: Data("world".utf8))
        }
    }
    let pairing = MacPairing(host: "mac.tail1.ts.net", ip: nil, port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)),
                               now: { clock.value })
    _ = try await client.hello()
    #expect(try await download(client, port: port, reply: reply) == "hello world")
    let seen = StubProtocol.requests(port: port)
    #expect(seen.map { $0.url!.path } == ["/phone/v1/ping", "/phone/v1/hello",
                                          "/phone/v1/ping", filePath, "/phone/v1/ping", filePath])
    let nonces = seen.filter { $0.url!.path == "/phone/v1/ping" }.map(nonce(of:))
    #expect(Set(nonces).count == 3)
    #expect(seen.filter { $0.url!.path == filePath }.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer \(token)" })
}

@Test func downloadRetryStopsWhenTheProofExpiresAndFails() async throws {
    // After the first attempt drops, the proof window has passed and a stranger answers.
    let port = 18_821
    let clock = Box(ContinuousClock.now), impostor = Box(false)
    let reply: StubProtocol.ReplyHandler = { request in
        if request.url!.path == "/phone/v1/ping" {
            return impostor.value ? StubReply(status: 200, body: pingAnswer([])) : proved(request)
        }
        impostor.value = true
        clock.value = clock.value.advanced(by: .seconds(GatewayClient.proofLifetime + 1))
        return StubReply(status: 200, body: Data("hello ".utf8), dropAfterBody: true)
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)),
                               now: { clock.value })
    await #expect(throws: GatewayError.unverified) { _ = try await download(client, port: port, reply: reply) }
    let seen = StubProtocol.requests(port: port)
    #expect(seen.map { $0.url!.path } == ["/phone/v1/ping", filePath, "/phone/v1/ping"])
    #expect(seen.last?.value(forHTTPHeaderField: "Authorization") == nil)
}

@Test func downloadRetryOnAChangedBaseStartsOver() async throws {
    // MagicDNS serves half the file, then stops answering; the IP proves and serves it whole.
    let port = 18_822
    let dnsDown = Box(false)
    let reply: StubProtocol.ReplyHandler = { request in
        let fromDNS = request.url!.host == "mac.tail1.ts.net"
        if fromDNS && dnsDown.value { return nil }
        if request.url!.path == "/phone/v1/ping" { return proved(request) }
        if fromDNS {
            dnsDown.value = true
            return StubReply(status: 200, body: Data("hello ".utf8), dropAfterBody: true)
        }
        guard request.value(forHTTPHeaderField: "Range") == nil else { return StubReply(status: 400) }
        return StubReply(status: 200, body: Data("hello world".utf8))
    }
    let pairing = MacPairing(host: "mac.tail1.ts.net", ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    #expect(try await download(client, port: port, reply: reply) == "hello world")
    let fileRequests = StubProtocol.requests(port: port).filter { $0.url!.path == filePath }
    #expect(fileRequests.map { $0.url!.host } == ["mac.tail1.ts.net", "100.64.0.1"])
    // The IP got the token only after its own ping proved it.
    let seen = StubProtocol.requests(port: port).map { "\($0.url!.host!) \($0.url!.path)" }
    let ipFile = seen.firstIndex(of: "100.64.0.1 \(filePath)")!
    #expect(seen[..<ipFile].contains("100.64.0.1 /phone/v1/ping"))
}

@Test func downloadStopsPastTheListedSize() async throws {
    let port = 18_823
    let reply: StubProtocol.ReplyHandler = { request in
        request.url!.path == "/phone/v1/ping" ? proved(request) : StubReply(status: 200, body: Data("hello world, and more".utf8))
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    await #expect(throws: VerifiedFileDownloader.Failure.oversized) { _ = try await download(client, port: port, reply: reply) }
}

@Test func downloadRejectsAResumeAtTheWrongOffset() async throws {
    let port = 18_824
    let fileCalls = Box(0)
    let reply: StubProtocol.ReplyHandler = { request in
        if request.url!.path == "/phone/v1/ping" { return proved(request) }
        fileCalls.value += 1
        if fileCalls.value == 1 { return StubReply(status: 200, body: Data("hello ".utf8), dropAfterBody: true) }
        return StubReply(status: 206, headers: ["Content-Range": "bytes 0-10/11"], body: Data("hello world".utf8))
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    await #expect(throws: VerifiedFileDownloader.Failure.badRange) { _ = try await download(client, port: port, reply: reply) }
}

@Test func downloadCancellationStopsWithoutRetrying() async throws {
    let port = 18_825
    let reply: StubProtocol.ReplyHandler = { request in
        request.url!.path == "/phone/v1/ping" ? proved(request)
            : StubReply(status: 200, body: Data("hello ".utf8), hangAfterBody: true)
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    let task = Task { try await download(client, port: port, reply: reply) }
    for _ in 0..<200 where !StubProtocol.requests(port: port).contains(where: { $0.url!.path == filePath }) {
        try await Task.sleep(for: .milliseconds(10))
    }
    task.cancel()
    await #expect(throws: CancellationError.self) { _ = try await task.value }
    #expect(StubProtocol.requests(port: port).map { $0.url!.path } == ["/phone/v1/ping", filePath])
}

@Test func backwardsInjectedClockForcesANewProof() async throws {
    let port = 18_826
    let clock = Box(ContinuousClock.now)
    let session = StubProtocol.session(port: port) { request in
        request.url!.path == "/phone/v1/ping"
            ? (200, pingAnswer([GatewayProof.proof(token: token, nonce: nonce(of: request))])) : (200, helloBody())
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: session, now: { clock.value })
    _ = try await client.hello()
    clock.value = clock.value.advanced(by: .seconds(-10))
    _ = try await client.hello()
    #expect(StubProtocol.requests(port: port).map { $0.url!.path }
            == ["/phone/v1/ping", "/phone/v1/hello", "/phone/v1/ping", "/phone/v1/hello"])
}

@Test func failedAgentStreamForgetsTheVerifiedBase() async throws {
    let port = 18_827
    let reply: StubProtocol.ReplyHandler = { request in
        switch request.url!.path {
        case "/phone/v1/ping": return proved(request)
        case "/phone/v1/agent":
            return StubReply(status: 200, headers: ["Content-Type": "text/event-stream"],
                             body: Data("data: {\"type\":\"delta\",\"text\":\"Hi\"}\n".utf8), dropAfterBody: true)
        default: return StubReply(status: 200, body: helloBody())
        }
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    var failed = false
    do { for try await _ in client.agent(prompt: "hi", sessionId: nil) {} } catch { failed = true }
    #expect(failed)
    _ = try await client.hello()
    #expect(StubProtocol.requests(port: port).map { $0.url!.path }
            == ["/phone/v1/ping", "/phone/v1/agent", "/phone/v1/ping", "/phone/v1/hello"])
}

// MARK: Transfer cancellation ordering (#341 audit P2, e044f54c)

private func transferFixture(port: Int, reply: @escaping StubProtocol.ReplyHandler)
    -> (VerifiedFileDownloader.Transfer, URLSession, URLRequest, URL) {
    let file = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-transfer-\(UUID().uuidString)")
    FileManager.default.createFile(atPath: file.path, contents: nil)
    let transfer = VerifiedFileDownloader.Transfer(file: file, maxBytes: 11, progress: { _, _ in })
    let session = URLSession(configuration: StubProtocol.configuration(port: port, reply: reply),
                             delegate: transfer, delegateQueue: nil)
    let request = URLRequest(url: URL(string: "http://100.64.0.1:\(port)\(filePath)")!)
    return (transfer, session, request, file)
}

/// Fails the test instead of hanging when `operation` never returns.
private func finishes<T: Sendable>(within seconds: Double = 5,
                                   _ operation: @escaping @Sendable () async throws -> T) async -> Result<T, Error>? {
    await withTaskGroup(of: Result<T, Error>?.self) { group in
        group.addTask { do { return .success(try await operation()) } catch { return .failure(error) } }
        group.addTask { try? await Task.sleep(for: .seconds(seconds)); return nil }
        let first = await group.next() ?? nil
        group.cancelAll()
        return first
    }
}

@Test func transferCancelledBeforeRegistrationFinishesWithoutStarting() async throws {
    // The cancel lands before the continuation exists: onCancel runs on entry to the handler.
    let port = 18_830
    let (transfer, session, request, file) = transferFixture(port: port) { _ in StubReply(status: 200, body: Data("x".utf8)) }
    defer { session.invalidateAndCancel(); try? FileManager.default.removeItem(at: file) }
    let outcome = await finishes {
        try await Task {
            withUnsafeCurrentTask { $0?.cancel() }
            try await transfer.run(session: session, request: request, offset: 0)
        }.value
    }
    guard case .failure(let error)? = outcome else { Issue.record("hung or succeeded: \(String(describing: outcome))"); return }
    #expect(error is CancellationError)
    #expect(StubProtocol.requests(port: port).isEmpty)
}

@Test func transferCancelledBetweenRegistrationAndStartFinishes() async throws {
    // The cancel lands after the continuation is registered but before the task is resumed.
    let port = 18_831
    let (transfer, session, request, file) = transferFixture(port: port) { _ in
        StubReply(status: 200, body: Data("x".utf8), hangAfterBody: true)
    }
    defer { session.invalidateAndCancel(); try? FileManager.default.removeItem(at: file) }
    transfer.beforeStart = { withUnsafeCurrentTask { $0?.cancel() } }
    let outcome = await finishes { try await transfer.run(session: session, request: request, offset: 0) }
    guard case .failure(let error)? = outcome else { Issue.record("hung or succeeded: \(String(describing: outcome))"); return }
    #expect((error as? URLError)?.code == .cancelled || error is CancellationError)
}

@Test func transferCancelledWhileRunningFinishes() async throws {
    let port = 18_832
    let (transfer, session, request, file) = transferFixture(port: port) { _ in
        StubReply(status: 200, body: Data("hello ".utf8), hangAfterBody: true)
    }
    defer { session.invalidateAndCancel(); try? FileManager.default.removeItem(at: file) }
    let outcome = await finishes {
        let task = Task { try await transfer.run(session: session, request: request, offset: 0) }
        for _ in 0..<200 where StubProtocol.requests(port: port).isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        task.cancel()
        return try await task.value
    }
    guard case .failure(let error)? = outcome else { Issue.record("hung or succeeded: \(String(describing: outcome))"); return }
    #expect((error as? URLError)?.code == .cancelled || error is CancellationError)
}

@Test func transferRunsAgainAfterACancelledAttempt() async throws {
    // A cancelled attempt leaves no state behind: the next run on the same Transfer completes.
    let port = 18_833
    let (transfer, session, request, file) = transferFixture(port: port) { _ in StubReply(status: 200, body: Data("hello".utf8)) }
    defer { session.invalidateAndCancel(); try? FileManager.default.removeItem(at: file) }
    _ = await finishes {
        try await Task {
            withUnsafeCurrentTask { $0?.cancel() }
            try await transfer.run(session: session, request: request, offset: 0)
        }.value
    }
    let outcome = await finishes { try await transfer.run(session: session, request: request, offset: 0) }
    guard case .success? = outcome else { Issue.record("second run: \(String(describing: outcome))"); return }
    #expect(transfer.received == 5)
}

@Test(arguments: ["eof", "failed"])
func uncleanAgentEndForgetsTheVerifiedBase(kind: String) async throws {
    // A stream that closes without `done`, or reports an error, makes the next request prove again.
    let port = kind == "eof" ? 18_834 : 18_835
    let line = kind == "eof" ? #"data: {"type":"delta","text":"Hi"}"# : #"data: {"type":"error","message":"boom"}"#
    let reply: StubProtocol.ReplyHandler = { request in
        switch request.url!.path {
        case "/phone/v1/ping": return proved(request)
        case "/phone/v1/agent":
            return StubReply(status: 200, headers: ["Content-Type": "text/event-stream"], body: Data((line + "\n").utf8))
        default: return StubReply(status: 200, body: helloBody())
        }
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    var events: [ChatEvent] = []
    for try await event in client.agent(prompt: "hi", sessionId: nil) { events.append(event) }
    #expect(events.contains { if case .failed = $0 { true } else { false } })
    _ = try await client.hello()
    #expect(StubProtocol.requests(port: port).map { $0.url!.path }
            == ["/phone/v1/ping", "/phone/v1/agent", "/phone/v1/ping", "/phone/v1/hello"])
}

@Test func cleanAgentEndKeepsTheVerifiedBase() async throws {
    let port = 18_836
    let reply: StubProtocol.ReplyHandler = { request in
        switch request.url!.path {
        case "/phone/v1/ping": return proved(request)
        case "/phone/v1/agent":
            return StubReply(status: 200, headers: ["Content-Type": "text/event-stream"], body: Data("data: {\"type\":\"done\"}\n".utf8))
        default: return StubReply(status: 200, body: helloBody())
        }
    }
    let pairing = MacPairing(host: nil, ip: "100.64.0.1", port: port, macName: "Mac", token: token)
    let client = GatewayClient(pairing: pairing, session: URLSession(configuration: StubProtocol.configuration(port: port, reply: reply)))
    for try await _ in client.agent(prompt: "hi", sessionId: nil) {}
    _ = try await client.hello()
    #expect(StubProtocol.requests(port: port).map { $0.url!.path } == ["/phone/v1/ping", "/phone/v1/agent", "/phone/v1/hello"])
}
