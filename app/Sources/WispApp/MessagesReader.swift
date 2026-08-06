import Foundation
import SQLite3

// Reads recent iMessage/SMS history directly from ~/Library/Messages/chat.db
// and pushes it to the backend, which summarizes with gemma — same pattern as
// Mail/Calendar. Unlike Mail, Messages.app's AppleScript dictionary can list
// chat names but CANNOT read message content at all, so this reads the SQLite
// database directly instead of scripting the app.
//
// Requires Full Disk Access for Wisp.app (chat.db is one of the classic
// FDA-protected paths). Opens READ-ONLY: Messages.app itself keeps this
// database open continuously in WAL mode, which safely supports concurrent
// readers, so a read-only connection here can't corrupt or lock it.
final class MessagesReader {
    private var timer: Timer?
    private let dbPath = (NSHomeDirectory() as NSString)
        .appendingPathComponent("Library/Messages/chat.db")

    // macOS (Sierra+) stores message.date as nanoseconds since the Apple/Cocoa
    // epoch (2001-01-01), not Unix epoch (1970-01-01). This is the fixed
    // offset between them in seconds — same constant used on the AppleScript
    // side for Mail's date received.
    private static let appleEpochOffset: Double = 978307200

    func start() {
        sync()
        for d in [3.0, 8.0, 15.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in self?.sync() }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    func sync() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            guard let rows = self.readRecentMessages() else {
                self.post(lines: "", diagnostics: ["available": false,
                                                    "reason": "chat.db not readable (Full Disk Access?)"])
                return
            }
            self.post(lines: rows.joined(separator: "\n"),
                     diagnostics: ["available": true, "count": rows.count])
        }
    }

    // Returns lines of "epochSecs | context | text", newest first, or nil if
    // the database couldn't be opened at all (no FDA / file missing).
    //
    // Scoped by TIME (one year) rather than a flat row count. The previous
    // `LIMIT 300` was roughly a WEEK of traffic on an active account, which
    // made `build_profile` describe the user almost entirely from whatever
    // single conversation happened to be busy that week — the reported
    // "profile only knows about one event" bug. `limit` remains as a safety
    // ceiling so a heavy account can't produce an unbounded payload, but it's
    // now far above the expected year's volume instead of the binding
    // constraint. This is a local SQLite read (no AppleScript, no network),
    // so a year's rows costs milliseconds, not the minutes Mail's scan does.
    private func readRecentMessages(days: Int = 365, limit: Int = 20000) -> [String]? {
        var db: OpaquePointer?
        // SQLITE_OPEN_READONLY: never write to a database Messages.app owns.
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        // associated_message_type = 0 filters out tapback reactions ("Liked a
        // message") and similar noise, keeping only real message content.
        //
        // The date predicate converts the CUTOFF into Apple-epoch nanoseconds
        // (rather than converting every row's date to Unix seconds) so the
        // comparison stays sargable against message.date's index — important
        // now that this scans a year rather than the newest 300 rows.
        let cutoffNs = Int64((Date().timeIntervalSince1970
                              - Double(days) * 86400 - Self.appleEpochOffset) * 1_000_000_000)
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
        LIMIT \(limit)
        """
        // Participants per chat, so a conversation can be LABELLED usefully.
        // chat.display_name is only set for groups the user manually named, so
        // without this: 1:1 chats surfaced as a bare phone number and unnamed
        // groups as an opaque "chat178595049381205392" — measured on this Mac,
        // 33 of 62 conversations were bare numbers and 5 were opaque IDs. The
        // summarizer had nothing to distinguish them by, which is why group
        // chats blurred together and 1:1 conversations got skipped entirely.
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
            guard let text, !text.isEmpty else { continue }   // attachment-only, undecodable, etc.

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

    /// chat.ROWID -> its participant handles. One extra cheap query; the join
    /// table is tiny compared to `message`.
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
    /// Groups are explicitly tagged "Group" and, when unnamed, identified by
    /// their members rather than an opaque `chat<digits>` id — so two different
    /// unnamed group chats stop looking identical. 1:1 chats resolve to the
    /// other person's handle, which the backend then swaps for their contact
    /// name (see imessage_tools.resolve_contact), instead of staying a raw
    /// phone number the model would treat as noise and skip.
    static func label(chatName: String?, chatIdentifier: String?,
                      members: [String], fallback: String) -> String {
        let named = (chatName?.isEmpty == false) ? chatName! : ""
        let isGroup = members.count > 1
        if !named.isEmpty { return isGroup ? "Group \"\(named)\"" : named }
        if isGroup {
            // Cap the member list so a 30-person thread doesn't dominate every
            // line; the count keeps it identifiable and stable.
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
    // `text`. Two decode attempts: the direct secure-coding path, then a
    // manual unarchiver reading the well-known "NSString" key, since some
    // real-world archives don't round-trip through the class-based API.
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

    private func post(lines: String, diagnostics: [String: Any]) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/messages")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["lines": lines,
                                                                    "diagnostics": diagnostics])
        WispSession.shared.dataTask(with: req).resume()
    }
}
