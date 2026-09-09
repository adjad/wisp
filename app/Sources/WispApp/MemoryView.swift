import SwiftUI
import AppKit

struct MemoryFact: Decodable, Identifiable {
    let id: Int
    let text: String
    let category: String
    let status: String
    let origin: String
    let pinned: Int
    let observed_at: Double
    let explanation: String
    let evidence: [MemoryEvidence]?
}

struct MemoryEvidence: Decodable, Identifiable {
    let id: Int
    let source_type: String
    let source_id: String
    let session_id: String?
    let turn_idx: Int?
    let quote: String
    let observed_at: Double
    let label: String
}

struct MemoryStatus: Decodable {
    let capture_enabled: Bool
    let jobs: [String: Int]
    let worker: String
    let count: Int
    let proposals: Int
    let unreviewed: Int
}

struct MemoryInvestigation: Decodable, Identifiable {
    let id: Int
    let question: String
    let model_role: String
    let status: String
    let result: String
    let error: String
}

@MainActor
final class MemoryModel: ObservableObject {
    @Published var facts: [MemoryFact] = []
    @Published var status: MemoryStatus?
    @Published var investigations: [MemoryInvestigation] = []
    @Published var error = ""
    @Published var busy = false
    var filter = "active"
    private struct FactList: Decodable { let facts: [MemoryFact] }
    private struct FactDetail: Decodable { let fact: MemoryFact }
    private struct InvestigationList: Decodable { let investigations: [MemoryInvestigation] }

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else { throw URLError(.badURL) }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NSError(domain: "WispMemory", code: 1, userInfo: [NSLocalizedDescriptionKey:
                obj?["detail"] as? String ?? "Memory is unavailable. Check that the updated Wisp backend is running."])
        }
        return data
    }

    func refresh() async {
        do {
            let decoder = JSONDecoder()
            status = try decoder.decode(MemoryStatus.self, from: await request("/memory/status"))
            facts = try decoder.decode(FactList.self, from: await request("/memory/facts?status=\(filter)")).facts
            investigations = try decoder.decode(InvestigationList.self, from: await request("/memory/investigations")).investigations
        } catch { self.error = error.localizedDescription }
    }

    func mutate(_ path: String, method: String = "POST", body: [String: Any] = [:]) async {
        guard !busy else { return }
        busy = true
        error = ""
        await PendingConfigWrites.shared.begin()
        defer { busy = false }
        do {
            let data = try await request(path, method: method, body: body)
            if let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], obj["ok"] as? Bool == false {
                throw NSError(domain: "WispMemory", code: 2, userInfo: [NSLocalizedDescriptionKey: "That memory changed. Refresh and try again."])
            }
            await refresh()
        } catch { self.error = error.localizedDescription }
        await PendingConfigWrites.shared.end()
    }

    func detail(_ id: Int) async throws -> MemoryFact {
        try JSONDecoder().decode(FactDetail.self, from: await request("/memory/facts/\(id)")).fact
    }
}

@MainActor
final class MemoryWindow {
    static let shared = MemoryWindow()
    private var window: NSWindow?
    func show() {
        if window == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 700),
                               styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            win.title = "Wisp Memory"
            win.contentView = NSHostingView(rootView: MemoryView())
            win.minSize = NSSize(width: 640, height: 500)
            win.isReleasedWhenClosed = false
            win.center()
            window = win
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}

