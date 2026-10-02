import AppKit
import SwiftUI

// One place that decides where Wisp's overlay lives on whatever display it is
// on. Everything here is plain values so it can be tested for every MacBook and
// monitor size without a physical screen; `DisplayGeometry` is the only piece
// that touches AppKit, and it refreshes whenever displays change.
//
// Two styles, chosen per display:
//   .notch    — any MacBook's built-in display. Fused to the top edge and
//               centred on the camera housing. Macs without a physical notch
//               (M1 Air, 13" Pro) get a *virtual* notch of the same size, so
//               the overlay looks and behaves identically on every MacBook.
//   .floating — external monitors. Same hover-to-open behaviour and the same
//               panel, but no notch is drawn: a detached capsule sits just
//               under the menu bar and opens into a fully rounded card.

/// The facts about one display that layout needs.
struct DisplayMetrics: Equatable {
    var frame: CGRect
    var visibleFrame: CGRect
    var safeAreaTop: CGFloat
    var auxLeftWidth: CGFloat
    var auxRightWidth: CGFloat
    var isBuiltIn: Bool

    /// The physical camera-housing size in points, or nil when the display has
    /// none (or reports geometry too odd to trust).
    var physicalNotch: CGSize? {
        guard safeAreaTop > 0 else { return nil }
        let width = frame.width - auxLeftWidth - auxRightWidth
        guard width >= 100, width <= 400 else { return nil }
        return CGSize(width: width, height: safeAreaTop)
    }

    init(frame: CGRect, visibleFrame: CGRect, safeAreaTop: CGFloat = 0,
         auxLeftWidth: CGFloat = 0, auxRightWidth: CGFloat = 0, isBuiltIn: Bool) {
        self.frame = frame
        self.visibleFrame = visibleFrame
        self.safeAreaTop = safeAreaTop
        self.auxLeftWidth = auxLeftWidth
        self.auxRightWidth = auxRightWidth
        self.isBuiltIn = isBuiltIn
    }

    init(screen: NSScreen) {
        let id = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID
        self.init(frame: screen.frame, visibleFrame: screen.visibleFrame,
                  safeAreaTop: screen.safeAreaInsets.top,
                  auxLeftWidth: screen.auxiliaryTopLeftArea?.width ?? 0,
                  auxRightWidth: screen.auxiliaryTopRightArea?.width ?? 0,
                  isBuiltIn: id.map { CGDisplayIsBuiltin($0) != 0 } ?? false)
    }
}

struct SurfaceLayout: Equatable {
    enum Style: Equatable { case notch, floating }

    // Reference notch (14"/16" MacBook Pro, 13"/15" Air): 185 x 32 pt. Used
    // for the virtual notch on MacBooks that have no camera housing.
    static let referenceNotch = CGSize(width: 185, height: 32)
    static let maxExpandedWidth: CGFloat = 640
    /// Collapsed bar overhang on each side of the notch — a hairline of black
    /// so the bar reads as Wisp's, not merely as the hardware.
    static let wing: CGFloat = 6
    /// Extra reach of the hover zone past the visible bar. Forgiving on the
    /// way in, and paired with a dwell delay so a brush doesn't open it.
    static let hoverMarginX: CGFloat = 8
    static let hoverMarginY: CGFloat = 6
    /// Notch only: how far the hover zone reaches past each side of the bar. The
    /// camera housing is a narrow target, and the menu bar's own items sit well
    /// beyond this, so wider is safe.
    static let notchHoverMarginX: CGFloat = 20
    /// Notch only: the lowest part of the notch (a fraction of its height) does NOT
    /// open the menu. People cross it on the way to whatever is under the notch, and
    /// the zone no longer extends below the notch at all.
    static let notchHoverBottomExclusion: CGFloat = 0.25
    static let floatingGap: CGFloat = 8
    static let floatingBar = CGSize(width: 148, height: 28)
    static let screenMargin: CGFloat = 12

