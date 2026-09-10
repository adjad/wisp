import Foundation

final class DeliveryMock: URLProtocol {
    enum Reply { case json(Int, [String: Any]), corrupt, disconnected }
    static var handler: (URLRequest) -> Reply = { _ in .json(500, [:]) }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    static func body(_ request: URLRequest) -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&bytes, maxLength: bytes.count)
                if n <= 0 { break }
                data.append(contentsOf: bytes.prefix(n))
            }
        }
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }
    override func startLoading() {
        precondition(request.url!.host == "offline.fixture")
        let status: Int, data: Data
        switch Self.handler(request) {
        case .json(let code, let body): status = code; data = try! JSONSerialization.data(withJSONObject: body)
        case .corrupt: status = 200; data = Data("{broken transport".utf8)
        case .disconnected:
            client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost)); return
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

final class ReceiptServer {
    let event: WispClient.Event
    var payload: [String: Any] { event.payload.filter { $0.key != "event_id" } }
    var claimed = false
    var result: [String: Any]?
    var claimPosts = 0, resultPosts = 0, ackPosts = 0
    var badResult: DeliveryMock.Reply?
    var badClaim: DeliveryMock.Reply?
    var badAck: DeliveryMock.Reply?
    init(_ event: WispClient.Event) { self.event = event }
    var identity: [String: Any] { ["event_id": event.str("event_id"), "action_id": event.str("action_id"), "kind": event.type] }
    func respond(_ request: URLRequest) -> DeliveryMock.Reply {
        let body = DeliveryMock.body(request)
        if request.url!.path.hasSuffix("/claim") {
            claimPosts += 1
            guard body["action_id"] as? String == event.str("action_id"),
                  body["kind"] as? String == event.type,
                  let proposed = body["payload"] as? NSDictionary, proposed.isEqual(to: payload) else { return .json(409, [:]) }
            if let badClaim { return badClaim }
            var response = identity
            response["payload"] = payload
            response["execute"] = !claimed && result == nil
            response["recorded"] = result != nil
            if let result { response["result"] = result }
            else if claimed { response["error"] = "native outcome unknown" }
            else { response["claim_token"] = "exclusive-token"; claimed = true }
            return .json(200, response)
        }
        if request.url!.path.hasSuffix("action_result") {
            resultPosts += 1
            precondition(body["event_id"] as? String == event.str("event_id"))
            precondition(body["action_id"] as? String == event.str("action_id"))
            precondition(body["kind"] as? String == event.type)
            precondition(body["claim_token"] as? String == "exclusive-token")
            if let badResult { return badResult }
            result = body["result"] as? [String: Any]
            precondition(result != nil)
            return .json(200, identity.merging(["ok": true, "recorded": true]) { _, new in new })
        }
        precondition(request.url!.path.hasSuffix("/ack"))
        ackPosts += 1
        if let badAck { return badAck }
        precondition(!event.type.contains("calendar_event") || result != nil)
        return .json(200, ["ok": true, "event_id": event.str("event_id"), "kind": event.type])
    }
}

