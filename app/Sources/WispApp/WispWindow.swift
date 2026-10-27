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
    private var closeObserver: NSObjectProtocol?
    /// Between the window's willClose and its next showing.
    private var closing = false
    /// Called as the window closes (AppDelegate: Settings › Models › Setup stops
    /// being a host of the setup guide).
    var onClose: @MainActor () -> Void = {}
    let store: ChatStore
    let navigation = WispWindowNavigation()
    private let launchers: ChatLaunchers

    init(backend: ChatBackend, launchers: ChatLaunchers, debug: @escaping () -> Bool = { false }) {
        store = ChatStore(backend: backend, debug: debug)
        self.launchers = launchers
    }

    /// On screen or in the Dock, and not closing.
    var isOpen: Bool {
        guard let window, !closing else { return false }
        return window.isVisible || window.isMiniaturized
    }

    /// Open the window onto the chat list, or onto a new chat when there is none
    /// selected. A window already open comes forward as it is: the Dock or ⌘1
    /// never moves someone off Settings, Today or a chat they were in.
    func show() {
        guard !isOpen else { present(); return }
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

    /// Open the window's Today section, the full planner, on one of its tabs
    /// (0 is Needs my attention, 1 is Day plan).
    func showToday(tab: Int) {
        navigation.todayTab = tab
        navigation.section = .today
        present()
        // A planner already on screen switches tabs and re-reads the clock.
        NotificationCenter.default.post(name: TodayWindow.didShowNotification, object: nil, userInfo: ["tab": tab])
    }

    /// Open the window's Settings section, on the pane it was left on (or the one
    /// AppDelegate just wrote to SettingsView.paneKey).
    func showSettings() {
        navigation.section = .settings
        present()
    }

    /// Open the window's Research section: saved research jobs.
    func showResearch() {
        // Already on Research: a job may have finished or been saved since.
        if navigation.section == .research { navigation.researchLibrary?.refresh() }
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
            closeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: win, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.closing = true
                    self?.onClose()
                }
            }
        }
        closing = false
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

/// What the Wisp window's main column shows: chats, the full planner, the
/// merged Memory, saved research, or Settings.
@MainActor
final class WispWindowNavigation: ObservableObject {
    enum Section: Equatable { case chats, today, memory, research, settings }
    /// The planner's tab when the Today section opens.
    var todayTab = 1
    @Published var section: Section = .chats
    @Published var memoryTab: WispRoute.MemoryFocus = .facts
    /// The Routines tab's model lives as long as the window, as it did in the
    /// old What Wisp has learned window: a source-consent write still in flight
    /// when the person switches tabs reports its outcome when they come back.
    lazy var learned = LearnedModel()
    /// Saved research for the Research section. AppDelegate supplies it, with
    /// the service client; without one (the QA harness) the section is empty.
    var researchLibrary: ResearchLibraryModel?
    /// The app's one day plan, shared with the notch's Brief and Day tabs. Without one
    /// (the QA harness) the Today section is empty.
    var today: TodayModel?
    /// The app's one settings loader, shared with the notch's quick settings, and
    /// its one setup model, for Settings › Models › Setup. Without a loader the
    /// Settings section is empty; without a setup model it has no Setup subpane.
    var settings: SettingsLoader?
    var setup: SetupGuideModel?
    /// Settings › Models › Setup is standing in for the first-run window, which is
    /// hidden until it is left (AppDelegate.openSetupGuide).
    @Published var setupStandsInForWelcome = false
}
