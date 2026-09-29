import Foundation
import CryptoKit
import Security
import Darwin

/// A04 private-IPC client and its A10 WP3 activation (bottom of this file).
/// Nothing here starts unless the user enabled a browser profile (default off)
/// and a native peer requirement is configured for it. No effects, no approvals.
/// HMAC authenticates frames; the enclosing private IPC must keep them private.
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

/// Connected Unix-stream transport. The trusted native bootstrap owns endpoint
/// creation and supplies the peer requirement; neither comes from wire input.
/// Use an independently connected socket, not a pre-fork socketpair whose audit
/// token may identify the creator. The listener that accepts these descriptors
/// is `BrowserBridgeUnixListener`, started only by `BrowserBridgeActivation`.
/// One serialized owner; init takes ownership of fd even on failure.
/// What a peer session needs from a verified, framed connection. The signed
/// Unix-stream transport below is the production conformer.
protocol BrowserBridgePeerTransport: AnyObject {
    func receive() throws -> Data
    func send(_ data: Data) throws
    /// Wake any blocked read/write from another thread; the owner then closes.
    func cancel()
    func close()
}

final class BrowserBridgeTransport: BrowserBridgePeerTransport {
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
    /// shutdown(2) makes a blocked poll/recv return without racing a close.
    func cancel() {
        let descriptor = fd
        if descriptor >= 0 { _ = shutdown(descriptor, SHUT_RDWR) }
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

// MARK: - A10 WP3 activation
//
// Trust model
//  * Off by default. With no enabled profile (or no configured peer requirement
//    for its browser) nothing is created: no directory, socket, thread, Keychain
//    call or backend traffic.
//  * The backend link is a private Unix socket the APP listens on. The backend
//    connects; the app accepts only the exact process it launched (same UID and
//    the PID BackendManager recorded). The backend never listens.
//  * Chrome and Safari peers connect to their own sockets. A peer is accepted
//    only if the kernel-reported audit token satisfies the browser's code
//    requirement, which native bootstrap configures. Nothing a peer sends
//    chooses its browser or its requirement.
//  * Backend keys never leave the app. A verified peer sends plain, closed
//    JSON; the app seals observations with that profile's credential using the
//    A04 client, so revocation and the backend's own context check still apply.
//  * BridgeRuntimeContext is native-owned. `begin` is the verified native peer's
//    per-click report; it is validated, bounded to 30 seconds, spent by one
//    observation, and merged with the app's own enablement flag. It is never
//    decoded from an observation frame. Unknown, expired or private denies.
//  * No approval authority: only extension/native_bridge credentials exist here,
//    and peers can send `hello`, `begin`, `observation` and `disconnect`.

/// Closed control vocabulary shared with service/browser/host.py.
enum BrowserBridgeControl {
    static let maxFrame = 1_048_576
    static let contextTTLMilliseconds = 30_000
    static let codes: Set<String> = ["incompatible_version", "missing_capability", "invalid_payload", "disabled",
                                     "site_permission_denied", "private_context", "foreground_preempted",
                                     "bridge_unauthorized"]

    static func provision(_ identity: BrowserBridgeIdentity, key: Data) -> [String: Any] {
        ["op": "provision", "credential_id": identity.credentialID, "peer": identity.peer,
         "credential_role": identity.credentialRole, "profile_id": identity.profileID,
         "key": key.map { String(format: "%02x", $0) }.joined()]
    }
    static func revoke(credentialID: String) -> [String: Any] { ["op": "revoke", "credential_id": credentialID] }
    static func context(profile: String, origin: String, url: String) -> [String: Any] {
        ["op": "context", "profile_id": profile, "enabled": true, "private_context": false,
         "trigger": "user_click", "allowed_origins": [origin], "url": url, "ttl_ms": contextTTLMilliseconds]
    }
    static func clearContext(profile: String) -> [String: Any] { ["op": "clear_context", "profile_id": profile] }
    static func peerOpen(connection: Int, credentialID: String) -> [String: Any] {
        ["op": "peer_open", "conn": connection, "credential_id": credentialID]
    }
    static func peerFrame(connection: Int, frame: Data) -> [String: Any] {
        ["op": "peer_frame", "conn": connection, "frame": frame.base64EncodedString()]
    }
    static func peerClose(connection: Int) -> [String: Any] { ["op": "peer_close", "conn": connection] }

    /// Replies are closed: `ok` must be exactly true, otherwise only a known code survives.
    static func expectOK(_ reply: [String: Any]) throws -> [String: Any] {
        guard let ok = reply["ok"], WispBrowserContracts.isBool(ok), ok as? Bool == true else {
            let code = reply["code"] as? String
            throw BrowserContractViolation(message: "Bridge control denied",
                                           code: code.flatMap { codes.contains($0) ? $0 : nil } ?? "invalid_payload")
        }
        return reply
    }
    static func code(of error: Error) -> String {
        if let violation = error as? BrowserContractViolation, codes.contains(violation.code) { return violation.code }
        return error is BrowserBridgeClosed ? "bridge_unauthorized" : "invalid_payload"
    }
}

protocol BrowserBridgeControlChannel: AnyObject {
    var isConnected: Bool { get }
    /// Serialized request/reply. Any failure closes the channel: fail closed.
    func call(_ message: [String: Any]) throws -> [String: Any]
}

/// POSIX helpers for private AF_UNIX endpoints. Every wait is bounded.
enum BrowserBridgeSocket {
    static func address(_ path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8) + [UInt8(0)]
        try BrowserBridgeWire.check(path.hasPrefix("/") && bytes.count <= MemoryLayout.size(ofValue: address.sun_path),
                                    "Bridge endpoint unavailable")
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        return address
    }
    /// An owner-only directory, never a symlink, never shared. Fail closed on any doubt.
    static func makePrivateDirectory(_ path: String) throws {
        var info = stat()
        if lstat(path, &info) != 0 {
            try BrowserBridgeWire.check(mkdir(path, 0o700) == 0 && chmod(path, 0o700) == 0, "Bridge endpoint unavailable")
            try BrowserBridgeWire.check(lstat(path, &info) == 0, "Bridge endpoint unavailable")
        }
        try BrowserBridgeWire.check((info.st_mode & S_IFMT) == S_IFDIR && info.st_uid == geteuid() &&
                                    info.st_mode & 0o077 == 0, "Bridge endpoint unavailable")
    }
    static func prepare(_ fd: Int32) throws {
        let flags = fcntl(fd, F_GETFL)
        var one: Int32 = 1
        try BrowserBridgeWire.check(flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 &&
            fcntl(fd, F_SETFD, FD_CLOEXEC) == 0 &&
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout.size(ofValue: one))) == 0,
            "Bridge endpoint unavailable")
    }
    static func deadline(_ seconds: Double) -> UInt64 {
        DispatchTime.now().uptimeNanoseconds + UInt64(seconds * 1_000_000_000)
    }
    private static func wait(_ fd: Int32, _ events: Int16, until deadline: UInt64) throws {
        while true {
            let now = DispatchTime.now().uptimeNanoseconds
            try BrowserBridgeWire.check(now < deadline, "Bridge control timed out")
            var item = pollfd(fd: fd, events: events, revents: 0)
            let milliseconds = Int32(min((deadline - now + 999_999) / 1_000_000, UInt64(Int32.max)))
            let ready = poll(&item, 1, milliseconds)
            if ready < 0 && errno == EINTR { continue }
            try BrowserBridgeWire.check(ready > 0 && item.revents & events != 0, "Bridge control closed")
            return
        }
    }
    static func read(_ fd: Int32, count: Int, until deadline: UInt64) throws -> Data {
        var data = Data(count: count)
        var offset = 0
        while offset < count {
            try wait(fd, Int16(POLLIN), until: deadline)
            let n = data.withUnsafeMutableBytes { Darwin.recv(fd, $0.baseAddress!.advanced(by: offset), count - offset, 0) }
            if n < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
            try BrowserBridgeWire.check(n > 0, "Bridge control closed")
            offset += n
        }
        return data
    }
    static func write(_ fd: Int32, _ data: Data, until deadline: UInt64) throws {
        var offset = 0
        while offset < data.count {
            try wait(fd, Int16(POLLOUT), until: deadline)
            let n = data.withUnsafeBytes { Darwin.send(fd, $0.baseAddress!.advanced(by: offset), data.count - offset, 0) }
            if n < 0 && [EINTR, EAGAIN, EWOULDBLOCK].contains(errno) { continue }
            try BrowserBridgeWire.check(n > 0, "Bridge control closed")
            offset += n
        }
    }
    static func peerProcessID(_ fd: Int32) throws -> pid_t {
        var pid: pid_t = 0
        var size = socklen_t(MemoryLayout.size(ofValue: pid))
        try BrowserBridgeWire.check(getsockopt(fd, SOL_LOCAL, LOCAL_PEERPID, &pid, &size) == 0 && pid > 1,
                                    "Bridge peer unavailable")
        return pid
    }
    static func peerIsSameUser(_ fd: Int32) -> Bool {
        var uid: uid_t = 0
        var gid: gid_t = 0
        return getpeereid(fd, &uid, &gid) == 0 && uid == geteuid()
    }
}

