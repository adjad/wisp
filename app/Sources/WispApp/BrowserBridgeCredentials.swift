import Foundation
import Security

/// Dedicated, non-synchronizing Keychain namespace. Secrets never go to logs,
/// UserDefaults, arguments, environment, extension storage or shared containers.
/// Bootstrap transfers the backend copy ONLY through protected inherited IPC.
/// Tests inject synthetic Data directly and never call this Keychain adapter.
enum BrowserBridgeCredentials {
    private static let service = "com.wisp.browser-bridge.v1"
    enum Operation: CaseIterable { case add, lookup, delete }

    /// Pure configuration builder so synthetic tests can check all operations
    /// without calling Security or touching a user's Keychain.
    static func query(_ identity: BrowserBridgeIdentity, operation: Operation) throws -> [String: Any] {
        try identity.validate()
        var item: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: identity.credentialID,
            kSecAttrSynchronizable as String: false, kSecUseDataProtectionKeychain as String: true]
        // On macOS accessibility attributes apply to the Data Protection
        // Keychain. Never fall back to the legacy keychain or synchronization.
        switch operation {
        case .add:
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        case .lookup:
            item[kSecReturnData as String] = true
            item[kSecMatchLimit as String] = kSecMatchLimitOne
            // Explicit failure without UI remains available, though deprecated.
            // LAContext's flag did not persist in the isolated native fixture;
            // do not silently accept its default interactive behavior.
            item[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail
        case .delete: break
        }
        return item
    }
    static func create(_ identity: BrowserBridgeIdentity) throws -> Data {
        var key = Data(count: 32)
        let status = key.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, 32, $0.baseAddress!) }
        try BrowserBridgeWire.check(status == errSecSuccess, "Bridge credential unavailable")
        var item = try query(identity, operation: .add)
        // Bind peer, role and profile to the protected record, never to a caller's handshake.
        let record = Record(identity: identity, key: key)
        item[kSecValueData as String] = try JSONEncoder().encode(record)
        try BrowserBridgeWire.check(SecItemAdd(item as CFDictionary, nil) == errSecSuccess, "Bridge credential unavailable")
        return key
    }
    static func load(_ identity: BrowserBridgeIdentity) throws -> Data {
        let item = try query(identity, operation: .lookup)
        var result: CFTypeRef?
        try BrowserBridgeWire.check(SecItemCopyMatching(item as CFDictionary, &result) == errSecSuccess,
                                    "Bridge credential unavailable")
        guard let data = result as? Data, let record = try? JSONDecoder().decode(Record.self, from: data),
              record.identity == identity, record.key.count == 32 else {
            throw BrowserContractViolation(message: "Bridge credential unavailable", code: "bridge_unauthorized")
        }
        return record.key
    }
    /// Trusted bootstrap revokes the backend registration first, then deletes
    /// this record. Rotation uses a fresh credential ID and a fresh random key.
    static func remove(_ identity: BrowserBridgeIdentity) throws {
        let status = SecItemDelete(try query(identity, operation: .delete) as CFDictionary)
        try BrowserBridgeWire.check(status == errSecSuccess || status == errSecItemNotFound,
                                    "Bridge credential unavailable")
    }
    /// Selects every record in this namespace. Used only at trusted bootstrap to
    /// sweep records a previous run left behind: the backend forgets every
    /// registration on restart, so a persisted key can never be reused.
    static func sweepQuery() -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrSynchronizable as String: false, kSecUseDataProtectionKeychain as String: true]
    }
    static func removeAll() throws {
        let status = SecItemDelete(sweepQuery() as CFDictionary)
        try BrowserBridgeWire.check(status == errSecSuccess || status == errSecItemNotFound,
                                    "Bridge credential unavailable")
    }
    private struct Record: Codable { let identity: BrowserBridgeIdentity; let key: Data }
}

