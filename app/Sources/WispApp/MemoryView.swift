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
    var evidence: [MemoryEvidence]?
    var supersedes: [Int]?
    var superseded_by: [Int]?
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
    let retention_policy: String?
}

@MainActor
final class MemoryModel: ObservableObject {
    @Published var facts: [MemoryFact] = []
    @Published var activeFacts: [MemoryFact] = []
    @Published var status: MemoryStatus?
    @Published var error = ""
    @Published var busy = false
    var filter = "active"
    var query = ""
    private var revision = 0
    private var detailRevision = 0

    func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: WispClient.baseURL) else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NSError(domain: "Memory", code: 1, userInfo: [NSLocalizedDescriptionKey: json?["detail"] as? String ?? "Memory request failed."])
        }
        return data
    }

    func refresh() async {
        revision += 1
        let requested = revision
        let wantedFilter = filter
        let wantedQuery = query
        do {
            let decoder = JSONDecoder()
            let state = try decoder.decode(MemoryStatus.self, from: await request("/memory/status"))
            var components = URLComponents()
            components.path = "/memory/facts"
            components.queryItems = [URLQueryItem(name: "status", value: wantedFilter), URLQueryItem(name: "query", value: wantedQuery)]
            let records = try decoder.decode(FactsResponse.self, from: await request(components.string!))
            guard requested == revision, wantedFilter == filter, wantedQuery == query else { return }
            status = state
            facts = records.facts
            error = ""
        } catch {
            if requested == revision { self.error = error.localizedDescription }
        }
    }

    func mutate(_ path: String, method: String = "POST", body: [String: Any]? = nil) async -> Bool {
        guard !busy else { return false }
        busy = true
        await PendingConfigWrites.shared.begin()
        var succeeded = false
        do {
            let data = try await request(path, method: method, body: body)
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if let ok = json?["ok"] as? Bool, !ok {
                throw NSError(domain: "Memory", code: 2, userInfo: [NSLocalizedDescriptionKey: "This memory is no longer available. Refresh and try again."])
            }
            await refresh()
            succeeded = true
        } catch { self.error = error.localizedDescription }
        await PendingConfigWrites.shared.end()
        busy = false
        return succeeded
    }

    func detail(_ id: Int) async -> MemoryFact? {
        detailRevision += 1
        let requested = detailRevision
        do {
            let response = try JSONDecoder().decode(FactResponse.self, from: await request("/memory/facts/\(id)"))
            let choices = try JSONDecoder().decode(FactsResponse.self, from: await request("/memory/facts?status=active&limit=1000"))
            guard requested == detailRevision else { return nil }
            activeFacts = choices.facts.filter { $0.id != id }
            return response.fact
        } catch { self.error = error.localizedDescription; return nil }
    }

    private struct FactsResponse: Decodable { let facts: [MemoryFact] }
    private struct FactResponse: Decodable { let fact: MemoryFact }
}

@MainActor
final class MemoryWindow {
    static let shared = MemoryWindow()
    private var window: NSWindow?

