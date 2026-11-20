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

/// A spring, in SwiftUI's terms: `response` is the undamped period in seconds,
/// `dampingRatio` 1 is no bounce at all.
///
/// Opening and closing start from NotchBox's two springs, measured from that
/// app: it opens on `.spring(response: 0.5, dampingFraction: 0.7).speed(1.2)`
/// and closes on `.spring(response: 0.35, dampingFraction: 1)`. A spring has no
/// fixed end, so it starts fast and eases into rest without the abrupt ends of
/// a timing curve. RevealMotion turns the two into the paths the panel takes.
struct NotchSpring: Equatable {
    var response: Double
    var dampingRatio: Double

    static let open = NotchSpring(response: 0.5 / 1.2, dampingRatio: 0.7)
    static let close = NotchSpring(response: 0.35, dampingRatio: 1)

    /// How far past the end the spring swings, as a fraction of the travel.
    var overshoot: Double {
        guard dampingRatio < 1 else { return 0 }
        return exp(-Double.pi * dampingRatio / (1 - dampingRatio * dampingRatio).squareRoot())
    }

    /// The same spring, damped just enough to swing no further than `limit`
    /// (a fraction of the travel). Unchanged when it already stays inside.
    func capped(overshoot limit: Double) -> NotchSpring {
        guard limit < overshoot else { return self }
        guard limit > 0 else { return NotchSpring(response: response, dampingRatio: 1) }
        let l = log(limit)
        return NotchSpring(response: response,
                           dampingRatio: min(-l / (Double.pi * Double.pi + l * l).squareRoot(), 1))
    }

    /// Where the spring is `t` seconds after it is let go: 0 at the start, 1 at rest.
    func position(at t: Double) -> Double {
        guard t > 0 else { return 0 }
        let w = 2 * Double.pi / response
        if dampingRatio >= 1 { return 1 - (1 + w * t) * exp(-w * t) }
        let root = (1 - dampingRatio * dampingRatio).squareRoot()
        let wd = w * root
        return 1 - exp(-dampingRatio * w * t) * (cos(wd * t) + dampingRatio / root * sin(wd * t))
    }

    /// Seconds until what is left of the motion stays under `tolerance`, a
    /// fraction of the travel.
    func settleTime(tolerance: Double) -> Double {
        let w = 2 * Double.pi / response
        let left = min(max(tolerance, 1e-4), 0.5)
        if dampingRatio >= 1 {
            // Critically damped: (1 + wt) e^-wt is what is left. Bisect for wt.
            var low = 0.0, high = 24.0
            for _ in 0..<48 {
                let mid = (low + high) / 2
                if (1 + mid) * exp(-mid) > left { low = mid } else { high = mid }
            }
            return high / w
        }
        // Under-damped: the swing decays inside e^-(ratio w t) / sqrt(1 - ratio^2).
        let root = (1 - dampingRatio * dampingRatio).squareRoot()
        return max(log(1 / (left * root)) / (dampingRatio * w), 0)
    }
}

/// The path an open or close takes: how far along it is every 1/120 s, 0 at
/// the shape it starts from and 1 at the shape it ends on. The size and the
/// corners follow the same path, so they stay together.
struct RevealMotion: Equatable {
    static let step = 1.0 / 120
    var progress: [Double]
    var duration: Double { Double(max(progress.count - 1, 1)) * Self.step }

    /// How far the opening spring would swing past the panel if left to, in
    /// points: it sets how softly the open lands. NotchBox's own spring swings
    /// 4.6% past, which on Wisp's panel is a 10 pt wobble of the outline around
    /// content that stands still. The spring keeps NotchBox's pace and is
    /// damped until it would pass by a point at most.
    static let openOvershoot: CGFloat = 1

    /// Opening, for a reveal whose furthest edge travels `reach` points. The
    /// path ends as the shape arrives, moving about a third of a point a
    /// frame by then, so it never passes the panel and never backs up.
    static func opening(reach: CGFloat) -> RevealMotion {
        let reach = Double(max(reach, 1))
        let spring = NotchSpring.open.capped(overshoot: Double(openOvershoot) / reach)
        let end = spring.settleTime(tolerance: 1 / reach)
        var progress: [Double] = []
        var t = 0.0
        while t < end {
            let along = spring.position(at: t)
            if along >= 1 { break }
            progress.append(along)
            t += step
        }
        progress.append(1)
        return RevealMotion(progress: progress)
    }

