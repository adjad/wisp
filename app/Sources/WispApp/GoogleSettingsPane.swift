import AppKit
import SwiftUI

/// Settings ▸ Google: connect Wisp to Gmail (read-only) so mail syncs without
/// Mail.app open. All OAuth work happens in the local backend (gmail_source.py);
/// this pane only sends the user's own OAuth client id/secret to loopback,
/// opens the consent URL in the default browser, and shows connection status.
@MainActor
final class GoogleAccountModel: ObservableObject {
    @Published var connected = false
    @Published var email = ""
    @Published var authorizing = false
    @Published var lastError = ""
    @Published var lastSync: Date?
    @Published var message = ""
    @Published var clientID = ""
    @Published var clientSecret = ""
    @Published var hasSavedSecret = false

    // Google Workspace MCP server (Gmail, Calendar, Drive as tools)
    @Published var wsConfigured = false
    @Published var wsRunning = false
    @Published var wsTools = 0
    @Published var wsError = ""
    @Published var wsPreparing = false
    @Published var wsNeedsUV = false
    @Published var wsMessage = ""
    @Published var googleEmail = ""

    private var polling: Task<Void, Never>?
    private var wsPolling: Task<Void, Never>?

    func refresh() async {
        guard let s = await request("GET", "assistant/gmail/status") else {
            message = "Wisp's service isn't responding."
            return
        }
        connected = s["connected"] as? Bool ?? false
        email = s["email"] as? String ?? ""
        authorizing = s["authorizing"] as? Bool ?? false
        lastError = s["last_error"] as? String ?? ""
        hasSavedSecret = s["has_secret"] as? Bool ?? false
        if clientID.isEmpty { clientID = s["client_id"] as? String ?? "" }
        if let t = s["last_headers_sync"] as? Double { lastSync = Date(timeIntervalSince1970: t) }
    }

    func refreshWorkspace() async {
        guard let s = await request("GET", "assistant/google/workspace/status") else { return }
        wsConfigured = s["configured"] as? Bool ?? false
        wsRunning = s["running"] as? Bool ?? false
        wsTools = s["tools"] as? Int ?? 0
        wsError = s["error"] as? String ?? ""
        wsNeedsUV = !(s["uvx"] as? Bool ?? true)
        let prep = s["prepare"] as? [String: Any]
        wsPreparing = (prep?["state"] as? String) == "preparing"
        if (prep?["state"] as? String) == "failed" { wsError = prep?["detail"] as? String ?? wsError }
        if clientID.isEmpty { clientID = s["client_id"] as? String ?? "" }
        if googleEmail.isEmpty { googleEmail = s["email"] as? String ?? "" }
        if let has = s["has_secret"] as? Bool, has { hasSavedSecret = true }
    }

    func enableWorkspace() {
        let id = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty else { wsMessage = "Enter your OAuth client ID first."; return }
        wsMessage = ""
        Task {
            let body: [String: Any] = ["client_id": id,
                                       "client_secret": clientSecret.trimmingCharacters(in: .whitespacesAndNewlines),
                                       "email": googleEmail.trimmingCharacters(in: .whitespacesAndNewlines)]
            guard let r = await request("POST", "assistant/google/workspace/enable", body: body,
                                        timeout: 20) else {
                wsMessage = "Wisp's service isn't responding."
                return
            }
            if r["ok"] as? Bool != true {
                wsMessage = r["error"] as? String ?? "Couldn't set up the Google Workspace server."
                return
            }
            clientSecret = ""
            wsMessage = "Setting up the Google Workspace server — the first run downloads it."
            wsPreparing = true
            watchWorkspace()
        }
    }

    func disableWorkspace() {
        Task {
            _ = await request("POST", "assistant/google/workspace/disable", timeout: 30)
            wsMessage = "Google Workspace removed."
            await refreshWorkspace()
        }
    }

    func signInWorkspace() {
        Task {
            let body: [String: Any] = ["email": googleEmail.trimmingCharacters(in: .whitespacesAndNewlines)]
            guard let r = await request("POST", "assistant/google/workspace/signin", body: body,
                                        timeout: 60) else {
                wsMessage = "Wisp's service isn't responding."
                return
            }
            if r["ok"] as? Bool == true, let s = r["auth_url"] as? String,
               let url = URL(string: s), url.scheme == "https", url.host == "accounts.google.com" {
                NSWorkspace.shared.open(url)
                wsMessage = "Finish signing in with Google in your browser, then ask Wisp about your mail."
            } else {
                wsMessage = r["error"] as? String ?? "Couldn't start Google sign-in."
            }
        }
    }

