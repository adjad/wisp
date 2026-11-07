#if WISP_NOTCH_QA
import SwiftUI
import AppKit
import ApplicationServices

// Permissions and Settings at their real cap, each state they can be in, on three
// screens: every height printed, a PNG of each, and real clicks on a row and on the
// section switcher. Windows stay offscreen, and it never contacts the Wisp service.
//
//   WISP_NOTCH_QA_PAGES=1 WISP_NOTCH_QA_OUT=/some/dir .build/debug/WispApp -WispTextSize 1.3
// The header probe mounts the real OverlayView, whose refreshes go to the service:
// run it where network is denied (sandbox-exec, as scripts/test_brief_time_view.sh does).
@MainActor
enum NotchQAPages {
    typealias Item = FirstRunPermissions.Item
    typealias States = [Item: FirstRunPermissions.State]

    private static var windows: [(name: String, window: NSWindow, limit: CGFloat)] = []
    private static var kept: [AnyObject] = []

    /// (screen name, width, height).
    static let screens: [(String, CGFloat, CGFloat)] = [("1117", 1728, 1117), ("982", 1512, 982), ("878", 1352, 878)]

    /// The drawings' four states.
    static let shot: States = [.calendar: .allowed, .reminders: .allowed, .mail: .allowed, .messages: .allowed,
                               .fullDiskAccess: .allowed, .contacts: .notAsked, .notifications: .allowed,
                               .screenRecording: .notAllowed, .accessibility: .notAllowed]
    static var link: States { shot.merging([.fullDiskAccess: .notAsked]) { $1 } }
    static let allowed: States = Dictionary(uniqueKeysWithValues: Item.allCases.map { ($0, .allowed) })
    static let fresh: States = Dictionary(uniqueKeysWithValues: Item.allCases.map { ($0, .notAsked) })

    static func layout(_ w: CGFloat, _ h: CGFloat) -> SurfaceLayout {
        let side = (w - 185) / 2
        return SurfaceLayout.make(for: DisplayMetrics(
            frame: CGRect(x: 0, y: 0, width: w, height: h),
            visibleFrame: CGRect(x: 0, y: 0, width: w, height: h - 32),
            safeAreaTop: 32, auxLeftWidth: side, auxRightWidth: side, isBuiltIn: true))
    }

    /// What the real OverlayView measured its header at (headerProbe).
    static var liveHeader: CGFloat?

