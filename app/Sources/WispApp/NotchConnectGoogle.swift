import AppKit
import SwiftUI

/// "Connect Google" in the notch: a judge (or the user) signs in with Google in
/// their browser and, within about a minute, Wisp shows their own proactive
/// to-dos. Connected accounts are listed with "Disconnect & erase", which
/// removes the account and everything Wisp derived from it.
///
/// All OAuth work stays in the backend (gmail_source.py); this view only calls
/// the existing loopback routes:
///   GET  /assistant/gmail/status      accounts, the OAuth client, sign-in progress
///   POST /assistant/gmail/connect {}  uses the client saved in Settings ▸ Google
///   POST /assistant/gmail/disconnect {"account"}   disconnect & erase
///
/// No sheets or alerts: the notch is a non-activating panel, so the erase
/// confirmation is an inline second tap.
@MainActor
final class NotchGoogleModel: ObservableObject {
    struct Account: Identifiable, Equatable {
        let id: String
        let calendar: Bool
        let error: String
    }

    @Published var accounts: [Account] = []
    @Published var configured = true
    @Published var stage: NotchGoogleStage = .idle
    @Published var confirming: String?
    @Published var erasing: String?
    @Published var note = ""

    private var watch: Task<Void, Never>?

    func refresh() async {
        guard let s = await request("GET", "assistant/gmail/status") else { return }
        apply(s)
    }

