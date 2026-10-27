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
    private var voiceHotKey: GlobalHotKey?
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
    private lazy var notchCorpus = NotchCorpusController(store: wispWindow.store)
    private let model = OverlayModel()
    private let client = WispClient()
    private let backend = BackendManager()
    private var settingsWindow: NSWindow?
    private var setupWindow: NSWindow?
    // The onboarding window: model, permissions, Google, Getting to know you, how
    // to use. Opens by itself only on a Mac that has never run Wisp
    // (FirstRunSetup.shouldShow); Permissions… in the menu opens it at that step.
    private var firstRunWindow: NSWindow?
    private var firstRunCloseObserver: NSObjectProtocol?
    private var firstRunFinishing = false
    private var firstRunPermissionStates: [FirstRunPermissions.Item: FirstRunPermissions.State] = [:]
    // The window's Getting to know you and Google steps keep their state while
    // the person moves between steps (and across a close and reopen).
    private lazy var onboardingKnowYou = KnowYouModel(client: client)
    private lazy var onboardingGoogle = GoogleAccountModel()
    // Which titled Wisp window was key most recently, for the Dock (WispReopen).
    private var windowKeyOrder: [ObjectIdentifier: Int] = [:]
    private var windowKeyCount = 0
    private var setupObserver: NSObjectProtocol?
    // One spelling for the keys the first-run window takes over (FirstRunSetup):
    // a drifted copy would show existing users the window, or new users the old tip.
    private static let setupGuideShownKey = FirstRunSetup.setupGuideShownKey
    private static let notchTipShownKey = FirstRunSetup.notchTipShownKey
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
    // The menu-bar icon carries the bar's status where no bar is drawn
    // (SurfaceLayout.statusInMenuBar). See observeMenuBarStatus.
    private var menuBarStatusObservers: [AnyCancellable] = []
    private var menuBarStatusQueued = false
    private var shownMenuBarStatus: MenuBarStatus?
    private var workingPulse: Timer?
    private var workingPulseDim = false
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
    /// The content's fade at the start of a close: well inside the 0.28 s
    /// retraction, so the shape never carries text for most of its travel.
    static let closeFade: TimeInterval = 0.08

    func applicationDidFinishLaunching(_ notification: Notification) {
        // One Wisp at a time. A second launch used to terminate the first one's
        // backend and race it for the port; now it hands over to the running app.
        let selfPID = ProcessInfo.processInfo.processIdentifier
        let peerApps = NSRunningApplication
            .runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? "")
            .filter { $0.processIdentifier != selfPID }
        if SingleInstance.shouldYield(bundleIdentifier: Bundle.main.bundleIdentifier,
                                      selfPID: selfPID, peers: peerApps.map(\.processIdentifier)) {
            // macOS's Quit & Reopen (after Full Disk Access) can relaunch Wisp while
            // the old copy is still stopping its engine (up to 20 s). Handing over
            // to that dying copy left nothing running, so wait for it to go, then
            // launch. A live copy still gets the hand-over, as before.
            let defaults = UserDefaults.standard
            let shuttingDown = defaults.integer(forKey: Self.shuttingDownPIDKey)
            if FirstRunSetup.waitsForPeerToQuit(
                resumePending: defaults.bool(forKey: FirstRunSetup.resumeKey),
                peerShuttingDown: peerApps.contains { Int($0.processIdentifier) == shuttingDown }) {
                Task { @MainActor [weak self] in
                    // Polled on the main actor between sleeps, so the run loop keeps
                    // turning (isTerminated is updated there) and nothing hangs.
                    let deadline = Date().addingTimeInterval(FirstRunSetup.peerQuitWait)
                    while !peerApps.allSatisfy(\.isTerminated), Date() < deadline {
                        try? await Task.sleep(nanoseconds: 250_000_000)
                    }
                    guard let self else { return }
                    if peerApps.allSatisfy(\.isTerminated) { self.launch() } else { self.yield(to: peerApps) }
                }
                return
            }
            yield(to: peerApps)
            return
        }
        launch()
    }

    /// Set by a Wisp that has started quitting (noteTerminating), so a copy
    /// launched in the meantime waits for it instead of handing over to it.
    private static let shuttingDownPIDKey = "wisp.shuttingDownPID"
    /// The full launch ran. Until then (a second copy waiting for the first to
    /// quit) a Dock click or a quit does nothing of Wisp's own.
    private var launched = false

    private func yield(to peers: [NSRunningApplication]) {
        peers.first { !$0.isTerminated }?.activate(options: [])
        terminateWithoutShutdown()
    }

    private func launch() {
        launched = true
        // A committed /resetwisp (its marker is on disk): this app's part runs once
        // per marker, before any setting is read, so this launch is a first run.
        var resetPending = false
        var ignoredResetMarker = false
        switch WispReset.readMarker() {
        case .valid(let marker):
            resetPending = true
            // Never once the service says "erased": a crash between removing the marker
            // and the app's record, or a marker that won't delete, must not wipe the
            // settings of a Wisp already set up again.
            if !marker.serviceDone && !WispReset.appPartRan(
                markerID: marker.id, record: WispReset.readAppDone(),
                defaultsID: UserDefaults.standard.string(forKey: WispReset.appPartDoneKey)) {
                let problems = WispResetLaunch.run(bundleID: Bundle.main.bundleIdentifier)
                UserDefaults.standard.set(marker.id, forKey: WispReset.appPartDoneKey)
                WispReset.writeAppDone(WispReset.AppDone(id: marker.id, problems: problems))
                // The model read Debug Mode before the defaults were cleared.
                if model.debugMode { model.debugMode = false }
            }
        case .invalid(let reason):
            NSLog("%@", "Wisp reset: ignored a reset marker (\(reason))")
            ignoredResetMarker = !UserDefaults.standard.bool(forKey: WispReset.ignoredMarkerReportedKey)
            UserDefaults.standard.set(true, forKey: WispReset.ignoredMarkerReportedKey)
        case .absent:
            break
        }
        // Whoever was shutting down is gone now.
        UserDefaults.standard.removeObject(forKey: Self.shuttingDownPIDKey)
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
            forName: .wispRestartBackend, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in await self?.backend.restart() }
        }
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
        let launchedAt = Date()
        var outage = 0
        backend.onReachabilityChange = { [weak self] up in
            guard let self else { return }
            outage += 1
            if up {
                self.model.serviceReachable = true
                self.model.serviceStarting = false
                self.considerKnowYouOffer(after: 5)
                // Whatever failed to load while the service was away loads now.
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    await self.todayModel.refresh()
                    await DayCards.refresh(force: true)
                    self.model.onResize()
                }
                return
            }
            let this = outage
            Task { @MainActor [weak self] in
                // Until the service has answered once, a cold first launch can take
                // minutes (bundle verification, first import): that is "Starting
                // Wisp…", not down. Nor is a service busy answering right now.
                while let self, this == outage, self.backend.lastReachable == false,
                      self.model.isProcessing
                        || (!self.backend.everReachable && Date().timeIntervalSince(launchedAt) < 180) {
                    self.model.serviceStarting = true
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                }
                guard let self, this == outage, self.backend.lastReachable == false else { return }
                self.model.serviceStarting = false
                self.model.serviceReachable = false
            }
        }
        model.onRetryService = { [weak self] in
            guard let self else { return }
            // Visible feedback even when a start is already under way.
            self.model.serviceStarting = true
            Task { @MainActor [weak self] in
                await self?.backend.startIfNeeded()
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                guard let self, self.backend.lastReachable != true else { return }
                self.model.serviceStarting = false
                self.model.serviceReachable = false
            }
        }
        backend.startReachabilityWatch()
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
            guard !self.shutdownStarted else { return }
            self.todayModel.startBackgroundRefresh(afterRefresh: { [weak self] in
                guard let self else { return }
                if self.todayModel.isShowingCurrentDay {
                    await CheckInModel.shared.load(day: self.todayModel.day, timezone: self.todayModel.timezone.identifier)
                    self.model.checkInDue = false
                }
                await DayCards.refresh()
                self.model.onResize()
            })
        }
        WispRouter.shared.handler = { [weak self] route in self?.open(route) }
        observeFullAccess()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = Self.menubarOrb()
            button.setAccessibilityLabel("Wisp")
            button.toolTip = "Wisp"
            button.action = #selector(toggle)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        buildMenu()
        observeMenuBarStatus()
        installEditMenu()    // enables ⌘C/⌘V/⌘X/⌘A in the text field
        // ⌥Space summons the overlay
        hotKey = GlobalHotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey)) { [weak self] in
            DispatchQueue.main.async { self?.toggle() }
        }
        // ⌥⇧Space asks by voice, like holding the Siri key: it opens Ask and
        // listens; pressed again while listening, it sends at once.
        voiceHotKey = GlobalHotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey | shiftKey)) { [weak self] in
            DispatchQueue.main.async { self?.askByVoice() }
        }
        // ⌘⇧F is Smart Search. Deliberately NOT ⌘F: Carbon would claim that
        // system-wide and break native find in every app on the machine,
        // including the ones (Xcode, terminals) where native find is better.
        searchHotKey = GlobalHotKey(keyCode: UInt32(kVK_ANSI_F),
                                    modifiers: UInt32(cmdKey | shiftKey)) { [weak self] in
            DispatchQueue.main.async { self?.toggleSearch() }
        }
        // Notchbox-style: the panel lives at the top-centre permanently — the
        // notch on a notched MacBook, an invisible top-edge zone on other
        // built-in displays, a floating capsule on a monitor. Hovering it
        // expands. Layout
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
        NotificationCenter.default.addObserver(
            forName: .wispContinueFirstRun, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.bringSetupForward() }
        }
        // A Brief or Day row about a permission macOS was never asked for.
        NotificationCenter.default.addObserver(
            forName: .wispOpenPermissions, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.openFirstRunSetup(at: .permissions) }
        }
        // Remember which Wisp window was used last, so the Dock brings back that one.
        NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main
        ) { [weak self] note in
            MainActor.assumeIsolated {
                guard let window = note.object as? NSWindow else { return }
                self?.noteKeyWindow(window)
            }
        }
        let defaults = UserDefaults.standard
        let reopens = defaults.integer(forKey: FirstRunSetup.reopensKey)
        let flag: (String) -> Bool = { defaults.bool(forKey: $0) }
        let firstRun = FirstRunSetup.shouldShow(flag, reopens: reopens)
        let countsTowardCap = FirstRunSetup.launchCountsTowardCap(flag)
        // A quit with the window up (or macOS's Quit & Reopen after Full Disk
        // Access) resumes once, here; the next quit sets it again if the window is up.
        defaults.removeObject(forKey: FirstRunSetup.resumeKey)
        if firstRun {
            // Opening by itself again after an early close counts toward the cap;
            // past it, Continue setup and the Dock still open it. Wisp ending under
            // the window never counts (FirstRunSetup.launchCountsTowardCap).
            if countsTowardCap {
                defaults.set(reopens + 1, forKey: FirstRunSetup.reopensKey)
            }
            // The window gets the model, offers permissions and shows where Wisp
            // lives; the old launch did the tip and the setup card on its own.
            FirstRunSetup.takeOverLaunchPrompts { defaults.set(true, forKey: $0) }
        } else {
            offerSetupGuideOnFirstRun()
        }
        createPanelIfNeeded()
        model.collapsed = true
        panel?.present()
        watchSetupReadiness()
        armKnowYouOffers()
        // Wisp starts in the notch. The Wisp window opens only when asked for:
        // the Dock icon, the menu bar, or a hand-off from the notch.
        if firstRun { openFirstRunSetup() } else { showNotchTipOnce() }
        if resetPending { finishPendingReset() }
        if ignoredResetMarker { model.showTransientNotice(WispReset.ignoredMarkerNotice, duration: 15) }
    }

    /// The last step of a reset: once the service's restart pass says "erased" (it
    /// runs in the backend this launch started, before any store opens), remove the
    /// app's record and then the marker, and say what didn't work, if anything. A
    /// restart pass that didn't finish leaves the marker; the next launch waits again
    /// (the service gives up after a few attempts and lists what it left).
    private func finishPendingReset() {
        Task { [weak self] in
            for _ in 0..<90 {
                switch WispReset.readMarker() {
                case .valid(let marker) where marker.serviceDone:
                    let appProblems = WispReset.readAppDone()?.problems ?? []
                    // The marker first: while it says "erased" the app's part never runs
                    // again, so its record may go after it (or stay, if the marker won't).
                    let markerGone = WispReset.removeOwnedFile(WispReset.markerURL().path)
                    if markerGone, !WispReset.removeOwnedFile(WispReset.appDoneURL().path) {
                        NSLog("%@", "Wisp reset: couldn't remove the app's reset record")
                    }
                    if markerGone { UserDefaults.standard.removeObject(forKey: WispReset.appPartDoneKey) }
                    if !markerGone { NSLog("%@", "Wisp reset: couldn't remove the reset marker") }
                    let notice = WispReset.launchNotice(marker: marker, appProblems: appProblems)
                    let text = markerGone ? notice : [notice, WispReset.markerStuckNotice].compactMap { $0 }
                        .joined(separator: " ")
                    if let text { self?.model.showTransientNotice(text, duration: 15) }
                    return
                case .valid:
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                case .absent, .invalid:
                    return
                }
            }
            if case .valid(let marker) = WispReset.readMarker(),
               let notice = WispReset.launchNotice(marker: marker, appProblems: WispReset.readAppDone()?.problems ?? []) {
                self?.model.showTransientNotice(notice, duration: 15)
            }
        }
    }

    /// Opens the onboarding window at the step the person was on (or `step`), or
    /// brings it forward, out of the Dock if it was minimized, if it is already up.
    private func openFirstRunSetup(at step: OnboardingStep? = nil) {
        let defaults = UserDefaults.standard
        if let step { defaults.set(step.rawValue, forKey: FirstRunSetup.stepKey) }
        if let open = firstRunWindow {
            if step != nil { open.contentView = onboardingContent() }
            NSApp.activate(ignoringOtherApps: true)
            if open.isMiniaturized { open.deminiaturize(nil) }
            open.makeKeyAndOrderFront(nil)
            return
        }
        // 600 pt tall where it fits; on a small or scaled display, the room under
        // the menu bar and above the Dock, so the footer's buttons stay reachable.
        let room = NSScreen.main?.visibleFrame.height ?? 800
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: FirstRunSetup.width,
                                               height: FirstRunSetup.windowHeight(room: room)),
                           styleMask: Self.onboardingStyle, backing: .buffered, defer: false)
        win.contentMinSize = NSSize(width: FirstRunSetup.width, height: FirstRunSetup.minimumHeight)
        win.contentMaxSize = NSSize(width: FirstRunSetup.width, height: 900)
        win.title = "Welcome to Wisp"
        win.titlebarAppearsTransparent = true
        win.titleVisibility = .hidden
        win.isMovableByWindowBackground = true
        win.appearance = NSAppearance(named: .darkAqua)
        win.isReleasedWhenClosed = false
        win.contentView = onboardingContent()
        win.center()
        firstRunWindow = win
        // Closing it early does not give setup up: the download keeps going in the
        // engine, the notch's quiet setup cue carries it until Wisp can answer, and
        // the window comes back at this step from the Dock or the next launch.
        firstRunCloseObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: win, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.firstRunClosed() }
        }
        setupModel.show(in: "first-run")
        // "Wisp is ready" is promised only for when the person isn't looking at
        // this window (FirstRunSetup.notifiesReady).
        if setupModel.status?.canAnswer != true { notifyWhenReady() }
        NSApp.activate(ignoringOtherApps: true)
        win.makeKeyAndOrderFront(nil)
    }

    /// Titled, closable, minimizable (the yellow button and ⌘M) and resizable in height.
    static let onboardingStyle: NSWindow.StyleMask = [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView]

    private func onboardingContent() -> NSView {
        let defaults = UserDefaults.standard
        return NSHostingView(rootView: FirstRunSetupView(
            setup: setupModel, knowYou: onboardingKnowYou, google: onboardingGoogle,
            initialStep: OnboardingStep.restored(defaults.object(forKey: FirstRunSetup.stepKey) as? Int),
            openDetailedSetup: { [weak self] in self?.openSetupGuide() },
            finish: { [weak self] prompt in self?.finishFirstRun(asking: prompt) },
            permissionsChanged: { [weak self] states in self?.firstRunPermissionsChanged(states) },
            stepChanged: { [weak self] step in
                UserDefaults.standard.set(step.rawValue, forKey: FirstRunSetup.stepKey)
                // The last step means the permissions and About you are answered, so the
                // first Brief can be composed now instead of at its hour. Only during
                // setup: Permissions… opened later never gets here.
                if step == .tour, FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) }) {
                    Task { await self?.client.onboardingBriefReady() }
                }
            },
            askKeyRegistered: hotKey?.registered ?? false,
            searchKeyRegistered: searchHotKey?.registered ?? false,
            // "Setup still unfinished" (started, not done), not "doneKey unset": someone
            // who upgraded from a build without this window never had doneKey written,
            // and opening Permissions… must not switch or test their model.
            setupDone: !FirstRunSetup.isUnfinished { defaults.bool(forKey: $0) },
            aboutYouDecided: { UserDefaults.standard.set(true, forKey: FirstRunSetup.aboutYouDecidedKey) }))
    }

    /// Done, or one of the last step's prompts: close the window and open the
    /// notch on Ask. A prompt waits in Ask's box, unsent, for the person's Return.
    /// This is the only way setup becomes done (FirstRunSetup.marksDone).
    private func finishFirstRun(asking prompt: String? = nil) {
        firstRunFinishing = true
        let ready = setupModel.status?.canAnswer == true
        firstRunWindow?.close()
        // Not ready yet: the quiet cue (a line on Ask) carries the download in the
        // notch, and a notification says when Wisp can answer.
        if !ready {
            if model.setupCard == .hidden { model.setupCard = .later }
            notifyWhenReady()
        }
        model.showingQuickSettings = false
        model.tab = .ask
        if let prompt { model.input = prompt }
        openAssistant()
    }

    private func firstRunClosed() {
        let defaults = UserDefaults.standard
        let canAnswer = setupModel.status?.canAnswer == true
        // Only Done (or a last-step prompt) ends setup. A close while Wisp is
        // quitting is the quit, and an early close, even with a ready model, leaves
        // setup unfinished at its saved step.
        let ending = FirstRunSetup.ending(finishing: firstRunFinishing, terminating: appTerminating)
        if FirstRunSetup.marksDone(ending) {
            defaults.set(true, forKey: FirstRunSetup.doneKey)
            defaults.removeObject(forKey: FirstRunSetup.stepKey)
            defaults.removeObject(forKey: FirstRunSetup.reopensKey)
            defaults.removeObject(forKey: FirstRunSetup.resumeKey)
        } else if FirstRunSetup.resumesAtNextLaunch(ending) {
            defaults.set(true, forKey: FirstRunSetup.resumeKey)
        } else {
            // Closed early: the person put setup aside, so a mark left by opening
            // Full Disk Access (and never toggling it) must not reopen it past the cap.
            defaults.removeObject(forKey: FirstRunSetup.resumeKey)
        }
        setupModel.hide(from: "first-run")
        if let observer = firstRunCloseObserver { NotificationCenter.default.removeObserver(observer) }
        firstRunCloseObserver = nil
        // Let the window and its view go (the view's permission poll stops with it);
        // Continue setup builds a new one.
        let closing = firstRunWindow
        firstRunWindow = nil
        DispatchQueue.main.async { closing?.contentView = nil }
        defer { firstRunFinishing = false }
        guard ending == .closedEarly else { return }
        // An early close opens nothing by itself: the notch keeps its quiet setup
        // cue, the Dock or Continue setup brings the window back at this step, and
        // "Wisp is ready" follows once the model has arrived.
        if !canAnswer {
            if model.setupCard == .hidden { model.setupCard = .later }
            notifyWhenReady()
        }
    }

    private var setupWatch: Task<Void, Never>?
    private var notifyOnReady = false

    /// Once Wisp can answer, say so to someone who isn't looking at setup: the
    /// model download is the long wait, and nobody should have to keep checking.
    /// The setup cue stays the way in until then.
    private func notifyWhenReady() {
        notifyOnReady = true
        watchSetupReadiness()
    }

    /// From launch until Wisp can answer, on every launch: while it can't and no
    /// first-run window is up, the notch carries the quiet setup cue
    /// (FirstRunSetup.setupCue), so leaving setup aside, an early close or a quit
    /// never leaves Wisp silent on a later launch. Once it can, the cue goes, and
    /// "Wisp is ready" follows if it was promised.
    private func watchSetupReadiness() {
        guard setupWatch == nil else { return }
        let started = Date()
        setupWatch = Task { @MainActor [weak self] in
            var request = URLRequest(url: WispClient.baseURL.appendingPathComponent("setup/status"))
            request.timeoutInterval = 10
            var answered = false
            while !Task.isCancelled {
                // Quick until the service first answers (a cold first launch takes
                // minutes); then every 5 s while a notification is promised, else
                // every 30 s, easing to 5 minutes after an hour.
                let seconds: UInt64 = !answered ? 2 : self?.notifyOnReady == true ? 5
                    : Date().timeIntervalSince(started) < 3600 ? 30 : 300
                try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)
                guard let self else { return }
                guard let (data, _) = try? await URLSession.shared.data(for: request),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ready = object["ready"] as? Bool else { continue }
                answered = true
                let canAnswer = FirstRunSetup.canAnswer(ready: ready,
                                                        remoteOnly: object["remote_only"] as? Bool == true)
                self.setupMenuItem?.title = canAnswer ? "Model & Engine…" : "Finish Setup…"
                let cue = FirstRunSetup.setupCue(self.model.setupCard, canAnswer: canAnswer,
                                                 firstRunWindowUp: self.firstRunWindow != nil)
                if cue != self.model.setupCard { self.model.setupCard = cue }
                guard canAnswer else { continue }
                if FirstRunSetup.notifiesReady(promised: self.notifyOnReady, windowUp: self.firstRunWindow != nil,
                                               windowMinimized: self.firstRunWindow?.isMiniaturized == true) {
                    Notifications.post(id: Notifications.readyId, title: "Wisp is ready",
                                       body: "Press ⌥ Space and ask about your day.")
                }
                self.notifyOnReady = false
                self.setupWatch = nil
                return
            }
        }
    }

    // Clicking the Dock icon, or opening Wisp again from Finder or Spotlight while it
    // is running, brings forward exactly one window (WispReopen): the onboarding
    // window while setup is unfinished, else the Wisp window used last (out of the
    // Dock if minimized), else the Wisp window. It returns false so AppKit doesn't
    // also bring back a minimized window of its own choosing: that, and opening
    // chat while unfinished setup sat in the notch, was the "two windows" bug.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard launched else { return false }
        let guideStandsIn = firstRunHiddenForGuide && (setupWindow?.isVisible == true || setupWindow?.isMiniaturized == true)
        let unfinished = firstRunWindow != nil || guideStandsIn
            || FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) })
        let windows = reopenCandidates()
        switch WispReopen.choose(setupUnfinished: unfinished, windows: windows.map(\.candidate)) {
        case .onboarding:
            bringSetupForward()
        case .window(let id, let deminiaturize):
            guard let window = windows.first(where: { $0.candidate.id == id })?.window else { break }
            NSApp.activate(ignoringOtherApps: true)
            if deminiaturize { window.deminiaturize(nil) }
            window.makeKeyAndOrderFront(nil)
        case .chat:
            open(.chat(sessionId: nil))
        }
        return false
    }

    /// Wisp's own titled windows (not the notch panel, Search, alerts or sheets).
    private func reopenCandidates() -> [(window: NSWindow, candidate: WispReopen.Candidate)] {
        NSApp.windows.enumerated().compactMap { index, window in
            guard window.styleMask.contains(.titled), !(window is NSPanel), window.sheetParent == nil,
                  window.contentView != nil, window !== firstRunWindow else { return nil }
            return (window, WispReopen.Candidate(id: index, visible: window.isVisible,
                                                 minimized: window.isMiniaturized,
                                                 lastKey: windowKeyOrder[ObjectIdentifier(window)] ?? 0))
        }
    }

    private func noteKeyWindow(_ window: NSWindow) {
        guard window.styleMask.contains(.titled), !(window is NSPanel) else { return }
        windowKeyCount += 1
        windowKeyOrder[ObjectIdentifier(window)] = windowKeyCount
    }

    /// The one setup surface on screen, forward: the detailed guide while it
    /// stands in for the first-run window, else the first-run window (reopened at
    /// its step when setup is unfinished), else the notch's setup card.
    private func bringSetupForward() {
        if firstRunHiddenForGuide, let guide = setupWindow, guide.isVisible || guide.isMiniaturized {
            NSApp.activate(ignoringOtherApps: true)
            if guide.isMiniaturized { guide.deminiaturize(nil) }
            guide.makeKeyAndOrderFront(nil)
        } else if firstRunWindow != nil || FirstRunSetup.isUnfinished({ UserDefaults.standard.bool(forKey: $0) }) {
            openFirstRunSetup()
        } else {
            openSetupInNotch()
        }
    }

    // The menu's setup item says where setup is: "Finish Setup…" until Wisp can
    // answer, then "Model & Engine…" (the same place, for changing them later).
    private var setupMenuItem: NSMenuItem?

    @objc private func openSetupFromMenu() { bringSetupForward() }

    /// The onboarding window's Permissions step, any time: each row asks only when clicked.
    @objc private func openPermissionsFromMenu() { openFirstRunSetup(at: .permissions) }

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
            self.model.showTransientNotice(DisplayGeometry.shared.layout.firstRunTip, duration: duration)
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
            bringSetupForward()
        case .knowYou:
            knowYouChosen = true
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
        setupMenuItem = menu.addItem(withTitle: "Model & Engine…", action: #selector(openSetupFromMenu), keyEquivalent: "")
        menu.addItem(withTitle: "Permissions…", action: #selector(openPermissionsFromMenu), keyEquivalent: "")
        menu.addItem(withTitle: "Tell Wisp About You…", action: #selector(tellWispAboutYou), keyEquivalent: "")
        menu.addItem(withTitle: "Grant More Access…", action: #selector(grantMoreAccess), keyEquivalent: "")
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
    private func askByVoice() {
        notchDocked = true
        if presentation.desired != .chat {
            model.prepareTab(byHover: false)
            expand()
        }
        model.toggleVoice()
    }

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
            // An explicit close, like the chevron and Esc: an open Getting to
            // know you counts as Later.
            model.leaveKnowYou()
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
        // A panel that just closed is the moment Getting to know you may be offered.
        if step.to == .bar { considerKnowYouOffer(after: 10) }
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
            // The content fades in the first part of the close, so the
            // retracting shape carries only the surface (see OverlayModel.closing).
            if !panel.reduceMotion() {
                withAnimation(.easeOut(duration: Self.closeFade)) { model.closing = true }
            }
            panel.rollUp { [weak self] in
                guard let self else { return }
                self.model.closing = false
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
                model.closing = false
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
        // Closing the notch outright while Getting to know you is open is Later.
        model.leaveKnowYou()
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
        DispatchQueue.main.asyncAfter(deadline: .now() + DisplayGeometry.shared.layout.hoverDwell, execute: work)
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
        notchCorpus.canStart = { [weak self] in self?.model.canStartCorpusCommand() ?? false }
        notchCorpus.onStateChanged = { [weak self] busy, presented in
            self?.model.corpusBusy = busy
            self?.model.showingCorpus = presented
            self?.model.onResize()
        }
        model.onCorpusCommand = { [weak self] command in self?.notchCorpus.submit(command) }
        model.onHideCorpus = { [weak self] in self?.notchCorpus.hide() }
        model.onResetCorpus = { [weak self] in self?.notchCorpus.stopAndHide() }
        model.onOpenCorpus = { [weak self] in
            guard let self else { return }
            self.notchCorpus.selectInWindow()
            self.wispWindow.show()
        }
        model.onResetWisp = { [weak self] in
            guard let self else { return "Wisp is quitting." }
            return await self.performWispReset()
        }
        panel = OverlayPanel {
            OverlayView(model: self.model, corpus: self.notchCorpus, researchModel: self.researchModel, todayModel: self.todayModel,
                        settings: self.settingsLoader, setup: self.setupModel,
                        onClose: { [weak self] in self?.collapse() },
                        onDismiss: { [weak self] in self?.dismissToMenuBar() })
        }
        panel?.isCompact = { [weak self] in self?.model.collapsed ?? false }
        model.onResize = { [weak self] in
            DispatchQueue.main.async { self?.panel?.resizeToFit() }
        }
        model.onResizeNow = { [weak self] in self?.panel?.resizeToFit() }
        model.requestExpand = { [weak self] in self?.expand() }
        model.requestCollapse = { [weak self] in
            // An explicit close (chevron, Esc) while Getting to know you is
            // open counts as Later. Auto-collapse never reaches here: the open
            // flow holds the panel open.
            self?.model.leaveKnowYou()
            self?.collapse()
        }

        // Assistant layer: the commitment clock and reminder stream (the notch
        // "Up Next" countdown). Nothing here asks macOS for a permission: the
        // setup window's rows do, one click each (Permissions.swift).
        model.onCreateCalendarEvent = { [weak self] title, ts, dur, loc in
            self?.calendarReader?.createEvent(title: title, startTs: ts,
                                              durationMin: dur, location: loc)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onCreateAllDayCalendarEvent = { [weak self] title, day, zone, loc in
            self?.calendarReader?.createAllDayEvent(title: title, day: day, timezone: zone, location: loc)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.showCalendarEventURL = { [weak self] identifier, startTs in
            self?.calendarReader?.showInCalendarURL(identifier: identifier, at: startTs)
        }
        model.onDeleteCalendarEvent = { [weak self] identifier, occurrenceTs in
            self?.calendarReader?.deleteEvent(identifier: identifier, occurrenceTs: occurrenceTs)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onCancelCalendarEvent = { [weak self] identifier, title, expected, invitees in
            self?.calendarReader?.cancelEvent(identifier: identifier, expectedTitle: title,
                                              expectedStart: expected, expectedInvitees: invitees)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onMoveCalendarEvent = { [weak self] identifier, title, expected, start, duration, invitees in
            self?.calendarReader?.moveEvent(identifier: identifier, expectedTitle: title,
                                            expectedStart: expected, newStart: start, durationMin: duration,
                                            expectedInvitees: invitees)
                ?? ["ok": false, "error": "Calendar handler unavailable"]
        }
        model.onVerifiedReminderAction = { [weak self] kind, actionID, sourceID,
            expectedTitle, expectedDueTs, title, dueTs, commitmentKind, dueDate, expectedDueDate in
            self?.remindersWriter?.performVerified(kind: kind, actionID: actionID,
                sourceID: sourceID, expectedTitle: expectedTitle,
                expectedDueTs: expectedDueTs, title: title, dueTs: dueTs,
                commitmentKind: commitmentKind, dueDate: dueDate, expectedDueDate: expectedDueDate)
                ?? ["ok": false, "status": "failed", "error": "Reminders handler unavailable"]
        }
        model.onReconcileReminderAction = { [weak self] kind, actionID, sourceID,
            title, dueTs, commitmentKind, dueDate in
            guard let self else {
                return ["ok": false, "status": "unknown", "error": "Reminders handler unavailable"]
            }
            guard let writer = self.remindersWriter else {
                return ["ok": false, "status": "unknown", "error": "Reminders handler unavailable"]
            }
            return await writer.reconcileVerified(kind: kind, actionID: actionID,
                sourceID: sourceID, title: title, dueTs: dueTs,
                commitmentKind: commitmentKind, dueDate: dueDate)
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
        Notifications.onOpenAsk = { [weak self] in self?.openAssistant() }
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
    // MARK: - Menu-bar status

    /// Where no bar is drawn (a built-in display without a notch), the menu-bar
    /// icon shows what the bar would: working, due today, or not set up.
    private func observeMenuBarStatus() {
        let changed: () -> Void = { [weak self] in self?.queueMenuBarStatus() }
        menuBarStatusObservers = [
            model.objectWillChange.sink { _ in changed() },
            todayModel.objectWillChange.sink { _ in changed() },
            DisplayGeometry.shared.$layout.sink { _ in changed() },
        ]
        updateMenuBarStatus()
    }

    /// Both publishers fire BEFORE the change lands, and a streaming reply fires
    /// once per token: read the state once, on the next turn of the run loop.
    private func queueMenuBarStatus() {
        guard !menuBarStatusQueued else { return }
        menuBarStatusQueued = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.menuBarStatusQueued = false
            self.updateMenuBarStatus()
        }
    }

    private func updateMenuBarStatus() {
        guard let button = statusItem?.button else { return }
        // The same conditions as the collapsed bar (OverlayView.notchBar).
        let status = DisplayGeometry.shared.layout.statusInMenuBar
            ? MenuBarStatus(working: model.isProcessing || model.phase == .streaming,
                            dueToday: todayModel.dueBadgeCount,
                            setupSetAside: model.setupCard == .later)
            : MenuBarStatus(working: false, dueToday: 0, setupSetAside: false)
        guard status != shownMenuBarStatus else { return }
        shownMenuBarStatus = status
        button.title = status.title
        button.imagePosition = status.title.isEmpty ? .imageOnly : .imageLeading
        button.setAccessibilityLabel(status.accessibilityLabel)
        setWorkingPulse(status.mark == .working)
    }

    /// The bar's breathing dot, on the icon: a slow fade while Wisp works. With
    /// Reduce Motion it holds dimmed instead.
    private func setWorkingPulse(_ on: Bool) {
        workingPulse?.invalidate()
        workingPulse = nil
        workingPulseDim = false
        statusItem.button?.alphaValue = 1
        guard on else { return }
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            statusItem.button?.alphaValue = 0.55
            return
        }
        workingPulse = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, let button = self.statusItem.button else { return }
                self.workingPulseDim.toggle()
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = 0.9
                    button.animator().alphaValue = self.workingPulseDim ? 0.35 : 1
                }
            }
        }
    }

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
        startPromptingReaders()
        guard !nativeSourceReadersStarted else { return }
        nativeSourceReadersStarted = true
        // These never make macOS ask: a missing Full Disk Access is only a failed read.
        messagesReader = MessagesReader()
        browserHistoryReader = BrowserHistoryReader()
        activityReader = ActivityReader()
        messagesReader?.start()
        browserHistoryReader?.start()
        activityReader?.start()
    }

    /// The source readers, each started once at launch. None of them asks macOS
    /// for access (Permissions.swift): Calendar and Reminders read only what is
    /// already allowed, Contacts never asks, and Mail and Notes skip scripted
    /// reads until Automation has an answer.
    private func startPromptingReaders() {
        if calendarReader == nil {
            calendarReader = CalendarReader()
            calendarReader?.start()
        }
        if mailReader == nil {
            mailReader = MailReader()
            mailReader?.start()
        }
        if remindersWriter == nil {
            remindersWriter = RemindersWriter()
            remindersWriter?.start()
        }
        if notesReader == nil {
            notesReader = NotesReader()
            notesReader?.start()
        }
        if contactsReader == nil {
            // Contacts is asked for from its row in setup, never by the reader.
            contactsReader = ContactsReader(mayAsk: { false })
            contactsReader?.start()
        }
    }

    /// The setup window read its permissions: a new grant is read at once instead
    /// of on the reader's next tick.
    private func firstRunPermissionsChanged(_ states: [FirstRunPermissions.Item: FirstRunPermissions.State]) {
        let before = firstRunPermissionStates
        firstRunPermissionStates = states
        for (item, state) in states where state == .allowed && before[item].map({ $0 != .allowed }) == true {
            resyncReaders(after: item)
        }
    }

    private func resyncReaders(after item: FirstRunPermissions.Item) {
        switch item {
        case .calendar:
            calendarReader?.sync()
        case .reminders:
            remindersWriter?.sync()
        case .mail:
            mailReader?.sync()
            mailReader?.syncRaw()
            notesReader?.sync()
        case .fullDiskAccess:
            // Messages, and the on-disk Mail and Notes reads used while those apps are closed.
            messagesReader?.sync()
            mailReader?.sync()
            notesReader?.sync()
        case .contacts:
            contactsReader?.sync()
        case .messages, .notifications:
            // Sending only: nothing here reads with them.
            break
        }
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
    /// Set before anything else on every way out, so the first-run window's close
    /// (if AppKit sends one while Wisp ends) is read as the quit, never as the
    /// person finishing or putting setup aside.
    private var appTerminating = false

    /// Wisp is ending: with setup unfinished and its window up (or stood aside for
    /// the detailed guide), the next launch reopens it at its step, uncounted.
    private func noteTerminating() {
        guard !appTerminating else { return }
        appTerminating = true
        // A copy that is only handing over (or waiting) owns nothing to resume.
        guard launched else { return }
        let defaults = UserDefaults.standard
        defaults.set(Int(ProcessInfo.processInfo.processIdentifier), forKey: Self.shuttingDownPIDKey)
        if firstRunWindow != nil, FirstRunSetup.resumesAtNextLaunch(.appQuit),
           FirstRunSetup.isUnfinished({ defaults.bool(forKey: $0) }) {
            defaults.set(true, forKey: FirstRunSetup.resumeKey)
        }
    }
    /// True when the system (logout, restart) asked to quit and is waiting for a reply.
    private var systemAwaitingReply = false

    private func beginShutdown() {
        noteTerminating()
        shutdownStarted = true
        todayModel.stopBackgroundRefresh()
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
            // Only Wisp's own service: another program on the port (kept open at
            // the port-conflict alert) must not have its engine stopped for good.
            if !backend.portConflict { await client.shutdownOMLX() }
            await BrowserBridgeActivation.shared.shutdown()
            await backend.stopAndWait()
            await MainActor.run { self.finishShutdown() }
        }
    }

    /// /resetwisp, after the person clicked Reset Wisp on its card. Returns why it
    /// stopped. Once the service has committed the reset (its marker is on disk)
    /// this never returns: Wisp stops the engine and the service and relaunches,
    /// and that launch finishes the reset (WispReset), so no launch ever sees half
    /// a reset. A reset whose answer was lost is finished by the next launch too.
    func performWispReset() async -> String {
        guard launched, !shutdownStarted else { return "Wisp is quitting." }
        guard !backend.portConflict else {
            return "Another program holds Wisp's port, so nothing was reset."
        }
        // Wisp's own activity. The service seals itself, cancels its requests and
        // stops its schedulers and syncs.
        todayModel.stopBackgroundRefresh()
        let (status, body) = await client.resetWisp()
        var reply = WispReset.ServiceReply.parse(status: status, body: body)
        if reply.kind == .unknown {
            // No answer: the marker on disk says whether the reset committed.
            if case .valid = WispReset.readMarker() {
                reply = .afterNoAnswer(markerOnDisk: true)
            } else {
                reply = .afterNoAnswer(markerOnDisk: false)
            }
        }
        guard reply.relaunches else { return reply.detail }
        NSLog("%@", "Wisp reset: service committed (\(reply.kind == .erased ? "erased" : "unfinished")); relaunching")
        noteTerminating()
        shutdownStarted = true
        // The engine and the service stop as on quit; the model files stay. The next
        // service start erases the engine's prompt cache, now that it is stopped.
        await client.shutdownOMLX()
        await BrowserBridgeActivation.shared.shutdown()
        await backend.stopAndWait()
        // Open again once this process is gone; that launch finishes the reset.
        if let arguments = WispReset.relaunchArguments(pid: ProcessInfo.processInfo.processIdentifier,
                                                        app: Bundle.main.bundleURL) {
            let relaunch = Process()
            relaunch.executableURL = URL(fileURLWithPath: WispReset.relaunchShell)
            relaunch.arguments = arguments
            try? relaunch.run()
        }
        // Not NSApp.terminate: nothing this process still writes matters, and the
        // relaunched Wisp clears the defaults before reading any.
        exit(0)
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
        noteTerminating()
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
        noteTerminating()
        if shutdownFinished { return .terminateNow }
        // Still waiting for a quitting copy: no engine or backend of ours to stop.
        if !launched { shutdownFinished = true; return .terminateNow }
        systemAwaitingReply = true
        if !shutdownStarted { beginShutdown() }
        return .terminateLater
    }

    @objc private func openSettings() {
        if settingsWindow == nil {
            // SettingsView has a fixed 760×620 layout: the sidebar, form labels,
            // and 300-point fields all fit without clipping at this content size.
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 620),
                               styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            win.title = "Wisp Settings"
            win.contentView = NSHostingView(rootView: SettingsView(loader: settingsLoader))
            win.center()
            win.isReleasedWhenClosed = false
            settingsWindow = win
        }
        NSApp.activate(ignoringOtherApps: true)
        if settingsWindow?.isMiniaturized == true { settingsWindow?.deminiaturize(nil) }
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
    // The first-run window steps aside while the detailed guide is up (both
    // centred, the guide covering it, read as two Wisp windows at once) and
    // comes back, at its step, when the guide closes.
    private var firstRunHiddenForGuide = false

    @objc private func openSetupGuide() {
        if let welcome = firstRunWindow, welcome.isVisible {
            welcome.orderOut(nil)
            firstRunHiddenForGuide = true
        }
        // Already open: just bring it forward, so an in-flight action isn't discarded.
        if let open = setupWindow, open.isVisible || open.isMiniaturized {
            NSApp.activate(ignoringOtherApps: true)
            if open.isMiniaturized { open.deminiaturize(nil) }
            open.makeKeyAndOrderFront(nil)
            return
        }
        let win = setupWindow ?? {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 700),
                               styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
            win.title = "Wisp Setup"
            win.isReleasedWhenClosed = false
            win.center()
            setupWindow = win
            // This window object is the host, not its title: closing it is the
            // window's last word on setup, however SwiftUI keeps its view.
            setupCloseObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: win, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.setupModel.hide(from: "window")
                    if self.firstRunHiddenForGuide {
                        self.firstRunHiddenForGuide = false
                        DispatchQueue.main.async { self.openFirstRunSetup() }
                    }
                }
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
            // The backend starts alongside the app; a cold first launch can take
            // minutes (bundle verification, first import), so wait up to ~3 min.
            for _ in 0..<120 {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                guard let self else { return }
                guard let (data, _) = try? await URLSession.shared.data(for: request),
                      let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let ready = object["ready"] as? Bool else { continue }
                if !ready { self.openSetupInNotch() }
                UserDefaults.standard.set(true, forKey: Self.setupGuideShownKey)
                return
            }
            // Never came up: say so in the notch instead of staying silent. The
            // guide is not marked shown, so the next launch tries again.
            // It isn't opened unasked: the reachability watch already shows it
            // the next time the notch opens.
            guard let self, self.backend.lastReachable != true else { return }
            self.model.serviceStarting = false
            self.model.serviceReachable = false
        }
    }

    /// The permissions first launch defers (Screen Recording, Accessibility,
    /// Full Disk Access), asked for when the person chooses to: only what is
    /// still off, each with its reason, and one system surface per click.
    @objc private func grantMoreAccess() {
        let missing = Permissions.missingExtras()
        let alert = NSAlert()
        if let next = missing.first {
            alert.messageText = "Give Wisp more access"
            alert.informativeText = missing.map { "• \($0.title), \($0.reason)." }.joined(separator: "\n")
                + (missing.count > 1 ? "\n\nOne at a time: choose Grant More Access again for the next." : "")
            alert.addButton(withTitle: next == .fullDiskAccess ? "Open Full Disk Access…" : "Allow \(next.title)…")
            alert.addButton(withTitle: "Not Now")
        } else {
            alert.messageText = "Wisp has all the access it can use"
            alert.informativeText = "Screen Recording, Accessibility and Full Disk Access are on."
            alert.addButton(withTitle: "OK")
        }
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn, let next = missing.first { Permissions.request(next) }
    }

    /// Getting to know you, from the menu bar. Choosing it
    /// ends Wisp's own offers for this session.
    @objc private func tellWispAboutYou() {
        knowYouChosen = true
        openAssistant()
        model.startKnowYou()
    }

    private var knowYouChosen = false
    /// Set from GET /profile: once About you is complete, panel closes stop asking.
    private var knowYouProfileComplete = false
    private var knowYouOffersArmed = false
    private var knowYouCheck: Task<Void, Never>?
    private var knowYouTimer: Timer?
    private var wakeObserver: NSObjectProtocol?

    /// Wisp's own offer of Getting to know you, for the life of the app: checked
    /// when the service comes up, when the panel closes, on wake and every half
    /// hour. The service says whether it is due (never finished, no recent Later,
    /// at most twice on its own; the menu bar, /about and Memory → About you remain
    /// the way in); KnowYouOffer says whether now is a quiet moment.
    private func armKnowYouOffers() {
        guard !knowYouOffersArmed else { return }
        knowYouOffersArmed = true
        knowYouTimer = Timer.scheduledTimer(withTimeInterval: 1800, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.considerKnowYouOffer() }
        }
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.considerKnowYouOffer(after: 30) }
        }
        considerKnowYouOffer(after: 20)
    }

    private var knowYouMoment: KnowYouOffer.Moment {
        KnowYouOffer.Moment(
            firstRunWindowUp: firstRunWindow != nil, setupWindowUp: setupWindow?.isVisible == true,
            setupCardOpen: model.setupCard == .open, replying: model.isProcessing,
            flowOpen: model.knowYou != nil,
            panelInUse: presentation.desired == .chat || presentation.desired == .search
                || !model.collapsed || model.holdsOpen,
            dismissedToMenuBar: !notchDocked, chosenThisSession: knowYouChosen,
            profileComplete: knowYouProfileComplete,
            aboutYouDecidedInWindow: FirstRunSetup.aboutYouDecidedInWindow { UserDefaults.standard.bool(forKey: $0) })
    }

    /// One check, after `seconds` (a panel that just closed gets a moment before
    /// anything opens it again). A newer trigger replaces a pending one.
    private func considerKnowYouOffer(after seconds: Double = 0) {
        guard knowYouOffersArmed else { return }
        knowYouCheck?.cancel()
        knowYouCheck = Task { @MainActor [weak self] in
            if seconds > 0 { try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }
            guard !Task.isCancelled, let self, KnowYouOffer.quiet(self.knowYouMoment) else { return }
            guard let envelope = await WispClient().profile(), !Task.isCancelled else { return }
            // Finished, or both automatic offers spent (two Laters, or an erase,
            // which records the same): stop asking the service on every close.
            if envelope.status.completed || envelope.profile.dismissCount >= 2 { self.knowYouProfileComplete = true }
            guard KnowYouOffer.shouldOffer(self.knowYouMoment, serviceOffers: envelope.status.offer) else { return }
            // An offer nobody asked for must not take the keyboard from the
            // app the person is typing in: it opens without becoming key.
            self.createPanelIfNeeded()
            self.panel?.revealsWithoutKey = true
            self.openAssistant()
            self.model.startKnowYou()
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            self.panel?.revealsWithoutKey = false
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
