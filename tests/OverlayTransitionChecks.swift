import AppKit
import SwiftUI

@MainActor
final class MotionFixtureModel: ObservableObject {
    @Published var height: CGFloat = 220
}

struct MotionFixtureView: View {
    @ObservedObject var model: MotionFixtureModel
    var body: some View { Text("Synthetic motion fixture").frame(width: 640, height: model.height) }
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

    static func main() async {
        lifecycleChecks()
        #if WISP_MOTION_APP_DELEGATE_CHECKS
        await AppDelegate.checkDismissReopenInput()
        #endif
        if ProcessInfo.processInfo.environment["WISP_MOTION_NATIVE_CHECK"] == "1" {
            await nativeChecks()
        }
    }
}
