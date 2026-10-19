import AppKit
import Combine
import SwiftUI
import Carbon.HIToolbox

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var panel: OverlayPanel?
    private var hotKey: GlobalHotKey?
    private var searchHotKey: GlobalHotKey?
    // Hosted in an OverlayPanel — the SAME notch-fused reveal panel class the
    // chat assistant uses (see createSearchPanelIfNeeded), so Smart Search
    // gets identical chrome and animation rather than a separately-styled
    // window. The two are mutually exclusive at the notch (only one physical
    // notch exists to fuse with) — see toggleSearch/expand.
    private var searchPanel: OverlayPanel?
    private lazy var searchModel = SearchModel(client: client)
    private lazy var researchModel = ResearchModel(client: client)
    // One day plan for the notch's Brief and Day tabs and the window's planner.
    private lazy var todayModel = TodayModel()
    // One settings loader for the notch's quick settings, the Settings window and
    // the Wisp window's Full access pill: one source for confirmed and pending state.
    private lazy var settingsLoader = SettingsLoader()
    private var fullAccessObserver: AnyCancellable?
    // One setup model for the notch's setup card and the setup window, so a
    // test result, a running action and readiness are the same in both.
    private lazy var setupModel = SetupGuideModel()
    private var setupCloseObserver: NSObjectProtocol?
    // The Wisp window: long conversations, saved chats, and the same approvals
    // as the notch, in a real window. It opens only when asked for; Wisp starts
    // in the notch. Its sidebar opens the other parts of the app through
    // WispRouter, like every other control (see open(_:)).
    private lazy var wispWindow: WispWindowController = {
        let window = WispWindowController(
            backend: LiveChatBackend(client: client, access: settingsLoader),
            launchers: ChatLaunchers(
                today: { WispRouter.shared.open(.today(focus: .plan)) },
                research: { WispRouter.shared.open(.research) },
                openResearchJob: { [weak self] id in self?.openSavedResearch(id) },
                newResearch: { [weak self] in self?.startResearchInNotch() },
                memory: { WispRouter.shared.open(.memory(.facts)) },
                settings: { WispRouter.shared.open(.settings) },
                searchEverything: { [weak self] in self?.toggleSearch() }),
            debug: { UserDefaults.standard.bool(forKey: "wisp.debugMode") })
        window.navigation.researchLibrary = ResearchLibraryModel(client: client)
        window.navigation.today = todayModel
        return window
    }()
    private var searchKeyMonitor: Any?
    private var presentation = OverlayTransition()
    private var searchCaptureID: UUID?
    private var searchReady = false
    private var capturedSearchPage: PageText?
    private let model = OverlayModel()
    private let client = WispClient()
    private let backend = BackendManager()
    private var settingsWindow: NSWindow?
    private var setupWindow: NSWindow?
    private var setupObserver: NSObjectProtocol?
    private static let setupGuideShownKey = "wisp.setupGuide.shown"
    private static let notchTipShownKey = "wisp.notchTip.shown"
    private var pendingCollapse: DispatchWorkItem?
    // A menu opened from the panel (the chat switcher) is open. See
    // installMenuTrackingObservers.
    private var menuTracking = false
    // An auto-collapse came due while that menu was open and was held back.
    private var collapseHeldForMenu = false
    // Whether the pointer is over the expanded panel. Replaceable so the motion
    // checks, which host no real panel, can exercise both answers.
    private var pointerInPanel: () -> Bool = { false }
    private var hoverIntent = HoverIntent()
    private var hoverDwell: DispatchWorkItem?
    private var hoverMonitors: [Any] = []
    // Whether the notch-fused bar is showing at all. The X button turns this
    // off (dismiss from the notch, free the resident model, stay in the menu bar); the
    // menu-bar icon or ⌥Space bring it back. Distinct from `model.collapsed`,
    // which only tracks bar-vs-expanded WHILE docked.
    private var notchDocked = true
    private var calendarReader: CalendarReader?
    private var remindersWriter: RemindersWriter?
    private var mailReader: MailReader?
    private var messagesReader: MessagesReader?
    private var notesReader: NotesReader?
    private var contactsReader: ContactsReader?
    private var browserHistoryReader: BrowserHistoryReader?
    private var activityReader: ActivityReader?
    private var nativeSourceReadersStarted = false
    private static let autoCollapseDelay: TimeInterval = 2

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One Wisp at a time. A second launch used to terminate the first one's
        // backend and race it for the port; now it hands over to the running app.
        let peers = NSRunningApplication
            .runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .map(\.processIdentifier)
        let selfPID = ProcessInfo.processInfo.processIdentifier
        if SingleInstance.shouldYield(bundleIdentifier: Bundle.main.bundleIdentifier,
                                      selfPID: selfPID, peers: peers) {
            NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
                .first { $0.processIdentifier != selfPID }?
                .activate(options: [])
            terminateWithoutShutdown()
            return
        }
        // Port 8000 belongs to oMLX and is deliberately NOT policed here: it used to be
        // cleared by killing whatever listened (which took out MTPLX and Homebrew oMLX
        // along with the odd stray server). oMLX's own peer attribution refuses an
        // engine it cannot verify, with a message that says so.
        //
        // Port 8765 is Wisp's backend. Whatever holds it at STARTUP is a conflict: this
        // app instance has spawned nothing yet, and Wisp keeps no record of an earlier
        // launch's backend (a file any same-user program could forge). So a holder is
        // never adopted, reclaimed or signalled, including a backend an earlier Wisp left
        // behind after a crash or upgrade; the person is told and can quit it. Judged only
        // here, after the single-instance hand-over, so a yielding second launch touches nothing.
        let folderHints = PortGuard.backendFolderHints(devRoot: backend.backendRootPath)
        // From here on, every request to the backend port is sent only after a fresh check that
        // the program there is the process this app spawned (see BackendTrust). The check is
        // not bound to the connection that carries the request (audit N7, accepted residual).
        BackendTrustConfiguration.set()
        BackendTrustProtocol.enforcedPort = BackendTrust.productionPort
        URLProtocol.registerClass(BackendTrustProtocol.self)
        NotificationCenter.default.addObserver(
            forName: .wispBackendRefused, object: nil, queue: .main) { [weak self] note in
            let message = note.userInfo?["message"] as? String ?? ""
            MainActor.assumeIsolated { self?.presentBackendRefusal(message) }
        }
        // Something the listing missed (or that arrived after it) answering on the port
        // with no live child of ours: the manager stops and reports it here, once.
        backend.onPortConflict = { [weak self] in
            DispatchQueue.global(qos: .userInitiated).async {
                var holders: [PortGuard.Listener] = []
                if case .conflict(let foreign) = PortGuard.check(port: 8765, receipt: .none) { holders = foreign }
                let message = PortGuard.conflictMessage(port: 8765, listeners: holders, folderHints: folderHints)
                DispatchQueue.main.async { self?.presentPortConflict(message) }
            }
        }
        // A10 WP3: inert unless the user enabled a browser (default off).
        backend.extraEnvironment = {
            BrowserBridgeActivation.shared.controlPathForBackend()
                .map { ["WISP_BROWSER_BRIDGE_CONTROL": $0] } ?? [:]
        }
        backend.didLaunchBackend = { BrowserBridgeActivation.shared.backendLaunched(pid: $0) }
        Task { await BrowserBridgeActivation.shared.begin() }
        // The backend starts only after the port has been judged. The judgment is retried for
        // a few seconds when a holder is there, because after a quick quit and relaunch the
        // holder is usually the previous run's backend still shutting down; a holder that
        // outlasts that window is a conflict, as before.
        Task { [weak self] in
            let verdict = await Task.detached(priority: .userInitiated) {
                BackendManager.settledStartupVerdict(check: { PortGuard.check(port: 8765, receipt: .none) })
            }.value
            guard let self else { return }
            if case .conflict(let foreign) = verdict {
                self.backend.portConflict = true
                self.presentPortConflict(PortGuard.conflictMessage(port: 8765, listeners: foreign,
                                                                   folderHints: folderHints))
                return
            }
            // `.owned` cannot occur without a receipt. On `.free`/`.unknown` a responder the
            // listing missed is still caught by BackendManager before it launches anything.
            await self.backend.startIfNeeded()
        }
        WispRouter.shared.handler = { [weak self] route in self?.open(route) }
        observeFullAccess()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = Self.menubarOrb()
            button.action = #selector(toggle)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        buildMenu()
        installEditMenu()    // enables ⌘C/⌘V/⌘X/⌘A in the text field
        // ⌥Space summons the overlay
        hotKey = GlobalHotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey)) { [weak self] in
            DispatchQueue.main.async { self?.toggle() }
        }
        // ⌘⇧F is Smart Search. Deliberately NOT ⌘F: Carbon would claim that
        // system-wide and break native find in every app on the machine,
        // including the ones (Xcode, terminals) where native find is better.
        searchHotKey = GlobalHotKey(keyCode: UInt32(kVK_ANSI_F),
                                    modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            DispatchQueue.main.async { self?.toggleSearch() }
        }
        // Notchbox-style: the panel lives at the top-centre permanently — the
        // notch (physical or virtual) on a MacBook, a floating capsule on a
        // monitor. Collapsed it's a small bar; hovering it expands. Layout
        // follows the displays live, so attaching a monitor or changing
        // resolution re-fits it instead of leaving it stranded.
        DisplayGeometry.shared.onChange = { [weak self] in self?.displaysChanged() }
        DisplayGeometry.shared.startObserving()
        installHoverMonitors()
        installMenuTrackingObservers()
        setupObserver = NotificationCenter.default.addObserver(
            forName: .wispOpenSetupGuide, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.openSetupGuide() }
        }
        offerSetupGuideOnFirstRun()
        offerKnowYouWhenReady()
        createPanelIfNeeded()
        model.collapsed = true
        panel?.present()
        // Wisp starts in the notch. The Wisp window opens only when asked for:
        // the Dock icon, the menu bar, or a hand-off from the notch.
        showNotchTipOnce()
    }

    // Clicking the Dock icon, or opening Wisp again from Finder or Spotlight while it
    // is running, opens the Wisp window. Closing the window does not quit Wisp.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        open(.chat(sessionId: nil))
        return true
    }

    /// Opening the window at launch used to be how Wisp said "I'm running". Now
    /// that it starts in the notch, open the notch once and say where Wisp lives,
    /// so neither a first launch nor an upgrade looks like Wisp failed to open.
    private func showNotchTipOnce() {
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: Self.notchTipShownKey) else { return }
        defaults.set(true, forKey: Self.notchTipShownKey)
        let duration: TimeInterval = 10
        // After the bar is on screen, so the drop-open reads as coming from the notch.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            guard let self else { return }
            self.openAssistant()
            self.model.showTransientNotice(
                "Wisp lives in the notch now. Press ⌥Space or hover here to ask. "
                    + "Click Wisp in the Dock for the full window.",
                duration: duration)
            // Tuck back in once the tip is gone, unless the pointer is on the panel
            // (scheduleCollapse also keeps an unsent draft open).
            DispatchQueue.main.asyncAfter(deadline: .now() + duration) { [weak self] in
                guard let self, !self.model.collapsed,
                      !(self.panel?.frame.contains(NSEvent.mouseLocation) ?? false) else { return }
                self.scheduleCollapse()
            }
        }
    }

    /// Full access has one source; the Wisp window's composer pill follows it
    /// wherever it changes (quick settings, Settings, or the pill itself).
    private func observeFullAccess() {
        fullAccessObserver = settingsLoader.$fullAccess.dropFirst().sink { [weak self] value in
            MainActor.assumeIsolated { self?.wispWindow.store.applyFullAccess(value) }
        }
    }

    /// Where every route lands. Memory is a section of the Wisp window. Today,
    /// Research, Settings and Setup still open their own windows; later changes
    /// move them into the notch or the Wisp window here, without touching their callers.
    private func open(_ route: WispRoute) {
        switch route {
        case .chat(let sessionId):
            if let sessionId { wispWindow.show(sessionId: sessionId) } else { wispWindow.show() }
        case .today(let focus):
            wispWindow.showToday(tab: focus.tab)
        case .research:
            wispWindow.showResearch()
        case .memory(let focus):
            wispWindow.show(memory: focus)
        case .settings:
            openSettings()
        case .setup:
            openSetupInNotch()
        case .knowYou:
            openAssistant()
            model.startKnowYou()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // MARK: - Smart Search (⌘⇧F)

    @objc private func toggleSearch() {
        if presentation.desired == .search {
            closeSearch()
            return
        }
        // Record intent before the asynchronous capture, so repeated shortcuts
        // cancel it and stale captures can never reopen a dismissed surface.
        cancelScheduledCollapse()
        let captureID = UUID()
        searchCaptureID = captureID
        searchReady = false
        capturedSearchPage = nil
        presentation.request(.search)
        Task { @MainActor [weak self] in
            let page = await PageReader.read()
            guard let self, self.searchCaptureID == captureID,
                  self.presentation.desired == .search else { return }
            self.capturedSearchPage = page
            self.searchReady = true
            self.drivePresentation()
        }
        drivePresentation()
    }

    private func createSearchPanelIfNeeded() {
        guard searchPanel == nil else { return }
        let panel = OverlayPanel { SearchView(model: self.searchModel) }
        // Search has no separate "collapsed bar" content of its own — it's
        // either closed or open at full size — so it's never compact; the
        // notch-stretch open/close animation still applies (dropOpen/rollUp
        // animate purely on window geometry, independent of this flag), this
        // only governs resizeToFit's guard against resizing while compact.
        panel.isCompact = { false }
        searchModel.onResize = { [weak panel] in
            DispatchQueue.main.async { panel?.resizeToFit() }
        }
        searchPanel = panel
    }

    private func closeSearch() {
        guard presentation.desired == .search || presentation.settled == .search else { return }
        requestPresentation(notchDocked ? .bar : .hidden)
    }

    /// Arrow/escape handling. A local monitor rather than SwiftUI `.onKeyPress`
    /// so ↑/↓ still navigate results while the text field holds focus — the
    /// field would otherwise swallow them as cursor movement.
    private func installSearchKeyMonitor() {
        guard searchKeyMonitor == nil else { return }
        searchKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] ev in
            guard let self, let panel = self.searchPanel, panel.isKeyWindow else { return ev }
            switch Int(ev.keyCode) {
            case kVK_Escape:
                self.closeSearch(); return nil
            case kVK_DownArrow:
                self.searchModel.move(1); return nil
            case kVK_UpArrow:
                self.searchModel.move(-1); return nil
            case kVK_Return where ev.modifierFlags.contains(.command):
                self.searchModel.askAnyway(); return nil
            case kVK_Return:
                // ↵ jumps to the current hit and leaves the panel open, so a
                // second ↵ walks to the next one — same rhythm as ⌘G.
                self.searchModel.move(ev.modifierFlags.contains(.shift) ? -1 : 1)
                return nil
            default:
                return ev
            }
        }
    }

    // The app's main menu: Wisp (Settings, Hide, Quit), Edit (so the standard edit
    // shortcuts route to the focused field), and Window.
    private func installEditMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let app = NSMenu(title: "Wisp")
        appItem.submenu = app
        let settingsItem = app.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        settingsItem.target = self
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Wisp", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(.separator())
        app.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        let quitItem = app.addItem(withTitle: "Quit Wisp", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        editItem.submenu = edit
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let windowItem = NSMenuItem()
        main.addItem(windowItem)
        let window = NSMenu(title: "Window")
        windowItem.submenu = window
        window.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        window.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        window.addItem(.separator())
        let chatItem = window.addItem(withTitle: "Wisp Window", action: #selector(openChat), keyEquivalent: "1")
        chatItem.target = self
        NSApp.windowsMenu = window
        NSApp.mainMenu = main
    }

    private func buildMenu() {
        let menu = NSMenu()
        // Keep the menu-bar menu focused on app configuration and diagnostics.
        // Primary workflows live in Wisp's panel, where they have context and
        // progress UI instead of duplicating five shortcuts here.
        menu.addItem(withTitle: "Open Wisp Window", action: #selector(openChat), keyEquivalent: "")
        menu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "Set Up Inference…", action: #selector(openSetupInNotch), keyEquivalent: "")
        menu.addItem(withTitle: "Tell Wisp About You…", action: #selector(tellWispAboutYou), keyEquivalent: "")
        menu.addItem(.separator())
        // Debug Mode is in the notch's quick settings ("Reply details").
        // Export works any time there's a conversation, whether or not
        // it is on: debug metadata is tracked for every turn unconditionally (see
        // OverlayModel.applyDebugFields).
        menu.addItem(withTitle: "Report a Problem…", action: #selector(reportProblem), keyEquivalent: "")
        menu.addItem(withTitle: "Export Chat Debug Log…", action: #selector(exportDebugLog), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Wisp", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        // Attach the menu only on right-click; left-click toggles the panel.
        statusItem.menu = nil
        rightClickMenu = menu
    }

    private var rightClickMenu: NSMenu?

    // The panel is always on screen; the hotkey and menu-bar click just flip
    // between the notch bar and the expanded assistant. No reset — the visible
    // transcript (and server-side session) survive collapse/expand.
    @objc private func toggle() {
        if let event = NSApp.currentEvent, event.type == .rightMouseUp, let menu = rightClickMenu {
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        // Re-dock if the X button previously dismissed the notch bar — the
        // menu-bar icon (or ⌥Space) is the way back in.
        notchDocked = true
        if presentation.desired == .chat {
            collapse()
        } else {
            // ⌥Space and the menu-bar icon always open Ask.
            model.prepareTab(byHover: false)
            expand()
        }
    }

    /// Track menus so an open one holds auto-collapse. AppKit posts these for every
    /// menu in the app, in begin/end pairs. In case a pair is ever unbalanced, any
    /// presentation change or the pointer coming back into the panel also clears
    /// the flag, but only when no menu is actually on screen, so it can never
    /// disable auto-collapse for good and never lets the panel collapse under an
    /// open menu. When the menu closes, a collapse it held back is re-armed if the
    /// pointer is outside the panel.
    private func installMenuTrackingObservers() {
        pointerInPanel = { [weak self] in
            self?.panel?.frame.contains(NSEvent.mouseLocation) ?? false
        }
        NotificationCenter.default.addObserver(forName: NSMenu.didBeginTrackingNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.menuTracking = true }
        }
        NotificationCenter.default.addObserver(forName: NSMenu.didEndTrackingNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.menuDidEndTracking() }
        }
    }

    private func menuDidEndTracking() {
        menuTracking = false
        guard collapseHeldForMenu else { return }
        collapseHeldForMenu = false
        if !model.collapsed, !pointerInPanel() { scheduleCollapse() }
    }

    /// Drop a menu flag left set by an unbalanced begin, unless a menu really is open.
    private func clearStaleMenuTracking(menuOnScreen: Bool? = nil) {
        let menuOnScreen = menuOnScreen
            ?? (NSApp?.windows.contains { $0.isVisible && $0.level == .popUpMenu } ?? false)
        guard !menuOnScreen else { return }
        menuTracking = false
        collapseHeldForMenu = false
    }

    private func requestPresentation(_ surface: OverlayTransition.Surface) {
        clearStaleMenuTracking()
        // Pinning only holds off automatic collapse. Once the panel goes back to
        // the bar or away by any route (collapse, Search closing, dismiss), it is unpinned.
        if surface == .bar || surface == .hidden { model.pinned = false }
        cancelScheduledCollapse()
        cancelHoverDwell()
        if surface != .search {
            searchCaptureID = nil
            searchReady = false
            capturedSearchPage = nil
        }
        presentation.request(surface)
        drivePresentation()
    }

    private func expand() {
        notchDocked = true
        requestPresentation(.chat)
    }

    private func collapse() {
        // A delayed focus/hover callback from chat must not cancel Search.
        guard presentation.desired == .chat else { return }
        requestPresentation(.bar)
    }

    private func finishPresentation(_ step: OverlayTransition.Step) {
        guard presentation.finish(step) else { return }
        // A pointer left resting on the bar after a close must leave the zone
        // once before hovering can open the panel again.
        if step.to == .bar || step.to == .hidden {
            hoverIntent.didCollapse(pointerInZone: pointerInHoverZone())
        }
        drivePresentation()
    }

    /// Serialize native and SwiftUI changes through one owner. An input during
    /// motion updates `desired`; the short current step finishes before the
    /// next starts, keeping content mounted and preventing competing masks.
    private func drivePresentation() {
        guard let step = presentation.next(searchReady: searchReady) else { return }
        if step.from == .chat {
            guard let panel else { finishPresentation(step); return }
            panel.makeFirstResponder(nil)
            panel.rollUp { [weak self] in
                guard let self else { return }
                self.model.collapsed = true
                DispatchQueue.main.async {
                    panel.settleToBar()
                    self.finishPresentation(step)
                }
            }
        } else if step.from == .search {
            if let monitor = searchKeyMonitor { NSEvent.removeMonitor(monitor); searchKeyMonitor = nil }
            guard let searchPanel else { finishPresentation(step); return }
            searchPanel.makeFirstResponder(nil)
            searchPanel.rollUp { [weak self] in
                guard let self else { return }
                searchPanel.orderOut(nil)
                self.searchModel.reset()
                if self.notchDocked { self.panel?.present() }
                self.finishPresentation(step)
            }
        } else {
            switch step.to {
            case .chat:
                createPanelIfNeeded()
                guard let panel else { finishPresentation(step); return }
                if !panel.isVisible { panel.present() }
                model.collapsed = false
                DispatchQueue.main.async { [weak self] in
                    panel.dropOpen { self?.finishPresentation(step) }
                }
            case .search:
                createSearchPanelIfNeeded()
                guard let searchPanel else { finishPresentation(step); return }
                panel?.orderOut(nil)
                searchModel.reset()
                if let page = capturedSearchPage { searchModel.capture(page) }
                capturedSearchPage = nil
                searchPanel.present()
                installSearchKeyMonitor()
                DispatchQueue.main.async { [weak self] in
                    searchPanel.dropOpen { self?.finishPresentation(step) }
                }
            case .bar:
                model.collapsed = true
                panel?.present()
                finishPresentation(step)
            case .hidden:
                model.collapsed = true
                panel?.orderOut(nil)
                finishPresentation(step)
                Task { await client.unloadAgent() }
            }
        }
    }

    // Reset at the explicit dismiss action, before any subsequent input or
    // summary request can be created. Presentation callbacks only move views.
    private func dismissToMenuBar() {
        notchDocked = false
        let wasCollapsed = model.collapsed
        model.newChat()
        model.collapsed = wasCollapsed
        requestPresentation(.hidden)
    }

    // Auto-collapse triggers (mouse leaves the panel, or focus moves to another
    // app) don't collapse immediately — they give a 3-second grace period so a
    // quick glance away or an accidental drift-off doesn't tuck it away. Coming
    // back (re-hover or re-focus) cancels it.
    private func scheduleCollapse() {
        guard !model.collapsed else { return }
        pendingCollapse?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.model.collapsed else { return }
            // Don't hide a pinned panel or an unfinished edit (typed input, a
            // message draft being edited) out from under the user; a mid-task
            // generation is fine (the notch bar's dot shows it's still working).
            if self.model.holdsOpen { return }
            // A menu opened from the panel (the chat switcher) runs outside it,
            // so the pointer leaving the panel is not the person leaving. Hold
            // the collapse until the menu closes (see installMenuTrackingObservers).
            if self.menuTracking { self.collapseHeldForMenu = true; return }
            self.collapse()
        }
        pendingCollapse = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.autoCollapseDelay, execute: work)
    }

    private func pointerEnteredPanel() {
        // Reentry cancels auto-collapse even while opening. Only the hover
        // expansion itself must wait for a settled, docked bar.
        cancelScheduledCollapse()
        clearStaleMenuTracking()
        guard notchDocked, presentation.active == nil,
              presentation.desired != .search else { return }
        // Entering the bar window only re-evaluates intent; the same dwell and
        // arming rules as the global pointer monitor decide whether to open.
        if model.collapsed { pointerMoved() }
    }

    // MARK: - Hover intent

    private var canHoverOpen: Bool {
        model.collapsed && notchDocked && presentation.active == nil
            && presentation.desired == .bar
    }

    private func pointerInHoverZone() -> Bool {
        DisplayGeometry.shared.layout.hoverZone.contains(NSEvent.mouseLocation)
    }

    /// Hover is detected from the pointer's screen position, not only from the
    /// bar window's tracking area: the bar is a few points tall, the window
    /// changes size and order as it opens and closes, and a window that appears
    /// under a resting pointer never receives an enter event. A global monitor
    /// sees every move regardless of window state (and needs no permission).
    private func installHoverMonitors() {
        guard hoverMonitors.isEmpty else { return }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved], handler: { [weak self] _ in
            MainActor.assumeIsolated { self?.pointerMoved() }
        }) { hoverMonitors.append(global) }
        if let local = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved], handler: { [weak self] event in
            self?.pointerMoved()
            return event
        }) { hoverMonitors.append(local) }
    }

    private func pointerMoved() {
        let action = hoverIntent.pointerMoved(inZone: pointerInHoverZone(),
                                              buttonsDown: NSEvent.pressedMouseButtons != 0,
                                              collapsed: canHoverOpen)
        switch action {
        case .beginDwell: beginHoverDwell()
        case .cancelDwell: cancelHoverDwell()
        case .none: break
        }
    }

    private func beginHoverDwell() {
        guard hoverDwell == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.hoverDwell = nil
            if self.hoverIntent.shouldOpen(inZone: self.pointerInHoverZone(),
                                           buttonsDown: NSEvent.pressedMouseButtons != 0,
                                           collapsed: self.canHoverOpen) {
                self.model.prepareTab(byHover: true)
                self.expand()
            }
        }
        hoverDwell = work
        DispatchQueue.main.asyncAfter(deadline: .now() + HoverIntent.dwell, execute: work)
    }

    private func cancelHoverDwell() {
        hoverDwell?.cancel()
        hoverDwell = nil
    }

    /// Displays were attached, removed, rearranged, or rescaled.
    private func displaysChanged() {
        cancelHoverDwell()
        panel?.applyDisplayChange()
        searchPanel?.applyDisplayChange()
    }

    private func cancelScheduledCollapse() {
        pendingCollapse?.cancel()
        pendingCollapse = nil
    }

    private func createPanelIfNeeded() {
        guard panel == nil else { return }
        model.todayReport = todayModel.report
        panel = OverlayPanel {
            OverlayView(model: self.model, researchModel: self.researchModel, todayModel: self.todayModel,
                        settings: self.settingsLoader, setup: self.setupModel,
                        onClose: { [weak self] in self?.collapse() },
                        onDismiss: { [weak self] in self?.dismissToMenuBar() })
        }
        panel?.isCompact = { [weak self] in self?.model.collapsed ?? false }
        model.onResize = { [weak self] in
            DispatchQueue.main.async { self?.panel?.resizeToFit() }
        }
        model.requestExpand = { [weak self] in self?.expand() }
        model.requestCollapse = { [weak self] in self?.collapse() }

        // Assistant layer: request the privacy/security permissions Wisp uses
        // (once; re-runnable from the menu) and start the commitment clock +
        // reminder stream (the notch "Up Next" countdown).
        Permissions.bootstrap()
        model.onCreateCalendarEvent = { [weak self] title, ts, dur, loc in
            self?.calendarReader?.createEvent(title: title, startTs: ts,
                                              durationMin: dur, location: loc)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onDeleteCalendarEvent = { [weak self] identifier, occurrenceTs in
            self?.calendarReader?.deleteEvent(identifier: identifier, occurrenceTs: occurrenceTs)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onMoveCalendarEvent = { [weak self] identifier, title, expected, start, duration, invitees in
            self?.calendarReader?.moveEvent(identifier: identifier, expectedTitle: title,
                                            expectedStart: expected, newStart: start, durationMin: duration,
                                            expectedInvitees: invitees)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onVerifiedReminderAction = { [weak self] kind, actionID, sourceID,
            expectedTitle, expectedDueTs, title, dueTs, commitmentKind in
            self?.remindersWriter?.performVerified(kind: kind, actionID: actionID,
                sourceID: sourceID, expectedTitle: expectedTitle,
                expectedDueTs: expectedDueTs, title: title, dueTs: dueTs,
                commitmentKind: commitmentKind)
                ?? ["ok": false, "status": "failed", "error": "Reminders handler unavailable"]
        }
        model.onReconcileReminderAction = { [weak self] kind, actionID, sourceID,
            title, dueTs, commitmentKind in
            guard let self else {
                return ["ok": false, "status": "unknown", "error": "Reminders handler unavailable"]
            }
            guard let writer = self.remindersWriter else {
                return ["ok": false, "status": "unknown", "error": "Reminders handler unavailable"]
            }
            return await writer.reconcileVerified(kind: kind, actionID: actionID,
                sourceID: sourceID, title: title, dueTs: dueTs,
                commitmentKind: commitmentKind)
        }
        model.onOpenChat = { [weak self] sessionId in
            self?.open(.chat(sessionId: sessionId))
        }
        model.onStartResearch = { [weak self] prompt in
            self?.openResearch(prompt: prompt)
        }
        // Backend requests an on-demand Mail sync (cold-cache email query);
        // refresh both header and raw caches so summarize_emails/view_emails
        // can be served without waiting on the 5-min timer.
        model.onSyncEmails = { [weak self] in
            guard let self else { return }
            self.mailReader?.sync()
            self.mailReader?.syncRaw()
        }
        model.onSyncAssistantSources = { [weak self] sources in
            guard let self else { return }
            if sources.contains("calendar") { self.calendarReader?.sync() }
            if sources.contains("reminders") { self.remindersWriter?.sync() }
            if sources.contains("email") { self.mailReader?.sync() }
            if sources.contains("messages") { self.messagesReader?.sync() }
            if sources.contains("notes") { self.notesReader?.sync() }
            if sources.contains("browser_history") { self.browserHistoryReader?.sync() }
        }
        model.startAssistant()
        Notifications.onOpenToday = { [weak self] in self?.openTodayInNotch() }
        startNativeSourceReaders()

        // Pointer enter/exit come from a real AppKit tracking area on the
        // panel (HoverView), not SwiftUI's flaky .onHover. Entering the bar
        // expands; entering the open panel cancels a pending collapse; leaving
        // the open panel arms the 2s grace timer so it retreats into the notch.
        panel?.onMouseEnter = { [weak self] in self?.pointerEnteredPanel() }
        panel?.onMouseExit = { [weak self] in
            guard let self, !self.model.collapsed else { return }
            self.scheduleCollapse()
        }

        // "Don't sit on top of everything at all times": the panel keeps its
        // elevated level (required to fuse with the notch / be visible from an
        // accessory app), but when it stops being the key window — the user
        // clicked into another app — it tucks back into the notch after the
        // grace delay instead of hovering over their work.
        NotificationCenter.default.addObserver(
            forName: NSWindow.didResignKeyNotification, object: panel, queue: .main
        ) { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.model.collapsed else { return }
                // Not when focus moved to one of OUR windows (the attach-file
                // dialog, Settings) — only when it left the app for real.
                if self.model.pickingFile || NSApp.keyWindow != nil { return }
                self.scheduleCollapse()
            }
        }
        // Coming back to the panel cancels a pending auto-collapse.
        NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification, object: panel, queue: .main
        ) { [weak self] _ in
            DispatchQueue.main.async { self?.cancelScheduledCollapse() }
        }
    }

    // Monochrome orb glyph for the menu bar (template → adapts to light/dark).
    private static func menubarOrb() -> NSImage {
        let d: CGFloat = 18
        let img = NSImage(size: NSSize(width: d, height: d))
        img.lockFocus()
        if let ctx = NSGraphicsContext.current?.cgContext {
            ctx.setLineWidth(1.5)
            ctx.setStrokeColor(NSColor.black.cgColor)
            let inset: CGFloat = 2.5
            ctx.strokeEllipse(in: CGRect(x: inset, y: inset, width: d - 2 * inset, height: d - 2 * inset))
            ctx.setFillColor(NSColor.black.cgColor)
            ctx.fillEllipse(in: CGRect(x: d * 0.6, y: d * 0.6, width: 3, height: 3))
        }
        img.unlockFocus()
        img.isTemplate = true
        return img
    }

    /// Reveal the normal prompt surface from the right-click menu. This uses
    /// the same transition as the menu-bar icon, so a previously dismissed
    /// notch panel is restored before the user starts typing.
    @objc private func openAssistant() {
        notchDocked = true
        createPanelIfNeeded()
        if model.collapsed || panel?.isVisible != true { expand() }
    }

    /// The menu-bar counterpart to the in-panel Daily Summary control. Open
    /// the panel first so the streaming brief has somewhere visible to land.
    @objc private func runDailySummary() {
        openAssistant()
        model.runDailySummary()
    }

    /// New research starts in the notch's Research tab, where the prompt drafts
    /// an editable plan before Wisp searches the web.
    private func startResearchInNotch() {
        prepareNewResearch()
        openAssistant()
    }

    private func prepareNewResearch() {
        model.showingQuickSettings = false
        // Leaves a plan on screen the way Back does, releasing the prompt queue it held.
        model.returnToChat()
        model.tab = .research
    }

    @objc private func openChat() { open(.chat(sessionId: nil)) }

    private func openSavedResearch(_ id: String) {
        openAssistant()
        model.showingResearch = true
        model.tab = .research
        researchModel.openJob(id)
    }

    private func startNativeSourceReaders() {
        guard !nativeSourceReadersStarted else { return }
        nativeSourceReadersStarted = true
        calendarReader = CalendarReader()
        mailReader = MailReader()
        messagesReader = MessagesReader()
        browserHistoryReader = BrowserHistoryReader()
        activityReader = ActivityReader()
        calendarReader?.start()
        mailReader?.start()
        messagesReader?.start()
        browserHistoryReader?.start()
        activityReader?.start()
        remindersWriter = RemindersWriter()
        notesReader = NotesReader()
        contactsReader = ContactsReader()
        remindersWriter?.start()
        notesReader?.start()
        contactsReader?.start()
    }

    // Writes the full conversation (every turn's model, route decision,
    // timing, tok/s, tool calls, and any errors) to ~/Downloads as JSON + a
    // readable .txt companion, then reveals them in Finder. No-ops with a
    // brief alert if there's no conversation yet.
    /// Opens the report inline in the notch, under the last reply.
    @objc private func reportProblem() {
        model.reportCurrentProblem()
        openAssistant()
    }

    @objc private func exportDebugLog() {
        guard let url = model.exportDebugLog() else {
            let alert = NSAlert()
            alert.messageText = "Nothing to export yet"
            alert.informativeText = "Start a conversation in Wisp first, then export its debug log."
            alert.alertStyle = .informational
            alert.runModal()
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    /// Quit Wisp after the engine and its helpers have stopped. Every quit request
    /// goes through here (see applicationShouldTerminate), so the Dock, the
    /// application menu and the menu bar all shut down the same way.
    @objc private func quit() {
        // A second Quit while the first is still stopping the engine means "now".
        if shutdownStarted { finishShutdown(); return }
        beginShutdown()
    }

    private var shutdownStarted = false
    private var shutdownFinished = false
    /// True when the system (logout, restart) asked to quit and is waiting for a reply.
    private var systemAwaitingReply = false

    private func beginShutdown() {
        shutdownStarted = true
        // Quitting must never hang: exit after a bound even if a stop step stalls.
        // The bound is longer than the stop steps' own limits added together.
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            MainActor.assumeIsolated { self.finishShutdown() }
        }
        // Stopping the engine unloads any models and frees its ~2GB baseline too.
        Task {
            // Let any just-fired Settings change (role/model) finish
            // persisting to ~/.moe/config.yaml before the backend that writes it
            // gets killed — see PendingConfigWrites' docstring for the race this
            // closes. Capped so a genuinely stuck request can't hang quitting.
            await PendingConfigWrites.shared.waitUntilIdle()
            await client.shutdownOMLX()
            await BrowserBridgeActivation.shared.shutdown()
            await backend.stopAndWait()
            await MainActor.run { self.finishShutdown() }
        }
    }

    private func finishShutdown() {
        guard !shutdownFinished else { return }
        shutdownFinished = true
        // Signals the backend if the stop steps did not get to it. After a clean
        // stop the backend is already detached, so this does nothing.
        backend.stop()
        exitNow()
    }

    /// Exit without stopping the engine. Used when this launch only hands over to a
    /// copy that is already running, and when an alert quits Wisp without a service.
    private func terminateWithoutShutdown() {
        shutdownFinished = true
        exitNow()
    }

    /// Ends the app, answering a system logout or restart that is waiting for it.
    private func exitNow() {
        if systemAwaitingReply {
            NSApp.reply(toApplicationShouldTerminate: true)
        } else {
            NSApp.terminate(nil)
        }
    }

    // A quit from the Dock, the application menu or the system does not call quit().
    // Run the same shutdown and reply once it finishes, so a logout is not cancelled.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if shutdownFinished { return .terminateNow }
        systemAwaitingReply = true
        if !shutdownStarted { beginShutdown() }
        return .terminateLater
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            // SettingsView has a fixed 760×620 layout: the sidebar, form labels,
            // and 300-point fields all fit without clipping at this content size.
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 620),
                               styleMask: [.titled, .closable], backing: .buffered, defer: false)
            win.title = "Wisp Settings"
            win.contentView = NSHostingView(rootView: SettingsView(loader: settingsLoader))
            win.center()
            win.isReleasedWhenClosed = false
            settingsWindow = win
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    private var lastRefusalShown = ""

    /// The app has just refused to talk to the program on the backend port. Shown once
    /// per distinct reason, never repeatedly while it stays refused.
    private func presentBackendRefusal(_ message: String) {
        guard !message.isEmpty, message != lastRefusalShown else { return }
        lastRefusalShown = message
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Wisp isn't using its service"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Quit Wisp")
        if alert.runModal() == .alertSecondButtonReturn { terminateWithoutShutdown() }
    }

    /// A program that is not Wisp's own holds the backend port. Wisp does not stop
    /// it: the person decides.
    private func presentPortConflict(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Wisp can't start its service"
        alert.informativeText = message
        alert.addButton(withTitle: "Quit Wisp")
        alert.addButton(withTitle: "Keep Wisp Open")
        if alert.runModal() == .alertFirstButtonReturn { terminateWithoutShutdown() }
    }

    /// The full setup guide in its own window: every step, including Smart
    /// Search, other local apps and model downloads. Settings opens it, and so
    /// does the notch card's ⤢.
    @objc private func openSetupGuide() {
        // Already open: just bring it forward, so an in-flight action isn't discarded.
        if let open = setupWindow, open.isVisible {
            NSApp.activate(ignoringOtherApps: true)
            open.makeKeyAndOrderFront(nil)
            return
        }
        let win = setupWindow ?? {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 700),
                               styleMask: [.titled, .closable], backing: .buffered, defer: false)
            win.title = "Wisp Setup"
            win.isReleasedWhenClosed = false
            win.center()
            setupWindow = win
            // This window object is the host, not its title: closing it is the
            // window's last word on setup, however SwiftUI keeps its view.
            setupCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: win, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.setupModel.hide(from: "window") }
            }
            return win
        }()
        win.contentView = NSHostingView(rootView: SetupGuideView(
            model: setupModel,
            onDone: { [weak win] in win?.close() },
            openSettings: { [weak self, weak win] in win?.close(); self?.openSettings() }))
        // Showing it starts the poll (the first host), which re-reads the machine.
        setupModel.show(in: "window")
        UserDefaults.standard.set(true, forKey: Self.setupGuideShownKey)
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// Setup in the notch: the panel opens on Ask with the setup card, which
    /// keeps it open until the person finishes or chooses Later.
    @objc private func openSetupInNotch() {
        showSetupCard()
        openAssistant()
    }

    /// Day in the notch: where a clicked notification lands.
    private func openTodayInNotch() {
        prepareTodayInNotch()
        openAssistant()
    }

    private func prepareTodayInNotch() {
        model.showingQuickSettings = false
        model.tab = .day
    }

    private func showSetupCard() {
        model.showingQuickSettings = false
        model.setupCard = .open
        model.tab = .ask
    }

    /// On a Mac where Wisp can't answer yet (no engine, or no model), open the
    /// notch on the setup card once. It never reopens by itself afterwards; Later
    /// leaves a line on Ask until Wisp is ready, and the menu-bar item and
    /// Settings remain the way back in.
    private func offerSetupGuideOnFirstRun() {
        guard !UserDefaults.standard.bool(forKey: Self.setupGuideShownKey) else { return }
        Task { @MainActor [weak self] in
            var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("setup/status"))
            request.timeoutInterval = 10
            // The backend starts alongside the app; wait for it, but only so long.
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard let self else { return }
                guard let (data, _) = try? await URLSession.shared.data(for: request),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ready = object["ready"] as? Bool else { continue }
                if !ready { self.openSetupInNotch() }
                UserDefaults.standard.set(true, forKey: Self.setupGuideShownKey)
                return
            }
        }
    }

    /// Getting to know you, from the menu bar.
    @objc private func tellWispAboutYou() {
        openAssistant()
        model.startKnowYou()
    }

    /// Once inference is set up, open Getting to know you in the notch if the
    /// service says it is due: never finished, and no Later in the last few
    /// days (it offers at most twice on its own; the menu bar, /about and
    /// Memory → About you remain the way in). Waits for the service and for a
    /// quiet panel, so the first launch is not two things at once.
    private func offerKnowYouWhenReady() {
        Task { @MainActor [weak self] in
            var setupReady = false
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard let self else { return }
                if !setupReady {
                    var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("setup/status"))
                    request.timeoutInterval = 10
                    guard let (data, _) = try? await URLSession.shared.data(for: request),
                          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let ready = object["ready"] as? Bool else { continue }
                    // The setup guide owns a first launch until inference works.
                    guard ready else { return }
                    setupReady = true
                }
                guard let envelope = await WispClient().profile() else { continue }
                guard envelope.status.offer else { return }
                // Not over the setup card, a reply in progress, or a flow already up.
                if self.setupWindow?.isVisible == true || self.model.setupCard == .open
                    || self.model.isProcessing || self.model.knowYou != nil {
                    continue
                }
                self.openAssistant()
                self.model.startKnowYou()
                return
            }
        }
    }

    private func openResearch(prompt: String) {
        openAssistant()
        model.showingResearch = true
        model.tab = .research
        researchModel.createPlan(prompt: prompt)
    }
}

