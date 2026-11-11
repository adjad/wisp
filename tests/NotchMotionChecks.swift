import SwiftUI
import AppKit
import QuartzCore

/// Frame-by-frame evidence for the notch panel's in-panel motion: the REAL
/// OverlayView inside the REAL OverlayPanel, kept far off-screen, sampled about
/// every 16 ms through each page change. Per sample it records the window's
/// height and where the content is actually drawn to (the bottom of the opaque
/// surface in the host's bitmap), then asserts:
///   (a) with motion: the content is never clipped by the window;
///   (b) with motion: no single sample moves the content bottom by more than
///       45 % of the whole change (no snap);
///   (c) with motion: the window ends at the content's final height;
///   Reduce Motion: the change has landed (window and content) by the first
///       sample after it.
/// Writes one contact sheet per transition and a per-sample report. No
/// AppDelegate, no service (network is denied by the script), no visible window.
@main
struct NotchMotionChecks {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--print-real-home") {
            print(FileManager.default.homeDirectoryForCurrentUser.path)
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        Task { @MainActor in
            do {
                let ok = try await Harness.runAll()
                exit(ok ? 0 : 1)
            } catch {
                fputs("FAIL: \(error)\n", stderr)
                exit(1)
            }
        }
        app.run()
    }
}

struct MotionFailure: Error, CustomStringConvertible { let description: String }

struct MotionSample {
    var t: Double          // seconds since the change
    var win: Double        // window frame height, points
    var vis: Double        // bottom of the drawn surface, points from the top
    var image: CGImage?
    var scale: Double      // image pixels per point
    var imageHeight: Double
}

@MainActor
final class MotionRig {
    let reduce: Bool
    let model = OverlayModel()
    let today: TodayModel
    let settings = SettingsLoader()
    let setup = SetupGuideModel()
    let panel: OverlayPanel
    let base: SurfaceLayout
    var host: NSView!

    static let offscreenShift: CGFloat = -20000

