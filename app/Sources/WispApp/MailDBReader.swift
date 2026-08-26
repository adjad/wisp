import Foundation
import SQLite3

// Reads Mail headers/history straight from Mail's own on-disk index
// (~/Library/Mail/V*/MailData/Envelope Index) instead of scripting Mail.app —
// so the 5-min header sync and 2-year history backfill never have to launch
// Mail just to answer "what's in my inbox". Same Full Disk Access grant
// BrowserHistoryReader/MessagesReader already use for their direct-SQLite
// reads (Permissions.swift requests it once at first launch).
//
// This is a FALLBACK, not a replacement: MailReader's AppleScript path stays
// the primary source whenever Mail happens to already be open (it also
// resolves account display names, which this file can't do on its own — see
// AccountLabelCache below). This only fires when MailReader's own
// isMailRunning() check skipped a tick because Mail wasn't open.
//
// Verified against the real Envelope Index on this machine before writing
// any of this: date_received/date_sent are plain Unix-epoch SECONDS (not the
// Mac/Core-Data epoch every other Apple store on this machine uses) — get
// this wrong and every header silently sorts as decades off.
final class MailDBReader {
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
    /// Format matches MailReader's AppleScript output exactly: "epochSecs |
    /// R/U | account | sender | subject", so the backend parser
    /// (email_tools._parse_pipe_lines) doesn't need to know which path
    /// produced a given line.
    func readHeadersAndHistory() -> (headers: String, history: String)? {
        guard let path = envelopeIndexPath() else { return nil }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }

        let cutoff = Date().timeIntervalSince1970 - Double(historyCutoffDays) * 86400

        let mailboxIDs = sourceMailboxIDs(db)
        guard !mailboxIDs.isEmpty else { return ("", "") }
        let idList = mailboxIDs.map(String.init).joined(separator: ",")

