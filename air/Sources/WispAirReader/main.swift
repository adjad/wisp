import Cocoa

// Headless menu-bar agent (LSUIElement — no Dock icon, no windows). Its only
// job is to hold the three TCC grants that a Python process cannot, and shuttle
// data between macOS and air_periodic.py on 127.0.0.1:8767.
//
// It stays a real .app, and a real NSApplication run loop, for two reasons:
// TCC attributes grants to a signed bundle, and the readers are Timer-driven,
// which needs a run loop.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let mail = MailReader()
    private let messages = MessagesReader()
    private let reminders = RemindersWriter()
    private var statusItem: NSStatusItem?

    func applicationDidFinishLaunching(_ notification: Notification) {
        if Config.apiKey.isEmpty {
            NSLog("[WispAirReader] WARNING: ~/.wispair/api_key not found — every request will 401. Start air_periodic.py first.")
        }
        setUpMenu()

        // Reminders first: it triggers the EventKit permission prompt, and
        // asking for it while the user is still at the keyboard (rather than
        // 30 seconds later, mid-drain) is the difference between a prompt they
        // answer and one they never see.
        reminders.start()
        mail.start()
        messages.start()
        NSLog("[WispAirReader] started")
    }

    // A menu-bar item, not a pure background daemon: it gives the user a
    // visible way to confirm the reader is alive, force a read, and quit it —
    // otherwise the only evidence it runs at all is a permission prompt.
    private func setUpMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "◇"
        item.button?.toolTip = "Wisp Air Reader"

        let menu = NSMenu()
        menu.addItem(withTitle: "Wisp Air Reader", action: nil, keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Sync Now",
                                action: #selector(syncNow), keyEquivalent: "r"))
        menu.addItem(NSMenuItem(title: "Write Pending Reminders",
                                action: #selector(drainNow), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"))
        for i in menu.items where i.action != nil { i.target = self }
        item.menu = menu
        statusItem = item
    }

    @objc private func syncNow() {
        mail.sync()
        messages.sync()
    }

    @objc private func drainNow() { reminders.drain() }

    @objc private func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // LSUIElement at runtime too
app.run()
