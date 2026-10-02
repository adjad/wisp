import Foundation
import SQLite3

// Reads Mail headers/history straight from Mail's own on-disk index
// (~/Library/Mail/V*/MailData/Envelope Index) instead of scripting Mail.app —
// so the 5-min header sync and 2-year history backfill never have to launch
// Mail just to answer "what's in my inbox". Same Full Disk Access grant
// BrowserHistoryReader/MessagesReader already use for their direct-SQLite
// reads (Permissions.swift requests it once at first launch).
//
// Two roles. For recent HEADERS it is a fallback: MailReader's AppleScript
// path stays primary whenever Mail is open (it also resolves account display
// names, which this file can't do on its own — see AccountLabelCache below),
// and readHeadersAndHistory() only fires when Mail isn't running. For the
// 2-year HISTORY it is the primary source whenever it is readable and its
// account labels can be trusted (readHistory + MailIndexHistory.isTrusted):
// the AppleScript history walk it replaces cost ~5 minutes of serialized
// Apple Events every 30 minutes.
//
// Verified against the real Envelope Index on this machine before writing
// any of this: date_received/date_sent are plain Unix-epoch SECONDS (not the
// Mac/Core-Data epoch every other Apple store on this machine uses) — get
// this wrong and every header silently sorts as decades off.
final class MailDBReader {
    private let indexPath: String?

    // An explicit path lets regression tests exercise SQLite with fixtures,
    // without touching the user's Mail database. Production discovers it.
    init(indexPath: String? = nil) { self.indexPath = indexPath }

    // Matches MailReader's own constants so the two paths produce
    // comparably-scoped output regardless of which one answers a given tick.
    private let headerLimitPerAccount = 200
    private let historyCutoffDays = 730
    // Safety ceiling on total rows fetched in one query — this is a single
    // indexed SQL scan (milliseconds), not the multi-minute AppleScript batch
    // walk it replaces, so there's no real cost pressure to keep this tight;
    // it exists purely so a truly enormous mailbox can't produce an
    // unbounded payload.
    private let totalRowCap = 30000
    // Same per-account ceiling as MailReader.historyCap, so either history
    // source reports truncation for the same account at the same depth.
    private let historyCap = 12000

