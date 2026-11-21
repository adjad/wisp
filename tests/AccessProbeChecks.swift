// Contract for Full Disk Access's live probe (app/Sources/WispApp/AccessProbe.swift),
// the reopen prompt's timing, the relaunch decision that depends on the probe
// (PrivacyRelaunch.swift) and the per-version privacy names (PrivacyNames.swift).
// Pure: every disk answer is a synthetic closure, nothing is opened, read or
// launched, and no path of this Mac's is touched.
import Foundation

@main
struct AccessProbeChecks {
    static var count = 0

    static func check(_ condition: Bool, _ message: String) {
        count += 1
        if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
    }

    typealias P = AccessProbe
    typealias Store = AccessProbe.Store

    static func main() {
        states()
        probeThroughAFakeDisk()
        gains()
        reopenPrompt()
        relaunch()
        names()
        print("AccessProbeChecks: \(count) checks passed")
    }

    static func states() {
        check(P.decide([.messages: .readable, .mail: .readable, .notes: .readable]) == .on, "all readable is on")
        check(P.decide([.messages: .refused, .mail: .refused, .notes: .refused]) == .off, "none readable is off")
        check(P.decide([.messages: .readable, .mail: .refused, .notes: .readable]) == .partial(unreadable: [.mail]),
              "some readable is partial, naming what isn't")
        check(P.decide([.messages: .refused, .mail: .readable, .notes: .refused])
                == .partial(unreadable: [.messages, .notes]), "the unreadable list is sorted")
        check(P.decide([:]) == .unknown && P.decide([.messages: .missing, .mail: .missing, .notes: .missing]) == .unknown,
              "no store on this Mac is unknown")
        check(P.decide([.messages: .readable, .mail: .missing, .notes: .missing]) == .on,
              "a store that doesn't exist (Mail never set up) is not counted against on")
        check(P.decide([.messages: .refused, .mail: .missing, .notes: .missing]) == .off,
              "nor against off")
        check(P.decide([.messages: .missing, .mail: .refused, .notes: .readable]) == .partial(unreadable: [.mail]),
              "nor against partial")
        check(P.State.on.works && P.State.partial(unreadable: [.mail]).works && !P.State.off.works && !P.State.unknown.works,
              "on and partial work; off and unknown don't")
        check(P.outcome(openError: 0) == .readable && P.outcome(openError: EPERM) == .refused
                && P.outcome(openError: EACCES) == .refused && P.outcome(openError: ENOENT) == .missing
                && P.outcome(openError: EBUSY) == .missing, "errno to outcome: EPERM and EACCES are macOS's refusal")
        check(P.partialNote([.mail]).contains("Mail") && P.partialNote([.mail, .notes]).contains("Mail and Notes"),
              "the partial note names the stores")
    }

    /// A fake disk: `openErrors` per path (missing keys are ENOENT), `folders` listings.
    static func fakeDisk(open: [String: Int32] = [:], lists: [String: Result<[String], P.FileSystem.Errno>] = [:])
        -> P.FileSystem {
        P.FileSystem(open: { open[$0] ?? ENOENT }, list: { lists[$0] ?? .failure(P.FileSystem.Errno(code: ENOENT)) })
    }

    static func probeThroughAFakeDisk() {
        let home = "/home/synthetic"
        let chat = home + "/Library/Messages/chat.db"
        let notes = home + "/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"
        let mailFolder = home + "/Library/Mail"
        let index10 = mailFolder + "/V10/MailData/Envelope Index"
        let index9 = mailFolder + "/V9/MailData/Envelope Index"

        let nothing = P.probe(home: home, fakeDisk())
        check(nothing.state == .unknown, "a Mac with none of the three stores")

        let allIn = P.probe(home: home, fakeDisk(open: [chat: 0, notes: 0, index10: 0],
                                                 lists: [mailFolder: .success(["V10", "Other"])]))
        check(allIn.state == .on && allIn.outcomes[.mail] == .readable, "every store opens")

        let allOut = P.probe(home: home, fakeDisk(open: [chat: EPERM, notes: EPERM],
                                                  lists: [mailFolder: .failure(P.FileSystem.Errno(code: EPERM))]))
        check(allOut.state == .off, "macOS refuses all three")

        let scoped = P.probe(home: home, fakeDisk(open: [chat: 0, notes: EPERM],
                                                  lists: [mailFolder: .failure(P.FileSystem.Errno(code: EPERM))]))
        check(scoped.state == .partial(unreadable: [.mail, .notes]), "a folder-scoped grant: Messages only")

        let neverSetUpMail = P.probe(home: home, fakeDisk(open: [chat: 0, notes: 0]))
        check(neverSetUpMail.outcomes[.mail] == .missing && neverSetUpMail.state == .on, "Mail never set up isn't counted")

        let newestIndex = P.probe(home: home, fakeDisk(open: [chat: ENOENT, notes: ENOENT, index10: EPERM, index9: 0],
                                                       lists: [mailFolder: .success(["V9", "V10"])]))
        check(newestIndex.outcomes[.mail] == .refused, "the newest V folder decides, not a lingering older one")

        let emptyFolder = P.probe(home: home, fakeDisk(lists: [mailFolder: .success([])]))
        check(emptyFolder.outcomes[.mail] == .readable,
              "a Mail folder that lists is readable even before it has an index (the listing is what macOS guards)")

        let lockedFile = P.probe(home: home, fakeDisk(open: [chat: EBUSY]))
        check(lockedFile.state == .unknown, "an error that isn't a permission refusal says nothing")
    }