    /// How near rest the closing spring is taken before the path ends, as a
    /// fraction of the travel: under two points on the tallest panel.
    static let closeSettle = 0.002

    /// Closing is NotchBox's own closing spring, as that app plays it: no
    /// hold, aimed at the bar itself, quick out of the panel and a long ease
    /// into the notch, about half a second in all. It was chosen by eye on a
    /// real screen from nine closes tried side by side (a shorter one that
    /// stopped dead at the bar, timed glides, a snap, a dissolve, a soft
    /// edge, a funnel and a roll-up among them).
    static func closing() -> RevealMotion {
        let end = NotchSpring.close.settleTime(tolerance: closeSettle)
        var progress: [Double] = []
        var t = 0.0
        while t < end {
            progress.append(NotchSpring.close.position(at: t))
            t += step
        }
        progress.append(1)
        return RevealMotion(progress: progress)
    }
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
        // Everything in this panel is drawn on black (Theme.surface), so its
        // system controls (text fields, toggles, pickers, spinners) take the
        // dark look whatever the Mac's appearance; on a light Mac they drew
        // light-style on the black panel.
        appearance = NSAppearance(named: .darkAqua)
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
        // A notch's ears (SurfaceLayout.notchEarRadius): two small shapes hung off
        // the mask's top corners. A mask's sublayers add to what it lets through,
        // so they widen its top row and curve into the body, outside its bounds.
        for side in [NotchEar.left, .right] {
            let ear = CAShapeLayer()
            ear.name = side.name
            ear.fillColor = NSColor.black.cgColor
            ear.anchorPoint = .zero
            mask.addSublayer(ear)
        }
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
        if !layout.barIsVisible { backBar(false) }
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
        // A close cut short still has its swap to the bar ahead of it (the
        // caller's completion makes it), and a bar that has only just taken
        // over may not have drawn yet. Both keep their backing, and
        // settleToBar drops it as after any close. Nothing else has a use for
        // one, and a bar that draws nothing never has one.
        let headedForBar = interrupted?.opening == false || isCompact()
        if !(headedForBar && l.barIsVisible) { backBar(false) }
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
        backBar(false)
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
        let body = CGSize(width: layout.bodyWidth(ofWindow: frame.width), height: frame.height)
        mask.bounds = CGRect(origin: .zero, size: body)
        mask.position = CGPoint(x: frame.width / 2, y: frame.height)
        mask.cornerRadius = isCompact() ? barCornerRadius : panelCornerRadius
        placeEars(on: mask, body: body)
        if disableActions { CATransaction.commit() }
    }

    /// The ears' layers, left then right.
    private func ears(of mask: CALayer) -> [CAShapeLayer] {
        [NotchEar.left, .right].compactMap { side in
            mask.sublayers?.first { $0.name == side.name } as? CAShapeLayer
        }
    }

    /// The ears at the top corners of a body this size, in the mask's own
    /// coordinates (its origin is the body's bottom left).
    private func placeEars(on mask: CALayer, body: CGSize) {
        let radius = layout.earRadius
        for (ear, side) in zip(ears(of: mask), [NotchEar.left, .right]) {
            ear.isHidden = radius <= 0
            ear.contentsScale = mask.contentsScale
            ear.bounds = CGRect(x: 0, y: 0, width: radius, height: radius)
            ear.path = radius > 0 ? NotchEar.path(side, radius: radius) : nil
            ear.position = side.origin(body: body, radius: radius)
        }
    }

    // MARK: - Top-anchored reveal

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
        revealMask.map { ears(of: $0).forEach { $0.removeAllAnimations() } }
        showContent()
    }

    /// How long into a close the content's layers stay in the picture: its
    /// fade (AppDelegate.closeFade) and a couple of frames for it to land.
    static let contentHideDelay: TimeInterval = 0.08

    /// Bring the content's layers back after a close took them out (see reveal).
    private func showContent() {
        if slide?.isHidden == true { slide?.isHidden = false }
    }

    /// Whether a close has taken the content's layers out for now (for tests).
    var contentIsHidden: Bool { slide?.isHidden == true }

    /// Ask for the display's full refresh rate while the shape moves.
    static let revealFrameRate = CAFrameRateRange(minimum: 80, maximum: 120, preferred: 120)

    override func orderOut(_ sender: Any?) {
        cancelReveal()
        cancelPendingShrink()
        backBar(false)
        super.orderOut(sender)
    }

    // MARK: Bar backing

    private var barBackingGeneration: UInt64 = 0

    /// The panel's surface behind the content, in the shape the mask shows.
    /// A close turns it on so the bar stays solid through the moment the
    /// content is swapped for the collapsed bar and the window drops to the
    /// bar's frame: the bar's own view may not have drawn yet, and without
    /// this the bar could blink off for a frame. Never on while the bar draws
    /// nothing (a hidden notch, the menu-bar seed).
    private func backBar(_ on: Bool) {
        barBackingGeneration &+= 1
        guard let layer = stage?.layer else { return }
        let color = on && layout.barIsVisible ? NSColor(Theme.surface).cgColor : nil
        guard layer.backgroundColor != color else { return }
        CATransaction.begin(); CATransaction.setDisableActions(true)
        layer.backgroundColor = color
        CATransaction.commit()
    }

    /// The mask's size for the window as it is: the body, without the ears (for tests).
    var maskBody: CGSize { CGSize(width: layout.bodyWidth(ofWindow: frame.width), height: frame.height) }

    /// Whether the bar's backing is on (for tests).
    var hasBarBacking: Bool { stage?.layer?.backgroundColor != nil }

    /// How long the backing outlives a close: long enough for the bar's view to draw.
    static let barBackingLinger: TimeInterval = 0.15

    private func dropBarBackingSoon() {
        guard hasBarBacking else { return }
        barBackingGeneration &+= 1
        let token = barBackingGeneration
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.barBackingLinger) { [weak self] in
            guard let self, self.barBackingGeneration == token else { return }
            self.backBar(false)
        }
    }

    /// Bring an opening panel forward: key, unless nobody asked for this open.
    private func bringForward() {
        if revealsWithoutKey {
            // Nobody asked for this open: show it, but leave the keyboard
            // with the app the person is typing in until they click here.
            revealsWithoutKey = false
            orderFrontRegardless()
        } else {
            makeKeyAndOrderFront(nil)
        }
    }

    private func reveal(opening: Bool, completion: @escaping () -> Void) {
        // Capture visible geometry before replacing the one animation group.
        let visible = isRevealing ? revealMask?.presentation() : nil
        let visibleBounds = visible?.bounds
        let visibleRadius = visible?.cornerRadius
        cancelReveal()
        guard let bar = barFrame() else { completion(); return }
        if opening {
            guard let full = frame(compact: false) else { completion(); return }
            backBar(false)
            passMouseThrough(collapsed: false)
            setFrame(full, display: true)
        }
        guard let mask = revealMask, frame.width > 0, frame.height > 0 else {
            if opening { bringForward() }
            completion()
            return
        }
        // The mask is the body; the window also holds the ears beside it.
        func body(_ window: CGSize) -> CGSize { CGSize(width: layout.bodyWidth(ofWindow: window.width), height: window.height) }
        let startSize = visibleBounds?.size ?? body(opening ? bar.size : frame.size)
        let endSize = body(opening ? frame.size : bar.size)
        let startRadius = visibleRadius ?? (opening ? barCornerRadius : panelCornerRadius)
        let endRadius = opening ? panelCornerRadius : barCornerRadius
        let token = revealGeneration
        isRevealing = true
        pendingReveal = (opening: opening, completion: completion)

        // The shape hangs from the top centre of the window.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        matchBackingScale(mask)
        mask.position = CGPoint(x: frame.width / 2, y: frame.height)
        mask.bounds = CGRect(origin: .zero, size: endSize)
        mask.cornerRadius = endRadius
        placeEars(on: mask, body: endSize)
        CATransaction.commit()

        let finish = { [weak self] in
            guard let self, self.revealGeneration == token else { return }
            self.isRevealing = false
            self.pendingReveal = nil
            // Before the caller swaps in the bar, which the content draws.
            self.showContent()
            completion()
            // Coalesce height changes received while opening. A close keeps
            // its small mask until the caller has swapped in the compact bar.
            if opening, self.revealGeneration == token { self.resizeToFit() }
        }
        guard !reduceMotion() else {
            if opening { bringForward() }
            finish()
            return
        }
        if !opening { backBar(true) }

        // One path moves the size and the corners together (RevealMotion).
        let motion = opening
            ? RevealMotion.opening(reach: max(abs(endSize.width - startSize.width) / 2,
                                              abs(endSize.height - startSize.height)))
            : RevealMotion.closing()
        func along(_ from: CGFloat, _ to: CGFloat, _ p: Double) -> CGFloat { from + (to - from) * CGFloat(p) }
        let size = CAKeyframeAnimation(keyPath: "bounds.size")
        size.values = motion.progress.map {
            NSValue(size: NSSize(width: max(along(startSize.width, endSize.width, $0), 0),
                                 height: max(along(startSize.height, endSize.height, $0), 0)))
        }
        let radius = CAKeyframeAnimation(keyPath: "cornerRadius")
        radius.values = motion.progress.map { max(along(startRadius, endRadius, $0), 0) }
        for part in [size, radius] {
            part.calculationMode = .linear
            part.duration = motion.duration
        }
        let group = CAAnimationGroup()
        group.animations = [size, radius]
        group.duration = motion.duration
        group.preferredFrameRateRange = Self.revealFrameRate
        // The ears ride the same samples, so they stay on the body's top corners.
        let radiusNow = layout.earRadius
        let earMotions: [(CAShapeLayer, CAKeyframeAnimation)] = radiusNow > 0
            ? zip(ears(of: mask), [NotchEar.left, .right]).map { ear, side in
                let move = CAKeyframeAnimation(keyPath: "position")
                move.values = motion.progress.map { p in
                    NSValue(point: side.origin(body: CGSize(width: max(along(startSize.width, endSize.width, p), 0),
                                                            height: max(along(startSize.height, endSize.height, p), 0)),
                                               radius: radiusNow))
                }
                move.calculationMode = .linear
                move.duration = motion.duration
                move.preferredFrameRateRange = Self.revealFrameRate
                return (ear, move)
            }
            : []
        CATransaction.begin()
        CATransaction.setCompletionBlock(finish)
        mask.add(group, forKey: "reveal")
        for (ear, move) in earMotions { ear.add(move, forKey: "reveal") }
        CATransaction.commit()
        // Once the content has faded, a close is only the surface in the
        // mask's shape. Its layers come out of the picture until the bar is
        // swapped in, so each frame is one flat colour to draw. Only where
        // the backing is there to show that surface.
        if !opening, hasBarBacking {
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.contentHideDelay) { [weak self] in
                guard let self, self.revealGeneration == token, self.isRevealing else { return }
                self.slide?.isHidden = true
            }
        }
        // Send the window's new frame, the shape and its motion to the screen
        // now. macOS applies a window's frame with the layer changes made
        // beside it, so they always land together, but left to the end of this
        // run-loop turn they wait behind whatever else the turn does (making
        // the window key, SwiftUI laying the panel out), and the reveal starts
        // that much late.
        CATransaction.flush()
        if opening { bringForward() }
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
        // The bar takes over now, not at the end of the turn (see reveal).
        CATransaction.flush()
        passMouseThrough(collapsed: true)
        dropBarBackingSoon()
    }
}