    init(reduce: Bool) throws {
        self.reduce = reduce
        var calendar = Calendar(identifier: .gregorian)
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        calendar.timeZone = zone
        let midnight = calendar.startOfDay(for: Date())
        let now = midnight.addingTimeInterval(10 * 3600 + 32 * 60)
        let dayStart = midnight.timeIntervalSince1970
        today = TodayModel(now: { now }, timeZoneProvider: { zone }, transport: { request in
            let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: true) }
            let ok = { (body: Data) in
                (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }
            if request.httpMethod == "POST" { return ok(Data(#"{"ok":true,"detail":"Done"}"#.utf8)) }
            if components?.path == "/assistant/daily_summary/latest" {
                return (Data(#"{"detail":"no composed summary for that day"}"#.utf8),
                        HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
            }
            if components?.path == "/assistant/upcoming" { return ok(try NotchQADriver.fixtureUpcoming(dayStart: dayStart)) }
            let day = components?.queryItems?.first { $0.name == "day" }?.value ?? ""
            return ok(try NotchQADriver.fixturePlan(day: day, dayStart: dayStart))
        })

        // The real main display's metrics, moved far to the left: the same
        // widths as DisplayGeometry.shared.layout (which the view reads), a
        // window nobody can see.
        guard let target = SurfaceLayout.target(from: DisplayGeometry.currentDisplays()) else {
            throw MotionFailure(description: "no display metrics")
        }
        var shifted = target
        shifted.frame = target.frame.offsetBy(dx: Self.offscreenShift, dy: 0)
        shifted.visibleFrame = target.visibleFrame.offsetBy(dx: Self.offscreenShift, dy: 0)
        base = SurfaceLayout.make(for: shifted, forcing: DisplayGeometry.forcedStyle)

        let client = WispClient()
        let model = self.model, today = self.today, settings = self.settings, setup = self.setup
        let reduce = reduce
        panel = OverlayPanel {
            OverlayView(model: model,
                        corpus: NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client))),
                        researchModel: ResearchModel(client: client), todayModel: today,
                        settings: settings, setup: setup, onClose: {})
                .environment(\.colorScheme, .dark)
                .environment(\.reduceMotionOverride, reduce)
        }
        let base = self.base
        panel.isCompact = { false }
        panel.reduceMotion = { reduce }
        panel.layoutProvider = {
            MainActor.assumeIsolated {
                base.widened(NotchBriefView.showsWide(model: model, today: today, canWiden: base.canWiden))
            }
        }
        model.onResize = { [weak panel] in DispatchQueue.main.async { panel?.resizeToFit() } }
        model.onResizeNow = { [weak panel] in panel?.resizeToFit() }
        host = Self.findHost(in: panel.contentView!)
        guard host != nil else { throw MotionFailure(description: "no hosting view in the panel") }
    }

    static func findHost(in view: NSView) -> NSView? {
        if String(describing: type(of: view)).contains("NSHostingView") { return view }
        for sub in view.subviews { if let found = findHost(in: sub) { return found } }
        return nil
    }

    func open() async throws {
        await today.refresh()
        panel.present()
        panel.revealsWithoutKey = true
        var opened = false
        panel.dropOpen { opened = true }
        for _ in 0..<200 where !opened { try await Task.sleep(for: .milliseconds(10)) }
        guard opened else { throw MotionFailure(description: "panel never finished opening") }
        panel.resizeToFit()
        // Never visible: the whole frame must be left of any screen.
        guard panel.frame.maxX < -1000 else {
            panel.orderOut(nil)
            throw MotionFailure(description: "panel is not off-screen: \(panel.frame)")
        }
        try await settle()
    }

    func close() { panel.orderOut(nil); panel.close() }

    // MARK: Measuring

    /// The window's height and the bottom of the drawn surface, now.
    func capture() -> MotionSample {
        let win = Double(panel.frame.height)
        let bounds = host.bounds
        let h = min(Double(bounds.height), win + 120)
        let rect = host.isFlipped
            ? NSRect(x: 0, y: 0, width: bounds.width, height: h)
            : NSRect(x: 0, y: bounds.height - h, width: bounds.width, height: h)
        guard let rep = host.bitmapImageRepForCachingDisplay(in: rect) else {
            return MotionSample(t: 0, win: win, vis: -1, image: nil, scale: 1, imageHeight: h)
        }
        host.cacheDisplay(in: rect, to: rep)
        let scale = Double(rep.pixelsHigh) / h
        let vis = Self.surfaceBottom(rep) / scale
        return MotionSample(t: 0, win: win, vis: vis, image: rep.cgImage, scale: scale, imageHeight: h)
    }

    /// Rows from the top of the centre column that are opaque (alpha > 0.9).
    static func surfaceBottom(_ rep: NSBitmapImageRep) -> Double {
        let x = rep.pixelsWide / 2
        var rows = 0
        for y in 0..<rep.pixelsHigh {
            var alpha: CGFloat = 0
            if let data = rep.bitmapData, rep.samplesPerPixel == 4, rep.bitsPerPixel == 32 {
                let offset = rep.bitmapFormat.contains(.alphaFirst) ? 0 : 3
                alpha = CGFloat(data[y * rep.bytesPerRow + x * 4 + offset]) / 255
            } else {
                alpha = rep.colorAt(x: x, y: y)?.alphaComponent ?? 0
            }
            if alpha > 0.9 { rows += 1 } else { break }
        }
        return Double(rows)
    }

    /// Waits until the window and the drawn surface have held still for 300 ms.
    func settle() async throws {
        var last = capture()
        var calm = 0
        for _ in 0..<200 {
            try await Task.sleep(for: .milliseconds(50))
            let now = capture()
            if abs(now.win - last.win) < 0.5, abs(now.vis - last.vis) < 0.5, !panel.hasPendingShrink { calm += 1 } else { calm = 0 }
            last = now
            if calm >= 6 { return }
        }
        throw MotionFailure(description: "panel never came to rest (window \(last.win), content \(last.vis))")
    }
}

struct TransitionResult {
    var name: String
    var ok: Bool
    var lines: [String]
}

@MainActor
enum Harness {
    static func runAll() async throws -> Bool {
        guard let outPath = ProcessInfo.processInfo.environment["WISP_NOTCH_MOTION_OUT"] else {
            throw MotionFailure(description: "WISP_NOTCH_MOTION_OUT is not set")
        }
        let out = URL(fileURLWithPath: outPath)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        guard let target = SurfaceLayout.target(from: DisplayGeometry.currentDisplays()) else {
            throw MotionFailure(description: "no display metrics")
        }
        let real = SurfaceLayout.make(for: target, forcing: DisplayGeometry.forcedStyle)
        var report = "Notch motion report\n"
        report += "display style: \(real.style.rawValue), notch: \(target.hasCameraHousing), expandedWidth \(real.expandedWidth)\n"
        print("display style \(real.style.rawValue) (notch: \(target.hasCameraHousing)), expandedWidth \(real.expandedWidth)")
        if !target.hasCameraHousing { print("note: no notch on this display; motion is measured on the \(real.style.rawValue) layout") }
        let shared = DisplayGeometry.shared.layout
        guard shared.expandedWidth == real.expandedWidth else {
            throw MotionFailure(description: "layout widths differ: view \(shared.expandedWidth) vs window \(real.expandedWidth)")
        }

        var failures = 0
        for reduce in [false, true] {
            let rig = try MotionRig(reduce: reduce)
            guard rig.base.expandedWidth == shared.expandedWidth else {
                throw MotionFailure(description: "offscreen layout width \(rig.base.expandedWidth) != \(shared.expandedWidth)")
            }
            try await rig.open()
            let mode = reduce ? "reduce-motion" : "motion"
            report += "\n===== \(mode) =====\n"
            print("--- \(mode) ---")

            func run(_ name: String, prepare: (() -> Void)? = nil, _ change: @escaping () -> Void) async throws {
                if let prepare { prepare(); try await rig.settle() }
                let result = try await transition(rig, name: name, reduce: reduce, change: change, out: out)
                report += result.lines.joined(separator: "\n") + "\n"
                print("\(result.ok ? "PASS" : "FAIL") \(mode): \(name) - \(result.lines.first ?? "")")
                if !result.ok {
                    failures += 1
                    for line in result.lines.dropFirst() where line.hasPrefix("  !") { print(line) }
                }
            }
            let m = rig.model
            try await run("ask-to-brief", prepare: { m.tab = .ask }) { m.tab = .brief }
            try await run("brief-to-day") { m.tab = .day }
            try await run("day-to-ask") { m.tab = .ask }
            try await run("day-to-brief-backwards", prepare: { m.tab = .day }) { m.tab = .brief }
            try await run("gear-open") { m.showingQuickSettings = true }
            try await run("permissions-open") {
                m.openPermissionPage(nil)
                m.permissionPage.access.stop()
                m.permissionPage.access.qaShow(states: NotchQAPages.shot, asking: [], waiting: [], messagesReadable: true)
            }
            try await run("permissions-done") { m.closePermissionPage() }
            try await run("gear-close") { m.showingQuickSettings = false }

            if !reduce {
                let line = try await scrollMemory(rig)
                report += line + "\n"
                print(line)
                if line.hasPrefix("FAIL") { failures += 1 }
            }
            rig.close()
        }

        try report.write(to: out.appendingPathComponent("motion-report.txt"), atomically: true, encoding: .utf8)
        print(failures == 0 ? "ALL PASS" : "\(failures) FAILED")
        return failures == 0
    }

