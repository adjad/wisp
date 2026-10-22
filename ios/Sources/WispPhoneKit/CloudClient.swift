import Foundation

public enum CloudProvider: String, Codable, CaseIterable, Sendable, Identifiable {
    case openRouter, openAI, custom

    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .openRouter: "OpenRouter"
        case .openAI: "OpenAI"
        case .custom: "Other (OpenAI-compatible)"
        }
    }
    public var defaultBaseURL: String {
        switch self {
        case .openRouter: "https://openrouter.ai/api/v1"
        case .openAI: "https://api.openai.com/v1"
        case .custom: ""
        }
    }
    public var defaultModel: String {
        switch self {
        case .openRouter: "openai/gpt-oss-120b"
        case .openAI: "gpt-5-mini"
        case .custom: ""
        }
    }
}

/// Everything about the cloud engine except the API key, which lives in the Keychain.
public struct CloudSettings: Codable, Equatable, Sendable {
    public var provider: CloudProvider
    public var baseURL: String
    public var model: String
    public var maxTokens: Int

    public init(provider: CloudProvider = .openRouter, baseURL: String? = nil,
                model: String? = nil, maxTokens: Int = 2048) {
        self.provider = provider
        self.baseURL = baseURL ?? provider.defaultBaseURL
        self.model = model ?? provider.defaultModel
        self.maxTokens = maxTokens
    }

    public enum Problem: Error, Equatable, LocalizedError {
        case missingModel, badURL, insecureURL, missingKey

        public var errorDescription: String? {
            switch self {
            case .missingModel: "Choose a model ID for the cloud provider."
            case .badURL: "The provider URL isn't valid."
            case .insecureURL: "The provider URL must use https (or http to a .ts.net name), so your key and messages are encrypted."
            case .missingKey: "Add your API key in Settings → Cloud."
            }
        }
    }

    /// The validated `/chat/completions` endpoint. Plain http is allowed only to a MagicDNS
    /// (`*.ts.net`) name: a bare 100.x address may be carrier CGNAT, not the tailnet.
    public func completionsURL() throws -> URL {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), let scheme = url.scheme?.lowercased(), let host = url.host else {
            throw Problem.badURL
        }
        if scheme != "https" {
            guard scheme == "http", MacPairing.isMagicDNSName(host.lowercased()) else { throw Problem.insecureURL }
        }
        guard !model.trimmingCharacters(in: .whitespaces).isEmpty else { throw Problem.missingModel }
        return url.appendingPathComponent("chat/completions")
    }
}

public enum CloudError: Error, Equatable, LocalizedError {
    case http(Int, String)
    case provider(String)

    public var errorDescription: String? {
        switch self {
        case .http(401, _), .http(403, _): "The provider rejected your API key."
        case .http(let code, let detail): detail.isEmpty ? "The provider answered with an error (\(code))." : detail
        case .provider(let message): message
        }
    }
}

/// Streams a chat completion from an OpenAI-compatible provider.
public struct CloudClient: Sendable {
    public let settings: CloudSettings
    private let apiKey: String
    private let session: URLSession

    public init(settings: CloudSettings, apiKey: String, session: URLSession = .shared) {
        self.settings = settings
        self.apiKey = apiKey
        self.session = session
    }

    public func makeRequest(messages: [[String: String]]) throws -> URLRequest {
        guard !apiKey.isEmpty else { throw CloudSettings.Problem.missingKey }
        var request = URLRequest(url: try settings.completionsURL())
        request.httpMethod = "POST"
        request.timeoutInterval = 300
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        if settings.provider == .openRouter { request.setValue("Wisp", forHTTPHeaderField: "X-Title") }
        let body: [String: Any] = ["model": settings.model, "messages": messages,
                                   "stream": true, "max_tokens": settings.maxTokens]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }

    public func stream(messages: [[String: String]]) -> AsyncThrowingStream<ChatEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try makeRequest(messages: messages)
                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else { throw CloudError.http(0, "") }
                    guard (200..<300).contains(http.statusCode) else {
                        var body = Data()
                        for try await byte in bytes { body.append(byte); if body.count > 16_384 { break } }
                        throw CloudError.http(http.statusCode, OpenAIStreamDecoder.errorMessage(from: body) ?? "")
                    }
                    var filter = ThinkFilter()
                    for try await line in bytes.lines {
                        switch OpenAIStreamDecoder.decode(line: line) {
                        case .content(let text):
                            let visible = filter.feed(text)
                            if !visible.isEmpty { continuation.yield(.delta(visible)) }
                        case .error(let message):
                            throw CloudError.provider(message)
                        case .done:
                            break
                        case nil:
                            continue
                        }
                    }
                    let tail = filter.finish()
                    if !tail.isEmpty { continuation.yield(.delta(tail)) }
                    continuation.yield(.done)
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
