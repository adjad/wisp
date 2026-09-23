import Foundation

struct LingEngineClient {
    static let origin = URL(string: "http://127.0.0.1:8767")!
    static let apiRoot = URL(string: "http://127.0.0.1:8767/v1")!

    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 35
        self.session = URLSession(configuration: configuration)
    }

    func health() async throws -> EngineHealth {
        let url = Self.origin.appendingPathComponent("health")
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = 2
        let (data, response) = try await session.data(for: request)
        try Self.requireSuccess(response, data: data)
        return try JSONDecoder().decode(EngineHealth.self, from: data)
    }

    func chat(messages: [ChatTurn], model: String, thinking: Bool) async throws -> ChatCompletionResponse {
        let url = Self.apiRoot.appendingPathComponent("chat/completions")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": model,
            "messages": messages.map { ["role": $0.role, "content": $0.content] },
            "max_tokens": 128,
            "temperature": 0.0,
            "seed": 0,
            "thinking": thinking,
            "use_cache": false,
            "stream": false,
        ])
        let (data, response) = try await session.data(for: request)
        try Self.requireSuccess(response, data: data)
        return try JSONDecoder().decode(ChatCompletionResponse.self, from: data)
    }

    private static func requireSuccess(_ response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw ClientError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? "No response details were provided."
            throw ClientError.server(status: http.statusCode, detail: body)
        }
    }

    enum ClientError: LocalizedError {
        case invalidResponse
        case server(status: Int, detail: String)

        var errorDescription: String? {
            switch self {
            case .invalidResponse:
                "The Ling engine returned an invalid HTTP response."
            case let .server(status, detail):
                "The Ling engine returned HTTP \(status): \(detail)"
            }
        }
    }
}
