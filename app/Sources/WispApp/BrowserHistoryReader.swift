import Foundation
import SQLite3

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

    private var timer: Timer?

    // Core Data epoch (2001-01-01) — same constant Notes/Messages use.
    private static let appleEpochOffset: Double = 978307200
    // Chrome/WebKit epoch (1601-01-01) in seconds.
    private static let chromeEpochOffset: Double = 11_644_473_600

    private var isEnabled: Bool {
        UserDefaults.standard.bool(forKey: Self.enabledKey)
    }

    func start() {
        // Re-checked on every fire (not just here), so flipping the Settings
        // toggle off takes effect on the next tick without needing a relaunch.
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
        guard isEnabled else {
            // Report the toggle without reading any browser data. Otherwise
            // the backend cannot distinguish disabled from not-yet-synced.
            post(browser: "", lines: "", diagnostics: [:], enabled: false)
            return
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.syncSafari()
            self?.syncChrome()
        }
    }

    // MARK: - Safari

    private func syncSafari() {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Safari/History.db")
        guard let rows = readSafari(path) else {
            post(browser: "safari", lines: "",
                 diagnostics: ["available": false,
                               "reason": "History.db not readable (Full Disk Access?)"])
            return
        }
        post(browser: "safari", lines: rows.joined(separator: "\n"),
             diagnostics: ["available": true, "count": rows.count])
    }

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
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let visitSecs = sqlite3_column_double(stmt, 0)
            guard visitSecs > 0, let url = columnText(stmt, 1) else { continue }
            let epochSecs = visitSecs + Self.appleEpochOffset
            let title = columnText(stmt, 2) ?? ""
            if let line = Self.formatLine(epochSecs: epochSecs, urlString: url, title: title) {
                out.append(line)
            }
        }
        return out
    }

    // MARK: - Chrome

    private func syncChrome() {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent("Library/Application Support/Google/Chrome/Default/History")
        guard let rows = readChrome(path) else {
            post(browser: "chrome", lines: "",
                 diagnostics: ["available": false,
                               "reason": "Chrome History not readable (not installed, or Full Disk Access?)"])
            return
        }
        post(browser: "chrome", lines: rows.joined(separator: "\n"),
             diagnostics: ["available": true, "count": rows.count])
    }

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
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let visitUs = sqlite3_column_int64(stmt, 0)
            guard visitUs > 0, let url = columnText(stmt, 1) else { continue }
            let epochSecs = Double(visitUs) / 1_000_000 - Self.chromeEpochOffset
            let title = columnText(stmt, 2) ?? ""
            if let line = Self.formatLine(epochSecs: epochSecs, urlString: url, title: title) {
                out.append(line)
            }
        }
        return out
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

    private func post(browser: String, lines: String, diagnostics: [String: Any], enabled: Bool = true) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/browser_history")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "browser": browser, "lines": lines, "diagnostics": diagnostics, "enabled": enabled,
        ])
        URLSession.shared.dataTask(with: req).resume()
    }
}
