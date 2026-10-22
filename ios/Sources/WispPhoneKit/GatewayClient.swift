import CryptoKit
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

    public static let allowedExtensions = [".safetensors", ".json", ".jinja", ".txt", ".model"]

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
    case unverified
    case unauthorized
    case backendDown
    case http(Int, String)
    case badResponse

    public var errorDescription: String? {
        switch self {
        case .unreachable:
            "Can't reach your Mac. Check that Tailscale is connected on both devices and the gateway is running."
        case .unverified:
            "Something answered at your Mac's address but couldn't prove it's your Mac, so the app didn't send your pairing token. Update Wisp on your Mac, or pair again from Settings."
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

/// The ping challenge (gateway v2): the Mac proves it holds this phone's pairing before
/// the phone sends its token. proof = HMAC-SHA256(key: SHA-256(token), "wisp-phone-ping-v1:" + nonce).
public enum GatewayProof {
    static let label = "wisp-phone-ping-v1:"

    /// 32 random bytes as lowercase hex. The system generator is cryptographically secure.
    public static func newNonce() -> String {
        var generator = SystemRandomNumberGenerator()
        return (0..<32).map { _ in String(format: "%02x", UInt8.random(in: .min ... .max, using: &generator)) }.joined()
    }

    public static func proof(token: String, nonce: String) -> String {
        let code = HMAC<SHA256>.authenticationCode(for: Data((label + nonce).utf8), using: key(token))
        return Data(code).map { String(format: "%02x", $0) }.joined()
    }

    /// True when any proof in the ping answer is this token's proof for `nonce`. Constant-time compare.
    public static func verify(pingResponse data: Data, token: String, nonce: String) -> Bool {
        struct Ping: Decodable { var proofs: [String]? }
        guard let proofs = (try? JSONDecoder().decode(Ping.self, from: data))?.proofs, proofs.count <= 1024 else {
            return false
        }
        let message = Data((label + nonce).utf8)
        return proofs.contains { candidate in
            guard let bytes = hexBytes(candidate), bytes.count == SHA256.byteCount else { return false }
            return HMAC<SHA256>.isValidAuthenticationCode(bytes, authenticating: message, using: key(token))
        }
    }

    private static func key(_ token: String) -> SymmetricKey {
        SymmetricKey(data: Data(SHA256.hash(data: Data(token.utf8))))
    }

    private static func hexBytes(_ text: String) -> Data? {
        let chars = Array(text.utf8)
        guard chars.count.isMultiple(of: 2) else { return nil }
        var out = Data(capacity: chars.count / 2)
        for i in stride(from: 0, to: chars.count, by: 2) {
            guard let byte = UInt8(String(decoding: chars[i...i + 1], as: UTF8.self), radix: 16) else { return nil }
            out.append(byte)
        }
        return out
    }
}

/// Talks to the Mac's phone gateway (see ios/docs/PHONE_GATEWAY.md).
public actor GatewayClient {
    public nonisolated let pairing: MacPairing
    private let session: URLSession
    /// How long a verified base is trusted before the next request re-proves it.
    public static let proofLifetime: TimeInterval = 60
    private let now: @Sendable () -> Date
    private var preferredBase: URL?
    private var verifiedAt = Date.distantPast

    public init(pairing: MacPairing, session: URLSession = .shared, now: @escaping @Sendable () -> Date = Date.init) {
        self.pairing = pairing
        self.session = session
        self.now = now
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

    /// A request for one model file, against a base proved by a ping made just now, never the
    /// cached one. A download can outlive the proof window by minutes, so VerifiedFileDownloader
    /// calls this before every attempt.
    public func fileRequest(model: String, file: String) async throws -> URLRequest {
        preferredBase = nil
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
                    await self.forgetBase()
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
        } catch {
            // Any failure, an HTTP error included, makes the next request prove the Mac again.
            preferredBase = nil
            throw Self.map(error)
        }
    }

    /// The first base URL whose ping proves it holds this pairing, MagicDNS name first.
    /// Nothing carries the token until then: 100.64/10 is also carrier CGNAT space, so with
    /// Tailscale off, whoever answers at the Mac's address may be a stranger.
    func reachableBase() async throws -> URL {
        // A negative age means the clock went backwards: fail closed rather than stretch the window.
        let age = now().timeIntervalSince(verifiedAt)
        if let preferredBase, age >= 0, age < Self.proofLifetime { return preferredBase }
        preferredBase = nil
        var answered = false
        for base in pairing.baseURLs {
            let nonce = GatewayProof.newNonce()
            guard var components = URLComponents(url: base.appendingPathComponent("phone/v1/ping"),
                                                 resolvingAgainstBaseURL: false) else { continue }
            components.queryItems = [URLQueryItem(name: "nonce", value: nonce)]
            guard let url = components.url else { continue }
            var request = URLRequest(url: url)
            request.timeoutInterval = 4
            guard let (data, response) = try? await session.data(for: request),
                  (response as? HTTPURLResponse)?.statusCode == 200 else { continue }
            answered = true
            if GatewayProof.verify(pingResponse: data, token: pairing.token, nonce: nonce) {
                preferredBase = base
                verifiedAt = now()
                return base
            }
        }
        throw answered ? GatewayError.unverified : GatewayError.unreachable
    }

    private func forgetBase() { preferredBase = nil }

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

/// One model-file download from the Mac with progress and automatic retry (multi-GB files over
/// Tailscale can drop mid-way). Returns a temporary file the caller must move or delete.
///
/// Every attempt, the first included, gets its request from `GatewayClient.fileRequest`, which
/// proves the Mac with a fresh ping first. A download can outlive the proof window by minutes,
/// and Tailscale may drop between attempts, so no retry reuses an earlier request or the opaque
/// resume data URLSession keeps. A retry resumes with an explicit `Range` request rebuilt for the
/// freshly proved base, and only when that base is the one the partial bytes came from.
/// Otherwise it starts over.
public enum VerifiedFileDownloader {
    public typealias Progress = @Sendable (Int64, Int64) -> Void

    public enum Failure: Error, Equatable, LocalizedError {
        case oversized
        case badRange

        public var errorDescription: String? {
            switch self {
            case .oversized: "Your Mac sent more than the file's listed size, so the download was stopped."
            case .badRange: "Your Mac resumed the download at the wrong place. Try the download again."
            }
        }
    }

    /// Stops as soon as more than `maxBytes` arrive, so a bad listing can't fill the disk.
    public static func download(
        gateway: GatewayClient, model: String, file: String, maxBytes: Int64, attempts: Int = 4,
        configuration: URLSessionConfiguration = .default,
        backoff: @escaping @Sendable (Int) async throws -> Void = { try await Task.sleep(for: .seconds(2 * $0)) },
        progress: @escaping Progress
    ) async throws -> URL {
        let partial = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-download-\(UUID().uuidString)")
        guard FileManager.default.createFile(atPath: partial.path, contents: nil) else { throw CocoaError(.fileWriteUnknown) }
        let transfer = Transfer(file: partial, maxBytes: maxBytes, progress: progress)
        let session = URLSession(configuration: configuration, delegate: transfer, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            var lastOrigin: String?
            var lastError: Error = URLError(.unknown)
            for attempt in 0..<max(attempts, 1) {
                try Task.checkCancellation()
                if attempt > 0 { try await backoff(attempt) }
                do {
                    var request = try await gateway.fileRequest(model: model, file: file)
                    let origin = request.url.map { "\($0.scheme ?? "")://\($0.host ?? ""):\($0.port ?? -1)" }
                    if origin != lastOrigin { try transfer.restart() }
                    lastOrigin = origin
                    let offset = transfer.received
                    if offset > 0 { request.setValue("bytes=\(offset)-", forHTTPHeaderField: "Range") }
                    try await transfer.run(session: session, request: request, offset: offset)
                    return partial
                } catch GatewayError.unreachable {
                    // No proof, so nothing was sent; Tailscale may be reconnecting.
                    lastError = GatewayError.unreachable
                } catch let error as URLError where error.code != .cancelled && error.code != .userAuthenticationRequired {
                    lastError = error
                }
            }
            throw lastError
        } catch {
            try? FileManager.default.removeItem(at: partial)
            if Task.isCancelled { throw CancellationError() }
            throw error
        }
    }

    /// Writes one attempt's bytes onto the partial file. Delegate callbacks arrive on the
    /// session's serial queue; the lock covers the reads from `download`.
    final class Transfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        private let file: URL
        private let maxBytes: Int64
        private let progress: Progress
        private let lock = NSLock()
        private var handle: FileHandle?
        private var written: Int64 = 0
        private var offset: Int64 = 0
        private var failure: Error?
        private var continuation: CheckedContinuation<Void, Error>?

        init(file: URL, maxBytes: Int64, progress: @escaping Progress) {
            self.file = file
            self.maxBytes = maxBytes
            self.progress = progress
        }

        var received: Int64 { lock.withLock { written } }

        func restart() throws {
            try lock.withLock {
                let handle = try FileHandle(forWritingTo: file)
                defer { try? handle.close() }
                try handle.truncate(atOffset: 0)
                written = 0
            }
        }

        func run(session: URLSession, request: URLRequest, offset: Int64) async throws {
            let task = session.dataTask(with: request)
            try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    lock.withLock {
                        self.continuation = continuation
                        self.offset = offset
                        self.failure = nil
                    }
                    task.resume()
                }
            } onCancel: {
                task.cancel()
            }
        }

        private func fail(_ error: Error, _ task: URLSessionTask) {
            lock.withLock { failure = error }
            task.cancel()
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                        completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
            guard let http = response as? HTTPURLResponse else {
                fail(GatewayError.badResponse, dataTask); return completionHandler(.cancel)
            }
            do {
                let requested = lock.withLock { offset }
                switch http.statusCode {
                case 200:
                    // A whole file, even if a range was asked for: start the partial over.
                    try restart()
                case 206:
                    let range = http.value(forHTTPHeaderField: "Content-Range") ?? ""
                    guard requested > 0, range.hasPrefix("bytes \(requested)-") else {
                        try? restart()
                        fail(Failure.badRange, dataTask); return completionHandler(.cancel)
                    }
                case 401:
                    fail(GatewayError.unauthorized, dataTask); return completionHandler(.cancel)
                default:
                    fail(GatewayError.http(http.statusCode, "The Mac refused the model download."), dataTask)
                    return completionHandler(.cancel)
                }
                let handle = try FileHandle(forWritingTo: file)
                try handle.seekToEnd()
                lock.withLock { self.handle = handle }
                completionHandler(.allow)
            } catch {
                fail(error, dataTask); completionHandler(.cancel)
            }
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
            let total: Int64? = lock.withLock {
                guard let handle, failure == nil else { return nil }
                guard written + Int64(data.count) <= maxBytes else { return nil }
                do { try handle.write(contentsOf: data) } catch { failure = error; return nil }
                written += Int64(data.count)
                return written
            }
            guard let total else {
                if received + Int64(data.count) > maxBytes { fail(Failure.oversized, dataTask) } else { dataTask.cancel() }
                return
            }
            progress(total, maxBytes)
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            let (continuation, failure) = lock.withLock {
                try? handle?.close()
                handle = nil
                let pending = (self.continuation, self.failure)
                self.continuation = nil
                self.failure = nil
                return pending
            }
            if let failure {
                continuation?.resume(throwing: failure)
            } else if let error {
                continuation?.resume(throwing: error)
            } else {
                continuation?.resume()
            }
        }
    }
}
