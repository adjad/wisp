import Foundation

/// Receipts contain identities/results, never notification text. Atomic writes
/// precede acknowledgement; a lost ACK reuses the receipt without repeating UI
/// effects. Calendar claims are exclusive in the backend across app instances.
@MainActor
final class AssistantDelivery {
    private let path: URL
    private var receipts: [String: [String: Any]]
    private var inFlight: Set<String> = []
    private var readable = true

    init(path: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".moe/assistant-delivery-receipts.json")) {
        self.path = path
        if FileManager.default.fileExists(atPath: path.path) {
            if let data = try? Data(contentsOf: path),
               let saved = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] {
                receipts = saved
            } else {
                receipts = [:]
                readable = false // Do not overwrite unclear recovery state.
            }
        } else { receipts = [:] }
    }

    private func save(_ id: String, _ receipt: [String: Any]) -> Bool {
        var next = receipts
        next[id] = receipt
        do {
            let data = try JSONSerialization.data(withJSONObject: next, options: [.sortedKeys])
            try FileManager.default.createDirectory(at: path.deletingLastPathComponent(),
                                                     withIntermediateDirectories: true)
            try data.write(to: path, options: [.atomic])
            receipts = next
            return true
        } catch { return false }
    }

    private func showUnknown(_ id: String, error: String,
                             perform: (WispClient.Event) async -> Bool) async -> Bool {
        let key = "unknown:" + id
        if receipts[key]?["done"] as? Bool == true { return true }
        guard await perform(WispClient.Event(type: "calendar_action_unknown", payload: [
            "event_id": id, "error": error
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
            if receipts[id] == nil {
                guard !event.str("action_id").isEmpty,
                      let claim = await client.assistantDeliveryPost("assistant/events/\(id)/claim",
                                                                   body: ["kind": event.type]) else { return false }
                if let result = claim["result"] as? [String: Any] {
                    // The backend already committed this receipt (or expired it).
                    guard save(id, ["kind": event.type, "done": true, "result": result]) else { return false }
                } else {
                    guard claim["execute"] as? Bool == true,
                          let token = claim["claim_token"] as? String else {
                        // Another execution claimed this action but lost its
                        // receipt. Surface uncertainty without authorizing a
                        // duplicate or pretending the action completed.
                        _ = await showUnknown(id,
                            error: claim["error"] as? String ?? "Check Calendar before retrying this action.",
                            perform: perform)
                        return false
                    }
                    guard save(id, ["kind": event.type, "claim_token": token, "started": true]) else { return false }
                    let result = await calendar(event)
                    guard save(id, ["kind": event.type, "claim_token": token, "result": result]) else { return false }
                }
            }
            guard let receipt = receipts[id] else { return false }
            if receipt["done"] as? Bool != true {
                // An app crash after claim but before persisting the result is
                // uncertain. Never execute it again, even if no result survived.
                if receipt["result"] == nil {
                    guard await showUnknown(id,
                        error: "Wisp was interrupted during this Calendar action. Check Calendar before retrying.",
                        perform: perform) else { return false }
                }
                let result = receipt["result"] as? [String: Any] ?? [
                    "ok": false, "error": "Wisp was interrupted; native Calendar outcome is unknown"]
                var body = result
                body["action_id"] = event.str("action_id")
                body["event_id"] = id
                body["kind"] = event.type
                body["claim_token"] = receipt["claim_token"]
                guard let response = await client.assistantDeliveryPost("assistant/action_result", body: body),
                      response["ok"] as? Bool == true else { return false }
                guard save(id, ["kind": event.type, "done": true]) else { return false }
            }
        } else if receipts[id]?["done"] as? Bool != true {
            guard await perform(event), save(id, ["kind": event.type, "done": true]) else { return false }
        }
        let response = await client.assistantDeliveryPost("assistant/events/\(id)/ack",
                                                         body: ["kind": event.type, "state": "handled"])
        return response?["ok"] as? Bool == true
    }
}
