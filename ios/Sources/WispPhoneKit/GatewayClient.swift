import Foundation

public struct GatewayHello: Decodable, Equatable, Sendable {
    public struct Device: Decodable, Equatable, Sendable { public var id: String; public var name: String }
    public struct Backend: Decodable, Equatable, Sendable { public var reachable: Bool }
    public var gatewayVersion: Int
    public var macName: String
    public var device: Device
    public var backend: Backend

    enum CodingKeys: String, CodingKey {
        case gatewayVersion = "gateway_version", macName = "mac_name", device, backend
    }
}

public struct ModelBundle: Decodable, Equatable, Identifiable, Sendable {
    public struct File: Decodable, Equatable, Sendable {
        public var name: String
        public var size: Int64
        public var sha256: String

        public init(name: String, size: Int64, sha256: String) {
            self.name = name
            self.size = size
            self.sha256 = sha256
        }
    }
    public var id: String
    public var modelType: String?
    public var totalBytes: Int64
    public var files: [File]

    public init(id: String, modelType: String?, totalBytes: Int64, files: [File]) {
        self.id = id
        self.modelType = modelType
        self.totalBytes = totalBytes
        self.files = files
    }

    enum CodingKeys: String, CodingKey {
        case id, modelType = "model_type", totalBytes = "total_bytes", files
    }

    /// The listing comes from the network, so its names become paths only if they pass the
    /// same rules the Mac serves by: plain names, no separators, no `..`, known file types.
    public var isSafe: Bool {
        Self.isSafeID(id) && !files.isEmpty && files.allSatisfy { Self.isSafeFileName($0.name) && $0.size >= 0 }
            && Set(files.map(\.name)).count == files.count
            && files.allSatisfy { $0.sha256.count == 64 && $0.sha256.allSatisfy(\.isHexDigit) }
    }

    public static func isSafeID(_ id: String) -> Bool {
        (1...128).contains(id.count) && id != "." && id != ".."
            && id.allSatisfy { ($0.isASCII && ($0.isLowercase || $0.isNumber)) || $0 == "." || $0 == "_" || $0 == "-" }
    }

    /// True when `child` is exactly one path segment below `parent`. Deliberately lexical: resolving
    /// symlinks would turn iOS's `/var` into `/private/var` only for paths that already exist.
    public static func isDirectChild(_ child: URL, of parent: URL) -> Bool {
        let name = child.lastPathComponent
        guard !name.isEmpty, name != ".", name != "..", !name.contains("/") else { return false }
        return child.deletingLastPathComponent().standardizedFileURL.path == parent.standardizedFileURL.path
    }

    public static let allowedExtensions =[".safetensors", ".json", ".jinja", ".txt", ".model"]

    public static func isSafeFileName(_ name: String) -> Bool {
        guard (1...128).contains(name.count), let first = name.first, first.isASCII, first.isLetter || first.isNumber,
              name.allSatisfy({ ($0.isASCII && ($0.isLetter || $0.isNumber)) || $0 == "." || $0 == "_" || $0 == "-" })
        else { return false }
        let lower = name.lowercased()
        return allowedExtensions.contains { lower.hasSuffix($0) }
    }
}

public enum GatewayError: Error, Equatable, LocalizedError {
    case unreachable
    case unauthorized
    case backendDown
    case http(Int, String)
    case badResponse

    public var errorDescription: String? {
        switch self {
        case .unreachable:
            "Can't reach your Mac. Check that Tailscale is connected on both devices and the gateway is running."
        case .unauthorized:
            "Your Mac no longer recognizes this iPhone. Pair again from Settings."
        case .backendDown:
            "The gateway is up, but Wisp isn't running on your Mac."
        case .http(let code, let detail):
            detail.isEmpty ? "Your Mac answered with an error (\(code))." : detail
        case .badResponse:
            "Your Mac sent a response the app couldn't read."
        }
    }
}

