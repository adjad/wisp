import Combine
import Foundation

/// One long-term reminder Wisp found in older mail (service/mail_memory), shown on the
/// Brief only when mail suggestions are switched on ("live"). Display text only:
/// nothing here acts until the person clicks Add to Reminders.
struct MailSuggestion: Identifiable, Equatable {
    let id: Int
    let what: String
    let due: String          // YYYY-MM-DD
    let precision: String    // day, week or month
    let quote: String
    let from: String         // "you" for the person's own sent mail
    let subject: String
    let messageID: String

    static func parse(_ reply: [String: Any]?) -> (mode: String, items: [MailSuggestion])? {
        guard let reply, let mode = reply["mode"] as? String, let rows = reply["suggestions"] as? [[String: Any]]
        else { return nil }
        let items = rows.compactMap { r -> MailSuggestion? in
            guard let id = (r["id"] as? NSNumber)?.intValue, let what = r["what"] as? String,
                  !what.trimmingCharacters(in: .whitespaces).isEmpty,
                  let due = r["due"] as? String, MailSuggestion.day(due) != nil,
                  let actions = r["actions"] as? [String], actions.contains("accept"), actions.contains("dismiss")
            else { return nil }
            return MailSuggestion(id: id, what: what, due: due, precision: r["precision"] as? String ?? "day",
                                  quote: r["quote"] as? String ?? "", from: r["from"] as? String ?? "",
                                  subject: r["subject"] as? String ?? "", messageID: r["message_id"] as? String ?? "")
        }
        return (mode, items)
    }

    static func day(_ iso: String) -> DateComponents? {
        let parts = iso.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, (1...12).contains(parts[1]), (1...31).contains(parts[2]) else { return nil }
        return DateComponents(year: parts[0], month: parts[1], day: parts[2])
    }

    /// "Fri, Jul 2, 2027", "in October", "week of Jun 15" — in the person's calendar.
    func dueLabel(now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        guard let parts = MailSuggestion.day(due), let date = calendar.date(from: parts) else { return due }
        let format = DateFormatter()
        format.locale = locale
        format.calendar = calendar
        switch precision {
        case "month":
            format.setLocalizedDateFormatFromTemplate("MMMM")
            return "in \(format.string(from: date))"
        case "week":
            let monday = calendar.date(byAdding: .day, value: -6, to: date) ?? date
            format.setLocalizedDateFormatFromTemplate("MMMd")
            return "week of \(format.string(from: monday))"
        default:
            let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
            format.setLocalizedDateFormatFromTemplate(sameYear ? "EEEMMMd" : "EEEMMMdyyyy")
            return format.string(from: date)
        }
    }

    var detail: String {
        let source = from == "you" ? "From your email" : (from.isEmpty ? "From an email" : "From \(from)")
        return subject.isEmpty ? source : "\(source) · “\(subject)”"
    }

    /// ASCII letters, digits and `@.-_+=$` pass through; everything else is percent-encoded.
    private static let linkSafe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@.-_+=$")

    /// Mail's own link to this message, or nil when its Message-ID isn't known.
    var mailURL: URL? {
        let bare = messageID.trimmingCharacters(in: CharacterSet(charactersIn: "<> "))
        guard !bare.isEmpty, !bare.contains(" "),
              let encoded = bare.addingPercentEncoding(withAllowedCharacters: MailSuggestion.linkSafe)
        else { return nil }
        return URL(string: "message://%3C\(encoded)%3E")
    }

    static func failure(status: Int, detail: String?) -> String {
        if let detail, !detail.isEmpty, [403, 409, 502, 503].contains(status) { return detail }
        return status == 404 ? "That suggestion is no longer open." : "Couldn't add it. Try again."
    }
}

