import Foundation

// Reads recent Mail.app inbox headers (timestamp | account | sender | subject)
// via AppleScript and pushes them to the backend, which can then summarize a
// specific day (not just "the last N messages"). Lives in the Swift app
// because Mail Automation (TCC) is per-process: NSAppleScript runs in-process
// as Wisp.app, so the prompt reads "Wisp wants to control Mail" and, once
// granted, actually works — unlike the separate Python backend.
//
// PER-ACCOUNT, not the unified inbox. Mail's application-level `inbox` is a
// CONCATENATION of each account's inbox, not a date-merged view: with two
// accounts linked, `messages of inbox` returns all 18K of account A
// (newest-first), and only then account B's. Every scan here is a top-N or a
// batched walk from index 1, so a second account sat entirely past the end of
// every scan and was invisible to Wisp — no headers, no raw bodies, and (since
// the history walk exits at the first message older than its 1-year cutoff)
// no history either. Scanning each account's own INBOX separately and merging
// by date is what makes a second account actually appear.
final class MailReader {
    private var timer: Timer?

    // 200 (not 25) so a full day's mail is actually covered, not just the
    // newest handful — a summary of "today" needs everything from today, not
    // a truncated top-N that might not even reach back that far. Applied PER
    // ACCOUNT: a quiet account (a school address) must not be crowded out of
    // the cache by a noisy one, and the backend filters by account AFTER
    // reading the cache (email_tools.py's `_filter_account`), so depth has to
    // exist per account for an account-scoped question to have anything to
    // answer from.
    private let headerLimit = 200

    // Safety CEILING (not the expected count) on total messages scanned per
    // account across all history batches — exists so a bad assumption
    // (unexpected message ordering, a huge mailbox) can't turn this into an
    // unbounded scan. Measured cost on this account's ~3.6K messages/year is a
    // few minutes total, which is why this runs on its own slow timer (see
    // syncHistory) instead of the 5-min cadence the recent scan uses.
    private let historyCap = 6000
    // Split into BATCHES (see historyBatchScript/syncHistory) rather than one
    // single call: a year's worth is a multi-minute scan on a large mailbox,
    // and a single NSAppleScript call has no way to report progress mid-run —
    // batching is what makes the live progress bar (SyncProgress.shared)
    // possible, not just a chunking convenience.
    private let historyBatchSize = 500

    // Full RAW content (`content of m`) is the SLOW AppleScript call — it
    // decodes the whole message body — so its scan is deliberately much
    // shallower than the header one. Budgeted across accounts rather than
    // applied per-account so linking a second account doesn't silently double
    // every 15-min sync: one account still gets the same 50 it always did, and
    // beyond that each account gets an equal share with a floor of 20 (enough
    // that a newly linked account is actually usable for verbatim lookups).
    private func rawLimit(accountCount n: Int) -> Int {
        guard n > 1 else { return 50 }
        return max(20, 50 / n)
    }

    // MARK: - AppleScript fragments

