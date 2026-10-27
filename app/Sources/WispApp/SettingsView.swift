import AppKit
import CryptoKit
import Security
import SwiftUI

extension Notification.Name {
    /// Settings › General's Export Chat Debug Log…: AppDelegate writes the notch's conversation.
    static let wispExportDebugLog = Notification.Name("WispExportDebugLog")
}

private enum CloudCredentialError: LocalizedError {
    case invalidKey
    case storage

    var errorDescription: String? {
        switch self {
        case .invalidKey: return "Enter a valid API key without spaces or line breaks."
        case .storage: return "Wisp could not save the API key in macOS Keychain."
        }
    }
}

/// Provider keys never enter YAML, URL requests to Wisp, logs, or debug exports.
/// The account name binds each key to its normalized HTTPS origin, matching the
/// backend's provider_credentials.keychain_account contract.
private enum CloudCredentialStore {
    #if WISP_SETTINGS_QA
    static let service = "com.wisp.settings-qa.inference"
    #else
    static let service = "com.wisp.inference"
    #endif

    static func normalizedBaseURL(_ raw: String) -> String {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }

    static func account(name: String, baseURL: String) -> String {
        let digest = SHA256.hash(data: Data(normalizedBaseURL(baseURL).utf8))
            .map { String(format: "%02x", $0) }.joined()
        return "\(name):\(digest)"
    }

    private static func query(name: String, baseURL: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(name: name, baseURL: baseURL),
        ]
    }

    private static func write(_ data: Data, name: String, baseURL: String) throws {
        let query = query(name: name, baseURL: baseURL)
        let attributes = [kSecValueData as String: data]
        let updated = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updated == errSecSuccess { return }
        guard updated == errSecItemNotFound else { throw CloudCredentialError.storage }
        var item = query
        item[kSecValueData as String] = data
        guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
            throw CloudCredentialError.storage
        }
    }

    static func save(_ secret: String, name: String, baseURL: String) throws {
        guard !secret.isEmpty, secret.utf8.count <= 4096,
              secret.unicodeScalars.allSatisfy({ $0.value >= 33 && $0.value <= 126 })
        else { throw CloudCredentialError.invalidKey }
        #if WISP_SETTINGS_QA
        try SettingsQAEnvironment.saveCredential(secret, account: account(name: name, baseURL: baseURL))
        #else
        try write(Data(secret.utf8), name: name, baseURL: baseURL)
        #endif
    }

    static func remove(name: String, baseURL: String) throws {
        #if WISP_SETTINGS_QA
        try SettingsQAEnvironment.removeCredential(account: account(name: name, baseURL: baseURL))
        #else
        let status = SecItemDelete(query(name: name, baseURL: baseURL) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CloudCredentialError.storage
        }
        #endif
    }
}

/// Stores only Keychain lookup metadata, never a provider secret. This lets a
/// later Settings refresh reconcile an operation whose HTTP response was lost.
private struct PendingCloudCredential {
    let mode: String
    let staged: Bool
    let name: String
    let baseURL: String
    let previousName: String
    let previousBaseURL: String
    let provider: String
    let apiPrefix: String
    let modelID: String
    let contextWindow: Int
    let roles: [String]
    let superModelEnabled: Bool

    private static let modeKey = "WispPendingCloudCredentialMode"
    private static let stagedKey = "WispPendingCloudCredentialWasStaged"
    private static let nameKey = "WispPendingCloudCredentialName"
    private static let baseKey = "WispPendingCloudCredentialBaseURL"
    private static let previousNameKey = "WispPendingCloudPreviousCredentialName"
    private static let previousBaseKey = "WispPendingCloudPreviousBaseURL"
    private static let providerKey = "WispPendingCloudProvider"
    private static let apiPrefixKey = "WispPendingCloudAPIPrefix"
    private static let modelKey = "WispPendingCloudModelID"
    private static let contextKey = "WispPendingCloudContextWindow"
    private static let rolesKey = "WispPendingCloudRoles"
    private static let superModelKey = "WispPendingCloudSuperModel"

    #if WISP_SETTINGS_QA
    private static var defaults: FixturePendingStore { SettingsQAEnvironment.pendingStore }
    #else
    private static var defaults: UserDefaults { .standard }
    #endif

    static func load() -> PendingCloudCredential? {
        let defaults = defaults
        guard let name = defaults.string(forKey: nameKey),
              let baseURL = defaults.string(forKey: baseKey) else { return nil }
        return PendingCloudCredential(
            mode: defaults.string(forKey: modeKey) ?? "connect",
            staged: defaults.bool(forKey: stagedKey),
            name: name,
            baseURL: baseURL,
            previousName: defaults.string(forKey: previousNameKey) ?? "",
            previousBaseURL: defaults.string(forKey: previousBaseKey) ?? "",
            provider: defaults.string(forKey: providerKey) ?? "",
            apiPrefix: defaults.string(forKey: apiPrefixKey) ?? "",
            modelID: defaults.string(forKey: modelKey) ?? "",
            contextWindow: defaults.integer(forKey: contextKey),
            roles: defaults.stringArray(forKey: rolesKey) ?? [],
            superModelEnabled: defaults.bool(forKey: superModelKey))
    }

    func save() {
        let defaults = Self.defaults
        defaults.set(mode, forKey: Self.modeKey)
        defaults.set(staged, forKey: Self.stagedKey)
        defaults.set(name, forKey: Self.nameKey)
        defaults.set(baseURL, forKey: Self.baseKey)
        defaults.set(previousName, forKey: Self.previousNameKey)
        defaults.set(previousBaseURL, forKey: Self.previousBaseKey)
        defaults.set(provider, forKey: Self.providerKey)
        defaults.set(apiPrefix, forKey: Self.apiPrefixKey)
        defaults.set(modelID, forKey: Self.modelKey)
        defaults.set(contextWindow, forKey: Self.contextKey)
        defaults.set(roles, forKey: Self.rolesKey)
        defaults.set(superModelEnabled, forKey: Self.superModelKey)
    }

    static func clear() {
        let defaults = defaults
        [modeKey, stagedKey, nameKey, baseKey, previousNameKey, previousBaseKey,
         providerKey, apiPrefixKey, modelKey, contextKey, rolesKey, superModelKey].forEach {
            defaults.removeObject(forKey: $0)
        }
    }
}

private struct CloudProviderPreset: Identifiable {
    let id: String
    let label: String
    let provider: String
    let baseURL: String
    let apiPrefix: String
}

private enum CloudSettingsError: LocalizedError {
    case message(String)
    case http(statusCode: Int, detail: String)
    var errorDescription: String? {
        switch self {
        case let .message(value): return value
        case let .http(_, detail): return detail
        }
    }
}

private enum SettingsResponseError: LocalizedError {
    case invalid
    var errorDescription: String? {
        "Wisp returned an incomplete settings response. Refresh models to confirm the saved state."
    }
}

@MainActor
final class SettingsLoader: ObservableObject {
    private typealias LocalSavedBinding = SettingsResponseValidator.LocalBinding

    struct LocalQualificationCheck: Identifiable, Equatable {
        let id: String
        let label: String
        let ok: Bool
        let detail: String
        let required: Bool
    }

    struct LocalQualification: Equatable {
        let qualified: Bool
        let effectiveContext: Int
        let hint: String
        let checks: [LocalQualificationCheck]
    }

    @Published var installed: [String] = []
    @Published var roles: [String: String] = [:]
    @Published var saving = false
    // Full access as the service last confirmed it; nil until confirmed or
    // when a read or write couldn't be confirmed. Never shown as "off" while nil.
    // This is Wisp's one Full access source: Settings, quick settings and the
    // Wisp window's composer all read and write it through this loader.
    @Published private(set) var fullAccess: Bool?
    @Published private(set) var fullAccessPending = false
    // The Daily Summary's morning and evening hours, as the service confirmed them.
    @Published private(set) var briefTimes: WispClient.BriefTimes?
    @Published private(set) var briefTimesPending = false
    @Published var idleMinutes: Double = 5
    @Published var humanizerEnabled = false
    @Published var behaviorEnabled: Bool? = nil
    @Published var behaviorChanging = false
    @Published var behaviorStatus: String?
    @Published var behaviorRemote = false
    @Published var behaviorPhraser = true
    private var behaviorControlEpoch: String?
    private var behaviorRequestGeneration: UInt64 = 0
    @Published var behaviorForgetStatus: String?
    @Published var cloudPreset = "openrouter"
    @Published var cloudBaseURL = "https://openrouter.ai"
    @Published var cloudAPIPrefix = "/api/v1"
    @Published var cloudModelID = ""
    @Published var cloudContextWindow = 16_384
    @Published var cloudAPIKey = ""
    @Published var cloudRoles: Set<String> = []
    @Published var savedCloudRoles: Set<String> = []
    @Published var cloudStateUnknown = true
    @Published var cloudTestOutcomeUnknown = false
    @Published var superModelEnabled = false
    @Published var cloudConnected = false
    @Published var cloudSaving = false
    @Published var cloudStatus = "Checking cloud settings…"
    @Published var localProviderBaseURL = "http://127.0.0.1:8767"
    @Published var localProviderAPIPrefix = "/v1"
    @Published var localProviderModelID = ""
    @Published var localProviderContextWindow = 8_192
    @Published var localProviderModels: [String] = []
    @Published var localProviderRoles: Set<String> = ["reasoning"]
    @Published var localProviderConnected = false
    @Published var localProviderActive = false
    @Published var localProviderSavedAssigned = false
    @Published var localProviderStateUnknown = true
    @Published var localProviderSaving = false
    @Published var localProviderToolsQualified = false
    @Published var localProviderQualifiedContext = 0
    @Published var localProviderTesting = false
    @Published var localProviderQualification: LocalQualification?
    @Published var localProviderStatus = "Checking local connection…"
    private var savedLocalBinding: LocalSavedBinding?
    var localProviderDisplayStatus: String {
        if localProviderStateUnknown {
            return "Status unknown"
        }
        if localProviderConnected && !localProviderSavedAssigned {
            return "Connected; not assigned"
        }
        if localProviderConnected && !localProviderActive {
            return "Paused by Super Model"
        }
        return localProviderStatus
    }
    private var savedCloudBaseURL = ""
    private var savedCloudCredentialName = "cloud"

