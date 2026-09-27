// Synthetic SQLite only. Compile with MessagesReader.swift and -lsqlite3.
import Foundation
import AppKit
import SQLite3

enum WispClient {
    static let baseURL = URL(string: "http://127.0.0.1:1")!
}

@main
enum MessagesReaderRegression {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-messages-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("chat.db").path
        let reader = MessagesReader(dbPath: path)
        precondition(reader.readRecentMessages() == nil, "Missing db is unavailable")

        var db: OpaquePointer?
        precondition(sqlite3_open(path, &db) == SQLITE_OK)
        defer { sqlite3_close(db) }
        func sql(_ statement: String) {
            precondition(sqlite3_exec(db, statement, nil, nil, nil) == SQLITE_OK,
                         "Fixture SQL failed: \(statement)")
        }
        precondition(reader.readRecentMessages() == nil, "Missing schema is unavailable")
        sql("CREATE TABLE message (date INTEGER, text TEXT, attributedBody BLOB, " +
            "is_from_me INTEGER, handle_id INTEGER, is_read INTEGER, " +
            "associated_message_type INTEGER, guid TEXT, date_edited INTEGER)")
        sql("CREATE TABLE handle (id TEXT)")
        sql("CREATE TABLE chat (display_name TEXT, chat_identifier TEXT, guid TEXT)")
        sql("CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER)")
        sql("CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER)")
        precondition(reader.readRecentMessages()?.lines.isEmpty == true,
                     "Empty readable store is ready")
        sql("INSERT INTO handle VALUES ('+15555550100')")
        sql("INSERT INTO chat VALUES ('Project', 'chat1', 'chat-guid-1')")
        sql("INSERT INTO chat_handle_join VALUES (1, 1)")
        let date = Int64((Date().timeIntervalSince1970 - 978307200 - 60) * 1_000_000_000)
        sql("INSERT INTO message VALUES (\(date), " +
            "'Schedule at https://schedule.example.test/meet?slot=3 and example.test' || " +
            "char(13) || char(8232) || char(8233) || 'More', " +
            "NULL, 0, 1, 0, 0, 'message-guid-1', 0)")
        sql("INSERT INTO chat_message_join VALUES (1, 1)")

        let edited = NSMutableAttributedString(string: "Edited: book a time")
        edited.addAttribute(.link, value: URL(string: "https://calendar.example.test/booking")!,
                            range: NSRange(location: 8, length: 4))
        let blob = try NSKeyedArchiver.archivedData(withRootObject: edited, requiringSecureCoding: true)
        var stmt: OpaquePointer?
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        precondition(sqlite3_prepare_v2(db, "INSERT INTO message VALUES (?, 'Old version', ?, 1, 1, 1, 0, ?, 1)",
                                        -1, &stmt, nil) == SQLITE_OK)
        sqlite3_bind_int64(stmt, 1, date - 1_000_000_000)
        _ = blob.withUnsafeBytes { bytes in
            sqlite3_bind_blob(stmt, 2, bytes.baseAddress, Int32(blob.count), transient)
        }
        sqlite3_bind_text(stmt, 3, "message-guid-2", -1, transient)
        precondition(sqlite3_step(stmt) == SQLITE_DONE)
        sqlite3_finalize(stmt)
        sql("INSERT INTO chat_message_join VALUES (1, 2)")
        sql("INSERT INTO message VALUES (\(date - 2_000_000_000), '', NULL, 0, 1, 0, 0, 'bad', 0)")
        let crossing = String(repeating: "x", count: 8180) +
            " https://calendar.example.test/?token=123456789ABCDEFGHIJK"
        var crossingStmt: OpaquePointer?
        precondition(sqlite3_prepare_v2(db,
            "INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (?, ?, 0, 1, 0, 'boundary-guid')", -1, &crossingStmt, nil) == SQLITE_OK)
        sqlite3_bind_int64(crossingStmt, 1, date - 3_000_000_000)
        _ = crossing.withCString { sqlite3_bind_text(crossingStmt, 2, $0, -1, transient) }
        precondition(sqlite3_step(crossingStmt) == SQLITE_DONE)
        sqlite3_finalize(crossingStmt)

