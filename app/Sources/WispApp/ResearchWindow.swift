import AppKit
import SwiftUI

/// Owns the single reusable Wisp Research window.
///
/// A dedicated resizable window rather than the 640-point notch panel — see
/// docs/RESEARCH_TOOL_PLAN.md section 9 — because a multi-minute run's plan,
/// activity log, source list, and report all need real room, and the window
/// must survive the notch panel closing.
@MainActor
final class ResearchWindowController {
    private var window: NSWindow?
    private let model: ResearchModel

    init(model: ResearchModel) { self.model = model }

    /// Open the window and draft a plan for a brand-new research request.
    func open(prompt: String) {
        show()
        model.createPlan(prompt: prompt)
    }

    /// Reopen the window onto whatever job is already in progress — no new
    /// plan — for the compact notch chip's "reopen" action.
    func reopen() {
        show()
    }

    private func show() {
        if window == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 720),
                               styleMask: [.titled, .closable, .resizable, .miniaturizable],
                               backing: .buffered, defer: false)
            win.title = "Wisp Research"
            win.contentView = NSHostingView(rootView: ResearchView(model: model))
            win.center()
            win.minSize = NSSize(width: 720, height: 560)
            win.isReleasedWhenClosed = false
            window = win
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
