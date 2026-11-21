import Foundation

/// Whether Full Disk Access works for this copy of Wisp right now, found by
/// really opening what it guards instead of asking macOS (there is no API for it;
/// Apple's own advice to developers is to probe by reading). Every grant is then
/// picked up the moment it is made: a new open is checked by macOS each time, so
/// switching Wisp on in System Settings needs no restart to take effect.
///
/// macOS 27.3 can scope Full Disk Access to chosen folders. Some of what Wisp
/// reads can then open and some can't, which is `.partial`, not "off".
///
/// The probe is two layers. `decide` and `ReopenPrompt` are pure, and checked
/// without a disk (tests/AccessProbeChecks.swift). `probe(home:)` is the thin
/// I/O: open(2) read-only on a file, close it at once. It never reads a byte, so
/// nothing in Messages, Mail or Notes is ever read or logged, and it takes the
/// folder as a parameter so a check points it at a scratch folder, never at the
/// real home.
enum AccessProbe {
    /// The stores Full Disk Access is judged by, one per source Wisp reads off disk.
    enum Store: String, CaseIterable, Comparable {
        case messages, mail, notes

        static func < (lhs: Store, rhs: Store) -> Bool { lhs.rawValue < rhs.rawValue }

        var label: String {
            switch self {
            case .messages: return "Messages"
            case .mail: return "Mail"
            case .notes: return "Notes"
            }
        }
    }

    /// What one store said when opened.
    enum Outcome: Equatable {
        /// It opened.
        case readable
        /// macOS refused it (EPERM, EACCES): it exists, and Wisp isn't allowed in.
        case refused
        /// Not there (Mail never set up, Messages never used), or an error that
        /// says nothing about permission. Counted as neither.
        case missing
    }

    enum State: Equatable {
        /// Every store that exists opened.
        case on
        /// None of the stores that exist opened.
        case off
        /// Some opened and some didn't: a grant scoped to chosen folders (macOS 27.3).
        case partial(unreadable: [Store])
        /// No store exists on this Mac, so nothing can say.
        case unknown

        /// Some store reads, so a restart would change nothing.
        var works: Bool {
            switch self {
            case .on, .partial: return true
            case .off, .unknown: return false
            }
        }
    }

    /// The states from what each store said. A store that doesn't exist is left out
    /// of the count, so a Mac that never set Mail up isn't "partial" for it. Pure.
    static func decide(_ outcomes: [Store: Outcome]) -> State {
        let counted = outcomes.filter { $0.value != .missing }
        guard !counted.isEmpty else { return .unknown }
        let refused = counted.filter { $0.value == .refused }.keys.sorted()
        if refused.isEmpty { return .on }
        if refused.count == counted.count { return .off }
        return .partial(unreadable: refused)
    }

    /// The stores that opened now and didn't before: what to read again at once. Pure.
    static func gained(from old: [Store: Outcome], to new: [Store: Outcome]) -> [Store] {
        Store.allCases.filter { new[$0] == .readable && old[$0] != .readable }
    }

    /// One probe: what each store said, and the state they add up to.
    struct Reading: Equatable {
        var outcomes: [Store: Outcome]
        var state: State { AccessProbe.decide(outcomes) }
        static let empty = Reading(outcomes: [:])
    }

    // MARK: Thin I/O

    /// What the probe needs from the disk, so a check can stand in for it.
    struct FileSystem {
        /// 0 when the file opens read-only, otherwise errno.
        var open: (String) -> Int32
        /// A folder's entry names, or the errno that stopped the listing.
        var list: (String) -> Result<[String], Errno>

        struct Errno: Error, Equatable { let code: Int32 }

        static let real = FileSystem(
            open: { path in
                let fd = Darwin.open(path, O_RDONLY | O_CLOEXEC)
                if fd >= 0 { close(fd); return 0 }
                return errno
            },
            list: { path in
                guard let dir = opendir(path) else { return .failure(Errno(code: errno)) }
                defer { closedir(dir) }
                var names: [String] = []
                while let entry = readdir(dir) {
                    let name = withUnsafePointer(to: &entry.pointee.d_name) {
                        $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
                    }
                    if name != "." && name != ".." { names.append(name) }
                }
                return .success(names)
            })
    }

    static let messagesPath = "Library/Messages/chat.db"
    static let mailFolder = "Library/Mail"
    static let notesPath = "Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"

    /// An open's errno as a store's answer. EPERM and EACCES are macOS's own refusal
    /// (what the privacy check returns); ENOENT says the store isn't there; anything
    /// else (a lock, an I/O error) says nothing about permission. Pure.
    static func outcome(openError code: Int32) -> Outcome {
        switch code {
        case 0: return .readable
        case EPERM, EACCES: return .refused
        default: return .missing
        }
    }

    /// Mail: the newest V<N> folder's Envelope Index (older ones linger after a
    /// macOS upgrade, the same rule as MailDBReader). Listing ~/Library/Mail is
    /// itself guarded, so a folder that lists is Full Disk Access working even
    /// before Mail has made an index.
    static func mailOutcome(home: String, _ fs: FileSystem) -> Outcome {
        let folder = home + "/" + mailFolder
        switch fs.list(folder) {
        case .failure(let error):
            return outcome(openError: error.code) == .refused ? .refused : .missing
        case .success(let entries):
            // Numeric, so V10 comes before V9 (a plain sort puts V9 first).
            let versions = entries.filter { $0.hasPrefix("V") }
                .sorted { (Int($0.dropFirst()) ?? -1) > (Int($1.dropFirst()) ?? -1) }
            for entry in versions {
                switch outcome(openError: fs.open("\(folder)/\(entry)/MailData/Envelope Index")) {
                case .readable: return .readable
                case .refused: return .refused
                case .missing: continue
                }
            }
            return .readable
        }
    }

    static func probe(home: String, _ fs: FileSystem = .real) -> Reading {
        Reading(outcomes: [
            .messages: outcome(openError: fs.open(home + "/" + messagesPath)),
            .mail: mailOutcome(home: home, fs),
            .notes: outcome(openError: fs.open(home + "/" + notesPath)),
        ])
    }

    static func probe(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Reading {
        probe(home: home.path, .real)
    }

    // MARK: Words

    /// What a partial grant says, for the permission page. Pure.
    static func partialNote(_ unreadable: [Store]) -> String {
        let names = unreadable.sorted().map(\.label)
        let list = names.count > 1 ? names.dropLast().joined(separator: ", ") + " and " + names.last! : names.first ?? ""
        return "Wisp can read some of your Mac but not \(list). Choose All Files in the page that opens."
    }
}

/// When the permission page says macOS needs Wisp reopened. It never does this
/// on its own: only when the person went to System Settings, came back, and a
/// read still fails 10 seconds later. Pure.
enum ReopenPrompt {
    /// How long after coming back the probe may still fail before the prompt shows.
    static let delay: TimeInterval = 10

    /// - Parameters:
    ///   - returnedAt: when the person came back from System Settings (seconds since
    ///     1970), nil if they haven't since the page was opened.
    ///   - dismissed: the person pressed Not now for this return.
    static func shows(returnedAt: TimeInterval?, now: TimeInterval, access: AccessProbe.State,
                      dismissed: Bool = false) -> Bool {
        guard let returnedAt, !dismissed, now >= returnedAt, now - returnedAt >= delay else { return false }
        // Only a probe that really fails: on and partial work, and unknown can't say.
        return access == .off
    }

    static let title = "macOS needs Wisp to reopen"
    static let detail = "You switched Wisp on, but macOS hasn't applied it yet. Reopen Wisp and it will."
    static let button = "Reopen Wisp"
}
