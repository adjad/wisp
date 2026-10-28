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

        // "Try another": the starters not yet asked in this chat.
        let all = AskStarters.prompts(unreadable: [])
        check(AskStarters.remaining(all, askedTexts: []) == all, "nothing asked: every starter remains")
        check(AskStarters.remaining(all, askedTexts: ["What can you do?"]) == Array(all.dropFirst()),
              "an asked starter drops out, order kept")
        check(AskStarters.remaining(all, askedTexts: ["  what CAN you do?\n", "unrelated question"]) == Array(all.dropFirst()),
              "matching ignores case and whitespace; other questions change nothing")
        check(AskStarters.remaining(all, askedTexts: all).isEmpty, "every starter asked: none remain")
        check(AskStarters.remaining([], askedTexts: ["x"]).isEmpty, "no starters: none remain")
        check(Set(AskStarters.chatWindow).isSuperset(of: ["What can you do?", "Is my data private?"])
              && AskStarters.remaining(AskStarters.chatWindow, askedTexts: ["Is my data private?"]).count
                  == AskStarters.chatWindow.count - 1, "the chat window's starters shrink the same way")

        // Before the service has said what it can read, no starter is about a source.
        check(AskStarters.prompts(unreadable: nil) == ["What can you do?", "Is my data private?", "How do I use Wisp?"],
              "access not known yet: no source starters")
        check(AskStarters.chatStarters(unreadable: nil) == ["What can you do?", "Is my data private?"],
              "the chat window waits too")
        check(AskStarters.chatStarters(unreadable: []) == AskStarters.chatWindow, "everything readable: every chat starter")
        check(AskStarters.chatStarters(unreadable: ["email"])
              == ["What can you do?", "What's on my calendar this week?", "Is my data private?"],
              "a denied Mail drops both mail starters from the chat window")

        // Personal starters, from what Wisp already holds.
        let me = AskStarters.Personal(nextEvent: "Design review", sender: "Maya Chen", senderSource: "email",
                                      goal: "Run a half marathon")
        check(AskStarters.prompts(unreadable: [], personal: me)
              == ["What can you do?", "What should I prep for Design review?",
                  "Anything from Maya Chen I need to answer?", "Help me with my goal: Run a half marathon"],
              "event, sender and goal make the starters personal")
        check(AskStarters.prompts(unreadable: ["calendar", "email"], personal: me)
              == ["What can you do?", "Help me with my goal: Run a half marathon", "Is my data private?",
                  "How do I use Wisp?"],
              "a denied source hides its personal starter too")
        check(AskStarters.prompts(unreadable: nil, personal: me).count == 4
              && !AskStarters.prompts(unreadable: nil, personal: me).contains { $0.contains("Maya") || $0.contains("Design") },
              "nothing personal about a source before access is known")
        check(AskStarters.goalStarterAllowed(cloudInUse: false) && !AskStarters.goalStarterAllowed(cloudInUse: true)
              && !AskStarters.goalStarterAllowed(cloudInUse: nil),
              "the goal starter only while no cloud model gets Ask's prompts, and not while that's unknown")
        var texted = me
        texted.senderSource = "messages"
        check(AskStarters.prompts(unreadable: ["email"], personal: texted).contains("Anything from Maya Chen I need to answer?"),
              "a waiting text still counts while Mail is denied")
        check(AskStarters.prompts(unreadable: ["messages"], personal: texted)
              .contains("Any emails I need to reply to?"), "texts denied: the general mail starter instead")
        check(AskStarters.prompts(unreadable: [], personal: AskStarters.Personal(nextEvent: String(repeating: "x", count: 60)))[1]
              == "What should I prep for \(String(repeating: "x", count: 27))…?", "a long title is clipped")

        let now = 1_000_000.0
        check(AskStarters.nextEvent([(title: "Later", start: now + 7200, allDay: false),
                                     (title: "Holiday", start: now + 60, allDay: true),
                                     (title: "  ", start: now + 120, allDay: false),
                                     (title: "Started", start: now - 60, allDay: false),
                                     (title: "Standup", start: now + 1800, allDay: false),
                                     (title: "Next week", start: now + 90_000, allDay: false)], now: now) == "Standup",
              "the soonest timed, titled event that hasn't started, within a day")
        check(AskStarters.nextEvent([], now: now).isEmpty, "no events: no prep starter")

        // The goal starter's cloud answer is per open: a new open forgets the
        // last "no", and an answer to an older open is dropped.
        var cloud = StarterCloudUse()
        let first = cloud.opened()
        cloud.answered(false, for: first)
        check(cloud.inUse == false, "the open's own answer lands")
        let second = cloud.opened()
        check(cloud.inUse == nil && !AskStarters.goalStarterAllowed(cloudInUse: cloud.inUse),
              "a reopened panel shows no goal starter until cloud use is read again")
        cloud.answered(false, for: first)
        check(cloud.inUse == nil, "a late answer to the previous open is dropped")
        cloud.answered(true, for: second)
        check(cloud.inUse == true && !AskStarters.goalStarterAllowed(cloudInUse: cloud.inUse),
              "cloud turned on since: no goal starter")
        check(AskStarters.waitingSender([(sender: "Newsletter", source: "email", person: false, replied: false),
                                         (sender: "Sam", source: "messages", person: true, replied: true),
                                         (sender: "a@b.com", source: "email", person: true, replied: false),
                                         (sender: " Priya ", source: "email", person: true, replied: false)])?.name
              == "Priya", "only a person still waiting, by name, never a bare address")
        check(AskStarters.waitingSender([(sender: "+1 (555) 010-2030", source: "messages", person: true, replied: false),
                                         (sender: "Sam 2", source: "messages", person: true, replied: false)])?.name
              == "Sam 2", "a bare phone number is not a name; a name with a digit still is")
        check(AskStarters.waitingSender([]) == nil, "nobody waiting")
        check(AskStarters.greeting(name: "Adi Jain") == "Hi, Adi." && AskStarters.greeting(name: "  ") == nil,
              "the greeting uses the first name, and only when there is one")

        // The fix button's signal comes from the tool result, never the reply text.
        check(AskAccess.kinds([["source": "calendar", "kind": "calendar_access"],
                               ["source": "calendar", "kind": "calendar_access"],
                               ["source": "messages", "kind": "full_disk_access"], ["kind": ""], ["source": "x"]])
              == ["calendar_access", "full_disk_access"], "kinds in order, each once, malformed skipped")
        check(AskAccess.kinds(nil).isEmpty && AskAccess.kinds("calendar_access").isEmpty, "no payload: no button")

        print("AgentSend: \(passed) checks passed")
    }
}
