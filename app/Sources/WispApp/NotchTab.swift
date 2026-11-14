import Foundation

/// The notch panel's tabs. Ask is the conversation; Brief is what's going on,
/// read only; Day is the rest of today, hour by hour; Meet listens to a meeting
/// and writes its notes. Research is not in the
/// strip: it is a mode of Ask (`/research`, a job opened from the window, New
/// research), reached through `.research` and left by choosing a tab.
///
/// Pure logic, checked by tests/NotchTabChecks.swift without the app.
enum NotchTab: Int, CaseIterable, Equatable {
    case ask, brief, day, research, meet

    /// The tabs shown in the strip, in order, with ⌘1 to ⌘4.
    static var strip: [NotchTab] { meetHidden ? [.ask, .brief, .day] : [.ask, .brief, .day, .meet] }

    /// Takes Meet out of the strip. Nothing sets it today: on a build with no
    /// on-device engine (the Intel preview) meetings are heard by the chosen cloud
    /// model, and when that model can't hear audio the service refuses the start
    /// with a plain reason, which the Meet tab shows (AppDelegate, at launch).
    nonisolated(unsafe) static var meetHidden = false
    static let meetHiddenReason = "Meeting notes need a model that can hear audio: Wisp's on-device engine, or a cloud model that takes audio."

    var title: String {
        switch self {
        case .ask: return "Ask"
        case .brief: return "Brief"
        case .day: return "Day"
        case .research: return "Research"
        case .meet: return "Meet"
        }
    }

    /// The strip tab drawn as selected. Research is a mode of Ask.
    var stripTab: NotchTab { self == .research ? .ask : self }

    /// ⌘1 to ⌘4 while the panel has focus; Research has none.
    var shortcutDigit: Character? {
        Self.strip.firstIndex(of: self).map { Character(String($0 + 1)) }
    }

    /// Brief and Day load and show the day, not a prompt field.
    var showsDay: Bool { self == .brief || self == .day }

    /// Ask and Research have the prompt field; the cursor goes there when they open.
    var hasPrompt: Bool { self == .ask || self == .research }
}

/// The Meet tab's opening paragraph. `cloudProvider` is who hears the audio besides
/// this Mac (GET /meetings `audio_destination`), nil on the on-device engine.
enum MeetingCopy {
    static func home(hearsCallAudio: Bool, cloudProvider: String?) -> String {
        let source = hearsCallAudio
            ? "Wisp listens through the microphone and to the sound this Mac plays, so a call on headphones is heard too. It"
            : "Wisp listens through the microphone. To hear the other side of a call on headphones, turn on Screen Recording for Wisp first. It"
        let after = "When you stop, it writes a summary, the decisions, the action items and a follow-up. "
        guard let provider = cloudProvider, !provider.isEmpty else {
            return source + " writes the words down on this Mac. " + after
                + "No audio is kept and nothing leaves your Mac."
        }
        return source + " sends the audio to \(provider), where the model you chose writes the words down. " + after
            + "The audio and the transcript are sent to \(provider); Wisp keeps no audio, only the text."
    }
}

/// What fills the open panel under its header. Tabs sit side by side, in strip
/// order; quick settings is pushed over a tab and a permission page over either.
/// The reset card and Getting to know you replace a tab in place.
enum NotchPage: Hashable {
    case tab(NotchTab)
    case settings
    case permissions
    case reset
    case knowYou

    /// How far down the push stack: tabs and cards 0, settings 1, permissions 2.
    var depth: Int {
        switch self {
        case .tab, .reset, .knowYou: return 0
        case .settings: return 1
        case .permissions: return 2
        }
    }

    /// The page shown for the panel's state, most specific first, as
    /// OverlayView.expandedPanel lays them out.
    static func shown(tab: NotchTab, settings: Bool, permissions: Bool, reset: Bool, knowYou: Bool) -> NotchPage {
        if reset { return .reset }
        if knowYou { return .knowYou }
        if permissions { return .permissions }
        if settings { return .settings }
        return .tab(tab)
    }

    /// How a change from one page to another slides (NotchPageMotion).
    /// - A deeper page pushes in from the trailing edge; the page under it
    ///   drifts a step toward the leading edge as it fades.
    /// - Going back is the mirror: the page leaves to the trailing edge, and the
    ///   one under it returns from a step to the leading side.
    /// - Tabs slide a step by strip order (Research counts as Ask).
    /// - The reset card and Getting to know you crossfade in place.
    static func motion(from old: NotchPage, to new: NotchPage) -> NotchPageMotion {
        guard old != new else { return .none }
        if new.depth > old.depth { return NotchPageMotion(insertion: 3, removal: -1) }
        if new.depth < old.depth { return NotchPageMotion(insertion: -1, removal: 3) }
        guard case .tab(let from) = old, case .tab(let to) = new,
              let a = NotchTab.strip.firstIndex(of: from.stripTab),
              let b = NotchTab.strip.firstIndex(of: to.stripTab), a != b else { return .none }
        return b > a ? NotchPageMotion(insertion: 1, removal: -1) : NotchPageMotion(insertion: -1, removal: 1)
    }
}

/// Where an arriving page starts and a leaving page ends, in slide steps
/// (Theme.Motion.slideStep): positive toward the trailing edge. Both fade.
struct NotchPageMotion: Equatable {
    var insertion: Double
    var removal: Double
    static let none = NotchPageMotion(insertion: 0, removal: 0)
}

/// Which tab the panel opens on. ⌥Space and the menu-bar icon always open Ask.
/// Hovering opens the tab last used, if the panel closed within the last ten
/// minutes; after that it is back to Ask.
struct NotchTabMemory: Equatable {
    static let hoverWindow: TimeInterval = 10 * 60
    private(set) var closedAt: Date?

    mutating func panelClosed(at date: Date) { closedAt = date }

    func tabForOpening(current: NotchTab, byHover: Bool, at date: Date) -> NotchTab {
        guard byHover, let closedAt, date.timeIntervalSince(closedAt) <= Self.hoverWindow else { return .ask }
        return current
    }
}

/// The setup card at the top of Ask, shown when Wisp can't answer yet.
enum NotchSetupCard: Equatable {
    /// Not shown: setup is finished, or was never needed.
    case hidden
    /// The full card. It holds the panel open, and the panel opens on Ask.
    case open
    /// Set aside with Later: one line on Ask and a "!" on the collapsed bar,
    /// until Wisp can answer.
    case later
    /// Wisp became ready while notifications were off: no card, only a gentle
    /// dot on the collapsed bar ("Wisp is ready") until the panel is opened.
    case ready

    /// The setup card or its one-line row is on Ask (not the ready dot).
    var showsOnAsk: Bool { self == .open || self == .later }

    /// The quiet ready dot, on the collapsed bar and (wherever the bar draws
    /// nothing) on the menu-bar icon. Both read this, so both clear together
    /// when the panel is opened (AppDelegate.readyLineSeen sets .hidden).
    var showsReadyDot: Bool { self == .ready }

    /// What the card becomes when the service reports readiness. A card set
    /// aside goes away by itself; an open one waits for Done, so the person
    /// sees the result first. The ready dot never outlives readiness.
    func afterReadiness(_ ready: Bool) -> NotchSetupCard {
        if ready { return self == .later ? .hidden : self }
        return self == .ready ? .hidden : self
    }

    /// The tab an opening panel shows: Ask while the full card is up.
    func tabForOpening(_ remembered: NotchTab) -> NotchTab { self == .open ? .ask : remembered }
}