    static func f(_ v: Double) -> String { String(format: "%.1f", v) }

    static func transition(_ rig: MotionRig, name: String, reduce: Bool, change: @escaping () -> Void,
                           out: URL) async throws -> TransitionResult {
        try await rig.settle()
        let before = rig.capture()
        let t0 = CACurrentMediaTime()
        change()
        var samples: [MotionSample] = []
        while true {
            try await Task.sleep(for: .milliseconds(16))
            let t = CACurrentMediaTime() - t0
            var s = rig.capture()
            s.t = t
            samples.append(s)
            if t >= 0.6 { break }
        }
        try await Task.sleep(for: .milliseconds(500))
        try await rig.settle()
        var final = rig.capture()
        final.t = CACurrentMediaTime() - t0

        let total = abs(final.vis - before.vis)
        var problems: [String] = []
        var maxClip = 0.0
        for s in samples { maxClip = max(maxClip, s.vis - s.win) }
        var steps: [Double] = []
        var previous = before.vis
        for s in samples { steps.append(abs(s.vis - previous)); previous = s.vis }
        let maxStep = steps.max() ?? 0
        var visibleTotal = f(total)

        if reduce {
            if let first = samples.first {
                if abs(first.vis - final.vis) > 1.5 { problems.append("first sample content \(f(first.vis)) is not at final \(f(final.vis))") }
                if abs(first.win - final.win) > 1.5 { problems.append("first sample window \(f(first.win)) is not at final \(f(final.win))") }
            }
            if maxClip > 1 { problems.append("content clipped by \(f(maxClip)) pt") }
        } else {
            if maxClip > 1 { problems.append("(a) clipped: content passes the window by \(f(maxClip)) pt") }
            if total > 20, maxStep > 0.45 * total {
                problems.append("(b) snap: one step moves \(f(maxStep)) pt of a \(f(total)) pt change (\(Int(maxStep / total * 100))%)")
            }
            if abs(final.win - final.vis) > 2 { problems.append("(c) window \(f(final.win)) != final content \(f(final.vis))") }
        }
        if total <= 20 { visibleTotal += " (small: snap check skipped)" }

        var lines = ["\(name) [\(reduce ? "reduce-motion" : "motion")] change \(f(before.vis)) -> \(f(final.vis)) = \(visibleTotal) pt, max step \(f(maxStep)), max overshoot past window \(f(max(maxClip, 0)))"]
        lines.append("  before: win \(f(before.win)) content \(f(before.vis))")
        for s in samples {
            lines.append(String(format: "  t=%4d ms  win %7.1f  content %7.1f%@", Int(s.t * 1000), s.win, s.vis,
                                s.vis > s.win + 1 ? "  CLIPPED" : ""))
        }
        lines.append("  final: win \(f(final.win)) content \(f(final.vis))")
        for p in problems { lines.append("  ! \(p)") }

        var picks: [(String, MotionSample)] = [("before", before)]
        var used = Set<Int>()
        for target in [20.0, 50, 90, 140, 210, 320] {
            if let i = samples.indices.min(by: { abs(samples[$0].t * 1000 - target) < abs(samples[$1].t * 1000 - target) }),
               used.insert(i).inserted {
                picks.append(("\(Int(samples[i].t * 1000)) ms", samples[i]))
            }
        }
        picks.append(("final", final))
        let file = out.appendingPathComponent(reduce ? "\(name)-reduce-motion.png" : "\(name).png")
        try contactSheet(title: "\(name)  \(reduce ? "(reduce motion)" : "(motion)")", frames: picks, to: file)
        return TransitionResult(name: name, ok: problems.isEmpty, lines: lines)
    }

