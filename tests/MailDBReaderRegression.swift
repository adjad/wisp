// Compile with the production reader, then run. Only temporary SQLite fixtures
// are accessed; this test never opens Mail or the user's Mail database.
// swiftc app/Sources/WispApp/MailDBReader.swift tests/MailDBReaderRegression.swift \
//   -lsqlite3 -o /tmp/<test-dir>/mail-db-regression
import Foundation
import SQLite3

// Same-module in-memory preferences: cache learning never touches user defaults.
final class UserDefaults {
    static let standard = UserDefaults()
    static let argumentDomain = "fixture-arguments"
    private var values: [String: Any] = [:]
    private var arguments: [String: Any] = [:]
    func dictionary(forKey key: String) -> [String: Any]? {
        (arguments[key] ?? values[key]) as? [String: Any]
    }
    func set(_ value: Any?, forKey key: String) { values[key] = value }
    func setVolatileDomain(_ domain: [String: Any], forName name: String) { arguments = domain }
    func volatileDomain(forName name: String) -> [String: Any] { arguments }
}

@main
enum MailDBReaderRegression {
    static func main() throws {
        if CommandLine.arguments.contains("zero-inventory") {
            precondition(AccountLabelCache.parse("") == [:],
                         "Successful zero-account output must be known empty, not unavailable")
            return
        }
        // Intel's closed-Mail index post is skipped only while the index is unchanged.
        let throttle = MailIndexPostThrottle(enabled: true, maxSkip: 1800)
        let start = Date()
        let same = ["i|1|2", "i-wal|0|-1"]
        precondition(!throttle.shouldSkip(same, now: start), "The first index read always posts")
        throttle.recordPost(same, now: start)
        precondition(throttle.shouldSkip(same, now: start.addingTimeInterval(300)), "An unchanged index skips")
        precondition(!throttle.shouldSkip(["i|3|2", "i-wal|0|-1"], now: start.addingTimeInterval(300)),
                     "A changed index posts")
        precondition(!throttle.shouldSkip(same, now: start.addingTimeInterval(1801)), "Still posts every 30 minutes")
        throttle.reset()
        precondition(!throttle.shouldSkip(same, now: start.addingTimeInterval(300)),
                     "After another source posted, the index posts again")
        precondition(!throttle.shouldSkip([], now: start), "No index never skips")
        let off = MailIndexPostThrottle(enabled: false)
        off.recordPost(same, now: start)
        precondition(!off.shouldSkip(same, now: start), "Disabled (Apple silicon) never skips")
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
        if let mode = CommandLine.arguments.dropFirst().first, ["identity", "scope"].contains(mode) {
            sql("CREATE TABLE mailboxes (url TEXT)")
            sql("CREATE TABLE messages (date_received REAL, read INTEGER, subject INTEGER, sender INTEGER, mailbox INTEGER)")
            sql("CREATE TABLE subjects (subject TEXT)")
            sql("CREATE TABLE addresses (address TEXT, comment TEXT)")
            sql("INSERT INTO mailboxes VALUES ('imap://UUID-W/INBOX'), ('imap://UUID-H/INBOX')")
            sql("INSERT INTO subjects VALUES ('Sent only')")
            sql("INSERT INTO addresses VALUES ('me@example.test', 'Me')")
            UserDefaults.standard.set(["UUID-W": "Home", "UUID-H": "Work"], forKey: "wisp.mailAccountLabels")
            if mode == "identity" {
                sql("INSERT INTO messages VALUES (\(Date().timeIntervalSince1970), 1, 1, 1, 1)")
                precondition(!reader.readHistory()!.isTrusted(forAccounts: ["UUID-H": "Home", "UUID-W": "Work"]),
                             "Positional labels must never authenticate account identity")
            } else {
                sql("INSERT INTO mailboxes VALUES ('imap://UUID-W/All%20Mail')")
                sql("INSERT INTO messages VALUES (\(Date().timeIntervalSince1970), 1, 1, 1, 3)")
                precondition(!reader.readHistory()!.history.contains("Sent only"),
                             "All Mail sent-only rows must never become Inbox history")
            }
            return
        }
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
        let fields = result!.headers.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\u{01}")
        precondition(fields.count == 10 && fields[0] == "H2", "Current header wire must be H2")
        precondition(fields[4] == "fixture-account", "Native account ID must survive")
        precondition(fields[5] == "Fixture sender" && fields[6] == "sender@example.test",
                     "Display name and sender address must remain separate")
        precondition(fields[7].isEmpty, "Missing Message-ID must remain unknown")
        precondition(fields[9] == "db:1", "Stable native row identity must survive missing Message-ID")
        sql("ALTER TABLE messages ADD COLUMN message_id TEXT")
        sql("UPDATE messages SET message_id = '<fixture-id@example.test>'")
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: path)
        let withID = reader.readHeadersAndHistory()!.headers
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\u{01}")
        precondition(withID[7] == "<fixture-id@example.test>",
                     "Available Message-ID must survive the header scan")
        precondition(reader.readHeadersAndHistory()?.headers == withID.joined(separator: "\u{01}") + "\n",
                     "Repeated reads of an unchanged index must still complete")
        sql("INSERT INTO mailboxes VALUES ('imap://other-account/INBOX')")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "VALUES (\(Date().timeIntervalSince1970 - 3600), 0, 1, 1, 2, '<newer-id@example.test>')")
        try FileManager.default.setAttributes([.modificationDate: oldDate], ofItemAtPath: path)
        let sorted = reader.readHeadersAndHistory()!.headers
            .split(separator: "\n").map { String($0).components(separatedBy: "\u{01}") }
        precondition(sorted.count == 2 && sorted[0][4] == "other-account" && sorted[1][4] == "fixture-account",
                     "H2 headers from multiple accounts must be globally newest-first")
        let modified = try FileManager.default.attributesOfItem(atPath: path)[.modificationDate] as! Date
        precondition(abs(modified.timeIntervalSince(oldDate)) < 1, "Reads must not modify Mail's index")
        let oldArguments = UserDefaults.standard.volatileDomain(forName: UserDefaults.argumentDomain)
        UserDefaults.standard.setVolatileDomain([
            "wisp.mailAccountLabels.identity.v1": ["fixture-account": "Same", "other-account": "Same"]
        ], forName: UserDefaults.argumentDomain)
        defer { UserDefaults.standard.setVolatileDomain(oldArguments, forName: UserDefaults.argumentDomain) }
        sql("DELETE FROM messages")
        let base = Date().timeIntervalSince1970
        for mailbox in 1...2 {
            sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<150) " +
                "INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
                "SELECT \(base) - x*2 - \(mailbox), 0, 1, 1, \(mailbox), 'id-'||\(mailbox)||'-'||x FROM seq")
        }
        let sameLabelLines = reader.readHeadersAndHistory()!.headers
            .split(separator: "\n").map(String.init)
        let sameLabelFields = sameLabelLines.map { $0.components(separatedBy: "\u{01}") }
        precondition(sameLabelFields.count == 300 && sameLabelFields.allSatisfy { $0[0] == "H2" },
                     "Distinct native accounts sharing one display label each keep their 150 headers")
        precondition(Set(sameLabelFields.map { $0[4] }) == Set(["fixture-account", "other-account"]),
                     "Identical display labels must not erase native account identity")
        sql("DELETE FROM messages")
        sql("INSERT INTO subjects VALUES ('Bad' || char(10) || 'subject')")
        sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<199) " +
            "INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "SELECT \(base) - x, 0, 1, 1, 1, 'good-'||x FROM seq")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "VALUES (\(base) - 200, 0, 2, 1, 1, 'bad')")
        let malformedLines = reader.readHeadersAndHistory()!.headers.split(separator: "\n")
            .map { String($0).components(separatedBy: "\u{01}") }
        precondition(malformedLines.filter { $0[0] == "H2" }.count == 199,
                     "Malformed header must be omitted from display rows")
        precondition(malformedLines.contains { $0[0] == "C2" && $0[3] == "200" && $0[4] == "1" && $0[5] == "1" },
                     "Native attempted/skipped/cap marker must survive one malformed header")
        let historyFields = reader.readHeadersAndHistory()!.history.split(separator: "\n")
            .map { String($0).components(separatedBy: "\u{01}") }
        precondition(historyFields.filter { $0[0] == "H2" }.count == 199 &&
                     historyFields.contains { $0[0] == "C2" && $0[2] == "fixture-account" &&
                         $0[3] == "200" && $0[4] == "1" && $0[5] == "0" },
                     "History must report its skipped header without inventing a history cap")
        sql("BEGIN EXCLUSIVE")
        precondition(reader.readHeadersAndHistory() == nil, "A locked read is failure, not empty-ready")
        sql("ROLLBACK")
        sql("DELETE FROM messages")
        let empty = reader.readHeadersAndHistory()
        precondition(empty?.headers == "" && empty?.history == "", "An empty scan must clear both caches")
        precondition(MailDBReader(indexPath: root.appendingPathComponent("missing").path)
            .readHeadersAndHistory() == nil, "An unreadable index must fail")
        precondition(MailDBReader(indexPath: root.appendingPathComponent("missing").path)
            .readHistory() == nil, "An unreadable index must fail the history-only read too")
        try closedReadFailureChecks(root: root)
        readPlanChecks()

        // History-only entry point (used while Mail is running) and the
        // account-label trust rule that gates it.
        func labels(_ map: [String: String]) {
            UserDefaults.standard.setVolatileDomain(["wisp.mailAccountLabels.identity.v1": map],
                                                    forName: UserDefaults.argumentDomain)
        }
        for mailbox in 1...2 {
            sql("INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
                "VALUES (\(base) - \(mailbox * 60), 1, 1, 1, \(mailbox), 'h-\(mailbox)')")
        }
        labels(["fixture-account": "Work", "other-account": "Home"])
        let history = reader.readHistory()!
        var wireFixtures = ["verified_history": history.history]
        precondition(history.history == reader.readHeadersAndHistory()!.history,
                     "History-only and combined reads must render identical history")
        precondition(history.indexAccountCount == 2 && !history.hasUnresolvedLabel,
                     "Both index accounts are counted and resolved")
        precondition(history.labelsByAccountID == ["fixture-account": "Work", "other-account": "Home"],
                     "Every native account carries its learned label")
        precondition(history.isTrusted(forAccounts: ["fixture-account": "Work", "other-account": "Home"]),
                     "Exact current native IDs and labels are trusted")
        precondition(!history.isTrusted(forAccounts: ["fixture-account": "Work"]),
                     "Fewer Mail accounts than index accounts must not be trusted")
        precondition(!history.isTrusted(forAccounts: ["fixture-account": "Work", "other-account": "Home", "school-account": "School"]),
                     "More Mail accounts than index accounts must not be trusted")
        precondition(!history.isTrusted(forAccounts: ["fixture-account": "Work", "other-account": "School"]),
                     "A stale learned label that is no longer a current account must not be trusted")
        precondition(!history.isTrusted(forAccounts: [:]),
                     "No account enumeration must not be trusted")
        labels(["fixture-account": "Work"])
        let partial = reader.readHistory()!
        precondition(partial.hasUnresolvedLabel && !partial.isTrusted(forAccounts: ["fixture-account": "Work", "other-account": "Home"]),
                     "A fallback 'Account N' label must not be trusted")
        precondition(partial.history.contains("Account 2"),
                     "The unresolved account keeps its positional fallback label")
        labels(["fixture-account": "Same", "other-account": "Same"])
        precondition(!reader.readHistory()!.isTrusted(forAccounts: ["fixture-account": "Same", "other-account": "Other"]),
                     "Two native accounts sharing one label must not be trusted")
        labels(["fixture-account": "Work", "other-account": "Home"])
        precondition(reader.orderedAccountUUIDs() == ["fixture-account", "other-account"],
                     "Cosmetic fallback order is mailbox-creation order")

        // The per-account history ceiling matches MailReader.historyCap.
        sql("DELETE FROM messages")
        sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<\(MailHistoryWindow.perAccount + 1)) " +
            "INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "SELECT \(base) - x, 1, 1, 1, 1, 'cap-'||x FROM seq")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "VALUES (\(base) - 5, 1, 1, 1, 2, 'other-one')")
        let capped = reader.readHistory()!.history.split(separator: "\n")
            .map { String($0).components(separatedBy: "\u{01}") }
        precondition(capped.filter { $0[0] == "H2" && $0[4] == "fixture-account" }.count == MailHistoryWindow.perAccount,
                     "History keeps at most historyCap rows per account")
        precondition(capped.contains { $0 == ["C2", "Work", "fixture-account", "\(MailHistoryWindow.perAccount)", "0", "0"] } &&
                     capped.contains { $0 == ["C2", "Work", "fixture-account", "0", "0", "1"] },
                     "A capped account reports attempted rows and a separate cap marker")
        precondition(capped.contains { $0 == ["C2", "Home", "other-account", "1", "0", "0"] } &&
                     !capped.contains { $0 == ["C2", "Home", "other-account", "0", "0", "1"] },
                     "One account's cap must not mark another account capped")
        precondition(!capped.contains { $0[0] == "C2" && $0[2] == "*" },
                     "Under the total-row cap there is no global cap marker")
        let cappedHeaders = reader.readHeadersAndHistory()!.headers.split(separator: "\n")
            .map { String($0).components(separatedBy: "\u{01}") }
        precondition(cappedHeaders.filter { $0[0] == "H2" && $0[4] == "fixture-account" }.count == 200,
                     "The history ceiling does not change the recent header limit")
        // Correct associations remain correct when enumeration order changes.
        UserDefaults.standard.setVolatileDomain([:], forName: UserDefaults.argumentDomain)
        let workHome = ["fixture-account": "Work", "other-account": "Home"]
        let reversed = AccountLabelCache.parse("other-account\u{01}Home\nfixture-account\u{01}Work\n")!
        precondition(AccountLabelCache.enumeration("") == .accounts([:]),
                     "A successful zero-enabled-account script is a known empty inventory")
        precondition(AccountLabelCache.enumeration(nil) == .unavailable,
                     "Script failure remains unavailable rather than empty")
        for malformed in ["\n", "\r\n", " ", "\t", "id", "id\u{01}", "\u{01}Work",
                          "id\u{01}Work\nid\u{01}Home\n"] {
            precondition(AccountLabelCache.enumeration(malformed) == .unavailable,
                         "Blank, malformed, or duplicate-ID output cannot clear accounts")
        }
        precondition(AccountLabelCache.enumeration("id-1\u{01}Work\nid-2\u{01}work\n") == .accounts(["id-1": "Work", "id-2": "work"]),
                     "Stable IDs preserve case-colliding labels")
        let sharedLabels = ["fixture-account": "Work", "other-account": "Work"]
        AccountLabelCache.learn(accounts: sharedLabels)
        precondition(reader.readHistory()!.isTrusted(forAccounts: sharedLabels),
                     "Same-label index trust still requires each exact native ID association")
        AccountLabelCache.learn(accounts: reversed)
        precondition(reader.readHistory()!.isTrusted(forAccounts: workHome))
        precondition(!reader.readHistory()!.isTrusted(forAccounts:
            ["fixture-account": "Home", "other-account": "Work"]), "Swapped names must fail exact identity proof")
        precondition(!reader.readHistory()!.isTrusted(forAccounts:
            ["new-account": "Work", "other-account": "Home"]), "Unknown native IDs must fall back")
        for unsafe in ["\u{01}Work\n", "fixture-account\u{01}\n",
                       "fixture-account\u{01}Work\nfixture-account\u{01}Home\n",
                       "fixture-account\u{01}Work\n\nother-account\u{01}Home\n",
                       " \u{01}Work\n", "id\u{01} \n", "id\u{01}Work\u{02}unsafe\n",
                       "fixture-account\u{01}Bad\rname\n", "id\u{01}Work\u{01}Extra\n"] {
            precondition(AccountLabelCache.parse(unsafe) == nil, "Unsafe or ambiguous metadata must fail")
        }
        AccountLabelCache.learn(accounts: ["fixture-account": "Office", "other-account": "Home"])
        precondition(reader.readHistory()!.isTrusted(forAccounts:
            ["fixture-account": "Office", "other-account": "Home"]), "Renaming with the same native ID is safe")
        AccountLabelCache.learn(accounts: workHome)
        sql("DELETE FROM messages")
        let authoritativeEmpty = reader.readHistory()!
        precondition(authoritativeEmpty.history.isEmpty && authoritativeEmpty.isTrusted(forAccounts: workHome),
                     "Successful empty scans authenticate the complete account inventory")
        precondition(reader.readTrustedHistory(forAccounts: workHome) == "",
                     "The production acceptance seam returns successful empty history")
        precondition(reader.readTrustedHistory(forAccounts: ["unknown": "Work"]) == nil,
                     "The acceptance seam declines an unverified inventory")
        precondition(!authoritativeEmpty.isTrusted(forAccounts:
            ["fixture-account": "Home", "other-account": "Work"]), "Empty accounts still require exact labels")
        sql("INSERT INTO mailboxes VALUES ('imap://fixture-account/All%20Mail')")
        sql("INSERT INTO subjects VALUES ('Sent only')")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox) VALUES (\(base), 1, 3, 1, 3)")
        precondition(reader.readHistory()!.history.isEmpty, "Sent-only All Mail is outside Inbox history")
        precondition(reader.readHeadersAndHistory()!.history.contains("Sent only"),
                     "The existing combined fallback retains its broader scope")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox) VALUES (\(base), 1, 1, 1, 1)")
        precondition(reader.readHistory()!.history.contains("Fixture subject") &&
                     !reader.readHistory()!.history.contains("Sent only"), "Inbox membership controls primary scope")
        wireFixtures["inbox_history"] = reader.readTrustedHistory(forAccounts: workHome)!
        sql("INSERT INTO mailboxes VALUES ('imap://missing-inbox/All%20Mail')")
        precondition(reader.readHistory() == nil, "Missing Inbox membership declines rather than clearing history")
        precondition(reader.readTrustedHistory(forAccounts: workHome) == nil,
                     "An unavailable source never becomes an empty successful update")
        sql("DELETE FROM mailboxes WHERE ROWID = 4")
        sql("INSERT INTO mailboxes VALUES ('imap://fixture-account/INBOX')")
        precondition(reader.readHistory() == nil, "Ambiguous duplicate Inbox rows decline")
        sql("DELETE FROM mailboxes WHERE ROWID = 4")
        sql("DELETE FROM messages")
        // Two mailboxes past the build's total-row cap together (30,000; Intel 120,000).
        for mailbox in 1...2 {
            sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<\(MailHistoryWindow.totalRows / 2 + 1000)) " +
                "INSERT INTO messages (date_received, read, subject, sender, mailbox) " +
                "SELECT \(base) - x, 1, 1, 1, \(mailbox) FROM seq")
        }
        let globalCap = reader.readHistory()!.history.components(separatedBy: "\n")
        precondition(globalCap.contains(["C2", "Mail", "*", "0", "0", "1"].joined(separator: "\u{01}")),
                     "The total-row cap remains honestly disclosed")
        if let flag = CommandLine.arguments.firstIndex(of: "--wire-output"),
           flag + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[flag + 1]
            precondition(path.hasPrefix("/private/tmp/wisp-pr142-"), "Wire output must use the synthetic artifact directory")
            try JSONSerialization.data(withJSONObject: wireFixtures).write(to: URL(fileURLWithPath: path))
        }
        AccountLabelCache.learn(accounts: workHome)
        AccountLabelCache.learn(accounts: [:])
        precondition(AccountLabelCache.stored().isEmpty,
                     "A known empty inventory removes previously learned account labels")
        AccountLabelCache.learn(accounts: workHome)
        let unavailableOutputs: [String?] = [nil, "\n", "id\u{01}Work\nid\u{01}Home\n",
                                            "id-1\u{01}Work\nid-2\u{01}\n"]
        for output in unavailableOutputs {
            if case .accounts(let accounts) = AccountLabelCache.enumeration(output) {
                AccountLabelCache.learn(accounts: accounts)
            }
            precondition(AccountLabelCache.stored() == workHome,
                         "Failed or malformed enumeration preserves learned labels")
        }
        // Gmail over IMAP: the INBOX mailbox holds no messages; Inbox membership is a
        // `labels` row on a message that lives in All Mail. The primary history read
        // must see those messages, and still exclude All Mail rows with no Inbox label.
        do {
            let gmailPath = root.appendingPathComponent("Gmail Envelope Index").path
            var gdb: OpaquePointer?
            precondition(sqlite3_open(gmailPath, &gdb) == SQLITE_OK)
            defer { sqlite3_close(gdb) }
            func gsql(_ query: String) {
                precondition(sqlite3_exec(gdb, query, nil, nil, nil) == SQLITE_OK, "Gmail fixture SQL failed")
            }
            gsql("CREATE TABLE mailboxes (url TEXT)")
            gsql("CREATE TABLE messages (date_received REAL, read INTEGER, subject INTEGER, sender INTEGER, mailbox INTEGER)")
            gsql("CREATE TABLE subjects (subject TEXT)")
            gsql("CREATE TABLE addresses (address TEXT, comment TEXT)")
            gsql("CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER)")
            gsql("INSERT INTO mailboxes VALUES ('imap://gmail-account/INBOX'), " +
                 "('imap://gmail-account/%5BGmail%5D/All%20Mail')")
            gsql("INSERT INTO subjects VALUES ('Inbox labelled'), ('Sent only')")
            gsql("INSERT INTO addresses VALUES ('sender@example.test', 'Fixture sender')")
            let now = Date().timeIntervalSince1970
            gsql("INSERT INTO messages (date_received, read, subject, sender, mailbox) VALUES (\(now - 60), 1, 1, 1, 2)")
            gsql("INSERT INTO messages (date_received, read, subject, sender, mailbox) VALUES (\(now - 120), 1, 2, 1, 2)")
            gsql("INSERT INTO labels VALUES (1, 1)")
            let gmail = MailDBReader(indexPath: gmailPath)
            let gmailHistory = gmail.readHistory()!.history
            precondition(gmailHistory.contains("Inbox labelled"),
                         "Gmail Inbox membership is a label on an All Mail message; an empty INBOX row must not empty the history")
            precondition(!gmailHistory.contains("Sent only"),
                         "An All Mail message with no Inbox label stays outside Inbox history")
            gsql("DROP TABLE labels")
            precondition(gmail.readHistory()!.history.isEmpty,
                         "Without a labels table the Inbox-only scope is unchanged")
        }
        print("MailDBReader: regression checks passed")
    }

    /// Why closed Mail can't be read, from fixture folders only. reason_kind is
    /// what the Brief keys its button on.
    static func closedReadFailureChecks(root: URL) throws {
        func failure(_ folder: URL) -> (kind: String, reason: String) {
            MailDBReader(indexPath: root.appendingPathComponent("missing").path,
                         mailDirectory: folder.path).closedReadFailure()
        }
        let absent = failure(root.appendingPathComponent("no-mail-folder"))
        precondition(absent.kind == "mail_not_configured" && absent.reason.contains("isn't set up"))
        let empty = root.appendingPathComponent("mail-empty")
        try FileManager.default.createDirectory(at: empty, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: empty.appendingPathComponent("V10/MailData"),
                                                withIntermediateDirectories: true)
        precondition(failure(empty).kind == "mail_not_configured", "a Mail folder with no index has no accounts")
        let withIndex = root.appendingPathComponent("mail-index")
        try FileManager.default.createDirectory(at: withIndex.appendingPathComponent("V10/MailData"),
                                                withIntermediateDirectories: true)
        // Plain writes: FileManager.createFile stages through the system temp folder.
        try Data().write(to: withIndex.appendingPathComponent("V10/MailData/Envelope Index"))
        let present = failure(withIndex)
        precondition(present.kind == "" && present.reason.contains("couldn't be read"),
                     "an index that exists but won't open has nothing to grant")
        let file = root.appendingPathComponent("mail-file")
        try Data().write(to: file)
        precondition(failure(file).kind == "mail_not_configured")

        // The rule itself: Full Disk Access only for macOS's privacy denial.
        typealias R = MailDBReader
        let denied = R.closedReadFailure(mailDirectoryExists: true, listErrno: EPERM, hasIndex: false)
        precondition(denied.kind == "full_disk_access"
                     && denied.reason == "Wisp can't read Apple Mail: open Mail once, or allow Full Disk Access for Wisp "
                        + "in System Settings > Privacy & Security.")
        let unixDenied = R.closedReadFailure(mailDirectoryExists: true, listErrno: EACCES, hasIndex: false)
        precondition(unixDenied.kind == "" && !unixDenied.reason.contains("Full Disk Access"),
                     "a plain permission error is not a Full Disk Access problem")
        precondition(R.closedReadFailure(mailDirectoryExists: false, listErrno: 0, hasIndex: false).kind
                     == "mail_not_configured")
        precondition(R.closedReadFailure(mailDirectoryExists: true, listErrno: 0, hasIndex: true).kind == "")
    }

    /// Every combination of Mail open or closed, Full Disk Access, and Automation
    /// allowed, denied or unknown, with a counting launcher: Mail is opened in one
    /// case only.
    static func readPlanChecks() {
        typealias P = MailReadPlan
        let fda = (kind: "full_disk_access", reason: P.openMailOrFullDiskAccess)
        let notConfigured = (kind: "mail_not_configured", reason: "Mail isn't set up on this Mac yet.")
        let damaged = (kind: "", reason: "Mail's saved index couldn't be read just now.")
        let states: [MailAutomationState] = [.granted, .denied, .unknown]
        var launches = 0
        func plan(open: Bool, _ automation: MailAutomationState, folder: Bool,
                  failure: (kind: String, reason: String)? = nil) -> P {
            let chosen = P.decide(mailOpen: open, automation: automation, folderReadable: folder,
                                  failure: failure ?? (folder ? damaged : fda))
            _ = P.openIfPlanned(chosen) { launches += 1; return true }
            return chosen
        }
        for automation in states {
            // Full Disk Access: the index, headers only, never a launch (Mail closed or open
            // without a usable Automation answer); Mail open and allowed scripts as before.
            launches = 0
            precondition(plan(open: false, automation, folder: true) == .localIndex)
            precondition(launches == 0, "Full Disk Access reads the index without opening Mail")
            precondition(plan(open: true, automation, folder: true) == (automation == .granted ? .appleScript : .localIndex))
        }
        // Mail closed, no Full Disk Access.
        launches = 0
        precondition(plan(open: false, .granted, folder: false) == .openHiddenThenAppleScript)
        precondition(launches == 1, "the one case that opens Mail")
        launches = 0
        let denied = plan(open: false, .denied, folder: false)
        precondition(denied == .cannotRead(kind: "mail_automation", reason: P.automationOff))
        let unknown = plan(open: false, .unknown, folder: false)
        precondition(unknown == .cannotRead(kind: "full_disk_access", reason: P.openMailOrFullDiskAccess))
        precondition(launches == 0, "denied or never-asked Automation never opens Mail")
        // Mail open, no Full Disk Access.
        precondition(plan(open: true, .granted, folder: false) == .appleScript)
        precondition(plan(open: true, .denied, folder: false)
                     == .cannotRead(kind: "mail_automation", reason: P.automationOff))
        precondition(plan(open: true, .unknown, folder: false)
                     == .cannotRead(kind: "mail_automation", reason: P.automationUnasked))
        precondition(launches == 0)
        // Reasons that are not about permissions pass through, and no launch for a Mac without Mail.
        precondition(plan(open: false, .granted, folder: false, failure: notConfigured)
                     == .cannotRead(kind: "mail_not_configured", reason: notConfigured.reason))
        precondition(plan(open: false, .unknown, folder: false, failure: damaged)
                     == .cannotRead(kind: "", reason: damaged.reason))
        precondition(plan(open: false, .denied, folder: false, failure: damaged)
                     == .cannotRead(kind: "mail_automation", reason: P.automationOffOnly))
        precondition(launches == 0)
        // The switch that opens Mail for Full Disk Access users too: only when Automation is allowed.
        for automation in states {
            let chosen = P.decide(mailOpen: false, automation: automation, folderReadable: true,
                                  failure: damaged, openHiddenWithFullDiskAccess: true)
            precondition(chosen == (automation == .granted ? .openHiddenThenAppleScript : .localIndex))
        }
        precondition(!P.openHiddenWithFullDiskAccess, "off by default")
        // A launcher that fails reports it; a plan that does not launch never calls it.
        precondition(!P.openIfPlanned(.openHiddenThenAppleScript) { false })
        precondition(P.openIfPlanned(.appleScript) { preconditionFailure("must not launch") })
        precondition(!P.openIfPlanned(.localIndex) { preconditionFailure("must not launch") })
        // Every sentence is one plain sentence naming the fix.
        precondition(P.openMailOrFullDiskAccess == "Wisp can't read Apple Mail: open Mail once, or allow Full Disk Access for Wisp in System Settings > Privacy & Security.")
        print("MailReadPlan: decision table checks passed")
    }
}