/// Owner-only AF_UNIX listener. Exists only between init and stop().
final class BrowserBridgeUnixListener: @unchecked Sendable {
    let path: String
    private var fd: Int32 = -1
    private var running = true
    private let lock = NSLock()
    private let finished = DispatchSemaphore(value: 0)
    private let handler: (Int32) -> Void

    /// `handler` takes ownership of each accepted descriptor.
    init(path: String, handler: @escaping (Int32) -> Void) throws {
        self.path = path
        self.handler = handler
        var address = try BrowserBridgeSocket.address(path)
        var info = stat()
        if lstat(path, &info) == 0 {
            // Only a stale socket this user owns may be replaced.
            try BrowserBridgeWire.check((info.st_mode & S_IFMT) == S_IFSOCK && info.st_uid == geteuid() &&
                                        unlink(path) == 0, "Bridge endpoint unavailable")
        }
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        try BrowserBridgeWire.check(descriptor >= 0, "Bridge endpoint unavailable")
        do {
            try BrowserBridgeSocket.prepare(descriptor)
            let status = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            try BrowserBridgeWire.check(status == 0, "Bridge endpoint unavailable")
            // The directory is 0700, so the interval before chmod is not reachable by others.
            try BrowserBridgeWire.check(chmod(path, 0o600) == 0 && listen(descriptor, 4) == 0,
                                        "Bridge endpoint unavailable")
        } catch {
            Darwin.close(descriptor)
            unlink(path)
            throw error
        }
        fd = descriptor
        Thread.detachNewThread { [self] in acceptLoop() }
    }
    private var isRunning: Bool { lock.lock(); defer { lock.unlock() }; return running }
    private func acceptLoop() {
        defer { finished.signal() }
        while isRunning {
            var item = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&item, 1, 200)
            guard isRunning else { return }
            if ready > 0 && item.revents & Int16(POLLIN) != 0 {
                let client = accept(fd, nil, nil)
                if client >= 0 {
                    if (try? BrowserBridgeSocket.prepare(client)) != nil { handler(client) } else { Darwin.close(client) }
                }
            }
        }
    }
    func stop() {
        lock.lock()
        let wasRunning = running
        running = false
        lock.unlock()
        guard wasRunning else { return }
        finished.wait()
        Darwin.close(fd)
        fd = -1
        unlink(path)
    }
}