    let style: Style
    let isVirtualNotch: Bool
    let screenFrame: CGRect
    let usableFrame: CGRect
    let notchSize: CGSize
    let expandedWidth: CGFloat
    let barFrame: CGRect
    /// Screen coordinates in which the pointer counts as "at the bar".
    let hoverZone: CGRect

    var barCornerRadius: CGFloat { style == .notch ? 10 : 14 }
    var panelCornerRadius: CGFloat { style == .notch ? 28 : 24 }
    /// SwiftUI content sits below the camera housing on a notch; a floating
    /// card just needs ordinary padding.
    var contentTopInset: CGFloat { style == .notch ? notchSize.height + 8 : 16 }
    var windowLevel: NSWindow.Level { style == .notch ? .statusBar : .floating }
    var maskedCorners: CACornerMask {
        style == .notch ? [.layerMinXMinYCorner, .layerMaxXMinYCorner]
                        : [.layerMinXMinYCorner, .layerMaxXMinYCorner,
                           .layerMinXMaxYCorner, .layerMaxXMaxYCorner]
    }
    var barSize: CGSize { barFrame.size }

    func shape(radius: CGFloat) -> UnevenRoundedRectangle {
        let top: CGFloat = style == .notch ? 0 : radius
        return UnevenRoundedRectangle(topLeadingRadius: top, bottomLeadingRadius: radius,
                                      bottomTrailingRadius: radius, topTrailingRadius: top)
    }
    var panelShape: UnevenRoundedRectangle { shape(radius: panelCornerRadius) }
    var barShape: UnevenRoundedRectangle { shape(radius: barCornerRadius) }

    /// Frame of the expanded panel for a given content height: centred on the
    /// anchor, hanging from the top, and never taller than the screen.
    func expandedFrame(contentHeight: CGFloat) -> CGRect {
        let top = style == .notch ? screenFrame.maxY
                                  : usableFrame.maxY - Self.floatingGap
        let room = top - screenFrame.minY - Self.screenMargin
        let height = min(contentHeight, max(room, 0))
        return CGRect(x: (usableFrame.midX - expandedWidth / 2).rounded(),
                      y: (top - height).rounded(), width: expandedWidth, height: height)
    }

    static func make(for metrics: DisplayMetrics) -> SurfaceLayout {
        let frame = metrics.frame
        let width = min(maxExpandedWidth, max(frame.width - 2 * screenMargin, 320))
        if metrics.isBuiltIn {
            let physical = metrics.physicalNotch
            let notch = physical ?? referenceNotch
            let barW = (notch.width + 2 * wing).rounded()
            let barH = notch.height.rounded()
            let bar = CGRect(x: (frame.midX - barW / 2).rounded(), y: frame.maxY - barH,
                             width: barW, height: barH)
            return SurfaceLayout(style: .notch, isVirtualNotch: physical == nil,
                                 screenFrame: frame, usableFrame: frame, notchSize: notch,
                                 expandedWidth: width, barFrame: bar,
                                 hoverZone: notchZone(around: bar, top: frame.maxY))
        }
        let usable = metrics.visibleFrame
        let size = floatingBar
        let bar = CGRect(x: (usable.midX - size.width / 2).rounded(),
                         y: (usable.maxY - floatingGap - size.height).rounded(),
                         width: size.width, height: size.height)
        return SurfaceLayout(style: .floating, isVirtualNotch: false, screenFrame: frame,
                             usableFrame: usable, notchSize: .zero, expandedWidth: width,
                             barFrame: bar, hoverZone: zone(around: bar, top: usable.maxY))
    }

    /// The notch's hover zone: wide, covering the upper part of the notch and the
    /// screen's top edge, and stopping above the notch's bottom strip.
    ///
    /// The top reaches ONE POINT past the screen edge on purpose. macOS clamps a
    /// pointer pushed against the top of the screen to y == frame.maxY exactly, and
    /// CGRect.contains treats its max edge as outside, so the natural gesture (fling
    /// the pointer to the top-centre) used to miss the zone entirely.
    private static func notchZone(around bar: CGRect, top: CGFloat) -> CGRect {
        let lower = bar.minY + bar.height * notchHoverBottomExclusion
        return CGRect(x: bar.minX - notchHoverMarginX, y: lower,
                      width: bar.width + 2 * notchHoverMarginX, height: top + 1 - lower)
    }

