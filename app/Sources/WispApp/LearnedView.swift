import SwiftUI
import AppKit

private extension Dictionary where Key == String, Value == Any {
    var selfID: String { self["id"] as? String ?? "unknown" }
}

// "What Wisp has learned": every routine the behavior engine (service/behavior)
// mined, grouped by kind, with the counts behind it, plus today's predictions.
// The user can confirm a routine or mark it "Not true"; a rejected routine stops
// driving predictions, nudges and the profile card until it is restored.

struct LearnedPattern: Identifiable, Equatable {
    let id: String
    let kind: String
    let statement: String
    let support: Int
    let opportunities: Int
    let confidence: Double
    let lastSeen: Double?
    let status: String

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, let kind = d["kind"] as? String,
              let statement = d["statement"] as? String else { return nil }
        self.id = id
        self.kind = kind
        self.statement = statement
        support = (d["support"] as? NSNumber)?.intValue ?? 0
        opportunities = (d["opportunities"] as? NSNumber)?.intValue ?? 0
        confidence = (d["confidence"] as? NSNumber)?.doubleValue ?? 0
        lastSeen = (d["last_seen"] as? NSNumber)?.doubleValue
        status = d["status"] as? String ?? "active"
    }

    /// "6 of the last 15 times". Counts are the evidence; no model wrote them.
    var evidence: String {
        guard opportunities > 0 else { return "" }
        return "\(support) of the last \(opportunities) times"
    }

    var strength: String {
        confidence >= 0.6 ? "Strong" : confidence >= 0.35 ? "Likely" : "Emerging"
    }
}

struct LearnedPrediction: Identifiable {
    let id: String
    let kind: String
    let subject: String
    let ts: Double
    let because: String
    let probability: Double

    init?(_ d: [String: Any], index: Int) {
        guard let kind = d["kind"] as? String, let subject = d["subject"] as? String,
              let ts = (d["ts"] as? NSNumber)?.doubleValue else { return nil }
        id = "\(index)|\(kind)|\(subject)|\(ts)"
        self.kind = kind
        self.subject = subject
        self.ts = ts
        because = d["because"] as? String ?? ""
        probability = (d["probability"] as? NSNumber)?.doubleValue ?? 0
    }

    /// Same wording as the morning preview (service/behavior/predict.py _USAGE_PHRASES).
    static let usagePhrases: [String: String] = [
        "brief": "your brief", "calendar": "checking your calendar", "reminders": "your reminders",
        "email": "checking email", "messages": "your messages", "weather": "checking the weather",
        "notes": "your notes", "news": "the news", "music": "music", "web": "a web lookup",
    ]

    var headline: String {
        switch kind {
        case "cancel_risk": return "The schedule for \(subject) may change"
        case "habit_conflict": return "\(subject) clashes with a routine"
        case "contact": return "Possible contact window with \(subject)"
        case "usage":
            let phrase = Self.usagePhrases[subject] ?? "asking about \(subject)"
            return phrase.prefix(1).uppercased() + phrase.dropFirst() + " with Wisp"
        default: return subject
        }
    }
}

enum LearnedWeeklyValue {
    static func unknownReason(_ metric: [String: Any]) -> String {
        switch metric["value_reason"] as? String {
        case "incomplete_calendar_coverage": return "Cannot calculate while Calendar coverage is incomplete."
        case "missing_event_end_times": return "Cannot calculate while event end times are missing."
        default: return "Cannot calculate from the available evidence."
        }
    }
}

/// Section order and wording. Kinds the app doesn't know yet land in "Other".
enum LearnedSection: String, CaseIterable, Identifiable {
    case routines, people, days, changes, wisp, other
    var id: String { rawValue }

    static func of(_ kind: String) -> LearnedSection {
        switch kind {
        case "habit": return .routines
        case "contact_rhythm": return .people
        case "day_shape": return .days
        case "cancel_propensity", "habit_protection": return .changes
        case "wisp_usage": return .wisp
        default: return .other
        }
    }

    var title: String {
        switch self {
        case .routines: return "Routines"
        case .people: return "People"
        case .days: return "Your days"
        case .changes: return "Cancelling and moving"
        case .wisp: return "How you use Wisp"
        case .other: return "Other"
        }
    }

    var symbol: String {
        switch self {
        case .routines: return "repeat"
        case .people: return "person.2"
        case .days: return "calendar"
        case .changes: return "arrow.uturn.backward"
        case .wisp: return "sparkles"
        case .other: return "circle.grid.2x2"
        }
    }
}

@MainActor
final class LearnedModel: ObservableObject {
    enum Phase: Equatable { case loading, off, ready, failed(String) }

    @Published var phase: Phase = .loading
    @Published var patterns: [LearnedPattern] = []
    @Published var today: [LearnedPrediction] = []
    @Published var sources: [String: Int] = [:]
    @Published var lastMined: Double?
    @Published var weekly: [String: Any] = [:]
    @Published var history: [String: Any] = [:]
    @Published var busy: Set<String> = []
    @Published var error = ""
    private let client = WispClient()
    private var revision = 0

