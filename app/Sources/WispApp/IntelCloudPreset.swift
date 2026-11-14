import Combine
import Foundation
import Security
import SwiftUI

/// The Intel build has no local engine, so its first run asks for one cloud key
/// (OpenRouter by default, or the Gemini API: CloudProvider) and connects all of Wisp
/// to a preset model. The values below are what the Settings > Models > Cloud… form
/// would hold for the same connection.
enum IntelCloudPreset {
    static let provider = "openrouter"
    static let baseURL = "https://openrouter.ai"
    static let apiPrefix = "/api/v1"
    /// What Wisp tells the backend each model can hold: one million tokens, so whole
    /// email threads, the mail history and long documents fit. Every listed model takes
    /// at least that on OpenRouter (checked 2026-11-14: 1,000,000 to 1,050,000). An
    /// unlisted model gets the old 32k.
    static let contextWindow = 32_000
    static let contextWindows: [String: Int] = [
        "google/gemini-3.8-flash": 1_000_000, "google/gemini-4-carbon": 1_000_000, "google/gemini-4-argon": 1_000_000,
        "anthropic/claude-haiku-5.5": 1_000_000, "anthropic/claude-sonnet-5.5": 1_000_000, "openai/gpt-6-luna": 1_000_000,
        // The same Gemini models on the Gemini API, by their bare ids.
        "gemini-3.8-flash": 1_000_000, "gemini-4-carbon": 1_000_000, "gemini-4-argon": 1_000_000,
    ]
    static func contextWindow(for modelID: String) -> Int { contextWindows[modelID] ?? contextWindow }
    static let roles = ["everything"]
    /// The default model: it takes text, pictures and audio, so Ask with pictures,
    /// look-at-screen, voice and Meet all work. Its inputs are listed in the backend's
    /// REMOTE_INPUT_MODALITIES (service/inference/remote_policy.py).
    static let modelID = "google/gemini-3.8-flash"
    /// The short list offered beside the key, default first.
    static let models: [(id: String, label: String)] = [
        (modelID, "Gemini 3.8 Flash (default)"),
        ("anthropic/claude-haiku-5.5", "Claude Haiku 5.5"),
        ("openai/gpt-6-luna", "GPT-6 Luna"),
        ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"),
        ("google/gemini-4-carbon", "Gemini 4 Carbon"),
        ("google/gemini-4-argon", "Gemini 4 Argon"),
    ]
    /// What the key card says goes off this Mac.
    static let disclosure = "In this build Wisp's AI runs on OpenRouter. What you ask, the mail, texts, calendar and notes Wisp reads to answer, and any screenshots, pictures and recorded audio (voice questions and meetings) are sent to OpenRouter and the model you choose."
    /// Mirrors the backend's model-id rule (service/inference/remote_policy.py `_MODEL_ID`).
    static let backendModelIDPattern = "[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}"

    /// This binary was built for an Intel Mac.
    static var compiledForIntel: Bool {
        #if arch(x86_64)
        return true
        #else
        return false
        #endif
    }

    /// The key-only first run: only an Intel build, and only when /setup/status says
    /// the bundled engine is not available. An engine that is available or unknown
    /// (no answer yet) never offers it.
    static func offersKeyOnlySetup(compiledForIntel: Bool, engineAvailable: Bool?) -> Bool {
        compiledForIntel && engineAvailable == false
    }

    static func offersKeyOnlySetup(status: SetupStatus?) -> Bool {
        offersKeyOnlySetup(compiledForIntel: compiledForIntel,
                           engineAvailable: status?.bundledEngine?.available)
    }

    static func isOffered(model id: String) -> Bool { models.contains { $0.id == id } }

    /// The Gemini API (a Google AI Studio key), Wisp's second cloud provider. It speaks
    /// the OpenAI format under /v1beta/openai and names models by bare ids.
    static let geminiProvider = "gemini"
    static let geminiBaseURL = "https://generativelanguage.googleapis.com"
    static let geminiAPIPrefix = "/v1beta/openai"
    static let geminiModelID = "gemini-3.8-flash"
    static let geminiModels: [(id: String, label: String)] = [
        (geminiModelID, "Gemini 3.8 Flash (default)"),
        ("gemini-4-carbon", "Gemini 4 Carbon"),
        ("gemini-4-argon", "Gemini 4 Argon"),
    ]
    static let geminiDisclosure = "In this build Wisp's AI runs on the Gemini API. What you ask, the mail, texts, calendar and notes Wisp reads to answer, and any screenshots, pictures and recorded audio (voice questions and meetings) are sent to Google. On Google's free tier, Google may use them to improve its products."
}

