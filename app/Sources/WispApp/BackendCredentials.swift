import Foundation
import Security
import LocalAuthentication
import Darwin

/// Fixed namespace shared by the launcher and the provisioning helper.
/// Values exist only in Keychain and process memory; never describe errors with data.
enum BackendCredentials {
    static let service = "com.wisp.inference"
    static let accounts = [
        "local-omlx": "WISP_LOCAL_OMLX_KEY",
        "mini-inference": "WISP_MINI_INFERENCE_KEY",
        "mini-node": "WISP_MINI_NODE_KEY",
    ]
    enum Failure: Error { case unavailable, malformed, storage, random, readerMismatch, quarantined }

    struct Snapshot {
        let credentials: [String: String]
        let generation: String
    }

    /// Only ENOENT means absent: dangling symlinks and unreadable paths block use.
    static func generation(home: String = NSHomeDirectory()) throws -> String {
        let directory = home + "/.moe"
        var info = stat()
        guard lstat(directory, &info) == 0 else {
            if errno == ENOENT { return "absent" }
            throw Failure.quarantined
        }
        guard info.st_mode & S_IFMT == S_IFDIR, info.st_uid == getuid(),
              info.st_mode & 0o022 == 0 else { throw Failure.quarantined }
        let marker = directory + "/.helper-transaction.json"
        guard lstat(marker, &info) != 0, errno == ENOENT else { throw Failure.quarantined }
        let fd = open(directory + "/.credential-generation", O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else {
            if errno == ENOENT { return "absent" }
            throw Failure.quarantined
        }
        defer { close(fd) }
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1,
              info.st_mode & 0o7777 == 0o600, info.st_size == 64 else { throw Failure.quarantined }
        var bytes = [UInt8](repeating: 0, count: 65)
        let count = bytes.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
        guard count == 64, let value = String(bytes: bytes.prefix(64), encoding: .utf8),
              valid(value) else { throw Failure.quarantined }
        guard lstat(marker, &info) != 0, errno == ENOENT else { throw Failure.quarantined }
        return value
    }

    /// Provisioning uses raw load under its exclusive transaction lock. App
    /// launches must instead bind every read to this single recovery generation.
    static func loadForBackend(reader: (String) throws -> String? = read,
                               state: () throws -> String = { try generation() }) throws -> Snapshot {
        let expected = try state()
        guard expected == "absent" || valid(expected) else { throw Failure.quarantined }
        func check() throws {
            guard try state() == expected else { throw Failure.quarantined }
        }
        let credentials = try loadForProvisioning { account in
            try check()
            let value = try reader(account)
            try check()
            return value
        }
        try check()
        return Snapshot(credentials: credentials, generation: expected)
    }

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

    static func expectedReaders() -> [String] {
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        let sibling = executable.deletingLastPathComponent().appendingPathComponent("mini-launcher")
        if executable.lastPathComponent == "mini-launcher" || executable.lastPathComponent == "keychain-helper" {
            return [sibling.deletingLastPathComponent().appendingPathComponent("keychain-helper").path, sibling.path]
        }
        return [NSHomeDirectory() + "/.moe/provisioning/wisp-keychain-helper", "/Applications/Wisp.app"]
    }

    static func trustedIdentity(_ path: String) throws -> Data {
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, [], &code) == errSecSuccess,
              let code, SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSStrictValidate), nil) == errSecSuccess else {
            throw Failure.unavailable
        }
        var app: SecTrustedApplication?
        var data: CFData?
        guard SecTrustedApplicationCreateFromPath(path, &app) == errSecSuccess, let app,
              SecTrustedApplicationCopyData(app, &data) == errSecSuccess, let data else { throw Failure.unavailable }
        return data as Data
    }

    /// A nil list means trust-all. Every decrypt/ANY ACL must have precisely the
    /// reviewed reader identities; extra ACLs cannot silently broaden access.
    static func validateReaderSets(_ sets: [Set<Data>?], expected: Set<Data>) throws {
        guard !expected.isEmpty, !sets.isEmpty,
              sets.allSatisfy({ $0 == expected }) else { throw Failure.readerMismatch }
    }

    @discardableResult
    static func verifyAccess(_ account: String, readers: [String]) throws -> Bool {
        var q = query(account)
        q[kSecReturnRef as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        if status == errSecItemNotFound { return false }
        guard status == errSecSuccess, let result,
              CFGetTypeID(result) == SecKeychainItemGetTypeID() else { throw Failure.unavailable }
        let item = unsafeBitCast(result, to: SecKeychainItem.self)
        try verifyItemAccess(item, readers: readers)
        return true
    }

    /// Inspect an already scoped item. The isolated native fixture supplies only
    /// references obtained from its explicit temporary Keychain search list.
    static func verifyItemAccess(_ item: SecKeychainItem, readers: [String]) throws {
        var access: SecAccess?
        var list: CFArray?
        guard SecKeychainItemCopyAccess(item, &access) == errSecSuccess, let access,
              SecAccessCopyACLList(access, &list) == errSecSuccess,
              let acls = list as? [SecACL] else { throw Failure.unavailable }
        let expected = try Set(readers.map(trustedIdentity))
        var sets: [Set<Data>?] = []
        for acl in acls {
            guard let authorizations = SecACLCopyAuthorizations(acl) as? [String] else { throw Failure.unavailable }
            if !authorizations.contains(kSecACLAuthorizationDecrypt as String) && !authorizations.contains(kSecACLAuthorizationAny as String) { continue }
            var applications: CFArray?
            var description: CFString?
            var selector = SecKeychainPromptSelector()
            guard SecACLCopyContents(acl, &applications, &description, &selector) == errSecSuccess else { throw Failure.unavailable }
            guard let apps = applications as? [SecTrustedApplication] else { sets.append(nil); continue }
            var identities = Set<Data>()
            for app in apps {
                var data: CFData?
                guard SecTrustedApplicationCopyData(app, &data) == errSecSuccess, let data else { throw Failure.unavailable }
                identities.insert(data as Data)
            }
            sets.append(identities)
        }
        try validateReaderSets(sets, expected: expected)
    }

    static func read(_ account: String) throws -> String? {
        guard accounts[account] != nil else { throw Failure.unavailable }
        guard try verifyAccess(account, readers: expectedReaders()) else { return nil }
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
        guard try verifyAccess(account, readers: readers) else { throw Failure.storage }
    }

    /// Explicit local-only compare-and-swap; the caller holds quarantine and
    /// exclusive provisioning lock. Existing ACLs are preserved, never widened.
    static func replaceLocal(expected: String?, replacement: String?) throws {
        guard expected == nil || valid(expected!), replacement == nil || valid(replacement!) else {
            throw Failure.malformed
        }
        guard try read("local-omlx") == expected else { throw Failure.storage }
        for account in ["mini-inference", "mini-node"] {
            if let remote = try read(account), let replacement, remote == replacement { throw Failure.malformed }
        }
        if let replacement {
            if expected == nil {
                try add("local-omlx", value: replacement, readers: expectedReaders())
            } else {
                guard SecItemUpdate(query("local-omlx") as CFDictionary,
                    [kSecValueData as String: Data(replacement.utf8)] as CFDictionary) == errSecSuccess else {
                    throw Failure.storage
                }
            }
        } else if expected != nil {
            guard SecItemDelete(query("local-omlx") as CFDictionary) == errSecSuccess else { throw Failure.storage }
        }
        guard try read("local-omlx") == replacement else { throw Failure.storage }
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

    static func load(reader: (String) throws -> String? = read,
                     state: () throws -> String = { try generation() }) throws -> [String: String] {
        try loadForBackend(reader: reader, state: state).credentials
    }

    /// Only the provisioning helper, invoked under its exclusive lock, may use
    /// this entry point while validating an unfinished helper transaction.
    static func loadForProvisioning(reader: (String) throws -> String? = read) throws -> [String: String] {
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
