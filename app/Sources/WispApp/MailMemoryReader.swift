import Foundation
import SQLite3

/// Reads months-old mail for the backend's mail-memory queue (service/mail_memory).
///
/// The backend can't read Mail's files itself: Full Disk Access belongs to this
/// app, not to the Python child (see service/tools/imessage_tools.py). So the
/// backend publishes a `mail_memory_read` event and this reader answers it by
/// POSTing to /assistant/sync/mail_memory with the event's `request_id`.
///
/// IMPORTANT rules:
/// - Read-only. The Envelope Index is opened SQLITE_OPEN_READONLY; .emlx files
///   are only read. Mail.app is never scripted or launched from here.
/// - Two requests only: `index` (metadata for a window of days, no bodies) and
///   `body` (one message's raw RFC 822 bytes, capped). The backend parses MIME.
/// - Every POST answers one request: nothing is ever sent unasked.
final class MailMemoryReader {
    static let maxDays = 200
    static let maxIndexRows = 6000
    static let maxBodyBytes = 262_144
    private static let emlxIndexTTL: TimeInterval = 600

    private let mailRoot: String
    private let work = DispatchQueue(label: "wisp.mail-memory", qos: .utility)
    private let send: ([String: Any]) -> Void
    private var emlx: [Int64: (path: String, partial: Bool)] = [:]
    private var emlxBuiltAt: Date?

    /// `send` delivers each reply; the app passes `MailMemoryReader.poster(to:)`.
    init(mailRoot: String = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Mail"),
         send: @escaping ([String: Any]) -> Void) {
        self.mailRoot = mailRoot
        self.send = send
    }

    /// Entry point for the `mail_memory_read` event. Returns false for a malformed request.
    @discardableResult
    func handle(_ payload: [String: Any]) -> Bool {
        guard let request = MailMemoryReader.request(payload) else { return false }
        work.async { [weak self] in
            guard let self else { return }
            var reply = self.answer(request)
            reply["request_id"] = request.id
            reply["kind"] = request.kind
            self.send(reply)
        }
        return true
    }

    struct Request: Equatable {
        let id: String
        let kind: String
        let days: Int
        let rowid: Int64
    }

