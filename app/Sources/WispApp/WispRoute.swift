import Foundation

/// A part of Wisp someone asked to see. Every control that opens a part of the
/// app names one of these and hands it to `WispRouter`, so moving a feature
/// (into the notch, or into the Wisp window) changes one handler in
/// AppDelegate rather than every button that leads to it.
enum WispRoute: Equatable {
    /// A chat in the Wisp window, by service session id. Nil opens the window's
    /// current chat (a new one when none is selected); an empty id opens a new chat.
    case chat(sessionId: String?)
    case today(focus: TodayFocus)
    case research
    case memory(MemoryFocus)
    case settings
    case setup

    enum TodayFocus: Equatable {
        case attention, plan

        /// TodayView's tab index: 0 is "Needs my attention", 1 is "Day plan".
        var tab: Int { self == .attention ? 0 : 1 }
    }

    enum MemoryFocus: Equatable {
        case facts, learned
    }
}

/// The one place routes are opened. AppDelegate installs the handler at launch;
/// without one (the QA harnesses, which install no AppDelegate) opening a route
/// does nothing.
@MainActor
final class WispRouter {
    static let shared = WispRouter()
    var handler: ((WispRoute) -> Void)?

    func open(_ route: WispRoute) { handler?(route) }
}
