// Pure-logic checks for MailReader's incremental history walk. No Mail, no
// AppleScript, no files, no network: every input is a synthetic H2 string.
// swiftc -parse-as-library -swift-version 5 \
//   app/Sources/WispApp/MailHistoryMerge.swift tests/MailHistoryChecks.swift -o <bin>
import Foundation

// Catch every request before DNS or sockets. This exercises the injected
// production transport, not a copied posting implementation.
final class CapturedHistoryProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var captured: [URLRequest] = []
    static var requests: [URLRequest] {
        lock.lock(); defer { lock.unlock() }; return captured
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var copy = request
        if copy.httpBody == nil, let stream = copy.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(contentsOf: buffer.prefix(count))
            }
            copy.httpBody = data
        }
        Self.lock.lock(); Self.captured.append(copy); Self.lock.unlock()
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200,
                            httpVersion: "HTTP/1.1", headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
enum MailHistoryChecks {
    static var passed = 0

    static func check(_ condition: Bool, _ message: String) {
        precondition(condition, message)
        passed += 1
    }

    static func row(_ ts: Double, _ id: String, account: String = "Work",
                    accountID: String = "UUID-W", read: String = "R",
                    subject: String = "Subject") -> String {
        ["H2", String(Int64(ts)), read, account, accountID, "Sender", "s@example.test",
         "<\(id)@example.test>", subject, "mail:\(id)"].joined(separator: "\u{01}")
    }

    static func walk(_ rows: [String], cutoff: Double?, failure: String? = nil,
                     skipped: Int = 0, reachedCap: Bool = false,
                     label: String = "Work", accountID: String = "UUID-W") -> MailHistoryWalk {
        MailHistoryWalk(label: label, accountID: accountID, rows: rows,
                        attempted: rows.count + skipped, skipped: skipped,
                        reachedCap: reachedCap, failure: failure, cutoff: cutoff)
    }

    static func lines(_ text: String) -> [[String]] {
        text.split(separator: "\n").map { $0.components(separatedBy: "\u{01}") }
    }

