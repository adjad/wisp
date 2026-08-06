import Foundation

// Reads new Mail.app inbox headers and pushes them to air_periodic.py.
//
// Ported from the Pro's MailReader, with one substantive change: this one is
// INCREMENTAL. The Pro re-scans a full year on a slow timer, which is a
// multi-minute job on a large mailbox — far too expensive to repeat every few
// minutes on a machine whose whole point is a ~2% duty cycle. Here the service
// hands back a cursor and the scan stops as soon as it walks past it.
//
// Lives in a signed .app rather than a script because Mail Automation is a TCC
// grant attached to a process bundle: NSAppleScript runs in-process, so the
// prompt reads "WispAirReader wants to control Mail" and, once granted,
// actually works. A Python process cannot hold this grant usefully.
final class MailReader {
    private var timer: Timer?
    private var inFlight = false

    // Ceilings, not expectations. The service reports scan_from = 0 on a fresh
    // install, and without these the very first run would walk an entire
    // 18,000-message mailbox over Apple Events — many minutes of blocking
    // AppleScript. Three hours of mail is a handful of messages; these bounds
    // only ever bite on first run or after a long outage, and the next pass
    // picks up anything they cut off.
    private let maxMessagesToScan = 400
    private let maxLookbackDays = 7.0

    func start() {
        sync()
        // Warm-up ramp: on a cold login Mail.app may not be running yet, or the
        // Automation dialog may still be sitting unanswered, so the first
        // attempt can silently fail. Without these retries a failed cold start
        // would leave the node blind until the next 5-minute tick.
        for d in [5.0, 15.0, 40.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in self?.sync() }
        }
        DispatchQueue.main.async { [weak self] in
            self?.timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { _ in
                self?.sync()
            }
        }
    }

    // Reads far enough back to cover the service's cursor, and never less than
    // the overlap the service asks for. `stopEarly` is the real optimization:
    // the inbox is ordered newest-first (verified against a real 18K-message
    // mailbox), so once one message falls older than the cutoff, every message
    // after it is older too.
    //
    // Deliberately NOT an AppleScript `whose` clause — those are pathologically
    // slow over Apple Events, far slower than walking and breaking manually.
    private func script(cutoff: Double) -> String {
        """
        tell application "Mail"
            set refDate to current date
            set year of refDate to 2001
            set month of refDate to 1
            set day of refDate to 1
            set time of refDate to 0
            set cutoffSecs to \(Int(cutoff))
            set output to ""
            set theMessages to messages of inbox
            set n to count of theMessages
            set lim to \(maxMessagesToScan)
            if n < lim then set lim to n
            repeat with i from 1 to lim
                set m to item i of theMessages
                try
                    set epochSecs to ((date received of m) - refDate) + 978307200
                    if epochSecs < cutoffSecs then exit repeat
                    set acctName to ""
                    try
                        set acctName to name of (account of (mailbox of m))
                    end try
                    set output to output & epochSecs & " | " & acctName & " | " & (extract name from sender of m) & " | " & (subject of m) & linefeed
                end try
            end repeat
            return output
        end tell
        """
    }

    func sync() {
        guard !inFlight else { return }   // a slow scan must not stack on its own timer
        inFlight = true
        Config.get("cursors") { [weak self] json in
            guard let self else { return }
            let scanFrom = ((json?["email"] as? [String: Any])?["scan_from"] as? Double) ?? 0
            self.run(scanFrom: scanFrom)
        }
    }

    private func run(scanFrom: Double) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            defer { self.inFlight = false }

            let floor = Date().timeIntervalSince1970 - self.maxLookbackDays * 86400
            let cutoff = max(scanFrom, floor)

            guard let s = NSAppleScript(source: self.script(cutoff: cutoff)) else { return }
            var err: NSDictionary?
            let result = s.executeAndReturnError(&err)

            if let err {
                // Usually Automation not granted (-1743) or Mail not running.
                // Report WHY rather than posting nothing, so /health can say
                // "permission missing" instead of the service silently
                // assuming there was simply no mail.
                let code = (err[NSAppleScript.errorNumber] as? Int) ?? 0
                let reason = code == -1743
                    ? "Mail Automation access isn't granted (System Settings ▸ Privacy ▸ Automation ▸ WispAirReader ▸ Mail)"
                    : "couldn't reach Mail (is it running?) — AppleScript error \(code)"
                Config.post("sync/emails", body: [
                    "headers": "",
                    "diagnostics": ["available": false, "reason": reason],
                ])
                return
            }

            let headers = result.stringValue ?? ""
            let count = headers.split(separator: "\n").count
            Config.post("sync/emails", body: [
                "headers": headers,
                "diagnostics": ["available": true, "count": count,
                                "scanned_from": cutoff],
            ])
            NSLog("[WispAirReader] mail: pushed \(count) headers (cutoff \(Int(cutoff)))")
        }
    }
}
