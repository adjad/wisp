import Foundation
import Security
import LocalAuthentication

/// Fixed namespace shared by the launcher and the provisioning helper.
/// Values exist only in Keychain and process memory; never describe errors with data.
enum BackendCredentials {
    static let service = "com.wisp.inference"
    static let accounts = [
        "local-omlx": "WISP_LOCAL_OMLX_KEY",
        "mini-inference": "WISP_MINI_INFERENCE_KEY",
        "mini-node": "WISP_MINI_NODE_KEY",
    ]
    enum Failure: Error { case unavailable, malformed, storage, random }

    static func valid(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy {
            (48...57).contains($0) || (97...102).contains($0)
        }
    }

    static func query(_ account: String) -> [String: Any] {
        let context = LAContext()
        context.interactionNotAllowed = true
        return [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account,
                kSecUseAuthenticationContext as String: context]
    }

    static func read(_ account: String) throws -> String? {
        guard accounts[account] != nil else { throw Failure.unavailable }
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw Failure.unavailable }
        guard let data = result as? Data, let value = String(data: data, encoding: .utf8),
              valid(value) else { throw Failure.malformed }
        return value
    }

    /// No automatic rotation or replacement of existing entries.
    static func add(_ account: String, value: String, readers: [String]) throws {
        guard valid(value), accounts[account] != nil else { throw Failure.malformed }
        var trusted: [SecTrustedApplication] = []
        for path in readers {
            var app: SecTrustedApplication?
            guard SecTrustedApplicationCreateFromPath(path, &app) == errSecSuccess,
                  let app else { throw Failure.storage }
            trusted.append(app)
        }
        guard !trusted.isEmpty else { throw Failure.storage }
        var access: SecAccess?
        guard SecAccessCreate("Wisp inference credential" as CFString, trusted as CFArray, &access) == errSecSuccess,
              let access else { throw Failure.storage }
        var q = query(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccess as String] = access
        guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else { throw Failure.storage }
    }

    static func initialize(local: String, readers: [String],
                           reader: (String) throws -> String? = read,
                           writer: (String, String, [String]) throws -> Void = add) throws {
        // Import only an already configured 256-bit local token. Generating a
        // different token here would break the running oMLX authentication.
        guard valid(local) else { throw Failure.malformed }
        if let existing = try reader("local-omlx"), existing != local { throw Failure.malformed }
        // Validate every existing entry before creating anything.
        var existingValues: Set<String> = [local]
        let missing = try accounts.keys.sorted().filter {
            guard let value = try reader($0) else { return true }
            guard valid(value) else { throw Failure.malformed }
            if $0 != "local-omlx" {
                guard existingValues.insert(value).inserted else { throw Failure.malformed }
            }
            return false
        }
        for account in missing {
            var bytes = [UInt8](repeating: 0, count: 32)
            guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess
                else { throw Failure.random }
            let value = account == "local-omlx" ? local : bytes.map { String(format: "%02x", $0) }.joined()
            try writer(account, value, readers)
        }
    }

    static func load(reader: (String) throws -> String? = read) throws -> [String: String] {
        var values: [String: String] = [:]
        for account in accounts.keys.sorted() {
            if let value = try reader(account) {
                guard valid(value) else { throw Failure.malformed }
                values[accounts[account]!] = value
            }
        }
        guard Set(values.values).count == values.count else { throw Failure.malformed }
        return values
    }

    static func injecting(_ credentials: [String: String], into base: [String: String]) -> [String: String] {
        var env = base
        for name in accounts.values { env.removeValue(forKey: name) }
        for name in accounts.values {
            if let value = credentials[name], valid(value) { env[name] = value }
        }
        return env
    }
}
