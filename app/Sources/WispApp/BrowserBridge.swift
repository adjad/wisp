import Foundation
import CryptoKit

/// Inactive A04 private-IPC client. Bootstrap must inject a separately provisioned
/// credential and native-owned context. No browser launch, listener or effects.
/// HMAC authenticates frames; the enclosing inherited IPC must keep them private.
enum BrowserBridgeWire {
    static let maxFrame = 262144
    static let maxPayload = 131072
    static let kinds: Set<String> = ["challenge", "register", "registered", "observation", "snapshot",
                                     "command", "result", "decision", "disconnect"]
    static func check(_ condition: Bool, _ message: String = "Invalid bridge message") throws {
        try WispBrowserContracts.require(condition, message, "bridge_unauthorized")
    }
    static func identifier(_ value: String) throws {
        try check(value.range(of: #"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"#, options: .regularExpression) != nil)
    }
    static func json(_ object: Any) throws -> Data {
        try check(JSONSerialization.isValidJSONObject(object))
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    }
    static func parse(_ data: Data) throws -> [String: Any] {
        try check(data.count <= maxFrame)
        // JSONSerialization accepts duplicate keys. Reject those before decoding,
        // including escaped duplicate names, at every nesting level.
        var scanner = BrowserBridgeJSONScanner(bytes: Array(data))
        try scanner.scan()
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw BrowserContractViolation(message: "Invalid bridge JSON", code: "invalid_payload")
        }
        return value
    }
    static func body(_ frame: [String: Any], direction: String) throws -> Data {
        try check(["to_peer", "to_service"].contains(direction))
        return Data(["wisp-browser-bridge/1", direction, frame["credential_id"] as! String,
                     frame["session_id"] as! String, String((frame["sequence"] as! NSNumber).int64Value),
                     frame["kind"] as! String, frame["payload"] as! String].joined(separator: "\n").utf8)
    }
    static func decodeMAC(_ value: Any?) throws -> Data {
        // Regex end anchors may accept a final line terminator. A wire MAC is
        // exactly 64 ASCII bytes, never a Unicode character-indexed string.
        guard let text = value as? String, text.utf8.count == 64 else {
            throw BrowserContractViolation(message: "Invalid bridge MAC", code: "invalid_payload")
        }
        let bytes = Array(text.utf8)
        func nibble(_ byte: UInt8) -> UInt8? {
            switch byte {
            case 48...57: return byte - 48
            case 97...102: return byte - 87
            default: return nil
            }
        }
        var mac = Data(capacity: 32)
        for offset in stride(from: 0, to: 64, by: 2) {
            guard let high = nibble(bytes[offset]), let low = nibble(bytes[offset + 1]) else {
                throw BrowserContractViolation(message: "Invalid bridge MAC", code: "invalid_payload")
            }
            mac.append((high << 4) | low)
        }
        return mac
    }
    static func shape(_ frame: [String: Any]) throws -> (Data, Data) {
        try check(Set(frame.keys) == ["version", "credential_id", "session_id", "sequence", "kind", "payload", "mac"])
        guard frame["version"] as? String == "1", let cid = frame["credential_id"] as? String,
              let sid = frame["session_id"] as? String, let sequence = frame["sequence"] as? NSNumber,
              !WispBrowserContracts.isBool(sequence), sequence.doubleValue >= 0,
              sequence.doubleValue <= 9007199254740991, sequence.doubleValue.rounded() == sequence.doubleValue,
              let kind = frame["kind"] as? String, kinds.contains(kind),
              let encoded = frame["payload"] as? String, encoded.count <= 4 * ((maxPayload + 2) / 3),
              let payload = Data(base64Encoded: encoded), payload.count <= maxPayload,
              payload.base64EncodedString() == encoded else {
            throw BrowserContractViolation(message: "Invalid bridge envelope", code: "invalid_payload")
        }
        try identifier(cid)
        try identifier(sid)
        return (payload, try decodeMAC(frame["mac"]))
    }
    static func seal(key: Data, credentialID: String, sessionID: String, sequence: Int64,
                     kind: String, payload: [String: Any], direction: String) throws -> Data {
        let raw = try json(payload)
        try check(key.count == 32 && raw.count <= maxPayload)
        var frame: [String: Any] = ["version": "1", "credential_id": credentialID, "session_id": sessionID,
                                   "sequence": sequence, "kind": kind, "payload": raw.base64EncodedString(),
                                   "mac": String(repeating: "0", count: 64)]
        _ = try shape(frame)
        let mac = HMAC<SHA256>.authenticationCode(for: try body(frame, direction: direction), using: SymmetricKey(data: key))
        frame["mac"] = mac.map { String(format: "%02x", $0) }.joined()
        return try json(frame)
    }
    static func open(_ raw: Data, key: Data, direction: String) throws -> ([String: Any], [String: Any]) {
        let frame = try parse(raw)
        let (payload, mac) = try shape(frame)
        try check(key.count == 32 && HMAC<SHA256>.isValidAuthenticationCode(mac,
            authenticating: try body(frame, direction: direction), using: SymmetricKey(data: key)), "Bridge authentication failed")
        return (frame, try parse(payload))
    }
}

/// Small structural scanner; Foundation still performs full JSON grammar/UTF-8
/// validation. This scanner adds duplicate-key rejection and a depth bound.
private struct BrowserBridgeJSONScanner {
    let bytes: [UInt8]
    var offset = 0
    mutating func whitespace() { while offset < bytes.count && [9, 10, 13, 32].contains(bytes[offset]) { offset += 1 } }
    mutating func string() throws -> String {
        let start = offset
        try BrowserBridgeWire.check(offset < bytes.count && bytes[offset] == 34)
        offset += 1
        while offset < bytes.count {
            let byte = bytes[offset]
            offset += 1
            if byte == 92 { offset += 1 }
            else if byte == 34 {
                let wrapped = Data([91] + bytes[start..<offset] + [93])
                guard let result = try JSONSerialization.jsonObject(with: wrapped) as? [String], result.count == 1 else {
                    throw BrowserContractViolation(message: "Invalid bridge JSON", code: "invalid_payload")
                }
                return result[0]
            }
        }
        throw BrowserContractViolation(message: "Invalid bridge JSON", code: "invalid_payload")
    }
    mutating func value(depth: Int) throws {
        whitespace()
        try BrowserBridgeWire.check(depth <= 32 && offset < bytes.count)
        let first = bytes[offset]
        if first == 34 { _ = try string(); return }
        if first == 123 || first == 91 {
            let object = first == 123
            let end: UInt8 = object ? 125 : 93
            offset += 1
            whitespace()
            if offset < bytes.count && bytes[offset] == end { offset += 1; return }
            var keys = Set<String>()
            while true {
                whitespace()
                if object {
                    let key = try string()
                    try BrowserBridgeWire.check(keys.insert(key).inserted, "Duplicate bridge key")
                    whitespace()
                    try BrowserBridgeWire.check(offset < bytes.count && bytes[offset] == 58)
                    offset += 1
                }
                try value(depth: depth + 1)
                whitespace()
                try BrowserBridgeWire.check(offset < bytes.count)
                if bytes[offset] == end { offset += 1; return }
                try BrowserBridgeWire.check(bytes[offset] == 44)
                offset += 1
            }
        }
        let start = offset
        while offset < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[offset]) { offset += 1 }
        try BrowserBridgeWire.check(offset > start)
    }
    mutating func scan() throws {
        try value(depth: 0)
        whitespace()
        try BrowserBridgeWire.check(offset == bytes.count)
    }
}

