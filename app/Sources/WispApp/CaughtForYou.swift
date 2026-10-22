import SwiftUI

/// "Caught for you": what the attention runner (service/assistant/attention_runner.py)
/// noticed in your texts. A plan stated in a message that is on neither your calendar
/// nor your reminders gets a reminder: in Live mode Wisp adds it, in Preview mode
/// ("shadow" in the backend) it only lists what it would have added.
struct AttentionRecord: Decodable, Identifiable, Equatable {
    let source_id: String
    let state: String
    let title: String?
    let due_ts: Double?
    let event_ts: Double?
    let decided_at: Double
    let when: String?
    let quote: String?
    /// The backend's view of whether Undo can work (identity recorded, title on file).
    let undoable: Bool?
    let paired: Bool?
    let fingerprint: String?
    let captureable: Bool?
    let effects: [String: CaptureEffect]?

    struct CaptureEffect: Decodable, Equatable {
        let status: String
        let error: String?
    }
    var id: String { source_id }

    /// States worth showing. `already_on_file` means nothing needed doing.
    static let shown: Set<String> = ["created", "shadow", "capped", "unknown", "failed", "undone", "partial"]

    var label: String {
        if paired == true {
            let names = ["calendar": "Calendar", "reminder": "Reminder"]
            if let effects, !effects.isEmpty {
                let summary = state == "partial" ? "Partially added · " : (state == "unknown" ? "Check both apps · " : "")
                return summary + ["calendar", "reminder"].compactMap { kind in
                    guard let effect = effects[kind] else { return nil }
                    let status: String
                    switch effect.status {
                    case "succeeded": status = "added"
                    case "already_on_file": status = "already saved"
                    case "unknown", "claimed": status = "check app · outcome unknown"
                    case "needs_duration": status = "needs an end time"
                    case "blocked": status = "blocked"
                    default: status = "couldn't add"
                    }
                    return "\(names[kind] ?? kind): \(status)"
                }.joined(separator: " · ")
            }
            return "Calendar + reminder · preview"
        }
        switch state {
        case "created": return "Added to Reminders"
        case "shadow": return "Would add · preview"
        case "capped": return "Not added · daily limit reached"
        case "unknown": return "Outcome unknown · check Reminders"
        case "failed": return "Couldn't add"
        case "undone": return "Removed"
        default: return state
        }
    }
}

struct AttentionSettings: Decodable, Equatable {
    let mode: String
    let daily_cap: Int
    let capture_events: Bool?
}

struct AttentionStatus: Decodable, Equatable {
    let settings: AttentionSettings
    let created_today: Int
    let recent: [AttentionRecord]
}

@MainActor
final class AttentionModel: ObservableObject {
    @Published private(set) var status: AttentionStatus?
    @Published var error = ""
    @Published private(set) var busy = false
    var transport: (URLRequest) async throws -> (Data, URLResponse)
    private let now: () -> Date
    /// Each refresh's ticket. A response to an older request (a poll that left before an
    /// Undo or a switch) is dropped instead of overwriting the newer status.
    private var generation = 0

    init(now: @escaping () -> Date = Date.init,
         transport: @escaping (URLRequest) async throws -> (Data, URLResponse) = { try await URLSession.shared.data(for: $0) }) {
        self.now = now
        self.transport = transport
    }

    var captureEvents: Bool { status?.settings.capture_events == true }

    var live: Bool { status?.settings.mode == "live" }

    /// The last week's decisions that say something, newest first.
    var items: [AttentionRecord] {
        let floor = now().timeIntervalSince1970 - 7 * 86400
        return (status?.recent ?? [])
            .filter { AttentionRecord.shown.contains($0.state) && $0.decided_at >= floor }
            .sorted { $0.decided_at > $1.decided_at }
    }

    private func send(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else { throw URLError(.badURL) }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = path.hasSuffix("/capture") ? 120 : 15
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await transport(req)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NSError(domain: "Attention", code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                          userInfo: [NSLocalizedDescriptionKey: detail?["detail"] as? String ?? "Wisp's local service didn't answer."])
        }
        return data
    }

    /// `keepError` is for the reload after an action, so a refusal ("that reminder was changed
    /// after Wisp created it") stays on screen instead of being cleared by the reload.
    func refresh(keepError: Bool = false) async {
        generation += 1
        let ticket = generation
        do {
            let next = try JSONDecoder().decode(AttentionStatus.self, from: try await send("/assistant/attention"))
            guard ticket == generation else { return }
            status = next
            if !keepError { error = "" }
        } catch {
            guard ticket == generation, !Task.isCancelled else { return }
            if !keepError || self.error.isEmpty { self.error = error.localizedDescription }
        }
    }

    /// Live adds reminders; Preview only lists them. Switching into Live never acts on
    /// messages that arrived before the switch (the backend re-baselines).
    func setLive(_ live: Bool) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await send("/assistant/attention/settings", method: "PUT", body: ["mode": live ? "live" : "shadow"])
            error = ""
        } catch {
            self.error = error.localizedDescription
        }
        await refresh(keepError: true)
    }

    func setCaptureEvents(_ enabled: Bool) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await send("/assistant/attention/settings", method: "PUT", body: ["capture_events": enabled])
            error = ""
        } catch { self.error = error.localizedDescription }
        await refresh(keepError: true)
    }

    func capture(_ record: AttentionRecord) async {
        guard !busy, record.captureable == true, let fingerprint = record.fingerprint else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await send("/assistant/attention/capture", method: "POST",
                               body: ["source_id": record.source_id, "fingerprint": fingerprint])
            error = ""
        } catch { self.error = error.localizedDescription }
        await refresh(keepError: true)
    }

    /// Removes exactly the reminder this record created (the backend refuses anything else).
    func undo(_ record: AttentionRecord) async {
        guard !busy, record.state == "created", record.undoable == true else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await send("/assistant/attention/undo", method: "POST", body: ["source_id": record.source_id])
            error = ""
        } catch {
            self.error = error.localizedDescription
        }
        await refresh(keepError: true)
    }
}