/// Where all of Wisp runs on the Intel build: OpenRouter (the default) or the Gemini
/// API. Each case holds what the key card, the notch's switch and POST /inference/cloud
/// need. OpenRouter's values are IntelCloudPreset's own.
enum CloudProvider: String, CaseIterable, Identifiable, Equatable {
    case openRouter = "openrouter"
    case gemini = "gemini"

    var id: String { rawValue }

    /// The backend's provider id ("openrouter", "gemini").
    var provider: String { rawValue }

    var label: String {
        switch self {
        case .openRouter: return "OpenRouter"
        case .gemini: return "Gemini API"
        }
    }

    var baseURL: String {
        switch self {
        case .openRouter: return IntelCloudPreset.baseURL
        case .gemini: return IntelCloudPreset.geminiBaseURL
        }
    }

    var apiPrefix: String {
        switch self {
        case .openRouter: return IntelCloudPreset.apiPrefix
        case .gemini: return IntelCloudPreset.geminiAPIPrefix
        }
    }

    /// The short model list, default first.
    var models: [(id: String, label: String)] {
        switch self {
        case .openRouter: return IntelCloudPreset.models
        case .gemini: return IntelCloudPreset.geminiModels
        }
    }

    var defaultModelID: String { models[0].id }

    func offers(model id: String) -> Bool { models.contains { $0.id == id } }

    var keyPlaceholder: String {
        switch self {
        case .openRouter: return "OpenRouter API key"
        case .gemini: return "Gemini API key"
        }
    }

    /// Where to get a key, as shown ("openrouter.ai/keys").
    var keySource: String {
        switch self {
        case .openRouter: return "openrouter.ai/keys"
        case .gemini: return "aistudio.google.com/apikey"
        }
    }

    var keyURL: URL { URL(string: "https://" + keySource)! }

    /// What the key card says goes off this Mac, and to whom.
    var disclosure: String {
        switch self {
        case .openRouter: return IntelCloudPreset.disclosure
        case .gemini: return IntelCloudPreset.geminiDisclosure
        }
    }

    /// The provider a saved connection (GET /inference/cloud) is on, or nil for any
    /// other. Settings' older Gemini preset saved "openai-compatible" at Gemini's URL.
    static func saved(provider: String?, baseURL: String?) -> CloudProvider? {
        let base = CloudCredentialStore.normalizedBaseURL(baseURL ?? "")
        switch provider {
        case "openrouter"?: return .openRouter
        case "gemini"?: return .gemini
        case "openai-compatible"? where base == IntelCloudPreset.geminiBaseURL: return .gemini
        default: return nil
        }
    }
}

/// The key card's and the notch switch's choice: which provider, the typed key and
/// the model. Switching provider clears the key and resets the model. Pure.
struct CloudKeyChoice: Equatable {
    private(set) var provider: CloudProvider = .openRouter
    var key = ""
    var modelID = CloudProvider.openRouter.defaultModelID

    init(provider: CloudProvider = .openRouter) {
        self.provider = provider
        modelID = provider.defaultModelID
    }

    mutating func choose(_ next: CloudProvider) {
        guard next != provider else { return }
        provider = next
        key = ""
        modelID = next.defaultModelID
    }