#if WISP_MOTION_APP_DELEGATE_CHECKS
extension AppDelegate {
    /// Exercise actual delegate/model side effects without launching readers,
    /// the backend, a window, or any native permission flow.
    static func checkPointerReentryDuringOpening() {
        let delegate = AppDelegate()
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        delegate.model.collapsed = false
        delegate.scheduleCollapse()
        let scheduled = delegate.pendingCollapse!
        delegate.pointerEnteredPanel()
        precondition(scheduled.isCancelled && delegate.pendingCollapse == nil,
                     "Pointer reentry during opening must cancel auto-collapse")
        precondition(delegate.presentation.active == opening && delegate.presentation.desired == .chat,
                     "Reentry must not introduce a competing transition")
        print("PASS: AppDelegate pointer reentry cancels collapse during opening")
    }

    static func checkSummaryDuringDismissal() async {
        let delegate = AppDelegate()
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        delegate.model.collapsed = false
        delegate.model.requestExpand = { [weak delegate] in delegate?.expand() }
        delegate.dismissToMenuBar()
        delegate.model.runDailySummary()
        delegate.finishPresentation(opening)
        for _ in 0..<100 {
            if delegate.model.brief != nil { break }
            try? await Task.sleep(for: .milliseconds(20))
        }
        precondition(delegate.model.brief?.text == "Synthetic daily summary",
                     "Summary response was discarded after dismissal/reopen")
        precondition(delegate.model.tab == .brief && !delegate.model.briefComposing,
                     "The brief lands on the Brief tab")
        precondition(delegate.model.turns.isEmpty, "The brief no longer goes into the chat transcript")
        precondition(delegate.model.conversationSettled && !delegate.model.willQueuePrompt,
                     "A composed brief frees the prompt queue")
        delegate.model.newChat()
        precondition(delegate.model.brief != nil, "New chat keeps the brief")
        delegate.model.reset()
        print("PASS: AppDelegate Daily Summary during dismissal completes")
    }

