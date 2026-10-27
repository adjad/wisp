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

    private let mailRoot: String
    private let rebuildInterval: TimeInterval
    private let work = DispatchQueue(label: "wisp.mail-memory", qos: .utility)
    private let send: ([String: Any]) -> Void
    private var emlx: [Int64: (path: String, partial: Bool)] = [:]
    private var emlxBuiltAt: Date?

    /// `send` delivers each reply; the app passes `MailMemoryReader.poster(to:)`.
    init(mailRoot: String = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Mail"),
         rebuildInterval: TimeInterval = 30,
         send: @escaping ([String: Any]) -> Void) {
        self.mailRoot = mailRoot
        self.rebuildInterval = rebuildInterval
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
            do {
                let (rows, capped) = try MailMemoryReader.indexRows(dbPath: path, since: since)
                return ["ok": true, "rows": rows, "capped": capped]
            } catch let failure as IndexFailure {
                return ["ok": false, "error": failure.rawValue]
            } catch {
                return ["ok": false, "error": IndexFailure.unreadable.rawValue]
            }
        }
        // Only a message that is in Inbox or Sent right now: one moved to Trash, Junk or
        // Archive since the index, or a ROWID Mail reused, is never read.
        guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
            return ["ok": false, "error": "no_mail_index"]
        }
        // An index that can't be read right now (busy, access revoked) is not an answer:
        // the backend retries later instead of skipping the email for good.
        guard let (member, _) = try? MailMemoryReader.indexRows(dbPath: path, since: 0, only: request.rowid) else {
            return ["ok": false, "error": "index_unreadable"]
        }
        guard !member.isEmpty else { return ["ok": false, "error": "not_in_inbox_or_sent"] }
        // A rowid missing from the map, or a cached path that no longer reads, rebuilds
        // the map once (at most one rebuild per `rebuildInterval`) and retries.
        var rebuilt = false
        if emlx[request.rowid] == nil { rebuilt = rebuildEmlx() }
        guard var file = emlx[request.rowid] else { return ["ok": false, "error": "not_on_disk"] }
        var read = MailMemoryReader.message(at: file.path)
        if read == nil, !rebuilt, rebuildEmlx() {
            guard let fresh = emlx[request.rowid] else { return ["ok": false, "error": "not_on_disk"] }
            file = fresh
            read = MailMemoryReader.message(at: file.path)
        }
        guard let (data, truncated) = read else { return ["ok": false, "error": "unreadable"] }
        return ["ok": true, "rowid": request.rowid, "partial": file.partial, "truncated": truncated,
                "raw_b64": data.base64EncodedString()]
    }

    /// Rebuilds the .emlx map unless it was built within `rebuildInterval`. True when it did.
    private func rebuildEmlx() -> Bool {
        if let built = emlxBuiltAt, Date().timeIntervalSince(built) < rebuildInterval { return false }
        emlx = MailMemoryReader.emlxFiles(mailRoot)
        emlxBuiltAt = Date()
        return true
    }

    /// Why an index read failed. The raw value is the `error` the backend sees.
    enum IndexFailure: String, Error {
        case unreadable
        case unsupportedSchema = "unsupported_schema"
    }

    // MARK: - Envelope Index

    /// The newest `V*` folder that has an Envelope Index (V10 is newer than V9).
    /// The index and the .emlx map must come from this same folder: ROWIDs are per database.
    static func versionDirectory(_ mailRoot: String) -> String? {
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: mailRoot) else { return nil }
        let versions = entries.compactMap { e -> (Int, String)? in
            guard e.hasPrefix("V"), let n = Int(e.dropFirst()) else { return nil }
            return (n, e)
        }
        for (_, e) in versions.sorted(by: { $0.0 > $1.0 }) {
            if FileManager.default.fileExists(atPath: "\(mailRoot)/\(e)/MailData/Envelope Index") {
                return "\(mailRoot)/\(e)"
            }
        }
        return nil
    }

    static func envelopeIndexPath(_ mailRoot: String) -> String? {
        versionDirectory(mailRoot).map { "\($0)/MailData/Envelope Index" }
    }

    /// True for a real Sent mailbox: the last path component is "Sent Messages", "Sent",
    /// "Sent Mail" or "Sent Items" (so "[Gmail]/Sent Mail" counts), never a name that merely
    /// contains "sent" like Consent or Presentations. The URL may be percent-encoded.
    static func isSentMailbox(url: String) -> Bool {
        let decoded = url.removingPercentEncoding ?? url
        guard let scheme = decoded.range(of: "://") else { return false }
        let rest = decoded[scheme.upperBound...]
        guard let slash = rest.firstIndex(of: "/") else { return false }
        let name = rest[rest.index(after: slash)...].split(separator: "/", omittingEmptySubsequences: true).last
        guard let name else { return false }
        return ["sent messages", "sent", "sent mail", "sent items"].contains(name.lowercased())
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
    /// Optional columns are probed and fall back to 0 or ''. Missing core tables throw
    /// `.unsupportedSchema`, anything else unreadable throws `.unreadable`.
    /// `only` limits the read to one message (the body check), whatever its age.
    static func indexRows(dbPath: String, since: Double, only: Int64? = nil) throws -> ([[String: Any]], Bool) {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        var probe: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM sqlite_master", -1, &probe, nil) == SQLITE_OK,
              sqlite3_step(probe) == SQLITE_ROW else {
            sqlite3_finalize(probe)
            throw IndexFailure.unreadable
        }
        sqlite3_finalize(probe)
        guard exists(db, table: "messages", column: "date_received"),
              exists(db, table: "messages", column: "mailbox"),
              exists(db, table: "mailboxes") else { throw IndexFailure.unsupportedSchema }
        let labels = exists(db, table: "labels", column: "mailbox_id") && exists(db, table: "labels", column: "message_id")
        let hasURL = exists(db, table: "mailboxes", column: "url")
        let bulkColumns: [String] = ["list_id_hash", "unsubscribe_type", "automated_conversation"]
        let bulkParts = bulkColumns.filter { exists(db, table: "messages", column: $0) }
            .map { (column: String) in "IFNULL(m.\(column), 0) != 0" }
        let bulk = bulkParts.isEmpty ? "0" : "(" + bulkParts.joined(separator: " OR ") + ")"
        let messageID = exists(db, table: "messages", column: "message_id") ? "m.message_id" : "''"
        let conversation = exists(db, table: "messages", column: "conversation_id") ? "IFNULL(m.conversation_id, 0)" : "0"
        let sent = exists(db, table: "messages", column: "date_sent") ? "IFNULL(m.date_sent, 0)" : "0"
        let notDeleted = exists(db, table: "messages", column: "deleted") ? "IFNULL(m.deleted, 0) = 0" : "1"
        let hasSubject = exists(db, table: "messages", column: "subject") && exists(db, table: "subjects", column: "subject")
        let subject = hasSubject ? "IFNULL(s.subject, '')" : "''"
        let subjectJoin = hasSubject ? "LEFT JOIN subjects s ON m.subject = s.ROWID" : ""
        let hasSender = exists(db, table: "messages", column: "sender")
        let hasAddress = hasSender && exists(db, table: "addresses", column: "address")
        let address = hasAddress ? "IFNULL(a.address, '')" : "''"
        let comment = hasAddress && exists(db, table: "addresses", column: "comment") ? "IFNULL(a.comment, '')" : "''"
        let addressJoin = hasAddress ? "LEFT JOIN addresses a ON m.sender = a.ROWID" : ""
        let mailboxURL = hasURL ? "IFNULL(mb.url, '')" : "''"
        // Sent mailboxes are classified in Swift (isSentMailbox), not by a SQL substring match.
        var sentIDs: [Int64] = []
        if hasURL {
            var list: OpaquePointer?
            guard sqlite3_prepare_v2(db, "SELECT ROWID, url FROM mailboxes", -1, &list, nil) == SQLITE_OK else {
                throw IndexFailure.unreadable
            }
            while sqlite3_step(list) == SQLITE_ROW {
                if isSentMailbox(url: text(list, 1)) { sentIDs.append(sqlite3_column_int64(list, 0)) }
            }
            sqlite3_finalize(list)
        }
        let sentList = sentIDs.isEmpty ? "-1" : sentIDs.map(String.init).joined(separator: ",")
        let boxes = hasURL
            ? "SELECT ROWID id, CASE WHEN ROWID IN (\(sentList)) THEN 'sent' ELSE 'inbox' END kind FROM mailboxes WHERE url LIKE '%/INBOX' OR ROWID IN (\(sentList))"
            : "SELECT ROWID id, 'inbox' kind FROM mailboxes WHERE 0"
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
        WITH box AS (\(boxes)),
        member AS (
          SELECT m.ROWID mid, box.kind kind FROM messages m JOIN box ON m.mailbox = box.id
          \(labelMembers))
        SELECT m.ROWID, \(mailboxURL), MAX(member.kind), m.date_received, \(sent), \(messageID),
               \(conversation), \(subject), \(address), \(comment), \(bulk),
               \(category), \(rfcID)
        FROM member
        JOIN messages m ON m.ROWID = member.mid
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        \(globalJoin)
        \(subjectJoin)
        \(addressJoin)
        WHERE \(notDeleted) AND m.date_received > \(since)\(only.map { " AND m.ROWID = \($0)" } ?? "")
        GROUP BY m.ROWID
        ORDER BY m.date_received DESC
        LIMIT \(maxIndexRows + 1)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw IndexFailure.unreadable }
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
        guard step == SQLITE_DONE else { throw IndexFailure.unreadable }
        return (rows, false)
    }

    // MARK: - .emlx files

    /// ROWID → file for every message Mail keeps on disk. A `.partial.emlx` had its
    /// attachments stripped; a full file wins when both exist.
    /// Only the `V*` folder whose Envelope Index is read, so rowids can't collide across versions.
    static func emlxFiles(_ mailRoot: String) -> [Int64: (path: String, partial: Bool)] {
        var out: [Int64: (path: String, partial: Bool)] = [:]
        guard let base = versionDirectory(mailRoot),
              let walker = FileManager.default.enumerator(atPath: base) else { return out }
        while let rel = walker.nextObject() as? String {
            guard rel.hasSuffix(".emlx"), rel.contains("/Messages/") else { continue }
            let name = (rel as NSString).lastPathComponent
            guard let id = Int64(name.prefix { $0.isNumber }), id > 0 else { continue }
            let partial = name.hasSuffix(".partial.emlx")
            if let have = out[id], !have.partial { continue }
            out[id] = ("\(base)/\(rel)", partial)
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