@main
struct AssistantDeliveryChecks {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [DeliveryMock.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let client = WispClient(deliverySession: session, deliveryURL: URL(string: "http://offline.fixture")!)
        var scenarios = 0
        func path() -> URL { root.appendingPathComponent(UUID().uuidString + ".json") }
        func read(_ path: URL) -> [String: [String: Any]] {
            (try! JSONSerialization.jsonObject(with: Data(contentsOf: path))) as! [String: [String: Any]]
        }
        func write(_ path: URL, _ receipts: [String: [String: Any]]) throws {
            try JSONSerialization.data(withJSONObject: receipts).write(to: path)
        }
        func calendarEvent(_ id: String = "cal-1") -> WispClient.Event {
            .init(type: "create_calendar_event", payload: ["type": "create_calendar_event", "event_id": id,
                "action_id": "action-" + id, "title": "Fixture lunch", "when_ts": 1900000000.0,
                "duration_min": 60, "location": ""])
        }
        let note = WispClient.Event(type: "reminder", payload: ["type": "reminder", "event_id": "r1"])
        for malformed in ["data: {", "data: {}", "data: {\"type\":\"\"}", "data: {\"type\":\"reminder\",\"event_id\":1}"] {
            precondition(WispClient.Event.decodeAssistantLine(malformed) == nil)
        }
        precondition(WispClient.Event.decodeAssistantLine("data: {\"type\":\"changed\",\"event_id\":\"fixture\"}")?.type == "changed")
        scenarios += 1
        // Failed handling and failed ACK must leave retryable state; restart
        // after successful handling never repeats the notification effect.
        do {
            let server = ReceiptServer(note), file = path()
            DeliveryMock.handler = server.respond
            var performed = 0
            var delivery = AssistantDelivery(path: file)
            let failed = await delivery.handle(note, client: client, perform: { _ in performed += 1; return false }, calendar: { _ in fatalError() })
            precondition(!failed && server.ackPosts == 0)
            server.badAck = .disconnected
            let lost = await delivery.handle(note, client: client, perform: { _ in performed += 1; return true }, calendar: { _ in fatalError() })
            precondition(!lost && performed == 2 && server.ackPosts == 1)
            delivery = AssistantDelivery(path: file); server.badAck = nil
            let retried = await delivery.handle(note, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(retried && server.ackPosts == 2)
            scenarios += 1
        }
        // F5 exact audit case and structural corruption variants. No malformed
        // done value authorizes skipping handling or ACKing failed handling.
        let corruptDone: [Any] = [1, 0, "true", "false", NSNull(), [], ["value": true]]
        for value in corruptDone {
            let server = ReceiptServer(note), file = path()
            DeliveryMock.handler = server.respond
            try write(file, ["r1": ["kind": "reminder", "done": value]])
            let before = try Data(contentsOf: file)
            var performed = 0
            let failed = await AssistantDelivery(path: file).handle(note, client: client,
                perform: { _ in performed += 1; return false }, calendar: { _ in fatalError() })
            precondition(!failed && performed == 1 && server.ackPosts == 0)
            let after = try Data(contentsOf: file)
            precondition(before == after)
            let fixed = await AssistantDelivery(path: file).handle(note, client: client,
                perform: { _ in performed += 1; return true }, calendar: { _ in fatalError() })
            precondition(fixed && performed == 2 && server.ackPosts == 1)
            precondition(AssistantDelivery.jsonBoolean(read(file)["r1"]?["done"]) == true)
            scenarios += 1
        }
        for done in [true, false] {
            let server = ReceiptServer(note), file = path()
            DeliveryMock.handler = server.respond
            try write(file, ["r1": ["kind": "reminder", "done": done]])
            var performed = 0
            let accepted = await AssistantDelivery(path: file).handle(note, client: client,
                perform: { _ in performed += 1; return false }, calendar: { _ in fatalError() })
            precondition(accepted == done && performed == (done ? 0 : 1) && server.ackPosts == (done ? 1 : 0))
            scenarios += 1
        }
        for receipt: [String: Any] in [["kind": "reminder"], ["kind": "reminder", "done": true, "started": true]] {
            let server = ReceiptServer(note), file = path()
            DeliveryMock.handler = server.respond
            try write(file, ["r1": receipt])
            var performed = 0
            let rejected = await AssistantDelivery(path: file).handle(note, client: client,
                perform: { _ in performed += 1; return false }, calendar: { _ in fatalError() })
            precondition(!rejected && performed == 1 && server.ackPosts == 0)
            scenarios += 1
        }
        // F6 retained native result survives unrecorded/mismatched/corrupt
        // replies. Corrected replay re-posts it, without another native effect.
        let event = calendarEvent()
        let matching: [String: Any] = ["ok": true, "recorded": true, "event_id": "cal-1", "action_id": "action-cal-1", "kind": event.type]
        let badResults: [DeliveryMock.Reply] = [
            .json(200, ["ok": true, "recorded": false, "delivered": false]),
            .json(200, matching.merging(["event_id": "wrong"]) { _, n in n }),
            .json(200, matching.merging(["action_id": "wrong"]) { _, n in n }),
            .json(200, matching.merging(["kind": "wrong"]) { _, n in n }),
            .json(200, matching.merging(["recorded": 1]) { _, n in n }),
            .json(200, matching.merging(["ok": 1]) { _, n in n }),
            .json(200, matching.merging(["ok": "true"]) { _, n in n }),
            .json(200, matching.merging(["recorded": "true"]) { _, n in n }),
            .json(200, ["ok": true]), .json(503, matching), .corrupt, .disconnected]
        for reply in badResults {
            let server = ReceiptServer(event), file = path()
            server.badResult = reply; DeliveryMock.handler = server.respond
            var executions = 0
            let first = await AssistantDelivery(path: file).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in
                executions += 1
                precondition(AssistantDelivery.jsonBoolean(read(file)["cal-1"]?["started"]) == true)
                return ["ok": true, "source_id": "native-fixture"]
            })
            precondition(!first && executions == 1 && server.resultPosts == 1 && server.ackPosts == 0)
            let receipt = read(file)["cal-1"]!
            precondition(receipt["payload"] == nil && (receipt["payload_digest"] as? String)?.count == 64)
            let savedText = try String(contentsOf: file, encoding: .utf8)
            precondition(!savedText.contains("Fixture lunch"))
            precondition(receipt["claim_token"] as? String == "exclusive-token" && receipt["result"] is [String: Any] && receipt["done"] == nil)
            // A wrong replay identity cannot submit/ACK the cached callback.
            var wrong = event.payload; wrong["action_id"] = "wrong"
            let mismatch = await AssistantDelivery(path: file).handle(.init(type: event.type, payload: wrong), client: client,
                perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(!mismatch && server.resultPosts == 1 && server.ackPosts == 0)
            var changedPayload = event.payload; changedPayload["title"] = "Another meeting"
            let changed = await AssistantDelivery(path: file).handle(.init(type: event.type, payload: changedPayload), client: client,
                perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(!changed && server.resultPosts == 1 && server.ackPosts == 0)
            server.badResult = nil
            let fixed = await AssistantDelivery(path: file).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(fixed && executions == 1 && server.resultPosts == 2 && server.ackPosts == 1)
            let duplicate = await AssistantDelivery(path: file).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(duplicate && server.resultPosts == 2 && server.ackPosts == 2)
            scenarios += 1
        }
        // Payload and identity are checked before invoking the native callback.
        let serverIdentity = ReceiptServer(event)
        var grant = serverIdentity.identity
        grant["payload"] = serverIdentity.payload; grant["execute"] = true; grant["recorded"] = false; grant["claim_token"] = "exclusive-token"
        let badClaims: [[String: Any]] = [
            grant.merging(["action_id": "wrong"]) { _, n in n },
            grant.merging(["event_id": "wrong"]) { _, n in n },
            grant.merging(["execute": 1]) { _, n in n },
            grant.merging(["recorded": 0]) { _, n in n },
            grant.merging(["payload": serverIdentity.payload.merging(["title": "Wrong target"]) { _, n in n }]) { _, n in n },
            grant.merging(["claim_token": ""]) { _, n in n }]
        for bad in badClaims {
            let server = ReceiptServer(event), file = path()
            server.badClaim = .json(200, bad); DeliveryMock.handler = server.respond
            let rejected = await AssistantDelivery(path: file).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(!rejected && server.resultPosts == 0 && server.ackPosts == 0)
            server.badClaim = nil
            let corrected = await AssistantDelivery(path: file).handle(event, client: client, perform: { _ in fatalError() }, calendar: { _ in ["ok": true, "source_id": "fixture"] })
            precondition(corrected && server.resultPosts == 1 && server.ackPosts == 1)
            scenarios += 1
        }
        for badTerminal: [String: Any] in [["ok": 1, "status": "succeeded", "error": "", "source_id": "fixture"],
            ["ok": true, "error": "", "source_id": "fixture"],
            ["ok": true, "status": "failed", "error": "native failed", "source_id": "fixture"]] {
            let server = ReceiptServer(event), file = path()
            var bad = grant
            bad["execute"] = false; bad["recorded"] = true; bad["result"] = badTerminal
            server.badClaim = .json(200, bad); DeliveryMock.handler = server.respond
            let rejected = await AssistantDelivery(path: file).handle(event, client: client,
                perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(!rejected && server.resultPosts == 0 && server.ackPosts == 0)
            server.badClaim = nil; server.result = ["ok": true, "status": "succeeded", "error": "", "source_id": "fixture"]
            let corrected = await AssistantDelivery(path: file).handle(event, client: client,
                perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(corrected && server.resultPosts == 0 && server.ackPosts == 1)
            scenarios += 1
        }
        for badAck in [DeliveryMock.Reply.json(200, ["ok": 1, "event_id": "r1", "kind": "reminder"]),
                       .json(200, ["ok": true, "event_id": "wrong", "kind": "reminder"]), .corrupt] {
            let server = ReceiptServer(note), file = path()
            server.badAck = badAck; DeliveryMock.handler = server.respond
            let rejected = await AssistantDelivery(path: file).handle(note, client: client, perform: { _ in true }, calendar: { _ in fatalError() })
            precondition(!rejected)
            server.badAck = nil
            let replay = await AssistantDelivery(path: file).handle(note, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(replay && server.ackPosts == 2)
            scenarios += 1
        }
        // Interrupted native receipt and lost claim both show durable unknown
        // notices; neither recovery repeats native execution.
        for started in [true, false] {
            let server = ReceiptServer(event), file = path()
            server.claimed = true; DeliveryMock.handler = server.respond
            if started { try write(file, ["cal-1": ["kind": event.type, "claim_token": "exclusive-token", "started": true]]) }
            var notices = 0
            for _ in 0..<2 {
                let recovered = await AssistantDelivery(path: file).handle(event, client: client,
                    perform: { ev in precondition(ev.type == "calendar_action_unknown"); notices += 1; return true }, calendar: { _ in fatalError() })
                precondition(recovered == started)
            }
            precondition(notices == 1 && server.resultPosts == (started ? 1 : 0))
            if started { precondition(server.result?["status"] as? String == "unknown") }
            scenarios += 1
        }
        // Legacy crash results remain unknown, both before and after the
        // backend has migrated its terminal receipt.
        for recorded in [false, true] {
            let server = ReceiptServer(event), file = path()
            let legacy: [String: Any] = ["ok": false, "error": "Wisp was interrupted; native Calendar outcome is unknown"]
            server.claimed = true
            if recorded { server.result = legacy.merging(["status": "unknown"]) { _, new in new } }
            DeliveryMock.handler = server.respond
            try write(file, ["cal-1": ["kind": event.type, "claim_token": "exclusive-token", "result": legacy]])
            let recovered = await AssistantDelivery(path: file).handle(event, client: client,
                perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            precondition(recovered && server.result?["status"] as? String == "unknown")
            precondition(server.resultPosts == (recorded ? 0 : 1) && server.ackPosts == 1)
            scenarios += 1
        }
        do {
            let file = path(), data = Data("not JSON".utf8)
            try data.write(to: file)
            let corrupt = await AssistantDelivery(path: file).handle(note, client: client, perform: { _ in fatalError() }, calendar: { _ in fatalError() })
            let preserved = try Data(contentsOf: file)
            precondition(!corrupt && preserved == data)
            scenarios += 1
        }
        print("AssistantDelivery: \(scenarios) offline audit/receipt/transport scenarios passed")
    }
}
