import Foundation
import Security

/// The Mac pairing token and the cloud API key. Stored this-device-only, so they never
/// sync to iCloud Keychain or move to a restored backup on another phone.
enum Keychain {
    enum Item: String {
        case macToken = "mac-gateway-token"
        case cloudKey = "cloud-api-key"
    }

    private static let service = "app.wisp.iphone"

    static func read(_ item: Item) -> String? {
        var query = base(item)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func write(_ value: String, for item: Item) -> Bool {
        delete(item)
        var query = base(item)
        query[kSecValueData as String] = Data(value.utf8)
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }

    static func delete(_ item: Item) {
        SecItemDelete(base(item) as CFDictionary)
    }

    private static func base(_ item: Item) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: item.rawValue]
    }
}
