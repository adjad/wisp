import SwiftUI

struct DailyAttentionItem: Decodable, Identifiable {
    let id: String
    let source: String
    let title: String
    let reason: String
    let quote: String
    let due_ts: Double?
    let all_day: Bool?
    let due_date: String?
    let priority: Int
    let reference: String
    let suggested_minutes: Int
    let needs_review: Bool
    let task_kind: String?
    let ownership: String?
    /// Email and Messages only: who it's from, whether that looks like a
    /// person, when it came, and whether you've texted that conversation since.
    let sender: String?
    let person: Bool?
    let received_ts: Double?
    let replied: Bool?
}

struct DailySourceCoverage: Decodable, Identifiable {
    let source: String
    let ready: Bool
    let detail: String
    let inspected: Int
    let omitted: Int
    /// What the user must grant before this source can be read: "full_disk_access".
    var blocked: String? = nil
    var id: String { source }
}

struct DailyProposal: Decodable {
    let blocks: [TodayBlock]
    let unscheduled: [TodayUnscheduled]
    let warnings: [String]
}

struct DailyWorkspace: Decodable {
    let attention: [DailyAttentionItem]
    let attention_omitted: Int
    let source_coverage: [DailySourceCoverage]
    let behavior_detail: String
    let proposed_plan: DailyProposal
    let explanation: String
    /// The Brief's one-line MAIL and TEXTS overviews (service/assistant/mail_gists.py);
    /// absent from an older service, or for a source that couldn't be read.
    var mail_overview: DailyOverview? = nil
    var texts_overview: DailyOverview? = nil
    /// The written inbox summary under MAIL: a model's paragraph, or prose built without one.
    var mail_summary: DailyOverview? = nil
}

struct DailyOverview: Decodable, Equatable {
    let text: String
    /// "model" for a gist, "summary" for the sentence built without one.
    let source: String
}

