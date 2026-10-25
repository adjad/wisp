import Foundation
import Combine

/// Clear and Mark done for the Brief's rows: the Needs you rows
/// (`POST /assistant/needs_you/<id>/clear|done`, service/assistant/needs_you_api.py)
/// and the proactive Brief's to-dos and mail/text threads
/// (`POST /assistant/proactive/todos|threads/<id>/clear`, `.../todos/<id>/done`,
/// service/proactive/api.py), each row naming its own route.
///
/// Clear only hides a row from Needs you; the reminder, mail or text stays where
/// it is, and the row comes back if it changes. Mark done completes a reminder
/// through the service's verified native path, and the row leaves only once the
/// service says the completion was read back. Both run only from the person's
/// click. Its own transport, so render fixtures never reach the service.
@MainActor
final class NeedsYouActionsModel: ObservableObject {
    /// The row whose × is open, showing "Clear · Mark done" in its place.
    @Published var open: String?
    /// The row waiting on the service, and for what.
    @Published private(set) var busy: [String: NeedsYouRowAction] = [:]
    /// A failed answer's message, by row id.
    @Published private(set) var failures: [String: String] = [:]
    /// Rows the service confirmed cleared or done (NeedsYouRow.dismissalKey),
    /// hidden until the next plan leaves them out on its own.
    @Published private(set) var hidden: Set<String> = []
    /// Rows whose Mark done failed or ended unknown: no second try from here, so
    /// a retry can never act on a reminder that already changed.
    @Published private(set) var doneBlocked: Set<String> = []

    private let transport: (URLRequest) async throws -> (Data, URLResponse)
    private let baseURL: URL

    init(baseURL: URL,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) }) {
        self.baseURL = baseURL
        self.transport = transport
    }

    func isHidden(_ row: NeedsYouRow) -> Bool { hidden.contains(row.dismissalKey) }

    /// What the row offers now: Mark done goes once it failed.
    func actions(for row: NeedsYouRow) -> [NeedsYouRowAction] {
        row.actions.filter { $0 != .markDone || !doneBlocked.contains(row.id) }
    }

    func toggle(_ row: NeedsYouRow) {
        open = open == row.id ? nil : row.id
    }

    /// Runs one answer; true when the service confirmed it.
    @discardableResult
    func perform(_ action: NeedsYouRowAction, on row: NeedsYouRow) async -> Bool {
        guard busy.isEmpty, actions(for: row).contains(action), let fingerprint = row.fingerprint else { return false }
        busy[row.id] = action
        failures[row.id] = nil
        defer { busy[row.id] = nil }
        let base = row.route.split(separator: "/").reduce(baseURL) { $0.appendingPathComponent(String($1)) }
        var request = URLRequest(url: base.appendingPathComponent(row.id).appendingPathComponent(action.path))
        request.httpMethod = "POST"
        request.timeoutInterval = action == .markDone ? 30 : 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["fingerprint": fingerprint])
        do {
            let (data, response) = try await transport(request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 200 {
                hidden.insert(row.dismissalKey)
                if open == row.id { open = nil }
                return true
            }
            let detail = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["detail"] as? String
            failures[row.id] = Self.failure(status: status, detail: detail, action: action)
            if action == .markDone, status != 404 { doneBlocked.insert(row.id) }
        } catch {
            // No reply: a completion may or may not have happened.
            failures[row.id] = action == .markDone
                ? "Wisp didn't hear back, so it may or may not be done. Check Reminders."
                : "Couldn't reach Wisp. Try again."
            if action == .markDone { doneBlocked.insert(row.id) }
        }
        return false
    }

    /// A failed answer in plain words. The service's own detail wins for the
    /// answers its API defines (404, 409, 502).
    static func failure(status: Int, detail: String?, action: NeedsYouRowAction) -> String {
        if let detail, !detail.isEmpty, [404, 409, 502].contains(status) { return detail }
        switch (status, action) {
        case (404, _): return "That's no longer in Needs you."
        case (_, .clear): return "Couldn't clear it. Try again."
        default: return "Wisp couldn't confirm it was marked done. Check Reminders."
        }
    }
}
