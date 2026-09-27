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
            "'Schedule at https://schedule.example.test/meet?slot=3 and example.test', " +
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

        let modified = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        let scan = reader.readRecentMessages()!
        precondition(scan.attempted == 3 && scan.skipped == 1
                     && scan.lines.count == 2 && scan.structured.count == 2)
        precondition(scan.lines[0].contains("message: 1") == false)
        precondition(scan.lines[0].contains("Schedule at https://schedule.example.test/meet?slot=3"))
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
        let after = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        precondition(after == modified, "Read-only scan did not change the database")

        let limited = reader.readRecentMessages(limit: 1)!
        precondition(limited.attempted == 1 && limited.structured.count == 1)
        sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<2001) " +
            "INSERT INTO message (date, text, is_from_me, is_read, associated_message_type, guid) " +
            "SELECT \(date) - (x+3)*1000000000, 'Synthetic row', 0, 1, 0, 'bulk-'||x FROM seq")
        let bounded = reader.readRecentMessages(limit: 5000)!
        precondition(bounded.attempted == 2004 && bounded.skipped == 1
                     && bounded.structured.count == 2000 && bounded.truncated >= 2,
                     "Structured carrier has an explicit row cap and partial coverage")
        print("MessagesReader: synthetic read-only regression passed")
    }
}
