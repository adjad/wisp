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
// Coding and Agent are valid Local roles now (they require a passing tool test on the
// backend); duplicates are still invalid.
for roles in [["reasoning", "reasoning"], ["coding", "coding"], ["fast"]] {
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


// Multi-role local provider (Reasoning + Agent + Coding) and optional tool fields.
var multi = local
multi["roles"] = ["reasoning", "agent", "coding"]
multi["tools_qualified"] = true
multi["qualified_context"] = 16_384
check(SettingsResponseValidator.valid(multi, path: "inference/local-provider"), "Multi-role Local rejected")
for roles in [["agent"], ["coding"], ["reasoning", "agent"]] {
    multi["roles"] = roles
    check(SettingsResponseValidator.valid(multi, path: "inference/local-provider"), "Roles \(roles) rejected")
}
for roles in [["fast"], ["router"], ["embedding"], ["agent", "agent"], ["nope"]] {
    multi["roles"] = roles
    check(!SettingsResponseValidator.valid(multi, path: "inference/local-provider"), "Roles \(roles) accepted")
}
multi["roles"] = ["agent"]
multi["tools_qualified"] = 1
check(!SettingsResponseValidator.valid(multi, path: "inference/local-provider"), "Numeric tools_qualified accepted")
multi["tools_qualified"] = true
multi["qualified_context"] = true
check(!SettingsResponseValidator.valid(multi, path: "inference/local-provider"), "Boolean qualified_context accepted")
var activeUnassigned = local
activeUnassigned["roles"] = [String]()
check(!SettingsResponseValidator.valid(activeUnassigned, path: "inference/local-provider"), "Active with no role accepted")

let report: [String: Any] = [
    "qualified": true, "effective_context": 16_384, "claimed_context": 32_768, "hint": "", "seconds": 18.0,
    "checks": [["id": "context", "label": "Context", "ok": true, "detail": "", "required": true]],
]
check(SettingsResponseValidator.valid(report, path: "inference/local-provider/qualify"), "Report rejected")
for key in ["qualified", "effective_context", "checks"] {
    var partial = report
    partial.removeValue(forKey: key)
    check(!SettingsResponseValidator.valid(partial, path: "inference/local-provider/qualify"), "Report missing \(key) accepted")
}
var badReport = report
badReport["checks"] = [["id": "context", "label": "Context", "ok": 1]]
check(!SettingsResponseValidator.valid(badReport, path: "inference/local-provider/qualify"), "Numeric ok accepted")
badReport["checks"] = [["label": "Context", "ok": true]]
check(!SettingsResponseValidator.valid(badReport, path: "inference/local-provider/qualify"), "Check without id accepted")

// Context values must be checked before NSNumber integer conversion.
let invalidContextValues: [Any] = [
    true, 8192.5, -0.5, -1, 262_145, NSNumber(value: UInt64.max),
    NSNumber(value: Double.greatestFiniteMagnitude), NSNumber(value: Double.nan),
    NSNumber(value: Double.infinity), NSNumber(value: -Double.infinity),
]
for value in invalidContextValues {
    var candidate = report
    candidate["effective_context"] = value
    check(!SettingsResponseValidator.valid(candidate, path: "inference/local-provider/qualify"),
          "Invalid effective_context accepted")
    candidate["qualified"] = false
    check(!SettingsResponseValidator.valid(candidate, path: "inference/local-provider/qualify"),
          "Failure report accepted an invalid effective_context")
    candidate = report
    candidate["qualified_context"] = value
    check(!SettingsResponseValidator.valid(candidate, path: "inference/local-provider/qualify"),
          "Invalid optional report context accepted")
    for state in [local, inactiveLocal, disabledLocal] {
        var settings = state
        settings["qualified_context"] = value
        check(!SettingsResponseValidator.valid(settings, path: "inference/local-provider"),
              "Invalid qualified_context accepted")
    }
    for (path, state) in [("inference/local-provider", local), ("inference/cloud", cloud)] {
        var settings = state
        settings["context_window"] = value
        check(!SettingsResponseValidator.valid(settings, path: path), "Invalid context_window accepted")
    }
}
for value in [512, 262_144] {
    var settings = local
    settings["context_window"] = value
    check(SettingsResponseValidator.valid(settings, path: "inference/local-provider"), "Window boundary rejected")
}
for value in [8_192, 262_144] {
    var candidate = report
    candidate["effective_context"] = NSNumber(value: Double(value))
    check(SettingsResponseValidator.qualificationSucceeded(candidate), "Integral measured context rejected")
}
for value in [0, 512, 8_191] {
    var candidate = report
    candidate["effective_context"] = value
    check(!SettingsResponseValidator.valid(candidate, path: "inference/local-provider/qualify"),
          "Insufficient tool context accepted as qualified")
}

var inconsistent = report
inconsistent["checks"] = [[String: Any]]()
check(!SettingsResponseValidator.qualificationSucceeded(inconsistent), "Empty checks invented success")
inconsistent["checks"] = [["id": "call", "label": "Tool call", "ok": false]]
check(!SettingsResponseValidator.valid(inconsistent, path: "inference/local-provider/qualify"),
      "Failed default-required check accepted as qualified")
inconsistent["checks"] = [["id": "call", "label": "Tool call", "ok": false, "required": true]]
check(!SettingsResponseValidator.qualificationSucceeded(inconsistent), "Failed required check invented success")
inconsistent = report
inconsistent["qualified"] = false
check(!SettingsResponseValidator.valid(inconsistent, path: "inference/local-provider/qualify"),
      "All passing required checks contradicted the qualified flag")
for required in [1, "false", NSNull(), NSNumber(value: 0.0)] as [Any] {
    inconsistent = report
    inconsistent["checks"] = [["id": "context", "label": "Context", "ok": true, "required": required]]
    check(!SettingsResponseValidator.valid(inconsistent, path: "inference/local-provider/qualify"),
          "Malformed required flag accepted")
}
var defaultRequired = report
defaultRequired["checks"] = [["id": "context", "label": "Context", "ok": true]]
check(SettingsResponseValidator.qualificationSucceeded(defaultRequired), "Missing required did not default to true")
var advisory = report
advisory["checks"] = (report["checks"] as! [[String: Any]]) + [
    ["id": "restraint", "label": "Restraint", "ok": false, "required": false],
]
check(SettingsResponseValidator.qualificationSucceeded(advisory), "Advisory failure blocked valid success")
advisory["checks"] = [["id": "restraint", "label": "Restraint", "ok": true, "required": false]]
check(!SettingsResponseValidator.qualificationSucceeded(advisory), "Advisory-only report invented required evidence")
for id in ["context", "error", "deadline"] {
    var failure = report
    failure["qualified"] = false
    failure["effective_context"] = 0
    failure["checks"] = [["id": id, "label": "Failed check", "ok": false]]
    check(SettingsResponseValidator.valid(failure, path: "inference/local-provider/qualify"),
          "Legitimate failure report rejected")
    check(!SettingsResponseValidator.qualificationSucceeded(failure), "Failure report invented test success")
}
check(SettingsResponseValidator.validQualificationResponse(report, requestedContext: 32_768),
      "Healthy current test response rejected")
check(!SettingsResponseValidator.validQualificationResponse(report, requestedContext: 8_192),
      "Measured context above the current request accepted")
check(!SettingsResponseValidator.validQualificationResponse(report, requestedContext: 262_145),
      "Out-of-range test request accepted")

// GET validation and POST matching share the production binding contract.
for roles in [["agent"], ["coding"], ["reasoning", "agent", "coding"]] {
    var settings = local
    settings["roles"] = roles
    settings["tools_qualified"] = true
    settings["qualified_context"] = 16_384
    settings["context_window"] = 16_384
    let requested = SettingsResponseValidator.LocalBinding(
        baseURL: "http://127.0.0.1:8767", apiPrefix: "/v1", modelID: "example",
        contextWindow: 32_768, roles: Set(roles))
    check(SettingsResponseValidator.valid(settings, path: "inference/local-provider"), "Qualified tool state rejected")
    check(SettingsResponseValidator.localProviderMatches(settings, requested: requested),
          "min(requested, measured) saved window rejected")
    var missing = settings
    missing.removeValue(forKey: "tools_qualified")
    check(!SettingsResponseValidator.valid(missing, path: "inference/local-provider"), "Missing tool qualification accepted")
    check(!SettingsResponseValidator.localProviderMatches(missing, requested: requested), "Missing tool proof matched a save")
    missing = settings
    missing.removeValue(forKey: "qualified_context")
    check(!SettingsResponseValidator.valid(missing, path: "inference/local-provider"), "Missing measured context accepted")
    for flag in [false, 1, "true"] as [Any] {
        var bad = settings
        bad["tools_qualified"] = flag
        check(!SettingsResponseValidator.valid(bad, path: "inference/local-provider"), "Invalid tool success flag accepted")
        check(!SettingsResponseValidator.localProviderMatches(bad, requested: requested), "Unqualified save matched")
    }
    for measured in [0, 8_191] {
        var bad = settings
        bad["qualified_context"] = measured
        check(!SettingsResponseValidator.valid(bad, path: "inference/local-provider"), "Insufficient measured context accepted")
    }
    for saved in [512, 8_191, 32_768] {
        var bad = settings
        bad["context_window"] = saved
        check(!SettingsResponseValidator.valid(bad, path: "inference/local-provider"), "Inconsistent saved tool context accepted")
    }
    var smaller = settings
    smaller["context_window"] = 8_192
    check(SettingsResponseValidator.valid(smaller, path: "inference/local-provider"), "Bounded saved GET state rejected")
    check(!SettingsResponseValidator.localProviderMatches(smaller, requested: requested),
          "Save did not enforce min(requested, measured)")
    let smallerRequest = SettingsResponseValidator.LocalBinding(
        baseURL: requested.baseURL, apiPrefix: requested.apiPrefix, modelID: requested.modelID,
        contextWindow: 8_192, roles: requested.roles)
    check(SettingsResponseValidator.localProviderMatches(smaller, requested: smallerRequest), "Smaller requested window rejected")
    var paused = settings
    paused["active"] = false
    paused.removeValue(forKey: "tools_qualified")
    paused.removeValue(forKey: "qualified_context")
    check(SettingsResponseValidator.valid(paused, path: "inference/local-provider"), "Inactive legacy state rejected")
    check(!SettingsResponseValidator.localProviderMatches(paused, requested: requested), "Inactive unqualified state invented save success")
    var wrongIdentity = settings
    wrongIdentity["api_prefix"] = "/alternate"
    check(!SettingsResponseValidator.localProviderMatches(wrongIdentity, requested: requested), "Different prefix matched")
}
let legacyRequest = SettingsResponseValidator.LocalBinding(
    baseURL: "http://127.0.0.1:8767", apiPrefix: "/v1", modelID: "example",
    contextWindow: 8_192, roles: ["reasoning"])
check(SettingsResponseValidator.localProviderMatches(local, requested: legacyRequest), "Legacy Reasoning reply rejected")
check(SettingsResponseValidator.localBinding(disabledLocal) == nil, "Disabled state invented a saved assignment")
check(SettingsResponseValidator.localBinding(inactiveLocal) == nil, "Unassigned state invented a saved assignment")
var measuredState = local
measuredState["roles"] = ["agent"]
measuredState["context_window"] = 16_384
measuredState["tools_qualified"] = true
measuredState["qualified_context"] = 16_384
let savedBinding = SettingsResponseValidator.localBinding(measuredState)!
let toolRequest = SettingsResponseValidator.LocalBinding(
    baseURL: savedBinding.baseURL, apiPrefix: savedBinding.apiPrefix, modelID: savedBinding.modelID,
    contextWindow: 32_768, roles: ["agent"])
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: savedBinding,
      refreshed: savedBinding, requested: toolRequest, httpStatus: nil) == .savedMatchUnconfirmed,
      "Older matching saved binding invented current-test success")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: false, prior: nil,
      refreshed: savedBinding, requested: toolRequest, httpStatus: nil) == .savedMatchUnconfirmed,
      "Unknown prior state invented a new commit")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: nil,
      refreshed: savedBinding, requested: toolRequest, httpStatus: nil) == .recoveredReply,
      "Lost-reply recovery for a new validated binding rejected")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: nil,
      refreshed: savedBinding, requested: toolRequest, httpStatus: 503) == .recoveredReply,
      "Post-save server failure recovery rejected")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: nil,
      refreshed: savedBinding, requested: toolRequest, httpStatus: 400) == .currentTestFailed,
      "Failed current test was replaced by saved-state success")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: savedBinding,
      refreshed: savedBinding, requested: toolRequest, httpStatus: 409) == .outcomeUnknown,
      "Newer-state conflict invented success")
check(SettingsResponseValidator.localSaveRecovery(priorStateKnown: true, prior: nil,
      refreshed: nil, requested: toolRequest, httpStatus: nil) == .failed,
      "Unconfirmed assignment invented success")
var malformedGET = measuredState
malformedGET["qualified_context"] = 8_192.5
check(SettingsResponseValidator.localBinding(malformedGET) == nil,
      "Malformed GET supplied reconciliation evidence")
check(!SettingsResponseValidator.localProviderMatches(malformedGET, requested: toolRequest),
      "Malformed POST supplied current save success")
var staleFailure = report
staleFailure["qualified"] = false
staleFailure["checks"] = [["id": "call", "label": "Tool call", "ok": false]]
check(SettingsResponseValidator.valid(staleFailure, path: "inference/local-provider/qualify"),
      "Valid current failure after an earlier saved pass rejected")
check(!SettingsResponseValidator.qualificationSucceeded(staleFailure), "Earlier saved proof invented current test success")

print("\(checks) settings checks passed")
