// Compile with app/Sources/WispApp/WispClient.swift and AgentSend.swift.
// Drives WispClient.runAgent through a stub URLProtocol that answers every
// request in-process: nothing reaches a socket, least of all the live backend.
import Foundation

/// Scripted answers, one per request, in order.
enum Reply {
    case status(Int)
    case fail(URLError.Code)
    case stream([String], end: URLError.Code? = nil)
}

final class StubProtocol: URLProtocol {
    nonisolated(unsafe) static var script: [Reply] = []
    nonisolated(unsafe) static var requests = 0

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests += 1
        let reply = Self.script.isEmpty ? .status(599) : Self.script.removeFirst()
        let url = request.url!
        switch reply {
        case .fail(let code):
            client?.urlProtocol(self, didFailWithError: URLError(code))
        case .status(let code):
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: code, httpVersion: nil, headerFields: nil)!,
                                cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data("{\"detail\":\"no\"}".utf8))
            client?.urlProtocolDidFinishLoading(self)
        case .stream(let events, let end):
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil,
                                                                  headerFields: ["Content-Type": "text/event-stream"])!,
                                cacheStoragePolicy: .notAllowed)
            for event in events { client?.urlProtocol(self, didLoad: Data("data: \(event)\n\n".utf8)) }
            if let end { client?.urlProtocol(self, didFailWithError: URLError(end)) }
            else { client?.urlProtocolDidFinishLoading(self) }
        }
    }

    override func stopLoading() {}
}

final class Events: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [WispClient.Event] = []
    func add(_ event: WispClient.Event) { lock.lock(); items.append(event); lock.unlock() }
    var all: [WispClient.Event] { lock.lock(); defer { lock.unlock() }; return items }
}

@main
enum AgentSendChecks {
    static var passed = 0

    static func check(_ ok: Bool, _ message: String) {
        guard ok else { print("FAIL: \(message)"); exit(1) }
        passed += 1
    }

    static func run(_ script: [Reply]) async -> (events: [WispClient.Event], requests: Int) {
        StubProtocol.script = script
        StubProtocol.requests = 0
        let events = Events()
        await WispClient().runAgent(prompt: "hi", image: nil) { events.add($0) }
        return (events.all, StubProtocol.requests)
    }

    static func main() async {
        URLProtocol.registerClass(StubProtocol.self)
        let done = #"{"type":"done"}"#, text = #"{"type":"text","text":"hello"}"#

        // A 5xx before the stream: the handler never ran, so it is sent once more.
        var r = await run([.status(503), .stream([text, done])])
        check(r.requests == 2 && r.events.map(\.type) == ["text", "done"], "503 then a reply: sent again, answered")
        r = await run([.status(503), .status(503)])
        check(r.requests == 2 && r.events.count == 1 && r.events[0].type == "error"
              && r.events[0].bool("unsent") && r.events[0].str("message").contains("503"),
              "503 twice: one retry, then it says so and the prompt is unsent")
        // A 4xx is the request itself: not sent again.
        r = await run([.status(422)])
        check(r.requests == 1 && r.events.first?.bool("unsent") == true, "422: no retry")
        // No connection, or a pooled socket the server had closed: sent again.
        r = await run([.fail(.cannotConnectToHost), .stream([done])])
        check(r.requests == 2 && r.events.map(\.type) == ["done"], "refused, then answered")
        r = await run([.fail(.networkConnectionLost), .fail(.networkConnectionLost)])
        check(r.requests == 2 && r.events.first?.str("message") == "Couldn't reach Wisp." && r.events.first?.bool("unsent") == true,
              "a stale socket twice: one retry, then a visible reason")
        r = await run([.fail(.timedOut)])
        check(r.requests == 1 && r.events.first?.bool("dropped") == true, "a timeout is not provably unsent: no retry")
        // Once a 200 stream started, a tool may have run: never sent again.
        r = await run([.stream([text]), .stream([done])])
        check(r.requests == 1 && r.events.map(\.type) == ["text", "error"] && r.events[1].bool("dropped")
              && !r.events[1].bool("unsent"), "a stream that ends without a terminal: dropped, never resent")
        r = await run([.stream([], end: .networkConnectionLost), .stream([done])])
        check(r.requests == 1 && r.events.first?.bool("dropped") == true && r.events.first?.bool("unsent") == false,
              "a stream that breaks after 200: never resent")
        r = await run([.stream([], end: nil)])
        check(r.requests == 1 && r.events.first?.str("message") == "The assistant didn't respond.", "an empty 200 stream")

        // A turn that failed only because the model isn't ready is not an error.
        check(AskNotReady.classify(code: "setup_needed", message: "") == .setup, "setup_needed opens setup")
        check(AskNotReady.classify(code: "model_loading", message: "") == .starting, "model_loading waits")
        check(AskNotReady.classify(code: "engine_starting", message: "") == .starting, "engine_starting waits")
        check(AskNotReady.classify(code: "", message: "Wisp's model isn't downloaded yet. Finish setup from the menu bar.") == .setup,
              "the not-downloaded message opens setup")
        check(AskNotReady.classify(code: "", message: "The local AI engine isn't running. Wisp is trying to restart it — try again in a few seconds.") == .starting,
              "the restarting-engine message waits")
        check(AskNotReady.classify(code: "", message: "The bundled AI engine did not become ready") == .starting,
              "an engine that isn't ready yet waits")
        check(AskNotReady.classify(code: "", message: "That request took too long and timed out.") == nil,
              "a real failure stays an error")
        check(AskNotReady.setup.notice(willRetry: false).contains("Finish setup")
              && AskNotReady.starting.notice(willRetry: true).contains("again in a few seconds")
              && !AskNotReady.starting.notice(willRetry: false).lowercased().contains("error"),
              "the notices say plainly what happens next")

        // Ask's starter questions work on a fresh install with nothing allowed.
        check(AskStarters.prompts(unreadable: []) == ["What can you do?", "What's on my calendar today?",
                                                      "Any emails I need to reply to?", "Is my data private?"],
              "four starters when Calendar and Mail can be read")
        let none = AskStarters.prompts(unreadable: ["calendar", "email"])
        check(none == ["What can you do?", "Is my data private?", "How do I use Wisp?"],
              "nothing that needs a denied source: \(none)")
        check(!AskStarters.prompts(unreadable: ["email"]).contains("Any emails I need to reply to?")
              && AskStarters.prompts(unreadable: ["email"]).count == 4, "a denied Mail swaps its starter out")
        check(AskStarters.unreadable(states: ["calendar": "unavailable", "email": "syncing", "notes": "disabled"])
              == ["calendar", "notes"], "unavailable and turned-off sources are unreadable; syncing is not")
        check(AskStarters.prompts(unreadable: []).allSatisfy { !$0.lowercased().contains("organize") && !$0.contains("/corpus") },
              "no dead or developer starters")

        print("AgentSend: \(passed) checks passed")
    }
}
