import Foundation

final class DeliveryMock: URLProtocol {
    static var handler: (URLRequest) -> (Int, [String: Any]) = { _ in (500, [:]) }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, body) = Self.handler(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
struct AssistantDeliveryChecks {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        for malformed in ["data: {", "data: {}", "data: {\"type\":\"\"}",
                          "data: {\"type\":\"reminder\",\"event_id\":7}"] {
            precondition(WispClient.Event.decodeAssistantLine(malformed) == nil)
        }
        precondition(WispClient.Event.decodeAssistantLine("data: {\"type\":\"changed\",\"event_id\":\"fixture\"}")?.type == "changed")
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [DeliveryMock.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let client = WispClient(deliverySession: session, deliveryURL: URL(string: "http://offline.fixture")!)
        let event = WispClient.Event(type: "reminder", payload: ["event_id": "notice-1"])
        var handled = 0, posts = 0
        DeliveryMock.handler = { request in
            precondition(request.url!.host == "offline.fixture")
            posts += 1
            return (503, ["ok": false])
        }
        let path = root.appendingPathComponent("receipts.json")
        var delivery = AssistantDelivery(path: path)
        let first = await delivery.handle(event, client: client, perform: { _ in handled += 1; return true }, calendar: { _ in fatalError() })
        precondition(!first && handled == 1 && posts == 1)
        // Restart after a lost ACK retains identity, never repeats handling.
        delivery = AssistantDelivery(path: path)
        DeliveryMock.handler = { request in
            posts += 1
            precondition(request.url!.path.hasSuffix("/ack"))
            return (200, ["ok": true])
        }
        let replay = await delivery.handle(event, client: client, perform: { _ in handled += 1; return true }, calendar: { _ in fatalError() })
        precondition(replay && handled == 1 && posts == 2)
        let failed = WispClient.Event(type: "reminder", payload: ["event_id": "notice-2"])
        let failure = await delivery.handle(failed, client: client, perform: { _ in false }, calendar: { _ in fatalError() })
        precondition(!failure && posts == 2)
        let undecodable = WispClient.Event(type: "future-unknown", payload: ["event_id": "notice-3"])
        let unknown = await delivery.handle(undecodable, client: client, perform: { _ in false }, calendar: { _ in fatalError() })
        precondition(!unknown && posts == 2)
        let calendar = WispClient.Event(type: "create_calendar_event", payload: ["event_id": "cal-1", "action_id": "act-1"])
        var executions = 0, claims = 0, results = 0, acks = 0
        var resultAvailable = false
        DeliveryMock.handler = { request in
            if request.url!.path.hasSuffix("/claim") {
                claims += 1
                return (200, ["execute": true, "claim_token": "exclusive-token"])
            }
            if request.url!.path.hasSuffix("action_result") {
                results += 1
                return (resultAvailable ? 200 : 503, ["ok": resultAvailable])
            }
            acks += 1
            return (200, ["ok": true])
        }
        let native = await delivery.handle(calendar, client: client, perform: { _ in fatalError() }, calendar: { _ in
            executions += 1
            return ["ok": true, "source_id": "native-fixture"]
        })
        precondition(!native && executions == 1 && claims == 1 && results == 1 && acks == 0)
        delivery = AssistantDelivery(path: path)
        resultAvailable = true
        let nativeReplay = await delivery.handle(calendar, client: client, perform: { _ in fatalError() }, calendar: { _ in executions += 1; return [:] })
        precondition(nativeReplay && executions == 1 && claims == 1 && results == 2 && acks == 1)
        let duplicate = await delivery.handle(calendar, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
        precondition(duplicate && results == 2 && acks == 2)
        // Interrupted native execution reports unknown, never executes twice.
        let crashPath = root.appendingPathComponent("crash.json")
        try JSONSerialization.data(withJSONObject: ["cal-1": ["kind": "create_calendar_event", "claim_token": "exclusive-token", "started": true]])
            .write(to: crashPath)
        DeliveryMock.handler = { request in
            if request.url!.path.hasSuffix("action_result") {
                // URLProtocol may expose a body stream instead of httpBody.
                return (200, ["ok": true])
            }
            return (200, ["ok": true])
        }
        let recovered = await AssistantDelivery(path: crashPath).handle(calendar, client: client, perform: { event in
            precondition(event.type == "calendar_action_unknown")
            return true
        }, calendar: { _ in fatalError() })
        precondition(recovered)
        // An unreadable receipt file is retained and blocks delivery.
        let badPath = root.appendingPathComponent("corrupt.json")
        try Data("not json".utf8).write(to: badPath)
        let corrupt = await AssistantDelivery(path: badPath).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
        let preserved = try String(contentsOf: badPath, encoding: .utf8)
        precondition(!corrupt && preserved == "not json")
        // A stranded claim is shown as unknown and never acknowledged as a success.
        var unknownShown = 0
        DeliveryMock.handler = { request in
            precondition(request.url!.path.hasSuffix("/claim"))
            return (200, ["execute": false, "error": "native outcome unknown"])
        }
        let stranded = WispClient.Event(type: "create_calendar_event", payload: ["event_id": "stranded", "action_id": "act-stranded"])
        let strandedResult = await delivery.handle(stranded, client: client, perform: { event in
            precondition(event.type == "calendar_action_unknown")
            unknownShown += 1
            return true
        }, calendar: { _ in fatalError() })
        precondition(!strandedResult && unknownShown == 1)
        delivery = AssistantDelivery(path: path)
        let strandedReplay = await delivery.handle(stranded, client: client,
            perform: { _ in unknownShown += 1; return true }, calendar: { _ in fatalError() })
        precondition(!strandedReplay && unknownShown == 1)
        print("AssistantDelivery: 12 offline receipt/transport scenarios passed")
    }
}