/// The app's end of the backend link. Serves at most one connection, from the
/// exact backend process the app launched.
final class BrowserBridgeControlServer: BrowserBridgeControlChannel, @unchecked Sendable {
    let directory: String
    let path: String
    /// Called after every connect or disconnect. Handlers must not block.
    var onChange: (() -> Void)?
    private let backendPID: () -> pid_t?
    private let waitForBackend: Double
    private let callTimeout: Double
    private var listener: BrowserBridgeUnixListener?
    private var connection: Int32 = -1
    private let lock = NSLock()

    init(directory: String, backendPID: @escaping () -> pid_t?, waitForBackend: Double = 3, callTimeout: Double = 5) {
        self.directory = directory
        path = directory + "/control.sock"
        self.backendPID = backendPID
        self.waitForBackend = waitForBackend
        self.callTimeout = callTimeout
    }
    func start() throws {
        try BrowserBridgeSocket.makePrivateDirectory(directory)
        listener = try BrowserBridgeUnixListener(path: path) { [weak self] descriptor in self?.accepted(descriptor) }
    }
    var isConnected: Bool { lock.lock(); defer { lock.unlock() }; return connection >= 0 }

    private func expectedPID() -> pid_t? {
        let limit = BrowserBridgeSocket.deadline(waitForBackend)
        while DispatchTime.now().uptimeNanoseconds < limit {
            if let pid = backendPID() { return pid }
            usleep(50_000)
        }
        return backendPID()
    }
    private func accepted(_ descriptor: Int32) {
        // Same UID is not identity. Only the backend this app launched may drive it.
        guard BrowserBridgeSocket.peerIsSameUser(descriptor), let expected = expectedPID(),
              (try? BrowserBridgeSocket.peerProcessID(descriptor)) == expected else {
            Darwin.close(descriptor)
            return
        }
        lock.lock()
        let previous = connection
        connection = descriptor
        lock.unlock()
        if previous >= 0 { Darwin.close(previous) }
        onChange?()
    }
    func call(_ message: [String: Any]) throws -> [String: Any] {
        lock.lock()
        let descriptor = connection
        let result: Result<[String: Any], Error>
        if descriptor < 0 {
            result = .failure(BrowserContractViolation(message: "Bridge control unavailable", code: "bridge_unauthorized"))
        } else {
            result = Result {
                let body = try BrowserBridgeWire.json(message)
                try BrowserBridgeWire.check(body.count <= BrowserBridgeControl.maxFrame)
                var size = UInt32(body.count).bigEndian
                var packet = withUnsafeBytes(of: &size) { Data($0) }
                packet.append(body)
                let limit = BrowserBridgeSocket.deadline(callTimeout)
                try BrowserBridgeSocket.write(descriptor, packet, until: limit)
                let header = try BrowserBridgeSocket.read(descriptor, count: 4, until: limit)
                let count = header.reduce(0) { ($0 << 8) | Int($1) }
                try BrowserBridgeWire.check(count > 0 && count <= BrowserBridgeWire.maxFrame)
                return try BrowserBridgeWire.parse(try BrowserBridgeSocket.read(descriptor, count: count, until: limit))
            }
            if case .failure = result {
                Darwin.close(descriptor)
                connection = -1
            }
        }
        lock.unlock()
        if case .failure = result, descriptor >= 0 { onChange?() }
        return try result.get()
    }
    func stop() {
        listener?.stop()
        listener = nil
        lock.lock()
        let descriptor = connection
        connection = -1
        lock.unlock()
        if descriptor >= 0 { Darwin.close(descriptor) }
        rmdir(directory)
    }
}

