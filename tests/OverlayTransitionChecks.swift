import AppKit
import SwiftUI

// Intercept every request in this test process; no request reaches a service.
final class MotionFixtureProtocol: URLProtocol {
    /// What POST /unload_all answers, so a failed unload can be checked.
    static var unloadStatus = 200
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        // Setting Full access answers after a short delay, so a change can be in
        // flight while the notch collapses. Reading it gets a reply with no flag.
        let settingMode = request.url?.path == "/mode" && request.httpMethod == "POST"
        let body = Data((settingMode ? #"{"full_access":true,"read_only":false}"#
                                     : #"{"text":"Synthetic daily summary","session_id":"fixture"}"#).utf8)
        let status = request.url?.path == "/unload_all" ? MotionFixtureProtocol.unloadStatus : 200
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        let finish = { [weak self] in
            guard let self else { return }
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: body)
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if settingMode { DispatchQueue.global().asyncAfter(deadline: .now() + 0.3, execute: finish) } else { finish() }
    }
    override func stopLoading() {}
}

@MainActor
final class MotionFixtureModel: ObservableObject {
    @Published var height: CGFloat = 220
    /// The content's width. In the app it follows the layout (OverlayView's
    /// `.frame(width:)`), and the hosting view holds the window to it.
    @Published var width: CGFloat = 640
}

struct MotionFixtureView: View {
    @ObservedObject var model: MotionFixtureModel
    var body: some View { Text("Synthetic motion fixture").frame(width: model.width, height: model.height) }
}

@main
struct OverlayTransitionChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func lifecycleChecks() {
        var state = OverlayTransition()
        state.request(.chat)
        let opening = state.next()!
        state.request(.bar)
        state.request(.chat)
        check(state.next() == nil, "Only one native step may run")
        check(state.finish(opening), "Opening completes")
        check(state.next() == nil && state.settled == .chat, "Latest toggle wins")
        state.request(.hidden)
        let closing = state.next()!
        check(closing.to == .bar, "Expanded content retracts before hiding")
        state.request(.chat)
        check(!state.finish(opening), "Stale completion cannot complete a newer step")
        check(state.finish(closing), "Close completes")
        let reopening = state.next()!
        check(reopening.to == .chat, "Reopen supersedes pending hide")
        state.finish(reopening)
        state.request(.search)
        let retractChat = state.next(searchReady: false)!
        check(retractChat.to == .bar, "Chat closes while search captures")
        state.finish(retractChat)
        check(state.next(searchReady: false) == nil, "Search waits for capture")
        state.request(.hidden)
        let hide = state.next()!
        state.finish(hide)
        check(state.next(searchReady: true) == nil, "Late capture cannot reopen dismissed search")

        // Exhaust all short request sequences, including changes during each
        // step. Every accepted step must settle and converge without overlap.
        let surfaces: [OverlayTransition.Surface] = [.bar, .chat, .search, .hidden]
        for first in surfaces {
            for second in surfaces {
                for last in surfaces {
                    var machine = OverlayTransition()
                    machine.request(first)
                    let inFlight = machine.next()
                    machine.request(second)
                    machine.request(last)
                    if let inFlight { machine.finish(inFlight) }
                    var steps = 0
                    while let next = machine.next() {
                        check(!(next.from == .chat && next.to == .search) &&
                              !(next.from == .search && next.to == .chat), "Surfaces never overlap")
                        machine.finish(next)
                        steps += 1
                        check(steps <= 3, "Reconciliation must terminate")
                    }
                    check(machine.settled == last, "Last request must win")
                }
            }
        }
        print("PASS: lifecycle, stale completions, capture gating, and 64 request sequences")
    }

    /// An approval card is announced to VoiceOver with its own sentence and
    /// exactly the buttons it shows.
    static func approvalAnnouncementChecks() {
        let once = OverlayModel.approvalAnnouncement(reason: "Send this email to pat@example.com", grantable: false)
        check(once == "Wisp needs your approval: Send this email to pat@example.com. Choices: Deny, Allow once.",
              "A card without Always allow names two choices")
        check(!once.contains("Always allow"), "A hidden button is never announced")
        let always = OverlayModel.approvalAnnouncement(reason: "Run a shell command in\n  ~/Projects/demo.\n", grantable: true)
        check(always == "Wisp needs your approval: Run a shell command in ~/Projects/demo. Choices: Deny, Always allow, Allow once.",
              "A grantable card names all three, in the card's order, on one line")
        check(OverlayModel.approvalAnnouncement(reason: "Delete 3 files?", grantable: false)
              == "Wisp needs your approval: Delete 3 files? Choices: Deny, Allow once.", "A question keeps its mark")
        check(OverlayModel.approvalAnnouncement(reason: " \n", grantable: false)
              == "Wisp needs your approval. Choices: Deny, Allow once.", "A card with no sentence is still announced")
        print("PASS: approval cards are announced with the choices they show")
    }

    /// A card with Always allow always says what the rule would cover.
    static func alwaysAllowLineChecks() {
        check(OverlayModel.alwaysAllowLine(tool: "write_file", scopeHint: "~/Projects/demo")
              == "Always allow applies to write_file on ~/Projects/demo", "A scoped rule names its folder")
        check(OverlayModel.alwaysAllowLine(tool: "web_fetch", scopeHint: "example.com")
              == "Always allow applies to web_fetch on example.com", "Or its site")
        check(OverlayModel.alwaysAllowLine(tool: "move_path", scopeHint: "")
              == "Always allow applies to every move_path action", "A rule with no limit says it covers every action")
        check(OverlayModel.alwaysAllowLine(tool: "create_note", scopeHint: " \n")
              == "Always allow applies to every create_note action", "A blank hint is no limit")
        print("PASS: Always allow says what it covers, with or without a limit")
    }

    /// A heartbeat is about 1.5 s from the agent loop and 8 s of silence from
    /// plain chat; the stall line and the working label count with the same two.
    @MainActor
    static func heartbeatSecondsChecks() {
        check(OverlayModel.loopHeartbeatSeconds == 1.5 && OverlayModel.silenceHeartbeatSeconds == 8,
              "The two heartbeat paces are the backend's")
        check(OverlayModel.heartbeatSeconds(6, fromLoop: true) == 9, "Six loop heartbeats are 9 s, not 48")
        check(OverlayModel.heartbeatSeconds(1, fromLoop: true) == 1, "A part second rounds down")
        check(OverlayModel.heartbeatSeconds(2, fromLoop: false) == 16, "Two silent waits are 16 s")
        check(OverlayModel.heartbeatSeconds(0, fromLoop: false) == 0, "No heartbeat, no seconds")
        print("PASS: heartbeats count seconds at their sender's pace")
    }

