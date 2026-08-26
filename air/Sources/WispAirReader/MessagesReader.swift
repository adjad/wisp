import Foundation
import SQLite3

// Reads new iMessage/SMS history straight from ~/Library/Messages/chat.db.
//
// Ported from the Pro's MessagesReader. Messages.app's AppleScript dictionary
// can list chat names but cannot read message content at all, so this reads the
// SQLite database directly instead of scripting the app — which is why this
// reader needs Full Disk Access rather than an Automation grant.
//
// Opens READ-ONLY. Messages.app keeps this database open continuously in WAL
// mode, which safely supports concurrent readers, so a read-only connection
// here cannot corrupt or lock it.
//
// Incremental by cursor, which costs nothing to do properly here: the SQL
// already had a date predicate, so this is a changed constant rather than the
// early-termination scan Mail needs.
final class MessagesReader {
    private var timer: Timer?
    private let dbPath = (NSHomeDirectory() as NSString)
        .appendingPathComponent("Library/Messages/chat.db")

    // macOS stores message.date as nanoseconds since the Apple/Cocoa epoch
    // (2001-01-01), not the Unix epoch. Same constant the Mail AppleScript uses.
    private static let appleEpochOffset: Double = 978307200

    private let maxLookbackDays = 7.0
    private let rowLimit = 2000

