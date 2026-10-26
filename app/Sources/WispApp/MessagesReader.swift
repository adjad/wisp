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
        /// False when the body came from the typedstream scan, which recovers
        /// the text but not its NSLink attributes.
        var linksKnown = true
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
    /// Why the last `readRecentMessages` returned nil, from the error itself.
    private(set) var lastFailure: MessagesReadFailure?
    private let backendRetry = NativeBackendRetry()

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
                let failure = self.lastFailure ?? MessagesReadFailure.classify(
                    systemErrno: 0, sqliteCode: SQLITE_ERROR, sqliteMessage: "")
                self.post(lines: "", diagnostics: failure.diagnostics)
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
            // `skipped` counts rows with no readable text at all, so a decoding
            // gap shows up in diagnostics instead of as quietly missing messages.
            self.post(lines: (structured + scan.lines).joined(separator: "\n"),
                      diagnostics: ["available": true, "count": scan.lines.count,
                                    "attempted": scan.attempted, "skipped": scan.skipped])
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
        lastFailure = nil
        // SQLITE_OPEN_READONLY: never write to a database Messages.app owns.
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            lastFailure = MessagesReadFailure.from(db, path: dbPath)
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
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            lastFailure = MessagesReadFailure.from(db, path: dbPath)
            return nil
        }
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
            // A typedstream body gives the text but not its link attributes, so
            // its links are as unknown as an unreadable body's.
            let attributedUnreadable = sqlite3_column_type(stmt, 2) != SQLITE_NULL
                && (attributed == nil || attributed?.linksKnown == false)
            let wasEdited = sqlite3_column_int64(stmt, 13) > 0
            let text = wasEdited ? (attributed?.text ?? columnText(stmt, 1))
                                 : (columnText(stmt, 1) ?? attributed?.text)
            guard let text, !text.isEmpty else { skipped += 1; continue }
            // An unedited row may contain a stale attributedBody alongside a
            // newer plain text. NSLink metadata only describes its own body.
            let attributedBodyMismatch = attributed.map { $0.text != text } ?? false

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
                                             attributed: attributedBodyMismatch ? [] : (attributed?.links ?? []))
            let links = extracted.links.filter { ($0["url"] ?? "").count <= Self.structuredURLLimit }
            if attributedBodyMismatch || extracted.partial || links.count < extracted.links.count
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
                                 || attributedBodyMismatch
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
        guard step == SQLITE_DONE else {
            lastFailure = MessagesReadFailure.from(db, path: dbPath)
            return nil
        }
        return MessageScan(lines: out, structured: structured, attempted: attempted,
                           skipped: skipped, truncated: truncated)
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
        var inQueryOrFragment = false
        while cursor < text.endIndex {
            let char = text[cursor]
            if char.isWhitespace || char.isNewline { break }
            if (char == "?" || char == "#") { inQueryOrFragment = true }
            // A second scheme after path punctuation could be an adjacent
            // link or part of one URI. Neither split is source-certain, so
            // omit the whole token unless a wrapper already ended this URL.
            if (char == "," || char == ";") && !inQueryOrFragment
                    && startsAnotherURL(text, after: cursor) {
                var end = cursor
                while end < text.endIndex && !text[end].isWhitespace && !text[end].isNewline {
                    end = text.index(after: end)
                }
                return LiteralScan(url: nil, end: end, partial: true)
            }
            if char == "'" && cursor < text.index(before: text.endIndex) {
                let next = text[text.index(after: cursor)]
                if next.isLetter || next.isNumber {
                    cursor = text.index(after: cursor)
                    continue
                }
            }
            if char == "'" && wrappers.first == "'" {
                // Another quote before whitespace makes this one ambiguous:
                // it may be URI data followed by the real closing quote.
                var later = text.index(after: cursor)
                var anotherQuote = false
                while later < text.endIndex && !text[later].isWhitespace
                        && !text[later].isNewline {
                    anotherQuote = anotherQuote || text[later] == "'"
                    later = text.index(after: later)
                }
                let followsWithoutSpace = cursor < text.index(before: text.endIndex)
                    && !text[text.index(after: cursor)].isWhitespace
                    && !text[text.index(after: cursor)].isNewline
                if anotherQuote || followsWithoutSpace {
                    return LiteralScan(url: nil, end: later, partial: true)
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
                    // A bare closer may be URI data or prose punctuation.
                    // Never publish the shorter prefix as its destination.
                    var end = cursor
                    while end < text.endIndex && !text[end].isWhitespace
                            && !text[end].isNewline {
                        end = text.index(after: end)
                    }
                    return LiteralScan(url: nil, end: end, partial: true)
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
            if after < text.endIndex {
                let next = text[after]
                let nextEnd = text.index(after: after)
                let separated = next.isWhitespace || next.isNewline
                let sentenceEnd = ".,;!".contains(next)
                    && (nextEnd == text.endIndex || text[nextEnd].isWhitespace
                        || text[nextEnd].isNewline)
                let adjacentWrapped = (next == "," || next == ";")
                    && nextEnd < text.endIndex
                    && closingDelimiter(text[nextEnd]) != nil
                    && startsAnotherURL(text, after: after)
                if !separated && !sentenceEnd && !adjacentWrapped {
                    // An earlier matching closer may be URI data; a later
                    // closer in this token may be the real wrapper boundary.
                    var end = after
                    while end < text.endIndex && !text[end].isWhitespace
                            && !text[end].isNewline {
                        end = text.index(after: end)
                    }
                    return LiteralScan(url: nil, end: end, partial: true)
                }
            }
        }
        guard cursor <= visibleEnd else { return LiteralScan(url: nil, end: cursor, partial: true) }
        let url = String(text[start..<cursor])
        if wrappers.isEmpty, let last = url.last, ".,;!".contains(last) {
            // These characters are all legal URI data. With no explicit
            // wrapper, punctuation trimming would change the destination.
            return LiteralScan(url: nil, end: cursor, partial: true)
        }
        guard !url.isEmpty && url.count <= structuredURLLimit else {
            return LiteralScan(url: nil, end: cursor, partial: true)
        }
        return LiteralScan(url: url, end: cursor, partial: false)
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

    // Modern Messages.app often stores rich/edited text as an archived
    // NSAttributedString blob (`attributedBody`) instead of plain `text`.
    // Three decode attempts: the direct secure-coding path, then a manual
    // unarchiver reading the well-known "NSString" key, then the typedstream
    // scan (TypedStreamText), since most real rows use the older typedstream
    // format that neither keyed path reads; those rows used to be skipped.
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
        if let text = TypedStreamText.decode(data) {
            return DecodedBody(text: text, links: [], linksKnown: false)
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
        // A backend that isn't listening yet gets a fresh read soon, not the
        // next 5-minute tick.
        URLSession.shared.dataTask(with: req) { [weak self] _, _, error in
            self?.backendRetry.handle(error: error) { self?.sync() }
        }.resume()
    }
}

/// The text of a Messages `attributedBody` stored as a typedstream (the format
/// of NSArchiver, which Messages still writes), without unarchiving it.
///
/// Rules:
/// - IMPORTANT: never hand the blob to NSUnarchiver. It instantiates whatever
///   classes the stream names, and part of this blob comes from other people.
///   The scan below only reads bytes.
/// - The text is the first string after the "NSString" class name: the class
///   header, then a "+" type tag, then a length (one byte below 0x80; 0x81 and a
///   2-byte little-endian length; 0x82 and a 4-byte one), then that many UTF-8
///   bytes. This is the layout imessage-exporter reads.
/// - Anything that doesn't fit that layout, or isn't valid UTF-8, is nil, never
///   a guess.
enum TypedStreamText {
    static let header = Data([0x04, 0x0B]) + Data("streamtyped".utf8)
    private static let marker = Data("NSString".utf8)

    /// A mutable body names NSMutableString first, then NSString as its
    /// superclass, and the string follows that NSString entry, so the first
    /// "NSString" serves both. The class names precede the text, so message
    /// text that itself says "NSString" can't be mistaken for one.
    static func decode(_ data: Data) -> String? {
        guard data.starts(with: header), let found = data.range(of: marker) else { return nil }
        return string(in: [UInt8](data), after: found.upperBound - data.startIndex)
    }

    private static func string(in bytes: [UInt8], after start: Int) -> String? {
        // The class header between the name and the "+" tag is a few bytes
        // (version, class reference, object start). Look no further than that.
        let window = bytes[start..<min(bytes.count, start + 8)]
        guard let tag = window.firstIndex(of: 0x2B) else { return nil }
        var cursor = tag + 1
        guard cursor < bytes.count else { return nil }
        let length: Int
        switch bytes[cursor] {
        case 0x00..<0x80:
            length = Int(bytes[cursor]); cursor += 1
        case 0x81:
            guard cursor + 2 < bytes.count else { return nil }
            length = Int(bytes[cursor + 1]) | Int(bytes[cursor + 2]) << 8
            cursor += 3
        case 0x82:
            guard cursor + 4 < bytes.count else { return nil }
            length = (0..<4).reduce(0) { $0 | Int(bytes[cursor + 1 + $1]) << (8 * $1) }
            cursor += 5
        default:
            return nil
        }
        guard length > 0, length <= bytes.count - cursor else { return nil }
        return String(bytes: bytes[cursor..<cursor + length], encoding: .utf8)
    }
}

/// Re-runs a native read whose post found the backend not listening yet, as
/// on a cold launch or a backend restart.
///
/// Rules:
/// - IMPORTANT: only "nothing is listening" failures are retried. An HTTP error
///   or a cancelled request is an answer from the backend, not its absence.
/// - The retry re-reads the source. It never re-sends an old snapshot, which a
///   newer read may already have replaced.
/// - One retry is pending at a time. The delays grow 3, 8, 20, then 60 seconds,
///   about five minutes in all; after that the reader's own timer takes over.
///   Any completed request starts the schedule over.
/// MailReader keeps its own copy of this schedule, so its spliced flow checks
/// compile without this file.
final class NativeBackendRetry {
    static let delays: [TimeInterval] = [3, 8, 20, 60, 60, 60, 60]
    private let lock = NSLock()
    private var attempt = 0
    private var pending = false

    static func isBackendUnreachable(_ error: Error?) -> Bool {
        guard let error = error as? URLError else { return false }
        return [.cannotConnectToHost, .networkConnectionLost, .timedOut,
                .cannotFindHost, .notConnectedToInternet, .dnsLookupFailed].contains(error.code)
    }

    /// After a post: reset on any completed request; schedule `retry` on the
    /// main queue after an unreachable backend, unless one is already pending
    /// or the schedule is spent.
    func handle(error: Error?, retry: @escaping () -> Void) {
        guard error != nil else { reached(); return }
        guard Self.isBackendUnreachable(error), let delay = nextDelay() else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [self] in
            fired()
            retry()
        }
    }

    /// The delay before the next retry, marking it pending, or nil.
    func nextDelay() -> TimeInterval? {
        lock.lock(); defer { lock.unlock() }
        guard !pending, attempt < Self.delays.count else { return nil }
        pending = true
        attempt += 1
        return Self.delays[attempt - 1]
    }

    /// The pending retry is running now.
    func fired() { lock.lock(); pending = false; lock.unlock() }

    /// The backend answered, so the next outage starts the schedule over.
    func reached() { lock.lock(); attempt = 0; lock.unlock() }
}

