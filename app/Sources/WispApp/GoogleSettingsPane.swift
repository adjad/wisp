import AppKit
import SwiftUI

/// Settings ▸ Google: one section, one sign-in per account.
///
/// A Google sign-in serves everything: the Google Workspace tools Ask uses
/// (Gmail, Calendar, Drive, Docs, Chat) and, as hidden plumbing, Brief's
/// read-only mail and calendar sync. Each account shows ONE status line that
/// the backend merged from both (gmail_source.account_state). The first
/// connect sets the Workspace server up by itself; its controls, the
/// corresponding Mail account, the user's own OAuth client and the
/// Testing-mode note sit under Advanced.
///
/// All OAuth work happens in the local backend; this pane only calls loopback
/// routes, opens the consent URL in the default browser and shows status.
@MainActor
final class GoogleAccountModel: ObservableObject {
    struct MailAccount: Identifiable {
        let id: String
        let name: String
    }
    @Published var status = GoogleStatus()
    @Published var loaded = false
    @Published var mailAccounts: [MailAccount] = []
    @Published var selectedMailAccountID = ""
    @Published var authorizationState = "idle"
    @Published var authorizationError = ""
    @Published var authorizationGeneration: Int?
    @Published var authorizing = false
    @Published var message = ""
    /// The user's own OAuth client (Advanced ▸ Use your own OAuth client).
    @Published var clientID = ""
    @Published var clientSecret = ""

    // The Google Workspace server (Advanced).
    @Published var wsConfigured = false
    @Published var wsRunning = false
    @Published var wsTools = 0
    @Published var wsError = ""
    @Published var wsPreparing = false
    @Published var wsMessage = ""

    private var polling: Task<Void, Never>?
    private var wsPolling: Task<Void, Never>?

    var consentNotice: String { GoogleConsentCopy.notice(status) }
    /// Typed into Advanced: this sign-in uses the user's own client.
    private var typedClientID: String { clientID.trimmingCharacters(in: .whitespacesAndNewlines) }
    var canConnect: Bool { status.canConnect || !typedClientID.isEmpty }

    /// Said while the service doesn't answer; a later answer clears it.
    nonisolated static let serviceDown = "Wisp's service isn't responding."

    func refresh() async {
        guard let s = await request("GET", "assistant/gmail/status") else {
            message = Self.serviceDown
            return
        }
        if message == Self.serviceDown { message = "" }
        status = GoogleStatus.parse(s)
        loaded = true
        authorizing = s["authorizing"] as? Bool ?? false
        let authorization = s["authorization"] as? [String: Any] ?? [:]
        authorizationState = authorization["state"] as? String ?? "idle"
        authorizationError = authorization["error"] as? String ?? ""
        authorizationGeneration = authorization["generation"] as? Int
        if clientID.isEmpty { clientID = status.userClientID }
        if let w = s["workspace"] as? [String: Any] { applyWorkspace(w) }
        mailAccounts = (s["mail_accounts"] as? [[String: Any]] ?? []).compactMap { row in
            guard let id = row["id"] as? String, let name = row["name"] as? String else { return nil }
            return MailAccount(id: id, name: name)
        }
    }

    func refreshWorkspace() async {
        guard let s = await request("GET", "assistant/google/workspace/status") else { return }
        applyWorkspace(s)
    }

    private func applyWorkspace(_ s: [String: Any]) {
        wsConfigured = s["configured"] as? Bool ?? false
        wsRunning = s["running"] as? Bool ?? false
        wsTools = s["tools"] as? Int ?? 0
        wsError = s["error"] as? String ?? ""
        let prep = s["prepare"] as? [String: Any]
        wsPreparing = (prep?["state"] as? String) == "preparing"
        if (prep?["state"] as? String) == "failed" { wsError = prep?["detail"] as? String ?? wsError }
    }

