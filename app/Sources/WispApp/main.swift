import AppKit

// Program starts on the main thread; enter the main actor to build the app.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    // Menu-bar only: no Dock icon, no main window.
    app.setActivationPolicy(.accessory)
    app.run()
}
