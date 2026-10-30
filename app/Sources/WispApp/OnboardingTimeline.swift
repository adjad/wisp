import Foundation

extension Notification.Name {
    /// Posted each time ⌥Space opens Ask, so the setup window's last step can
    /// show that the shortcut really works on this Mac.
    static let wispAskHotkeyPressed = Notification.Name("wisp.askHotkeyPressed")
}

/// A short local record of how first-run setup went: steps reached, download
/// started or stopped, model ready, setup finished. Report a Problem includes it,
/// so a stuck setup can be told apart from a slow one. It holds event names and
/// times only, never anything the person typed or anything read from their Mac.
enum OnboardingTimeline {
    static let key = "wisp.onboarding.timeline"
    /// Oldest entries drop off past this, so the record stays small.
    static let limit = 80

    static func record(_ event: String, at date: Date = Date(), defaults: UserDefaults = .standard) {
        var events = defaults.stringArray(forKey: key) ?? []
        // The same event twice in a row (a state the window re-enters on every
        // poll) is noise; keep the first.
        if let last = events.last, last.hasSuffix(" " + event) { return }
        events.append(ISO8601DateFormatter().string(from: date) + " " + event)
        if events.count > limit { events.removeFirst(events.count - limit) }
        defaults.set(events, forKey: key)
    }

    static func entries(defaults: UserDefaults = .standard) -> [String] {
        defaults.stringArray(forKey: key) ?? []
    }
}
