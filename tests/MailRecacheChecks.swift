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
        // The index only moves while Mail runs: without Google, a good index read still opens Mail
        // hidden once no live read is fresh, at most every 30 minutes.
        f = base; f.indexRead = true
        check(M.route(f, now: now) == .openMail, "(b) index read, no Google, no live read: open Mail hidden")
        f = base; f.indexRead = true; f.lastRead = ago(1)
        check(M.route(f, now: now) == .openMail, "an index read alone never makes the cache fresh")
        f = base; f.indexRead = true; f.lastLiveRead = ago(10)
        check(M.route(f, now: now) == .none(.cacheFresh), "index read, live read 10 min ago: leave Mail closed")
        f = base; f.indexRead = true; f.lastLiveRead = ago(31)
        check(M.route(f, now: now) == .openMail, "index read, live read 31 min ago: open Mail hidden")
        f = base; f.indexRead = true; f.lastOpenedByWisp = ago(29)
        check(M.route(f, now: now) == .none(.openedRecently), "index read, opened 29 min ago: wait")
        f = base; f.indexRead = true; f.userQuitAt = ago(14)
        check(M.route(f, now: now) == .none(.userQuitRecently), "index read, person quit 14 min ago: leave it")
        f = base; f.indexRead = true; f.automationAllowed = false
        check(M.route(f, now: now) == .none(.automationNotAllowed), "index read, Automation not allowed: never asks")
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

        // An unchecked Google account: an index read can't vouch for it, only a live Mail.app read can.
        let expired = M.GmailAccount(connected: false, lastSync: ago(1))
        let healthy = M.GmailAccount(connected: true, lastSync: ago(1))
        check(!M.gmailLeavesUnchecked([], now: now), "no Google account: nothing unchecked")
        check(!M.gmailLeavesUnchecked([healthy], now: now), "all healthy: nothing unchecked")
        check(M.gmailLeavesUnchecked([expired], now: now), "expired sign-in: unchecked")
        check(M.gmailLeavesUnchecked([healthy, expired], now: now), "healthy plus expired: still unchecked")
        check(M.gmailLeavesUnchecked([.init(connected: true, lastSync: ago(45))], now: now), "offline: unchecked")
        check(M.gmailLeavesUnchecked([.init(connected: true, lastSync: nil)], now: now), "never synced: unchecked")
        check(M.gmailCovers([healthy, expired], now: now), "healthy plus expired still 'covers' on its own")
        var u = base; u.indexRead = true; u.gmailUnchecked = true; u.gmailCovers = true
        check(M.route(u, now: now) == .openMail, "index read + healthy + expired, no live read: open Mail")
        u = base; u.indexRead = true; u.gmailUnchecked = true; u.lastRead = ago(1)
        check(M.route(u, now: now) == .openMail, "index read fresh but no live read: open Mail")
        u.lastLiveRead = ago(10)
        check(M.route(u, now: now) == .none(.cacheFresh), "live read 10 min ago: fresh")
        u.lastLiveRead = ago(31)
        check(M.route(u, now: now) == .openMail, "live read 31 min ago: stale")
        u = base; u.indexRead = true; u.gmailUnchecked = true; u.automationAllowed = false
        check(M.route(u, now: now) == .none(.automationNotAllowed), "unchecked but Automation not allowed: none")
        u = base; u.indexRead = true; u.gmailUnchecked = true; u.mailInstalled = false
        check(M.route(u, now: now) == .none(.mailMissing), "unchecked but no Mail app: none")
        u = base; u.indexRead = true; u.gmailUnchecked = true; u.lastOpenedByWisp = ago(29)
        check(M.route(u, now: now) == .none(.openedRecently), "unchecked, opened 29 min ago: wait")
        u = base; u.indexRead = true; u.gmailUnchecked = true; u.userQuitAt = ago(14)
        check(M.route(u, now: now) == .none(.userQuitRecently), "unchecked, person quit 14 min ago: leave it")
        u = base; u.mailOpen = true; u.indexRead = true; u.gmailUnchecked = true
        check(M.route(u, now: now) == .mailApp, "unchecked with Mail open: script it")
        u = base; u.gmailUnchecked = true; u.gmailCovers = true
        check(M.route(u, now: now) == .openMail, "no index, healthy plus expired: open Mail")
        u = base; u.indexRead = true; u.gmailCovers = true
        check(M.route(u, now: now) == .envelopeIndex, "all healthy: the index, unchanged")
        u = base; u.gmailCovers = true
        check(M.route(u, now: now) == .none(.gmailCovers), "all healthy, no index: covers, unchanged")

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

        // Outside working hours (weekends included): at most once an hour.
        f = base; f.offHours = true; f.lastOpenedByWisp = ago(45)
        check(M.route(f, now: now) == .none(.openedRecently), "off-hours, opened 45 min ago: wait")
        f = base; f.offHours = true; f.lastOpenedByWisp = ago(60)
        check(M.route(f, now: now) == .openMail, "off-hours, opened an hour ago: may open")
        f = base; f.offHours = true; f.indexRead = true; f.lastLiveRead = ago(45)
        check(M.route(f, now: now) == .none(.cacheFresh), "off-hours, live read 45 min ago: still fresh")
        f = base; f.offHours = true; f.indexRead = true; f.lastLiveRead = ago(61)
        check(M.route(f, now: now) == .openMail, "off-hours, live read 61 min ago: open")
        f = base; f.lastOpenedByWisp = ago(45)
        check(M.route(f, now: now) == .openMail, "working hours, opened 45 min ago: may open")
        let week: Set<Int> = [0, 1, 2, 3, 4]
        check(!M.offHours(start: 540, end: 1020, days: week, minute: 600, weekday: 2), "Wed 10:00: working")
        check(M.offHours(start: 540, end: 1020, days: week, minute: 1080, weekday: 2), "Wed 18:00: off")
        check(M.offHours(start: 540, end: 1020, days: week, minute: 480, weekday: 2), "Wed 08:00: off")
        check(M.offHours(start: 540, end: 1020, days: week, minute: 600, weekday: 5), "Sat 10:00: off")
        check(M.offHours(start: 540, end: 540, days: week, minute: 600, weekday: 2), "no working window: off")
        check(!M.offHours(start: 1320, end: 360, days: [0], minute: 1380, weekday: 0), "night shift Mon 23:00: working")
        check(!M.offHours(start: 1320, end: 360, days: [0], minute: 120, weekday: 1), "night shift Tue 02:00: working")
        check(M.offHours(start: 1320, end: 360, days: [0], minute: 120, weekday: 0), "night shift Mon 02:00: off")
        check(M.offHoursOpenEvery == 3600, "once an hour off-hours")

        // Offline: Mail couldn't fetch, so it isn't opened (and the 30-minute clock isn't spent).
        f = base; f.online = false
        check(M.route(f, now: now) == .none(.offline), "offline: leave Mail closed")
        f = base; f.online = false; f.indexRead = true
        check(M.route(f, now: now) == .none(.offline), "offline with an index read: the index stands")
        f = base; f.online = false; f.mailOpen = true
        check(M.route(f, now: now) == .mailApp, "offline but Mail open: still read it")
        f = base; f.online = false; f.automationAllowed = false
        check(M.route(f, now: now) == .none(.automationNotAllowed), "offline and not allowed: the permission reason")

        // Fetching: done only after fetchIdlePolls idle polls in a row.
        check(!M.fetchSettled([]), "no polls: not settled")
        check(!M.fetchSettled([0]), "one idle poll: not yet")
        check(M.fetchSettled([0, 0]), "two idle polls: settled")
        check(!M.fetchSettled([0, 0, 3]), "fetching again: not settled")
        check(M.fetchSettled([2, 1, 0, 0]), "fetch finished: settled")
        check(!M.fetchSettled([0, nil]), "an unreadable poll isn't idle")
        check(M.fetchWaitOver([nil, nil, nil]), "count never readable: read anyway")
        check(!M.fetchWaitOver([nil, 2, nil]), "fetching, one poll unreadable: keep waiting")
        check(M.fetchTimeout == 300 && M.fetchIdlePolls == 2, "5-minute fetch cap, two idle polls")

        // The limits and the words Brief shows.
        check(M.openEvery == 1800 && M.userQuitGrace == 900 && M.staleAfter == 1800 && M.gmailFreshFor == 1800,
              "30-minute rate limit, 15-minute quit grace, 30-minute staleness")
        check(M.readingNote == "Reading Mail…" && M.readingKind == "mail_recaching", "Brief's words and kind")

        if failed > 0 { print("MailRecacheChecks: \(failed) of \(count) failed"); exit(1) }
        print("MailRecacheChecks: \(count) checks passed")
    }
}