/// Native-owned, single-use capture grants. Filled only by a verified native
/// peer's `begin`, validated first; never by an observation frame.
final class BrowserBridgeNativeContext: @unchecked Sendable {
    static let ttl: Double = 30
    private struct Grant { let url: String; let origin: String; let expires: Double }
    private var grants: [String: Grant] = [:]
    private let lock = NSLock()
    private let clock: () -> Double

    init(clock: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }) { self.clock = clock }

    /// http(s) only, ASCII, no credentials, bounded, plain host[:port].
    static func origin(of url: String) throws -> String {
        try WispBrowserContracts.require(!url.isEmpty && url.utf8.count <= 2048 && url.allSatisfy { $0.isASCII },
                                         "Native URL denied", "site_permission_denied")
        guard let parts = URLComponents(string: url), let scheme = parts.scheme, ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty, host.utf8.count <= 253, parts.user == nil, parts.password == nil,
              host.range(of: #"^[A-Za-z0-9.-]+$"#, options: .regularExpression) != nil,
              parts.port.map({ (1...65535).contains($0) }) ?? true else {
            throw BrowserContractViolation(message: "Native URL denied", code: "site_permission_denied")
        }
        return "\(scheme)://\(host)" + (parts.port.map { ":\($0)" } ?? "")
    }
    func grant(profile: String, url: String, origin: String) {
        lock.lock(); defer { lock.unlock() }
        grants[profile] = Grant(url: url, origin: origin, expires: clock() + Self.ttl)
    }
    func clear(profile: String) {
        lock.lock(); defer { lock.unlock() }
        grants[profile] = nil
    }
    func clearAll() {
        lock.lock(); defer { lock.unlock() }
        grants.removeAll()
    }
    /// `enabled` is the app's own flag, read fresh by the caller. Missing or
    /// expired context is unknown context, which denies.
    func runtime(profile: BrowserBridgeProfile, enabled: Bool) throws -> BrowserBridgeRuntimeContext {
        lock.lock()
        let grant = grants[profile.id]
        if let grant, clock() >= grant.expires { grants[profile.id] = nil }
        lock.unlock()
        guard let grant, clock() < grant.expires else {
            throw BrowserContractViolation(message: "Native context unavailable", code: "bridge_unauthorized")
        }
        // A04's `background` predates D1. A user-click read is the user's own
        // action and cannot preempt them; the trigger is fixed to user_click.
        return BrowserBridgeRuntimeContext(profileID: profile.id, enabled: enabled, privateContext: false,
            background: true, allowedOrigins: [grant.origin], url: grant.url, taskID: nil, snapshotID: nil,
            approvalProposalID: nil)
    }
}

/// State shared by peer threads and the activation actor. Thread-safe.
final class BrowserBridgeRuntimeHub: @unchecked Sendable {
    static let maxPeersPerBrowser = 4
    let enablement: BrowserBridgeEnablement
    let keyring = BrowserBridgeKeyring()
    let contexts: BrowserBridgeNativeContext
    private let lock = NSLock()
    private var channel: BrowserBridgeControlChannel?
    private var backend: pid_t?
    private var nextConnection = 0
    private var sessions: [ObjectIdentifier: (profile: String, session: BrowserBridgePeerSession)] = [:]
    private var slots: [BrowserBridgeBrowser: Int] = [:]

