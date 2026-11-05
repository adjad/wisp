import Foundation

// Offline checks for app/Sources/WispApp/ReminderActionLedger.swift. Every reminder here
// is an in-memory fixture and every file is in a temp directory: nothing opens EventKit,
// so nothing can create or edit a real reminder.

private var checks = 0
private func check(_ condition: @autoclosure () -> Bool, _ message: String, line: Int = #line) {
    checks += 1
    if !condition() {
        print("FAILED line \(line): \(message)")
        exit(1)
    }
}

private let day = "reminder:59d6b4875bd1844a67b54472432e20e8"
private let start = Date(timeIntervalSince1970: 1_900_000_000.25)
private let target = ReminderCreateTarget(title: "Fixture: read chapter 3", dueTs: 1_900_100_000,
                                          dueDate: "", kind: "exam")
private let dueMinute = Int(1_900_100_000.0 / 60)

private func row(_ id: String, title: String = "Fixture: read chapter 3", notes: String? = nil,
                 list: String? = "list-a", completed: Bool = false) -> ReminderRow {
    ReminderRow(id: id, externalID: "ext-" + id, title: title, notes: notes, dueMinute: dueMinute,
                allDayDate: nil, alarmCount: 1, isCompleted: completed, listID: list,
                otherFacts: ["due", "alarm"])
}

private struct Boom: Error {}

/// A Reminders stand-in: `rows` is what a fetch would return, `saves` how many saves ran.
private final class FakeStore {
    var rows: [ReminderRow] = []
    var saves = 0
    func save(_ made: ReminderRow) -> (id: String, externalID: String?) {
        saves += 1
        rows.append(made)
        return (made.id, made.externalID)
    }
}

@main struct Checks {
    static var scratch = URL(fileURLWithPath: "/")
    static var counter = 0

    static func newPath() -> URL {
        counter += 1
        return scratch.appendingPathComponent("case-\(counter)/ledger.json")
    }

    /// What a relaunch sees: only what reached the disk.
    static func relaunch(_ path: URL) -> ReminderActionLedger { ReminderActionLedger(path: path) }