    /// Strict shape: {type, request_id, kind: "index", days} or {type, request_id, kind: "body", rowid}.
    static func request(_ payload: [String: Any]) -> Request? {
        guard payload["type"] as? String == "mail_memory_read",
              let id = payload["request_id"] as? String,
              id.range(of: "^[A-Za-z0-9_-]{16,64}$", options: .regularExpression) != nil,
              let kind = payload["kind"] as? String else { return nil }
        // The hub stamps durable events with an event_id; reads are published non-durable,
        // but a stamped one is still the same request.
        let keys = Set(payload.keys).subtracting(["event_id"])
        if kind == "index" {
            guard keys == ["type", "request_id", "kind", "days"],
                  let days = payload["days"] as? Int, (1...maxDays).contains(days) else { return nil }
            return Request(id: id, kind: kind, days: days, rowid: 0)
        }
        if kind == "body" {
            guard keys == ["type", "request_id", "kind", "rowid"],
                  let rowid = (payload["rowid"] as? NSNumber)?.int64Value, rowid > 0 else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: rowid)
        }
        return nil
    }

    private func answer(_ request: Request) -> [String: Any] {
        if request.kind == "index" {
            guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
                return ["ok": false, "error": "no_mail_index"]
            }
            let since = Date().timeIntervalSince1970 - Double(request.days) * 86400
            guard let (rows, capped) = MailMemoryReader.indexRows(dbPath: path, since: since) else {
                return ["ok": false, "error": "unreadable"]
            }
            return ["ok": true, "rows": rows, "capped": capped]
        }
        if emlx[request.rowid] == nil, emlxBuiltAt.map({ Date().timeIntervalSince($0) > MailMemoryReader.emlxIndexTTL }) ?? true {
            emlx = MailMemoryReader.emlxFiles(mailRoot)
            emlxBuiltAt = Date()
        }
        guard let file = emlx[request.rowid] else { return ["ok": false, "error": "not_on_disk"] }
        guard let (data, truncated) = MailMemoryReader.message(at: file.path) else {
            return ["ok": false, "error": "unreadable"]
        }
        return ["ok": true, "rowid": request.rowid, "partial": file.partial, "truncated": truncated,
                "raw_b64": data.base64EncodedString()]
    }

    // MARK: - Envelope Index

    static func envelopeIndexPath(_ mailRoot: String) -> String? {
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: mailRoot) else { return nil }
        for e in entries.sorted().reversed() where e.hasPrefix("V") {
            let candidate = "\(mailRoot)/\(e)/MailData/Envelope Index"
            if FileManager.default.fileExists(atPath: candidate) { return candidate }
        }
        return nil
    }

    private static func exists(_ db: OpaquePointer?, table: String, column: String? = nil) -> Bool {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK else { return false }
        defer { sqlite3_finalize(stmt) }
        var any = false
        while sqlite3_step(stmt) == SQLITE_ROW {
            any = true
            if let column, let c = sqlite3_column_text(stmt, 1), String(cString: c) == column { return true }
        }
        return column == nil && any
    }

    private static func text(_ stmt: OpaquePointer?, _ i: Int32) -> String {
        guard let c = sqlite3_column_text(stmt, i) else { return "" }
        return String(cString: c)
    }

    /// Inbox and Sent messages received since `since`, newest first. A Gmail-over-IMAP
    /// account stores its mail in All Mail and marks Inbox/Sent with `labels` rows, so
    /// membership is the mailbox OR a label. A message in both counts as sent.
    static func indexRows(dbPath: String, since: Double) -> ([[String: Any]], Bool)? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        guard exists(db, table: "messages", column: "date_received") else { return nil }
        let labels = exists(db, table: "labels", column: "mailbox_id")
        let bulkColumns: [String] = ["list_id_hash", "unsubscribe_type", "automated_conversation"]
        let bulkParts = bulkColumns.filter { exists(db, table: "messages", column: $0) }
            .map { (column: String) in "IFNULL(m.\(column), 0) != 0" }
        let bulk = bulkParts.isEmpty ? "0" : "(" + bulkParts.joined(separator: " OR ") + ")"
        let messageID = exists(db, table: "messages", column: "message_id") ? "m.message_id" : "''"
        let conversation = exists(db, table: "messages", column: "conversation_id") ? "IFNULL(m.conversation_id, 0)" : "0"
        let sent = exists(db, table: "messages", column: "date_sent") ? "IFNULL(m.date_sent, 0)" : "0"
        let labelMembers = labels ? "UNION SELECT l.message_id, box.kind FROM labels l JOIN box ON l.mailbox_id = box.id" : ""
        // Mail's on-device categories (macOS 15+): 0 Primary, 1 Transactions, 2 Updates,
        // 3 Promotions; -1 when Mail doesn't record them. The RFC Message-ID lives here too.
        let global = exists(db, table: "message_global_data", column: "model_category")
            && exists(db, table: "messages", column: "global_message_id")
        let hasHeader = global && exists(db, table: "message_global_data", column: "message_id_header")
        let category = global ? "IFNULL(g.model_category, -1)" : "-1"
        let rfcID = hasHeader ? "IFNULL(g.message_id_header, '')" : "''"
        let globalJoin = global ? "LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id" : ""
        let sql = """
        WITH box AS (
          SELECT ROWID id, CASE WHEN url LIKE '%Sent%' THEN 'sent' ELSE 'inbox' END kind
          FROM mailboxes WHERE url LIKE '%/INBOX' OR url LIKE '%Sent%'),
        member AS (
          SELECT m.ROWID mid, box.kind kind FROM messages m JOIN box ON m.mailbox = box.id
          \(labelMembers))
        SELECT m.ROWID, mb.url, MAX(member.kind), m.date_received, \(sent), \(messageID),
               \(conversation), IFNULL(s.subject, ''), IFNULL(a.address, ''), IFNULL(a.comment, ''), \(bulk),
               \(category), \(rfcID)
        FROM member
        JOIN messages m ON m.ROWID = member.mid
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        \(globalJoin)
        LEFT JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        WHERE IFNULL(m.deleted, 0) = 0 AND m.date_received > \(since)
        GROUP BY m.ROWID
        ORDER BY m.date_received DESC
        LIMIT \(maxIndexRows + 1)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }
        var rows: [[String: Any]] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            if rows.count == maxIndexRows { return (rows, true) }
            let url = text(stmt, 1)
            rows.append([
                "rowid": sqlite3_column_int64(stmt, 0),
                "account": URL(string: url)?.host ?? "",
                "box": text(stmt, 2),
                "ts": sqlite3_column_double(stmt, 3),
                "sent_ts": sqlite3_column_double(stmt, 4),
                "message_id": text(stmt, 5),
                "conversation_id": sqlite3_column_int64(stmt, 6),
                "subject": text(stmt, 7),
                "sender": text(stmt, 8),
                "sender_name": text(stmt, 9),
                "bulk": sqlite3_column_int(stmt, 10) != 0,
                "category": Int(sqlite3_column_int(stmt, 11)),
                "rfc_message_id": text(stmt, 12),
            ])
        }
        guard step == SQLITE_DONE else { return nil }
        return (rows, false)
    }

    // MARK: - .emlx files

    /// ROWID → file for every message Mail keeps on disk. A `.partial.emlx` had its
    /// attachments stripped; a full file wins when both exist.
    static func emlxFiles(_ mailRoot: String) -> [Int64: (path: String, partial: Bool)] {
        var out: [Int64: (path: String, partial: Bool)] = [:]
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: mailRoot) else { return out }
        for version in entries where version.hasPrefix("V") {
            let base = "\(mailRoot)/\(version)"
            guard let walker = FileManager.default.enumerator(atPath: base) else { continue }
            while let rel = walker.nextObject() as? String {
                guard rel.hasSuffix(".emlx"), rel.contains("/Messages/") else { continue }
                let name = (rel as NSString).lastPathComponent
                guard let id = Int64(name.prefix { $0.isNumber }), id > 0 else { continue }
                let partial = name.hasSuffix(".partial.emlx")
                if let have = out[id], !have.partial { continue }
                out[id] = ("\(base)/\(rel)", partial)
            }
        }
        return out
    }

    /// The RFC 822 bytes of an .emlx: a decimal byte count on the first line, then
    /// that many bytes, then Mail's plist trailer (dropped). Capped at `maxBodyBytes`.
    static func message(at path: String) -> (Data, Bool)? {
        guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? handle.close() }
        guard let head = try? handle.read(upToCount: 32), let newline = head.firstIndex(of: 0x0A),
              let count = Int(String(decoding: head[head.startIndex..<newline], as: UTF8.self)
                                .trimmingCharacters(in: .whitespaces)), count > 0 else { return nil }
        let start = UInt64(newline - head.startIndex + 1)
        let wanted = min(count, maxBodyBytes)
        guard (try? handle.seek(toOffset: start)) != nil,
              let data = try? handle.read(upToCount: wanted), !data.isEmpty else { return nil }
        return (data, count > maxBodyBytes)
    }

    // MARK: - Transport

    static func poster(to endpoint: URL) -> ([String: Any]) -> Void {
        { reply in
            var request = URLRequest(url: endpoint)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: reply)
            URLSession.shared.dataTask(with: request).resume()
        }
    }
}