    init(enablement: BrowserBridgeEnablement, contexts: BrowserBridgeNativeContext = BrowserBridgeNativeContext()) {
        self.enablement = enablement
        self.contexts = contexts
    }
    func setChannel(_ channel: BrowserBridgeControlChannel?) {
        lock.lock(); defer { lock.unlock() }
        self.channel = channel
    }
    func setBackendPID(_ pid: pid_t?) {
        lock.lock(); defer { lock.unlock() }
        backend = pid
    }
    func backendPID() -> pid_t? {
        lock.lock(); defer { lock.unlock() }
        return backend
    }
    func call(_ message: [String: Any]) throws -> [String: Any] {
        lock.lock()
        let channel = self.channel
        lock.unlock()
        guard let channel else {
            throw BrowserContractViolation(message: "Bridge control unavailable", code: "bridge_unauthorized")
        }
        return try channel.call(message)
    }
    func expectOK(_ message: [String: Any]) throws -> [String: Any] {
        try BrowserBridgeControl.expectOK(try call(message))
    }
    func newConnectionID() -> Int {
        lock.lock(); defer { lock.unlock() }
        nextConnection = nextConnection >= Int(Int32.max) - 1 ? 1 : nextConnection + 1
        return nextConnection
    }
    func acquireSlot(_ browser: BrowserBridgeBrowser) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard slots[browser, default: 0] < Self.maxPeersPerBrowser else { return false }
        slots[browser, default: 0] += 1
        return true
    }
    func releaseSlot(_ browser: BrowserBridgeBrowser) {
        lock.lock(); defer { lock.unlock() }
        slots[browser] = max(0, slots[browser, default: 0] - 1)
    }
    func register(_ session: BrowserBridgePeerSession, profile: String) {
        lock.lock(); defer { lock.unlock() }
        sessions[ObjectIdentifier(session)] = (profile, session)
    }
    func unregister(_ session: BrowserBridgePeerSession) {
        lock.lock(); defer { lock.unlock() }
        sessions[ObjectIdentifier(session)] = nil
    }
    func closeSessions(profile: String? = nil) {
        lock.lock()
        let doomed = sessions.values.filter { profile == nil || $0.profile == profile }.map { $0.session }
        lock.unlock()
        doomed.forEach { $0.cancel() }
        if let profile { contexts.clear(profile: profile) } else { contexts.clearAll() }
    }
    func isEnabled(_ profile: BrowserBridgeProfile) -> Bool {
        enablement.enabledProfiles().contains(profile.id) && keyring.credential(profile: profile.id) != nil
    }
    func runtime(for profile: BrowserBridgeProfile) throws -> BrowserBridgeRuntimeContext {
        try contexts.runtime(profile: profile, enabled: isEnabled(profile))
    }
    /// The verified native peer's per-click report. Unknown or private denies.
    func beginCapture(profile: BrowserBridgeProfile, url: Any?, nativePrivate: Any?, extensionIncognito: Any?) throws {
        contexts.clear(profile: profile.id)
        try BrowserBridgeWire.check(isEnabled(profile), "Browser bridge disabled")
        // Exact `false` from BOTH the native peer and the extension, or deny (D1/D8).
        guard let native = nativePrivate, let incognito = extensionIncognito,
              WispBrowserContracts.isBool(native), WispBrowserContracts.isBool(incognito),
              native as? Bool == false, incognito as? Bool == false else {
            throw BrowserContractViolation(message: "Private context excluded", code: "private_context")
        }
        guard let url = url as? String else {
            throw BrowserContractViolation(message: "Native URL denied", code: "site_permission_denied")
        }
        let origin = try BrowserBridgeNativeContext.origin(of: url)
        contexts.grant(profile: profile.id, url: url, origin: origin)
        do {
            _ = try expectOK(BrowserBridgeControl.context(profile: profile.id, origin: origin, url: url))
        } catch {
            contexts.clear(profile: profile.id)
            throw error
        }
    }
    func endCapture(profile: String) {
        contexts.clear(profile: profile)
        _ = try? call(BrowserBridgeControl.clearContext(profile: profile))
    }
}

/// Bridges a synchronous peer thread to the A04 client's async API. Peer
/// threads are dedicated Threads, never cooperative-pool threads.
private final class BrowserBridgeBlocking<T>: @unchecked Sendable {
    var result: Result<T, Error>?
}
private func blocking<T>(_ operation: @escaping () async throws -> T) throws -> T {
    let box = BrowserBridgeBlocking<T>()
    let done = DispatchSemaphore(value: 0)
    Task.detached {
        do { box.result = .success(try await operation()) } catch { box.result = .failure(error) }
        done.signal()
    }
    done.wait()
    return try box.result!.get()
}