struct BrowserBridgeIdentity: Codable, Equatable {
    let credentialID: String
    let peer: String
    let credentialRole: String
    let profileID: String

    func validate() throws {
        try BrowserBridgeWire.identifier(credentialID)
        try BrowserBridgeWire.identifier(profileID)
        try BrowserBridgeWire.check((["app", "extension"].contains(peer) && credentialRole == "native_bridge") ||
                                    (peer == "app" && credentialRole == "app_approval"))
    }
    var capabilities: [String] {
        credentialRole == "app_approval" ? ["exact_app_approval"] : ["dom_text", "verified_receipts"]
    }
}

struct BrowserBridgeRuntimeContext {
    let profileID: String
    let enabled: Bool
    let privateContext: Bool
    let background: Bool
    let allowedOrigins: Set<String>
    let url: String
    let taskID: String?
    let snapshotID: String?

    func validate(identity: BrowserBridgeIdentity, destination: String? = nil) throws {
        try BrowserBridgeWire.check(profileID == identity.profileID && enabled && !privateContext && background,
                                    "Native browser context denied")
        for value in [url] + (destination.map { [$0] } ?? []) {
            guard let u = URLComponents(string: value), let scheme = u.scheme, ["http", "https"].contains(scheme),
                  let host = u.host, u.user == nil, u.password == nil else {
                throw BrowserContractViolation(message: "Native URL denied", code: "site_permission_denied")
            }
            let origin = "\(scheme)://\(host)" + (u.port.map { ":\($0)" } ?? "")
            try BrowserBridgeWire.check(allowedOrigins.contains(origin), "Native site permission denied")
        }
    }
}