/// One of a notch shape's two ears (SurfaceLayout.notchEarRadius): the top row
/// reaches `radius` past the body and curves into its side, as NotchBox draws
/// it and as the camera housing itself meets the menu bar.
enum NotchEar {
    case left, right

    var name: String { self == .left ? "wisp.ear.left" : "wisp.ear.right" }

    /// The ear's bottom-left corner in the mask's coordinates (origin at the
    /// body's bottom left, y up): beside the body's top corner, outside it.
    func origin(body: CGSize, radius: CGFloat) -> CGPoint {
        CGPoint(x: self == .left ? -radius : body.width, y: body.height - radius)
    }

    /// The ear in its own `radius` square, y up: full width along the top,
    /// nothing at the bottom, and between them a quarter circle centred on the
    /// square's outer bottom corner. Its points are where NotchBox's closed shape
    /// measured (the top row 5 pt past the body, gone 5 pt down).
    static func path(_ side: NotchEar, radius r: CGFloat) -> CGPath {
        let path = CGMutablePath()
        // Left: the body is on the right (x = r); the circle is centred at (0, 0).
        // Right: mirrored, the body on the left and the circle at (r, 0).
        let inner: CGFloat = side == .left ? r : 0
        let outer: CGFloat = side == .left ? 0 : r
        path.move(to: CGPoint(x: inner, y: 0))
        path.addLine(to: CGPoint(x: inner, y: r))
        path.addLine(to: CGPoint(x: outer, y: r))
        // From the outer top corner down and in to the body's side.
        let steps = 24
        for i in 1...steps {
            let a = CGFloat(i) / CGFloat(steps) * .pi / 2          // 0 at the top, π/2 at the body
            let x = r * sin(a), y = r * cos(a)
            path.addLine(to: CGPoint(x: side == .left ? x : r - x, y: y))
        }
        path.closeSubpath()
        return path
    }
}