/// One accepted, OS-verified peer connection. Blocks in run(); always closes.
final class BrowserBridgePeerSession: @unchecked Sendable {
    private let transport: BrowserBridgePeerTransport
    private let browser: BrowserBridgeBrowser
    private let hub: BrowserBridgeRuntimeHub
    private let connection: Int

    init(transport: BrowserBridgePeerTransport, browser: BrowserBridgeBrowser, hub: BrowserBridgeRuntimeHub) {
        self.transport = transport
        self.browser = browser
        self.hub = hub
        connection = hub.newConnectionID()
    }
    func cancel() { transport.cancel() }

    /// Malformed or unexpected peer messages are invalid payloads, never authority.
    private func shape(_ condition: Bool) throws {
        try WispBrowserContracts.require(condition, "Invalid bridge message", "invalid_payload")
    }
    private func message(keys: Set<String>? = nil) throws -> [String: Any] {
        let value = try BrowserBridgeWire.parse(try transport.receive())
        if let keys { try shape(Set(value.keys) == keys) }
        return value
    }
    private func reply(_ value: [String: Any]) {
        if let data = try? BrowserBridgeWire.json(value) { try? transport.send(data) }
    }

    func run() {
        var profile: BrowserBridgeProfile?
        var opened = false
        defer {
            if opened { _ = try? hub.call(BrowserBridgeControl.peerClose(connection: connection)) }
            if opened, let profile { hub.endCapture(profile: profile.id) }
            hub.unregister(self)
            transport.close()
        }
        do {
            let hello = try message(keys: ["op", "profile_id"])
            try shape(hello["op"] as? String == "hello")
            let named = try BrowserBridgeProfile(hello["profile_id"] as? String ?? "")
            // The requirement chose the browser; a peer cannot claim another's profile.
            try BrowserBridgeWire.check(named.browser == browser)
            profile = named
            guard hub.isEnabled(named), let credential = hub.keyring.credential(profile: named.id) else {
                throw BrowserContractViolation(message: "Browser bridge disabled", code: "disabled")
            }
            hub.register(self, profile: named.id)
            let client = try BrowserBridge(identity: credential.identity, key: credential.key,
                                           runtime: { [hub] in try hub.runtime(for: named) })
            let open = try hub.expectOK(BrowserBridgeControl.peerOpen(connection: connection,
                                                                     credentialID: credential.identity.credentialID))
            opened = true
            let challenge = try Self.frame(open)
            let registration = try blocking { try await client.register(challenge: challenge) }
            let registered = try hub.expectOK(BrowserBridgeControl.peerFrame(connection: connection, frame: registration))
            try BrowserBridgeWire.check(registered["event"] as? String == "registered")
            _ = try blocking { try await client.receive(try Self.frame(registered)) }
            reply(["ok": true])
            while true {
                let request = try message()
                switch request["op"] as? String {
                case "begin":
                    try shape(Set(request.keys) == ["op", "url", "native_private_context", "extension_incognito"])
                    try hub.beginCapture(profile: named, url: request["url"],
                                         nativePrivate: request["native_private_context"],
                                         extensionIncognito: request["extension_incognito"])
                    reply(["ok": true])
                case "observation":
                    try shape(Set(request.keys) == ["op", "payload"])
                    guard let payload = request["payload"] as? [String: Any] else {
                        throw BrowserContractViolation(message: "Invalid bridge message", code: "invalid_payload")
                    }
                    // One click, one read: the grant is spent whatever happens next.
                    defer { hub.endCapture(profile: named.id) }
                    let frame = try blocking { try await client.publish(kind: "observation", payload: payload) }
                    let result = try hub.expectOK(BrowserBridgeControl.peerFrame(connection: connection, frame: frame))
                    try BrowserBridgeWire.check(result["event"] as? String == "observation")
                    reply(["ok": true])
                case "disconnect":
                    try shape(Set(request.keys) == ["op"])
                    return
                default:
                    throw BrowserContractViolation(message: "Invalid bridge message", code: "invalid_payload")
                }
            }
        } catch {
            reply(["ok": false, "code": BrowserBridgeControl.code(of: error)])
        }
    }
    /// The first `frames` entry of a control reply, as raw frame bytes.
    private static func frame(_ reply: [String: Any]) throws -> Data {
        guard let frames = reply["frames"] as? [String], frames.count == 1,
              let data = Data(base64Encoded: frames[0]), !data.isEmpty, data.count <= BrowserBridgeWire.maxFrame else {
            throw BrowserContractViolation(message: "Invalid bridge control reply", code: "invalid_payload")
        }
        return data
    }
}

