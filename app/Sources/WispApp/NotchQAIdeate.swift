#if WISP_NOTCH_QA
import SwiftUI
import AppKit

// Ideate, the tab strip with wildcard tabs, and quick settings' Tabs row, rendered
// to 2x PNGs for docs/ideate/mockups. The Ideate tab draws from
// IdeateModel.qaFixture (invented data, no networking). The strip is the real
// OverlayView's own header, mounted once per choice of tabs and cropped; the
// Ideate and Settings pages sit under that crop, so every page shows the strip
// the way the notch draws it.
//
//   WISP_NOTCH_QA_IDEATE=1 WISP_NOTCH_QA_OUT=/some/dir <qa binary>
// Mounting OverlayView refreshes against the service: run it with network
// denied (docs/ideate/mockups/README.md has the recipe).
@MainActor
enum NotchQAIdeate {
    private static var kept: [AnyObject] = []
    private static let screen = NotchQAPages.layout(1512, 982)

    /// The Ideate pages: every kind empty, ui and debug through their states,
    /// and the three other states once, on general.
    static let ideatePages: [(String, IdeateKind, IdeateQAState)] = {
        var pages: [(String, IdeateKind, IdeateQAState)] = IdeateKind.allCases.map { ("ideate-\($0.rawValue)-empty", $0, .empty) }
        for kind in [IdeateKind.ui, .debug] {
            for (name, state) in [("running", IdeateQAState.running), ("approval", .approval), ("done", .done)] {
                pages.append(("ideate-\(kind.rawValue)-\(name)", kind, state))
            }
        }
        pages.append(("ideate-general-failed", .general, .failed))
        pages.append(("ideate-general-unavailable", .general, .unavailable))
        pages.append(("ideate-general-model-swap", .general, .modelSwap))
        return pages
    }()

