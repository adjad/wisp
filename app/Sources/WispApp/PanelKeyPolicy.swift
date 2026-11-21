import Foundation

/// Who gets the keyboard when the notch panel opens or is clicked. Pure (OverlayPanel applies it).
///
/// 2026-11-21, macOS 27.3 (26C5035r): typing into an open panel went to the app in front (Safari's
/// address bar, Claude Code's prompt). The panel is a non-activating panel that was made key without
/// Wisp ever becoming the active app; AppKit then reported the panel key while the keyboard stayed
/// with the frontmost app. So an open the person asked for, and any press inside the panel, make the
/// panel key AND make Wisp the frontmost app. The only exception is one open nobody asked for (the
/// permission page coming back, the know-you offer), which shows without taking the keyboard, and
/// that exception lasts for exactly one open.
enum PanelKeyPolicy {
    enum Opening: Equatable {
        /// Show the panel and leave the keyboard where it is.
        case showOnly
        /// Make the panel key and bring Wisp to the front.
        case takeKeyboard
    }

    /// What an opening does. `revealsWithoutKey` is read once and is cleared by the same open.
    static func opening(revealsWithoutKey: Bool) -> Opening {
        revealsWithoutKey ? .showOnly : .takeKeyboard
    }

    /// The flag after an open or a close: always cleared, so it never outlives one open.
    static func flagAfterReveal(_ flag: Bool) -> Bool { false }

    /// A press inside the panel takes the keyboard when the panel isn't key or Wisp isn't the
    /// frontmost app (AppKit can say the app is active while another app is in front).
    static func pressTakesKeyboard(isPress: Bool, isKey: Bool, wispFrontmost: Bool) -> Bool {
        isPress && (!isKey || !wispFrontmost)
    }

    /// On close, the keyboard goes back to the app that was in front, unless another Wisp window
    /// (Settings, the chat window) is key: the person moved there on purpose.
    static func returnsKeyboard(otherWispWindowKey: Bool) -> Bool { !otherWispWindowKey }

    /// Taking the keyboard also activates Wisp unless it already is the frontmost app.
    static func activates(wispFrontmost: Bool) -> Bool { !wispFrontmost }
}
