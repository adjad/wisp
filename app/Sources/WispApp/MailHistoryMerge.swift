import Foundation

/// The production history transport. Empty successful snapshots are updates,
/// too; unavailable reads never invoke this helper. Session injection allows
/// captured synthetic requests without contacting the backend.
enum MailHistoryTransport {
    static func post(_ history: String, to endpoint: URL, session: URLSession = .shared,
                     completion: @escaping () -> Void = {}) {
        post(["history": history], to: endpoint, session: session, completion: completion)
    }

    static func postEmptyAccounts(to endpoint: URL, readSource: String,
                                  session: URLSession = .shared, completion: @escaping () -> Void = {}) {
        post(["headers": "", "history": "",
              "diagnostics": ["available": true, "reason": "", "syncing": false, "read_source": readSource]],
             to: endpoint, session: session, completion: completion)
    }

    private static func post(_ payload: [String: Any], to endpoint: URL, session: URLSession,
                             completion: @escaping () -> Void) {
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        session.dataTask(with: request) { _, _, _ in completion() }.resume()
    }
}

/// Serializes local snapshot replacement with captured-walk commit/enqueue.
/// The lock establishes local ordering only, not backend arrival ordering.
final class MailHistoryState {
    private let lock = NSLock()
    private var generation: UInt64 = 0
    private var accounts: [String: MailHistoryAccount] = [:]
    private var identities: [String: String]?
    private var lastCompletedIdentities: [String: String]?

    func capture() -> (generation: UInt64, accounts: [String: MailHistoryAccount], identities: [String: String]?, lastKnownEmpty: Bool) {
        lock.lock(); defer { lock.unlock() }
        return (generation, accounts, identities, lastCompletedIdentities?.isEmpty == true)
    }

    /// Every enumeration observation fences work captured with older or
    /// unknown identity. Missing identity preserves the last known snapshot.
    func observe(_ current: [String: String]?) {
        lock.lock(); defer { lock.unlock() }
        guard identities != current else { return }
        identities = current
        generation &+= 1
        if let current {
            lastCompletedIdentities = current
            accounts = accounts.filter { current[$0.key] != nil }
        }
    }

    @discardableResult
    func replace(generation expected: UInt64? = nil, enqueue: () -> Void) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard expected == nil || generation == expected else { return false }
        generation &+= 1
        accounts = [:]
        enqueue()
        return true
    }

    /// A completed zero-enabled-account observation is authoritative without
    /// reading a stale index. Unknown identity is distinct and never clears.
    @discardableResult
    func publishEmptyInventory(enqueue: () -> Void) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let identities, identities.isEmpty else { return false }
        generation &+= 1
        accounts = [:]
        enqueue()
        return true
    }

    @discardableResult
    func commit(generation expected: UInt64, accounts next: [String: MailHistoryAccount],
                enqueue: () -> Void) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard generation == expected else { return false }
        accounts = next
        enqueue()
        return true
    }
}

// Pure bookkeeping for MailReader's AppleScript history walk. Kept free of
// AppKit and AppleScript so tests/MailHistoryChecks.swift can
// compile it on its own and exercise every merge rule with plain strings.
//
// WHY THIS EXISTS. Measured from the app's own call log: a 500-message
// history batch costs a flat ~19 s, a typical walk is ~16 batches, and the
// walk re-read the same two years from scratch every 30 minutes (934 walks in
// 22 days). Messages come back newest-first, so after one complete walk only
// the newest slice can have changed. An incremental walk reads back to just
// before the newest row it already has, and this file stitches that fresh
// slice onto the rows it already holds.
//
// What an incremental walk cannot see: read-flag changes and deletions among
// OLDER rows. That is why a full walk still runs on first use, after any
// account's walk failed or was interrupted, and at least every six hours.

/// One account's history as last posted, kept in memory between walks.
struct MailHistoryAccount: Equatable {
    var label: String
    var accountID: String
    /// H2 lines, newest-first.
    var rows: [String]
    /// Native attempted/skipped counts reported in this account's C2 marker.
    var attempted: Int
    var skipped: Int
    /// Skipped headers seen by the last FULL walk. An incremental walk only
    /// re-reads the newest slice, so its own skips are added to this base
    /// rather than to the running total (which would grow every tick).
    var skippedBase: Int
    /// The per-account safety ceiling was reached (history may be truncated).
    var capped: Bool
    /// nil = the last walk of this account finished. Otherwise the C3 reason
    /// ("failed" | "interrupted") that must be disclosed downstream.
    var incompleteReason: String?
    /// Unix time of the last FULL walk of this account that finished.
    var fullWalkAt: Double
}

/// What one AppleScript walk of one account produced.
struct MailHistoryWalk: Equatable {
    var label: String
    var accountID: String
    var rows: [String]
    var attempted: Int
    var skipped: Int
    var reachedCap: Bool
    /// nil when the walk finished; otherwise "failed" or "interrupted".
    var failure: String?
    /// nil = a full 730-day walk. Otherwise the unix-epoch boundary the walk
    /// stopped at: every message at or after it was re-read.
    var cutoff: Double?
}

