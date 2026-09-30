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
    private var revealGeneration: UInt64 = 0
    private(set) var isRevealing = false
    // Injectable for the isolated native fixture; production follows macOS.
    var reduceMotion: () -> Bool = { NSWorkspace.shared.accessibilityDisplayShouldReduceMotion }
    // Injectable so fixtures can place the panel on any display shape;
    // production reads the live, display-change-aware layout.
    var layoutProvider: () -> SurfaceLayout = { MainActor.assumeIsolated { DisplayGeometry.shared.layout } }
    private var layout: SurfaceLayout { layoutProvider() }

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
        level = layout.windowLevel
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
        mask.anchorPoint = CGPoint(x: 0.5, y: 1)
        mask.backgroundColor = NSColor.black.cgColor   // opaque; only alpha matters for a mask
        // Round the bottom two corners like the panel itself — a plain
        // rectangular mask reveals the rounded content through a hard-edged
        // rectangular window, which reads as a stark black rectangle outline
        // during the grow/shrink instead of matching the panel's own shape.
        mask.maskedCorners = layout.maskedCorners
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

    // MARK: - Frames

    /// The collapsed bar and its window: on a MacBook it covers the notch
    /// (real or virtual) plus a hairline of overhang; on a monitor it is a
    /// small capsule under the menu bar.
    private func barFrame() -> NSRect? { layout.barFrame }

    /// The frame for the given state. Collapsed → the bar. Expanded → the
    /// layout's width (must match the SwiftUI content's `.frame(width:)`, which
    /// reads the same value, or the window and content disagree and clip),
    /// centred, hanging from the notch or from just under the menu bar.
    private func frame(compact: Bool) -> NSRect? {
        if compact { return barFrame() }
        guard let host = host else { return nil }
        host.layoutSubtreeIfNeeded()
        let h = host.fittingSize.height
        guard h > 0 else { return nil }
        return layout.expandedFrame(contentHeight: h)
    }

    /// Re-fit after displays changed (attached, removed, rearranged, rescaled).
    /// Cancels any in-flight reveal so its stale geometry can't win.
    func applyDisplayChange() {
        let l = layout
        level = l.windowLevel
        revealMask?.maskedCorners = l.maskedCorners
        guard isVisible else { return }
        cancelReveal()
        if isCompact() {
            settleToBar()
        } else if let f = frame(compact: false) {
            setFrame(f, display: true)
            fillMask()
        }
    }

    // Instant grow/shrink to fit streaming content.
    func resizeToFit() {
        guard isVisible, !isRevealing, !isCompact(), let f = frame(compact: false) else { return }
        guard abs(f.height - frame.height) > 1 || abs(f.width - frame.width) > 1
              || abs(f.origin.x - frame.origin.x) > 1 else { return }
        setFrame(f, display: true)
        fillMask()
    }

    func present() {
        cancelReveal()
        layoutIfNeeded()
        if let f = barFrame() { setFrame(f, display: true) }
        fillMask()
        alphaValue = 1
        orderFrontRegardless()
    }

    // The mask's rounding follows the layout's bar and panel radii, so it
    // matches whichever shape is actually at rest at each end of the reveal.
    private var barCornerRadius: CGFloat { layout.barCornerRadius }
    private var panelCornerRadius: CGFloat { layout.panelCornerRadius }

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
        mask.position = CGPoint(x: frame.width / 2, y: frame.height)
        mask.cornerRadius = isCompact() ? barCornerRadius : panelCornerRadius
        if disableActions { CATransaction.commit() }
    }

    // MARK: - Calm, top-anchored reveal

    // A shared non-overshooting curve keeps size and corners together. The
    // top edge never moves, so content grows out of the notch without bounce.
    private static let revealCurve = CAMediaTimingFunction(controlPoints: 0.22, 0.75, 0.25, 1)

    private func cancelReveal() {
        revealGeneration &+= 1
        isRevealing = false
        revealMask?.removeAllAnimations()
    }

    override func orderOut(_ sender: Any?) {
        cancelReveal()
        super.orderOut(sender)
    }

    private func maskRects() -> (bar: CGRect, full: CGRect)? {
        guard let bar = barFrame(), frame.width > 0, frame.height > 0 else { return nil }
        return (CGRect(x: bar.origin.x - frame.origin.x, y: bar.origin.y - frame.origin.y,
                       width: bar.width, height: bar.height),
                CGRect(origin: .zero, size: frame.size))
    }

    private func reveal(opening: Bool, completion: @escaping () -> Void) {
        // Capture visible geometry before replacing the one animation group.
        let visible = isRevealing ? revealMask?.presentation() : nil
        let visibleBounds = visible?.bounds
        let visibleRadius = visible?.cornerRadius
        cancelReveal()
        if opening {
            guard let full = frame(compact: false) else { completion(); return }
            setFrame(full, display: true)
            makeKeyAndOrderFront(nil)
        }
        guard let mask = revealMask, let rects = maskRects() else { completion(); return }
        let startSize = visibleBounds?.size ?? (opening ? rects.bar.size : rects.full.size)
        let endSize = opening ? rects.full.size : rects.bar.size
        let startRadius = visibleRadius ?? (opening ? barCornerRadius : panelCornerRadius)
        let endRadius = opening ? panelCornerRadius : barCornerRadius
        let token = revealGeneration
        isRevealing = true

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        mask.position = CGPoint(x: rects.full.midX, y: rects.full.maxY)
        mask.bounds = CGRect(origin: .zero, size: endSize)
        mask.cornerRadius = endRadius
        CATransaction.commit()

        let finish = { [weak self] in
            guard let self, self.revealGeneration == token else { return }
            self.isRevealing = false
            completion()
            // Coalesce height changes received while opening. A close keeps
            // its small mask until the caller has swapped in the compact bar.
            if opening, self.revealGeneration == token { self.resizeToFit() }
        }
        guard !reduceMotion() else { finish(); return }

        let size = CABasicAnimation(keyPath: "bounds.size")
        size.fromValue = NSValue(size: startSize)
        size.toValue = NSValue(size: endSize)
        let radius = CABasicAnimation(keyPath: "cornerRadius")
        radius.fromValue = startRadius
        radius.toValue = endRadius
        let group = CAAnimationGroup()
        group.animations = [size, radius]
        group.duration = opening ? 0.38 : 0.30
        size.duration = group.duration
        radius.duration = group.duration
        group.timingFunction = Self.revealCurve
        CATransaction.begin()
        CATransaction.setCompletionBlock(finish)
        mask.add(group, forKey: "reveal")
        CATransaction.commit()
    }

    func dropOpen(_ completion: (() -> Void)? = nil) {
        reveal(opening: true) { completion?() }
    }

    func rollUp(_ completion: @escaping () -> Void) {
        guard isVisible else { completion(); return }
        reveal(opening: false, completion: completion)
    }

    func settleToBar() {
        cancelReveal()
        if let f = barFrame() { setFrame(f, display: true) }
        fillMask()
    }
}