    /// The window that covers both the current frame and the target: the wider
    /// and the taller of the two, hanging from the same top.
    static func coveringChecks() {
        let current = NSRect(x: 100, y: 500, width: 640, height: 300) // top 800
        func same(_ a: NSRect, _ b: NSRect) -> Bool { a == b }
        // Wider target, same height: grows both sides, same top.
        let wider = NSRect(x: 0, y: 500, width: 840, height: 300)
        check(same(OverlayPanel.covering(current, wider), wider), "A wider target is covered by its own frame")
        // Narrower target, shorter: the current frame still covers it.
        let narrowShort = NSRect(x: 150, y: 600, width: 540, height: 200)
        check(same(OverlayPanel.covering(current, narrowShort), current), "A smaller target keeps the current frame")
        // Taller target (same x, same top): the taller height wins.
        let taller = NSRect(x: 100, y: 400, width: 640, height: 400)
        check(same(OverlayPanel.covering(current, taller), taller), "A taller target is covered by its own frame")
        // Shorter target, same width: the current height stays.
        let shorter = NSRect(x: 100, y: 600, width: 640, height: 200)
        check(same(OverlayPanel.covering(current, shorter), current), "A shorter target keeps the current height")
        // Wider and shorter together: the union width at the current height.
        let wideShort = NSRect(x: 0, y: 600, width: 840, height: 200)
        check(same(OverlayPanel.covering(current, wideShort), NSRect(x: 0, y: 500, width: 840, height: 300)),
              "Wider and shorter covers the wider width at the taller height")
        // Narrower and taller together.
        let narrowTall = NSRect(x: 150, y: 400, width: 540, height: 400)
        check(same(OverlayPanel.covering(current, narrowTall), NSRect(x: 100, y: 400, width: 640, height: 400)),
              "Narrower and taller keeps the width at the taller height")
        for target in [wider, narrowShort, taller, shorter, wideShort, narrowTall] {
            let c = OverlayPanel.covering(current, target)
            check(c.maxY == 800 && target.maxY == 800, "The top stays put")
            check(c.contains(current) && c.contains(target), "The result holds both frames")
            check(OverlayPanel.covering(c, target) == c && OverlayPanel.covering(target, current) == OverlayPanel.covering(current, target),
                  "Covering is idempotent and symmetric")
        }
        // Different tops: the higher top wins and the height is the taller one.
        let lower = NSRect(x: 100, y: 450, width: 640, height: 300) // top 750
        check(same(OverlayPanel.covering(current, lower), NSRect(x: 100, y: 500, width: 640, height: 300)),
              "The higher top wins")
        print("PASS: covering holds both frames from the same top")
    }

    /// The paths an open and a close take (RevealMotion), built on NotchBox's springs.
    static func revealMotionChecks() {
        let open = NotchSpring.open, close = NotchSpring.close
        check(abs(open.response - 0.5 / 1.2) < 1e-9 && open.dampingRatio == 0.7,
              "Open starts from NotchBox's spring: response 0.5 at 1.2x, damping 0.7")
        check(close.response == 0.35 && close.dampingRatio == 1, "Close starts from NotchBox's: response 0.35, no bounce")
        check(abs(open.overshoot - 0.046) < 0.001 && close.overshoot == 0, "Left alone, NotchBox's open swings 4.6% past")
        check(open.position(at: 0) == 0 && close.position(at: 0) == 0, "A spring starts where it is")
        let peak = stride(from: 0.0, through: 1.0, by: 0.002).map { open.position(at: $0) }.max()!
        check(abs(peak - (1 + open.overshoot)) < 0.001, "The spring's path peaks where its overshoot says: \(peak)")
        check(abs(open.position(at: 2) - 1) < 1e-6 && abs(close.position(at: 2) - 1) < 1e-6, "Both come to rest on the target")
        check(open.capped(overshoot: 0).dampingRatio == 1 && open.capped(overshoot: 0.5) == open,
              "Capping at nothing removes the bounce; a generous cap changes nothing")

        // Opening: NotchBox's pace, damped to land softly; the path ends as the shape arrives.
        for reach in [120, 221, 480, 940] as [CGFloat] {
            let motion = RevealMotion.opening(reach: reach)
            let p = motion.progress
            check(p.first == 0 && p.last == 1, "An open starts on the bar and ends exactly on the panel")
            check(p.allSatisfy { $0 <= 1 }, "A \(reach) pt open never passes the panel")
            for i in 1..<p.count { check(p[i] >= p[i - 1], "An open never backs up") }
            check(motion.duration > 0.28 && motion.duration < 0.5, "An open is over in about a third of a second: \(motion.duration)")
            let at95 = Double(p.firstIndex { $0 >= 0.95 }!) * RevealMotion.step
            check(at95 < 0.3, "Most of an open is done well inside that: \(at95)")
            check(CGFloat(1 - p[p.count - 2]) * reach < 1.01, "An open arrives slowly: its last step is under a point")
        }

        // Closing: a hold while the content fades, then into the bar and gone.
        let closing = RevealMotion.closing()
        let c = closing.progress
        let held = Int((RevealMotion.closeHold / RevealMotion.step).rounded())
        check(c.first == 0 && c.last == 1, "A close starts on the panel and ends exactly on the bar")
        check(c.prefix(held).allSatisfy { $0 == 0 } && c[held + 3] > 0, "A close holds its shape for the fade, then moves")
        check(c.allSatisfy { $0 <= 1 }, "A close never passes the bar")
        for i in 1..<c.count { check(c[i] >= c[i - 1], "A close only ever shrinks") }
        check(closing.duration > 0.25 && closing.duration < 0.4, "A close takes about a third of a second: \(closing.duration)")
        // Still moving when it reaches the bar, so no lip is left hanging under the notch:
        // the last 5% of the travel takes a few frames, not the 200 ms a spring aimed at the bar takes.
        let lastTwentieth = Double(c.count - 1 - c.firstIndex { $0 >= 0.95 }!) * RevealMotion.step
        check(lastTwentieth < 0.09, "A close crosses its last 5% in under 90 ms: \(lastTwentieth)")
        check((1 - c[c.count - 3]) / (2 * RevealMotion.step) > 0.2, "A close arrives at the bar still moving")
        let unaimed = close.settleTime(tolerance: 0.002) - close.settleTime(tolerance: 0.05)
        check(unaimed > 0.15, "Aimed at the bar itself, the same spring would creep for \(unaimed) s")
        check(closing.duration < RevealMotion.opening(reach: 221).duration, "Closing is the quicker of the two")
        print("PASS: an open lands softly without passing the panel; a close holds, then tucks into the bar and stops")
    }

