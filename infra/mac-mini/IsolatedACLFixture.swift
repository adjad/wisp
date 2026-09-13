// Qualification only. Never uses BackendCredentials.query/read/add/initialize/load.
// Every store operation is scoped to one private temporary Keychain reference.
import Foundation
import Security
import LocalAuthentication
import Darwin

@main
struct IsolatedACLFixture {
    enum Failure: Error { case isolation, storePathUnavailable, storePathMismatch, storeFileMismatch, storage, osDenied }
    static let service = "com.wisp.synthetic.acl-qualification"
    static let account = "synthetic-only"

    static func checkedRoot(_ path: String) throws -> URL {
        let root = URL(fileURLWithPath: path).standardizedFileURL
        var info = stat()
        guard root.path == root.resolvingSymlinksInPath().path,
              root.lastPathComponent.hasPrefix("wisp-acl-fixture-"),
              lstat(root.path, &info) == 0, info.st_uid == getuid(),
              info.st_mode & S_IFMT == S_IFDIR, info.st_mode & 0o777 == 0o700 else { throw Failure.isolation }
        return root
    }

    static func checkedStore(_ keychain: SecKeychain, root: URL) throws {
        var buffer = [CChar](repeating: 0, count: 4096)
        var size = UInt32(buffer.count)
        guard SecKeychainGetPath(keychain, &size, &buffer) == errSecSuccess else { throw Failure.storePathUnavailable }
        let expected = root.appendingPathComponent("synthetic.keychain-db")
        let reported = URL(fileURLWithPath: String(cString: buffer))
        // Security may report /var while the private root is canonical /private/var.
        // Both names must resolve to this exact store; the store itself cannot be a symlink.
        guard reported.resolvingSymlinksInPath().path == expected.path else { throw Failure.storePathMismatch }
        var info = stat()
        var reportedInfo = stat()
        guard lstat(expected.path, &info) == 0, lstat(reported.path, &reportedInfo) == 0,
              info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
              reportedInfo.st_mode & S_IFMT == S_IFREG,
              info.st_dev == reportedInfo.st_dev, info.st_ino == reportedInfo.st_ino else { throw Failure.storeFileMismatch }
    }

    static func metadata(_ keychain: SecKeychain) throws -> [String: Any] {
        var buffer = [CChar](repeating: 0, count: 4096)
        var size = UInt32(buffer.count)
        var status: SecKeychainStatus = 0
        guard SecKeychainGetPath(keychain, &size, &buffer) == errSecSuccess,
              SecKeychainGetStatus(keychain, &status) == errSecSuccess else { throw Failure.storage }
        return ["path": String(cString: buffer), "status_bits": status]
    }

    static func ambientMetadata() throws -> [String: Any] {
        // Metadata only: no item queries, unlocks, or writes to these references.
        var defaultStore: SecKeychain?
        let status = SecKeychainCopyDefault(&defaultStore)
        let defaultState: Any
        if status == errSecNoDefaultKeychain {
            defaultState = NSNull()
        } else {
            guard status == errSecSuccess, let store = defaultStore else { throw Failure.storage }
            defaultState = try metadata(store)
        }
        var list: CFArray?
        guard SecKeychainCopySearchList(&list) == errSecSuccess,
              let stores = list as? [SecKeychain], stores.count <= 1000 else { throw Failure.storage }
        return ["schema_version": 1, "default": defaultState,
                "search_list": try stores.map { try metadata($0) }]
    }

    static func scopedQuery(_ keychain: SecKeychain) -> [String: Any] {
        let context = LAContext()
        context.interactionNotAllowed = true
        return [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service, kSecAttrAccount as String: account,
                kSecMatchSearchList as String: [keychain],
                kSecUseAuthenticationContext as String: context]
    }

