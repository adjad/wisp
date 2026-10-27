import AppKit

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
        print("PASS: display geometry — 8 notched MacBooks, 7 notchless built-in displays, 7 monitor placements, overrides, target selection, hover intent, menu-bar status")
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