    #if WISP_MOTION_APP_DELEGATE_CHECKS
    /// The content's veil through an open and a close (OverlayModel.contentVeiled),
    /// with the panel's own motion stood in for: no window, so it runs everywhere.
    @MainActor
    static func contentVeilChecks() async {
        check(AppDelegate.closeFade <= RevealMotion.closeHold,
              "The close's fade fits inside the hold: \(AppDelegate.closeFade) s in \(RevealMotion.closeHold) s")
        check(AppDelegate.openFadeDelay + AppDelegate.openFade < RevealMotion.opening(reach: 221).duration,
              "The open's fade is done before the shape lands")
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        model.collapsed = false

        // A close: veiled while the shape retracts, lifted as the bar takes over.
        var endClose: (() -> Void)?
        var closed = 0
        AppDelegate.veiledClose(model: model, animated: true, rollUp: { endClose = $0 }) { closed += 1 }
        check(model.contentVeiled && !model.collapsed && closed == 0,
              "A close veils the content and keeps it mounted while the shape retracts")
        // The reveal's end and a display change cutting it short both arrive here.
        endClose?()
        check(!model.contentVeiled && model.collapsed && closed == 1,
              "The end of a close lifts the veil, swaps in the bar, then tells the caller")

        // An open: mounted veiled, and the fade-in starts with the reveal, not after it.
        var endOpen: (() -> Void)?
        var veiledAtReveal: Bool?
        var opened = 0
        AppDelegate.veiledOpen(model: model, animated: true,
                               dropOpen: { veiledAtReveal = model.contentVeiled; endOpen = $0 }) { opened += 1 }
        check(model.contentVeiled && !model.collapsed && endOpen == nil,
              "An open mounts the content veiled, before the shape moves")
        await waitUntil { endOpen != nil }
        check(veiledAtReveal == false && !model.contentVeiled,
              "The fade-in is under way as the reveal starts, so nothing can leave the content hidden")
        check(opened == 0, "The open is not finished until the panel says so")
        endOpen?()
        check(opened == 1 && !model.contentVeiled, "Then the caller hears, once")

        // A close whose end never arrives (the panel was hidden mid-close) leaves
        // the veil down; the next open clears it, with or without motion.
        AppDelegate.veiledClose(model: model, animated: true, rollUp: { _ in }) { closed += 1 }
        check(model.contentVeiled && closed == 1, "A close that never ends leaves the veil down")
        endOpen = nil
        AppDelegate.veiledOpen(model: model, animated: true, dropOpen: { endOpen = $0 }) {}
        await waitUntil { endOpen != nil }
        check(!model.contentVeiled, "The next open lifts a veil left behind")
        model.contentVeiled = true
        endOpen = nil
        AppDelegate.veiledOpen(model: model, animated: false, dropOpen: { endOpen = $0 }) {}
        check(!model.contentVeiled, "With Reduce Motion an open never veils, and clears one left behind at once")
        await waitUntil { endOpen != nil }

        // With Reduce Motion a close never veils either.
        var veiledDuringClose: Bool?
        AppDelegate.veiledClose(model: model, animated: false,
                                rollUp: { veiledDuringClose = model.contentVeiled; $0() }) {}
        check(veiledDuringClose == false && !model.contentVeiled && model.collapsed,
              "With Reduce Motion a close swaps straight to the bar")
        print("PASS: the content's veil lifts at the end of every close and with the start of every open")
    }
    #endif

