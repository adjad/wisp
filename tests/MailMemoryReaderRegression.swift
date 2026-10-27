// Compile with the production reader, then run. Only a temporary fixture Mail
// folder is read; this test never opens Mail or the user's Mail database.
// swiftc app/Sources/WispApp/MailMemoryReader.swift tests/MailMemoryReaderRegression.swift \
//   -lsqlite3 -o /tmp/<test-dir>/mail-memory-regression
import Foundation
import SQLite3

@main
enum MailMemoryReaderRegression {
    static var failures = 0
    static var total = 0

    static func check(_ ok: Bool, _ label: String) {
        total += 1
        if !ok { failures += 1; print("FAIL: \(label)") }
    }

    static func exec(_ db: OpaquePointer?, _ sql: String) {
        var err: UnsafeMutablePointer<CChar>?
        if sqlite3_exec(db, sql, nil, nil, &err) != SQLITE_OK {
            print("fixture SQL failed: \(err.map { String(cString: $0) } ?? "?")")
            exit(2)
        }
    }

    /// Collects a reader's replies so a test can wait for one.
    final class Replies {
        let sem = DispatchSemaphore(value: 0)
        var last: [String: Any] = [:]
        func ask(_ reader: MailMemoryReader, _ request: [String: Any]) -> [String: Any] {
            last = [:]
            _ = reader.handle(request)
            _ = sem.wait(timeout: .now() + 5)
            return last
        }
    }

    static func makeReader(_ root: String, rebuildInterval: TimeInterval = 30) -> (MailMemoryReader, Replies) {
        let replies = Replies()
        let reader = MailMemoryReader(mailRoot: root, rebuildInterval: rebuildInterval) { reply in
            replies.last = reply
            replies.sem.signal()
        }
        return (reader, replies)
    }

    static func body(_ pair: (MailMemoryReader, Replies), _ id: String, _ rowid: Int) -> [String: Any] {
        pair.1.ask(pair.0, ["type": "mail_memory_read", "request_id": id, "kind": "body", "rowid": rowid])
    }

