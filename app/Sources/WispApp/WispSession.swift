import Foundation

// The URLSession every call to Wisp's backend goes through.
//
// The backend requires a shared key on every request (see service/auth.py for
// why loopback on its own isn't enough — a web page can POST to 127.0.0.1
// without a CORS preflight). Rather than remembering to attach that header at
// ~46 call sites, it is set once here as a default header on the session, so
// any request made through it carries the key and any new call site inherits it.
//
// Deliberately NOT used for requests to anywhere other than Wisp's own backend:
// NetworkStats' speed test builds its own session and should keep doing so —
// this key must never be sent to a third-party host.
enum WispSession {
    private static let lock = NSLock()
    private static var cachedSession: URLSession?
    private static var cachedKey: String?

    /// Rebuilt if the key changes or first appears.
    ///
    /// It genuinely can appear late: on a first-ever run the backend generates
    /// the key file as it starts, which is after the app has already launched.
    /// Caching a keyless session forever would leave every request 401ing until
    /// the next restart, so the key is re-read and the session rebuilt when it
    /// changes.
    static var shared: URLSession {
        lock.lock()
        defer { lock.unlock() }

        let key = apiKey()
        if let cachedSession, cachedKey == key { return cachedSession }

        let config = URLSessionConfiguration.default
        if let key { config.httpAdditionalHeaders = ["X-Wisp-Key": key] }
        // The backend is a local process; a request unanswered after a minute
        // is stuck, not slow. Streaming call sites set their own timeouts.
        config.timeoutIntervalForRequest = 60

        let session = URLSession(configuration: config)
        cachedSession = session
        cachedKey = key
        return session
    }

    /// Read the key the backend generated, or nil if it hasn't yet.
    static func apiKey() -> String? {
        // Mirrors service/paths.py: WISP_HOME wins, else ~/.moe.
        let env = ProcessInfo.processInfo.environment
        let home: URL
        if let override = env["WISP_HOME"], !override.isEmpty {
            home = URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        } else {
            home = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".moe")
        }
        let path = home.appendingPathComponent("api_key")
        guard let raw = try? String(contentsOf: path, encoding: .utf8) else { return nil }
        let key = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return key.isEmpty ? nil : key
    }
}
