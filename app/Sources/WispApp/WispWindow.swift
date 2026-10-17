import AppKit
import SwiftUI

/// Owns the single reusable Wisp window: a real resizable window, not the notch
/// panel, for the work that needs room. Wisp starts in the notch; this window
/// opens only when asked for (the Dock, the menu bar, or a hand-off from the
/// notch). It survives the notch closing, and its chats live in the service, so
/// closing the window loses nothing.
@MainActor
final class WispWindowController {
    private static let autosaveName = "WispWindow"
    /// The autosave name this window had while it was the "Wisp Chat" window.
    private static let previousAutosaveName = "WispChatWindow"

    private var window: NSWindow?
    let store: ChatStore
    private let launchers: ChatLaunchers

    init(backend: ChatBackend, launchers: ChatLaunchers, debug: @escaping () -> Bool = { false }) {
        store = ChatStore(backend: backend, debug: debug)
        self.launchers = launchers
    }

    /// Open the window onto the chat list, or onto a new chat when there is none selected.
    func show() {
        present()
        if store.selected == nil { store.newChat() }
        Task { await store.refreshWhenReady() }
    }

    /// Open the window onto an existing chat by its service session id: the
    /// notch's "Open in Wisp Chat" hand-off. An empty id opens a new chat.
    func show(sessionId: String) {
        present()
        Task { await store.open(sessionId: sessionId) }
    }

    private func present() {
        if window == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1120, height: 740),
                               styleMask: [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView],
                               backing: .buffered, defer: false)
            win.title = "Wisp"
            win.titleVisibility = .hidden
            win.titlebarAppearsTransparent = true
            win.isMovableByWindowBackground = false
            win.contentView = NSHostingView(rootView: ChatRootView(store: store, launchers: launchers))
            win.center()
            win.minSize = NSSize(width: 820, height: 560)
            win.isReleasedWhenClosed = false
            Self.carryOverSavedFrame()
            win.setFrameAutosaveName(Self.autosaveName)
            window = win
        }
        NSApp.activate(ignoringOtherApps: true)
        if window?.isMiniaturized == true { window?.deminiaturize(nil) }
        window?.makeKeyAndOrderFront(nil)
    }

    /// Keep the window where the person last left it across the rename: copy the
    /// frame AppKit saved under the old name, once, if none is saved under the new one.
    private static func carryOverSavedFrame() {
        let defaults = UserDefaults.standard
        let key = "NSWindow Frame " + autosaveName
        guard defaults.object(forKey: key) == nil,
              let previous = defaults.string(forKey: "NSWindow Frame " + previousAutosaveName) else { return }
        defaults.set(previous, forKey: key)
    }
}
