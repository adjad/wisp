import AppKit
import SwiftUI

/// "Connect Google" in the notch's Quick Settings, for a new user only.
///
/// This is the user's deliberate exception to the rule that controls which
/// widen what Wisp reads stay in the Settings window: the very first connect
/// may start here. QuickSettingsView shows the row only while no Google
/// account is connected and an OAuth client exists
/// (NotchGoogleVisibility.showsConnect); once an account is connected the row
/// disappears entirely. Accounts, their status, Reconnect and
/// "Disconnect & erase" live in Settings ▸ Google only.
///
/// All OAuth work stays in the backend (gmail_source.py); this view only calls
///   GET  /assistant/gmail/status      accounts, the client source, sign-in progress
///   POST /assistant/gmail/connect {}  the user's own client, else the built-in one
@MainActor
final class NotchGoogleModel: ObservableObject {
    @Published var status = GoogleStatus()
    /// A status read has landed; until then nothing is shown.
    @Published var loaded = false
    @Published var stage: NotchGoogleStage = .idle

    private var watch: Task<Void, Never>?

    /// Whether Quick Settings shows the Google row at all.
    var showsRow: Bool {
        NotchGoogleVisibility.showsConnect(loaded: loaded, status: status, signInInProgress: stage.busy)
    }

    func refresh() async {
        guard let s = await request("GET", "assistant/gmail/status") else { return }
        apply(s)
    }

    /// Keep the decision current while Quick Settings is open (Settings may
    /// connect or erase an account meanwhile).
    func keepCurrent() async {
        await refresh()
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard !Task.isCancelled else { return }
            if !stage.busy { await refresh() }
        }
    }

    private func apply(_ s: [String: Any], generation: Int? = nil) {
        status = GoogleStatus.parse(s)
        loaded = true
        if let generation {
            stage = NotchGoogleStage.from(authorization: s["authorization"] as? [String: Any] ?? [:],
                                          generation: generation, current: stage)
        }
    }

    var consentLine: String {
        "Gmail, Calendar, Drive, Docs and Chat, read-only. Wisp builds your to-dos on this Mac."
    }

    func connect() {
        guard !stage.busy else { return }
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
                stage = .failed(error.contains("client_id") ? "Set up Google in Settings ▸ Google."
                                : error.isEmpty ? "Couldn't start Google sign-in." : error)
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

/// The notch's one Google row: a Connect button for a new user, with the
/// unverified-app notice before the browser opens and the sign-in's progress.
struct NotchConnectGoogle: View {
    @ObservedObject var model: NotchGoogleModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.crop.circle.badge.plus").foregroundStyle(Theme.textPrimary)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Google account").font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Theme.textPrimary)
                    Text(model.consentLine)
                        .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                chip(model.stage.busy ? "Connecting…" : "Connect Google", primary: !model.stage.busy) {
                    model.connect()
                }
                .disabled(model.stage.busy)
            }
            let notice = GoogleConsentCopy.notice(model.status)
            if !notice.isEmpty && !model.stage.busy {
                Text(notice).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
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
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
    }

    private var isFailure: Bool {
        if case .failed = model.stage { return true }
        return false
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