    var hasKey: Bool { !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
}

extension CloudCredentialStore {
    /// Keychain names of keys saved earlier for `baseURL`, newest first. It reads
    /// only item names, never a key, so macOS asks nothing.
    static func savedNames(baseURL: String) -> [String] {
        #if WISP_SETTINGS_QA
        return []
        #else
        let suffix = account(name: "", baseURL: baseURL)  // ":<sha256 of the origin>"
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnAttributes as String: true,
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let items = result as? [[String: Any]] else { return [] }
        return items.compactMap { item -> (String, Date)? in
            guard let account = item[kSecAttrAccount as String] as? String, account.hasSuffix(suffix) else { return nil }
            let name = String(account.dropLast(suffix.count))
            guard name.range(of: "^[A-Za-z][A-Za-z0-9_-]{0,63}$", options: .regularExpression) != nil else { return nil }
            return (name, item[kSecAttrModificationDate as String] as? Date ?? .distantPast)
        }
        .sorted { $0.1 > $1.1 }
        .map(\.0)
        #endif
    }
}

/// The name shown for the cloud model Wisp answers with (/setup/status `remote_model`),
/// so Ask, Brief and setup say which model it is ("Gemini 3.8 Flash"). Pure.
enum CloudModelName {
    /// "Gemini 3.8 Flash" for a listed model, else the id's last part ("my-model").
    static func model(_ id: String) -> String {
        if let known = CloudProvider.allCases.flatMap(\.models).first(where: { $0.id == id }) {
            return known.label.replacingOccurrences(of: " (default)", with: "")
        }
        // An unlisted id: its last part without an ":tag", kept short for the notch.
        let last = id.split(separator: "/").last.map(String.init) ?? id
        let bare = last.split(separator: ":").first.map(String.init) ?? last
        return bare.count > 24 ? String(bare.prefix(23)) + "…" : bare
    }

    /// The name from the status field; nil when absent or malformed.
    static func display(_ field: Any?) -> String? {
        guard let object = field as? [String: Any], let id = object["id"] as? String,
              !id.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return model(id)
    }
}

/// Stores the key, connects every role to the chosen provider through POST
/// /inference/cloud and restarts the backend: the Cloud form's path, with one field.
@MainActor
final class IntelKeyConnector: ObservableObject {
    @Published private(set) var connecting = false
    @Published private(set) var connected = false
    @Published private(set) var error: String?
    /// The validated reply of the last connect that worked: the saved connection.
    private(set) var reply: [String: Any]?
    /// Every Connect's POST timeout (see send).
    nonisolated static let connectTimeout: TimeInterval = 200

    /// The key is passed in and never kept: callers clear their own copy afterwards.
    func connect(key rawKey: String, modelID: String, provider: CloudProvider = .openRouter) async {
        guard !connecting else { return }
        let key = rawKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard provider.offers(model: modelID) else {
            error = "Choose one of the listed models."
            return
        }
        guard !key.isEmpty else {
            error = "Enter your \(provider.keyPlaceholder)."
            return
        }
        await run(provider: provider, modelID: modelID, newKey: key, savedName: nil)
    }

    /// Connects with a key saved earlier for this provider (CloudCredentialStore.savedNames).
    /// A failed attempt leaves that key where it was.
    func connect(savedKey name: String, modelID: String, provider: CloudProvider) async {
        guard !connecting else { return }
        guard provider.offers(model: modelID) else {
            error = "Choose one of the listed models."
            return
        }
        await run(provider: provider, modelID: modelID, newKey: nil, savedName: name)
    }

    /// Back to the start, for another attempt; never while one runs.
    func reset() {
        guard !connecting else { return }
        connected = false
        error = nil
        reply = nil
    }

    private func run(provider: CloudProvider, modelID: String, newKey: String?, savedName: String?) async {
        connecting = true
        error = nil
        await PendingConfigWrites.shared.begin()
        let name = savedName ?? CloudConnection.newCredentialName()
        var stored = false
        do {
            if let newKey {
                try CloudCredentialStore.save(newKey, name: name, baseURL: provider.baseURL)
                stored = true
            }
            let body = CloudConnection.requestBody(
                provider: provider.provider, baseURL: provider.baseURL,
                apiPrefix: provider.apiPrefix, modelID: modelID,
                contextWindow: IntelCloudPreset.contextWindow(for: modelID), roles: IntelCloudPreset.roles,
                superModelEnabled: false, credentialName: name)
            let object = try await Self.send("POST", "inference/cloud", body: body, provider: provider)
            guard SettingsResponseValidator.valid(object, path: "inference/cloud"),
                  object["enabled"] as? Bool == true, object["model_id"] as? String == modelID,
                  object["credential_name"] as? String == name,
                  CloudProvider.saved(provider: object["provider"] as? String,
                                      baseURL: object["base_url"] as? String) == provider else {
                throw IntelConnectError.message("Wisp could not confirm the connection. Try again.")
            }
            reply = object
            NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
            connected = true
            NotificationCenter.default.post(name: .wispRestartBackend, object: nil)
        } catch {
            self.error = error.localizedDescription
            if stored { await removeUnlessReferenced(name, baseURL: provider.baseURL) }
        }
        connecting = false
        await PendingConfigWrites.shared.end()
    }