    var active: [LearnedPattern] { patterns.filter { $0.status != "rejected" } }
    var rejected: [LearnedPattern] { patterns.filter { $0.status == "rejected" } }

    func refresh() async {
        revision += 1
        let requested = revision
        guard let settings = await client.behaviorSettings() else {
            if requested == revision { phase = .failed("Couldn't reach Wisp. Is it running?") }
            return
        }
        guard settings["enabled"] as? Bool ?? true else {
            if requested == revision { phase = .off; patterns = []; today = [] }
            return
        }
        async let status = client.behaviorGet("status")
        async let listed = client.behaviorGet("patterns")
        async let predicted = client.behaviorPredictions(day: "today")
        async let measured = client.behaviorGet("weekly")
        async let scanned = client.behaviorGet("history")
        let (s, l, p, w, h) = await (status, listed, predicted, measured, scanned)
        guard requested == revision else { return }
        guard let l, let rows = l["patterns"] as? [[String: Any]] else {
            phase = .failed("Couldn't load what Wisp learned. Try Refresh.")
            return
        }
        patterns = rows.compactMap(LearnedPattern.init)
            .sorted { ($0.status == "confirmed" ? 0 : 1, -$0.confidence) < ($1.status == "confirmed" ? 0 : 1, -$1.confidence) }
        let preds = (p?["predictions"] as? [[String: Any]]) ?? []
        let now = Date().timeIntervalSince1970
        today = preds.enumerated().compactMap { LearnedPrediction($1, index: $0) }
            .filter { $0.ts >= now || $0.kind == "contact" }
        let engine = s?["engine"] as? [String: Any]
        sources = (engine?["observations"] as? [String: Any] ?? [:])
            .compactMapValues { ($0 as? NSNumber)?.intValue }
        lastMined = (engine?["last_mined_at"] as? NSNumber)?.doubleValue
        weekly = w ?? [:]
        history = h?["sources"] as? [String: Any] ?? [:]
        phase = .ready
    }

    func historyControl(_ source: String, command: String) async {
        if await client.behaviorHistoryControl(source, command: command) { await refresh() }
        else { error = "Couldn't update the history scan. Try again." }
    }

    func set(_ pattern: LearnedPattern, to status: String) async {
        busy.insert(pattern.id)
        defer { busy.remove(pattern.id) }
        if await client.setBehaviorPatternStatus(pattern.id, status: status) {
            error = ""
            await refresh()
        } else {
            error = "Couldn't update that routine. Try again."
        }
    }

    /// "From 70 calendar events, 1,886 messages and 512 Wisp requests."
    var sourceLine: String {
        let fmt = NumberFormatter()
        fmt.numberStyle = .decimal
        func n(_ v: Int) -> String { fmt.string(from: NSNumber(value: v)) ?? "\(v)" }
        let calendar = (sources["event"] ?? 0) + (sources["event_cancelled"] ?? 0) + (sources["event_moved"] ?? 0)
        let messages = (sources["message_in"] ?? 0) + (sources["message_out"] ?? 0)
        let asks = sources["wisp_ask"] ?? 0
        var parts: [String] = []
        if calendar > 0 { parts.append("\(n(calendar)) calendar event\(calendar == 1 ? "" : "s")") }
        if messages > 0 { parts.append("\(n(messages)) message\(messages == 1 ? "" : "s")") }
        if asks > 0 { parts.append("\(n(asks)) Wisp request\(asks == 1 ? "" : "s")") }
        guard !parts.isEmpty else { return "Nothing observed yet." }
        let joined = parts.count == 1 ? parts[0]
            : parts.dropLast().joined(separator: ", ") + " and " + parts.last!
        return "Learned on this Mac from \(joined)."
    }
}

final class LearnedWindow {
    static let shared = LearnedWindow()
    private var window: NSWindow?