    func start() {
        sync()
        for d in [5.0, 15.0, 40.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in self?.sync() }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func sync() {
        Config.get("cursors") { [weak self] json in
            guard let self else { return }
            let scanFrom = ((json?["messages"] as? [String: Any])?["scan_from"] as? Double) ?? 0
            self.run(scanFrom: scanFrom)
        }
    }

    private func run(scanFrom: Double) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let floor = Date().timeIntervalSince1970 - self.maxLookbackDays * 86400
            let cutoff = max(scanFrom, floor)

            guard let rows = self.readMessages(sinceEpoch: cutoff) else {
                Config.post("sync/messages", body: [
                    "lines": "",
                    "diagnostics": ["available": false,
                                    "reason": "chat.db not readable — grant Full Disk Access to WispAirReader"],
                ])
                return
            }
            Config.post("sync/messages", body: [
                "lines": rows.joined(separator: "\n"),
                "diagnostics": ["available": true, "count": rows.count,
                                "scanned_from": cutoff],
            ])
            NSLog("[WispAirReader] messages: pushed \(rows.count) lines (cutoff \(Int(cutoff)))")
        }
    }

    /// Lines of "epochSecs | context | who: text", or nil if the database
    /// couldn't be opened at all (no Full Disk Access / file missing).
    private func readMessages(sinceEpoch: Double) -> [String]? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        // The cutoff is converted INTO Apple-epoch nanoseconds rather than
        // converting every row's date to Unix seconds, so the comparison stays
        // sargable against message.date's index.
        //
        // associated_message_type = 0 filters out tapback reactions ("Liked a
        // message") and similar noise, keeping only real message content.
        let cutoffNs = Int64((sinceEpoch - Self.appleEpochOffset) * 1_000_000_000)
        let sql = """
        SELECT m.date, m.text, m.attributedBody, m.is_from_me,
               h.id AS handle_id, c.display_name AS chat_name, c.chat_identifier,
               c.ROWID AS chat_id
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        LEFT JOIN chat c ON cmj.chat_id = c.ROWID
        WHERE m.associated_message_type = 0 AND m.date > \(cutoffNs)
        ORDER BY m.date DESC
        LIMIT \(rowLimit)
        """
        let participants = readParticipants(db)

        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            let dateNs = sqlite3_column_int64(stmt, 0)
            guard dateNs > 0 else { continue }
            let epochSecs = Double(dateNs) / 1_000_000_000 + Self.appleEpochOffset

            let text = columnText(stmt, 1) ?? decodeAttributedBody(stmt, 2)
            guard let text, !text.isEmpty else { continue }   // attachment-only, undecodable

            let isFromMe = sqlite3_column_int(stmt, 3) == 1
            let handle = columnText(stmt, 4)
            let chatName = columnText(stmt, 5)
            let chatIdentifier = columnText(stmt, 6)
            let chatId = sqlite3_column_int64(stmt, 7)

            let who = isFromMe ? "Me" : (handle ?? "Unknown")
            let members = participants[chatId] ?? []
            let context = Self.label(chatName: chatName, chatIdentifier: chatIdentifier,
                                     members: members, fallback: who)

            let oneLine = text.replacingOccurrences(of: "\n", with: " ")
            out.append("\(epochSecs) | \(context) | \(who): \(oneLine)")
        }
        return out
    }

    /// chat.ROWID -> participant handles. One extra cheap query; the join table
    /// is tiny compared to `message`.
    private func readParticipants(_ db: OpaquePointer?) -> [Int64: [String]] {
        let sql = """
        SELECT chj.chat_id, h.id
        FROM chat_handle_join chj
        JOIN handle h ON chj.handle_id = h.ROWID
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [:] }
        defer { sqlite3_finalize(stmt) }
        var out: [Int64: [String]] = [:]
        while sqlite3_step(stmt) == SQLITE_ROW {
            let chatId = sqlite3_column_int64(stmt, 0)
            guard let h = sqlite3_column_text(stmt, 1) else { continue }
            out[chatId, default: []].append(String(cString: h))
        }
        return out
    }

    /// A conversation label the summarizer can actually reason about.
    ///
    /// chat.display_name is only set for groups the user manually named, so
    /// without this, 1:1 chats surface as a bare phone number and unnamed
    /// groups as an opaque "chat178595049381205392" — measured on the Pro, 33
    /// of 62 conversations were bare numbers and 5 were opaque IDs, which left
    /// the summarizer nothing to tell them apart by.
    static func label(chatName: String?, chatIdentifier: String?,
                      members: [String], fallback: String) -> String {
        let named = (chatName?.isEmpty == false) ? chatName! : ""
        let isGroup = members.count > 1
        if !named.isEmpty { return isGroup ? "Group \"\(named)\"" : named }
        if isGroup {
            let shown = members.sorted().prefix(4).joined(separator: ", ")
            let more = members.count > 4 ? ", +\(members.count - 4) more" : ""
            return "Group of \(members.count) (\(shown)\(more))"
        }
        if let only = members.first, !only.isEmpty { return only }
        if let ci = chatIdentifier, !ci.isEmpty, !ci.hasPrefix("chat") { return ci }
        return fallback
    }

    private func columnText(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let c = sqlite3_column_text(stmt, idx) else { return nil }
        return String(cString: c)
    }

    // Modern Messages.app often stores rich/edited text as an NSKeyedArchiver-
    // encoded NSAttributedString blob (`attributedBody`) instead of plain
    // `text`. Two decode attempts: the direct secure-coding path, then a manual
    // unarchiver reading the well-known "NSString" key, since some real-world
    // archives don't round-trip through the class-based API.
    private func decodeAttributedBody(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let blob = sqlite3_column_blob(stmt, idx) else { return nil }
        let len = Int(sqlite3_column_bytes(stmt, idx))
        guard len > 0 else { return nil }
        let data = Data(bytes: blob, count: len)

        if let attr = try? NSKeyedUnarchiver.unarchivedObject(
            ofClasses: [NSAttributedString.self, NSString.self], from: data) as? NSAttributedString {
            return attr.string
        }
        if let unarchiver = try? NSKeyedUnarchiver(forReadingFrom: data) {
            unarchiver.requiresSecureCoding = false
            if let s = unarchiver.decodeObject(forKey: "NSString") as? String {
                unarchiver.finishDecoding()
                return s
            }
            unarchiver.finishDecoding()
        }
        return nil
    }
}
