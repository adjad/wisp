// Compile with the production reader, then run. Only temporary SQLite fixtures
// are accessed; this test never opens Mail or the user's Mail database.
// swiftc app/Sources/WispApp/MailDBReader.swift tests/MailDBReaderRegression.swift \
//   -lsqlite3 -o /tmp/<test-dir>/mail-db-regression
import Foundation
import SQLite3

@main
enum MailDBReaderRegression {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-mail-db-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("Envelope Index").path
        var db: OpaquePointer?
        precondition(sqlite3_open(path, &db) == SQLITE_OK)
        defer { sqlite3_close(db) }
        func sql(_ query: String) {
            precondition(sqlite3_exec(db, query, nil, nil, nil) == SQLITE_OK,
                         "Fixture SQL failed")
        }
        let reader = MailDBReader(indexPath: path)
        precondition(reader.readHeadersAndHistory() == nil, "Missing schema must fail, not be empty-ready")
        sql("CREATE TABLE mailboxes (url TEXT)")
        precondition(reader.readHeadersAndHistory()?.headers == "", "No mailboxes is a completed empty read")
        sql("INSERT INTO mailboxes VALUES ('imap://fixture-account/INBOX')")
        precondition(reader.readHeadersAndHistory() == nil, "Missing messages table must fail")
        sql("CREATE TABLE messages (date_received REAL, read INTEGER, subject INTEGER, sender INTEGER, mailbox INTEGER)")
        sql("CREATE TABLE subjects (subject TEXT)")
        sql("CREATE TABLE addresses (address TEXT, comment TEXT)")
        precondition(reader.readHeadersAndHistory()?.headers == "", "Empty inbox is a completed read")
        sql("INSERT INTO subjects VALUES ('Fixture subject')")
        sql("INSERT INTO addresses VALUES ('sender@example.test', 'Fixture sender')")
        sql("INSERT INTO messages VALUES (\(Date().timeIntervalSince1970 - 86400), 0, 1, 1, 1)")
        let oldDate = Date(timeIntervalSinceNow: -7 * 86400)
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: path)
        let result = reader.readHeadersAndHistory()
        precondition(result?.headers.contains("Fixture subject") == true,
                     "An old index timestamp must not stop a successful read")
        precondition(result?.history.contains("Fixture subject") == true)
        precondition(reader.readHeadersAndHistory()?.headers == result?.headers,
                     "Repeated reads of an unchanged index must still complete")
        let modified = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        precondition(abs(modified.timeIntervalSince(oldDate)) < 1, "Reads must not modify Mail's index")
        sql("BEGIN EXCLUSIVE")
        precondition(reader.readHeadersAndHistory() == nil, "A locked read is failure, not empty-ready")
        sql("ROLLBACK")
        sql("DELETE FROM messages")
        let empty = reader.readHeadersAndHistory()
        precondition(empty?.headers == "" && empty?.history == "", "An empty scan must clear both caches")
        precondition(MailDBReader(indexPath: root.appendingPathComponent("missing").path)
            .readHeadersAndHistory() == nil, "An unreadable index must fail")
        print("MailDBReader: 11 regression checks passed")
    }
}
