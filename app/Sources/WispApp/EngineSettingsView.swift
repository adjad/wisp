import AppKit
import CoreFoundation
import Foundation
import SwiftUI

private enum EngineMode: String {
    case bundled
    case externalOmlx = "external-omlx"

    var title: String {
        switch self {
        case .bundled: return "Built-in engine"
        case .externalOmlx: return "My installed oMLX"
        }
    }
}

private struct EngineSettingsResponse {
    let mode: EngineMode
    let configuredMode: EngineMode?
    let bundledAvailable: Bool
    let unsupportedReason: String?
    let running: Bool
    let omlxVersion: String?
    let adminURL: URL?
    let externalOmlxInstalled: Bool
    let restartRequired: Bool
    let externalEngineRunning: Bool

    init?(object: [String: Any]) {
        let requiredKeys: Set<String> = [
            "mode", "configured_mode", "bundled_available", "unsupported_reason",
            "running", "omlx_version", "admin_url", "external_omlx_installed",
            "restart_required",
        ]
        let allowedKeys = requiredKeys.union(["external_engine_running"])
        guard requiredKeys.isSubset(of: Set(object.keys)),
              Set(object.keys).isSubset(of: allowedKeys),
              let modeString = object["mode"] as? String,
              let mode = EngineMode(rawValue: modeString),
              let bundledAvailable = Self.boolean(object["bundled_available"]),
              let running = Self.boolean(object["running"]),
              let externalOmlxInstalled = Self.boolean(object["external_omlx_installed"]),
              let restartRequired = Self.boolean(object["restart_required"]),
              let unsupportedReason = Self.optionalString(object["unsupported_reason"]),
              let omlxVersion = Self.optionalString(object["omlx_version"]),
              let rawAdminURL = Self.optionalString(object["admin_url"]),
              let adminURL = Self.validatedAdminURL(rawAdminURL),
              let externalEngineRunning = object["external_engine_running"] == nil
                ? false : Self.boolean(object["external_engine_running"]) else {
            return nil
        }

        let configuredMode: EngineMode?
        if let value = object["configured_mode"] as? String {
            guard let decoded = EngineMode(rawValue: value) else { return nil }
            configuredMode = decoded
        } else if object["configured_mode"] is NSNull {
            configuredMode = nil
        } else {
            return nil
        }

        self.mode = mode
        self.configuredMode = configuredMode
        self.bundledAvailable = bundledAvailable
        self.unsupportedReason = unsupportedReason
        self.running = running
        self.omlxVersion = omlxVersion
        self.adminURL = adminURL
        self.externalOmlxInstalled = externalOmlxInstalled
        self.restartRequired = restartRequired
        self.externalEngineRunning = externalEngineRunning
    }

    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    private static func optionalString(_ value: Any?) -> String?? {
        guard let value else { return nil }
        if value is NSNull { return .some(nil) }
        guard let string = value as? String else { return nil }
        return .some(string)
    }

    private static func validatedAdminURL(_ value: String?) -> URL?? {
        guard let value else { return .some(nil) }
        guard let components = URLComponents(string: value),
              components.scheme == "http", components.host == "127.0.0.1",
              let port = components.port, (1...65_535).contains(port),
              components.user == nil, components.password == nil,
              components.path == "/admin" || components.path == "/admin/",
              components.query == nil, components.fragment == nil,
              let url = components.url else { return nil }
        return .some(url)
    }
}

@MainActor
private final class EngineSettingsLoader: ObservableObject {
    @Published private(set) var engine: EngineSettingsResponse?
    @Published private(set) var isLoading = false
    @Published private(set) var isSaving = false
    @Published private(set) var statusMessage: String?

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            engine = try await request("GET", path: "engine")
            statusMessage = nil
        } catch {
            statusMessage = "Wisp couldn't load the engine settings. Try again later."
        }
    }

    func setMode(_ mode: EngineMode) async {
        guard let engine,
              !isSaving,
              mode != (engine.configuredMode ?? engine.mode),
              mode != .bundled || engine.bundledAvailable else { return }

        isSaving = true
        defer { isSaving = false }
        do {
            self.engine = try await request("POST", path: "engine/mode",
                                            body: ["mode": mode.rawValue])
            statusMessage = nil
        } catch {
            statusMessage = "Wisp couldn't save the engine setting. Try again."
        }
    }

    private func request(_ method: String, path: String,
                         body: [String: String]? = nil) async throws -> EngineSettingsResponse {
        guard let backendURL = Self.loopbackBackendURL() else {
            throw URLError(.badURL)
        }
        let url = backendURL.appendingPathComponent(path)

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode),
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let engine = EngineSettingsResponse(object: object) else {
            // Error bodies are deliberately not decoded, displayed, or logged.
            throw URLError(.badServerResponse)
        }
        return engine
    }

    private static func loopbackBackendURL() -> URL? {
        let url = WispClient.baseURL
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme == "http", components.host == "127.0.0.1",
              let port = components.port, (1...65_535).contains(port),
              components.user == nil, components.password == nil,
              components.path.isEmpty || components.path == "/",
              components.query == nil, components.fragment == nil else { return nil }
        return url
    }
}

struct EngineSettingsView: View {
    @StateObject private var loader = EngineSettingsLoader()

    private var selectedMode: Binding<EngineMode> {
        Binding(
            get: { loader.engine?.configuredMode ?? loader.engine?.mode ?? .bundled },
            set: { mode in Task { await loader.setMode(mode) } })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let engine = loader.engine {
                HStack {
                    Label("Running on \(engine.mode.title)",
                          systemImage: engine.running ? "checkmark.circle.fill" : "circle.dashed")
                        .font(.callout)
                        .foregroundStyle(engine.running ? Color.primary : Color.secondary)
                    Spacer()
                    Menu("Change engine") {
                        Button("Built-in engine") { selectedMode.wrappedValue = .bundled }
                            .disabled(!engine.bundledAvailable)
                        Button("My installed oMLX") { selectedMode.wrappedValue = .externalOmlx }
                        Divider()
                        if engine.mode == .bundled {
                            Button("Set up engine and models…") {
                                NotificationCenter.default.post(name: .wispOpenSetupGuide, object: nil)
                            }
                        } else if let url = engine.adminURL {
                            Button("Open engine dashboard") { NSWorkspace.shared.open(url) }
                        }
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                    .disabled(loader.isSaving)
                }

                if !engine.bundledAvailable {
                    Label(engine.unsupportedReason ?? "The built-in engine isn't available on this Mac.",
                          systemImage: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if engine.restartRequired {
                    Label("Restart Wisp to apply", systemImage: "arrow.clockwise")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if engine.mode == .bundled && engine.externalEngineRunning {
                    Label("Your own oMLX is also running; both use memory",
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if loader.isLoading {
                ProgressView("Loading engine settings…")
                    .controlSize(.small)
            } else {
                Text("Engine settings are unavailable.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let message = loader.statusMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { await loader.refresh() }
    }
}