struct MemoryView: View {
    @StateObject private var model = MemoryModel()
    @State private var filter = "active"
    @State private var query = ""
    @State private var investigation = ""
    @State private var investigationModel = "agent"
    @State private var selected: MemoryFact?
    @State private var correction = ""
    @State private var sourceText: String?
    @State private var pilotSize = 20

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Memory").font(.largeTitle.weight(.semibold))
                    Text("What Wisp remembers, and the evidence behind it.").foregroundStyle(.secondary)
                }
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("Refresh") { Task { model.error = ""; await model.refresh() } }
            }
            if let status = model.status {
                Toggle("Remember clear personal facts from new conversations", isOn: Binding(
                    get: { status.capture_enabled },
                    set: { value in Task { await model.mutate("/memory/capture", body: ["enabled": value]) } }))
                Text("\(status.count) saved · \(status.proposals) awaiting review · \(status.jobs["queued", default: 0]) turns queued")
                    .font(.caption).foregroundStyle(.secondary)
                if status.worker != "idle" {
                    Text(status.worker.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(.caption).foregroundStyle(.secondary)
                }
                if status.jobs["failed", default: 0] > 0 {
                    Button("Retry \(status.jobs["failed", default: 0]) failed extractions") {
                        Task { await model.mutate("/memory/retry") }
                    }
                }
            }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red).textSelection(.enabled) }
            Picker("Show", selection: $filter) {
                Text("Saved facts").tag("active")
                Text("Possible connections & facts").tag("proposed")
                Text("Later").tag("deferred")
            }.pickerStyle(.segmented)
            TextField("Search these memories", text: $query).textFieldStyle(.roundedBorder)
            List {
                ForEach(model.facts.filter { query.isEmpty || $0.text.localizedCaseInsensitiveContains(query) }) { fact in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(fact.text).textSelection(.enabled)
                        Text(label(fact)).font(.caption).foregroundStyle(.secondary)
                        if !fact.explanation.isEmpty { Text(fact.explanation).font(.callout).foregroundStyle(.secondary) }
                        HStack {
                            Button("Evidence & edit") {
                                Task {
                                    do { selected = try await model.detail(fact.id); correction = fact.text }
                                    catch { model.error = error.localizedDescription }
                                }
                            }
                            Spacer()
                            if fact.status != "active" {
                                Button("Confirm") { review(fact, "confirm") }
                                Button("Incorrect") { review(fact, "reject") }
                                if fact.status != "deferred" { Button("Later") { review(fact, "later") } }
                            } else {
                                Button(fact.pinned == 1 ? "Unpin" : "Pin") {
                                    Task { await model.mutate("/memory/facts/\(fact.id)/pin", body: ["pinned": fact.pinned == 0]) }
                                }
                                Button("Forget") { Task { await model.mutate("/memory/facts/\(fact.id)", method: "DELETE") } }
                            }
                        }.buttonStyle(.borderless)
                    }.padding(.vertical, 8)
                }
                if model.facts.isEmpty { Text("No memories in this view.").foregroundStyle(.secondary) }
            }
            DisclosureGroup("Recover facts from earlier conversations") {
                HStack {
                    Stepper("Latest \(pilotSize) conversations", value: $pilotSize, in: 1...100)
                    Button("Start review pilot") { Task { await model.mutate("/memory/backfill", body: ["sessions": pilotSize]) } }
                }
                Text("Candidates need your review. Old conversations may contain examples or test data.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Investigate a connection") {
                TextField("For example: which email address might belong to Mom?", text: $investigation).textFieldStyle(.roundedBorder)
                HStack {
                    Picker("Model", selection: $investigationModel) {
                        Text("Chat model (Ling)").tag("agent")
                        Text("Research model (Ornith)").tag("research")
                    }
                    Button("Investigate locally") {
                        Task { await model.mutate("/memory/investigations", body: ["question": investigation, "model_role": investigationModel]) }
                    }.disabled(investigation.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                Text("Searches available Contacts, conversations and cached mail. Results remain possible connections until confirmed.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(model.investigations.prefix(3)) { job in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(job.question).font(.callout)
                            Text(job.error.isEmpty ? "\(job.status) · \(job.result)" : "Failed: \(job.error)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if job.status == "queued" || job.status == "running" {
                            Button("Cancel") { Task { await model.mutate("/memory/investigations/\(job.id)/cancel") } }
                        }
                    }
                }
            }
        }
        .padding(22)
        .disabled(model.busy)
        .task {
            while !Task.isCancelled {
                await model.refresh()
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { break }
            }
        }
        .onChange(of: filter) { _, value in model.filter = value; Task { await model.refresh() } }
        .sheet(item: $selected) { fact in
            VStack(alignment: .leading, spacing: 16) {
                Text("Memory evidence").font(.title2)
                TextEditor(text: $correction).frame(height: 80)
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(fact.evidence ?? []) { e in
                            Text(e.label.isEmpty ? e.source_type : e.label).font(.headline)
                            Text(Date(timeIntervalSince1970: e.observed_at), style: .date).font(.caption)
                            Text(e.quote).textSelection(.enabled)
                            if let sid = e.session_id {
                                Button("Read source conversation") {
                                    Task {
                                        do {
                                            let data = try await model.request("/sessions/\(sid)")
                                            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                                            let turns = obj?["turns"] as? [[String: Any]] ?? []
                                            sourceText = turns.map { "\($0["role"] as? String ?? ""): \($0["content"] as? String ?? "")" }.joined(separator: "\n\n")
                                            if turns.isEmpty { sourceText = "The source conversation is no longer available." }
                                        } catch { sourceText = error.localizedDescription }
                                    }
                                }
                            }
                            Divider()
                        }
                        if fact.evidence?.isEmpty != false { Text("This saved memory has no linked source. Older memories are preserved without inventing evidence.") }
                        if let sourceText { Text(sourceText).textSelection(.enabled) }
                    }
                }
                HStack {
                    Button("Close") { selected = nil; sourceText = nil }
                    Spacer()
                    Button("Save correction") {
                        Task {
                            await model.mutate("/memory/facts/\(fact.id)/review", body: ["decision": "edit", "text": correction])
                            if model.error.isEmpty { selected = nil; sourceText = nil }
                        }
                    }.disabled(correction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                }
                if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
            }.padding(24).frame(width: 620, height: 540)
        }
    }

    private func label(_ fact: MemoryFact) -> String {
        if fact.status != "active" { return fact.origin == "connection" ? "Possible match · needs confirmation" : "Extracted statement · needs review" }
        return "\(fact.category.capitalized) · \(fact.origin == "automatic" ? "From your conversation" : fact.origin == "connection" ? "Confirmed connection" : "Saved memory")"
    }

    private func review(_ fact: MemoryFact, _ decision: String) {
        Task { await model.mutate("/memory/facts/\(fact.id)/review", body: ["decision": decision]) }
    }
}

struct MemoryReviewNotice: View {
    @State private var count = 0
    var body: some View {
        Group {
            if count > 0 {
                Button { MemoryWindow.shared.show() } label: {
                    Label("\(count) possible memor\(count == 1 ? "y" : "ies") to review", systemImage: "point.3.connected.trianglepath.dotted")
                        .font(.caption)
                }.buttonStyle(.plain).foregroundStyle(.orange).padding(.top, 6)
            }
        }.task {
            while !Task.isCancelled {
                if let (data, _) = try? await URLSession.shared.data(from: WispClient.baseURL.appendingPathComponent("memory/status")),
                   let status = try? JSONDecoder().decode(MemoryStatus.self, from: data) { count = status.unreviewed }
                do { try await Task.sleep(nanoseconds: 15_000_000_000) } catch { break }
            }
        }
    }
}
