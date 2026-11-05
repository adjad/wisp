import Darwin
import Foundation

// Reminders Wisp creates used to carry two visible note lines ("Wisp action: …" and
// "Wisp kind: …"). They did two jobs: finding a creation whose reply was lost after
// EventKit saved it, and remembering the commitment kind (which sets how often Wisp
// notifies). Reminders has no field the person can't see, so both jobs now live in a
// private local ledger. Everything here is free of EventKit, so the rules can be checked
// without touching anyone's Reminders (tests/ReminderActionLedgerChecks.swift).
//
// A new EKReminder has its identifier before it is saved and keeps it through the save, so
// that ID goes into the ledger first. A reminder with an ID nothing else has ever used can
// only exist because this save landed, so finding it is proof. By where the app died:
//   before the intent is durable   no save ran; the outcome stays unknown
//   intent durable, before save    the ID isn't in Reminders; the outcome stays unknown
//   save committed, reply lost     the exact ID finds it
// If a store ever assigned a different ID at save, the last line would apply only once the
// ID after the save was recorded; a crash inside that gap would stay unknown. A failed
// search never authorizes a retry, so a lost reply can't create a second reminder on its
// own: the worst case is the "check Reminders" message.

/// The two note lines older builds wrote. Never written any more; still read, so
/// reminders from those builds keep reconciling and keep their kind.
enum ReminderMarkerNotes {
    static let kinds: Set<String> = ["reminder", "assignment", "exam", "meeting", "event"]
    private static let actionPrefix = "Wisp action: "
    private static let kindPrefix = "Wisp kind: "

    static func isValidActionID(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 128
            && id.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || "._:-".contains($0)) }
    }

    /// The reader older builds shipped, kept exactly: the first non-empty line names the action.
    static func hasMarker(_ notes: String?, actionID: String) -> Bool {
        notes?.split(separator: "\n").first == "\(actionPrefix)\(actionID)"[...]
    }

    /// The kind an old marker carries, or nil when the notes have no marker at all.
    static func legacyKind(_ notes: String?) -> String? {
        let lines = notes?.split(separator: "\n") ?? []
        guard lines.first?.hasPrefix(actionPrefix) == true else { return nil }
        let candidate = lines.first(where: { $0.hasPrefix(kindPrefix) })
            .map { String($0.dropFirst(kindPrefix.count)) }
        guard let candidate, kinds.contains(candidate) else { return "reminder" }
        return candidate
    }

    struct Exact: Equatable {
        let actionID: String
        let kind: String
        /// The notes with Wisp's two lines gone: every other byte as it was, nil when nothing is left.
        let cleaned: String?
    }

    /// Only Wisp's two lines exactly as it wrote them, at the very start of the notes. The
    /// comparison is on UTF-8 bytes because Swift treats "\r\n" as one Character. Anything
    /// else (a changed line, text above the marker, CRLF) is not matched and stays as it is.
    static func exact(_ notes: String?) -> Exact? {
        guard let bytes = notes.map({ Array($0.utf8) }),
              let firstEnd = bytes.firstIndex(of: 0x0A) else { return nil }
        let afterFirst = bytes[(firstEnd + 1)...]
        let secondEnd = afterFirst.firstIndex(of: 0x0A)
        let first = String(decoding: bytes[..<firstEnd], as: UTF8.self)
        let second = String(decoding: bytes[(firstEnd + 1)..<(secondEnd ?? bytes.count)], as: UTF8.self)
        guard first.hasPrefix(actionPrefix), second.hasPrefix(kindPrefix) else { return nil }
        let actionID = String(first.dropFirst(actionPrefix.count))
        let kind = String(second.dropFirst(kindPrefix.count))
        guard isValidActionID(actionID), kinds.contains(kind) else { return nil }
        var cleaned: String?
        if let secondEnd, secondEnd + 1 < bytes.count {
            cleaned = String(decoding: bytes[(secondEnd + 1)...], as: UTF8.self)
        }
        return Exact(actionID: actionID, kind: kind, cleaned: cleaned)
    }
}