    fileprivate let cloudPresets = [
        CloudProviderPreset(id: "openrouter", label: "OpenRouter", provider: "openrouter",
                            baseURL: "https://openrouter.ai", apiPrefix: "/api/v1"),
        CloudProviderPreset(id: "openai", label: "OpenAI", provider: "openai-compatible",
                            baseURL: "https://api.openai.com", apiPrefix: "/v1"),
        CloudProviderPreset(id: "gemini", label: "Google Gemini", provider: "openai-compatible",
                            baseURL: "https://generativelanguage.googleapis.com", apiPrefix: "/v1beta/openai"),
        CloudProviderPreset(id: "custom", label: "OpenAI-compatible", provider: "openai-compatible",
                            baseURL: "", apiPrefix: "/v1"),
    ]
    // No "Everything" option: running all of Wisp (Ask, Brief, mail sync,
    // memory, tool results) on the cloud model is too wide for one checkbox.
    // The backend still honours `everything` set in config by hand; the
    // caption below then says so, and Disconnect returns to local models.
    let cloudRoleOptions = [
        (id: "reasoning", label: "Reasoning"),
        (id: "coding", label: "Coding"),
        (id: "research", label: "Research"),
    ]

    // "research" is deliberately last and optional: service/research/
    // orchestrator.py's _research_model() falls back to the `coding` role
    // whenever no explicit "research" override is set, so leaving this on
    // its default isn't a broken state — it's the normal one.
    let roleOrder = ["fast", "coding", "reasoning", "general", "research"]
    // Shown only when the backend is unreachable and can't report the real
    // roster. Mirrors service/config/models.yaml's defaults — if you change
    // them there, change them here, or an offline Settings pane shows models
    // the user never chose.
    let fallbackModels = [
        "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    ]
    let fallbackRoles = [
        "fast": "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
        "coding": "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
        "reasoning": "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
        "general": "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    ]
    private let client = WispClient()

    func label(_ r: String) -> String {
        ["fast": "Fast & routing", "coding": "Coding", "reasoning": "Reasoning",
         "general": "General & agentic", "research": "Research"][r] ?? r
    }
    func desc(_ r: String) -> String {
        ["fast": "Quick replies, picks the expert", "coding": "Writing and fixing code",
         "reasoning": "Multi-step thinking, planning", "general": "Everything else, tool use",
         "research": "Wisp Research's plan/query/extraction/synthesis calls"][r] ?? ""
    }

    var modelChoices: [String] {
        // The bundled-default fallback is only for an unreachable backend; a Mac
        // with its own models must not be offered a model it never installed.
        installed.isEmpty ? fallbackModels : Array(Set(installed + roles.values.filter { !$0.isEmpty })).sorted()
    }

    func selectedModel(for role: String) -> String {
        if role == "research", roles["research"] == nil {
            // No explicit override yet — show what it actually resolves to
            // at runtime (the coding model) rather than an unrelated model
            // that would look "selected" without being true until touched.
            return selectedModel(for: "coding")
        }
        return roles[role] ?? (installed.isEmpty ? fallbackRoles[role] : nil) ?? modelChoices.first ?? ""
    }

    func savedModelLabel(for role: String) -> String {
        guard let model = roles[role], !model.isEmpty else { return "Model unknown" }
        return OverlayModel.abbrev(model)
    }

    func refresh() {
        Task {
            let m = await client.models()
            self.installed = m.installed
            self.roles = m.roles
            await self.refreshFullAccess()
            self.idleMinutes = await client.idleTimeout()
            self.humanizerEnabled = await client.humanizerEnabled()
            await self.refreshBehavior()
            await self.refreshCloud()
            await self.refreshLocalProvider()
        }
    }

    @discardableResult
    func refreshLocalProvider() async -> Bool {
        guard let object = try? await request("GET", path: "inference/local-provider") else {
            localProviderStateUnknown = true
            localProviderStatus = "Wisp could not confirm local inference status. Use Refresh models to retry."
            return false
        }
        applyLocalProviderState(object)
        return true
    }

    private func applyLocalProviderState(_ object: [String: Any]) {
        localProviderConnected = object["enabled"] as? Bool ?? false
        localProviderActive = object["active"] as? Bool ?? false
        localProviderBaseURL = object["base_url"] as? String ?? localProviderBaseURL
        localProviderAPIPrefix = object["api_prefix"] as? String ?? localProviderAPIPrefix
        localProviderModelID = object["model_id"] as? String ?? localProviderModelID
        localProviderContextWindow = SettingsResponseValidator.contextValue(object["context_window"], minimum: 512)
            ?? localProviderContextWindow
        let savedRoles = Set(object["roles"] as? [String] ?? [])
        localProviderSavedAssigned = localProviderConnected && !savedRoles.isEmpty
        savedLocalBinding = SettingsResponseValidator.localBinding(object)
        localProviderToolsQualified = localProviderConnected
            && SettingsResponseValidator.toolQualificationContext(object) != nil
        localProviderQualifiedContext = SettingsResponseValidator.contextValue(object["qualified_context"]) ?? 0
        localProviderStateUnknown = false
        localProviderRoles = localProviderConnected ? savedRoles : ["reasoning"]
        localProviderStatus = !localProviderConnected ? "Not connected"
            : localProviderSavedAssigned
            ? (localProviderToolsQualified
               ? "Tools verified at \(localProviderQualifiedContext.formatted()) tokens"
               : "Configured; use Test & Save to recheck")
            : "Choose what to use it for, then Test & Save"
    }

    private func matchesLocalProviderState(_ object: [String: Any],
                                           baseURL: String, apiPrefix: String,
                                           modelID: String, contextWindow: Int,
                                           roles: Set<String>) -> Bool {
        SettingsResponseValidator.localProviderMatches(object, requested: LocalSavedBinding(
            baseURL: baseURL, apiPrefix: apiPrefix, modelID: modelID,
            contextWindow: contextWindow, roles: roles))
    }

    private static let localToolRoles: Set<String> = ["agent", "coding"]

    func discoverLocalProviderModels() {
        localProviderSaving = true
        localProviderStatus = "Looking for models…"
        Task {
            await PendingConfigWrites.shared.begin()
            do {
                let object = try await request("POST", path: "inference/local-provider/probe",
                                               body: ["base_url": localProviderBaseURL,
                                                      "api_prefix": localProviderAPIPrefix])
                localProviderModels = object["models"] as? [String] ?? []
                if !localProviderModels.contains(localProviderModelID) {
                    localProviderModelID = localProviderModels.first ?? ""
                }
                localProviderStatus = localProviderModels.isEmpty
                    ? "The app is reachable but reports no models"
                    : "Found \(localProviderModels.count) model(s)"
            } catch {
                localProviderStatus = error.localizedDescription
            }
            localProviderSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    func connectLocalProvider() {
        let origin = CloudCredentialStore.normalizedBaseURL(localProviderBaseURL)
        let prefix = localProviderAPIPrefix
        let model = localProviderModelID.trimmingCharacters(in: .whitespacesAndNewlines)
        let window = localProviderContextWindow
        let requestedRoles = localProviderRoles
        let needsTools = !requestedRoles.isDisjoint(with: Self.localToolRoles)
        let requestedBinding = LocalSavedBinding(baseURL: origin, apiPrefix: prefix,
                                                 modelID: model, contextWindow: window,
                                                 roles: requestedRoles)
        let priorStateKnown = !localProviderStateUnknown
        let priorBinding = savedLocalBinding
        localProviderSaving = true
        localProviderStatus = needsTools
            ? "Testing tool calling… this can take up to a minute."
            : "Checking the local inference app…"
        Task {
            await PendingConfigWrites.shared.begin()
            var didSendRequest = false
            do {
                guard let url = URLComponents(string: origin), url.scheme == "http",
                      url.host == "127.0.0.1", let port = url.port,
                      (1024...65535).contains(port), port != 8000, port != 8765, port != 8791,
                      url.user == nil, url.password == nil,
                      url.path.isEmpty || url.path == "/",
                      url.query == nil, url.fragment == nil,
                      !model.isEmpty, !requestedRoles.isEmpty else {
                    throw CloudSettingsError.message(
                        "Use an app on a distinct 127.0.0.1 port, select its exact model, and choose what to use it for.")
                }
                didSendRequest = true
                let object = try await request("POST", path: "inference/local-provider", body: [
                    "base_url": origin, "api_prefix": prefix, "model_id": model,
                    "context_window": window, "roles": requestedRoles.sorted(),
                ], timeout: needsTools ? 420 : 40)
                guard matchesLocalProviderState(object, baseURL: origin, apiPrefix: prefix,
                                                modelID: model, contextWindow: window,
                                                roles: requestedRoles) else {
                    throw SettingsResponseError.invalid
                }
                applyLocalProviderState(object)
                localProviderStatus = needsTools
                    ? "Tools verified at \(localProviderQualifiedContext.formatted()) tokens"
                    : "Streaming reply verified"
                self.roles = (await client.models()).roles
                await refreshCloud()
            } catch {
                let failure = error.localizedDescription
                if !didSendRequest {
                    localProviderStatus = failure
                } else if await refreshLocalProvider() {
                    self.roles = (await client.models()).roles
                    await refreshCloud()
                    var httpStatus: Int?
                    if let settingsError = error as? CloudSettingsError,
                       case let .http(statusCode, _) = settingsError {
                        httpStatus = statusCode
                    }
                    // Local probe 4xx errors occur before persistence. A 5xx
                    // can follow the save, and a lost/malformed reply leaves
                    // the commit outcome uncertain until this validated GET.
                    switch SettingsResponseValidator.localSaveRecovery(
                        priorStateKnown: priorStateKnown, prior: priorBinding,
                        refreshed: savedLocalBinding, requested: requestedBinding, httpStatus: httpStatus) {
                    case .recoveredReply:
                        localProviderStatus = "Connection saved; Wisp recovered after losing the reply."
                    case .currentTestFailed:
                        localProviderStatus = "The current local provider test failed: \(failure) "
                            + (localProviderSavedAssigned
                               ? "The earlier saved assignment remains."
                               : "Nothing is assigned to this app.")
                    case .outcomeUnknown:
                        localProviderStatus = "Wisp could not confirm whether the current local provider test completed. "
                            + (localProviderSavedAssigned
                               ? "The saved assignment remains."
                               : "No assignment is confirmed.")
                    case .savedMatchUnconfirmed:
                        localProviderStatus = "The saved assignment matches this request, but Wisp could not confirm this test completed."
                    case .failed:
                        localProviderStatus = failure
                    }
                } else {
                    localProviderStateUnknown = true
                    localProviderStatus = "Wisp could not confirm whether the change was saved. Reopen Settings to refresh before retrying."
                }
            }
            localProviderSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    /// Run the backend's tool-calling qualification without saving anything. Takes up to a
    /// minute on a large model; uses only synthetic prompts.
    func testLocalProviderTools() {
        let origin = CloudCredentialStore.normalizedBaseURL(localProviderBaseURL)
        let model = localProviderModelID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !model.isEmpty else {
            localProviderStatus = "Choose the app's exact model first."
            return
        }
        localProviderTesting = true
        localProviderQualification = nil
        localProviderStatus = "Testing tool calling… this can take up to a minute."
        let window = localProviderContextWindow
        let prefix = localProviderAPIPrefix
        Task {
            do {
                let object = try await request("POST", path: "inference/local-provider/qualify", body: [
                    "base_url": origin, "api_prefix": prefix, "model_id": model,
                    "context_window": window,
                ], timeout: 420)  // a cold model load plus a full-window prompt can take a while
                guard SettingsResponseValidator.validQualificationResponse(object, requestedContext: window) else {
                    throw SettingsResponseError.invalid
                }
                let rows = (object["checks"] as? [[String: Any]] ?? []).compactMap { row -> LocalQualificationCheck? in
                    guard let id = row["id"] as? String, let label = row["label"] as? String,
                          let ok = row["ok"] as? Bool else { return nil }
                    return LocalQualificationCheck(id: id, label: label, ok: ok,
                                                   detail: row["detail"] as? String ?? "",
                                                   required: row["required"] as? Bool ?? true)
                }
                let qualified = SettingsResponseValidator.qualificationSucceeded(object)
                let effective = SettingsResponseValidator.contextValue(object["effective_context"]) ?? 0
                localProviderQualification = LocalQualification(
                    qualified: qualified, effectiveContext: effective,
                    hint: object["hint"] as? String ?? "", checks: rows)
                localProviderStatus = qualified
                    ? "This app can run Wisp's tools (\(effective.formatted())-token window)."
                    : "This app can't run Wisp's tools yet."
            } catch {
                localProviderStatus = error.localizedDescription
            }
            localProviderTesting = false
        }
    }

    func disconnectLocalProvider() {
        localProviderSaving = true
        localProviderStatus = "Returning everything to Wisp’s managed model…"
        localProviderQualification = nil
        Task {
            await PendingConfigWrites.shared.begin()
            do {
                let object = try await request("DELETE", path: "inference/local-provider")
                applyLocalProviderState(object)
                localProviderStatus = localProviderConnected
                    ? "The local app is still connected" : "Not connected"
                self.roles = (await client.models()).roles
            } catch {
                let failure = error.localizedDescription
                if await refreshLocalProvider() {
                    self.roles = (await client.models()).roles
                    await refreshCloud()
                    localProviderStatus = localProviderConnected ? failure : "Not connected"
                } else {
                    localProviderStateUnknown = true
                    localProviderStatus = "Wisp could not confirm whether the app was disconnected. Reopen Settings to refresh."
                }
            }
            localProviderSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    private var selectedPreset: CloudProviderPreset {
        cloudPresets.first(where: { $0.id == cloudPreset }) ?? cloudPresets[0]
    }

    func selectCloudPreset(_ id: String) {
        cloudPreset = id
        guard let preset = cloudPresets.first(where: { $0.id == id }) else { return }
        cloudBaseURL = preset.baseURL
        cloudAPIPrefix = preset.apiPrefix
    }

    func setCloudRole(_ role: String, enabled: Bool) {
        var next = cloudRoles
        // "Everything" (only ever from config now) runs all of Wisp on the
        // cloud model, so it stands alone.
        if enabled && role == "everything" { next = [] }
        if enabled && role != "everything" { next.remove("everything") }
        if enabled { next.insert(role) } else { next.remove(role) }
        cloudRoles = next
    }

    private func request(_ method: String, path: String,
                         body: [String: Any]? = nil,
                         timeout: TimeInterval = 40) async throws -> [String: Any] {
        // A write to a provider or its roles, even one that failed or timed out,
        // may have changed which model answers: setup's test no longer applies.
        defer {
            if method != "GET", path.hasPrefix("inference/"), !path.hasSuffix("/probe") {
                NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
            }
        }
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse else {
            throw SettingsResponseError.invalid
        }
        guard (200..<300).contains(http.statusCode) else {
            throw CloudSettingsError.http(statusCode: http.statusCode,
                                          detail: object?["detail"] as? String
                                          ?? "Wisp could not connect to this provider.")
        }
        guard let object, !object.isEmpty,
              SettingsResponseValidator.valid(object, path: path) else {
            throw SettingsResponseError.invalid
        }
        return object
    }

    private func matchesCloudState(_ object: [String: Any], provider: String,
                                   baseURL: String, apiPrefix: String, modelID: String,
                                   contextWindow: Int, roles: Set<String>,
                                   superModelEnabled: Bool,
                                   credentialName: String) -> Bool {
        object["enabled"] as? Bool == true
            && object["provider"] as? String == provider
            && CloudCredentialStore.normalizedBaseURL(object["base_url"] as? String ?? "") == baseURL
            && object["api_prefix"] as? String == apiPrefix
            && object["model_id"] as? String == modelID
            && object["context_window"] as? Int == contextWindow
            && Set(object["roles"] as? [String] ?? []) == roles
            && object["super_model_enabled"] as? Bool == superModelEnabled
            && object["credential_name"] as? String == credentialName
    }

    private func cloudCredentialIsReferenced(_ object: [String: Any],
                                             name: String, baseURL: String) -> Bool {
        object["enabled"] as? Bool == true
            && object["credential_name"] as? String == name
            && CloudCredentialStore.normalizedBaseURL(object["base_url"] as? String ?? "") == baseURL
    }

    private func reconcilePendingCredential(with object: [String: Any]) -> String? {
        guard let pending = PendingCloudCredential.load() else { return nil }
        let requestedStateCommitted = pending.mode == "connect"
            && matchesCloudState(object, provider: pending.provider,
                                 baseURL: pending.baseURL, apiPrefix: pending.apiPrefix,
                                 modelID: pending.modelID,
                                 contextWindow: pending.contextWindow,
                                 roles: Set(pending.roles),
                                 superModelEnabled: pending.superModelEnabled,
                                 credentialName: pending.name)
        let credentialIsReferenced = cloudCredentialIsReferenced(
            object, name: pending.name, baseURL: pending.baseURL)
        do {
            if pending.mode == "disconnect" && credentialIsReferenced {
                PendingCloudCredential.clear()
                return "The disconnect did not complete; cloud inference remains active."
            }
            if credentialIsReferenced {
                if !pending.previousName.isEmpty && !pending.previousBaseURL.isEmpty
                    && (pending.previousName != pending.name
                        || pending.previousBaseURL != pending.baseURL) {
                    try CloudCredentialStore.remove(name: pending.previousName,
                                                    baseURL: pending.previousBaseURL)
                }
            } else if pending.staged || pending.mode == "disconnect" {
                try CloudCredentialStore.remove(name: pending.name, baseURL: pending.baseURL)
            }
            PendingCloudCredential.clear()
            return requestedStateCommitted || pending.mode == "disconnect"
                ? nil
                : "The prior cloud configuration remains active."
        } catch {
            if pending.mode == "disconnect" {
                return "The retired cloud key still needs cleanup."
            }
            return credentialIsReferenced
                ? "The previous Keychain key still needs cleanup."
                : "A staged Keychain key still needs cleanup."
        }
    }

    func refreshCloud() async {
        guard let object = try? await request("GET", path: "inference/cloud") else {
            cloudStateUnknown = true
            cloudStatus = "Wisp could not confirm cloud settings. Use Refresh models to retry."
            return
        }
        cloudStateUnknown = false
        cloudConnected = object["enabled"] as? Bool ?? false
        cloudBaseURL = object["base_url"] as? String ?? cloudBaseURL
        cloudAPIPrefix = object["api_prefix"] as? String ?? cloudAPIPrefix
        cloudModelID = object["model_id"] as? String ?? cloudModelID
        cloudContextWindow = object["context_window"] as? Int ?? cloudContextWindow
        savedCloudRoles = Set(object["roles"] as? [String] ?? [])
        cloudRoles = savedCloudRoles
        superModelEnabled = object["super_model_enabled"] as? Bool ?? false
        savedCloudBaseURL = cloudConnected ? cloudBaseURL : ""
        savedCloudCredentialName = object["credential_name"] as? String ?? "cloud"
        let provider = object["provider"] as? String ?? "openrouter"
        if provider == "openrouter" {
            cloudPreset = "openrouter"
        } else if cloudBaseURL == "https://api.openai.com" {
            cloudPreset = "openai"
        } else if cloudBaseURL == "https://generativelanguage.googleapis.com" {
            cloudPreset = "gemini"
        } else {
            cloudPreset = "custom"
        }
        cloudStatus = cloudConnected
            ? "Connected to \(object["provider_label"] as? String ?? "cloud provider")"
            : "Local models only"
        if cloudConnected && cloudTestOutcomeUnknown {
            cloudStatus = "Provider configuration is present; the last connection test outcome remains unconfirmed. Test & Save again."
        }
        if superModelEnabled {
            let routerStatus = object["super_model_router"] as? String
            if routerStatus == "unavailable" {
                cloudStatus += ". Laya is unavailable on this Mac; Super Model requests stay local"
            } else if routerStatus != "ready" {
                cloudStatus += ". Laya is preparing; requests stay local until it is ready"
            }
        }
        if let warning = reconcilePendingCredential(with: object) {
            cloudStatus += ". \(warning)"
        }
        if PendingCloudCredential.load() != nil {
            cloudStateUnknown = true
            cloudStatus += ". Keychain cleanup is pending; cloud changes are paused until Refresh models succeeds"
        }
    }

    func connectCloud() {
        guard !cloudStateUnknown, PendingCloudCredential.load() == nil else {
            cloudStatus = "Cloud settings or Keychain cleanup are not confirmed. Refresh models before changing them."
            return
        }
        let requestedBase = CloudCredentialStore.normalizedBaseURL(cloudBaseURL)
        let requestedPrefix = cloudAPIPrefix
        let requestedModel = cloudModelID.trimmingCharacters(in: .whitespacesAndNewlines)
        let requestedContext = cloudContextWindow
        let requestedRoles = Array(cloudRoles).sorted()
        let requestedSuperModel = superModelEnabled
        let requestedKey = cloudAPIKey
        let requestedPreset = selectedPreset
        let previousBase = savedCloudBaseURL
        let previousCredentialName = savedCloudCredentialName
        let requestedCredentialName = requestedKey.isEmpty
            ? previousCredentialName
            : "cloud-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let pendingConnect = PendingCloudCredential(
            mode: "connect", staged: !requestedKey.isEmpty,
            name: requestedCredentialName, baseURL: requestedBase,
            previousName: previousCredentialName, previousBaseURL: previousBase,
            provider: requestedPreset.provider, apiPrefix: requestedPrefix,
            modelID: requestedModel, contextWindow: requestedContext,
            roles: requestedRoles, superModelEnabled: requestedSuperModel)
        cloudSaving = true
        cloudStatus = "Testing secure connection…"
        Task {
            await PendingConfigWrites.shared.begin()
            var stagedCredential = false
            do {
                guard let url = URLComponents(string: requestedBase), url.scheme == "https",
                      url.host != nil, url.user == nil, url.password == nil,
                      url.query == nil, url.fragment == nil,
                      url.path.isEmpty || url.path == "/",
                      !requestedModel.isEmpty else {
                    throw CloudSettingsError.message(
                        "Enter an HTTPS provider origin without a path, query, credentials, or fragment, plus an exact model ID.")
                }
                if requestedKey.isEmpty && (!cloudConnected || requestedBase != previousBase
                                             || previousCredentialName.isEmpty) {
                    throw CloudSettingsError.message("Enter the provider API key for the first connection.")
                }
                if !requestedKey.isEmpty {
                    try CloudCredentialStore.save(requestedKey, name: requestedCredentialName,
                                                  baseURL: requestedBase)
                    stagedCredential = true
                }
                let body: [String: Any] = [
                    "provider": requestedPreset.provider, "base_url": requestedBase,
                    "api_prefix": requestedPrefix, "model_id": requestedModel,
                    "context_window": requestedContext, "roles": requestedRoles,
                    "super_model_enabled": requestedSuperModel,
                    "credential_name": requestedCredentialName,
                ]
                var object: [String: Any]?
                var requestError: Error?
                var recoveredStatus: String?
                var recoveredTestOutcomeUnknown = false
                do {
                    object = try await request("POST", path: "inference/cloud", body: body)
                    if let response = object,
                       !matchesCloudState(response, provider: requestedPreset.provider,
                                          baseURL: requestedBase, apiPrefix: requestedPrefix,
                                          modelID: requestedModel, contextWindow: requestedContext,
                                          roles: Set(requestedRoles),
                                          superModelEnabled: requestedSuperModel,
                                          credentialName: requestedCredentialName) {
                        throw SettingsResponseError.invalid
                    }
                } catch {
                    requestError = error
                    object = nil
                    // Even an HTTP 400/500 can follow a successful save: the
                    // backend persists before warming the router and replying.
                    // Only a complete GET can establish whether the staged
                    // credential is still referenced.
                    if let current = try? await request("GET", path: "inference/cloud") {
                        if matchesCloudState(current, provider: requestedPreset.provider,
                                             baseURL: requestedBase, apiPrefix: requestedPrefix,
                                             modelID: requestedModel, contextWindow: requestedContext,
                                             roles: Set(requestedRoles),
                                             superModelEnabled: requestedSuperModel,
                                             credentialName: requestedCredentialName) {
                            object = current
                            recoveredTestOutcomeUnknown = !(error is CloudSettingsError)
                            recoveredStatus = recoveredTestOutcomeUnknown
                                ? "The requested provider configuration is present, but this connection test's outcome could not be confirmed. Test & Save again."
                                : "The provider test failed: \(error.localizedDescription). The saved configuration is still present."
                        } else {
                            if cloudCredentialIsReferenced(current,
                                                           name: requestedCredentialName,
                                                           baseURL: requestedBase) {
                                pendingConnect.save()
                                cloudStateUnknown = true
                                throw CloudSettingsError.message(
                                    "Wisp saved a different provider configuration that still uses the staged key. The key was preserved; refresh Settings before retrying.")
                            }
                            if stagedCredential {
                                do {
                                    try CloudCredentialStore.remove(name: requestedCredentialName,
                                                                    baseURL: requestedBase)
                                } catch {
                                    pendingConnect.save()
                                    cloudStateUnknown = true
                                    throw CloudSettingsError.message(
                                        "The provider change failed. The staged Keychain key will be cleaned up on the next Settings refresh.")
                                }
                            }
                            throw requestError ?? CloudSettingsError.message(
                                "The provider change was not saved.")
                        }
                    }
                }
                guard let object else {
                    pendingConnect.save()
                    cloudStateUnknown = true
                    cloudStatus = "Wisp could not confirm whether the provider change was saved. Existing access and the staged key were preserved; reopen Settings to reconcile."
                    cloudSaving = false
                    await PendingConfigWrites.shared.end()
                    return
                }
                cloudAPIKey = ""
                cloudConnected = true
                cloudTestOutcomeUnknown = recoveredTestOutcomeUnknown
                savedCloudRoles = Set(object["roles"] as? [String] ?? [])
                cloudRoles = savedCloudRoles
                cloudStateUnknown = false
                savedCloudBaseURL = requestedBase
                savedCloudCredentialName = requestedCredentialName
                cloudStatus = recoveredStatus
                    ?? "Connected to \(object["provider_label"] as? String ?? requestedPreset.label)"
                if object["restart_required"] as? Bool == true {
                    cloudStatus += ". Restarting Wisp's engine on the cloud model…"
                    NotificationCenter.default.post(name: .wispRestartBackend, object: nil)
                }
                if requestedSuperModel, object["super_model_router"] as? String != "ready" {
                    cloudStatus += ". Laya is preparing; requests stay local until it is ready"
                }
                if !previousBase.isEmpty
                    && (previousBase != requestedBase || previousCredentialName != requestedCredentialName) {
                    do {
                        try CloudCredentialStore.remove(name: previousCredentialName,
                                                        baseURL: previousBase)
                    } catch {
                        pendingConnect.save()
                        cloudStateUnknown = true
                        cloudStatus += ". The previous Keychain key could not be removed."
                    }
                }
                self.roles = (await client.models()).roles
                await refreshLocalProvider()
            } catch {
                cloudStatus = error.localizedDescription
                if PendingCloudCredential.load() != nil { cloudStateUnknown = true }
            }
            cloudSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    func disconnectCloud() {
        guard !cloudStateUnknown, PendingCloudCredential.load() == nil else {
            cloudStatus = "Cloud settings or Keychain cleanup are not confirmed. Refresh models before changing them."
            return
        }
        let oldBase = savedCloudBaseURL
        let oldCredentialName = savedCloudCredentialName
        let pendingDisconnect = PendingCloudCredential(
            mode: "disconnect", staged: false,
            name: oldCredentialName, baseURL: oldBase,
            previousName: "", previousBaseURL: "",
            provider: selectedPreset.provider, apiPrefix: cloudAPIPrefix,
            modelID: cloudModelID, contextWindow: cloudContextWindow,
            roles: Array(cloudRoles).sorted(),
            superModelEnabled: superModelEnabled)
        cloudSaving = true
        cloudStatus = "Returning cloud roles to local models…"
        Task {
            await PendingConfigWrites.shared.begin()
            do {
                var confirmedDisabled = false
                do {
                    let object = try await request("DELETE", path: "inference/cloud")
                    confirmedDisabled = object["enabled"] as? Bool == false
                    if object["restart_required"] as? Bool == true {
                        NotificationCenter.default.post(name: .wispRestartBackend, object: nil)
                    }
                } catch {
                    if let current = try? await request("GET", path: "inference/cloud") {
                        if current["enabled"] as? Bool == false {
                            confirmedDisabled = true
                        } else {
                            throw error
                        }
                    } else {
                        pendingDisconnect.save()
                        cloudStateUnknown = true
                        throw CloudSettingsError.message(
                            "Wisp could not confirm whether cloud inference was disabled. The existing key was preserved for reconciliation on the next Settings refresh.")
                    }
                }
                guard confirmedDisabled else {
                    throw CloudSettingsError.message("Wisp could not confirm that cloud inference was disabled.")
                }
                cloudConnected = false
                savedCloudBaseURL = ""
                savedCloudCredentialName = "cloud"
                cloudRoles = []
                savedCloudRoles = []
                cloudStateUnknown = false
                superModelEnabled = false
                cloudAPIKey = ""
                cloudStatus = "Local models only"
                if !oldBase.isEmpty {
                    do {
                        try CloudCredentialStore.remove(name: oldCredentialName, baseURL: oldBase)
                    } catch {
                        pendingDisconnect.save()
                        cloudStateUnknown = true
                        cloudStatus = "Local models only. The old cloud key could not be removed from Keychain."
                    }
                }
                self.roles = (await client.models()).roles
                await refreshLocalProvider()
            } catch {
                cloudStatus = error.localizedDescription
                if PendingCloudCredential.load() != nil { cloudStateUnknown = true }
            }
            cloudSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    func setHumanizerEnabled(_ on: Bool) {
        humanizerEnabled = on
        Task { self.humanizerEnabled = await client.setHumanizerEnabled(on) }
    }

    func refreshBehavior() async {
        guard !behaviorChanging else { return }
        behaviorRequestGeneration &+= 1
        await readBehaviorSettings(generation: behaviorRequestGeneration)
    }

    private func readBehaviorSettings(generation: UInt64) async {
        let settings = await client.behaviorSettings()
        guard generation == behaviorRequestGeneration else { return }
        guard let settings, settings["updates_settled"] as? Bool == true,
              let on = settings["enabled"] as? Bool,
              let epoch = settings["control_epoch"] as? String else {
            behaviorEnabled = nil
            behaviorControlEpoch = nil
            behaviorStatus = "Couldn't confirm learning settings. Wisp may still be learning. Check status or try turning it off."
            return
        }
        behaviorEnabled = on
        behaviorControlEpoch = epoch
        behaviorRemote = settings["remote_active"] as? Bool ?? false
        behaviorPhraser = settings["phraser"] as? Bool ?? true
        behaviorStatus = nil
    }

    func setBehaviorEnabled(_ on: Bool) {
        guard !behaviorChanging else { return }
        behaviorRequestGeneration &+= 1
        let generation = behaviorRequestGeneration
        let controlEpoch = behaviorControlEpoch
        behaviorControlEpoch = nil
        behaviorChanging = true
        behaviorStatus = on ? "Turning learning on. Waiting for confirmation…"
                            : "Turning learning off. Waiting for confirmation…"
        Task {
            if let receipt = await client.setBehaviorEnabled(on, controlEpoch: controlEpoch),
               let actual = receipt["enabled"] as? Bool,
               let epoch = receipt["control_epoch"] as? String {
                guard generation == self.behaviorRequestGeneration else { return }
                self.behaviorEnabled = actual
                self.behaviorControlEpoch = epoch
                self.behaviorRemote = receipt["remote_active"] as? Bool ?? false
                self.behaviorPhraser = receipt["phraser"] as? Bool ?? true
                self.behaviorStatus = nil
            } else {
                // A lost response may mean the write succeeded. Only a fresh
                // authoritative read can resolve it; double failure is unknown.
                await self.readBehaviorSettings(generation: generation)
            }
            guard generation == self.behaviorRequestGeneration else { return }
            if let actual = self.behaviorEnabled, actual != on {
                self.behaviorStatus = actual
                    ? "Wisp confirmed that learning is still on. The requested change was not accepted."
                    : "Wisp confirmed that learning is still off. The requested change was not accepted."
            }
            self.behaviorChanging = false
        }
    }

    /// Deletes everything Wisp learned about the user's routines, plus the
    /// untouched reminders and holds it created on its own.
    func forgetBehaviorData() {
        behaviorForgetStatus = "Forgetting…"
        Task {
            guard let result = await client.forgetBehaviorData() else {
                self.behaviorForgetStatus = "Couldn't forget what Wisp learned. Try again."
                return
            }
            let removed = (result["removed_items"] as? [Any])?.count ?? 0
            let kept = result["kept_items"] as? Int ?? 0
            let failed = (result["files_failed"] as? [Any])?.count ?? 0
            var text = failed > 0
                ? "Forgot most of what Wisp learned, but \(failed) file\(failed == 1 ? "" : "s") couldn't be deleted. Check the folder permissions and try again."
                : "Forgot what Wisp learned."
            if removed > 0 { text += " Removed \(removed) reminder\(removed == 1 ? "" : "s") or hold\(removed == 1 ? "" : "s") Wisp had added." }
            if kept > 0 { text += " Kept \(kept) you changed." }
            self.behaviorForgetStatus = text
        }
    }

    /// The settings quick settings shows, without the models and providers.
    func refreshQuick() async {
        await refreshFullAccess()
        await refreshBehavior()
        await refreshBriefTimes()
    }

    func refreshFullAccess() async {
        guard !fullAccessPending else { return }
        let confirmed = await client.fullAccessState()
        guard !fullAccessPending else { return }
        fullAccess = confirmed
    }

    func setFullAccess(_ on: Bool) {
        guard !fullAccessPending else { return }
        fullAccessPending = true
        Task { await self.confirmFullAccess(on) }
    }

    /// Ask for Full access on or off and return what the service confirmed.
    /// One change at a time: while one is pending, another is refused (nil).
    func requestFullAccess(_ on: Bool) async -> Bool? {
        guard !fullAccessPending else { return nil }
        fullAccessPending = true
        return await confirmFullAccess(on)
    }

    @discardableResult
    private func confirmFullAccess(_ on: Bool) async -> Bool? {
        var confirmed = await client.requestFullAccess(on)
        // A lost reply may mean the write happened; only a fresh read can tell.
        if confirmed == nil { confirmed = await client.fullAccessState() }
        fullAccess = confirmed
        fullAccessPending = false
        return confirmed
    }

    #if WISP_NOTCH_QA || WISP_CHAT_QA
    func qaShow(fullAccess: Bool?, briefTimes: WispClient.BriefTimes?) {
        self.fullAccess = fullAccess
        self.briefTimes = briefTimes
    }
    #endif

    var fullAccessExplanation: String {
        if fullAccessPending { return "Updating Full access. Waiting for confirmation…" }
        guard let on = fullAccess else {
            return "Full access hasn't been confirmed. Wisp may still be acting without asking. Check status or turn it off."
        }
        return on ? "Actions run automatically with no confirmation."
                  : "Risky actions pause for your confirmation."
    }

    func refreshBriefTimes() async {
        guard !briefTimesPending else { return }
        let confirmed = await client.briefTimes()
        guard !briefTimesPending else { return }
        briefTimes = confirmed
    }

    func setBriefTimes(morning: Int? = nil, evening: Int? = nil) {
        guard !briefTimesPending else { return }
        briefTimesPending = true
        Task {
            var confirmed = await client.setBriefTimes(morning: morning, evening: evening)
            if confirmed == nil { confirmed = await client.briefTimes() }
            self.briefTimes = confirmed
            self.briefTimesPending = false
        }
    }

    /// What "Learn my routines" does with data, which differs when a remote
    /// inference server is configured (the learned summary is part of the prompt,
    /// and wording / event labeling would send calendar titles to that server).
    /// Settings and quick settings both show exactly this.
    var behaviorExplanation: String {
        if behaviorChanging {
            return behaviorStatus ?? "Updating learning settings. Waiting for confirmation…"
        }
        guard let enabled = behaviorEnabled else {
            return behaviorStatus
                ?? "Learning status has not been confirmed. Wisp may still be learning. Check status or turn learning off."
        }
        let sources = "Learns from your calendar, reminders, message timing and how you use Wisp. "
            + "Optional app, website, email and message interpretation has separate source controls in Memory › Routines. "
            + "Nudges at most 4 a day. "
        guard enabled else {
            return "Off. Wisp is not learning your routines or sending nudges. What it already "
                + "learned stays on this Mac and is still deleted automatically as it ages; "
                + "use Forget to erase it now."
        }
        guard behaviorRemote else {
            return sources + "What it learns stays on this Mac, and old data is deleted automatically."
        }
        if behaviorPhraser {
            return sources + "Learned data is stored on this Mac, but you use a remote inference "
                + "server: nudge wording and event categorization send calendar titles and nudge "
                + "details to your configured server, and a short summary of your routines is part "
                + "of the prompts it receives."
        }
        return sources + "Learned data is stored on this Mac, but you use a remote inference server: "
            + "a short summary of your routines is part of the prompts it receives. Nudge wording "
            + "and event categorization are off, so calendar titles are not sent for them."
    }

    func setIdleMinutes(_ minutes: Double) {
        idleMinutes = minutes
        Task { self.idleMinutes = await client.setIdleTimeout(minutes) }
    }

    func set(_ role: String, _ model: String) {
        roles[role] = model
        saving = true
        Task {
            await PendingConfigWrites.shared.begin()
            var req = URLRequest(url: WispClient.baseURL.appendingPathComponent("config"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["role": role, "model": model])
            _ = try? await URLSession.shared.data(for: req)
            self.saving = false
            await PendingConfigWrites.shared.end()
            // A role now points at another model: setup's test no longer applies.
            NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
        }
    }
}

struct SettingsView: View {
    private struct Pane: Identifiable {
        let id: String
        let title: String
        let symbol: String
    }

    private var behaviorExplanation: String { loader.behaviorExplanation }

    private var fullAccessLabel: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Full access")
                .font(.system(size: 14, weight: .medium))
            Text(loader.fullAccessExplanation)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var behaviorLearningLabel: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Learn my routines")
                .font(.system(size: 14, weight: .medium))
            Text(behaviorExplanation)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var panes: [Pane] {
        #if WISP_SETTINGS_QA
        [Pane(id: "models", title: "Models", symbol: "cpu")]
        #else
        [Pane(id: "general", title: "General", symbol: "slider.horizontal.3"),
         Pane(id: "models", title: "Models", symbol: "cpu"),
         Pane(id: "google", title: "Google", symbol: "envelope.badge.shield.half.filled"),
         Pane(id: "activity", title: "Activity", symbol: "clock.arrow.circlepath"),
         Pane(id: "privacy", title: "Privacy", symbol: "hand.raised")]
        #endif
    }

    // App-scoped (AppDelegate owns it), shared with the notch's quick settings,
    // so both show the same confirmed and pending states.
    @ObservedObject private var loader: SettingsLoader
    /// The Wisp window's Settings section (true), or the standalone form with its
    /// own sidebar, fixed frame and black surface (false), which only the
    /// WISP_SETTINGS_QA render still uses. Embedded, it follows the Mac's appearance.
    private let embedded: Bool
    /// Settings › Models › Setup's guide, the app's one SetupGuideModel. Without
    /// one (the QA harnesses) Setup guide… posts .wispOpenSetupGuide instead.
    private let setup: SetupGuideModel?
    /// Setup is standing in for the first-run window, hidden while it is up:
    /// Setup offers the way back to it.
    private let setupStandsInForWelcome: Bool
    @MainActor init(loader: SettingsLoader, setup: SetupGuideModel? = nil, embedded: Bool = false,
                    setupStandsInForWelcome: Bool = false) {
        self.loader = loader
        self.setup = setup
        self.embedded = embedded
        self.setupStandsInForWelcome = setupStandsInForWelcome
    }
    /// Where Settings keeps its last pane, so it reopens where the person left it.
    /// AppDelegate writes them too, to open Settings › Models › Setup.
    static let paneKey = "WispSettingsPane"
    static let modelsPaneKey = "WispModelsPane"
    private var palette: ThemePalette { embedded ? .wispWindow : .notch }
    @ObservedObject private var sync = SyncProgress.shared
    // Keep the last-used settings pane so reopening Settings returns to the
    // user's context when the window opens again.
    @AppStorage(SettingsView.paneKey) private var selectedPane = "models"
    @AppStorage(SettingsView.modelsPaneKey) private var modelsPane = "assignments"
    // Off by default — see BrowserHistoryReader's doc comment on why this
    // needs its own explicit opt-in rather than following Mail/Notes/Messages
    // (which sync as soon as their own TCC permission is granted).
    @AppStorage(BrowserHistoryReader.enabledKey) private var browserHistoryEnabled = false
    @AppStorage(ContactsReader.enabledKey) private var contactsEnabled = true
    @ObservedObject private var browserPrivacySync = BrowserHistoryReader.delivery
    @ObservedObject private var contactsPrivacySync = ContactsReader.delivery
    @State private var confirmForgetBehavior = false

    var body: some View {
        if embedded {
            VStack(alignment: .leading, spacing: 0) {
                embeddedHeader
                paneScroll
            }
            .environment(\.themePalette, palette)
            .foregroundStyle(ChatTheme.text)
            .tint(palette.accent)
            .task { refreshOnAppear() }
        } else {
            HStack(spacing: 0) {
                sidebar
                Rectangle().fill(Theme.hairline).frame(width: 1)
                paneScroll
            }
            .frame(width: 760, height: 620)
            .background(Theme.surface)
            .foregroundStyle(Theme.textPrimary)
            .tint(Theme.accent)
            .preferredColorScheme(.dark)
            .task { refreshOnAppear() }
        }
    }

    private func refreshOnAppear() {
        // The QA renders never reach Wisp's service.
        #if !WISP_SETTINGS_QA && !WISP_CHAT_QA
        loader.refresh()
        #endif
    }

    /// The Wisp window's Settings header: the panes as one segmented row, in
    /// place of the standalone form's sidebar.
    private var embeddedHeader: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Settings").font(.system(size: 22, weight: .medium)).foregroundStyle(ChatTheme.text)
                Text("Models, Google, activity, privacy and access.")
                    .font(.system(size: 13)).foregroundStyle(ChatTheme.text3)
            }
            Picker("Settings", selection: $selectedPane) {
                ForEach(panes) { pane in Text(pane.title).tag(pane.id) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
        .padding(.horizontal, 24).padding(.top, 18).padding(.bottom, 4)
    }

    @ViewBuilder
    private var paneScroll: some View {
        ScrollView(.vertical) {
            if embedded {
                // A readable column on a wide window, from the left like the other sections.
                paneContent.padding(24)
                    .frame(maxWidth: 720, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                paneContent.padding(24)
            }
        }
        .scrollIndicators(.visible)
    }

    /// The standalone form's pane list.
    private var sidebar: some View {
            VStack(alignment: .leading, spacing: 6) {
                Text("SETTINGS")
                    .font(.system(size: 11, weight: .semibold))
                    .tracking(1.2)
                    .foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 12)
                ForEach(panes) { pane in
                    Button { selectedPane = pane.id } label: {
                        HStack(spacing: 10) {
                            Image(systemName: pane.symbol).frame(width: 18)
                            Text(pane.title)
                            Spacer(minLength: 0)
                        }
                        .font(.system(size: 13, weight: selectedPane == pane.id ? .semibold : .medium))
                        .foregroundStyle(selectedPane == pane.id ? Theme.textPrimary : Theme.textSecondary)
                        .padding(.horizontal, 12)
                        .frame(height: 36)
                        .background(RoundedRectangle(cornerRadius: 9)
                            .fill(selectedPane == pane.id ? Theme.chipFill : Color.clear))
                        .overlay(RoundedRectangle(cornerRadius: 9)
                            .stroke(selectedPane == pane.id ? Theme.chipStroke : Color.clear, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(pane.title)
                }
                Spacer(minLength: 0)
            }
            .frame(width: 164)
            .padding(16)
            .background(Theme.chipFill.opacity(0.35))
    }

    private var paneContent: some View {
            VStack(alignment: .leading, spacing: 16) {
                if selectedPane == "general" {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Personal memory").font(.title2.weight(.medium))
                        Text("Review facts, evidence, and possible connections.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Research") { WispRouter.shared.open(.research) }
                    Button("Open Memory") { WispRouter.shared.open(.memory(.facts)) }
                }
                Divider()
                }
                if selectedPane == "models" {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Models").font(.title2.weight(.medium))
                    VStack(alignment: .leading, spacing: 14) {
                        if modelsPane == "assignments" {
                        EngineSettingsView()
                        Text("Pick the expert for each kind of task")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        ForEach(loader.roleOrder, id: \.self) { role in
                            HStack(alignment: .center) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(loader.label(role))
                                        .font(.system(size: 14, weight: .medium))
                                    Text(loader.desc(role))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if role == "reasoning" && loader.localProviderStateUnknown {
                                    Label("Assignment unknown", systemImage: "questionmark.circle")
                                        .accessibilityLabel("Assignment unknown")
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 190, alignment: .trailing)
                                } else if loader.localProviderActive && loader.localProviderSavedAssigned
                                            && role == "reasoning" {
                                    Label("Local · \(loader.savedModelLabel(for: role))",
                                          systemImage: "desktopcomputer")
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 190, alignment: .trailing)
                                } else if loader.cloudStateUnknown {
                                    Label("Assignment unknown", systemImage: "questionmark.circle")
                                        .accessibilityLabel("Assignment unknown")
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 190, alignment: .trailing)
                                } else if loader.savedCloudRoles.contains(role) {
                                    Label("Cloud · \(loader.savedModelLabel(for: role))",
                                          systemImage: "cloud.fill")
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 190, alignment: .trailing)
                                } else if loader.roles.isEmpty {
                                    Label("Model unknown", systemImage: "questionmark.circle")
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .frame(width: 190, alignment: .trailing)
                                } else {
                                    Picker("", selection: Binding(
                                        get: { loader.selectedModel(for: role) },
                                        set: { loader.set(role, $0) })) {
                                        ForEach(loader.modelChoices, id: \.self) {
                                            Text(OverlayModel.abbrev($0)).tag($0)
                                        }
                                    }
                                    .pickerStyle(.menu)
                                    .labelsHidden()
                                    .redrawsOnAppearanceChange()
                                    .frame(width: 190)
                                }
                            }
                            if role != loader.roleOrder.last { Divider() }
                        }
                        Divider()
                        HStack {
                            Text("Manage downloaded models and cloud keys")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Local models…") { modelsPane = "local" }
                            Button("Cloud…") { modelsPane = "cloud" }
                            Button("Setup guide…") {
                                if setup != nil {
                                    modelsPane = "setup"
                                } else {
                                    NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil)
                                }
                            }
                        }

                        } else {
                        Button {
                            modelsPane = "assignments"
                        } label: {
                            Label("Models", systemImage: "chevron.left")
                        }
                        .buttonStyle(.link)
                        }
                        if modelsPane == "setup", let setup {
                        SettingsSetupPane(model: setup, standsInForWelcome: setupStandsInForWelcome,
                                          backToWelcome: { modelsPane = "assignments" })
                        }
                        if modelsPane == "local" {
                        GroupBox {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack(alignment: .firstTextBaseline) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Local")
                                            .font(.system(size: 15, weight: .semibold))
                                        Text("Connect Ollama, LM Studio, llama.cpp, Ling, or another OpenAI-compatible app running on this Mac.")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Label(loader.localProviderDisplayStatus,
                                          systemImage: loader.localProviderStateUnknown
                                            ? "questionmark.circle" : loader.localProviderActive
                                            ? "checkmark.circle.fill" : "circle.dashed")
                                        .font(.caption)
                                        .foregroundStyle(loader.localProviderActive
                                                         && !loader.localProviderStateUnknown
                                                         ? .green : .secondary)
                                }
                                if loader.localProviderStatus != loader.localProviderDisplayStatus {
                                    Label(loader.localProviderStatus, systemImage: "info.circle")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                LabeledContent("App address") {
                                    TextField("http://127.0.0.1:8767", text: $loader.localProviderBaseURL)
                                        .textFieldStyle(.roundedBorder).frame(width: 300)
                                }
                                LabeledContent("API prefix") {
                                    TextField("/v1", text: $loader.localProviderAPIPrefix)
                                        .textFieldStyle(.roundedBorder).frame(width: 180)
                                }
                                HStack {
                                    Button("Find Models") { loader.discoverLocalProviderModels() }
                                        .disabled(loader.localProviderSaving)
                                    if !loader.localProviderModels.isEmpty {
                                        Picker("Available model", selection: $loader.localProviderModelID) {
                                            ForEach(loader.localProviderModels, id: \.self) { model in
                                                Text(model).tag(model)
                                            }
                                        }
                                        .redrawsOnAppearanceChange()
                                        .frame(width: 260)
                                    }
                                }
                                LabeledContent("Model ID") {
                                    TextField("Exact model ID", text: $loader.localProviderModelID)
                                        .textFieldStyle(.roundedBorder).frame(width: 300)
                                }
                                LabeledContent("Context window") {
                                    Stepper(value: $loader.localProviderContextWindow,
                                            in: 512...262_144, step: 1024) {
                                        Text("\(loader.localProviderContextWindow.formatted()) tokens")
                                            .monospacedDigit().frame(width: 130, alignment: .trailing)
                                    }
                                }
                                Text("What you set the app to load. Wisp measures the real window when it tests tool use and saves the smaller of the two.")
                                    .font(.caption2).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("Use this app for")
                                        .font(.caption).foregroundStyle(.secondary)
                                    ForEach([("reasoning", "Reasoning", "Answers questions; no tools."),
                                             ("agent", "Agent", "Runs Wisp's tools: calendar, mail, messages, files, and more."),
                                             ("coding", "Coding", "Code questions, with tools when needed.")],
                                            id: \.0) { role, title, detail in
                                        Toggle(isOn: Binding(
                                            get: { loader.localProviderRoles.contains(role) },
                                            set: { enabled in
                                                if enabled { loader.localProviderRoles.insert(role) }
                                                else { loader.localProviderRoles.remove(role) }
                                            })) {
                                            HStack(spacing: 6) {
                                                Text(title).font(.system(size: 13, weight: .medium))
                                                Text(detail).font(.caption).foregroundStyle(.secondary)
                                            }
                                        }
                                        .toggleStyle(.checkbox)
                                    }
                                }
                                .disabled(loader.localProviderStateUnknown || loader.localProviderSaving
                                          || loader.localProviderTesting)
                                HStack {
                                    Button("Test Tool Calling") { loader.testLocalProviderTools() }
                                        .disabled(loader.localProviderSaving || loader.localProviderTesting
                                                  || loader.localProviderModelID.isEmpty
                                                  || loader.localProviderStateUnknown)
                                    if loader.localProviderTesting { ProgressView().controlSize(.small) }
                                    Text("Checks the real context window and that the model can call tools. Uses only made-up prompts.")
                                        .font(.caption2).foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                if let report = loader.localProviderQualification {
                                    VStack(alignment: .leading, spacing: 4) {
                                        ForEach(report.checks) { check in
                                            Label(check.label, systemImage: check.ok ? "checkmark.circle.fill"
                                                  : check.required ? "xmark.circle.fill" : "exclamationmark.triangle.fill")
                                                .font(.caption)
                                                .foregroundStyle(check.ok ? Color.green
                                                                 : check.required ? Color.red : Color.orange)
                                            if !check.ok && !check.detail.isEmpty {
                                                Text(check.detail).font(.caption2).foregroundStyle(.secondary)
                                                    .padding(.leading, 22)
                                                    .fixedSize(horizontal: false, vertical: true)
                                            }
                                        }
                                        if !report.hint.isEmpty {
                                            Text(report.hint).font(.caption).foregroundStyle(.primary)
                                                .padding(8)
                                                .background(RoundedRectangle(cornerRadius: 6).fill(Color.orange.opacity(0.12)))
                                                .fixedSize(horizontal: false, vertical: true)
                                        }
                                    }
                                }
                                Text(loader.localProviderStateUnknown
                                     ? "Wisp cannot confirm the current assignment. Use Refresh models before changing this connection."
                                     : loader.localProviderConnected && !loader.localProviderSavedAssigned
                                     ? "This app is connected but nothing is assigned to it. Choose what to use it for, then Test & Save."
                                     : loader.localProviderRoles.contains("agent") || loader.localProviderRoles.contains("coding")
                                     ? "With Agent or Coding selected, the results of Wisp's tools — your calendar, mail, messages, notes and files — are sent to this app to answer you. Wisp tests tool calling first and saves the context window the app really uses. Routing, summaries, search and memory still use Wisp's managed models, and skills stay on this Mac's managed model. A local app without an API key is not identity-verified; connect only one you trust."
                                     : "Reasoning prompts are sent to this app when Super Model is off. Wisp does not automatically send earlier conversation summaries or remembered facts. Routing, summary generation, and tool use stay with managed models. A local app without an API key is not identity-verified; connect only one you trust.")
                                    .font(.caption2).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                HStack {
                                    if loader.localProviderConnected {
                                        Button("Disconnect", role: .destructive) {
                                            loader.disconnectLocalProvider()
                                        }
                                        .disabled(loader.localProviderSaving
                                                  || loader.localProviderStateUnknown)
                                    }
                                    Spacer()
                                    if loader.localProviderSaving { ProgressView().controlSize(.small) }
                                    Button(loader.localProviderConnected ? "Test & Save" : "Connect") {
                                        loader.connectLocalProvider()
                                    }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(loader.localProviderSaving || loader.localProviderTesting
                                              || loader.localProviderStateUnknown
                                              || loader.localProviderModelID.isEmpty
                                              || loader.localProviderRoles.isEmpty)
                                }
                            }
                            .padding(4)
                        }
                        }
                        if modelsPane == "cloud" {
                        GroupBox {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack(alignment: .firstTextBaseline) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Cloud inference")
                                            .font(.system(size: 15, weight: .semibold))
                                        Text("Connect selected workloads while keeping routing and summaries local.")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Label(loader.cloudStateUnknown ? "Status unknown" : loader.cloudStatus,
                                          systemImage: loader.cloudStateUnknown || loader.cloudTestOutcomeUnknown
                                            ? "questionmark.circle" : loader.cloudConnected
                                            ? "checkmark.circle.fill" : "circle.dashed")
                                        .font(.caption)
                                        .foregroundStyle(loader.cloudConnected && !loader.cloudStateUnknown
                                                         && !loader.cloudTestOutcomeUnknown
                                                         ? .green : .secondary)
                                }

                                if loader.cloudStateUnknown {
                                    Label(loader.cloudStatus, systemImage: "info.circle")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }

                                LabeledContent("Provider") {
                                    Picker("Provider", selection: Binding(
                                        get: { loader.cloudPreset },
                                        set: { loader.selectCloudPreset($0) })) {
                                        ForEach(loader.cloudPresets) { preset in
                                            Text(preset.label).tag(preset.id)
                                        }
                                    }
                                    .labelsHidden().redrawsOnAppearanceChange().frame(width: 210)
                                }

                                LabeledContent("HTTPS endpoint") {
                                    TextField("https://provider.example", text: $loader.cloudBaseURL)
                                        .textFieldStyle(.roundedBorder).frame(width: 300)
                                }

                                if loader.cloudPreset == "custom" {
                                    LabeledContent("API prefix") {
                                        TextField("/v1", text: $loader.cloudAPIPrefix)
                                            .textFieldStyle(.roundedBorder).frame(width: 180)
                                    }
                                }

                                LabeledContent("Model ID") {
                                    TextField("provider/model-name", text: $loader.cloudModelID)
                                        .textFieldStyle(.roundedBorder).frame(width: 300)
                                }

                                LabeledContent("API key") {
                                    SecureField(loader.cloudConnected
                                        ? "Leave blank to keep saved key" : "Stored only in Keychain",
                                                text: $loader.cloudAPIKey)
                                        .textFieldStyle(.roundedBorder).frame(width: 300)
                                }

                                LabeledContent("Context window") {
                                    Stepper(value: $loader.cloudContextWindow,
                                            in: 512...262_144, step: 1024) {
                                        Text("\(loader.cloudContextWindow.formatted()) tokens")
                                            .monospacedDigit().frame(width: 130, alignment: .trailing)
                                    }
                                }

                                VStack(alignment: .leading, spacing: 7) {
                                    Toggle(isOn: $loader.superModelEnabled) {
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text("Super Model")
                                                .font(.system(size: 13, weight: .semibold))
                                            Text("Use this cloud model for non-sensitive questions and public web synthesis. Private data, context-dependent follow-ups, Mac access, and outbound actions stay local. Other apps and windows remain open.")
                                                .font(.caption2).foregroundStyle(.secondary)
                                        }
                                    }
                                    .toggleStyle(.switch)
                                    .disabled(loader.cloudStateUnknown)

                                    Divider()

                                    Text("Use cloud model for")
                                        .font(.system(size: 13, weight: .medium))
                                    HStack(spacing: 18) {
                                        ForEach(loader.cloudRoleOptions, id: \.id) { role in
                                            Toggle(role.label, isOn: Binding(
                                                get: { loader.cloudRoles.contains(role.id) },
                                                set: { loader.setCloudRole(role.id, enabled: $0) }))
                                                .toggleStyle(.checkbox)
                                                .disabled(loader.superModelEnabled
                                                          || loader.cloudStateUnknown)
                                        }
                                    }
                                    Text(loader.superModelEnabled
                                         ? "Super Model overrides these workload choices while it is on. Public web tools run locally; the cloud model explains their results."
                                         : loader.cloudRoles.contains("everything")
                                         ? "All of Wisp runs on this model (set in config.yaml): Ask, Brief, summaries, drafts and tools. Your mail, texts and calendar go to the provider. Disconnect to return to local models."
                                         : "Fast routing, message/email summaries, embeddings, and tool execution stay local.")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }

                                Label("Selected prompts and their conversation context are sent to the provider and may incur charges. Your API key stays in macOS Keychain.",
                                      systemImage: "lock.shield")
                                    .font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)

                                HStack {
                                    if loader.cloudConnected {
                                        Button("Disconnect", role: .destructive) {
                                            loader.disconnectCloud()
                                        }
                                        .disabled(loader.cloudSaving || loader.cloudStateUnknown)
                                    }
                                    Spacer()
                                    if loader.cloudSaving { ProgressView().controlSize(.small) }
                                    Button(loader.cloudConnected ? "Test & Save" : "Connect") {
                                        loader.connectCloud()
                                    }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(loader.cloudSaving || loader.cloudStateUnknown
                                              || loader.cloudBaseURL.isEmpty
                                              || loader.cloudModelID.isEmpty)
                                }
                            }
                            .padding(4)
                        }
                        }
                    }
                }
                }

                // Background/scheduled work — currently just the mail history
                // scan. Backed by SyncProgress.shared for the live bar, which
                // MailReader's batch loop updates directly (same process).
                if selectedPane == "google" {
                    GoogleSettingsPane()
                }
                if selectedPane == "activity" {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Activity").font(.title2.weight(.medium))
                    VStack(alignment: .leading, spacing: 14) {
                        syncStatusRow(
                            title: "Mail history (up to a year)",
                            fraction: sync.mailHistoryFraction,
                            activeLabel: "Scanning…",
                            lastDate: sync.mailHistoryLastSynced)
                    }
                }
                }

                // What Wisp is allowed to do (Access) and which accounts it
                // reads from (Linked Accounts) — grouped as "Privacy & Access"
                // since both answer "what can Wisp touch", just at different
                // scopes (an action vs. a data source).
                if selectedPane == "privacy" {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Privacy & Access").font(.title2.weight(.medium))
                    VStack(alignment: .leading, spacing: 14) {
                        if let fullAccess = loader.fullAccess {
                            Toggle(isOn: Binding(
                                get: { fullAccess },
                                set: { loader.setFullAccess($0) })) {
                                fullAccessLabel
                            }
                            .toggleStyle(.switch)
                            .tint(palette.accent)
                            .disabled(loader.fullAccessPending)
                        } else {
                            VStack(alignment: .leading, spacing: 8) {
                                fullAccessLabel
                                HStack {
                                    Button("Check status") { Task { await loader.refreshFullAccess() } }
                                    Button("Turn Full access off") { loader.setFullAccess(false) }
                                }
                                .controlSize(.small)
                                .disabled(loader.fullAccessPending)
                            }
                        }

                        Divider()

                        Toggle(isOn: Binding(get: { browserHistoryEnabled }, set: {
                            browserHistoryEnabled = $0
                            BrowserHistoryReader.setEnabled($0)
                        })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Browser history")
                                    .font(.system(size: 14, weight: .medium))
                                Text(browserPrivacySync.pending
                                     ? "Updating browser access. Wisp will retry until the backend confirms."
                                     : browserHistoryEnabled
                                     ? "Reading recent Safari + Chrome history so Wisp can answer "
                                       + "questions about sites you've visited."
                                     : "Off. Wisp does not read Safari or Chrome history.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text("Only the site, page, and title are kept — never the full "
                                     + "URL's query string, which can carry search text or "
                                     + "sign-in links.")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(palette.accent)

                        Divider()

                        Toggle(isOn: Binding(get: { contactsEnabled }, set: {
                            contactsEnabled = $0
                            ContactsReader.setEnabled($0)
                        })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Contacts").font(.system(size: 14, weight: .medium))
                                Text(contactsPrivacySync.pending
                                     ? "Updating contact access. Wisp will retry until the backend confirms."
                                     : contactsEnabled
                                     ? "Uses permitted contacts to look up names, recipients, and birthdays."
                                     : "Off. Cached contact names, recipients, and birthdays are cleared.")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(palette.accent)

                        Divider()

                        MailMemorySettingsRow()

                        Divider()

                        if loader.behaviorEnabled != nil {
                            Toggle(isOn: Binding(
                                get: { loader.behaviorEnabled == true },
                                set: { loader.setBehaviorEnabled($0) })) {
                                behaviorLearningLabel
                            }
                            .toggleStyle(.switch)
                            .tint(palette.accent)
                            .disabled(loader.behaviorChanging)
                            if !loader.behaviorChanging, let status = loader.behaviorStatus {
                                Text(status).font(.caption).foregroundStyle(.secondary)
                            }
                        } else {
                            VStack(alignment: .leading, spacing: 8) {
                                behaviorLearningLabel
                                HStack {
                                    Button("Check status") {
                                        Task { await loader.refreshBehavior() }
                                    }
                                    Button("Turn learning off") { loader.setBehaviorEnabled(false) }
                                }
                                .controlSize(.small)
                                .disabled(loader.behaviorChanging)
                            }
                        }

                        HStack(spacing: 10) {
                            Button("See what Wisp learned") { WispRouter.shared.open(.memory(.routines)) }
                            Button("Forget what Wisp learned…", role: .destructive) {
                                confirmForgetBehavior = true
                            }
                            if let status = loader.behaviorForgetStatus {
                                Text(status)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .alert("Forget what Wisp learned?", isPresented: $confirmForgetBehavior) {
                            Button("Forget", role: .destructive) { loader.forgetBehaviorData() }
                            Button("Cancel", role: .cancel) {}
                        } message: {
                            Text("This deletes your learned routines, patterns and nudge history, and "
                                 + "removes the reminders and holds Wisp added on its own that you haven't "
                                 + "changed. Anything you edited or completed stays. If \"Learn my "
                                 + "routines\" is on, Wisp starts learning again from scratch.")
                        }

                        Divider()

                        // Native accounts remain managed by macOS. Google inbox
                        // connections are optional and configured separately.
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Linked Accounts")
                                .font(.system(size: 14, weight: .medium))
                            Text("Wisp reads permitted Mail and Calendar accounts configured in macOS. "
                                 + "Mail’s local cache can be out of date while Mail is closed. "
                                 + "Use Settings ▸ Google to connect each Google inbox directly and see its last successful refresh. "
                                 + "Other native accounts can be added in Internet Accounts settings.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Button("Open Internet Accounts Settings…") {
                                if let url = URL(string:
                                    "x-apple.systempreferences:com.apple.preferences.internetaccounts") {
                                    NSWorkspace.shared.open(url)
                                }
                            }
                        }

                        Divider()

                        // What macOS lets Wisp read and do. Each button asks only
                        // when clicked, one row or one system surface at a time.
                        VStack(alignment: .leading, spacing: 10) {
                            Text("macOS permissions")
                                .font(.system(size: 14, weight: .medium))
                            Text("Permissions… shows what Wisp reads, such as Calendar, Mail and Messages, each "
                                 + "with its own Allow. More Access… asks for Screen Recording, Accessibility and "
                                 + "Full Disk Access, one at a time.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            HStack {
                                Button("Permissions…") {
                                    NotificationCenter.default.post(name: .wispOpenPermissions, object: nil)
                                }
                                Button("More Access…") {
                                    NotificationCenter.default.post(name: .wispGrantMoreAccess, object: nil)
                                }
                            }
                        }
                    }
                }
                }

                // Occasional, technical knobs — memory management — that
                // most people set once and forget, as opposed to Models
                // (tuned often) or Automation (watched periodically).
                if selectedPane == "general" {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Advanced").font(.title3.weight(.medium))
                    VStack(alignment: .leading, spacing: 14) {
                        HStack(alignment: .center) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Auto-unload idle models")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.idleMinutes <= 0
                                     ? "Never — models stay resident until you unload them."
                                     : "Unload a model after \(Int(loader.idleMinutes)) min of no use.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Stepper(value: Binding(
                                get: { loader.idleMinutes },
                                set: { loader.setIdleMinutes($0) }), in: 0...120, step: 5) {
                                Text(loader.idleMinutes <= 0 ? "Off" : "\(Int(loader.idleMinutes))m")
                                    .font(.system(size: 13, weight: .medium))
                                    .frame(width: 40, alignment: .trailing)
                            }
                            .frame(width: 130)
                        }

                        Divider()

                        Toggle(isOn: Binding(
                            get: { loader.humanizerEnabled },
                            set: { loader.setHumanizerEnabled($0) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Human-sounding replies")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.humanizerEnabled
                                     ? "Plain conversational replies avoid stock AI phrasing "
                                       + "(forced lists, chatbot sign-offs, filler). Code and "
                                       + "tool-using replies are never affected."
                                     : "Off. Replies use the model's default phrasing.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(palette.accent)

                        Divider()

                        HStack(alignment: .center) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Chat debug log")
                                    .font(.system(size: 14, weight: .medium))
                                Text("Saves the notch's conversation to Downloads, with each turn's "
                                     + "model, route, timing, tool calls and errors.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Button("Export Chat Debug Log…") {
                                NotificationCenter.default.post(name: .wispExportDebugLog, object: nil)
                            }
                        }
                    }
                }
                }

                HStack {
                    Text(selectedPane == "models"
                         ? "Model connections require Test & Save"
                         : "Switches apply instantly")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if loader.saving { ProgressView().controlSize(.small) }
                    if selectedPane == "models" {
                        Button("Refresh models") { loader.refresh() }
                    }
                }
            }
    }

    // One sync's live status: a percentage + linear bar while `fraction` is
    // non-nil (actively running), otherwise a quiet "last synced <relative
    // time>" line, or "not run yet" if it's never completed this launch.
    @ViewBuilder
    private func syncStatusRow(title: String, fraction: Double?, activeLabel: String,
                               lastDate: Date?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.system(size: 14, weight: .medium))
                Spacer()
                if let fraction {
                    Text("\(Int(fraction * 100))%")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if let fraction {
                ProgressView(value: fraction)
                    .progressViewStyle(.linear)
                Text(activeLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let lastDate {
                HStack(spacing: 4) {
                    Text("Last synced")
                    Text(lastDate, style: .relative)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                Text("Not run yet this launch")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// Settings › Models › Setup: the setup guide's header and page in the Settings
/// column, where its own window used to be. The guide's footer stays behind (its
/// Done and Settings… belong to a window of their own), so only Check again comes
/// along, and Back to Welcome while Setup stands in for the first-run window.
/// AppDelegate counts this pane as the shared SetupGuideModel's "settings" host
/// while it is on screen, so the guide's poll runs exactly then.
struct SettingsSetupPane: View {
    @ObservedObject var model: SetupGuideModel
    var standsInForWelcome = false
    var backToWelcome: () -> Void = {}
    @Environment(\.themePalette) private var palette

    var body: some View {
        let guide = SetupGuideView(model: model, inset: 0, palette: palette)
        VStack(alignment: .leading, spacing: 16) {
            guide.header
            guide.pageContent
            HStack(spacing: 8) {
                if let busy = model.busy {
                    ProgressView().controlSize(.small)
                    Text(busy).font(.caption).foregroundStyle(palette.textSecondary)
                }
                Spacer()
                if standsInForWelcome {
                    Button("Back to Welcome") { backToWelcome() }
                        .help("Wisp's first-run window comes back at the step you left")
                }
                Button("Check again") { Task { await model.refresh(); await model.refreshDownloads() } }
                    .disabled(model.busy != nil)
            }
        }
    }
}

/// The Wisp window's Settings section: SettingsView without its own sidebar,
/// frame or black surface, under the window's top bar, in the window's colours.
struct SettingsHomeView: View {
    let loader: SettingsLoader
    let setup: SetupGuideModel?
    let setupStandsInForWelcome: Bool
    @Binding var sidebarVisible: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                if !sidebarVisible { Spacer().frame(width: 62) }   // clear the window's traffic lights
                Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                    .buttonStyle(ChatIconButtonStyle()).help("Show or hide the sidebar (⌘B)")
                    .accessibilityLabel("Toggle sidebar")
                Text("Settings").font(.system(size: 14.5, weight: .medium)).foregroundStyle(ChatTheme.text)
                Spacer()
            }
            .padding(.leading, 14).padding(.trailing, 16)
            .frame(height: 52)
            Rectangle().fill(ChatTheme.line).frame(height: 1)
            SettingsView(loader: loader, setup: setup, embedded: true,
                         setupStandsInForWelcome: setupStandsInForWelcome)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

extension ThemePalette {
    /// The Wisp window's colours (ChatTheme), which follow the Mac's appearance.
    static let wispWindow = ThemePalette(
        surface: .clear, textPrimary: ChatTheme.text, textSecondary: ChatTheme.text2,
        textMuted: ChatTheme.text3, hairline: ChatTheme.line, chipFill: ChatTheme.glass,
        chipStroke: ChatTheme.line2, accent: ChatTheme.teal, onAccent: ChatTheme.sendIcon,
        selectedFill: ChatTheme.tealWash, selectedStroke: ChatTheme.tealLine,
        cardFill: ChatTheme.glass, codeFill: ChatTheme.glass2, actionFill: ChatTheme.glass2)
}