    static func checkPinnedPanelHoldsOpen() async {
        func opened() -> AppDelegate {
            let delegate = AppDelegate()
            delegate.presentation.request(.chat)
            let opening = delegate.presentation.next()!
            delegate.model.collapsed = false
            delegate.finishPresentation(opening)
            return delegate
        }
        let idle = opened()
        let pinned = opened()
        pinned.model.pinned = true
        let typing = opened()
        typing.model.input = "half a thought"
        let editing = opened()
        editing.model.messageDraft = OverlayModel.MessageDraft(to: "fixture", text: "Hi", isEditing: true)
        let addingTask = opened()
        addingTask.model.tab = .day
        addingTask.model.todayTaskTitle = "Lab report draft"
        for delegate in [idle, pinned, typing, editing, addingTask] { delegate.scheduleCollapse() }
        try? await Task.sleep(for: .seconds(autoCollapseDelay + 0.5))
        precondition(idle.presentation.desired == .bar, "Control: an idle panel must auto-collapse")
        for (delegate, what) in [(pinned, "a pin"), (typing, "typed input"), (editing, "a draft being edited"),
                                 (addingTask, "a Day task being typed")] {
            precondition(delegate.presentation.desired == .chat, "\(what) must hold the panel open")
        }
        pinned.collapse()
        precondition(!pinned.model.pinned, "An explicit collapse must unpin")
        precondition(pinned.presentation.desired == .bar, "An explicit collapse still collapses a pinned panel")
        let dismissed = opened()
        dismissed.model.pinned = true
        dismissed.dismissToMenuBar()
        precondition(!dismissed.model.pinned, "Dismissing must unpin")
        let history = [OverlayModel.Turn(role: "user", text: "a"), OverlayModel.Turn(role: "assistant", text: "b"),
                       OverlayModel.Turn(role: "user", text: "c")]
        let local = [OverlayModel.Turn(role: "user", text: "c"), OverlayModel.Turn(role: "assistant", text: "d")]
        precondition(OverlayModel.joining(history: history, before: local).map(\.text) == ["a", "b", "c", "d"],
                     "A turn saved before its chat's history loaded appears once")
        precondition(OverlayModel.joining(history: Array(history.prefix(2)), before: local).map(\.text) == ["a", "b", "c", "d"],
                     "History goes before turns asked while it loaded")
        // The tab survives collapse; ⌥Space reopens on Ask, a prompt hover on the last tab.
        precondition(addingTask.model.tab == .day, "The tab survives collapse")
        addingTask.model.collapsed = true
        addingTask.model.prepareTab(byHover: true)
        precondition(addingTask.model.tab == .day, "A hover soon after closing reopens the last tab")
        addingTask.model.prepareTab(byHover: true, at: Date().addingTimeInterval(NotchTabMemory.hoverWindow + 60))
        precondition(addingTask.model.tab == .ask, "A hover long after closing opens Ask")
        addingTask.model.tab = .research
        addingTask.model.prepareTab(byHover: false)
        precondition(addingTask.model.tab == .ask, "⌥Space opens Ask")
        print("PASS: AppDelegate collapse holds (pin, typing, draft, Day task), unpins on collapse and dismiss; tabs reopen as designed")
    }