    static func item(_ keychain: SecKeychain) throws -> SecKeychainItem {
        var query = scopedQuery(keychain)
        query[kSecReturnRef as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        try checkedReadStatus(status)
        guard let result, CFGetTypeID(result) == SecKeychainItemGetTypeID() else { throw Failure.storage }
        return unsafeBitCast(result, to: SecKeychainItem.self)
    }

    static func checkedReadStatus(_ status: OSStatus) throws {
        if status == errSecAuthFailed || status == errSecInteractionNotAllowed {
            throw Failure.osDenied
        }
        guard status == errSecSuccess else { throw Failure.storage }
    }

    static func access(_ readers: [String]) throws -> SecAccess {
        var applications: [SecTrustedApplication] = []
        for path in readers {
            _ = try BackendCredentials.trustedIdentity(path)
            var application: SecTrustedApplication?
            guard SecTrustedApplicationCreateFromPath(path, &application) == errSecSuccess,
                  let application else { throw Failure.storage }
            applications.append(application)
        }
        var value: SecAccess?
        guard SecAccessCreate("Synthetic ACL fixture" as CFString, applications as CFArray, &value) == errSecSuccess,
              let value else { throw Failure.storage }
        return value
    }

    static func syntheticPassword() throws -> [UInt8] {
        let input = FileHandle.standardInput.readData(ofLength: 1025)
        guard input.count <= 1024,
              let values = try JSONSerialization.jsonObject(with: input) as? [String: String],
              Set(values.keys) == Set(["password"]), let password = values["password"],
              BackendCredentials.valid(password) else { throw Failure.isolation }
        return Array(password.utf8)
    }

    static func rebind(_ reference: SecKeychainItem, root: URL, readers: [String], password: [UInt8]) throws {
        var owner: SecKeychain?
        guard SecKeychainItemCopyKeychain(reference, &owner) == errSecSuccess,
              let owner else { throw Failure.isolation }
        try checkedStore(owner, root: root)
        // Fixture-only SPI: Apple's password-authenticated edit does not prompt.
        // A missing symbol is unavailable, never a successful qualification.
        guard let library = dlopen("/System/Library/Frameworks/Security.framework/Security", RTLD_NOW | RTLD_LOCAL) else { throw Failure.storage }
        defer { dlclose(library) }
        guard let symbol = dlsym(library, "SecKeychainItemSetAccessWithPassword") else { throw Failure.storage }
        typealias Edit = @convention(c) (SecKeychainItem, SecAccess, UInt32, UnsafeRawPointer?) -> OSStatus
        let edit = unsafeBitCast(symbol, to: Edit.self)
        let replacement = try access(readers)
        let status = password.withUnsafeBytes { edit(reference, replacement, UInt32(password.count), $0.baseAddress) }
        guard status == errSecSuccess else { throw Failure.storage }
        try BackendCredentials.verifyItemAccess(reference, readers: readers)
    }

    static func main() {
        do {
            let args = CommandLine.arguments
            guard args.count == 4, args[1] == "--isolated-temporary-keychain",
                  ["create", "read", "rebind", "lock", "cleanup", "state"].contains(args[3]) else { throw Failure.isolation }
            let root = try checkedRoot(args[2])
            let storePath = root.appendingPathComponent("synthetic.keychain-db").path
            let readers = [root.appendingPathComponent("active-reader").path,
                           root.appendingPathComponent("controller").path]
            // Per-process prompt suppression; no default/search-list mutations.
            guard SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else { throw Failure.isolation }
            if args[3] == "state" {
                let data = try JSONSerialization.data(withJSONObject: ambientMetadata(), options: [.sortedKeys])
                FileHandle.standardOutput.write(data)
                return
            }
            var keychain: SecKeychain?
            if args[3] == "create" {
                guard !FileManager.default.fileExists(atPath: storePath) else { throw Failure.isolation }
                let input = FileHandle.standardInput.readData(ofLength: 1025)
                guard input.count <= 1024,
                      let values = try JSONSerialization.jsonObject(with: input) as? [String: String],
                      Set(values.keys) == Set(["password", "value"]),
                      let password = values["password"], let value = values["value"],
                      BackendCredentials.valid(password), BackendCredentials.valid(value) else { throw Failure.isolation }
                let bytes = Array(password.utf8)
                let status = bytes.withUnsafeBytes {
                    SecKeychainCreate(storePath, UInt32(bytes.count), $0.baseAddress, false, nil, &keychain)
                }
                guard status == errSecSuccess, let store = keychain else { throw Failure.storage }
                try checkedStore(store, root: root)
                // Write scope uses kSecUseKeychain, never an ambient default.
                let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                    kSecAttrService as String: service, kSecAttrAccount as String: account,
                    kSecUseKeychain as String: store, kSecValueData as String: Data(value.utf8),
                    kSecAttrAccess as String: try access(readers)]
                guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else { throw Failure.storage }
                try BackendCredentials.verifyItemAccess(item(store), readers: readers)
            } else {
                var info = stat()
                guard lstat(storePath, &info) == 0, info.st_uid == getuid(),
                      info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1 else { throw Failure.isolation }
                guard SecKeychainOpen(storePath, &keychain) == errSecSuccess, let store = keychain else { throw Failure.storage }
                try checkedStore(store, root: root)
                if args[3] == "cleanup" {
                    let bytes = try syntheticPassword()
                    let unlocked = bytes.withUnsafeBytes {
                        SecKeychainUnlock(store, UInt32(bytes.count), $0.baseAddress, true)
                    }
                    guard unlocked == errSecSuccess, SecKeychainDelete(store) == errSecSuccess else { throw Failure.storage }
                    guard !FileManager.default.fileExists(atPath: storePath) else { throw Failure.storage }
                } else if args[3] == "lock" {
                    guard SecKeychainLock(store) == errSecSuccess else { throw Failure.storage }
                } else if args[3] == "rebind" {
                    // Explicit fixture-only migration of the one scoped item.
                    let reference = try item(store)
                    try rebind(reference, root: root, readers: readers, password: syntheticPassword())
                } else {
                    try BackendCredentials.verifyItemAccess(item(store), readers: readers)
                    var query = scopedQuery(store)
                    query[kSecReturnData as String] = true
                    query[kSecMatchLimit as String] = kSecMatchLimitOne
                    var value: CFTypeRef?
                    try checkedReadStatus(SecItemCopyMatching(query as CFDictionary, &value))
                    guard let data = value as? Data, data.count == 64 else { throw Failure.storage }
                }
            }
            #if FIXTURE_REPLACEMENT
            print("fixture-replacement-pass")
            #elseif FIXTURE_UNRELATED
            print("fixture-unrelated-pass")
            #else
            print("fixture-original-pass")
            #endif
        } catch BackendCredentials.Failure.readerMismatch {
            fail("EXPECTED_POLICY_DENIAL")
        } catch Failure.osDenied {
            fail("EXPECTED_OS_DENIAL")
        } catch Failure.storePathUnavailable {
            fail("STORE_PATH_UNAVAILABLE")
        } catch Failure.storePathMismatch {
            fail("STORE_PATH_MISMATCH")
        } catch Failure.storeFileMismatch {
            fail("STORE_FILE_MISMATCH")
        } catch Failure.isolation {
            fail("ISOLATION_FAILURE")
        } catch {
            fail("UNAVAILABLE")
        }
    }

    static func fail(_ outcome: String) -> Never {
        // Fixed typed outcomes; unavailable operations cannot pass denial cases.
        FileHandle.standardError.write(Data((outcome + "\n").utf8))
        exit(1)
    }
}