    static func main() {
        let now: Double = 1_790_000_000
        let day: Double = 86_400
        let cap = 12_000

        // Empty previous: the first walk is always FULL.
        check(HistoryMerge.incrementalCutoff(previous: nil, currentAccountID: "UUID-W", now: now) == nil,
              "No previous history must force a full walk")
        let first = HistoryMerge.merge(
            previous: nil,
            walk: walk([row(now - 100, "a"), row(now - 3 * day, "b"), row(now - 40 * day, "c")],
                       cutoff: nil),
            now: now, cap: cap)
        check(first.rows.count == 3 && first.incompleteReason == nil && first.fullWalkAt == now,
              "A finished full walk records its rows and its full-walk time")

        if CommandLine.arguments.contains("replacement") {
            let replacement = HistoryMerge.merge(previous: first,
                walk: walk([row(now - 5, "new", accountID: "UUID-NEW")], cutoff: now - day,
                           accountID: "UUID-NEW"), now: now + 60, cap: cap)
            check(!replacement.rows.contains { $0.contains("UUID-W") },
                  "A replacement account must never inherit the former account's rows")
            return
        }

        // Incremental boundary = newest previous row minus one day.
        let later = now + 1800
        let cutoff = HistoryMerge.incrementalCutoff(previous: first, currentAccountID: "UUID-W", now: later)
        check(cutoff == now - 100 - day, "Boundary is the newest held row minus the 1-day margin")
        check(HistoryMerge.appleScriptCutoff(cutoff) == "\(Int64(now - 100 - day)).0",
              "An explicit cutoff is passed to AppleScript as a real epoch literal")
        check(HistoryMerge.appleScriptCutoff(nil).contains("730 * days"),
              "A full walk keeps the fixed 730-day cutoff")

        // Merge: fresh rows at/after the boundary replace previous ones, older
        // rows survive, duplicates collapse, and a deleted message disappears.
        let boundary = cutoff!
        let previous = HistoryMerge.merge(
            previous: nil,
            walk: walk([row(now - 100, "a", read: "U"), row(now - 200, "deleted"),
                        row(boundary, "at-boundary"), row(now - 3 * day, "b"),
                        row(now - 40 * day, "c")], cutoff: nil),
            now: now, cap: cap)
        let incremental = HistoryMerge.merge(
            previous: previous,
            walk: walk([row(later - 5, "new"), row(now - 100, "a", read: "R"),
                        row(now - 100, "a", read: "R"), row(boundary, "at-boundary")],
                       cutoff: boundary),
            now: later, cap: cap)
        let ids = incremental.rows.map { $0.components(separatedBy: "\u{01}")[9] }
        check(ids == ["mail:new", "mail:a", "mail:at-boundary", "mail:b", "mail:c"],
              "Incremental merge keeps old rows, replaces the window, dedupes: \(ids)")
        check(incremental.rows[1].components(separatedBy: "\u{01}")[2] == "R",
              "A re-read row in the window takes the fresh read flag")
        check(!ids.contains("mail:deleted"),
              "A message gone from the re-read window is dropped")
        check(incremental.fullWalkAt == now && incremental.incompleteReason == nil,
              "An incremental walk does not reset the six-hour full-walk clock")

        // Boundary handling: a previous row exactly AT the boundary is
        // replaced by the fresh read (or dropped if the fresh read lacks it);
        // a row one second older survives untouched.
        let edge = HistoryMerge.merge(
            previous: HistoryMerge.merge(previous: nil,
                                         walk: walk([row(boundary + 10, "n"), row(boundary, "edge"),
                                                     row(boundary - 1, "older")], cutoff: nil),
                                         now: now, cap: cap),
            walk: walk([row(boundary + 10, "n")], cutoff: boundary), now: later, cap: cap)
        check(edge.rows.map { $0.components(separatedBy: "\u{01}")[9] } == ["mail:n", "mail:older"],
              "Rows at the boundary belong to the fresh window; older rows are kept")

        // Six-hour rule.
        check(HistoryMerge.incrementalCutoff(previous: first, currentAccountID: "UUID-W", now: now + 6 * 3600) == nil,
              "Six hours after the last full walk, walk in full again")
        check(HistoryMerge.incrementalCutoff(previous: first, currentAccountID: "UUID-W", now: now + 6 * 3600 - 1) != nil,
              "Inside six hours an incremental walk is allowed")
        check(HistoryMerge.incrementalCutoff(previous: first, currentAccountID: "UUID-W", now: now - 10) == nil,
              "A clock that moved backwards forces a full walk")

        // A failed account keeps its previous rows AND is marked incomplete.
        let failed = HistoryMerge.merge(
            previous: previous,
            walk: walk([row(later - 5, "new")], cutoff: boundary, failure: "failed"),
            now: later, cap: cap)
        check(failed.rows.count == previous.rows.count + 1,
              "A failed walk must not drop the account's previous rows")
        check(failed.incompleteReason == "failed",
              "A failed walk is marked incomplete")
        check(HistoryMerge.incrementalCutoff(previous: failed, currentAccountID: "UUID-W", now: later + 60) == nil,
              "After a failed walk the next walk is full")
        let failedText = HistoryMerge.render([failed])
        check(lines(failedText).contains { $0 == ["C3", "Work", "UUID-W", "failed"] },
              "Failure renders the same C3 marker as before")

        // Interrupted with no previous rows (first walk aborted after one
        // batch): partial rows + C3 interrupted, exactly like the old walk.
        let interrupted = HistoryMerge.merge(
            previous: nil, walk: walk([row(now - 5, "p")], cutoff: nil, failure: "interrupted",
                                      accountID: ""),
            now: now, cap: cap)
        let interruptedLines = lines(HistoryMerge.render([interrupted]))
        check(interruptedLines.first?[0] == "H2" &&
              interruptedLines.contains { $0[0] == "C3" && $0[3] == "interrupted" },
              "An interrupted first walk posts its partial rows with an interrupted marker")
        check(HistoryMerge.incrementalCutoff(previous: interrupted, currentAccountID: "UUID-W", now: now + 60) == nil,
              "An interrupted walk forces a full walk next time")

        // An aborted walk never posts SHORTER history without an incomplete
        // marker: an empty failed walk keeps every previous row.
        let emptyFailure = HistoryMerge.merge(previous: previous,
                                              walk: walk([], cutoff: nil, failure: "failed"),
                                              now: later, cap: cap)
        let emptyFailureRows = lines(HistoryMerge.render([emptyFailure])).filter { $0[0] == "H2" }
        check(emptyFailureRows.count == previous.rows.count &&
              emptyFailure.incompleteReason == "failed",
              "A walk that failed on its first batch keeps every previous row, marked incomplete")

        // Cap markers: a previous cap persists through incremental merges,
        // and a merge that exceeds the ceiling trims oldest-first and caps.
        let capped = HistoryMerge.merge(previous: nil,
                                        walk: walk([row(now - 1, "x"), row(now - 2, "y")],
                                                   cutoff: nil, reachedCap: true),
                                        now: now, cap: 2)
        check(lines(HistoryMerge.render([capped])).contains { $0 == ["C2", "Work", "UUID-W", "0", "0", "1"] },
              "A capped full walk renders the history cap marker")
        let stillCapped = HistoryMerge.merge(previous: capped,
                                             walk: walk([row(later, "z")], cutoff: now - 0.5),
                                             now: later, cap: 2)
        check(stillCapped.capped && stillCapped.rows.count == 2 &&
              stillCapped.rows.map { $0.components(separatedBy: "\u{01}")[9] } == ["mail:z", "mail:x"],
              "Merging past the ceiling keeps the newest rows and stays capped")

        // Rows older than the 730-day window age out of retained history.
        let aging = HistoryMerge.merge(
            previous: HistoryMerge.merge(previous: nil,
                                         walk: walk([row(now - 1, "fresh"),
                                                     row(now - 729.9 * day, "ancient")], cutoff: nil),
                                         now: now, cap: cap),
            walk: walk([row(now + day, "new")], cutoff: now - 1 - day),
            now: now + day, cap: cap)
        check(!aging.rows.contains { $0.contains("mail:ancient") },
              "Retained rows past the 730-day window are dropped")

        // C2 coverage semantics: attempted >= skipped, skipped preserved from
        // the full walk and not compounded by repeated incremental walks.
        let skippedFull = HistoryMerge.merge(previous: nil,
                                             walk: walk([row(now - 1, "s1")], cutoff: nil, skipped: 2),
                                             now: now, cap: cap)
        var rolling = skippedFull
        for i in 1...4 {
            let t = now + Double(i) * 1800
            rolling = HistoryMerge.merge(
                previous: rolling,
                walk: walk([row(now - 1, "s1")], cutoff: HistoryMerge.incrementalCutoff(previous: rolling, currentAccountID: "UUID-W", now: t),
                           skipped: 1),
                now: t, cap: cap)
        }
        let rollingC2 = lines(HistoryMerge.render([rolling])).first { $0[0] == "C2" && $0[5] == "0" }!
        check(rollingC2[4] == "3" && Int(rollingC2[3])! >= 3,
              "Skips are the full walk's plus the latest window's, never compounded: \(rollingC2)")

        // Rendering across accounts is a global newest-first merge with the
        // markers after every row.
        let home = HistoryMerge.merge(
            previous: nil,
            walk: walk([row(now - 50, "h1", account: "Home", accountID: "UUID-H")], cutoff: nil,
                       label: "Home", accountID: "UUID-H"),
            now: now, cap: cap)
        let combined = lines(HistoryMerge.render([first, home]))
        let h2 = combined.filter { $0[0] == "H2" }.map { Double($0[1])! }
        check(h2 == h2.sorted(by: >), "Rows from several accounts are globally newest-first")
        check(combined.firstIndex { $0[0] != "H2" }! == h2.count,
              "Coverage markers follow the rows")
        check(HistoryMerge.render([]) == "", "A successful empty snapshot renders an empty update")

        // Parsing a raw batch result.
        var parsed = MailHistoryWalk(label: "Work", accountID: "", rows: [], attempted: 0,
                                     skipped: 0, reachedCap: false, failure: nil, cutoff: nil)
        HistoryMerge.absorb([row(now - 1, "q"), row(now - 2, "r"),
                             ["C2", "Work", "UUID-W", "3", "1", "0"].joined(separator: "\u{01}")]
            .joined(separator: "\n") + "\n", into: &parsed)
        check(parsed.rows.count == 2 && parsed.attempted == 3 && parsed.skipped == 1 &&
              parsed.accountID == "UUID-W", "A batch's rows and C2 counts are absorbed")
        check(HistoryMerge.epoch(of: ["H2", "1.785958494E+9", "R"].joined(separator: "\u{01}")) == 1_785_958_494,
              "AppleScript scientific-notation epochs parse")

        let replacedID = "UUID-NEW"
        check(HistoryMerge.incrementalCutoff(previous: first, currentAccountID: replacedID, now: later) == nil,
              "Replacement identity forces a full read before any cutoff is chosen")
        check(HistoryMerge.incrementalCutoff(previous: first, currentAccountID: "", now: later) == nil,
              "Missing current identity cannot authenticate an incremental read")
        var missingPreviousID = first
        missingPreviousID.accountID = ""
        check(HistoryMerge.incrementalCutoff(previous: missingPreviousID, currentAccountID: "UUID-W", now: later) == nil,
              "Missing previous identity cannot authenticate an incremental read")
        let newFull = HistoryMerge.merge(previous: first,
            walk: walk([row(now - 30 * day, "older-new", accountID: replacedID)], cutoff: nil,
                       accountID: replacedID), now: later, cap: cap)
        check(newFull.rows.count == 1 && newFull.rows[0].contains("older-new") &&
              !newFull.rows[0].contains("UUID-W") && newFull.fullWalkAt == later,
              "Replacement full read covers NEW rows older than the OLD overlap window")
        let newFailure = HistoryMerge.merge(previous: first,
            walk: walk([row(later, "new-partial", accountID: replacedID)], cutoff: nil,
                       failure: "failed", accountID: replacedID), now: later, cap: cap)
        check(newFailure.rows.count == 1 && newFailure.incompleteReason == "failed" &&
              newFailure.fullWalkAt == 0 && !HistoryMerge.render([newFailure]).contains("UUID-W"),
              "A replacement failure retains only NEW partial rows and C3")
        let unknown = HistoryMerge.merge(previous: first,
            walk: walk([], cutoff: nil, failure: "interrupted", accountID: ""), now: later, cap: cap)
        check(unknown.rows.isEmpty && unknown.accountID.isEmpty && unknown.incompleteReason != nil,
              "Unknown identity never inherits the previous account's ID or rows")
        let renamed = HistoryMerge.merge(previous: first,
            walk: walk([], cutoff: boundary, failure: "failed", label: "Office"), now: later, cap: cap)
        check(renamed.rows.count == first.rows.count && renamed.rows.allSatisfy {
            $0.components(separatedBy: "\u{01}")[3] == "Office"
        } && renamed.accountID == "UUID-W" && renamed.incompleteReason == "failed",
              "Same-ID rename preserves failed-walk rows under the verified current label")
        let mismatchedCutoff = HistoryMerge.merge(previous: first,
            walk: walk([], cutoff: boundary, accountID: replacedID), now: later, cap: cap)
        check(mismatchedCutoff.rows.isEmpty && mismatchedCutoff.incompleteReason == "failed",
              "A defensively rejected narrow read cannot claim complete history")
        let completeEmpty = HistoryMerge.merge(previous: first,
            walk: walk([], cutoff: nil), now: later, cap: cap)
        check(completeEmpty.rows.isEmpty && completeEmpty.accountID == "UUID-W" &&
              completeEmpty.incompleteReason == nil && HistoryMerge.render([completeEmpty]).isEmpty,
              "A known-ID successful empty full read clears previous rows")
        var changingBatch = walk([], cutoff: boundary)
        HistoryMerge.absorb(row(later, "wrong", accountID: replacedID), into: &changingBatch)
        check(changingBatch.rows.isEmpty && changingBatch.failure == "failed" && changingBatch.accountID == "UUID-W",
              "A different identity observed during the walk fails without relabeling its rows")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [CapturedHistoryProtocol.self]
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.urlCredentialStorage = nil
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let endpoint = URL(string: "https://fixture.invalid/assistant/sync/emails")!
        for history in [HistoryMerge.render([first]), ""] {
            let done = DispatchSemaphore(value: 0)
            let before = CapturedHistoryProtocol.requests.count
            MailHistoryTransport.post(history, to: endpoint, session: session) { done.signal() }
            check(done.wait(timeout: .now() + 5) == .success, "Captured production transport completes")
            let requests = CapturedHistoryProtocol.requests
            check(requests.count == before + 1, "Every successful snapshot, including empty, sends exactly one update")
            let request = requests.last!
            check(request.httpMethod == "POST" && request.url == endpoint &&
                  request.value(forHTTPHeaderField: "Content-Type") == "application/json", "History request routing is preserved")
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
            check(body == ["history": history], "The JSON history key preserves an explicit empty value")
        }
        let state = MailHistoryState()
        var enqueued = 0
        let initial = state.capture()
        check(state.commit(generation: initial.generation, accounts: ["UUID-W": first]) { enqueued += 1 },
              "A current captured walk can commit and enqueue")
        let staleWalk = state.capture()
        state.replace { enqueued += 1 }
        check(!state.commit(generation: staleWalk.generation, accounts: staleWalk.accounts) { enqueued += 1 },
              "A replacement snapshot invalidates an already captured walk")
        check(state.capture().accounts.isEmpty && enqueued == 2,
              "A stale walk neither restores rows nor enqueues a request")
        let freshWalk = state.capture()
        check(state.commit(generation: freshWalk.generation, accounts: [replacedID: newFull]) { enqueued += 1 },
              "A fresh post-replacement walk can commit")
        check(state.capture().accounts == [replacedID: newFull] && enqueued == 3,
              "The new account snapshot survives the stale-walk interleaving")
        state.observe([replacedID: "Work"])
        let beforeIdentityChange = state.capture()
        state.observe(["UUID-THIRD": "Work"])
        check(!state.commit(generation: beforeIdentityChange.generation,
                            accounts: beforeIdentityChange.accounts) { enqueued += 1 } &&
              state.capture().accounts.isEmpty && enqueued == 3,
              "An account replacement fences captured walks and removes obsolete native state")
        check(!state.replace(generation: beforeIdentityChange.generation) { enqueued += 1 } && enqueued == 3,
              "An index read captured before identity changed cannot enqueue a stale snapshot")
        let known = state.capture()
        let third = HistoryMerge.merge(previous: nil,
            walk: walk([row(later, "third", accountID: "UUID-THIRD")], cutoff: nil,
                       accountID: "UUID-THIRD"), now: later, cap: cap)
        check(state.commit(generation: known.generation, accounts: ["UUID-THIRD": third]) {},
              "Known identity can hold a snapshot")
        state.observe(nil)
        check(state.capture().accounts.count == 1 &&
              !state.commit(generation: known.generation, accounts: [:]) { enqueued += 1 } && enqueued == 3,
              "Unavailable identity preserves prior rows while fencing an in-flight walk")
        var emptyUpdates = 0
        check(!state.publishEmptyInventory { emptyUpdates += 1 } && emptyUpdates == 0 &&
              state.capture().accounts.count == 1,
              "Unknown identity cannot publish an empty inventory or drop prior rows")
        state.observe(["UUID-THIRD": "Work"])
        check(!state.publishEmptyInventory { emptyUpdates += 1 } && emptyUpdates == 0,
              "A nonempty enabled-account inventory cannot publish an empty update")
        let beforeEmptyInventory = state.capture()
        state.observe([:])
        check(state.capture().identities == [:] && state.capture().accounts.isEmpty,
              "Completed zero-account enumeration removes obsolete native account state")
        check(state.capture().lastKnownEmpty,
              "Known empty inventory prevents a later closed-index read from restoring old rows")
        check(!state.commit(generation: beforeEmptyInventory.generation,
                            accounts: beforeEmptyInventory.accounts) { emptyUpdates += 1 } && emptyUpdates == 0,
              "A zero-account observation fences an older captured walk")
        check(!state.replace(generation: beforeEmptyInventory.generation) { emptyUpdates += 1 } && emptyUpdates == 0,
              "A zero-account observation fences an older local-index snapshot")
        let emptyDone = DispatchSemaphore(value: 0)
        let beforeEmptyRequest = CapturedHistoryProtocol.requests.count
        check(state.publishEmptyInventory {
            emptyUpdates += 1
            MailHistoryTransport.post("", to: endpoint, session: session) { emptyDone.signal() }
        }, "Known empty inventory publishes under the production state fence")
        check(emptyDone.wait(timeout: .now() + 5) == .success && emptyUpdates == 1 &&
              CapturedHistoryProtocol.requests.count == beforeEmptyRequest + 1,
              "Known empty inventory enqueues exactly one captured production update")
        let emptyInventoryBody = CapturedHistoryProtocol.requests.last!.httpBody!
        check((try! JSONSerialization.jsonObject(with: emptyInventoryBody) as! [String: String]) == ["history": ""],
              "Known zero-account update carries an explicit empty history field")
        let emptySnapshotDone = DispatchSemaphore(value: 0)
        let beforeSnapshot = CapturedHistoryProtocol.requests.count
        check(state.publishEmptyInventory {
            MailHistoryTransport.postEmptyAccounts(to: endpoint, readSource: "mail_app", session: session) {
                emptySnapshotDone.signal()
            }
        }, "Current zero-account header snapshot is guarded by the production state fence")
        check(emptySnapshotDone.wait(timeout: .now() + 5) == .success &&
              CapturedHistoryProtocol.requests.count == beforeSnapshot + 1,
              "Zero-account headers and history enqueue one compound update")
        let emptySnapshotBody = CapturedHistoryProtocol.requests.last!.httpBody!
        let snapshot = try! JSONSerialization.jsonObject(with: emptySnapshotBody) as! [String: Any]
        let diagnostics = snapshot["diagnostics"] as! [String: Any]
        check(snapshot["headers"] as? String == "" && snapshot["history"] as? String == "" &&
              diagnostics["available"] as? Bool == true && diagnostics["syncing"] as? Bool == false &&
              diagnostics["read_source"] as? String == "mail_app",
              "Empty headers, history and readiness travel together in the production request")
        state.observe(nil)
        check(state.capture().lastKnownEmpty && !state.publishEmptyInventory { emptyUpdates += 1 } && emptyUpdates == 1,
              "Unavailable metadata preserves the last known empty boundary without another clearing update")
        state.observe(["UUID-THIRD": "Work"])
        check(!state.capture().lastKnownEmpty && !state.publishEmptyInventory { emptyUpdates += 1 } && emptyUpdates == 1,
              "A successful nonempty observation releases the closed-index empty boundary")
        if let flag = CommandLine.arguments.firstIndex(of: "--wire-output"),
           flag + 1 < CommandLine.arguments.count {
            let path = CommandLine.arguments[flag + 1]
            check(path.hasPrefix("/private/tmp/wisp-pr142-"), "Wire output stays in the synthetic artifact directory")
            let fixtures = ["failed_history": HistoryMerge.render([emptyFailure]),
                            "replacement_history": HistoryMerge.render([newFull]),
                            "positive_post": String(data: CapturedHistoryProtocol.requests[0].httpBody!, encoding: .utf8)!,
                            "empty_post": String(data: CapturedHistoryProtocol.requests[1].httpBody!, encoding: .utf8)!,
                            "empty_inventory_post": String(data: emptyInventoryBody, encoding: .utf8)!,
                            "empty_accounts_snapshot": String(data: emptySnapshotBody, encoding: .utf8)!]
            try! JSONSerialization.data(withJSONObject: fixtures).write(to: URL(fileURLWithPath: path))
        }
        print("MailHistoryMerge: \(passed) checks passed")
    }
}