    private func apply(_ s: [String: Any], generation: Int? = nil) {
        configured = !((s["client_id"] as? String) ?? "").isEmpty
        accounts = (s["accounts"] as? [[String: Any]] ?? []).compactMap { row in
            guard let email = row["email"] as? String else { return nil }
            let error = (row["last_error"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                ?? (row["calendar_error"] as? String ?? "")
            return Account(id: email, calendar: row["calendar"] as? Bool ?? false, error: error)
        }
        if let generation {
            stage = NotchGoogleStage.from(authorization: s["authorization"] as? [String: Any] ?? [:],
                                          generation: generation, current: stage)
        }
    }

    func connect() {
        guard !stage.busy else { return }
        note = ""
        stage = .starting
        watch?.cancel()
        Task {
            guard let r = await request("POST", "assistant/gmail/connect", body: [:], timeout: 15) else {
                stage = .failed("Wisp's service isn't responding.")
                return
            }
            guard r["ok"] as? Bool == true, let s = r["auth_url"] as? String,
                  let generation = r["generation"] as? Int,
                  let url = URL(string: s), url.scheme == "https", url.host == "accounts.google.com" else {
                let error = r["error"] as? String ?? ""
                if error.contains("client_id") { configured = false; stage = .idle; return }
                stage = .failed(error.isEmpty ? "Couldn't start Google sign-in." : error)
                return
            }
            NSWorkspace.shared.open(url)
            stage = .waiting
            follow(generation)
        }
    }

    /// Poll until the sign-in this view started is ready, fails, or is replaced.
    private func follow(_ generation: Int) {
        watch = Task { [weak self] in
            for _ in 0..<240 {   // 6 minutes: the backend gives sign-in 5
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard let self, !Task.isCancelled else { return }
                guard let s = await self.request("GET", "assistant/gmail/status") else { continue }
                self.apply(s, generation: generation)
                if case .connected = self.stage { return }
                if case .failed = self.stage { return }
                if self.stage == .idle { return }
            }
            guard let self, !Task.isCancelled else { return }
            if self.stage.busy { self.stage = .failed("Sign-in didn't finish. Try again.") }
        }
    }

    func disconnect(_ account: String) {
        guard confirming == account else { confirming = account; return }
        confirming = nil
        erasing = account
        Task {
            let r = await request("POST", "assistant/gmail/disconnect", body: ["account": account], timeout: 20)
            erasing = nil
            if r?["ok"] as? Bool == true {
                note = "Disconnected \(account) and erased its mail, calendar and to-dos from this Mac."
                if case .connected(let email, _) = stage, email == account { stage = .idle }
            } else {
                note = "Couldn't confirm the erase. Try again."
            }
            await refresh()
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

/// Where one Connect Google attempt is. Pure: built from the backend's
/// `authorization` status (state + phase) for the attempt's generation.
enum NotchGoogleStage: Equatable {
    case idle, starting, waiting, reading, analyzing
    case connected(String, analyze: String)
    case failed(String)

    var busy: Bool {
        switch self {
        case .starting, .waiting, .reading, .analyzing: return true
        default: return false
        }
    }

    var line: String {
        switch self {
        case .idle: return ""
        case .starting: return "Opening Google sign-in…"
        case .waiting: return "Waiting for sign-in in your browser…"
        case .reading: return "Reading mail & calendar…"
        case .analyzing: return "Building your to-dos…"
        case .connected(let email, let analyze):
            return analyze == "requested" ? "Connected as \(email). Your to-dos are on the way in Brief."
                                          : "Connected as \(email)."
        case .failed(let message): return message
        }
    }

    static func from(authorization a: [String: Any], generation: Int, current: NotchGoogleStage) -> NotchGoogleStage {
        if let g = a["generation"] as? Int, g != generation {
            return .failed("Another sign-in replaced this one. Try again.")
        }
        let email = a["email"] as? String ?? ""
        switch a["state"] as? String ?? "" {
        case "authorizing": return .waiting
        case "connected":
            switch a["phase"] as? String ?? "" {
            case "analyzing": return .analyzing
            case "ready": return .connected(email, analyze: a["analyze"] as? String ?? "")
            case "reading": return .reading
            // A backend without phases: connected is the end.
            default: return .connected(email, analyze: "")
            }
        case "failed":
            let error = a["error"] as? String ?? ""
            return .failed(error.isEmpty ? "Sign-in didn't finish. Try again." : error)
        case "cancelled": return .idle
        default: return current
        }
    }
}

struct NotchConnectGoogle: View {
    @StateObject private var model = NotchGoogleModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.crop.circle.badge.plus").foregroundStyle(Theme.textPrimary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Google account").font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                    Text("Read-only mail & calendar. Wisp builds your to-dos on this Mac.")
                        .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                }
                Spacer(minLength: 8)
                if model.configured {
                    chip(model.stage.busy ? "Connecting…" : "Connect Google", primary: !model.stage.busy) {
                        model.connect()
                    }
                    .disabled(model.stage.busy)
                }
            }
            if !model.configured {
                Text("Add your Google OAuth client in Settings ▸ Google first.")
                    .font(.system(size: 11.5)).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.stage.line.isEmpty {
                HStack(spacing: 6) {
                    if model.stage.busy { ProgressView().controlSize(.mini) }
                    Text(model.stage.line).font(.system(size: 11.5))
                        .foregroundStyle(isFailure ? Color.orange : Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            ForEach(model.accounts) { account in
                accountRow(account)
            }
            if !model.note.isEmpty {
                Text(model.note).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
        .task {
            await model.refresh()
            // Keep the list current while visible (another surface may connect or erase).
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard !Task.isCancelled else { return }
                if !model.stage.busy { await model.refresh() }
            }
        }
    }

    private var isFailure: Bool {
        if case .failed = model.stage { return true }
        return false
    }

    private func accountRow(_ account: NotchGoogleModel.Account) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: account.error.isEmpty ? "checkmark.circle.fill" : "exclamationmark.circle")
                .foregroundStyle(account.error.isEmpty ? Theme.textPrimary : Color.orange)
            VStack(alignment: .leading, spacing: 1) {
                Text(account.id).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                Text(account.error.isEmpty ? (account.calendar ? "Mail & calendar" : "Mail only (calendar not shared)")
                                           : account.error)
                    .font(.system(size: 10.5)).foregroundStyle(account.error.isEmpty ? Theme.textMuted : Color.orange)
                    .lineLimit(2)
            }
            Spacer(minLength: 6)
            if model.erasing == account.id {
                ProgressView().controlSize(.mini)
            } else if model.confirming == account.id {
                chip("Erase", primary: true) { model.disconnect(account.id) }
                chip("Keep") { model.confirming = nil }
            } else {
                chip("Disconnect & erase") { model.disconnect(account.id) }
            }
        }
    }

    private func chip(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: primary ? .semibold : .medium))
                .foregroundStyle(primary ? Color.black : Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(primary ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}