    /// Frames side by side (four a row), each labelled; the window's bottom edge
    /// is the red line, and what lies beyond it (clipped on screen) is tinted.
    static func contactSheet(title: String, frames: [(String, MotionSample)], to url: URL) throws {
        let columns = 4
        let scale = 0.75
        let pad: CGFloat = 10, label: CGFloat = 34, head: CGFloat = 30
        let imageWidth = frames.compactMap { f -> Double? in f.1.image.map { Double($0.width) / f.1.scale } }.max() ?? 700
        let imageHeight = frames.map { $0.1.imageHeight }.max() ?? 400
        let w = CGFloat(imageWidth * scale), h = CGFloat(imageHeight * scale)
        let rows = (frames.count + columns - 1) / columns
        let width = Int(CGFloat(columns) * (w + pad) + pad)
        let height = Int(head + CGFloat(rows) * (h + label + pad) + pad)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
              let context = NSGraphicsContext(bitmapImageRep: rep) else { throw MotionFailure(description: "no sheet context") }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSColor(white: 0.32, alpha: 1).setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()
        let big: [NSAttributedString.Key: Any] = [.font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.white]
        let small: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedSystemFont(ofSize: 11, weight: .regular),
                                                    .foregroundColor: NSColor.white]
        (title as NSString).draw(at: NSPoint(x: pad, y: CGFloat(height) - 24), withAttributes: big)
        for (i, frame) in frames.enumerated() {
            let (name, s) = frame
            let col = i % columns, row = i / columns
            let x = pad + CGFloat(col) * (w + pad)
            let top = CGFloat(height) - head - CGFloat(row) * (h + label + pad)
            ("\(name)   window \(f(s.win))  content \(f(s.vis))" as NSString)
                .draw(at: NSPoint(x: x, y: top - 16), withAttributes: small)
            let imageTop = top - label
            guard let cg = s.image else { continue }
            let dw = CGFloat(Double(cg.width) / s.scale * scale), dh = CGFloat(s.imageHeight * scale)
            NSColor(white: 0.12, alpha: 1).setFill()
            NSRect(x: x, y: imageTop - h, width: w, height: h).fill()
            NSImage(cgImage: cg, size: NSSize(width: dw, height: dh))
                .draw(in: NSRect(x: x, y: imageTop - dh, width: dw, height: dh))
            // Everything below the window's bottom edge is what the clip hides.
            let winY = imageTop - CGFloat(s.win * scale)
            if winY > imageTop - dh {
                NSColor.red.withAlphaComponent(0.35).setFill()
                NSRect(x: x, y: imageTop - dh, width: dw, height: winY - (imageTop - dh)).fill()
            }
            NSColor.red.setFill()
            NSRect(x: x, y: winY - 1, width: dw, height: 2).fill()
        }
        context.flushGraphics()
        guard let png = rep.representation(using: .png, properties: [:]) else { throw MotionFailure(description: "no sheet PNG") }
        try png.write(to: url)
    }

