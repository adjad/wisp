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

    /// A built-in display with no notch (M1 Air, 13" Pro): 24 pt menu bar.
    static func notchless(_ w: CGFloat, _ h: CGFloat) -> DisplayMetrics {
        DisplayMetrics(frame: CGRect(x: 0, y: 0, width: w, height: h),
                       visibleFrame: CGRect(x: 0, y: 0, width: w, height: h - 24), isBuiltIn: true)
    }

    static func monitor(_ w: CGFloat, _ h: CGFloat, origin: CGPoint = .zero) -> DisplayMetrics {
        DisplayMetrics(frame: CGRect(x: origin.x, y: origin.y, width: w, height: h),
                       visibleFrame: CGRect(x: origin.x, y: origin.y, width: w, height: h - 24), isBuiltIn: false)
    }

    static func checkLayout(_ name: String, _ d: DisplayMetrics, style: SurfaceLayout.Style, virtual: Bool = false) {
        let l = SurfaceLayout.make(for: d)
        check(l.style == style, "\(name): style")
        check(l.isVirtualNotch == virtual, "\(name): virtual flag")
        // The bar is centred on its display and touches the top of its anchor area.
        check(abs(l.barFrame.midX - l.usableFrame.midX) <= 1, "\(name): bar centred")
        check(l.barFrame.maxY <= l.usableFrame.maxY + 0.5, "\(name): bar inside the display")
        check(d.frame.contains(l.barFrame), "\(name): bar on screen")
        // The hover zone always contains the bar and is easier to hit than it.
        check(l.hoverZone.contains(l.barFrame), "\(name): zone contains bar")
        check(l.hoverZone.width > l.barFrame.width && l.hoverZone.height > l.barFrame.height, "\(name): zone is forgiving")
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

        // Notchless MacBooks (M1 Air, 13" Pro): same notch, drawn virtually.
        checkLayout("M1 Air", notchless(1440, 900), style: .notch, virtual: true)
        checkLayout("M1 Air more space", notchless(1680, 1050), style: .notch, virtual: true)
        checkLayout("13in Pro", notchless(1280, 800), style: .notch, virtual: true)
        let virtual = SurfaceLayout.make(for: notchless(1440, 900))
        let real = SurfaceLayout.make(for: notched(1512, 982))
        check(virtual.barSize == real.barSize && virtual.contentTopInset == real.contentTopInset,
              "Virtual and physical notch must be the same size, so every MacBook looks alike")
        check(real.notchSize == CGSize(width: 185, height: 32), "Reads the physical notch")
        check(real.barFrame.width == 185 + 2 * SurfaceLayout.wing, "Bar wraps the notch")
        check(real.barFrame.height == 32, "Bar covers the full notch height")

        // Implausible reports fall back to the standard notch rather than a bad one.
        var odd = notched(1512, 982)
        odd.auxLeftWidth = 0; odd.auxRightWidth = 0   // width would be the whole screen
        check(SurfaceLayout.make(for: odd).notchSize == SurfaceLayout.referenceNotch, "Odd notch width falls back")
        check(SurfaceLayout.make(for: odd).isVirtualNotch, "…and is treated as virtual")

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
        print("PASS: display geometry — 8 notched MacBooks, 3 notchless MacBooks, 7 monitor placements, target selection, hover intent")
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