enum HistoryMerge {
    static let maxAgeDays = 730
    /// Re-read one day before the newest row already held, so a message that
    /// lands with a slightly older date (delayed delivery, clock skew) is
    /// still picked up by the next incremental walk.
    static let marginSeconds: Double = 86_400
    /// Read flags and deletions only refresh on a full walk.
    static let fullRefreshSeconds: Double = 6 * 3600

    static func ageCutoff(now: Double) -> Double {
        now - Double(maxAgeDays) * 86_400
    }

    /// The boundary for this account's next walk, or nil for a FULL walk.
    static func incrementalCutoff(previous: MailHistoryAccount?, currentAccountID: String,
                                  now: Double) -> Double? {
        guard let previous,
              !currentAccountID.isEmpty, currentAccountID != "*",
              previous.accountID == currentAccountID,
              previous.incompleteReason == nil,
              previous.fullWalkAt > 0,
              now >= previous.fullWalkAt,
              now - previous.fullWalkAt < fullRefreshSeconds,
              let newest = newestEpoch(previous.rows) else { return nil }
        return max(newest - marginSeconds, ageCutoff(now: now))
    }

    /// Fold one walk into the account's previous state.
    static func merge(previous: MailHistoryAccount?, walk: MailHistoryWalk,
                      now: Double, cap: Int) -> MailHistoryAccount {
        // Display names can be reused after removing an account. Missing IDs
        // cannot authenticate carryover, including on an empty or failed read.
        var previous = !walk.accountID.isEmpty && walk.accountID != "*" &&
            previous?.accountID == walk.accountID ? previous : nil
        // Native identity authenticates a rename, too. Retained rows must use
        // its current label so another account cannot inherit the old name.
        if previous?.label != walk.label, var renamed = previous {
            renamed.label = walk.label
            renamed.rows = renamed.rows.map { row in
                var fields = row.components(separatedBy: "\u{01}")
                guard fields.count >= 10, fields[0] == "H2", fields[4] == walk.accountID else { return row }
                fields[3] = safe(walk.label, fallback: "Mail")
                return fields.joined(separator: "\u{01}")
            }
            previous = renamed
        }
        var walk = walk
        if walk.cutoff != nil, previous == nil { walk.failure = "failed" }
        let oldest = ageCutoff(now: now)
        let accountID = walk.accountID

        if let failure = walk.failure {
            // An aborted walk must never quietly shrink history: keep every
            // previous row the fresh partial read did not replace, and carry
            // the C3 marker so downstream disclosures say it is incomplete.
            let fresh = dedupe(walk.rows)
            let freshIDs = Set(fresh.map(identity))
            let kept = (previous?.rows ?? []).filter {
                epoch(of: $0) >= oldest && !freshIDs.contains(identity($0))
            }
            var rows = sortNewestFirst(fresh + kept)
            var capped = walk.reachedCap || (previous?.capped ?? false)
            if rows.count > cap { rows = Array(rows.prefix(cap)); capped = true }
            let skipped = (previous?.skippedBase ?? 0) + walk.skipped
            return MailHistoryAccount(
                label: walk.label, accountID: accountID, rows: rows,
                attempted: rows.count + skipped, skipped: skipped,
                skippedBase: previous?.skippedBase ?? 0, capped: capped,
                incompleteReason: failure, fullWalkAt: previous?.fullWalkAt ?? 0)
        }

        guard let cutoff = walk.cutoff, let previous else {
            // A finished full walk replaces everything it covers. (A bounded
            // walk with no previous state to stitch onto is not a full walk,
            // so it does not reset the six-hour clock.)
            let rows = sortNewestFirst(dedupe(walk.rows))
            return MailHistoryAccount(
                label: walk.label, accountID: accountID, rows: rows,
                attempted: max(walk.attempted, rows.count + walk.skipped),
                skipped: walk.skipped, skippedBase: walk.skipped,
                capped: walk.reachedCap, incompleteReason: nil,
                fullWalkAt: walk.cutoff == nil ? now : 0)
        }

        // Incremental: previous rows strictly older than the boundary stay;
        // everything at or after it is replaced by the fresh read (so a
        // message deleted in that window disappears, and duplicates collapse).
        let fresh = dedupe(walk.rows)
        let freshIDs = Set(fresh.map(identity))
        let kept = previous.rows.filter {
            let ts = epoch(of: $0)
            return ts < cutoff && ts >= oldest && !freshIDs.contains(identity($0))
        }
        var rows = sortNewestFirst(fresh + kept)
        var capped = walk.reachedCap || previous.capped
        if rows.count > cap { rows = Array(rows.prefix(cap)); capped = true }
        let skipped = previous.skippedBase + walk.skipped
        return MailHistoryAccount(
            label: walk.label, accountID: accountID, rows: rows,
            attempted: rows.count + skipped, skipped: skipped,
            skippedBase: previous.skippedBase, capped: capped,
            incompleteReason: nil, fullWalkAt: previous.fullWalkAt)
    }

