import Foundation
import Combine
import SQLite3

// Shared by the two consent-sensitive readers. All state runs on the main
// queue. A revision is reserved BEFORE reading; disabling access invalidates
// that read and any older HTTP request. Only the latest snapshot is retried.
final class PrivacySyncChannel: ObservableObject {
    @Published private(set) var pending = false
    private(set) var revision = 0
    private let key: String
    private let endpoint: String
    private let defaults: UserDefaults
    private let session: URLSession
    private let retryDelay: Double
    private var task: URLSessionDataTask?
    private var statusTask: URLSessionDataTask?
    private var monitor: Timer?
    var refresh: (() -> Void)?

    init(key: String, endpoint: String, defaults: UserDefaults = .standard,
         session: URLSession = .shared, retryDelay: Double = 2) {
        self.key = key
        self.endpoint = endpoint
        self.defaults = defaults
        self.session = session
        self.retryDelay = retryDelay
    }

    deinit {
        task?.cancel()
        statusTask?.cancel()
        monitor?.invalidate()
    }

    func startMonitoring() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.monitor == nil else { return }
            self.monitor = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                self?.checkBackend()
            }
        }
    }

    // Backend restarts intentionally discard personal snapshots. A small
    // status GET detects that without reading Contacts/history every tick.
    func checkBackend() {
        dispatchPrecondition(condition: .onQueue(.main))
        guard !pending, statusTask == nil else { return }
        let expected = revision
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(endpoint))
        request.timeoutInterval = 5
        statusTask = session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.statusTask = nil
                guard self.revision == expected, !self.pending else { return }
                let reply = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
                if error == nil, let response = response as? HTTPURLResponse,
                   (200..<300).contains(response.statusCode), reply?["ok"] as? Bool == true,
                   reply?["current"] as? Bool == true, reply?["revision"] as? Int == expected {
                    return
                }
                self.requestFreshSnapshot(reply)
            }
        }
        statusTask?.resume()
    }

    private func requestFreshSnapshot(_ reply: [String: Any]?) {
        if let serverRevision = reply?["revision"] as? Int {
            defaults.set(max(serverRevision, defaults.integer(forKey: key)), forKey: key)
        }
        pending = true
        refresh?()
    }

    func begin() -> Int {
        dispatchPrecondition(condition: .onQueue(.main))
        revision = max(defaults.integer(forKey: key) + 1,
                       Int(Date().timeIntervalSince1970 * 1_000))
        defaults.set(revision, forKey: key)
        task?.cancel()
        task = nil
        pending = true
        return revision
    }

    func submit(_ payload: [String: Any], revision expected: Int) {
        dispatchPrecondition(condition: .onQueue(.main))
        guard expected == revision else { return }
        var body = payload
        body["revision"] = expected
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(endpoint))
        request.httpMethod = "POST"
        request.timeoutInterval = 5
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        request.httpBody = data
        task = session.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self, self.revision == expected else { return }
                let reply = data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
                if error == nil, let response = response as? HTTPURLResponse,
                   (200..<300).contains(response.statusCode), reply?["ok"] as? Bool == true {
                    self.task = nil
                    if reply?["current"] as? Bool == true, reply?["revision"] as? Int == expected {
                        self.pending = false
                    } else {
                        // An ignored same-revision retry after a backend restart
                        // is not fresh data. Recheck consent and read a new snapshot.
                        self.requestFreshSnapshot(reply)
                    }
                } else {
                    // Includes connection refusal during backend startup and
                    // non-2xx disk-clear errors. Never silently roll consent on.
                    DispatchQueue.main.asyncAfter(deadline: .now() + self.retryDelay) { [weak self] in
                        self?.submit(payload, revision: expected)
                    }
                }
            }
        }
        task?.resume()
    }
}

// Reads recent Safari + Chrome browsing history directly from their SQLite
// databases and pushes it to the backend for the search_browser_history tool
// — same direct-SQLite pattern as MessagesReader (chat.db), for the same reason:
// neither browser exposes history via AppleScript. Opens READ-ONLY; both
// browsers keep their history DB in WAL mode while running, which safely
// supports concurrent readers, so no copy-aside is needed (same reasoning
// as MessagesReader).
//
// Off by default. Unlike Mail/Notes/Messages — which sync as soon as their
// own TCC permission is granted — this piggybacks on the Full Disk Access
// grant that Permissions.swift requests for the file/shell tools, so a
// silent default-on here would repurpose that grant to read something the
// user never separately agreed to. SettingsView's "Browser History" toggle
// (key below) is the actual consent; this class no-ops until it's on.
//
// Only domain + path + title + visit time leave the machine — query strings
// are stripped before anything is cached or posted, since they routinely
// carry auth tokens, password-reset links, and search text that's far more
// sensitive than the fact a domain was visited.
final class BrowserHistoryReader {
    static let enabledKey = "wisp.browserHistoryEnabled"

    static let preferenceChanged = Notification.Name("wisp.browserHistoryPreferenceChanged")
    static let delivery = PrivacySyncChannel(key: "wisp.browserHistoryRevision",
                                             endpoint: "assistant/sync/browser_history")
    private let channel: PrivacySyncChannel
    private let enabled: () -> Bool
    private let snapshot: (() -> [String: Any])?
    private var timer: Timer?
    private var observer: NSObjectProtocol?

    init(channel: PrivacySyncChannel = BrowserHistoryReader.delivery,
         enabled: @escaping () -> Bool = { UserDefaults.standard.bool(forKey: enabledKey) },
         snapshot: (() -> [String: Any])? = nil) {
        self.channel = channel
        self.enabled = enabled
        self.snapshot = snapshot
        channel.refresh = { [weak self] in self?.sync() }
        observer = NotificationCenter.default.addObserver(forName: Self.preferenceChanged,
                                                          object: nil, queue: .main) { [weak self] _ in
            self?.sync()
        }
    }

