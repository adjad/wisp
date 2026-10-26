import Foundation

// Pure checks for Google in Settings and the notch (GoogleAccountState.swift):
// when the notch shows its one Connect row, and how each account's merged
// status reads. No window, service or network.
@main
struct GoogleConnectChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func status(_ json: String) -> GoogleStatus {
        GoogleStatus.parse(try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any])
    }

    static func row(_ email: String, _ state: String, _ message: String = "", detail: String = "") -> String {
        #"{"email":"\#(email)","state":"\#(state)","message":"\#(message)","detail":"\#(detail)","last_error":"","calendar_error":""}"#
    }

    static func main() {
        let builtinNoAccounts = status(#"{"accounts":[],"client_id":"","client_source":"builtin","consent_notice":"Google will warn that Wisp isn't verified yet. Choose Continue."}"#)
        let builtinOneAccount = status(#"{"accounts":[\#(row("me@gmail.com", "connected", "Connected"))],"client_source":"builtin"}"#)
        let noClient = status(#"{"accounts":[],"client_id":"","client_source":"none"}"#)
        let userClient = status(#"{"accounts":[],"client_id":"mine.apps.googleusercontent.com","has_secret":true,"client_source":"user"}"#)
        let expiredOnly = status(#"{"accounts":[\#(row("me@gmail.com", "expired"))],"client_source":"builtin"}"#)

        // Notch: the Connect row is for a new user only.
        check(NotchGoogleVisibility.showsConnect(loaded: true, status: builtinNoAccounts, signInInProgress: false),
              "a new user with the built-in client sees Connect Google")
        check(NotchGoogleVisibility.showsConnect(loaded: true, status: userClient, signInInProgress: false),
              "a new user with their own client sees Connect Google")
        check(!NotchGoogleVisibility.showsConnect(loaded: true, status: builtinOneAccount, signInInProgress: false),
              "any connected account removes the notch Google row")
        check(!NotchGoogleVisibility.showsConnect(loaded: true, status: expiredOnly, signInInProgress: false),
              "an expired account is still an account: Reconnect lives in Settings")
        check(!NotchGoogleVisibility.showsConnect(loaded: true, status: noClient, signInInProgress: false),
              "no OAuth client: no row at all")
        check(!NotchGoogleVisibility.showsConnect(loaded: false, status: builtinNoAccounts, signInInProgress: false),
              "nothing shows before the first status read")
        check(NotchGoogleVisibility.showsConnect(loaded: true, status: builtinOneAccount, signInInProgress: true),
              "a sign-in started from the row keeps it until that sign-in ends")

        // Client source.
        check(builtinNoAccounts.canConnect && userClient.canConnect && !noClient.canConnect, "canConnect follows the source")
        check(builtinNoAccounts.userClientID.isEmpty, "the built-in client id is never the user's")
        check(userClient.userClientID == "mine.apps.googleusercontent.com" && userClient.hasSavedSecret, "user client parsed")
        check(status(#"{"accounts":[],"client_source":"surprise"}"#).clientSource == "none", "unknown source is none")
        check(status(#"{"accounts":[],"client_id":"legacy"}"#).clientSource == "user", "older backend: a saved id is the user's")

        // The unverified-app notice before the browser opens.
        check(GoogleConsentCopy.notice(builtinNoAccounts) == "Google will warn that Wisp isn't verified yet. Choose Continue.",
              "built-in sign-in shows the unverified notice")
        check(GoogleConsentCopy.notice(status(#"{"accounts":[],"client_source":"builtin"}"#)) == GoogleConsentCopy.unverified,
              "the notice is shown even if the backend omits it")
        check(GoogleConsentCopy.notice(userClient).isEmpty, "the user's own client gets no Wisp notice")

        // One status line per account.
        let connected = builtinOneAccount.accounts[0]
        check(connected.statusLine == "Connected" && connected.reconnectTitle == nil, "connected")
        let expired = expiredOnly.accounts[0]
        check(expired.statusLine == "Sign-in expired" && expired.reconnectTitle == "Reconnect", "expired offers Reconnect")
        let setup = status(#"{"accounts":[\#(row("a@gmail.com", "needs_reconnect", "Reconnect to finish setup."))]}"#).accounts[0]
        check(setup.reconnectTitle == "Reconnect to finish setup" && setup.statusLine == "Reconnect to finish setup.",
              "older sign-ins get one Reconnect to finish setup")
        let attention = status(#"{"accounts":[\#(row("a@gmail.com", "attention", "Calendar isn't available for this account yet.", detail: "Turn on the Google Calendar API"))]}"#).accounts[0]
        check(attention.statusLine == "Needs attention: Calendar isn't available for this account yet.",
              "attention reads as one plain sentence")
        check(attention.reconnectTitle == nil && attention.detail == "Turn on the Google Calendar API",
              "attention keeps the raw detail for Advanced")

        // An older backend without the merged state still reads sensibly.
        let legacy = status(#"{"accounts":[{"email":"a@gmail.com","last_error":"","calendar_error":"Calendar refused"},{"email":"b@gmail.com","expired":true},{"email":"c@gmail.com"}]}"#)
        check(legacy.accounts.map(\.state) == [.attention, .expired, .connected], "legacy rows derive a state")

        // Disconnect & erase note (Settings).
        let note = GoogleErase.note(account: "a@gmail.com", response: ["ok": true, "removed": ["a@gmail.com"],
            "erased": ["a@gmail.com": ["mail": "erased", "calendar": "erased 3", "mail_overviews": "erased", "proactive": "erased"]]])
        check(note.hasPrefix("Disconnected a@gmail.com and erased its mail"), "erase note names what was erased")

        print("Google connect checks passed")
    }
}
