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
//
// The hybrid notch: on a notched display, while a full-screen app, video or
// presentation is in front, the collapsed notch bar becomes an undrawn hitbox
// (see `CollapsedPresentation`) and its status moves to the menu-bar icon.

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
    let expandedWidth: CGFloat
    /// The collapsed window. On .menuBar it is never drawn and lets clicks
    /// through to the menu bar under it (see `barIsVisible`).
    let barFrame: CGRect
    /// Screen coordinates in which the pointer counts as "at the bar".
    let hoverZone: CGRect
    /// Whether the display has a camera housing (see `CollapsedPresentation`).
    var displayHasNotch = false
    /// Whether a full-screen app, video or presentation is in front on this
    /// display. Only ever set on a notched display (DisplayGeometry's watch).
    var frontmostFullScreen = false

    /// How the collapsed bar presents right now: drawn, or an undrawn hitbox.
    var presentation: CollapsedPresentation {
        CollapsedPresentation.decide(style: style, displayHasNotch: displayHasNotch,
                                     frontmostFullScreen: frontmostFullScreen)
    }
    /// Whether anything is drawn while collapsed. False on .menuBar, and on a
    /// notch behind a full-screen app; the collapsed window then also ignores
    /// the mouse, so the menu bar or the app under it keeps every click.
    var barIsVisible: Bool { presentation.mode == .bar }
    /// Whether the menu-bar icon carries the bar's status (working, ready, due
    /// today, not set up), because there is no visible bar to carry it.
    var statusInMenuBar: Bool { presentation.statusInMenuBar }
    /// How long the pointer must rest in the zone. Longer wherever nothing is
    /// drawn: on .menuBar the zone shares the top edge with whatever menu sits
    /// under the centre, and over a full-screen video a brush on the way to the
    /// hidden menu bar should not open Wisp on top of it.
    var hoverDwell: TimeInterval { barIsVisible ? HoverIntent.dwell : 0.2 }
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
    /// `frontmostFullScreen`: a full-screen app is in front on this display
    /// (only acted on for a notch; see `CollapsedPresentation`).
    static func make(for metrics: DisplayMetrics, forcing forced: Style? = nil,
                     frontmostFullScreen: Bool = false) -> SurfaceLayout {
        var layout = base(for: metrics, forcing: forced)
        layout.displayHasNotch = metrics.hasCameraHousing
        layout.frontmostFullScreen = frontmostFullScreen
        return layout
    }

    /// The style for a display: the debug override, else the hardware.
    static func style(for metrics: DisplayMetrics, forcing forced: Style? = nil) -> Style {
        forced ?? (metrics.isBuiltIn ? (metrics.hasCameraHousing ? .notch : .menuBar) : .floating)
    }

    private static func base(for metrics: DisplayMetrics, forcing forced: Style?) -> SurfaceLayout {
        let frame = metrics.frame
        let width = min(maxExpandedWidth, max(frame.width - 2 * screenMargin, 320))
        switch style(for: metrics, forcing: forced) {
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
    enum Mark: Equatable { case none, working, ready, due(Int), notSetUp }
    let mark: Mark

    /// `ready`: the bar's quiet "Wisp is ready" dot (OverlayView.notchBar).
    init(working: Bool, ready: Bool = false, dueToday: Int, setupSetAside: Bool) {
        if working { mark = .working }
        else if ready { mark = .ready }
        else if !setupSetAside, dueToday > 0 { mark = .due(dueToday) }
        else if setupSetAside { mark = .notSetUp }
        else { mark = .none }
    }

    /// Text beside the icon.
    var title: String {
        switch mark {
        case .due(let n): return "\(n)"
        case .ready: return "•"
        case .notSetUp: return "!"
        case .none, .working: return ""
        }
    }

    var accessibilityLabel: String {
        switch mark {
        case .none: return "Wisp"
        case .working: return "Wisp, working"
        case .ready: return "Wisp is ready"
        case .due(1): return "Wisp, 1 thing is due today"
        case .due(let n): return "Wisp, \(n) things are due today"
        case .notSetUp: return "Wisp isn't set up yet"
        }
    }
}

/// How the collapsed bar presents on its display: the hybrid notch.
///
/// - `.bar`: drawn, and it takes the mouse (the notch's black bar, a monitor's
///   capsule).
/// - `.hitbox`: nothing drawn and the window ignores the mouse, so every click
///   reaches the menu bar or the app under it; hover is detected from the
///   pointer's position (AppDelegate's global monitor and `hoverZone`), and
///   ⌥Space and the menu-bar icon still open Wisp. The bar's status marks move
///   to the menu-bar icon so they aren't lost.
///
/// A notch turns into a hitbox while a full-screen app, video or presentation
/// is in front on its display: the drawn bar there covered the app and took its
/// clicks. Built-in displays without a notch are always a hitbox (.menuBar);
/// monitors always keep their capsule.
struct CollapsedPresentation: Equatable {
    enum Mode: Equatable { case bar, hitbox }
    let mode: Mode
    let statusInMenuBar: Bool

    static func decide(style: SurfaceLayout.Style, displayHasNotch: Bool,
                       frontmostFullScreen: Bool) -> CollapsedPresentation {
        switch style {
        case .menuBar:
            return CollapsedPresentation(mode: .hitbox, statusInMenuBar: true)
        case .notch where displayHasNotch && frontmostFullScreen:
            return CollapsedPresentation(mode: .hitbox, statusInMenuBar: true)
        case .notch, .floating:
            return CollapsedPresentation(mode: .bar, statusInMenuBar: false)
        }
    }

    /// Whether full screen can change anything on this display, so whether it
    /// is worth watching for at all.
    static func watchesFullScreen(style: SurfaceLayout.Style, displayHasNotch: Bool) -> Bool {
        decide(style: style, displayHasNotch: displayHasNotch, frontmostFullScreen: true)
            != decide(style: style, displayHasNotch: displayHasNotch, frontmostFullScreen: false)
    }
}

/// One on-screen window, as CGWindowListCopyWindowInfo reports it. Bounds,
/// layer, owner and alpha need no Screen Recording permission (only other
/// apps' window TITLES do, and they are never read), and the call never
/// prompts for anything.
struct OnScreenWindow: Equatable {
    /// AppKit screen coordinates (bottom-left origin, y up).
    var frame: CGRect
    var layer: Int
    var ownerPID: Int32
    var alpha: Double = 1

    /// From one CGWindowList entry, whose bounds are Quartz coordinates (top-left
    /// origin of the primary display, y down). Nil when the entry is malformed.
    init?(info: [String: Any], primaryHeight: CGFloat) {
        guard let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
              let quartz = CGRect(dictionaryRepresentation: boundsDict as CFDictionary),
              let layer = (info[kCGWindowLayer as String] as? NSNumber)?.intValue,
              let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value else { return nil }
        self.frame = CGRect(x: quartz.minX, y: primaryHeight - quartz.maxY,
                            width: quartz.width, height: quartz.height)
        self.layer = layer
        self.ownerPID = pid
        self.alpha = (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1
    }

    init(frame: CGRect, layer: Int = 0, ownerPID: Int32, alpha: Double = 1) {
        self.frame = frame
        self.layer = layer
        self.ownerPID = ownerPID
        self.alpha = alpha
    }
}

/// Whether the frontmost space on a display is full screen, from public facts
/// only: the on-screen windows and the display's visible frame.
enum FullScreenProbe {
    /// How far a window edge may sit from the display's edge and still meet it.
    static let edgeSlack: CGFloat = 2
    /// Split View's two windows sit a divider apart.
    static let splitGap: CGFloat = 16

    /// The menu bar is hidden on the display: a full-screen space (or the menu
    /// bar set to hide automatically, which `userHidesMenuBar` rules out).
    /// UNVERIFIED LIVE: that a native full-screen Space on a notched Mac reports
    /// its visible frame up to the top edge. If it doesn't, the bar simply stays
    /// drawn there, as before the hybrid notch.
    static func menuBarHidden(on display: DisplayMetrics) -> Bool {
        display.visibleFrame.maxY >= display.frame.maxY - 0.5
    }

    /// The global-domain default behind "Automatically hide and show the menu
    /// bar": true for Always and On Desktop Only. Read only, never written; it
    /// needs no permission.
    static let userHidesMenuBarKey = "_HIHideMenuBar"

    /// Whether `globalDefaults` (NSGlobalDomain's values) hide the menu bar on
    /// the desktop. Absent or malformed reads as not hidden.
    static func userHidesMenuBar(globalDefaults: [String: Any]?) -> Bool {
        (globalDefaults?[userHidesMenuBarKey] as? NSNumber)?.boolValue ?? false
    }

    /// `windows` front to back, as CGWindowList lists them.
    ///
    /// Full screen when either:
    /// - the frontmost normal window on the display covers all of it, top edge
    ///   included (a player's or game's own full-screen window), or
    /// - the menu bar is hidden there and normal windows reaching from the
    ///   camera strip down to the bottom edge span its whole width (a native
    ///   full-screen app, video or presentation, or Split View's pair; on a
    ///   notched display those sit just under the camera housing).
    /// A zoomed window under a visible menu bar is never full screen.
    ///
    /// `userHidesMenuBar`: the person set the menu bar to hide on the desktop
    /// (System Settings › Control Center › Automatically hide and show the menu
    /// bar: Always or On Desktop Only). Then a maximized window on the desktop
    /// looks exactly like full screen, and a hitbox there would move the bar's
    /// marks to a menu-bar icon that is itself hidden. So nothing counts as
    /// full screen, and the bar keeps its marks.
    static func isFullScreen(on display: DisplayMetrics, windows: [OnScreenWindow], ownPID: Int32,
                             userHidesMenuBar: Bool = false) -> Bool {
        guard !userHidesMenuBar else { return false }
        let screen = display.frame
        let normal = windows
            .filter { $0.layer == 0 && $0.ownerPID != ownPID && $0.alpha > 0.01 }
            .map { $0.frame.intersection(screen) }
            .filter { !$0.isNull && $0.width > 1 && $0.height > 1 }
        guard let front = normal.first else { return false }
        if reachesSides(front, screen) && front.minY <= screen.minY + edgeSlack
            && front.maxY >= screen.maxY - edgeSlack { return true }
        guard menuBarHidden(on: display) else { return false }
        let strip = max(display.safeAreaTop, SurfaceLayout.standardMenuBarHeight) + 16
        let tall = normal.filter { $0.minY <= screen.minY + edgeSlack && $0.maxY >= screen.maxY - strip }
        return spansWidth(tall, screen)
    }

    private static func reachesSides(_ r: CGRect, _ screen: CGRect) -> Bool {
        r.minX <= screen.minX + edgeSlack && r.maxX >= screen.maxX - edgeSlack
    }

    private static func spansWidth(_ rects: [CGRect], _ screen: CGRect) -> Bool {
        var reach = screen.minX
        var first = true
        for r in rects.sorted(by: { $0.minX < $1.minX }) {
            if r.minX > reach + (first ? edgeSlack : splitGap) { return false }
            reach = max(reach, r.maxX)
            first = false
        }
        return !first && reach >= screen.maxX - edgeSlack
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
    /// Called after `layout`'s geometry changed (panels re-fit themselves).
    var onChange: () -> Void = {}
    /// Called after ONLY the collapsed bar's presentation changed (a full-screen
    /// app came to the front on the notched display, or left it). The bar's
    /// frame is the same either way, so panels only re-decide whether they take
    /// the mouse; nothing in flight is cancelled.
    var onPresentationChange: () -> Void = {}

    // The hybrid notch's full-screen watch. See `checkFullScreen`.
    private var displays: [DisplayMetrics]
    private var frontmostFullScreen = false
    private var workspaceObservers: [NSObjectProtocol] = []
    private var fullScreenChecks: [DispatchWorkItem] = []
    private var fullScreenPoll: Timer?
    /// Injectable for fixtures; production reads the window list.
    var fullScreenProbe: (DisplayMetrics) -> Bool = { DisplayGeometry.probeFullScreen(on: $0) }

    init(displays: @autoclosure () -> [DisplayMetrics] = DisplayGeometry.currentDisplays()) {
        let current = displays()
        self.displays = current
        layout = Self.layout(for: current, fullScreen: false)
    }

    /// Debug override for the style, so the menu-bar and floating styles can be
    /// tried on a notched MacBook (no notchless Mac needed):
    /// `defaults write <bundle id> wisp.debug.surfaceStyle menuBar` (or
    /// `floating`, `notch`), then relaunch. Delete the key to follow the hardware.
    nonisolated static let forcedStyleKey = "wisp.debug.surfaceStyle"
    nonisolated static var forcedStyle: SurfaceLayout.Style? {
        UserDefaults.standard.string(forKey: forcedStyleKey).flatMap(SurfaceLayout.Style.init(rawValue:))
    }

    private static func layout(for displays: [DisplayMetrics], fullScreen: Bool) -> SurfaceLayout {
        SurfaceLayout.target(from: displays).map {
            SurfaceLayout.make(for: $0, forcing: forcedStyle, frontmostFullScreen: fullScreen)
        } ?? .fallback
    }

    nonisolated static func currentDisplays() -> [DisplayMetrics] {
        NSScreen.screens.map(DisplayMetrics.init(screen:))
    }

    /// How many workspace events the full-screen watch listens to (for checks:
    /// a second `startObserving` must not add more).
    var watchedEventCount: Int { workspaceObservers.count }

    /// Safe to call more than once: only the first call installs anything.
    func startObserving() {
        guard observer == nil, workspaceObservers.isEmpty else { return }
        observer = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            // Displays often report several changes in a burst while waking or
            // hot-plugging; settle on the last one.
            MainActor.assumeIsolated { self?.scheduleRefresh() }
        }
        // Everything that can bring a full-screen space to the front of the
        // notched display, or take it away: entering or leaving full screen
        // and switching Spaces (both change the active space), an app coming
        // forward, launching, hiding or quitting, and waking. A display
        // attached or removed arrives through the screen notification above.
        let center = NSWorkspace.shared.notificationCenter
        let names: [Notification.Name] = [
            NSWorkspace.activeSpaceDidChangeNotification,
            NSWorkspace.didActivateApplicationNotification,
            NSWorkspace.didLaunchApplicationNotification,
            NSWorkspace.didTerminateApplicationNotification,
            NSWorkspace.didHideApplicationNotification,
            NSWorkspace.didUnhideApplicationNotification,
            NSWorkspace.didWakeNotification,
            NSWorkspace.screensDidWakeNotification,
        ]
        workspaceObservers = names.map { name in
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.scheduleFullScreenCheck() }
            }
        }
        updateFullScreenPoll()
        checkFullScreen()
    }

    // MARK: - Full screen (the hybrid notch)

    /// When a full-screen check runs after an event: at once, then twice more
    /// while the Space-switch animation settles (windows and the screen's
    /// visible frame move during it). A newer event replaces pending checks.
    static let settleDelays: [TimeInterval] = [0.05, 0.45, 1.1]
    /// The safety net's period, for what no notification announces: a player's
    /// or game's own borderless full-screen window, or a missed event. One
    /// window-list read (about a millisecond), coalesced by a wide tolerance,
    /// and only while a notched display hosts Wisp.
    static let pollInterval: TimeInterval = 3

    /// Whether the target display's bar can change with full screen at all.
    /// Only a notched display's can; nothing is watched otherwise.
    private var watchesFullScreen: Bool {
        guard let target = SurfaceLayout.target(from: displays) else { return false }
        return CollapsedPresentation.watchesFullScreen(
            style: SurfaceLayout.style(for: target, forcing: Self.forcedStyle),
            displayHasNotch: target.hasCameraHousing)
    }

    private func scheduleFullScreenCheck() {
        fullScreenChecks.forEach { $0.cancel() }
        fullScreenChecks = Self.settleDelays.map { delay in
            let work = DispatchWorkItem { [weak self] in
                MainActor.assumeIsolated { self?.checkFullScreen() }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
            return work
        }
    }

    private func updateFullScreenPoll() {
        guard workspaceObservers.isEmpty == false, watchesFullScreen else {
            fullScreenPoll?.invalidate()
            fullScreenPoll = nil
            return
        }
        guard fullScreenPoll == nil else { return }
        let timer = Timer(timeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.checkFullScreen() }
        }
        timer.tolerance = 1
        RunLoop.main.add(timer, forMode: .common)
        fullScreenPoll = timer
    }

    /// Look again whether a full-screen app is in front on the notched display,
    /// and switch the bar between drawn and hitbox if that changed. Reads the
    /// display fresh, since its visible frame follows the menu bar.
    func checkFullScreen() {
        guard watchesFullScreen, let target = SurfaceLayout.target(from: displays) else {
            return setFullScreen(false)
        }
        let live = Self.currentDisplays().first { $0.frame == target.frame } ?? target
        setFullScreen(fullScreenProbe(live))
    }

    private func setFullScreen(_ value: Bool) {
        guard value != frontmostFullScreen else { return }
        frontmostFullScreen = value
        let next = Self.layout(for: displays, fullScreen: value)
        guard next != layout else { return }
        layout = next
        onPresentationChange()
    }

    /// The live window list, public API only: CGWindowListCopyWindowInfo's
    /// bounds, layer, owner and alpha need no Screen Recording permission and
    /// never prompt. Window titles, which would, are never read.
    static func probeFullScreen(on display: DisplayMetrics) -> Bool {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                                    kCGNullWindowID) as? [[String: Any]] else { return false }
        // Quartz's origin is the top-left of the primary display (the one at 0,0).
        let primary = NSScreen.screens.first { $0.frame.origin == .zero } ?? NSScreen.screens.first
        let primaryHeight = primary?.frame.height ?? display.frame.height
        let windows = list.compactMap { OnScreenWindow(info: $0, primaryHeight: primaryHeight) }
        let global = UserDefaults.standard.persistentDomain(forName: UserDefaults.globalDomain)
        return FullScreenProbe.isFullScreen(on: display, windows: windows,
                                            ownPID: ProcessInfo.processInfo.processIdentifier,
                                            userHidesMenuBar: FullScreenProbe.userHidesMenuBar(globalDefaults: global))
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
        self.displays = displays
        updateFullScreenPoll()
        // A display attached or removed may change which display Wisp is on, or
        // whether a full-screen app is in front of it: look now, and again as
        // the change settles.
        if !watchesFullScreen {
            frontmostFullScreen = false
        } else if !workspaceObservers.isEmpty, let target = SurfaceLayout.target(from: displays) {
            frontmostFullScreen = fullScreenProbe(target)
            scheduleFullScreenCheck()
        }
        let next = Self.layout(for: displays, fullScreen: frontmostFullScreen)
        guard next != layout else { return }
        var sameGeometry = next
        sameGeometry.frontmostFullScreen = layout.frontmostFullScreen
        let geometryChanged = sameGeometry != layout
        layout = next
        if geometryChanged { onChange() } else { onPresentationChange() }
    }
}
