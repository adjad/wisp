import Foundation
import CoreFoundation
import CryptoKit

/// Atomic local receipts precede ACKs. Calendar effects require an identity-
/// and payload-bound claim; a cached result survives every unconfirmed POST.
@MainActor
final class AssistantDelivery {
    private let path: URL
    private var receipts: [String: [String: Any]]
    private var inFlight: Set<String> = []
    private var readable = true

    // NSNumber(1) bridges to Bool on macOS. Only JSON's actual Boolean type
    // can authorize a state transition, including after decoding local files.
    static func jsonBoolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    private static func text(_ value: Any?) -> String? {
        guard let value = value as? String,
              !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return value
    }

    private static func number(_ value: Any?) -> Double? {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
              value.doubleValue.isFinite else { return nil }
        return value.doubleValue
    }

    static func calendarPayload(_ value: [String: Any], kind: String) -> [String: Any]? {
        let create = kind == "create_calendar_event"
        guard create || kind == "delete_calendar_event" else { return nil }
        let keys: Set<String> = create
            ? ["type", "action_id", "title", "when_ts", "duration_min", "location"]
            : ["type", "action_id", "source_id", "when_ts"]
        guard Set(value.keys) == keys, value["type"] as? String == kind,
              text(value["action_id"]) != nil, let when = number(value["when_ts"]), when > 0 else { return nil }
        var result = value
        result["when_ts"] = when
        if create {
            guard text(value["title"]) != nil, value["location"] is String,
                  let duration = number(value["duration_min"]), (1...10080).contains(duration),
                  duration.rounded() == duration else { return nil }
            result["duration_min"] = Int(duration)
        } else if text(value["source_id"]) == nil { return nil }
        return result
    }

    /// Native callbacks from the existing app return ok/error. Convert those
    /// once; persisted and wire results must carry an explicit terminal status.
    static func terminalResult(_ value: [String: Any], kind: String, native: Bool = false) -> [String: Any]? {
        var value = value
        guard let ok = jsonBoolean(value["ok"]) else { return nil }
        if native {
            if value["status"] == nil {
                value["status"] = ok ? "succeeded" :
                    (value["error"] as? String == "Wisp was interrupted; native Calendar outcome is unknown" ? "unknown" : "failed")
            }
            if value["error"] == nil { value["error"] = "" }
        }
        let allowed: Set<String> = kind == "create_calendar_event"
            ? ["ok", "status", "error", "source_id"] : ["ok", "status", "error"]
        guard Set(value.keys).isSubset(of: allowed),
              let status = value["status"] as? String, let error = value["error"] as? String,
              ok ? (status == "succeeded" && error.isEmpty) :
                   (["failed", "unknown"].contains(status) && text(error) != nil) else { return nil }
        if ok && kind == "create_calendar_event" {
            guard text(value["source_id"]) != nil else { return nil }
        } else if value["source_id"] != nil { return nil }
        return value
    }

    private static func equal(_ lhs: [String: Any], _ rhs: [String: Any]) -> Bool {
        guard let a = try? JSONSerialization.data(withJSONObject: lhs, options: [.sortedKeys]),
              let b = try? JSONSerialization.data(withJSONObject: rhs, options: [.sortedKeys]) else { return false }
        return a == b
    }

