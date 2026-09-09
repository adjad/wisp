import SwiftUI

@MainActor
final class OverlayModel: ObservableObject {
    enum Phase { case idle, routing, working, streaming, confirming, done, error }

    struct Pending: Identifiable {
        let id = UUID()
        let sessionId: String
        let actionId: String
        let tool: String
        let reason: String
        // False for actions the backend refuses to ever pre-approve (sending
        // mail/messages — see service/safety/grants.py). The card hides its
        // "Always allow" button in that case rather than offering one that
        // would silently do nothing.
        var grantable: Bool = true
        // What an "always" answer would cover: a directory, a hostname, a
        // program name. Empty means the grant applies to the whole tool.
        var scopeHint: String = ""
        // For create_tool: the exact code about to be written to disk. The
        // args alone ({"name": "..."}) say nothing about what the tool DOES,
        // and this is the moment the user authorizes it — so the card shows
        // the real thing rather than asking them to trust a summary.
        var preview: String = ""
    }

    struct MessageDraft: Identifiable {
        let id = UUID()
        var to: String
        var text: String
        var isEditing = false
        var isSending = false
        var sent = false
        var status = ""
    }

    // One tool invocation within an assistant turn — the structured record
    // behind the human-readable `activity` log line, kept for debug export.
    // A debug_capture record a tool recorded about its OWN internals while it
    // ran — e.g. summarize_emails/summarize_messages record the raw source
    // lines fed to their own internal model call ("source") plus that call's
    // exact request/response ("model_call"); create_tool's prepare_draft
    // records its own code-generation call's request/response. See
    // service/debug_capture.py. Kept as one
    // pretty-JSON blob per record (like argsJSON below) rather than a typed
    // breakdown, since the shape genuinely varies by kind.
    struct ToolDebugRecord: Identifiable {
        let id = UUID()
        var kind: String = ""     // "source" | "model_call"
        var json: String = ""
    }

    struct ToolCallRecord: Identifiable {
        let id = UUID()
        var callId: String = ""       // correlates the tool_call/tool_result SSE pair
        var name: String = ""
        var argsJSON: String = ""     // raw args, pretty-printed JSON
        var decision: String = ""     // "allow" | "confirm" | "deny"
        var decisionReason: String = ""
        var result: String = ""
        var debugRecords: [ToolDebugRecord] = []
    }

    // Exactly what was sent to and returned by the model for one completion
    // call (`raw_model_io` SSE event) — a turn can carry several of these,
    // one per agent-loop step/attempt. Request includes the full messages
    // array (system prompt + history) and tool schemas; response is either
    // the real wire response (non-streaming reasoning path) or the
    // reassembled content/reasoning_content/tool_calls (streamed paths).
    struct RawModelIO: Identifiable {
        let id = UUID()
        var model: String = ""
        var requestJSON: String = ""
        var responseJSON: String = ""
    }

    // One exchange in the conversation.
    struct Turn: Identifiable {
        let id = UUID()
        let role: String   // "user" or "assistant"
        var text: String
        var activity: [String] = []   // tool-call log for this turn, shown above its answer
        var isError: Bool = false
        // Raw exception text behind an error turn (see service/errors.py) —
        // never shown as the headline, only folded into the debug export so
        // a real diagnosis doesn't require reproducing the failure.
        var errorDetail: String? = nil
        var isDailySummary: Bool = false   // see runDailySummary/"daily_brief": replaced, never stacked

        // Debug metadata — populated on assistant turns only (see handle()'s
        // "done"/"error" cases). Everything a debug-mode export needs to
        // reconstruct exactly what happened for this exchange.
        var model: String? = nil
        var routeRole: String? = nil        // router's role classification: fast/agent/general/coding/reasoning
        var routeReason: String? = nil      // WHY the router picked that role/model
        var routeSource: String? = nil      // rules/fallback/default
        var neededTools: Bool? = nil
        var tokPerSec: Int? = nil
        var startedAt: Date? = nil          // when this turn's request was sent
        var firstTokenAt: Date? = nil       // first delta/reasoning/text token received
        var finishedAt: Date? = nil         // when "done"/"error" arrived
        var timeToFirstTokenSec: Double? {
            guard let s = startedAt, let f = firstTokenAt else { return nil }
            return f.timeIntervalSince(s)
        }
        var durationSec: Double? {
            guard let s = startedAt, let f = finishedAt else { return nil }
            return f.timeIntervalSince(s)
        }
        var heartbeatCount: Int = 0
        var reasoningText: String = ""
        var toolCalls: [ToolCallRecord] = []
        var rawIO: [RawModelIO] = []
    }

    @Published var input = ""
    @Published var answer = ""
    @Published var reasoning = ""
    @Published var showReasoning = false
    @Published var role = ""
    @Published var modelAbbrev = ""
    @Published var routeSource = ""
    @Published var tokPerSec = 0
    @Published var phase: Phase = .idle
    // Human-readable "what's actually happening right now" (e.g. "Loading
    // Agents-A1-4B… (3s)" during a cold model swap) — see the backend's
    // `status` SSE event (OMLXClient.ensure_only). Overrides the generic
    // phase-based processingLabel below while non-empty, so a long silent
    // stretch names the real cause instead of just sitting on "Thinking…".
    @Published var statusText = ""
    @Published var activity: [String] = []
    @Published var pending: Pending?
    @Published var messageDraft: MessageDraft?
    @Published var attachedImageName: String?
    @Published var researchMode = false
    // Once a research question is submitted, its plan, progress, sources, and
    // report replace the chat surface inside the main Wisp panel. Research is
    // a mode of Wisp, not a separate destination window.
    @Published var showingResearch = false
    @Published var collapsed = false { didSet { onCollapsedChanged() } }
    @Published var turns: [Turn] = []      // conversation history
    @Published var heartbeats = 0          // keepalive pings during long silent generation (e.g. reasoning)
    // Current-source reads, including a sync that outlives a tool/summary's
    // bounded wait. Kept visible after "still syncing" until readers finish.
    @Published var dailySyncProgress: Double?
    @Published var dailySyncLabel = ""
    @Published var sourceSyncStatuses: [WispClient.SourceSyncStatus] = []
    private var sourceSyncTask: Task<Void, Never>?
    private var sourceSyncID = UUID()
    private var trackedSyncSources: [String] = []
    private var dailySummaryRunning = false
    private var dailySummaryID = UUID()