/// The Brief's mail suggestions. Reads and answers through its own transport so
/// checks never reach the service. Add runs only from the person's click.
@MainActor
final class MailMemoryModel: ObservableObject {
    @Published private(set) var suggestions: [MailSuggestion] = []
    @Published private(set) var mode = "shadow"
    /// True once the service has answered, so Settings can tell "off" from "not loaded".
    @Published private(set) var known = false
    @Published private(set) var modeChanging = false
    @Published private(set) var modeStatus: String?
    @Published private(set) var answering: Int?
    @Published private(set) var failures: [Int: String] = [:]
    /// What the last Add did, said once under the list ("Added to Reminders.").
    @Published private(set) var confirmation: String?

    /// A confirmation older than this is gone by the next refresh.
    static let confirmationLifetime: TimeInterval = 8

    private let transport: (URLRequest) async throws -> (Data, URLResponse)
    private let baseURL: URL
    private let clock: () -> Date
    private var confirmedAt: Date?

    init(baseURL: URL,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) },
         clock: @escaping () -> Date = { Date() }) {
        self.baseURL = baseURL
        self.transport = transport
        self.clock = clock
    }

    private func confirm(_ text: String?) {
        confirmation = text
        confirmedAt = text == nil ? nil : clock()
    }

    /// A failed read keeps what was shown. A confirmation older than eight seconds is dropped.
    func refresh() async {
        if let at = confirmedAt, clock().timeIntervalSince(at) > Self.confirmationLifetime { confirm(nil) }
        var request = URLRequest(url: baseURL.appendingPathComponent("assistant/mail_memory"))
        request.timeoutInterval = 15
        guard let (data, response) = try? await transport(request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let parsed = MailSuggestion.parse((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])
        else { return }
        mode = parsed.mode
        known = true
        suggestions = parsed.mode == "live" ? parsed.items : []
        failures = failures.filter { id, _ in suggestions.contains { $0.id == id } }
    }

    static let modes = ["live", "shadow", "off"]

    /// Settings' switch: "live" shows important suggestions, "shadow" records only, "off" stops.
    func setMode(_ value: String) async {
        guard !modeChanging, Self.modes.contains(value) else { return }
        modeChanging = true
        modeStatus = nil
        defer { modeChanging = false }
        var request = URLRequest(url: baseURL.appendingPathComponent("assistant/mail_memory/settings"))
        request.httpMethod = "PUT"
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["mode": value])
        guard let (data, response) = try? await transport(request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["mode"] as? String == value
        else {
            modeStatus = "Couldn't change it. Try again."
            return
        }
        mode = value
        known = true
        if value != "live" { suggestions = [] }
        await refresh()
    }

    func accept(_ item: MailSuggestion) async { await answer(item, action: "accept") }
    func dismiss(_ item: MailSuggestion) async { await answer(item, action: "dismiss") }

    private func answer(_ item: MailSuggestion, action: String) async {
        guard answering == nil else { return }
        answering = item.id
        failures[item.id] = nil
        confirm(nil)
        defer { answering = nil }
        var request = URLRequest(url: baseURL.appendingPathComponent("assistant/mail_memory/\(action)"))
        request.httpMethod = "POST"
        request.timeoutInterval = action == "accept" ? 30 : 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["id": item.id])
        do {
            let (data, response) = try await transport(request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if status == 200 {
                if action == "accept" {
                    confirm(body?["decision"] as? String == "already_on_file"
                        ? "“\(item.what)” is already in Reminders or Calendar."
                        : "Added “\(item.what)” to Reminders.")
                }
                suggestions.removeAll { $0.id == item.id }
                await refresh()
                return
            }
            failures[item.id] = MailSuggestion.failure(status: status, detail: body?["detail"] as? String)
            if status == 404 { await refresh() }
        } catch {
            // No reply: an Add may or may not have happened, so don't invite a second one blindly.
            failures[item.id] = action == "accept"
                ? "Wisp didn't hear back. Check Reminders before adding it again."
                : "Couldn't reach Wisp. Try again."
        }
    }
}
