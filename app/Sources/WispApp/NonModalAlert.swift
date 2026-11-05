import AppKit

/// An alert that leaves every other Wisp window working. `NSAlert.runModal` is
/// app-modal: the notch stops responding until it is answered, and when Wisp isn't
/// the active app (the notch and the menu bar never activate it) macOS also bounces
/// the Dock icon and can open the alert behind other apps. This shows the same alert
/// as an ordinary window, with Wisp brought forward first, one level above the notch.
@MainActor
final class NonModalAlert: NSObject {
    private let alert = NSAlert()
    private let onClose: () -> Void
    /// Replaced by the checks, so running them never takes focus from anyone.
    static var activateApp: () -> Void = { NSApp.activate(ignoringOtherApps: true) }

    var window: NSWindow { alert.window }
    var closeButton: NSButton { alert.buttons[0] }

    init(title: String, text: String, style: NSAlert.Style, onClose: @escaping () -> Void) {
        self.onClose = onClose
        super.init()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = style
        let button = alert.addButton(withTitle: "OK")
        // The alert's own button action ends a modal session; here there is none.
        button.target = self
        button.action = #selector(close)
        alert.layout()
        window.level = NSWindow.Level(DisplayGeometry.shared.layout.windowLevel.rawValue + 1)
        // Comes to the Space the person is on, a full-screen one too, without moving them.
        window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
    }

    func show() {
        window.center()
        bringForward()
    }

    func bringForward() {
        Self.activateApp()
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func close() {
        guard window.isVisible else { return }
        window.orderOut(nil)
        // onClose drops the last reference to this, with the button still mid-click.
        DispatchQueue.main.async { [alert] in _ = alert }
        onClose()
    }
}
