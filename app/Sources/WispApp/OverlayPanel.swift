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
    // The caller's completion for the reveal in flight, and which way it runs.
    // A display change cancels the animation but still owes the caller its
    // completion (see applyDisplayChange); every other cancel drops it.
    private var pendingReveal: (opening: Bool, completion: () -> Void)?
    // A shrink waits for the content's own animation (a Brief row closing) to
    // finish before the window follows; see resizeToFit.
    private var pendingShrink: DispatchWorkItem?
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

        // The content keeps its own (ideal) height and sits at the top of a
        // host that never resizes. A host sized to the content resized inside
        // every SwiftUI animation, and SwiftUI then centred the animating
        // content in it: a Brief row opening dropped the whole panel by half
        // the change and slid it back up.
        let hostView = NSHostingView(rootView: content()
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxHeight: .infinity, alignment: .top))
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
        mask.contentsScale = NSScreen.main?.backingScaleFactor ?? 2
        container.layer?.mask = mask
        self.revealMask = mask

        NSLayoutConstraint.activate([
            // host fills the slide view's height and is centred across it at
            // the content's own width. Pinned leading and trailing, the host's
            // size constraints dragged the window to the content's width the
            // moment it changed, from the left edge, so leaving the two-column
            // Brief sat off-centre until the window settled. Centred, only
            // resizeToFit decides the window's width, and the panel (centred
            // under the notch, like the window) stays centred while it
            // narrows inside the wider window.
            hostView.topAnchor.constraint(equalTo: slideView.topAnchor),
            hostView.bottomAnchor.constraint(equalTo: slideView.bottomAnchor),
            hostView.centerXAnchor.constraint(equalTo: slideView.centerXAnchor),
            // slide view pinned top/leading/trailing — NOT bottom — at a fixed
            // height taller than any screen; the window bounds clip it, and
            // the window's height comes from the content's measured size.
            slideView.heightAnchor.constraint(equalToConstant: Self.hostHeight),
            slideView.topAnchor.constraint(equalTo: container.topAnchor),
            slideView.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            slideView.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        contentView = container
    }

    /// The host's fixed height: taller than any display, so content never
    /// reaches its bottom. Only the window's own bounds decide what shows.
    static let hostHeight: CGFloat = 8192

    // Pointer entered / left the panel's content (set by AppDelegate).
    var onMouseEnter: () -> Void = {}
    var onMouseExit: () -> Void = {}

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    var isCompact: () -> Bool = { false }

    // MARK: - Frames

    /// The collapsed bar and its window: on a notched MacBook it covers the
    /// notch plus a hairline of overhang; on a monitor it is a small capsule
    /// under the menu bar; on a notchless built-in display it is an undrawn
    /// seed under the menu bar that the card grows out of.
    private func barFrame() -> NSRect? { layout.barFrame }

    /// A collapsed bar that draws nothing (.menuBar, or a notch behind a
    /// full-screen app) must not take clicks from the menu bar or the app under
    /// it; hover is detected from the pointer position, and the menu-bar icon
    /// and ⌥Space open it. Everything else takes the mouse. The view draws the
    /// bar from the same `barIsVisible`, so the two cannot disagree for longer
    /// than one layout change takes to land.
    private func passMouseThrough(collapsed: Bool) {
        ignoresMouseEvents = Self.ignoresMouse(collapsed: collapsed, layout: layout)
    }

    /// The rule itself: only a collapsed bar that draws nothing lets clicks through.
    static func ignoresMouse(collapsed: Bool, layout: SurfaceLayout) -> Bool {
        collapsed && !layout.barIsVisible
    }

    /// The collapsed bar switched between drawn and hitbox (a full-screen app
    /// came to the front on its display, or left). Its frame is unchanged, so
    /// only whether the window takes the mouse follows; an open or close in
    /// flight carries on untouched, and sets this again when it settles.
    func applyPresentationChange() {
        passMouseThrough(collapsed: isCompact())
    }

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
    /// Cancels any in-flight reveal so its stale geometry can't win, then
    /// finishes it for the caller: the open or close it belonged to is waiting
    /// on that completion, and nothing else would ever end the step.
    func applyDisplayChange() {
        let l = layout
        level = l.windowLevel
        revealMask?.maskedCorners = l.maskedCorners
        passMouseThrough(collapsed: isCompact())
        guard isVisible else { return }
        let interrupted = pendingReveal
        cancelReveal()
        if isCompact() {
            settleToBar()
        } else if interrupted?.opening == false {
            // A close cut short keeps its small mask, as a finished close does,
            // until the caller's completion swaps in the bar or hides the panel.
        } else if let f = frame(compact: false) {
            setFrame(f, display: true)
            fillMask()
        }
        guard let interrupted else { return }
        let token = revealGeneration
        interrupted.completion()
        // As after a finished open, unless the completion began another reveal.
        if interrupted.opening, revealGeneration == token { resizeToFit() }
    }

    /// How long a shrink waits for SwiftUI to finish animating the content
    /// down before the window follows: the panel's settle time (Theme.Motion).
    static let shrinkSettle: TimeInterval = Theme.Motion.settle

    /// Fit the window to the content.
    ///
    /// The window is a clip, not the panel's edge: the panel's black surface,
    /// its rounded bottom and its shadow are drawn by the SwiftUI content, which
    /// animates its own size (Theme.Motion). The window only has to stay at
    /// least as big as what the content is drawing, and as small as it can be
    /// once the content is at rest. So:
    /// - Growing applies at once, before the content animates into the room,
    ///   so a growing panel is never clipped at the window's bottom edge.
    /// - Shrinking waits `shrinkSettle`, until the content has animated down, so
    ///   a page still animating out is never cut off. What the content has left
    ///   is transparent by then, so the window's last step is invisible.
    /// - A change of width and height together (the two-column Brief, a page
    ///   switch from it) grows at once to cover both shapes, then settles.
    /// - Calls coalesce: each one measures afresh, a newer shrink replaces a
    ///   pending one, and a grow lands at once, so rapid switches never stack.
    /// - With Reduce Motion nothing animates, so every change applies at once.
    func resizeToFit() {
        guard isVisible, !isRevealing, !isCompact(), let f = frame(compact: false) else { return }
        cancelPendingShrink()
        guard differs(f) else { return }
        if reduceMotion() {
            setFrame(f, display: true)
            fillMask()
            return
        }
        let covering = Self.covering(frame, f)
        if differs(covering) {
            setFrame(covering, display: true)
            fillMask()
        }
        guard differs(f) else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingShrink = nil
            guard self.isVisible, !self.isRevealing, !self.isCompact(),
                  let latest = self.frame(compact: false), self.differs(latest) else { return }
            self.setFrame(latest, display: true)
            self.fillMask()
        }
        pendingShrink = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.shrinkSettle, execute: work)
    }

    /// The smallest window that holds both the current frame and the target:
    /// the panel hangs from a fixed top and is centred, so this is the wider
    /// of the two and the taller, with the same top.
    static func covering(_ current: NSRect, _ target: NSRect) -> NSRect {
        let top = max(current.maxY, target.maxY)
        let minX = min(current.minX, target.minX), maxX = max(current.maxX, target.maxX)
        let height = max(current.height, target.height)
        return NSRect(x: minX, y: top - height, width: maxX - minX, height: height)
    }

    /// Whether a shrink is waiting for the content to finish animating.
    var hasPendingShrink: Bool { pendingShrink != nil }

    private func differs(_ f: NSRect) -> Bool {
        abs(f.height - frame.height) > 1 || abs(f.width - frame.width) > 1
            || abs(f.origin.x - frame.origin.x) > 1
    }

    private func cancelPendingShrink() {
        pendingShrink?.cancel()
        pendingShrink = nil
    }

    func present() {
        cancelReveal()
        layoutIfNeeded()
        if let f = barFrame() { setFrame(f, display: true) }
        fillMask()
        passMouseThrough(collapsed: isCompact())
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
        matchBackingScale(mask)
        mask.bounds = CGRect(origin: .zero, size: frame.size)
        mask.position = CGPoint(x: frame.width / 2, y: frame.height)
        mask.cornerRadius = isCompact() ? barCornerRadius : panelCornerRadius
        if disableActions { CATransaction.commit() }
    }

    // MARK: - Calm, top-anchored reveal

    // A shared non-overshooting curve keeps size and corners together. The
    // top edge never moves, so content grows out of the notch without bounce.
    private static let revealCurve = CAMediaTimingFunction(controlPoints: 0.22, 0.75, 0.25, 1)
    // Closing starts gently, so the shape barely moves while the content
    // fades (AppDelegate.closeFade), then gathers into the notch.
    private static let closeCurve = CAMediaTimingFunction(controlPoints: 0.4, 0, 0.2, 1)

    /// The mask's rounded edge is rasterized at the layer's scale; left at the
    /// default 1x it can step visibly on a Retina screen while it animates.
    private func matchBackingScale(_ mask: CALayer) {
        let scale = backingScaleFactor
        if mask.contentsScale != scale { mask.contentsScale = scale }
        mask.allowsEdgeAntialiasing = true
    }

    private func cancelReveal() {
        revealGeneration &+= 1
        isRevealing = false
        pendingReveal = nil
        revealMask?.removeAllAnimations()
    }

    override func orderOut(_ sender: Any?) {
        cancelReveal()
        cancelPendingShrink()
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
            passMouseThrough(collapsed: false)
            setFrame(full, display: true)
            if revealsWithoutKey {
                // Nobody asked for this open: show it, but leave the keyboard
                // with the app the person is typing in until they click here.
                revealsWithoutKey = false
                orderFrontRegardless()
            } else {
                makeKeyAndOrderFront(nil)
            }
        }
        guard let mask = revealMask, let rects = maskRects() else { completion(); return }
        let startSize = visibleBounds?.size ?? (opening ? rects.bar.size : rects.full.size)
        let endSize = opening ? rects.full.size : rects.bar.size
        let startRadius = visibleRadius ?? (opening ? barCornerRadius : panelCornerRadius)
        let endRadius = opening ? panelCornerRadius : barCornerRadius
        let token = revealGeneration
        isRevealing = true
        pendingReveal = (opening: opening, completion: completion)

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        matchBackingScale(mask)
        mask.position = CGPoint(x: rects.full.midX, y: rects.full.maxY)
        mask.bounds = CGRect(origin: .zero, size: endSize)
        mask.cornerRadius = endRadius
        CATransaction.commit()

        let finish = { [weak self] in
            guard let self, self.revealGeneration == token else { return }
            self.isRevealing = false
            self.pendingReveal = nil
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
        // Closing is a touch quicker than opening; the content fades out
        // (OverlayModel.closing) while the slow start holds the shape.
        group.duration = opening ? 0.38 : 0.28
        size.duration = group.duration
        radius.duration = group.duration
        group.timingFunction = opening ? Self.revealCurve : Self.closeCurve
        CATransaction.begin()
        CATransaction.setCompletionBlock(finish)
        mask.add(group, forKey: "reveal")
        CATransaction.commit()
    }

    /// The next open shows the panel without making it key (an unrequested offer).
    var revealsWithoutKey = false

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
        passMouseThrough(collapsed: true)
    }
}
