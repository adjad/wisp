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