    /// The saved connection (GET /inference/cloud), validated; nil when it can't be read.
    static func savedConnection() async -> [String: Any]? {
        guard let object = try? await send("GET", "inference/cloud", body: nil, timeout: 15),
              SettingsResponseValidator.valid(object, path: "inference/cloud") else { return nil }
        return object
    }

    /// A failed connect leaves no unused key behind, unless the saved configuration
    /// already points at it (or that can't be read): then the key stays.
    private func removeUnlessReferenced(_ name: String, baseURL: String) async {
        guard let current = try? await Self.send("GET", "inference/cloud", body: nil, timeout: 15),
              current["credential_name"] as? String != name else { return }
        try? CloudCredentialStore.remove(name: name, baseURL: baseURL)
    }

    /// The default timeout leaves room for the server's lock wait, macOS's one-time
    /// login-password question about the saved key and the test replies. It must stay
    /// above the server's own Connect time: a timeout here deletes a key the server
    /// may then save. Every Connect (key card, notch switch) uses it.
    private static func send(_ method: String, _ path: String, body: [String: Any]?,
                             provider: CloudProvider? = nil,
                             timeout: TimeInterval = connectTimeout) async throws -> [String: Any] {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse else {
            throw IntelConnectError.message("Wisp returned an incomplete answer. Try again.")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw IntelConnectError.message(object?["detail"] as? String
                                            ?? "Wisp could not connect to \((provider ?? .openRouter).label).")
        }
        guard let object, !object.isEmpty else {
            throw IntelConnectError.message("Wisp returned an incomplete answer. Try again.")
        }
        return object
    }
}

private enum IntelConnectError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case let .message(value): return value }
    }
}

/// "Get a key at aistudio.google.com/apikey", the address a link.
struct CloudKeyHint: View {
    let provider: CloudProvider
    var font: Font = .system(size: 12)

    var body: some View {
        HStack(spacing: 4) {
            Text("Get a key at").foregroundStyle(Theme.textMuted)
            Link(provider.keySource, destination: provider.keyURL)
        }
        .font(font)
        .accessibilityElement(children: .combine)
    }
}

/// The notch's Advanced › Model provider: where all of Wisp runs now (GET
/// /inference/cloud) and a switch to the other provider through the key card's
/// connect path (save the key, POST, restart).
@MainActor
final class CloudProviderSwitch: ObservableObject {
    /// nil while checking, false when the saved connection couldn't be read.
    @Published private(set) var checked: Bool?
    @Published private(set) var connected = false
    /// The saved connection's provider; nil when not connected or on another provider.
    @Published private(set) var current: CloudProvider?
    @Published private(set) var currentModelID = ""
    /// The provider being switched to, its form open.
    @Published private(set) var choosing: CloudProvider?
    @Published var choice = CloudKeyChoice()
    /// A key saved earlier for the provider being switched to.
    @Published private(set) var savedKeyName: String?
    /// The last switch worked; Wisp is restarting on it.
    @Published private(set) var switched = false
    let connector = IntelKeyConnector()
    private var forward: AnyCancellable?

    init() {
        forward = connector.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }

    var busy: Bool { connector.connecting }

    func refresh() async {
        guard !busy, choosing == nil else { return }
        guard let object = await IntelKeyConnector.savedConnection() else {
            if checked == nil { checked = false }
            return
        }
        apply(object)
    }

    private func apply(_ object: [String: Any]) {
        checked = true
        connected = object["enabled"] as? Bool == true
        current = connected ? CloudProvider.saved(provider: object["provider"] as? String,
                                                  baseURL: object["base_url"] as? String) : nil
        currentModelID = connected ? (object["model_id"] as? String ?? "") : ""
    }

    /// A provider chip: the other one opens its form; the current one closes it.
    func open(_ provider: CloudProvider) {
        guard !busy else { return }
        if connected, provider == current {
            cancel()
            return
        }
        guard choosing != provider else { return }
        choosing = provider
        choice = CloudKeyChoice(provider: provider)
        connector.reset()
        switched = false
        savedKeyName = CloudCredentialStore.savedNames(baseURL: provider.baseURL).first
    }

