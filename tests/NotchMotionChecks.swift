import SwiftUI
import AppKit
import QuartzCore

/// Frame-by-frame evidence for the notch panel's in-panel motion: the REAL
/// OverlayView inside the REAL OverlayPanel, kept far off-screen, sampled about
/// every 16 ms through each page change. Per sample it records the window's
/// height and where the content is actually drawn to (the bottom of the opaque
/// surface in the host's bitmap), then asserts:
///   (a) with motion: the content is never clipped by the window;
///   (b) with motion: no snap. At least one sample catches the content
///       between 10% and 90% of the change, and samples under 50 ms apart
///       never show it moving more than 45% of the change per 1/30 s;
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
        if MotionRig.lightMac { app.appearance = NSAppearance(named: .aqua) }
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
    var cost: Double = 0   // seconds the capture itself took
    var band = false       // image is only the header band (thin samples)
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
    /// WISP_NOTCH_MOTION_APPEARANCE=light: the app runs as on a Mac set to
    /// Light, with nothing forcing the panel dark but the panel itself.
    static let lightMac = ProcessInfo.processInfo.environment["WISP_NOTCH_MOTION_APPEARANCE"] == "light"

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
            // The engine's Brief, with its proactive cards, as people see it;
            // the wide layout exists only for it.
            return ok(try NotchQADriver.fixturePlan(day: day, dayStart: dayStart, proactive: .ready))
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
            MotionRoot(forceDark: !Self.lightMac) {
                OverlayView(model: model,
                            corpus: NotchCorpusController(store: ChatStore(backend: LiveChatBackend(client: client))),
                            researchModel: ResearchModel(client: client), todayModel: today,
                            settings: settings, setup: setup, onClose: {})
            }
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

    /// The window's height and the bottom of the drawn surface, now. `full`
    /// renders the whole width for a contact sheet; otherwise only a 2 pt
    /// column down the middle is drawn, which is quick enough to sample
    /// every frame.
    func capture(full: Bool = true) -> MotionSample {
        let win = Double(panel.frame.height)
        let bounds = host.bounds
        let h = min(Double(bounds.height), win + 120)
        let x = full ? 0 : (bounds.width / 2 - 1).rounded()
        let width = full ? bounds.width : 2
        let rect = host.isFlipped
            ? NSRect(x: x, y: 0, width: width, height: h)
            : NSRect(x: x, y: bounds.height - h, width: width, height: h)
        guard let rep = host.bitmapImageRepForCachingDisplay(in: rect) else {
            return MotionSample(t: 0, win: win, vis: -1, image: nil, scale: 1, imageHeight: h)
        }
        host.cacheDisplay(in: rect, to: rep)
        let scale = Double(rep.pixelsHigh) / h
        let vis = Self.surfaceBottom(rep) / scale
        if full { return MotionSample(t: 0, win: win, vis: vis, image: rep.cgImage, scale: scale, imageHeight: h) }
        // A thin sample also keeps the header band, full width, so the tab
        // capsule can be read in it (check d) without a whole frame.
        let bandH = min(h, 90)
        let bandRect = host.isFlipped
            ? NSRect(x: 0, y: 0, width: bounds.width, height: bandH)
            : NSRect(x: 0, y: bounds.height - bandH, width: bounds.width, height: bandH)
        guard let bandRep = host.bitmapImageRepForCachingDisplay(in: bandRect) else {
            return MotionSample(t: 0, win: win, vis: vis, image: nil, scale: scale, imageHeight: h)
        }
        host.cacheDisplay(in: bandRect, to: bandRep)
        return MotionSample(t: 0, win: win, vis: vis, image: bandRep.cgImage,
                            scale: Double(bandRep.pixelsHigh) / bandH, imageHeight: bandH, band: true)
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

/// The panel's root as the app builds it: forced dark only when the harness
/// stands in for a dark Mac (the default, as before).
struct MotionRoot<Content: View>: View {
    let forceDark: Bool
    @ViewBuilder let content: () -> Content
    var body: some View {
        if forceDark { content().environment(\.colorScheme, .dark) } else { content() }
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
        report += "display style: \(real.style.rawValue), notch: \(target.hasCameraHousing), expandedWidth \(real.expandedWidth), appearance \(MotionRig.lightMac ? "light Mac" : "dark")\n"
        print("display style \(real.style.rawValue) (notch: \(target.hasCameraHousing)), expandedWidth \(real.expandedWidth)")
        if !target.hasCameraHousing { print("note: no notch on this display; motion is measured on the \(real.style.rawValue) layout") }
        let shared = DisplayGeometry.shared.layout
        guard shared.expandedWidth == real.expandedWidth else {
            throw MotionFailure(description: "layout widths differ: view \(shared.expandedWidth) vs window \(real.expandedWidth)")
        }

        if let rounds = ProcessInfo.processInfo.environment["WISP_NOTCH_MOTION_PROFILE"].flatMap(Int.init) {
            return try await profile(rounds: rounds, out: out)
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
            // Something arriving inside a page (here Day's notice line) moves
            // the panel on the same spring as a page change.
            try await run("day-notice", prepare: { m.tab = .day }) {
                // Long enough that it can't expire inside a later measurement.
                m.showTransientNotice("Couldn't reach Calendar just now. Wisp will try again in a minute, and nothing on your day has changed.",
                                      duration: 120)
            }
            try await run("day-to-brief-with-notice") { m.tab = .brief }
            try await run("notice-clears") { m.transientNotice = nil }
            // Meet, the fourth tab: in from both sides and out to both sides.
            // Meet shows its home with three earlier meetings (no service).
            let meet = MeetingModel.shared
            try await run("brief-to-meet", prepare: { meet.qaShow(.earlier) }) { m.tab = .meet }
            try await run("meet-to-day") { m.tab = .day }
            try await run("day-to-meet") { m.tab = .meet }
            try await run("meet-open-notes") { meet.qaShow(.notes) }
            try await run("meet-close-notes") { meet.qaShow(.earlier) }
            try await run("meet-to-ask") { m.tab = .ask }
            // The Brief in two columns: the panel widens to its wide layout
            // (840 pt where the display allows), and tabs switch out of it.
            let wide = { (on: Bool) in
                UserDefaults.standard.set(on, forKey: NotchBriefView.wideKey)
                m.onResizeNow()
            }
            if rig.base.canWiden {
                try await run("brief-wide-on", prepare: { m.tab = .brief }) { wide(true) }
                try await run("wide-brief-to-day") { m.tab = .day }
                try await run("day-to-wide-brief") { m.tab = .brief }
                try await run("brief-wide-off") { wide(false) }
            } else {
                report += "SKIP wide Brief: this display has no room for the wide layout\n"
                print("SKIP wide Brief: no room for the wide layout")
            }
            try await run("gear-open") { m.showingQuickSettings = true }
            try await run("permissions-open") {
                m.openPermissionPage(nil)
                m.permissionPage.access.stop()
                m.permissionPage.access.qaShow(states: NotchQAPages.shot, asking: [], waiting: [], messagesReadable: true)
            }
            try await run("permissions-done") { m.closePermissionPage() }
            try await run("gear-close") { m.showingQuickSettings = false }

            // Study, on the QA fixture: the writing row arriving and leaving, and a
            // material pushed over the home list and popped back. The tab's own
            // refresh fails offline here (the QA transport) and leaves a notice,
            // which each `prepare` clears by showing the fixture again.
            let study = StudyModel.shared
            let midnight = Calendar.current.startOfDay(for: Date()).timeIntervalSince1970
            let fixture = try JSONDecoder().decode(StudyState.self, from: JSONSerialization.data(
                withJSONObject: NotchQADriver.studyState(midnight: midnight)))
            let guide = try JSONDecoder().decode(StudyMaterial.self, from: JSONSerialization.data(
                withJSONObject: NotchQADriver.studyMaterial("m-guide", midnight: midnight)))
            m.tab = .study
            try await rig.settle()
            try await run("study-writing", prepare: { study.show(state: fixture) }) {
                study.show(state: fixture, busy: .making(.guide))
            }
            try await run("study-writing-done") { study.show(state: fixture) }
            try await run("study-open-material") { study.show(state: fixture, opened: guide) }
            try await run("study-back") { study.close() }
            // A Coming-up row opening into its make buttons, a second row opening
            // as the first closes (one change), then closing.
            try await run("study-row-open") { NotchStudyView.qaToggle.send("qa:lab4") }
            try await run("study-row-switch") { NotchStudyView.qaToggle.send("qa:pset6") }
            try await run("study-row-close") { NotchStudyView.qaToggle.send("qa:pset6") }
            try await run("study-to-ask") { m.tab = .ask }

            // An Ask answer streaming in, from the first question on an empty chat.
            let stream = try await streamCheck(rig, reduce: reduce, out: out)
            report += stream.lines.joined(separator: "\n") + "\n"
            print("\(stream.ok ? "PASS" : "FAIL") \(mode): ask-stream - \(stream.lines.first ?? "")")
            if !stream.ok {
                failures += 1
                for line in stream.lines.dropFirst() where line.hasPrefix("  !") { print(line) }
            }

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

    /// How long the main thread is held between a change and its first frame:
    /// the SwiftUI update, layout and one draw of the host, done synchronously
    /// right after the change. Loops so a sampler can attach and see where it goes.
    static func profile(rounds: Int, out: URL) async throws -> Bool {
        let rig = try MotionRig(reduce: false)
        try await rig.open()
        let m = rig.model
        let steps: [(String, () -> Void)] = [
            ("ask->brief", { m.tab = .brief }), ("brief->day", { m.tab = .day }),
            ("day->meet", { m.tab = .meet }), ("meet->ask", { m.tab = .ask }),
            ("ask->day", { m.tab = .day }), ("day->ask", { m.tab = .ask }),
            ("gear open", { m.showingQuickSettings = true }), ("gear close", { m.showingQuickSettings = false }),
        ]
        var times: [String: [Double]] = [:]
        print("profile pid \(getpid()), \(rounds) rounds")
        for _ in 0..<rounds {
            for (name, change) in steps {
                let t0 = CACurrentMediaTime()
                change()
                rig.host.layoutSubtreeIfNeeded()
                rig.host.display()
                let update = CACurrentMediaTime() - t0
                times[name, default: []].append(update * 1000)
                try await Task.sleep(for: .milliseconds(450))
            }
        }
        var lines = ["first-frame cost (ms): median / max over \(rounds) rounds"]
        for (name, _) in steps {
            let v = (times[name] ?? []).sorted()
            guard !v.isEmpty else { continue }
            lines.append(String(format: "  %-11@ %6.1f / %6.1f", name as NSString, v[v.count / 2], v.last!))
        }
        let text = lines.joined(separator: "\n")
        print(text)
        try text.write(to: out.appendingPathComponent("profile.txt"), atomically: true, encoding: .utf8)
        rig.close()
        return true
    }

    static func transition(_ rig: MotionRig, name: String, reduce: Bool, change: @escaping () -> Void,
                           out: URL) async throws -> TransitionResult {
        try await rig.settle()
        let before = rig.capture()
        let widthBefore = rig.panel.frame.width
        let t0 = CACurrentMediaTime()
        change()
        var samples: [MotionSample] = []
        var lastFull = -1.0
        while true {
            try await Task.sleep(for: .milliseconds(8))
            let t = CACurrentMediaTime() - t0
            // A full frame now and then for the contact sheet; a thin column
            // otherwise. A full frame of a big page can take a quarter of a
            // second to capture (cacheDisplay draws on the CPU, unlike the app),
            // so the first 250 ms get one (after 150 ms) and are otherwise
            // sampled thin, where a change is mid-flight.
            let full = t - lastFull >= 0.07 && (t >= 0.25 || (lastFull < 0 && t >= 0.15))
            let c0 = CACurrentMediaTime()
            var s = rig.capture(full: full)
            s.cost = CACurrentMediaTime() - c0
            if full { lastFull = t }
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
        // A snap is the content going from where it was to where it ends with
        // nothing in between. SwiftUI animations run on the clock, so a sample
        // shows where the content is at that moment however late it is taken:
        // a moving page is caught part-way (between 10% and 90% of the change);
        // a swapped one only ever reads as before or after. That holds even
        // though sampling is coarse: capturing re-renders the whole SwiftUI
        // host (cacheDisplay), which takes ~100-200 ms while it animates,
        // however narrow the strip. Where two samples do fall close together
        // (under 50 ms), the speed between them must also stay under 45% of
        // the change per 1/30 s; the 0.3 s spring peaks near 26%.
        var steps: [Double] = []
        // The gap between samples less the previous capture's own cost is time
        // the main thread spent elsewhere: in the app, that is dropped frames.
        var fastest = 0.0, fastestClose = 0.0, widestGap = 0.0, widestBusy = 0.0
        var previous = before.vis, previousT = 0.0, previousCost = 0.0
        var midFlight = 0
        for s in samples {
            let step = abs(s.vis - previous), dt = max(s.t - previousT, 1.0 / 120)
            steps.append(step)
            if step > 0.5 {
                widestGap = max(widestGap, dt)
                widestBusy = max(widestBusy, dt - previousCost)
                fastest = max(fastest, step / dt / 30)
                if dt <= 0.05 { fastestClose = max(fastestClose, step / dt / 30) }
            }
            if total > 0 {
                let share = abs(s.vis - before.vis) / total
                if share > 0.1, share < 0.9 { midFlight += 1 }
            }
            previous = s.vis; previousT = s.t; previousCost = s.cost
        }
        let firstSample = samples.first.map { Int($0.t * 1000) } ?? -1
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
            if total > 20, midFlight == 0 {
                problems.append("(b) snap: no sample caught the content between 10% and 90% of a \(f(total)) pt change")
            }
            if total > 20, fastestClose > 0.45 * total {
                problems.append("(b) snap: the content moves \(Int(fastestClose / total * 100))% of a \(f(total)) pt change per 1/30 s")
            }
            if abs(final.win - final.vis) > 2 { problems.append("(c) window \(f(final.win)) != final content \(f(final.vis))") }
        }
        if total <= 20 { visibleTotal += " (small: snap check skipped)" }

        // The selected-tab capsule: one solid white shape that moves. Two
        // capsules crossfading show as a wide band of half-bright pixels.
        var capsuleLine = "  capsule: not found"
        // A width change moves the whole strip with the panel, so the capsule's
        // own travel can't be read against a fixed row; the sheet shows it.
        let widthChanged = abs(rig.panel.frame.width - widthBefore) > 1
        if widthChanged {
            capsuleLine = "  capsule: not measured (panel width \(f(widthBefore)) -> \(f(rig.panel.frame.width)))"
        } else if let row = capsuleRow(before), let a = readCapsule(before, row: row, span: nil),
           let b = readCapsule(final, row: row, span: nil) {
            let span = (min(a.lo, b.lo), max(a.hi, b.hi))
            let from = a.centre, to = b.centre, moved = abs(to - from)
            var worstGhost = 0.0, caught = 0, firstShare: Double? = nil
            for s in samples where (s.image?.width ?? 0) > 8 {
                guard let r = readCapsule(s, row: row, span: span) else { continue }
                worstGhost = max(worstGhost, r.ghost)
                if moved > 1 {
                    let share = (r.centre - from) / (to - from)
                    if firstShare == nil { firstShare = share }
                    if share > 0.1, share < 0.9 { caught += 1 }
                }
            }
            capsuleLine = "  capsule: moved \(f(moved)) pt, \(caught) sample(s) mid-flight, worst ghost \(f(worstGhost)) pt half-bright"
            if worstGhost > 6 {
                problems.append("(d) ghost: \(f(worstGhost)) pt of the tab strip is half-bright, so two capsules show at once")
            }
            if moved > 20 {
                if reduce, let first = firstShare, abs(first - 1) > 0.05 {
                    problems.append("(d) capsule's first sample is \(Int(first * 100))% of the way, not at its tab")
                }
                if !reduce, caught == 0 {
                    problems.append("(d) capsule snap: no sample caught it between 10% and 90% of its \(f(moved)) pt move")
                }
                // The dark label on the capsule must be the grey label it
                // covers, in the same place: the strip draws it as a second
                // row, which once laid out narrower, truncating "Day" to "…"
                // and shifting the other labels off their capsule.
                let inset = (b.lo + 6, b.hi - 6)
                if let grey = labelInk(before, row: row, span: inset, onCapsule: false),
                   let dark = labelInk(final, row: row, span: inset, onCapsule: true) {
                    capsuleLine += ", label ink \(f(grey.0))-\(f(grey.1)) grey vs \(f(dark.0))-\(f(dark.1)) on the capsule"
                    if abs(grey.0 - dark.0) > 1 || abs(grey.1 - dark.1) > 1 {
                        problems.append("(e) label on the capsule spans \(f(dark.0))-\(f(dark.1)) pt, but the tab's label spans \(f(grey.0))-\(f(grey.1)) pt")
                    }
                } else {
                    problems.append("(e) no label ink found under the capsule")
                }
            }
        }

        var lines = ["\(name) [\(reduce ? "reduce-motion" : "motion")] change \(f(before.vis)) -> \(f(final.vis)) = \(visibleTotal) pt, max step \(f(maxStep)), \(midFlight) sample(s) mid-flight, first sample \(firstSample) ms, peak \(total > 0 ? Int(fastest / total * 100) : 0)%/frame@30, widest gap \(Int(widestGap * 1000)) ms (\(Int(widestBusy * 1000)) ms not capturing), max overshoot past window \(f(max(maxClip, 0)))"]
        lines.append("  before: win \(f(before.win)) content \(f(before.vis))")
        lines.append(capsuleLine)
        for s in samples {
            lines.append(String(format: "  t=%4d ms  capture %3d ms  win %7.1f  content %7.1f%@", Int(s.t * 1000), Int(s.cost * 1000), s.win, s.vis,
                                s.vis > s.win + 1 ? "  CLIPPED" : ""))
        }
        lines.append("  final: win \(f(final.win)) content \(f(final.vis))")
        for p in problems { lines.append("  ! \(p)") }

        var picks: [(String, MotionSample)] = [("before", before)]
        var used = Set<Int>()
        for target in [20.0, 50, 90, 140, 210, 320] {
            if let i = samples.indices.filter({ samples[$0].image != nil && !samples[$0].band && samples[$0].scale > 0 && samples[$0].image!.width > 8 }).min(by: { abs(samples[$0].t * 1000 - target) < abs(samples[$1].t * 1000 - target) }),
               used.insert(i).inserted {
                picks.append(("\(Int(samples[i].t * 1000)) ms", samples[i]))
            }
        }
        picks.append(("final", final))
        let file = out.appendingPathComponent(reduce ? "\(name)-reduce-motion.png" : "\(name).png")
        try contactSheet(title: "\(name)  \(reduce ? "(reduce motion)" : "(motion)")", frames: picks, to: file)
        return TransitionResult(name: name, ok: problems.isEmpty, lines: lines)
    }

    /// A frame as top-down RGBA8 rows, composited over black like the panel.
    static func pixels(_ s: MotionSample) -> (data: [UInt8], w: Int, h: Int)? {
        guard let image = s.image, image.width > 8, let space = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        let w = image.width, h = image.height
        var data = [UInt8](repeating: 0, count: w * h * 4)
        let drawn: Bool = data.withUnsafeMutableBytes { buffer in
            guard let ctx = CGContext(data: buffer.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
            ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        return drawn ? (data, w, h) : nil
    }

    /// The darkest channel at a pixel, 0-255: white reads ~255, grey text and
    /// the strip's 8% fill read low.
    static func whiteness(_ p: (data: [UInt8], w: Int, h: Int), _ x: Int, _ y: Int) -> Int {
        let i = (y * p.w + x) * 4
        return Int(min(p.data[i], p.data[i + 1], p.data[i + 2]))
    }

    /// The pixel row 2 pt inside the top of the white tab capsule (above the
    /// labels and the Day badge), found in the frame before the change.
    static func capsuleRow(_ s: MotionSample) -> Int? {
        guard let p = pixels(s) else { return nil }
        let need = Int(8 * s.scale)
        for y in 0..<min(p.h, Int(100 * s.scale)) {
            var run = 0, best = 0
            for x in 0..<p.w {
                run = whiteness(p, x, y) >= 235 ? run + 1 : 0
                best = max(best, run)
            }
            if best >= need { return y + Int(2 * s.scale) }
        }
        return nil
    }

    /// On that row: the widest solid white run (its centre and ends, points)
    /// and how much of the strip is half-bright (points), inside `span`.
    static func readCapsule(_ s: MotionSample, row: Int, span: (Double, Double)?)
        -> (centre: Double, lo: Double, hi: Double, ghost: Double)? {
        guard let p = pixels(s), row < p.h else { return nil }
        let lo = span.map { max(0, Int($0.0 * s.scale)) } ?? 0
        let hi = span.map { min(p.w, Int($0.1 * s.scale) + 1) } ?? p.w
        guard lo < hi else { return nil }
        var bestStart = 0, bestLen = 0, start = 0, len = 0, half = 0
        for x in lo..<hi {
            let v = whiteness(p, x, row)
            if v >= 235 {
                if len == 0 { start = x }
                len += 1
                if len > bestLen { bestLen = len; bestStart = start }
            } else {
                len = 0
                if v >= 70 && v < 215 { half += 1 }
            }
        }
        guard bestLen >= Int(4 * s.scale) else {
            // No solid capsule at all: everything half-bright is ghost.
            return span == nil ? nil : (0, 0, 0, Double(half) / s.scale)
        }
        let a = Double(bestStart) / s.scale, b = Double(bestStart + bestLen) / s.scale
        return ((a + b) / 2, a, b, Double(half) / s.scale)
    }

    /// The first and last columns (points) with label ink inside `span`, over
    /// the 16 pt of rows below the capsule row: bright text and badge on the
    /// dark strip, or dark text and badge on the white capsule.
    static func labelInk(_ s: MotionSample, row: Int, span: (Double, Double), onCapsule: Bool) -> (Double, Double)? {
        guard let p = pixels(s) else { return nil }
        let lo = max(0, Int(span.0 * s.scale)), hi = min(p.w, Int(span.1 * s.scale))
        let top = row + Int(1 * s.scale), bottom = min(p.h, row + Int(17 * s.scale))
        guard lo < hi, top < bottom else { return nil }
        var first: Int? = nil, last: Int? = nil
        for x in lo..<hi {
            for y in top..<bottom {
                let v = whiteness(p, x, y)
                if onCapsule ? v <= 165 : v >= 90 {
                    if first == nil { first = x }
                    last = x
                    break
                }
            }
        }
        guard let a = first, let b = last else { return nil }
        return (Double(a) / s.scale, Double(b + 1) / s.scale)
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

    // MARK: Streaming

    /// A first question on an empty Ask: the suggestions give way to the
    /// conversation and "Thinking…", then an answer streams in the way the
    /// service delivers it (a flush of a few words every 60 ms, each followed by
    /// onResize), then the reply moves into the history. Checks the panel
    /// only ever grows while text arrives (no shrink-and-regrow jitter), the
    /// window never clips it, and reports how far the panel jumps when the
    /// reply is finished.
    static func streamCheck(_ rig: MotionRig, reduce: Bool, out: URL) async throws -> TransitionResult {
        let m = rig.model
        m.tab = .ask
        m.turns = []; m.answer = ""; m.phase = .idle
        try await rig.settle()
        let words = Self.streamAnswer.split(separator: " ", omittingEmptySubsequences: false).map(String.init)
        let before = rig.capture()
        let t0 = CACurrentMediaTime()
        m.turns.append(OverlayModel.Turn(role: "user", text: "What should I get done before the study group?"))
        m.phase = .working
        m.onResize()
        var samples: [MotionSample] = []
        var lastFull = -1.0
        var next = 0, streamStart = -1.0, doneAt = -1.0, lastFlush = 0.0
        func sample(full: Bool) {
            let t = CACurrentMediaTime() - t0
            let c0 = CACurrentMediaTime()
            var s = rig.capture(full: full)
            s.cost = CACurrentMediaTime() - c0
            s.t = t
            samples.append(s)
        }
        while true {
            // Sample first, then change: a change and a capture in the same
            // turn would catch the window before its queued refit, a state the
            // screen never shows (the app commits frames between turns).
            try await Task.sleep(for: .milliseconds(8))
            let st = CACurrentMediaTime() - t0
            let full = st - lastFull >= 0.25
            if full { lastFull = st }
            sample(full: full)
            if doneAt > 0, st - doneAt >= 0.8 { break }
            if st > 12 { break }
            try await Task.sleep(for: .milliseconds(1))
            let t = CACurrentMediaTime() - t0
            if t >= 0.5, next < words.count, t - lastFlush >= 0.06 {
                if streamStart < 0 { streamStart = t; m.phase = .streaming }
                let chunk = words[next..<min(words.count, next + 4)].joined(separator: " ")
                m.answer += (next == 0 ? "" : " ") + chunk
                next += 4
                lastFlush = t
                m.onResize()
            } else if next >= words.count, doneAt < 0, t - lastFlush >= 0.3 {
                doneAt = t
                m.turns.append(OverlayModel.Turn(role: "assistant", text: m.answer))
                m.answer = ""
                m.phase = .done
                m.onResize()
            }
        }
        try await rig.settle()
        var final = rig.capture()
        final.t = CACurrentMediaTime() - t0

        var problems: [String] = []
        var maxClip = 0.0, shrinks = 0, worstShrink = 0.0, high = 0.0, winShrinks = 0
        var lastWin = before.win, lastBeforeDone = before.vis
        for s in samples {
            maxClip = max(maxClip, s.vis - s.win)
            if s.t >= streamStart, s.t < doneAt {
                if s.vis < high - 1 { shrinks += 1; worstShrink = max(worstShrink, high - s.vis) }
                if s.win < lastWin - 1 { winShrinks += 1 }
                lastBeforeDone = s.vis
            }
            high = max(high, s.vis)
            lastWin = s.win
        }
        let doneJump = final.vis - lastBeforeDone
        if maxClip > 1 { problems.append("(s) clipped: the answer passes the window by \(f(maxClip)) pt") }
        // With motion the surface's spring absorbs a reflow; with Reduce Motion
        // it follows the text exactly, so a list's first line settling (about
        // 5 pt) shows as is: noted there, a failure only past 6 pt.
        let tolerated = reduce && worstShrink <= 6
        if shrinks > 0, !tolerated { problems.append("(s) jitter: the panel shrank \(shrinks) time(s) while text arrived, by up to \(f(worstShrink)) pt") }
        if winShrinks > 0, !tolerated { problems.append("(s) the window shrank \(winShrinks) time(s) while text arrived") }
        var lines = ["ask-stream [\(reduce ? "reduce-motion" : "motion")] content \(f(before.vis)) -> \(f(final.vis)), streamed \(words.count) words from \(Int(streamStart * 1000)) to \(Int(doneAt * 1000)) ms, \(samples.count) samples, max overshoot past window \(f(max(maxClip, 0))), jump when finished \(f(doneJump)) pt"]
        var steps: [Double] = []
        var prev = before.vis
        for s in samples where s.t >= streamStart && s.t < doneAt { steps.append(s.vis - prev); prev = s.vis }
        if shrinks > 0, tolerated { lines.append("  note: Reduce Motion, the panel stepped back \(shrinks) time(s) by up to \(f(worstShrink)) pt as the text reflowed") }
        lines.append("  growth steps while streaming: \(steps.filter { abs($0) > 0.5 }.map { f($0) }.joined(separator: " "))")
        for p in problems { lines.append("  ! \(p)") }

        var picks: [(String, MotionSample)] = [("before", before)]
        for target in [0.1, streamStart + 0.2, streamStart + 0.8, streamStart + 1.6, doneAt - 0.02, doneAt + 0.1, doneAt + 0.4] {
            if let s = samples.filter({ $0.image != nil && !$0.band && ($0.image?.width ?? 0) > 8 }).min(by: { abs($0.t - target) < abs($1.t - target) }) {
                picks.append(("\(Int(s.t * 1000)) ms", s))
            }
        }
        picks.append(("final", final))
        try contactSheet(title: "ask-stream  \(reduce ? "(reduce motion)" : "(motion)")", frames: picks,
                         to: out.appendingPathComponent(reduce ? "ask-stream-reduce-motion.png" : "ask-stream.png"))
        // Leave Ask empty for whatever runs next.
        m.turns = []; m.answer = ""; m.phase = .idle
        try await rig.settle()
        return TransitionResult(name: "ask-stream", ok: problems.isEmpty, lines: lines)
    }

    static let streamAnswer = """
    Before the study group at 6, the one thing that matters is **Worksheet 3 for STAT 5**: it's due tomorrow and you have a free hour from 2 to 3.

    - Finish problems 4–7 on the worksheet; 1–3 are already done.
    - Skim the CSE 30 readings so you can follow the review.
    - Reply to Ed about taking Exam 1 early on Tuesday.
    - Grab coffee on the way: the group meets at the library café.

    If you only get one of these done, make it the worksheet. I can hold 2–3 PM for it if you'd like.
    """

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
