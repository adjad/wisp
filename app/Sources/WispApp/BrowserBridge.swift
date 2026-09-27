import Foundation
import CryptoKit
import Security
import Darwin

/// Inactive A04 private-IPC client. Bootstrap must inject a separately provisioned
/// credential and native-owned context. No browser launch, listener or effects.
/// HMAC authenticates frames; the enclosing inherited IPC must keep them private.
enum BrowserBridgeWire {
    static let maxFrame = 262144
    static let maxPayload = 131072
    static let kinds: Set<String> = ["challenge", "register", "registered", "observation", "snapshot",
                                     "command", "result", "result_ack", "decision", "disconnect"]
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
    /// Supplied by trusted app review state, never by the incoming decision.
    let approvalProposalID: String?

    func validateApproval(identity: BrowserBridgeIdentity, proposalID: String, intent: [String: Any]) throws {
        try BrowserBridgeWire.check(profileID == identity.profileID && approvalProposalID == proposalID &&
            taskID == intent["task_id"] as? String && snapshotID == intent["snapshot_id"] as? String,
            "Native approval scope denied")
    }

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
    private var awaitingReceiptID: String?

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
            } else if kind == "result_ack" {
                let intent = pending?["intent"] as? [String: Any]
                try BrowserBridgeWire.check(Set(p.keys) == ["action_id", "receipt_id"] &&
                    awaitingReceiptID != nil && p["receipt_id"] as? String == awaitingReceiptID &&
                    p["action_id"] as? String == intent?["action_id"] as? String,
                    "Unsolicited result acknowledgement")
                pending = nil
                awaitingReceiptID = nil
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
    /// Serialized bytes are not delivery acknowledgement. For results the
    /// pending remains uncertain until an authenticated, correlated result ACK.
    /// Process-crash recovery still requires the executor's durable action claim.
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
                guard awaitingReceiptID == nil, let action = pending, let intent = action["intent"] as? [String: Any] else {
                    throw BrowserContractViolation(message: "Unsolicited bridge result", code: "invalid_payload")
                }
                let proposal = (action["approval"] as? [String: Any])?["proposal_id"] ?? NSNull()
                try BrowserBridgeWire.check(payload["action_id"] as? String == intent["action_id"] as? String &&
                    payload["task_id"] as? String == intent["task_id"] as? String &&
                    WispBrowserContracts.equal(payload["proposal_id"]!, proposal) && payload["completes_obligation"] as? Bool == false)
            }
            let raw = try send(kind, payload)
            if kind == "result" { awaitingReceiptID = payload["id"] as? String }
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
            let intent = try WispBrowserContracts.validate("ActionIntent", payload["intent"] as Any)
            guard let proposalID = payload["proposal_id"] as? String else {
                throw BrowserContractViolation(message: "Invalid app decision", code: "invalid_payload")
            }
            try BrowserBridgeWire.identifier(proposalID)
            // Reviewing a decision does not require browser capture permissions.
            // Both sides independently recheck current profile/review scope.
            try runtime().validateApproval(identity: identity, proposalID: proposalID, intent: intent)
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
        awaitingReceiptID = nil
        return actionID.map { [$0] } ?? []
    }
}

/// Inactive connected Unix-stream transport. The trusted native bootstrap owns
/// endpoint creation and supplies the peer requirement; neither comes from wire
/// input. Use an independently connected socket, not a pre-fork socketpair whose
/// audit token may identify the creator. No listener or production wiring here.
/// One serialized owner; init takes ownership of fd even on failure.
final class BrowserBridgeTransport {
    private var fd: Int32
    private let requirement: SecRequirement
    private let timeout: UInt64