    private func envelopeIndexPath() -> String? {
        let mailDir = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Mail")
        guard let entries = try? FileManager.default.contentsOfDirectory(atPath: mailDir) else { return nil }
        // Highest "V<N>" wins — that's always the current format version;
        // stale V<N-1> directories can linger after a macOS upgrade.
        for e in entries.sorted().reversed() where e.hasPrefix("V") {
            let candidate = "\(mailDir)/\(e)/MailData/Envelope Index"
            if FileManager.default.fileExists(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// Header + history lines, newest-first, keyed by account label — or nil
    /// if the index couldn't be opened at all (no FDA, or file moved).
    /// Format matches MailReader's AppleScript H2 header output, so the backend parser
    /// (email_tools._parse_pipe_lines) doesn't need to know which path
    /// produced a given line.
    func readHeadersAndHistory() -> (headers: String, history: String)? {
        guard let scan = scanIndex(inboxOnly: false) else { return nil }
        return (headersText(scan), historyText(scan))
    }

    /// History only, for MailReader's 30-minute history sync while Mail is
    /// running. One indexed SQL read replaces the multi-minute AppleScript
    /// walk and sends Mail no Apple Events at all, so it can never queue
    /// ahead of a header read someone is waiting on. Returns nil when the
    /// index can't be opened or read (no Full Disk Access, missing schema,
    /// locked past the busy timeout). Whether its account LABELS can be
    /// trusted is a separate question — see MailIndexHistory.isTrusted.
    func readHistory() -> MailIndexHistory? {
        guard let scan = scanIndex(inboxOnly: true) else { return nil }
        return MailIndexHistory(history: historyText(scan),
                                indexAccountCount: scan.orderedUUIDs.count,
                                labelsByAccountID: scan.labelByAccount,
                                hasUnresolvedLabel: scan.hasUnresolvedLabel)
    }

    /// nil declines the source; an empty string is a successful snapshot.
    func readTrustedHistory(forAccounts accounts: [String: String]) -> String? {
        guard let history = readHistory(), history.isTrusted(forAccounts: accounts) else { return nil }
        return history.history
    }

    /// Everything one read of the index learned, before it is rendered.
    private struct IndexScan {
        var byAccount: [String: [String]] = [:]   // header-eligible lines, DESC
        var attemptedByAccount: [String: Int] = [:]
        var skippedByAccount: [String: Int] = [:]
        var labelByAccount: [String: String] = [:]
        var historyLines: [String] = []
        // History applies MailReader's per-account ceiling (historyCap) the
        // same way the AppleScript walk does: attempts stop counting once an
        // account reaches it, and the account gets a cap marker.
        var historyAttempted: [String: Int] = [:]
        var historySkipped: [String: Int] = [:]
        var historyCapped: Set<String> = []
        var totalRows = 0
        var orderedUUIDs: [String] = []
        var hasUnresolvedLabel = false
        var empty = false
    }

    private func scanIndex(inboxOnly: Bool) -> IndexScan? {
        guard let path = indexPath ?? envelopeIndexPath() else { return nil }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }
        sqlite3_busy_timeout(db, 2000)

        let cutoff = Date().timeIntervalSince1970 - Double(historyCutoffDays) * 86400

        guard let mailboxIDs = sourceMailboxIDs(db, inboxOnly: inboxOnly) else { return nil }
        var scan = IndexScan()
        scan.orderedUUIDs = accountUUIDsByFirstAppearance(db)
        let labels = AccountLabelCache.stored()
        // Include empty accounts in the trust proof; message rows are not an
        // account inventory. Legacy positional preferences are never read.
        for id in scan.orderedUUIDs {
            if let label = labels[id] { scan.labelByAccount[id] = label }
            else { scan.hasUnresolvedLabel = true }
        }
        guard !mailboxIDs.isEmpty else {
            scan.empty = true
            return scan
        }
        let idList = mailboxIDs.map(String.init).joined(separator: ",")

        // Mail index schemas vary by macOS version. Only request Message-ID
        // when the column exists; a missing ID is safer than failing the scan.
        let hasMessageID = columnExists(db, table: "messages", column: "message_id")
        let messageIDColumn = hasMessageID ? "m.message_id" : "''"
        // A Gmail-over-IMAP account keeps its real mail in All Mail and marks Inbox
        // membership with a `labels` row pointing at the INBOX mailbox; the INBOX
        // mailbox itself holds no messages. Without this, an Inbox-only read of
        // such an account is a successful EMPTY history that replaces a good one.
        let inboxByLabel = inboxOnly && columnExists(db, table: "labels", column: "message_id")
            && columnExists(db, table: "labels", column: "mailbox_id")
        let membership = inboxByLabel
            ? "(m.mailbox IN (\(idList)) OR m.ROWID IN (SELECT message_id FROM labels WHERE mailbox_id IN (\(idList))))"
            : "m.mailbox IN (\(idList))"
        let sql = """
        SELECT m.date_received, m.read, s.subject, a.address, a.comment, mb.url, \(messageIDColumn), m.ROWID
        FROM messages m
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        LEFT JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        WHERE \(membership) AND m.date_received > \(cutoff)
        ORDER BY m.date_received DESC
        LIMIT \(totalRowCap)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            scan.totalRows += 1
            let epoch = sqlite3_column_double(stmt, 0)
            let readFlag = sqlite3_column_int(stmt, 1) == 1 ? "R" : "U"
            let subject = text(stmt, 2) ?? "(no subject)"
            let address = text(stmt, 3) ?? ""
            let comment = text(stmt, 4) ?? ""
            let sender = comment.isEmpty ? address : comment
            let url = text(stmt, 5) ?? ""
            let account = AccountLabelCache.label(forURL: url, orderedUUIDs: scan.orderedUUIDs,
                                                  stored: labels)
            if AccountLabelCache.resolvedLabel(forURL: url, stored: labels) == nil {
                scan.hasUnresolvedLabel = true
            }
            let accountID = URL(string: url)?.host ?? ""
            let accountKey = accountID.isEmpty ? (url.isEmpty ? account : url) : accountID
            scan.attemptedByAccount[accountKey, default: 0] += 1
            scan.labelByAccount[accountKey] = account
            let inHistory = scan.historyAttempted[accountKey, default: 0] < historyCap
            if inHistory {
                scan.historyAttempted[accountKey, default: 0] += 1
            } else {
                scan.historyCapped.insert(accountKey)
            }
            guard epoch > 0 else {
                scan.skippedByAccount[accountKey, default: 0] += 1
                if inHistory { scan.historySkipped[accountKey, default: 0] += 1 }
                continue
            }
            let messageID = text(stmt, 6) ?? ""
            let nativeID = "db:\(sqlite3_column_int64(stmt, 7))"

            // Trim to an int for display — fractional seconds don't exist in
            // this column, but formatting a Double directly here would print
            // "1787537314.0", which the backend's numeric-prefix parser (see
            // MailReader.epoch()) still accepts fine, but keeping it a clean
            // integer matches what the AppleScript path emits.
            let fields = ["H2", String(Int64(epoch)), readFlag, account,
                          accountID, sender, address, messageID, subject, nativeID]
            // A malformed header cannot be allowed to forge another record.
            guard fields.allSatisfy({ !$0.contains("\u{01}") && !$0.contains("\n")
                                      && !$0.contains("\r") }) else {
                scan.skippedByAccount[accountKey, default: 0] += 1
                if inHistory { scan.historySkipped[accountKey, default: 0] += 1 }
                continue
            }
            let line = fields.joined(separator: "\u{01}")
            if inHistory { scan.historyLines.append(line) }
            scan.byAccount[accountKey, default: []].append(line)
        }
        // SQLITE_BUSY/IOERR are failures, not a successful empty/partial scan.
        guard step == SQLITE_DONE else { return nil }
        return scan
    }

    private func safeMarkerFields(_ scan: IndexScan, _ key: String) -> (String, String) {
        let label = scan.labelByAccount[key] ?? "Mail"
        let safeLabel = label.contains("\u{01}") || label.contains("\n") || label.contains("\r") ? "Mail" : label
        let safeKey = key.contains("\u{01}") || key.contains("\n") || key.contains("\r") ? "" : key
        return (safeLabel, safeKey)
    }

    private func headersText(_ scan: IndexScan) -> String {
        guard !scan.empty else { return "" }
        var headerLines: [String] = []
        for (key, lines) in scan.byAccount {
            headerLines.append(contentsOf: lines.prefix(headerLimitPerAccount))
            let attempted = scan.attemptedByAccount[key, default: 0]
            let skipped = scan.skippedByAccount[key, default: 0]
            if attempted >= headerLimitPerAccount || skipped > 0 {
                let (safeLabel, safeKey) = safeMarkerFields(scan, key)
                headerLines.append(["C2", safeLabel, safeKey, String(attempted),
                                    String(skipped), attempted >= headerLimitPerAccount ? "1" : "0"]
                    .joined(separator: "\u{01}"))
            }
        }
        // An account may have only malformed headers; retain its coverage
        // marker even when no valid H2 row can identify it downstream.
        for key in scan.attemptedByAccount.keys where scan.byAccount[key] == nil {
            let (safeLabel, safeKey) = safeMarkerFields(scan, key)
            let attempted = scan.attemptedByAccount[key] ?? 0
            headerLines.append(["C2", safeLabel, safeKey, String(attempted),
                                String(scan.skippedByAccount[key] ?? 0),
                                attempted >= headerLimitPerAccount ? "1" : "0"]
                .joined(separator: "\u{01}"))
        }
        if scan.totalRows >= totalRowCap {
            headerLines.append(["C2", "Mail", "*", "0", "0", "1"]
                .joined(separator: "\u{01}"))
        }
        guard !headerLines.isEmpty else { return "" }
        // Re-sort: concatenating per-account slices loses the original
        // global date ordering (see MailReader.mergeHeaderChunks, which
        // faces the same problem for the AppleScript path and solves it the
        // same way — summarize_inbox_recent takes the first N lines as "the
        // most recent", so this must be a true global sort, not per-account).
        headerLines.sort { epoch(of: $0) > epoch(of: $1) }
        return headerLines.joined(separator: "\n") + "\n"
    }

    /// History wire text with the same C2 semantics as the AppleScript walk:
    /// one attempted/skipped marker per account, a separate cap marker for an
    /// account that reached historyCap, and the total-row cap marker.
    private func historyText(_ scan: IndexScan) -> String {
        guard !scan.empty else { return "" }
        var historyLines = scan.historyLines
        for key in scan.historyAttempted.keys.sorted() {
            let (safeLabel, safeKey) = safeMarkerFields(scan, key)
            historyLines.append(["C2", safeLabel, safeKey,
                                 String(scan.historyAttempted[key, default: 0]),
                                 String(scan.historySkipped[key, default: 0]), "0"]
                .joined(separator: "\u{01}"))
            if scan.historyCapped.contains(key) {
                historyLines.append(["C2", safeLabel, safeKey, "0", "0", "1"]
                    .joined(separator: "\u{01}"))
            }
        }
        if scan.totalRows >= totalRowCap {
            historyLines.append(["C2", "Mail", "*", "0", "0", "1"]
                .joined(separator: "\u{01}"))
        }
        guard !historyLines.isEmpty else { return "" }
        return historyLines.joined(separator: "\n") + "\n"
    }

    private func epoch(of line: String) -> Double {
        if line.hasPrefix("H2\u{01}") {
            let parts = line.components(separatedBy: "\u{01}")
            return parts.count > 1 ? Double(parts[1]) ?? 0 : 0
        }
        return Double(line.split(separator: "|", maxSplits: 1)[0].trimmingCharacters(in: .whitespaces)) ?? 0
    }

    /// Per account, the ROWID of the mailbox that actually holds its mail.
    ///
    /// NOT simply "the mailbox whose url ends in /INBOX" — verified against
    /// the real database on this machine that for a Gmail-via-IMAP account,
    /// that mailbox is essentially empty (0 recent rows out of 20k+ real
    /// messages) despite Mail.app's own AppleScript `mailbox "INBOX" of
    /// account` correctly returning hundreds of messages for the same
    /// account. Gmail's IMAP presents "All Mail" as the actual comprehensive
    /// store; Mail.app's AppleScript object model resolves "INBOX" through
    /// Gmail's label metadata rather than through a same-named physical
    /// mailbox row, which this direct SQL read has no access to. So: prefer
    /// "All Mail" when the account has one (every Gmail account does), fall
    /// back to "INBOX" otherwise (every non-Gmail IMAP/iCloud account does,
    /// and lacks "All Mail" entirely). This does mean Gmail accounts' synced
    /// headers include the user's own sent mail (All Mail contains it,
    /// Drafts/Trash/Spam don't since those stay separate mailbox rows) —
    /// broader than the AppleScript path's strict "received in inbox"
    /// scope, but the alternative is zero data for exactly the accounts
    /// most people actually have.
    private func sourceMailboxIDs(_ db: OpaquePointer?, inboxOnly: Bool) -> [Int64]? {
        var stmt: OpaquePointer?
        let sql = "SELECT ROWID, url FROM mailboxes WHERE url LIKE '%/INBOX' OR url LIKE '%/All%20Mail'"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        var inboxByAccount: [String: Int64] = [:]
        var allMailByAccount: [String: Int64] = [:]
        var step = sqlite3_step(stmt)
        while step == SQLITE_ROW {
            defer { step = sqlite3_step(stmt) }
            let rowid = sqlite3_column_int64(stmt, 0)
            guard let c = sqlite3_column_text(stmt, 1) else { continue }
            let urlString = String(cString: c)
            guard let url = URL(string: urlString), let host = url.host else { continue }
            if urlString.hasSuffix("/All%20Mail") {
                allMailByAccount[host] = rowid
            } else {
                if inboxOnly, inboxByAccount[host] != nil { return nil }
                inboxByAccount[host] = rowid
            }
        }
        guard step == SQLITE_DONE else { return nil }
        // All Mail membership cannot prove Inbox membership. Keep the older
        // broad fallback for combined closed-Mail reads, but never promote it
        // to authoritative Inbox history. Missing INBOX means fallback, not
        // a successful empty read that could erase previously held history.
        if inboxOnly {
            guard Set(allMailByAccount.keys).isSubset(of: Set(inboxByAccount.keys)) else { return nil }
            return Array(inboxByAccount.values)
        }
        let accounts = Set(inboxByAccount.keys).union(allMailByAccount.keys)
        return accounts.compactMap { allMailByAccount[$0] ?? inboxByAccount[$0] }
    }

    /// Distinct account UUIDs (parsed from each INBOX mailbox's url host),
    /// ordered by mailbox ROWID only for stable cosmetic fallback labels.
    /// This order establishes no relationship with AppleScript enumeration.
    private func accountUUIDsByFirstAppearance(_ db: OpaquePointer?) -> [String] {
        var stmt: OpaquePointer?
        let sql = "SELECT url FROM mailboxes WHERE url LIKE '%/INBOX' ORDER BY ROWID ASC"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let c = sqlite3_column_text(stmt, 0) else { continue }
            if let uuid = URL(string: String(cString: c))?.host, !out.contains(uuid) { out.append(uuid) }
        }
        return out
    }

    private func text(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let c = sqlite3_column_text(stmt, idx) else { return nil }
        return String(cString: c)
    }

    private func columnExists(_ db: OpaquePointer?, table: String, column: String) -> Bool {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "PRAGMA table_info(\(table))", -1, &stmt, nil) == SQLITE_OK else {
            return false
        }
        defer { sqlite3_finalize(stmt) }
        while sqlite3_step(stmt) == SQLITE_ROW {
            if text(stmt, 1) == column { return true }
        }
        return false
    }

    /// Stable index inventory, also used by synthetic regression fixtures.
    func orderedAccountUUIDs() -> [String] {
        guard let path = indexPath ?? envelopeIndexPath() else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        return accountUUIDsByFirstAppearance(db)
    }
}

/// One history read of the Envelope Index plus what MailReader needs to
/// decide whether its account labels can be posted.
struct MailIndexHistory {
    let history: String
    /// Distinct INBOX accounts, including accounts with no messages.
    let indexAccountCount: Int
    /// Native account ID (mailbox URL host) -> the label every row carries.
    let labelsByAccountID: [String: String]
    /// Some row fell back to "Account N" / "Account" / "Mail".
    let hasUnresolvedLabel: Bool

    /// Exact current native-ID/name associations must match every index
    /// account. If Mail's ID format differs from index URL hosts, decline.
    func isTrusted(forAccounts accounts: [String: String]) -> Bool {
        AccountLabelCache.valid(accounts) && indexAccountCount == accounts.count &&
            !hasUnresolvedLabel && labelsByAccountID == accounts
    }
}

/// UUID -> Mail account display name, persisted across launches. The
/// Envelope Index has no account-name table at all (verified: no table with
/// "account" in its name), so the only source of truth for a human-readable
/// name is Mail.app's own AppleScript `accounts` list — which requires Mail
/// to be running. This cache is how a name learned on some earlier tick
/// (Mail happened to be open) survives to label DB-only ticks (Mail closed).
/// A UUID with no cached name yet falls back to "Account N" (N = its
/// position in mailbox-creation order) rather than the raw UUID — cosmetic
/// only, never blocks headers from syncing.
enum AccountLabelCache {
    enum Enumeration: Equatable {
        case accounts([String: String])
        case unavailable
        case duplicateLabels
    }
    // A new key deliberately rejects labels learned by the old positional zip.
    private static let key = "wisp.mailAccountLabels.identity.v1"   // [native ID: name]

    static func label(forURL mailboxURL: String, orderedUUIDs: [String],
                      stored cache: [String: String]? = nil) -> String {
        guard let uuid = URL(string: mailboxURL)?.host else { return "Mail" }
        if let name = resolvedLabel(forURL: mailboxURL, stored: cache) { return name }
        if let idx = orderedUUIDs.firstIndex(of: uuid) { return "Account \(idx + 1)" }
        return "Account"
    }

    /// The learned display name for this mailbox's account, or nil when only
    /// a fallback ("Account N", "Account", "Mail") is available.
    static func resolvedLabel(forURL mailboxURL: String,
                              stored cache: [String: String]? = nil) -> String? {
        guard let uuid = URL(string: mailboxURL)?.host else { return nil }
        guard let name = (cache ?? stored())[uuid], !name.isEmpty else { return nil }
        return name
    }

    /// Each pair comes from id/name properties of the SAME enabled account.
    static func learn(accounts: [String: String]) {
        guard accounts.isEmpty || valid(accounts) else { return }
        UserDefaults.standard.set(accounts, forKey: key)
    }

    static func valid(_ accounts: [String: String]) -> Bool {
        !accounts.isEmpty && accounts.allSatisfy { id, name in
            !id.isEmpty && !name.isEmpty && !id.contains("\u{01}") && !name.contains("\u{01}") &&
                !id.contains("\n") && !name.contains("\n") && !id.contains("\r") && !name.contains("\r")
        } && Set(accounts.values.map { $0.lowercased() }).count == accounts.count
    }

    static func parse(_ text: String) -> [String: String]? {
        // The script starts with "" and appends one pair per enabled account.
        // Only exact empty SUCCESS means zero accounts; blank/malformed output
        // and an unavailable script must never authenticate an empty inventory.
        if text.isEmpty { return [:] }
        var accounts: [String: String] = [:]
        for line in text.split(separator: "\n") {
            let fields = line.components(separatedBy: "\u{01}")
            guard fields.count == 2, accounts[fields[0]] == nil else { return nil }
            accounts[fields[0]] = fields[1]
        }
        return valid(accounts) ? accounts : nil
    }

    static func enumeration(_ text: String?) -> Enumeration {
        guard let text else { return .unavailable }
        let labels = text.split(separator: "\n").compactMap { line -> String? in
            let fields = line.components(separatedBy: "\u{01}")
            return fields.count == 2 ? fields[1] : nil
        }
        guard Set(labels.map { $0.lowercased() }).count == labels.count else { return .duplicateLabels }
        guard let accounts = parse(text) else { return .unavailable }
        return .accounts(accounts)
    }

    static func stored() -> [String: String] {
        UserDefaults.standard.dictionary(forKey: key) as? [String: String] ?? [:]
    }
}