/// One reminder as recovery and cleanup see it, read from EventKit or from a test fixture.
struct ReminderRow: Equatable {
    var id: String
    var externalID: String?
    var title: String?
    var notes: String?
    var dueMinute: Int?
    var allDayDate: String?
    var alarmCount = 0
    var isCompleted = false
    var listID: String?
    /// Everything else EventKit shows about it, only ever compared before and after a cleanup.
    var otherFacts: [String] = []
}

/// What a claimed creation asked for.
struct ReminderCreateTarget: Equatable {
    let title: String
    let dueTs: Double
    let dueDate: String
    let kind: String

    func matches(_ row: ReminderRow) -> Bool {
        guard row.title == title else { return false }
        if dueDate.isEmpty { return row.dueMinute == Int(dueTs / 60) && row.allDayDate == nil }
        return row.allDayDate == dueDate && row.alarmCount == 0
    }
}

/// A private, owner-only record of Wisp's own creations. It holds action IDs, EventKit
/// IDs and kinds, never a title: the outbox stays the owner of that text.
final class ReminderActionLedger {
    struct Entry: Codable, Equatable {
        let actionID: String
        let kind: String
        let startedAt: Double
        /// The ID the new reminder was given before its save, replaced if the save changed it.
        var sourceID: String?
        var externalID: String?
    }

    enum Begin: Equatable { case recorded, alreadyStarted, unavailable }

    private struct Stored: Codable {
        var version = 1
        var entries: [Entry]
        var cleanupHalted: Bool
    }

    /// A reminder due far off still needs its kind, so this is a size bound rather than an
    /// age: the oldest entries go first.
    static let defaultMaxEntries = 4096

    private let path: URL
    private let maxEntries: Int
    private let lock = NSLock()
    private var entries: [String: Entry] = [:]
    private var actionBySource: [String: String] = [:]
    private var actionByExternal: [String: String] = [:]
    private var halted = false
    private var dirty = false
    /// False when the file exists but can't be parsed. Nothing is overwritten then, and
    /// no creation runs: a kind or an unresolved intent may be in there.
    private(set) var readable = true

