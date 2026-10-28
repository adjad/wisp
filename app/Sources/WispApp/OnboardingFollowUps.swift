import Foundation

// What setup still owes the person once they look away from the setup window:
// the first Brief when the model arrives late, "Wisp is ready" when
// notifications are off, a word about a download a closed lid paused, and one
// later offer of Getting to know you after a skip. Pure (Foundation only), so
// each rule is checked without a window, a service or a notification center.

/// The first Brief, composed as soon as setup is answered instead of at its
/// hour. Owed once setup reaches its last step (or finishes); asked for once,
/// the first time the model can answer after that. The service records it
/// once too (POST /assistant/onboarding/brief_ready), so a repeat is harmless.
enum FirstBrief {
    static let owedKey = "wisp.onboarding.firstBriefOwed"
    static let requestedKey = "wisp.onboarding.firstBriefRequested"

    static func asksNow(owed: Bool, requested: Bool, canAnswer: Bool) -> Bool {
        owed && !requested && canAnswer
    }
}

/// "Wisp is ready", for someone who never allowed notifications: the notch
/// says it instead, in the open panel, until they have opened it and seen it.
enum ReadyNotice {
    static let title = "Wisp is ready"
    static let body = "Press ⌥ Space and ask about your day."
    static let notchLine = "Wisp is ready. Ask about your day here, or press ⌥ Space any time."
    /// How long the line stays once the panel is open in front of the person.
    static let seenFor: TimeInterval = 10
    /// Unseen, it waits this long at most (a day): long enough for the next
    /// time the notch is opened, not forever.
    static let unseenFor: TimeInterval = 24 * 3600
}

/// A closed lid sleeps the Mac and pauses the model download with it (Wisp
/// only holds off idle sleep). Nothing restarts the engine across sleep, so its
/// saved queue (which resumes rows after a restart) doesn't come into it: the
/// same transfer carries on after wake once the network is back, and one that
/// can't (the engine gives up after five minutes without progress and one
/// retry) shows as stopped, where Try again starts the download again.
enum SleepPause {
    /// The download at wake: which model and its received bytes, or that it stopped.
    enum Transfer: Equatable {
        case downloading(model: String, bytes: Double)
        case stopped
        /// Finished, never started, or no longer a download at all.
        case other
    }

    /// When the Mac woke, and what each download had received by the time it
    /// went to sleep (keyed by model, so two downloads never share one mark).
    struct Wake: Equatable {
        var at: Date
        var bytesAtSleep: [String: Double]
    }

    /// After this, the download row alone tells the story.
    static let shownFor: TimeInterval = 30 * 60
    /// Bytes past the sleep mark that count as moving again (a few MB, so a
    /// last chunk flushed at wake isn't taken for the download resuming).
    static let movedBytes: Double = 8 * 1024 * 1024

    static let pausedLine = "Your Mac slept, so the download paused. It usually picks up again by itself in a few minutes."
    static let stoppedLine = "The download stopped while your Mac slept."

    /// The running downloads when the Mac went to sleep, each with its bytes;
    /// nil when nothing was downloading.
    static func atSleep(_ transfers: [(model: String, state: String, bytes: Double)]) -> [String: Double]? {
        let running = transfers.filter { $0.state == "downloading" || $0.state == "pending" }
        guard !running.isEmpty else { return nil }
        return Dictionary(running.map { ($0.model, $0.bytes) }, uniquingKeysWith: max)
    }

    /// The card's line after a wake, or nil once there is nothing to add.
    static func line(_ wake: Wake?, now: Transfer, at date: Date) -> String? {
        guard let wake, date.timeIntervalSince(wake.at) < shownFor else { return nil }
        switch now {
        case .downloading(let model, let bytes):
            // A download that started after the wake was never paused by it.
            guard let mark = wake.bytesAtSleep[model] else { return nil }
            return bytes >= mark + movedBytes ? nil : pausedLine
        case .stopped: return stoppedLine
        case .other: return nil
        }
    }
}

/// Skipping About you in the setup window used to end Getting to know you's
/// offers for good. Now the notch may offer it once more, a few days later and
/// only at a quiet moment (KnowYouOffer.quiet), and never after that: a Later
/// there, or any other answer, is the last word. A finished About you is never
/// offered again (the service stops offering it).
enum KnowYouSecondOffer {
    static let decidedAtKey = "wisp.knowYou.windowDecidedAt"
    static let offeredKey = "wisp.knowYou.secondOfferShown"
    static let wait: TimeInterval = 3 * 24 * 3600

    /// Whether the window's decision still holds the notch's offers back.
    static func holdsBack(decidedInWindow: Bool, decidedAt: Date?, offered: Bool, now: Date) -> Bool {
        guard decidedInWindow else { return false }
        guard !offered, let decidedAt else { return true }
        return now.timeIntervalSince(decidedAt) < wait
    }
}