    deinit {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        timer?.invalidate()
    }

    static func setEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: enabledKey)
        NotificationCenter.default.post(name: preferenceChanged, object: nil)
    }

    // Core Data epoch (2001-01-01) — same constant Notes/Messages use.
    private static let appleEpochOffset: Double = 978307200
    // Chrome/WebKit epoch (1601-01-01) in seconds.
    private static let chromeEpochOffset: Double = 11_644_473_600

    private var isEnabled: Bool {
        enabled()
    }

    func start() {
        channel.startMonitoring()
        // Reassert persisted consent on launch, including an explicit clear
        // when off. Settings notifications also sync immediately.
        sync()
        for d in [4.0, 10.0, 20.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in self?.sync() }
        }
        DispatchQueue.main.async { [weak self] in
            // History accumulates fast but doesn't need message-level
            // freshness for profile purposes — 30 min keeps it current
            // without re-scanning the DB constantly.
            self?.timer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func sync() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let revision = self.channel.begin()
            guard self.isEnabled else {
                self.channel.submit(["enabled": false], revision: revision)
                return
            }
            DispatchQueue.global(qos: .utility).async { [weak self] in
                guard let self, self.isEnabled else { return }
                let browsers = self.snapshot?() ?? self.readBrowsers()
                DispatchQueue.main.async {
                    guard self.isEnabled, self.channel.revision == revision else { return }
                    self.channel.submit(["enabled": true, "browsers": browsers], revision: revision)
                }
            }
        }
    }

    private func readBrowsers() -> [String: Any] {
        let home = NSHomeDirectory() as NSString
        guard isEnabled else { return [:] }
        let safari = readSafari(home.appendingPathComponent("Library/Safari/History.db"))
        guard isEnabled else { return [:] }
        let chrome = readChrome(home.appendingPathComponent("Library/Application Support/Google/Chrome/Default/History"))
        func entry(_ rows: [String]?) -> [String: Any] {
            ["lines": rows?.joined(separator: "\n") ?? "",
             "diagnostics": ["available": rows != nil,
                             "reason": rows == nil ? "History unavailable (Full Disk Access or browser missing)" : ""]]
        }
        return ["safari": entry(safari), "chrome": entry(chrome)]
    }

    // MARK: - Safari

    // history_visits.visit_time is Core Data seconds; history_items.url holds
    // the full URL. Windowed to 30 days, capped at 5000 rows as a safety
    // ceiling — history has no natural cap the way Notes/Calendar do.
    private func readSafari(_ path: String, days: Int = 30, limit: Int = 5000) -> [String]? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        let cutoff = Date().timeIntervalSince1970 - Double(days) * 86400 - Self.appleEpochOffset
        let sql = """
        SELECT v.visit_time, i.url, v.title
        FROM history_visits v
        JOIN history_items i ON v.history_item = i.id
        WHERE v.visit_time > \(cutoff)
        ORDER BY v.visit_time DESC
        LIMIT \(limit)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            guard isEnabled else { return nil }
            defer { step = sqlite3_step(stmt) }
            let visitSecs = sqlite3_column_double(stmt, 0)
            guard visitSecs > 0, let url = columnText(stmt, 1) else { continue }
            let epochSecs = visitSecs + Self.appleEpochOffset
            let title = columnText(stmt, 2) ?? ""
            if let line = Self.formatLine(epochSecs: epochSecs, urlString: url, title: title) {
                out.append(line)
            }
        }
        return step == SQLITE_DONE ? out : nil
    }

    // MARK: - Chrome

    // visits.visit_time / urls.last_visit_time are Chrome-epoch microseconds.
    private func readChrome(_ path: String, days: Int = 30, limit: Int = 5000) -> [String]? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        let cutoffUs = Int64((Date().timeIntervalSince1970
                              - Double(days) * 86400 + Self.chromeEpochOffset) * 1_000_000)
        let sql = """
        SELECT v.visit_time, u.url, u.title
        FROM visits v
        JOIN urls u ON v.url = u.id
        WHERE v.visit_time > \(cutoffUs)
        ORDER BY v.visit_time DESC
        LIMIT \(limit)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            guard isEnabled else { return nil }
            defer { step = sqlite3_step(stmt) }
            let visitUs = sqlite3_column_int64(stmt, 0)
            guard visitUs > 0, let url = columnText(stmt, 1) else { continue }
            let epochSecs = Double(visitUs) / 1_000_000 - Self.chromeEpochOffset
            let title = columnText(stmt, 2) ?? ""
            if let line = Self.formatLine(epochSecs: epochSecs, urlString: url, title: title) {
                out.append(line)
            }
        }
        return step == SQLITE_DONE ? out : nil
    }

    // MARK: - Shared

    // "epochSecs | host | path | title" — query string and fragment dropped
    // (see the class doc comment on why). Bare data:/about:/chrome-extension
    // URLs and similar without a host are skipped; they're never meaningful
    // profile signal and some encode large opaque blobs.
    static func formatLine(epochSecs: Double, urlString: String, title: String) -> String? {
        guard let comps = URLComponents(string: urlString), let host = comps.host, !host.isEmpty
        else { return nil }
        let path = comps.path.isEmpty ? "/" : comps.path
        let cleanTitle = title.replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "|", with: "/")
        return "\(epochSecs) | \(host) | \(path) | \(cleanTitle)"
    }

    private func columnText(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let c = sqlite3_column_text(stmt, idx) else { return nil }
        return String(cString: c)
    }

}