    // Debug mode: shows a per-reply metadata line (model, route reason, tok/s,
    // timing, tool calls) inline in the transcript, and unlocks exporting the
    // full session as JSON/text from the menu bar. Persisted across launches.
    @Published var debugMode = UserDefaults.standard.bool(forKey: "wisp.debugMode") {
        didSet { UserDefaults.standard.set(debugMode, forKey: "wisp.debugMode") }
    }

    // --- assistant layer (calendar/reminders) ---
    // Upcoming commitments themselves surface as system notifications (see
    // Notifications.swift / handleAssistantEvent's "reminder" case) rather
    // than a live in-panel list.
    @Published var summaryPeriod = "AM"    // scheduled daily-brief time: AM(8am) / PM(8pm)
    private var assistantTask: Task<Void, Never>?
    // The last scheduled brief this session notified about. The SSE stream is
    // re-subscribed after any drop, so the same brief can arrive twice.
    private var lastDailyBriefText = ""

    var onResize: () -> Void = {}
    // Collapse/expand resizes animate; streaming resizes (onResize) are instant.
    var onCollapsedChanged: () -> Void = {}
    // Set by the app delegate: expand / collapse WITH the drop animation.
    // The view calls these instead of flipping `collapsed` directly, so the
    // delegate can roll the panel up before swapping to the bar.
    var requestExpand: () -> Void = {}
    var requestCollapse: () -> Void = {}

    // Physical notch geometry, set once at launch (0 on non-notched Macs).
    var notchWidth: CGFloat = 0
    var notchInset: CGFloat = 0
    // True while the attach-file dialog is up — it takes key focus from the
    // panel, which must not be mistaken for "the user switched apps".
    var pickingFile = false

    // True while auto-collapse should hold off: mid-task, pending confirm,
    // or the user has typed something they haven't sent. Checked once, right
    // when the cursor leaves the panel — collapse is otherwise immediate, no
    // idle timer.
    var busy: Bool {
        switch phase {
        case .routing, .working, .streaming, .confirming: return true
        default: return !input.trimmingCharacters(in: .whitespaces).isEmpty
        }
    }

    // The compact welcome menu surfaces the main workflows directly below the
    // prompt. Research belongs here alongside Daily Summary — not only in the
    // header chip — so it is discoverable before the user has started a chat.
    let suggestions = ["Daily summary", "Research a topic", "Organize files", "Summarize a PDF", "Describe an image"]

    private let client = WispClient()
    private var sessionId = ""
    private var attachedImage: String?
    private var streamStart: Date?
    private var streamChars = 0
    // Coalesces rapid delta chunks into ~60ms visual updates instead of
    // publishing `answer` (and triggering a full MarkdownView re-parse +
    // re-render) on every single raw SSE token. Individual model token
    // chunks can arrive many times a second; batching them is what makes
    // text feel like it's flowing in smoothly rather than jittering token by
    // token. Never delays more than one tick — see flushPendingDelta().
    private var pendingDelta = ""
    private var flushScheduled = false

    // Debug metadata for the turn currently in flight — folded into a Turn
    // record on "done"/"error" (see handle()). Reset in submit().
    private var turnStartedAt: Date?
    private var turnFirstTokenAt: Date?
    private var turnModel: String?
    private var turnRouteRole: String?
    private var turnRouteReason: String?
    private var turnNeededTools: Bool?
    private var turnToolCalls: [ToolCallRecord] = []
    private var turnRawIO: [RawModelIO] = []

    var healthy: Bool { phase != .error }

    // True between submit and the first visible ANSWER token — drives the
    // "working" indicator. Keyed on `answer` alone (not `reasoning`): reasoning
    // starting doesn't mean the model is about to answer — there's often a
    // long silent gap between reasoning finishing and the answer starting to
    // stream. Previously isProcessing flipped false as soon as reasoning had
    // any text, which hid the ONLY status indicator right during that gap —
    // the reasoning disclosure is collapsed by default, so the UI looked like
    // a dead void with nothing visibly happening.
    var isProcessing: Bool {
        switch phase {
        case .routing, .working: return true
        case .streaming: return answer.isEmpty
        default: return false
        }
    }

    var processingLabel: String {
        if !statusText.isEmpty { return statusText }
        switch phase {
        case .routing: return "Routing…"
        case .working:
            let base = activity.isEmpty ? "Working…" : "Running tools…"
            // A slow tool (e.g. a screen capture) can run silent for 30-40s+ with
            // no new activity line — show elapsed time so it doesn't look frozen.
            // Agent-loop heartbeats fire every ~1.5s (loop.py's _run_step).
            let elapsed = Int(Double(heartbeats) * 1.5)
            return heartbeats > 0 ? "\(base) (\(elapsed)s)" : base
        default:
            let base = modelAbbrev.isEmpty ? "Thinking…" : "Thinking with \(modelAbbrev)…"
            // Hard reasoning prompts can run silent for a while; heartbeats prove
            // it's still working rather than stuck.
            return heartbeats > 0 ? "\(base) (\(heartbeats * 12)s)" : base
        }
    }

    var routeLabel: String {
        switch routeSource {
        case "air_router": return "Air router"
        case "local_router": return "Local router"
        case "fallback": return "Local fallback"
        case "rules": return "Local rules"
        case "default": return "Direct to OSS"
        default: return ""
        }
    }

