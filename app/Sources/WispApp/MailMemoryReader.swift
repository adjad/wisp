import Foundation
import SQLite3

/// Reads months-old mail for the backend's mail-memory queue (service/mail_memory).
///
/// The backend can't read Mail's files itself: Full Disk Access belongs to this
/// app, not to the Python child (see service/tools/imessage_tools.py). So the
/// backend publishes a `mail_memory_read` event and this reader answers it by
/// POSTing to /assistant/sync/mail_memory with the event's `request_id`.
///
/// IMPORTANT rules:
/// - Read-only. The Envelope Index is opened SQLITE_OPEN_READONLY; .emlx files
///   are only read. Mail.app is never scripted or launched from here.
/// - Eight requests only: `index` (metadata for a window of days, no bodies),
///   `body` (one message's raw RFC 822 bytes, capped; the backend parses MIME),
///   `sent` (the user's own sent mail for a few weeks: when, to whom, subject; no
///   bodies), which answers Ask's "what did I send" (view_emails mailbox='sent'),
///   `attachments` (one metadata row per attachment, filtered by terms, dates and RFC
///   Message-IDs, with whether Mail saved the file; never the bytes) and `attachment`
///   (one saved file's bytes, at most `maxAttachmentBytes`), and `recognize` (text in one
///   JPEG the backend itself re-encoded, read by Apple Vision on this Mac; nothing else is decoded),
///   `message_attachments` (one metadata row per file texted in Messages, filtered by terms, handles
///   and dates; never the bytes) and `message_attachment` (one such file's bytes, at most
///   `maxAttachmentBytes`). The Messages database is chat.db, opened SQLITE_OPEN_READONLY.
/// - Attachments are only read from `<Mail version>/.../Attachments/<rowid>/<id>/<name>`,
///   next to the `Messages` folder: a regular file (no symlink anywhere on the path)
///   inside the Mail version folder. Trash, junk/spam and drafts are never listed or
///   read, and a file is never opened, run or unpacked here: only its bytes are returned.
/// - A Messages file is only read from inside the real `~/Library/Messages/Attachments`: the path
///   comes from chat.db's own row (a request never carries one), must be a regular file with no
///   symlink anywhere below that folder, and hidden, sticker, link-preview and tapback rows are
///   never listed or read. A file is never opened, run or unpacked here: only its bytes are returned.
/// - Every POST answers one request: nothing is ever sent unasked.
final class MailMemoryReader {
    static let maxDays = 200
    static let maxIndexRows = 6000
    static let maxBodyBytes = 262_144
    static let maxSentDays = 31
    static let maxSentRows = 200
    static let maxRecipients = 5
    static let maxAttachmentBytes = 33_554_432
    static let maxAttachmentTerms = 5
    static let maxAttachmentRFCIDs = 40
    static let maxAttachmentRows = 400
    static let maxMessageHandles = 10
    static let maxRecognizeBytes = 4_194_304
    /// Base64 of `maxRecognizeBytes` bytes: 4 characters per 3 bytes, rounded up.
    static let maxRecognizeBase64 = (maxRecognizeBytes + 2) / 3 * 4

    private let mailRoot: String
    private let messagesDB: String
    private let messagesAttachmentsRoot: String
    private let maxMessageFileBytes: Int
    private let rebuildInterval: TimeInterval
    private let work = DispatchQueue(label: "wisp.mail-memory", qos: .utility)
    /// Text recognition has its own serial queue: a 40-page scan is 40 requests of up to a
    /// few seconds each and must not hold up Mail index and attachment reads.
    private let recognition = DispatchQueue(label: "wisp.text-recognition", qos: .userInitiated)
    private let send: ([String: Any]) -> Void
    private var emlx: [Int64: (path: String, partial: Bool)] = [:]
    private var emlxBuiltAt: Date?

    /// `send` delivers each reply; the app passes `MailMemoryReader.poster(to:)`.
    init(mailRoot: String = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Mail"),
         rebuildInterval: TimeInterval = 30,
         messagesDB: String = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Messages/chat.db"),
         messagesAttachmentsRoot: String = (NSHomeDirectory() as NSString)
             .appendingPathComponent("Library/Messages/Attachments"),
         maxMessageFileBytes: Int = MailMemoryReader.maxAttachmentBytes,
         send: @escaping ([String: Any]) -> Void) {
        self.mailRoot = mailRoot
        self.messagesDB = messagesDB
        self.messagesAttachmentsRoot = messagesAttachmentsRoot
        self.maxMessageFileBytes = maxMessageFileBytes
        self.rebuildInterval = rebuildInterval
        self.send = send
    }

    /// Entry point for the `mail_memory_read` event. Returns false for a malformed request.
    @discardableResult
    func handle(_ payload: [String: Any]) -> Bool {
        guard let request = MailMemoryReader.request(payload) else { return false }
        if request.kind == "recognize" {
            let received = Date()
            recognition.async { [weak self] in
                guard let self else { return }
                var reply: [String: Any]
                if MailMemoryReader.isLate(received: received, wait: request.wait, now: Date()) {
                    reply = ["ok": false, "error": "late"]
                } else {
                    reply = MailMemoryReader.answerRecognize(request.imageB64)
                }
                reply["request_id"] = request.id
                reply["kind"] = request.kind
                self.send(reply)
            }
            return true
        }
        work.async { [weak self] in
            guard let self else { return }
            var reply = self.answer(request)
            reply["request_id"] = request.id
            reply["kind"] = request.kind
            self.send(reply)
        }
        return true
    }

    struct Request: Equatable {
        let id: String
        let kind: String
        let days: Int
        let rowid: Int64
        var terms: [String] = []
        var handles: [String] = []
        var since: Double = 0
        var until: Double = 0
        var rfcIDs: [String] = []
        var limit: Int = 0
        var attachmentID: String = ""
        var imageB64: String = ""
        /// Seconds the backend will wait for a `recognize` reply (0 = not stated).
        var wait: Double = 0
    }

    /// True when the backend has already given up: more than `wait` seconds passed between
    /// receiving the request and its reaching the front of the recognition queue.
    static func isLate(received: Date, wait: Double, now: Date) -> Bool {
        wait > 0 && now.timeIntervalSince(received) > wait
    }

