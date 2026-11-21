import Foundation

/// MailRecache: when Google isn't covering mail, the read order, the 30-minute rate limit, the
/// person-quit rule and quitting only a Mail Wisp opened. Pure; compiled with MailRecache.swift alone.
@main
struct MailRecacheChecks {
    static func main() {
        var count = 0
        var failed = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { failed += 1; print("FAIL: \(message)") }
        }
        typealias M = MailRecache
        let now = Date(timeIntervalSince1970: 1_790_000_000)
        func ago(_ minutes: Double) -> Date { now.addingTimeInterval(-minutes * 60) }
        let base = M.Facts(mailOpen: false, indexRead: false, automationAllowed: true, gmailCovers: false,
                           lastRead: nil, lastOpenedByWisp: nil, userQuitAt: nil)

        // Trigger: Google covers mail only when connected AND synced in the last 30 minutes.
        check(!M.gmailCovers([], now: now), "no Google account: not covered")
        check(M.gmailCovers([.init(connected: true, lastSync: ago(5))], now: now), "connected and fresh: covered")
        check(!M.gmailCovers([.init(connected: true, lastSync: ago(45))], now: now), "offline (stale sync): not covered")
        check(!M.gmailCovers([.init(connected: false, lastSync: ago(1))], now: now), "expired sign-in: not covered")
        check(!M.gmailCovers([.init(connected: true, lastSync: nil)], now: now), "never synced: not covered")
        check(M.gmailCovers([.init(connected: false, lastSync: nil), .init(connected: true, lastSync: ago(1))], now: now),
              "one healthy account is enough")

        // Order: open Mail → script it; closed with the index → index; only then open Mail.
        var f = base; f.mailOpen = true
        check(M.route(f, now: now) == .mailApp, "(a) Mail open and allowed: script it")
        f = base; f.indexRead = true
        check(M.route(f, now: now) == .envelopeIndex, "(b) closed with Full Disk Access: the index")
        f = base; f.indexRead = true; f.gmailCovers = true
        check(M.route(f, now: now) == .envelopeIndex, "the index is read even when Google covers mail")
        check(M.route(base, now: now) == .openMail, "(c) neither, Google off, allowed: open Mail")
        f = base; f.gmailCovers = true
        check(M.route(f, now: now) == .none(.gmailCovers), "Google covering mail: never open Mail")
        f = base; f.automationAllowed = false
        check(M.route(f, now: now) == .none(.automationNotAllowed), "Automation not allowed: today's behaviour")
        f = base; f.mailOpen = true; f.automationAllowed = false
        check(M.route(f, now: now) == .none(.automationNotAllowed), "Mail open but not allowed: never asks")
        f = base; f.mailInstalled = false
        check(M.route(f, now: now) == .none(.mailMissing), "no Mail app: nothing to open")

        // Only when the cache is stale.
        f = base; f.lastRead = ago(10)
        check(M.route(f, now: now) == .none(.cacheFresh), "read 10 min ago: fresh")
        f = base; f.lastRead = ago(31)
        check(M.route(f, now: now) == .openMail, "read 31 min ago: stale")

        // At most once every 30 minutes.
        f = base; f.lastOpenedByWisp = ago(29)
        check(M.route(f, now: now) == .none(.openedRecently), "opened 29 min ago: wait")
        f = base; f.lastOpenedByWisp = ago(30)
        check(M.route(f, now: now) == .openMail, "opened 30 min ago: may open again")

        // Not within 15 minutes of the person quitting Mail.
        f = base; f.userQuitAt = ago(14)
        check(M.route(f, now: now) == .none(.userQuitRecently), "person quit 14 min ago: leave it")
        f = base; f.userQuitAt = ago(16)
        check(M.route(f, now: now) == .openMail, "person quit 16 min ago: may open")
        check(M.quitWasPerson(wispAskedToQuit: false), "a quit Wisp didn't ask for is the person's")
        check(!M.quitWasPerson(wispAskedToQuit: true), "Wisp's own polite quit isn't the person's")

        // Quit only a Mail Wisp opened, still hidden, in the background, untouched, windowless.
        check(M.quitsAfterReading(openedByWisp: true, isHidden: true, isActive: false, personTouched: false, visibleWindows: 0),
              "Wisp's hidden Mail: quit")
        check(!M.quitsAfterReading(openedByWisp: false, isHidden: true, isActive: false, personTouched: false, visibleWindows: 0),
              "the person's Mail: never")
        check(!M.quitsAfterReading(openedByWisp: true, isHidden: false, isActive: false, personTouched: false, visibleWindows: 0),
              "unhidden: leave it")
        check(!M.quitsAfterReading(openedByWisp: true, isHidden: true, isActive: true, personTouched: false, visibleWindows: 0),
              "in front: leave it")
        check(!M.quitsAfterReading(openedByWisp: true, isHidden: true, isActive: false, personTouched: true, visibleWindows: 0),
              "brought forward earlier: leave it")
        check(!M.quitsAfterReading(openedByWisp: true, isHidden: true, isActive: false, personTouched: false, visibleWindows: 1),
              "a window open: leave it")

        // The limits and the words Brief shows.
        check(M.openEvery == 1800 && M.userQuitGrace == 900 && M.staleAfter == 1800 && M.gmailFreshFor == 1800,
              "30-minute rate limit, 15-minute quit grace, 30-minute staleness")
        check(M.readingNote == "Reading Mail…" && M.readingKind == "mail_recaching", "Brief's words and kind")

        if failed > 0 { print("MailRecacheChecks: \(failed) of \(count) failed"); exit(1) }
        print("MailRecacheChecks: \(count) checks passed")
    }
}
