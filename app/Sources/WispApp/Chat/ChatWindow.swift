import AppKit
import SwiftUI

/// Owns the single reusable Wisp Chat window: a real resizable window, not the
/// notch panel, for conversations that are long or that need real room. It
/// survives the notch closing, and its chats live in the service, so closing
/// the window loses nothing.
@MainActor
final class ChatWindowController {
    private var window: NSWindow?
    private var closeObserver: NSObjectProtocol?
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
        Task { await store.refresh() }
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
            win.setFrameAutosaveName("WispChatWindow")
            window = win
            // Wisp is a menu-bar app. The chat window is the one place that wants
            // a Dock icon, so the app takes one only while this window is open.
            closeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: win, queue: .main
            ) { _ in
                // setActivationPolicy returns a Bool; discard it so the closure is Void.
                MainActor.assumeIsolated { _ = NSApp.setActivationPolicy(.accessory) }
            }
        }
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