    private static func zone(around bar: CGRect, top: CGFloat) -> CGRect {
        let x = bar.minX - hoverMarginX
        let y = bar.minY - hoverMarginY
        return CGRect(x: x, y: y, width: bar.width + 2 * hoverMarginX,
                      height: max(top - y, bar.height + hoverMarginY))
    }

    /// The display Wisp lives on: a MacBook's built-in screen when it is
    /// present, otherwise the primary display (the one holding the menu bar).
    static func target(from displays: [DisplayMetrics]) -> DisplayMetrics? {
        displays.first(where: \.isBuiltIn) ?? displays.first
    }

    /// Used only if the system reports no displays at all.
    static let fallback = make(for: DisplayMetrics(
        frame: CGRect(x: 0, y: 0, width: 1512, height: 982),
        visibleFrame: CGRect(x: 0, y: 0, width: 1512, height: 950),
        safeAreaTop: 32, auxLeftWidth: 663.5, auxRightWidth: 663.5, isBuiltIn: true))
}

/// Pointer-intent rules for the collapsed bar, kept pure so the reliability
/// fixes are testable: open only after the pointer dwells in the zone, never
/// while a button is held (dragging a window or file past the notch), and only
/// after the pointer has left the zone since the last close, so the panel
/// can't reopen under a pointer that simply stayed where it was.
struct HoverIntent: Equatable {
    static let dwell: TimeInterval = 0.12
    private(set) var armed = true

    enum Action: Equatable { case none, beginDwell, cancelDwell }

    mutating func pointerMoved(inZone: Bool, buttonsDown: Bool, collapsed: Bool) -> Action {
        if !inZone { armed = true }
        guard collapsed, inZone, armed, !buttonsDown else {
            return inZone ? .none : .cancelDwell
        }
        return .beginDwell
    }

    /// Call when the panel settles back to the bar after an open.
    mutating func didCollapse(pointerInZone: Bool) { armed = !pointerInZone }

    /// Whether a dwell that has just elapsed should still open the panel.
    func shouldOpen(inZone: Bool, buttonsDown: Bool, collapsed: Bool) -> Bool {
        collapsed && inZone && armed && !buttonsDown
    }
}

/// Live layout for the current displays. Recomputed when displays are
/// attached, removed, rearranged, or change resolution — the launch-time
/// snapshot the overlay used before went stale on every one of those.
@MainActor
final class DisplayGeometry: ObservableObject {
    static let shared = DisplayGeometry()

    @Published private(set) var layout: SurfaceLayout
    private var observer: NSObjectProtocol?
    private var pending: DispatchWorkItem?
    /// Called after `layout` actually changed (panels re-fit themselves).
    var onChange: () -> Void = {}

    init(displays: @autoclosure () -> [DisplayMetrics] = DisplayGeometry.currentDisplays()) {
        layout = SurfaceLayout.target(from: displays()).map(SurfaceLayout.make) ?? .fallback
    }

    nonisolated static func currentDisplays() -> [DisplayMetrics] {
        NSScreen.screens.map(DisplayMetrics.init(screen:))
    }

    func startObserving() {
        guard observer == nil else { return }
        observer = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            // Displays often report several changes in a burst while waking or
            // hot-plugging; settle on the last one.
            MainActor.assumeIsolated { self?.scheduleRefresh() }
        }
    }

    private func scheduleRefresh() {
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.refresh() }
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
    }

    func refresh(displays: [DisplayMetrics] = DisplayGeometry.currentDisplays()) {
        let next = SurfaceLayout.target(from: displays).map(SurfaceLayout.make) ?? .fallback
        guard next != layout else { return }
        layout = next
        onChange()
    }
}