    static var defaultPath: URL {
        let home = ProcessInfo.processInfo.environment["WISP_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".moe")
        return home.appendingPathComponent("reminder-action-ledger.json")
    }

    init(path: URL = ReminderActionLedger.defaultPath, maxEntries: Int = ReminderActionLedger.defaultMaxEntries) {
        self.path = path
        self.maxEntries = maxEntries
        guard FileManager.default.fileExists(atPath: path.path) else { return }
        guard let data = try? Data(contentsOf: path),
              let stored = try? JSONDecoder().decode(Stored.self, from: data) else {
            readable = false
            return
        }
        entries = Dictionary(stored.entries.map { ($0.actionID, $0) }, uniquingKeysWith: { _, last in last })
        halted = stored.cleanupHalted
        reindex()
    }

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    /// Durable before EventKit is touched, or the creation doesn't run. An action that
    /// already began never saves again, whatever became of the first try.
    func begin(actionID: String, kind: String, plannedID: String, at startedAt: Date) -> Begin {
        locked {
            guard readable else { return .unavailable }
            guard entries[actionID] == nil else { return .alreadyStarted }
            entries[actionID] = Entry(actionID: actionID, kind: kind, startedAt: startedAt.timeIntervalSince1970,
                                      sourceID: plannedID.isEmpty ? nil : plannedID)
            reindex()
            guard persist() else {
                entries[actionID] = nil
                reindex()
                return .unavailable
            }
            return .recorded
        }
    }

    /// Records the IDs EventKit reports after the save. The reminder exists whether or not
    /// this reaches the disk, so a failed write is kept and retried by `flush`.
    func resolve(actionID: String, sourceID: String, externalID: String?) {
        locked {
            guard var entry = entries[actionID] else { return }
            entry.sourceID = sourceID
            entry.externalID = externalID
            entries[actionID] = entry
            reindex()
            dirty = !persist()
        }
    }

    /// Takes over what an old marker knows, durably, before the marker is removed. Refuses
    /// when this action already belongs to a different reminder.
    func adopt(actionID: String, kind: String, sourceID: String, externalID: String?, at: Date) -> Bool {
        locked {
            guard readable else { return false }
            if let known = entries[actionID] {
                return known.sourceID == sourceID && known.kind == kind && persist()
            }
            entries[actionID] = Entry(actionID: actionID, kind: kind, startedAt: at.timeIntervalSince1970,
                                      sourceID: sourceID, externalID: externalID)
            reindex()
            guard persist() else {
                entries[actionID] = nil
                reindex()
                return false
            }
            return true
        }
    }

    func flush() {
        locked { if dirty { dirty = !persist() } }
    }

    func entry(_ actionID: String) -> Entry? { locked { entries[actionID] } }

    /// EventKit may change a reminder's identifier after a full sync; its external ID is
    /// the second chance at the same reminder.
    func entry(sourceID: String, externalID: String?) -> Entry? {
        locked {
            if let action = actionBySource[sourceID] { return entries[action] }
            if let externalID, !externalID.isEmpty, let action = actionByExternal[externalID] { return entries[action] }
            return nil
        }
    }

    var cleanupHalted: Bool { locked { halted } }

    func haltCleanup() {
        locked {
            halted = true
            dirty = !persist()
        }
    }

    private func reindex() {
        actionBySource = [:]
        actionByExternal = [:]
        for entry in entries.values {
            if let source = entry.sourceID { actionBySource[source] = entry.actionID }
            if let external = entry.externalID, !external.isEmpty { actionByExternal[external] = entry.actionID }
        }
    }

    private func persist() -> Bool {
        guard readable else { return false }
        if entries.count > maxEntries {
            let oldestFirst = entries.values.sorted { $0.startedAt < $1.startedAt }
            for entry in oldestFirst.prefix(entries.count - maxEntries) { entries[entry.actionID] = nil }
            reindex()
        }
        let stored = Stored(entries: entries.values.sorted { $0.startedAt < $1.startedAt }, cleanupHalted: halted)
        guard let data = try? JSONEncoder().encode(stored) else { return false }
        return Self.writeDurably(data, to: path)
    }

    /// Replaced atomically, 0600 from the first byte, flushed before the rename: this
    /// record has to outlive a crash that follows it immediately. The same steps as
    /// WispClient.writeOwnerOnly, which the isolated reader harnesses don't compile.
    private static func writeDurably(_ data: Data, to url: URL) -> Bool {
        let directory = url.deletingLastPathComponent()
        guard (try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])) != nil else { return false }
        let temporary = directory.appendingPathComponent("." + url.lastPathComponent + "." + UUID().uuidString + ".tmp").path
        let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return false }
        var ok = fchmod(fd, 0o600) == 0
        if ok {
            ok = data.withUnsafeBytes { buffer -> Bool in
                var offset = 0
                while offset < buffer.count {
                    let count = write(fd, buffer.baseAddress! + offset, buffer.count - offset)
                    if count > 0 { offset += count; continue }
                    if count < 0 && errno == EINTR { continue }
                    return false
                }
                return true
            }
        }
        if ok { ok = fsync(fd) == 0 }
        close(fd)
        if ok { ok = rename(temporary, url.path) == 0 }
        if !ok { unlink(temporary) }
        return ok
    }
}

/// One creation from intent to verified readback. EventKit stays behind the two closures,
/// as in CalendarCreationReceipt.
enum ReminderCreationFlow {
    enum Outcome: Equatable {
        /// Nothing was written, so this is a real failure.
        case refused(String)
        case uncertain(String)
        case created(ReminderRow)
    }

    static func run(ledger: ReminderActionLedger, actionID: String, target: ReminderCreateTarget,
                    plannedID: String, now: Date = Date(),
                    save: () throws -> (id: String, externalID: String?),
                    readback: (String) -> ReminderRow?) -> Outcome {
        switch ledger.begin(actionID: actionID, kind: target.kind, plannedID: plannedID, at: now) {
        case .unavailable:
            return .refused("Wisp couldn't record this reminder first, so nothing was added")
        case .alreadyStarted:
            return .uncertain("Reminder creation remains uncertain")
        case .recorded:
            break
        }
        let saved: (id: String, externalID: String?)
        do { saved = try save() }
        catch { return .uncertain("Native reminder creation outcome is unknown") }
        guard !saved.id.isEmpty else { return .uncertain("Created reminder could not be read back") }
        ledger.resolve(actionID: actionID, sourceID: saved.id, externalID: saved.externalID)
        guard let row = readback(saved.id), target.matches(row), (row.notes ?? "").isEmpty else {
            return .uncertain("Created reminder could not be read back")
        }
        return .created(row)
    }
}

