import AppKit
import Carbon
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
    private let backendRetry = NativeBackendRetry()

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
            && Self.automationDecided("com.apple.Notes")
    }

    /// Whether macOS has an answer about Wisp scripting this app. A scripted
    /// read while it has none makes macOS ask, and Wisp never asks on its own
    /// (Permissions.swift): until the person clicks Allow in setup, the read
    /// goes to the on-disk copy as if the app were closed. Never asks (`false`).
    static func automationDecided(_ bundleID: String) -> Bool {
        var target = AEAddressDesc()
        let created = bundleID.withCString { pointer in
            AECreateDesc(DescType(typeApplicationBundleID), pointer, strlen(pointer), &target)
        }
        guard created == noErr else { return false }
        defer { AEDisposeDesc(&target) }
        let status = AEDeterminePermissionToAutomateTarget(&target, AEEventClass(typeWildCard),
                                                           AEEventID(typeWildCard), false)
        return status != OSStatus(errAEEventWouldRequireUserConsent)
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
                // The reader says why, from the error: only macOS's privacy
                // denial points at Full Disk Access.
                let failure = self.dbReader.lastFailure
                    ?? NotesDBReader.failure(openErrno: 0)
                self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false,
                          reason: failure.reason, kind: failure.kind)
            }
            return
        }
        guard let s = NSAppleScript(source: self.script) else {
            self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false,
                      reason: "Wisp couldn't start reading Notes. Try again in a moment.")
            return
        }
        var err: NSDictionary?
        let result = s.executeAndReturnError(&err)
        if let err {
            let code = (err[NSAppleScript.errorNumber] as? Int) ?? 0
            self.post(snapshotStartedAt: snapshotStartedAt, raw: "", available: false,
                      reason: Self.scriptFailureReason(code: code))
            return
        }
        self.post(snapshotStartedAt: snapshotStartedAt, raw: result.stringValue ?? "")
    }

    /// Why the AppleScript read failed, from its error number: -1743 is macOS
    /// refusing Automation, -1712 is Notes not answering in time. Notes
    /// Automation has no reason_kind of its own, so the reason names the setting.
    static func scriptFailureReason(code: Int) -> String {
        switch code {
        case -1743:
            return "Wisp isn't allowed to read Notes yet. Turn on Notes for Wisp in System Settings > "
                + "Privacy & Security > Automation."
        case -1712:
            return "Notes was too slow to answer. Wisp will try again later."
        default:
            return "Notes didn't answer. Wisp will try again later."
        }
    }

    // `kind` is the reason_kind the Brief keys its button on, or "" for none.
    private func post(snapshotStartedAt: Double, raw: String, available: Bool = true, reason: String = "",
                      kind: String = "") {
        let url = WispClient.baseURL.appendingPathComponent("assistant/sync/notes")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // When the native read began: the backend accepts a post as proof of
        // freshness only if this is later than its refresh request.
        var diagnostics: [String: Any] = ["available": available, "reason": reason,
                                          "snapshot_started_at": snapshotStartedAt]
        if !kind.isEmpty { diagnostics["reason_kind"] = kind }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["raw": raw, "diagnostics": diagnostics])
        // A backend that isn't listening yet gets a fresh read soon, instead of
        // waiting for the daily tick.
        URLSession.shared.dataTask(with: req) { [weak self] _, _, error in
            self?.backendRetry.handle(error: error) { self?.syncIfIdle() }
        }.resume()
    }
}
