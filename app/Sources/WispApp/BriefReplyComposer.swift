import AppKit
import SwiftUI

/// Reply to an email inline in the notch: Wisp drafts, the user edits, then taps Send.
///
/// Backend: service/assistant/email_reply_api.py.
/// - `POST /assistant/email_reply/draft {reply_ref}` writes a short draft and
///   returns the exact To/Subject Mail will use plus a signed, expiring
///   `envelope`. Nothing is sent.
/// - `POST /assistant/email_reply/send {envelope, body}` sends the text in the
///   editor. Recipients and subject come from the envelope only, so this view
///   never sends them. The Send tap IS the confirmation `email_send` requires.
///   If Mail would really send to different people (a Reply-To), nothing is
///   sent: the answer is `status: "confirm"` with the real To and a new
///   envelope, and the user taps Send again.
///
/// Inline only, no sheets or alerts (the notch is a non-activating panel).
/// Depends on Theme.swift alone so a standalone contract compile can include it.
/// `reply_ref` is the thread's `{message_id, account, account_id}` from
/// `workspace.proactive.threads[].reply_ref`.
struct BriefReplyComposer: View {
    @StateObject private var model: BriefReplyModel
    private let onDismiss: () -> Void

    init(replyRef: [String: String], account: String,
         baseURL: URL = BriefReplyModel.defaultBaseURL,
         transport: BriefReplyModel.Transport? = nil,
         onSent: @escaping () -> Void = {},
         onDismiss: @escaping () -> Void) {
        _model = StateObject(wrappedValue: BriefReplyModel(
            replyRef: replyRef, account: account, baseURL: baseURL,
            transport: transport ?? BriefReplyModel.defaultTransport, onSent: onSent))
        self.onDismiss = onDismiss
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            editor
            if !model.status.isEmpty {
                Text(model.status)
                    .font(.system(size: 11))
                    .foregroundStyle(model.phase == .sent ? Theme.textSecondary
                                     : model.phase == .sending ? Theme.textMuted : Theme.bad)
                    .fixedSize(horizontal: false, vertical: true)
            }
            buttons
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke, lineWidth: 1))
        .task { await model.loadDraft() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(model.phase == .sent ? "REPLY · SENT" : "REPLY · \(model.account.uppercased())")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
                .lineLimit(1)
            if !model.toLine.isEmpty {
                Text("To: \(model.toLine)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Theme.textSecondary)
                    .lineLimit(2)
            }
            if !model.subject.isEmpty {
                Text("Subject: \(model.subject)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(Theme.textSecondary)
                    .lineLimit(1)
            }
        }
    }

    @ViewBuilder private var editor: some View {
        Group {
            if model.phase == .drafting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Drafting…")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textSecondary)
                    Spacer()
                }
                .frame(minHeight: 90)
                .padding(.horizontal, 8)
            } else {
                TextEditor(text: $model.text)
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textPrimary)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 90, maxHeight: 200)
                    .padding(4)
                    .disabled(!model.editable)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.35)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
    }

    private var buttons: some View {
        HStack(spacing: 8) {
            Spacer()
            if model.phase == .sent {
                chip("Done", primary: true) { onDismiss() }
            } else {
                chip("Cancel", primary: false) { onDismiss() }
                    .disabled(model.phase == .sending)
                if model.canSend {
                    chip(model.phase == .sending ? "Sending…" : "Send", primary: true) {
                        Task { await model.send() }
                    }
                    .disabled(!model.sendEnabled)
                    .keyboardShortcut(.return, modifiers: .command)
                } else if let url = model.openURL {
                    chip(model.openLabel.isEmpty ? "Open in Mail ↗" : model.openLabel + " ↗", primary: true) {
                        model.copyDraft()
                        NSWorkspace.shared.open(url)
                    }
                    .disabled(model.phase == .drafting)
                }
            }
        }
    }

    private func chip(_ title: String, primary: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 12, weight: primary ? .semibold : .regular))
                .foregroundStyle(primary ? Color.black : Theme.textPrimary)
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .background(Capsule().fill(primary ? Theme.textPrimary : Theme.chipFill))
                .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// Draft/send state for one reply. The transport is injectable so fixtures and