    static func checkResearchReleasesQueueOffTab() async {
        let delegate = AppDelegate()
        let model = delegate.model
        var started: [String] = []
        model.onStartResearch = { started.append($0) }
        model.tab = .research
        model.input = "battery chemistry"
        model.submit()
        precondition(started == ["battery chemistry"] && model.showingResearch, "A research prompt starts a plan")
        precondition(model.willQueuePrompt, "An on-screen research plan holds the prompt queue")
        model.tab = .ask
        precondition(!model.willQueuePrompt, "Leaving the Research tab frees the queue for Ask")
        precondition(model.showingResearch, "The research plan stays on its tab")
        model.runDailySummary()
        precondition(model.briefComposing, "A brief can be composed while research sits on its tab")
        model.reset()
        // New research from the window, while a plan is on screen in the notch.
        let window = AppDelegate()
        window.model.onStartResearch = { _ in }
        window.model.tab = .research
        window.model.input = "solid-state batteries"
        window.model.submit()
        precondition(window.model.willQueuePrompt, "A plan on screen holds the queue")
        window.prepareNewResearch()
        precondition(window.model.tab == .research && !window.model.showingResearch && !window.model.willQueuePrompt,
                     "New research opens the Research prompt and frees the queue the old plan held")
        print("PASS: AppDelegate research releases the prompt queue when its tab is left or New research opens")
    }

