import AppKit
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
    private lazy var researchLibrary = ResearchLibraryWindowController(client: client,
        onOpen: { [weak self] id in self?.openSavedResearch(id) },
        onNew: { [weak self] in self?.newResearch() })
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
    private var pendingCollapse: DispatchWorkItem?
    private var hoverIntent = HoverIntent()
    private var hoverDwell: DispatchWorkItem?
    private var hoverMonitors: [Any] = []
    // Whether the notch-fused bar is showing at all. The X button turns this
    // off (dismiss from the notch, free the resident model, stay in the menu bar); the
    // menu-bar icon or ⌥Space bring it back. Distinct from `model.collapsed`,
    // which only tracks bar-vs-expanded WHILE docked.
    private var notchDocked = true
    private let calendarReader = CalendarReader()
    private let remindersWriter = RemindersWriter()
    private let mailReader = MailReader()
    private let messagesReader = MessagesReader()
    private let notesReader = NotesReader()
    private let contactsReader = ContactsReader()
    private let browserHistoryReader = BrowserHistoryReader()
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
            NSApp.terminate(nil)
            return
        }
        // Port 8000 belongs to oMLX and is deliberately NOT policed here: it used to be
        // cleared by killing whatever listened (which took out MTPLX and Homebrew oMLX
        // along with the odd stray server). oMLX's own peer attribution refuses an
        // engine it cannot verify, with a message that says so.
        //
        // Port 8765 is Wisp's backend. Wisp never signals a program it does not own:
        // a stranger there is reported, and only Wisp's OWN backend (proved by the
        // kernel-reported executable path inside its backend directory) is reclaimed,
        // and only when it has stopped answering.
        let ownedPrefixes = PortGuard.ownedBackendPrefixes(devRoot: backend.backendRootPath)
        switch PortGuard.check(port: 8765, ownedPrefixes: ownedPrefixes) {
        case .conflict(let foreign):
            backend.portConflict = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                self?.presentPortConflict(port: 8765, listeners: foreign)
            }
        case .owned(let own):
            Task { [backend] in
                if !(await backend.isResponsive()) {
                    PortGuard.terminateOwned(own, ownedPrefixes: ownedPrefixes)
                }
            }
        case .free, .unknown:
            break
        }
        // A10 WP3: inert unless the user enabled a browser (default off).
        backend.extraEnvironment = {
            BrowserBridgeActivation.shared.controlPathForBackend()
                .map { ["WISP_BROWSER_BRIDGE_CONTROL": $0] } ?? [:]
        }
        backend.didLaunchBackend = { BrowserBridgeActivation.shared.backendLaunched(pid: $0) }
        Task { await BrowserBridgeActivation.shared.begin() }
        Task { await backend.startIfNeeded() }
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
        setupObserver = NotificationCenter.default.addObserver(
            forName: .wispOpenSetupGuide, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.openSetupGuide() }
        }
        offerSetupGuideOnFirstRun()
        createPanelIfNeeded()
        model.collapsed = true
        panel?.present()
    }

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

    // An accessory app has no menu bar, so the standard edit shortcuts don't route
    // to the focused field. A hidden main menu with an Edit submenu fixes that.
    private func installEditMenu() {
        let main = NSMenu()
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
        NSApp.mainMenu = main
    }

    private func buildMenu() {
        let menu = NSMenu()
        // Keep the menu-bar menu focused on app configuration and diagnostics.
        // Primary workflows live in Wisp's panel, where they have context and
        // progress UI instead of duplicating five shortcuts here.
        menu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "Set Up Inference…", action: #selector(openSetupGuide), keyEquivalent: "")
        menu.addItem(.separator())
        // Debug Mode: a checkable toggle (state synced in toggle() right before
        // the menu is shown, since NSMenuItem state doesn't observe SwiftUI
        // @Published state on its own) that turns on the inline per-reply debug
        // line in the transcript (model, route reason, tok/s, timing, tool
        // calls). Export works any time there's a conversation, regardless of
        // whether Debug Mode is currently on — debug metadata is tracked for
        // every turn unconditionally (see OverlayModel.applyDebugFields), so
        // switching it on partway through a chat doesn't lose earlier turns.
        let debugItem = menu.addItem(withTitle: "Debug Mode", action: #selector(toggleDebugMode), keyEquivalent: "")
        debugModeItem = debugItem
        menu.addItem(withTitle: "Export Chat Debug Log…", action: #selector(exportDebugLog), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Wisp", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        // Attach the menu only on right-click; left-click toggles the panel.
        statusItem.menu = nil
        rightClickMenu = menu
    }

    private var rightClickMenu: NSMenu?
    private var debugModeItem: NSMenuItem?

    // The panel is always on screen; the hotkey and menu-bar click just flip
    // between the notch bar and the expanded assistant. No reset — the visible
    // transcript (and server-side session) survive collapse/expand.
    @objc private func toggle() {
        if let event = NSApp.currentEvent, event.type == .rightMouseUp, let menu = rightClickMenu {
            debugModeItem?.state = model.debugMode ? .on : .off
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
            return
        }
        // Re-dock if the X button previously dismissed the notch bar — the
        // menu-bar icon (or ⌥Space) is the way back in.
        notchDocked = true
        if presentation.desired == .chat { collapse() } else { expand() }
    }

    private func requestPresentation(_ surface: OverlayTransition.Surface) {
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
            // Don't hide an unsent draft out from under the user; a mid-task
            // generation is fine (the notch bar's dot shows it's still working).
            if !self.model.input.trimmingCharacters(in: .whitespaces).isEmpty { return }
            self.collapse()
        }
        pendingCollapse = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.autoCollapseDelay, execute: work)
    }

    private func pointerEnteredPanel() {
        // Reentry cancels auto-collapse even while opening. Only the hover
        // expansion itself must wait for a settled, docked bar.
        cancelScheduledCollapse()
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
        panel = OverlayPanel {
            OverlayView(model: self.model, researchModel: self.researchModel,
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
            self?.calendarReader.createEvent(title: title, startTs: ts,
                                             durationMin: dur, location: loc)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onDeleteCalendarEvent = { [weak self] identifier, occurrenceTs in
            self?.calendarReader.deleteEvent(identifier: identifier, occurrenceTs: occurrenceTs)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onVerifiedReminderAction = { [weak self] kind, actionID, sourceID,
            expectedTitle, expectedDueTs, title, dueTs, commitmentKind in
            self?.remindersWriter.performVerified(kind: kind, actionID: actionID,
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
            return await self.remindersWriter.reconcileVerified(kind: kind, actionID: actionID,
                sourceID: sourceID, title: title, dueTs: dueTs,
                commitmentKind: commitmentKind)
        }
        model.onStartResearch = { [weak self] prompt in
            self?.openResearch(prompt: prompt)
        }
        // Backend requests an on-demand Mail sync (cold-cache email query);
        // refresh both header and raw caches so summarize_emails/view_emails
        // can be served without waiting on the 5-min timer.
        model.onSyncEmails = { [weak self] in
            self?.mailReader.sync()
            self?.mailReader.syncRaw()
        }
        model.onSyncAssistantSources = { [weak self] sources in
            guard let self else { return }
            if sources.contains("calendar") { self.calendarReader.sync() }
            if sources.contains("reminders") { self.remindersWriter.sync() }
            if sources.contains("email") { self.mailReader.sync() }
            if sources.contains("messages") { self.messagesReader.sync() }
            if sources.contains("notes") { self.notesReader.sync() }
            if sources.contains("browser_history") { self.browserHistoryReader.sync() }
        }
        model.startAssistant()
        // Read Calendar + Reminders + Mail + Messages here (clean Wisp.app TCC
        // identity) and push to the backend; prompts for access once, then
        // syncs periodically.
        calendarReader.start()
        remindersWriter.start()
        mailReader.start()
        messagesReader.start()
        notesReader.start()
        browserHistoryReader.start()
        // Resolves chat.db's bare phone/email handles to real contact names.
        contactsReader.start()

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

    /// Start the existing plan-first Research workflow directly from Wisp's
    /// menu-bar menu. Research deliberately opens its own window because the
    /// editable plan, source list, activity log, and cited report do not fit
    /// inside the compact notch panel.
    @objc private func newResearch() {
        let alert = NSAlert()
        alert.messageText = "New Wisp Research"
        alert.informativeText = "Enter a question. Wisp will draft an editable plan before it searches the web."
        alert.addButton(withTitle: "Create plan")
        alert.addButton(withTitle: "Cancel")

        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 440, height: 24))
        field.placeholderString = "What would you like Wisp to research?"
        alert.accessoryView = field

        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let prompt = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }
        openResearch(prompt: prompt)
    }

    @objc private func openResearchLibrary() { researchLibrary.show() }

    private func openSavedResearch(_ id: String) {
        openAssistant()
        model.showingResearch = true
        model.researchMode = false
        researchModel.openJob(id)
    }

    @objc private func freeMemory() {
        Task { await client.unloadAll() }
    }

    @objc private func toggleDebugMode() {
        model.debugMode.toggle()
    }

    // Writes the full conversation (every turn's model, route decision,
    // timing, tok/s, tool calls, and any errors) to ~/Downloads as JSON + a
    // readable .txt companion, then reveals them in Finder. No-ops with a
    // brief alert if there's no conversation yet.
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

    @objc private func quit() {
        // Stopping the engine unloads any models and frees its ~2GB baseline too.
        Task {
            // Let any just-fired Settings change (role/model) finish
            // persisting to ~/.moe/config.yaml before the backend that writes it
            // gets killed — see PendingConfigWrites' docstring for the race this
            // closes. Capped so a genuinely stuck request can't hang quitting.
            await PendingConfigWrites.shared.waitUntilIdle()
            await client.shutdownOMLX()
            await BrowserBridgeActivation.shared.shutdown()
            backend.stop()
            await MainActor.run { NSApp.terminate(nil) }
        }
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            // SettingsView has a fixed 760×620 layout: the sidebar, form labels,
            // and 300-point fields all fit without clipping at this content size.
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 620),
                               styleMask: [.titled, .closable], backing: .buffered, defer: false)
            win.title = "Wisp Settings"
            win.contentView = NSHostingView(rootView: SettingsView())
            win.center()
            win.isReleasedWhenClosed = false
            settingsWindow = win
        }
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow?.makeKeyAndOrderFront(nil)
    }

    /// A program that is not Wisp's own holds the backend port. Wisp does not stop
    /// it: the person decides.
    private func presentPortConflict(port: Int, listeners: [PortGuard.Listener]) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Wisp can't start its service"
        alert.informativeText = "Another program is already using port \(port): "
            + "\(PortGuard.describe(listeners)). Wisp did not stop it, because it isn't Wisp's. "
            + "Quit that program, then reopen Wisp."
        alert.addButton(withTitle: "Quit Wisp")
        alert.addButton(withTitle: "Keep Wisp Open")
        if alert.runModal() == .alertFirstButtonReturn { NSApp.terminate(nil) }
    }

    @objc private func openSetupGuide() {
        // Already open: just bring it forward, so an in-flight action isn't discarded.
        if let open = setupWindow, open.isVisible {
            NSApp.activate(ignoringOtherApps: true)
            open.makeKeyAndOrderFront(nil)
            return
        }
        // Otherwise a fresh view, so the checklist reflects the machine as it is now.
        let win = setupWindow ?? {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 700),
                               styleMask: [.titled, .closable], backing: .buffered, defer: false)
            win.title = "Wisp Setup"
            win.isReleasedWhenClosed = false
            win.center()
            setupWindow = win
            return win
        }()
        win.contentView = NSHostingView(rootView: SetupGuideView(
            onDone: { [weak win] in win?.close() },
            openSettings: { [weak self, weak win] in win?.close(); self?.openSettings() }))
        UserDefaults.standard.set(true, forKey: Self.setupGuideShownKey)
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// On a Mac where Wisp can't answer yet (no engine, or no model), open the
    /// guide once. It never reopens by itself afterwards; the menu-bar item and
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
                if !ready { self.openSetupGuide() }
                else { UserDefaults.standard.set(true, forKey: Self.setupGuideShownKey) }
                return
            }
        }
    }

    private func openResearch(prompt: String) {
        openAssistant()
        model.showingResearch = true
        model.researchMode = false
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
            if delegate.model.phase == .done { break }
            try? await Task.sleep(for: .milliseconds(20))
        }
        precondition(delegate.model.phase == .done, "Summary response was discarded after dismissal/reopen")
        precondition(delegate.model.turns.last?.text == "Synthetic daily summary")
        delegate.model.reset()
        print("PASS: AppDelegate Daily Summary during dismissal completes")
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