struct BrowserBridgeConfiguration {
    var directory: String
    /// Code requirement each browser's native peer must satisfy. A browser with
    /// no entry cannot be enabled: WP1 (Chrome host) and WP7 (Safari appex) add
    /// theirs when those binaries exist and are signed.
    var peerRequirements: [BrowserBridgeBrowser: String]
    var peerTimeoutSeconds: Double = 30

    var controlPath: String { directory + "/control.sock" }
    func peerPath(_ browser: BrowserBridgeBrowser) -> String { directory + "/" + browser.rawValue + ".sock" }

    static func production() -> BrowserBridgeConfiguration {
        BrowserBridgeConfiguration(
            directory: (NSTemporaryDirectory() as NSString).appendingPathComponent("wisp-browser-bridge"),
            peerRequirements: [:])
    }
}

enum BrowserBridgeUnavailable: Error { case browserNotConfigured }

/// The backend link and peer listeners as the actor sees them. Production uses
/// private Unix sockets; a synthetic qualification can substitute pipes.
protocol BrowserBridgeControlEndpoint: BrowserBridgeControlChannel {
    var onChange: (() -> Void)? { get set }
    func start() throws
    func stop()
}
extension BrowserBridgeControlServer: BrowserBridgeControlEndpoint {}
protocol BrowserBridgeStoppable: AnyObject { func stop() }
extension BrowserBridgeUnixListener: BrowserBridgeStoppable {}

struct BrowserBridgeEndpoints {
    var control: (BrowserBridgeConfiguration, BrowserBridgeRuntimeHub) -> BrowserBridgeControlEndpoint
    var peer: (BrowserBridgeBrowser, String, BrowserBridgeConfiguration, BrowserBridgeRuntimeHub) throws -> BrowserBridgeStoppable

    static let sockets = BrowserBridgeEndpoints(
        control: { configuration, hub in
            BrowserBridgeControlServer(directory: configuration.directory, backendPID: { hub.backendPID() })
        },
        peer: { browser, requirement, configuration, hub in
            try BrowserBridgeUnixListener(path: configuration.peerPath(browser)) { descriptor in
                guard hub.acquireSlot(browser) else { Darwin.close(descriptor); return }
                Thread.detachNewThread {
                    defer { hub.releaseSlot(browser) }
                    // The transport closes the descriptor and throws unless the kernel-reported
                    // peer satisfies this browser's requirement.
                    guard let transport = try? BrowserBridgeTransport(connectedFD: descriptor,
                        peerRequirement: requirement, timeoutSeconds: configuration.peerTimeoutSeconds) else { return }
                    BrowserBridgePeerSession(transport: transport, browser: browser, hub: hub).run()
                }
            }
        })
}

