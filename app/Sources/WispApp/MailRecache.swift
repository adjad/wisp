import Foundation

/// When Google isn't covering mail (no Gmail account connected, or its sync is failing because the
/// Mac is offline or the sign-in expired), Wisp makes sure Mail.app's mail is read:
///
///   (a) Mail is open → read it through AppleScript, as always;
///   (b) Mail is closed and Full Disk Access is on → read the Envelope Index, without opening Mail;
///   (c) Mail Automation is ALREADY allowed and no live read is fresh → open Mail hidden (no
///       activation, not in recent items), wait until it answers, then run the header, raw and
///       history reads. This runs after a good index read too.
///
/// The index only moves while Mail runs and fetches: closed, Mail downloads nothing, so the index
/// holds the mail as of Mail's last run. Unless Google covers mail, a read of it is never fresh on
/// its own: only a live read through Mail.app is (and only that clears "Couldn't check <account>"
/// in the service, service/proactive/cycle.py). So while any listed Google account is unchecked,
/// one healthy account doesn't count as covering mail either.
///
/// Never asks for a permission. Never opens Mail while the Mac is offline (it couldn't fetch); the
/// network coming back, or the Mac waking, checks again. Once Mail answers, Wisp tells it to check
/// for new mail and waits until it has finished fetching (or fetchTimeout) before reading.
/// Polite: opens Mail at most once every 30 minutes (once an hour outside the person's working
/// hours, weekends included), only when the mail cache is stale, never within 15 minutes of the person quitting Mail themselves, and afterwards
/// quits only a Mail that Wisp opened, with `terminate` (never forced), and not if the person has
/// since brought it forward or opened a window. Pure: MailReader supplies the facts.
enum MailRecache {
    /// Wisp opens Mail at most this often.
    static let openEvery: TimeInterval = 30 * 60
    /// Outside working hours (weekends included) Wisp opens Mail at most once an hour.
    static let offHoursOpenEvery: TimeInterval = 60 * 60
    /// The longest Wisp waits for a Mail it opened to finish fetching before reading anyway.
    static let fetchTimeout: TimeInterval = 5 * 60
    /// Mail must report no background activity this many polls in a row to count as done fetching
    /// (it can read 0 for a moment before a fetch starts).
    static let fetchIdlePolls = 2
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

    /// Some listed Google account isn't connected and synced recently, so Google can't vouch for
    /// its mail, even when another account is healthy.
    static func gmailLeavesUnchecked(_ accounts: [GmailAccount], now: Date) -> Bool {
        accounts.contains { account in
            guard account.connected, let last = account.lastSync else { return true }
            return now.timeIntervalSince(last) >= gmailFreshFor
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
        case gmailCovers, automationNotAllowed, cacheFresh, openedRecently, userQuitRecently, mailMissing, offline
    }

    struct Facts: Equatable {
        var mailOpen: Bool
        /// The Envelope Index read just succeeded (Full Disk Access on and the index there).
        var indexRead: Bool
        /// macOS already allows Wisp to script Mail. Undecided or denied is false.
        var automationAllowed: Bool
        var gmailCovers: Bool
        /// A listed Google account is unchecked (gmailLeavesUnchecked).
        var gmailUnchecked: Bool = false
        var mailInstalled = true
        /// The last successful read of Mail's mail (either way); nil when none yet.
        var lastRead: Date?
        /// The last successful read through Mail.app itself; an index read doesn't count.
        var lastLiveRead: Date? = nil
        /// When Wisp last opened Mail itself.
        var lastOpenedByWisp: Date?
        /// When the person last quit Mail themselves.
        var userQuitAt: Date?
        /// Now is outside the person's working hours (offHours); false when the hours are unknown.
        var offHours = false
        /// The Mac has a network path. Offline, Mail couldn't fetch, so it isn't opened.
        var online = true
    }

    /// Working hours as service/user_hours.py holds them: minutes after local midnight, `days` with
    /// Monday = 0, a window past midnight wraps (and counts for the day it started). Outside it is
    /// off-hours. An empty window (start == end) has no working time, so every minute is off-hours.
    static func offHours(start: Int, end: Int, days: Set<Int>, minute: Int, weekday: Int) -> Bool {
        if start == end { return true }
        if start < end { return !(start <= minute && minute < end && days.contains(weekday)) }
        if minute >= start { return !days.contains(weekday) }
        if minute < end { return !days.contains((weekday + 6) % 7) }
        return true
    }

    /// Mail has finished fetching: its background activity count read 0 for the last
    /// fetchIdlePolls polls (newest last). An unreadable poll (nil) isn't idle.
    static func fetchSettled(_ polls: [Int?]) -> Bool {
        polls.count >= fetchIdlePolls && polls.suffix(fetchIdlePolls).allSatisfy { $0 == 0 }
    }

    /// Stop waiting: settled, or Mail hasn't answered the count the first three times (an older Mail
    /// or a busy one), so waiting the full fetchTimeout would only delay the read.
    static func fetchWaitOver(_ polls: [Int?]) -> Bool {
        fetchSettled(polls) || (polls.count >= 3 && polls.allSatisfy { $0 == nil })
    }

    /// The order: the open app first, then (only when allowed and polite) opening Mail, after the index
    /// read or in its place. Any route but .openMail leaves the index read, if any, as the result.
    static func route(_ f: Facts, now: Date) -> Route {
        if f.mailOpen && f.automationAllowed { return .mailApp }
        // Google covering mail: the index (when read) is all Mail needs to add.
        if f.gmailCovers && !f.gmailUnchecked { return f.indexRead ? .envelopeIndex : .none(.gmailCovers) }
        if !f.automationAllowed { return .none(.automationNotAllowed) }
        if !f.mailInstalled { return .none(.mailMissing) }
        // The index is never fresh on its own (Mail fetches only while it runs), so with an index
        // read, or an unchecked Google account, only a live read counts.
        if !f.online { return .none(.offline) }
        let last = f.indexRead || f.gmailUnchecked ? f.lastLiveRead : f.lastRead
        let every = f.offHours ? offHoursOpenEvery : openEvery
        if let last, now.timeIntervalSince(last) < max(staleAfter, every) { return .none(.cacheFresh) }
        if let opened = f.lastOpenedByWisp, now.timeIntervalSince(opened) < every { return .none(.openedRecently) }
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