    static func checkQuickSettingsSurviveCollapse() async {
        let delegate = AppDelegate()
        let model = delegate.model
        let loader = delegate.settingsLoader
        delegate.observeFullAccess()
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        model.collapsed = false
        delegate.finishPresentation(opening)
        model.run(.settings)
        precondition(model.showingQuickSettings, "/settings opens quick settings")
        await loader.refreshFullAccess()
        precondition(loader.fullAccess == nil, "An unreadable Full access flag is unconfirmed, never off")
        precondition(loader.fullAccessExplanation.contains("hasn't been confirmed"), "Unconfirmed Full access says so")
        loader.setFullAccess(true)
        precondition(loader.fullAccessPending && loader.fullAccess == nil, "A change waits for the service before it shows")
        model.collapsed = true
        precondition(!model.showingQuickSettings, "Collapsing closes quick settings")
        model.collapsed = false
        for _ in 0..<100 where loader.fullAccessPending { try? await Task.sleep(for: .milliseconds(20)) }
        precondition(!loader.fullAccessPending && loader.fullAccess == true,
                     "A change started before the notch collapsed is confirmed after it reopens")
        let store = delegate.wispWindow.store
        precondition(store.fullAccessKnown && store.fullAccess, "The Wisp window's pill follows the one Full access source")
        model.showingQuickSettings = true
        model.tab = .brief
        precondition(!model.showingQuickSettings, "Switching tabs closes quick settings")
        print("PASS: AppDelegate quick settings share one loader; a Full access change survives collapse; unconfirmed is never off")
    }

