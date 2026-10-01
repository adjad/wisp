// Pure-logic checks for MailReader's incremental history walk. No Mail, no
// AppleScript, no files, no network: every input is a synthetic H2 string.
// swiftc -parse-as-library -swift-version 5 \
//   app/Sources/WispApp/MailHistoryMerge.swift tests/MailHistoryChecks.swift -o <bin>
import Foundation

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
        check(HistoryMerge.incrementalCutoff(previous: nil, now: now) == nil,
              "No previous history must force a full walk")
        let first = HistoryMerge.merge(
            previous: nil,
            walk: walk([row(now - 100, "a"), row(now - 3 * day, "b"), row(now - 40 * day, "c")],
                       cutoff: nil),
            now: now, cap: cap)
        check(first.rows.count == 3 && first.incompleteReason == nil && first.fullWalkAt == now,
              "A finished full walk records its rows and its full-walk time")

        // Incremental boundary = newest previous row minus one day.
        let later = now + 1800
        let cutoff = HistoryMerge.incrementalCutoff(previous: first, now: later)
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
        check(HistoryMerge.incrementalCutoff(previous: first, now: now + 6 * 3600) == nil,
              "Six hours after the last full walk, walk in full again")
        check(HistoryMerge.incrementalCutoff(previous: first, now: now + 6 * 3600 - 1) != nil,
              "Inside six hours an incremental walk is allowed")
        check(HistoryMerge.incrementalCutoff(previous: first, now: now - 10) == nil,
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
        check(HistoryMerge.incrementalCutoff(previous: failed, now: later + 60) == nil,
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
        check(HistoryMerge.incrementalCutoff(previous: interrupted, now: now + 60) == nil,
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
                walk: walk([row(now - 1, "s1")], cutoff: HistoryMerge.incrementalCutoff(previous: rolling, now: t),
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
        check(HistoryMerge.render([]) == "", "No accounts renders nothing (nothing is posted)")

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

        print("MailHistoryMerge: \(passed) checks passed")
    }
}