    init(connectedFD: Int32, peerRequirement: String, timeoutSeconds: Double = 5) throws {
        fd = connectedFD
        var parsed: SecRequirement?
        guard timeoutSeconds.isFinite && timeoutSeconds > 0 && timeoutSeconds <= 30,
              SecRequirementCreateWithString(peerRequirement as CFString, [], &parsed) == errSecSuccess,
              let parsed else {
            Darwin.close(connectedFD)
            fd = -1
            throw BrowserContractViolation(message: "Bridge transport unavailable", code: "bridge_unauthorized")
        }
        requirement = parsed
        timeout = UInt64(timeoutSeconds * 1_000_000_000)
        do {
            var address = sockaddr_storage()
            var size = socklen_t(MemoryLayout.size(ofValue: address))
            let status = withUnsafeMutablePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &size) }
            }
            var kind: Int32 = 0
            size = socklen_t(MemoryLayout.size(ofValue: kind))
            try BrowserBridgeWire.check(status == 0 && Int32(address.ss_family) == AF_UNIX &&
                getsockopt(fd, SOL_SOCKET, SO_TYPE, &kind, &size) == 0 && kind == SOCK_STREAM)
            let flags = fcntl(fd, F_GETFL)
            var one: Int32 = 1
            try BrowserBridgeWire.check(flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 &&
                fcntl(fd, F_SETFD, FD_CLOEXEC) == 0 &&
                setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout.size(ofValue: one))) == 0)
            try verifyPeer()
        } catch { close(); throw error }
    }
    deinit { close() }
    func close() {
        if fd >= 0 { Darwin.close(fd); fd = -1 }
    }
    private func verifyPeer() throws {
        var uid: uid_t = 0
        var gid: gid_t = 0
        var token = audit_token_t()
        var count = socklen_t(MemoryLayout.size(ofValue: token))
        try BrowserBridgeWire.check(fd >= 0 && getpeereid(fd, &uid, &gid) == 0 && uid == geteuid() &&
            getsockopt(fd, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &count) == 0 &&
            count == MemoryLayout.size(ofValue: token), "Bridge peer unavailable")
        let data = withUnsafeBytes(of: token) { Data($0) }
        var code: SecCode?
        try BrowserBridgeWire.check(SecCodeCopyGuestWithAttributes(nil,
            [kSecGuestAttributeAudit as String: data] as CFDictionary, [], &code) == errSecSuccess &&
            code != nil, "Bridge peer unavailable")
        try BrowserBridgeWire.check(SecCodeCheckValidity(code!, [], requirement) == errSecSuccess,
                                    "Bridge peer denied")
    }
    private func wait(_ events: Int16, deadline: UInt64) throws {
        while true {
            let now = DispatchTime.now().uptimeNanoseconds
            try BrowserBridgeWire.check(now < deadline, "Bridge transport timed out")
            var item = pollfd(fd: fd, events: events, revents: 0)
            let milliseconds = Int32(min((deadline - now + 999_999) / 1_000_000, UInt64(Int32.max)))
            let ready = poll(&item, 1, milliseconds)
            if ready < 0 && errno == EINTR { continue }
            try BrowserBridgeWire.check(ready > 0 && item.revents & events != 0,
                                        "Bridge transport closed")
            return
        }
    }
    private func read(_ count: Int, deadline: UInt64) throws -> Data {
        var data = Data(count: count)
        var offset = 0
        while offset < count {
            try wait(Int16(POLLIN), deadline: deadline)
            let n = data.withUnsafeMutableBytes {
                Darwin.recv(fd, $0.baseAddress!.advanced(by: offset), count - offset, 0)
            }
            if n < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
            try BrowserBridgeWire.check(n > 0, "Bridge transport closed")
            offset += n
        }
        return data
    }
    func receive() throws -> Data {
        do {
            try verifyPeer()
            let deadline = DispatchTime.now().uptimeNanoseconds + timeout
            let header = try read(4, deadline: deadline)
            let count = header.reduce(0) { ($0 << 8) | Int($1) }
            try BrowserBridgeWire.check(count > 0 && count <= BrowserBridgeWire.maxFrame)
            let data = try read(count, deadline: deadline)
            try verifyPeer()
            return data
        } catch { close(); throw error }
    }
    func send(_ data: Data) throws {
        do {
            try verifyPeer()
            try BrowserBridgeWire.check(!data.isEmpty && data.count <= BrowserBridgeWire.maxFrame)
            var size = UInt32(data.count).bigEndian
            var packet = withUnsafeBytes(of: &size) { Data($0) }
            packet.append(data)
            let deadline = DispatchTime.now().uptimeNanoseconds + timeout
            var offset = 0
            while offset < packet.count {
                try wait(Int16(POLLOUT), deadline: deadline)
                let n = packet.withUnsafeBytes {
                    Darwin.send(fd, $0.baseAddress!.advanced(by: offset), packet.count - offset, 0)
                }
                if n < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
                try BrowserBridgeWire.check(n > 0, "Bridge transport closed")
                offset += n
            }
        } catch { close(); throw error }
    }
}
