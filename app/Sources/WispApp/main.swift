import AppKit

#if WISP_SETTINGS_QA
import SwiftUI

// This binary is never packaged as Wisp.app. It renders the real Settings view
// without installing AppDelegate, reserving 8765, or starting background work.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let loader = SettingsLoader()
    let view = SettingsView(loader: loader)
        .defaultAppStorage(SettingsQAEnvironment.defaults)
    let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 1100, height: 850),
                          styleMask: [.titled, .closable, .resizable],
                          backing: .buffered, defer: false)
    window.title = "Wisp Settings QA"
    window.contentView = NSHostingView(rootView: view)
    window.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)
    Task {
        await SettingsQADriver.run(loader: loader, window: window)
        app.terminate(nil)
    }
    app.run()
}
#elseif WISP_CHAT_QA

// Renders the Chat window against a scripted fixture and writes screenshots.
// Installs no AppDelegate and never contacts the Wisp service.
MainActor.assumeIsolated {
    ChatQADriver.run()
}
#else

// Program starts on the main thread; enter the main actor to build the app.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    // A regular app: a Dock icon, an app menu with Quit, and the Chat window as its
    // main window. The notch and the menu-bar icon stay as quick ways in.
    app.setActivationPolicy(.regular)
    app.run()
}
#endif
