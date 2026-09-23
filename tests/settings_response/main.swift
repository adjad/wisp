import Foundation

var checks = 0
func check(_ condition: Bool, _ message: String) {
    guard condition else { fatalError(message) }
    checks += 1
}

let cloud: [String: Any] = [
    "enabled": true, "provider": "openrouter", "provider_label": "OpenRouter",
    "base_url": "https://openrouter.ai", "api_prefix": "/api/v1",
    "model_id": "example", "context_window": 16_384,
    "credential_name": "cloud-staged", "roles": ["reasoning"],
    "super_model_enabled": false, "super_model_router": "disabled",
]
let local: [String: Any] = [
    "enabled": true, "active": true, "authenticated": false,
    "base_url": "http://127.0.0.1:8767", "api_prefix": "/v1",
    "model_id": "example", "context_window": 8192,
    "roles": ["reasoning"],
]

for (path, complete) in [("inference/cloud", cloud), ("inference/local-provider", local)] {
    check(SettingsResponseValidator.valid(complete, path: path), "Complete \(path) rejected")
    for key in complete.keys {
        var partial = complete
        partial.removeValue(forKey: key)
        check(!SettingsResponseValidator.valid(partial, path: path), "Missing \(key) accepted")
    }
}

var disabled = cloud
disabled["enabled"] = false
disabled["roles"] = [String]()
check(SettingsResponseValidator.valid(disabled, path: "inference/cloud"), "Disabled cloud rejected")
disabled["credential_name"] = ""
disabled["base_url"] = ""
check(SettingsResponseValidator.valid(disabled, path: "inference/cloud"), "Disabled empty identity rejected")

var malformed = cloud
malformed["context_window"] = true
check(!SettingsResponseValidator.valid(malformed, path: "inference/cloud"), "Boolean window accepted")
malformed["context_window"] = 8192.5
check(!SettingsResponseValidator.valid(malformed, path: "inference/cloud"), "Fractional window accepted")
malformed = cloud
malformed["roles"] = ["reasoning", "unknown"]
check(!SettingsResponseValidator.valid(malformed, path: "inference/cloud"), "Unknown role accepted")
for key in ["credential_name", "provider", "base_url", "model_id"] {
    malformed = cloud
    malformed[key] = ""
    check(!SettingsResponseValidator.valid(malformed, path: "inference/cloud"), "Empty \(key) accepted")
}
malformed = local
malformed["active"] = 1
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"), "Numeric Boolean accepted")

print("\(checks) settings checks passed")