/// Why chat.db couldn't be read, taken from the error itself, and what the user
/// is told.
///
/// Rules:
/// - IMPORTANT: Full Disk Access only when the error says so: macOS refused the
///   file (EPERM, its privacy denial) or SQLite reports "authorization denied".
///   Any other failure keeps its own reason, so the user is never sent to the
///   wrong setting.
/// - `reason_kind` tells the service and the Brief which case this is; they key
///   the Full Disk Access button on it, never on the wording.
struct MessagesReadFailure: Equatable {
    enum Kind: String { case fullDiskAccess = "full_disk_access", missing, unreadable }

    static let fullDiskAccessReason = "Wisp needs Full Disk Access to read Messages. Open System Settings "
        + "\u{2192} Privacy & Security \u{2192} Full Disk Access and turn on Wisp."

    let kind: Kind
    let reason: String

    var diagnostics: [String: Any] { ["available": false, "reason": reason, "reason_kind": kind.rawValue] }

    static func from(_ db: OpaquePointer?, path: String) -> MessagesReadFailure {
        let message = db.flatMap { sqlite3_errmsg($0) }.map { String(cString: $0) } ?? ""
        let code = db.map { sqlite3_extended_errcode($0) } ?? SQLITE_CANTOPEN
        // SQLite's own errno after a failed open is not reliable (it reports a
        // later syscall), so a file SQLite can't open is opened once more here:
        // that errno is the real reason (EPERM for a privacy denial).
        var systemErrno: Int32 = 0
        if code & 0xff == SQLITE_CANTOPEN {
            let fd = open(path, O_RDONLY)
            if fd >= 0 { close(fd) } else { systemErrno = errno }
        }
        return classify(systemErrno: systemErrno, sqliteCode: code, sqliteMessage: message)
    }

    static func classify(systemErrno: Int32, sqliteCode: Int32, sqliteMessage: String) -> MessagesReadFailure {
        if systemErrno == EPERM || sqliteCode & 0xff == SQLITE_AUTH
            || sqliteMessage.lowercased().contains("authorization denied") {
            return MessagesReadFailure(kind: .fullDiskAccess, reason: fullDiskAccessReason)
        }
        if systemErrno == ENOENT {
            return MessagesReadFailure(kind: .missing,
                                       reason: "No Messages history was found on this Mac (chat.db is missing).")
        }
        let detail = !sqliteMessage.isEmpty ? sqliteMessage
            : systemErrno != 0 ? String(cString: strerror(systemErrno)) : "unknown error"
        return MessagesReadFailure(kind: .unreadable, reason: "Couldn't read Messages: \(detail).")
    }
}
