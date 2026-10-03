import CoreFoundation
import Foundation

/// Validate the complete backend contract before callers change UI state or
/// reconcile Keychain entries. Foundation bridges JSON booleans to NSNumber,
/// so `as? Int` alone would also accept a boolean context window.
enum SettingsResponseValidator {
    static let minimumToolContext = 8_192

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    /// Check the numeric value before any integer conversion, including NSNumber
    /// values supplied directly rather than through JSONSerialization.
    static func contextValue(_ value: Any?, minimum: Int = 0) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let value = number.doubleValue
        guard value.isFinite, value >= Double(minimum), value <= 262_144,
              value.rounded(.towardZero) == value else { return nil }
        let integer = Int(value)
        guard number.compare(NSNumber(value: integer)) == .orderedSame else { return nil }
        return integer
    }

    static func qualificationSucceeded(_ object: [String: Any]) -> Bool {
        valid(object, path: "inference/local-provider/qualify") && boolean(object["qualified"]) == true
    }

    static func validQualificationResponse(_ object: [String: Any], requestedContext: Int) -> Bool {
        guard (512...262_144).contains(requestedContext),
              valid(object, path: "inference/local-provider/qualify"),
              let effective = contextValue(object["effective_context"]) else { return false }
        return effective <= requestedContext
    }

    static func toolQualificationContext(_ object: [String: Any]) -> Int? {
        guard boolean(object["tools_qualified"]) == true,
              let measured = contextValue(object["qualified_context"], minimum: minimumToolContext),
              let saved = contextValue(object["context_window"], minimum: minimumToolContext),
              saved <= measured else { return nil }
        return measured
    }

    struct LocalBinding: Equatable {
        let baseURL: String
        let apiPrefix: String
        let modelID: String
        let contextWindow: Int
        let roles: Set<String>
        var qualifiedContext: Int? = nil

        func satisfies(_ requested: LocalBinding) -> Bool {
            guard baseURL == requested.baseURL, apiPrefix == requested.apiPrefix,
                  modelID == requested.modelID, roles == requested.roles,
                  (512...262_144).contains(contextWindow),
                  (512...262_144).contains(requested.contextWindow) else { return false }
            if !roles.isDisjoint(with: ["agent", "coding"]) {
                guard let measured = qualifiedContext,
                      (SettingsResponseValidator.minimumToolContext...262_144).contains(measured),
                      contextWindow >= SettingsResponseValidator.minimumToolContext else { return false }
                return contextWindow == min(requested.contextWindow, measured)
            }
            return contextWindow <= requested.contextWindow
        }
    }

    static func localBinding(_ object: [String: Any]) -> LocalBinding? {
        guard valid(object, path: "inference/local-provider"), boolean(object["enabled"]) == true,
              let roles = object["roles"] as? [String], !roles.isEmpty,
              let baseURL = object["base_url"] as? String,
              let prefix = object["api_prefix"] as? String,
              let model = object["model_id"] as? String,
              let context = contextValue(object["context_window"], minimum: 512) else { return nil }
        var origin = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while origin.hasSuffix("/") { origin.removeLast() }
        return LocalBinding(baseURL: origin, apiPrefix: prefix, modelID: model,
                            contextWindow: context, roles: Set(roles),
                            qualifiedContext: toolQualificationContext(object))
    }

    static func localProviderMatches(_ object: [String: Any], requested: LocalBinding) -> Bool {
        localBinding(object)?.satisfies(requested) ?? false
    }

    enum LocalSaveRecovery: Equatable {
        case recoveredReply, currentTestFailed, outcomeUnknown, savedMatchUnconfirmed, failed
    }

    /// A validated GET can confirm a changed saved binding after a lost reply.
    /// Matching a binding that was already saved cannot prove this test passed.
    static func localSaveRecovery(priorStateKnown: Bool, prior: LocalBinding?,
                                  refreshed: LocalBinding?, requested: LocalBinding,
                                  httpStatus: Int?) -> LocalSaveRecovery {
        let saved = refreshed?.satisfies(requested) ?? false
        let newlyCommitted = saved && priorStateKnown && !(prior?.satisfies(requested) ?? false)
        if newlyCommitted && (httpStatus.map { $0 >= 500 } ?? true) { return .recoveredReply }
        if httpStatus == 400 { return .currentTestFailed }
        if httpStatus != nil { return .outcomeUnknown }
        return saved ? .savedMatchUnconfirmed : .failed
    }

    static func valid(_ object: [String: Any], path: String) -> Bool {
        func bool(_ key: String) -> Bool { boolean(object[key]) != nil }
        func integer(_ key: String) -> Bool { contextValue(object[key], minimum: 512) != nil }
        func string(_ key: String) -> Bool { object[key] is String }
        if path == "inference/local-provider/qualify" {
            guard let qualified = boolean(object["qualified"]),
                  let effective = contextValue(object["effective_context"]),
                  let checks = object["checks"] as? [[String: Any]] else { return false }
            if let context = object["qualified_context"], contextValue(context) == nil { return false }
            var requiredCount = 0
            var requiredFailed = false
            for check in checks {
                guard check["id"] is String, check["label"] is String,
                      let ok = boolean(check["ok"]) else { return false }
                let required: Bool
                if let value = check["required"] {
                    guard let flag = boolean(value) else { return false }
                    required = flag
                } else {
                    required = true
                }
                if required {
                    requiredCount += 1
                    if !ok { requiredFailed = true }
                }
            }
            let success = effective >= minimumToolContext && requiredCount > 0 && !requiredFailed
            return qualified == success
        }

        guard path == "inference/cloud" || path == "inference/local-provider" else {
            return true
        }
        guard bool("enabled"), string("base_url"), string("api_prefix"),
              string("model_id"), integer("context_window"),
              let roles = object["roles"] as? [String] else { return false }
        if path == "inference/local-provider" {
            let allowedRoles: Set<String> = ["reasoning", "agent", "coding"]
            guard bool("active"), bool("authenticated"),
                  object["authenticated"] as? Bool == false,
                  Set(roles).count == roles.count,
                  Set(roles).isSubset(of: allowedRoles) else { return false }
            // Added with tool qualification; optional so an older backend still validates.
            if object["tools_qualified"] != nil && !bool("tools_qualified") { return false }
            if let context = object["qualified_context"], contextValue(context) == nil { return false }
            let enabled = object["enabled"] as? Bool == true
            let active = object["active"] as? Bool == true
            if !enabled { return !active && roles.isEmpty }
            if enabled && active && !Set(roles).isDisjoint(with: ["agent", "coding"]),
               toolQualificationContext(object) == nil { return false }
            guard !active || !roles.isEmpty,
                  let model = object["model_id"] as? String,
                  !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let prefix = object["api_prefix"] as? String,
                  prefix.range(of: "^(?:/[A-Za-z0-9_-]+)+$", options: .regularExpression) != nil,
                  let rawURL = object["base_url"] as? String,
                  let url = URLComponents(string: rawURL),
                  url.scheme == "http", url.host == "127.0.0.1",
                  let port = url.port, (1024...65535).contains(port),
                  port != 8000, port != 8765,
                  url.user == nil, url.password == nil,
                  url.path.isEmpty || url.path == "/",
                  url.query == nil, url.fragment == nil else { return false }
            return true
        }
        guard string("provider") && string("provider_label")
            && string("credential_name") && bool("super_model_enabled")
            && string("super_model_router")
            && roles.allSatisfy({ ["reasoning", "coding", "research"].contains($0) })
        else { return false }
        // A live cloud endpoint with an empty identity is not safe to use for
        // Keychain reference checks, even when every field is present.
        guard object["enabled"] as? Bool == true else { return true }
        guard let name = object["credential_name"] as? String,
              name.range(of: "^[A-Za-z][A-Za-z0-9_-]{0,63}$", options: .regularExpression) != nil,
              let provider = object["provider"] as? String,
              ["openrouter", "openai-compatible"].contains(provider),
              let model = object["model_id"] as? String,
              !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let rawURL = object["base_url"] as? String,
              let url = URLComponents(string: rawURL), url.scheme == "https",
              url.host != nil, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else { return false }
        return true
    }
}