    private static func fingerprint(_ payload: [String: Any]) -> String? {
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]) else { return nil }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    init(path: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".moe/assistant-delivery-receipts.json")) {
        self.path = path
        if FileManager.default.fileExists(atPath: path.path) {
            if let data = try? Data(contentsOf: path),
               let saved = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] {
                receipts = saved
            } else {
                receipts = [:]
                readable = false
            }
        } else { receipts = [:] }
    }

    private func save(_ id: String, _ receipt: [String: Any]) -> Bool {
        var next = receipts
        next[id] = receipt
        do {
            let data = try JSONSerialization.data(withJSONObject: next, options: [.sortedKeys])
            try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: path, options: [.atomic])
            receipts = next
            return true
        } catch { return false }
    }

    private func showUnknown(_ id: String, perform: (WispClient.Event) async -> Bool) async -> Bool {
        let key = "unknown:" + id
        if receipts[key]?["kind"] as? String == "calendar_action_unknown",
           Self.jsonBoolean(receipts[key]?["done"]) == true,
           Set(receipts[key]!.keys) == ["kind", "done"] { return true }
        guard await perform(WispClient.Event(type: "calendar_action_unknown", payload: [
            "event_id": id, "error": "Wisp lost confirmation of this Calendar action. Check Calendar before retrying."
        ])) else { return false }
        return save(key, ["kind": "calendar_action_unknown", "done": true])
    }

    func handle(_ event: WispClient.Event, client: WispClient,
                perform: (WispClient.Event) async -> Bool,
                calendar: (WispClient.Event) async -> [String: Any]) async -> Bool {
        let id = event.str("event_id")
        if id.isEmpty { return await perform(event) }
        guard readable, !inFlight.contains(id) else { return false }
        inFlight.insert(id)
        defer { inFlight.remove(id) }
        if let receipt = receipts[id], receipt["kind"] as? String != event.type { return false }
        let isCalendar = ["create_calendar_event", "delete_calendar_event"].contains(event.type)
        if isCalendar {
            var raw = event.payload
            raw.removeValue(forKey: "event_id")
            guard let payload = Self.calendarPayload(raw, kind: event.type),
                  let actionID = Self.text(payload["action_id"]), let digest = Self.fingerprint(payload) else { return false }
            // Bind the private payload without retaining Calendar title/location
            // in a second local store. The outbox remains its canonical owner.
            let binding: [String: Any] = ["kind": event.type, "action_id": actionID, "payload_digest": digest]
            if let receipt = receipts[id] {
                // An invalid Calendar receipt may represent an already-run
                // effect. Fail closed; never treat corruption as permission to
                // execute again. Older valid receipts bind via the claim below.
                if let prior = receipt["action_id"], prior as? String != actionID { return false }
                if let prior = receipt["payload_digest"], prior as? String != digest { return false }
                if let prior = receipt["payload"] {
                    guard let prior = prior as? [String: Any], Self.equal(prior, payload) else { return false }
                }
                if receipt["done"] != nil {
                    guard Self.jsonBoolean(receipt["done"]) == true,
                          receipt["started"] == nil, receipt["claim_token"] == nil else { return false }
                    if let result = receipt["result"] {
                        guard let result = result as? [String: Any],
                              Self.terminalResult(result, kind: event.type, native: true) != nil else { return false }
                    }
                } else {
                    guard Self.text(receipt["claim_token"]) != nil else { return false }
                    if receipt["started"] != nil {
                        guard Self.jsonBoolean(receipt["started"]) == true, receipt["result"] == nil else { return false }
                    } else {
                        guard let result = receipt["result"] as? [String: Any],
                              Self.terminalResult(result, kind: event.type, native: true) != nil else { return false }
                    }
                }
                let allowed: Set<String> = ["kind", "action_id", "payload", "payload_digest", "done", "started", "claim_token", "result"]
                guard Set(receipt.keys).isSubset(of: allowed) else { return false }
            }
            guard let claim = await client.assistantDeliveryPost("assistant/events/\(id)/claim",
                body: ["kind": event.type, "action_id": actionID, "payload": payload]),
                  claim["event_id"] as? String == id, claim["action_id"] as? String == actionID,
                  claim["kind"] as? String == event.type,
                  let confirmed = claim["payload"] as? [String: Any],
                  let confirmed = Self.calendarPayload(confirmed, kind: event.type), Self.equal(confirmed, payload),
                  let execute = Self.jsonBoolean(claim["execute"]),
                  let recorded = Self.jsonBoolean(claim["recorded"]) else { return false }
            if recorded {
                guard !execute, let result = claim["result"] as? [String: Any],
                      let result = Self.terminalResult(result, kind: event.type) else { return false }
                if let local = receipts[id]?["result"] as? [String: Any],
                   let local = Self.terminalResult(local, kind: event.type, native: true), !Self.equal(local, result) { return false }
                guard save(id, binding.merging(["done": true]) { _, new in new }) else { return false }
            } else {
                guard claim["result"] == nil else { return false }
                if receipts[id] == nil {
                    guard execute, let token = Self.text(claim["claim_token"]) else {
                        if !execute, Self.text(claim["error"]) != nil { _ = await showUnknown(id, perform: perform) }
                        return false
                    }
                    guard save(id, binding.merging(["claim_token": token, "started": true]) { _, new in new }) else { return false }
                    guard let result = Self.terminalResult(await calendar(event), kind: event.type, native: true),
                          save(id, binding.merging(["claim_token": token, "result": result]) { _, new in new }) else { return false }
                }
                guard let receipt = receipts[id], Self.jsonBoolean(receipt["done"]) != true,
                      let token = Self.text(receipt["claim_token"]) else { return false }
                let result: [String: Any]
                if let cached = receipt["result"] as? [String: Any],
                   let terminal = Self.terminalResult(cached, kind: event.type, native: true) { result = terminal }
                else {
                    guard await showUnknown(id, perform: perform) else { return false }
                    result = ["ok": false, "status": "unknown", "error": "Wisp was interrupted; native Calendar outcome is unknown"]
                }
                // Retain both token and canonical result until the server
                // confirms durable recording for these exact identities.
                guard save(id, binding.merging(["claim_token": token, "result": result]) { _, new in new }),
                      let response = await client.assistantDeliveryPost("assistant/action_result", body: [
                        "event_id": id, "action_id": actionID, "kind": event.type,
                        "claim_token": token, "result": result
                      ]), Self.jsonBoolean(response["ok"]) == true,
                      Self.jsonBoolean(response["recorded"]) == true,
                      response["event_id"] as? String == id, response["action_id"] as? String == actionID,
                      response["kind"] as? String == event.type else { return false }
                guard save(id, binding.merging(["done": true]) { _, new in new }) else { return false }
            }
        } else {
            let receipt = receipts[id] ?? [:]
            let done = Set(receipt.keys) == ["kind", "done"] && Self.jsonBoolean(receipt["done"]) == true
            if !done {
                guard await perform(event), save(id, ["kind": event.type, "done": true]) else { return false }
            }
        }
        let response = await client.assistantDeliveryPost("assistant/events/\(id)/ack",
                                                         body: ["kind": event.type, "state": "handled"])
        return Self.jsonBoolean(response?["ok"]) == true && response?["event_id"] as? String == id
            && response?["kind"] as? String == event.type
    }
}