        let modified = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        let scan = reader.readRecentMessages()!
        precondition(scan.attempted == 4 && scan.skipped == 1
                     && scan.lines.count == 3 && scan.structured.count == 3)
        precondition(scan.lines[0].contains("message: 1") == false)
        precondition(scan.lines[0].contains("Schedule at https://schedule.example.test/meet?slot=3"))
        precondition(!scan.lines[0].contains("\r") && !scan.lines[0].contains("\u{2028}")
                     && !scan.lines[0].contains("\u{2029}"), "V2 bodies cannot add wire lines")
        precondition(scan.lines[1].contains("Edited: book a time")
                     && !scan.lines[1].contains("Old version"), "Edited attributedBody won over stale text")
        let rows = try scan.structured.map { line -> [String: Any] in
            precondition(line.hasPrefix("V3 | "))
            return try JSONSerialization.jsonObject(with: Data(line.dropFirst(5).utf8)) as! [String: Any]
        }
        let first = rows[0]["record"] as! [String: Any]
        precondition(first["guid"] as? String == "message-guid-1")
        precondition(first["conversation"] as? String == "chat-guid-1")
        precondition(first["direction"] as? String == "incoming")
        let firstLinks = first["links"] as! [[String: String]]
        precondition(firstLinks.count == 1 && firstLinks[0]["provenance"] == "literal_text")
        precondition(firstLinks[0]["url"] == "https://schedule.example.test/meet?slot=3")
        let second = rows[1]["record"] as! [String: Any]
        precondition(second["guid"] as? String == "message-guid-2")
        precondition(second["direction"] as? String == "outgoing")
        let secondLinks = second["links"] as! [[String: String]]
        precondition(secondLinks.contains { $0["url"] == "https://calendar.example.test/booking"
            && $0["provenance"] == "attributed_link" }, "Explicit attributed link survived")
        let source = rows[0]["source"] as! [String: Any]
        precondition(source["chat_guid"] as? String == "chat-guid-1"
                     && source["navigation_url"] is NSNull)
        let boundary = rows[2]["record"] as! [String: Any]
        precondition(boundary["guid"] as? String == "boundary-guid")
        precondition((boundary["links"] as! [[String: String]]).isEmpty,
                     "A URL crossing the text clip cannot become a shorter source link")
        let boundaryCoverage = rows[2]["coverage"] as! [String: String]
        precondition(boundaryCoverage["text"] == "partial" && boundaryCoverage["links"] == "partial")
        let after = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        precondition(after == modified, "Read-only scan did not change the database")