    static func emlx(_ path: String, _ message: String) throws {
        let bytes = Data(message.utf8)
        var file = Data("\(bytes.count)\n".utf8)
        file.append(bytes)
        file.append(Data("<?xml version=\"1.0\"?><plist><dict/></plist>".utf8))
        try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                                withIntermediateDirectories: true)
        try file.write(to: URL(fileURLWithPath: path))
    }

    static func main() throws {
        let tmp = ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory()
        let root = (tmp as NSString).appendingPathComponent("mail-memory-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(atPath: root) }
        let data = "\(root)/V10/MailData"
        try FileManager.default.createDirectory(atPath: data, withIntermediateDirectories: true)
        var db: OpaquePointer?
        guard sqlite3_open("\(data)/Envelope Index", &db) == SQLITE_OK else { exit(2) }
        let now = Date().timeIntervalSince1970
        let old = now - 400 * 86400
        exec(db, """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, message_id TEXT, sender INTEGER, subject INTEGER,
          date_sent REAL, date_received REAL, mailbox INTEGER, deleted INTEGER, conversation_id INTEGER,
          list_id_hash INTEGER, unsubscribe_type INTEGER, automated_conversation INTEGER, global_message_id INTEGER);
        CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, model_category INTEGER, message_id_header TEXT);
        INSERT INTO message_global_data VALUES (1, 0, '<lease-1@harbor.example>'), (2, 3, '<sale@shop.example>');
        INSERT INTO mailboxes VALUES (1, 'imap://GMAIL-UUID/%5BGmail%5D/All%20Mail'),
          (2, 'imap://GMAIL-UUID/INBOX'), (3, 'imap://GMAIL-UUID/%5BGmail%5D/Sent%20Mail'),
          (4, 'imap://ICLOUD-UUID/INBOX'), (5, 'imap://ICLOUD-UUID/Sent%20Messages'), (6, 'imap://ICLOUD-UUID/Junk'),
          (7, 'imap://ICLOUD-UUID/Consent'), (8, 'imap://ICLOUD-UUID/Presentations');
        INSERT INTO subjects VALUES (1, 'Lease renewal'), (2, 'Re: thesis'), (3, 'Sale!'), (4, 'Old'), (5, 'Spam');
        INSERT INTO addresses VALUES (1, 'leasing@harbor.example', 'Harbor'), (2, 'me@example.com', 'Me');
        -- Gmail: real mail in All Mail, Inbox/Sent by label.
        INSERT INTO messages VALUES (10, '<a@x>', 1, 1, \(now - 86400), \(now - 86000), 1, 0, 7, 0, 0, 0, 1);
        INSERT INTO labels VALUES (10, 2);
        INSERT INTO messages VALUES (11, '<b@x>', 2, 2, \(now - 3600), \(now - 3500), 1, 0, 7, 0, 0, 0, NULL);
        INSERT INTO labels VALUES (11, 3), (11, 2);
        -- All Mail with no Inbox/Sent label (archived promo): excluded.
        INSERT INTO messages VALUES (12, '<c@x>', 1, 3, \(now), \(now - 100), 1, 0, 8, 5, 0, 0, NULL);
        -- iCloud: Inbox mailbox directly, bulk flag set.
        INSERT INTO messages VALUES (20, '<d@x>', 1, 3, \(now), \(now - 200), 4, 0, 9, 0, 1, 0, 2);
        -- Outside the window, deleted, and junk: all excluded.
        INSERT INTO messages VALUES (21, '<e@x>', 1, 4, \(old), \(old), 4, 0, 9, 0, 0, 0, NULL);
        INSERT INTO messages VALUES (22, '<f@x>', 1, 4, \(now), \(now - 300), 4, 1, 9, 0, 0, 0, NULL);
        INSERT INTO messages VALUES (23, '<g@x>', 1, 5, \(now), \(now - 400), 6, 0, 9, 0, 0, 0, NULL);
        -- Folders that merely contain "sent": not Inbox, not Sent, so excluded.
        INSERT INTO messages VALUES (24, '<h@x>', 1, 1, \(now), \(now - 500), 7, 0, 9, 0, 0, 0, NULL);
        INSERT INTO messages VALUES (25, '<i@x>', 1, 1, \(now), \(now - 600), 8, 0, 9, 0, 0, 0, NULL);
        """)
        sqlite3_close(db)
        // An older V9 with its own Envelope Index: V10 is newer even though "V9" sorts after "V10".
        try FileManager.default.createDirectory(atPath: "\(root)/V9/MailData", withIntermediateDirectories: true)
        try Data().write(to: URL(fileURLWithPath: "\(root)/V9/MailData/Envelope Index"))

        // Requests: strict shapes only.
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "index", "days": 90])?.days == 90, "index request")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "body", "rowid": 10, "event_id": "e1"])?.rowid == 10,
              "body request, hub event_id tolerated")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "index", "days": 900]) == nil, "window is capped")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "short",
                                        "kind": "index", "days": 9]) == nil, "request id must be a nonce")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "index", "days": 9, "path": "/etc"]) == nil, "extra keys refused")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "delete", "rowid": 1]) == nil, "unknown kinds refused")

        // Index rows.
        guard let path = MailMemoryReader.envelopeIndexPath(root),
              let (rows, capped) = try? MailMemoryReader.indexRows(dbPath: path, since: now - 90 * 86400) else {
            print("FAIL: index unreadable"); exit(1)
        }
        check(path == "\(root)/V10/MailData/Envelope Index", "newest V folder by number: \(path)")
        let byID = Dictionary(uniqueKeysWithValues: rows.map { (($0["rowid"] as? Int64) ?? -1, $0) })
        check(Set(byID.keys) == [10, 11, 20], "inbox+sent in window only: \(byID.keys.sorted())")
        check(!capped, "not capped")
        check(byID[10]?["box"] as? String == "inbox" && byID[10]?["account"] as? String == "GMAIL-UUID",
              "Gmail inbox by label")
        check(byID[11]?["box"] as? String == "sent", "a message labelled Inbox and Sent counts as sent")
        check(byID[20]?["bulk"] as? Bool == true && byID[10]?["bulk"] as? Bool == false, "bulk flags")
        check(byID[10]?["subject"] as? String == "Lease renewal" && byID[10]?["sender"] as? String == "leasing@harbor.example",
              "subject and sender")
        check(rows.compactMap { $0["rowid"] as? Int64 } == [20, 11, 10], "newest first")
        check(byID[10]?["category"] as? Int == 0 && byID[10]?["rfc_message_id"] as? String == "<lease-1@harbor.example>",
              "Mail's category and the RFC Message-ID")
        check(byID[20]?["category"] as? Int == 3, "Promotions is reported, for the backend to drop")
        check(byID[11]?["category"] as? Int == -1 && byID[11]?["rfc_message_id"] as? String == "",
              "no recorded category: -1")

        // Sent detection: real Sent mailboxes only.
        let sentURLs = ["imap://A/Sent%20Messages", "imap://A/Sent", "imap://A/Sent%20Mail", "imap://A/Sent%20Items",
                        "imap://A/%5BGmail%5D/Sent%20Mail", "imap://A/[Gmail]/Sent Mail", "imap://A/INBOX/Sent",
                        "ews://A/Sent%20Items/", "imap://A/sent"]
        for u in sentURLs { check(MailMemoryReader.isSentMailbox(url: u), "sent: \(u)") }
        let otherURLs = ["imap://A/Consent", "imap://A/Presentations", "imap://A/Absent", "imap://A/Sentinel",
                         "imap://A/Sent%20Messages/Archive", "imap://A/INBOX", "imap://Sent", "", "imap://A/Resent"]
        for u in otherURLs { check(!MailMemoryReader.isSentMailbox(url: u), "not sent: \(u)") }

        // .emlx files: full wins over partial; the count line bounds the message.
        let box = "\(root)/V10/GMAIL-UUID/[Gmail].mbox/All Mail.mbox/STORE/Data/0/Messages"
        try emlx("\(box)/10.emlx", "Subject: Lease\r\n\r\nNotice due July 2, 2027.")
        try emlx("\(box)/10.partial.emlx", "Subject: Lease\r\n\r\npartial")
        try emlx("\(box)/11.partial.emlx", "Subject: thesis\r\n\r\nHappy to review.")
        try Data("not mail".utf8).write(to: URL(fileURLWithPath: "\(box)/notes.txt"))
        // The older V9 folder has its own rowids: 10 (a different message) and 55. Neither may leak in.
        let oldBox = "\(root)/V9/GMAIL-UUID/[Gmail].mbox/All Mail.mbox/STORE/Data/0/Messages"
        try emlx("\(oldBox)/10.emlx", "Subject: Other\r\n\r\nfrom V9")
        try emlx("\(oldBox)/55.emlx", "Subject: Other\r\n\r\nfrom V9 too")
        let files = MailMemoryReader.emlxFiles(root)
        check(Set(files.keys) == [10, 11], "emlx map: \(files.keys.sorted())")
        check(files.values.allSatisfy { $0.path.hasPrefix("\(root)/V10/") }, "emlx map only from the indexed V folder")
        check(files[10]?.partial == false && files[11]?.partial == true, "full beats partial")
        if let (bytes, truncated) = MailMemoryReader.message(at: files[10]!.path) {
            let text = String(decoding: bytes, as: UTF8.self)
            check(text.hasSuffix("July 2, 2027.") && !text.contains("plist") && !truncated, "trailer dropped: \(text)")
        } else { check(false, "message readable") }

        // A full request round trip through handle().
        let done = DispatchSemaphore(value: 0)
        var replies: [[String: Any]] = []
        let reader = MailMemoryReader(mailRoot: root) { reply in replies.append(reply); done.signal() }
        check(reader.handle(["type": "mail_memory_read", "request_id": "abcdefghijklmnop0", "kind": "body", "rowid": 10]),
              "body request accepted")
        _ = done.wait(timeout: .now() + 5)
        check(reader.handle(["type": "mail_memory_read", "request_id": "abcdefghijklmnop1", "kind": "body", "rowid": 99]),
              "missing body request accepted")
        _ = done.wait(timeout: .now() + 5)
        check(!reader.handle(["type": "mail_memory_read", "kind": "body", "rowid": 10]), "no request id, no work")
        check(replies.count == 2, "one reply per request")
        if replies.count == 2 {
            let raw = (replies[0]["raw_b64"] as? String).flatMap { Data(base64Encoded: $0) }
            check(replies[0]["request_id"] as? String == "abcdefghijklmnop0" && replies[0]["ok"] as? Bool == true
                  && raw.map { String(decoding: $0, as: UTF8.self).contains("July 2, 2027") } == true, "body reply")
            check(replies[1]["ok"] as? Bool == false && replies[1]["error"] as? String == "not_in_inbox_or_sent", "an unknown rowid is refused")
        }

        // A stale cache: a moved file and a new file both rebuild the map once and retry.
        let stale = makeReader(root, rebuildInterval: 0)
        check(body(stale, "stalecache-000001", 11)["ok"] as? Bool == true, "partial body read")
        let moved = "\(root)/V10/GMAIL-UUID/[Gmail].mbox/Moved.mbox/STORE/Data/0/Messages"
        try FileManager.default.createDirectory(atPath: moved, withIntermediateDirectories: true)
        try FileManager.default.moveItem(atPath: "\(box)/11.partial.emlx", toPath: "\(moved)/11.partial.emlx")
        check(body(stale, "stalecache-000002", 11)["ok"] as? Bool == true, "a moved file is found after a rebuild")
        try emlx("\(box)/20.emlx", "Subject: New\r\n\r\nArrived later.")
        check(body(stale, "stalecache-000003", 20)["ok"] as? Bool == true, "a new file is found after a rebuild")
        let limited = makeReader(root, rebuildInterval: 3600)
        check(body(limited, "stalecache-000004", 11)["ok"] as? Bool == true, "limited reader reads")
        try FileManager.default.moveItem(atPath: "\(moved)/11.partial.emlx", toPath: "\(box)/11.partial.emlx")
        check(body(limited, "stalecache-000005", 11)["error"] as? String == "unreadable", "rebuilds are rate limited")

        // Only mail in Inbox or Sent right now: Junk, an archived message, and a "Consent"
        // folder are refused even when their files are on disk.
        for (n, rowid) in [12, 23, 24].enumerated() {
            try emlx("\(box)/\(rowid).emlx", "Subject: Not yours to read\r\n\r\nx")
            check(body(stale, "notinboxorsent-00\(n)", rowid)["error"] as? String == "not_in_inbox_or_sent",
                  "rowid \(rowid) refused")
        }

        // Schema tolerance: optional columns fall back; missing core tables are unsupported_schema.
        func makeDB(_ name: String, _ sql: String) -> String {
            let file = "\(root)/\(name).db"
            var handle: OpaquePointer?
            guard sqlite3_open(file, &handle) == SQLITE_OK else { exit(2) }
            exec(handle, sql)
            sqlite3_close(handle)
            return file
        }
        let bare = makeDB("bare", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER);
        INSERT INTO mailboxes VALUES (1, 'imap://X/INBOX');
        INSERT INTO messages VALUES (1, \(now - 60), 1);
        """)
        if let (bareRows, _) = try? MailMemoryReader.indexRows(dbPath: bare, since: now - 86400) {
            check(bareRows.count == 1 && bareRows[0]["box"] as? String == "inbox" && bareRows[0]["subject"] as? String == ""
                  && bareRows[0]["sender"] as? String == "" && bareRows[0]["account"] as? String == "X",
                  "no deleted/subjects/addresses: rows still come back")
        } else { check(false, "bare schema returns rows") }
        let noComment = makeDB("nocomment", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER, sender INTEGER);
        INSERT INTO mailboxes VALUES (1, 'imap://X/Sent%20Messages');
        INSERT INTO addresses VALUES (1, 'me@example.com');
        INSERT INTO messages VALUES (1, \(now - 60), 1, 1);
        """)
        if let (r, _) = try? MailMemoryReader.indexRows(dbPath: noComment, since: now - 86400) {
            check(r.count == 1 && r[0]["box"] as? String == "sent" && r[0]["sender"] as? String == "me@example.com"
                  && r[0]["sender_name"] as? String == "", "no comment column")
        } else { check(false, "no-comment schema returns rows") }
        let noMailboxes = makeDB("nomailboxes", """
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER);
        """)
        do { _ = try MailMemoryReader.indexRows(dbPath: noMailboxes, since: 0); check(false, "missing mailboxes throws") }
        catch { check((error as? MailMemoryReader.IndexFailure) == .unsupportedSchema, "missing mailboxes: unsupported_schema") }
        let junk = "\(root)/junk.db"
        try Data("this is not a database at all, just text".utf8).write(to: URL(fileURLWithPath: junk))
        do { _ = try MailMemoryReader.indexRows(dbPath: junk, since: 0); check(false, "junk file throws") }
        catch { check((error as? MailMemoryReader.IndexFailure) == .unreadable, "not a database: unreadable") }
        let unsupportedRoot = "\(root)/unsupported"
        try FileManager.default.createDirectory(atPath: "\(unsupportedRoot)/V10/MailData", withIntermediateDirectories: true)
        try FileManager.default.copyItem(atPath: noMailboxes, toPath: "\(unsupportedRoot)/V10/MailData/Envelope Index")
        let unsupported = makeReader(unsupportedRoot)
        let indexReply = unsupported.1.ask(unsupported.0, ["type": "mail_memory_read", "request_id": "unsupported-00001",
                                                          "kind": "index", "days": 30])
        check(indexReply["ok"] as? Bool == false && indexReply["error"] as? String == "unsupported_schema",
              "the reply names unsupported_schema: \(indexReply)")

        if failures > 0 { print("\(failures) failure(s)"); exit(1) }
        print("MailMemoryReader: \(total) checks passed")
    }
}