/// Read-only recovery for a creation whose reply was lost. A nil result stays "unknown"
/// and never authorizes another save.
enum ReminderRecovery {
    enum Proof: Equatable { case exactID, legacyMarker }

    static func find(actionID: String, target: ReminderCreateTarget, rows: [ReminderRow],
                     ledger: ReminderActionLedger) -> (row: ReminderRow, proof: Proof)? {
        if let entry = ledger.entry(actionID), let source = entry.sourceID {
            let row = rows.first { $0.id == source }
                ?? entry.externalID.flatMap { external in
                    external.isEmpty ? nil : rows.first { $0.externalID == external }
                }
            guard let row, entry.kind == target.kind, target.matches(row), !row.id.isEmpty else { return nil }
            return (row, .exactID)
        }
        let marked = rows.filter { ReminderMarkerNotes.hasMarker($0.notes, actionID: actionID) }
        guard marked.count == 1, let row = marked.first, target.matches(row), !row.id.isEmpty,
              ReminderMarkerNotes.legacyKind(row.notes) == target.kind else { return nil }
        return (row, .legacyMarker)
    }
}

/// Removes Wisp's two old lines from reminders it made: the exact lines, nothing else,
/// through a save that's read back. Safe to re-run: a cleaned reminder no longer matches.
enum ReminderMarkerCleanup {
    enum WriteResult: Equatable {
        /// The save ran, with what EventKit reads back afterwards.
        case saved(ReminderRow)
        /// It changed since it was read, or is gone. Nothing was written.
        case changedSinceRead
        case failed
    }

    enum Step: Equatable {
        case cleaned, skipped
        /// The save failed: stop now and try again next launch.
        case stopForNow
        /// Something other than the notes changed: never run again.
        case halted
    }

    /// Reminders this can touch: an exact marker, in a list Wisp can write to, the only
    /// reminder naming that action. A copy made in Reminders carries the same marker, and
    /// neither copy can then be told to be Wisp's own.
    static func candidates(_ rows: [ReminderRow], writableLists: Set<String>) -> [ReminderRow] {
        var named: [String: Int] = [:]
        for row in rows {
            guard let first = row.notes?.split(separator: "\n").first, first.hasPrefix("Wisp action: ") else { continue }
            named[String(first.dropFirst("Wisp action: ".count)), default: 0] += 1
        }
        return rows.filter { row in
            guard let exact = ReminderMarkerNotes.exact(row.notes), let list = row.listID else { return false }
            return writableLists.contains(list) && named[exact.actionID] == 1 && !row.id.isEmpty
        }
    }

    /// The ledger takes over the action and the kind first: the marker can only go once
    /// nothing depends on it.
    static func clean(_ row: ReminderRow, ledger: ReminderActionLedger, now: Date = Date(),
                      write: (_ expected: ReminderRow, _ notes: String?) -> WriteResult) -> Step {
        guard !ledger.cleanupHalted, let exact = ReminderMarkerNotes.exact(row.notes) else { return .skipped }
        guard ledger.adopt(actionID: exact.actionID, kind: exact.kind, sourceID: row.id,
                           externalID: row.externalID, at: now) else { return .skipped }
        var expected = row
        expected.notes = exact.cleaned
        switch write(row, exact.cleaned) {
        case .changedSinceRead:
            return .skipped
        case .failed:
            return .stopForNow
        case .saved(let readback):
            var seen = readback
            if seen.notes == "" { seen.notes = nil }
            guard seen == expected else {
                ledger.haltCleanup()
                return .halted
            }
            return .cleaned
        }
    }
}
