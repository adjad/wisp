import AppKit
import SwiftUI

// Intercept every request in this test process; no request reaches a service.
final class MotionFixtureProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        // Setting Full access answers after a short delay, so a change can be in
        // flight while the notch collapses. Reading it gets a reply with no flag.
        let settingMode = request.url?.path == "/mode" && request.httpMethod == "POST"
        let body = Data((settingMode ? #"{"full_access":true,"read_only":false}"#
                                     : #"{"text":"Synthetic daily summary","session_id":"fixture"}"#).utf8)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200,
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

    #if WISP_MOTION_APP_DELEGATE_CHECKS
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
        guard case .saved(let saved) = model.exportDebugLog(to: folder) else { return check(false, "The export saves") }
        guard case .saved(let again) = model.exportDebugLog(to: folder), again != saved else {
            return check(false, "A second export in the same second saves beside the first")
        }
        let written = (try? FileManager.default.contentsOfDirectory(atPath: folder.path))?.sorted() ?? []
        check(written.count == 4 && written.allSatisfy { $0.hasPrefix("wisp-debug-") } && saved.pathExtension == "json",
              "Two .json and .txt pairs, neither export replacing the other")
        for name in written {
            let mode = (try? FileManager.default.attributesOfItem(atPath: folder.appendingPathComponent(name).path))?[.posixPermissions] as? Int
            check(mode == 0o600, "\(name) is readable only by this user")
        }
        guard case .failed(let reason) = model.exportDebugLog(to: folder.appendingPathComponent("missing")), !reason.isEmpty else {
            return check(false, "A write that fails says so")
        }

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
        await MainActor.run { coldStartFailureChecks() }
        #if WISP_MOTION_APP_DELEGATE_CHECKS
        precondition(URLProtocol.registerClass(MotionFixtureProtocol.self))
        await AppDelegate.checkDismissReopenInput()
        await AppDelegate.checkPointerReentryDuringOpening()
        await AppDelegate.checkSummaryDuringDismissal()
        await AppDelegate.checkPinnedPanelHoldsOpen()
        await AppDelegate.checkMenuHoldsCollapse()
        await AppDelegate.checkResearchReleasesQueueOffTab()
        await AppDelegate.checkQuickSettingsSurviveCollapse()
        await AppDelegate.checkSetupCardHoldsAsk()
        await AppDelegate.checkInlineReportHoldsOpen()
        await chatArchiveChecks()
        await AppDelegate.checkMenuExportsDebugLog()
        await AppDelegate.checkMenuChecksForUpdates()
        #endif
        if ProcessInfo.processInfo.environment["WISP_MOTION_NATIVE_CHECK"] == "1" {
            await nativeChecks()
        }
    }
}
