import Foundation

/// What Settings ▸ Google and the notch know about Google, read from
/// GET /assistant/gmail/status. Pure (Foundation only) so the contract checks
/// in tests/GoogleConnectChecks.swift compile it alone.
///
/// One Google sign-in per account serves everything: Brief's read-only mail
/// and calendar sync and the Google Workspace tools Ask uses. The backend
/// merges their problems into one `state` and one plain `message` per account
/// (gmail_source.account_state); this file only decides how to show them.
struct GoogleStatus: Equatable {
    struct Account: Identifiable, Equatable {
        enum State: String {
            case connected, expired, needsReconnect = "needs_reconnect", attention
        }
        let id: String          // the email address
        let state: State
        let message: String
        /// The raw reason (developer wording) for Advanced; empty when fine.
        let detail: String
        let lastSync: Date?

        /// The one status line under the address.
        var statusLine: String {
            switch state {
            case .connected: return "Connected"
            case .expired: return "Sign-in expired"
            case .needsReconnect: return message.isEmpty ? "Reconnect to finish setup." : message
            case .attention: return "Needs attention: " + (message.isEmpty ? "Wisp will try again." : message)
            }
        }

        /// Reconnect is offered exactly when a new sign-in fixes the account.
        var reconnectTitle: String? {
            switch state {
            case .expired: return "Reconnect"
            case .needsReconnect: return "Reconnect to finish setup"
            default: return nil
            }
        }

        static func parse(_ row: [String: Any]) -> Account? {
            guard let email = row["email"] as? String, !email.isEmpty else { return nil }
            let lastError = row["last_error"] as? String ?? ""
            let calendarError = row["calendar_error"] as? String ?? ""
            // A backend from before the merged state: derive the same thing.
            let state = (row["state"] as? String).flatMap(State.init(rawValue:))
                ?? ((row["expired"] as? Bool ?? false) ? .expired
                    : (lastError.isEmpty && calendarError.isEmpty) ? .connected : .attention)
            let message = row["message"] as? String
                ?? (lastError.isEmpty ? calendarError : lastError)
            return Account(id: email, state: state, message: message,
                           detail: row["detail"] as? String ?? "",
                           lastSync: (row["last_headers_sync"] as? Double).map(Date.init(timeIntervalSince1970:)))
        }
    }

    var accounts: [Account] = []
    /// "user" (the user's own OAuth client), "builtin" (shipped with Wisp) or "none".
    var clientSource = "none"
    /// The user's own client ID (never the built-in one) and whether its secret is saved.
    var userClientID = ""
    var hasSavedSecret = false
    var consentNotice = ""
    /// Trouble with the Workspace server (Ask's Google tools), once for the
    /// whole section and shown under Advanced; it never changes an account's
    /// state, so an account whose mail and calendar work reads Connected.
    var workspaceNote = ""
    var workspaceNoteDetail = ""

    /// A Connect button can work: some OAuth client exists.
    var canConnect: Bool { clientSource == "user" || clientSource == "builtin" }

    static func parse(_ s: [String: Any]) -> GoogleStatus {
        let userID = s["client_id"] as? String ?? ""
        let source = s["client_source"] as? String ?? (userID.isEmpty ? "none" : "user")
        let note = s["workspace_note"] as? [String: Any] ?? [:]
        return GoogleStatus(
            accounts: (s["accounts"] as? [[String: Any]] ?? []).compactMap(Account.parse),
            clientSource: ["user", "builtin", "none"].contains(source) ? source : "none",
            userClientID: userID, hasSavedSecret: s["has_secret"] as? Bool ?? false,
            consentNotice: s["consent_notice"] as? String ?? "",
            workspaceNote: note["message"] as? String ?? "",
            workspaceNoteDetail: note["detail"] as? String ?? "")
    }
}

/// Copy shown before the browser opens for a sign-in.
enum GoogleConsentCopy {
    static let unverified = "Google will warn that Wisp isn't verified yet. Choose Continue."

    /// The notice for this sign-in: always for the built-in client, which is
    /// Wisp's own unverified Testing-mode app; the backend's word otherwise.
    static func notice(_ status: GoogleStatus) -> String {
        if !status.consentNotice.isEmpty { return status.consentNotice }
        return status.clientSource == "builtin" ? unverified : ""
    }
}

/// Whether the notch's Quick Settings shows a Google row at all.
///
/// The standing rule keeps controls that widen what Wisp reads out of the
/// notch (see QuickSettingsView). The user made one deliberate, narrow
/// exception: a new user with no Google account connected gets a single
/// "Connect Google" button there for the very first connect. Once any
/// account is connected the row is gone entirely: no account list, no
/// Disconnect & erase; those live in Settings ▸ Google only. With no OAuth
/// client available there is nothing the button could do, so no row either.
/// A sign-in started from the row keeps it visible until that sign-in ends,
/// so its progress line isn't cut off the moment the account is saved.
enum NotchGoogleVisibility {
    static func showsConnect(loaded: Bool, status: GoogleStatus, signInInProgress: Bool) -> Bool {
        if signInInProgress { return true }
        return loaded && status.accounts.isEmpty && status.canConnect
    }
}

/// What a Disconnect & erase response proves was erased. Pure. Only parts the
/// backend reported as "erased" are named; anything else is called out.
enum GoogleErase {
    static func note(account: String, response r: [String: Any]?) -> String {
        guard let r else { return "Couldn't confirm the erase. Try again." }
        let parts = (r["erased"] as? [String: Any])?[account] as? [String: Any] ?? [:]
        func erased(_ key: String) -> Bool { (parts[key] as? String)?.hasPrefix("erased") == true }
        var done: [String] = []
        if erased("mail") { done.append("mail") }
        if erased("calendar") { done.append("calendar events") }
        if erased("proactive") { done.append("to-dos") }
        let removed = (r["removed"] as? [String] ?? []).contains(account)
        var line = removed ? "Disconnected \(account)" : "\(account) was not connected"
        if !done.isEmpty { line += " and erased its " + ListFormatter.localizedString(byJoining: done) }
        line += "."
        let missing = ["mail": "mail", "calendar": "calendar events", "mail_overviews": "mail summary",
                       "proactive": "to-dos"].filter { !erased($0.key) }.map(\.value).sorted()
        if r["ok"] as? Bool != true && !missing.isEmpty {
            line += " Not confirmed erased: " + missing.joined(separator: ", ") + "."
        }
        return line
    }
}
