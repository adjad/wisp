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
    private struct Record: Codable { let identity: BrowserBridgeIdentity; let key: Data }
}
