import Foundation
import SwiftUI

/// The research-only transport surface lets library/recovery tests use fixtures.
protocol ResearchClient {
    func researchJobs() async throws -> [ResearchLibraryItem]
    func researchSnapshot(jobId: String) async -> WispClient.ResearchSnapshot?
    func createResearchPlan(prompt: String, depth: String) async -> WispClient.ResearchSnapshot?
    func updateResearchPlan(jobId: String, plan: WispClient.ResearchPlanPayload) async -> WispClient.ResearchSnapshot?
    func researchAction(jobId: String, action: String, body: [String: Any]) async -> Bool
    func streamResearch(jobId: String, after: Int, onEvent: @escaping @Sendable (WispClient.Event) -> Void) async
    func pinResearch(jobId: String, pinned: Bool) async -> Bool
    func deleteResearch(jobId: String) async -> Bool
    func updateResearchDomains(jobId: String, allowed: [String]?, blocked: [String]?) async -> WispClient.ResearchSnapshot?
    func exportResearch(jobId: String, title: String) async -> URL?
}

extension ResearchClient {
    func researchAction(jobId: String, action: String) async -> Bool {
        await researchAction(jobId: jobId, action: action, body: [:])
    }
}

extension WispClient: ResearchClient {
    func researchJobs() async throws -> [ResearchLibraryItem] {
        try await researchJobs(using: .shared)
    }

    func researchJobs(using session: URLSession) async throws -> [ResearchLibraryItem] {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs"))
        request.timeoutInterval = 10
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = object["jobs"] as? [[String: Any]] else {
            throw URLError(.cannotParseResponse)
        }
        // A malformed response must not masquerade as an empty library.
        return try rows.map { row in
            guard let item = ResearchLibraryItem.parse(row) else {
                throw URLError(.cannotParseResponse)
            }
            return item
        }
    }
}

struct ResearchLibraryItem: Identifiable, Equatable {
    let id: String
    let title: String
    let objective: String
    let state: String
    let depth: String
    let updatedAt: Date
    let pinned: Bool

    var stateLabel: String {
        ["planning": "Preparing plan", "awaiting_approval": "Ready for review",
         "running": "In progress", "paused": "Paused", "complete": "Complete",
         "partial": "Partial report", "cancelled": "Cancelled", "failed": "Failed"][state]
            ?? "Saved"
    }

    var isUnfinished: Bool {
        ["planning", "awaiting_approval", "running", "paused"].contains(state)
    }

    static func parse(_ row: [String: Any]) -> ResearchLibraryItem? {
        guard let id = row["id"] as? String, !id.isEmpty,
              let state = row["state"] as? String else { return nil }
        let plan = row["plan"] as? [String: Any] ?? [:]
        let prompt = row["prompt"] as? String ?? ""
        let title = (plan["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return ResearchLibraryItem(id: id, title: title.isEmpty ? (prompt.isEmpty ? "Untitled research" : prompt) : title,
            objective: plan["objective"] as? String ?? prompt, state: state,
            depth: plan["depth"] as? String ?? "standard",
            updatedAt: Date(timeIntervalSince1970: row["updated_at"] as? Double ?? 0),
            pinned: row["pinned"] as? Bool ?? false)
    }
}

@MainActor
final class ResearchLibraryModel: ObservableObject {
    enum Filter: String, CaseIterable { case recent = "Recent", pinned = "Pinned", unfinished = "Unfinished" }
    @Published var items: [ResearchLibraryItem] = []
    @Published var query = ""
    @Published var filter: Filter = .recent
    @Published var loading = false
    @Published var errorText = ""
    private let client: ResearchClient
    private var refreshTask: Task<Void, Never>?
    private var refreshID = UUID()

    init(client: ResearchClient) { self.client = client }

    var visibleItems: [ResearchLibraryItem] {
        let terms = query.split(whereSeparator: \.isWhitespace).map(String.init)
        return items.filter { item in
            (filter != .pinned || item.pinned) && (filter != .unfinished || item.isUnfinished)
                && terms.allSatisfy { term in
                    (item.title + " " + item.objective).localizedStandardContains(term)
                }
        }.sorted {
            if $0.updatedAt != $1.updatedAt { return $0.updatedAt > $1.updatedAt }
            return $0.id < $1.id
        }
    }

    func refresh() {
        refreshTask?.cancel()
        let id = UUID(); refreshID = id
        loading = true; errorText = ""
        refreshTask = Task { [weak self] in
            guard let self else { return }
            do {
                let jobs = try await client.researchJobs()
                guard refreshID == id, !Task.isCancelled else { return }
                items = jobs
            } catch {
                guard refreshID == id, !Task.isCancelled else { return }
                errorText = "Couldn't load saved research. Check that Wisp is running, then try again."
            }
            loading = false
        }
    }
}