/// Owns activation. Off by default; the listener, sockets, credentials and
/// backend link exist only while a configured browser has an enabled profile.
actor BrowserBridgeActivation {
    static let shared = BrowserBridgeActivation(configuration: .production(),
        enablement: BrowserBridgeDefaultsEnablement(), store: BrowserBridgeKeychainStore())

    nonisolated let configuration: BrowserBridgeConfiguration
    nonisolated let hub: BrowserBridgeRuntimeHub
    private let store: BrowserBridgeKeyringStore
    private let endpoints: BrowserBridgeEndpoints
    private var server: BrowserBridgeControlEndpoint?
    private var peerListeners: [BrowserBridgeBrowser: BrowserBridgeStoppable] = [:]
    private var lifecycles: [String: BrowserBridgeCredentialLifecycle] = [:]
    private var swept = false

    init(configuration: BrowserBridgeConfiguration, enablement: BrowserBridgeEnablement,
         store: BrowserBridgeCredentialStore, contexts: BrowserBridgeNativeContext = BrowserBridgeNativeContext(),
         endpoints: BrowserBridgeEndpoints = .sockets) {
        self.configuration = configuration
        self.endpoints = endpoints
        let runtime = BrowserBridgeRuntimeHub(enablement: enablement, contexts: contexts)
        hub = runtime
        self.store = BrowserBridgeKeyringStore(base: store, keyring: runtime.keyring)
    }

    /// The control endpoint to hand the backend, or nil when no browser can
    /// ever activate. The path is fixed; the backend only checks it exists.
    nonisolated func controlPathForBackend() -> String? {
        configuration.peerRequirements.isEmpty ? nil : configuration.controlPath
    }
    /// BackendManager reports the child it launched; only that PID is accepted.
    nonisolated func backendLaunched(pid: pid_t) { hub.setBackendPID(pid) }

    /// Launch hook. A no-op unless a configured browser is enabled.
    func begin() async { await reconcile() }

    /// The user's per-profile toggle (D4/D7). Enabling needs a configured peer requirement.
    func setEnabled(_ enabled: Bool, profile: String) async throws {
        let target = try BrowserBridgeProfile(profile)
        if enabled {
            guard configuration.peerRequirements[target.browser] != nil else {
                throw BrowserBridgeUnavailable.browserNotConfigured
            }
        }
        let wasEnabled = hub.enablement.enabledProfiles().contains(target.id)
        hub.enablement.setEnabled(enabled, profile: target.id)
        await reconcile()
        // A record left by a crashed run is unreachable but must not outlive a disable.
        if !enabled, wasEnabled, !swept { try? store.base.removeAll() }
    }
    /// D7 "forget": disable, close live peers, revoke and delete this profile's
    /// credential. Re-enabling issues a fresh credential and key.
    func forget(profile: String) async throws {
        let target = try BrowserBridgeProfile(profile)
        hub.enablement.setEnabled(false, profile: target.id)
        hub.closeSessions(profile: target.id)
        await revoke(profileID: target.id)
        hub.keyring.remove(profile: target.id)
        await reconcile()
    }

    /// App quit: close peers, revoke credentials and remove every endpoint.
    func shutdown() async { await teardown() }

    func reconcile() async {
        let desired = hub.enablement.enabledProfiles().compactMap { try? BrowserBridgeProfile($0) }
            .filter { configuration.peerRequirements[$0.browser] != nil }
        guard !desired.isEmpty else { return await teardown() }
        guard startServer() else { return await teardown() }
        let browsers = Set(desired.map { $0.browser })
        for browser in BrowserBridgeBrowser.allCases {
            if browsers.contains(browser) { startPeerListener(browser) } else { stopPeerListener(browser) }
        }
        let wanted = Set(desired.map { $0.id })
        for id in Array(lifecycles.keys) where !wanted.contains(id) { await revoke(profileID: id) }
        guard server?.isConnected == true else { return }
        if !swept {
            // The backend forgot every registration when it last restarted, so any
            // record an earlier run left in the Keychain can never be used again.
            try? store.base.removeAll()
            swept = true
        }
        for profile in desired where lifecycles[profile.id] == nil { await provision(profile) }
    }

    /// The backend link connected or dropped. Its registrations are gone either
    /// way, so nothing is carried across: drop every credential, then reissue
    /// fresh ones if a backend is connected right now.
    func controlChanged() async {
        await dropCredentials()
        await reconcile()
    }

    private func startServer() -> Bool {
        if server != nil { return true }
        let candidate = endpoints.control(configuration, hub)
        candidate.onChange = { [weak self] in Task { await self?.controlChanged() } }
        do { try candidate.start() } catch { candidate.stop(); return false }
        server = candidate
        hub.setChannel(candidate)
        return true
    }
    private func startPeerListener(_ browser: BrowserBridgeBrowser) {
        guard peerListeners[browser] == nil, let requirement = configuration.peerRequirements[browser] else { return }
        peerListeners[browser] = try? endpoints.peer(browser, requirement, configuration, hub)
    }
    private func stopPeerListener(_ browser: BrowserBridgeBrowser) {
        peerListeners.removeValue(forKey: browser)?.stop()
    }
    private func provision(_ profile: BrowserBridgeProfile) async {
        let hub = self.hub
        let lifecycle = BrowserBridgeCredentialLifecycle(store: store,
            provision: { identity, key in _ = try hub.expectOK(BrowserBridgeControl.provision(identity, key: key)) },
            revoke: { id in
                let reply = try hub.expectOK(BrowserBridgeControl.revoke(credentialID: id))
                return reply["uncertain_actions"] as? [String] ?? []
            })
        do {
            try await lifecycle.create(try profile.newIdentity())
            lifecycles[profile.id] = lifecycle
            hub.keyring.activate(profile: profile.id)
        } catch {
            // Left off; the next connect or toggle retries with a fresh identity.
            hub.keyring.remove(profile: profile.id)
        }
    }
    private func revoke(profileID: String) async {
        hub.closeSessions(profile: profileID)
        hub.keyring.remove(profile: profileID)
        if let lifecycle = lifecycles.removeValue(forKey: profileID) { try? await lifecycle.revoke() }
    }
    private func dropCredentials() async {
        hub.closeSessions()
        for id in Array(lifecycles.keys) { await revoke(profileID: id) }
        hub.keyring.removeAll()
        if swept { try? store.base.removeAll() }
    }
    private func teardown() async {
        guard server != nil || !peerListeners.isEmpty || !lifecycles.isEmpty else { return }
        await dropCredentials()
        for browser in BrowserBridgeBrowser.allCases { stopPeerListener(browser) }
        hub.setChannel(nil)
        server?.stop()
        server = nil
        swept = false
    }
}