/// checks never reach the service.
@MainActor
final class BriefReplyModel: ObservableObject {
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)

    enum Phase: Equatable { case drafting, ready, sending, sent, unknown }

    /// Mirrors WispClient.baseURL; kept here so this file compiles on its own.
    nonisolated static let defaultBaseURL = URL(string: "http://127.0.0.1:8765")!

    /// Notch QA renders never reach a socket: they get a canned draft.
    nonisolated static var defaultTransport: Transport {
        #if WISP_NOTCH_QA
        return { request in
            let path = request.url?.lastPathComponent ?? ""
            let body: [String: Any] = path == "draft"
                ? ["ok": true, "draft": "Hi Dan, Thursday at 10 works. See you then.",
                   "to": ["dan@example.com"], "cc": [], "subject": "Re: Coffee chat?",
                   "account": "Work", "envelope": "qa", "can_send": true]
                : ["ok": true, "status": "sent", "result": "Reply sent to dan@example.com."]
            let data = try JSONSerialization.data(withJSONObject: body)
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        #else
        return { try await URLSession.shared.data(for: $0) }
        #endif
    }

    @Published var text = ""
    @Published private(set) var phase: Phase = .drafting
    @Published private(set) var status = ""
    @Published private(set) var account: String
    @Published private(set) var to: [String] = []
    @Published private(set) var cc: [String] = []
    @Published private(set) var subject = ""
    @Published private(set) var canSend = false
    @Published private(set) var openURL: URL?
    @Published private(set) var openLabel = ""

    private let replyRef: [String: String]
    private let baseURL: URL
    private let transport: Transport
    private let onSent: () -> Void
    private var envelope: String?
    private var loaded = false

    init(replyRef: [String: String], account: String, baseURL: URL,
         transport: @escaping Transport, onSent: @escaping () -> Void = {}) {
        self.replyRef = replyRef
        self.account = account
        self.baseURL = baseURL
        self.transport = transport
        self.onSent = onSent
    }

    var toLine: String { (to + cc).joined(separator: ", ") }
    var editable: Bool { phase == .ready }
    var sendEnabled: Bool {
        phase == .ready && canSend && envelope != nil
            && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    func loadDraft() async {
        guard !loaded else { return }
        loaded = true
        phase = .drafting
        status = ""
        let obj = await post("assistant/email_reply/draft", ["reply_ref": replyRef], timeout: 90)
        apply(draft: obj)
    }

    /// Exposed for checks: applies a /draft response.
    func apply(draft obj: [String: Any]?) {
        phase = .ready
        guard let obj, obj["ok"] as? Bool == true else {
            status = (obj?["result"] as? String) ?? "Wisp couldn't open this email for a reply."
            canSend = false
            return
        }
        text = obj["draft"] as? String ?? ""
        to = obj["to"] as? [String] ?? []
        cc = obj["cc"] as? [String] ?? []
        subject = obj["subject"] as? String ?? ""
        if let label = obj["account"] as? String, !label.isEmpty { account = label }
        envelope = obj["envelope"] as? String
        canSend = obj["can_send"] as? Bool == true && envelope != nil
        openURL = (obj["open_url"] as? String).flatMap { $0.isEmpty ? nil : URL(string: $0) }
        openLabel = obj["open_label"] as? String ?? ""
        if !canSend {
            let reason = obj["reason"] as? String ?? ""
            status = reason.isEmpty ? "Wisp can't send from this account." : reason
            if openURL != nil { status += " The draft is copied when you open it." }
        } else if let note = obj["draft_error"] as? String, !note.isEmpty {
            status = note
        }
    }

    func send() async {
        guard sendEnabled, let envelope else { return }
        phase = .sending
        status = "Sending…"
        // Only the envelope and the edited text: recipients live in the envelope.
        let obj = await post("assistant/email_reply/send", ["envelope": envelope, "body": text], timeout: 120)
        apply(send: obj)
    }

    /// Exposed for checks: applies a /send response.
    func apply(send obj: [String: Any]?) {
        let result = obj?["result"] as? String ?? ""
        switch obj?["status"] as? String {
        case "sent" where obj?["ok"] as? Bool == true:
            phase = .sent
            status = "Sent ✓"
            onSent()
        case "confirm":
            // Mail's real reply goes to different people (a Reply-To, say).
            // Nothing was sent: show who it really goes to and wait for a new tap.
            to = obj?["to"] as? [String] ?? to
            cc = obj?["cc"] as? [String] ?? []
            if let s = obj?["subject"] as? String, !s.isEmpty { subject = s }
            envelope = obj?["envelope"] as? String
            canSend = envelope != nil
            phase = .ready
            status = result.isEmpty ? "Mail will send this to \(toLine). Tap Send to confirm." : result
        case "refused":
            // Nothing went out; the user may edit and try again unless the
            // envelope itself is no longer usable.
            phase = .ready
            status = result.isEmpty ? "Not sent." : result
            if let error = obj?["error"] as? String, error.hasPrefix("envelope_") { canSend = false }
        default:
            // Unknown outcome (or no answer): never offer a second Send.
            phase = .unknown
            canSend = false
            status = obj == nil ? "Wisp couldn't confirm the reply. Check Mail before trying again."
                : (result.isEmpty ? "Mail didn't confirm the reply. Check Mail before trying again." : result)
        }
    }

    func copyDraft() {
        let draft = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !draft.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(draft, forType: .string)
    }

    private func post(_ path: String, _ body: [String: Any], timeout: TimeInterval) async -> [String: Any]? {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await transport(request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        return obj
    }
}
