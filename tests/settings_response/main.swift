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

var inactiveLocal = local
inactiveLocal["active"] = false
check(SettingsResponseValidator.valid(inactiveLocal, path: "inference/local-provider"),
      "Inactive enabled Local provider rejected")
inactiveLocal["roles"] = [String]()
check(SettingsResponseValidator.valid(inactiveLocal, path: "inference/local-provider"),
      "Enabled but unassigned Local provider rejected")

var disabledLocal = local
disabledLocal["enabled"] = false
disabledLocal["active"] = false
disabledLocal["roles"] = [String]()
disabledLocal["model_id"] = ""
check(SettingsResponseValidator.valid(disabledLocal, path: "inference/local-provider"),
      "Disabled Local default response rejected")

for origin in ["https://127.0.0.1:8767", "http://localhost:8767",
               "http://127.0.0.1:8000", "http://127.0.0.1:8765",
               "http://127.0.0.1:8767/admin", "http://user@127.0.0.1:8767",
               "http://127.0.0.1:8767?x=1", "http://127.0.0.1"] {
    malformed = local
    malformed["base_url"] = origin
    check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
          "Invalid Local origin accepted: \(origin)")
}
for prefix in ["", "v1", "/v1/", "/v1?x=1"] {
    malformed = local
    malformed["api_prefix"] = prefix
    check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
          "Invalid Local API prefix accepted: \(prefix)")
}
malformed = local
malformed["model_id"] = "   "
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
      "Empty Local model accepted")
malformed = local
malformed["authenticated"] = true
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
      "Authenticated Local provider accepted")
malformed = local
malformed["authenticated"] = 0
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
      "Numeric Local authentication state accepted")
for roles in [["reasoning", "reasoning"], ["coding"], ["reasoning", "coding"]] {
    malformed = local
    malformed["roles"] = roles
    check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
          "Invalid Local roles accepted")
}
malformed = local
malformed["roles"] = [String]()
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
      "Active Local provider without Reasoning accepted")
malformed = disabledLocal
malformed["active"] = true
check(!SettingsResponseValidator.valid(malformed, path: "inference/local-provider"),
      "Active disabled Local provider accepted")

print("\(checks) settings checks passed")