    static func main() {
        scratch = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory())
            .appendingPathComponent("wisp-ledger-\(getpid())")
        defer { try? FileManager.default.removeItem(at: scratch) }
        newRemindersCarryNoMarker()
        lostReplyAfterIDIsProven()
        crashAfterSaveIsFoundByThePlannedID()
        crashBeforeSaveStaysUnknown()
        aChangedIDLeavesOnlyAnHonestGap()
        identicalRemindersAreNeverClaimed()
        oldMarkerRemindersStillReconcile()
        kindsSurviveWithoutMarkers()
        ledgerFile()
        markerParsing()
        cleanup()
        damagedLedger()
        print("reminder action ledger: \(checks) checks passed")
    }

    static func newRemindersCarryNoMarker() {
        let path = newPath(), ledger = relaunch(path), store = FakeStore()
        let outcome = ReminderCreationFlow.run(ledger: ledger, actionID: day, target: target, plannedID: "ek-1",
            now: start, save: { store.save(row("ek-1")) }, readback: { id in store.rows.first { $0.id == id } })
        guard case .created(let made) = outcome else { return check(false, "creation succeeds: \(outcome)") }
        check(made.notes == nil && store.rows[0].notes == nil, "a new reminder has no notes")
        check(store.saves == 1, "one save")
        check(relaunch(path).entry(day)?.sourceID == "ek-1", "the ID is durable after the save")

        // A readback that shows any note text is not a verified creation.
        let other = newPath(), ledger2 = relaunch(other), store2 = FakeStore()
        let marked = ReminderCreationFlow.run(ledger: ledger2, actionID: day, target: target, plannedID: "ek-1", now: start,
            save: { store2.save(row("ek-1", notes: "Wisp action: \(day)\nWisp kind: exam")) },
            readback: { id in store2.rows.first { $0.id == id } })
        check(marked == .uncertain("Created reminder could not be read back"), "text in the notes is not verified")

        // The intent is durable BEFORE the save, and refusal means nothing was written.
        let order = newPath(), ledger3 = relaunch(order), store3 = FakeStore()
        _ = ReminderCreationFlow.run(ledger: ledger3, actionID: day, target: target, plannedID: "ek-1", now: start,
            save: {
                check(relaunch(order).entry(day)?.sourceID == "ek-1",
                      "the intent and the ID reach the disk before EventKit is touched")
                return store3.save(row("ek-1"))
            }, readback: { id in store3.rows.first { $0.id == id } })

        let blocked = scratch.appendingPathComponent("not-a-directory")
        try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        let plainFile = open(blocked.path, O_WRONLY | O_CREAT, 0o600)
        check(plainFile >= 0, "fixture: a plain file")
        close(plainFile)
        let unwritable = ReminderActionLedger(path: blocked.appendingPathComponent("ledger.json"))
        let store4 = FakeStore()
        let refused = ReminderCreationFlow.run(ledger: unwritable, actionID: day, target: target, plannedID: "ek-1", now: start,
            save: { store4.save(row("ek-1")) }, readback: { _ in nil })
        guard case .refused = refused else { return check(false, "no durable intent, no save: \(refused)") }
        check(store4.saves == 0, "nothing is saved without a durable intent")
    }

    static func lostReplyAfterIDIsProven() {
        let path = newPath(), ledger = relaunch(path), store = FakeStore()
        // The save and the ledger both land; the readback (and so the reply) doesn't.
        let outcome = ReminderCreationFlow.run(ledger: ledger, actionID: day, target: target, plannedID: "ek-1",
            now: start, save: { store.save(row("ek-1")) }, readback: { _ in nil })
        check(outcome == .uncertain("Created reminder could not be read back"), "no readback stays uncertain")

        let after = relaunch(path)
        let hit = ReminderRecovery.find(actionID: day, target: target, rows: store.rows, ledger: after)
        check(hit?.row.id == "ek-1" && hit?.proof == .exactID, "the exact ID finds it")
        check(store.rows.count == 1 && store.saves == 1, "recovery only reads")

        // A retry of the same action never saves a second time.
        let retry = ReminderCreationFlow.run(ledger: after, actionID: day, target: target, plannedID: "ek-1", now: start,
            save: { store.save(row("ek-2")) }, readback: { _ in nil })
        check(retry == .uncertain("Reminder creation remains uncertain"), "a started action reports uncertain")
        check(store.saves == 1 && store.rows.count == 1, "no duplicate")

        // An identifier EventKit changed in a full sync: same reminder, new ID.
        var moved = row("ek-1b")
        moved.externalID = "ext-ek-1"
        check(ReminderRecovery.find(actionID: day, target: target, rows: [moved], ledger: after)?.row.id == "ek-1b",
              "the external ID is a second chance")
        check(after.entry(sourceID: "ek-1b", externalID: "ext-ek-1")?.kind == "exam", "and keeps the kind")

        // The person edited it since: recovery does not claim it.
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("ek-1", title: "Edited")], ledger: after) == nil,
              "changed facts stay uncertain")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [], ledger: after) == nil,
              "absent stays uncertain")
        let wrongKind = ReminderCreateTarget(title: target.title, dueTs: target.dueTs, dueDate: "", kind: "meeting")
        check(ReminderRecovery.find(actionID: day, target: wrongKind, rows: store.rows, ledger: after) == nil,
              "another kind stays uncertain")
    }

    static func crashAfterSaveIsFoundByThePlannedID() {
        // The app dies inside save: the row exists, and nothing after the save ran.
        let path = newPath(), ledger = relaunch(path), store = FakeStore()
        let outcome = ReminderCreationFlow.run(ledger: ledger, actionID: day, target: target, plannedID: "ek-1", now: start,
            save: { _ = store.save(row("ek-1")); throw Boom() }, readback: { _ in nil })
        check(outcome == .uncertain("Native reminder creation outcome is unknown"), "a thrown save is unknown")
        let after = relaunch(path)
        let hit = ReminderRecovery.find(actionID: day, target: target, rows: store.rows, ledger: after)
        check(hit?.row.id == "ek-1" && hit?.proof == .exactID, "the ID recorded before the save finds it")
        check(store.saves == 1 && store.rows.count == 1, "no duplicate")
        check(after.entry(sourceID: "ek-1", externalID: nil)?.kind == "exam", "the kind is known too")
    }

    static func crashBeforeSaveStaysUnknown() {
        let path = newPath(), ledger = relaunch(path)
        check(ledger.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start) == .recorded, "intent recorded")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [], ledger: relaunch(path)) == nil,
              "no reminder, no success")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("someone-elses")], ledger: relaunch(path)) == nil,
              "an identical reminder with another ID is not this one")
        let unknownAction = "reminder:ffffffffffffffffffffffffffffffff"
        check(ReminderRecovery.find(actionID: unknownAction, target: target, rows: [row("ek-1")], ledger: relaunch(path)) == nil,
              "an action the ledger never saw isn't claimed")
    }

    static func aChangedIDLeavesOnlyAnHonestGap() {
        // A store that assigned a different ID at save: the ID after the save replaces the planned one.
        let path = newPath(), ledger = relaunch(path), store = FakeStore()
        let outcome = ReminderCreationFlow.run(ledger: ledger, actionID: day, target: target, plannedID: "planned", now: start,
            save: { store.save(row("assigned")) }, readback: { id in store.rows.first { $0.id == id } })
        guard case .created = outcome else { return check(false, "creation succeeds: \(outcome)") }
        check(relaunch(path).entry(day)?.sourceID == "assigned", "the real ID replaces the planned one")
        check(ReminderRecovery.find(actionID: day, target: target, rows: store.rows, ledger: relaunch(path))?.row.id == "assigned",
              "and finds it")

        // Dying between that save and the record stays unknown: never a guess.
        let gap = newPath(), gapLedger = relaunch(gap), gapStore = FakeStore()
        _ = ReminderCreationFlow.run(ledger: gapLedger, actionID: day, target: target, plannedID: "planned", now: start,
            save: { _ = gapStore.save(row("assigned")); throw Boom() }, readback: { _ in nil })
        check(ReminderRecovery.find(actionID: day, target: target, rows: gapStore.rows, ledger: relaunch(gap)) == nil,
              "the outcome stays unknown")

        // An empty planned ID behaves the same way.
        let none = newPath(), noneLedger = relaunch(none)
        check(noneLedger.begin(actionID: day, kind: "exam", plannedID: "", at: start) == .recorded, "recorded")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("a")], ledger: relaunch(none)) == nil, "not claimed")
    }

    static func identicalRemindersAreNeverClaimed() {
        // Two creations of the same reminder each find their own.
        let ledger = relaunch(newPath())
        let sibling = "reminder:11111111111111111111111111111111"
        _ = ledger.begin(actionID: day, kind: "exam", plannedID: "a", at: start)
        _ = ledger.begin(actionID: sibling, kind: "exam", plannedID: "b", at: start)
        let both = [row("b"), row("a")]
        check(ReminderRecovery.find(actionID: day, target: target, rows: both, ledger: ledger)?.row.id == "a", "the first")
        check(ReminderRecovery.find(actionID: sibling, target: target, rows: both, ledger: ledger)?.row.id == "b", "the second")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("b")], ledger: ledger) == nil,
              "the other's reminder is not a stand-in")

        // All-day creations match on the date, with no alarm.
        let allDay = ReminderCreateTarget(title: target.title, dueTs: 0, dueDate: "2030-03-17", kind: "reminder")
        var dated = row("d")
        dated.dueMinute = 42
        dated.allDayDate = "2030-03-17"
        check(!allDay.matches(dated), "an all-day reminder has no alarm")
        dated.alarmCount = 0
        check(allDay.matches(dated), "the all-day facts match")
        dated.allDayDate = nil
        check(!target.matches(dated), "a timed target never matches another minute")
        var otherDue = row("a")
        otherDue.dueMinute = dueMinute + 1
        check(!target.matches(otherDue), "another due minute")
    }

    static func oldMarkerRemindersStillReconcile() {
        let ledger = relaunch(newPath())
        let notes = "Wisp action: \(day)\nWisp kind: exam"
        let found = ReminderRecovery.find(actionID: day, target: target, rows: [row("old", notes: notes)], ledger: ledger)
        check(found?.row.id == "old" && found?.proof == .legacyMarker, "the old marker is proof")
        let withText = "\(notes)\nbring the blue folder"
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("old", notes: withText)], ledger: ledger) != nil,
              "text under the marker doesn't hide it")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("a", notes: notes), row("b", notes: notes)], ledger: ledger) == nil,
              "a copied marker is ambiguous, as before")
        let meeting = "Wisp action: \(day)\nWisp kind: meeting"
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("old", notes: meeting)], ledger: ledger) == nil,
              "another kind stays uncertain")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("old", title: "Edited", notes: notes)], ledger: ledger) == nil,
              "changed facts stay uncertain")
    }

    static func kindsSurviveWithoutMarkers() {
        check(ReminderMarkerNotes.legacyKind("Wisp action: x\nWisp kind: assignment") == "assignment", "old kind is read")
        check(ReminderMarkerNotes.legacyKind("Wisp action: x\nWisp kind: nonsense") == "reminder", "an unknown old kind is a reminder")
        check(ReminderMarkerNotes.legacyKind("Wisp action: x") == "reminder", "a marker with no kind is a reminder")
        check(ReminderMarkerNotes.legacyKind("my notes\nWisp kind: exam") == nil, "not a marker")
        check(ReminderMarkerNotes.legacyKind(nil) == nil, "no notes")

        let path = newPath(), ledger = relaunch(path), store = FakeStore()
        for kind in ["reminder", "assignment", "exam", "meeting", "event"] {
            let made = ReminderCreateTarget(title: target.title, dueTs: target.dueTs, dueDate: "", kind: kind)
            _ = ReminderCreationFlow.run(ledger: ledger, actionID: "reminder:" + kind, target: made, plannedID: "ek-" + kind, now: start,
                save: { store.save(row("ek-" + kind)) }, readback: { id in store.rows.first { $0.id == id } })
            check(relaunch(path).entry(sourceID: "ek-" + kind, externalID: nil)?.kind == kind, "\(kind) survives a relaunch")
        }
        check(ledger.entry(sourceID: "someone-elses", externalID: nil) == nil, "a reminder Wisp didn't make has no entry")
    }

    static func ledgerFile() {
        let path = newPath(), ledger = relaunch(path)
        _ = ledger.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start)
        let bytes = String(decoding: (try? Data(contentsOf: path)) ?? Data(), as: UTF8.self)
        check(!bytes.isEmpty && !bytes.contains("chapter"), "the title isn't kept in the ledger")
        let mode = (try? FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions]) as? Int
        check(mode == 0o600, "owner only")
        let names = (try? FileManager.default.contentsOfDirectory(atPath: path.deletingLastPathComponent().path)) ?? []
        check(names == ["ledger.json"], "no temp file left: \(names)")

        // The size bound drops the oldest entries.
        let bounded = ReminderActionLedger(path: newPath(), maxEntries: 8)
        for index in 0..<11 {
            let id = "reminder:\(index)"
            _ = bounded.adopt(actionID: id, kind: "exam", sourceID: "ek-\(index)", externalID: nil,
                              at: Date(timeIntervalSince1970: 100 + Double(index)))
        }
        check(bounded.entry("reminder:0") == nil, "the oldest entry went")
        check(bounded.entry("reminder:10") != nil, "the newest stayed")
        check(bounded.entry(sourceID: "ek-0", externalID: nil) == nil, "the lookup follows")
    }

    static func markerParsing() {
        let head = "Wisp action: \(day)\nWisp kind: exam"
        func cleaned(_ notes: String) -> String?? { ReminderMarkerNotes.exact(notes).map(\.cleaned) }
        check(ReminderMarkerNotes.exact(head) == .init(actionID: day, kind: "exam", cleaned: nil), "marker only")
        check(cleaned(head + "\n") == .some(nil), "a lone newline after it goes with the marker")
        check(cleaned(head + "\nfolder") == .some("folder"), "text under it stays")

        // Every other character stays, byte for byte.
        for text in ["  two spaces, tab\t, trailing \n\n", "\nleading newline", "🧑🏽‍🏫 e\u{301} ﬁ", "win\r\nline", "Wisp action: mine",
                     "Wisp kind: exam\nWisp action: reminder:abc", "\u{200B}"] {
            let got = cleaned(head + "\n" + text)
            check(got.flatMap({ $0 }).map { Array($0.utf8) } == Array(text.utf8), "kept exactly: \(text.debugDescription)")
        }

        // Anything that isn't exactly Wisp's two lines at the start is left alone.
        for notes in ["", "plain", "Wisp action: \(day)", "note\n" + head, "\n" + head,
                      "Wisp action: \(day)\r\nWisp kind: exam", "Wisp action: \(day)\nWisp kind: exam\r",
                      "wisp action: \(day)\nWisp kind: exam", "Wisp action:  \(day)\nWisp kind: exam",
                      "Wisp action: \(day) \nWisp kind: exam", "Wisp action: \nWisp kind: exam",
                      "Wisp action: \(day)\nWisp kind: homework", "Wisp action: \(day)\nWisp kind: exam ",
                      "Wisp action: \(day)\nnote\nWisp kind: exam", "Wisp kind: exam\nWisp action: \(day)",
                      "Wisp action: has space\nWisp kind: exam", "Wisp action: é\nWisp kind: exam"] {
            check(ReminderMarkerNotes.exact(notes) == nil, "left alone: \(notes.debugDescription)")
        }
        check(ReminderMarkerNotes.exact(nil) == nil, "no notes")
        let tooLong = String(repeating: "a", count: 129)
        check(ReminderMarkerNotes.exact("Wisp action: \(tooLong)\nWisp kind: exam") == nil, "an impossible action ID")
    }

    static func cleanup() {
        let head = "Wisp action: \(day)\nWisp kind: exam"
        let user = "bring the blue folder 📁\n\n  second paragraph "

        // Which reminders it may touch.
        let lists: Set<String> = ["list-a"]
        check(ReminderMarkerCleanup.candidates([row("a", notes: head)], writableLists: lists).map(\.id) == ["a"], "an exact marker")
        check(ReminderMarkerCleanup.candidates([row("a", notes: head, list: "read-only")], writableLists: lists).isEmpty, "a read-only list")
        check(ReminderMarkerCleanup.candidates([row("a", notes: head, list: nil)], writableLists: lists).isEmpty, "no list")
        check(ReminderMarkerCleanup.candidates([row("a", notes: "plain"), row("b")], writableLists: lists).isEmpty, "not Wisp's")
        check(ReminderMarkerCleanup.candidates([row("a", notes: head), row("b", notes: head + "\nmine")], writableLists: lists).isEmpty,
              "a copy of the same action is left alone, both of them")
        check(ReminderMarkerCleanup.candidates([row("a", notes: head), row("b", notes: "Wisp action: \(day)")], writableLists: lists).isEmpty,
              "so is one whose copy no longer matches exactly")

        // Marker only, then marker above the person's text.
        for (notes, want) in [(head, nil), (head + "\n" + user, user)] as [(String, String?)] {
            let path = newPath(), ledger = relaunch(path)
            var stored = row("a", notes: notes)
            var writes = 0
            func write(_ expected: ReminderRow, _ next: String?) -> ReminderMarkerCleanup.WriteResult {
                check(relaunch(path).entry(day)?.sourceID == "a", "the ledger has it before the marker goes")
                writes += 1
                stored.notes = next
                return .saved(stored)
            }
            let before = stored
            check(ReminderMarkerCleanup.clean(stored, ledger: ledger, write: write) == .cleaned, "cleaned")
            check(stored.notes == want, "only Wisp's lines went")
            var expected = before
            expected.notes = want
            check(stored == expected, "nothing else about it changed")
            check(writes == 1, "one write")

            // Re-running does nothing: it no longer matches.
            check(ReminderMarkerCleanup.candidates([stored], writableLists: lists).isEmpty, "a cleaned reminder isn't a candidate")
            check(ReminderMarkerCleanup.clean(stored, ledger: ledger, write: write) == .skipped, "safe to re-run")
            check(writes == 1, "no second write")

            // Recovery and the kind still work with the marker gone, after a relaunch.
            let after = relaunch(path)
            check(ReminderRecovery.find(actionID: day, target: target, rows: [stored], ledger: after)?.proof == .exactID,
                  "the old action still reconciles through the ledger")
            check(after.entry(sourceID: "a", externalID: nil)?.kind == "exam", "the kind is kept")
        }

        // An interrupted cleanup (ledger written, save never ran) simply runs again.
        let again = relaunch(newPath()), original = row("a", notes: head)
        check(ReminderMarkerCleanup.clean(original, ledger: again, write: { _, _ in .failed }) == .stopForNow, "a failed save stops the pass")
        check(!again.cleanupHalted, "and can try again")
        var retried = original
        retried.notes = nil
        check(ReminderMarkerCleanup.clean(original, ledger: again, write: { _, _ in .saved(retried) }) == .cleaned, "the retry works")

        // Changed since it was read: nothing is written.
        let moved = relaunch(newPath())
        check(ReminderMarkerCleanup.clean(original, ledger: moved, write: { _, _ in .changedSinceRead }) == .skipped, "skipped")
        check(!moved.cleanupHalted, "not a fault")

        // A readback that differs anywhere stops every future run, across a relaunch.
        func halts(_ change: (inout ReminderRow) -> Void) -> Bool {
            let path = newPath(), ledger = relaunch(path)
            var seen = original
            seen.notes = nil
            change(&seen)
            let step = ReminderMarkerCleanup.clean(original, ledger: ledger, write: { _, _ in .saved(seen) })
            guard step == .halted, relaunch(path).cleanupHalted else { return false }
            let written = ReminderMarkerCleanup.clean(row("b", notes: "Wisp action: other\nWisp kind: exam"), ledger: relaunch(path),
                                                      write: { _, _ in .failed })
            return written == .skipped
        }
        check(halts { $0.title = "Changed" }, "a changed title")
        check(halts { $0.dueMinute = 1 }, "a changed due time")
        check(halts { $0.isCompleted = true }, "a changed completion")
        check(halts { $0.alarmCount = 0 }, "a lost alarm")
        check(halts { $0.listID = "list-b" }, "a changed list")
        check(halts { $0.otherFacts = ["due"] }, "another fact")
        check(halts { $0.notes = "leftover" }, "wrong notes")

        // An empty string back from EventKit is the same as no notes.
        let empty = relaunch(newPath())
        var blank = original
        blank.notes = ""
        check(ReminderMarkerCleanup.clean(original, ledger: empty, write: { _, _ in .saved(blank) }) == .cleaned, "empty is none")

        // An action the ledger already has on another reminder isn't handed over.
        let taken = relaunch(newPath())
        _ = taken.adopt(actionID: day, kind: "exam", sourceID: "other", externalID: nil, at: start)
        var wrote = false
        check(ReminderMarkerCleanup.clean(original, ledger: taken, write: { _, _ in wrote = true; return .failed }) == .skipped, "skipped")
        check(!wrote, "nothing written")
    }
    static func write(_ bytes: Data, to path: URL, mode: mode_t = 0o644) {
        try? FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        // Plain open and write: FileManager.createFile fails inside the gate's sandbox.
        let fd = open(path.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        check(fd >= 0, "fixture: \(path.lastPathComponent)")
        _ = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
        chmod(path.path, mode)
    }

    static func damagedCopy(_ path: URL) -> URL {
        path.deletingLastPathComponent().appendingPathComponent(ReminderActionLedger.damagedFileName)
    }

    static func damagedLedger() {
        let head = "Wisp action: \(day)\nWisp kind: exam"

        // Whatever kind of damage: a new reminder still goes through, and the bytes are kept.
        for bytes in [Data("not json".utf8), Data(), Data("[]".utf8), Data("{\"entries\":".utf8),
                      Data([0xFF, 0xFE, 0x00, 0x80]), Data("{\"version\":1}".utf8)] {
            let path = newPath()
            write(bytes, to: path)
            let ledger = relaunch(path), store = FakeStore()
            check(ledger.readable, "a damaged file doesn't stop the ledger")
            let made = ReminderCreationFlow.run(ledger: ledger, actionID: day, target: target, plannedID: "ek-1", now: start,
                save: { store.save(row("ek-1")) }, readback: { id in store.rows.first { $0.id == id } })
            guard case .created = made else { return check(false, "a new reminder still works: \(made)") }
            check(store.saves == 1, "one save")
            check((try? Data(contentsOf: damagedCopy(path))) == bytes, "the damaged bytes are kept as they were")
            let mode = (try? FileManager.default.attributesOfItem(atPath: damagedCopy(path).path)[.posixPermissions]) as? Int
            check(mode == 0o600, "the copy is owner-only, whatever it was before")
            let after = relaunch(path)
            check(after.readable && after.entry(day)?.sourceID == "ek-1", "the new ledger is a real one")
            check(after.entry(sourceID: "ek-1", externalID: nil)?.kind == "exam", "and keeps kinds from here on")
            let names = (try? FileManager.default.contentsOfDirectory(atPath: path.deletingLastPathComponent().path))?.sorted() ?? []
            check(names == ["ledger.json", ReminderActionLedger.damagedFileName].sorted(), "two files and no temp: \(names)")
        }

        // A later damaged file replaces the earlier copy: one copy to erase, never a pile.
        let twice = newPath()
        write(Data("first".utf8), to: twice)
        _ = relaunch(twice)
        write(Data("second".utf8), to: twice)
        _ = relaunch(twice)
        check((try? Data(contentsOf: damagedCopy(twice))) == Data("second".utf8), "the newest copy stays")

        // A folder where the copy goes doesn't stop it either.
        let folder = newPath()
        write(Data("bad".utf8), to: folder)
        try? FileManager.default.createDirectory(at: damagedCopy(folder), withIntermediateDirectories: true)
        check(relaunch(folder).readable, "the folder is replaced")
        check((try? Data(contentsOf: damagedCopy(folder))) == Data("bad".utf8), "by the copy")

        // A link is moved as it is: what it points at keeps its bytes and its mode.
        let linked = newPath(), target_ = scratch.appendingPathComponent("link-target-\(counter)")
        write(Data("not json".utf8), to: target_)
        try? FileManager.default.createDirectory(at: linked.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? FileManager.default.createSymbolicLink(at: linked, withDestinationURL: target_)
        check(relaunch(linked).readable, "a link to junk is set aside")
        let linkMode = (try? FileManager.default.attributesOfItem(atPath: target_.path)[.posixPermissions]) as? Int
        check(linkMode == 0o644 && (try? Data(contentsOf: target_)) == Data("not json".utf8), "its target is untouched")

        // Nothing on an existing reminder changes hands after the ledger is lost.
        let lost = newPath()
        let before = relaunch(lost)
        _ = before.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start)
        before.resolve(actionID: day, sourceID: "ek-1", externalID: "ext-ek-1")
        write(Data("damaged".utf8), to: lost)
        let fresh = relaunch(lost)
        check(fresh.entry(day) == nil, "the old record is gone")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("ek-1")], ledger: fresh) == nil,
              "a reply lost earlier stays unknown: it isn't claimed from identical facts")
        check(fresh.entry(sourceID: "ek-1", externalID: "ext-ek-1") == nil, "its kind reads as a plain reminder")
        check(ReminderRecovery.find(actionID: day, target: target, rows: [row("old", notes: head)], ledger: fresh)?.proof == .legacyMarker,
              "an old reminder still proves itself from its own notes")
        check(ReminderMarkerNotes.legacyKind(head) == "exam", "and keeps its kind")

        // Cleanup stays off after damage, across relaunches: the file may have said to stop.
        let off = newPath()
        let flagged = relaunch(off)
        flagged.haltCleanup()
        write(Data("damaged".utf8), to: off)
        for ledger in [relaunch(off), relaunch(off)] {
            check(ledger.cleanupHalted, "cleanup stays off")
            var wrote = false
            let step = ReminderMarkerCleanup.clean(row("a", notes: head), ledger: ledger, write: { _, _ in wrote = true; return .failed })
            check(step == .skipped && !wrote, "no reminder is touched")
            check(ledger.entry(day) == nil, "and nothing is adopted")
        }

        // A missing ledger is a first run: cleanup still does only its exact job, and still stops itself.
        let missing = relaunch(newPath()), text = "bring the blue folder 📁"
        check(!missing.cleanupHalted, "a missing ledger isn't damage")
        var cleaned = row("a", notes: head + "\n" + text)
        let step = ReminderMarkerCleanup.clean(cleaned, ledger: missing, write: { _, notes in
            check(notes == text, "only the two exact lines go")
            cleaned.notes = notes
            return .saved(cleaned)
        })
        check(step == .cleaned && cleaned.notes == text, "cleaned")
        for notes in ["note\n" + head, "\n" + head, "Wisp action: \(day)\r\nWisp kind: exam", "plain"] {
            var wrote = false
            check(ReminderMarkerCleanup.clean(row("b", notes: notes), ledger: relaunch(newPath()),
                                              write: { _, _ in wrote = true; return .failed }) == .skipped && !wrote,
                  "a fresh ledger touches nothing else: \(notes.debugDescription)")
        }
        let halting = newPath()
        var changed = row("a", notes: head)
        changed.notes = nil
        changed.title = "Changed"
        check(ReminderMarkerCleanup.clean(row("a", notes: head), ledger: relaunch(halting), write: { _, _ in .saved(changed) }) == .halted,
              "a differing readback stops a fresh ledger too")
        check(relaunch(halting).cleanupHalted, "for good")

        // A file that can't be read may be a passing failure: it isn't moved until a creation needs the ledger.
        let unreadable = newPath()
        let good = relaunch(unreadable)
        _ = good.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start)
        chmod(unreadable.path, 0)
        let waiting = relaunch(unreadable)
        check(!waiting.readable, "not readable yet")
        check(FileManager.default.fileExists(atPath: unreadable.path) && !FileManager.default.fileExists(atPath: damagedCopy(unreadable).path),
              "left where it is")
        chmod(unreadable.path, 0o600)
        waiting.flush()
        check(waiting.readable && waiting.entry(day)?.sourceID == "ek-1", "it comes back whole, and nothing was lost")
        chmod(unreadable.path, 0)
        let stuck = relaunch(unreadable)
        check(stuck.begin(actionID: "reminder:later", kind: "exam", plannedID: "ek-2", at: start) == .recorded,
              "a creation still goes through")
        check(FileManager.default.fileExists(atPath: damagedCopy(unreadable).path), "and the old file is kept aside")
        check(stuck.cleanupHalted, "cleanup is off")

        // A folder that can't be written can't set anything aside: nothing changes, and it recovers.
        let locked = newPath()
        write(Data("damaged".utf8), to: locked)
        let directory = locked.deletingLastPathComponent()
        chmod(directory.path, 0o500)
        let blocked = relaunch(locked)
        check(!blocked.readable, "still not usable")
        check(blocked.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start) == .unavailable, "nothing runs on top of it")
        check(!blocked.adopt(actionID: day, kind: "exam", sourceID: "a", externalID: nil, at: start), "no cleanup on top of it")
        check((try? Data(contentsOf: locked)) == Data("damaged".utf8), "the file is as it was")
        chmod(directory.path, 0o700)
        check(blocked.begin(actionID: day, kind: "exam", plannedID: "ek-1", at: start) == .recorded, "and the next creation recovers")
        check((try? Data(contentsOf: damagedCopy(locked))) == Data("damaged".utf8), "keeping the bytes")

        check(!ReminderActionLedger.damagedLogLine.contains("/") && !ReminderActionLedger.damagedLogLine.contains("reminder:"),
              "the log line names no path and no action")
    }
}
