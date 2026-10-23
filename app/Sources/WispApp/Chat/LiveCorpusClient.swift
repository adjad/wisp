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

extension WispClient {
    /// Sends one /corpus prompts command to the service and returns the progress it reports.
    func corpusPrompts(_ command: CorpusPromptsCommand) async throws -> CorpusPromptsStatus {
        let base = Self.baseURL.appendingPathComponent("corpus/prompts")
        var request: URLRequest
        switch command {
        case .status, .export:
            request = URLRequest(url: base.appendingPathComponent("status"))
        case .resume, .stop:
            request = URLRequest(url: base.appendingPathComponent(command == .resume ? "resume" : "stop"))
            request.httpMethod = "POST"
        case .start(let selector):
            request = URLRequest(url: base.appendingPathComponent("start"))
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: ["selector": selector])
        case .importFile(let path):
            request = URLRequest(url: base.appendingPathComponent("import"))
            request.httpMethod = "POST"
            request.httpBody = try JSONSerialization.data(withJSONObject: ["path": path])
        }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Stopping waits for the current prompt to wind down.
        request.timeoutInterval = 30
        let (data, response) = try await URLSession.shared.data(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            struct Detail: Decodable { let detail: String }
            let message = (try? JSONDecoder().decode(Detail.self, from: data))?.detail
                ?? (code == 404 ? "This Wisp build does not run prompts files yet." : "The prompts runner is unavailable.")
            throw NSError(domain: "WispCorpus", code: code, userInfo: [NSLocalizedDescriptionKey: message])
        }
        if case .importFile = command {
            // Import returns the file it recorded; progress comes from status.
            return try await corpusPrompts(.status)
        }
        return try JSONDecoder().decode(CorpusPromptsStatus.self, from: data)
    }

    /// The newest finished prompts with Wisp's whole answers.
    func corpusPromptsResults(limit: Int) async throws -> [CorpusPromptsResult] {
        var parts = URLComponents(url: Self.baseURL.appendingPathComponent("corpus/prompts/results"),
                                  resolvingAgainstBaseURL: false)!
        parts.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        let (data, response) = try await URLSession.shared.data(from: parts.url!)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return [] }
        struct Envelope: Decodable { let results: [CorpusPromptsResult] }
        return try JSONDecoder().decode(Envelope.self, from: data).results
    }

    /// The current prompts run as Markdown and CSV; nil before any prompt finishes.
    func corpusPromptsExport() async throws -> (markdown: String, csv: String)? {
        let base = Self.baseURL.appendingPathComponent("corpus/prompts")
        func fetch(_ name: String) async throws -> String? {
            let (body, reply) = try await URLSession.shared.data(from: base.appendingPathComponent(name))
            guard (reply as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            return String(data: body, encoding: .utf8)
        }
        guard let markdown = try await fetch("export.md"), let csv = try await fetch("export.csv") else { return nil }
        return (markdown, csv)
    }
}