    /// The history wire text: every H2 row newest-first across accounts, then
    /// the same C2/C3 markers the old one-shot walk emitted.
    static func render(_ accounts: [MailHistoryAccount]) -> String {
        var rows: [String] = []
        var markers: [String] = []
        for account in accounts {
            rows.append(contentsOf: account.rows)
            let label = safe(account.label, fallback: "Mail")
            let key = safe(account.accountID, fallback: "")
            if account.attempted > 0 {
                markers.append(["C2", label, key, String(account.attempted),
                                String(min(account.skipped, account.attempted)), "0"]
                    .joined(separator: "\u{01}"))
            }
            if account.capped {
                markers.append(["C2", label, key, "0", "0", "1"].joined(separator: "\u{01}"))
            }
            if let reason = account.incompleteReason {
                markers.append(["C3", label, key, reason].joined(separator: "\u{01}"))
            }
        }
        let lines = sortNewestFirst(rows) + markers
        return lines.isEmpty ? "" : lines.joined(separator: "\n") + "\n"
    }

    /// Fold one historyBatchScript result (its lines after the DONE/CONTINUE
    /// status line) into the walk being accumulated.
    static func absorb(_ batch: String, into walk: inout MailHistoryWalk) {
        for piece in batch.split(separator: "\n", omittingEmptySubsequences: true) {
            let line = String(piece).trimmingCharacters(in: CharacterSet(charactersIn: "\r"))
            let fields = line.components(separatedBy: "\u{01}")
            let observedID: String?
            if fields.first == "H2", fields.count >= 10 { observedID = fields[4] }
            else if fields.first == "C2", fields.count == 6 { observedID = fields[2] }
            else { observedID = nil }
            if let observedID, !walk.accountID.isEmpty, walk.accountID != "*",
               observedID != walk.accountID {
                walk.failure = "failed"
                continue
            }
            if line.hasPrefix("H2\u{01}") {
                walk.rows.append(line)
                if walk.accountID.isEmpty, fields.count > 4 { walk.accountID = fields[4] }
            } else if line.hasPrefix("C2\u{01}"), fields.count == 6,
                      let attempted = Int(fields[3]), let skipped = Int(fields[4]),
                      attempted >= 0, skipped >= 0 {
                walk.attempted += attempted
                walk.skipped += skipped
                if walk.accountID.isEmpty { walk.accountID = fields[2] }
            }
        }
    }

    /// The AppleScript expression for historyBatchScript's `cutoffSecs`: the
    /// fixed 730-day window for a full walk, or an explicit unix-epoch literal
    /// (written as a real, since AppleScript integers stop at 2^29) for an
    /// incremental one.
    static func appleScriptCutoff(_ cutoff: Double?) -> String {
        guard let cutoff, cutoff.isFinite, cutoff > 0 else {
            return "(((current date) - (\(maxAgeDays) * days)) - refDate) + 978307200"
        }
        return "\(Int64(cutoff.rounded(.down))).0"
    }

    // MARK: - Row helpers

    /// Leading epoch of an H2 row. AppleScript prints large reals in
    /// scientific notation ("1.785958494E+9"), which Double parses directly.
    static func epoch(of line: String) -> Double {
        guard line.hasPrefix("H2\u{01}") else { return 0 }
        let fields = line.split(separator: "\u{01}", maxSplits: 2, omittingEmptySubsequences: false)
        guard fields.count > 1 else { return 0 }
        return Double(fields[1].trimmingCharacters(in: .whitespaces)) ?? 0
    }

    static func newestEpoch(_ rows: [String]) -> Double? {
        rows.map(epoch(of:)).filter { $0 > 0 }.max()
    }

    /// Mail's native message id when present, else Message-ID + date, else
    /// the line itself.
    static func identity(_ line: String) -> String {
        let fields = line.components(separatedBy: "\u{01}")
        if fields.count >= 10, !fields[9].isEmpty { return "n:" + fields[9] }
        if fields.count >= 8, !fields[7].isEmpty { return "m:" + fields[7] + "|" + fields[1] }
        return "l:" + line
    }

    private static func dedupe(_ rows: [String]) -> [String] {
        var seen = Set<String>()
        return rows.filter { $0.hasPrefix("H2\u{01}") && seen.insert(identity($0)).inserted }
    }

    private static func sortNewestFirst(_ rows: [String]) -> [String] {
        rows.enumerated()
            .sorted { lhs, rhs in
                let a = epoch(of: lhs.element), b = epoch(of: rhs.element)
                return a != b ? a > b : lhs.offset < rhs.offset
            }
            .map(\.element)
    }

    private static func safe(_ value: String, fallback: String) -> String {
        value.contains("\u{01}") || value.contains("\n") || value.contains("\r") ? fallback : value
    }
}
