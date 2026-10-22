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
