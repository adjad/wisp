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
    private lazy var researchWindowController = ResearchWindowController(model: researchModel)
    private var searchKeyMonitor: Any?
    private var searchTransitioning = false
    private let model = OverlayModel()
    private let client = WispClient()
    private let backend = BackendManager()
    private var settingsWindow: NSWindow?
    private var pendingCollapse: DispatchWorkItem?
    private var transitioning = false
    private var transitionToken: UUID?
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
        PortGuard.reserve(port: 8000, exemptExecutablePrefixes: ["/Applications/oMLX.app", "\(NSHomeDirectory())/Applications/oMLX.app"])
        PortGuard.reserve(port: 8765, exemptExecutablePrefixes: [])
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
        // Notchbox-style: the panel lives at the notch permanently. Collapsed
        // it's a black bar fused with the camera housing; hovering it expands.
        if let screen = OverlayPanel.notchScreen(),
           let m = OverlayPanel.notchMetrics(for: screen) {
            model.notchWidth = m.width
            model.notchInset = m.inset
        }
        createPanelIfNeeded()
        model.collapsed = true
        panel?.present()
    }

    // MARK: - Smart Search (⌘⇧F)

    @objc private func toggleSearch() {
        if let panel = searchPanel, panel.isVisible {
            closeSearch()
            return
        }
        guard !searchTransitioning else { return }
        // Only one thing can be fused to the physical notch at a time. If the
        // chat panel is currently expanded there, roll it up first rather
        // than stacking two opaque panels at the same screen location.
        if !model.collapsed { collapse() }
        // ORDER MATTERS: read the focused window BEFORE Wisp takes focus.
        // Once our panel is key, `frontmostApplication` is Wisp and there's
        // nothing left to read — PageReader would return empty.
        Task { @MainActor in
            let page = await PageReader.read()
            searchModel.reset()
            searchModel.capture(page)
            openSearchPanel()
        }
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

    private func openSearchPanel() {
        createSearchPanelIfNeeded()
        guard let panel = searchPanel else { return }
        searchTransitioning = true
        panel.present()
        // Let the host lay out SearchView's natural (small, field-only) size
        // before computing the drop-open target frame — same two-step
        // present()-then-dropOpen() sequence the chat panel uses in expand().
        DispatchQueue.main.async { [weak self] in
            panel.dropOpen { self?.searchTransitioning = false }
        }
        installSearchKeyMonitor()
    }

    private func closeSearch() {
        guard let panel = searchPanel, panel.isVisible, !searchTransitioning else { return }
        if let m = searchKeyMonitor { NSEvent.removeMonitor(m); searchKeyMonitor = nil }
        searchTransitioning = true
        panel.makeFirstResponder(nil)
        panel.rollUp { [weak self] in
            guard let self else { return }
            panel.orderOut(nil)
            panel.settleToBar()
            self.searchModel.reset()
            self.searchTransitioning = false
        }
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
        menu.addItem(withTitle: "Free up memory (keep running)", action: #selector(freeMemory), keyEquivalent: "")
        menu.addItem(withTitle: "Settings…", action: #selector(openSettings), keyEquivalent: ",")
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
        if model.collapsed { expand() } else { collapse() }
    }

    // Guard against re-entrant expand/collapse while a drop/roll animation is
    // still running. Clicking the menu-bar icon fast (open→close→open) used to
    // fire a second expand()/collapse() mid-animation, so dropOpen's and
    // rollUp's mask animations collided — the "opens weirdly" glitch. New
    // toggles during a transition are ignored; a safety timeout guarantees the
    // flag never sticks if a completion block is somehow missed.
    private func beginTransition() {
        transitioning = true
        let token = UUID(); transitionToken = token
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            if self?.transitionToken == token { self?.transitioning = false }
        }
    }
    private func endTransition() { transitioning = false; transitionToken = nil }

    // Expand: switch to the expanded content, then unroll it straight down.
    private func expand() {
        cancelScheduledCollapse()
        createPanelIfNeeded()
        guard !transitioning else { return }
        // Same notch, same rule as toggleSearch: only one panel fuses with it
        // at a time.
        if searchPanel?.isVisible == true { closeSearch() }
        guard model.collapsed || panel?.isVisible != true else { return }
        beginTransition()
        if panel?.isVisible != true { panel?.present() }
        model.collapsed = false                       // SwiftUI swaps to expanded content
        // Drop open on the next runloop, after the host has laid out at full
        // height, so `dropOpen` starts from a valid full-frame target.
        DispatchQueue.main.async { [weak self] in
            self?.panel?.dropOpen { [weak self] in self?.endTransition() }
        }
    }

    // Collapse: roll the panel up first, THEN swap back to the notch bar — so
    // the expanded content stays visible while it retracts (a true reverse of
    // the drop), instead of popping to the bar before the animation.
    private func collapse() {
        cancelScheduledCollapse()
        guard let panel, !model.collapsed, !transitioning else { return }
        beginTransition()
        panel.makeFirstResponder(nil)                 // stop swallowing keystrokes
        panel.rollUp { [weak self] in
            guard let self else { return }
            self.model.collapsed = true               // SwiftUI swaps to the bar
            DispatchQueue.main.async {
                self.panel?.settleToBar()
                self.endTransition()
            }
        }
    }

    // The X button: NOT quit. Dismisses the notch-fused bar entirely (so it
    // stops inviting hover-opens), clears the conversation so reopening starts
    // fresh, and frees the resident model's memory — but Wisp keeps running. The
    // menu-bar icon (or ⌥Space) brings it right back, and the small always-on
    // router model stays warm for a fast reopen.
    private func dismissToMenuBar() {
        cancelScheduledCollapse()
        notchDocked = false
        model.newChat()   // clear the visible transcript; next message starts a new session
        let hide = { [weak self] in
            guard let self else { return }
            self.panel?.orderOut(nil)
            Task { await self.client.unloadAgent() }
        }
        if let panel, !model.collapsed {
            panel.makeFirstResponder(nil)
            panel.rollUp { [weak self] in
                self?.model.collapsed = true
                hide()
            }
        } else {
            hide()
        }
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

    private func cancelScheduledCollapse() {
        pendingCollapse?.cancel()
        pendingCollapse = nil
    }

    private func createPanelIfNeeded() {
        guard panel == nil else { return }
        panel = OverlayPanel {
            OverlayView(model: self.model,
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
        }
        model.onDeleteCalendarEvent = { [weak self] identifier, occurrenceTs in
            self?.calendarReader.deleteEvent(identifier: identifier, occurrenceTs: occurrenceTs)
        }
        model.onCreateAppleReminder = { [weak self] title, dueTs in
            self?.remindersWriter.create(title: title, dueTs: dueTs)
        }
        model.onDeleteAppleReminder = { [weak self] identifier in
            self?.remindersWriter.delete(identifier: identifier)
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
        panel?.onMouseEnter = { [weak self] in
            guard let self else { return }
            if self.model.collapsed { self.expand() } else { self.cancelScheduledCollapse() }
        }
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
            // Let any just-fired Settings change (role/model, Super Model) finish
            // persisting to ~/.moe/config.yaml before the backend that writes it
            // gets killed — see PendingConfigWrites' docstring for the race this
            // closes. Capped so a genuinely stuck request can't hang quitting.
            await PendingConfigWrites.shared.waitUntilIdle()
            await client.shutdownOMLX()
            backend.stop()
            await MainActor.run { NSApp.terminate(nil) }
        }
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 620),
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

    private func openResearch(prompt: String) {
        researchWindowController.open(prompt: prompt)
    }
}
