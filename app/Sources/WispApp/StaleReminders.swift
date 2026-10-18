import Foundation
import Combine

/// One "keep or delete this old undated reminder?" question from the service
/// (`GET /assistant/stale_reminders`, service/assistant/stale_reminders.py).
/// An undated reminder about a week old is asked about once; the service stops
/// listing it after it is answered, or after another week. Older ones are never
/// mentioned, so the notch shows only the questions, never a hidden count.
struct StaleReminderAsk: Equatable, Identifiable {
    let sourceID: String
    let title: String
    /// The Reminders list it is in, when known.
    let context: String
    let ageDays: Int?
    var id: String { sourceID }

    /// "Added 9 days ago · Groceries"
    var detail: String {
        let age = ageDays.map { "Added \($0) day\($0 == 1 ? "" : "s") ago" } ?? "No date"
        return context.isEmpty ? age : age + " · " + context
    }

    /// The questions in a reply, or nil when the reply isn't one.
    static func parse(_ obj: [String: Any]?) -> [StaleReminderAsk]? {
        guard let rows = obj?["asks"] as? [[String: Any]] else { return nil }
        return rows.compactMap { row in
            guard let id = row["source_id"] as? String, !id.isEmpty,
                  let title = row["title"] as? String, !title.isEmpty,
                  (row["actions"] as? [String]).map({ Set($0).isSuperset(of: ["keep", "delete"]) }) ?? true else { return nil }
            return StaleReminderAsk(sourceID: id, title: title, context: row["context"] as? String ?? "",
                                    ageDays: (row["age_days"] as? NSNumber)?.intValue)
        }
    }

    /// What a failed Keep or Delete says, in plain words. The service's own
    /// detail wins for the answers its API defines (404, 409, 502). Anything else
    /// on a Delete (a 500 can follow a delete that happened) is unknown, so it
    /// never reads as "nothing was changed" or "try again".
    static func failure(status: Int, detail: String?, deleting: Bool) -> String {
        if let detail, !detail.isEmpty, [404, 409, 502].contains(status) { return detail }
        switch status {
        case 404: return "That reminder is no longer in the list."
        case 409 where deleting: return "Wisp couldn't confirm whether it was deleted. Check Reminders before trying again."
        case 409: return "That reminder can't be answered right now."
        case 502: return "Reminders didn't delete it."
        default:
            return deleting ? "Wisp couldn't confirm whether it was deleted. Check Reminders before trying again."
                : "Couldn't keep it. Try again."
        }
    }
}

/// The Day tab's stale-reminder questions. Reads and answers through its own
/// transport so the render fixture never reaches the service. Delete runs only
/// from the person's click on that reminder's Delete.
@MainActor
final class StaleRemindersModel: ObservableObject {
    @Published private(set) var asks: [StaleReminderAsk] = []
    /// The reminder whose Keep or Delete is waiting for the service.
    @Published private(set) var answering: String?
    /// A failed answer's message, by reminder.
    @Published private(set) var failures: [String: String] = [:]
    /// Reminders whose Delete failed or ended unknown: no second Delete from here,
    /// so a retry can never act on something that already changed.
    @Published private(set) var deleteBlocked: Set<String> = []

    private let transport: (URLRequest) async throws -> (Data, URLResponse)
    private let baseURL: URL

    init(baseURL: URL,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) }) {
        self.baseURL = baseURL
        self.transport = transport
    }

    /// A failed read keeps what was shown.
    func refresh() async {
        var request = URLRequest(url: baseURL.appendingPathComponent("assistant/stale_reminders"))
        request.timeoutInterval = 15
        guard let (data, response) = try? await transport(request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let asks = StaleReminderAsk.parse((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])
        else { return }
        self.asks = asks
        failures = failures.filter { id, _ in asks.contains { $0.sourceID == id } }
    }

    func keep(_ ask: StaleReminderAsk) async { await answer(ask, action: "keep") }

    func delete(_ ask: StaleReminderAsk) async {
        guard !deleteBlocked.contains(ask.sourceID) else { return }
        await answer(ask, action: "delete")
    }

    private func answer(_ ask: StaleReminderAsk, action: String) async {
        guard answering == nil else { return }
        answering = ask.sourceID
        failures[ask.sourceID] = nil
        defer { answering = nil }
        var request = URLRequest(url: baseURL.appendingPathComponent("assistant/stale_reminders/\(action)"))
        request.httpMethod = "POST"
        request.timeoutInterval = action == "delete" ? 30 : 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["source_id": ask.sourceID])
        let deleting = action == "delete"
        do {
            let (data, response) = try await transport(request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 200 {
                asks.removeAll { $0.sourceID == ask.sourceID }
                await refresh()
                return
            }
            let detail = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["detail"] as? String
            failures[ask.sourceID] = StaleReminderAsk.failure(status: status, detail: detail, deleting: deleting)
            if deleting { deleteBlocked.insert(ask.sourceID) }
            if status == 404 { await refresh() }
        } catch {
            // No reply: a delete may or may not have happened.
            failures[ask.sourceID] = deleting
                ? "Wisp didn't hear back, so it may or may not be deleted. Check Reminders before trying again."
                : "Couldn't reach Wisp. Try again."
            if deleting { deleteBlocked.insert(ask.sourceID) }
        }
    }
}
