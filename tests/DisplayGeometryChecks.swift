import AppKit
import SwiftUI

// Pure layout checks: no screen, window, or Wisp process is involved. Display
// shapes are modelled on the real MacBook and monitor point sizes.
@main
struct DisplayGeometryChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    /// A built-in display with a physical notch (aux areas flank the housing).
    static func notched(_ w: CGFloat, _ h: CGFloat, notch: CGFloat = 185, top: CGFloat = 32,
                        origin: CGPoint = .zero) -> DisplayMetrics {
        let side = (w - notch) / 2
        return DisplayMetrics(frame: CGRect(x: origin.x, y: origin.y, width: w, height: h),
                              visibleFrame: CGRect(x: origin.x, y: origin.y, width: w, height: h - top),
                              safeAreaTop: top, auxLeftWidth: side, auxRightWidth: side, isBuiltIn: true)
    }

    /// A built-in display with no notch (M1 Air, 13" Pro, iMac): 24 pt menu bar,
    /// or none reported while the menu bar auto-hides.
    static func notchless(_ w: CGFloat, _ h: CGFloat, menuBar: CGFloat = 24) -> DisplayMetrics {
        DisplayMetrics(frame: CGRect(x: 0, y: 0, width: w, height: h),
                       visibleFrame: CGRect(x: 0, y: 0, width: w, height: h - menuBar), isBuiltIn: true)
    }

    static func monitor(_ w: CGFloat, _ h: CGFloat, origin: CGPoint = .zero) -> DisplayMetrics {
        DisplayMetrics(frame: CGRect(x: origin.x, y: origin.y, width: w, height: h),
                       visibleFrame: CGRect(x: origin.x, y: origin.y, width: w, height: h - 24), isBuiltIn: false)
    }

    static func checkLayout(_ name: String, _ d: DisplayMetrics, style: SurfaceLayout.Style,
                            forcing: SurfaceLayout.Style? = nil) {
        let l = SurfaceLayout.make(for: d, forcing: forcing)
        check(l.style == style, "\(name): style")
        // The bar is centred on its display and touches the top of its anchor area.
        check(abs(l.barFrame.midX - l.usableFrame.midX) <= 1, "\(name): bar centred")
        check(l.barFrame.maxY <= l.usableFrame.maxY + 0.5, "\(name): bar inside the display")
        check(d.frame.contains(l.barFrame), "\(name): bar on screen")
        // The hover zone is easier to hit than the bar. On a notch it is wider than the
        // bar, covers the top of the notch and the screen's top edge, and leaves out the
        // notch's bottom strip; on a monitor it is the bar plus a margin.
        check(l.hoverZone.width > l.barFrame.width, "\(name): zone is wider than the bar")
        check(l.hoverZone.minX < l.barFrame.minX && l.hoverZone.maxX > l.barFrame.maxX, "\(name): zone reaches past both sides")
        check(l.barIsVisible == (style != .menuBar) && l.statusInMenuBar == (style == .menuBar),
              "\(name): only the menu-bar style hides the bar and moves its status to the icon")
        if style == .notch {
            hoverZoneChecks(name, l, d)
        } else if style == .menuBar {
            edgeZoneChecks(name, l, d)
        } else {
            check(l.hoverZone.contains(l.barFrame), "\(name): zone contains the capsule")
            check(l.hoverZone.height > l.barFrame.height, "\(name): zone is forgiving")
        }
        // Expanded panel: centred, inside the screen, whatever the content height.
        for h in [120, 300, 640, 4000] as [CGFloat] {
            let f = l.expandedFrame(contentHeight: h)
            check(d.frame.contains(f), "\(name): expanded \(h) on screen")
            check(abs(f.midX - l.usableFrame.midX) <= 1, "\(name): expanded centred")
            check(f.width == l.expandedWidth && l.expandedWidth <= 640, "\(name): width")
            if style == .notch {
                check(abs(f.maxY - d.frame.maxY) < 0.5, "\(name): panel hangs from the top edge")
            } else {
                check(f.maxY < d.visibleFrame.maxY, "\(name): floating card sits below the menu bar")
                check(f.maxY <= d.frame.maxY - 24, "\(name): card clears even an auto-hidden menu bar")
            }
        }
        check(l.expandedFrame(contentHeight: 4000).height <= d.frame.height - SurfaceLayout.screenMargin, "\(name): tall content clamped")
    }

    static func main() {
        // Notch MacBooks, M1 Pro through M5: each model's point size, incl. scaled modes.
        checkLayout("MBP 14", notched(1512, 982), style: .notch)
        checkLayout("MBP 14 more space", notched(1800, 1169), style: .notch)
        checkLayout("MBP 14 larger text", notched(1352, 878), style: .notch)
        checkLayout("MBP 16", notched(1728, 1117, notch: 185), style: .notch)
        checkLayout("MBP 16 more space", notched(2056, 1329), style: .notch)
        checkLayout("Air 13.6", notched(1470, 956), style: .notch)
        checkLayout("Air 15.3", notched(1710, 1107), style: .notch)
        checkLayout("Air 13.6 larger text", notched(1280, 832), style: .notch)

        let real = SurfaceLayout.make(for: notched(1512, 982))
        tabScrollCapChecks()

        // Built-in displays without a notch (M1 Air, 13"/M2 13" Pro, iMac 24"): nothing
        // drawn; the menu-bar icon and an invisible top-edge zone stand in. Default
        // and scaled point sizes, plus a hidden (auto-hide) menu bar.
        for (n, d) in [("M1 Air", notchless(1440, 900)), ("M1 Air more space", notchless(1680, 1050)),
                       ("M1 Air larger text", notchless(1280, 800)), ("13in Pro", notchless(1440, 900)),
                       ("iMac 24", notchless(2240, 1260)), ("iMac 24 more space", notchless(2560, 1440)),
                       ("M1 Air menu bar hidden", notchless(1440, 900, menuBar: 0))] {
            checkLayout(n, d, style: .menuBar)
            let l = SurfaceLayout.make(for: d)
            check(l.notchSize == .zero && l.contentTopInset == 16, "\(n): no notch inset")
            check(l.barFrame.maxY <= d.frame.maxY - 24, "\(n): the undrawn seed sits under the menu bar")
            check(l.maskedCorners.contains(.layerMaxXMaxYCorner), "\(n): card rounds all four corners")
            check(l.hoverDwell > HoverIntent.dwell, "\(n): a longer dwell, since menus share the edge")
            check(l.windowLevel == .floating, "\(n): not above the menu bar")
        }
        check(SurfaceLayout.make(for: notched(1512, 982)).hoverDwell == HoverIntent.dwell, "Notch keeps its dwell")

        check(real.notchSize == CGSize(width: 185, height: 32), "Reads the physical notch")
        check(real.barFrame.width == 185 + 2 * SurfaceLayout.wing, "Bar wraps the notch")
        check(real.barFrame.height == 32, "Bar covers the full notch height")
        check(SurfaceLayout.make(for: notched(1512, 982, top: 34)).barFrame.height == 34, "Reads a taller notch")

        // A camera housing with implausible side areas is still a notch: standard
        // width, its real height.
        var odd = notched(1512, 982, top: 34)
        odd.auxLeftWidth = 0; odd.auxRightWidth = 0   // width would be the whole screen
        let oddLayout = SurfaceLayout.make(for: odd)
        check(oddLayout.style == .notch, "Odd notch stays a notch")
        check(oddLayout.notchSize == CGSize(width: SurfaceLayout.referenceNotch.width, height: 34), "Odd notch width falls back")

        // The debug override puts any style on any display (how the menu-bar style is
        // tried on a notched MacBook).
        checkLayout("MBP 14 forced menu bar", notched(1512, 982), style: .menuBar, forcing: .menuBar)
        checkLayout("M1 Air forced notch", notchless(1440, 900), style: .notch, forcing: .notch)
        checkLayout("MBP 16 forced floating", notched(1728, 1117), style: .floating, forcing: .floating)
        check(SurfaceLayout.Style(rawValue: "menuBar") == .menuBar && SurfaceLayout.Style(rawValue: "virtual") == nil,
              "Override values are the style names")

        // First-run tip names the right place.
        check(real.firstRunTip.contains("notch"), "Notch tip names the notch")
        let air = SurfaceLayout.make(for: notchless(1440, 900))
        check(!air.firstRunTip.contains("notch") && air.firstRunTip.contains("menu bar"), "Menu-bar tip never mentions a notch")

        // Monitors: floating, no notch.
        for (n, m) in [("1080p", monitor(1920, 1080)), ("1440p", monitor(2560, 1440)),
                       ("4K scaled", monitor(3008, 1692)), ("ultrawide", monitor(3440, 1440)),
                       ("small", monitor(1366, 768)), ("right of laptop", monitor(2560, 1440, origin: CGPoint(x: 1512, y: 0))),
                       ("above laptop", monitor(1920, 1080, origin: CGPoint(x: 0, y: 982)))] {
            checkLayout("monitor \(n)", m, style: .floating)
            let l = SurfaceLayout.make(for: m)
            check(l.notchSize == .zero && l.contentTopInset == 16, "monitor \(n): no notch inset")
            check(l.barFrame.maxY < m.visibleFrame.maxY, "monitor \(n): capsule detached from the menu bar")
            check(l.panelCornerRadius > 0 && l.barCornerRadius > 0, "monitor \(n): rounded")
        }
        check(SurfaceLayout.make(for: monitor(1920, 1080)).maskedCorners.contains(.layerMaxXMaxYCorner), "Floating card rounds all four corners")
        check(!SurfaceLayout.make(for: notched(1512, 982)).maskedCorners.contains(.layerMaxXMaxYCorner), "Notch panel keeps a flat top")

        // Which display hosts Wisp.
        let builtIn = notched(1512, 982), ext = monitor(2560, 1440, origin: CGPoint(x: 1512, y: 0))
        check(SurfaceLayout.target(from: [ext, builtIn]) == builtIn, "MacBook screen wins over a monitor")
        check(SurfaceLayout.target(from: [ext]) == ext, "Clamshell: the monitor hosts Wisp")
        check(SurfaceLayout.target(from: []) == nil, "No displays → fallback")
        check(SurfaceLayout.fallback.style == .notch, "Fallback is the standard notch")

        // Narrow screens never get a panel wider than the screen.
        check(SurfaceLayout.make(for: monitor(500, 700)).expandedWidth <= 500, "Narrow display clamp")

        hoverChecks()
        statusChecks()
        tabScrollSourceChecks()
        hybridNotchChecks()
        fullScreenProbeChecks()
        widePanelChecks()
        print("PASS: display geometry — 8 notched MacBooks, 7 notchless built-in displays, 7 monitor placements, overrides, target selection, hover intent, menu-bar status, Brief/Day scroll cap, hybrid notch, full-screen probe, wide Brief panel")
    }

    /// The hybrid notch: a notch behind a full-screen app becomes an undrawn
    /// hitbox with its status on the menu-bar icon; nothing else changes.
    static func hybridNotchChecks() {
        typealias P = CollapsedPresentation
        let bar = P(mode: .bar, statusInMenuBar: false), hitbox = P(mode: .hitbox, statusInMenuBar: true)
        check(P.decide(style: .notch, displayHasNotch: true, frontmostFullScreen: false) == bar, "Desktop notch: the drawn bar")
        check(P.decide(style: .notch, displayHasNotch: true, frontmostFullScreen: true) == hitbox, "Full-screen notch: hitbox")
        check(P.decide(style: .menuBar, displayHasNotch: false, frontmostFullScreen: false) == hitbox, "Notchless: hitbox as before")
        check(P.decide(style: .menuBar, displayHasNotch: false, frontmostFullScreen: true) == hitbox, "Notchless in full screen: unchanged")
        check(P.decide(style: .floating, displayHasNotch: false, frontmostFullScreen: false) == bar, "Monitor: capsule")
        check(P.decide(style: .floating, displayHasNotch: false, frontmostFullScreen: true) == bar, "Monitor in full screen: unchanged")
        // The debug override's forced notch on a notchless display keeps its bar.
        check(P.decide(style: .notch, displayHasNotch: false, frontmostFullScreen: true) == bar, "Forced notch without a notch: unchanged")
        // A notched MacBook forced to another style follows that style.
        check(P.decide(style: .menuBar, displayHasNotch: true, frontmostFullScreen: true) == hitbox, "Forced menu bar on a notch")
        check(P.decide(style: .floating, displayHasNotch: true, frontmostFullScreen: true) == bar, "Forced floating on a notch")
        // Only a real notch is watched at all.
        check(P.watchesFullScreen(style: .notch, displayHasNotch: true), "A notch is watched")
        check(!P.watchesFullScreen(style: .notch, displayHasNotch: false), "A forced notch is not")
        check(!P.watchesFullScreen(style: .menuBar, displayHasNotch: false), "Notchless is not watched")
        check(!P.watchesFullScreen(style: .floating, displayHasNotch: false), "A monitor is not watched")

        // Through the layout: same frame and hover zone, only presentation changes.
        for d in [notched(1512, 982), notched(1728, 1117), notched(1470, 956)] {
            let desk = SurfaceLayout.make(for: d)
            let full = SurfaceLayout.make(for: d, frontmostFullScreen: true)
            check(desk.barIsVisible && !desk.statusInMenuBar, "Desktop notch draws its bar, keeps its status")
            check(!full.barIsVisible && full.statusInMenuBar, "Full-screen notch draws nothing, status on the icon")
            check(full.barFrame == desk.barFrame && full.hoverZone == desk.hoverZone, "Hitbox keeps the notch's frame and zone")
            check(full.style == .notch && full.windowLevel == desk.windowLevel, "Still the notch style")
            check(full.hoverDwell > HoverIntent.dwell && desk.hoverDwell == HoverIntent.dwell,
                  "A longer dwell over a full-screen app")
            check(full != desk, "The change is visible to observers of the layout")
            check(full.firstRunTip == desk.firstRunTip, "Tip unchanged")
        }
        for d in [notchless(1440, 900), monitor(1920, 1080)] {
            check(SurfaceLayout.make(for: d, frontmostFullScreen: true).presentation
                  == SurfaceLayout.make(for: d).presentation, "Full screen changes nothing off the notch")
        }
    }

    /// Full-screen detection from the window list: bounds in, a yes or no out.
    static func fullScreenProbeChecks() {
        let me: Int32 = 99, app: Int32 = 500, other: Int32 = 501
        // 14" MacBook Pro: 1512 x 982, notch 32 pt; menu bar 32 pt on the desktop.
        let desk = notched(1512, 982)
        var full = desk
        full.visibleFrame = desk.frame                     // the menu bar is hidden
        func w(_ x: CGFloat, _ y: CGFloat, _ wd: CGFloat, _ h: CGFloat, pid: Int32 = 500,
               layer: Int = 0, alpha: Double = 1) -> OnScreenWindow {
            OnScreenWindow(frame: CGRect(x: x, y: y, width: wd, height: h), layer: layer, ownerPID: pid, alpha: alpha)
        }
        let belowNotch = w(0, 0, 1512, 950)                // a native full-screen window under the camera
        let whole = w(0, 0, 1512, 982)                     // a player's own full-screen window
        typealias F = FullScreenProbe
        check(F.isFullScreen(on: full, windows: [belowNotch], ownPID: me), "Native full screen under the notch")
        check(F.isFullScreen(on: full, windows: [whole], ownPID: me), "Native full screen over the notch")
        check(F.isFullScreen(on: desk, windows: [whole], ownPID: me), "A borderless window covering everything")
        // A zoomed window under a visible menu bar, Dock hidden or not, is the desktop.
        check(!F.isFullScreen(on: desk, windows: [belowNotch], ownPID: me), "Zoomed window, Dock hidden: desktop")
        check(!F.isFullScreen(on: desk, windows: [w(0, 70, 1512, 880)], ownPID: me), "Zoomed window above the Dock: desktop")
        check(!F.isFullScreen(on: desk, windows: [], ownPID: me), "No windows: desktop")
        check(!F.isFullScreen(on: full, windows: [], ownPID: me), "Hidden menu bar, no windows: not full screen")
        check(!F.isFullScreen(on: full, windows: [w(200, 100, 800, 600)], ownPID: me), "Hidden menu bar, small window")
        // Split View: two apps side by side with a divider.
        check(F.isFullScreen(on: full, windows: [w(0, 0, 752, 950, pid: app), w(760, 0, 752, 950, pid: other)], ownPID: me),
              "Split View counts")
        check(!F.isFullScreen(on: full, windows: [w(0, 0, 600, 950), w(900, 0, 612, 950)], ownPID: me),
              "Two windows with a wide gap do not")
        // A small front window (a full-screen app's own panel) over the full-screen window.
        check(F.isFullScreen(on: full, windows: [w(500, 300, 400, 300), belowNotch], ownPID: me), "Panel over full screen")
        // But on the desktop only the FRONT window counts for the borderless case.
        check(!F.isFullScreen(on: desk, windows: [w(500, 300, 400, 300), whole], ownPID: me),
              "A covering window behind another on the desktop does not")
        // Wisp's own windows, other layers and invisible windows never count.
        check(!F.isFullScreen(on: desk, windows: [w(0, 0, 1512, 982, pid: me)], ownPID: me), "Wisp's own window")
        check(!F.isFullScreen(on: desk, windows: [w(0, 0, 1512, 982, layer: 25)], ownPID: me), "Status-bar level")
        check(!F.isFullScreen(on: desk, windows: [w(0, 0, 1512, 982, alpha: 0)], ownPID: me), "A transparent window")
        // Per display: a full-screen window on the monitor says nothing about the laptop.
        let laptop = notched(1512, 982)
        check(!F.isFullScreen(on: laptop, windows: [w(1512, 0, 2560, 1440)], ownPID: me), "Full screen on another display")
        var mon = monitor(2560, 1440, origin: CGPoint(x: 1512, y: 0))
        mon.visibleFrame = mon.frame
        check(F.isFullScreen(on: mon, windows: [w(1512, 0, 2560, 1440)], ownPID: me), "Probe works on any display's frame")

        // CGWindowList entries: Quartz bounds (top-left origin) to AppKit.
        let info: [String: Any] = [
            kCGWindowBounds as String: CGRect(x: 0, y: 32, width: 1512, height: 950).dictionaryRepresentation,
            kCGWindowLayer as String: NSNumber(value: 0),
            kCGWindowOwnerPID as String: NSNumber(value: Int32(500)),
            kCGWindowAlpha as String: NSNumber(value: 1.0),
        ]
        let parsed = OnScreenWindow(info: info, primaryHeight: 982)
        check(parsed?.frame == CGRect(x: 0, y: 0, width: 1512, height: 950), "Quartz bounds flip to AppKit")
        check(parsed?.layer == 0 && parsed?.ownerPID == 500 && parsed?.alpha == 1, "Layer, owner and alpha read")
        check(OnScreenWindow(info: [kCGWindowLayer as String: NSNumber(value: 0)], primaryHeight: 982) == nil,
              "A malformed entry is skipped")
        // A monitor above the laptop: Quartz y is negative there.
        let above = OnScreenWindow(info: [
            kCGWindowBounds as String: CGRect(x: 0, y: -1080, width: 1920, height: 1080).dictionaryRepresentation,
            kCGWindowLayer as String: NSNumber(value: 0), kCGWindowOwnerPID as String: NSNumber(value: Int32(1)),
        ], primaryHeight: 982)
        check(above?.frame == CGRect(x: 0, y: 982, width: 1920, height: 1080) && above?.alpha == 1, "Above the primary display")

        // The live wiring, read from source: the panel and view follow one flag,
        // and the watch reacts to every event that can change the answer.
        let src = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("app/Sources/WispApp")
        guard let geometry = try? String(contentsOf: src.appendingPathComponent("DisplayGeometry.swift"), encoding: .utf8),
              let panel = try? String(contentsOf: src.appendingPathComponent("OverlayPanel.swift"), encoding: .utf8),
              let view = try? String(contentsOf: src.appendingPathComponent("OverlayView.swift"), encoding: .utf8),
              let app = try? String(contentsOf: src.appendingPathComponent("AppDelegate.swift"), encoding: .utf8)
        else { preconditionFailure("hybrid notch: read the sources") }
        for event in ["activeSpaceDidChangeNotification", "didActivateApplicationNotification",
                      "didTerminateApplicationNotification", "didHideApplicationNotification",
                      "didUnhideApplicationNotification", "didWakeNotification", "didChangeScreenParametersNotification"] {
            check(geometry.contains(event), "The full-screen watch hears \(event)")
        }
        check(!geometry.contains("kCGWindowName"), "Window titles (Screen Recording) are never read")
        check(!geometry.contains("CGRequestScreenCaptureAccess") && !geometry.contains("CGPreflightScreenCaptureAccess"),
              "Nothing asks for Screen Recording")
        check(panel.contains("ignoresMouseEvents = Self.ignoresMouse(collapsed: collapsed, layout: layout)")
              && panel.contains("collapsed && !layout.barIsVisible"), "The window follows barIsVisible")
        check(view.contains("if layout.barIsVisible { notchBar } else { hiddenBar }"), "The view follows barIsVisible")
        check(view.contains(".animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: layout.barIsVisible)"),
              "The fade respects Reduce Motion")
        check(app.contains("DisplayGeometry.shared.onPresentationChange = { [weak self] in self?.barPresentationChanged() }"),
              "The app applies presentation changes")
        // The icon's ready mark and the bar's ready dot read the same condition, so
        // they clear together once the panel is opened (readyLineSeen → .hidden).
        check(app.contains("ready: model.setupCard.showsReadyDot,"), "The icon carries the ready dot")
        check(view.contains("} else if model.setupCard.showsReadyDot {"), "The bar's dot reads the same condition")
        check(app.contains("if model.setupCard == .ready { model.setupCard = .hidden }"), "Opening the panel clears it")
        check(NotchSetupCard.ready.showsReadyDot, "Ready shows the dot")
        for card in [NotchSetupCard.hidden, .open, .later] {
            check(!card.showsReadyDot, "\(card) shows no ready dot")
            check(MenuBarStatus(working: false, ready: card.showsReadyDot, dueToday: 0, setupSetAside: card == .later).mark
                  != .ready, "\(card): the icon's ready mark is gone")
        }

        // A menu bar the person hides on the desktop is not full screen: the bar
        // stays drawn and keeps its marks (the icon would be hidden too).
        check(!F.isFullScreen(on: full, windows: [belowNotch], ownPID: me, userHidesMenuBar: true),
              "Auto-hidden menu bar, maximized window: not full screen")
        check(!F.isFullScreen(on: full, windows: [whole], ownPID: me, userHidesMenuBar: true),
              "Auto-hidden menu bar, window to the top: not full screen")
        check(F.isFullScreen(on: full, windows: [belowNotch], ownPID: me, userHidesMenuBar: false), "Setting off: detected")
        check(F.userHidesMenuBar(globalDefaults: ["_HIHideMenuBar": NSNumber(value: true)]), "Hide Always reads as hidden")
        check(!F.userHidesMenuBar(globalDefaults: ["_HIHideMenuBar": NSNumber(value: false)]), "Never reads as shown")
        check(!F.userHidesMenuBar(globalDefaults: [:]) && !F.userHidesMenuBar(globalDefaults: nil), "Absent reads as shown")
        check(!F.userHidesMenuBar(globalDefaults: ["_HIHideMenuBar": "yes"]), "Malformed reads as shown")
        check(geometry.contains("userHidesMenuBar: FullScreenProbe.userHidesMenuBar(globalDefaults: global)"),
              "The live probe passes the setting")

        MainActor.assumeIsolated { liveWatchChecks() }
    }

    /// The real call path: an injected probe answers, `checkFullScreen` runs, and
    /// the layout, the panel's click-through rule and the change callbacks all
    /// follow it, on and off again.
    @MainActor
    static func liveWatchChecks() {
        let laptop = notched(1512, 982)
        let geometry = DisplayGeometry(displays: [laptop])
        var fullScreen = false
        var presentationChanges = 0, geometryChanges = 0
        geometry.fullScreenProbe = { _ in fullScreen }
        // Native, when the harness has a graphical session; never ordered on screen.
        var panel: OverlayPanel?
        if !NSScreen.screens.isEmpty {
            _ = NSApplication.shared
            let p = OverlayPanel { EmptyView() }
            p.layoutProvider = { geometry.layout }
            p.isCompact = { true }
            p.applyPresentationChange()
            panel = p
        }
        // Wired as AppDelegate wires them: a re-fit, or only the mouse flag.
        geometry.onChange = {
            geometryChanges += 1
            panel?.applyDisplayChange()
        }
        geometry.onPresentationChange = {
            presentationChanges += 1
            panel?.applyPresentationChange()
        }
        // Starting the watch twice installs its observers once. (Real workspace
        // observers; the harness runs no run loop, so they and the poll stay idle.)
        geometry.startObserving()
        let watched = geometry.watchedEventCount
        check(watched >= 8, "The watch hears its events (\(watched))")
        geometry.startObserving()
        check(geometry.watchedEventCount == watched, "A second start adds nothing")
        func expect(barVisible: Bool, _ step: String) {
            let l = geometry.layout
            check(l.barIsVisible == barVisible, "\(step): bar drawn = \(barVisible)")
            check(l.statusInMenuBar == !barVisible, "\(step): status on the icon = \(!barVisible)")
            // Collapsed: clicks pass through exactly when nothing is drawn. Open: never.
            check(OverlayPanel.ignoresMouse(collapsed: true, layout: l) == !barVisible, "\(step): collapsed click-through")
            check(!OverlayPanel.ignoresMouse(collapsed: false, layout: l), "\(step): an open panel takes the mouse")
            if let panel {
                check(panel.ignoresMouseEvents == !barVisible, "\(step): the real panel's ignoresMouseEvents")
            }
        }
        expect(barVisible: true, "Start")
        fullScreen = true
        geometry.checkFullScreen()
        expect(barVisible: false, "Full screen on")
        check(presentationChanges == 1 && geometryChanges == 0, "A presentation change, not a re-fit")
        geometry.checkFullScreen()
        check(presentationChanges == 1, "No change, no callback")
        fullScreen = false
        geometry.checkFullScreen()
        expect(barVisible: true, "Full screen off")
        check(presentationChanges == 2 && geometryChanges == 0, "Back to the bar, again without a re-fit")
        // A display change re-probes: full screen on the laptop at the moment it changes.
        fullScreen = true
        geometry.refresh(displays: [laptop, monitor(2560, 1440, origin: CGPoint(x: 1512, y: 0))])
        expect(barVisible: false, "Monitor attached during full screen")
        fullScreen = false
        geometry.refresh(displays: [laptop])
        expect(barVisible: true, "Monitor removed after leaving full screen")
        // Clamshell: Wisp moves to the monitor, which is never watched, so the
        // probe's answer no longer matters and nothing stays a hitbox.
        fullScreen = true
        geometry.refresh(displays: [monitor(2560, 1440)])
        check(geometry.layout.style == .floating && geometry.layout.barIsVisible, "A monitor keeps its capsule")
        geometry.checkFullScreen()
        check(geometry.layout.barIsVisible, "A monitor ignores full screen")
        geometry.refresh(displays: [laptop])
        geometry.checkFullScreen()
        expect(barVisible: false, "Back on the laptop, full screen is picked up")
        fullScreen = false
        geometry.checkFullScreen()
        expect(barVisible: true, "And dropped")
        print("Live watch: \(panel == nil ? "rule only (no graphical session)" : "rule and a real panel")")
    }

    /// 2026-10-27, 14" MacBook Pro: the Brief clipped at the bottom with no way to
    /// scroll. The panel stops 12 pt above the screen's bottom, so Brief and Day
    /// scroll inside what is left under the header and padding.
    static func tabScrollCapChecks() {
        let header: CGFloat = 30
        let insets = SurfaceLayout.tabTopPadding + SurfaceLayout.panelBottomPadding
        for (name, d, room) in [("MBP 14", notched(1512, 982), 970), ("MBP 14 larger text", notched(1352, 878), 866),
                                ("MBP 16", notched(1728, 1117), 1105)] as [(String, DisplayMetrics, CGFloat)] {
            let l = SurfaceLayout.make(for: d)
            check(l.maxPanelHeight == room, "\(name): panel room \(l.maxPanelHeight)")
            check(l.expandedFrame(contentHeight: 4000).height == l.maxPanelHeight, "\(name): the window stops at the room")
            let cap = l.tabScrollCap(headerHeight: header)
            // 970 − (32 + 8) inset − 30 header − 12 − 16 = 872 on the 14".
            check(cap == room - l.contentTopInset - header - insets, "\(name): cap \(cap)")
            // A panel holding a full cap fits on screen exactly: nothing is cut off.
            check(l.contentTopInset + header + insets + cap <= l.maxPanelHeight, "\(name): capped panel fits")
            // A taller header (larger Text size) shortens the scroll, never the header.
            check(l.tabScrollCap(headerHeight: header + 10) == cap - 10, "\(name): header grows, scroll shrinks")
            check(l.tabScrollCap(headerHeight: 30.4) == l.tabScrollCap(headerHeight: 31), "\(name): header rounds up")
        }
        check(SurfaceLayout.make(for: notched(1512, 982)).tabScrollCap(headerHeight: 30) == 872, "14in cap is 872 pt")
        check(SurfaceLayout.make(for: notched(1352, 878)).tabScrollCap(headerHeight: 30) == 768, "14in larger-text cap is 768 pt")
        // Cards hang under the menu bar and a gap.
        let air = SurfaceLayout.make(for: notchless(1440, 900))
        check(air.maxPanelHeight == 900 - 24 - SurfaceLayout.floatingGap - SurfaceLayout.screenMargin, "Menu-bar card room")
        let screen = SurfaceLayout.make(for: monitor(1920, 1080))
        check(screen.maxPanelHeight == 1080 - 24 - SurfaceLayout.floatingGap - SurfaceLayout.screenMargin, "Monitor card room")
        // A tiny display still gets a usable scroll rather than none.
        check(SurfaceLayout.make(for: monitor(800, 200)).tabScrollCap(headerHeight: header) == SurfaceLayout.minTabScroll,
              "Floor on a tiny display")
    }

    /// The overlay wraps Brief and Day, and only them, in the capped scroll.
    static func tabScrollSourceChecks() {
        let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("app/Sources/WispApp")
        guard let overlay = try? String(contentsOf: app.appendingPathComponent("OverlayView.swift"), encoding: .utf8),
              let cards = try? String(contentsOf: app.appendingPathComponent("ProactiveCards.swift"), encoding: .utf8)
        else { preconditionFailure("tab scroll: read the overlay's source") }
        check(overlay.contains("case .brief:\n                tabScroll { NotchBriefView(model: model, today: todayModel, wide: briefIsWide) }"),
              "Brief sits in the tab scroll")
        check(overlay.contains("case .day:\n                tabScroll { NotchDayView(model: model, today: todayModel) }"),
              "Day sits in the tab scroll")
        check(overlay.components(separatedBy: "tabScroll {").count - 1 == 2, "only Brief and Day scroll as a tab")
        check(overlay.contains("CappedScroll(cap: layout.tabScrollCap(headerHeight: headerHeight), reveals: true)"),
              "the tab scroll is capped from the display's geometry")
        check(overlay.contains("bottom: SurfaceLayout.panelBottomPadding"), "the cap and the panel share one bottom padding")
        // The header is outside the scroll, so it stays put.
        let panel = overlay.components(separatedBy: "private var expandedPanel: some View {")[1]
        check(panel.hasPrefix("\n        VStack(alignment: .leading, spacing: 0) {\n            header\n"), "header stays fixed")
        // Fits: no indicator, no bounce. Composer and draft scroll into sight, unanimated.
        check(cards.contains(".scrollIndicators(height > cap ? .automatic : .never)"), "no indicator while it fits")
        check(cards.contains(".scrollBounceBehavior(.basedOnSize)"), "no bounce while it fits")
        check(cards.contains("replyArea(thread).revealInPanel("), "the reply composer scrolls into sight")
        check(cards.contains("draftEditor(card, action, fire: fire, withSend: sendInDraft).revealInPanel("),
              "an opened draft scrolls into sight")
        check(!cards.contains("withAnimation { proxy.scrollTo") && cards.contains("{ proxy.scrollTo(id) }"),
              "revealing never animates")
    }

    /// The Brief's two-column view: a wider expanded panel, still centred under the
    /// notch or card anchor, only where the display has the room. Nothing else about
    /// the layout moves.
    static func widePanelChecks() {
        let wide = SurfaceLayout.wideExpandedWidth
        check(wide == 840 && SurfaceLayout.minWideWidth == 800 && SurfaceLayout.minWideWidth < wide, "wide panel constants")
        // Notch, menu-bar and floating layouts all widen, scaled modes included.
        for (n, d, style) in [("MBP 14", notched(1512, 982), .notch),
                              ("MBP 14 larger text", notched(1352, 878), .notch),
                              ("M1 Air", notchless(1440, 900), .menuBar),
                              ("monitor", monitor(1920, 1080), .floating),
                              ("monitor right of laptop", monitor(2560, 1440, origin: CGPoint(x: 1512, y: 0)), .floating)]
            as [(String, DisplayMetrics, SurfaceLayout.Style)] {
            let l = SurfaceLayout.make(for: d)
            let w = l.widened(true)
            check(l.style == style && l.canWiden && w.canWiden, "\(n): has room for the wide panel")
            check(l.expandedWidth == 640 && w.expandedWidth == wide, "\(n): widens 640 → \(wide)")
            // Only the expanded width differs: the bar, its zone and the anchor stay put.
            var narrowed = w; narrowed.expandedWidth = l.expandedWidth
            check(narrowed == l, "\(n): widening changes nothing but the expanded width")
            check(w.maxPanelHeight == l.maxPanelHeight && w.contentTopInset == l.contentTopInset, "\(n): same room and inset")
            for h in [120, 640, 4000] as [CGFloat] {
                let f = w.expandedFrame(contentHeight: h)
                check(f.width == wide && d.frame.contains(f), "\(n): wide panel \(h) on screen")
                check(abs(f.midX - l.usableFrame.midX) <= 1, "\(n): wide panel centred on the anchor")
                check(f.maxY == l.expandedFrame(contentHeight: h).maxY, "\(n): wide panel hangs from the same top")
            }
            check(l.widened(false) == l, "\(n): widened(false) is unchanged")
            check(w.widened(true) == w, "\(n): widening twice is the same as once")
        }
        let notch = SurfaceLayout.make(for: notched(1512, 982))
        check(abs(notch.widened(true).expandedFrame(contentHeight: 300).midX - 756) <= 1, "Notch: wide panel centred on the display")
        // A display without the room stays at its normal width, whatever is asked.
        for (n, d) in [("small monitor", monitor(820, 700)), ("narrow", monitor(500, 700)),
                       ("just short", monitor(823, 700))] {
            let l = SurfaceLayout.make(for: d)
            check(!l.canWiden, "\(n): too narrow to widen")
            check(l.widened(true) == l && l.widened(false) == l, "\(n): keeps its normal width")
        }
        // The edge: usable width less the screen margins must reach 800.
        let edge = SurfaceLayout.make(for: monitor(824, 700)).widened(true)
        check(edge.expandedWidth == 800, "At the edge the panel fills the margins' room")
        check(SurfaceLayout.make(for: monitor(840.5, 700)).widened(true).expandedWidth == 816, "Narrower than 840 rounds down to the room")
        check(SurfaceLayout.make(for: monitor(860, 700)).widened(true).expandedWidth == 836, "A display a little wider than 840 fits within its margins")
        // Two columns only on the proactive Brief with nothing over it.
        check(NotchBriefView.canShowWide(tab: .brief, covered: false, engineBrief: true, canWiden: true),
              "the proactive Brief can show two columns")
        check(!NotchBriefView.canShowWide(tab: .day, covered: false, engineBrief: true, canWiden: true)
              && !NotchBriefView.canShowWide(tab: .ask, covered: false, engineBrief: true, canWiden: true),
              "Day and Ask stay one panel wide")
        check(!NotchBriefView.canShowWide(tab: .brief, covered: true, engineBrief: true, canWiden: true),
              "Quick settings or a card over the Brief: one column")
        check(!NotchBriefView.canShowWide(tab: .brief, covered: false, engineBrief: false, canWiden: true),
              "loading, the tiles Brief or another day: one column")
        check(!NotchBriefView.canShowWide(tab: .brief, covered: false, engineBrief: true, canWiden: false),
              "no room on the display: one column")
        // The window and the content widen from the same state; the search panel never does.
        let app = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("app/Sources/WispApp")
        guard let overlay = try? String(contentsOf: app.appendingPathComponent("OverlayView.swift"), encoding: .utf8),
              let panel = try? String(contentsOf: app.appendingPathComponent("OverlayPanel.swift"), encoding: .utf8),
              let delegate = try? String(contentsOf: app.appendingPathComponent("AppDelegate.swift"), encoding: .utf8)
        else { preconditionFailure("wide panel: read the overlay's source") }
        check(overlay.contains(".frame(width: layout.widened(briefIsWide).expandedWidth)"),
              "the content widens with the Brief's two columns")
        check(delegate.contains("layout.widened(NotchBriefView.showsWide(model: self.model, today: self.todayModel,"),
              "the notch window widens from the same state")
        check(panel.contains("var layoutProvider: () -> SurfaceLayout = { MainActor.assumeIsolated { DisplayGeometry.shared.layout } }"),
              "a panel's default layout (the search panel's) is never widened")
    }

    /// The notch's hover zone: most of the notch, never its bottom strip, and the top edge.
    static func hoverZoneChecks(_ name: String, _ l: SurfaceLayout, _ d: DisplayMetrics) {
        let bar = l.barFrame, zone = l.hoverZone
        let midX = bar.midX
        // THE regression: macOS pins a pointer pushed to the top of the screen at exactly
        // frame.maxY, and CGRect.contains treats its max edge as outside.
        check(zone.contains(CGPoint(x: midX, y: d.frame.maxY)), "\(name): pointer pinned to the top edge opens")
        check(zone.contains(CGPoint(x: bar.minX + 1, y: d.frame.maxY)) && zone.contains(CGPoint(x: bar.maxX - 1, y: d.frame.maxY)),
              "\(name): top edge opens across the whole notch")
        // Most of the notch: everything above the bottom strip.
        let strip = bar.height * SurfaceLayout.notchHoverBottomExclusion
        for fraction in [0.3, 0.5, 0.75, 0.99] as [CGFloat] {
            check(zone.contains(CGPoint(x: midX, y: bar.minY + bar.height * fraction)), "\(name): \(fraction) up the notch opens")
        }
        check(zone.contains(CGPoint(x: bar.minX, y: bar.maxY - 2)) && zone.contains(CGPoint(x: bar.maxX, y: bar.maxY - 2)),
              "\(name): the notch's side edges open")
        // The bottom strip, and everything under the notch, does not.
        check(!zone.contains(CGPoint(x: midX, y: bar.minY + strip - 0.5)), "\(name): bottom strip does not open")
        check(!zone.contains(CGPoint(x: midX, y: bar.minY)), "\(name): the notch's bottom edge does not open")
        check(!zone.contains(CGPoint(x: midX, y: bar.minY - 1)) && !zone.contains(CGPoint(x: midX, y: bar.minY - 12)),
              "\(name): nothing below the notch opens")
        check(zone.minY >= bar.minY, "\(name): the zone never extends below the notch")
        check(zone.maxY > d.frame.maxY, "\(name): the zone reaches past the screen's top edge")
        // Wider than before (8 pt each side), without reaching the menu bar's own items.
        check(zone.minX <= bar.minX - 16 && zone.maxX >= bar.maxX + 16, "\(name): generous sideways reach")
        check(zone.width <= bar.width + 64, "\(name): not so wide it hijacks menu bar items")
        check(zone.contains(CGPoint(x: bar.minX - 10, y: d.frame.maxY - 4)), "\(name): just beside the notch opens")
        check(!zone.contains(CGPoint(x: bar.minX - 60, y: d.frame.maxY - 4)), "\(name): well away from the notch does not")
        check(!zone.contains(CGPoint(x: bar.maxX + 60, y: d.frame.maxY - 4)), "\(name): well away (right) does not")
        // Strictly bigger usable target than the old zone over the part that matters.
        let oldTop = CGRect(x: bar.minX - 8, y: bar.minY - 6, width: bar.width + 16, height: bar.height + 6)
        check(zone.width > oldTop.width, "\(name): wider than the old zone")
    }

    /// The invisible top-edge zone: hit by a push against the top-centre, never by
    /// hovering a menu at the menu bar's middle or anything under it.
    static func edgeZoneChecks(_ name: String, _ l: SurfaceLayout, _ d: DisplayMetrics) {
        let zone = l.hoverZone, top = d.frame.maxY, midX = d.frame.midX
        check(zone.contains(CGPoint(x: midX, y: top)), "\(name): pointer pinned to the top edge opens")
        check(zone.contains(CGPoint(x: midX - 100, y: top)) && zone.contains(CGPoint(x: midX + 100, y: top)),
              "\(name): the whole top-centre edge opens")
        check(zone.contains(CGPoint(x: midX, y: top - 3)), "\(name): just under the edge opens")
        check(!zone.contains(CGPoint(x: midX, y: top - 12)), "\(name): a menu hovered at the menu bar's middle does not")
        check(!zone.contains(CGPoint(x: midX, y: top - 30)), "\(name): nothing under the menu bar opens")
        check(!zone.contains(CGPoint(x: midX - 160, y: top)) && !zone.contains(CGPoint(x: midX + 160, y: top)),
              "\(name): the edge away from the centre does not")
        check(zone.maxY > top, "\(name): the zone reaches past the screen's top edge")
        check(abs(zone.midX - midX) <= 1, "\(name): zone centred on the display")
    }

    static func statusChecks() {
        // Same precedence as the collapsed bar: working, then due today, then not set up.
        check(MenuBarStatus(working: false, dueToday: 0, setupSetAside: false).mark == .none, "Idle: plain icon")
        check(MenuBarStatus(working: false, dueToday: 0, setupSetAside: false).title.isEmpty, "Idle: no text")
        check(MenuBarStatus(working: true, dueToday: 3, setupSetAside: true).mark == .working, "Working wins")
        check(MenuBarStatus(working: true, dueToday: 3, setupSetAside: false).title.isEmpty, "Working shows no count")
        check(MenuBarStatus(working: false, dueToday: 3, setupSetAside: false).title == "3", "Due count shows")
        check(MenuBarStatus(working: false, dueToday: 3, setupSetAside: true).title == "!", "Not set up hides the count, like the bar")
        check(MenuBarStatus(working: false, dueToday: 0, setupSetAside: true).mark == .notSetUp, "Not set up")
        check(MenuBarStatus(working: false, dueToday: 1, setupSetAside: false).accessibilityLabel == "Wisp, 1 thing is due today",
              "Singular label")
        check(MenuBarStatus(working: false, dueToday: 2, setupSetAside: false).accessibilityLabel == "Wisp, 2 things are due today",
              "Plural label")
        // The bar's ready dot: after working, before the due count, like the bar.
        check(MenuBarStatus(working: false, ready: true, dueToday: 3, setupSetAside: false).mark == .ready, "Ready shows")
        check(MenuBarStatus(working: true, ready: true, dueToday: 0, setupSetAside: false).mark == .working, "Working beats ready")
        check(MenuBarStatus(working: false, ready: true, dueToday: 0, setupSetAside: false).title == "•", "Ready is a dot")
        check(MenuBarStatus(working: false, ready: true, dueToday: 0, setupSetAside: false).accessibilityLabel == "Wisp is ready",
              "Ready label")
    }

    static func hoverChecks() {
        var h = HoverIntent()
        // Brushing through: enter, then leave before the dwell elapses.
        check(h.pointerMoved(inZone: true, buttonsDown: false, collapsed: true) == .beginDwell, "Entering starts a dwell")
        check(h.pointerMoved(inZone: false, buttonsDown: false, collapsed: true) == .cancelDwell, "Leaving cancels the dwell")
        check(!h.shouldOpen(inZone: false, buttonsDown: false, collapsed: true), "Not opened after leaving")
        // Dragging something past the notch must not open it.
        check(h.pointerMoved(inZone: true, buttonsDown: true, collapsed: true) == .none, "Held button never begins a dwell")
        check(!h.shouldOpen(inZone: true, buttonsDown: true, collapsed: true), "Held button never opens")
        // Only a collapsed bar opens.
        check(h.pointerMoved(inZone: true, buttonsDown: false, collapsed: false) == .none, "Open panel ignores the zone")
        // After a close with the pointer still resting in the zone, it must leave first.
        h.didCollapse(pointerInZone: true)
        check(h.pointerMoved(inZone: true, buttonsDown: false, collapsed: true) == .none, "No reopen under a resting pointer")
        check(!h.shouldOpen(inZone: true, buttonsDown: false, collapsed: true), "Disarmed until the pointer leaves")
        _ = h.pointerMoved(inZone: false, buttonsDown: false, collapsed: true)
        check(h.pointerMoved(inZone: true, buttonsDown: false, collapsed: true) == .beginDwell, "Re-arms after leaving once")
        check(h.shouldOpen(inZone: true, buttonsDown: false, collapsed: true), "Opens after a real re-entry")
        var away = HoverIntent()
        away.didCollapse(pointerInZone: false)
        check(away.pointerMoved(inZone: true, buttonsDown: false, collapsed: true) == .beginDwell, "Closing elsewhere stays armed")
    }
}
