import Foundation
import AppKit
import SQLite3

// Reads recent iMessage/SMS history directly from ~/Library/Messages/chat.db
// and pushes it to the backend, which summarizes with the resident model — same pattern as
// Mail/Calendar. Unlike Mail, Messages.app's AppleScript dictionary can list
// chat names but CANNOT read message content at all, so this reads the SQLite
// database directly instead of scripting the app.
//
// Requires Full Disk Access for Wisp.app (chat.db is one of the classic
// FDA-protected paths). Opens READ-ONLY: Messages.app itself keeps this
// database open continuously in WAL mode, which safely supports concurrent
// readers, so a read-only connection here can't corrupt or lock it.
final class MessagesReader {
    struct MessageScan {
        let lines: [String]
        let structured: [String]
        let attempted: Int
        let skipped: Int
        let truncated: Int
    }

    private struct DecodedBody {
        let text: String
        let links: [String]
    }

    private struct LinkExtraction {
        let links: [[String: String]]
        let partial: Bool
    }

    private struct LiteralScan {
        let url: String?
        let end: String.Index
        let partial: Bool
    }

    private static let structuredLimit = 2000
    private static let structuredByteLimit = 4_000_000
    private static let structuredTextLimit = 8192
    private static let structuredLinkLimit = 32
    private static let structuredURLLimit = 8192
    private static let literalURLStart = try! NSRegularExpression(
        pattern: #"https?://"#, options: [.caseInsensitive])
    private var timer: Timer?
    private var inFlight = false
    private let dbPath: String

    init(dbPath: String = (NSHomeDirectory() as NSString)
        .appendingPathComponent("Library/Messages/chat.db")) {
        self.dbPath = dbPath
    }

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
        // Requests for a current read arrive from several places at once (the
        // Daily Summary's readiness wait, a message tool with a cold cache, the
        // periodic timer). A read already in flight answers all of them; running
        // it again only makes each one slower. Same guard as MailReader.sync.
        guard !inFlight else { return }
        inFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            defer { self?.inFlight = false }
            guard let self else { return }
            guard let scan = self.readRecentMessages() else {
                self.post(lines: "", diagnostics: ["available": false,
                                                    "reason": "chat.db not readable (Full Disk Access?)"])
                return
            }
            let coverage: [String: Any] = [
                "version": 1, "kind": "coverage", "attempted": scan.attempted,
                "emitted": scan.structured.count, "skipped": scan.skipped,
                "truncated": scan.truncated,
                "limit": Self.structuredLimit,
                "byte_limit": Self.structuredByteLimit,
                "window_days": 365, "row_limit": 20000,
                "reached_row_limit": scan.attempted == 20000,
            ]
            let structured = [Self.wire(coverage)] + scan.structured
            // V3 occupies the leading, reader-controlled block. Message text
            // can never precede it or append a trusted record to that block.
            self.post(lines: (structured + scan.lines).joined(separator: "\n"),
                      diagnostics: ["available": true, "count": scan.lines.count])
        }
    }

    // Returns lines of "epochSecs | U/R | chatID | context | text", newest first, or nil if
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
    func readRecentMessages(days: Int = 365, limit: Int = 20000) -> MessageScan? {
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
        // Older stores may lack GUID columns. Keep V2 available and make the
        // missing structured identity explicit instead of inventing one.
        let messageGUID = hasColumn(db, table: "message", name: "guid") ? "m.guid" : "NULL"
        let chatGUID = hasColumn(db, table: "chat", name: "guid") ? "c.guid" : "NULL"
        let dateEdited = hasColumn(db, table: "message", name: "date_edited") ? "m.date_edited" : "0"
        let sql = """
        SELECT m.date, m.text, m.attributedBody, m.is_from_me,
               h.id AS handle_id, c.display_name AS chat_name, c.chat_identifier,
               c.ROWID AS chat_id, COALESCE(m.is_read, 0) AS is_read,
               m.ROWID AS message_id, COALESCE(m.handle_id, 0) AS handle_row_id,
               \(messageGUID) AS message_guid, \(chatGUID) AS chat_guid,
               \(dateEdited) AS date_edited
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
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        var out: [String] = []
        var structured: [String] = []
        var structuredBytes = 0
        var attempted = 0
        var skipped = 0
        var truncated = 0
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            attempted += 1
            let dateNs = sqlite3_column_int64(stmt, 0)
            guard dateNs > 0 else { skipped += 1; continue }
            let epochSecs = Double(dateNs) / 1_000_000_000 + Self.appleEpochOffset

            let attributed = decodeAttributedBody(stmt, 2)
            let attributedUnreadable = sqlite3_column_type(stmt, 2) != SQLITE_NULL && attributed == nil
            let wasEdited = sqlite3_column_int64(stmt, 13) > 0
            let text = wasEdited ? (attributed?.text ?? columnText(stmt, 1))
                                 : (columnText(stmt, 1) ?? attributed?.text)
            guard let text, !text.isEmpty else { skipped += 1; continue }

            let isFromMe = sqlite3_column_int(stmt, 3) == 1
            let handle = columnText(stmt, 4)
            let chatName = columnText(stmt, 5)
            let chatIdentifier = columnText(stmt, 6)
            let chatId = sqlite3_column_int64(stmt, 7)
            let isUnread = !isFromMe && sqlite3_column_int(stmt, 8) == 0
            let messageId = sqlite3_column_int64(stmt, 9)
            let handleRowId = sqlite3_column_int64(stmt, 10)
            let messageGuid = columnText(stmt, 11)
            let chatGuid = columnText(stmt, 12)
            // Namespace each SQLite table explicitly. Their ROWIDs are only
            // unique within one table, so a bare/negative number can merge an
            // orphan handle and an unrelated orphan message.
            let conversationId: String
            if chatId > 0 {
                conversationId = "chat:\(chatId)"
            } else if handleRowId > 0 {
                conversationId = "handle:\(handleRowId)"
            } else {
                conversationId = "message:\(messageId)"
            }

            let who = isFromMe ? "Me" : (handle ?? "Unknown")
            let members = participants[chatId] ?? []
            let context = Self.label(chatName: chatName, chatIdentifier: chatIdentifier,
                                     members: members, fallback: who)

            let oneLine = Self.flattenLegacyLine(text)
            out.append("V2 | \(epochSecs) | \(isUnread ? "U" : "R") | \(conversationId) | \(context) | \(who): \(oneLine)")
            guard structured.count < Self.structuredLimit else { truncated += 1; continue }
            let clipped = String(text.prefix(Self.structuredTextLimit))
            if clipped.count < text.count { truncated += 1 }
            let extracted = Self.actualLinks(text: text,
                                             visibleCount: clipped.count,
                                             attributed: attributed?.links ?? [])
            let links = extracted.links.filter { ($0["url"] ?? "").count <= Self.structuredURLLimit }
            if extracted.partial || links.count < extracted.links.count
                    || links.count > Self.structuredLinkLimit { truncated += 1 }
            let record: [String: Any] = [
                "guid": messageGuid as Any? ?? NSNull(),
                "conversation": chatGuid as Any? ?? NSNull(),
                "sender": who, "direction": isFromMe ? "outgoing" : "incoming",
                "timestamp": epochSecs, "text": clipped,
                "links": Array(links.prefix(Self.structuredLinkLimit)),
            ]
            let source: [String: Any] = [
                "kind": "messages", "message_guid": messageGuid as Any? ?? NSNull(),
                "chat_guid": chatGuid as Any? ?? NSNull(),
                // A GUID is a read-only native locator. There is no supported
                // Messages URL for opening an existing message by GUID.
                "navigation_url": NSNull(),
            ]
            let wire = Self.wire([
                "version": 1, "kind": "record", "record": record, "source": source,
                "coverage": ["text": clipped.count < text.count || (wasEdited && attributed == nil)
                                 ? "partial" : "complete",
                             "links": clipped.count < text.count || attributedUnreadable
                                 || extracted.partial || links.count < extracted.links.count
                                 || links.count > Self.structuredLinkLimit
                                 ? "partial" : "complete"],
            ])
            if !wire.isEmpty && structuredBytes + wire.utf8.count <= Self.structuredByteLimit {
                structured.append(wire)
                structuredBytes += wire.utf8.count
            } else {
                truncated += 1
            }
        }
        return step == SQLITE_DONE ? MessageScan(lines: out, structured: structured,
                                                attempted: attempted, skipped: skipped,
                                                truncated: truncated) : nil
    }

    private func hasColumn(_ db: OpaquePointer?, table: String, name: String) -> Bool {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK else {
            return false
        }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            if columnText(stmt, 1) == name { return true }
        }
        return false
    }

    private static func wire(_ object: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return "" }
        return "V3 | \(json)"
    }

    private static func flattenLegacyLine(_ text: String) -> String {
        // Python's splitlines recognizes more than LF. All of them are data
        // within a V2 body, never message or metadata delimiters.
        let separators = CharacterSet(charactersIn: "\n\r\u{000B}\u{000C}\u{001C}\u{001D}\u{001E}\u{0085}\u{2028}\u{2029}")
        return text.unicodeScalars.map { separators.contains($0) ? " " : String($0) }.joined()
    }

    private static func actualLinks(text: String, visibleCount: Int,
                                    attributed: [String]) -> LinkExtraction {
        // Only explicit HTTP(S) starts and NSLink attributes are evidence.
        // Inspect past the text clip so a URL cut there is omitted, not changed.
        let examined = String(text.prefix(structuredTextLimit + structuredURLLimit + 1))
        let visibleEnd = examined.index(examined.startIndex, offsetBy: visibleCount)
        let range = NSRange(examined.startIndex..<examined.endIndex, in: examined)
        var links: [[String: String]] = []
        var seen = Set<String>()
        var partial = false
        var consumedUntil = examined.startIndex
        for match in literalURLStart.matches(in: examined, range: range) {
            guard let start = Range(match.range, in: examined)?.lowerBound,
                  start >= consumedUntil else { continue }
            let scan = scanLiteralURL(in: examined, from: start, visibleEnd: visibleEnd,
                                      sourceContinues: examined.count < text.count)
            consumedUntil = scan.end
            partial = partial || scan.partial
            if let url = scan.url, seen.insert("text:\(url)").inserted {
                links.append(["url": url, "provenance": "literal_text"])
            }
        }
        // NSLink attributes have no surviving source ranges here. On clipped
        // text, omit them rather than claim a link belongs to the visible part.
        for url in (visibleCount == text.count ? attributed : [])
                where seen.insert("attributed:\(url)").inserted {
            links.append(["url": url, "provenance": "attributed_link"])
        }
        return LinkExtraction(links: links, partial: partial)
    }

    private static func closingDelimiter(_ opener: Character) -> Character? {
        switch opener {
        case "(": return ")"
        case "[": return "]"
        case "{": return "}"
        case "<": return ">"
        case "\"", "'": return opener
        default: return nil
        }
    }

    private static func startsAnotherURL(_ text: String, after separator: String.Index) -> Bool {
        var cursor = text.index(after: separator)
        while cursor < text.endIndex, closingDelimiter(text[cursor]) != nil {
            cursor = text.index(after: cursor)
        }
        let prefix = String(text[cursor...].prefix(8)).lowercased()
        return prefix.hasPrefix("https://") || prefix.hasPrefix("http://")
    }

    private static func scanLiteralURL(in text: String, from start: String.Index,
                                       visibleEnd: String.Index, sourceContinues: Bool) -> LiteralScan {
        // External wrappers are collected nearest first. URI-internal pairs
        // are tracked separately, so Function_(math) stays inside the URL while
        // the closing ')' of ((URL)) stays outside it.
        var wrappers: [Character] = []
        var before = start
        while before > text.startIndex {
            let prior = text.index(before: before)
            guard let close = closingDelimiter(text[prior]) else { break }
            wrappers.append(close)
            before = prior
        }
        if wrappers.isEmpty && before > text.startIndex {
            let prior = text[text.index(before: before)]
            if prior.isLetter || prior.isNumber || prior == "_" {
                return LiteralScan(url: nil, end: text.index(after: start), partial: true)
            }
        }

        var cursor = start
        var internalClosers: [Character] = []
        var closedWrapper = false
        var partial = false
        var inQueryOrFragment = false
        while cursor < text.endIndex {
            let char = text[cursor]
            if char.isWhitespace || char.isNewline { break }
            if (char == "?" || char == "#") { inQueryOrFragment = true }
            // A second scheme in a query or fragment can be part of this
            // URL's value. Splitting there invents a different destination.
            if (char == "," || char == ";") && !inQueryOrFragment
                    && startsAnotherURL(text, after: cursor) { break }
            if char == "'" && cursor < text.index(before: text.endIndex) {
                let next = text[text.index(after: cursor)]
                if next.isLetter || next.isNumber {
                    cursor = text.index(after: cursor)
                    continue
                }
            }
            if let close = closingDelimiter(char), char != "\"" && char != "'" && char != "<" {
                internalClosers.append(close)
            } else if char == ")" || char == "]" || char == "}" || char == ">"
                        || char == "\"" || char == "'" || char == "<" {
                if internalClosers.last == char {
                    internalClosers.removeLast()
                } else if internalClosers.isEmpty && wrappers.first == char {
                    closedWrapper = true
                    break
                } else if wrappers.isEmpty && (char == ")" || char == "]" || char == "}") {
                    // Unmatched prose closer is an explicit boundary, though
                    // its intent remains ambiguous without a wrapper.
                    partial = true
                    break
                } else {
                    // Quotes and mismatched wrappers are ambiguous source
                    // boundaries. A shortened prefix is not a valid link.
                    return LiteralScan(url: nil, end: cursor, partial: true)
                }
            }
            cursor = text.index(after: cursor)
        }
        if !internalClosers.isEmpty || (!wrappers.isEmpty && !closedWrapper)
                || (cursor == text.endIndex && sourceContinues) {
            return LiteralScan(url: nil, end: cursor, partial: true)
        }
        if closedWrapper {
            var after = text.index(after: cursor)
            for expected in wrappers.dropFirst() {
                guard after < text.endIndex, text[after] == expected else {
                    return LiteralScan(url: nil, end: cursor, partial: true)
                }
                after = text.index(after: after)
            }
        }
        guard cursor <= visibleEnd else { return LiteralScan(url: nil, end: cursor, partial: true) }
        var url = String(text[start..<cursor])
        if wrappers.isEmpty {
            let original = url
            while let last = url.last, ".,;!".contains(last) { url.removeLast() }
            partial = partial || url != original
        }
        guard !url.isEmpty && url.count <= structuredURLLimit else {
            return LiteralScan(url: nil, end: cursor, partial: true)
        }
        return LiteralScan(url: url, end: cursor, partial: partial)
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
    private func decodeAttributedBody(_ stmt: OpaquePointer?, _ idx: Int32) -> DecodedBody? {
        guard let blob = sqlite3_column_blob(stmt, idx) else { return nil }
        let len = Int(sqlite3_column_bytes(stmt, idx))
        guard len > 0 else { return nil }
        let data = Data(bytes: blob, count: len)

        if let attr = try? NSKeyedUnarchiver.unarchivedObject(
            ofClasses: [NSAttributedString.self, NSMutableAttributedString.self,
                        NSString.self, NSURL.self], from: data) as? NSAttributedString {
            var links: [String] = []
            attr.enumerateAttribute(.link, in: NSRange(location: 0, length: attr.length)) { value, _, _ in
                if let url = value as? URL { links.append(url.absoluteString) }
                else if let url = value as? String { links.append(url) }
            }
            return DecodedBody(text: attr.string, links: links)
        }
        if let unarchiver = try? NSKeyedUnarchiver(forReadingFrom: data) {
            unarchiver.requiresSecureCoding = false
            if let s = unarchiver.decodeObject(forKey: "NSString") as? String {
                unarchiver.finishDecoding()
                return DecodedBody(text: s, links: [])
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
        URLSession.shared.dataTask(with: req).resume()
    }
}
