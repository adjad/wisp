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

    /// A recipient in the form Messages' own chat ids can actually be matched
    /// against.
    ///
    /// VERIFIED FAILURE 2026-08-10. Messages chat ids are E.164 —
    /// `any;-;+16507961110` — while `lookup_contact` returns the display form,
    /// `1 (650) 796-1110`. `id contains "1 (650) 796-1110"` is therefore FALSE
    /// for the very chat it names, so draftMessage's lookup matched nothing and
    /// (see below) typed a message to the user's mother into an unrelated group
    /// chat. Confirmed against the real Messages library: the display form
    /// returns no chat, the digit run returns `any;-;+16507961110`.
    ///
    /// Matching on the bare DIGIT RUN rather than reconstructing a `+1…` E.164
    /// is deliberate: it makes `1 (650) 796-1110`, `(650) 796-1110` and
    /// `+16507961110` all match the same chat (verified, all three), without
    /// this having to guess a country code it doesn't know.
    static func messagesHandle(_ s: String) -> String {
        if s.contains("@") { return s }          // an iMessage email address
        let digits = s.filter(\.isNumber)
        // Below ~7 digits a substring match stops identifying anyone — "1110"
        // matches half the library — so keep the original and let the lookup
        // fail closed rather than select a stranger's thread.
        return digits.count >= 7 ? digits : s
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

    // Runs a Mail/Messages script and reports the outcome, so the six actions
    // below don't each repeat the NSAppleScript + TCC-code + post dance.
    private static func run(actionId: String, app: String, script: String) {
        var err: NSDictionary?
        guard let s = NSAppleScript(source: script) else {
            post(actionId: actionId, ok: false, error: "couldn't build the \(app) script")
            return
        }
        s.executeAndReturnError(&err)
        if let err {
            let code = err[NSAppleScript.errorNumber] as? Int ?? 0
            let reason = code == -1743
                ? "\(app) Automation access isn't granted — allow it in System Settings › Privacy & Security › Automation"
                : (err[NSAppleScript.errorMessage] as? String ?? "\(app) refused the request")
            post(actionId: actionId, ok: false, error: reason)
            return
        }
        post(actionId: actionId, ok: true, error: "")
    }

    // Locates a message by its RFC Message-ID across every account's inbox.
    // Mail has no "message whose message id is X" lookup that spans accounts,
    // so this walks them. `message id` is the only stable handle — index
    // position shifts every time mail arrives (see MailReader.rawScript).
    private static func findMessage(_ messageId: String) -> String {
        """
            set theMsg to missing value
            repeat with acct in accounts
                if theMsg is missing value then
                    try
                        repeat with mbx in {inbox of acct}
                            try
                                set hits to (every message of mbx whose message id is "\(escape(messageId))")
                                if (count of hits) > 0 then
                                    set theMsg to item 1 of hits
                                    exit repeat
                                end if
                            end try
                        end repeat
                    end try
                end if
            end repeat
            if theMsg is missing value then error "couldn't find that message in the inbox — it may have been moved or is older than the synced window"
        """
    }

    static func replyToEmail(actionId: String, messageId: String, body: String,
                             replyAll: Bool) {
        DispatchQueue.global(qos: .userInitiated).async {
            // `reply` opens a pre-addressed reply window with the quoted
            // original; the body is prepended to that, then sent. Keeping the
            // quoted text is the point of replying in-thread rather than
            // composing fresh with send_email.
            let script = """
            tell application "Mail"
            \(findMessage(messageId))
                set theReply to reply theMsg opening window false reply to all \(replyAll ? "true" : "false")
                tell theReply
                    set content to "\(escape(body))" & return & content
                    send
                end tell
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
        }
    }

    static func draftEmail(actionId: String, to: [String], cc: [String],
                           subject: String, body: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            var recipientLines = to.map {
                "make new to recipient at end of to recipients with properties {address:\"\(escape($0))\"}"
            }
            recipientLines += cc.map {
                "make new cc recipient at end of cc recipients with properties {address:\"\(escape($0))\"}"
            }
            // visible:true and NO `send` — the entire difference from
            // sendEmail. The window is brought to the front so the user
            // actually sees the thing they asked to review.
            let script = """
            tell application "Mail"
                set newMsg to make new outgoing message with properties {subject:"\(escape(subject))", content:"\(escape(body))", visible:true}
                tell newMsg
                    \(recipientLines.joined(separator: "\n            "))
                end tell
                activate
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
        }
    }

    static func draftMessage(actionId: String, to: String, text: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            // Messages exposes no "create draft" verb, so this opens the
            // conversation and types the text into the compose field without
            // pressing send. System Events needs Accessibility, which is a
            // different grant from Automation — reported as such if missing.
            // FAILS CLOSED. The previous version wrapped the chat lookup in a
            // bare `try … end try` and then typed REGARDLESS of whether it had
            // found anything — so when the lookup missed (which it always did,
            // see messagesHandle above) `keystroke` went to whatever
            // conversation happened to be selected in Messages. Verified
            // 2026-08-10 from a user report: asked to draft their mother an
            // update, Wisp typed the whole message into an unrelated group
            // chat she is not even a member of, and still reported
            // `ok: true` — the tool then told the user "Messages opened with a
            // draft to 1 (650) 796-1110", naming a recipient it had never
            // targeted.
            //
            // So the selection is now VERIFIED before a single character is
            // typed: didTarget is only set on the success path, and the script
            // returns NOTFOUND (typing nothing at all) otherwise. Typing into
            // an unverified conversation is the one outcome this must never
            // produce — the text lands in a live compose field where one
            // stray Return sends it to the wrong people.
            let handle = messagesHandle(to)
            let script = """
            tell application "Messages"
                activate
                set targetHandle to "\(escape(handle))"
                set didTarget to false
                try
                    set theChat to 1st chat whose id contains targetHandle
                    set selected of theChat to true
                    set didTarget to true
                end try
                if didTarget is false then return "NOTFOUND"
            end tell
            delay 0.4
            tell application "System Events"
                tell process "Messages"
                    keystroke "\(escape(text))"
                end tell
            end tell
            return "OK"
            """
            var err: NSDictionary?
            guard let s = NSAppleScript(source: script) else {
                post(actionId: actionId, ok: false, error: "couldn't build the Messages script")
                return
            }
            let out = s.executeAndReturnError(&err)
            if let err {
                let code = err[NSAppleScript.errorNumber] as? Int ?? 0
                let reason: String
                switch code {
                case -1743:
                    reason = "Messages Automation access isn't granted — allow it in System Settings › Privacy & Security › Automation"
                case -25211, -1719:
                    reason = "typing the draft needs Accessibility access — allow Wisp in System Settings › Privacy & Security › Accessibility"
                default:
                    reason = err[NSAppleScript.errorMessage] as? String ?? "Messages refused the draft"
                }
                post(actionId: actionId, ok: false, error: reason)
                return
            }
            // The script ran cleanly but found no conversation, so it typed
            // NOTHING. Reported as a failure rather than swallowed: the whole
            // point is that the caller must not go on to tell the user a draft
            // is waiting for someone it never reached.
            if out.stringValue == "NOTFOUND" {
                post(actionId: actionId, ok: false,
                     error: "no existing Messages conversation matches \(to) — "
                          + "nothing was typed. Open or start that conversation "
                          + "once, or send it directly instead of drafting.")
                return
            }
            post(actionId: actionId, ok: true, error: "")
        }
    }

    static func markEmailRead(actionId: String, messageId: String, read: Bool) {
        DispatchQueue.global(qos: .userInitiated).async {
            let script = """
            tell application "Mail"
            \(findMessage(messageId))
                set read status of theMsg to \(read ? "true" : "false")
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
        }
    }

    static func archiveEmail(actionId: String, messageId: String) {
        DispatchQueue.global(qos: .userInitiated).async {
            // Prefer the account's real Archive mailbox; fall back to a
            // top-level one. Deliberately a MOVE, never `delete` — the tool
            // promises the message stays findable.
            let script = """
            tell application "Mail"
            \(findMessage(messageId))
                set acct to account of mailbox of theMsg
                set archiveBox to missing value
                try
                    set archiveBox to mailbox "Archive" of acct
                end try
                if archiveBox is missing value then
                    try
                        set archiveBox to mailbox "Archive"
                    end try
                end if
                if archiveBox is missing value then error "no Archive mailbox found for this account"
                move theMsg to archiveBox
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
        }
    }

    static func forwardEmail(actionId: String, messageId: String, to: [String], body: String) {
        guard !to.isEmpty else {
            post(actionId: actionId, ok: false, error: "forwardEmail needs at least one recipient")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let recipients = to.map {
                "make new to recipient at end of to recipients with properties {address:\"\(escape($0))\"}"
            }.joined(separator: "\n                    ")
            let prependNote = body.isEmpty ? ""
                : "                    set content to \"\(escape(body))\" & return & return & content\n"
            let script = """
            tell application "Mail"
            \(findMessage(messageId))
                set fwd to forward theMsg with opening window
                tell fwd
                    \(recipients)
            \(prependNote)                send
                end tell
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
        }
    }

    static func flagEmail(actionId: String, messageId: String, flagged: Bool) {
        DispatchQueue.global(qos: .userInitiated).async {
            let script = """
            tell application "Mail"
            \(findMessage(messageId))
                set flagged status of theMsg to \(flagged ? "true" : "false")
            end tell
            """
            run(actionId: actionId, app: "Mail", script: script)
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
        URLSession.shared.dataTask(with: req).resume()
    }
}
