import CoreFoundation
import Foundation

/// Validate the complete backend contract before callers change UI state or
/// reconcile Keychain entries. Foundation bridges JSON booleans to NSNumber,
/// so `as? Int` alone would also accept a boolean context window.
enum SettingsResponseValidator {
    static func valid(_ object: [String: Any], path: String) -> Bool {
        func bool(_ key: String) -> Bool {
            guard let value = object[key] as? NSNumber else { return false }
            return CFGetTypeID(value) == CFBooleanGetTypeID()
        }
        func integer(_ key: String) -> Bool {
            guard let value = object[key] as? NSNumber else { return false }
            return CFGetTypeID(value) != CFBooleanGetTypeID()
                && value.intValue >= 512 && value.intValue <= 262_144
                && value.doubleValue == Double(value.intValue)
        }
        func string(_ key: String) -> Bool { object[key] is String }
        guard path == "inference/cloud" || path == "inference/local-provider" else {
            return true
        }
        guard bool("enabled"), string("base_url"), string("api_prefix"),
              string("model_id"), integer("context_window"),
              let roles = object["roles"] as? [String] else { return false }
        if path == "inference/local-provider" {
            return bool("active") && bool("authenticated")
                && roles.allSatisfy { $0 == "reasoning" }
        }
        return string("provider") && string("provider_label")
            && string("credential_name") && bool("super_model_enabled")
            && string("super_model_router")
            && roles.allSatisfy { ["reasoning", "coding", "research"].contains($0) }
    }
}