        let limited = reader.readRecentMessages(limit: 1)!
        precondition(limited.attempted == 1 && limited.structured.count == 1)
        let literalCases = [
            ("balanced-guid", "See https://en.wikipedia.org/wiki/Function_(mathematics).",
             "https://en.wikipedia.org/wiki/Function_(mathematics)"),
            ("unbalanced-guid", "See https://example.test/report).",
             "https://example.test/report"),
            ("balanced-wrapper-guid", "(https://example.test/part_(one)).",
             "https://example.test/part_(one)"),
            ("quoted-bang-guid", "Open \"https://example.test/search?q=hello!\" now",
             "https://example.test/search?q=hello!"),
            ("quoted-period-guid", "Open \"https://example.test/report.\" now",
             "https://example.test/report."),
            ("quoted-comma-guid", "Open \"https://example.test/list,a,\" now",
             "https://example.test/list,a,"),
            ("quoted-semicolon-guid", "Open \"https://example.test/query;x;\" now",
             "https://example.test/query;x;"),
            ("angle-bang-guid", "Open <https://example.test/search?q=hello!> now",
             "https://example.test/search?q=hello!"),
            ("parenthesis-bang-guid", "Open (https://example.test/search?q=hello!).",
             "https://example.test/search?q=hello!"),
            ("unwrapped-bang-guid", "Open https://example.test/report! now",
             "https://example.test/report"),
            ("nested-wrapper-guid", "((https://example.test/report)).",
             "https://example.test/report"),
            ("nested-path-guid", "((https://example.test/A_(B))).",
             "https://example.test/A_(B)"),
            ("square-bang-guid", "[https://example.test/search?q=hello!]",
             "https://example.test/search?q=hello!"),
            ("nested-uri-guid", "See https://example.test/f_(a_(b)).",
             "https://example.test/f_(a_(b))"),
            ("query-scheme-guid", "See https://redirect.test/?to=https://other.test/path",
             "https://redirect.test/?to=https://other.test/path"),
            ("apostrophe-guid", "See https://example.test/O'Reilly today",
             "https://example.test/O'Reilly"),
            ("single-quoted-apostrophe-guid", "Open 'https://example.test/O'Reilly' now",
             "https://example.test/O'Reilly"),
            ("double-quoted-apostrophe-guid", "Open \"https://example.test/O'Reilly\" now",
             "https://example.test/O'Reilly"),
            ("query-comma-scheme-guid",
             "See https://redirect.test/?next=https://other.test/a,https://third.test/b",
             "https://redirect.test/?next=https://other.test/a,https://third.test/b"),
            ("quoted-query-comma-guid",
             "Open \"https://redirect.test/?next=https://other.test/a,https://third.test/b\" now",
             "https://redirect.test/?next=https://other.test/a,https://third.test/b"),
            ("fragment-comma-scheme-guid",
             "See https://redirect.test/#next=https://other.test/a,https://third.test/b",
             "https://redirect.test/#next=https://other.test/a,https://third.test/b"),
            ("quoted-fragment-comma-guid",
             "Open \"https://redirect.test/#next=https://other.test/a,https://third.test/b\" now",
             "https://redirect.test/#next=https://other.test/a,https://third.test/b"),
        ]
        for (index, fixture) in literalCases.enumerated() {
            var literalStmt: OpaquePointer?
            precondition(sqlite3_prepare_v2(db,
                "INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
                "VALUES (?, ?, 0, 1, 0, ?)", -1, &literalStmt, nil) == SQLITE_OK)
            sqlite3_bind_int64(literalStmt, 1, date - Int64(index + 4) * 1_000_000_000)
            _ = fixture.1.withCString { sqlite3_bind_text(literalStmt, 2, $0, -1, transient) }
            _ = fixture.0.withCString { sqlite3_bind_text(literalStmt, 3, $0, -1, transient) }
            precondition(sqlite3_step(literalStmt) == SQLITE_DONE)
            sqlite3_finalize(literalStmt)
        }
        sql("INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (\(date - Int64(literalCases.count + 4) * 1_000_000_000), " +
            "'(https://example.test/a),(https://example.test/b)', 0, 1, 0, 'adjacent-guid')")
        sql("INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (\(date - Int64(literalCases.count + 7) * 1_000_000_000), " +
            "'https://example.test/a,https://example.test/b', 0, 1, 0, 'unwrapped-adjacent-guid')")
        sql("INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (\(date - Int64(literalCases.count + 5) * 1_000_000_000), " +
            "'prefixhttps://example.test/not-a-link', 0, 1, 0, 'embedded-prefix-guid')")
        sql("INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (\(date - Int64(literalCases.count + 6) * 1_000_000_000), " +
            "'(https://example.test/unclosed', 0, 1, 0, 'unclosed-guid')")
        sql("INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "VALUES (\(date - Int64(literalCases.count + 8) * 1_000_000_000), " +
            "'https://example.test/O''', 0, 1, 0, 'ambiguous-quote-guid')")
        let ambiguousCases = [
            ("path-comma-guid", "https://example.test/a,https://other.test/b"),
            ("quoted-path-comma-guid", "\"https://example.test/a,https://other.test/b\""),
            ("interior-apostrophe-query-guid",
             "See 'https://example.test/?q=authors'&sort=asc' now"),
            ("interior-apostrophe-path-guid", "See 'https://example.test/O'!Reilly' now"),
            ("ambiguous-apostrophe-suffix-guid", "See 'https://example.test/O'!Reilly now"),
        ]
        for (index, fixture) in ambiguousCases.enumerated() {
            var ambiguousStmt: OpaquePointer?
            precondition(sqlite3_prepare_v2(db,
                "INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
                "VALUES (?, ?, 0, 1, 0, ?)", -1, &ambiguousStmt, nil) == SQLITE_OK)
            sqlite3_bind_int64(ambiguousStmt, 1,
                              date - Int64(literalCases.count + 9 + index) * 1_000_000_000)
            _ = fixture.1.withCString { sqlite3_bind_text(ambiguousStmt, 2, $0, -1, transient) }
            _ = fixture.0.withCString { sqlite3_bind_text(ambiguousStmt, 3, $0, -1, transient) }
            precondition(sqlite3_step(ambiguousStmt) == SQLITE_DONE)
            sqlite3_finalize(ambiguousStmt)
        }
        let punctuationScan = reader.readRecentMessages(limit: 40)!
        let punctuationRecords = try punctuationScan.structured.map { line in
            try JSONSerialization.jsonObject(with: Data(line.dropFirst(5).utf8)) as! [String: Any]
        }
        for (guid, _, expected) in literalCases {
            let record = punctuationRecords.compactMap { $0["record"] as? [String: Any] }
                .first { $0["guid"] as? String == guid }!
            let links = record["links"] as! [[String: String]]
            precondition(links.count == 1 && links[0]["url"] == expected,
                         "Literal URL delimiters must remain source-exact: \(guid)")
            if guid.contains("apostrophe") || guid.contains("query-comma")
                    || guid.contains("fragment-comma") {
                let coverage = punctuationRecords.first {
                    ($0["record"] as? [String: Any])?["guid"] as? String == guid
                }!["coverage"] as! [String: String]
                precondition(coverage["links"] == "complete",
                             "Exact quoted or query URL must retain complete link coverage: \(guid)")
            }
        }
        let adjacent = punctuationRecords.compactMap { $0["record"] as? [String: Any] }
            .first { $0["guid"] as? String == "adjacent-guid" }!
        let adjacentLinks = adjacent["links"] as! [[String: String]]
        precondition(adjacentLinks.map { $0["url"]! } == ["https://example.test/a", "https://example.test/b"],
                     "Adjacent wrapped URLs must remain two source links")
        for guid in ["embedded-prefix-guid", "unclosed-guid", "ambiguous-quote-guid",
                     "unwrapped-adjacent-guid"] + ambiguousCases.map({ $0.0 }) {
            let row = punctuationRecords.first { ($0["record"] as? [String: Any])?["guid"] as? String == guid }!
            let record = row["record"] as! [String: Any]
            let coverage = row["coverage"] as! [String: String]
            precondition((record["links"] as! [[String: String]]).isEmpty
                         && coverage["links"] == "partial", "Ambiguous source URL must be omitted: \(guid)")
        }
        sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<2001) " +
            "INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "SELECT \(date) - (x+3)*1000000000, 'Synthetic row', 0, 1, 0, 'bulk-'||x FROM seq")
        let bounded = reader.readRecentMessages(limit: 5000)!
        precondition(bounded.attempted == 2015 + literalCases.count && bounded.skipped == 1
                     && bounded.structured.count == 2000
                     && bounded.truncated >= 14 + literalCases.count,
                     "Structured carrier has an explicit row cap and partial coverage")
        print("MessagesReader: synthetic read-only regression passed")
    }
}