struct DailyAttentionView: View {
    let workspace: DailyWorkspace?
    @ObservedObject var model: TodayModel
    @State private var durations: [String: Int] = [:]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                CaughtForYouSection()
                if let workspace {
                    Text("What needs your attention").font(.title2.bold())
                    Text("Due work, commitments and source evidence worth reviewing.").foregroundStyle(.secondary)
                    if workspace.attention.isEmpty {
                        Text("No attention items in the available evidence. Check source coverage below.")
                            .padding(.vertical, 12)
                    }
                    ForEach(workspace.attention) { item in
                        GroupBox {
                            VStack(alignment: .leading, spacing: 7) {
                                HStack(alignment: .top) {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(item.title).font(.headline).textSelection(.enabled)
                                        Text(item.source.capitalized + (item.needs_review ? " · Review suggestion" : ""))
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if item.source != "calendar" && item.source != "wisp" {
                                        Button("Add as task") {
                                            Task {
                                                _ = await model.add(title: item.needs_review && item.ownership != "user" ? "Review: " + item.title : item.title,
                                                    kind: item.task_kind ?? "task", minutes: durations[item.id] ?? item.suggested_minutes, priority: item.priority,
                                                    due: item.due_ts.flatMap { $0 > Date().timeIntervalSince1970 ? Date(timeIntervalSince1970: $0) : nil })
                                            }
                                        }.disabled(model.busy)
                                    }
                                }
                                Text(item.reason).font(.callout)
                                if item.source != "calendar" && item.source != "wisp" {
                                    Stepper("\(durations[item.id] ?? item.suggested_minutes) min estimate for your plan", value: Binding(
                                        get: { durations[item.id] ?? item.suggested_minutes },
                                        set: { durations[item.id] = $0 }), in: 1...480, step: 5)
                                        .font(.caption).frame(maxWidth: 330)
                                }
                                if let due = item.due_ts {
                                    if item.all_day == true {
                                        Text("All day · " + (item.due_date ?? Date(timeIntervalSince1970: due).formatted(.dateTime.month().day())))
                                            .font(.caption).foregroundStyle(.secondary)
                                    } else {
                                        Text(Date(timeIntervalSince1970: due), format: .dateTime.month().day().hour().minute().timeZone())
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                                DisclosureGroup("Source evidence") {
                                    VStack(alignment: .leading, spacing: 6) {
                                        if !item.quote.isEmpty { Text(item.quote).textSelection(.enabled) }
                                        Text(item.reference).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                                        Text("Use this reference to ask Wisp about the original source. Adding a task changes your Wisp plan.")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.top, 5)
                                }.font(.caption)
                            }.padding(7).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    if workspace.attention_omitted > 0 {
                        Text("\(workspace.attention_omitted) additional attention items are outside this bounded view.").font(.caption)
                    }
                    GroupBox("Source coverage") {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(workspace.source_coverage) { source in
                                VStack(alignment: .leading, spacing: 3) {
                                    Label(source.source.capitalized, systemImage: source.ready ? "checkmark.circle" : "clock.badge.exclamationmark")
                                    Text(source.detail).font(.caption).foregroundStyle(.secondary)
                                    if source.omitted > 0 { Text("\(source.omitted) source records not inspected or displayed.").font(.caption).foregroundStyle(.orange) }
                                }
                            }
                            Text(workspace.behavior_detail).font(.caption).foregroundStyle(.secondary)
                        }.padding(6).frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else {
                    ContentUnavailableView("Source evidence is unavailable", systemImage: "tray",
                        description: Text("Refresh to compile your sources. Your saved tasks remain in Day plan."))
                }
            }.padding(.trailing, 6)
        }.environment(\.timeZone, model.timezone)
    }
}

struct DailyEmailPreferences: Decodable {
    let enabled: Bool
    let revision: Int
    let recipient: String
    let timezone: String
    let minute: Int
    let start_day: String
}
struct DailyEmailResult: Decodable {
    let day: String
    let status: String
    let detail: String
}
struct DailyEmailHealth: Decodable { let state: String }
struct DailyEmailStatus: Decodable {
    let settings: DailyEmailPreferences
    let own_addresses: [String]
    let identity_ready: Bool
    let identity_detail: String
    let consent_current: Bool
    let delivery_allowed: Bool?
    let last_result: DailyEmailResult?
    let health: DailyEmailHealth?
}
struct DailyEmailPreview: Decodable {
    let token: String
    let subject: String
    let body: String
    let explanation: String
}

@MainActor
final class DailyEmailModel: ObservableObject {
    @Published var status: DailyEmailStatus?
    @Published var preview: DailyEmailPreview?
    @Published var error = ""
    @Published var busy = false
    @Published var recipient = ""
    @Published var timezone = TimeZone.current.identifier
    @Published var hour = 8
    @Published var minute = 0
    private var generation = 0

    func load(_ today: TodayModel) async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        generation += 1
        let ticket = generation
        do {
            let data = try await today.request("/assistant/today/workspace/email")
            let next = try JSONDecoder().decode(DailyEmailStatus.self, from: data)
            guard ticket == generation else { return }
            status = next
            if !next.own_addresses.contains(recipient) { recipient = next.own_addresses.contains(next.settings.recipient) ? next.settings.recipient : next.own_addresses.first ?? "" }
            if next.settings.revision > 0 {
                timezone = next.settings.timezone
                hour = next.settings.minute / 60
                minute = next.settings.minute % 60
            } else { timezone = today.timezone.identifier }
            error = ""
        } catch {
            guard ticket == generation else { return }
            self.error = error.localizedDescription
            status = nil
        }
    }

    func review(_ today: TodayModel) async {
        guard !busy else { return }
        busy = true
        generation += 1
        preview = nil
        do {
            let data = try await today.request("/assistant/today/workspace/email/preview", method: "POST",
                body: ["recipient": recipient, "timezone": timezone, "minute": hour * 60 + minute, "day": today.day])
            preview = try JSONDecoder().decode(DailyEmailPreview.self, from: data)
            error = ""
        } catch { self.error = error.localizedDescription }
        busy = false
    }

    func enable(_ today: TodayModel) async {
        guard !busy, let preview else { return }
        busy = true
        generation += 1
        var failure: String?
        do {
            _ = try await today.request("/assistant/today/workspace/email/enable", method: "POST",
                                        body: ["token": preview.token, "confirmed": true])
            self.preview = nil
            error = ""
        } catch { failure = error.localizedDescription }
        busy = false
        await load(today)
        if let failure { error = failure }
    }

    func pause(_ today: TodayModel) async {
        guard !busy, let status else { return }
        busy = true
        generation += 1
        var failure: String?
        do {
            _ = try await today.request("/assistant/today/workspace/email/pause", method: "POST", body: ["revision": status.settings.revision])
            preview = nil
            error = ""
        } catch { failure = error.localizedDescription }
        busy = false
        await load(today)
        if let failure { error = failure }
    }
}

struct DailyEmailSettingsView: View {
    @ObservedObject var today: TodayModel
    @StateObject private var delivery = DailyEmailModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Email my day plan daily").font(.title2.bold())
            Text("Each email is newly composed from Calendar, Reminders, email, Messages, Notes and your Wisp tasks, with source coverage and tentative behavioral holds. The preview is an example; future plans will change.")
                .font(.callout).foregroundStyle(.secondary)
            if let status = delivery.status {
                Label(status.settings.enabled ? "Daily email is enabled" : "Daily email is off", systemImage: status.settings.enabled ? "envelope.badge" : "envelope")
                Text(status.identity_detail).font(.caption).foregroundStyle(.secondary)
                if status.delivery_allowed == false {
                    Text("Wisp’s current access policy blocks email. Review its access settings before enabling automatic delivery.")
                        .font(.caption).foregroundStyle(.orange)
                }
                if status.settings.enabled && !status.consent_current {
                    Text("Connected-account eligibility changed. Review and enable again before another email can be submitted.")
                        .font(.caption).foregroundStyle(.orange)
                }
                Picker("My connected address", selection: $delivery.recipient) {
                    if status.own_addresses.isEmpty { Text("Waiting for connected Mail accounts").tag("") }
                    ForEach(status.own_addresses, id: \.self) { Text($0).tag($0) }
                }
                HStack {
                    Text("Every day at")
                    Picker("Hour", selection: $delivery.hour) { ForEach(0..<24) { Text(String(format: "%02d", $0)).tag($0) } }.frame(width: 80)
                    Text(":")
                    Picker("Minute", selection: $delivery.minute) { ForEach(0..<60) { Text(String(format: "%02d", $0)).tag($0) } }.frame(width: 80)
                    TextField("IANA timezone", text: $delivery.timezone)
                }
                Text("Wisp must be running and Mail connected. Late delivery is skipped after 30 minutes. A restart or changed account requires review again. Pausing stops future submissions; one already in progress may finish.")
                    .font(.caption).foregroundStyle(.secondary)
                if let result = status.last_result {
                    Text("Last plan (\(result.day)): \(result.status.replacingOccurrences(of: "_", with: " ")) — \(result.detail)")
                        .font(.caption).textSelection(.enabled)
                }
                if let health = status.health, ["waiting_for_sources", "waiting_for_self_identity", "blocked_by_policy", "connected_accounts_changed_review_settings"].contains(health.state) {
                    Text(health.state.replacingOccurrences(of: "_", with: " ").capitalized).font(.caption).foregroundStyle(.orange)
                }
            }
            if !delivery.error.isEmpty { Text(delivery.error).foregroundStyle(.red).font(.callout) }
            if let preview = delivery.preview {
                Text("Example email to \(delivery.recipient)").font(.headline)
                Text(preview.subject).bold()
                ScrollView { Text(preview.body).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
                    .frame(minHeight: 140, maxHeight: 250).padding(10).background(.quaternary.opacity(0.3)).clipShape(RoundedRectangle(cornerRadius: 8))
                Text(preview.explanation).font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Button("Close") { dismiss() }
                Button("Refresh accounts") { Task { await delivery.load(today) } }
                if delivery.status?.settings.enabled == true {
                    Button("Pause daily email") { Task { await delivery.pause(today) } }
                }
                Spacer()
                if delivery.preview != nil {
                    Button("Enable automatic daily self-email") { Task { await delivery.enable(today) } }.buttonStyle(.borderedProminent)
                        .disabled(delivery.status?.delivery_allowed == false)
                } else {
                    Button("Review example email") { Task { await delivery.review(today) } }
                        .disabled(delivery.recipient.isEmpty || delivery.status?.identity_ready != true)
                }
            }
        }
        .padding(24).frame(width: 760).disabled(delivery.busy)
        .task { await delivery.load(today) }
        .onChange(of: delivery.recipient) { delivery.preview = nil }
        .onChange(of: delivery.timezone) { delivery.preview = nil }
        .onChange(of: delivery.hour) { delivery.preview = nil }
        .onChange(of: delivery.minute) { delivery.preview = nil }
    }
}