    private func watchWorkspace() {
        wsPolling?.cancel()
        wsPolling = Task { [weak self] in
            for _ in 0..<150 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.refreshWorkspace()
                if !self.wsPreparing && (self.wsRunning || !self.wsError.isEmpty) { return }
            }
        }
    }

    func connect() {
        let id = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty else { message = "Enter your OAuth client ID first."; return }
        message = ""
        Task {
            let body: [String: Any] = ["client_id": id,
                                       "client_secret": clientSecret.trimmingCharacters(in: .whitespacesAndNewlines)]
            guard let r = await request("POST", "assistant/gmail/connect", body: body),
                  r["ok"] as? Bool == true, let s = r["auth_url"] as? String,
                  let url = URL(string: s), url.host == "accounts.google.com" else {
                message = "Couldn't start Google sign-in."
                return
            }
            clientSecret = ""
            NSWorkspace.shared.open(url)
            message = "Finish signing in with Google in your browser."
            authorizing = true
            watchForCompletion()
        }
    }

    func disconnect() {
        Task {
            _ = await request("POST", "assistant/gmail/disconnect")
            message = "Gmail disconnected. Wisp will use Mail.app again."
            await refresh()
        }
    }

    private func watchForCompletion() {
        polling?.cancel()
        polling = Task { [weak self] in
            for _ in 0..<150 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.refresh()
                if self.connected { self.message = "Connected."; return }
                if !self.authorizing { return }
            }
        }
    }

    private func request(_ method: String, _ path: String, body: [String: Any]? = nil,
                         timeout: TimeInterval = 10) async -> [String: Any]? {
        var req = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode)
        else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

struct GoogleSettingsPane: View {
    @StateObject private var model = GoogleAccountModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Google").font(.title2.weight(.medium))
            VStack(alignment: .leading, spacing: 12) {
                // Shared credentials: the user's own Google Cloud "Desktop app" OAuth client.
                Text("Your Google OAuth client")
                    .font(.system(size: 13, weight: .medium))
                Text("In Google Cloud Console enable the APIs you want (Gmail, Calendar, Drive), add yourself as a "
                     + "test user on the consent screen, and create an OAuth client of type \"Desktop app\".")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("OAuth client ID", text: $model.clientID).textFieldStyle(.roundedBorder)
                SecureField(model.hasSavedSecret ? "Client secret (saved — leave blank to keep)" : "Client secret",
                            text: $model.clientSecret).textFieldStyle(.roundedBorder)
                TextField("Your Google email", text: $model.googleEmail).textFieldStyle(.roundedBorder)
                Button("Open Google Cloud Console") {
                    if let u = URL(string: "https://console.cloud.google.com/apis/credentials") {
                        NSWorkspace.shared.open(u)
                    }
                }

                Divider()

                // Google Workspace MCP server
                HStack(spacing: 8) {
                    Image(systemName: model.wsRunning ? "checkmark.circle.fill" : "circle.dashed")
                        .foregroundStyle(model.wsRunning ? Color.green : Theme.textMuted)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Google Workspace (Gmail, Calendar, Drive)")
                            .font(.system(size: 14, weight: .medium))
                        Text(model.wsRunning
                             ? "Running — \(model.wsTools) Google tools available to Wisp. Each one asks before it runs, except plain read-only lookups."
                             : model.wsPreparing ? "Setting up…"
                             : model.wsConfigured ? "Configured but not running."
                             : "Not set up. Runs Google's tools locally on this Mac through an MCP server.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if model.wsConfigured { Button("Remove") { model.disableWorkspace() } }
                }
                if model.wsNeedsUV {
                    Text("This needs uv. Install it in Terminal with: brew install uv")
                        .font(.caption).foregroundStyle(.orange)
                }
                if !model.wsError.isEmpty {
                    Text(model.wsError).font(.caption).foregroundStyle(.orange)
                }
                HStack {
                    Button(model.wsPreparing ? "Setting up…" : model.wsConfigured ? "Update settings" : "Set up Google Workspace") {
                        model.enableWorkspace()
                    }
                    .disabled(model.wsPreparing || model.clientID.trimmingCharacters(in: .whitespaces).isEmpty)
                    .buttonStyle(.borderedProminent).tint(Theme.accent)
                    Button("Sign in with Google") { model.signInWorkspace() }
                        .disabled(!model.wsRunning)
                }
                if !model.wsMessage.isEmpty {
                    Text(model.wsMessage).font(.caption).foregroundStyle(.secondary)
                }
                Text("Sign-in expires after 7 days while your Google app is in \"Testing\" mode.")
                    .font(.caption2).foregroundStyle(.secondary)

                Divider()

                // Direct Gmail inbox sync (feeds summaries and the daily brief)
                DisclosureGroup("Inbox sync (direct Gmail, optional)") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 8) {
                            Image(systemName: model.connected ? "checkmark.circle.fill" : "circle.dashed")
                                .foregroundStyle(model.connected ? Color.green : Theme.textMuted)
                            Text(model.connected
                                 ? "Connected as \(model.email). Mail syncs straight from Google into summaries and the daily brief."
                                 : "Not connected. Summaries use Mail.app, which has to be open to stay current.")
                                .font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if model.connected { Button("Disconnect") { model.disconnect() } }
                        }
                        if model.connected, let last = model.lastSync {
                            Text("Last synced \(last.formatted(.relative(presentation: .named)))")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        if !model.lastError.isEmpty {
                            Text("Last sync problem: \(model.lastError)").font(.caption).foregroundStyle(.orange)
                        }
                        Button(model.authorizing ? "Waiting for Google…" : "Connect Gmail inbox sync") { model.connect() }
                            .disabled(model.authorizing || model.clientID.trimmingCharacters(in: .whitespaces).isEmpty)
                        if !model.message.isEmpty {
                            Text(model.message).font(.caption).foregroundStyle(.secondary)
                        }
                        Text("Read-only. Uses the same OAuth client and needs the Gmail API enabled.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .padding(.top, 6)
                }
            }
        }
        .task {
            await model.refresh()
            await model.refreshWorkspace()
        }
    }
}
