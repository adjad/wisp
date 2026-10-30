import AppKit
import SwiftUI

// One place that decides where Wisp's overlay lives on whatever display it is
// on. Everything here is plain values so it can be tested for every MacBook and
// monitor size without a physical screen; `DisplayGeometry` is the only piece
// that touches AppKit, and it refreshes whenever displays change.
//
// Three styles, chosen per display:
//   .notch    — a built-in display with a camera housing. Fused to the top
//               edge and centred on the notch.
//   .menuBar  — a built-in display without one (M1 Air, 13" Pro, iMac). Nothing
//               is drawn at rest: a drawn notch there would cover app menus and
//               window titles. The menu-bar icon is the visible anchor and
//               carries the bar's status; pushing the pointer to the top-centre
//               edge opens the same panel as a card just under the menu bar.
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

    /// Whether the display has a camera housing at all, even one whose side
    /// areas are too odd to measure.
    var hasCameraHousing: Bool { safeAreaTop > 0 }

    /// The menu bar's height, or the standard one while it is hidden.
    var menuBarHeight: CGFloat {
        let reported = frame.maxY - visibleFrame.maxY
        return reported > 0 ? reported : SurfaceLayout.standardMenuBarHeight
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
    enum Style: String, Equatable { case notch, menuBar, floating }

    // Reference notch (14"/16" MacBook Pro, 13"/15" Air): 185 pt wide. Used only
    // when a display reports a camera housing but implausible side areas.
    static let referenceNotch = CGSize(width: 185, height: 32)
    static let maxExpandedWidth: CGFloat = 640
    /// The panel's width for the Brief's two-column view.
    static let wideExpandedWidth: CGFloat = 840
    /// The narrowest usable width worth widening to; a display with less room
    /// than this keeps the one-column Brief.
    static let minWideWidth: CGFloat = 800
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
    /// Menu bar only: the invisible top-centre target. As wide as a notch's zone,
    /// but only the top few points of the menu bar, because on these displays app
    /// menus and status items DO run under the centre: people hover them at the
    /// menu bar's middle, while the open gesture is a push against the top edge
    /// (macOS pins the pointer there, so a thin strip is easy to hit).
    static let edgeZoneWidth: CGFloat = 240
    static let edgeZoneDepth: CGFloat = 4
    /// Menu bar only: the never-drawn shape the card grows out of and gathers
    /// back into, at the top-centre of the card. Small, so the close ends on
    /// almost nothing rather than on a shape that then vanishes.
    static let edgeSeed = CGSize(width: 64, height: 12)
    /// Menu bar heights: macOS's standard, used when the menu bar is hidden
    /// (auto-hide) and the display reports no height of its own.
    static let standardMenuBarHeight: CGFloat = 24

    let style: Style
    let screenFrame: CGRect
    let usableFrame: CGRect
    let notchSize: CGSize
    var expandedWidth: CGFloat
    /// The collapsed window. On .menuBar it is never drawn and lets clicks
    /// through to the menu bar under it (see `barIsVisible`).
    let barFrame: CGRect
    /// Screen coordinates in which the pointer counts as "at the bar".
    let hoverZone: CGRect

    /// Whether anything is drawn while collapsed. Only false on .menuBar, where
    /// the collapsed window must also ignore the mouse so the menu bar keeps it.
    var barIsVisible: Bool { style != .menuBar }
    /// Whether the menu-bar icon carries the bar's status (working, due today,
    /// not set up), because there is no visible bar to carry it.
    var statusInMenuBar: Bool { style == .menuBar }
    /// How long the pointer must rest in the zone. Longer on .menuBar, whose zone
    /// shares the top edge with whatever menu sits under the centre.
    var hoverDwell: TimeInterval { style == .menuBar ? 0.2 : HoverIntent.dwell }
    /// The first-run tip, naming where Wisp lives on this display.
    var firstRunTip: String {
        let place: String
        switch style {
        case .notch: place = "Wisp lives in the notch now. Press ⌥Space or hover here to ask."
        case .menuBar: place = "Wisp lives at the top of your screen. Press ⌥Space, push the pointer "
            + "to the top centre, or click the Wisp icon in the menu bar to ask."
        case .floating: place = "Wisp lives at the top of your screen. Press ⌥Space or hover here to ask."
        }
        return place + " Click Wisp in the Dock for the full window."
    }
    var barCornerRadius: CGFloat {
        switch style {
        case .notch: return 10
        case .menuBar: return Self.edgeSeed.height / 2
        case .floating: return 14
        }
    }
    var panelCornerRadius: CGFloat { style == .notch ? 28 : 24 }
    /// SwiftUI content sits below the camera housing on a notch; a card just
    /// needs ordinary padding.
    var contentTopInset: CGFloat { style == .notch ? notchSize.height + 8 : 16 }
    var windowLevel: NSWindow.Level { style == .notch ? .statusBar : .floating }
    var maskedCorners: CACornerMask {
        style == .notch ? [.layerMinXMinYCorner, .layerMaxXMinYCorner]
                        : [.layerMinXMinYCorner, .layerMaxXMinYCorner,
                           .layerMinXMaxYCorner, .layerMaxXMaxYCorner]
    }
    var barSize: CGSize { barFrame.size }

    /// Whether the display has room for the wide panel.
    var canWiden: Bool { usableFrame.width - 2 * Self.screenMargin >= Self.minWideWidth }

    /// This layout with the wide panel's width when asked for and there is room
    /// for it, otherwise unchanged. Only the expanded width differs: the bar, its
    /// zone and the panel's centring on the display are the same.
    func widened(_ wide: Bool) -> SurfaceLayout {
        guard wide, canWiden else { return self }
        var copy = self
        copy.expandedWidth = min(Self.wideExpandedWidth, usableFrame.width - 2 * Self.screenMargin).rounded(.down)
        return copy
    }

    func shape(radius: CGFloat) -> UnevenRoundedRectangle {
        let top: CGFloat = style == .notch ? 0 : radius
        return UnevenRoundedRectangle(topLeadingRadius: top, bottomLeadingRadius: radius,
                                      bottomTrailingRadius: radius, topTrailingRadius: top)
    }
    var panelShape: UnevenRoundedRectangle { shape(radius: panelCornerRadius) }
    var barShape: UnevenRoundedRectangle { shape(radius: barCornerRadius) }

    /// Top of the expanded panel: the screen edge on a notch, just under the menu
    /// bar otherwise.
    private var panelTop: CGFloat {
        style == .notch ? screenFrame.maxY : usableFrame.maxY - Self.floatingGap
    }

    /// The tallest the expanded panel gets: from its top to a margin above the
    /// screen's bottom edge. 970 pt on a 14" MacBook Pro's default 982.
    var maxPanelHeight: CGFloat { max(panelTop - screenFrame.minY - Self.screenMargin, 0) }

    /// The panel's padding under its content, and above a tab's content under
    /// the header (OverlayView.expandedPanel uses both).
    static let panelBottomPadding: CGFloat = 16
    static let tabTopPadding: CGFloat = 12
    /// A scrolling tab never gets shorter than this, however small the display.
    static let minTabScroll: CGFloat = 160

    /// The tallest Brief's or Day's content gets before it scrolls: the panel's
    /// maximum less the content inset, the header and the padding, so the
    /// header stays put and nothing is cut off by the window's bottom edge.
    func tabScrollCap(headerHeight: CGFloat) -> CGFloat {
        let room = maxPanelHeight - contentTopInset - headerHeight.rounded(.up)
            - Self.tabTopPadding - Self.panelBottomPadding
        return max(room.rounded(.down), Self.minTabScroll)
    }

    /// Frame of the expanded panel for a given content height: centred on the
    /// anchor, hanging from the top, and never taller than the screen.
    func expandedFrame(contentHeight: CGFloat) -> CGRect {
        let top = panelTop
        let height = min(contentHeight, maxPanelHeight)
        return CGRect(x: (usableFrame.midX - expandedWidth / 2).rounded(),
                      y: (top - height).rounded(), width: expandedWidth, height: height)
    }

    /// The layout for one display. `forcing` overrides the style chosen from the
    /// hardware, for the debug override (see `DisplayGeometry.forcedStyle`).
    static func make(for metrics: DisplayMetrics, forcing forced: Style? = nil) -> SurfaceLayout {
        let frame = metrics.frame
        let width = min(maxExpandedWidth, max(frame.width - 2 * screenMargin, 320))
        let style = forced ?? (metrics.isBuiltIn ? (metrics.hasCameraHousing ? .notch : .menuBar) : .floating)
        switch style {
        case .notch:
            let notch = metrics.physicalNotch
                ?? CGSize(width: referenceNotch.width,
                          height: metrics.safeAreaTop > 0 ? metrics.safeAreaTop : referenceNotch.height)
            let barW = (notch.width + 2 * wing).rounded()
            let barH = notch.height.rounded()
            let bar = CGRect(x: (frame.midX - barW / 2).rounded(), y: frame.maxY - barH,
                             width: barW, height: barH)
            return SurfaceLayout(style: .notch, screenFrame: frame, usableFrame: frame, notchSize: notch,
                                 expandedWidth: width, barFrame: bar,
                                 hoverZone: notchZone(around: bar, top: frame.maxY))
        case .menuBar:
            // Under the menu bar, even while it is auto-hidden (visibleFrame then
            // reaches the top edge, and a card there would sit on the menu bar when
            // it slides back in).
            var usable = metrics.visibleFrame
            usable.size.height = min(usable.height, frame.maxY - metrics.menuBarHeight - usable.minY)
            let seed = edgeSeed
            let bar = CGRect(x: (usable.midX - seed.width / 2).rounded(),
                             y: (usable.maxY - floatingGap - seed.height).rounded(),
                             width: seed.width, height: seed.height)
            return SurfaceLayout(style: .menuBar, screenFrame: frame, usableFrame: usable, notchSize: .zero,
                                 expandedWidth: width, barFrame: bar,
                                 hoverZone: edgeZone(centredOn: frame.midX, top: frame.maxY))
        case .floating:
            let usable = metrics.visibleFrame
            let size = floatingBar
            let bar = CGRect(x: (usable.midX - size.width / 2).rounded(),
                             y: (usable.maxY - floatingGap - size.height).rounded(),
                             width: size.width, height: size.height)
            return SurfaceLayout(style: .floating, screenFrame: frame, usableFrame: usable, notchSize: .zero,
                                 expandedWidth: width, barFrame: bar,
                                 hoverZone: zone(around: bar, top: usable.maxY))
        }
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

    /// The menu bar's invisible zone: a thin strip along the top-centre edge,
    /// reaching one point past it for the same reason as the notch's.
    private static func edgeZone(centredOn midX: CGFloat, top: CGFloat) -> CGRect {
        CGRect(x: (midX - edgeZoneWidth / 2).rounded(), y: top - edgeZoneDepth,
               width: edgeZoneWidth, height: edgeZoneDepth + 1)
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

/// What the menu-bar icon shows when it stands in for the bar (.menuBar): the
/// same signals, in the same order of precedence, as the collapsed bar.
struct MenuBarStatus: Equatable {
    enum Mark: Equatable { case none, working, due(Int), notSetUp }
    let mark: Mark

    init(working: Bool, dueToday: Int, setupSetAside: Bool) {
        if working { mark = .working }
        else if !setupSetAside, dueToday > 0 { mark = .due(dueToday) }
        else if setupSetAside { mark = .notSetUp }
        else { mark = .none }
    }

    /// Text beside the icon.
    var title: String {
        switch mark {
        case .due(let n): return "\(n)"
        case .notSetUp: return "!"
        case .none, .working: return ""
        }
    }

    var accessibilityLabel: String {
        switch mark {
        case .none: return "Wisp"
        case .working: return "Wisp, working"
        case .due(1): return "Wisp, 1 thing is due today"
        case .due(let n): return "Wisp, \(n) things are due today"
        case .notSetUp: return "Wisp isn't set up yet"
        }
    }
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
        layout = Self.layout(for: displays())
    }

    /// Debug override for the style, so the menu-bar and floating styles can be
    /// tried on a notched MacBook (no notchless Mac needed):
    /// `defaults write <bundle id> wisp.debug.surfaceStyle menuBar` (or
    /// `floating`, `notch`), then relaunch. Delete the key to follow the hardware.
    nonisolated static let forcedStyleKey = "wisp.debug.surfaceStyle"
    nonisolated static var forcedStyle: SurfaceLayout.Style? {
        UserDefaults.standard.string(forKey: forcedStyleKey).flatMap(SurfaceLayout.Style.init(rawValue:))
    }

    private static func layout(for displays: [DisplayMetrics]) -> SurfaceLayout {
        SurfaceLayout.target(from: displays).map { SurfaceLayout.make(for: $0, forcing: forcedStyle) } ?? .fallback
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
        let next = Self.layout(for: displays)
        guard next != layout else { return }
        layout = next
        onChange()
    }
}