    static func run(outDir: URL) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            await render(outDir: outDir)
            // Nothing the renders chose is left in this binary's own defaults.
            UserDefaults.standard.removePersistentDomain(forName: ProcessInfo.processInfo.processName)
            app.terminate(nil)
        }
        app.run()
    }

    private static func choose(_ tabs: [NotchTab], kind: IdeateKind = .general) {
        WildcardTabs.set(tabs)
        HarborPersonaStore.setIdeateKind(kind)
    }

    private static func render(outDir: URL) async {
        // The tab strip, three ways.
        var stripImages: [String: (NSImage, CGFloat)] = [:]
        for (name, tabs, selected) in [
            ("tabs-ask-brief-day-ideate-study", [NotchTab.ideate, .study], NotchTab.ideate),
            ("tabs-ask-brief-day-meet-ideate", [NotchTab.meet, .ideate], NotchTab.ideate),
            ("tabs-ask-brief-day-only", [], NotchTab.ask),
        ] as [(String, [NotchTab], NotchTab)] {
            choose(tabs)
            guard let header = await headerImage(selected: selected) else { print("\(name): header NOT measured"); continue }
            stripImages[name] = header
            write(framed(Image(nsImage: header.0), height: header.1 + 18), to: outDir, name: name, width: 640)
        }
        guard let ideateHeader = stripImages["tabs-ask-brief-day-ideate-study"] else { return }

        // The Ideate tab. Its kind comes from the fixture, so the saved choice stays put.
        choose([.ideate, .study])
        let ideateRoom = screen.pageContentCap(headerHeight: ideateHeader.1)
        for (name, kind, state) in ideatePages {
            let model = IdeateModel.qaFixture(kind: kind, state: state)
            kept.append(model)
            let view = panel(header: ideateHeader) { NotchIdeateView(ideate: model, cap: ideateRoom) }
            await write(view, to: outDir, name: name, width: 640, settle: true)
        }

        // Quick settings, General, with Ideate and Study on.
        choose([.ideate, .study], kind: .ui)
        let google = NotchGoogleModel()
        let overlay = OverlayModel()
        overlay.qaShow(modelMemory: ModelMemory(models: [.init(id: "gemma-4-e4b", bytes: 11_200_000_000)]))
        let loader = SettingsLoader()
        loader.qaShow(fullAccess: true, briefTimes: WispClient.BriefTimes(morning: 10, evening: 20), saveWhenAway: true, saveMemory: true)
        loader.behaviorEnabled = true
        kept += [overlay, loader]
        let settings = panel(header: ideateHeader) {
            QuickSettingsView(model: overlay, settings: loader, refreshes: false, cap: ideateRoom, section: .general,
                              picking: nil, hours: UserHours(), choosingVoice: false, google: google)
        }
        await write(settings, to: outDir, name: "settings-tabs-row", width: 640, settle: true)
    }

    // MARK: Pieces

    /// The real OverlayView on `selected`, with the tabs already chosen in
    /// UserDefaults: its header, cropped, and the header's height.
    private static func headerImage(selected: NotchTab) async -> (NSImage, CGFloat)? {
        NotchQAPages.liveHeader = nil
        let model = OverlayModel()
        model.collapsed = false
        model.tab = selected
        let today = TodayModel(now: { Date() }, timeZoneProvider: { .current }, transport: { _ in throw URLError(.cannotConnectToHost) })
        let client = WispClient()
        let view = OverlayView(model: model, corpus: NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client))),
                               researchModel: ResearchModel(client: client), todayModel: today, settings: SettingsLoader(),
                               setup: SetupGuideModel(), onClose: {})
            .environment(\.colorScheme, .dark)
            .frame(width: 640)
            .background(Theme.surface)
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 640, height: 500), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        let host = NSHostingView(rootView: view)
        window.contentView = host
        kept += [model, today, window]
        for _ in 0..<50 where NotchQAPages.liveHeader == nil {
            host.layoutSubtreeIfNeeded()
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        try? await Task.sleep(nanoseconds: 600_000_000)
        guard let header = NotchQAPages.liveHeader else { return nil }
        window.setContentSize(host.fittingSize)
        host.layoutSubtreeIfNeeded()
        let inset = DisplayGeometry.shared.layout.contentTopInset
        let scale: CGFloat = 2
        guard let rep = bitmap(host, scale: scale), let full = rep.cgImage else { return nil }
        let crop = CGRect(x: 0, y: (inset - 10) * scale, width: 640 * scale, height: (header + 18) * scale)
        guard let cut = full.cropping(to: crop) else { return nil }
        return (NSImage(cgImage: cut, size: NSSize(width: 640, height: header + 18)), header)
    }

    /// The header image above a page, as OverlayView lays a panel out.
    private static func panel<Page: View>(header: (NSImage, CGFloat), @ViewBuilder page: () -> Page) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Image(nsImage: header.0).resizable().frame(width: 640, height: header.1 + 18)
            page().padding(.top, SurfaceLayout.tabTopPadding).padding(.horizontal, 22)
        }
        .padding(.bottom, SurfaceLayout.panelBottomPadding)
        .frame(width: 640, alignment: .topLeading)
        .background(Theme.surface)
        .environment(\.colorScheme, .dark)
    }

    private static func framed(_ image: Image, height: CGFloat) -> some View {
        image.resizable().frame(width: 640, height: height).background(Theme.surface)
    }

    private static func write<V: View>(_ view: V, to dir: URL, name: String, width: CGFloat) {
        let host = NSHostingView(rootView: view.environment(\.colorScheme, .dark))
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 200), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.contentView = host
        window.setContentSize(host.fittingSize)
        host.layoutSubtreeIfNeeded()
        save(host, dir: dir, name: name)
        kept.append(window)
    }

    /// Hosts the view, lets a CappedScroll learn its height, then saves it.
    private static func write<V: View>(_ view: V, to dir: URL, name: String, width: CGFloat, settle: Bool) async {
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 760), styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host
        kept.append(window)
        for _ in 0..<3 {
            try? await Task.sleep(nanoseconds: 350_000_000)
            window.setContentSize(host.fittingSize)
            host.layoutSubtreeIfNeeded()
        }
        save(host, dir: dir, name: name)
    }

    private static func bitmap(_ view: NSView, scale: CGFloat) -> NSBitmapImageRep? {
        let size = view.bounds.size
        guard size.width > 0, size.height > 0,
              let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return nil }
        rep.size = size
        view.cacheDisplay(in: view.bounds, to: rep)
        return rep
    }

    private static func save(_ view: NSView, dir: URL, name: String) {
        guard let rep = bitmap(view, scale: 2), let png = rep.representation(using: .png, properties: [:]) else {
            print("\(name): NOT written")
            return
        }
        try? png.write(to: dir.appendingPathComponent("\(name).png"))
        print("\(name): \(Int(view.bounds.width))x\(Int(view.bounds.height)) pt")
    }
}
#endif