    func show() {
        if let window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "What Wisp has learned"
        window.contentView = NSHostingView(rootView: LearnedView())
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct LearnedView: View {
    @StateObject private var model = LearnedModel()
    @State private var showRejected = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header.padding(20)
            Divider()
            content
        }
        .frame(minWidth: 560, minHeight: 460)
        .task { await model.refresh() }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text("What Wisp has learned").font(.title2.bold())
                if model.phase == .ready {
                    Text(model.sourceLine).foregroundStyle(.secondary)
                    if let mined = model.lastMined {
                        Text("Updated \(Date(timeIntervalSince1970: mined).formatted(.relative(presentation: .named)))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
            Button("Refresh") { Task { await model.refresh() } }
        }
    }

    @ViewBuilder private var content: some View {
        switch model.phase {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .off:
            message("Learning is off",
                    "Turn on \"Learn my routines\" in Settings and Wisp will start noticing your routines from Calendar, Messages and how you use Wisp. It stays on this Mac.")
        case .failed(let text):
            message("Nothing to show", text)
        case .ready:
            list
        }
    }

    private var list: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 18) {
                weeklySection
                historySection
                if model.patterns.isEmpty {
                    Text("Wisp can estimate routines from existing accessible history. Results appear as sources finish syncing; completion remains unknown unless recorded.")
                        .foregroundStyle(.secondary)
                }
                if !model.today.isEmpty { todaySection }
                ForEach(LearnedSection.allCases) { section in
                    let rows = model.active.filter { LearnedSection.of($0.kind) == section }
                    if !rows.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Label(section.title, systemImage: section.symbol).font(.headline)
                            ForEach(rows) { row($0) }
                        }
                    }
                }
                if !model.rejected.isEmpty {
                    DisclosureGroup("Marked not true (\(model.rejected.count))", isExpanded: $showRejected) {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(model.rejected) { row($0) }
                        }.padding(.top, 6)
                    }
                }
                if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
                Text("Calendar patterns describe scheduled activities. Recorded completions and observed interactions are separate evidence. "
                     + "\"Not true\" stops Wisp from using it for predictions, nudges and answers. "
                     + "To erase everything, use \"Forget what Wisp learned…\" in Settings.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(20)
        }
    }

    private var weeklySection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Your last seven complete days", systemImage: "chart.bar").font(.headline)
            if let first = model.weekly["start_date"] as? String,
               let end = model.weekly["end_date_exclusive"] as? String {
                Text("\(first) to \(end) · ending at midnight")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(model.weekly["metrics"] as? [[String: Any]] ?? [], id: \.selfID) { metric in
                VStack(alignment: .leading, spacing: 2) {
                    Text(metric["label"] as? String ?? "Activity").font(.subheadline)
                    let unit = metric["unit"] as? String ?? ""
                    let coverage = (metric["coverage"] as? NSNumber)?.doubleValue ?? 0
                    if let value = (metric["value"] as? NSNumber)?.doubleValue {
                        Text("\(value.formatted(.number.precision(.fractionLength(0...1)))) \(unit) observed · \(Int(coverage * 100))% of period covered")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text(LearnedWeeklyValue.unknownReason(metric)).font(.caption).foregroundStyle(.secondary)
                    }
                    if let baseline = metric["baseline_weekly_average"] as? NSNumber,
                       let change = metric["change"] as? NSNumber {
                        Text("Prior four-week average: \(baseline.doubleValue.formatted(.number.precision(.fractionLength(0...1)))) · change: \(change.doubleValue.formatted(.number.sign(strategy: .always()).precision(.fractionLength(0...1))))")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("More complete history is needed for comparison.").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Text("Unbooked calendar time does not establish focused work. Missing history is not zero activity.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private var historySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("History scan", systemImage: "clock.arrow.circlepath").font(.headline)
            ForEach(["calendar", "completed_reminders"], id: \.self) { source in
                let job = model.history[source] as? [String: Any]
                let state = job?["state"] as? String ?? "waiting"
                HStack {
                    VStack(alignment: .leading) {
                        Text(source == "calendar" ? "Calendar plans" : "Completed reminders")
                        Text(state == "waiting" ? "Waiting for source access and sync" : state.capitalized)
                            .font(.caption).foregroundStyle(.secondary)
                        if let count = job?["rows"] as? Int {
                            Text("\(count) records scanned; recent evidence informs current routines")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        if let error = job?["error"] as? String, !error.isEmpty {
                            Text(error).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    Button(state == "scanning" ? "Pause" : state == "complete" ? "Scan again" : "Resume") {
                        Task { await model.historyControl(source, command: state == "scanning" ? "pause" : state == "complete" ? "restart" : "resume") }
                    }
                }
            }
            Text("Reads records currently accessible to Calendar and Reminders. Deleted records and past app usage cannot be reconstructed. Older records remain subject to retention.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var todaySection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Coming up today", systemImage: "sun.max").font(.headline)
            ForEach(model.today) { p in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(Date(timeIntervalSince1970: p.ts).formatted(date: .omitted, time: .shortened))
                        .font(.system(.body, design: .monospaced)).foregroundStyle(.secondary)
                        .frame(width: 78, alignment: .leading)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(p.headline)
                        if !p.because.isEmpty {
                            Text(p.because).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private func row(_ p: LearnedPattern) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(p.statement).textSelection(.enabled)
            HStack(spacing: 8) {
                Text(p.strength)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
                if !p.evidence.isEmpty { Text(p.evidence).font(.caption).foregroundStyle(.secondary) }
                if let seen = p.lastSeen {
                    Text("· last seen \(Date(timeIntervalSince1970: seen).formatted(.relative(presentation: .named)))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                actions(p)
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
        .disabled(model.busy.contains(p.id))
    }

    @ViewBuilder private func actions(_ p: LearnedPattern) -> some View {
        switch p.status {
        case "rejected":
            Button("Restore") { Task { await model.set(p, to: "active") } }
        case "confirmed":
            Label("You confirmed", systemImage: "checkmark").font(.caption).foregroundStyle(.secondary)
            Button("Not true") { Task { await model.set(p, to: "rejected") } }
        default:
            Button("That's right") { Task { await model.set(p, to: "confirmed") } }
            Button("Not true") { Task { await model.set(p, to: "rejected") } }
        }
    }

    private func message(_ title: String, _ body: String) -> some View {
        VStack(spacing: 8) {
            Text(title).font(.headline)
            Text(body).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }
}