    /// The reveal itself, sampled on a real panel: an open stays inside its
    /// window and lands on the panel; a close only shrinks, stops at the bar,
    /// and keeps the bar solid until the collapsed bar has taken over.
    @MainActor
    static func revealPathChecks() async {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        var compact = false
        let fixture = MotionFixtureModel()
        let panel = OverlayPanel { MotionFixtureView(model: fixture) }
        panel.isCompact = { compact }
        panel.reduceMotion = { false }
        fixture.height = 300
        let layout = panel.layoutProvider()
        let target = layout.expandedFrame(contentHeight: 300)
        let bar = layout.barFrame.size
        panel.present()
        let mask = panel.contentView!.layer!.mask!

        var opened = false
        panel.dropOpen { opened = true }
        check(panel.frame == target, "An open takes the panel's own frame, nothing more: \(panel.frame) vs \(target)")
        check(!panel.hasBarBacking, "Nothing backs the content of an open panel")
        let parts = (mask.animation(forKey: "reveal") as? CAAnimationGroup)?.animations ?? []
        check(parts.count == 2 && parts.allSatisfy { $0 is CAKeyframeAnimation }, "Size and corners follow one sampled path")
        var widest: CGFloat = 0, tallest: CGFloat = 0, samples = 0
        while !opened {
            if let live = mask.presentation() {
                widest = max(widest, live.bounds.width)
                tallest = max(tallest, live.bounds.height)
                check(live.bounds.width >= bar.width - 0.5 && live.bounds.height >= bar.height - 0.5,
                      "An open never dips under the bar")
                samples += 1
            }
            try? await Task.sleep(for: .milliseconds(4))
            check(samples < 1000, "The open ends")
        }
        // A loaded machine oversleeps the 4 ms poll, so only ask for a handful.
        check(samples > 4, "The open was sampled through its travel: \(samples)")
        check(widest <= target.width + 0.5 && tallest <= target.height + 0.5,
              "An open never passes the panel: \(widest) x \(tallest) for \(target.size)")
        check(panel.frame == target && mask.bounds.size == target.size, "At rest the window and the mask are the panel")
        check(mask.animationKeys()?.isEmpty != false, "No residual animation after the open")

        var closed = false
        let closeStart = CACurrentMediaTime()
        panel.rollUp { compact = true; closed = true }
        check(panel.hasBarBacking == layout.barIsVisible, "A close backs the bar it is heading for, when there is one to draw")
        // The lip, read off the animation the panel is really playing rather
        // than timed by polling: from 5% out to the bar takes a few frames.
        let nearBar = bar.height + 0.05 * (target.height - bar.height)
        let closeParts = ((mask.animation(forKey: "reveal") as? CAAnimationGroup)?.animations ?? [])
            .compactMap { $0 as? CAKeyframeAnimation }
        let heights = (closeParts.first { $0.keyPath == "bounds.size" }?.values ?? [])
            .compactMap { ($0 as? NSValue)?.sizeValue.height }
        check(heights.count > 20 && abs(heights[0] - target.height) < 0.5 && abs(heights[heights.count - 1] - bar.height) < 0.5,
              "The close's path runs from the panel to the bar: \(heights.count) steps")
        if let firstNear = heights.firstIndex(where: { $0 <= nearBar }), let playing = closeParts.first {
            let tail = Double(heights.count - 1 - firstNear) * playing.duration / Double(heights.count - 1)
            check(tail < 0.09, "No lip hangs under the notch: \(tail) s from 5% out to the bar")
        }
        var narrowest = target.width, shortest = target.height, lastWidth = target.width
        samples = 0
        while !closed {
            if let live = mask.presentation() {
                narrowest = min(narrowest, live.bounds.width)
                shortest = min(shortest, live.bounds.height)
                check(live.bounds.width <= lastWidth + 0.5, "A close only ever narrows: \(live.bounds.width) after \(lastWidth)")
                lastWidth = live.bounds.width
                samples += 1
            }
            try? await Task.sleep(for: .milliseconds(4))
            check(samples < 1000, "The close ends")
        }
        let closedAt = CACurrentMediaTime()
        check(samples > 4, "The close was sampled through its travel: \(samples)")
        check(narrowest >= bar.width - 0.5 && shortest >= bar.height - 0.5,
              "A close never passes the bar: \(narrowest) x \(shortest) for \(bar)")
        // Generous: the path's own length is checked exactly above and in revealMotionChecks.
        check(closedAt - closeStart < 1.5, "A close is over promptly: \(closedAt - closeStart) s")
        // Where the shape came to rest on the screen, against where the bar
        // will be: the same rectangle, so nothing steps sideways at the swap.
        let landed = mask.frame.offsetBy(dx: panel.frame.minX, dy: panel.frame.minY)
        check(abs(landed.minX - layout.barFrame.minX) < 0.01 && abs(landed.maxX - layout.barFrame.maxX) < 0.01
              && abs(landed.maxY - layout.barFrame.maxY) < 0.01 && abs(landed.minY - layout.barFrame.minY) < 0.01,
              "A close lands exactly on the bar: \(landed) for \(layout.barFrame)")
        if layout.style == .notch {
            check(layout.barFrame.midX == layout.screenFrame.midX && target.midX == layout.screenFrame.midX,
                  "The bar and the panel share the notch's centre: \(layout.barFrame.midX), \(target.midX), \(layout.screenFrame.midX)")
        }
        panel.settleToBar()
        check(mask.bounds.size == panel.frame.size && panel.frame.size == bar, "Then the bar")
        check(panel.frame == layout.barFrame, "The bar's window is where the close landed")
        check(panel.hasBarBacking == layout.barIsVisible, "The bar stays backed while the collapsed bar takes over")
        await waitUntil { !panel.hasBarBacking }
        print("PASS: the backing goes once the collapsed bar has had time to draw")

        // A reopen straight after a close must not keep the backing under the open panel.
        compact = false
        panel.rollUp { compact = true }
        opened = false
        panel.dropOpen { opened = true }
        check(!panel.hasBarBacking, "An open drops the backing a close turned on")
        await waitUntil { opened }

        // A display change in the middle of a close ends the close, but the
        // swap to the bar is still to come: the backing must outlast it.
        var cutShort = false
        panel.rollUp { compact = true; cutShort = true }
        check(panel.isRevealing && !cutShort, "The close is in flight")
        panel.applyDisplayChange()
        check(cutShort && !panel.isRevealing, "The display change ends the close")
        check(panel.hasBarBacking == layout.barIsVisible, "A close cut short by a display change keeps the bar backed")
        panel.settleToBar()
        check(panel.hasBarBacking == layout.barIsVisible, "And through the swap to the bar")
        // A second display change while the bar is still taking over keeps it too.
        panel.applyDisplayChange()
        check(panel.hasBarBacking == layout.barIsVisible, "A display change just after the swap keeps the bar backed")
        await waitUntil { !panel.hasBarBacking }

        #if WISP_MOTION_APP_DELEGATE_CHECKS
        // The same cut-short close through the app's own veil: the content
        // must not stay hidden behind the surface.
        let veil = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        veil.collapsed = false
        compact = false
        opened = false
        AppDelegate.veiledOpen(model: veil, animated: true, dropOpen: { panel.dropOpen($0) }) { opened = true }
        check(veil.contentVeiled, "A real open starts veiled")
        await waitUntil { opened }
        check(!veil.contentVeiled && !veil.collapsed, "An open panel shows its content")
        var veiledClosed = false
        AppDelegate.veiledClose(model: veil, animated: true, rollUp: { panel.rollUp($0) }) {
            compact = true
            veiledClosed = true
        }
        check(veil.contentVeiled && panel.isRevealing && !veiledClosed, "A real close veils the content while it retracts")
        panel.applyDisplayChange()
        check(veiledClosed && !veil.contentVeiled && veil.collapsed,
              "A close cut short by a display change lifts the veil and hands over to the bar")
        panel.settleToBar()
        #endif
        panel.orderOut(nil)
        print("PASS: a sampled open stays inside its window; a sampled close only shrinks, stops at the bar and keeps it solid")
    }