struct CaughtForYouSection: View {
    @StateObject private var model: AttentionModel
    /// The notch shows at most three rows and no explanation paragraph.
    let compact: Bool
    @State private var reviewedCapture: AttentionRecord?

    @MainActor
    init(compact: Bool = false) { self.init(model: AttentionModel(), compact: compact) }

    @MainActor
    init(model: AttentionModel, compact: Bool = false) {
        _model = StateObject(wrappedValue: model)
        self.compact = compact
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Caught for you").font(compact ? .system(size: 13, weight: .semibold) : .title3.bold())
                Spacer()
                if let status = model.status, status.settings.mode != "off" {
                    Toggle("Add reminders automatically", isOn: Binding(
                        get: { model.live }, set: { value in Task { await model.setLive(value) } }))
                        .toggleStyle(.switch).controlSize(.mini).font(.caption)
                        .disabled(model.busy)
                        .help("Off: Wisp only lists what it would add. On: it adds the reminder (at most \(status.settings.daily_cap) a day) and tells you.")
                }
            }
            if model.status?.settings.mode != "off" {
                Toggle("Also capture Calendar events from mail and texts", isOn: Binding(
                    get: { model.captureEvents }, set: { value in Task { await model.setCaptureEvents(value) } }))
                    .toggleStyle(.switch).controlSize(.mini).font(.caption).disabled(model.busy)
                    .help("With automatic reminders on, new confirmed plans also create Calendar events. Existing plans stay previews. Dates without times become all-day events; the reminder uses the shown default hour.")
                if model.captureEvents && !model.live {
                    Text("Calendar capture is in preview until automatic reminders are on.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if !model.error.isEmpty {
                Text(model.error).font(.caption).foregroundStyle(.orange)
            } else if model.status?.settings.mode == "off" {
                Text("Off. Wisp isn't watching mail or texts for plans.").font(.caption).foregroundStyle(.secondary)
            } else if model.items.isEmpty {
                Text(model.live
                     ? "Nothing caught this week. When a text states a plan that isn't on your calendar or reminders, Wisp adds a reminder and it shows here."
                     : "Nothing caught this week. Wisp is watching your texts for plans that aren't on your calendar; it lists them here before you let it add any.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(compact ? Array(model.items.prefix(3)) : model.items) { item in row(item) }
        }
        .alert("Add Calendar event and reminder?", isPresented: Binding(
            get: { reviewedCapture != nil }, set: { if !$0 { reviewedCapture = nil } })) {
                Button("Add") { if let item = reviewedCapture { Task { await model.capture(item) } }; reviewedCapture = nil }
                Button("Cancel", role: .cancel) { reviewedCapture = nil }
            } message: {
                Text([reviewedCapture?.title, reviewedCapture?.when].compactMap { $0 }.joined(separator: "\n"))
            }
        .task {
            while !Task.isCancelled {
                await model.refresh()
                do { try await Task.sleep(for: .seconds(60)) } catch { break }
            }
        }
    }

    private func row(_ item: AttentionRecord) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title ?? "A plan from your messages")
                    .font(compact ? .system(size: 13, weight: .medium) : .headline).lineLimit(2)
                Text([item.label, item.when].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(compact ? 2 : 3)
                if let quote = item.quote, !quote.isEmpty {
                    Text("“\(quote)”").font(.caption).italic().foregroundStyle(.secondary)
                        .lineLimit(compact ? 1 : 3).textSelection(.enabled)
                }
            }
            Spacer(minLength: 8)
            if item.captureable == true {
                Button("Review & add") { reviewedCapture = item }
                    .disabled(model.busy)
            }
            if item.state == "created" && item.undoable == true {
                Button("Undo") { Task { await model.undo(item) } }
                    .disabled(model.busy)
                    .help("Removes the reminder Wisp added for this message, and nothing else.")
            }
        }
    }
}