/// Talks to the Mac's phone gateway (see ios/docs/PHONE_GATEWAY.md).
public actor GatewayClient {
    public nonisolated let pairing: MacPairing
    private let session: URLSession
    private var preferredBase: URL?

    public init(pairing: MacPairing, session: URLSession = .shared) {
        self.pairing = pairing
        self.session = session
    }

    // MARK: Requests

    public func hello() async throws -> GatewayHello {
        let (data, _) = try await send(path: "phone/v1/hello", method: "GET", timeout: 8)
        do { return try JSONDecoder().decode(GatewayHello.self, from: data) } catch { throw GatewayError.badResponse }
    }

    public func models() async throws -> [ModelBundle] {
        struct Listing: Decodable { var models: [ModelBundle] }
        // The Mac may hash a multi-GB file on the first listing.
        let (data, _) = try await send(path: "phone/v1/models", method: "GET", timeout: 300)
        do { return try JSONDecoder().decode(Listing.self, from: data).models } catch { throw GatewayError.badResponse }
    }

    public func approve(_ request: ApprovalRequest, approved: Bool, scope: ApprovalScope) async throws {
        var body: [String: Any] = ["session_id": request.sessionId, "action_id": request.id,
                                   "approved": approved, "scope": scope.rawValue]
        if !request.requestId.isEmpty { body["request_id"] = request.requestId }
        _ = try await send(path: "phone/v1/agent/approve", method: "POST", body: body, timeout: 15)
    }

    /// A request for one model file, for a URLSession download task.
    public func fileRequest(model: String, file: String) async throws -> URLRequest {
        let base = try await reachableBase()
        var request = URLRequest(url: base.appendingPathComponent("phone/v1/models/\(model)/files/\(file)"))
        request.setValue("Bearer \(pairing.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 120
        return request
    }

    /// One Wisp turn on the Mac. Mac-side history continues when `sessionId` is set.
    /// The request is never re-sent once the Mac has answered, since a tool may already have run.
    public nonisolated func agent(prompt: String, sessionId: String?) -> AsyncThrowingStream<ChatEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var body: [String: Any] = ["prompt": prompt]
                    if let sessionId, !sessionId.isEmpty { body["session_id"] = sessionId }
                    let base = try await self.reachableBase()
                    var request = URLRequest(url: base.appendingPathComponent("phone/v1/agent"))
                    request.httpMethod = "POST"
                    request.timeoutInterval = 600
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("Bearer \(self.pairing.token)", forHTTPHeaderField: "Authorization")
                    request.httpBody = try JSONSerialization.data(withJSONObject: body)
                    let (bytes, response) = try await self.session.bytes(for: request)
                    try Self.check(response, body: nil)
                    var current = sessionId ?? ""
                    var finished = false
                    for try await line in bytes.lines {
                        guard let event = GatewayEventDecoder.decode(line: line, sessionId: current) else { continue }
                        if case .session(let id) = event { current = id }
                        continuation.yield(event)
                        if case .done = event { finished = true; break }
                        if case .failed = event { finished = true; break }
                    }
                    if !finished { continuation.yield(.failed("The connection to your Mac closed before Wisp finished.")) }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: Self.map(error))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: Plumbing

    private func send(path: String, method: String, body: [String: Any]? = nil,
                      timeout: TimeInterval) async throws -> (Data, HTTPURLResponse) {
        let base = try await reachableBase()
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("Bearer \(pairing.token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        do {
            let (data, response) = try await session.data(for: request)
            let http = try Self.check(response, body: data)
            return (data, http)
        } catch let error as GatewayError {
            throw error
        } catch {
            preferredBase = nil
            throw Self.map(error)
        }
    }

    /// The first base URL that answers the unauthenticated ping, MagicDNS name first.
    private func reachableBase() async throws -> URL {
        if let preferredBase { return preferredBase }
        for base in pairing.baseURLs {
            var request = URLRequest(url: base.appendingPathComponent("phone/v1/ping"))
            request.timeoutInterval = 4
            if let (_, response) = try? await session.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                preferredBase = base
                return base
            }
        }
        throw GatewayError.unreachable
    }

    @discardableResult
    private static func check(_ response: URLResponse, body: Data?) throws -> HTTPURLResponse {
        guard let http = response as? HTTPURLResponse else { throw GatewayError.badResponse }
        switch http.statusCode {
        case 200..<300: return http
        case 401: throw GatewayError.unauthorized
        case 502: throw GatewayError.backendDown
        default:
            let detail = body.flatMap { OpenAIStreamDecoder.errorMessage(from: $0) } ?? ""
            throw GatewayError.http(http.statusCode, detail)
        }
    }

    private static func map(_ error: Error) -> Error {
        if error is GatewayError { return error }
        if let url = error as? URLError {
            switch url.code {
            case .cannotConnectToHost, .cannotFindHost, .timedOut, .networkConnectionLost,
                 .notConnectedToInternet, .dnsLookupFailed:
                return GatewayError.unreachable
            default: break
            }
        }
        return error
    }
}