    /// Width changes: the panel's width comes from the layout, so inject one.
    @MainActor
    static func widthResizeChecks() async {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let fixture = MotionFixtureModel()
        let panel = OverlayPanel { MotionFixtureView(model: fixture) }
        panel.isCompact = { false }
        panel.reduceMotion = { false }
        let base = panel.layoutProvider()
        var wide = base.widened(true)
        if wide.expandedWidth == base.expandedWidth { wide.expandedWidth = base.expandedWidth + 200 }
        var useWide = false
        panel.layoutProvider = { useWide ? wide : base }
        fixture.width = base.expandedWidth
        fixture.height = 320
        panel.present()
        var opened = 0
        panel.dropOpen { opened += 1 }
        await waitUntil { opened == 1 }
        check(panel.frame.width == base.expandedWidth && panel.frame.height == 320, "Opened at the narrow width")
        let narrowFrame = panel.frame
        let settle = Int(OverlayPanel.shrinkSettle * 1000)

        // Widen and shrink together: covering frame at once, the target after the settle.
        useWide = true
        fixture.width = wide.expandedWidth
        fixture.height = 240
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        let target = wide.expandedFrame(contentHeight: 240)
        let covering = OverlayPanel.covering(narrowFrame, target)
        check(panel.frame == covering, "A width change with a height shrink grows at once to the covering frame: \(panel.frame) vs \(covering)")
        check(panel.frame.width == wide.expandedWidth && panel.frame.height == 320, "Covering is wide and still tall")
        check(panel.hasPendingShrink, "The target waits for the settle")
        let mask = panel.contentView!.layer!.mask!
        check(mask.bounds.size == panel.frame.size, "The mask follows the covering frame")
        try? await Task.sleep(for: .milliseconds(settle / 2))
        check(panel.frame == covering && panel.hasPendingShrink, "Still the covering frame before the settle")
        await waitUntil { panel.frame.height == 240 }
        check(panel.frame == target && !panel.hasPendingShrink, "Settles to the target: \(panel.frame) vs \(target)")
        check(mask.bounds.size == panel.frame.size, "The mask follows the target")

        // Narrow back and shrink together: the covering frame keeps the wide width until the settle.
        let wideFrame = panel.frame
        useWide = false
        fixture.width = base.expandedWidth
        fixture.height = 200
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame == wideFrame && panel.hasPendingShrink, "A narrowing shrink waits entirely")
        await waitUntil { panel.frame.height == 200 }
        check(panel.frame.width == base.expandedWidth && !panel.hasPendingShrink, "Then lands narrow")

