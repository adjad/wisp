#if WISP_SETTINGS_QA
import Darwin
import Foundation

/// Compile-time-only fixture boundary. A production build cannot select it
/// through environment variables or a runtime setting.
enum SettingsQAEnvironment {
    static let home: URL = {
        guard let raw = ProcessInfo.processInfo.environment["WISP_HOME"],
              raw.hasPrefix("/private/tmp/wisp-settings-qa-"),
              FileManager.default.fileExists(atPath: raw) else {
            fatalError("Settings QA requires an existing /private/tmp fixture WISP_HOME")
        }
        let resolved = URL(fileURLWithPath: raw, isDirectory: true)
            .standardizedFileURL.resolvingSymlinksInPath()
        guard (resolved.path.hasPrefix("/private/tmp/wisp-settings-qa-")
               || resolved.path.hasPrefix("/tmp/wisp-settings-qa-")),
              let attributes = try? FileManager.default.attributesOfItem(atPath: resolved.path),
              attributes[.type] as? FileAttributeType == .typeDirectory,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid() else {
            fatalError("Settings QA fixture WISP_HOME must resolve to an owned temp directory")
        }
        return resolved
    }()

    static let baseURL: URL = {
        guard let raw = ProcessInfo.processInfo.environment["WISP_SETTINGS_QA_BASE_URL"],
              let url = URL(string: raw), url.scheme == "http",
              url.host == "127.0.0.1", let port = url.port,
              port > 0 && port != 8765,
              url.path.isEmpty || url.path == "/",
              url.user == nil, url.password == nil, url.query == nil,
              url.fragment == nil else {
            fatalError("Settings QA requires a synthetic ephemeral loopback endpoint")
        }
        return url
    }()

    static let defaults: UserDefaults = {
        guard let runID = ProcessInfo.processInfo.environment["WISP_SETTINGS_QA_RUN_ID"],
              !runID.isEmpty, runID.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }),
              let value = UserDefaults(suiteName: "com.wisp.settings-qa.\(runID)") else {
            fatalError("Settings QA requires a dedicated defaults suite")
        }
        value.register(defaults: ["WispSettingsPane": "models",
                                  "WispModelsPane": "assignments"])
        return value
    }()

    static let pendingStore = FixturePendingStore(
        url: home.appendingPathComponent("qa-pending-defaults.json"))

    private static var credentialsURL: URL { home.appendingPathComponent("qa-credentials.json") }

    static func credentials() throws -> [String: String] {
        guard FileManager.default.fileExists(atPath: credentialsURL.path) else { return [:] }
        let data = try Data(contentsOf: credentialsURL)
        return try JSONDecoder().decode([String: String].self, from: data)
    }

    static func saveCredential(_ secret: String, account: String) throws {
        var values = try credentials()
        values[account] = secret
        try JSONEncoder().encode(values).write(to: credentialsURL, options: .atomic)
    }

    static func removeCredential(account: String) throws {
        var values = try credentials()
        values.removeValue(forKey: account)
        try JSONEncoder().encode(values).write(to: credentialsURL, options: .atomic)
    }
}

/// The app still has a distinct UserDefaults suite for AppStorage, but pending
/// credential metadata is file-backed inside WISP_HOME so a restart cannot
/// leave secret-adjacent state in the user's preferences directory.
final class FixturePendingStore {
    private let url: URL
    init(url: URL) { self.url = url }

    private func values() -> [String: Any] {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return object
    }

    private func write(_ values: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: values) else {
            fatalError("Invalid QA pending metadata")
        }
        do { try data.write(to: url, options: .atomic) }
        catch { fatalError("Could not persist QA pending metadata: \(error)") }
    }

    func string(forKey key: String) -> String? { values()[key] as? String }
    func stringArray(forKey key: String) -> [String]? { values()[key] as? [String] }
    func bool(forKey key: String) -> Bool { values()[key] as? Bool ?? false }
    func integer(forKey key: String) -> Int { values()[key] as? Int ?? 0 }
    func set(_ value: Any, forKey key: String) {
        var data = values()
        data[key] = value
        write(data)
    }
    func removeObject(forKey key: String) {
        var data = values()
        data.removeValue(forKey: key)
        write(data)
    }
}
#endif
