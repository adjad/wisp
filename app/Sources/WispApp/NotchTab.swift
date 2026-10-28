import Foundation

/// The notch panel's tabs. Ask is the conversation; Brief is what's going on,
/// read only; Day is the rest of today, hour by hour. Research is not in the
/// strip: it is a mode of Ask (`/research`, a job opened from the window, New
/// research), reached through `.research` and left by choosing a tab.
///
/// Pure logic, checked by tests/NotchTabChecks.swift without the app.
enum NotchTab: Int, CaseIterable, Equatable {
    case ask, brief, day, research

    /// The tabs shown in the strip, in order, with ⌘1 to ⌘3.
    static let strip: [NotchTab] = [.ask, .brief, .day]

    var title: String {
        switch self {
        case .ask: return "Ask"
        case .brief: return "Brief"
        case .day: return "Day"
        case .research: return "Research"
        }
    }

    /// The strip tab drawn as selected. Research is a mode of Ask.
    var stripTab: NotchTab { self == .research ? .ask : self }

    /// ⌘1 to ⌘3 while the panel has focus; Research has none.
    var shortcutDigit: Character? {
        Self.strip.firstIndex(of: self).map { Character(String($0 + 1)) }
    }

    /// Brief and Day load and show the day, not a prompt field.
    var showsDay: Bool { self == .brief || self == .day }
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
