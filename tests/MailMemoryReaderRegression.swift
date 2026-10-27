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
          (4, 'imap://ICLOUD-UUID/INBOX'), (5, 'imap://ICLOUD-UUID/Sent%20Messages'), (6, 'imap://ICLOUD-UUID/Junk');
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
        """)
        sqlite3_close(db)

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
              let (rows, capped) = MailMemoryReader.indexRows(dbPath: path, since: now - 90 * 86400) else {
            print("FAIL: index unreadable"); exit(1)
        }
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

        // .emlx files: full wins over partial; the count line bounds the message.
        let box = "\(root)/V10/GMAIL-UUID/[Gmail].mbox/All Mail.mbox/STORE/Data/0/Messages"
        try emlx("\(box)/10.emlx", "Subject: Lease\r\n\r\nNotice due July 2, 2027.")
        try emlx("\(box)/10.partial.emlx", "Subject: Lease\r\n\r\npartial")
        try emlx("\(box)/11.partial.emlx", "Subject: thesis\r\n\r\nHappy to review.")
        try Data("not mail".utf8).write(to: URL(fileURLWithPath: "\(box)/notes.txt"))
        let files = MailMemoryReader.emlxFiles(root)
        check(Set(files.keys) == [10, 11], "emlx map: \(files.keys.sorted())")
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
            check(replies[1]["ok"] as? Bool == false && replies[1]["error"] as? String == "not_on_disk", "missing body reply")
        }

        if failures > 0 { print("\(failures) failure(s)"); exit(1) }
        print("MailMemoryReader: \(total) checks passed")
    }
}