    func show() {
        if let window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 780, height: 650),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Memory"
        window.contentView = NSHostingView(rootView: MemoryView())
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct MemoryView: View {
    @StateObject private var model = MemoryModel()
    @State private var filter = "active"
    @State private var query = ""
    @State private var selected: MemoryFact?
    @State private var correction = ""
    @State private var replaces = 0
    @State private var sourceText = ""
    @State private var sourceRequest = UUID()
    @State private var pilotSize = 20
    @State private var forgetting: MemoryFact?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Memory").font(.largeTitle.bold())
                Spacer()
                Button("Refresh") { Task { await model.refresh() } }
                Toggle("Capture new statements", isOn: Binding(get: { model.status?.capture_enabled ?? false },
                    set: { enabled in Task { await model.mutate("/memory/capture", body: ["enabled": enabled]) } }))
                    .toggleStyle(.switch)
                    .disabled(model.busy || model.status == nil)
            }
            if let status = model.status {
                HStack {
                    Text("\(status.count) saved · \(status.proposals) awaiting review · \(status.worker)")
                    Spacer()
                    Text("Queued: \(status.jobs["queued", default: 0]) · Failed: \(status.jobs["failed", default: 0])")
                    if status.jobs["failed", default: 0] > 0 {
                        Button("Retry") { Task { await model.mutate("/memory/retry") } }
                    }
                }.font(.caption).foregroundStyle(.secondary)
            }
            Picker("View", selection: $filter) {
                Text("Saved").tag("active")
                Text("Review").tag("proposed")
                Text("Later").tag("deferred")
                Text("Corrections").tag("superseded")
            }.pickerStyle(.segmented)
            TextField("Search memories", text: $query).textFieldStyle(.roundedBorder)
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red).textSelection(.enabled) }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if model.facts.isEmpty { Text("No matching memories.").foregroundStyle(.secondary).padding() }
                    ForEach(model.facts) { fact in card(fact) }
                }
            }
            DisclosureGroup("Review a small historical pilot") {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Historical statements become review candidates, never automatic facts. Old chats may contain tests or outdated information. At most 2,000 user turns are queued per pilot.").font(.caption)
                    HStack {
                        Stepper("Latest \(pilotSize) conversations", value: $pilotSize, in: 1...100)
                        Button("Queue pilot") { Task { await model.mutate("/memory/backfill", body: ["sessions": pilotSize]) } }
                        Button("Cancel pending pilot") { Task { await model.mutate("/memory/backfill/cancel") } }
                    }
                    Text("Capture must be enabled and the agent model already loaded for queued work to run.").font(.caption).foregroundStyle(.secondary)
                }.padding(.top, 6)
            }
            Text(model.status?.retention_policy ?? "Explicit, confirmed and pinned memories remain when a conversation is deleted. Use Forget to remove a memory and its correction history.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(minWidth: 650, minHeight: 500)
        .task {
            while !Task.isCancelled {
                await model.refresh()
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
            }
        }
        .task(id: query) {
            model.query = query
            do { try await Task.sleep(for: .milliseconds(250)) } catch { return }
            await model.refresh()
        }
        .onChange(of: filter) { _, value in model.filter = value; Task { await model.refresh() } }
        .sheet(item: $selected) { fact in editor(fact) }
        .confirmationDialog("Forget this memory and all its correction history?", isPresented: Binding(
            get: { forgetting != nil }, set: { if !$0 { forgetting = nil } }), titleVisibility: .visible) {
                if let fact = forgetting {
                    Button("Forget", role: .destructive) {
                        Task { await model.mutate("/memory/facts/\(fact.id)", method: "DELETE") }
                        forgetting = nil
                    }
                }
            } message: { Text("Old source passages will be excluded from memory retrieval. The original conversation remains in chat history.") }
    }

    private func card(_ fact: MemoryFact) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(fact.text).textSelection(.enabled)
            Text("\(fact.category.capitalized) · \(fact.origin) · \(Date(timeIntervalSince1970: fact.observed_at).formatted(date: .abbreviated, time: .omitted))")
                .font(.caption).foregroundStyle(.secondary)
            if !fact.explanation.isEmpty { Text(fact.explanation).font(.caption) }
            HStack {
                Button(fact.status == "superseded" ? "Evidence and history" : "Evidence, edit and review") {
                    Task {
                        if let detail = await model.detail(fact.id) {
                            correction = detail.text; replaces = 0; sourceText = ""; sourceRequest = UUID(); selected = detail
                        }
                    }
                }
                if fact.status == "active" {
                    Button(fact.pinned == 0 ? "Pin" : "Unpin") {
                        Task { await model.mutate("/memory/facts/\(fact.id)/pin", body: ["pinned": fact.pinned == 0]) }
                    }
                }
                if fact.status == "proposed" || fact.status == "deferred" {
                    Button("Later") { Task { await model.mutate("/memory/facts/\(fact.id)/review", body: ["decision": "later"]) } }
                }
                Spacer()
                Button("Forget", role: .destructive) { forgetting = fact }
            }.disabled(model.busy)
        }.padding(12).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private func editor(_ fact: MemoryFact) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Memory \(fact.id)").font(.title2.bold())
            if fact.status == "superseded" {
                Text(fact.text).textSelection(.enabled)
                Text("Historical version; excluded from active memory.").foregroundStyle(.secondary)
            } else {
                TextEditor(text: $correction).frame(height: 95).border(.quaternary)
            }
            if let prior = fact.supersedes, !prior.isEmpty { Text("Replaces memory IDs: \(prior.map(String.init).joined(separator: ", "))").font(.caption) }
            if let next = fact.superseded_by, !next.isEmpty { Text("Replaced by memory IDs: \(next.map(String.init).joined(separator: ", "))").font(.caption) }
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(fact.evidence ?? []) { evidence in
                        Text("\(evidence.label) · \(Date(timeIntervalSince1970: evidence.observed_at).formatted())").font(.caption).foregroundStyle(.secondary)
                        Text(evidence.quote).textSelection(.enabled)
                        if let sid = evidence.session_id, let idx = evidence.turn_idx {
                            Button("Show source context") {
                                let requested = UUID()
                                sourceRequest = requested
                                Task {
                                    do {
                                        let data = try await model.request("/memory/sources/\(sid)/\(idx)")
                                        let response = try JSONDecoder().decode(SourceResponse.self, from: data)
                                        guard sourceRequest == requested, selected?.id == fact.id else { return }
                                        sourceText = response.turns.map { "\($0.role.capitalized) [\($0.idx)]: \($0.content ?? "")" }.joined(separator: "\n\n")
                                    } catch {
                                        if sourceRequest == requested, selected?.id == fact.id { sourceText = error.localizedDescription }
                                    }
                                }
                            }
                        }
                    }
                    if fact.evidence?.isEmpty ?? true { Text("No source passage is retained for this version.").foregroundStyle(.secondary) }
                    if !sourceText.isEmpty { Divider(); Text(sourceText).font(.caption).textSelection(.enabled) }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            if fact.status == "proposed" || fact.status == "deferred" {
                Picker("On confirmation", selection: $replaces) {
                    Text("Keep as a separate memory").tag(0)
                    ForEach(model.activeFacts) { prior in Text("Replace #\(prior.id): \(prior.text)").tag(prior.id) }
                }
                Text("Choose the old memory when this statement is a correction. Dates alone never overwrite a confirmed memory.").font(.caption).foregroundStyle(.secondary)
            }
            if !model.error.isEmpty { Text(model.error).foregroundStyle(.red) }
            HStack {
                Button("Close") { selected = nil; sourceText = "" }
                Spacer()
                if fact.status == "proposed" || fact.status == "deferred" {
                    Button("Confirm") {
                        Task {
                            var body: [String: Any] = ["decision": "confirm"]
                            if replaces != 0 { body["supersedes_id"] = replaces }
                            if await model.mutate("/memory/facts/\(fact.id)/review", body: body) { selected = nil }
                        }
                    }.disabled(model.busy || correction != fact.text)
                }
                if fact.status != "superseded" {
                    Button("Save correction") {
                        Task {
                            var body: [String: Any] = ["decision": "edit", "text": correction]
                            if replaces != 0 { body["supersedes_id"] = replaces }
                            if await model.mutate("/memory/facts/\(fact.id)/review", body: body) { selected = nil }
                        }
                    }.disabled(model.busy || correction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || correction == fact.text)
                }
            }
        }.padding(20).frame(width: 650, height: 540)
    }

    private struct SourceResponse: Decodable { let turns: [SourceTurn] }
    private struct SourceTurn: Decodable { let role: String; let idx: Int; let content: String? }
}

struct MemoryReviewNotice: View {
    @State private var count = 0
    var body: some View {
        Group {
            if count > 0 {
                Button("Review \(count) new memor\(count == 1 ? "y" : "ies")") { MemoryWindow.shared.show() }
                    .font(.caption).buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }.task {
            let model = MemoryModel()
            while !Task.isCancelled {
                await model.refresh()
                count = model.status?.unreviewed ?? 0
                do { try await Task.sleep(for: .seconds(15)) } catch { break }
            }
        }
    }
}