    // Mail account names are user-editable free text and land inside AppleScript
    // string literals below, so they have to be escaped — an account named with
    // a quote would otherwise produce a syntax error and silently drop that
    // account from every scan.
    private func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
         .replacingOccurrences(of: "\"", with: "\\\"")
    }

    // AppleScript dates don't give epoch seconds directly, and formatting via
    // "as string" is locale-dependent and painful to parse reliably. The
    // standard robust trick: build a reference date at a known moment
    // (2001-01-01, AppleScript's own epoch) via property assignment
    // (locale-independent, unlike parsing a date string literal), then subtract
    // to get a plain number of seconds, and add the fixed offset to Unix epoch
    // (1970-01-01) — 978307200s — so Python gets an ordinary POSIX timestamp
    // with zero parsing ambiguity.
    private let refDateSetup = """
        set refDate to current date
        set year of refDate to 2001
        set month of refDate to 1
        set day of refDate to 1
        set time of refDate to 0
    """

    // Resolve the message list to scan. `account == nil` means the legacy
    // unified inbox — kept as the fallback for when account enumeration itself
    // fails (see accountNames), so a broken enumeration degrades to the old
    // single-account-correct behaviour instead of syncing nothing at all.
    //
    // "INBOX" is the IMAP-standard name and what both Gmail and Google
    // Workspace accounts use; the "Inbox" retry covers account types that
    // localize/capitalize it differently. An account whose inbox resolves to
    // neither yields an empty list rather than aborting the whole sync.
    private func inboxSource(_ account: String?) -> String {
        guard let account else { return "        set theMessages to messages of inbox" }
        let n = esc(account)
        return """
                set theMessages to {}
                try
                    set theMessages to messages of (mailbox "INBOX" of account "\(n)")
                on error
                    try
                        set theMessages to messages of (mailbox "Inbox" of account "\(n)")
                    end try
                end try
        """
    }

    // Sets `acctName` for the current message `m`. When scanning a specific
    // account it's just the known name — cheaper AND always populated, unlike
    // the unified-inbox path's `account of (mailbox of m)` lookup, which is a
    // per-message round trip and comes back blank for an odd mailbox with no
    // normal account. Wrapped in its own try there so such a message drops its
    // account tag rather than dropping the whole row.
    private func acctNameFragment(_ account: String?) -> String {
        guard let account else {
            return """
                        set acctName to ""
                        try
                            set acctName to name of (account of (mailbox of m))
                        end try
            """
        }
        return "                set acctName to \"\(esc(account))\""
    }

    // Recent header scan for one account (or the unified inbox when nil).
    private func headerScript(account: String?, limit: Int) -> String {
        """
        tell application "Mail"
        \(refDateSetup)
            set output to ""
        \(inboxSource(account))
            set n to count of theMessages
            set lim to \(limit)
            if n < lim then set lim to n
            repeat with i from 1 to lim
                set m to item i of theMessages
                try
                    set epochSecs to ((date received of m) - refDate) + 978307200
        \(acctNameFragment(account))
                    set output to output & epochSecs & " | " & acctName & " | " & (extract name from sender of m) & " | " & (subject of m) & linefeed
                end try
            end repeat
            return output
        end tell
        """
    }

    // One history batch: scan messages [start, start+count) of one account's
    // inbox for the header-only history cache, stopping early at the 1-year
    // cutoff. Messages come back newest-first (verified against a real
    // 18K-message inbox, and again per-account), so once one falls older than
    // the cutoff everything after it is older too — the cutoff check is a real
    // optimization, not just a safety net, since it means this only walks the
    // messages actually received in the last year rather than the whole mailbox.
    //
    // Returns "DONE"|"CONTINUE" as the first line (whether the caller should
    // keep going) followed by this batch's header lines — encoding both in one
    // string return keeps this a plain NSAppleScript string result instead of
    // needing multi-value AppleEventDescriptor parsing.
    private func historyBatchScript(account: String?, start: Int, count: Int) -> String {
        """
        tell application "Mail"
        \(refDateSetup)
            set cutoffSecs to (((current date) - (365 * days)) - refDate) + 978307200
        \(inboxSource(account))
            set n to count of theMessages
            set startIdx to \(start)
            set endIdx to startIdx + \(count) - 1
            if endIdx > n then set endIdx to n
            if startIdx > n then return "DONE" & linefeed
            set output to ""
            set stoppedEarly to false
            repeat with i from startIdx to endIdx
                set m to item i of theMessages
                try
                    set epochSecs to ((date received of m) - refDate) + 978307200
                    if epochSecs < cutoffSecs then
                        set stoppedEarly to true
                        exit repeat
                    end if
        \(acctNameFragment(account))
                    set output to output & epochSecs & " | " & acctName & " | " & (extract name from sender of m) & " | " & (subject of m) & linefeed
                end try
            end repeat
            if stoppedEarly or endIdx >= n then
                return "DONE" & linefeed & output
            else
                return "CONTINUE" & linefeed & output
            end if
        end tell
        """
    }

    // Second pass: full RAW content (account, sender address, recipient
    // addresses, subject, body) for view_emails — verbatim lookups (order
    // numbers, pickup times, party details), not the summarize_emails digest.
    // Unlike headers, this deliberately does NOT extend to a year (a year of
    // full bodies is impractical both in scan time and in what the backend
    // would have to hold/transmit at once).
    //
    // FS/RS are control characters (SOH / STX), not "|" or linefeed — a raw
    // email body routinely contains both, which would otherwise corrupt the
    // record boundaries the header format gets away with because subjects
    // don't.
    private func rawScript(account: String?, limit: Int) -> String {
        """
        tell application "Mail"
        \(refDateSetup)
            set FS to ASCII character 1
            set RS to ASCII character 2
            set output to ""
        \(inboxSource(account))
            set n to count of theMessages
            set lim to \(limit)
            if n < lim then set lim to n
            repeat with i from 1 to lim
                set m to item i of theMessages
                try
                    set epochSecs to ((date received of m) - refDate) + 978307200
        \(acctNameFragment(account))
                    set toAddrs to {}
                    repeat with r in (to recipients of m)
                        set end of toAddrs to (address of r)
                    end repeat
                    set oldDelims to AppleScript's text item delimiters
                    set AppleScript's text item delimiters to ", "
                    set toLine to toAddrs as string
                    set AppleScript's text item delimiters to oldDelims
                    set output to output & epochSecs & FS & acctName & FS & (sender of m) & FS & toLine & FS & (subject of m) & FS & (content of m) & RS
                end try
            end repeat
            return output
        end tell
        """
    }

    // Enabled accounts, one name per line. Cheap (accounts, not messages), and
    // it drives every scan below — a disabled account is skipped here rather
    // than left to fail per-mailbox deeper in.
    private let accountsScript = """
    tell application "Mail"
        set output to ""
        repeat with a in accounts
            try
                if enabled of a then set output to output & (name of a) & linefeed
            end try
        end repeat
        return output
    end tell
    """

    // The user's OWN account addresses. Identity ground truth for the profile
    // builder and for every second-person answer: a mailbox is full of other
    // people's names and addresses, and without knowing which addresses are the
    // user's it misattributed a stranger's details to them. Cheap (accounts,
    // not messages), so it rides along with the header sync.
    //
    // Coerced with `as string` under a linefeed delimiter rather than iterated
    // element-by-element. `repeat with e in (email addresses of a)` looks
    // equivalent but is NOT: each loop variable comes back as an unresolved
    // specifier (`item 1 of «class emad» of item 1 of every «class mact»`) that
    // can't coerce to text, so the loop raised -1700 on its FIRST address and
    // the enclosing try swallowed it — this script returned an empty string on
    // every sync, and no account addresses ever reached the backend. Setting
    // the delimiter matters too: the default "" would run an account's multiple
    // addresses together into one unparseable line.
    private let identityScript = """
    tell application "Mail"
        set output to ""
        set oldDelims to AppleScript's text item delimiters
        set AppleScript's text item delimiters to linefeed
        repeat with a in accounts
            try
                set output to output & ((email addresses of a) as string) & linefeed
            end try
        end repeat
        set AppleScript's text item delimiters to oldDelims
        return output
    end tell
    """

    private var rawTimer: Timer?
    private var historyTimer: Timer?
    // Re-entrancy guard: the history scan can take a few minutes on a large
    // mailbox — and now scales with the number of linked accounts — so a
    // stacked/overlapping run (e.g. a slow scan still going when its own next
    // timer tick fires) would mean two concurrent multi-minute AppleScript
    // calls fighting over Mail.app. This just skips the tick instead.
    private var historyInFlight = false

    // MARK: - Helpers

    /// Run a script, returning its string result, or nil (with the error code)
    /// when it failed. Every scan below goes through this so a failure is
    /// always distinguishable from a legitimately empty result — posting an
    /// empty string on failure would wipe a good cache.
    private func run(_ source: String) -> (text: String?, code: Int) {
        guard let s = NSAppleScript(source: source) else { return (nil, 0) }
        var err: NSDictionary?
        let result = s.executeAndReturnError(&err)
        if let err {
            return (nil, (err[NSAppleScript.errorNumber] as? Int) ?? 0)
        }
        return (result.stringValue ?? "", 0)
    }

    /// Enabled account names. Empty means enumeration failed OR there are no
    /// accounts; callers treat empty as "fall back to the unified inbox", which
    /// is exactly the old behaviour and stays correct for a single account.
    private func accountNames() -> [String] {
        let (text, _) = run(accountsScript)
        guard let text else { return [] }
        return text.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    /// Leading epoch-seconds field of a scan line. AppleScript emits these in
    /// scientific notation ("1.785958494E+9"), which Double parses directly.
    private func epoch(_ field: Substring) -> Double {
        Double(field.trimmingCharacters(in: .whitespaces)) ?? 0
    }

    /// Merge per-account "epochSecs | account | sender | subject" chunks into
    /// one newest-first stream. Sorting is REQUIRED, not cosmetic: the backend's
    /// `summarize_inbox_recent` takes the first N lines as "the most recent
    /// messages", so simply concatenating accounts would make the newest N mean
    /// "the newest N of whichever account happened to be scanned first".
    private func mergeHeaderChunks(_ chunks: [String]) -> String {
        let lines = chunks
            .flatMap { $0.split(separator: "\n", omittingEmptySubsequences: true) }
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        guard !lines.isEmpty else { return "" }
        let sorted = lines.sorted { epoch($0.split(separator: "|", maxSplits: 1)[0])
                                  > epoch($1.split(separator: "|", maxSplits: 1)[0]) }
        return sorted.joined(separator: "\n") + "\n"
    }

    /// Same merge for raw records, which are RS-separated with FS-separated
    /// fields rather than one-per-line.
    private func mergeRawChunks(_ chunks: [String]) -> String {
        let RS = "\u{02}", FS = "\u{01}"
        let records = chunks
            .flatMap { $0.components(separatedBy: RS) }
            .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        guard !records.isEmpty else { return "" }
        let sorted = records.sorted {
            epoch(Substring($0.components(separatedBy: FS)[0]))
                > epoch(Substring($1.components(separatedBy: FS)[0]))
        }
        return sorted.joined(separator: RS) + RS
    }

    // MARK: - Lifecycle

    func start() {
        sync()
        syncRaw()
        // Beat the backend-startup race, then settle into a 5-min cadence for
        // headers (cheap, needed for accurate day-summaries). Raw content gets
        // the SAME warm-up retries — not just the one immediate call above —
        // because on a cold launch Mail.app may not be running yet, or the
        // Automation permission dialog may not be dismissed yet, so that first
        // attempt can silently fail. Headers recover on their next 5-min tick
        // regardless; raw content's own tick is 15 minutes away, so without
        // these retries a failed cold start left it empty until then — these
        // warm-up attempts close that gap immediately instead of waiting out
        // the full interval.
        for d in [3.0, 8.0, 15.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in
                self?.sync()
                self?.syncRaw()
            }
        }
        // The history scan gets its OWN single warm-up attempt, not the fast
        // ramp above — it can take a few minutes, so firing it 3 times in
        // quick succession (like the cheap scans above) would risk stacking
        // multiple long-running AppleScript calls against Mail.app at once.
        DispatchQueue.main.asyncAfter(deadline: .now() + 10.0) { [weak self] in
            self?.syncHistory()
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in
                self?.sync()
            }
            // Raw content (full addresses + body — real PII) syncs every 15
            // minutes. This costs no meaningful disk storage: the backend
            // cache is in-memory only and each sync REPLACES it wholesale
            // (service/tools/email_tools.py's cache_raw_emails), never
            // appends — so nothing accumulates regardless of frequency. The
            // backend's cache TTL is separate (see _RAW_TTL_SECONDS) and now
            // set to a week, well past this interval, so the cache should
            // rarely go cold between syncs.
            self?.rawTimer = Timer.scheduledTimer(withTimeInterval: 900, repeats: true) { _ in
                self?.syncRaw()
            }
            // History is a multi-minute scan on a large mailbox, so it gets a
            // much slower cadence than the 5-min recent one — "up to a year
            // old" doesn't need near-real-time freshness the way "today's
            // mail" does.
            self?.historyTimer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { _ in
                self?.syncHistory()
            }
        }
    }

    // MARK: - Scans

    func sync() {
        // NSAppleScript is synchronous and can be slow — run off the main thread.
        DispatchQueue.global(qos: .utility).async { [weak self] in
            // `tell application "Mail"` auto-launches Mail if it isn't
            // running — skip entirely while Super Model is active, which
            // deliberately quit it to free memory. See SuperModelState.
            guard !SuperModelState.shared.active else { return }
            guard let self else { return }

            // nil = unified-inbox fallback when enumeration failed. One scan
            // per account otherwise.
            let names = self.accountNames()
            let targets: [String?] = names.isEmpty ? [nil] : names.map { $0 }

            var chunks: [String] = []
            var lastFailureCode = 0
            var anySucceeded = false
            for target in targets {
                guard !SuperModelState.shared.active else { return }
                let (text, code) = self.run(self.headerScript(account: target,
                                                              limit: self.headerLimit))
                guard let text else { lastFailureCode = code; continue }
                anySucceeded = true
                chunks.append(text)
            }

            // Every account failed — usually Automation not granted
            // (errAEEventNotPermitted, -1743) or Mail not running. Report it so
            // the backend's "no data" message can say WHY instead of always
            // blaming permission. Do NOT post headers (nothing to post, and it
            // must not wipe the existing cache). A PARTIAL failure (one account
            // unreadable, another fine) still counts as available: mail is
            // flowing, and claiming otherwise would send the user to a
            // permission screen that isn't the problem.
            guard anySucceeded else {
                let reason = lastFailureCode == -1743
                    ? "Mail Automation access isn't granted"
                    : "couldn't reach Mail (is it running?)"
                self.postDiagnostic(available: false, reason: reason)
                return
            }
            // Ran fine — permission is OK even if the inbox scan was empty.
            self.post(headers: self.mergeHeaderChunks(chunks))
            self.postDiagnostic(available: true, reason: "")
            self.syncIdentity()
        }
    }

    private func syncIdentity() {
        // Re-checked rather than relying on sync()'s entry guard: the header
        // scans that run before this can take long enough for Super Model to
        // engage in between, and this is another `tell application "Mail"`.
        guard !SuperModelState.shared.active else { return }
        let (text, _) = run(identityScript)
        guard let text else { return }
        let emails = text
            .split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { $0.contains("@") }
        guard !emails.isEmpty else { return }
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/emails")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["identity_emails": emails])
        WispSession.shared.dataTask(with: req).resume()
    }

    func syncRaw() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard !SuperModelState.shared.active else { return }
            guard let self else { return }
            let names = self.accountNames()
            let targets: [String?] = names.isEmpty ? [nil] : names.map { $0 }
            let limit = self.rawLimit(accountCount: targets.count)

            var chunks: [String] = []
            for target in targets {
                guard !SuperModelState.shared.active else { return }
                // Mail not running or Automation not granted — skip this
                // account rather than the whole sync.
                let (text, _) = self.run(self.rawScript(account: target, limit: limit))
                guard let text else { continue }
                chunks.append(text)
            }
            self.post(raw: self.mergeRawChunks(chunks))
        }
    }

    func syncHistory() {
        guard !historyInFlight else { return }
        historyInFlight = true
        Task { @MainActor in SyncProgress.shared.mailHistoryFraction = 0 }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            defer {
                self?.historyInFlight = false
                Task { @MainActor in
                    SyncProgress.shared.mailHistoryFraction = nil
                    SyncProgress.shared.mailHistoryLastSynced = Date()
                }
            }
            guard !SuperModelState.shared.active else { return }
            guard let self else { return }
            let names = self.accountNames()
            let targets: [String?] = names.isEmpty ? [nil] : names.map { $0 }

            var chunks: [String] = []
            for (idx, target) in targets.enumerated() {
                var start = 1
                while start <= self.historyCap {
                    // Re-check per batch, not just at entry: this loop runs for
                    // minutes on a large mailbox, so a scan already in flight
                    // when Super Model engages would keep issuing `tell
                    // application "Mail"` — relaunching Mail seconds after
                    // AppQuitter quit it, over and over. Bail mid-scan instead;
                    // the next timer tick restarts it once Super Model is off.
                    guard !SuperModelState.shared.active else {
                        self.post(history: self.mergeHeaderChunks(chunks))
                        return
                    }
                    let (text, _) = self.run(self.historyBatchScript(
                        account: target, start: start, count: self.historyBatchSize))
                    guard let text else { break }  // Mail unreachable for this account
                    let parts = text.split(separator: "\n", maxSplits: 1,
                                           omittingEmptySubsequences: false)
                    guard let status = parts.first else { break }
                    if parts.count > 1 { chunks.append(String(parts[1])) }

                    // Progress spans all accounts, so a two-account sync doesn't
                    // run the bar to 100% and then start over.
                    let scanned = min(start + self.historyBatchSize - 1, self.historyCap)
                    let within = min(1.0, Double(scanned) / Double(self.historyCap))
                    let fraction = (Double(idx) + within) / Double(targets.count)
                    Task { @MainActor in SyncProgress.shared.mailHistoryFraction = fraction }

                    if status.trimmingCharacters(in: .whitespacesAndNewlines) == "DONE" { break }
                    start += self.historyBatchSize
                }
            }
            self.post(history: self.mergeHeaderChunks(chunks))
        }
    }

    // MARK: - Posting

    private func post(headers: String) {
        guard !headers.isEmpty else { return }
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/emails")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["headers": headers])
        WispSession.shared.dataTask(with: req).resume()
    }

    private func post(raw: String) {
        guard !raw.isEmpty else { return }
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/emails")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["raw": raw])
        WispSession.shared.dataTask(with: req).resume()
    }

    private func post(history: String) {
        guard !history.isEmpty else { return }
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/emails")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["history": history])
        WispSession.shared.dataTask(with: req).resume()
    }

    // Report whether the last inbox read actually succeeded (permission), sent
    // on EVERY sync — including empty or failed ones that carry no headers — so
    // the backend's "no data" message reflects real state instead of always
    // implying permission is missing.
    private func postDiagnostic(available: Bool, reason: String) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/emails")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "diagnostics": ["available": available, "reason": reason],
        ])
        WispSession.shared.dataTask(with: req).resume()
    }
}