        // A pure widen (same height) applies at once with nothing pending.
        useWide = true
        fixture.width = wide.expandedWidth
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame == wide.expandedFrame(contentHeight: 200), "A pure widen applies at once")
        check(!panel.hasPendingShrink, "A pure widen leaves nothing pending")
        panel.orderOut(nil)
        print("PASS: a width change with a shrink grows to the covering frame then settles; a pure widen is immediate")
    }

    @MainActor
    static func waitUntil(_ condition: () -> Bool) async {
        for _ in 0..<100 {
            if condition() { return }
            try? await Task.sleep(for: .milliseconds(20))
        }
        preconditionFailure("Native animation did not settle within two seconds")
    }

    @MainActor
    static func nativeChecks() async {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        print("Native graphical session: \(NSScreen.screens.count) screen(s)")
        precondition(!NSScreen.screens.isEmpty, "Native motion requires a graphical session")
        var compact = false
        let fixture = MotionFixtureModel()
        let panel = OverlayPanel { MotionFixtureView(model: fixture) }
        panel.isCompact = { compact }
        panel.reduceMotion = { false }
        panel.present()
        var opened = 0
        panel.dropOpen { opened += 1 }
        let target = panel.frame
        fixture.height = 320
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame == target && panel.isRevealing, "Resize cannot replace in-flight geometry")
        let mask = panel.contentView!.layer!.mask!
        check(mask.anchorPoint.y == 1, "Reveal must stay anchored to the top")
        check(mask.animationKeys() == ["reveal"], "One animation owns mask properties")
        await waitUntil { opened == 1 }
        check(!panel.isRevealing, "Open must release animation ownership")
        check(panel.frame.height == 320, "Deferred content height must apply after opening")
        check(mask.bounds.size == panel.frame.size, "Open mask fits panel")
        check(mask.contentsScale == panel.backingScaleFactor, "Mask rasterizes at the screen's scale")

        // A shrink waits for the content's own animation; a grow never does.
        fixture.height = 240
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame.height == 320 && panel.hasPendingShrink, "A shrink waits for the content to animate out")
        await waitUntil { panel.frame.height == 240 }
        check(!panel.hasPendingShrink && mask.bounds.size == panel.frame.size, "The held shrink lands and the mask follows")
        // Rapid toggles: a shrink, then a grow before it lands, then a shrink.
        fixture.height = 200
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        fixture.height = 360
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame.height == 360 && !panel.hasPendingShrink, "A grow applies at once and cancels a held shrink")
        fixture.height = 260
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        panel.resizeToFit()
        try? await Task.sleep(for: .milliseconds(Int(OverlayPanel.shrinkSettle * 1000) + 120))
        check(panel.frame.height == 260 && !panel.hasPendingShrink, "Coalesced resizes settle on the newest height")
        panel.reduceMotion = { true }
        fixture.height = 220
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame.height == 220 && !panel.hasPendingShrink, "Reduce Motion shrinks at once")
        panel.reduceMotion = { false }
        fixture.height = 320
        try? await Task.sleep(for: .milliseconds(60))
        panel.resizeToFit()
        check(panel.frame.height == 320, "Back to the opened height")
        // The host never follows the content: resized inside a SwiftUI
        // animation, it would centre the animating content and jolt it.
        let stage = panel.contentView!
        stage.layoutSubtreeIfNeeded()
        let slide = stage.subviews[0]
        check(slide.frame.height == OverlayPanel.hostHeight && slide.frame.maxY == stage.bounds.maxY,
              "The content host keeps a fixed height, pinned to the top")

        var stale = 0
        var closed = false
        panel.rollUp { stale += 1 }
        panel.dropOpen { opened += 1 }
        await waitUntil { opened == 2 }
        check(stale == 0, "Superseded close must not hide a reopened panel")
        panel.rollUp { compact = true; closed = true }
        panel.resizeToFit()
        await waitUntil { closed }
        panel.settleToBar()
        check(mask.bounds.size == panel.frame.size, "Compact mask fits after close")
        check(mask.animationKeys()?.isEmpty != false, "No residual animation at rest")

        // A display change (a resolution switch, a monitor plugged in) while an
        // open or close is in flight cancels the animation but must still end
        // the step that started it, once, or the notch can never move again.
        compact = false
        var machine = OverlayTransition()
        machine.request(.chat)
        let cutOpen = machine.next()!
        var cutOpens = 0
        panel.dropOpen { cutOpens += 1; machine.finish(cutOpen) }
        check(panel.isRevealing && cutOpens == 0, "The open is in flight")
        panel.applyDisplayChange()
        check(cutOpens == 1 && !panel.isRevealing, "A display change finishes the open it interrupts")
        check(machine.active == nil && machine.settled == .chat, "The interrupted open's step is settled")
        check(mask.bounds.size == panel.frame.size && mask.animationKeys()?.isEmpty != false,
              "The interrupted open is refit and fully shown")
        machine.request(.bar)
        let cutClose = machine.next()
        check(cutClose?.to == .bar, "The notch can still close after a display change")
        var cutCloses = 0
        panel.rollUp {
            cutCloses += 1
            compact = true
            if let cutClose { machine.finish(cutClose) }
        }
        check(panel.isRevealing && cutCloses == 0, "The close is in flight")
        panel.applyDisplayChange()
        check(cutCloses == 1 && !panel.isRevealing, "A display change finishes the close it interrupts")
        panel.settleToBar()
        check(machine.active == nil && machine.settled == .bar, "The interrupted close's step is settled")
        check(mask.bounds.size == panel.frame.size, "Compact mask fits after an interrupted close")
        // The cancelled animations' own callbacks land later and add nothing.
        try? await Task.sleep(for: .milliseconds(450))
        check(cutOpens == 1 && cutCloses == 1, "An interrupted reveal completes exactly once")
        panel.applyDisplayChange()
        check(cutOpens == 1 && cutCloses == 1, "A display change at rest completes nothing")
        machine.request(.chat)
        check(machine.next()?.to == .chat, "The notch can open again after a display change")

        panel.reduceMotion = { true }
        compact = false
        var reducedOpened = false
        panel.dropOpen { reducedOpened = true }
        check(reducedOpened && !panel.isRevealing, "Reduce Motion settles immediately")
        check(mask.animationKeys()?.isEmpty != false, "Reduce Motion adds no spatial animation")
        panel.reduceMotion = { false }
        panel.rollUp { stale += 1 }
        panel.orderOut(nil)
        try? await Task.sleep(for: .milliseconds(450))
        check(stale == 0 && !panel.isVisible && !panel.isRevealing, "Hidden panel rejects stale callbacks")
        print("PASS: isolated native reveal, resize, reversal, reduced motion, and hide cancellation")
    }

    /// Audit 32649065: a start failure on a cold launch, before any reachability
    /// probe has answered, must show the down card instead of "Starting Wisp…".
    @MainActor
    static func coldStartFailureChecks() {
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        check(model.serviceReachable && model.serviceStarting && !model.serviceDown,
              "A cold launch starts in the neutral starting state")
        NotificationCenter.default.post(name: .wispBackendStartFailed, object: nil,
                                        userInfo: ["reason": "Wisp's service exited (status 1)."])
        check(!model.serviceReachable && model.serviceDown,
              "A recorded start failure on a cold launch shows the down card")
        check(model.serviceStartFailure == "Wisp's service exited (status 1).", "The card names the cause")
        model.serviceStarting = false
        model.serviceReachable = true
        check(!model.serviceDown && model.serviceStartFailure == nil,
              "The first probe that answers clears the failure")
        NotificationCenter.default.post(name: .wispBackendStartFailed, object: nil, userInfo: ["reason": "late"])
        check(model.serviceReachable && model.serviceStartFailure == nil,
              "A stale failure after the service answered is ignored")
        print("PASS: a cold-launch start failure shows the down card; a running service ignores stale failures")
    }

    /// Ask's own forget clears the debug log's memory only when the tool says it
    /// removed something (the strings are service/tools/memory_tools.py's).
    static func forgetResultChecks() {
        func removed(_ tool: String, _ result: String, isInternal: Bool = false) -> Bool {
            ForgetToolResult.removedSomething(tool: tool, result: result, isInternal: isInternal)
        }
        check(removed("forget", "Forgot 1 matching memory record(s), including correction history."),
              "A forget that removed a memory")
        check(removed("forget", "Forgot 2 matching memory record(s), including correction history."
                      + " Also withdrew 1 notice(s) waiting for quiet hours to end."), "With a held notice too")
        check(removed("forget", "No exact matching memories were forgotten."
                      + " Also withdrew 1 notice(s) waiting for quiet hours to end."), "Or only a held notice")
        check(!removed("forget", "No exact matching memories were forgotten. Use recall to identify the statement to forget."),
              "A forget that matched nothing leaves the log alone")
        check(!removed("forget", "User denied forget."), "So does a denied one")
        check(!removed("forget", "Forgot 1 matching memory record(s), including correction history.", isInternal: true),
              "And a note to the model about its own mis-call")
        check(!removed("forget", ""), "And a call with no result yet")
        check(!removed("recall", "Forgot 1 matching memory record(s), including correction history.")
              && !removed("remember", "Saved memory 4: Forgot my keys once"), "Only the forget tool counts")
        print("PASS: only a forget that removed something clears the debug log's memory")
    }

    /// Try again is offered only where sending the same line again asks the same
    /// question, and never takes over what is already in the box.
    @MainActor
    static func tryAgainChecks() {
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        func failed() -> OverlayModel.Turn { OverlayModel.Turn(role: "assistant", text: "Something went wrong.", isError: true) }
        var error = failed()
        model.turns = [OverlayModel.Turn(role: "user", text: "What's the weather?"), error]
        check(model.offersTryAgain(error), "A plain question that failed offers Try again")
        var ran = failed(); ran.activity = ["read_mail — ok"]
        model.turns = [OverlayModel.Turn(role: "user", text: "What's the weather?"), ran]
        check(!model.offersTryAgain(ran), "Not after a tool ran")
        model.turns = [OverlayModel.Turn(role: "user", text: "What's the weather?"), error,
                       OverlayModel.Turn(role: "user", text: "next"), OverlayModel.Turn(role: "assistant", text: "ok")]
        check(!model.offersTryAgain(error), "Only under the last reply")

        // A question sent with an attachment keeps only its marker.
        for marker in QueuedPrompt.historyMarkers {
            error = failed()
            model.turns = [OverlayModel.Turn(role: "user", text: "Explain this \(marker)"), error]
            check(!model.offersTryAgain(error), "No Try again after a \(marker) question")
            model.tryAgain(error)
            check(model.turns.count == 2 && model.input.isEmpty && model.transientNotice == nil,
                  "Nor is the \(marker) line ever sent again as text")
        }

        // Something already typed is kept, and the failed exchange stays.
        error = failed()
        model.turns = [OverlayModel.Turn(role: "user", text: "What's the weather?"), error]
        model.input = "a new question"
        model.tryAgain(error)
        check(model.input == "a new question" && model.turns.count == 2 && model.turns[1].id == error.id,
              "Try again leaves a typed draft alone")
        check(model.transientNotice == "Send or clear what's in the box first, then try again.", "And says why nothing was sent")
        model.input = ""
        model.transientNotice = nil
        print("PASS: Try again never resends an attachment's marker as text or replaces a typed draft")
    }

    #if WISP_MOTION_APP_DELEGATE_CHECKS
    /// "Reading document…" is for an empty chip. An attachment already there keeps
    /// its own label through a second read, so one that fails leaves it as it was.
    @MainActor
    static func attachmentLabelChecks() async {
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        let missing = URL(fileURLWithPath: "/nonexistent-wisp-fixture/notes.txt")
        model.loadAttachment(url: missing)
        check(model.attachedLabel == "Reading document…", "An empty chip says a document is being read")
        await waitUntil { model.attachedLabel.isEmpty }
        check(!model.hasAttachment && model.transientNotice == DocumentAttachment.unreadableMessage,
              "A read that fails clears the chip and says why")

        await model.loadAttachment(name: "fixture.jpg") {
            MediaAttachment.Pending.image(jpeg: Data([0xFF, 0xD8, 0xFF, 0xD9]), source: .file)
        }.value
        check(model.hasAttachment && model.attachedLabel == "Image", "A picture is attached")
        model.transientNotice = nil
        model.loadAttachment(url: missing)
        check(model.attachedLabel == "Image", "A second read leaves the first attachment's label")
        await waitUntil { model.transientNotice != nil }
        try? await Task.sleep(for: .milliseconds(100))
        check(model.hasAttachment && model.attachedLabel == "Image" && model.attachedImageName == "fixture.jpg",
              "A second read that fails leaves the first attachment as it was")
        model.removeAttachment()
        check(!model.hasAttachment && model.attachedLabel.isEmpty, "Removing it clears the chip")
        print("PASS: a failed second attachment leaves the first one's chip unchanged")
    }

    /// Quick settings' Unload says "unloaded" only when the service answered that it
    /// was done; a failed unload says so, and the button comes back either way.
    @MainActor
    static func unloadNoticeChecks() async {
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        MotionFixtureProtocol.unloadStatus = 500
        model.freeUpMemory()
        check(model.freeingMemory, "Unload is under way")
        await waitUntil { !model.freeingMemory }
        check(model.transientNotice == "Couldn't unload the models. Try again in a moment." && !model.transientNoticeIsInfo,
              "A failed unload is never reported as done")
        MotionFixtureProtocol.unloadStatus = 200
        model.freeUpMemory()
        await waitUntil { !model.freeingMemory }
        check(model.transientNotice == "Models unloaded. Wisp reloads them on the next ask." && model.transientNoticeIsInfo,
              "An unload the service confirmed says so")
        model.transientNotice = nil
        print("PASS: Unload reports success only when the service confirmed it")
    }

    /// Export Chat Debug Log… covers every chat since Wisp opened: New chat and opening
    /// another chat archive the one left, whole; reopening it continues it with its
    /// detail; the export lists each chat and keeps the open one's top-level fields.
    @MainActor
    static func chatArchiveChecks() async {
        let model = OverlayModel(loadDebugMode: { false }, saveDebugMode: { _ in })
        check(model.debugExport() == nil, "Nothing to export before any chat")
        func reply(_ text: String, tool: String) -> OverlayModel.Turn {
            var turn = OverlayModel.Turn(role: "assistant", text: text)
            var call = OverlayModel.ToolCallRecord(); call.name = tool; call.argsJSON = "{}"; call.result = "result of \(tool)"
            var io = OverlayModel.RawModelIO(); io.model = "fixture"; io.requestJSON = #"{"q":1}"#; io.responseJSON = #"{"a":2}"#
            turn.toolCalls = [call]; turn.rawIO = [io]; turn.reasoningText = "thought about \(tool)"
            turn.startedAt = Date(timeIntervalSince1970: 1_000)
            return turn
        }
        func open(_ id: String) async {
            model.reopenChat(ChatSummary(id: id, title: id, preview: "", lastUsed: Date(), turnCount: 0))
            for _ in 0..<50 { await Task.yield() }
        }
        await open("chat-a")
        model.turns += [OverlayModel.Turn(role: "user", text: "mail?"), reply("Two emails.", tool: "read_mail")]
        model.newChat()
        check(model.turns.isEmpty && model.chatArchive.chats.map(\.sessionId) == ["chat-a"], "New chat archives the chat left")
        model.turns = [OverlayModel.Turn(role: "user", text: "unsaved"), OverlayModel.Turn(role: "assistant", text: "x", isError: true)]
        model.newChat()
        check(model.chatArchive.chats.map(\.sessionId) == ["chat-a", ""], "A chat with no session is kept, in order")

        await open("chat-a")
        check(model.turns.map(\.text) == ["mail?", "Two emails."] && model.turns[1].toolCalls.first?.result == "result of read_mail"
              && model.turns[1].rawIO.count == 1, "Reopening an archived chat brings back its full detail")
        check(model.chatArchive.chats.map(\.sessionId) == [""], "A reopened chat leaves the archive while it is open")
        model.turns += [OverlayModel.Turn(role: "user", text: "and calendar?"), reply("Free at 3.", tool: "read_calendar")]
        await open("chat-c")
        check(model.chatArchive.chats.map(\.sessionId) == ["", "chat-a"]
              && model.chatArchive.chats[1].turns.count == 4, "Opening another chat archives the continued one whole")
        model.turns = [OverlayModel.Turn(role: "user", text: "now"), reply("Now.", tool: "clock")]

        guard let export = model.debugExport(),
              let json = try? JSONSerialization.jsonObject(with: export.json) as? [String: Any],
              let chats = json["chats"] as? [[String: Any]] else { return check(false, "The export is JSON with chats") }
        check(chats.map { $0["session_id"] as? String } == ["", "chat-a", "chat-c"], "Every chat, oldest first, the open one last")
        check(chats.map { $0["current"] as? Bool } == [false, false, true], "Only the open chat is marked current")
        check(chats.map { $0["turn_count"] as? Int } == [2, 4, 2], "Each chat counts its turns")
        check(json["session_id"] as? String == "chat-c" && json["turn_count"] as? Int == 2
              && (json["turns"] as? [[String: Any]])?.map { $0["text"] as? String } == ["now", "Now."],
              "Top-level session_id / turn_count / turns stay the open chat's")
        let continued = chats[1]["turns"] as? [[String: Any]] ?? []
        check(continued[1]["raw_model_io"] != nil && continued[3]["tool_calls"] != nil && continued[1]["reasoning"] != nil,
              "Archived turns keep tool results, reasoning and model I/O")
        check(json["dropped_chats"] == nil && json["chat_count"] as? Int == 3, "Nothing dropped under the cap")
        check(export.text.contains("CHAT 1 of 3") && export.text.contains("CHAT 3 of 3 — open now")
              && export.text.contains("Session: chat-a") && export.text.contains("Session: (not saved)"),
              "The transcript has a header per chat")

        // Saved privately, or not at all.
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-export-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        guard case .saved(let saved) = await model.exportDebugLog(to: folder) else { return check(false, "The export saves") }
        guard case .saved(let again) = await model.exportDebugLog(to: folder), again != saved else {
            return check(false, "A second export in the same second saves beside the first")
        }
        let written = (try? FileManager.default.contentsOfDirectory(atPath: folder.path))?.sorted() ?? []
        check(written.count == 4 && written.allSatisfy { $0.hasPrefix("wisp-debug-") } && saved.pathExtension == "json",
              "Two .json and .txt pairs, neither export replacing the other")
        for name in written {
            let mode = (try? FileManager.default.attributesOfItem(atPath: folder.appendingPathComponent(name).path))?[.posixPermissions] as? Int
            check(mode == 0o600, "\(name) is readable only by this user")
        }
        guard case .failed(let reason) = await model.exportDebugLog(to: folder.appendingPathComponent("missing")), !reason.isEmpty else {
            return check(false, "A write that fails says so")
        }

        // The files are the snapshot's, byte for byte, written off the main actor.
        let fixed = Date(timeIntervalSince1970: 2_000)
        let separate = folder.appendingPathComponent("byte-for-byte")
        try? FileManager.default.createDirectory(at: separate, withIntermediateDirectories: true)
        guard let snapshot = model.debugLogSnapshot(now: fixed), let expected = model.debugExport(now: fixed),
              case .saved(let jsonURL) = OverlayModel.writeDebugLog(snapshot, to: separate) else {
            return check(false, "The snapshot exports")
        }
        check((try? Data(contentsOf: jsonURL)) == expected.json, "The .json is what the export built")
        check((try? String(contentsOf: jsonURL.deletingPathExtension().appendingPathExtension("txt"), encoding: .utf8)) == expected.text,
              "The .txt is what the export built")
        var finished = false
        let running = Task { @MainActor in
            _ = await model.exportDebugLog(to: separate)
            finished = true
        }
        await Task.yield()
        check(!finished, "The main actor runs on while the export builds and writes")
        await running.value
        check(finished, "The export finishes")

        model.newChat()
        check(model.debugExport() != nil, "After New chat the archive still exports")
        NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil, userInfo: ["session_id": "chat-a"])
        check(model.chatArchive.chats.map(\.sessionId) == ["", "chat-c"], "A deleted chat leaves the archive")
        NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil)
        check(model.chatArchive.chats.isEmpty && model.debugExport() == nil, "A forget clears the archive")

        // The chat open here, deleted in the Chat window or forgotten: kept on screen, never exported.
        await open("chat-x")
        model.turns = [OverlayModel.Turn(role: "user", text: "secret"), reply("Done.", tool: "read_mail")]
        NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil, userInfo: ["session_id": "other"])
        check(!model.liveChatForgotten && model.debugExport() != nil, "Deleting another chat leaves the open one")
        NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil, userInfo: ["session_id": "chat-x"])
        check(model.turns.count == 2 && model.liveChatForgotten && model.debugExport() == nil,
              "A deleted open chat stays on screen but is not exported")
        model.newChat()
        check(model.chatArchive.chats.isEmpty && !model.liveChatForgotten, "Nor archived when left; the next chat starts clear")
        model.turns = [OverlayModel.Turn(role: "user", text: "next"), OverlayModel.Turn(role: "assistant", text: "ok")]
        NotificationCenter.default.post(name: .wispForgetChatDebugArchive, object: nil)
        check(model.turns.count == 2 && model.debugExport() == nil, "A forget drops the open chat from the export too")
        model.newChat()
        check(model.chatArchive.chats.isEmpty, "A forgotten open chat is not archived")
        let history = [OverlayModel.Turn(role: "user", text: "window"), OverlayModel.Turn(role: "assistant", text: "w"),
                       OverlayModel.Turn(role: "user", text: "mail?"), OverlayModel.Turn(role: "assistant", text: "Two emails.")]
        let local = [OverlayModel.Turn(role: "user", text: "mail?"), reply("Two emails.", tool: "read_mail"),
                     OverlayModel.Turn(role: "assistant", text: "oops", isError: true)]
        let merged = OverlayModel.merging(history: history, into: local)
        check(merged.map(\.text) == ["window", "w", "mail?", "Two emails.", "oops"] && merged[3].toolCalls.count == 1,
              "Saved history merges around archived turns, keeping their detail and the local-only error")
        print("PASS: the debug export covers every chat since launch; reopened chats continue with full detail")
    }
    #endif

    static func main() async {
        lifecycleChecks()
        coveringChecks()
        revealMotionChecks()
        approvalAnnouncementChecks()
        alwaysAllowLineChecks()
        forgetResultChecks()
        await MainActor.run {
            heartbeatSecondsChecks()
            coldStartFailureChecks()
            tryAgainChecks()
        }
        #if WISP_MOTION_APP_DELEGATE_CHECKS
        precondition(URLProtocol.registerClass(MotionFixtureProtocol.self))
        await AppDelegate.checkDismissReopenInput()
        await AppDelegate.checkPointerReentryDuringOpening()
        await AppDelegate.checkSummaryDuringDismissal()
        await AppDelegate.checkPinnedPanelHoldsOpen()
        await AppDelegate.checkBriefReplyHoldsAndSurvives()
        await AppDelegate.checkMenuHoldsCollapse()
        await AppDelegate.checkResearchReleasesQueueOffTab()
        await AppDelegate.checkPermissionPageStepsAside()
        await AppDelegate.checkQuickSettingsSurviveCollapse()
        await AppDelegate.checkSetupCardHoldsAsk()
        await AppDelegate.checkInlineReportHoldsOpen()
        await AppDelegate.checkReportSaveHoldsUntilAnswered()
        await attachmentLabelChecks()
        await unloadNoticeChecks()
        await chatArchiveChecks()
        await contentVeilChecks()
        await AppDelegate.checkMenuExportsDebugLog()
        #endif
        if ProcessInfo.processInfo.environment["WISP_MOTION_NATIVE_CHECK"] == "1" {
            await nativeChecks()
            await revealPathChecks()
            await widthResizeChecks()
            #if WISP_MOTION_APP_DELEGATE_CHECKS
            await AppDelegate.checkDebugExportLeavesNotchLive()
            #endif
        }
    }
}