struct BrowserBridgeClosed: Error {
    let uncertainActionIDs: [String]
}

/// Serialized caller-owned channel. A new instance is required after any error
/// or disconnect. Returned commands are data for a gated executor, not effects.
actor BrowserBridge {
    private let identity: BrowserBridgeIdentity
    private let key: Data
    private let runtime: () throws -> BrowserBridgeRuntimeContext
    private var sessionID: String?
    private var clientNonce: String?
    private var incoming: Int64 = 0
    private var outgoing: Int64 = 0
    private var registered = false
    private var closed = false
    private var pending: [String: Any]?

    init(identity: BrowserBridgeIdentity, key: Data, runtime: @escaping () throws -> BrowserBridgeRuntimeContext) throws {
        try identity.validate()
        try BrowserBridgeWire.check(key.count == 32)
        self.identity = identity
        self.key = key
        self.runtime = runtime
    }
    private func context(_ destination: String? = nil) throws -> BrowserBridgeRuntimeContext {
        let c = try runtime()
        try c.validate(identity: identity, destination: destination)
        return c
    }
    private func send(_ kind: String, _ payload: [String: Any]) throws -> Data {
        try BrowserBridgeWire.check(!closed && sessionID != nil)
        let raw = try BrowserBridgeWire.seal(key: key, credentialID: identity.credentialID,
            sessionID: sessionID!, sequence: outgoing, kind: kind, payload: payload, direction: "to_service")
        outgoing += 1
        return raw
    }
    /// Accept the authenticated service challenge and return our registration.
    func register(challenge: Data) throws -> Data {
        do {
            try BrowserBridgeWire.check(!closed && sessionID == nil)
            let (f, p) = try BrowserBridgeWire.open(challenge, key: key, direction: "to_peer")
            _ = try WispBrowserContracts.validate("Handshake", p)
            try BrowserBridgeWire.check(f["credential_id"] as? String == identity.credentialID &&
                f["kind"] as? String == "challenge" && (f["sequence"] as? NSNumber)?.int64Value == 0 &&
                p["peer"] as? String == "service" && p["credential_role"] as? String == "native_bridge" &&
                Set(p["capabilities"] as! [String]) == Set(identity.capabilities) &&
                Set(p["required_capabilities"] as! [String]) == Set(identity.capabilities))
            sessionID = f["session_id"] as? String
            // Fresh client contribution prevents replay of an old server
            // challenge/agreement/command transcript after client restart.
            clientNonce = SymmetricKey(size: .bits256).withUnsafeBytes {
                $0.map { String(format: "%02x", $0) }.joined()
            }
            incoming = 1
            let handshake: [String: Any] = ["schema_version": "1.0", "peer": identity.peer,
                "credential_role": identity.credentialRole, "supported_versions": ["1.0"],
                "capabilities": identity.capabilities, "required_capabilities": identity.capabilities]
            return try send("register", ["handshake": handshake, "client_nonce": clientNonce!])
        } catch { throw BrowserBridgeClosed(uncertainActionIDs: close()) }
    }
    /// Returns only validated command/registration/disconnect data. A caller must
    /// recheck live runtime, policy and budget immediately before executing.
    func receive(_ raw: Data) throws -> (String, [String: Any]) {
        do {
            try BrowserBridgeWire.check(!closed && sessionID != nil)
            let (f, p) = try BrowserBridgeWire.open(raw, key: key, direction: "to_peer")
            try BrowserBridgeWire.check(f["credential_id"] as? String == identity.credentialID &&
                f["session_id"] as? String == sessionID && (f["sequence"] as? NSNumber)?.int64Value == incoming)
            incoming += 1
            let kind = f["kind"] as! String
            if !registered {
                try BrowserBridgeWire.check(Set(p.keys) == ["negotiated", "client_nonce"] &&
                                            p["client_nonce"] as? String == clientNonce)
                let negotiated = try WispBrowserContracts.validate("NegotiatedCapabilities", p["negotiated"] as Any)
                try BrowserBridgeWire.check(kind == "registered" &&
                    Set(negotiated["capabilities"] as! [String]) == Set(identity.capabilities))
                registered = true
            } else if kind == "disconnect" {
                try BrowserBridgeWire.check(p.isEmpty)
                return (kind, ["uncertain_action_ids": close()])
            } else {
                try BrowserBridgeWire.check(kind == "command" && identity.credentialRole == "native_bridge" && pending == nil)
                _ = try WispBrowserContracts.validate("BrowserAction", p)
                let intent = p["intent"] as! [String: Any]
                let c = try context(intent["url"] as? String)
                try BrowserBridgeWire.check(intent["task_id"] as? String == c.taskID && intent["snapshot_id"] as? String == c.snapshotID)
                pending = p
            }
            return (kind, p)
        } catch { throw BrowserBridgeClosed(uncertainActionIDs: close()) }
    }
    func publish(kind: String, payload: [String: Any]) throws -> Data {
        do {
            try BrowserBridgeWire.check(registered && !closed && identity.credentialRole == "native_bridge")
            let contracts = ["observation": "SourceObservation", "snapshot": "BrowserSnapshot", "result": "ActionReceipt"]
            guard let contract = contracts[kind] else {
                throw BrowserContractViolation(message: "Bridge capability denied", code: "bridge_unauthorized")
            }
            _ = try WispBrowserContracts.validate(contract, payload)
            let c = try context()
            if kind == "observation" {
                try BrowserBridgeWire.check(payload["source_kind"] as? String == "browser" && payload["source_url"] as? String == c.url)
            } else if kind == "snapshot" {
                try BrowserBridgeWire.check(payload["url"] as? String == c.url && payload["task_id"] as? String == c.taskID &&
                                            payload["id"] as? String == c.snapshotID)
            } else {
                guard let action = pending, let intent = action["intent"] as? [String: Any] else {
                    throw BrowserContractViolation(message: "Unsolicited bridge result", code: "invalid_payload")
                }
                let proposal = (action["approval"] as? [String: Any])?["proposal_id"] ?? NSNull()
                try BrowserBridgeWire.check(payload["action_id"] as? String == intent["action_id"] as? String &&
                    payload["task_id"] as? String == intent["task_id"] as? String &&
                    WispBrowserContracts.equal(payload["proposal_id"]!, proposal) && payload["completes_obligation"] as? Bool == false)
            }
            let raw = try send(kind, payload)
            if kind == "result" { pending = nil }
            return raw
        } catch { throw BrowserBridgeClosed(uncertainActionIDs: close()) }
    }
    /// Call only from the trusted app approval UI after exact user review.
    /// Backend A03 validates revisions/evidence and supplies its own capability.
    func decide(_ payload: [String: Any]) throws -> Data {
        do {
            try BrowserBridgeWire.check(registered && identity.credentialRole == "app_approval")
            try BrowserBridgeWire.check(Set(payload.keys) == ["proposal_id", "decision", "expected_proposal_revision",
                "expected_item_storage_revision", "intent", "evidence_ids", "expires_at_ms"])
            _ = try WispBrowserContracts.validate("ActionIntent", payload["intent"] as Any)
            return try send("decision", payload)
        } catch { throw BrowserBridgeClosed(uncertainActionIDs: close()) }
    }
    func disconnect() throws -> (Data, [String]) {
        do {
            let raw = try send("disconnect", [:])
            return (raw, close())
        } catch { throw BrowserBridgeClosed(uncertainActionIDs: close()) }
    }
    /// Trusted transport EOF/error hook. Preserve IDs before dropping payloads.
    func close() -> [String] {
        let actionID = (pending?["intent"] as? [String: Any])?["action_id"] as? String
        closed = true
        registered = false
        pending = nil
        return actionID.map { [$0] } ?? []
    }
}
