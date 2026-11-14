import Foundation
import SwiftUI

/// The Intel build has no local engine, so its first run asks for one OpenRouter
/// key and connects all of Wisp to a preset model. The values below are what the
/// Settings > Models > Cloud… form would hold for the same connection.
enum IntelCloudPreset {
    static let provider = "openrouter"
    static let baseURL = "https://openrouter.ai"
    static let apiPrefix = "/api/v1"
    static let contextWindow = 32_000
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
}

/// The name shown for the cloud model Wisp answers with (/setup/status `remote_model`),
/// so Ask, Brief and setup say which model it is ("Gemini 3.8 Flash"). Pure.
enum CloudModelName {
    /// "Gemini 3.8 Flash" for a listed model, else the id's last part ("my-model").
    static func model(_ id: String) -> String {
        if let known = IntelCloudPreset.models.first(where: { $0.id == id }) {
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

/// Stores the key, connects every role to OpenRouter through POST /inference/cloud
/// and restarts the backend: the Cloud form's path, with one field.
@MainActor
final class IntelKeyConnector: ObservableObject {
    @Published private(set) var connecting = false
    @Published private(set) var connected = false
    @Published private(set) var error: String?

    /// The key is passed in and never kept: callers clear their own copy afterwards.
    func connect(key rawKey: String, modelID: String) async {
        guard !connecting else { return }
        let key = rawKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard IntelCloudPreset.isOffered(model: modelID) else {
            error = "Choose one of the listed models."
            return
        }
        guard !key.isEmpty else {
            error = "Enter your OpenRouter API key."
            return
        }
        connecting = true
        error = nil
        await PendingConfigWrites.shared.begin()
        let name = CloudConnection.newCredentialName()
        var stored = false
        do {
            try CloudCredentialStore.save(key, name: name, baseURL: IntelCloudPreset.baseURL)
            stored = true
            let body = CloudConnection.requestBody(
                provider: IntelCloudPreset.provider, baseURL: IntelCloudPreset.baseURL,
                apiPrefix: IntelCloudPreset.apiPrefix, modelID: modelID,
                contextWindow: IntelCloudPreset.contextWindow, roles: IntelCloudPreset.roles,
                superModelEnabled: false, credentialName: name)
            let object = try await Self.send("POST", "inference/cloud", body: body)
            guard SettingsResponseValidator.valid(object, path: "inference/cloud"),
                  object["enabled"] as? Bool == true, object["model_id"] as? String == modelID,
                  object["credential_name"] as? String == name else {
                throw IntelConnectError.message("Wisp could not confirm the connection. Try again.")
            }
            NotificationCenter.default.post(name: .wispInferenceConfigurationChanged, object: nil)
            connected = true
            NotificationCenter.default.post(name: .wispRestartBackend, object: nil)
        } catch {
            self.error = error.localizedDescription
            if stored { await removeUnlessReferenced(name) }
        }
        connecting = false
        await PendingConfigWrites.shared.end()
    }

    /// A failed connect leaves no unused key behind, unless the saved configuration
    /// already points at it (or that can't be read): then the key stays.
    private func removeUnlessReferenced(_ name: String) async {
        guard let current = try? await Self.send("GET", "inference/cloud", body: nil),
              current["credential_name"] as? String != name else { return }
        try? CloudCredentialStore.remove(name: name, baseURL: IntelCloudPreset.baseURL)
    }

    private static func send(_ method: String, _ path: String, body: [String: Any]?) async throws -> [String: Any] {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        // Room for macOS's one-time login-password question about the saved key, then
        // the one test reply (Gemini answers all at once).
        request.timeoutInterval = 180
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { request.httpBody = try JSONSerialization.data(withJSONObject: body) }
        let (data, response) = try await URLSession.shared.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard let http = response as? HTTPURLResponse else {
            throw IntelConnectError.message("Wisp returned an incomplete answer. Try again.")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw IntelConnectError.message(object?["detail"] as? String
                                            ?? "Wisp could not connect to OpenRouter.")
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

/// First-run model step on a build with no local engine: one key field, a short
/// model list, Connect. It replaces the "can't set this up" card.
struct IntelKeyCard: View {
    @ObservedObject var setup: SetupGuideModel
    @StateObject private var connector = IntelKeyConnector()
    @State private var key = ""
    @State private var modelID = IntelCloudPreset.modelID

    static let disclosure = IntelCloudPreset.disclosure

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 12) {
                Image(systemName: "key").font(.system(size: 15))
                    .foregroundStyle(OnboardingPalette.lilac)
                    .frame(width: 30, height: 30)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Connect Wisp to OpenRouter").font(.system(size: 13, weight: .medium))
                    Text(Self.disclosure).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            SecureField("OpenRouter API key", text: $key)
                .textFieldStyle(.roundedBorder)
                .disabled(connector.connecting || connector.connected)
                .accessibilityLabel("OpenRouter API key")
                .onSubmit { connect() }
            Picker("Model", selection: $modelID) {
                ForEach(IntelCloudPreset.models, id: \.id) { Text($0.label).tag($0.id) }
            }
            .disabled(connector.connecting || connector.connected)
            HStack(spacing: 8) {
                if connector.connecting {
                    ProgressView().controlSize(.small)
                    Text("Testing the key and model… If macOS asks for your login password, choose Always Allow.")
                        .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                } else if connector.connected {
                    ProgressView().controlSize(.small)
                    Text("Connected. Starting Wisp on OpenRouter…").font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                } else if let error = connector.error {
                    Text(error).font(.system(size: 12)).foregroundStyle(Color.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Button("Connect") { connect() }
                    .buttonStyle(FirstRunButtonStyle(primary: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(connector.connecting || connector.connected
                              || key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
        .padding(.bottom, 9)
        .onDisappear { key = "" }
    }

    private func connect() {
        let entered = key
        let model = modelID
        Task {
            await connector.connect(key: entered, modelID: model)
            // The key is in the Keychain now (or the attempt failed and the field keeps
            // nothing worth re-sending): clear it either way.
            if connector.connected { key = "" }
            // The backend restarts; look again until it answers on OpenRouter.
            var tries = 0
            while connector.connected, setup.status?.canAnswer != true, tries < 40 {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await setup.refresh(quiet: true)
                tries += 1
            }
        }
    }
}