    func submit() {
        let prompt = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, !isProcessing else { return }
        stopSyncProgress()
        if researchMode {
            input = ""
            showingResearch = true
            onStartResearch?(prompt)
            return
        }
        turns.append(Turn(role: "user", text: prompt))
        input = ""
        answer = ""; reasoning = ""; showReasoning = false; activity = []
        role = ""; modelAbbrev = ""; routeSource = ""; statusText = ""
        tokPerSec = 0; pending = nil; messageDraft = nil
        streamStart = nil; streamChars = 0; heartbeats = 0
        pendingDelta = ""; flushScheduled = false
        turnStartedAt = Date(); turnFirstTokenAt = nil
        turnModel = nil; turnRouteRole = nil; turnRouteReason = nil; turnNeededTools = nil
        turnToolCalls = []
        turnRawIO = []
        phase = .routing
        let image = attachedImage
        let sid = sessionId   // continue the same conversation (empty -> server starts one)
        let wantsDebug = debugMode
        Task { [weak self] in
            await self?.client.runAgent(prompt: prompt, image: image, sessionId: sid,
                                        debug: wantsDebug) { ev in
                Task { @MainActor in self?.handle(ev) }
            }
        }
    }

    // Start a brand-new conversation: drop the session so the server opens a fresh one.
    func newChat() {
        sessionId = ""
        reset()
    }

    func pick(_ s: String) {
        // "Daily summary" reuses the SAME dedicated brief endpoint as the
        // header button (see runDailySummary/build_daily_brief on the
        // backend), which covers calendar + email + messages together in one
        // pass. This used to instead fill the input with a hand-written
        // calendar-only prompt and send it through the plain chat pipeline —
        // the router correctly classified that literal text as a calendar-
        // only read (it never mentioned email or messages), so the user got
        // just their schedule and had to separately ask "and my email" /
        // "what's on my messages" as follow-ups.
        if s == "Daily summary" {
            runDailySummary()
            return
        }
        if s == "Research a topic" {
            researchMode = true
            return
        }
        input = s
        submit()
    }

    // `scope`: "once" answers just this call; "always"/"never" also record a
    // standing grant so Wisp stops asking about this tool and target.
    func resolve(_ approved: Bool, scope: String = "once") {
        guard let p = pending else { return }
        pending = nil
        phase = .working
        Task {
            await client.approve(sessionId: p.sessionId, actionId: p.actionId,
                                 approved: approved, scope: scope)
        }
    }

    func setDraftText(_ text: String) {
        messageDraft?.text = text
    }

    func toggleDraftEditing() {
        guard messageDraft?.sent != true, messageDraft?.isSending != true else { return }
        messageDraft?.isEditing.toggle()
    }

    func discardMessageDraft() {
        guard messageDraft?.isSending != true else { return }
        messageDraft = nil
        onResize()
    }

