import Foundation

// Sends mail and iMessages on the backend's behalf, and reports the outcome
// back. Lives here for the same reason MailReader does: Automation (TCC) is
// per-process, and only Wisp.app has the stable signed identity those grants
// attach to — the Python backend can't drive Mail or Messages at all.
//
// Unlike the calendar events (fire-and-forget, confirmed by the next sync),
// these are REQUEST/RESPONSE: the backend blocks an agent turn waiting for the
// result so it can tell the user "sent" or "that failed" truthfully. Every path
// out of here therefore has to post a result — including the failures. A
// silent return would hang the turn until the backend's timeout and then
// report a misleading "the app didn't respond".
//
// The user has already approved the send on a confirmation card by the time
// this runs; the policy engine treats mail and messages as always-confirm and
// refuses to let them be pre-approved (service/safety/grants.py).
enum OutboundSender {

    // AppleScript string literals: escape backslashes first, then quotes, or
    // the backslashes introduced by quote-escaping get escaped a second time.
    // Newlines become \n escapes so a multi-paragraph email body stays a single
    // valid literal instead of breaking the script into unparseable lines.
    private static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
         .replacingOccurrences(of: "\"", with: "\\\"")
         .replacingOccurrences(of: "\r\n", with: "\\n")
         .replacingOccurrences(of: "\r", with: "\\n")
         .replacingOccurrences(of: "\n", with: "\\n")
    }

    static func sendEmail(actionId: String, to: [String], cc: [String],
                          subject: String, body: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            var recipientLines = to.map {
                "make new to recipient at end of to recipients with properties {address:\"\(escape($0))\"}"
            }
            recipientLines += cc.map {
                "make new cc recipient at end of cc recipients with properties {address:\"\(escape($0))\"}"
            }
            let script = """
            tell application "Mail"
                set newMsg to make new outgoing message with properties {subject:"\(escape(subject))", content:"\(escape(body))", visible:false}
                tell newMsg
                    \(recipientLines.joined(separator: "\n            "))
                    send
                end tell
            end tell
            """
            var err: NSDictionary?
            guard let s = NSAppleScript(source: script) else {
                post(actionId: actionId, ok: false, error: "couldn't build the send script")
                return
            }
            s.executeAndReturnError(&err)
            if let err {
                let code = err[NSAppleScript.errorNumber] as? Int ?? 0
                // -1743 is the TCC "not authorized" code — worth naming
                // explicitly, since "Mail got an error" tells the user nothing
                // about the one thing they can actually fix.
                let reason = code == -1743
                    ? "Mail Automation access isn't granted — allow it in System Settings › Privacy & Security › Automation"
                    : (err[NSAppleScript.errorMessage] as? String ?? "Mail refused the send")
                post(actionId: actionId, ok: false, error: reason)
                return
            }
            post(actionId: actionId, ok: true, error: "")
        }
    }

    static func sendMessage(actionId: String, to: String, text: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            // Resolve the service first and fall back to SMS: `buddy ... of
            // service ... iMessage` fails outright for a recipient who isn't on
            // iMessage, which for a plain phone number is the common case.
            let script = """
            tell application "Messages"
                set targetBuddy to "\(escape(to))"
                try
                    set theService to 1st account whose service type = iMessage
                    send "\(escape(text))" to participant targetBuddy of theService
                on error
                    set theService to 1st account whose service type = SMS
                    send "\(escape(text))" to participant targetBuddy of theService
                end try
            end tell
            """
            var err: NSDictionary?
            guard let s = NSAppleScript(source: script) else {
                post(actionId: actionId, ok: false, error: "couldn't build the send script")
                return
            }
            s.executeAndReturnError(&err)
            if let err {
                let code = err[NSAppleScript.errorNumber] as? Int ?? 0
                let reason = code == -1743
                    ? "Messages Automation access isn't granted — allow it in System Settings › Privacy & Security › Automation"
                    : (err[NSAppleScript.errorMessage] as? String ?? "Messages refused the send")
                post(actionId: actionId, ok: false, error: reason)
                return
            }
            post(actionId: actionId, ok: true, error: "")
        }
    }

    private static func post(actionId: String, ok: Bool, error: String) {
        let url = WispClient.baseURL.appendingPathComponent("assistant/action_result")
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "action_id": actionId, "ok": ok, "error": error,
        ])
        WispSession.shared.dataTask(with: req).resume()
    }
}