        let sql = """
        SELECT m.date_received, m.read, s.subject, a.address, a.comment, mb.url
        FROM messages m
        JOIN mailboxes mb ON m.mailbox = mb.ROWID
        LEFT JOIN subjects s ON m.subject = s.ROWID
        LEFT JOIN addresses a ON m.sender = a.ROWID
        WHERE m.mailbox IN (\(idList)) AND m.date_received > \(cutoff)
        ORDER BY m.date_received DESC
        LIMIT \(totalRowCap)
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        defer { sqlite3_finalize(stmt) }

        let orderedUUIDs = accountUUIDsByFirstAppearance(db)
        var byAccount: [String: [String]] = [:]   // label -> lines, DESC order preserved
        var allLines: [String] = []

        while sqlite3_step(stmt) == SQLITE_ROW {
            let epoch = sqlite3_column_double(stmt, 0)
            guard epoch > 0 else { continue }
            let readFlag = sqlite3_column_int(stmt, 1) == 1 ? "R" : "U"
            let subject = text(stmt, 2) ?? "(no subject)"
            let address = text(stmt, 3) ?? ""
            let comment = text(stmt, 4) ?? ""
            let sender = comment.isEmpty ? address : comment
            let url = text(stmt, 5) ?? ""
            let account = AccountLabelCache.label(forURL: url, orderedUUIDs: orderedUUIDs)

            // Trim to an int for display — fractional seconds don't exist in
            // this column, but formatting a Double directly here would print
            // "1787537314.0", which the backend's numeric-prefix parser (see
            // MailReader.epoch()) still accepts fine, but keeping it a clean
            // integer matches what the AppleScript path emits.
            let line = "\(Int64(epoch)) | \(readFlag) | \(account) | \(sender) | \(subject)"
            allLines.append(line)
            byAccount[account, default: []].append(line)
        }

        guard !allLines.isEmpty else { return ("", "") }

        var headerLines: [String] = []
        for (_, lines) in byAccount {
            headerLines.append(contentsOf: lines.prefix(headerLimitPerAccount))
        }
        // Re-sort: concatenating per-account slices loses the original
        // global date ordering (see MailReader.mergeHeaderChunks, which
        // faces the same problem for the AppleScript path and solves it the
        // same way — summarize_inbox_recent takes the first N lines as "the
        // most recent", so this must be a true global sort, not per-account).
        headerLines.sort { epoch(of: $0) > epoch(of: $1) }

        return (headerLines.joined(separator: "\n") + "\n", allLines.joined(separator: "\n") + "\n")
    }

    private func epoch(of line: String) -> Double {
        Double(line.split(separator: "|", maxSplits: 1)[0].trimmingCharacters(in: .whitespaces)) ?? 0
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
    private func sourceMailboxIDs(_ db: OpaquePointer?) -> [Int64] {
        var stmt: OpaquePointer?
        let sql = "SELECT ROWID, url FROM mailboxes WHERE url LIKE '%/INBOX' OR url LIKE '%/All%20Mail'"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }

        var inboxByAccount: [String: Int64] = [:]
        var allMailByAccount: [String: Int64] = [:]
        while sqlite3_step(stmt) == SQLITE_ROW {
            let rowid = sqlite3_column_int64(stmt, 0)
            guard let c = sqlite3_column_text(stmt, 1) else { continue }
            let urlString = String(cString: c)
            guard let url = URL(string: urlString), let host = url.host else { continue }
            if urlString.hasSuffix("/All%20Mail") {
                allMailByAccount[host] = rowid
            } else {
                inboxByAccount[host] = rowid
            }
        }
        let accounts = Set(inboxByAccount.keys).union(allMailByAccount.keys)
        return accounts.compactMap { allMailByAccount[$0] ?? inboxByAccount[$0] }
    }

    /// Distinct account UUIDs (parsed from each INBOX mailbox's url host),
    /// ordered by that mailbox's own ROWID ascending — Mail creates mailbox
    /// rows in account-creation order, so this is the same order Mail.app's
    /// AppleScript `accounts` list itself returns in. Used only as the
    /// POSITIONAL bridge AccountLabelCache needs to turn an AppleScript name
    /// list into a UUID->name mapping — approximate by construction, but it
    /// only affects a display label, never which messages are attributed to
    /// which account (that's the UUID itself, always exact).
    private func accountUUIDsByFirstAppearance(_ db: OpaquePointer?) -> [String] {
        var stmt: OpaquePointer?
        let sql = "SELECT url FROM mailboxes WHERE url LIKE '%/INBOX' ORDER BY ROWID ASC"
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return [] }
        defer { sqlite3_finalize(stmt) }
        var out: [String] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let c = sqlite3_column_text(stmt, 0) else { continue }
            if let uuid = URL(string: String(cString: c))?.host { out.append(uuid) }
        }
        return out
    }

    private func text(_ stmt: OpaquePointer?, _ idx: Int32) -> String? {
        guard let c = sqlite3_column_text(stmt, idx) else { return nil }
        return String(cString: c)
    }

    /// Public wrapper so MailReader can feed AccountLabelCache.learn() right
    /// after a successful AppleScript accountNames() call, without owning
    /// any SQLite plumbing itself.
    func orderedAccountUUIDs() -> [String] {
        guard let path = envelopeIndexPath() else { return [] }
        var db: OpaquePointer?
        guard sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return []
        }
        defer { sqlite3_close(db) }
        return accountUUIDsByFirstAppearance(db)
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
    private static let key = "wisp.mailAccountLabels"   // [uuid: name]

    static func label(forURL mailboxURL: String, orderedUUIDs: [String]) -> String {
        guard let uuid = URL(string: mailboxURL)?.host else { return "Mail" }
        if let name = stored()[uuid], !name.isEmpty { return name }
        if let idx = orderedUUIDs.firstIndex(of: uuid) { return "Account \(idx + 1)" }
        return "Account"
    }

    /// Called by MailReader whenever its AppleScript accountNames() call
    /// succeeds (Mail is open) — positionally zips those names against the
    /// same mailbox-creation-order UUID list this file derives, then merges
    /// into the persisted cache. Positional, not name-matched: AppleScript's
    /// `accounts` exposes no UUID/url property to join on directly.
    static func learn(names: [String], orderedUUIDs: [String]) {
        guard !names.isEmpty, !orderedUUIDs.isEmpty else { return }
        var cache = stored()
        for (uuid, name) in zip(orderedUUIDs, names) {
            cache[uuid] = name
        }
        UserDefaults.standard.set(cache, forKey: key)
    }

    private static func stored() -> [String: String] {
        UserDefaults.standard.dictionary(forKey: key) as? [String: String] ?? [:]
    }
}
