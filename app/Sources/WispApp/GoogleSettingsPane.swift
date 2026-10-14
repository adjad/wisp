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

    private var polling: Task<Void, Never>?

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

    private func request(_ method: String, _ path: String, body: [String: Any]? = nil) async -> [String: Any]? {
        var req = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.timeoutInterval = 10
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
                HStack(spacing: 8) {
                    Image(systemName: model.connected ? "checkmark.circle.fill" : "circle.dashed")
                        .foregroundStyle(model.connected ? Color.green : Theme.textMuted)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Gmail").font(.system(size: 14, weight: .medium))
                        Text(model.connected
                             ? "Connected as \(model.email). Mail syncs directly from Google, so Mail.app doesn't need to be open."
                             : "Not connected. Wisp reads mail through Mail.app, which has to be open to stay current.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if model.connected {
                        Button("Disconnect") { model.disconnect() }
                    }
                }
                if model.connected, let last = model.lastSync {
                    Text("Last synced \(last.formatted(.relative(presentation: .named)))")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if !model.lastError.isEmpty {
                    Text("Last sync problem: \(model.lastError). If Google revoked access, reconnect below.")
                        .font(.caption).foregroundStyle(.orange)
                }

                Divider()

                Text(model.connected ? "Reconnect with a different client" : "Connect Gmail")
                    .font(.system(size: 13, weight: .medium))
                Text("Read-only: Wisp can read your mail but never send, delete or change it. Mail goes from Google to this Mac only. "
                     + "In Google Cloud Console, enable the Gmail API, add yourself as a test user on the consent screen, "
                     + "and create an OAuth client of type \"Desktop app\". Paste its details here.")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("OAuth client ID", text: $model.clientID)
                    .textFieldStyle(.roundedBorder)
                SecureField(model.hasSavedSecret ? "Client secret (saved — leave blank to keep)" : "Client secret",
                            text: $model.clientSecret)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    Button(model.authorizing ? "Waiting for Google…" : "Connect with Google") { model.connect() }
                        .disabled(model.authorizing || model.clientID.trimmingCharacters(in: .whitespaces).isEmpty)
                        .buttonStyle(.borderedProminent)
                        .tint(Theme.accent)
                    Button("Open Google Cloud Console") {
                        if let u = URL(string: "https://console.cloud.google.com/apis/credentials") {
                            NSWorkspace.shared.open(u)
                        }
                    }
                }
                if !model.message.isEmpty {
                    Text(model.message).font(.caption).foregroundStyle(.secondary)
                }
                Text("If your Google app is in \"Testing\" mode, Google expires the sign-in after 7 days and you'll need to reconnect.")
                    .font(.caption2).foregroundStyle(.secondary)
                Text("Only Gmail is supported so far. Google Calendar and Drive aren't connected.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
        .task { await model.refresh() }
    }
}