    func sendMessageDraft() {
        guard var draft = messageDraft, !draft.isSending, !draft.sent else { return }
        guard !draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            messageDraft?.status = "The message is empty."
            return
        }
        draft.isEditing = false
        draft.isSending = true
        draft.status = "Sending…"
        messageDraft = draft
        let draftID = draft.id
        Task { [weak self] in
            guard let self else { return }
            let outcome = await client.sendMessageDraft(to: draft.to, text: draft.text)
            guard self.messageDraft?.id == draftID else { return }
            self.messageDraft?.isSending = false
            self.messageDraft?.sent = outcome.ok
            self.messageDraft?.status = outcome.ok ? "Sent" : outcome.result
            self.onResize()
        }
    }

    func attach(name: String, dataURL: String) {
        attachedImageName = name
        attachedImage = dataURL
    }

    func reset() {
        dailySummaryID = UUID()
        dailySummaryRunning = false
        stopSyncProgress()
        input = ""; answer = ""; reasoning = ""; activity = []
        routeSource = ""; statusText = ""
        attachedImage = nil; attachedImageName = nil; messageDraft = nil; phase = .idle
        collapsed = false; turns = []
    }

    /// Leave the inline Research surface without cancelling its persisted job.
    /// The backend run may continue; starting a new research prompt returns to
    /// this same in-panel workflow.
    func returnToChat() {
        showingResearch = false
        researchMode = false
    }

    // Writes the full current conversation — every turn's text, model, route
    // decision, timing, token rate, tool calls, and any errors — to disk as
    // both a complete JSON file and a human-readable companion transcript.
    // Debug metadata is tracked unconditionally (see applyDebugFields), not
    // just while Debug Mode is on, so this captures the whole session even if
    // Debug Mode was only switched on partway through. Returns the JSON file's
    // URL (nil if there's nothing to export yet) so the caller can reveal it
    // in Finder.
    func exportDebugLog() -> URL? {
        guard !turns.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        func rounded(_ v: Double) -> Double { (v * 100).rounded() / 100 }

        func turnJSON(_ t: Turn) -> [String: Any] {
            var d: [String: Any] = ["role": t.role, "text": t.text]
            if t.isError { d["is_error"] = true }
            if let detail = t.errorDetail { d["error_detail"] = detail }
            if !t.activity.isEmpty { d["activity_log"] = t.activity }
            if let m = t.model { d["model"] = m }
            if let r = t.routeRole { d["route_role"] = r }
            if let r = t.routeReason { d["route_reason"] = r }
            if let r = t.routeSource { d["route_source"] = r }
            if let n = t.neededTools { d["needed_tools"] = n }
            if let tps = t.tokPerSec { d["tok_per_sec"] = tps }
            if let s = t.startedAt { d["started_at"] = iso.string(from: s) }
            if let f = t.firstTokenAt { d["first_token_at"] = iso.string(from: f) }
            if let fin = t.finishedAt { d["finished_at"] = iso.string(from: fin) }
            if let ttft = t.timeToFirstTokenSec { d["time_to_first_token_sec"] = rounded(ttft) }
            if let dur = t.durationSec { d["duration_sec"] = rounded(dur) }
            if t.heartbeatCount > 0 { d["heartbeats"] = t.heartbeatCount }
            if !t.reasoningText.isEmpty { d["reasoning"] = t.reasoningText }
            if !t.toolCalls.isEmpty {
                d["tool_calls"] = t.toolCalls.map { tc -> [String: Any] in
                    var td: [String: Any] = ["name": tc.name, "args": tc.argsJSON, "decision": tc.decision,
                                             "decision_reason": tc.decisionReason, "result": tc.result]
                    if !tc.debugRecords.isEmpty {
                        // What the tool itself saw/sent internally — e.g. the
                        // real emails/messages fed to summarize_emails's own
                        // synthesis call, or create_tool's own code-generation
                        // request. See service/debug_capture.py.
                        td["debug"] = tc.debugRecords.map { Self.parsedJSON($0.json) }
                    }
                    return td
                }
            }
            if !t.rawIO.isEmpty {
                // Parsed back into real JSON objects (not left as strings) so
                // the exported .json is directly queryable/greppable, e.g.
                // with jq — mirrors how every other field here is native JSON.
                d["raw_model_io"] = t.rawIO.map { io -> [String: Any] in
                    ["model": io.model,
                     "request": Self.parsedJSON(io.requestJSON),
                     "response": Self.parsedJSON(io.responseJSON)]
                }
            }
            return d
        }

        let payload: [String: Any] = [
            "exported_at": iso.string(from: Date()),
            "session_id": sessionId,
            "turn_count": turns.count,
            "turns": turns.map(turnJSON),
        ]
        guard let jsonData = try? JSONSerialization.data(
            withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) else { return nil }

        // Human-readable companion — same data, laid out to actually read.
        let clock = DateFormatter(); clock.dateFormat = "HH:mm:ss"
        var text = "Wisp debug export\n"
        text += "Exported: \(iso.string(from: Date()))\n"
        text += "Session: \(sessionId)\n"
        text += String(repeating: "=", count: 70) + "\n\n"
        for t in turns {
            if t.role == "user" {
                let ts = t.startedAt.map { "[\(clock.string(from: $0))] " } ?? ""
                text += "── USER \(ts)──\n\(t.text)\n\n"
                continue
            }
            var header = t.isError ? "── ERROR" : "── ASSISTANT"
            if let m = t.model { header += " — \(m)" }
            if let r = t.routeRole {
                header += " (\(r)" + (t.routeSource.map { " · \($0)" } ?? "") + ")"
            }
            text += header + " ──\n"
            if let reason = t.routeReason { text += "route reason: \(reason)\n" }
            if let detail = t.errorDetail { text += "raw: \(detail)\n" }
            var metrics: [String] = []
            if let d = t.durationSec { metrics.append(String(format: "%.1fs total", d)) }
            if let ttft = t.timeToFirstTokenSec { metrics.append(String(format: "%.1fs to first token", ttft)) }
            if let tps = t.tokPerSec { metrics.append("\(tps) tok/s") }
            if t.heartbeatCount > 0 { metrics.append("\(t.heartbeatCount) heartbeats") }
            if !metrics.isEmpty { text += metrics.joined(separator: " · ") + "\n" }
            for tc in t.toolCalls {
                text += "  tool: \(tc.name) [\(tc.decision)]"
                if !tc.decisionReason.isEmpty { text += " — \(tc.decisionReason)" }
                text += "\n"
                if tc.argsJSON != "{}" { text += "    args: \(tc.argsJSON)\n" }
                if !tc.result.isEmpty { text += "    result: \(tc.result.prefix(300))\n" }
                for rec in tc.debugRecords {
                    let label = rec.kind.isEmpty ? "debug" : rec.kind
                    text += "    ── \(label) ──\n"
                    text += rec.json.split(separator: "\n").map { "      \($0)" }.joined(separator: "\n") + "\n"
                }
            }
            if !t.reasoningText.isEmpty { text += "  reasoning: \(t.reasoningText)\n" }
            for (i, io) in t.rawIO.enumerated() {
                text += "  ── raw model I/O #\(i + 1)"
                if !io.model.isEmpty { text += " (\(io.model))" }
                text += " ──\n"
                text += "  request:\n" + io.requestJSON.split(separator: "\n").map { "    \($0)" }.joined(separator: "\n") + "\n"
                text += "  response:\n" + io.responseJSON.split(separator: "\n").map { "    \($0)" }.joined(separator: "\n") + "\n"
            }
            text += "\n\(t.text)\n\n"
        }

        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        let stamp = DateFormatter(); stamp.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let base = "wisp-debug-\(stamp.string(from: Date()))"
        let jsonURL = dir.appendingPathComponent("\(base).json")
        let textURL = dir.appendingPathComponent("\(base).txt")
        try? jsonData.write(to: jsonURL)
        try? text.write(to: textURL, atomically: true, encoding: .utf8)
        return jsonURL
    }

    private func handle(_ ev: WispClient.Event) {
        // Any event other than another `status` means whatever it was
        // narrating (a model load/swap — see ensure_only) is over or was
        // superseded by real progress; clear it so processingLabel falls
        // back to the normal phase-based text instead of going stale.
        if ev.type != "status" { statusText = "" }
        switch ev.type {
        case "session": sessionId = ev.str("id")
        case "status": statusText = ev.str("text")
        case "routed":
            role = ev.str("role")
            modelAbbrev = Self.abbrev(ev.str("model"))
            routeSource = ev.str("route_source")
            turnModel = ev.str("model")
            turnRouteRole = ev.str("role")
            turnRouteReason = ev.str("reason")
            turnNeededTools = ev.bool("needs_tools")
            if routeSource == "air_router" {
                activity.append("Air router - \(role)")
            } else if routeSource == "fallback" {
                activity.append("Air router unavailable - local fallback")
            }
            phase = ev.bool("needs_tools") ? .working : .streaming
        case "tool_call":
            let mark = ["allow": "ok", "confirm": "needs ok", "deny": "blocked"][ev.str("decision")] ?? ""
            activity.append("\(ev.str("name")) — \(mark)")
            var rec = ToolCallRecord()
            rec.callId = ev.str("id")
            rec.name = ev.str("name")
            rec.argsJSON = Self.prettyJSON(ev.payload["args"])
            rec.decision = ev.str("decision")
            rec.decisionReason = ev.str("reason")
            turnToolCalls.append(rec)
            if turnFirstTokenAt == nil { turnFirstTokenAt = Date() }
        case "raw_model_io":
            // Exactly what was sent to and returned by the model for this
            // completion call — see loop.py's _run_step / main.py's reasoning
            // and general/fast/coding branches. Tracked unconditionally
            // (like the rest of debug metadata), surfaced only in the
            // Debug Mode export (see exportDebugLog).
            var io = RawModelIO()
            io.model = ev.str("model")
            io.requestJSON = Self.prettyJSON(ev.payload["request"])
            io.responseJSON = Self.prettyJSON(ev.payload["response"])
            turnRawIO.append(io)
        case "clear_answer":
            // The backend detected that the model streamed some preamble
            // content before deciding to call a tool (e.g. "let me check
            // that..." right before the actual tool_call) — that text isn't
            // the real answer, so discard whatever got rendered from it
            // before the tool result (or the next step's narration) lands.
            // Also resets the timing/rate metrics that stream was skewing —
            // they'll be recomputed fresh from the genuine answer.
            answer = ""; pendingDelta = ""; flushScheduled = false
            streamStart = nil; streamChars = 0; tokPerSec = 0
            turnFirstTokenAt = nil
        case "confirm":
            pending = Pending(sessionId: sessionId, actionId: ev.str("id"),
                              tool: ev.str("tool"), reason: ev.str("reason"),
                              // Older backends don't send `grantable`; default
                              // to hiding the button rather than showing one
                              // whose answer wouldn't be recorded.
                              grantable: ev.payload["grantable"] as? Bool ?? false,
                              scopeHint: ev.str("scope_hint"),
                              preview: ev.str("preview"))
            phase = .confirming
        case "message_draft":
            messageDraft = MessageDraft(to: ev.str("to"), text: ev.str("text"))
            phase = .streaming
        case "confirm_timeout":
            // Backend gave up waiting (see approver.py) and auto-denied so the
            // turn — and the daily brief / profile rotation gated behind it —
            // isn't stuck forever. If the card for this exact action is still
            // showing, clear it rather than leaving it answering into the void.
            if pending?.actionId == ev.str("id") {
                pending = nil
                activity.append("⏱ Confirmation timed out — denied automatically")
            }
        case "tool_result":
            activity.append("→ \(ev.str("result").prefix(60))")
            let cid = ev.str("id")
            if let idx = turnToolCalls.lastIndex(where: { $0.callId == cid }) {
                turnToolCalls[idx].result = ev.str("result")
                // Only present when the tool itself recorded something about
                // its own internals (debug_capture.record) — most tools never
                // set this. Each entry is {"kind": "source"|"model_call", ...}.
                if let debug = ev.payload["debug"] as? [[String: Any]] {
                    turnToolCalls[idx].debugRecords = debug.map { rec in
                        ToolDebugRecord(kind: rec["kind"] as? String ?? "",
                                        json: Self.prettyJSON(rec))
                    }
                }
            }
        case "heartbeat":
            heartbeats += 1
        case "reasoning":
            reasoning = ev.str("text"); phase = .streaming
            if turnFirstTokenAt == nil { turnFirstTokenAt = Date() }
        case "delta":
            if streamStart == nil { streamStart = Date() }
            if turnFirstTokenAt == nil { turnFirstTokenAt = Date() }
            let t = ev.str("text"); streamChars += t.count
            if let s = streamStart {
                let el = Date().timeIntervalSince(s)
                if el > 0.3 { tokPerSec = Int(Double(streamChars) / 4.0 / el) }
            }
            pendingDelta += t
            scheduleFlush()
            phase = .streaming
            // Return early instead of falling through to the trailing
            // onResize() below: `answer` didn't actually change yet (it's
            // buffered — see scheduleFlush), so resizing now would repeat the
            // same native panel resize on every raw token instead of once per
            // visual update. flushPendingDelta() calls onResize() itself at
            // the point content actually changes.
            return
        case "text":
            // Flush first: preserves ordering if any throttled delta text is
            // still buffered (defensive — the backend doesn't currently mix
            // delta and text events in one turn, but this keeps it correct
            // regardless).
            flushPendingDelta()
            if turnFirstTokenAt == nil { turnFirstTokenAt = Date() }
            answer += ev.str("text"); phase = .streaming
        case "error":
            // A "dropped" error that arrived before ANY reply content (no route,
            // no tokens, no tool activity) is almost always a stale-socket close,
            // not a real failure. Silently restore the prompt and remove the dead
            // user bubble so a single Return re-sends it — instead of leaving an
            // orphan question and forcing a retype. We do NOT auto-resend, since a
            // tool command ("delete …") may already have run server-side.
            flushPendingDelta()
            let gotNothing = answer.isEmpty && activity.isEmpty && role.isEmpty
            if ev.bool("dropped") && gotNothing {
                if let last = turns.last, last.role == "user" {
                    input = last.text
                    turns.removeLast()
                }
                phase = .idle
            } else {
                let msg = ev.str("message").isEmpty ? "Something went wrong." : ev.str("message")
                phase = .error
                // Persist into history immediately (previously the error only
                // lived in the transient `answer` binding and was silently lost
                // the moment the next message was sent) — debug export needs
                // to see exactly what failed, when, and under which route/model.
                var t = Turn(role: "assistant", text: msg, activity: activity, isError: true)
                let detail = ev.str("detail")
                t.errorDetail = detail.isEmpty ? nil : detail
                applyDebugFields(to: &t)
                turns.append(t)
                answer = ""; activity = []
            }
        case "done":
            // Flush BEFORE finalizing — without this, any text still sitting
            // in the throttle buffer (up to ~60ms worth) would be silently
            // dropped instead of appearing in the finished reply.
            flushPendingDelta()
            // Move the finished reply into the conversation history so the next
            // turn renders below it. Carry this turn's tool-call activity along
            // with it (and clear the live copy) — previously `activity` was
            // never reset here, so it lingered and rendered as dangling,
            // unattributed text below the NEXT turn instead of being shown,
            // correctly ordered, above the turn it actually belongs to.
            if !answer.isEmpty {
                var t = Turn(role: "assistant", text: answer, activity: activity)
                applyDebugFields(to: &t)
                turns.append(t)
                answer = ""
            }
            activity = []
            phase = .done
        default: break
        }
        onResize()
    }

    // Folds this turn's tracked debug metadata into the Turn being finalized.
    private func applyDebugFields(to turn: inout Turn) {
        turn.model = turnModel
        turn.routeRole = turnRouteRole
        turn.routeReason = turnRouteReason
        turn.routeSource = routeSource.isEmpty ? nil : routeSource
        turn.neededTools = turnNeededTools
        turn.tokPerSec = tokPerSec > 0 ? tokPerSec : nil
        turn.startedAt = turnStartedAt
        turn.firstTokenAt = turnFirstTokenAt
        turn.finishedAt = Date()
        turn.heartbeatCount = heartbeats
        turn.reasoningText = reasoning
        turn.toolCalls = turnToolCalls
        turn.rawIO = turnRawIO
    }

    // Pretty-prints a tool call's `args` payload (an arbitrary JSON-ish `Any`
    // from the SSE event) for the debug log — falls back to a plain string
    // description if it isn't valid JSON-serializable data.
    private static func prettyJSON(_ value: Any?) -> String {
        guard let value else { return "{}" }
        guard JSONSerialization.isValidJSONObject(value),
              let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
              let s = String(data: data, encoding: .utf8)
        else { return "\(value)" }
        return s
    }

    // Reverses prettyJSON for the .json export: re-parses a pretty-printed
    // JSON string back into a native object/array so raw_model_io round-trips
    // as real JSON rather than a doubly-encoded string. Falls back to the raw
    // string if it somehow isn't parseable (shouldn't happen — it came from
    // prettyJSON itself — but the export must not crash over a debug field).
    private static func parsedJSON(_ s: String) -> Any {
        guard let data = s.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        else { return s }
        return obj
    }

    // Throttle: at most one `answer` mutation (and the MarkdownView re-parse +
    // re-render it triggers) per ~60ms, no matter how many raw delta chunks
    // arrive in that window. 60ms is short enough that streaming still reads
    // as immediate, but long enough to coalesce a burst of same-tick token
    // chunks into one visual update instead of many — this plus the stable
    // block IDs in MarkdownView are what make streamed text look like it's
    // flowing in smoothly rather than jittering token by token.
    private func scheduleFlush() {
        guard !flushScheduled else { return }
        flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self] in
            self?.flushPendingDelta()
        }
    }

    private func flushPendingDelta() {
        flushScheduled = false
        guard !pendingDelta.isEmpty else { return }
        answer += pendingDelta
        pendingDelta = ""
        onResize()
    }

    // --- assistant layer -------------------------------------------------
    // Called once at launch: do a first fetch and subscribe to the reminder
    // stream (auto-reconnecting). Reminders fire as system notifications —
    // see handleAssistantEvent's "reminder" case.
    func startAssistant() {
        refreshAssistant()
        assistantTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.client.assistantEvents { ev in
                    Task { @MainActor in self?.handleAssistantEvent(ev) }
                }
                // Stream ended (backend restart) — back off, then re-subscribe.
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    func refreshAssistant() {
        Task { [weak self] in
            self?.summaryPeriod = await self?.client.summarySchedule() ?? "AM"
        }
    }

    // Daily Summary button: fetch the combined calendar+email brief and show it
    // as an assistant turn (uses the fast model directly, not the agent loop).
    func runDailySummary() {
        guard !isProcessing else { return }
        requestExpand()
        // A summary is a snapshot, not a running log entry — an old one left
        // sitting in the transcript (e.g. from the scheduled 8am/8pm push,
        // never cleared because only the X button resets `turns`) reads as
        // current when it's really from hours or a day earlier. Drop any
        // prior daily-summary turns before adding this one, so there is only
        // ever the latest.
        turns.removeAll { $0.isDailySummary }
        turns.append(Turn(role: "user", text: "Daily summary", isDailySummary: true))
        answer = ""; reasoning = ""; activity = []
        statusText = "Checking your data…"
        dailySummaryRunning = true
        let summaryID = UUID()
        dailySummaryID = summaryID
        startSyncProgress(sources: ["calendar", "reminders", "email", "messages"])
        phase = .working
        Task { [weak self] in
            guard let self else { return }
            let result = await self.client.dailySummary(sessionId: self.sessionId)
            let text = result.text ?? "Couldn't build a summary right now."
            guard self.dailySummaryID == summaryID else { return }
            // Adopt the session the brief was recorded in, so a follow-up
            // ("send this to Trishe") continues the conversation it is in.
            if !result.sessionId.isEmpty { self.sessionId = result.sessionId }
            self.dailySummaryRunning = false
            self.statusText = ""
            // Do not stop source polling here. A "still syncing" response is
            // a bounded wait, not completion of the background sync itself.
            self.turns.append(Turn(role: "assistant", text: text, isDailySummary: true))
            self.phase = .done
            self.onResize()
        }
    }

    private func stopSyncProgress() {
        sourceSyncTask?.cancel()
        sourceSyncTask = nil
        sourceSyncID = UUID()
        trackedSyncSources = []
        dailySyncProgress = nil
        dailySyncLabel = ""
        sourceSyncStatuses = []
    }

    private func startSyncProgress(sources: [String]) {
        // Several tools can request reads concurrently for an aggregate task.
        // Keep every pending source in the same bar, not just the last event.
        var wanted = (!dailySummaryRunning && (dailySyncProgress ?? 1) < 1)
            ? trackedSyncSources : []
        for source in sources where !wanted.contains(source) { wanted.append(source) }
        stopSyncProgress()
        trackedSyncSources = wanted
        let syncID = sourceSyncID
        dailySyncProgress = 0
        dailySyncLabel = "Checking synced data…"
        sourceSyncTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let status = await self.client.assistantSyncStatus(sources: wanted)
                guard !Task.isCancelled, self.sourceSyncID == syncID else { return }
                if let status {
                    self.sourceSyncStatuses = status.sources
                    self.dailySyncProgress = status.progress
                    self.dailySyncLabel = status.label
                    if status.pendingLabels.isEmpty {
                        if self.isProcessing {
                            // Keep the compact sync tip current while the
                            // summary is generated, including local-cache caveats.
                            if self.dailySummaryRunning {
                                self.statusText = "Building your daily summary…"
                            }
                        } else {
                            self.onResize()
                            return
                        }
                        // Keep checking during generation: a requested Mail
                        // refresh can replace a local-only snapshot and its
                        // warning after the first terminal status was shown.
                    }
                } else {
                    // Preserve the last measured fraction, not a fake 100%.
                    self.dailySyncLabel = "Reconnecting to check sync…"
                }
                self.onResize()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    func setSummaryPeriod(_ period: String) {
        summaryPeriod = period
        Task { [weak self] in _ = await self?.client.setSummarySchedule(period) }
    }

    // Set by the app delegate: create/remove real macOS Calendar events (the app
    // holds the Calendar grant; the backend can't touch Calendar directly).
    var onCreateCalendarEvent: ((_ title: String, _ startTs: Double,
                                 _ durationMin: Int, _ location: String) -> Void)?
    var onDeleteCalendarEvent: ((_ identifier: String, _ occurrenceTs: Double?) -> Void)?
    var onCreateAppleReminder: ((_ title: String, _ dueTs: Double) -> Void)?
    var onUpdateAppleReminder: ((_ identifier: String, _ oldTitle: String,
                                 _ oldDueTs: Double, _ title: String,
                                 _ dueTs: Double) -> Void)?
    var onDeleteAppleReminder: ((_ identifier: String) -> Void)?
    var onStartResearch: ((_ prompt: String) -> Void)?
    // Backend asks (via the assistant event stream) for an immediate Mail
    // re-sync when an email query hits a cold cache — beats waiting on the
    // MailReader's 5-min timer so the FIRST "check my emails" works.
    var onSyncEmails: (() -> Void)?
    // The backend can be restarted independently of the app, so its in-memory
    // readiness resets even when the readers already ran. This callback lets a
    // summary/tool request current source reads immediately instead of waiting
    // for the next 1-5 minute timer.
    var onSyncAssistantSources: (([String]) -> Void)?

    private func handleAssistantEvent(_ ev: WispClient.Event) {
        switch ev.type {
        case "reminder":
            postReminderNotification(ev)
        case "create_calendar_event":
            let ts = ev.payload["when_ts"] as? Double ?? 0
            let dur = ev.payload["duration_min"] as? Int ?? 60
            onCreateCalendarEvent?(ev.str("title"), ts, dur, ev.str("location"))
        case "delete_calendar_event":
            let ts = ev.payload["when_ts"] as? Double
            onDeleteCalendarEvent?(ev.str("source_id"), ts)
        case "create_apple_reminder":
            let ts = ev.payload["when_ts"] as? Double ?? 0
            onCreateAppleReminder?(ev.str("title"), ts)
        case "update_apple_reminder":
            onUpdateAppleReminder?(
                ev.str("source_id"), ev.str("old_title"),
                ev.payload["old_when_ts"] as? Double ?? 0,
                ev.str("title"), ev.payload["when_ts"] as? Double ?? 0)
        case "delete_apple_reminder":
            onDeleteAppleReminder?(ev.str("source_id"))
        case "sync_emails_now":
            if !dailySummaryRunning { startSyncProgress(sources: ["email"]) }
            onSyncEmails?()
        case "sync_assistant_sources_now":
            let sources = ev.payload["sources"] as? [String] ?? []
            if !dailySummaryRunning { startSyncProgress(sources: sources) }
            onSyncAssistantSources?(sources)
        case "send_email":
            // The user already approved this on a confirmation card; the
            // backend is blocked awaiting the result (see OutboundSender).
            OutboundSender.sendEmail(
                actionId: ev.str("action_id"),
                to: ev.payload["to"] as? [String] ?? [],
                cc: ev.payload["cc"] as? [String] ?? [],
                subject: ev.str("subject"), body: ev.str("body"))
        case "send_message":
            OutboundSender.sendMessage(
                actionId: ev.str("action_id"),
                to: ev.str("to"), text: ev.str("text"))
        case "scheduled_send_result":
            // A send the user approved earlier just fired on its own. They
            // weren't necessarily watching, so this is a notification rather
            // than a transcript line — especially on failure, where silence
            // would leave them believing it went out.
            let who = ev.str("display")
            let kind = ev.str("channel") == "email" ? "Email" : "Text"
            if ev.payload["ok"] as? Bool ?? false {
                Notifications.post(title: "✅ \(kind) sent", body: "Your scheduled \(kind.lowercased()) to \(who) went out.")
            } else {
                Notifications.post(title: "⚠️ Scheduled \(kind.lowercased()) failed",
                                   body: "Couldn't send to \(who): \(ev.str("error"))")
            }
        case "scheduled_send_missed":
            // Came due while Wisp wasn't running and is now too late to send
            // (see outbound_queue). Never delivered late and never dropped
            // silently — the user decides whether it still makes sense.
            let who = ev.str("display")
            let kind = ev.str("channel") == "email" ? "email" : "text"
            Notifications.post(title: "⏰ Scheduled \(kind) missed",
                               body: "Wisp wasn't running when your \(kind) to \(who) was due, so it wasn't sent.")
        case "scheduled_send_unknown":
            // Wisp was interrupted between handing the send to the bridge and
            // recording the result, so we genuinely do not know whether it
            // arrived. It is never retried — a duplicate the user didn't ask
            // for is worse than telling them to check.
            let who = ev.str("display")
            let kind = ev.str("channel") == "email" ? "email" : "text"
            Notifications.post(title: "❓ Scheduled \(kind) outcome unknown",
                               body: "Wisp was interrupted while sending your \(kind) to \(who). It wasn't sent again — check whether it arrived.")
        case "reply_to_email":
            OutboundSender.replyToEmail(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"), body: ev.str("body"),
                replyAll: ev.payload["reply_all"] as? Bool ?? false)
        case "draft_email":
            // No confirmation card for drafts — nothing is sent, and the
            // user's own click in Mail is the real gate (see policy.py).
            OutboundSender.draftEmail(
                actionId: ev.str("action_id"),
                to: ev.payload["to"] as? [String] ?? [],
                cc: ev.payload["cc"] as? [String] ?? [],
                subject: ev.str("subject"), body: ev.str("body"))
        case "draft_message":
            OutboundSender.draftMessage(
                actionId: ev.str("action_id"),
                to: ev.str("to"), text: ev.str("text"))
        case "mark_email_read":
            OutboundSender.markEmailRead(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"),
                read: ev.payload["read"] as? Bool ?? true)
        case "archive_email":
            OutboundSender.archiveEmail(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"))
        case "forward_email":
            OutboundSender.forwardEmail(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"),
                to: ev.payload["to"] as? [String] ?? [],
                body: ev.str("body"))
        case "flag_email":
            OutboundSender.flagEmail(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"),
                flagged: ev.payload["flagged"] as? Bool ?? true)
        case "email_summary":
            Notifications.post(title: "📧 Morning email summary", body: ev.str("summary"))
        case "daily_brief":
            // Scheduled 8am/8pm brief: content-ful notifications (calendar+email,
            // messages) plus the full write-up in the transcript for when the app
            // is opened.
            //
            // Every card here says something the user can act on without opening
            // Wisp, and NONE of them is posted twice for one brief. The old
            // fallback card — "Your daily summary is ready in Wisp." with no
            // content — fired whenever the backend couldn't split the brief,
            // which included the case where there was no brief to split because
            // the sources were still syncing. The backend no longer publishes
            // that (see brief.run_scheduled_brief), and the identical-text guard
            // below covers a re-publish reaching this same session.
            let briefText = ev.str("text")
            guard !briefText.isEmpty, briefText != lastDailyBriefText else { break }
            lastDailyBriefText = briefText
            let today = ev.str("today_summary")
            let messages = ev.str("messages_summary")
            if !today.isEmpty { Notifications.post(title: "📅 Today", body: today) }
            if !messages.isEmpty { Notifications.post(title: "💬 Messages", body: messages) }
            if today.isEmpty && messages.isEmpty {
                // No split available: notify with the brief's own opening lines
                // rather than announcing that something is ready elsewhere.
                let part = ev.str("part_of_day") == "evening" ? "Evening" : "Morning"
                Notifications.post(title: "🗞️ \(part) brief",
                                   body: Self.notificationBody(from: briefText))
            }
            // Same replace-not-stack rule as the manual button: this is the
            // scheduler's once-daily push, and without this it can sit in the
            // transcript across a sleep/wake or an unattended day and read as
            // "today's" summary when it's actually from the prior firing.
            turns.removeAll { $0.isDailySummary }
            turns.append(Turn(role: "assistant", text: briefText, isDailySummary: true))
        default:
            break
        }
    }

    /// The first few content lines of a rendered brief, as plain text.
    ///
    /// A notification renders no Markdown and fits a few lines, so the bold
    /// section headers, bullets and sign-off are stripped rather than shown as
    /// literal asterisks.
    static func notificationBody(from brief: String, maxLines: Int = 4) -> String {
        var lines: [String] = []
        for raw in brief.split(separator: "\n", omittingEmptySubsequences: true) {
            var line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            if line.hasPrefix("- ") { line = String(line.dropFirst(2)) }
            line = line.replacingOccurrences(of: "**", with: "")
                       .replacingOccurrences(of: "•", with: "")
                       .trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, !line.hasPrefix("Let me know") else { continue }
            lines.append(line)
            if lines.count == maxLines { break }
        }
        return lines.joined(separator: "\n")
    }

    private func postReminderNotification(_ ev: WispClient.Event) {
        let title = ev.str("title")
        let whenLabel = ev.str("when_label")
        let ctx = ev.str("context")
        let sub = [ctx, whenLabel].filter { !$0.isEmpty }.joined(separator: " · ")
        Notifications.post(title: title.isEmpty ? "Reminder" : title, body: sub)
    }

    static func abbrev(_ model: String) -> String {
        // Short badges for the routing chip. Unlisted models fall through to
        // their full id, which is correct but wide — add an entry when a model
        // becomes part of the standing roster.
        let map = [
            "Agents-A1-4B-oQe6": "A1-4B",
            "gemma-4-E4B-it-qat-4bit": "G4-E4B",
            "Qwen3.6-27B-oQ3": "Q3.6-27B",
            "LFM2.5-2.6B-MLX-6bit": "LFM-2.6B",
        ]
        return map[model] ?? model
    }
}