    static func checkSetupCardHoldsAsk() async {
        let delegate = AppDelegate()
        let model = delegate.model
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        model.collapsed = false
        delegate.finishPresentation(opening)
        model.tab = .day
        model.showingQuickSettings = true
        delegate.showSetupCard()
        precondition(model.setupCard == .open && model.tab == .ask && !model.showingQuickSettings,
                     "Setup opens the panel on Ask with the full card")
        precondition(model.holdsOpen, "An open setup card holds the panel open")
        delegate.scheduleCollapse()
        try? await Task.sleep(for: .seconds(autoCollapseDelay + 0.5))
        precondition(delegate.presentation.desired == .chat, "Auto-collapse waits for setup to be finished or set aside")
        model.collapsed = true
        model.tab = .research
        model.prepareTab(byHover: true)
        precondition(model.tab == .ask, "While setup is open, hovering reopens Ask")
        model.setupCard = .later
        precondition(!model.holdsOpen, "Later lets the panel collapse again")
        model.tab = .brief
        model.prepareTab(byHover: true)
        precondition(model.tab == .brief, "Setup set aside leaves hover's last tab alone")
        precondition(model.setupCard.afterReadiness(true) == .hidden, "Setup set aside leaves when Wisp is ready")
        // A clicked notification opens Day, even from quick settings on Ask.
        model.tab = .ask
        model.showingQuickSettings = true
        delegate.prepareTodayInNotch()
        precondition(model.tab == .day && !model.showingQuickSettings, "A clicked notification lands on Day")
        print("PASS: AppDelegate setup card opens on Ask, holds the panel until Done or Later; notifications open Day")
    }

