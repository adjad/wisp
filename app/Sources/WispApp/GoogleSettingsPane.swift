import AppKit
import SwiftUI

/// Settings ▸ Google: connect Wisp to Gmail and Google Calendar (read-only) so
/// mail and events sync without Mail.app or Calendar open. All OAuth work happens in the local backend (gmail_source.py);
/// this pane only sends the user's own OAuth client id/secret to loopback,
/// opens the consent URL in the default browser, and shows connection status.
@MainActor
final class GoogleAccountModel: ObservableObject {
    struct InboxAccount: Identifiable {
        let id: String
        let lastSync: Date?
        let fresh: Bool
        let error: String
    }
    struct MailAccount: Identifiable {
        let id: String
        let name: String
    }
    @Published var inboxAccounts: [InboxAccount] = []
    @Published var mailAccounts: [MailAccount] = []
    @Published var selectedMailAccountID = ""
    @Published var authorizationState = "idle"
    @Published var authorizationError = ""
    @Published var authorizationGeneration: Int?
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
        let authorization = s["authorization"] as? [String: Any] ?? [:]
        authorizationState = authorization["state"] as? String ?? "idle"
        authorizationError = authorization["error"] as? String ?? ""
        authorizationGeneration = authorization["generation"] as? Int
        lastError = s["last_error"] as? String ?? ""
        hasSavedSecret = s["has_secret"] as? Bool ?? false
        if clientID.isEmpty { clientID = s["client_id"] as? String ?? "" }
        lastSync = (s["last_headers_sync"] as? Double).map(Date.init(timeIntervalSince1970:))
        inboxAccounts = (s["accounts"] as? [[String: Any]] ?? []).compactMap { row in
            guard let address = row["email"] as? String else { return nil }
            return InboxAccount(id: address,
                lastSync: (row["last_headers_sync"] as? Double).map(Date.init(timeIntervalSince1970:)),
                fresh: row["fresh"] as? Bool ?? false, error: row["last_error"] as? String ?? "")
        }
        mailAccounts = (s["mail_accounts"] as? [[String: Any]] ?? []).compactMap { row in
            guard let id = row["id"] as? String, let name = row["name"] as? String else { return nil }
            return MailAccount(id: id, name: name)
        }
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
            guard let response = await request("POST", "assistant/google/workspace/disable", body: [:], timeout: 30),
                  response["ok"] as? Bool == true else {
                wsMessage = "Couldn’t confirm Google Workspace removal. Check its status or try again."
                await refreshWorkspace()
                return
            }
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
        guard !authorizing else { return }
        let id = clientID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !id.isEmpty else { message = "Enter your OAuth client ID first."; return }
        message = ""
        authorizing = true
        polling?.cancel()
        Task {
            let body: [String: Any] = ["client_id": id,
                                       "client_secret": clientSecret.trimmingCharacters(in: .whitespacesAndNewlines),
                                       "mail_account_id": selectedMailAccountID]
            guard let r = await request("POST", "assistant/gmail/connect", body: body) else {
                message = "Couldn't start Google sign-in."
                authorizing = false
                return
            }
            guard r["ok"] as? Bool == true, let s = r["auth_url"] as? String,
                  let generation = r["generation"] as? Int,
                  let url = URL(string: s), url.scheme == "https", url.host == "accounts.google.com" else {
                message = r["error"] as? String ?? "Couldn't confirm this Google sign-in attempt. Try again."
                authorizing = false
                return
            }
            clientSecret = ""
            NSWorkspace.shared.open(url)
            message = "Finish signing in with Google in your browser."
            authorizing = true
            watchForCompletion(generation: generation)
        }
    }

    func disconnect(_ account: String) {
        Task {
            guard let response = await request("POST", "assistant/gmail/disconnect", body: ["account": account]),
                  response["ok"] as? Bool == true else {
                message = "Couldn’t confirm disconnect. Try again."
                return
            }
            message = "Disconnected \(account) and erased its mail, calendar events and to-dos from Wisp. Mail.app's own copy, if any, is untouched."
            await refresh()
        }
    }

    private func watchForCompletion(generation: Int) {
        polling?.cancel()
        polling = Task { [weak self] in
            for _ in 0..<150 {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.refresh()
                guard !Task.isCancelled else { return }
                if let current = self.authorizationGeneration, current != generation {
                    self.message = "This sign-in attempt was replaced by another attempt. Check the connected accounts below."
                    return
                }
                if !self.authorizing {
                    self.message = self.authorizationGeneration == generation && self.authorizationState == "connected"
                        ? "Account connected. Check its refresh status below."
                        : (self.authorizationError.isEmpty ? "Sign-in did not finish. Try again." : self.authorizationError)
                    return
                }
            }
            guard let self, !Task.isCancelled else { return }
            self.message = "Couldn’t confirm Google sign-in. Check connected accounts or try again."
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
                VStack(alignment: .leading, spacing: 10) {
                    Text("Email and calendar with Mail closed").font(.system(size: 14, weight: .medium))
                    Text("Connect each Google or school Google account you want Wisp to check. Wisp reads Google directly: mail, and the next week of Google Calendar if you allow it. Other accounts continue to use Mail’s local cache.")
                        .font(.caption).foregroundStyle(.secondary)
                    ForEach(model.inboxAccounts) { account in
                        HStack(alignment: .top) {
                            Image(systemName: account.fresh ? "checkmark.circle.fill" : "exclamationmark.circle")
                                .foregroundStyle(account.fresh ? Color.green : Color.orange)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(account.id).font(.caption.weight(.medium))
                                Text(account.fresh ? "Recently checked with Google" : "Cache stale or not yet verified")
                                    .font(.caption).foregroundStyle(account.fresh ? Color.secondary : Color.orange)
                                if let last = account.lastSync {
                                    Text("Last successful refresh: \(last.formatted(date: .abbreviated, time: .shortened))")
                                        .font(.caption2).foregroundStyle(.secondary)
                                } else {
                                    Text("No successful refresh recorded").font(.caption2).foregroundStyle(.secondary)
                                }
                                if !account.error.isEmpty {
                                    Text(account.error).font(.caption).foregroundStyle(.orange)
                                }
                            }
                            Spacer()
                            Button("Disconnect & erase") { model.disconnect(account.id) }
                                .help("Revokes Wisp's Google access and deletes this account's mail, calendar events and to-dos from Wisp")
                        }
                    }
                    if !model.mailAccounts.isEmpty {
                        Picker("Corresponding Mail account", selection: $model.selectedMailAccountID) {
                            Text("Match by email address").tag("")
                            ForEach(model.mailAccounts) { account in
                                Text(account.name).tag(account.id)
                            }
                        }
                        Text("Select the same account if Mail uses a name such as “Google”. This keeps its older Mail copy from appearing twice.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Button(model.authorizing ? "Waiting for Google…" : "Connect or reconnect an email account") { model.connect() }
                        .disabled(model.authorizing || model.clientID.trimmingCharacters(in: .whitespaces).isEmpty)
                    if !model.message.isEmpty {
                        Text(model.message).font(.caption).foregroundStyle(.secondary)
                    }
                    Text("Read-only mail and calendar. Google sign-in requires your consent. A school administrator may restrict access. If a refresh fails, Wisp labels cached results and their last successful refresh.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .task {
            await model.refresh()
            await model.refreshWorkspace()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled else { return }
                await model.refresh()
            }
        }
    }
}