    /// The real panel, on Ask, once, for its real header height at this text size.
    private static func headerProbe() async -> CGFloat? {
        let model = OverlayModel()
        model.collapsed = false
        let today = TodayModel(now: { Date() }, timeZoneProvider: { .current }, transport: { _ in throw URLError(.cannotConnectToHost) })
        let client = WispClient()
        let view = OverlayView(model: model, corpus: NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client))), researchModel: ResearchModel(client: client),
                               todayModel: today, settings: SettingsLoader(), setup: SetupGuideModel(), onClose: {})
            .environment(\.colorScheme, .dark)
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 640, height: 500),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        let host = NSHostingView(rootView: view)
        window.contentView = host
        kept += [model, today, window]
        for _ in 0..<50 where liveHeader == nil {
            host.layoutSubtreeIfNeeded()
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return liveHeader
    }

    /// The panel around a page, as OverlayView.expandedPanel lays it out, with a
    /// band the height of the real header.
    private static func add<Page: View>(_ name: String, _ layout: SurfaceLayout, header: CGFloat, width: CGFloat? = nil,
                                        @ViewBuilder page: (CGFloat) -> Page) {
        let panelWidth = width ?? layout.expandedWidth
        let content = VStack(alignment: .leading, spacing: 0) {
            Text("HEADER (fixed)").font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .frame(maxWidth: .infinity, minHeight: header, maxHeight: header, alignment: .leading)
            page(layout.pageContentCap(headerHeight: header)).padding(.top, SurfaceLayout.tabTopPadding)
        }
        .padding(EdgeInsets(top: layout.contentTopInset, leading: 22,
                            bottom: SurfaceLayout.panelBottomPadding, trailing: 22))
        .frame(width: panelWidth, alignment: .topLeading)
        .background(Theme.surface)
        .environment(\.colorScheme, .dark)
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: panelWidth, height: 760),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        let host = NSHostingView(rootView: content)
        window.contentView = host
        window.setContentSize(host.fittingSize)
        windows.append((name, window, layout.maxPagePanelHeight))
    }

    private static func permissions(_ name: String, _ layout: SurfaceLayout, header: CGFloat, focus: Item?, states: States,
                                    width: CGFloat? = nil) {
        let model = OverlayModel()
        model.tab = .brief
        model.permissionPage.opened(focus: focus)
        model.permissionPage.access.qaShow(states: states, asking: [], waiting: [], messagesReadable: true)
        kept.append(model)
        add(name, layout, header: header, width: width) { cap in NotchPermissionsView(model: model, cap: cap) }
    }

    private static func settings(_ name: String, _ layout: SurfaceLayout, header: CGFloat,
                                 section: QuickSettingsSection, known: Bool = true,
                                 picking: QuickSettingsView.BriefSlot? = nil, width: CGFloat? = nil) {
        // A new user: no account yet, and a client to connect with.
        let google = NotchGoogleModel()
        if !known {
            google.status.clientSource = "builtin"
            google.loaded = true
        }
        let model = OverlayModel()
        model.qaShow(modelMemory: ModelMemory(models: [.init(id: "gemma-4-e4b", bytes: 11_200_000_000),
                                                       .init(id: "whisper", bytes: 800_000_000)]))
        let loader = SettingsLoader()
        loader.qaShow(fullAccess: known ? true : nil, briefTimes: known ? WispClient.BriefTimes(morning: 10, evening: 20) : nil,
                      saveWhenAway: known ? true : nil, saveMemory: known ? true : nil)
        loader.behaviorEnabled = known ? true : nil
        kept.append(model)
        kept.append(loader)
        add(name, layout, header: header, width: width) { cap in
            QuickSettingsView(model: model, settings: loader, refreshes: false, cap: cap, section: section,
                              picking: picking, workingHours: "9 AM to 5 PM", google: google)
        }
    }

    static func run(outDir: URL) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            let header = await headerProbe() ?? 30
            print("header \(liveHeader == nil ? "NOT measured, using" : "measured from OverlayView:") \(header) pt")
            for (screen, w, h) in screens {
                let l = layout(w, h)
                permissions("permissions-\(screen)-list", l, header: header, focus: nil, states: shot)
                permissions("permissions-\(screen)-shot", l, header: header, focus: .fullDiskAccess, states: shot)
                permissions("permissions-\(screen)-link", l, header: header, focus: .fullDiskAccess, states: link)
                permissions("permissions-\(screen)-allowed", l, header: header, focus: nil, states: allowed)
                permissions("permissions-\(screen)-new", l, header: header, focus: nil, states: fresh)
                permissions("permissions-\(screen)-screen-link", l, header: header, focus: .screenRecording, states: shot)
                for section in QuickSettingsSection.allCases {
                    settings("settings-\(screen)-\(section.rawValue)", l, header: header, section: section)
                }
                settings("settings-\(screen)-general-new-user", l, header: header, section: .general, known: false)
                settings("settings-\(screen)-brief-hours", l, header: header, section: .brief, picking: .morning)
            }
            // The panel's 320 pt floor.
            let small = layout(1352, 878)
            permissions("permissions-320-shot", small, header: header, focus: nil, states: shot, width: 320)
            settings("settings-320-general", small, header: header, section: .general, width: 320)
            settings("settings-320-voice", small, header: header, section: .voice, width: 320)

            // A CappedScroll learns its content's height over a pass or two.
            for _ in 0..<3 {
                try? await Task.sleep(nanoseconds: 300_000_000)
                for entry in windows {
                    guard let view = entry.window.contentView else { continue }
                    entry.window.setContentSize(view.fittingSize)
                    view.layoutSubtreeIfNeeded()
                }
            }
            print("text scale \(Theme.textScale), header \(header) pt")
            var failed = false
            for entry in windows {
                guard let view = entry.window.contentView else { continue }
                let height = view.bounds.height
                let scroll = outerScroll(in: view)
                let document = scroll?.documentView?.frame.height ?? 0
                let visible = scroll?.contentView.bounds.height ?? 0
                let scrolls = document > visible + 0.5
                print("\(entry.name): \(Int(height.rounded())) pt, limit \(Int(entry.limit)), "
                      + "scroll \(Int(visible.rounded()))/\(Int(document.rounded()))\(scrolls ? " SCROLLS" : "")")
                if height > entry.limit + 0.5 && !entry.name.contains("-320-") {
                    print("  FAILED: passes the limit")
                    failed = true
                }
                if let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?
                        .write(to: outDir.appendingPathComponent("\(entry.name).png"))
                }
            }
            await interactionChecks(&failed, outDir: outDir)
            print(failed ? "PAGES: FAILED" : "PAGES: passed")
            app.terminate(nil)
        }
        app.run()
    }

    private static func outerScroll(in view: NSView) -> NSScrollView? {
        if let scroll = view as? NSScrollView { return scroll }
        for sub in view.subviews { if let found = outerScroll(in: sub) { return found } }
        return nil
    }

    // MARK: Clicks, and what VoiceOver could find

    /// A real mouse click, down and up, at a point measured from the window's top left.
    private static func click(_ window: NSWindow, x: CGFloat, fromTop: CGFloat) async {
        let point = NSPoint(x: x, y: window.frame.height - fromTop)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                              windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1) {
                window.sendEvent(event)
            }
            try? await Task.sleep(nanoseconds: 60_000_000)
        }
        try? await Task.sleep(nanoseconds: 350_000_000)
    }

    private static func fit(_ window: NSWindow) -> CGFloat {
        guard let view = window.contentView else { return 0 }
        view.layoutSubtreeIfNeeded()
        window.setContentSize(view.fittingSize)
        view.layoutSubtreeIfNeeded()
        return view.bounds.height
    }

    /// Clicks on a row's title, its empty middle, its status and its chevron, which
    /// all sit over the row's one button; then the section switcher. It never clicks a
    /// row's action, which would ask macOS for a permission.
    private static func interactionChecks(_ failed: inout Bool, outDir: URL) async {
        guard let permissions = windows.first(where: { $0.name == "permissions-1117-list" })?.window,
              let settings = windows.first(where: { $0.name == "settings-1117-general" })?.window else { return }
        // Clicks need a window in the window list. Far off every display, and never
        // key: nothing shows and no focus moves.
        for window in [permissions, settings] {
            window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            window.orderFrontRegardless()
        }
        try? await Task.sleep(nanoseconds: 500_000_000)

        let closed = fit(permissions)
        // The first row (Calendar, allowed, so it has no action to hit) sits at the top
        // of the card, which ends 16 pt above the panel's bottom.
        let scroll = permissions.contentView.flatMap(outerScroll)?.contentView.bounds.height ?? 0
        let rowY = closed - SurfaceLayout.panelBottomPadding - scroll + 18
        var open = closed
        for (name, x) in [("title", CGFloat(90)), ("middle", 320), ("status", 545), ("chevron", 598)] as [(String, CGFloat)] {
            await click(permissions, x: x, fromTop: rowY)
            open = fit(permissions)
            let opened = open > closed
            await click(permissions, x: x, fromTop: rowY)
            let reclosed = abs(fit(permissions) - closed) < 0.5
            print("click row \(name): opens \(opened) (\(Int(closed)) to \(Int(open)) pt), closes \(reclosed)")
            if !opened || !reclosed { failed = true; print("  FAILED: row click on \(name)") }
        }

        // Each section's left edge: 12 pt inset, measured from the render.
        let before = settings.contentView.flatMap { $0.bitmapImageRepForCachingDisplay(in: $0.bounds) }
        if let view = settings.contentView, let before {
            view.cacheDisplay(in: view.bounds, to: before)
            let scrollHeight = view.subviewsScrollHeight
            let switcherY = view.bounds.height - SurfaceLayout.panelBottomPadding - scrollHeight - 12 - 13
            await click(settings, x: 171, fromTop: switcherY)
            let height = fit(settings)
            if let after = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: after)
                let changed = before.tiffRepresentation != after.tiffRepresentation
                try? after.representation(using: .png, properties: [:])?
                    .write(to: outDir.appendingPathComponent("settings-after-clicking-voice.png"))
                print("click Voice section: the page changed \(changed), height stays \(Int(height)) pt")
                if !changed { failed = true; print("  FAILED: section click") }
            }
        }

        // Back to the first section, then what an assistive client reads and presses.
        await click(settings, x: 58, fromTop: (settings.contentView?.bounds.height ?? 0)
                    - SurfaceLayout.panelBottomPadding - (settings.contentView?.subviewsScrollHeight ?? 0) - 12 - 13)
        await accessibilityChecks(&failed, permissions: permissions, settings: settings)
    }

    // MARK: What an assistive client finds

    private static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }

    private struct Node {
        let element: AXUIElement
        let role: String
        let label: String
        let value: String
        let hint: String
    }

    private static func walk(_ element: AXUIElement, depth: Int = 0) -> [Node] {
        guard depth < 40 else { return [] }
        let role = attribute(element, kAXRoleAttribute) as? String ?? ""
        let description = attribute(element, kAXDescriptionAttribute) as? String ?? ""
        let title = attribute(element, kAXTitleAttribute) as? String ?? ""
        let raw = attribute(element, kAXValueAttribute)
        let value = (raw as? String) ?? (raw as? NSNumber)?.stringValue ?? ""
        let selected = (attribute(element, kAXSelectedAttribute) as? NSNumber)?.boolValue == true
        let help = attribute(element, kAXHelpAttribute) as? String ?? ""
        let hint = selected ? "SELECTED " + help : help
        var nodes = [Node(element: element, role: role, label: description.isEmpty ? title : description, value: value, hint: hint)]
        for child in attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? [] {
            nodes += walk(child, depth: depth + 1)
        }
        return nodes
    }

    private static func dump(_ title: String, _ nodes: [Node]) {
        print("--- \(title)")
        for node in nodes where ["AXButton", "AXCheckBox", "AXSwitch", "AXStaticText", "AXHeading"].contains(node.role) {
            print("  \(node.role) \"\(node.label)\"\(node.value.isEmpty ? "" : " = \(node.value)")"
                  + (node.hint.isEmpty ? "" : " | \(node.hint)"))
        }
    }

    private static func accessibilityChecks(_ failed: inout Bool, permissions: NSWindow, settings: NSWindow) async {
        let app = AXUIElementCreateApplication(getpid())
        // Asking is what makes SwiftUI build the tree.
        func windowsNow() -> [AXUIElement] { attribute(app, kAXWindowsAttribute) as? [AXUIElement] ?? [] }
        _ = windowsNow()
        try? await Task.sleep(nanoseconds: 800_000_000)
        let all = windowsNow()
        print("accessibility: \(all.count) windows")
        func page(containing text: String) -> AXUIElement? {
            all.first { window in walk(window).contains { ($0.label + $0.value).contains(text) } }
        }
        guard let permissionsAX = page(containing: "Notifications"), let settingsAX = page(containing: "Full access") else {
            failed = true
            print("  FAILED: pages not found in the tree")
            return
        }
        dump("Permissions, in order", walk(permissionsAX))

        func press(_ window: AXUIElement, role: String = "AXButton", _ label: String) -> Bool {
            guard let node = walk(window).first(where: { $0.role == role && $0.label == label }) else { return false }
            return AXUIElementPerformAction(node.element, kAXPressAction as CFString) == .success
        }
        let closed = fit(permissions)
        let pressedRow = press(permissionsAX, "Calendar")
        try? await Task.sleep(nanoseconds: 500_000_000)
        let open = fit(permissions)
        let explained = walk(permissionsAX).contains { ($0.label + $0.value).contains("for your day plan") }
        print("press Calendar row: \(pressedRow), panel \(Int(closed)) to \(Int(open)) pt, its explanation in the tree: \(explained)")
        if !pressedRow || !explained || open <= closed { failed = true; print("  FAILED: row press") }
        dump("Permissions with Calendar open", walk(permissionsAX))

        let pressedSection = press(settingsAX, "Voice")
        try? await Task.sleep(nanoseconds: 500_000_000)
        let voice = walk(settingsAX)
        let voiceShown = voice.contains { ($0.label + $0.value).contains("Read answers aloud") }
        let generalGone = !voice.contains { ($0.label + $0.value).contains("Learn my routines") }
        print("press Voice section: \(pressedSection), Voice rows in the tree: \(voiceShown), General rows gone: \(generalGone)")
        if !pressedSection || !voiceShown || !generalGone { failed = true; print("  FAILED: section press") }
        dump("Settings, Voice", voice)
    }
}
#endif

#if WISP_NOTCH_QA
private extension NSView {
    /// The height of the outermost scroll view inside this view.
    var subviewsScrollHeight: CGFloat {
        if let scroll = self as? NSScrollView { return scroll.contentView.bounds.height }
        for sub in subviews {
            let height = sub.subviewsScrollHeight
            if height > 0 { return height }
        }
        return 0
    }
}
#endif
