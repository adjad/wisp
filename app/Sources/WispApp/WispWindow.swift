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
    let navigation = WispWindowNavigation()
    private let launchers: ChatLaunchers

    init(backend: ChatBackend, launchers: ChatLaunchers, debug: @escaping () -> Bool = { false }) {
        store = ChatStore(backend: backend, debug: debug)
        self.launchers = launchers
    }

    /// Open the window onto the chat list, or onto a new chat when there is none selected.
    func show() {
        navigation.section = .chats
        present()
        if store.selected == nil { store.newChat() }
        Task { await store.refreshWhenReady() }
    }

    /// Open the window onto an existing chat by its service session id: the
    /// notch's "Open in Wisp Chat" hand-off. An empty id opens a new chat.
    func show(sessionId: String) {
        navigation.section = .chats
        present()
        Task { await store.open(sessionId: sessionId) }
    }

    /// Open the window's Memory section on one of its tabs.
    func show(memory focus: WispRoute.MemoryFocus) {
        navigation.memoryTab = focus
        navigation.section = .memory
        present()
    }

    /// Open the window's Research section: saved research jobs.
    func showResearch() {
        navigation.section = .research
        present()
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
            win.contentView = NSHostingView(rootView: ChatRootView(store: store, launchers: launchers,
                                                                   navigation: navigation))
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

/// What the Wisp window's main column shows: chats, the merged Memory, or saved research.
@MainActor
final class WispWindowNavigation: ObservableObject {
    enum Section: Equatable { case chats, memory, research }
    @Published var section: Section = .chats
    @Published var memoryTab: WispRoute.MemoryFocus = .facts
    /// The Routines tab's model lives as long as the window, as it did in the
    /// old What Wisp has learned window: a source-consent write still in flight
    /// when the person switches tabs reports its outcome when they come back.
    lazy var learned = LearnedModel()
    /// Saved research for the Research section. AppDelegate supplies it, with
    /// the service client; without one (the QA harness) the section is empty.
    var researchLibrary: ResearchLibraryModel?
}
