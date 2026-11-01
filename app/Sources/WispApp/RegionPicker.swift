import AppKit

/// "Look" at part of the screen: a dimmed overlay on every display; the person
/// drags a rectangle (Esc or a click cancels). Returns the rectangle in global
/// display coordinates (ScreenLook.displayRect), after the overlay is gone so it
/// is never in the capture. The screen is not captured here; nothing is drawn
/// from the screen's pixels.
@MainActor
final class RegionPicker {
    private static var active: RegionPicker?

    private var windows: [NSWindow] = []
    private var completion: ((CGRect?) -> Void)?
    private var keyMonitor: Any?

    /// Shows the overlay; `completion` gets the region, or nil when cancelled.
    /// A second call while one is up cancels the first.
    static func pick(_ completion: @escaping (CGRect?) -> Void) {
        active?.finish(nil)
        let picker = RegionPicker()
        active = picker
        picker.completion = completion
        picker.show()
    }

    private func show() {
        for screen in NSScreen.screens {
            let window = RegionPickerWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
            window.level = .screenSaver
            window.isOpaque = false
            window.backgroundColor = .clear
            window.ignoresMouseEvents = false
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
            window.setFrame(screen.frame, display: false)
            let view = RegionPickerView(frame: CGRect(origin: .zero, size: screen.frame.size))
            view.onFinish = { [weak self, weak window] local in
                guard let self else { return }
                guard let local, let window else { self.finish(nil); return }
                let global = window.convertToScreen(local)
                let primaryHeight = NSScreen.screens.first?.frame.maxY ?? screen.frame.maxY
                self.finish(ScreenLook.displayRect(fromAppKit: global, primaryHeight: primaryHeight))
            }
            window.contentView = view
            window.makeKeyAndOrderFront(nil)
            windows.append(window)
        }
        NSApp.activate(ignoringOtherApps: true)
        NSCursor.crosshair.push()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            if event.keyCode == 53 { self?.finish(nil); return nil }   // Esc
            return event
        }
    }

    private func finish(_ rect: CGRect?) {
        guard let completion else { return }
        self.completion = nil
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
        NSCursor.pop()
        for window in windows { window.orderOut(nil) }
        windows = []
        if RegionPicker.active === self { RegionPicker.active = nil }
        // Let the window server drop the overlay before anything is captured.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { completion(rect) }
    }
}

/// Borderless windows can't become key by default; this one must, for Esc.
private final class RegionPickerWindow: NSWindow {
    override var canBecomeKey: Bool { true }
}

private final class RegionPickerView: NSView {
    var onFinish: ((CGRect?) -> Void)?
    private var start: CGPoint?
    private var current: CGPoint?

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        start = convert(event.locationInWindow, from: nil)
        current = start
        needsDisplay = true
    }

    override func mouseDragged(with event: NSEvent) {
        current = convert(event.locationInWindow, from: nil)
        needsDisplay = true
    }

    override func mouseUp(with event: NSEvent) {
        let end = convert(event.locationInWindow, from: nil)
        let region = start.flatMap { ScreenLook.region(from: $0, to: end) }
        start = nil; current = nil
        onFinish?(region)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.withAlphaComponent(0.25).setFill()
        bounds.fill()
        if let start, let current {
            let rect = CGRect(x: min(start.x, current.x), y: min(start.y, current.y),
                              width: abs(start.x - current.x), height: abs(start.y - current.y))
            NSColor.clear.setFill()
            rect.fill(using: .copy)
            NSColor.white.withAlphaComponent(0.9).setStroke()
            let path = NSBezierPath(rect: rect.insetBy(dx: 0.5, dy: 0.5))
            path.lineWidth = 1
            path.stroke()
        } else {
            let hint = "Drag over what Wisp should look at. Esc cancels." as NSString
            let attributes: [NSAttributedString.Key: Any] = [
                .font: NSFont.systemFont(ofSize: 15, weight: .medium),
                .foregroundColor: NSColor.white]
            let size = hint.size(withAttributes: attributes)
            hint.draw(at: CGPoint(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2),
                      withAttributes: attributes)
        }
    }
}