    static func checkInlineReportHoldsOpen() async {
        let delegate = AppDelegate()
        let model = delegate.model
        model.todayReport = delegate.todayModel.report
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        model.collapsed = false
        delegate.finishPresentation(opening)
        model.tab = .day
        model.reportCurrentProblem()
        precondition(model.tab == .ask && model.report.snapshot?.key == "chat:none" && model.reportIsUnattached,
                     "With no reply, Report a problem opens one report at the top of Ask")
        precondition(model.holdsOpen, "An open report holds the panel open")
        delegate.scheduleCollapse()
        try? await Task.sleep(for: .seconds(autoCollapseDelay + 0.5))
        precondition(delegate.presentation.desired == .chat, "Auto-collapse waits while a report is open")
        let reply = OverlayModel.Turn(role: "assistant", text: "Hello")
        model.turns = [OverlayModel.Turn(role: "user", text: "hi"), reply]
        model.report.note = "a quicker reply"
        model.reportProblem(for: reply)
        precondition(model.report.snapshot?.key == OverlayModel.reportKey(for: reply) && !model.reportIsUnattached,
                     "A reply's report unfolds under that reply")
        precondition(model.report.note.isEmpty && !model.report.includeDetails
                     && model.report.snapshot?.scrubbedDetails?["prompt"] as? String == "hi",
                     "A new report starts with no note, details off, and the reply's prompt in its snapshot")
        model.reset()
        precondition(!model.report.isOpen, "A new chat closes the report on the conversation it leaves")
        precondition(!model.holdsOpen, "Closing the report lets the panel collapse")
        model.tab = .day
        delegate.todayModel.reportProblem()
        precondition(model.holdsOpen, "Today's report holds the panel open on the Day tab")
        delegate.todayModel.report.close()
        print("PASS: AppDelegate inline reports: one at a time, under their reply, holding the panel while open")
    }