    /// Advanced ▸ Update settings: (re)write the Workspace server entry with the
    /// typed client, or the same client Connect uses when none is typed.
    func enableWorkspace() {
        wsMessage = ""
        Task {
            var body: [String: Any] = ["email": status.accounts.first?.id ?? ""]
            if !typedClientID.isEmpty {
                body["client_id"] = typedClientID
                body["client_secret"] = clientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
            }
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
            wsMessage = "Google Workspace removed. Reconnect an account to set it up again."
            await refreshWorkspace()
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

    /// Connect a new account, or (`reconnect`) sign an existing one in again.
    func connect(reconnect account: String? = nil) {
        guard !authorizing else { return }
        guard account != nil || canConnect else {
            message = "Add your own OAuth client under Advanced first."
            return
        }
        message = ""
        authorizing = true
        polling?.cancel()
        Task {
            var body: [String: Any] = ["mail_account_id": selectedMailAccountID]
            if let account {
                body["reconnect"] = account
            } else if !typedClientID.isEmpty {
                body["client_id"] = typedClientID
                body["client_secret"] = clientSecret.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            guard let r = await request("POST", "assistant/gmail/connect", body: body) else {
                message = "Couldn't start Google sign-in."
                authorizing = false
                return
            }
            guard r["ok"] as? Bool == true, let s = r["auth_url"] as? String,
                  let generation = r["generation"] as? Int,
                  let url = URL(string: s), url.scheme == "https", url.host == "accounts.google.com" else {
                let error = r["error"] as? String ?? ""
                message = error.contains("client_id") ? "Add your own OAuth client under Advanced first."
                    : error.isEmpty ? "Couldn't confirm this Google sign-in attempt. Try again." : error
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
            let response = await request("POST", "assistant/gmail/disconnect", body: ["account": account], timeout: 20)
            message = GoogleErase.note(account: account, response: response)
                + (response == nil ? "" : " Mail.app's own copy, if any, is untouched.")
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
                    self.message = "This sign-in attempt was replaced by another attempt. Check the accounts above."
                    return
                }
                if !self.authorizing {
                    self.message = self.authorizationGeneration == generation && self.authorizationState == "connected"
                        ? "Account connected."
                        : (self.authorizationError.isEmpty ? "Sign-in did not finish. Try again." : self.authorizationError)
                    await self.refreshWorkspace()
                    return
                }
            }
            guard let self, !Task.isCancelled else { return }
            self.message = "Couldn’t confirm Google sign-in. Check the accounts above or try again."
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
    /// Black and white in a standalone Settings, the Wisp window's colours there.
    @Environment(\.themePalette) private var palette
    @State private var advanced = false
    @State private var ownClient = false
    /// Account notes the user chose "Not now" for (UserDefaults, per account).
    @State private var dismissedNotes: [String: String] = [:]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Google").font(.title2.weight(.medium))
            Text("Gmail, Calendar, Drive, Docs and Chat through Google Workspace. Brief reads your mail and calendar; Ask can use Google tools and asks before anything that changes.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if !model.status.accounts.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(model.status.accounts) { account in accountRow(account) }
                }
            }

            VStack(alignment: .leading, spacing: 6) {
                Button(model.authorizing ? "Waiting for Google…" : "Connect Google account") { model.connect() }
                    .buttonStyle(.borderedProminent).tint(palette.accent)
                    .disabled(model.authorizing || !model.canConnect)
                if !model.consentNotice.isEmpty {
                    Text(model.consentNotice).font(.caption).foregroundStyle(.secondary)
                }
                if model.loaded && !model.canConnect {
                    Text("This copy of Wisp has no built-in Google sign-in. Add your own OAuth client under Advanced.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !model.message.isEmpty {
                    Text(model.message).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            DisclosureGroup("Advanced", isExpanded: $advanced) { advancedSection.padding(.top, 8) }
                .font(.system(size: 13))
        }
        .onChange(of: model.status.accounts.map(\.id)) { _, ids in
            for id in ids where dismissedNotes[id] == nil {
                dismissedNotes[id] = UserDefaults.standard.string(forKey: "WispGoogleNoteDismissed." + id) ?? ""
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

    private func color(_ state: GoogleStatus.Account.State) -> Color {
        state == .connected ? .green : .orange
    }

    private func accountRow(_ account: GoogleStatus.Account) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: account.state == .connected ? "checkmark.circle.fill" : "exclamationmark.circle")
                .foregroundStyle(color(account.state))
            VStack(alignment: .leading, spacing: 3) {
                Text(account.id).font(.callout.weight(.medium))
                Text(account.statusLine).font(.caption).foregroundStyle(color(account.state))
                    .fixedSize(horizontal: false, vertical: true)
                if GoogleAccountNote.shows(account, dismissed: dismissedNotes[account.id]) {
                    HStack(spacing: 8) {
                        Text(account.note).font(.caption2).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Reconnect") { model.connect(reconnect: account.id) }
                            .controlSize(.small).disabled(model.authorizing)
                        Button("Not now") {
                            UserDefaults.standard.set(account.note, forKey: GoogleAccountNote.key(account))
                            dismissedNotes[account.id] = account.note
                        }
                        .controlSize(.small)
                    }
                }
            }
            Spacer()
            if let title = account.reconnectTitle {
                Button(title) { model.connect(reconnect: account.id) }
                    .disabled(model.authorizing)
            }
            Button("Disconnect & erase") { model.disconnect(account.id) }
                .help("Revokes Wisp's Google access and deletes this account's mail, calendar events and to-dos from Wisp")
        }
    }

    @ViewBuilder private var advancedSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            // The Workspace server that serves Ask's Google tools.
            HStack(spacing: 8) {
                Image(systemName: model.wsRunning ? "checkmark.circle.fill" : "circle.dashed")
                    .foregroundStyle(model.wsRunning ? Color.green : palette.textMuted)
                Text(model.wsRunning ? "Running — \(model.wsTools) Google tools"
                     : model.wsPreparing ? "Setting up Google tools…"
                     : model.wsConfigured ? "Google tools configured but not running"
                     : "Google tools not set up. Connecting an account sets them up.")
                    .font(.caption)
                Spacer()
                Button(model.wsPreparing ? "Setting up…" : "Update settings") { model.enableWorkspace() }
                    .disabled(model.wsPreparing || (!model.canConnect))
                if model.wsConfigured { Button("Remove") { model.disableWorkspace() } }
            }
            // Workspace trouble shows once, here, never on an account row.
            if !model.status.workspaceNote.isEmpty {
                Text(model.status.workspaceNote).font(.caption).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                if !model.status.workspaceNoteDetail.isEmpty {
                    Text(model.status.workspaceNoteDetail).font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if !model.wsError.isEmpty {
                Text(model.wsError).font(.caption).foregroundStyle(.orange)
            }
            if !model.wsMessage.isEmpty {
                Text(model.wsMessage).font(.caption).foregroundStyle(.secondary)
            }

            // The raw reason behind each account's status line.
            ForEach(model.status.accounts.filter { !$0.detail.isEmpty }) { account in
                Text("\(account.id): \(account.detail)").font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !model.mailAccounts.isEmpty {
                Picker("Corresponding Mail account", selection: $model.selectedMailAccountID) {
                    Text("Match by email address").tag("")
                    ForEach(model.mailAccounts) { account in
                        Text(account.name).tag(account.id)
                    }
                }
                .redrawsOnAppearanceChange()
                Text("Select the same account if Mail uses a name such as “Google”. This keeps its older Mail copy from appearing twice. It applies to the next Connect.")
                    .font(.caption2).foregroundStyle(.secondary)
            }

            DisclosureGroup("Use your own OAuth client", isExpanded: $ownClient) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Optional. In Google Cloud Console enable the APIs you want, add yourself as a test user on the consent screen, and create an OAuth client of type \"Desktop app\". Your client is used instead of Wisp's.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    TextField("OAuth client ID", text: $model.clientID).textFieldStyle(.roundedBorder)
                    SecureField(model.status.hasSavedSecret ? "Client secret (saved — leave blank to keep)" : "Client secret",
                                text: $model.clientSecret).textFieldStyle(.roundedBorder)
                    Button("Open Google Cloud Console") {
                        if let u = URL(string: "https://console.cloud.google.com/apis/credentials") {
                            NSWorkspace.shared.open(u)
                        }
                    }
                }
                .padding(.top, 6)
            }
            .font(.caption)

            Text("While Wisp's Google app is in Testing mode, Google ends each sign-in after 7 days. Wisp says when, and Reconnect signs you back in.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