    // MARK: Scroll memory

    static func scrollViews(in view: NSView) -> [NSScrollView] {
        var found = (view as? NSScrollView).map { [$0] } ?? []
        for sub in view.subviews { found += scrollViews(in: sub) }
        return found
    }

    /// Brief scrolled, then Day and back: the Brief should come back where it was left.
    static func scrollMemory(_ rig: MotionRig) async throws -> String {
        rig.model.tab = .brief
        try await rig.settle()
        let candidates = scrollViews(in: rig.host).filter {
            guard let doc = $0.documentView else { return false }
            return doc.frame.height > $0.contentSize.height + 8
        }
        guard let scroll = candidates.first else {
            return "SKIP scroll memory: the Brief fits its cap on this display, so nothing scrolls"
        }
        let want: CGFloat = min(40, (scroll.documentView!.frame.height - scroll.contentSize.height) / 2)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: want))
        scroll.reflectScrolledClipView(scroll.contentView)
        try await Task.sleep(for: .milliseconds(300))
        rig.model.tab = .day
        try await rig.settle()
        rig.model.tab = .brief
        try await rig.settle()
        let back = scrollViews(in: rig.host).first {
            guard let doc = $0.documentView else { return false }
            return doc.frame.height > $0.contentSize.height + 8
        }
        let got = back?.contentView.bounds.origin.y ?? -1
        return abs(got - want) <= 2
            ? "PASS scroll memory: Brief came back at \(f(Double(got))) (left at \(f(Double(want))))"
            : "FAIL scroll memory: Brief left at \(f(Double(want))) came back at \(f(Double(got)))"
    }
}