/// Injectable storage keeps lifecycle qualification separate from real Keychain
/// eligibility. Production callers use BrowserBridgeKeychainStore explicitly.
protocol BrowserBridgeCredentialStore {
    // Atomic insert: failure must not create an owned record. Never replace an
    // existing identity. This is the SecItemAdd contract of the native adapter.
    func create(_ identity: BrowserBridgeIdentity) throws -> Data
    func remove(_ identity: BrowserBridgeIdentity) throws
    /// Bootstrap sweep of records left by an earlier run. Default: nothing stored.
    func removeAll() throws
}
extension BrowserBridgeCredentialStore {
    func removeAll() throws {}
}
struct BrowserBridgeKeychainStore: BrowserBridgeCredentialStore {
    func create(_ identity: BrowserBridgeIdentity) throws -> Data { try BrowserBridgeCredentials.create(identity) }
    func remove(_ identity: BrowserBridgeIdentity) throws { try BrowserBridgeCredentials.remove(identity) }
    func removeAll() throws { try BrowserBridgeCredentials.removeAll() }
}

/// Serialized, inactive bootstrap lifecycle. Backend hooks are trusted in-process
/// operations over the protected bootstrap, never peer-supplied RPCs. Revoke must
/// invalidate backend sessions AND close transports before it returns. A failed
/// operation leaves this owner disabled with cleanup retained for another revoke.
/// This is not a durable credential registry; startup must remain disabled until
/// any retained Keychain identity has been explicitly reconciled by bootstrap.
actor BrowserBridgeCredentialLifecycle {
    private let store: BrowserBridgeCredentialStore
    private let provisionBackend: (BrowserBridgeIdentity, Data) throws -> Void
    private let revokeBackend: (String) throws -> [String]
    private var active: BrowserBridgeIdentity?
    private var cleanup: [BrowserBridgeIdentity] = []
    private var issued = Set<String>()
    private var uncertain = Set<String>()

    init(store: BrowserBridgeCredentialStore,
         provision: @escaping (BrowserBridgeIdentity, Data) throws -> Void,
         revoke: @escaping (String) throws -> [String]) {
        self.store = store
        provisionBackend = provision
        revokeBackend = revoke
    }
    func status() -> (active: BrowserBridgeIdentity?, cleanupRequired: Bool, uncertain: [String]) {
        (active, !cleanup.isEmpty && active == nil, uncertain.sorted())
    }
    func create(_ identity: BrowserBridgeIdentity) throws {
        try identity.validate()
        try BrowserBridgeWire.check(active == nil && cleanup.isEmpty &&
            !issued.contains(identity.credentialID), "Bridge lifecycle unavailable")
        issued.insert(identity.credentialID)
        do {
            let key = try store.create(identity)
            cleanup.append(identity)
            try BrowserBridgeWire.check(key.count == 32, "Bridge credential unavailable")
            try provisionBackend(identity, key)
            active = identity
        } catch {
            // Provision may have succeeded before its response was lost. Try
            // revocation immediately; retain ownership if cleanup also fails.
            // No local state can prove a failed remote revoke took effect.
            active = nil
            try? revoke()
            throw error
        }
    }
    func revoke() throws {
        active = nil
        while let identity = cleanup.first {
            uncertain.formUnion(try revokeBackend(identity.credentialID))
            try store.remove(identity)
            cleanup.removeFirst()
        }
    }
    func rotate(to identity: BrowserBridgeIdentity) throws {
        try identity.validate()
        guard let previous = active else {
            throw BrowserContractViolation(message: "Bridge lifecycle unavailable", code: "bridge_unauthorized")
        }
        try BrowserBridgeWire.check(identity.peer == previous.peer &&
            identity.credentialRole == previous.credentialRole && identity.profileID == previous.profileID &&
            !issued.contains(identity.credentialID), "Bridge rotation scope denied")
        try revoke()
        try create(identity)
    }
}

// MARK: - A10 WP3 per-browser enablement and in-memory keyring

/// The two browsers are separate identities. The browser is encoded in the
/// credential ID prefix and the profile ID prefix; the backend requires both
/// to name the same browser. IDs are generated natively, never by a peer.
enum BrowserBridgeBrowser: String, CaseIterable {
    case chrome
    case safari
}

/// A browser profile the user opted in (D7). `id` is `"<browser>:<profile>"`.
struct BrowserBridgeProfile: Hashable {
    let browser: BrowserBridgeBrowser
    let id: String

    init(_ id: String) throws {
        try BrowserBridgeWire.identifier(id)
        guard let browser = BrowserBridgeBrowser.allCases.first(where: {
            id.hasPrefix($0.rawValue + ":") && id.utf8.count > $0.rawValue.utf8.count + 1
        }) else {
            throw BrowserContractViolation(message: "Bridge profile denied", code: "bridge_unauthorized")
        }
        self.browser = browser
        self.id = id
    }

