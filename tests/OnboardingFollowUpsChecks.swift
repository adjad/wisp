import Foundation

// Pure checks for what setup still owes the person after the setup window
// (app/Sources/WispApp/OnboardingFollowUps.swift) and the report's onboarding
// section (DiagnosticReport.withOnboarding). No window, service or notification.
@main
struct OnboardingFollowUpsChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func main() {
        // The first Brief: owed by the last step or Done, asked for once, only with the model.
        check(FirstBrief.asksNow(owed: true, requested: false, canAnswer: true), "Owed and the model answers: ask now")
        check(!FirstBrief.asksNow(owed: true, requested: false, canAnswer: false),
              "Owed without the model: wait for it (a Brief made without it would stand as the day's)")
        check(!FirstBrief.asksNow(owed: true, requested: true, canAnswer: true), "Asked once, never again")
        check(!FirstBrief.asksNow(owed: false, requested: false, canAnswer: true),
              "Setup never reached its last step: nothing is owed")
        check(FirstBrief.owedKey.hasPrefix("wisp.") && FirstBrief.requestedKey.hasPrefix("wisp."),
              "A reset clears both (its prefix sweep)")

        // "Wisp is ready" without notifications.
        check(ReadyNotice.notchLine.contains("⌥ Space") && ReadyNotice.body.contains("⌥ Space"),
              "Both ways of saying it teach the shortcut")
        check(ReadyNotice.seenFor < ReadyNotice.unseenFor, "Seen, the line goes soon; unseen, it waits")
        for text in [ReadyNotice.notchLine, ReadyNotice.body, SleepPause.pausedLine, SleepPause.stoppedLine] {
            for word in ["engine", "oMLX", "backend", "Smart Search", "Research"] {
                check(!text.contains(word), "No \(word) in \(text.debugDescription)")
            }
        }

        // A closed lid during the download.
        let mb = 1024.0 * 1024
        check(SleepPause.atSleep([]) == nil, "Nothing downloading: nothing to say on wake")
        check(SleepPause.atSleep([(model: "a", state: "completed", bytes: 5_000 * mb),
                                  (model: "b", state: "failed", bytes: 10 * mb)]) == nil,
              "Finished or stopped before sleep: nothing to say on wake")
        check(SleepPause.atSleep([(model: "a", state: "downloading", bytes: 900 * mb)]) == ["a": 900 * mb],
              "Downloading: its bytes, under its model")
        check(SleepPause.atSleep([(model: "a", state: "pending", bytes: 0)]) == ["a": 0],
              "About to start counts as downloading")
        check(SleepPause.atSleep([(model: "a", state: "downloading", bytes: 900 * mb),
                                  (model: "b", state: "pending", bytes: 40 * mb),
                                  (model: "c", state: "completed", bytes: 3_000 * mb)])
              == ["a": 900 * mb, "b": 40 * mb], "Two downloads keep a mark each, never one summed mark")
        let woke = Date(timeIntervalSince1970: 1_000_000)
        let wake = SleepPause.Wake(at: woke, bytesAtSleep: ["a": 900 * mb, "b": 40 * mb])
        check(SleepPause.line(nil, now: .downloading(model: "a", bytes: 900 * mb), at: woke) == nil, "No wake, no line")
        check(SleepPause.line(wake, now: .downloading(model: "a", bytes: 901 * mb), at: woke.addingTimeInterval(5))
              == SleepPause.pausedLine, "Not moving yet: paused, and that it usually carries on by itself")
        check(SleepPause.line(wake, now: .downloading(model: "a", bytes: 950 * mb), at: woke.addingTimeInterval(60)) == nil,
              "Moving again: the progress line says the rest")
        check(SleepPause.line(wake, now: .downloading(model: "b", bytes: 60 * mb), at: woke.addingTimeInterval(60)) == nil,
              "Measured against its own mark: the other download's bytes don't keep the line up")
        check(SleepPause.line(wake, now: .downloading(model: "z", bytes: 0), at: woke.addingTimeInterval(5)) == nil,
              "A download started after the wake was never paused by it")
        check(SleepPause.line(wake, now: .stopped, at: woke.addingTimeInterval(400)) == SleepPause.stoppedLine,
              "Stopped after the wake: say why, beside Try again")
        check(SleepPause.line(wake, now: .other, at: woke.addingTimeInterval(5)) == nil,
              "Finished (or no download any more): nothing to add")
        check(SleepPause.line(wake, now: .downloading(model: "a", bytes: 900 * mb),
                              at: woke.addingTimeInterval(SleepPause.shownFor)) == nil,
              "Half an hour on, the row alone tells it")
        check(!SleepPause.pausedLine.contains("Resume"), "Nothing to press after a sleep: it needs no Resume")

        // One later offer of Getting to know you after a skip in the window.
        let skipped = Date(timeIntervalSince1970: 2_000_000)
        func holds(_ decided: Bool, _ at: Date?, offered: Bool = false, after seconds: TimeInterval) -> Bool {
            KnowYouSecondOffer.holdsBack(decidedInWindow: decided, decidedAt: at, offered: offered,
                                         now: skipped.addingTimeInterval(seconds))
        }
        check(!holds(false, nil, after: 0), "Never decided in the window: the window holds nothing back")
        check(holds(true, skipped, after: 3600), "Just skipped: no offer yet")
        check(holds(true, skipped, after: KnowYouSecondOffer.wait - 1), "Not before the wait")
        check(!holds(true, skipped, after: KnowYouSecondOffer.wait), "A few days later: one offer may come")
        check(holds(true, skipped, offered: true, after: KnowYouSecondOffer.wait * 10), "Offered once: never again")
        check(holds(true, nil, after: KnowYouSecondOffer.wait * 10), "No date: held back (the app stamps one at launch)")
        check(KnowYouSecondOffer.wait >= 2 * 24 * 3600, "A few days, not the same day")

        // Report a Problem's onboarding section.
        let timeline = ["2026-10-27T09:00:00Z setup window opened", "2026-10-27T09:12:00Z setup finished"]
        let report = DiagnosticReport.withOnboarding(["surface": "chat"], timeline: timeline)
        check(report["onboarding"] as? [String] == timeline && report["surface"] as? String == "chat",
              "The timeline joins the report as its onboarding section")
        check(DiagnosticReport.withOnboarding(["surface": "chat"], timeline: [])["onboarding"] == nil,
              "No setup events: no section")
        check(DiagnosticReport.withOnboarding(["onboarding": ["kept"]], timeline: timeline)["onboarding"] as? [String] == ["kept"],
              "A caller's own onboarding entry is never replaced")
        check(DiagnosticReport.onboardingTimeline().isEmpty, "Unset (outside the app), the section reads nothing")
        let payload = DiagnosticReport.payload(metadata: report, details: nil, note: "", includeDetails: false)
        check(payload["onboarding"] as? [String] == timeline, "Event names and times survive the scrubber")

        print("PASS: onboarding follow-ups — first Brief, ready notice, sleep line, second offer, report section")
    }
}