    func cancel() {
        guard !busy else { return }
        choosing = nil
        choice.key = ""
        savedKeyName = nil
        connector.reset()
    }

    func connect(useSavedKey: Bool) async {
        guard let provider = choosing, !busy else { return }
        let model = choice.modelID
        if useSavedKey, let name = savedKeyName {
            await connector.connect(savedKey: name, modelID: model, provider: provider)
        } else {
            let key = choice.key
            await connector.connect(key: key, modelID: model, provider: provider)
        }
        guard connector.connected else { return }
        choice.key = ""
        if let reply = connector.reply { apply(reply) }
        choosing = nil
        savedKeyName = nil
        switched = true
    }
}

/// First-run model step on a build with no local engine: OpenRouter or the Gemini
/// API, one key field, a short model list, Connect. It replaces the "can't set this
/// up" card.
struct IntelKeyCard: View {
    @ObservedObject var setup: SetupGuideModel
    @StateObject private var connector = IntelKeyConnector()
    @State private var choice = CloudKeyChoice()

    static let disclosure = IntelCloudPreset.disclosure

    private var locked: Bool { connector.connecting || connector.connected }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Image(systemName: "key").font(.system(size: 15))
                    .foregroundStyle(OnboardingPalette.lilac)
                    .frame(width: 30, height: 30)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Connect Wisp to \(choice.provider.label)").font(.system(size: 13, weight: .medium))
                    Text(choice.provider.disclosure).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Picker("Provider", selection: Binding(get: { choice.provider }, set: { choice.choose($0) })) {
                ForEach(CloudProvider.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityLabel("Provider")
            .disabled(locked)
            SecureField(choice.provider.keyPlaceholder, text: $choice.key)
                .textFieldStyle(.roundedBorder)
                .disabled(locked)
                .accessibilityLabel(choice.provider.keyPlaceholder)
                .onSubmit { connect() }
            CloudKeyHint(provider: choice.provider)
            Picker("Model", selection: $choice.modelID) {
                ForEach(choice.provider.models, id: \.id) { Text($0.label).tag($0.id) }
            }
            .disabled(locked)
            HStack(spacing: 8) {
                if connector.connecting {
                    ProgressView().controlSize(.small)
                    Text("Testing the key and model… If macOS asks for your login password, choose Always Allow.")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                } else if connector.connected {
                    ProgressView().controlSize(.small)
                    Text("Connected. Starting Wisp on \(choice.provider.label)…")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                } else if let error = connector.error {
                    Text(error).font(.system(size: 12)).foregroundStyle(Color.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Button("Connect") { connect() }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(locked || !choice.hasKey)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
        .padding(.bottom, 9)
        .onDisappear { choice.key = "" }
    }

    private func connect() {
        let entered = choice.key
        let model = choice.modelID
        let provider = choice.provider
        Task {
            await connector.connect(key: entered, modelID: model, provider: provider)
            // The key is in the Keychain now (or the attempt failed and the field keeps
            // nothing worth re-sending): clear it either way.
            if connector.connected { choice.key = "" }
            // The backend restarts; look again until it answers on the new provider.
            var tries = 0
            while connector.connected, setup.status?.canAnswer != true, tries < 40 {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await setup.refresh(quiet: true)
                tries += 1
            }
        }
    }
}


/// Advanced › Model provider's text. Here, not in QuickSettingsText.swift, because it needs
/// `CloudProvider` and `CloudModelName`, and that file is also compiled on its own for its checks.
extension QuickSettingsText {
    /// Advanced › Model provider's line: where all of Wisp runs now. `checked` is nil
    /// while checking and false when the saved connection couldn't be read.
    static func modelProvider(checked: Bool?, connected: Bool, provider: CloudProvider?, modelID: String) -> String {
        guard let checked else { return "Checking…" }
        guard checked else { return "Wisp couldn't check this yet." }
        guard connected else { return "Not connected" }
        let model = modelID.isEmpty ? nil : CloudModelName.model(modelID)
        let name = provider?.label ?? "Another provider"
        return model.map { "\(name) · \($0)" } ?? name
    }

    /// Shown once a switch works, until Ask and Brief catch up.
    static func modelProviderSwitched(_ provider: CloudProvider?) -> String {
        "Switched. Wisp restarts on \(provider?.label ?? "the new provider"); Ask and Brief show the new model in a moment."
    }
}