    /// Strict shape: {type, request_id, kind: "index", days}, {type, request_id, kind: "body", rowid},
    /// {type, request_id, kind: "sent", days} (at most `maxSentDays`),
    /// {type, request_id, kind: "attachments", terms, since, until, rfc_ids, limit} or
    /// {type, request_id, kind: "attachment", rowid, attachment_id} or
    /// {type, request_id, kind: "recognize", image_b64[, wait: 1...60 seconds]} (an oversize or non-JPEG image is
    /// still a well-formed request: it is answered `too_large` / `bad_image`, never dropped),
    /// {type, request_id, kind: "message_attachments", terms (at most 5), handles (at most 10, each 1...100
    /// characters, no control characters), since, until, limit 1...400} or
    /// {type, request_id, kind: "message_attachment", attachment_id (1 to 18 digits)}.
    static func request(_ payload: [String: Any]) -> Request? {
        guard payload["type"] as? String == "mail_memory_read",
              let id = payload["request_id"] as? String,
              id.range(of: "^[A-Za-z0-9_-]{16,64}$", options: .regularExpression) != nil,
              let kind = payload["kind"] as? String else { return nil }
        // The hub stamps durable events with an event_id; reads are published non-durable,
        // but a stamped one is still the same request.
        let keys = Set(payload.keys).subtracting(["event_id"])
        if kind == "index" {
            guard keys == ["type", "request_id", "kind", "days"],
                  let days = payload["days"] as? Int, (1...maxDays).contains(days) else { return nil }
            return Request(id: id, kind: kind, days: days, rowid: 0)
        }
        if kind == "sent" {
            guard keys == ["type", "request_id", "kind", "days"],
                  let days = payload["days"] as? Int, (1...maxSentDays).contains(days) else { return nil }
            return Request(id: id, kind: kind, days: days, rowid: 0)
        }
        if kind == "attachments" {
            guard keys == ["type", "request_id", "kind", "terms", "since", "until", "rfc_ids", "limit"],
                  let terms = payload["terms"] as? [String], terms.count <= maxAttachmentTerms,
                  let rfcIDs = payload["rfc_ids"] as? [String], rfcIDs.count <= maxAttachmentRFCIDs,
                  let since = (payload["since"] as? NSNumber)?.doubleValue, since.isFinite, since >= 0,
                  let until = (payload["until"] as? NSNumber)?.doubleValue, until.isFinite, until >= 0,
                  let limit = payload["limit"] as? Int, (1...maxAttachmentRows).contains(limit) else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: 0, terms: terms, since: since, until: until,
                           rfcIDs: rfcIDs, limit: limit)
        }
        if kind == "message_attachments" {
            guard keys == ["type", "request_id", "kind", "terms", "handles", "since", "until", "limit"],
                  let terms = payload["terms"] as? [String], terms.count <= maxAttachmentTerms,
                  let handles = payload["handles"] as? [String], handles.count <= maxMessageHandles,
                  handles.allSatisfy(isValidHandle),
                  let since = (payload["since"] as? NSNumber)?.doubleValue, since.isFinite, since >= 0,
                  let until = (payload["until"] as? NSNumber)?.doubleValue, until.isFinite, until >= 0,
                  let limit = payload["limit"] as? Int, (1...maxAttachmentRows).contains(limit) else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: 0, terms: terms, handles: handles,
                           since: since, until: until, limit: limit)
        }
        if kind == "message_attachment" {
            guard keys == ["type", "request_id", "kind", "attachment_id"],
                  let attachmentID = payload["attachment_id"] as? String,
                  (1...18).contains(attachmentID.utf8.count),
                  attachmentID.utf8.allSatisfy({ (0x30...0x39).contains($0) }) else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: 0, attachmentID: attachmentID)
        }
        if kind == "attachment" {
            guard keys == ["type", "request_id", "kind", "rowid", "attachment_id"],
                  let rowid = (payload["rowid"] as? NSNumber)?.int64Value, rowid > 0,
                  let attachmentID = payload["attachment_id"] as? String,
                  attachmentID.range(of: "^[0-9A-Za-z._-]{1,40}$", options: .regularExpression) != nil
            else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: rowid, attachmentID: attachmentID)
        }
        if kind == "recognize" {
            // `wait` is optional, but an app from before it existed drops a request that has it
            // (strict keys); app and backend ship together in one bundle.
            guard keys == ["type", "request_id", "kind", "image_b64"]
                    || keys == ["type", "request_id", "kind", "image_b64", "wait"],
                  let image = payload["image_b64"] as? String else { return nil }
            var wait = 0.0
            if keys.contains("wait") {
                guard let number = payload["wait"] as? NSNumber,
                      CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.isFinite, (1...60).contains(number.doubleValue) else { return nil }
                wait = number.doubleValue
            }
            return Request(id: id, kind: kind, days: 0, rowid: 0, imageB64: image, wait: wait)
        }
        if kind == "body" {
            guard keys == ["type", "request_id", "kind", "rowid"],
                  let rowid = (payload["rowid"] as? NSNumber)?.int64Value, rowid > 0 else { return nil }
            return Request(id: id, kind: kind, days: 0, rowid: rowid)
        }
        return nil
    }

    private func answer(_ request: Request) -> [String: Any] {
        if request.kind == "message_attachments" { return answerMessageAttachments(request) }
        if request.kind == "message_attachment" { return answerMessageAttachment(request) }
        if request.kind == "attachments" { return answerAttachments(request) }
        if request.kind == "attachment" { return answerAttachment(request) }
        if request.kind == "recognize" { return MailMemoryReader.answerRecognize(request.imageB64) }
        if request.kind == "sent" {
            guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
                return ["ok": false, "error": "no_mail_index"]
            }
            let since = Date().timeIntervalSince1970 - Double(request.days) * 86400
            do {
                let (rows, capped) = try MailMemoryReader.sentRows(dbPath: path, since: since)
                return ["ok": true, "rows": rows, "capped": capped]
            } catch let failure as IndexFailure {
                return ["ok": false, "error": failure.rawValue]
            } catch {
                return ["ok": false, "error": IndexFailure.unreadable.rawValue]
            }
        }
        if request.kind == "index" {
            guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
                return ["ok": false, "error": "no_mail_index"]
            }
            let since = Date().timeIntervalSince1970 - Double(request.days) * 86400
            do {
                let (rows, capped) = try MailMemoryReader.indexRows(dbPath: path, since: since)
                return ["ok": true, "rows": rows, "capped": capped]
            } catch let failure as IndexFailure {
                return ["ok": false, "error": failure.rawValue]
            } catch {
                return ["ok": false, "error": IndexFailure.unreadable.rawValue]
            }
        }
        // Only a message that is in Inbox or Sent right now: one moved to Trash, Junk or
        // Archive since the index, or a ROWID Mail reused, is never read.
        guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
            return ["ok": false, "error": "no_mail_index"]
        }
        // An index that can't be read right now (busy, access revoked) is not an answer:
        // the backend retries later instead of skipping the email for good.
        guard let (member, _) = try? MailMemoryReader.indexRows(dbPath: path, since: 0, only: request.rowid) else {
            return ["ok": false, "error": "index_unreadable"]
        }
        guard !member.isEmpty else { return ["ok": false, "error": "not_in_inbox_or_sent"] }
        // A rowid missing from the map, or a cached path that no longer reads, rebuilds
        // the map once (at most one rebuild per `rebuildInterval`) and retries.
        var rebuilt = false
        if emlx[request.rowid] == nil { rebuilt = rebuildEmlx() }
        guard var file = emlx[request.rowid] else { return ["ok": false, "error": "not_on_disk"] }
        var read = MailMemoryReader.message(at: file.path)
        if read == nil, !rebuilt, rebuildEmlx() {
            guard let fresh = emlx[request.rowid] else { return ["ok": false, "error": "not_on_disk"] }
            file = fresh
            read = MailMemoryReader.message(at: file.path)
        }
        guard let (data, truncated) = read else { return ["ok": false, "error": "unreadable"] }
        return ["ok": true, "rowid": request.rowid, "partial": file.partial, "truncated": truncated,
                "raw_b64": data.base64EncodedString()]
    }

    /// Reads the text in one JPEG. Error is `too_large`, `bad_image` (not base64 or not a JPEG),
    /// `no_text` (recognition ran and found nothing) or `failed` (it could not run).
    static func answerRecognize(_ b64: String) -> [String: Any] {
        guard b64.utf8.count <= maxRecognizeBase64 else { return ["ok": false, "error": "too_large"] }
        guard let data = Data(base64Encoded: b64), data.count >= 3,
              data[data.startIndex] == 0xFF, data[data.startIndex + 1] == 0xD8, data[data.startIndex + 2] == 0xFF
        else { return ["ok": false, "error": "bad_image"] }
        guard data.count <= maxRecognizeBytes else { return ["ok": false, "error": "too_large"] }
        guard let found = TextRecognizer.recognize(jpeg: data) else { return ["ok": false, "error": "failed"] }
        if found.lines == 0 || found.text.isEmpty { return ["ok": false, "error": "no_text"] }
        return ["ok": true, "text": found.text, "lines": found.lines, "confidence": found.confidence]
    }

    private func answerAttachments(_ request: Request) -> [String: Any] {
        guard let path = MailMemoryReader.envelopeIndexPath(mailRoot) else {
            return ["ok": false, "error": "no_mail_index"]
        }
        if emlxBuiltAt == nil { _ = rebuildEmlx() }
        let base = MailMemoryReader.versionDirectory(mailRoot)
        let map = emlx
        do {
            let (rows, capped) = try MailMemoryReader.attachmentRows(
                dbPath: path, terms: request.terms, since: request.since, until: request.until,
                rfcIDs: request.rfcIDs, limit: request.limit
            ) { rowid, attachmentID, name in
                guard let base, let file = map[rowid] else { return nil }
                return MailMemoryReader.attachmentFile(emlxPath: file.path, versionDirectory: base, rowid: rowid,
                                                       attachmentID: attachmentID, name: name)?.size
            }
            return ["ok": true, "rows": rows, "capped": capped]
        } catch let failure as IndexFailure {
            return ["ok": false, "error": failure.rawValue]
        } catch {
            return ["ok": false, "error": IndexFailure.unreadable.rawValue]
        }
    }

    private func answerAttachment(_ request: Request) -> [String: Any] {
        guard let path = MailMemoryReader.envelopeIndexPath(mailRoot),
              let base = MailMemoryReader.versionDirectory(mailRoot) else {
            return ["ok": false, "error": "no_mail_index"]
        }
        // Same filters as the listing: a row Mail has since trashed is not read.
        let row: [String: Any]?
        do {
            row = try MailMemoryReader.attachmentRows(
                dbPath: path, terms: [], since: 0, until: 0, rfcIDs: [], limit: 1,
                only: (request.rowid, request.attachmentID)) { _, _, _ in nil }.0.first
        } catch {
            return ["ok": false, "error": IndexFailure.unreadable.rawValue]
        }
        guard let row, let name = row["name"] as? String else { return ["ok": false, "error": "not_found"] }
        var rebuilt = false
        if emlx[request.rowid] == nil { rebuilt = rebuildEmlx() }
        func locate() -> (path: String, size: Int)? {
            guard let file = emlx[request.rowid] else { return nil }
            return MailMemoryReader.attachmentFile(emlxPath: file.path, versionDirectory: base, rowid: request.rowid,
                                                   attachmentID: request.attachmentID, name: name)
        }
        var found = locate()
        if found == nil, !rebuilt, rebuildEmlx() { found = locate() }
        guard let found else { return ["ok": false, "error": "not_on_disk"] }
        guard found.size <= MailMemoryReader.maxAttachmentBytes else { return ["ok": false, "error": "too_large"] }
        guard let handle = FileHandle(forReadingAtPath: found.path) else { return ["ok": false, "error": "unreadable"] }
        defer { try? handle.close() }
        guard let data = try? handle.read(upToCount: MailMemoryReader.maxAttachmentBytes + 1) else {
            return ["ok": false, "error": "unreadable"]
        }
        guard data.count <= MailMemoryReader.maxAttachmentBytes else { return ["ok": false, "error": "too_large"] }
        return ["ok": true, "name": name, "size": data.count, "data_b64": data.base64EncodedString()]
    }

    /// Rebuilds the .emlx map unless it was built within `rebuildInterval`. True when it did.
    private func rebuildEmlx() -> Bool {
        if let built = emlxBuiltAt, Date().timeIntervalSince(built) < rebuildInterval { return false }
        emlx = MailMemoryReader.emlxFiles(mailRoot)
        emlxBuiltAt = Date()
        return true
    }

    /// Why an index read failed. The raw value is the `error` the backend sees.
    enum IndexFailure: String, Error {
        case unreadable
        case unsupportedSchema = "unsupported_schema"
    }

    // MARK: - Envelope Index

    /// The newest `V*` folder that has an Envelope Index (V10 is newer than V9).
    /// The index and the .emlx map must come from this same folder: ROWIDs are per database.
    static func versionDirectory(_ mailRoot: String) -> String? {
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: mailRoot) else { return nil }
        let versions = entries.compactMap { e -> (Int, String)? in
            guard e.hasPrefix("V"), let n = Int(e.dropFirst()) else { return nil }
            return (n, e)
        }
        for (_, e) in versions.sorted(by: { $0.0 > $1.0 }) {
            if FileManager.default.fileExists(atPath: "\(mailRoot)/\(e)/MailData/Envelope Index") {
                return "\(mailRoot)/\(e)"
            }
        }
        return nil
    }

    static func envelopeIndexPath(_ mailRoot: String) -> String? {
        versionDirectory(mailRoot).map { "\($0)/MailData/Envelope Index" }
    }

    /// True for a real Sent mailbox: the last path component is "Sent Messages", "Sent",
    /// "Sent Mail" or "Sent Items" (so "[Gmail]/Sent Mail" counts), never a name that merely
    /// contains "sent" like Consent or Presentations. The URL may be percent-encoded.
    static func isSentMailbox(url: String) -> Bool {
        let decoded = url.removingPercentEncoding ?? url
        guard let scheme = decoded.range(of: "://") else { return false }
        let rest = decoded[scheme.upperBound...]
        guard let slash = rest.firstIndex(of: "/") else { return false }
        let name = rest[rest.index(after: slash)...].split(separator: "/", omittingEmptySubsequences: true).last
        guard let name else { return false }
        return ["sent messages", "sent", "sent mail", "sent items"].contains(name.lowercased())
    }

    /// True for a mailbox nothing is read from: Trash, Deleted Messages/Items, Bin, Junk
    /// (Junk E-mail/Email), Spam or Drafts, by the last path component only. A name that
    /// merely contains one ("Spam filters", "Trashed ideas") is not hidden.
    static func isHiddenMailbox(url: String) -> Bool {
        let decoded = url.removingPercentEncoding ?? url
        guard let scheme = decoded.range(of: "://") else { return false }
        let rest = decoded[scheme.upperBound...]
        guard let slash = rest.firstIndex(of: "/") else { return false }
        let name = rest[rest.index(after: slash)...].split(separator: "/", omittingEmptySubsequences: true).last
        guard let name else { return false }
        return ["trash", "deleted messages", "deleted items", "bin", "junk", "junk e-mail", "junk email",
                "spam", "drafts"].contains(name.lowercased())
    }

    /// The saved file for one attachment:
    /// `<parent of the emlx's Messages folder>/Attachments/<rowid>/<id>/<name>`.
    /// Nil unless it is a regular file (never a symlink, at the end or anywhere on the path)
    /// whose real path is exactly that path and inside `versionDirectory`. Returns its size.
    /// `name` comes from the index, so a separator, `.`/`..` or NUL in it is refused.
    static func attachmentFile(emlxPath: String, versionDirectory: String, rowid: Int64,
                               attachmentID: String, name: String) -> (path: String, size: Int)? {
        guard !name.isEmpty, name != ".", name != "..", !name.contains("/"), !name.contains("\0"),
              attachmentID != ".", attachmentID != "..",
              attachmentID.range(of: "^[0-9A-Za-z._-]{1,40}$", options: .regularExpression) != nil else { return nil }
        let messages = (emlxPath as NSString).deletingLastPathComponent
        let parent = (messages as NSString).deletingLastPathComponent
        guard let realParent = realPath(parent), let realBase = realPath(versionDirectory),
              realParent == realBase || realParent.hasPrefix(realBase + "/") else { return nil }
        let expected = "\(realParent)/Attachments/\(rowid)/\(attachmentID)/\(name)"
        var info = stat()
        guard lstat(expected, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              let resolved = realPath(expected), resolved == expected else { return nil }
        return (expected, Int(info.st_size))
    }

    private static func realPath(_ path: String) -> String? {
        guard let resolved = realpath(path, nil) else { return nil }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    private enum Bind {
        case text(String)
        case double(Double)
        case int(Int64)
    }

    /// A LIKE pattern matching `term` anywhere, with backslash, % and _ taken literally
    /// (use with ESCAPE '\').
    static func likePattern(_ term: String) -> String {
        var out = "%"
        for ch in term {
            if ch == "\\" || ch == "%" || ch == "_" { out.append("\\") }
            out.append(ch)
        }
        return out + "%"
    }

    /// One row per attachment of mail that is not deleted and not in a hidden mailbox
    /// (`isHiddenMailbox`, also by Gmail label), newest first, at most `limit` (`capped`
    /// says there were more). Each `terms` entry must match the subject, sender address,
    /// sender name or attachment name (case-insensitive, escaped, bound). `since`/`until`
    /// are epoch seconds on date_received (0 = no bound); a non-empty `rfcIDs` keeps only
    /// those RFC Message-IDs. `onDisk` returns the saved file's size, or nil when Mail has
    /// not saved it. `only` limits the read to one attachment (the fetch re-check).
    /// Rows: rowid, ts, account, box (`sent`/`other`), subject, sender, sender_name,
    /// rfc_message_id, attachment_id, name, on_disk, size.
    static func attachmentRows(dbPath: String, terms: [String], since: Double, until: Double, rfcIDs: [String],
                               limit: Int, only: (rowid: Int64, attachmentID: String)? = nil,
                               onDisk: (Int64, String, String) -> Int?) throws -> ([[String: Any]], Bool) {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        var probe: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM sqlite_master", -1, &probe, nil) == SQLITE_OK,
              sqlite3_step(probe) == SQLITE_ROW else {
            sqlite3_finalize(probe)
            throw IndexFailure.unreadable
        }
        sqlite3_finalize(probe)
        guard exists(db, table: "attachments", column: "message"),
              exists(db, table: "attachments", column: "attachment_id"),
              exists(db, table: "attachments", column: "name"),
              exists(db, table: "messages", column: "date_received"),
              exists(db, table: "messages", column: "mailbox"),
              exists(db, table: "mailboxes", column: "url") else { throw IndexFailure.unsupportedSchema }
        var sentIDs: [Int64] = []
        var hiddenIDs: [Int64] = []
        var list: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT ROWID, url FROM mailboxes", -1, &list, nil) == SQLITE_OK else {
            sqlite3_finalize(list)
            throw IndexFailure.unreadable
        }
        while sqlite3_step(list) == SQLITE_ROW {
            let url = text(list, 1)
            if isSentMailbox(url: url) { sentIDs.append(sqlite3_column_int64(list, 0)) }
            if isHiddenMailbox(url: url) { hiddenIDs.append(sqlite3_column_int64(list, 0)) }
        }
        sqlite3_finalize(list)
        // Mailbox ROWIDs come from the index itself (integers), never from the request.
        let sentList = sentIDs.isEmpty ? "-1" : sentIDs.map(String.init).joined(separator: ",")
        let hiddenList = hiddenIDs.isEmpty ? "-1" : hiddenIDs.map(String.init).joined(separator: ",")
        let labels = exists(db, table: "labels", column: "mailbox_id") && exists(db, table: "labels", column: "message_id")
        let notDeleted = exists(db, table: "messages", column: "deleted") ? "IFNULL(m.deleted, 0) = 0" : "1"
        let hasSubject = exists(db, table: "messages", column: "subject") && exists(db, table: "subjects", column: "subject")
        let subject = hasSubject ? "IFNULL(s.subject, '')" : "''"
        let subjectJoin = hasSubject ? "LEFT JOIN subjects s ON m.subject = s.ROWID" : ""
        let hasAddress = exists(db, table: "messages", column: "sender") && exists(db, table: "addresses", column: "address")
        let address = hasAddress ? "IFNULL(a.address, '')" : "''"
        let hasComment = hasAddress && exists(db, table: "addresses", column: "comment")
        let comment = hasComment ? "IFNULL(a.comment, '')" : "''"
        let addressJoin = hasAddress ? "LEFT JOIN addresses a ON m.sender = a.ROWID" : ""
        let global = exists(db, table: "message_global_data", column: "message_id_header")
            && exists(db, table: "messages", column: "global_message_id")
        let rfcID = global ? "IFNULL(g.message_id_header, '')" : "''"
        let globalJoin = global ? "LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id" : ""
        let sentMember = labels
            ? "(m.mailbox IN (\(sentList)) OR m.ROWID IN (SELECT message_id FROM labels WHERE mailbox_id IN (\(sentList))))"
            : "m.mailbox IN (\(sentList))"
        let notHidden = labels
            ? "m.mailbox NOT IN (\(hiddenList)) AND m.ROWID NOT IN (SELECT message_id FROM labels WHERE mailbox_id IN (\(hiddenList)))"
            : "m.mailbox NOT IN (\(hiddenList))"
        var clauses = [notDeleted, notHidden]
        var binds: [Bind] = []
        for term in terms where !term.isEmpty {
            var fields = ["\(subject) LIKE ? ESCAPE '\\'", "IFNULL(att.name, '') LIKE ? ESCAPE '\\'"]
            if hasAddress { fields.append("\(address) LIKE ? ESCAPE '\\'") }
            if hasComment { fields.append("\(comment) LIKE ? ESCAPE '\\'") }
            clauses.append("(" + fields.joined(separator: " OR ") + ")")
            for _ in fields { binds.append(.text(likePattern(term))) }
        }
        if since > 0 { clauses.append("m.date_received > ?"); binds.append(.double(since)) }
        if until > 0 { clauses.append("m.date_received < ?"); binds.append(.double(until)) }
        if !rfcIDs.isEmpty {
            // Without the header table nothing can match an RFC Message-ID.
            guard global else { return ([], false) }
            clauses.append("g.message_id_header IN (" + rfcIDs.map { _ in "?" }.joined(separator: ",") + ")")
            for id in rfcIDs { binds.append(.text(id)) }
        }
        if let only {
            clauses.append("m.ROWID = ? AND att.attachment_id = ?")
            binds.append(.int(only.rowid))
            binds.append(.text(only.attachmentID))
        }
        binds.append(.int(Int64(limit + 1)))
        let sql = """
        SELECT m.ROWID, IFNULL(mb.url, ''), m.date_received, \(subject), \(address), \(comment), \(rfcID),
               IFNULL(att.attachment_id, ''), IFNULL(att.name, ''), \(sentMember)
        FROM attachments att
        JOIN messages m ON m.ROWID = att.message
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        \(globalJoin)
        \(subjectJoin)
        \(addressJoin)
        WHERE \(clauses.joined(separator: " AND "))
        ORDER BY m.date_received DESC, att.ROWID ASC
        LIMIT ?
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_finalize(stmt) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, bind) in binds.enumerated() {
            let slot = Int32(i + 1)
            switch bind {
            case .text(let value): sqlite3_bind_text(stmt, slot, value, -1, transient)
            case .double(let value): sqlite3_bind_double(stmt, slot, value)
            case .int(let value): sqlite3_bind_int64(stmt, slot, value)
            }
        }
        var rows: [[String: Any]] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            if rows.count == limit { return (rows, true) }
            let rowid = sqlite3_column_int64(stmt, 0)
            let attachmentID = text(stmt, 7)
            let name = text(stmt, 8)
            let size = onDisk(rowid, attachmentID, name)
            rows.append([
                "rowid": rowid,
                "ts": sqlite3_column_double(stmt, 2),
                "account": URL(string: text(stmt, 1))?.host ?? "",
                "box": sqlite3_column_int(stmt, 9) != 0 ? "sent" : "other",
                "subject": text(stmt, 3),
                "sender": text(stmt, 4),
                "sender_name": text(stmt, 5),
                "rfc_message_id": text(stmt, 6),
                "attachment_id": attachmentID,
                "name": name,
                "on_disk": size != nil,
                "size": size ?? 0,
            ])
        }
        guard step == SQLITE_DONE else { throw IndexFailure.unreadable }
        return (rows, false)
    }

    private static func exists(_ db: OpaquePointer?, table: String, column: String? = nil) -> Bool {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK else { return false }
        defer { sqlite3_finalize(stmt) }
        var any = false
        while sqlite3_step(stmt) == SQLITE_ROW {
            any = true
            if let column, let c = sqlite3_column_text(stmt, 1), String(cString: c) == column { return true }
        }
        return column == nil && any
    }

    private static func text(_ stmt: OpaquePointer?, _ i: Int32) -> String {
        guard let c = sqlite3_column_text(stmt, i) else { return "" }
        return String(cString: c)
    }

    /// Inbox and Sent messages received since `since`, newest first. A Gmail-over-IMAP
    /// account stores its mail in All Mail and marks Inbox/Sent with `labels` rows, so
    /// membership is the mailbox OR a label. A message in both counts as sent.
    /// Optional columns are probed and fall back to 0 or ''. Missing core tables throw
    /// `.unsupportedSchema`, anything else unreadable throws `.unreadable`.
    /// `only` limits the read to one message (the body check), whatever its age.
    static func indexRows(dbPath: String, since: Double, only: Int64? = nil) throws -> ([[String: Any]], Bool) {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        var probe: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM sqlite_master", -1, &probe, nil) == SQLITE_OK,
              sqlite3_step(probe) == SQLITE_ROW else {
            sqlite3_finalize(probe)
            throw IndexFailure.unreadable
        }
        sqlite3_finalize(probe)
        guard exists(db, table: "messages", column: "date_received"),
              exists(db, table: "messages", column: "mailbox"),
              exists(db, table: "mailboxes") else { throw IndexFailure.unsupportedSchema }
        let labels = exists(db, table: "labels", column: "mailbox_id") && exists(db, table: "labels", column: "message_id")
        let hasURL = exists(db, table: "mailboxes", column: "url")
        let bulkColumns: [String] = ["list_id_hash", "unsubscribe_type", "automated_conversation"]
        let bulkParts = bulkColumns.filter { exists(db, table: "messages", column: $0) }
            .map { (column: String) in "IFNULL(m.\(column), 0) != 0" }
        let bulk = bulkParts.isEmpty ? "0" : "(" + bulkParts.joined(separator: " OR ") + ")"
        let messageID = exists(db, table: "messages", column: "message_id") ? "m.message_id" : "''"
        let conversation = exists(db, table: "messages", column: "conversation_id") ? "IFNULL(m.conversation_id, 0)" : "0"
        let sent = exists(db, table: "messages", column: "date_sent") ? "IFNULL(m.date_sent, 0)" : "0"
        let notDeleted = exists(db, table: "messages", column: "deleted") ? "IFNULL(m.deleted, 0) = 0" : "1"
        let hasSubject = exists(db, table: "messages", column: "subject") && exists(db, table: "subjects", column: "subject")
        let subject = hasSubject ? "IFNULL(s.subject, '')" : "''"
        let subjectJoin = hasSubject ? "LEFT JOIN subjects s ON m.subject = s.ROWID" : ""
        let hasSender = exists(db, table: "messages", column: "sender")
        let hasAddress = hasSender && exists(db, table: "addresses", column: "address")
        let address = hasAddress ? "IFNULL(a.address, '')" : "''"
        let comment = hasAddress && exists(db, table: "addresses", column: "comment") ? "IFNULL(a.comment, '')" : "''"
        let addressJoin = hasAddress ? "LEFT JOIN addresses a ON m.sender = a.ROWID" : ""
        let mailboxURL = hasURL ? "IFNULL(mb.url, '')" : "''"
        // Sent mailboxes are classified in Swift (isSentMailbox), not by a SQL substring match.
        var sentIDs: [Int64] = []
        if hasURL {
            var list: OpaquePointer?
            guard sqlite3_prepare_v2(db, "SELECT ROWID, url FROM mailboxes", -1, &list, nil) == SQLITE_OK else {
                throw IndexFailure.unreadable
            }
            while sqlite3_step(list) == SQLITE_ROW {
                if isSentMailbox(url: text(list, 1)) { sentIDs.append(sqlite3_column_int64(list, 0)) }
            }
            sqlite3_finalize(list)
        }
        let sentList = sentIDs.isEmpty ? "-1" : sentIDs.map(String.init).joined(separator: ",")
        let boxes = hasURL
            ? "SELECT ROWID id, CASE WHEN ROWID IN (\(sentList)) THEN 'sent' ELSE 'inbox' END kind FROM mailboxes WHERE url LIKE '%/INBOX' OR ROWID IN (\(sentList))"
            : "SELECT ROWID id, 'inbox' kind FROM mailboxes WHERE 0"
        let labelMembers = labels ? "UNION SELECT l.message_id, box.kind FROM labels l JOIN box ON l.mailbox_id = box.id" : ""
        // Mail's on-device categories (macOS 15+): 0 Primary, 1 Transactions, 2 Updates,
        // 3 Promotions; -1 when Mail doesn't record them. The RFC Message-ID lives here too.
        let global = exists(db, table: "message_global_data", column: "model_category")
            && exists(db, table: "messages", column: "global_message_id")
        let hasHeader = global && exists(db, table: "message_global_data", column: "message_id_header")
        let category = global ? "IFNULL(g.model_category, -1)" : "-1"
        let rfcID = hasHeader ? "IFNULL(g.message_id_header, '')" : "''"
        let globalJoin = global ? "LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id" : ""
        let sql = """
        WITH box AS (\(boxes)),
        member AS (
          SELECT m.ROWID mid, box.kind kind FROM messages m JOIN box ON m.mailbox = box.id
          \(labelMembers))
        SELECT m.ROWID, \(mailboxURL), MAX(member.kind), m.date_received, \(sent), \(messageID),
               \(conversation), \(subject), \(address), \(comment), \(bulk),
               \(category), \(rfcID)
        FROM member
        JOIN messages m ON m.ROWID = member.mid
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        \(globalJoin)
        \(subjectJoin)
        \(addressJoin)
        WHERE \(notDeleted) AND m.date_received > \(since)\(only.map { " AND m.ROWID = \($0)" } ?? "")
        GROUP BY m.ROWID
        ORDER BY m.date_received DESC
        LIMIT \(maxIndexRows + 1)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { throw IndexFailure.unreadable }
        defer { sqlite3_finalize(stmt) }
        var rows: [[String: Any]] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            if rows.count == maxIndexRows { return (rows, true) }
            let url = text(stmt, 1)
            rows.append([
                "rowid": sqlite3_column_int64(stmt, 0),
                "account": URL(string: url)?.host ?? "",
                "box": text(stmt, 2),
                "ts": sqlite3_column_double(stmt, 3),
                "sent_ts": sqlite3_column_double(stmt, 4),
                "message_id": text(stmt, 5),
                "conversation_id": sqlite3_column_int64(stmt, 6),
                "subject": text(stmt, 7),
                "sender": text(stmt, 8),
                "sender_name": text(stmt, 9),
                "bulk": sqlite3_column_int(stmt, 10) != 0,
                "category": Int(sqlite3_column_int(stmt, 11)),
                "rfc_message_id": text(stmt, 12),
            ])
        }
        guard step == SQLITE_DONE else { throw IndexFailure.unreadable }
        return (rows, false)
    }

    /// The user's sent mail since `since`, newest first, at most `maxSentRows`: when it
    /// was sent, the account (its native ID, the mailbox URL host), the subject, the RFC
    /// Message-ID and up to `maxRecipients` To recipients ("Name <address>" or the bare
    /// address; Cc when there is no To), plus how many recipients there were in all.
    /// Sent membership is the mailbox or a Gmail label, as in `indexRows`. No bodies.
    static func sentRows(dbPath: String, since: Double) throws -> ([[String: Any]], Bool) {
        var db: OpaquePointer?
        guard sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)
        guard exists(db, table: "messages", column: "date_received"),
              exists(db, table: "messages", column: "mailbox"),
              exists(db, table: "mailboxes", column: "url") else { throw IndexFailure.unsupportedSchema }
        var sentIDs: [Int64] = []
        var list: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT ROWID, url FROM mailboxes", -1, &list, nil) == SQLITE_OK else {
            sqlite3_finalize(list)
            throw IndexFailure.unreadable
        }
        while sqlite3_step(list) == SQLITE_ROW {
            if isSentMailbox(url: text(list, 1)) { sentIDs.append(sqlite3_column_int64(list, 0)) }
        }
        sqlite3_finalize(list)
        guard !sentIDs.isEmpty else { return ([], false) }
        let sentList = sentIDs.map(String.init).joined(separator: ",")
        let labels = exists(db, table: "labels", column: "mailbox_id") && exists(db, table: "labels", column: "message_id")
        let membership = labels
            ? "(m.mailbox IN (\(sentList)) OR m.ROWID IN (SELECT message_id FROM labels WHERE mailbox_id IN (\(sentList))))"
            : "m.mailbox IN (\(sentList))"
        // date_sent is when the user sent it; date_received is when Mail filed the copy.
        let when = exists(db, table: "messages", column: "date_sent")
            ? "CASE WHEN IFNULL(m.date_sent, 0) > 0 THEN m.date_sent ELSE m.date_received END" : "m.date_received"
        let notDeleted = exists(db, table: "messages", column: "deleted") ? "IFNULL(m.deleted, 0) = 0" : "1"
        let hasSubject = exists(db, table: "messages", column: "subject") && exists(db, table: "subjects", column: "subject")
        let subject = hasSubject ? "IFNULL(s.subject, '')" : "''"
        let subjectJoin = hasSubject ? "LEFT JOIN subjects s ON m.subject = s.ROWID" : ""
        let global = exists(db, table: "message_global_data", column: "message_id_header")
            && exists(db, table: "messages", column: "global_message_id")
        let rfcID = global ? "IFNULL(g.message_id_header, '')" : "''"
        let globalJoin = global ? "LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id" : ""
        let sql = """
        SELECT m.ROWID, IFNULL(mb.url, ''), \(when) AS sent_at, \(subject), \(rfcID)
        FROM messages m
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        \(globalJoin)
        \(subjectJoin)
        WHERE \(membership) AND \(notDeleted) AND \(when) > \(since)
        ORDER BY sent_at DESC
        LIMIT \(maxSentRows + 1)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            throw IndexFailure.unreadable
        }
        defer { sqlite3_finalize(stmt) }
        // Recipients are optional: an index without the table still lists what was sent.
        var people: OpaquePointer?
        if exists(db, table: "recipients", column: "message") && exists(db, table: "addresses", column: "comment") {
            let recipientSQL = """
            SELECT a.address, a.comment, IFNULL(r.type, 0) FROM recipients r
            JOIN addresses a ON a.ROWID = r.address
            WHERE r.message = ? ORDER BY IFNULL(r.type, 0), IFNULL(r.position, 0)
            """
            if sqlite3_prepare_v2(db, recipientSQL, -1, &people, nil) != SQLITE_OK {
                sqlite3_finalize(people)
                people = nil
            }
        }
        defer { sqlite3_finalize(people) }
        var rows: [[String: Any]] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            if rows.count == maxSentRows { return (rows, true) }
            let rowid = sqlite3_column_int64(stmt, 0)
            let (to, total) = recipients(people, message: rowid)
            rows.append([
                "rowid": rowid,
                "account": URL(string: text(stmt, 1))?.host ?? "",
                "ts": sqlite3_column_double(stmt, 2),
                "subject": text(stmt, 3),
                "rfc_message_id": text(stmt, 4),
                "to": to,
                "recipients": total,
            ])
        }
        guard step == SQLITE_DONE else { throw IndexFailure.unreadable }
        return (rows, false)
    }

    /// Up to `maxRecipients` of one message's To recipients (Cc, then Bcc, when it has no
    /// To), each "Name <address>" or the bare address, and how many recipients it had.
    private static func recipients(_ stmt: OpaquePointer?, message: Int64) -> ([String], Int) {
        guard let stmt else { return ([], 0) }
        sqlite3_reset(stmt)
        sqlite3_bind_int64(stmt, 1, message)
        var byType: [Int32: [String]] = [:]
        var total = 0
        while sqlite3_step(stmt) == SQLITE_ROW {
            total += 1
            let address = text(stmt, 0)
            let name = text(stmt, 1).trimmingCharacters(in: .whitespaces)
            guard !address.isEmpty else { continue }
            byType[sqlite3_column_int(stmt, 2), default: []].append(name.isEmpty ? address : "\(name) <\(address)>")
        }
        guard let first = byType.keys.min() else { return ([], total) }
        return (Array(byType[first]!.prefix(maxRecipients)), total)
    }

    // MARK: - .emlx files

    /// ROWID → file for every message Mail keeps on disk. A `.partial.emlx` had its
    /// attachments stripped; a full file wins when both exist.
    /// Only the `V*` folder whose Envelope Index is read, so rowids can't collide across versions.
    static func emlxFiles(_ mailRoot: String) -> [Int64: (path: String, partial: Bool)] {
        var out: [Int64: (path: String, partial: Bool)] = [:]
        guard let base = versionDirectory(mailRoot),
              let walker = FileManager.default.enumerator(atPath: base) else { return out }
        while let rel = walker.nextObject() as? String {
            guard rel.hasSuffix(".emlx"), rel.contains("/Messages/") else { continue }
            let name = (rel as NSString).lastPathComponent
            guard let id = Int64(name.prefix { $0.isNumber }), id > 0 else { continue }
            let partial = name.hasSuffix(".partial.emlx")
            if let have = out[id], !have.partial { continue }
            out[id] = ("\(base)/\(rel)", partial)
        }
        return out
    }

    /// The RFC 822 bytes of an .emlx: a decimal byte count on the first line, then
    /// that many bytes, then Mail's plist trailer (dropped). Capped at `maxBodyBytes`.
    static func message(at path: String) -> (Data, Bool)? {
        guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
        defer { try? handle.close() }
        guard let head = try? handle.read(upToCount: 32), let newline = head.firstIndex(of: 0x0A),
              let count = Int(String(decoding: head[head.startIndex..<newline], as: UTF8.self)
                                .trimmingCharacters(in: .whitespaces)), count > 0 else { return nil }
        let start = UInt64(newline - head.startIndex + 1)
        let wanted = min(count, maxBodyBytes)
        guard (try? handle.seek(toOffset: start)) != nil,
              let data = try? handle.read(upToCount: wanted), !data.isEmpty else { return nil }
        return (data, count > maxBodyBytes)
    }

    // MARK: - Messages attachments (kinds `message_attachments` and `message_attachment`)

    /// Why a Messages read failed. The raw value is the `error` the backend sees.
    enum MessagesFailure: String, Error {
        case noDatabase = "no_messages_db"
        case unreadable
        case unsupportedSchema = "unsupported_schema"
    }

    /// One file texted in Messages, as chat.db describes it. `filename` is chat.db's own path
    /// text; it never leaves this class.
    struct MessageAttachment {
        let id: Int64
        let ts: Double
        let chat: String
        let sender: String
        let fromMe: Bool
        let name: String
        let mime: String
        let size: Int64
        let filename: String
    }

    private static let appleEpochOffset: Double = 978_307_200

    /// A handle filter value: 1...100 characters, no control characters.
    static func isValidHandle(_ value: String) -> Bool {
        guard (1...100).contains(value.count) else { return false }
        return !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }

    /// Unix seconds as Apple-epoch nanoseconds (what `message.date` holds), clamped so an absurd
    /// request can't trap.
    static func appleNanoseconds(_ unix: Double) -> Int64 {
        let ns = (unix - appleEpochOffset) * 1_000_000_000
        if ns >= 9.2e18 { return Int64.max }
        if ns <= -9.2e18 { return Int64.min }
        return Int64(ns)
    }

    /// A chat's label, built like `MessagesReader.label` (that file can't be linked into the
    /// standalone regression build, so the small function is copied): a named group is
    /// `Group "name"`, an unnamed one lists its members, a 1:1 chat is the other handle.
    static func messageChatLabel(chatName: String?, chatIdentifier: String?,
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

    /// The file chat.db names for an attachment, or nil. `filename` comes from the database only.
    /// A leading `~/Library/Messages/Attachments/` is placed on the real `root`; any other `~/`
    /// expands to the home folder. The result must lie textually inside `realpath(root)`, be a
    /// regular file (`lstat`), and equal its own `realpath`, so no symlink exists anywhere below the
    /// root. Returns the path and its size.
    static func messageAttachmentFile(filename: String, root: String) -> (path: String, size: Int)? {
        guard !filename.isEmpty, !filename.contains("\0"), let realRoot = realPath(root), realRoot != "/" else { return nil }
        var path = filename
        let conventional = "~/Library/Messages/Attachments/"
        if path.hasPrefix(conventional) {
            path = realRoot + "/" + String(path.dropFirst(conventional.count))
        } else if path.hasPrefix("~/") {
            path = NSHomeDirectory() + String(path.dropFirst(1))
        }
        guard path.hasPrefix(realRoot + "/") else { return nil }
        var info = stat()
        guard lstat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
              let resolved = realPath(path), resolved == path else { return nil }
        return (path, Int(info.st_size))
    }

    private static func openMessages(_ path: String) throws -> OpaquePointer? {
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            throw MessagesFailure.noDatabase
        }
        sqlite3_busy_timeout(db, 2000)
        var probe: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT COUNT(*) FROM sqlite_master", -1, &probe, nil) == SQLITE_OK,
              sqlite3_step(probe) == SQLITE_ROW else {
            sqlite3_finalize(probe)
            sqlite3_close(db)
            throw MessagesFailure.unreadable
        }
        sqlite3_finalize(probe)
        return db
    }

    /// One row per attachment of a real message, newest first, at most `limit` (`capped` says there
    /// were more). Left out: link previews (`.pluginpayloadattachment`), stickers, hidden
    /// attachments, tapback rows, rows without a file name. Every term must be in the chat's name,
    /// identifier or a member's handle, the sender's handle, or the file's name; a non-empty
    /// `handles` also requires a sender (never the user) whose handle equals one of them. `since`
    /// and `until` are unix seconds on the message date (0 = no bound, bounds exclusive). `only`
    /// limits the read to one attachment ROWID with the same filters.
    static func messageAttachmentRows(dbPath: String, terms: [String], handles: [String], since: Double,
                                      until: Double, limit: Int, only: Int64? = nil) throws -> ([MessageAttachment], Bool) {
        let db = try openMessages(dbPath)
        defer { sqlite3_close(db) }
        guard exists(db, table: "attachment", column: "filename"),
              exists(db, table: "message_attachment_join", column: "message_id"),
              exists(db, table: "message_attachment_join", column: "attachment_id"),
              exists(db, table: "message", column: "date"),
              exists(db, table: "message", column: "handle_id"),
              exists(db, table: "message", column: "is_from_me"),
              exists(db, table: "handle", column: "id") else { throw MessagesFailure.unsupportedSchema }
        let hasTransfer = exists(db, table: "attachment", column: "transfer_name")
        let transfer = hasTransfer ? "IFNULL(a.transfer_name, '')" : "''"
        let nameExpr = hasTransfer ? "IFNULL(NULLIF(a.transfer_name, ''), a.filename)" : "a.filename"
        let mime = exists(db, table: "attachment", column: "mime_type") ? "IFNULL(a.mime_type, '')" : "''"
        let total = exists(db, table: "attachment", column: "total_bytes") ? "IFNULL(a.total_bytes, 0)" : "0"
        var clauses = ["IFNULL(a.filename, '') != ''",
                       "LOWER(IFNULL(a.filename, '')) NOT LIKE '%.pluginpayloadattachment'"]
        if hasTransfer { clauses.append("LOWER(IFNULL(a.transfer_name, '')) NOT LIKE '%.pluginpayloadattachment'") }
        if exists(db, table: "attachment", column: "is_sticker") { clauses.append("IFNULL(a.is_sticker, 0) = 0") }
        if exists(db, table: "attachment", column: "hide_attachment") { clauses.append("IFNULL(a.hide_attachment, 0) = 0") }
        if exists(db, table: "message", column: "associated_message_type") {
            clauses.append("IFNULL(m.associated_message_type, 0) = 0")
        }
        let hasChat = exists(db, table: "chat_message_join", column: "message_id")
            && exists(db, table: "chat_message_join", column: "chat_id") && exists(db, table: "chat")
        let displayName = hasChat && exists(db, table: "chat", column: "display_name") ? "IFNULL(c.display_name, '')" : "''"
        let identifier = hasChat && exists(db, table: "chat", column: "chat_identifier") ? "IFNULL(c.chat_identifier, '')" : "''"
        let hasMembers = hasChat && exists(db, table: "chat_handle_join", column: "chat_id")
            && exists(db, table: "chat_handle_join", column: "handle_id")
        let chatJoin = hasChat
            ? "LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID LEFT JOIN chat c ON c.ROWID = cmj.chat_id" : ""
        let chatID = hasChat ? "IFNULL(MIN(c.ROWID), 0)" : "0"
        // The sender is the other person; a message the user sent has none.
        let sender = "CASE WHEN IFNULL(m.is_from_me, 0) != 0 THEN '' ELSE IFNULL(h.id, '') END"
        var binds: [Bind] = []
        for term in terms where !term.isEmpty {
            var fields = ["\(sender) LIKE ? ESCAPE '\\'", "\(nameExpr) LIKE ? ESCAPE '\\'"]
            if hasChat {
                fields.append("\(displayName) LIKE ? ESCAPE '\\'")
                fields.append("CASE WHEN \(identifier) LIKE 'chat%' THEN '' ELSE \(identifier) END LIKE ? ESCAPE '\\'")
            }
            if hasMembers {
                fields.append("""
                EXISTS (SELECT 1 FROM chat_handle_join chj JOIN handle hh ON hh.ROWID = chj.handle_id \
                WHERE chj.chat_id = c.ROWID AND hh.id LIKE ? ESCAPE '\\')
                """)
            }
            clauses.append("(" + fields.joined(separator: " OR ") + ")")
            for _ in fields { binds.append(.text(likePattern(term))) }
        }
        let wanted = handles.filter { !$0.isEmpty }
        if !handles.isEmpty {
            guard !wanted.isEmpty else { return ([], false) }
            clauses.append("(IFNULL(m.is_from_me, 0) = 0 AND ("
                           + wanted.map { _ in "h.id = ? COLLATE NOCASE" }.joined(separator: " OR ") + "))")
            for handle in wanted { binds.append(.text(handle)) }
        }
        if since > 0 { clauses.append("m.date > ?"); binds.append(.int(appleNanoseconds(since))) }
        if until > 0 { clauses.append("m.date < ?"); binds.append(.int(appleNanoseconds(until))) }
        if let only { clauses.append("a.ROWID = ?"); binds.append(.int(only)) }
        binds.append(.int(Int64(limit + 1)))
        let sql = """
        SELECT a.ROWID, IFNULL(m.date, 0), IFNULL(m.is_from_me, 0), \(sender), \(displayName), \(identifier),
               \(chatID), IFNULL(a.filename, ''), \(transfer), \(mime), \(total)
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        JOIN message m ON m.ROWID = maj.message_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        \(chatJoin)
        WHERE \(clauses.joined(separator: " AND "))
        GROUP BY a.ROWID
        ORDER BY m.date DESC, a.ROWID ASC
        LIMIT ?
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            throw MessagesFailure.unreadable
        }
        defer { sqlite3_finalize(stmt) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, bind) in binds.enumerated() {
            let slot = Int32(i + 1)
            switch bind {
            case .text(let value): sqlite3_bind_text(stmt, slot, value, -1, transient)
            case .double(let value): sqlite3_bind_double(stmt, slot, value)
            case .int(let value): sqlite3_bind_int64(stmt, slot, value)
            }
        }
        var people: OpaquePointer?
        if hasMembers {
            let memberSQL = "SELECT h.id FROM chat_handle_join chj JOIN handle h ON h.ROWID = chj.handle_id WHERE chj.chat_id = ?"
            if sqlite3_prepare_v2(db, memberSQL, -1, &people, nil) != SQLITE_OK {
                sqlite3_finalize(people)
                people = nil
            }
        }
        defer { sqlite3_finalize(people) }
        var members: [Int64: [String]] = [:]
        func membersOf(_ chat: Int64) -> [String] {
            guard chat != 0, let people else { return [] }
            if let known = members[chat] { return known }
            sqlite3_reset(people)
            sqlite3_bind_int64(people, 1, chat)
            var out: [String] = []
            while sqlite3_step(people) == SQLITE_ROW { out.append(text(people, 0)) }
            members[chat] = out
            return out
        }
        var rows: [MessageAttachment] = []
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            if rows.count == limit { return (rows, true) }
            let dateNs = sqlite3_column_int64(stmt, 1)
            let fromMe = sqlite3_column_int(stmt, 2) != 0
            let from = text(stmt, 3)
            let filename = text(stmt, 7)
            let transferName = text(stmt, 8)
            let name = transferName.isEmpty ? (filename as NSString).lastPathComponent : transferName
            let label = messageChatLabel(chatName: text(stmt, 4), chatIdentifier: text(stmt, 5),
                                         members: membersOf(sqlite3_column_int64(stmt, 6)),
                                         fallback: fromMe ? "Me" : (from.isEmpty ? "Messages" : from))
            rows.append(MessageAttachment(
                id: sqlite3_column_int64(stmt, 0),
                ts: dateNs > 0 ? Double(dateNs) / 1_000_000_000 + appleEpochOffset : 0,
                chat: String(label.prefix(120)), sender: from, fromMe: fromMe, name: name,
                mime: text(stmt, 9), size: max(0, sqlite3_column_int64(stmt, 10)), filename: filename))
        }
        guard step == SQLITE_DONE else { throw MessagesFailure.unreadable }
        return (rows, false)
    }

    private func answerMessageAttachments(_ request: Request) -> [String: Any] {
        do {
            let (found, capped) = try MailMemoryReader.messageAttachmentRows(
                dbPath: messagesDB, terms: request.terms, handles: request.handles, since: request.since,
                until: request.until, limit: request.limit)
            let rows: [[String: Any]] = found.map { row in
                let onDisk = MailMemoryReader.messageAttachmentFile(filename: row.filename,
                                                                    root: messagesAttachmentsRoot) != nil
                return ["id": String(row.id), "ts": row.ts, "chat": row.chat, "sender": row.sender,
                        "from_me": row.fromMe, "name": row.name, "mime": row.mime, "size": row.size,
                        "on_disk": onDisk]
            }
            return ["ok": true, "rows": rows, "capped": capped]
        } catch let failure as MessagesFailure {
            return ["ok": false, "error": failure.rawValue]
        } catch {
            return ["ok": false, "error": MessagesFailure.unreadable.rawValue]
        }
    }

    private func answerMessageAttachment(_ request: Request) -> [String: Any] {
        guard let id = Int64(request.attachmentID), id > 0 else { return ["ok": false, "error": "not_found"] }
        // Same filters as the listing: a hidden, sticker, preview or tapback row is not found.
        let row: MessageAttachment?
        do {
            row = try MailMemoryReader.messageAttachmentRows(
                dbPath: messagesDB, terms: [], handles: [], since: 0, until: 0, limit: 1, only: id).0.first
        } catch let failure as MessagesFailure {
            return ["ok": false, "error": failure.rawValue]
        } catch {
            return ["ok": false, "error": MessagesFailure.unreadable.rawValue]
        }
        guard let row else { return ["ok": false, "error": "not_found"] }
        guard let found = MailMemoryReader.messageAttachmentFile(filename: row.filename, root: messagesAttachmentsRoot) else {
            return ["ok": false, "error": "not_on_disk"]
        }
        let cap = maxMessageFileBytes
        guard found.size <= cap else { return ["ok": false, "error": "too_large"] }
        // No symlink at the end, and the opened file is checked again (it may have changed since lstat).
        let fd = open(found.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return ["ok": false, "error": "unreadable"] }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG else { return ["ok": false, "error": "not_on_disk"] }
        guard Int(info.st_size) <= cap else { return ["ok": false, "error": "too_large"] }
        guard let data = try? handle.read(upToCount: cap + 1) else { return ["ok": false, "error": "unreadable"] }
        guard data.count <= cap else { return ["ok": false, "error": "too_large"] }
        return ["ok": true, "name": row.name, "size": data.count, "data_b64": data.base64EncodedString()]
    }

    // MARK: - Transport

    static func poster(to endpoint: URL) -> ([String: Any]) -> Void {
        { reply in
            var request = URLRequest(url: endpoint)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: reply)
            URLSession.shared.dataTask(with: request).resume()
        }
    }
}
