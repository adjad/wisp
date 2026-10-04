import AppKit
import Foundation

// Reads Notes.app content via AppleScript and pushes it to the backend for
// search_notes (verbatim lookup — same shape as Mail's raw-content path, not
// a summary). Lives in the Swift app for the same reason as Mail/Calendar:
// Automation TCC is per-process, so the permission prompt reads "Wisp wants
// to control Notes" and actually works once granted, unlike the Python
// backend scripting it via run_shell.
final class NotesReader {
    private var timer: Timer?
    private var inFlight = false
    // A request that arrives while a read is running cannot be satisfied by it:
    // that read started before the request and holds an older view of Notes.
    // Remember the request and run one more read when the current one ends.
    private var rerunRequested = false
    private let stateLock = NSLock()
    private let dbReader = NotesDBReader()

    // Same FS/RS control-character delimiters as MailReader's raw script — a
    // note body routinely contains "|" and newlines, which would corrupt a
    // "|"-delimited, linefeed-separated format.
    private let script = """
    tell application "Notes"
        set refDate to current date
        set year of refDate to 2001
        set month of refDate to 1
        set day of refDate to 1
        set time of refDate to 0
        set FS to ASCII character 1
        set RS to ASCII character 2
        set output to ""
        set theNotes to notes
        set n to count of theNotes
        set lim to 500
        if n < lim then set lim to n
        repeat with i from 1 to lim
            set nt to item i of theNotes
            try
                set epochSecs to ((modification date of nt) - refDate) + 978307200
                set folderName to ""
                try
                    set folderName to name of container of nt
                end try
                set output to output & epochSecs & FS & (name of nt) & FS & folderName & FS & (plaintext of nt) & RS
            end try
        end repeat
        return output
    end tell
    """

    // Same once/day cadence + warm-up-retry shape as Mail's raw sync: content
    // fetch (`plaintext of nt`) over potentially hundreds of notes is the slow
    // part, and note bodies are personal content worth minimizing how long
    // they sit resident in the backend (see notes_tools.py's TTL purge).
    func start() {
        syncIfIdle()
        for d in [3.0, 8.0, 15.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in self?.syncIfIdle() }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 86400, repeats: true) { _ in
                self?.syncIfIdle()
            }
        }
    }

    // `tell application "Notes"` auto-launches Notes if it isn't running —
    // checked before every scan so a cold Wisp launch never pops Notes open
    // on its own. Bundle ID, not app name, since Notes' display name can be
    // localized.
    private func isNotesRunning() -> Bool {
        NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.apple.Notes" }
    }

    /// Warm-up and daily reads: duplicates are dropped while a read is running.
    func syncIfIdle() {
        stateLock.lock()
        if inFlight { stateLock.unlock(); return }
        inFlight = true
        stateLock.unlock()
        run()
    }

    /// On-demand read for a lookup. If one is already running, a further read
    /// is queued behind it so the backend can tell a post that started after
    /// the request (fresh) from the in-flight one (older).
    func sync() {
        stateLock.lock()
        if inFlight {
            rerunRequested = true
            stateLock.unlock()
            return
        }
        inFlight = true
        stateLock.unlock()
        run()
    }

    private func run() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.read(snapshotStartedAt: Date().timeIntervalSince1970)
            self.stateLock.lock()
            let again = self.rerunRequested
            self.rerunRequested = false
            if !again { self.inFlight = false }
            self.stateLock.unlock()
            if again { self.run() }
        }
    }

    private func read(snapshotStartedAt: Double) {
        guard self.isNotesRunning() else {
            // Notes isn't open — read straight from its on-disk store
            // instead of launching it. See NotesDBReader.
            if let raw = self.dbReader.readNotes() {
                self.post(snapshotStartedAt: snapshotStartedAt, raw: raw)
            } else {
                self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false,
                          reason: "Notes is closed and its local store is not readable. Open Notes or check Full Disk Access.")
            }
            return
        }
        guard let s = NSAppleScript(source: self.script) else {
            self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false, reason: "The Notes reader could not start.")
            return
        }
        var err: NSDictionary?
        let result = s.executeAndReturnError(&err)
        if err != nil {
            self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false, reason: "Notes did not respond. Check Wisp’s Notes Automation access.")
            return
        }
        self.post(snapshotStartedAt: snapshotStartedAt, raw: result.stringValue ?? "")
    }

    private func post(snapshotStartedAt: Double, raw: String, available: Bool = true, reason: String = "") {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/notes")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "raw": raw,
            // When the native read began: the backend accepts a post as proof of
            // freshness only if this is later than its refresh request.
            "diagnostics": ["available": available, "reason": reason,
                            "snapshot_started_at": snapshotStartedAt],
        ])
        URLSession.shared.dataTask(with: req).resume()
    }
}
