// Compile with the production reader, then run. Only a temporary fixture Mail
// folder is read; this test never opens Mail or the user's Mail database.
// swiftc app/Sources/WispApp/MailMemoryReader.swift app/Sources/WispApp/TextRecognizer.swift \
//   tests/MailMemoryReaderRegression.swift \
//   -lsqlite3 -o /tmp/<test-dir>/mail-memory-regression
import Foundation
import SQLite3
import CoreGraphics
import CoreText
import ImageIO

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

    /// A white bitmap with black text lines (CoreText), encoded as JPEG.
    static func textJPEG(_ lines: [String], size: Int = 900) -> Data? {
        let width = size, height = 60 + lines.count * 90
        guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let font = CTFontCreateWithName("Helvetica" as CFString, 48, nil)
        for (i, line) in lines.enumerated() {
            let attributes: [NSAttributedString.Key: Any] = [
                NSAttributedString.Key(kCTFontAttributeName as String): font,
                NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(red: 0, green: 0, blue: 0, alpha: 1)]
            let ctLine = CTLineCreateWithAttributedString(NSAttributedString(string: line, attributes: attributes))
            ctx.textPosition = CGPoint(x: 30, y: CGFloat(height - 90 - i * 90))
            CTLineDraw(ctLine, ctx)
        }
        guard let image = ctx.makeImage() else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.9] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
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
        // The folder's name in the account's language, whatever its case or accents
        // (precomposed or as a letter plus a combining accent).
        let localizedSentURLs = ["imap://A/%5BGmail%5D/Gesendet", "ews://A/Elementos%20enviados",
                                 "ews://A/Gesendete%20Elemente", "imap://A/Gesendete%20Objekte",
                                 "imap://A/%5BGmail%5D/Messages%20envoy%C3%A9s", "ews://A/%C3%89l%C3%A9ments%20envoy%C3%A9s",
                                 "imap://A/Envoye%CC%81s", "imap://A/%5BGmail%5D/Enviados", "imap://A/Mensajes%20enviados",
                                 "ews://A/Itens%20Enviados", "imap://A/%5BGmail%5D/Posta%20inviata", "imap://A/Inviati",
                                 "imap://A/VERZONDEN%20ITEMS", "imap://A/Verzonden%20berichten", "imap://A/verzonden",
                                 "imap://A/Skickat", "imap://A/Sendt"]
        for u in localizedSentURLs { check(MailMemoryReader.isSentMailbox(url: u), "sent in another language: \(u)") }
        let otherURLs = ["imap://A/Consent", "imap://A/Presentations", "imap://A/Absent", "imap://A/Sentinel",
                         "imap://A/Sent%20Messages/Archive", "imap://A/INBOX", "imap://Sent", "", "imap://A/Resent",
                         "imap://A/Gesendet/Archiv", "imap://A/Reenviados", "imap://A/Enviados%20antiguos",
                         "imap://A/Gesendetes", "imap://A/Verzonden%20facturen", "imap://Gesendet"]
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

        // An index that can't be read is retried later, never reported as "not in Inbox".
        let broken = "\(root)/broken"
        let brokenBox = "\(broken)/V10/X/INBOX.mbox/Messages"
        try FileManager.default.createDirectory(atPath: brokenBox, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(atPath: "\(broken)/V10/MailData", withIntermediateDirectories: true)
        try Data("not a database".utf8).write(to: URL(fileURLWithPath: "\(broken)/V10/MailData/Envelope Index"))
        try emlx("\(brokenBox)/10.emlx", "Subject: x\r\n\r\ny")
        check(body(makeReader(broken), "brokenindex-00001", 10)["error"] as? String == "index_unreadable",
              "an unreadable index is retried, not skipped")

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

        // Sent mail (view_emails mailbox='sent'): Sent mailboxes and Gmail's Sent label only,
        // newest by date_sent, To before Cc, capped recipients, no bodies.
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "sent", "days": 14])?.kind == "sent", "sent request")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "sent", "days": 90]) == nil, "sent window is capped")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop",
                                        "kind": "sent", "rowid": 3]) == nil, "sent takes days only")
        let sentRoot = "\(root)/sent"
        try FileManager.default.createDirectory(atPath: "\(sentRoot)/V10/MailData", withIntermediateDirectories: true)
        let sentDB = makeDB("sentfixture", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER, address INTEGER, type INTEGER, position INTEGER);
        CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, subject INTEGER, date_sent REAL, date_received REAL,
          mailbox INTEGER, deleted INTEGER, global_message_id INTEGER);
        CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, message_id_header TEXT);
        INSERT INTO message_global_data VALUES (1, '<dinner@me.example>');
        INSERT INTO mailboxes VALUES (1, 'imap://ICLOUD-UUID/Sent%20Messages'), (2, 'imap://ICLOUD-UUID/INBOX'),
          (3, 'imap://GMAIL-UUID/%5BGmail%5D/All%20Mail'), (4, 'imap://GMAIL-UUID/%5BGmail%5D/Sent%20Mail'),
          (5, 'imap://ICLOUD-UUID/Consent');
        INSERT INTO subjects VALUES (1, 'Dinner Friday'), (2, 'Lease'), (3, 'Inbox mail'), (4, 'Deleted'),
          (5, 'Old'), (6, 'Consent form'), (7, 'Team update');
        INSERT INTO addresses VALUES (1, 'ana@example.com', 'Ana'), (2, 'bo@example.com', ''),
          (3, 'cc@example.com', 'Cee'), (4, 'x1@example.com', ''), (5, 'x2@example.com', ''),
          (6, 'x3@example.com', ''), (7, 'x4@example.com', ''), (8, 'x5@example.com', ''), (9, 'x6@example.com', '');
        -- iCloud Sent: received (filed) later than sent; ordered by date_sent.
        INSERT INTO messages VALUES (1, 1, \(now - 60), \(now - 10), 1, 0, 1);
        INSERT INTO recipients VALUES (1, 1, 3, 1, 0), (2, 1, 1, 0, 0), (3, 1, 2, 0, 1);
        -- Gmail: All Mail with the Sent label.
        INSERT INTO messages VALUES (2, 2, \(now - 3600), \(now - 3600), 3, 0, NULL);
        INSERT INTO labels VALUES (2, 4);
        INSERT INTO recipients VALUES (4, 2, 3, 1, 0);
        -- Seven recipients: five shown, seven counted.
        INSERT INTO messages VALUES (3, 7, \(now - 7200), \(now - 7200), 1, 0, NULL);
        INSERT INTO recipients VALUES (5, 3, 4, 0, 0), (6, 3, 5, 0, 1), (7, 3, 6, 0, 2), (8, 3, 7, 0, 3),
          (9, 3, 8, 0, 4), (10, 3, 9, 0, 5), (11, 3, 2, 0, 6);
        -- Not sent mail: inbox, deleted, too old, a folder merely named like it.
        INSERT INTO messages VALUES (4, 3, \(now - 30), \(now - 30), 2, 0, NULL);
        INSERT INTO messages VALUES (5, 4, \(now - 30), \(now - 30), 1, 1, NULL);
        INSERT INTO messages VALUES (6, 5, \(now - 40 * 86400), \(now - 40 * 86400), 1, 0, NULL);
        INSERT INTO messages VALUES (7, 6, \(now - 30), \(now - 30), 5, 0, NULL);
        """)
        try FileManager.default.copyItem(atPath: sentDB, toPath: "\(sentRoot)/V10/MailData/Envelope Index")
        let sentReader = makeReader(sentRoot)
        let sentReply = sentReader.1.ask(sentReader.0, ["type": "mail_memory_read", "request_id": "sent-request-0001",
                                                        "kind": "sent", "days": 14])
        let sent = sentReply["rows"] as? [[String: Any]] ?? []
        check(sentReply["ok"] as? Bool == true && sentReply["kind"] as? String == "sent"
              && sentReply["request_id"] as? String == "sent-request-0001", "sent reply answers its request")
        check(sent.map { $0["subject"] as? String ?? "" } == ["Dinner Friday", "Lease", "Team update"],
              "sent rows: Sent mailbox and Gmail Sent label, newest first: \(sent)")
        if sent.count == 3 {
            check(sent[0]["to"] as? [String] == ["Ana <ana@example.com>", "bo@example.com"]
                  && sent[0]["recipients"] as? Int == 3, "To before Cc, name with address")
            check(sent[0]["account"] as? String == "ICLOUD-UUID" && sent[0]["rfc_message_id"] as? String == "<dinner@me.example>"
                  && abs((sent[0]["ts"] as? Double ?? 0) - (now - 60)) < 1, "account ID, Message-ID, date_sent")
            check(sent[1]["to"] as? [String] == ["Cee <cc@example.com>"], "Cc when there is no To")
            check((sent[2]["to"] as? [String])?.count == MailMemoryReader.maxRecipients
                  && sent[2]["recipients"] as? Int == 7, "recipients capped, total kept")
            check(sent.allSatisfy { $0["raw_b64"] == nil && $0["body"] == nil }, "sent rows carry no body")
        }
        let noRecipients = makeDB("sentbare", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER);
        INSERT INTO mailboxes VALUES (1, 'imap://X/Sent'), (2, 'imap://X/INBOX');
        INSERT INTO messages VALUES (1, \(now - 60), 1), (2, \(now - 60), 2);
        """)
        if let (r, capped) = try? MailMemoryReader.sentRows(dbPath: noRecipients, since: now - 86400) {
            check(r.count == 1 && (r[0]["to"] as? [String]) == [] && r[0]["subject"] as? String == "" && !capped,
                  "no recipients/subjects tables: sent rows still come back")
        } else { check(false, "bare schema returns sent rows") }
        // An account (an Inbox) with no mailbox recognised as Sent is a read problem, never
        // "nothing was sent": its Sent folder may have a name Wisp doesn't know.
        do { _ = try MailMemoryReader.sentRows(dbPath: bare, since: now - 86400); check(false, "an Inbox but no Sent mailbox throws") }
        catch { check((error as? MailMemoryReader.IndexFailure) == .noSentMailbox, "an Inbox but no Sent mailbox: no_sent_mailbox") }
        let noSentRoot = "\(root)/nosent"
        try FileManager.default.createDirectory(atPath: "\(noSentRoot)/V10/MailData", withIntermediateDirectories: true)
        try FileManager.default.copyItem(atPath: bare, toPath: "\(noSentRoot)/V10/MailData/Envelope Index")
        let noSent = makeReader(noSentRoot)
        let noSentReply = noSent.1.ask(noSent.0, ["type": "mail_memory_read", "request_id": "nosent-request-01",
                                                  "kind": "sent", "days": 14])
        check(noSentReply["ok"] as? Bool == false && noSentReply["error"] as? String == "no_sent_mailbox",
              "the reply names no_sent_mailbox: \(noSentReply)")
        // The mail-memory index is unchanged by that: the same file still lists its Inbox.
        check((try? MailMemoryReader.indexRows(dbPath: bare, since: now - 86400))?.0.count == 1,
              "no Sent mailbox: the index still reads the Inbox")
        let noAccount = makeDB("sentnoaccount", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER);
        INSERT INTO mailboxes VALUES (1, 'local://L/Receipts');
        INSERT INTO messages VALUES (1, \(now - 60), 1);
        """)
        if let (r, _) = try? MailMemoryReader.sentRows(dbPath: noAccount, since: now - 86400) {
            check(r.isEmpty, "no account and no Sent mailbox: an empty answer")
        } else { check(false, "no account and no Sent mailbox reads") }
        let localized = makeDB("sentlocalized", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, date_received REAL, mailbox INTEGER);
        INSERT INTO mailboxes VALUES (1, 'imap://G/%5BGmail%5D/Gesendet'), (2, 'imap://G/INBOX'),
          (3, 'ews://W/%C3%89l%C3%A9ments%20envoy%C3%A9s');
        INSERT INTO messages VALUES (1, \(now - 60), 1), (2, \(now - 60), 2), (3, \(now - 120), 3);
        """)
        if let (r, _) = try? MailMemoryReader.sentRows(dbPath: localized, since: now - 86400) {
            check(r.compactMap { $0["rowid"] as? Int64 } == [1, 3], "a Sent mailbox named in another language is read: \(r)")
            check(r.compactMap { $0["account"] as? String } == ["G", "W"], "each under its own account: \(r)")
        } else { check(false, "localized Sent mailboxes read") }
        if let (r, _) = try? MailMemoryReader.indexRows(dbPath: localized, since: now - 86400) {
            check(Set(r.filter { $0["box"] as? String == "sent" }.compactMap { $0["rowid"] as? Int64 }) == [1, 3],
                  "the index marks mail in a localized Sent mailbox as sent: \(r)")
        } else { check(false, "localized index reads") }

        // MARK: Attachments (kind "attachments" and "attachment")

        func attachmentsRequest(_ over: [String: Any] = [:]) -> [String: Any] {
            var base: [String: Any] = ["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "attachments",
                                       "terms": ["lease"], "since": 0, "until": 0, "rfc_ids": [], "limit": 50]
            for (k, v) in over { base[k] = v }
            return base
        }
        func attachmentRequest(_ over: [String: Any] = [:]) -> [String: Any] {
            var base: [String: Any] = ["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "attachment",
                                       "rowid": 10, "attachment_id": "1"]
            for (k, v) in over { base[k] = v }
            return base
        }
        let parsed = MailMemoryReader.request(attachmentsRequest(["since": 5.5, "until": 9, "rfc_ids": ["<a@x>"], "limit": 400]))
        check(parsed?.kind == "attachments" && parsed?.terms == ["lease"] && parsed?.since == 5.5 && parsed?.until == 9
              && parsed?.rfcIDs == ["<a@x>"] && parsed?.limit == 400, "attachments request parses")
        check(MailMemoryReader.request(attachmentsRequest(["terms": [], "event_id": "e1"]))?.terms == [],
              "attachments request: no terms, hub event_id tolerated")
        check(MailMemoryReader.request(attachmentsRequest(["terms": ["a", "b", "c", "d", "e"]])) != nil, "five terms allowed")
        check(MailMemoryReader.request(attachmentsRequest(["terms": ["a", "b", "c", "d", "e", "f"]])) == nil, "six terms refused")
        check(MailMemoryReader.request(attachmentsRequest(["terms": [1, 2]])) == nil, "non-string terms refused")
        check(MailMemoryReader.request(attachmentsRequest(["terms": "lease"])) == nil, "terms must be an array")
        check(MailMemoryReader.request(attachmentsRequest(["rfc_ids": (0..<40).map { "<\($0)@x>" }])) != nil, "40 RFC ids allowed")
        check(MailMemoryReader.request(attachmentsRequest(["rfc_ids": (0..<41).map { "<\($0)@x>" }])) == nil, "41 RFC ids refused")
        check(MailMemoryReader.request(attachmentsRequest(["limit": 0])) == nil, "limit 0 refused")
        check(MailMemoryReader.request(attachmentsRequest(["limit": 401])) == nil, "limit 401 refused")
        check(MailMemoryReader.request(attachmentsRequest(["limit": "5"])) == nil, "limit must be a number")
        check(MailMemoryReader.request(attachmentsRequest(["since": -1])) == nil, "negative since refused")
        check(MailMemoryReader.request(attachmentsRequest(["until": "never"])) == nil, "until must be a number")
        check(MailMemoryReader.request(attachmentsRequest(["path": "/etc"])) == nil, "attachments: extra key refused")
        var missingTerms = attachmentsRequest()
        missingTerms["terms"] = nil
        check(MailMemoryReader.request(missingTerms) == nil, "attachments: a missing key refused")
        check(MailMemoryReader.request(attachmentRequest())?.attachmentID == "1"
              && MailMemoryReader.request(attachmentRequest())?.rowid == 10, "attachment request parses")
        check(MailMemoryReader.request(attachmentRequest(["attachment_id": "A1.b_c-2"])) != nil, "id characters allowed")
        for bad in ["../x", "a/b", "", "a b", "a\\b", "..%2f", String(repeating: "a", count: 41), "x\0y", "é"] {
            check(MailMemoryReader.request(attachmentRequest(["attachment_id": bad])) == nil, "attachment_id refused: \(bad)")
        }
        check(MailMemoryReader.request(attachmentRequest(["attachment_id": String(repeating: "a", count: 40)])) != nil,
              "40-character id allowed")
        check(MailMemoryReader.request(attachmentRequest(["rowid": 0])) == nil, "attachment: rowid must be positive")
        check(MailMemoryReader.request(attachmentRequest(["terms": []])) == nil, "attachment: extra key refused")
        var noID = attachmentRequest()
        noID["attachment_id"] = nil
        check(MailMemoryReader.request(noID) == nil, "attachment: id required")

        // Hidden mailboxes and LIKE escaping.
        for u in ["imap://A/Trash", "imap://A/%5BGmail%5D/Trash", "imap://A/[Gmail]/Spam", "imap://A/Junk",
                  "imap://A/Junk%20E-mail", "imap://A/Junk%20Email", "imap://A/Deleted%20Messages",
                  "imap://A/Deleted%20Items", "imap://A/Bin", "imap://A/Drafts", "imap://A/%5BGmail%5D/Drafts",
                  "imap://A/INBOX/Trash", "ews://A/Deleted%20Items/", "imap://A/trash"] {
            check(MailMemoryReader.isHiddenMailbox(url: u), "hidden: \(u)")
        }
        for u in ["imap://A/INBOX", "imap://A/Sent%20Messages", "imap://A/%5BGmail%5D/All%20Mail", "imap://A/Spam%20filters",
                  "imap://A/Trashed%20ideas", "imap://A/Trash/Old", "imap://A/Binder", "imap://A/Draftsmen", "imap://Trash", ""] {
            check(!MailMemoryReader.isHiddenMailbox(url: u), "not hidden: \(u)")
        }
        check(MailMemoryReader.likePattern("50%_off\\") == "%50\\%\\_off\\\\%", "LIKE terms are escaped")

        // Fixture mail folder: one Gmail account, hidden folders, labels, files on disk.
        let attRoot = "\(root)/attachments"
        try FileManager.default.createDirectory(atPath: "\(attRoot)/V10/MailData", withIntermediateDirectories: true)
        let attDB = makeDB("attachmentfixture", """
        CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
        CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
        CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER);
        CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, subject INTEGER, sender INTEGER, date_received REAL,
          mailbox INTEGER, deleted INTEGER, global_message_id INTEGER);
        CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY, message_id_header TEXT);
        CREATE TABLE attachments (ROWID INTEGER PRIMARY KEY, message INTEGER, attachment_id TEXT, name TEXT);
        INSERT INTO message_global_data VALUES (1, '<a@x>'), (2, '<b@x>');
        INSERT INTO mailboxes VALUES (1, 'imap://GM/%5BGmail%5D/All%20Mail'), (2, 'imap://GM/INBOX'),
          (3, 'imap://GM/%5BGmail%5D/Sent%20Mail'), (4, 'imap://GM/%5BGmail%5D/Trash'), (5, 'imap://GM/Junk'),
          (6, 'imap://GM/%5BGmail%5D/Drafts'), (7, 'imap://GM/%5BGmail%5D/Spam'), (8, 'imap://GM/Spam%20filters');
        INSERT INTO subjects VALUES (1, 'Lease renewal'), (2, 'Taxes 50%_off'), (3, 'Hidden'), (4, 'Filters'),
          (5, 'Sent'), (6, 'Old'), (7, 'Linked');
        INSERT INTO addresses VALUES (1, 'leasing@harbor.example', 'Harbor Homes'), (2, 'me@example.com', 'Me');
        INSERT INTO messages VALUES (10, 1, 1, \(now - 86400), 2, 0, 1);
        INSERT INTO messages VALUES (11, 2, 2, \(now - 3600), 3, 0, 2);
        INSERT INTO messages VALUES (12, 3, 1, \(now - 100), 4, 0, NULL);
        INSERT INTO messages VALUES (13, 3, 1, \(now - 100), 5, 0, NULL);
        INSERT INTO messages VALUES (14, 3, 1, \(now - 100), 6, 0, NULL);
        INSERT INTO messages VALUES (15, 3, 1, \(now - 100), 7, 0, NULL);
        INSERT INTO messages VALUES (16, 3, 1, \(now - 100), 2, 1, NULL);
        INSERT INTO messages VALUES (17, 4, 2, \(now - 5000), 8, 0, NULL);
        INSERT INTO messages VALUES (18, 3, 1, \(now - 100), 1, 0, NULL);
        INSERT INTO messages VALUES (19, 5, 2, \(now - 7200), 1, 0, NULL);
        INSERT INTO messages VALUES (20, 6, 2, \(now - 400 * 86400), 2, 0, NULL);
        INSERT INTO labels VALUES (18, 4), (19, 3);
        INSERT INTO attachments VALUES (1, 10, '1', 'lease.pdf'), (2, 10, '2', 'photo.png'),
          (3, 11, '1', 'tax_50%_form.xlsx'), (4, 12, '1', 'trash.pdf'), (5, 13, '1', 'junk.pdf'),
          (6, 14, '1', 'draft.pdf'), (7, 15, '1', 'spam.pdf'), (8, 16, '1', 'deleted.pdf'),
          (9, 17, '1', 'filters.txt'), (10, 18, '1', 'labelled-trash.pdf'), (11, 19, '1', 'sent.pdf'),
          (12, 20, '1', 'old.pdf'), (13, 17, '2', 'big.bin'), (14, 11, '2', 'link.txt');
        """)
        try FileManager.default.copyItem(atPath: attDB, toPath: "\(attRoot)/V10/MailData/Envelope Index")
        let store = "\(attRoot)/V10/GM/[Gmail].mbox/All Mail.mbox/STORE/Data/0"
        for rowid in [10, 11, 12, 16, 17, 20] { try emlx("\(store)/Messages/\(rowid).partial.emlx", "Subject: x\r\n\r\ny") }
        func put(_ rel: String, _ bytes: Data) throws {
            let path = "\(store)/Attachments/\(rel)"
            try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                                    withIntermediateDirectories: true)
            try bytes.write(to: URL(fileURLWithPath: path))
        }
        try put("10/1/lease.pdf", Data("PDFDATA".utf8))
        try put("11/1/tax_50%_form.xlsx", Data("SHEET".utf8))
        try put("17/1/filters.txt", Data("hello".utf8))
        // Files for rows that must never be returned: hidden, deleted or labelled-away mail.
        for rel in ["12/1/trash.pdf", "16/1/deleted.pdf"] { try put(rel, Data("secret".utf8)) }
        // A file over the cap (sparse, nothing real is written).
        let bigPath = "\(store)/Attachments/17/2/big.bin"
        try FileManager.default.createDirectory(atPath: (bigPath as NSString).deletingLastPathComponent,
                                                withIntermediateDirectories: true)
        try Data().write(to: URL(fileURLWithPath: bigPath))
        let big = try FileHandle(forWritingTo: URL(fileURLWithPath: bigPath))
        try big.truncate(atOffset: UInt64(MailMemoryReader.maxAttachmentBytes) + 1)
        try big.close()
        // Symlinks: a file link, and a message folder that points outside the Mail folder.
        let outside = "\(root)/outside"
        try FileManager.default.createDirectory(atPath: "\(outside)/1", withIntermediateDirectories: true)
        try Data("outside secret".utf8).write(to: URL(fileURLWithPath: "\(outside)/secret.txt"))
        try Data("outside old".utf8).write(to: URL(fileURLWithPath: "\(outside)/1/old.pdf"))
        try FileManager.default.createDirectory(atPath: "\(store)/Attachments/11/2", withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(atPath: "\(store)/Attachments/11/2/link.txt",
                                                   withDestinationPath: "\(outside)/secret.txt")
        try FileManager.default.createSymbolicLink(atPath: "\(store)/Attachments/20", withDestinationPath: outside)

        func listing(_ over: [String: Any]) -> [String: Any] {
            let pair = makeReader(attRoot)
            return pair.1.ask(pair.0, attachmentsRequest(over))
        }
        func names(_ reply: [String: Any]) -> [String] {
            (reply["rows"] as? [[String: Any]] ?? []).map { $0["name"] as? String ?? "" }
        }
        let all = listing(["terms": []])
        let allRows = all["rows"] as? [[String: Any]] ?? []
        check(all["ok"] as? Bool == true && all["capped"] as? Bool == false && all["kind"] as? String == "attachments"
              && all["request_id"] as? String == "abcdefghijklmnop", "attachments reply answers its request: \(all)")
        check(names(all) == ["tax_50%_form.xlsx", "link.txt", "filters.txt", "big.bin", "sent.pdf", "lease.pdf", "photo.png", "old.pdf"],
              "newest first; trash, junk, drafts, spam, deleted and trash-labelled are excluded: \(names(all))")
        let byName = Dictionary(uniqueKeysWithValues: allRows.map { ($0["name"] as? String ?? "", $0) })
        check(byName["lease.pdf"]?["on_disk"] as? Bool == true && byName["lease.pdf"]?["size"] as? Int == 7,
              "on_disk true with the real size")
        check(byName["photo.png"]?["on_disk"] as? Bool == false && byName["photo.png"]?["size"] as? Int == 0,
              "on_disk false when Mail did not save it")
        check(byName["sent.pdf"]?["on_disk"] as? Bool == false, "on_disk false when the message has no .emlx")
        check(byName["link.txt"]?["on_disk"] as? Bool == false, "a symlinked file is not on disk")
        check(byName["old.pdf"]?["on_disk"] as? Bool == false, "a symlinked message folder is not on disk")
        check(byName["lease.pdf"]?["box"] as? String == "other" && byName["tax_50%_form.xlsx"]?["box"] as? String == "sent"
              && byName["sent.pdf"]?["box"] as? String == "sent" && byName["filters.txt"]?["box"] as? String == "other",
              "box: sent by mailbox or label, else other")
        if let lease = byName["lease.pdf"] {
            check(lease["rowid"] as? Int64 == 10 && lease["attachment_id"] as? String == "1"
                  && lease["account"] as? String == "GM" && lease["subject"] as? String == "Lease renewal"
                  && lease["sender"] as? String == "leasing@harbor.example" && lease["sender_name"] as? String == "Harbor Homes"
                  && lease["rfc_message_id"] as? String == "<a@x>" && abs((lease["ts"] as? Double ?? 0) - (now - 86400)) < 1,
                  "row fields: \(lease)")
        } else { check(false, "lease.pdf row present") }
        check(names(listing(["terms": ["filters"]])) == ["filters.txt", "big.bin"], "a folder merely named like spam is read")
        check(names(listing(["terms": ["lease"]])) == ["lease.pdf", "photo.png"], "term matches the subject")
        check(names(listing(["terms": ["LEASE"]])) == ["lease.pdf", "photo.png"], "terms are case-insensitive")
        check(names(listing(["terms": ["lease", "photo"]])) == ["photo.png"], "every term must match")
        check(names(listing(["terms": ["harbor"]])) == ["lease.pdf", "photo.png"], "term matches the sender name")
        check(names(listing(["terms": ["leasing@"]])) == ["lease.pdf", "photo.png"], "term matches the sender address")
        check(names(listing(["terms": ["xlsx"]])) == ["tax_50%_form.xlsx"], "term matches the attachment name")
        check(names(listing(["terms": ["50%"]])) == ["tax_50%_form.xlsx", "link.txt"], "% is literal")
        check(names(listing(["terms": ["%"]])) == ["tax_50%_form.xlsx", "link.txt"], "a lone % is not a wildcard")
        check(names(listing(["terms": ["_"]])) == ["tax_50%_form.xlsx", "link.txt"], "a lone _ is not a wildcard")
        check(names(listing(["terms": ["lease_pdf"]])).isEmpty, "_ does not match any character")
        check(names(listing(["terms": ["' OR 1=1 --"]])).isEmpty, "a quote is just text")
        check((listing(["terms": ["x'); DROP TABLE attachments; --"]])["ok"] as? Bool) == true
              && names(listing(["terms": []])).count == 8, "injection leaves the index intact")
        check(names(listing(["terms": [], "since": now - 100_000, "until": now - 6000])) == ["sent.pdf", "lease.pdf", "photo.png"],
              "since and until bound the date")
        check(names(listing(["terms": [], "since": now - 4000])) == ["tax_50%_form.xlsx", "link.txt"], "since only")
        check(names(listing(["terms": [], "rfc_ids": ["<a@x>"]])) == ["lease.pdf", "photo.png"], "rfc_ids filter")
        check(names(listing(["terms": [], "rfc_ids": ["<a@x>", "<b@x>"]])).count == 4, "several rfc_ids")
        check(names(listing(["terms": [], "rfc_ids": ["<nope@x>"]])).isEmpty, "unknown rfc_id: nothing")
        check(names(listing(["terms": [], "rfc_ids": ["<a@x>' OR '1'='1"]])).isEmpty, "rfc_ids are bound")
        let few = listing(["terms": [], "limit": 3])
        check(names(few).count == 3 && few["capped"] as? Bool == true, "limit caps and says so")
        let exact = listing(["terms": [], "limit": 8])
        check(names(exact).count == 8 && exact["capped"] as? Bool == false, "exactly the limit is not capped")

        // One attachment's bytes.
        func fetch(_ rowid: Int, _ id: String, root: String = attRoot) -> [String: Any] {
            let pair = makeReader(root)
            return pair.1.ask(pair.0, attachmentRequest(["rowid": rowid, "attachment_id": id]))
        }
        let got = fetch(10, "1")
        check(got["ok"] as? Bool == true && got["name"] as? String == "lease.pdf" && got["size"] as? Int == 7
              && (got["data_b64"] as? String).flatMap({ Data(base64Encoded: $0) }) == Data("PDFDATA".utf8)
              && got["kind"] as? String == "attachment", "attachment bytes returned: \(got.keys.sorted())")
        check(fetch(10, "2")["error"] as? String == "not_on_disk", "a row Mail did not save: not_on_disk")
        check(fetch(10, "9")["error"] as? String == "not_found", "no such attachment: not_found")
        check(fetch(999, "1")["error"] as? String == "not_found", "no such message: not_found")
        for (rowid, why) in [(12, "trash"), (13, "junk"), (14, "drafts"), (15, "spam"), (16, "deleted"), (18, "trash label")] {
            let reply = fetch(rowid, "1")
            check(reply["ok"] as? Bool == false && reply["error"] as? String == "not_found", "\(why) is never read: \(reply["error"] ?? "?")")
        }
        let tooBig = fetch(17, "2")
        check(tooBig["error"] as? String == "too_large", "over the cap: too_large (\(tooBig["error"] ?? "-"))")
        check(fetch(17, "1")["data_b64"] as? String == Data("hello".utf8).base64EncodedString(),
              "a folder merely named like spam is read")
        check(fetch(11, "2")["error"] as? String == "not_on_disk", "a symlinked file is refused")
        check(fetch(20, "1")["error"] as? String == "not_on_disk", "a symlinked folder outside Mail is refused")
        check(fetch(19, "1")["error"] as? String == "not_on_disk", "a message without an .emlx: not_on_disk")
        check(fetch(10, "1", root: "\(root)/nowhere")["error"] as? String == "no_mail_index", "no Mail folder: no_mail_index")
        let noAttachmentTable = makeReader("\(root)/unsupported")
        let unsupportedAttachments = noAttachmentTable.1.ask(noAttachmentTable.0, attachmentsRequest())
        check(unsupportedAttachments["ok"] as? Bool == false && unsupportedAttachments["error"] as? String == "unsupported_schema",
              "no attachments table: unsupported_schema")
        do { _ = try MailMemoryReader.attachmentRows(dbPath: bare, terms: [], since: 0, until: 0, rfcIDs: [], limit: 5) { _, _, _ in nil }
             check(false, "a schema without attachments throws") }
        catch { check((error as? MailMemoryReader.IndexFailure) == .unsupportedSchema, "attachmentRows: unsupported_schema") }
        do { _ = try MailMemoryReader.attachmentRows(dbPath: junk, terms: [], since: 0, until: 0, rfcIDs: [], limit: 5) { _, _, _ in nil }
             check(false, "a junk file throws") }
        catch { check((error as? MailMemoryReader.IndexFailure) == .unreadable, "attachmentRows: not a database is unreadable") }

        // Path guard, directly: separators, dot names and ids that climb out are refused.
        let emlxPath = "\(store)/Messages/10.partial.emlx"
        let version = "\(attRoot)/V10"
        check(MailMemoryReader.attachmentFile(emlxPath: emlxPath, versionDirectory: version, rowid: 10,
                                              attachmentID: "1", name: "lease.pdf")?.size == 7, "guard accepts the real file")
        for (id, name) in [("1", "../1/lease.pdf"), ("1", ".."), ("1", "."), ("1", ""), ("1", "a/b"), ("..", "lease.pdf"),
                           (".", "lease.pdf"), ("../1", "lease.pdf"), ("1", "lease.pdf\0x")] {
            check(MailMemoryReader.attachmentFile(emlxPath: emlxPath, versionDirectory: version, rowid: 10,
                                                  attachmentID: id, name: name) == nil, "guard refuses id \(id) name \(name)")
        }
        check(MailMemoryReader.attachmentFile(emlxPath: emlxPath, versionDirectory: "\(root)/V9", rowid: 10,
                                              attachmentID: "1", name: "lease.pdf") == nil, "outside the version folder is refused")
        check(MailMemoryReader.attachmentFile(emlxPath: emlxPath, versionDirectory: version, rowid: 10,
                                              attachmentID: "1", name: "missing.pdf") == nil, "a missing file is refused")
        check(MailMemoryReader.attachmentFile(emlxPath: emlxPath, versionDirectory: version, rowid: 10,
                                              attachmentID: "1", name: "..") == nil, "a directory is never a file")


        // MARK: Messages attachments (kinds "message_attachments" and "message_attachment")
        func msgListRequest(_ over: [String: Any] = [:]) -> [String: Any] {
            var base: [String: Any] = ["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "message_attachments",
                                       "terms": [], "handles": [], "since": 0, "until": 0, "limit": 50]
            for (k, v) in over { base[k] = v }
            return base
        }
        func msgOneRequest(_ over: [String: Any] = [:]) -> [String: Any] {
            var base: [String: Any] = ["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "message_attachment",
                                       "attachment_id": "1"]
            for (k, v) in over { base[k] = v }
            return base
        }
        let mParsed = MailMemoryReader.request(msgListRequest(["terms": ["a"], "handles": ["+1555"], "since": 5.5, "until": 9, "limit": 400]))
        check(mParsed?.kind == "message_attachments" && mParsed?.terms == ["a"] && mParsed?.handles == ["+1555"]
              && mParsed?.since == 5.5 && mParsed?.until == 9 && mParsed?.limit == 400, "message_attachments request parses")
        check(MailMemoryReader.request(msgListRequest(["event_id": "e1"])) != nil, "message_attachments: hub event_id tolerated")
        check(MailMemoryReader.request(msgListRequest(["terms": ["a", "b", "c", "d", "e"]])) != nil, "message_attachments: five terms allowed")
        check(MailMemoryReader.request(msgListRequest(["terms": ["a", "b", "c", "d", "e", "f"]])) == nil, "message_attachments: six terms refused")
        check(MailMemoryReader.request(msgListRequest(["terms": [1]])) == nil, "message_attachments: non-string term refused")
        check(MailMemoryReader.request(msgListRequest(["terms": "lease"])) == nil, "message_attachments: terms must be an array")
        check(MailMemoryReader.request(msgListRequest(["handles": (0..<10).map { "h\($0)" }])) != nil, "message_attachments: ten handles allowed")
        check(MailMemoryReader.request(msgListRequest(["handles": (0..<11).map { "h\($0)" }])) == nil, "message_attachments: eleven handles refused")
        check(MailMemoryReader.request(msgListRequest(["handles": [""]])) == nil, "message_attachments: empty handle refused")
        check(MailMemoryReader.request(msgListRequest(["handles": [String(repeating: "h", count: 100)]])) != nil, "message_attachments: 100-character handle allowed")
        check(MailMemoryReader.request(msgListRequest(["handles": [String(repeating: "h", count: 101)]])) == nil, "message_attachments: 101-character handle refused")
        for bad in ["a\nb", "a\0b", "a\u{7}", "\u{1B}[0m", "a\u{7F}"] {
            check(MailMemoryReader.request(msgListRequest(["handles": [bad]])) == nil, "message_attachments: control character in handle refused")
        }
        check(MailMemoryReader.request(msgListRequest(["handles": [5]])) == nil, "message_attachments: non-string handle refused")
        check(MailMemoryReader.request(msgListRequest(["handles": "bob"])) == nil, "message_attachments: handles must be an array")
        check(MailMemoryReader.request(msgListRequest(["terms": [""]])) == nil, "message_attachments: empty term refused")
        check(MailMemoryReader.request(msgListRequest(["terms": [String(repeating: "t", count: 100)]])) != nil, "message_attachments: 100-character term allowed")
        check(MailMemoryReader.request(msgListRequest(["terms": [String(repeating: "t", count: 101)]])) == nil, "message_attachments: 101-character term refused")
        for bad in ["a\nb", "a\0b", "a\u{7}", "a\u{7F}"] {
            check(MailMemoryReader.request(msgListRequest(["terms": [bad]])) == nil, "message_attachments: control character in term refused")
        }
        for key in ["since", "until", "limit"] {
            check(MailMemoryReader.request(msgListRequest([key: true])) == nil, "message_attachments: boolean \(key) refused")
            check(MailMemoryReader.request(msgListRequest([key: NSNumber(value: false)])) == nil, "message_attachments: NSNumber boolean \(key) refused")
        }
        check(MailMemoryReader.request(msgListRequest(["limit": 0])) == nil, "message_attachments: limit 0 refused")
        check(MailMemoryReader.request(msgListRequest(["limit": 401])) == nil, "message_attachments: limit 401 refused")
        check(MailMemoryReader.request(msgListRequest(["limit": "5"])) == nil, "message_attachments: limit must be a number")
        check(MailMemoryReader.request(msgListRequest(["since": -1])) == nil, "message_attachments: negative since refused")
        check(MailMemoryReader.request(msgListRequest(["until": "never"])) == nil, "message_attachments: until must be a number")
        check(MailMemoryReader.request(msgListRequest(["path": "/etc"])) == nil, "message_attachments: extra key refused")
        check(MailMemoryReader.request(msgListRequest(["rfc_ids": []])) == nil, "message_attachments: the Mail key rfc_ids refused")
        for key in ["terms", "handles", "since", "until", "limit"] {
            var missing = msgListRequest()
            missing[key] = nil
            check(MailMemoryReader.request(missing) == nil, "message_attachments: missing \(key) refused")
        }
        check(MailMemoryReader.request(msgOneRequest())?.attachmentID == "1" && MailMemoryReader.request(msgOneRequest())?.kind == "message_attachment",
              "message_attachment request parses")
        check(MailMemoryReader.request(msgOneRequest(["attachment_id": "123456789012345678"])) != nil, "message_attachment: 18 digits allowed")
        for bad: Any in ["", "1234567890123456789", "12a", "-1", "1.5", "+1", " 1", "1\n", "١٢", "../1", 5, NSNull()] {
            check(MailMemoryReader.request(msgOneRequest(["attachment_id": bad])) == nil, "message_attachment: id refused: \(bad)")
        }
        check(MailMemoryReader.request(msgOneRequest(["path": "/etc/hosts"])) == nil, "message_attachment: a path key is refused")
        check(MailMemoryReader.request(msgOneRequest(["rowid": 1])) == nil, "message_attachment: extra key refused")
        var noMsgID = msgOneRequest()
        noMsgID["attachment_id"] = nil
        check(MailMemoryReader.request(noMsgID) == nil, "message_attachment: id required")

        // Fixture chat.db and Attachments folder (real files; nothing from the user's Messages).
        func realOf(_ path: String) -> String {
            guard let resolved = realpath(path, nil) else { exit(2) }
            defer { free(resolved) }
            return String(cString: resolved)
        }
        let msgBase = realOf(root) + "/msg"
        let msgAttachments = msgBase + "/Attachments"
        func putMsg(_ rel: String, _ text: String) throws {
            let path = "\(msgAttachments)/\(rel)"
            try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                                    withIntermediateDirectories: true)
            try Data(text.utf8).write(to: URL(fileURLWithPath: path))
        }
        try putMsg("ab/01/G1/lease.pdf", "LEASEDATA!")
        try putMsg("ab/02/G2/photo.heic", "PHOTO")
        try putMsg("cd/03/G3/notes.txt", "NOTES")
        for rel in ["st/04/G4/sticker.png", "hi/05/G5/hidden.png", "pl/06/G6/x.pluginpayloadattachment", "ta/08/G8/tap.txt"] {
            try putMsg(rel, "never read")
        }
        try putMsg("gh/10/G10/tilde.txt", "TILDE")
        try putMsg("rc/18/G18/gone.txt", "GONE")
        try putMsg("em/20/G20/empty.txt", "")
        try FileManager.default.createDirectory(atPath: "\(msgBase)/outside", withIntermediateDirectories: true)
        try Data("outside secret".utf8).write(to: URL(fileURLWithPath: "\(msgBase)/outside/secret.txt"))
        try FileManager.default.createDirectory(atPath: "\(msgBase)/Attachments2", withIntermediateDirectories: true)
        try Data("sibling leak".utf8).write(to: URL(fileURLWithPath: "\(msgBase)/Attachments2/leak.txt"))
        try FileManager.default.createDirectory(atPath: "\(msgAttachments)/sy/07/G7", withIntermediateDirectories: true)
        try FileManager.default.createSymbolicLink(atPath: "\(msgAttachments)/sy/07/G7/linked.txt",
                                                   withDestinationPath: "\(msgBase)/outside/secret.txt")
        try FileManager.default.createSymbolicLink(atPath: "\(msgAttachments)/lnk", withDestinationPath: "\(msgBase)/outside")
        func appleNs(_ unix: Double) -> Int64 { Int64((unix - 978_307_200) * 1_000_000_000) }
        let msgT0 = 1_700_000_000.0
        let conv = "~/Library/Messages/Attachments"
        let chatDB = makeDB("chatfixture", """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, display_name TEXT, chat_identifier TEXT);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, handle_id INTEGER, is_from_me INTEGER,
          associated_message_type INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, transfer_name TEXT, mime_type TEXT,
          total_bytes INTEGER, is_sticker INTEGER, hide_attachment INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        CREATE TABLE chat_recoverable_message_join (chat_id INTEGER, message_id INTEGER, delete_date INTEGER);
        INSERT INTO handle VALUES (1, '+15551234567'), (2, 'Bob@Example.com'), (3, '+15559998888'), (4, '+15550001111');
        INSERT INTO chat VALUES (1, '', '+15551234567'), (2, 'Book Club', 'chat123'), (3, '', 'chat456'),
          (4, '', 'Bob@Example.com'), (5, 'Archive Trip', 'chat789');
        INSERT INTO chat_handle_join VALUES (1, 1), (2, 2), (2, 3), (3, 1), (3, 2), (4, 2), (5, 4);
        INSERT INTO message VALUES (1, \(appleNs(msgT0)), 1, 0, 0), (2, \(appleNs(msgT0 + 100)), 2, 0, 0),
          (3, \(appleNs(msgT0 + 200)), 2, 1, 0), (4, \(appleNs(msgT0 + 300)), 1, 0, 0), (5, \(appleNs(msgT0 + 350)), 1, 0, 2000),
          (6, \(appleNs(msgT0 + 400)), 3, 0, 0), (7, \(appleNs(msgT0 - 5000)), 4, 0, 0);
        INSERT INTO chat_message_join VALUES (1, 1), (2, 2), (3, 2), (4, 3), (1, 4), (1, 5), (2, 6), (5, 7);
        INSERT INTO chat_recoverable_message_join VALUES (5, 7, 1000);
        INSERT INTO attachment VALUES
          (1, '\(conv)/ab/01/G1/lease.pdf', 'Lease 2026.pdf', 'application/pdf', 10, 0, 0),
          (2, '\(conv)/ab/02/G2/photo.heic', 'photo.heic', 'image/heic', 5, 0, 0),
          (3, '\(conv)/cd/03/G3/notes.txt', 'notes.txt', 'text/plain', 5, 0, 0),
          (4, '\(conv)/st/04/G4/sticker.png', 'sticker.png', 'image/png', 9, 1, 0),
          (5, '\(conv)/hi/05/G5/hidden.png', 'hidden.png', 'image/png', 9, 0, 1),
          (6, '\(conv)/pl/06/G6/x.pluginpayloadattachment', 'x.pluginpayloadattachment', '', 9, 0, 0),
          (7, '\(conv)/sy/07/G7/linked.txt', 'linked.txt', 'text/plain', 14, 0, 0),
          (8, '\(conv)/ta/08/G8/tap.txt', 'tap.txt', 'text/plain', 9, 0, 0),
          (9, '/etc/hosts', 'hosts', 'text/plain', 9, 0, 0),
          (10, '\(conv)/gh/10/G10/tilde.txt', 'tilde.txt', 'text/plain', 5, 0, 0),
          (11, '\(conv)/lnk/secret.txt', 'secret.txt', 'text/plain', 14, 0, 0),
          (12, '\(conv)/zz/12/G12/missing.txt', 'missing.txt', 'text/plain', 9, 0, 0),
          (13, '', 'nameless.txt', 'text/plain', 9, 0, 0),
          (14, NULL, 'null.txt', 'text/plain', 9, 0, 0),
          (16, '\(conv)/../Attachments2/leak.txt', 'leak.txt', 'text/plain', 12, 0, 0),
          (17, '\(msgBase)/Attachments2/leak.txt', 'leak2.txt', 'text/plain', 12, 0, 0),
          (18, '\(conv)/rc/18/G18/gone.txt', 'gone.txt', 'text/plain', 4, 0, 0);
        INSERT INTO message_attachment_join VALUES (1, 1), (2, 2), (3, 3), (4, 4), (4, 5), (4, 6), (4, 7), (5, 8),
          (6, 9), (6, 10), (6, 11), (6, 12), (6, 13), (6, 14), (6, 16), (6, 17), (7, 18);
        """)
        func msgPair(_ db: String, cap: Int = MailMemoryReader.maxAttachmentBytes) -> (MailMemoryReader, Replies) {
            let replies = Replies()
            let reader = MailMemoryReader(mailRoot: "\(root)/nowhere", messagesDB: db, messagesAttachmentsRoot: msgAttachments,
                                          maxMessageFileBytes: cap) { reply in
                replies.last = reply
                replies.sem.signal()
            }
            return (reader, replies)
        }
        func msgList(_ over: [String: Any], db: String = chatDB) -> [String: Any] {
            let pair = msgPair(db)
            return pair.1.ask(pair.0, msgListRequest(over))
        }
        func msgIDs(_ reply: [String: Any]) -> [String] {
            (reply["rows"] as? [[String: Any]] ?? []).map { $0["id"] as? String ?? "?" }
        }
        let mAll = msgList([:])
        let mRows = mAll["rows"] as? [[String: Any]] ?? []
        check(mAll["ok"] as? Bool == true && mAll["capped"] as? Bool == false && mAll["kind"] as? String == "message_attachments"
              && mAll["request_id"] as? String == "abcdefghijklmnop", "message_attachments reply answers its request: \(mAll)")
        check(msgIDs(mAll) == ["9", "10", "11", "12", "16", "17", "7", "3", "2", "1"],
              "newest first, ties by id; sticker, hidden, preview, tapback and nameless rows left out, one row for a file in two chats: \(msgIDs(mAll))")
        let mBy = Dictionary(uniqueKeysWithValues: mRows.map { ($0["id"] as? String ?? "", $0) })
        if let lease = mBy["1"] {
            check(lease["chat"] as? String == "+15551234567" && lease["sender"] as? String == "+15551234567"
                  && lease["from_me"] as? Bool == false, "message attachment row, chat and sender: \(lease)")
            check(lease["name"] as? String == "Lease 2026.pdf" && lease["mime"] as? String == "application/pdf"
                  && lease["size"] as? Int == 10 && lease["on_disk"] as? Bool == true,
                  "message attachment row, file: \(lease)")
            check(abs((lease["ts"] as? Double ?? 0) - msgT0) < 0.001, "message attachment row, time: \(lease)")
        } else { check(false, "message attachment 1 present") }
        check(mBy["3"]?["from_me"] as? Bool == true && mBy["3"]?["sender"] as? String == "" && mBy["3"]?["chat"] as? String == "Bob@Example.com",
              "a file the user sent has no sender: \(String(describing: mBy["3"]))")
        check(mBy["2"]?["chat"] as? String == "Group \"Book Club\"" && mBy["2"]?["sender"] as? String == "Bob@Example.com",
              "a named group's label, the lowest chat of a file in two: \(String(describing: mBy["2"]))")
        check(mBy["7"]?["chat"] as? String == "+15551234567", "a file in a 1:1 chat is labelled by the other handle: \(String(describing: mBy["7"]))")
        check(MailMemoryReader.messageChatLabel(chatName: "", chatIdentifier: "chat456", members: ["Bob@Example.com", "+15551234567"],
                                                fallback: "x") == "Group of 2 (+15551234567, Bob@Example.com)",
              "an unnamed group lists its members, sorted")
        check(mBy["10"]?["on_disk"] as? Bool == true, "a ~-prefixed name resolves against the Attachments root")
        for (id, why) in [("7", "a symlinked file"), ("9", "a path outside the root (/etc/hosts)"), ("11", "a symlinked folder below the root"),
                          ("12", "a missing file"), ("16", "a .. path out of the root"), ("17", "an absolute path outside the root")] {
            check(mBy[id]?["on_disk"] as? Bool == false, "not on disk: \(why)")
        }
        check(!msgIDs(mAll).contains("18") && msgIDs(msgList(["terms": ["gone"]])).isEmpty,
              "a recently deleted message's file is not listed, even by name")
        check(msgIDs(msgList(["terms": ["lease"]])) == ["1"], "term matches the file name")
        check(msgIDs(msgList(["terms": ["LEASE", "2026.PDF"]])) == ["1"], "terms are case-insensitive and all must match")
        check(msgIDs(msgList(["terms": ["lease", "book"]])).isEmpty, "every term must match")
        check(msgIDs(msgList(["terms": ["book club"]])) == ["9", "10", "11", "12", "16", "17", "2"],
              "term matches the chat name: every file in that chat, whoever sent it")
        check(msgIDs(msgList(["terms": ["bob@"]])) == ["3", "2"],
              "term matches the sender's handle and a 1:1 chat's identifier, never another member's handle")
        check(msgIDs(msgList(["terms": ["+1555999"]])) == ["9", "10", "11", "12", "16", "17"], "term matches the sender's handle only")
        check(msgIDs(msgList(["terms": ["+1555123"]])) == ["7", "1"], "a sender's handle and a 1:1 chat's identifier")
        check(msgIDs(msgList(["terms": ["chat123"]])).isEmpty, "an opaque chat identifier is not searched")
        check(msgIDs(msgList(["terms": ["%"]])).isEmpty && msgIDs(msgList(["terms": ["_"]])).isEmpty, "% and _ are literal")
        check(msgIDs(msgList(["terms": ["' OR 1=1 --"]])).isEmpty, "a quote is just text")
        check(msgIDs(msgList(["terms": ["x'); DROP TABLE attachment; --"]])).isEmpty && msgIDs(msgList([:])).count == 10,
              "injection leaves chat.db intact")
        check(msgIDs(msgList(["handles": ["bob@example.COM"]])) == ["2"], "handle filter is case-insensitive and never matches the user's own files")
        check(msgIDs(msgList(["handles": ["+15559998888", "nobody"]])) == ["9", "10", "11", "12", "16", "17"], "several handles")
        check(msgIDs(msgList(["handles": ["nobody"]])).isEmpty, "unknown handle: nothing")
        check(msgIDs(msgList(["handles": ["+15551234567"], "terms": ["lease"]])) == ["1"], "handles and terms together")
        check(msgIDs(msgList(["handles": ["' OR '1'='1"]])).isEmpty, "handles are bound")
        check(msgIDs(msgList(["since": msgT0 + 100, "until": msgT0 + 300])) == ["3"], "since and until are exclusive")
        check(msgIDs(msgList(["since": msgT0 + 250])) == ["9", "10", "11", "12", "16", "17", "7"], "since only")
        check(msgIDs(msgList(["until": msgT0 + 150])) == ["2", "1"], "until only")
        check(msgIDs(msgList(["since": 1e300])).isEmpty, "an absurd since can't trap")
        let mFew = msgList(["limit": 3])
        check(msgIDs(mFew) == ["9", "10", "11"] && mFew["capped"] as? Bool == true, "limit caps and says so")
        let mExact = msgList(["limit": 10])
        check(msgIDs(mExact).count == 10 && mExact["capped"] as? Bool == false, "exactly the limit is not capped")
        check(msgList([:], db: "\(root)/no-such-chat.db")["error"] as? String == "no_messages_db", "no chat.db: no_messages_db")
        check(msgList([:], db: junk)["error"] as? String == "no_messages_db", "not a database: no_messages_db")
        check(msgList([:], db: bare)["error"] as? String == "unsupported_schema", "no attachment tables: unsupported_schema")

        // A schema without the optional columns and the chat tables still lists, by file name.
        let thinDB = makeDB("chatthin", """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, handle_id INTEGER, is_from_me INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+15551234567');
        INSERT INTO message VALUES (1, \(appleNs(msgT0)), 1, 0);
        INSERT INTO attachment VALUES (1, '\(conv)/ab/01/G1/lease.pdf'), (2, '\(conv)/cd/03/G3/notes.txt');
        INSERT INTO message_attachment_join VALUES (1, 1), (1, 2);
        """)
        let thin = msgList(["terms": ["lease"]], db: thinDB)
        let thinRow = (thin["rows"] as? [[String: Any]] ?? []).first ?? [:]
        check(msgIDs(thin) == ["1"], "thin schema lists the matching file: \(thin)")
        check(thinRow["name"] as? String == "lease.pdf" && thinRow["mime"] as? String == "" && thinRow["size"] as? Int == 0,
              "thin schema, file: \(thinRow)")
        check(thinRow["chat"] as? String == "+15551234567" && thinRow["on_disk"] as? Bool == true, "thin schema, chat: \(thinRow)")
        check(msgIDs(msgList([:], db: thinDB)) == ["1", "2"], "thin schema lists every file")

        // One attachment's bytes.
        func msgFetch(_ id: String, db: String = chatDB, cap: Int = MailMemoryReader.maxAttachmentBytes) -> [String: Any] {
            let pair = msgPair(db, cap: cap)
            return pair.1.ask(pair.0, msgOneRequest(["attachment_id": id]))
        }
        let mGot = msgFetch("1")
        check(mGot["ok"] as? Bool == true && mGot["name"] as? String == "Lease 2026.pdf" && mGot["size"] as? Int == 10
              && (mGot["data_b64"] as? String).flatMap({ Data(base64Encoded: $0) }) == Data("LEASEDATA!".utf8)
              && mGot["kind"] as? String == "message_attachment" && mGot["request_id"] as? String == "abcdefghijklmnop",
              "message attachment bytes returned: \(mGot.keys.sorted())")
        check((msgFetch("10")["data_b64"] as? String).flatMap({ Data(base64Encoded: $0) }) == Data("TILDE".utf8),
              "a ~-prefixed name is read from the fixture root")
        for (id, why) in [("4", "a sticker"), ("5", "a hidden attachment"), ("6", "a link preview"), ("8", "a tapback row"),
                          ("13", "an empty file name"), ("14", "a missing file name"), ("999", "an unknown id")] {
            let reply = msgFetch(id)
            check(reply["ok"] as? Bool == false && reply["error"] as? String == "not_found", "\(why) is not_found: \(reply["error"] ?? "?")")
        }
        for (id, why) in [("12", "a missing file"), ("7", "a symlinked file"), ("9", "/etc/hosts, outside the root"),
                          ("11", "a symlinked folder"), ("16", "a .. path out of the root"), ("17", "an absolute path outside the root")] {
            let reply = msgFetch(id)
            check(reply["ok"] as? Bool == false && reply["error"] as? String == "not_on_disk" && reply["data_b64"] == nil,
                  "\(why) is not_on_disk: \(reply["error"] ?? "?")")
        }
        check(msgFetch("18")["ok"] as? Bool == false && msgFetch("18")["error"] as? String == "not_found",
              "a recently deleted message's file is not_found")
        // A zero-byte file is a success with no bytes, not unreadable.
        let emptyDB = makeDB("chatempty", """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, handle_id INTEGER, is_from_me INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+15551234567');
        INSERT INTO message VALUES (1, \(appleNs(msgT0)), 1, 0);
        INSERT INTO attachment VALUES (1, '\(conv)/em/20/G20/empty.txt');
        INSERT INTO message_attachment_join VALUES (1, 1);
        """)
        let emptyGot = msgFetch("1", db: emptyDB)
        check(emptyGot["ok"] as? Bool == true && emptyGot["size"] as? Int == 0 && emptyGot["data_b64"] as? String == ""
              && emptyGot["name"] as? String == "empty.txt", "an empty file reads as empty: \(emptyGot)")
        check(msgFetch("1", cap: 9)["error"] as? String == "too_large", "over the cap: too_large")
        check(msgFetch("1", cap: 10)["ok"] as? Bool == true && msgFetch("2", cap: 9)["ok"] as? Bool == true, "exactly the cap, and under it, are read")
        check(msgFetch("1", db: "\(root)/no-such-chat.db")["error"] as? String == "no_messages_db", "read: no chat.db is no_messages_db")
        check(msgFetch("1", db: junk)["error"] as? String == "no_messages_db", "read: not a database is no_messages_db")
        check(msgFetch("1", db: "\(root)/no-such-dir/chat.db")["error"] as? String == "no_messages_db", "read: a missing folder is no_messages_db")
        check(MailMemoryReader.messageAttachmentFile(filename: "\(msgAttachments)/ab/01/G1/lease.pdf", root: msgAttachments)?.size == 10,
              "guard accepts the real file by its absolute path")
        check(MailMemoryReader.messageAttachmentFile(filename: "\(msgAttachments)/ab/01/G1", root: msgAttachments) == nil, "guard refuses a folder")
        check(MailMemoryReader.messageAttachmentFile(filename: "\(msgAttachments)/ab/01/G1/lease.pdf", root: "\(msgBase)/nowhere") == nil,
              "guard refuses when the root does not exist")
        check(MailMemoryReader.messageAttachmentFile(filename: "\(msgAttachments)/ab/01/G1/lease.pdf\0x", root: msgAttachments) == nil,
              "guard refuses a NUL")
        check(MailMemoryReader.messageAttachmentFile(filename: "~/Documents/lease.pdf", root: msgAttachments) == nil,
              "guard refuses a ~ path outside the Attachments folder")
        check(MailMemoryReader.messageChatLabel(chatName: nil, chatIdentifier: "chat9", members: [], fallback: "Me") == "Me"
              && MailMemoryReader.messageChatLabel(chatName: "Crew", chatIdentifier: nil, members: ["a"], fallback: "x") == "Crew",
              "chat labels")

        // MARK: Text recognition (kind "recognize")
        func recognizeRequest(_ over: [String: Any] = [:]) -> [String: Any] {
            var base: [String: Any] = ["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "recognize",
                                       "image_b64": "/9j/"]
            for (k, v) in over { base[k] = v }
            return base
        }
        check(MailMemoryReader.request(recognizeRequest())?.kind == "recognize"
              && MailMemoryReader.request(recognizeRequest())?.imageB64 == "/9j/", "recognize request parses")
        check(MailMemoryReader.request(recognizeRequest(["event_id": "e1"])) != nil, "recognize: hub event_id tolerated")
        check(MailMemoryReader.request(recognizeRequest(["rowid": 1])) == nil, "recognize: extra key refused")
        var noImage = recognizeRequest(); noImage.removeValue(forKey: "image_b64")
        check(MailMemoryReader.request(noImage) == nil, "recognize: missing image_b64 refused")
        check(MailMemoryReader.request(recognizeRequest(["image_b64": 5])) == nil, "recognize: non-string image refused")
        check(MailMemoryReader.request(recognizeRequest(["kind": "recognize", "attachment_id": "1"])) == nil,
              "recognize: attachment key refused")

        let recognizeReader = makeReader(root)
        let jpegHeader = Data([0xFF, 0xD8, 0xFF, 0xE0])
        let hugeImage = Data([0xFF, 0xD8, 0xFF]) + Data(count: MailMemoryReader.maxRecognizeBytes)
        let atLimit = Data([0xFF, 0xD8, 0xFF]) + Data(count: MailMemoryReader.maxRecognizeBytes - 3)
        let tooBigReply = recognizeReader.1.ask(recognizeReader.0, recognizeRequest(["image_b64": hugeImage.base64EncodedString()]))
        check(tooBigReply["ok"] as? Bool == false && tooBigReply["error"] as? String == "too_large"
              && tooBigReply["kind"] as? String == "recognize" && tooBigReply["request_id"] as? String == "abcdefghijklmnop",
              "recognize: over 4 MiB is too_large and still answered")
        check(MailMemoryReader.answerRecognize(String(repeating: "A", count: MailMemoryReader.maxRecognizeBase64 + 4))["error"]
              as? String == "too_large", "recognize: base64 longer than 4 MiB needs is too_large")
        check(MailMemoryReader.answerRecognize(atLimit.base64EncodedString())["error"] as? String == "failed",
              "recognize: exactly 4 MiB of JPEG-looking junk passes the size check and fails decoding")
        for (label, bytes) in [("png", Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A])), ("gif", Data("GIF89a".utf8)),
                               ("two bytes", Data([0xFF, 0xD8])), ("empty", Data()), ("pdf", Data("%PDF-1.4".utf8))] {
            check(MailMemoryReader.answerRecognize(bytes.base64EncodedString())["error"] as? String == "bad_image",
                  "recognize: \(label) is bad_image")
        }
        check(MailMemoryReader.answerRecognize("!!! not base64 !!!")["error"] as? String == "bad_image",
              "recognize: broken base64 is bad_image")
        check(MailMemoryReader.answerRecognize((jpegHeader + Data(count: 16)).base64EncodedString())["error"] as? String == "failed",
              "recognize: JPEG header over junk is failed")

        // Vision can't run inside some sandboxes (the gate's own, a CI runner's): there the
        // request itself fails. That is the environment, not the reader, so the two checks that
        // need real recognition are skipped with a note unless WISP_REQUIRE_TEXT_RECOGNITION=1.
        let mustRecognize = ProcessInfo.processInfo.environment["WISP_REQUIRE_TEXT_RECOGNITION"] == "1"
        let drawn = textJPEG(["INVOICE 4471", "TOTAL 1280.50"])
        check(drawn != nil, "the test image could be drawn")
        let canRecognize = drawn.flatMap { TextRecognizer.recognize(jpeg: $0) } != nil
        if !canRecognize && !mustRecognize {
            print("MailMemoryReader: text recognition can't run here; 2 recognition checks skipped")
        } else if let invoice = drawn,
           let found = TextRecognizer.recognize(jpeg: invoice) {
            check(found.text.contains("4471") && found.text.contains("1280"), "recognizes the drawn text: \(found.text)")
            check(found.lines >= 2 && (0...1).contains(found.confidence) && found.confidence > 0.3,
                  "lines and confidence are sensible: \(found.lines) \(found.confidence)")
            let viaReader = recognizeReader.1.ask(recognizeReader.0, recognizeRequest(["image_b64": invoice.base64EncodedString()]))
            check(viaReader["ok"] as? Bool == true && (viaReader["text"] as? String)?.contains("4471") == true
                  && viaReader["lines"] as? Int == found.lines, "recognize reply carries text, lines, confidence")
        } else { check(false, "could not draw or recognize the invoice image") }
        if !canRecognize && !mustRecognize {
        } else if let blank = textJPEG([]), let found = TextRecognizer.recognize(jpeg: blank) {
            check(found.text.isEmpty && found.lines == 0 && found.confidence == 0, "a blank image has no text")
            check(MailMemoryReader.answerRecognize(blank.base64EncodedString())["error"] as? String == "no_text",
                  "a blank image is answered no_text")
        } else { check(false, "blank image recognition did not run") }
        check(TextRecognizer.recognize(jpeg: Data([0x89, 0x50, 0x4E, 0x47])) == nil, "recognizer refuses a non-JPEG")
        let grouped = TextRecognizer.group([
            (CGRect(x: 0.5, y: 0.80, width: 0.2, height: 0.05), "Price"), (CGRect(x: 0.1, y: 0.801, width: 0.2, height: 0.05), "Item"),
            (CGRect(x: 0.1, y: 0.40, width: 0.2, height: 0.05), "Lamp"), (CGRect(x: 0.5, y: 0.401, width: 0.2, height: 0.05), "9.50")])
        check(grouped == ["Item\tPrice", "Lamp\t9.50"], "cells go left to right with a tab, rows top to bottom: \(grouped)")

        // Declared-size gate (read from the header, before any pixel is decoded).
        check(TextRecognizer.acceptableSize(width: 6000, height: 4000), "size: 6000x4000 passes")
        check(TextRecognizer.acceptableSize(width: 8192, height: 4880), "size: 8192x4880 (39.98 MP) passes")
        check(!TextRecognizer.acceptableSize(width: 8193, height: 10), "size: side over 8192 refused")
        check(!TextRecognizer.acceptableSize(width: 10, height: 8193), "size: tall side over 8192 refused")
        check(!TextRecognizer.acceptableSize(width: 8192, height: 4883), "size: over 40 MP refused")
        check(!TextRecognizer.acceptableSize(width: 0, height: 100) && !TextRecognizer.acceptableSize(width: 100, height: 0),
              "size: zero refused")
        check(!TextRecognizer.acceptableSize(width: nil, height: 100) && !TextRecognizer.acceptableSize(width: 100, height: nil),
              "size: missing refused")
        check(!TextRecognizer.acceptableSize(width: -5, height: 100), "size: negative refused")
        if let small = textJPEG(["SIZE"]), let source = CGImageSourceCreateWithData(small as CFData, nil),
           let declared = TextRecognizer.declaredSize(source) {
            check(declared.width == 900 && declared.height == 150
                  && TextRecognizer.acceptableSize(width: declared.width, height: declared.height),
                  "a real small JPEG declares its size and passes: \(declared)")
            var patched = small
            var sof = -1
            var i = 2
            while i + 9 < patched.count {
                if patched[i] == 0xFF, patched[i + 1] == 0xC0 || patched[i + 1] == 0xC2 { sof = i; break }
                i += 1
            }
            check(sof > 0, "found the JPEG start-of-frame marker")
            if sof > 0 {
                // SOF layout: FF Cn, length(2), precision(1), height(2), width(2).
                patched[sof + 5] = 0x75; patched[sof + 6] = 0x30   // 30000
                patched[sof + 7] = 0x75; patched[sof + 8] = 0x30
                if let huge = CGImageSourceCreateWithData(patched as CFData, nil) {
                    let size = TextRecognizer.declaredSize(huge)
                    // ImageIO may report 30000x30000 or no size at all for it; both are refused.
                    check(!TextRecognizer.acceptableSize(width: size?.width, height: size?.height),
                          "a header declaring 30000x30000 is refused by the size gate: \(String(describing: size))")
                } else { check(false, "patched JPEG header could not be opened") }
                check(TextRecognizer.recognize(jpeg: patched) == nil, "recognize refuses the 30000x30000 header")
                check(MailMemoryReader.answerRecognize(patched.base64EncodedString())["error"] as? String == "failed",
                      "recognize reply for the 30000x30000 header is an error, not a decode")
            }
        } else { check(false, "could not draw the small JPEG for the size checks") }

        // `wait`: optional, 1...60, a real number; the staleness decision is pure.
        check(MailMemoryReader.request(recognizeRequest(["wait": 20]))?.wait == 20, "recognize: wait 20 parses")
        check(MailMemoryReader.request(recognizeRequest(["wait": 1]))?.wait == 1
              && MailMemoryReader.request(recognizeRequest(["wait": 60]))?.wait == 60, "recognize: wait 1 and 60 parse")
        check(MailMemoryReader.request(recognizeRequest(["wait": 2.5]))?.wait == 2.5, "recognize: fractional wait parses")
        check(MailMemoryReader.request(recognizeRequest())?.wait == 0, "recognize: no wait means unstated")
        for bad: Any in [0, 61, -3, 0.5, "20", true, false, NSNull(), Double.nan, Double.infinity] {
            check(MailMemoryReader.request(recognizeRequest(["wait": bad])) == nil, "recognize: wait \(bad) refused")
        }
        check(MailMemoryReader.request(recognizeRequest(["wait": 20, "rowid": 1])) == nil, "recognize: wait plus another key refused")
        check(MailMemoryReader.request(["type": "mail_memory_read", "request_id": "abcdefghijklmnop", "kind": "body",
                                        "rowid": 1, "wait": 20]) == nil, "wait is refused on other kinds")
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        check(!MailMemoryReader.isLate(received: t0, wait: 20, now: t0.addingTimeInterval(19.9)), "not late inside the wait")
        check(!MailMemoryReader.isLate(received: t0, wait: 20, now: t0.addingTimeInterval(20)), "exactly the wait is not late")
        check(MailMemoryReader.isLate(received: t0, wait: 20, now: t0.addingTimeInterval(20.1)), "past the wait is late")
        check(!MailMemoryReader.isLate(received: t0, wait: 0, now: t0.addingTimeInterval(500)), "no stated wait is never late")
        check(!MailMemoryReader.isLate(received: t0, wait: 20, now: t0.addingTimeInterval(-5)), "a clock step back is not late")

        if failures > 0 { print("\(failures) failure(s)"); exit(1) }
        print("MailMemoryReader: \(total) checks passed")
    }
}
