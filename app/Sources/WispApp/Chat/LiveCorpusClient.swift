import Foundation

// Keep corpus decoding with the Chat feature so standalone WispClient
// contracts do not acquire a dependency on its evaluation model.
extension WispClient {
    func liveCorpusCases(selector: String) async throws -> [AssistantTaskCorpusCase] {
        var parts = URLComponents(url: Self.baseURL.appendingPathComponent("corpus/cases"), resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "selector", value: selector)]
        let (data, response) = try await URLSession.shared.data(from: parts.url!)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw NSError(domain: "WispCorpus", code: 1, userInfo: [NSLocalizedDescriptionKey: "The live corpus service is unavailable. Open a build that supports live cases and check the local profile."])
        }
        struct Envelope: Decodable { let cases: [AssistantTaskCorpusCase] }
        return try JSONDecoder().decode(Envelope.self, from: data).cases
    }
}

extension WispClient {
    /// The newest saved corpus run (service/corpus.py) as Markdown and CSV;
    /// nil when none is saved yet or the service predates saved runs.
    func latestCorpusExport() async throws -> (markdown: String, csv: String)? {
        struct Run: Decodable { let run_id: String; let date: String }
        struct Runs: Decodable { let runs: [Run] }
        let base = Self.baseURL.appendingPathComponent("corpus/runs")
        let (data, response) = try await URLSession.shared.data(from: base)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let run = try JSONDecoder().decode(Runs.self, from: data).runs.first else { return nil }
        func fetch(_ name: String) async throws -> String? {
            var parts = URLComponents(url: base.appendingPathComponent(run.date).appendingPathComponent(name),
                                      resolvingAgainstBaseURL: false)!
            parts.queryItems = [URLQueryItem(name: "run_id", value: run.run_id)]
            let (body, reply) = try await URLSession.shared.data(from: parts.url!)
            guard (reply as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return String(data: body, encoding: .utf8)
        }
        guard let markdown = try await fetch("export.md"), let csv = try await fetch("export.csv") else { return nil }
        return (markdown, csv)
    }
}
