import Foundation

/// When Google isn't covering mail (no Gmail account connected, or its sync is failing because the
/// Mac is offline or the sign-in expired), Wisp makes sure Mail.app's mail is read:
///
///   (a) Mail is open → read it through AppleScript, as always;
///   (b) Mail is closed and Full Disk Access is on → read the Envelope Index, without opening Mail;
///   (c) neither works and Mail Automation is ALREADY allowed → open Mail hidden (no activation,
///       not in recent items), wait until it answers, then run the header, raw and history reads.
///
/// Never asks for a permission. Polite: opens Mail at most once every 30 minutes, only when the mail
/// cache is stale, never within 15 minutes of the person quitting Mail themselves, and afterwards
/// quits only a Mail that Wisp opened, with `terminate` (never forced), and not if the person has
/// since brought it forward or opened a window. Pure: MailReader supplies the facts.
enum MailRecache {
    /// Wisp opens Mail at most this often.
    static let openEvery: TimeInterval = 30 * 60
    /// The person quit Mail within this long: leave it closed.
    static let userQuitGrace: TimeInterval = 15 * 60
    /// The mail cache counts as stale after this long without a successful read.
    static let staleAfter: TimeInterval = 30 * 60
    /// A Gmail account whose last header sync is older than this isn't covering mail (offline).
    static let gmailFreshFor: TimeInterval = 30 * 60
    /// How long Wisp waits for a Mail it opened to answer a script.
    static let answerTimeout: TimeInterval = 60
    /// The longest Wisp keeps a Mail it opened while the reads finish.
    static let recacheTimeout: TimeInterval = 15 * 60

    /// What Brief says while Wisp reads a Mail it opened.
    static let readingNote = "Reading Mail…"
    /// The reason_kind the service keys "Reading Mail…" on.
    static let readingKind = "mail_recaching"

    /// One Google account as the gmail status reports it.
    struct GmailAccount: Equatable {
        var connected: Bool
        var lastSync: Date?
    }

    /// Google covers mail: some account is connected and synced recently.
    static func gmailCovers(_ accounts: [GmailAccount], now: Date) -> Bool {
        accounts.contains { account in
            guard account.connected, let last = account.lastSync else { return false }
            return now.timeIntervalSince(last) < gmailFreshFor
        }
    }

    enum Route: Equatable {
        /// (a) script the open Mail.
        case mailApp
        /// (b) read the Envelope Index.
        case envelopeIndex
        /// (c) open Mail hidden and read it.
        case openMail
        /// Nothing more to do: today's behaviour (report why mail can't be read).
        case none(Skip)
    }

    enum Skip: Equatable {
        case gmailCovers, automationNotAllowed, cacheFresh, openedRecently, userQuitRecently, mailMissing
    }

    struct Facts: Equatable {
        var mailOpen: Bool
        /// The Envelope Index read just succeeded (Full Disk Access on and the index there).
        var indexRead: Bool
        /// macOS already allows Wisp to script Mail. Undecided or denied is false.
        var automationAllowed: Bool
        var gmailCovers: Bool
        var mailInstalled = true
        /// The last successful read of Mail's mail (either way); nil when none yet.
        var lastRead: Date?
        /// When Wisp last opened Mail itself.
        var lastOpenedByWisp: Date?
        /// When the person last quit Mail themselves.
        var userQuitAt: Date?
    }

    /// The order: the open app first, then the index, then (only when allowed and polite) opening Mail.
    static func route(_ f: Facts, now: Date) -> Route {
        if f.mailOpen && f.automationAllowed { return .mailApp }
        if f.indexRead { return .envelopeIndex }
        if f.gmailCovers { return .none(.gmailCovers) }
        if !f.automationAllowed { return .none(.automationNotAllowed) }
        if !f.mailInstalled { return .none(.mailMissing) }
        if let last = f.lastRead, now.timeIntervalSince(last) < staleAfter { return .none(.cacheFresh) }
        if let opened = f.lastOpenedByWisp, now.timeIntervalSince(opened) < openEvery { return .none(.openedRecently) }
        if let quit = f.userQuitAt, now.timeIntervalSince(quit) < userQuitGrace { return .none(.userQuitRecently) }
        return .openMail
    }

    /// Whether to quit Mail once the reads are done: only one Wisp opened, still hidden and in the
    /// background, that the person hasn't brought forward and that shows no window.
    static func quitsAfterReading(openedByWisp: Bool, isHidden: Bool, isActive: Bool,
                                  personTouched: Bool, visibleWindows: Int) -> Bool {
        openedByWisp && isHidden && !isActive && !personTouched && visibleWindows == 0
    }

    /// A Mail that quit counts as the person's quit unless Wisp asked it to.
    static func quitWasPerson(wispAskedToQuit: Bool) -> Bool { !wispAskedToQuit }
}