    /// Fresh per activation: the backend never accepts a reissued credential ID.
    func newIdentity() throws -> BrowserBridgeIdentity {
        var bytes = [UInt8](repeating: 0, count: 16)
        try BrowserBridgeWire.check(SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess,
                                    "Bridge credential unavailable")
        return BrowserBridgeIdentity(credentialID: browser.rawValue + "-" + bytes.map { String(format: "%02x", $0) }.joined(),
                                     peer: "extension", credentialRole: "native_bridge", profileID: id)
    }
}

/// D4: off by default, per browser profile. Reading is strict: anything but an
/// array of valid profile IDs means "nothing enabled".
protocol BrowserBridgeEnablement: AnyObject {
    func enabledProfiles() -> Set<String>
    func setEnabled(_ enabled: Bool, profile: String)
}

final class BrowserBridgeDefaultsEnablement: BrowserBridgeEnablement {
    static let key = "wisp.browserBridge.enabledProfiles.v1"
    private let defaults: UserDefaults
    private let lock = NSLock()

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func enabledProfiles() -> Set<String> {
        lock.lock(); defer { lock.unlock() }
        guard let raw = defaults.object(forKey: Self.key) as? [String] else { return [] }
        return Set(raw.filter { (try? BrowserBridgeProfile($0)) != nil })
    }
    func setEnabled(_ enabled: Bool, profile: String) {
        guard (try? BrowserBridgeProfile(profile)) != nil else { return }
        lock.lock(); defer { lock.unlock() }
        var current = Set((defaults.object(forKey: Self.key) as? [String]) ?? [])
        if enabled { current.insert(profile) } else { current.remove(profile) }
        defaults.set(current.sorted(), forKey: Self.key)
    }
}

/// Backend keys, in memory only, for the peers the app currently serves. Keys
/// never leave the app: a peer sends plain observations over an OS-verified
/// transport and the app seals them. Entries are usable only once the backend
/// has accepted the credential (`activate`), and vanish on revoke or restart.
final class BrowserBridgeKeyring: @unchecked Sendable {
    private struct Entry { let identity: BrowserBridgeIdentity; let key: Data; var active: Bool }
    private var entries: [String: Entry] = [:]
    private let lock = NSLock()

    func stage(_ identity: BrowserBridgeIdentity, key: Data) {
        lock.lock(); defer { lock.unlock() }
        entries[identity.profileID] = Entry(identity: identity, key: key, active: false)
    }
    func activate(profile: String) {
        lock.lock(); defer { lock.unlock() }
        entries[profile]?.active = true
    }
    func remove(credentialID: String) {
        lock.lock(); defer { lock.unlock() }
        for (profile, entry) in entries where entry.identity.credentialID == credentialID { entries[profile] = nil }
    }
    func remove(profile: String) {
        lock.lock(); defer { lock.unlock() }
        entries[profile] = nil
    }
    func removeAll() {
        lock.lock(); defer { lock.unlock() }
        entries.removeAll()
    }
    func credential(profile: String) -> (identity: BrowserBridgeIdentity, key: Data)? {
        lock.lock(); defer { lock.unlock() }
        guard let entry = entries[profile], entry.active else { return nil }
        return (entry.identity, entry.key)
    }
    func activeProfiles() -> Set<String> {
        lock.lock(); defer { lock.unlock() }
        return Set(entries.filter { $0.value.active }.keys)
    }
}

/// Wraps the durable store so the lifecycle's freshly created key is also staged
/// in the keyring, and removed with the record.
struct BrowserBridgeKeyringStore: BrowserBridgeCredentialStore {
    let base: BrowserBridgeCredentialStore
    let keyring: BrowserBridgeKeyring

    func create(_ identity: BrowserBridgeIdentity) throws -> Data {
        let key = try base.create(identity)
        keyring.stage(identity, key: key)
        return key
    }
    func remove(_ identity: BrowserBridgeIdentity) throws {
        keyring.remove(credentialID: identity.credentialID)
        try base.remove(identity)
    }
    func removeAll() throws {
        keyring.removeAll()
        try base.removeAll()
    }
}
