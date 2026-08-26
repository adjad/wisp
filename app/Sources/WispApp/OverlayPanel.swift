import AppKit
import SwiftUI
import QuartzCore

/// Content view that reports pointer enter/exit via a proper AppKit tracking
/// area — reliable regardless of key/active state and when the pointer leaves
/// the window upward into the menu-bar/notch region, unlike SwiftUI's
/// `.onHover`, which routinely drops the exit event inside a borderless
/// non-activating panel (leaving the auto-collapse timer never armed).
final class HoverView: NSView {
    var onEnter: () -> Void = {}
    var onExit: () -> Void = {}
    private var tracking: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                               owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseEntered(with event: NSEvent) { onEnter() }
    override func mouseExited(with event: NSEvent) { onExit() }
}

final class OverlayPanel: NSPanel {
    private var host: NSView?
    private var slide: NSView?
    // The content view. Its layer carries a MASK that grows/shrinks to reveal
    // or hide the content — the panel's content never itself moves or scales;
    // only how much of it is visible changes. This is what makes it read as
    // the physical notch stretching to accommodate the app, rather than a
    // separate panel sliding out from behind it.
    private var stage: NSView?
    private var revealMask: CALayer?

    init<Content: View>(@ViewBuilder content: () -> Content) {
        super.init(contentRect: NSRect(x: 0, y: 0, width: 640, height: 220),
                   styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        // Elevated level is load-bearing, not vanity: the bar must render INTO
        // the menu-bar strip (above the real menu bar's own level) to fuse with
        // the notch, and an accessory app's .normal-level window opens BEHIND
        // other apps' windows — tried once, looked completely broken. "Don't sit
        // on top of everything at all times" is handled by collapsing the panel
        // whenever it loses key focus (see AppDelegate), not by lowering it.
        level = Self.notchScreen() != nil ? .statusBar : .floating
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        animationBehavior = .none

        let hostView = NSHostingView(rootView: content())
        hostView.translatesAutoresizingMaskIntoConstraints = false
        self.host = hostView

        let slideView = NSView()
        slideView.translatesAutoresizingMaskIntoConstraints = false
        slideView.wantsLayer = true            // own layer so we can rasterize it
        self.slide = slideView
        slideView.addSubview(hostView)

        let container = HoverView()
        container.wantsLayer = true
        container.layer?.masksToBounds = true
        container.addSubview(slideView)
        container.onEnter = { [weak self] in self?.onMouseEnter() }
        container.onExit = { [weak self] in self?.onMouseExit() }
        self.stage = container

        let mask = CALayer()
        mask.backgroundColor = NSColor.black.cgColor   // opaque; only alpha matters for a mask
        // Round the bottom two corners like the panel itself — a plain
        // rectangular mask reveals the rounded content through a hard-edged
        // rectangular window, which reads as a stark black rectangle outline
        // during the grow/shrink instead of matching the panel's own shape.
        mask.maskedCorners = [.layerMinXMinYCorner, .layerMaxXMinYCorner]
        container.layer?.mask = mask
        self.revealMask = mask

        NSLayoutConstraint.activate([
            // host fully fills the slide view (slide view sizes to host height)
            hostView.topAnchor.constraint(equalTo: slideView.topAnchor),
            hostView.bottomAnchor.constraint(equalTo: slideView.bottomAnchor),
            hostView.leadingAnchor.constraint(equalTo: slideView.leadingAnchor),
            hostView.trailingAnchor.constraint(equalTo: slideView.trailingAnchor),
            // slide view pinned top/leading/trailing — NOT bottom, so it keeps
            // its full intrinsic height and the window bounds clip it.
            slideView.topAnchor.constraint(equalTo: container.topAnchor),
            slideView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            slideView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        contentView = container
    }

    // Pointer entered / left the panel's content (set by AppDelegate).
    var onMouseEnter: () -> Void = {}
    var onMouseExit: () -> Void = {}

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    var isCompact: () -> Bool = { false }

    // MARK: - Notch geometry

    static func notchScreen() -> NSScreen? {
        NSScreen.screens.first { $0.safeAreaInsets.top > 0 }
    }

    static func notchMetrics(for screen: NSScreen) -> (width: CGFloat, inset: CGFloat)? {
        guard screen.safeAreaInsets.top > 0 else { return nil }
        let left = screen.auxiliaryTopLeftArea?.width ?? 0
        let right = screen.auxiliaryTopRightArea?.width ?? 0
        let width = screen.frame.width - left - right
        guard width > 0, width < 500 else { return nil }
        return (width, screen.safeAreaInsets.top)
    }

    private func targetScreen() -> NSScreen? { Self.notchScreen() ?? NSScreen.main }

    // MARK: - Frames

    // Collapsed bar + its hover hitbox: hugs the notch tightly, both smaller
    // than the actual notch+wings dimensions so it's a deliberate target, not
    // an easy accidental brush. Shared with the notchBar view's own frame.
    // Tightened further after feedback that the 0.5 hitbox was too easy to
    // brush open by accident — 0.36 makes it a small, clearly intentional target.
    static let barScale: CGFloat = 0.36

    /// Always centered horizontally — on a notch screen, fused with the
    /// physical notch; on a plain screen, centered under the menu bar.
    private func barFrame() -> NSRect? {
        guard let screen = targetScreen() else { return nil }
        if let m = Self.notchMetrics(for: screen) {
            let w = ((m.width + 28) * Self.barScale).rounded()
            let h = ((m.inset + 8) * Self.barScale).rounded()
            return NSRect(x: (screen.frame.midX - w / 2).rounded(),
                          y: (screen.frame.maxY - h).rounded(), width: w, height: h)
        }
        let w: CGFloat = 160, h: CGFloat = 40
        return NSRect(x: (screen.visibleFrame.midX - w / 2).rounded(),
                      y: (screen.visibleFrame.maxY - h).rounded(), width: w, height: h)
    }

    /// The frame for the given state. Collapsed → the bar. Expanded → 640
    /// wide (must match OverlayView's expandedPanel .frame(width:) exactly,
    /// or the native window frame and the SwiftUI content disagree on size,
    /// clipping/misaligning the panel), always centered — flush under the
    /// notch on a notch screen, or centered under the menu bar otherwise.
    private func frame(compact: Bool) -> NSRect? {
        if compact { return barFrame() }
        guard let host = host, let screen = targetScreen() else { return nil }
        host.layoutSubtreeIfNeeded()
        let h = host.fittingSize.height
        guard h > 0 else { return nil }
        let w: CGFloat = 640
        let f = Self.notchMetrics(for: screen) != nil ? screen.frame : screen.visibleFrame
        return NSRect(x: (f.midX - w / 2).rounded(), y: (f.maxY - h).rounded(),
                      width: w, height: h)
    }

    // Instant grow/shrink to fit streaming content.
    func resizeToFit() {
        guard isVisible, !isCompact(), let f = frame(compact: false) else { return }
        guard abs(f.height - frame.height) > 1 || abs(f.width - frame.width) > 1
              || abs(f.origin.x - frame.origin.x) > 1 else { return }
        setFrame(f, display: true)
        fillMask()
    }

    func present() {
        layoutIfNeeded()
        if let f = barFrame() { setFrame(f, display: true) }
        fillMask()
        alphaValue = 1
        orderFrontRegardless()
    }

    // Matches notchBar's own clip radius (14) and Theme.notchCorners' bottom
    // radius (28) respectively, so the mask's rounding matches whichever
    // shape is actually at rest at each end of the reveal.
    private static let barCornerRadius: CGFloat = 14
    private static let panelCornerRadius: CGFloat = 28

    /// Set the mask to exactly fill the CURRENT window — i.e. "fully open,
    /// nothing clipped" — with the corner radius matching whichever shape is
    /// at rest right now. Used whenever there's no in-flight reveal animation
    /// (initial launch, settling into the bar, live-resize while streaming).
    private func fillMask(disableActions: Bool = true) {
        guard let mask = revealMask else { return }
        if disableActions {
            CATransaction.begin(); CATransaction.setDisableActions(true)
        }
        mask.bounds = CGRect(origin: .zero, size: frame.size)
        mask.position = CGPoint(x: frame.width / 2, y: frame.height / 2)
        mask.cornerRadius = isCompact() ? Self.barCornerRadius : Self.panelCornerRadius
        if disableActions { CATransaction.commit() }
    }

    // MARK: - Notch-stretch reveal (120 fps)

    // easeOutExpo out, easeInExpo in — used only for the (non-bouncy)
    // cornerRadius animation; size/position use a real spring, below.
    private static let dropCurve = CAMediaTimingFunction(controlPoints: 0.16, 1, 0.3, 1)
    private static let rollCurve = CAMediaTimingFunction(controlPoints: 0.7, 0, 0.84, 0)

    // A cubic-bezier ease can't overshoot its own target, so it can look
    // smooth but never "bubbly". A real spring can: it's a physical
    // simulation, so it naturally overshoots and settles back, which is what
    // actually reads as bouncy. Lower damping = more oscillation.
    private static let openSpring = (stiffness: 170.0, damping: 15.0)
    private static let closeSpring = (stiffness: 200.0, damping: 18.0)

    private static func spring(keyPath: String, _ tuning: (stiffness: Double, damping: Double)) -> CASpringAnimation {
        let a = CASpringAnimation(keyPath: keyPath)
        a.mass = 1
        a.stiffness = tuning.stiffness
        a.damping = tuning.damping
        a.initialVelocity = 0
        a.duration = a.settlingDuration
        return a
    }

    /// Cache the content as a flat bitmap for the duration of a reveal. The
    /// content is STATIC while the mask grows/shrinks, so rasterizing it means
    /// each animation frame is a cheap bitmap-through-mask composite instead of
    /// re-rendering all the SwiftUI text/subviews through the mask every frame
    /// — the main source of the reveal's frame drops. Off at rest so live
    /// (streaming) content stays crisp and updates normally.
    private func setContentRasterized(_ on: Bool) {
        guard let layer = slide?.layer else { return }
        layer.rasterizationScale = on ? backingScaleFactor : 1
        layer.shouldRasterize = on
    }

    /// The bar's rect and the full panel's rect, both expressed in `stage`'s
    /// own coordinate space (i.e. relative to the window's own origin) via
    /// plain arithmetic — this panel is `.borderless`, so its frame IS its
    /// content rect with zero inset, meaning "screen minus window origin" is
    /// directly the window-local point. No NSView/NSWindow coordinate-
    /// conversion API involved, and no cross-view geometry to get subtly wrong.
    private func maskRects() -> (bar: CGRect, full: CGRect)? {
        guard let bar = barFrame(), frame.width > 0, frame.height > 0 else { return nil }
        let barInStage = CGRect(x: bar.origin.x - frame.origin.x, y: bar.origin.y - frame.origin.y,
                                width: bar.width, height: bar.height)
        let full = CGRect(origin: .zero, size: frame.size)
        return (barInStage, full)
    }

    /// The notch physically stretches to reveal the app: the content itself
    /// never moves — it's laid out at full size from the start — only the
    /// MASK that reveals it grows, from exactly the bar's rectangle up to the
    /// full panel. Call AFTER the SwiftUI root switched to the expanded
    /// content (so `frame` reflects the full size once resized below).
    func dropOpen(_ completion: (() -> Void)? = nil) {
        guard let full = frame(compact: false) else { orderFrontRegardless(); completion?(); return }
        setFrame(full, display: true)
        makeKeyAndOrderFront(nil)
        guard let mask = revealMask, let rects = maskRects() else { fillMask(); completion?(); return }

        let barCenter = CGPoint(x: rects.bar.midX, y: rects.bar.midY)
        let fullCenter = CGPoint(x: rects.full.midX, y: rects.full.midY)

        CATransaction.begin(); CATransaction.setDisableActions(true)
        mask.bounds = CGRect(origin: .zero, size: rects.bar.size)
        mask.position = barCenter
        mask.cornerRadius = Self.barCornerRadius
        CATransaction.commit()

        setContentRasterized(true)
        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            self?.setContentRasterized(false)
            completion?()
        }
        mask.bounds = CGRect(origin: .zero, size: rects.full.size)
        mask.position = fullCenter
        mask.cornerRadius = Self.panelCornerRadius

        let size = Self.spring(keyPath: "bounds.size", Self.openSpring)
        size.fromValue = NSValue(size: rects.bar.size)
        size.toValue = NSValue(size: rects.full.size)
        let move = Self.spring(keyPath: "position", Self.openSpring)
        move.fromValue = NSValue(point: barCenter)
        move.toValue = NSValue(point: fullCenter)
        let radius = CABasicAnimation(keyPath: "cornerRadius")
        radius.fromValue = Self.barCornerRadius
        radius.toValue = Self.panelCornerRadius
        radius.duration = size.settlingDuration
        radius.timingFunction = Self.dropCurve

        mask.add(size, forKey: "reveal.size")
        mask.add(move, forKey: "reveal.position")
        mask.add(radius, forKey: "reveal.radius")
        CATransaction.commit()
    }

    /// Reverse: the reveal mask shrinks back down to the bar's exact
    /// rectangle — the notch retracting — rather than the content sliding or
    /// fading away. Hands off so the caller swaps to the bar and calls
    /// `settleToBar`.
    func rollUp(_ completion: @escaping () -> Void) {
        guard isVisible, let mask = revealMask, let rects = maskRects() else {
            completion(); return
        }
        let barCenter = CGPoint(x: rects.bar.midX, y: rects.bar.midY)
        let fullCenter = CGPoint(x: rects.full.midX, y: rects.full.midY)

        setContentRasterized(true)
        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            self?.setContentRasterized(false)
            completion()
        }
        mask.bounds = CGRect(origin: .zero, size: rects.bar.size)
        mask.position = barCenter
        mask.cornerRadius = Self.barCornerRadius

        let size = Self.spring(keyPath: "bounds.size", Self.closeSpring)
        size.fromValue = NSValue(size: rects.full.size)
        size.toValue = NSValue(size: rects.bar.size)
        let move = Self.spring(keyPath: "position", Self.closeSpring)
        move.fromValue = NSValue(point: fullCenter)
        move.toValue = NSValue(point: barCenter)
        let radius = CABasicAnimation(keyPath: "cornerRadius")
        radius.fromValue = Self.panelCornerRadius
        radius.toValue = Self.barCornerRadius
        radius.duration = size.settlingDuration
        radius.timingFunction = Self.rollCurve

        mask.add(size, forKey: "retract.size")
        mask.add(move, forKey: "retract.position")
        mask.add(radius, forKey: "retract.radius")
        CATransaction.commit()
    }

    /// After roll-up: resize the window down to the bar frame and reset the
    /// mask to fill it exactly, so the bar displays normally and the next
    /// dropOpen starts from a clean, fully-open-for-its-own-size mask.
    func settleToBar() {
        if let f = barFrame() { setFrame(f, display: true) }
        fillMask()
    }
}