    static func checkMenuHoldsCollapse() async {
        let delegate = AppDelegate()
        delegate.installMenuTrackingObservers()
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        delegate.model.collapsed = false
        delegate.finishPresentation(opening)
        let menu = NSMenu()
        NotificationCenter.default.post(name: NSMenu.didBeginTrackingNotification, object: menu)
        delegate.scheduleCollapse()
        try? await Task.sleep(for: .seconds(autoCollapseDelay + 0.5))
        precondition(delegate.presentation.desired == .chat, "An open menu must hold auto-collapse")
        precondition(delegate.collapseHeldForMenu, "The held collapse must be remembered")
        // The menu closes with the pointer outside the panel: the held collapse runs.
        NotificationCenter.default.post(name: NSMenu.didEndTrackingNotification, object: menu)
        try? await Task.sleep(for: .seconds(autoCollapseDelay + 0.5))
        precondition(delegate.presentation.desired == .bar, "A collapse held by a menu must re-arm when it closes")
        // Closing with the pointer over the panel does not re-arm it.
        let inside = AppDelegate()
        inside.installMenuTrackingObservers()
        inside.pointerInPanel = { true }
        inside.presentation.request(.chat)
        let insideOpening = inside.presentation.next()!
        inside.model.collapsed = false
        inside.finishPresentation(insideOpening)
        inside.collapseHeldForMenu = true
        inside.menuDidEndTracking()
        precondition(inside.pendingCollapse == nil, "Closing a menu over the panel must not re-arm a collapse")
        // A flag left by an unbalanced begin is cleared, but never while a menu is on screen.
        delegate.menuTracking = true
        delegate.clearStaleMenuTracking(menuOnScreen: true)
        precondition(delegate.menuTracking, "An open menu keeps holding auto-collapse")
        delegate.clearStaleMenuTracking(menuOnScreen: false)
        precondition(!delegate.menuTracking, "A stale menu flag is cleared once no menu is open")
        print("PASS: AppDelegate open menu holds auto-collapse, re-arms on close, never sticks")
    }

    static func checkDismissReopenInput() {
        let delegate = AppDelegate()
        delegate.presentation.request(.chat)
        let opening = delegate.presentation.next()!
        delegate.model.collapsed = false
        delegate.model.input = "Old draft"
        delegate.dismissToMenuBar()
        delegate.expand()
        precondition(delegate.model.input.isEmpty, "Reopen must first clear the dismissed draft")
        delegate.model.input = "New draft after reopening"
        delegate.finishPresentation(opening)
        precondition(delegate.model.input == "New draft after reopening",
                     "Opening completion cleared newly entered input")
        precondition(delegate.presentation.settled == .chat)
        print("PASS: AppDelegate dismiss/reopen preserves newly entered input")
    }
}
#endif