    static func gains() {
        let before: [Store: P.Outcome] = [.messages: .refused, .mail: .refused, .notes: .readable]
        let after: [Store: P.Outcome] = [.messages: .readable, .mail: .refused, .notes: .readable]
        check(P.gained(from: before, to: after) == [.messages], "only a store that just began to read is gained")
        check(P.gained(from: after, to: after).isEmpty, "no change gains nothing")
        check(P.gained(from: [:], to: after) == [.messages, .notes],
              "from nothing, everything that reads is new")
    }

    static func reopenPrompt() {
        let t: TimeInterval = 5_000
        func shows(_ returned: TimeInterval?, at now: TimeInterval, _ state: P.State, dismissed: Bool = false) -> Bool {
            ReopenPrompt.shows(returnedAt: returned, now: now, access: state, dismissed: dismissed)
        }
        check(ReopenPrompt.delay == 10, "the prompt waits 10 seconds")
        check(!shows(t, at: t, .off) && !shows(t, at: t + 9.9, .off), "not before 10 s after coming back")
        check(shows(t, at: t + 10, .off) && shows(t, at: t + 60, .off), "shown once 10 s have passed and the probe still fails")
        check(!shows(t, at: t + 10, .on) && !shows(t, at: t + 10, .partial(unreadable: [.mail])),
              "never when the probe passes, even partly")
        check(!shows(t, at: t + 10, .unknown), "never when nothing could be probed")
        check(!shows(nil, at: t + 100, .off), "never without a trip to System Settings")
        check(!shows(t, at: t + 100, .off, dismissed: true), "Not now keeps it away")
        check(!shows(t, at: t - 1, .off), "a clock that went back shows nothing")
    }

    static func relaunch() {
        let now: TimeInterval = 1_000_000
        let settings = PrivacyRelaunch.Quit.appleEvent(
            senderBundleID: "com.apple.systempreferences",
            senderPath: "/System/Applications/System Settings.app/Contents/MacOS/System Settings")
        func decide(_ quit: PrivacyRelaunch.Quit, _ state: P.State, stillNeeded: Bool = false) -> Bool {
            PrivacyRelaunch.shouldRelaunch(quit, paneOpenedAt: now - 30, now: now,
                                           accessProbePasses: state.works, restartStillNeeded: stillNeeded)
        }
        check(decide(settings, .off), "macOS's Quit & Reopen with the probe failing: reopen as before")
        check(decide(settings, .unknown), "with nothing to probe: reopen as before")
        check(!decide(settings, .on), "no relaunch when the probe passes")
        check(!decide(settings, .partial(unreadable: [.mail])), "no relaunch when the probe passes partly")
        check(decide(settings, .on, stillNeeded: true), "a switch that still needs a restart (Screen Recording) keeps it")
        check(!decide(.notAppleEvent, .off) && !decide(.notAppleEvent, .unknown),
              "Wisp's own quit never relaunches, probe or not")
        let dock = PrivacyRelaunch.Quit.appleEvent(senderBundleID: "com.apple.dock", senderPath: nil)
        check(!decide(dock, .off), "a Dock quit never relaunches")
        let logout = PrivacyRelaunch.Quit.appleEvent(senderBundleID: "com.apple.systempreferences", senderPath: nil, reason: "logo")
        check(!decide(logout, .off), "a logout never relaunches")
        check(PrivacyRelaunch.shouldRelaunch(settings, paneOpenedAt: nil, now: now), "the default arguments are the old rule")
    }

    static func names() {
        func v(_ major: Int, _ minor: Int = 0) -> OperatingSystemVersion {
            OperatingSystemVersion(majorVersion: major, minorVersion: minor, patchVersion: 0)
        }
        typealias N = PrivacyNames
        check(N.accessibility(for: v(26)) == "Accessibility" && N.accessibility(for: v(26, 4)) == "Accessibility",
              "macOS 26 says Accessibility")
        check(N.accessibility(for: v(27, 0)) == "Device Control and Data Access"
                && N.accessibility(for: v(27, 2)) == "Device Control and Data Access", "27.0 to 27.2 say Device Control and Data Access")
        check(N.accessibility(for: v(27, 3)) == "Accessibility" && N.accessibility(for: v(28, 0)) == "Accessibility",
              "27.3 and 28.0 say Accessibility again")
        check(N.protectionOverride(for: v(27, 3)) == "System & App Protection Override"
                && N.protectionOverride(for: v(28, 0)) == "System & App Protection Override", "27.3 and later have the override pane")
        check(N.protectionOverride(for: v(26)) == nil && N.protectionOverride(for: v(27, 2)) == nil, "earlier versions have none")
        check(N.accessibilityWithFormerName(for: v(27, 1)) == "Device Control and Data Access (called Accessibility before macOS 27)",
              "27.1 names the old name too")
        check(N.accessibilityWithFormerName(for: v(26)) == "Accessibility" && N.accessibilityWithFormerName(for: v(27, 3)) == "Accessibility",
              "no old-name note where the name is Accessibility")
        check(N.accessibilityPath(for: v(27, 0)) == "System Settings › Privacy & Security › Device Control and Data Access"
                && N.accessibilityPath(for: v(27, 3)) == "System Settings › Privacy & Security › Accessibility",
              "the path names the pane of that version")
        for version in [v(26), v(27, 0), v(27, 2), v(27, 3), v(28)] {
            check(N.accessibilityPane(for: version).hasSuffix("Privacy_Accessibility")
                    && !N.accessibilityPane(for: version).contains("preference.security"),
                  "one modern deep link for every version")
        }
        check(N.era(for: v(27, 0)) == .deviceControl && N.era(for: v(27, 3)) == .accessibilityWithOverride
                && N.era(for: v(26, 9)) == .accessibility, "the era boundaries")
    }
}
