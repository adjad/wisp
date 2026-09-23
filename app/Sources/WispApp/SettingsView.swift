import AppKit
import CryptoKit
import Security
import SwiftUI

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
    static let service = "com.wisp.inference"

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
        try write(Data(secret.utf8), name: name, baseURL: baseURL)
    }

    static func remove(name: String, baseURL: String) throws {
        let status = SecItemDelete(query(name: name, baseURL: baseURL) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw CloudCredentialError.storage
        }
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

    static func load() -> PendingCloudCredential? {
        let defaults = UserDefaults.standard
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
        let defaults = UserDefaults.standard
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
        let defaults = UserDefaults.standard
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
    var errorDescription: String? {
        if case let .message(value) = self { return value }
        return nil
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
    @Published var installed: [String] = []
    @Published var roles: [String: String] = [:]
    @Published var saving = false
    @Published var fullAccess = false
    @Published var idleMinutes: Double = 5
    @Published var humanizerEnabled = false
    @Published var cloudPreset = "openrouter"
    @Published var cloudBaseURL = "https://openrouter.ai"
    @Published var cloudAPIPrefix = "/api/v1"
    @Published var cloudModelID = ""
    @Published var cloudContextWindow = 16_384
    @Published var cloudAPIKey = ""
    @Published var cloudRoles: Set<String> = []
    @Published var savedCloudRoles: Set<String> = []
    @Published var cloudStateUnknown = true
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
    @Published var localProviderStatus = "Checking local connection…"
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
        CloudProviderPreset(id: "custom", label: "OpenAI-compatible", provider: "openai-compatible",
                            baseURL: "", apiPrefix: "/v1"),
    ]
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
        "Agents-A1-4B-oQe6",
        "gemma-4-E4B-it-qat-4bit",
    ]
    let fallbackRoles = [
        "fast": "Agents-A1-4B-oQe6",
        "coding": "Agents-A1-4B-oQe6",
        "reasoning": "Agents-A1-4B-oQe6",
        "general": "Agents-A1-4B-oQe6",
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
        Array(Set(installed + fallbackModels)).sorted()
    }

    func selectedModel(for role: String) -> String {
        if role == "research", roles["research"] == nil {
            // No explicit override yet — show what it actually resolves to
            // at runtime (the coding model) rather than an unrelated model
            // that would look "selected" without being true until touched.
            return selectedModel(for: "coding")
        }
        return roles[role] ?? fallbackRoles[role] ?? modelChoices.first ?? ""
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
            self.fullAccess = await client.mode().fullAccess
            self.idleMinutes = await client.idleTimeout()
            self.humanizerEnabled = await client.humanizerEnabled()
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
        localProviderConnected = object["enabled"] as? Bool ?? false
        localProviderActive = object["active"] as? Bool ?? false
        localProviderBaseURL = object["base_url"] as? String ?? localProviderBaseURL
        localProviderAPIPrefix = object["api_prefix"] as? String ?? localProviderAPIPrefix
        localProviderModelID = object["model_id"] as? String ?? localProviderModelID
        localProviderContextWindow = object["context_window"] as? Int ?? localProviderContextWindow
        let savedRoles = Set(object["roles"] as? [String] ?? [])
        localProviderSavedAssigned = localProviderConnected && savedRoles.contains("reasoning")
        localProviderStateUnknown = false
        localProviderRoles = localProviderConnected ? savedRoles : ["reasoning"]
        localProviderStatus = !localProviderConnected ? "Not connected"
            : localProviderSavedAssigned ? "Configured; use Test & Save to recheck"
            : "Select Use for reasoning, then Test & Save"
        return true
    }

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
        localProviderSaving = true
        localProviderStatus = "Checking the local inference app…"
        Task {
            await PendingConfigWrites.shared.begin()
            do {
                guard let url = URLComponents(string: origin), url.scheme == "http",
                      url.host == "127.0.0.1", let port = url.port,
                      (1024...65535).contains(port), port != 8000, port != 8765,
                      url.user == nil, url.password == nil,
                      url.path.isEmpty || url.path == "/",
                      url.query == nil, url.fragment == nil,
                      !model.isEmpty, localProviderRoles.contains("reasoning") else {
                    throw CloudSettingsError.message(
                        "Use an app on a distinct 127.0.0.1 port and select its exact model for Reasoning.")
                }
                let object = try await request("POST", path: "inference/local-provider", body: [
                    "base_url": origin, "api_prefix": prefix, "model_id": model,
                    "context_window": window, "roles": ["reasoning"],
                ])
                localProviderConnected = object["enabled"] as? Bool ?? false
                localProviderActive = object["active"] as? Bool ?? false
                localProviderSavedAssigned = localProviderConnected
                    && Set(object["roles"] as? [String] ?? []).contains("reasoning")
                localProviderStateUnknown = false
                localProviderRoles = localProviderSavedAssigned ? ["reasoning"] : []
                localProviderBaseURL = origin
                localProviderStatus = localProviderConnected
                    ? "Streaming reply verified" : "Connection was not saved"
                self.roles = (await client.models()).roles
                await refreshCloud()
            } catch {
                let failure = error.localizedDescription
                if error is CloudSettingsError {
                    localProviderStatus = failure
                } else if await refreshLocalProvider() {
                    self.roles = (await client.models()).roles
                    await refreshCloud()
                    let saved = localProviderConnected && localProviderSavedAssigned
                        && localProviderBaseURL == origin && localProviderAPIPrefix == prefix
                        && localProviderModelID == model && localProviderContextWindow == window
                    localProviderStatus = saved
                        ? "Connection saved; Wisp recovered after losing the reply."
                        : failure
                } else {
                    localProviderStateUnknown = true
                    localProviderStatus = "Wisp could not confirm whether the change was saved. Reopen Settings to refresh before retrying."
                }
            }
            localProviderSaving = false
            await PendingConfigWrites.shared.end()
        }
    }

    func disconnectLocalProvider() {
        localProviderSaving = true
        localProviderStatus = "Returning Reasoning to Wisp’s managed model…"
        Task {
            await PendingConfigWrites.shared.begin()
            do {
                let object = try await request("DELETE", path: "inference/local-provider")
                localProviderConnected = object["enabled"] as? Bool ?? false
                localProviderActive = object["active"] as? Bool ?? false
                localProviderSavedAssigned = false
                localProviderStateUnknown = false
                localProviderRoles = ["reasoning"]
                localProviderStatus = localProviderConnected
                    ? "The local app is still connected" : "Not connected"
                self.roles = (await client.models()).roles
            } catch {
                let failure = error.localizedDescription
                if error is CloudSettingsError {
                    localProviderStatus = failure
                } else if await refreshLocalProvider() {
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
        if enabled { next.insert(role) } else { next.remove(role) }
        cloudRoles = next
    }

    private func request(_ method: String, path: String,
                         body: [String: Any]? = nil) async throws -> [String: Any] {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 40
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw CloudSettingsError.message(object?["detail"] as? String
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
        } else {
            cloudPreset = "custom"
        }
        cloudStatus = cloudConnected
            ? "Connected to \(object["provider_label"] as? String ?? "cloud provider")"
            : "Local models only"
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
                savedCloudRoles = Set(object["roles"] as? [String] ?? [])
                cloudRoles = savedCloudRoles
                cloudStateUnknown = false
                savedCloudBaseURL = requestedBase
                savedCloudCredentialName = requestedCredentialName
                cloudStatus = "Connected to \(object["provider_label"] as? String ?? requestedPreset.label)"
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

    func setFullAccess(_ on: Bool) {
        fullAccess = on
        Task { self.fullAccess = await client.setFullAccess(on).fullAccess }
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
        }
    }
}

struct SettingsView: View {
    @StateObject private var loader = SettingsLoader()
    @ObservedObject private var sync = SyncProgress.shared
    // Keep the last-used settings pane so reopening Settings returns to the
    // user's context instead of a long, top-of-page disclosure list.
    @AppStorage("WispSettingsPane") private var selectedPane = "models"
    @AppStorage("WispModelsPane") private var modelsPane = "assignments"
    @State private var showModels = true
    @State private var showAutomation = true
    @State private var showAccess = true
    @State private var showAdvanced = true
    // Off by default — see BrowserHistoryReader's doc comment on why this
    // needs its own explicit opt-in rather than following Mail/Notes/Messages
    // (which sync as soon as their own TCC permission is granted).
    @AppStorage(BrowserHistoryReader.enabledKey) private var browserHistoryEnabled = false
    @AppStorage(ContactsReader.enabledKey) private var contactsEnabled = true
    @ObservedObject private var browserPrivacySync = BrowserHistoryReader.delivery
    @ObservedObject private var contactsPrivacySync = ContactsReader.delivery

    var body: some View {
        VStack(spacing: 0) {
            Picker("Settings pane", selection: $selectedPane) {
                Text("General").tag("general")
                Text("Models").tag("models")
                Text("Activity").tag("activity")
                Text("Privacy").tag("privacy")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 24)
            .padding(.vertical, 12)

            Divider()

            ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 16) {
                if selectedPane == "general" {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Personal memory").font(.title2.weight(.medium))
                        Text("Review facts, evidence, and possible connections.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Research Library") {
                        NSApp.sendAction(NSSelectorFromString("openResearchLibrary"),
                                         to: nil, from: nil)
                    }
                    Button("Open Memory") { MemoryWindow.shared.show() }
                }
                Divider()
                }
                if selectedPane == "models" {
                DisclosureGroup(isExpanded: $showModels) {
                    VStack(alignment: .leading, spacing: 14) {
                        Picker("Model settings", selection: $modelsPane) {
                            Text("Assignments").tag("assignments")
                            Text("Local").tag("local")
                            Text("Cloud").tag("cloud")
                        }
                        .pickerStyle(.segmented)

                        if modelsPane == "assignments" {
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
                                    .frame(width: 190)
                                }
                            }
                            if role != loader.roleOrder.last { Divider() }
                        }

                        }
                        if modelsPane == "local" {
                        GroupBox {
                            VStack(alignment: .leading, spacing: 12) {
                                HStack(alignment: .firstTextBaseline) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Local inference app")
                                            .font(.system(size: 15, weight: .semibold))
                                        Text("Connect Ling or another OpenAI-compatible app running on this Mac.")
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
                                Toggle("Use for reasoning", isOn: Binding(
                                    get: { loader.localProviderRoles.contains("reasoning") },
                                    set: { enabled in
                                        loader.localProviderRoles = enabled ? ["reasoning"] : []
                                    }))
                                    .toggleStyle(.checkbox)
                                    .disabled(loader.localProviderStateUnknown
                                              || loader.localProviderConnected
                                              && loader.localProviderSavedAssigned)
                                Text(loader.localProviderStateUnknown
                                     ? "Wisp cannot confirm the current Reasoning assignment. Use Refresh models before changing this connection."
                                     : loader.localProviderConnected && !loader.localProviderSavedAssigned
                                     ? "This app is connected but no longer assigned. Select Use for reasoning, then Test & Save to rebind it. Wisp does not automatically send earlier conversation summaries or remembered facts."
                                     : loader.localProviderConnected
                                     ? "Disconnect to stop using this app for reasoning. Wisp does not automatically send earlier conversation summaries or remembered facts to this app. Follow-ups may need context repeated."
                                     : "Reasoning prompts are sent to this app when Super Model is off. Wisp does not automatically send earlier conversation summaries or remembered facts. Routing, summary generation, and tool use stay with managed models. A loopback app without an API key is not identity-verified; connect only one you trust.")
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
                                    .disabled(loader.localProviderSaving || loader.localProviderStateUnknown
                                              || loader.localProviderModelID.isEmpty
                                              || !loader.localProviderRoles.contains("reasoning"))
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
                                          systemImage: loader.cloudStateUnknown
                                            ? "questionmark.circle" : loader.cloudConnected
                                            ? "checkmark.circle.fill" : "circle.dashed")
                                        .font(.caption)
                                        .foregroundStyle(loader.cloudConnected && !loader.cloudStateUnknown
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
                                    .labelsHidden().frame(width: 210)
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
                    .padding(.top, 8)
                } label: {
                    Text("Models").font(.title2.weight(.medium))
                }
                .tint(.secondary)
                }

                // Background/scheduled work — currently just the mail history
                // scan. Backed by SyncProgress.shared for the live bar, which
                // MailReader's batch loop updates directly (same process).
                if selectedPane == "activity" {
                DisclosureGroup(isExpanded: $showAutomation) {
                    VStack(alignment: .leading, spacing: 14) {
                        syncStatusRow(
                            title: "Mail history (up to a year)",
                            fraction: sync.mailHistoryFraction,
                            activeLabel: "Scanning…",
                            lastDate: sync.mailHistoryLastSynced)
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Automation").font(.title3.weight(.medium))
                }
                .tint(.secondary)
                }

                // What Wisp is allowed to do (Access) and which accounts it
                // reads from (Linked Accounts) — grouped as "Privacy & Access"
                // since both answer "what can Wisp touch", just at different
                // scopes (an action vs. a data source).
                if selectedPane == "privacy" {
                DisclosureGroup(isExpanded: $showAccess) {
                    VStack(alignment: .leading, spacing: 14) {
                        Toggle(isOn: Binding(
                            get: { loader.fullAccess },
                            set: { loader.setFullAccess($0) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Full access")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.fullAccess
                                     ? "Actions run automatically with no confirmation."
                                     : "Risky actions pause for your confirmation.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(.orange)

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
                        .tint(.orange)

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
                        .tint(.orange)

                        Divider()

                        // Wisp reads whatever Mail/Calendar accounts are
                        // already configured in Mail.app/Calendar.app — every
                        // linked account shows up automatically once granted,
                        // and Wisp already tells them apart (see the
                        // `account` param on summarize_emails/view_emails/
                        // get_upcoming/get_past_events). Adding an account
                        // itself is a macOS step, deliberately NOT something
                        // Wisp does for you: only the OS should ever hold
                        // your email password/OAuth token, never a
                        // third-party app. This just opens the right System
                        // Settings pane.
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Linked Accounts")
                                .font(.system(size: 14, weight: .medium))
                            Text("Wisp automatically reads every account already set up in "
                                 + "Mail.app and Calendar.app, and can tell them apart when "
                                 + "you ask about a specific one (e.g. \"what's in my work "
                                 + "email\"). To add another account, use macOS's own "
                                 + "Internet Accounts settings — Wisp never handles your "
                                 + "email password or sign-in directly.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Button("Open Internet Accounts Settings…") {
                                if let url = URL(string:
                                    "x-apple.systempreferences:com.apple.preferences.internetaccounts") {
                                    NSWorkspace.shared.open(url)
                                }
                            }
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Privacy & Access").font(.title3.weight(.medium))
                }
                .tint(.secondary)
                }

                // Occasional, technical knobs — memory management — that
                // most people set once and forget, as opposed to Models
                // (tuned often) or Automation (watched periodically).
                if selectedPane == "general" {
                DisclosureGroup(isExpanded: $showAdvanced) {
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
                        .tint(.orange)
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Advanced").font(.title3.weight(.medium))
                }
                .tint(.secondary)
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
            .padding(24)
            }
            .scrollIndicators(.visible)
        }
        .frame(width: 560, height: 620)
        .task { loader.refresh() }
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
