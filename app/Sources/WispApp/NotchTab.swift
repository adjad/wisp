import Foundation

/// The notch panel's tabs. Ask is the conversation; Today is the brief and the
/// day at a glance; Research turns the next prompt into a research plan.
///
/// Pure logic, checked by tests/NotchTabChecks.swift without the app.
enum NotchTab: Int, CaseIterable, Equatable {
    case ask, today, research

    var title: String {
        switch self {
        case .ask: return "Ask"
        case .today: return "Today"
        case .research: return "Research"
        }
    }

    /// ⌘1 to ⌘3 while the panel has focus.
    var shortcutDigit: Character { Character(String(rawValue + 1)) }
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

    /// What the card becomes when the service reports readiness. A card set
    /// aside goes away by itself; an open one waits for Done, so the person
    /// sees the result first.
    func afterReadiness(_ ready: Bool) -> NotchSetupCard {
        ready && self == .later ? .hidden : self
    }

    /// The tab an opening panel shows: Ask while the full card is up.
    func tabForOpening(_ remembered: NotchTab) -> NotchTab { self == .open ? .ask : remembered }
}

/// The Today tab's short "Up next" list.
enum NotchUpNext {
    /// The first `limit` items still under way or ahead at `now`, in their
    /// original order. An item with no end counts as under way until its start
    /// plus `openEndedGrace`.
    static func upcoming<Item>(_ items: [Item], start: (Item) -> Double, end: (Item) -> Double?,
                               now: Double, limit: Int = 3, openEndedGrace: Double = 15 * 60) -> [Item] {
        Array(items.filter { (end($0) ?? start($0) + openEndedGrace) > now }.prefix(limit))
    }
}
