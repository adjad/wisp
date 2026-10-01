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
        sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<12001) " +
            "INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "SELECT \(base) - x, 1, 1, 1, 1, 'cap-'||x FROM seq")
        sql("INSERT INTO messages (date_received, read, subject, sender, mailbox, message_id) " +
            "VALUES (\(base) - 5, 1, 1, 1, 2, 'other-one')")
        let capped = reader.readHistory()!.history.split(separator: "\n")
            .map { String($0).components(separatedBy: "\u{01}") }
        precondition(capped.filter { $0[0] == "H2" && $0[4] == "fixture-account" }.count == 12000,
                     "History keeps at most historyCap rows per account")
        precondition(capped.contains { $0 == ["C2", "Work", "fixture-account", "12000", "0", "0"] } &&
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
        AccountLabelCache.learn(accounts: reversed)
        precondition(reader.readHistory()!.isTrusted(forAccounts: workHome))
        precondition(!reader.readHistory()!.isTrusted(forAccounts:
            ["fixture-account": "Home", "other-account": "Work"]), "Swapped names must fail exact identity proof")
        precondition(!reader.readHistory()!.isTrusted(forAccounts:
            ["new-account": "Work", "other-account": "Home"]), "Unknown native IDs must fall back")
        for unsafe in ["\u{01}Work\n", "fixture-account\u{01}\n",
                       "fixture-account\u{01}Work\nfixture-account\u{01}Home\n",
                       "fixture-account\u{01}Work\nother-account\u{01}work\n",
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
        for mailbox in 1...2 {
            sql("WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x<16000) " +
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
        print("MailDBReader: regression checks passed")
    }
}
