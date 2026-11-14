import Foundation
import Security

enum StudyCredentialError: LocalizedError {
    case invalidKey
    case storage

    var errorDescription: String? {
        switch self {
        case .invalidKey: return "Enter a valid API key without spaces or line breaks."
        case .storage: return "Wisp could not save the Study key in macOS Keychain."
        }
    }
}

/// The Study connector's key (docs/STUDY_TAB.md): a generic password in the
/// login keychain, service `com.wisp.study`, account = provider id. It never
/// crosses HTTP, is never logged and never written to a file; the backend reads
/// it with `/usr/bin/security find-generic-password -s com.wisp.study -a <id> -w`.
///
/// Written the way Settings' CloudCredentialStore writes the cloud key: update
/// in place, else add, in the file-based login keychain (not the data
/// protection keychain, which `security` cannot read), never synchronized.
enum StudyCredentialStore {
    static let service = "com.wisp.study"
    static let providers: Set<String> = ["openrouter", "gemini"]
    static let maxBytes = 4096

    /// The key as it will be stored: whitespace and line breaks trimmed from the
    /// ends, then printable ASCII only (33...126), 1...4096 bytes. Nil when invalid.
    static func normalized(_ key: String) -> String? {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.utf8.count <= maxBytes,
              trimmed.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 })
        else { return nil }
        return trimmed
    }

    static func save(_ key: String, provider: String) throws {
        guard providers.contains(provider), let secret = normalized(key) else {
            throw StudyCredentialError.invalidKey
        }
        #if WISP_NOTCH_QA
        QAKeys.values[provider] = secret
        #else
        try write(Data(secret.utf8), provider: provider)
        #endif
    }

    static func remove(provider: String) {
        guard providers.contains(provider) else { return }
        #if WISP_NOTCH_QA
        QAKeys.values[provider] = nil
        #else
        _ = SecItemDelete(query(provider: provider) as CFDictionary)
        #endif
    }

    /// Whether a key is saved. Asks for attributes only, never the secret, so it
    /// never brings up a Keychain prompt.
    static func has(provider: String) -> Bool {
        guard providers.contains(provider) else { return false }
        #if WISP_NOTCH_QA
        return QAKeys.values[provider] != nil
        #else
        var q = query(provider: provider)
        q[kSecReturnAttributes as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        return SecItemCopyMatching(q as CFDictionary, &result) == errSecSuccess
        #endif
    }

    #if WISP_NOTCH_QA
    /// Fixture builds never touch the real Keychain.
    private enum QAKeys {
        static var values: [String: String] = [:]
    }
    #else
    private static func query(provider: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: provider,
        ]
    }

    private static func write(_ data: Data, provider: String) throws {
        let query = query(provider: provider)
        let attributes: [String: Any] = [kSecValueData as String: data]
        let updated = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw StudyCredentialError.storage }
        var item = query
        item[kSecValueData as String] = data
        item[kSecAttrLabel as String] = "Wisp Study key (\(provider))"
        item[kSecAttrSynchronizable as String] = false
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
            throw StudyCredentialError.storage
        }
    }
    #endif
}
