#if WISP_NOTCH_QA
import AppKit
import SwiftUI

// The onboarding window's steps, rendered to PNG for review, plus its two
// drawings in light and dark. Every request is refused in-process (the same
// Refuse protocol as the setup card's scenes), Getting to know you starts
// already loaded, and the Google step shows a fixture without asking the
// service. Permission rows show what macOS says about this QA binary.
//
//   swift build -Xswiftc -DWISP_NOTCH_QA
//   WISP_NOTCH_QA_ONBOARDING=1 WISP_QA_OFFSCREEN=1 WISP_NOTCH_QA_OUT=/some/dir .build/debug/WispApp
@MainActor
enum NotchQAOnboarding {
    private static var windows: [(String, NSWindow)] = []

    static func run(outDir: URL, offscreen: Bool) {
        URLProtocol.registerClass(NotchQASetup.Refuse.self)
        let app = NSApplication.shared
        app.setActivationPolicy(offscreen ? .accessory : .regular)

        let downloading = NotchQASetup.model(NotchQASetup.status(), downloads: [
            NotchQASetup.task("downloading", done: 2.4e9, speed: 18e6)])
        let ready = NotchQASetup.model(NotchQASetup.status(ready: true, installed: true))

        func knowYou() -> KnowYouModel {
            let model = KnowYouModel(client: WispClient(deliverySession: refusing()))
            model.profile.name = "Sam"
            model.loaded = true
            return model
        }
        func google(_ accounts: [[String: Any]], message: String = "", authorizing: Bool = false) -> GoogleAccountModel {
            let model = GoogleAccountModel()
            model.status = GoogleStatus.parse(["accounts": accounts, "client_source": "builtin"])
            model.loaded = true
            model.message = message
            model.authorizing = authorizing
            return model
        }
        let sam: [String: Any] = [
            "email": "sam@example.com", "state": "connected", "message": "Connected", "calendar": true,
            "workspace": true, "workspace_requested": true,
            "workspace_declined": ["https://www.googleapis.com/auth/drive.readonly",
                                   "https://www.googleapis.com/auth/chat.spaces.readonly"],
        ]
        let gmailMissing = "Wisp could not connect Gmail: Gmail read access wasn't granted — on Google's consent screen, "
            + "tick the box to view your email messages, then try again"

        let scenes: [(String, OnboardingStep, SetupGuideModel, GoogleAccountModel, CGFloat)] = [
            ("1-model", .brain, downloading, google([]), 600),
            ("2-permissions", .permissions, downloading, google([]), 860),
            ("3-google", .google, downloading, google([]), 600),
            ("3-google-waiting", .google, downloading, google([], authorizing: true), 600),
            ("3-google-connected", .google, downloading, google([sam]), 640),
            ("3-google-gmail-missing", .google, downloading, google([], message: gmailMissing), 600),
            ("4-about-you", .knowYou, downloading, google([]), 600),
            ("5-how-to-use", .tour, ready, google([sam]), 1260),
        ]
        // The You step's scenes are drawn one at a time below: the step reads what
        // is chosen when it appears, so each scene sets that first.
        // Tall windows, so the step's whole scroll content is in the image.
        let youScenes: [(String, () -> Void, Bool, CGFloat)] = [
            ("you-personas", { choose(nil) }, false, 940),
            ("you-builder-subtypes", { choose(.builderUI) }, false, 1240),
            ("you-change-list", { choose(.builderLearn) }, true, 1240),
        ]
        for (name, step, setup, account, height) in scenes {
            let view = FirstRunSetupView(setup: setup, knowYou: knowYou(), google: account, initialStep: step,
                                         openDetailedSetup: {}, finish: { _ in }, qaFixture: true)
                .frame(height: height)
            add("onboarding-\(name)", AnyView(view), size: CGSize(width: FirstRunSetup.width, height: height),
                scheme: .dark, offscreen: offscreen)
        }
        // The drawings on their own, in both appearances.
        for scheme in [ColorScheme.light, .dark] {
            let suffix = scheme == .light ? "light" : "dark"
            for style in [SurfaceLayout.Style.notch, .menuBar] {
                let drawing = VStack(alignment: .leading, spacing: 16) {
                    NotchLocationIllustration(style: style)
                    OpenNotchIllustration(tabs: FirstRunSetupView.tabLines)
                }
                .padding(24)
                .frame(width: 584)
                .background(scheme == .light ? Color(white: 0.96) : Color(white: 0.08))
                .environment(\.colorScheme, scheme)
                add("onboarding-drawings-\(style.rawValue)-\(suffix)", AnyView(drawing),
                    size: CGSize(width: 584, height: 330), scheme: scheme, offscreen: offscreen)
            }
        }
        if !offscreen { app.activate(ignoringOtherApps: true) }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            for (name, window) in windows {
                guard let view = window.contentView else { continue }
                view.layoutSubtreeIfNeeded()
                guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: outDir.appendingPathComponent("\(name).png"))
                print("\(name): \(Int(view.bounds.width))×\(Int(view.bounds.height)) pt")
            }
            for (name, prepare, openChange, height) in youScenes {
                prepare()
                let view = FirstRunSetupView(setup: downloading, knowYou: knowYou(), google: google([]), initialStep: .customize,
                                             openDetailedSetup: {}, finish: { _ in }, qaFixture: true)
                    .frame(height: height)
                add("onboarding-\(name)", AnyView(view), size: CGSize(width: FirstRunSetup.width, height: height),
                    scheme: .dark, offscreen: offscreen)
                guard let (_, window) = windows.last else { continue }
                if openChange {
                    // "Change" is a plain button; press it the way VoiceOver would.
                    window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
                    window.orderFrontRegardless()
                }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                if openChange {
                    // "Change" sits 825 pt from the top at this width (the builder scene's layout); a real click.
                    let point = NSPoint(x: 562, y: window.frame.height - 825)
                    for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                        if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                                          windowNumber: window.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1) {
                            window.sendEvent(event)
                        }
                        try? await Task.sleep(nanoseconds: 80_000_000)
                    }
                    try? await Task.sleep(nanoseconds: 800_000_000)
                }
                guard let view = window.contentView else { continue }
                view.layoutSubtreeIfNeeded()
                if let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: rep)
                    try? rep.representation(using: .png, properties: [:])?
                        .write(to: outDir.appendingPathComponent("onboarding-\(name).png"))
                    print("onboarding-\(name): \(Int(view.bounds.width))×\(Int(view.bounds.height)) pt")
                }
            }
            // Nothing the You scenes chose is left in this binary's own defaults.
            UserDefaults.standard.removePersistentDomain(forName: ProcessInfo.processInfo.processName)
            app.terminate(nil)
        }
        app.run()
    }

    /// The You step's saved choice: a persona and the tabs it suggests, or nothing.
    private static func choose(_ persona: HarborPersona?) {
        let defaults = UserDefaults.standard
        for key in [WildcardTabs.key, HarborPersonaStore.personaKey, HarborPersonaStore.ideateKindKey] { defaults.removeObject(forKey: key) }
        if let persona { HarborPersonaStore.choose(persona) }
    }

    private static func refusing() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NotchQASetup.Refuse.self]
        return URLSession(configuration: configuration)
    }

    private static func add(_ name: String, _ view: AnyView, size: CGSize, scheme: ColorScheme, offscreen: Bool) {
        let window = NSWindow(contentRect: NSRect(origin: CGPoint(x: 80, y: 80), size: size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: scheme == .light ? .aqua : .darkAqua)
        window.contentView = NSHostingView(rootView: view)
        window.setContentSize(size)
        if !offscreen { window.makeKeyAndOrderFront(nil) }
        windows.append((name, window))
    }
}
#endif
