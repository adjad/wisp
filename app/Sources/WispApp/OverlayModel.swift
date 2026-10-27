import Combine
import SwiftUI

@MainActor
final class OverlayModel: ObservableObject {
    enum Phase { case idle, routing, working, streaming, confirming, done, error }

    struct Pending: Identifiable {
        let id = UUID()
        let sessionId: String
        let actionId: String
        // The /agent request that raised this card. Action ids are not unique across
        // overlapping requests, so the answer names the request too and the backend
        // routes it to exactly that one. Empty from a backend that predates this.
        var requestId: String = ""
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
        var traceID: String? = nil
        var errorDetail: String? = nil
        var nodeResultID: String? = nil

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

    @Published var corpusBusy = false
    @Published var showingCorpus = false
    var onCorpusCommand: ((String) -> Void)?
    var onHideCorpus: () -> Void = {}
    var onResetCorpus: () -> Void = {}
    var onOpenCorpus: () -> Void = {}

    /// /resetwisp's confirmation card (ResetWisp.swift), nil when closed.
    @Published var resetCard: ResetCardState?
    /// Runs the reset after the Reset Wisp click (AppDelegate.performWispReset).
    /// Returns why it stopped before erasing anything; on success Wisp relaunches.
    var onResetWisp: (() async -> String)?

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
    @Published private(set) var attachedImageName: String?
    // The panel's tab. App-scoped, so it survives collapse.
    @Published var tab: NotchTab = .ask {
        didSet {
            guard tab != oldValue else { return }
            // A research plan holds the prompt queue only while it is on screen.
            // Leaving the tab lets a question asked in Ask, or a brief, run.
            if oldValue == .research { releaseResearchDispatch() }
            // Any way off Ask (⌘1-3, a deep link, a notification) ends the
            // questionnaire; its answers so far are already saved.
            if tab != .ask { leaveKnowYou() }
            showingQuickSettings = false
            onResize()
        }
    }
    private var tabMemory = NotchTabMemory()
    // The next prompt opens an editable research plan. This is the Research tab.
    var researchMode: Bool {
        get { tab == .research }
        set {
            if newValue { tab = .research } else if tab == .research { tab = .ask }
        }
    }
    // Once a research question is submitted, its plan, progress, sources, and
    // report replace the prompt on the Research tab. Research is a mode of
    // Wisp, not a separate destination window.
    @Published var showingResearch = false
    // The gear's short list of settings, shown in place of the tab's content.
    @Published var showingQuickSettings = false {
        didSet {
            guard showingQuickSettings != oldValue else { return }
            // The gear during Getting to know you: step out of the flow (its
            // answers so far are saved) rather than open settings behind it.
            if showingQuickSettings { leaveKnowYou() }
            onResize()
        }
    }
    @Published private(set) var freeingMemory = false
    /// Quick settings' Model memory row: what the engine has resident, nil when unknown.
    @Published private(set) var modelMemory: ModelMemory?
    // First-run setup at the top of Ask. Its readiness lives on the app's one
    // SetupGuideModel, which the setup window shares.
    @Published var setupCard: NotchSetupCard = .hidden { didSet { if setupCard != oldValue { onResize() } } }
    // Whether Wisp's own service answers (BackendManager's reachability watch).
    // False shows "Wisp service isn't running" with Retry instead of a green dot.
    @Published var serviceReachable = true {
        didSet {
            if serviceReachable { serviceStartFailure = nil }
            if serviceReachable != oldValue { onResize(); sendHeldForStartup() }
        }
    }
    // The service is coming up (a cold first launch, or after Retry): a neutral
    // "Starting Wisp…" line, never the red down card. True from launch until the
    // first reachability probe answers, so a question typed in the first seconds
    // is held, not sent into a service that isn't listening yet.
    @Published var serviceStarting = true {
        didSet { if serviceStarting != oldValue { onResize(); sendHeldForStartup() } }
    }
    /// Why the service's last start failed (BackendManager posts
    /// .wispBackendStartFailed: exit status and the log's last line). A recorded
    /// failure ends the "Starting Wisp…" hold at once: the down card shows the
    /// cause with Retry and Report instead of minutes of "Starting".
    @Published private(set) var serviceStartFailure: String? { didSet { if serviceStartFailure != oldValue { onResize() } } }
    var serviceDown: Bool { !serviceReachable && (!serviceStarting || serviceStartFailure != nil) }
    // Set by the app delegate: ask the backend manager to start the service again.
    var onRetryService: () -> Void = {}
    private var startFailureObserver: NSObjectProtocol?

    /// Retry from the service-down card or status line: forget the recorded
    /// failure (a new start may fix it) and start the service again.
    func retryService() {
        serviceStartFailure = nil
        onRetryService()
    }

    /// Prompts typed while the service was still starting. Sent, in order, as
    /// soon as it answers; the line beside the box says they are waiting.
    private var heldForStartup: [QueuedPrompt] = []

    private func sendHeldForStartup() {
        guard serviceReachable, !serviceStarting, !heldForStartup.isEmpty else { return }
        let held = heldForStartup
        heldForStartup = []
        for prompt in held {
            if let dispatch = submissionState.submit(prompt) { start(dispatch) }
        }
        syncQueue()
    }
    // The one problem report open in the notch, unfolded under the reply it is
    // about (or at the top of Ask when it isn't about a reply on screen).
    let report = ReportDraft()
    private var reportObserver: AnyCancellable?

    init(loadDebugMode: () -> Bool = { UserDefaults.standard.bool(forKey: "wisp.debugMode") },
         saveDebugMode: @escaping (Bool) -> Void = { UserDefaults.standard.set($0, forKey: "wisp.debugMode") },
         recentChatsLoader: ((Int) async -> [[String: Any]]?)? = nil) {
        self.saveDebugMode = saveDebugMode
        self.recentChatsLoader = recentChatsLoader
        _debugMode = Published(initialValue: loadDebugMode())
        // Rows that show the report read it through this model, so redraw with it.
        reportObserver = report.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        startFailureObserver = NotificationCenter.default.addObserver(
            forName: .wispBackendStartFailed, object: nil, queue: .main) { [weak self] note in
            let reason = note.userInfo?["reason"] as? String ?? ""
            MainActor.assumeIsolated {
                guard let self, !self.serviceReachable || self.serviceStarting else { return }
                // On a cold launch reachability is still its optimistic `true`
                // (no probe has answered yet), which would hide the down card.
                // A start that provably died means the service is not reachable;
                // the next probe that answers sets it back and clears the failure.
                self.serviceReachable = false
                self.serviceStartFailure = reason.isEmpty ? "Wisp's service stopped while starting." : reason
            }
        }
    }
    /// True while the panel retracts into the bar: the content fades out at
    /// the start of the close so the shrinking shape never slices live text.
    /// Cleared when the bar takes over or the panel opens again.
    @Published var closing = false

    @Published var collapsed = false {
        didSet {
            if collapsed && !oldValue {
                tabMemory.panelClosed(at: Date())
                showingQuickSettings = false
            }
            onCollapsedChanged()
        }
    }
    @Published var turns: [Turn] = []      // conversation history
    @Published var heartbeats = 0          // keepalive pings during long silent generation (e.g. reasoning)
    // Current-source reads, including a sync that outlives a tool/summary's
    // bounded wait. Kept visible after "still syncing" until readers finish.
    @Published var dailySyncProgress: Double?
    @Published var dailySyncLabel = ""
    @Published var sourceSyncStatuses: [WispClient.SourceSyncStatus] = []
    @Published private(set) var queuedPromptCount = 0
    /// The line beside the box while prompts wait: what they wait on, never silent.
    @Published private(set) var queueLabel = ""
    private var sourceSyncTask: Task<Void, Never>?
    private var sourceSyncID = UUID()
    private var trackedSyncSources: [String] = []
    private var dailySummaryRunning = false
    private var dailySummaryID = UUID()
    private var submissionState = PromptSubmissionState()
    private var researchDispatchID: UInt64?

    // Debug mode: shows a per-reply metadata line (model, route reason, tok/s,
    // timing, tool calls) inline in the transcript, and unlocks exporting the
    // full session as JSON/text from the menu bar. Persisted across launches.
    private let saveDebugMode: (Bool) -> Void
    private let recentChatsLoader: ((Int) async -> [[String: Any]]?)?
    @Published var debugMode = false {
        didSet { saveDebugMode(debugMode) }
    }

    // --- assistant layer (calendar/reminders) ---
    // Upcoming commitments themselves surface as system notifications (see
    // Notifications.swift / handleAssistantEvent's "reminder" case) rather
    // than a live in-panel list.
    // The latest Daily Summary brief, shown on the Brief tab: the scheduled one
    // from the service, or one composed with Refresh. It belongs to Today, not
    // to the chat, so New chat keeps it.
    struct DailyBrief: Equatable {
        let text: String
        let receivedAt: Date
        let traceID: String?
        // The saved chat the service recorded this brief in, so a follow-up can
        // continue from it. Empty for a scheduled brief, which is not recorded.
        let sessionId: String
    }
    @Published private(set) var brief: DailyBrief?
    @Published private(set) var briefComposing = false
    // The Day tab's unsent inline task. Kept here so it survives collapse.
    @Published var todayTaskTitle = ""
    // Behavior-engine nudges shown as cards at the top of the panel (newest
    // first, see NudgeViews.swift). Each also arrives as a notification.
    @Published var nudges: [BehaviorNudge] = []
    @Published var transientNotice: String?
    @Published private(set) var transientNoticeIsInfo = false
    // Getting to know you (KnowYouView.swift), replacing the tab content while
    // it runs. Nil when the flow is not on screen.
    @Published private(set) var knowYou: KnowYouModel?
    // The service said tonight's check-in is due; Today's card refreshes on it.
    @Published var checkInDue = false
    // Keep the panel open: no auto-collapse when the pointer leaves or focus
    // moves to another app. Cleared by any explicit collapse (see AppDelegate).
    @Published var pinned = false
    // Recent saved chats for the notch's chat switcher, newest first, without
    // the one open here. Refreshed each time the panel opens.
    @Published private(set) var recentChats: [ChatSummary] = []
    // The saved title of a chat reopened from the switcher; nil for a chat
    // started here, which is named after its first prompt.
    @Published private(set) var reopenedChatTitle: String?
    private var assistantTask: Task<Void, Never>?
    private var transientNoticeTask: Task<Void, Never>?
    // Scheduled briefs already shown on the Brief tab, by hub event id. The SSE
    // stream is re-subscribed after any drop, so the same brief can arrive twice.
    private var appliedBriefEvents: Set<String> = []

    /// Whether a scheduled brief event replaces the Brief tab's brief: not when
    /// it was already applied, and not when the brief shown is newer than it.
    static func appliesBrief(eventID: String, created: Date?, applied: Set<String>, current: DailyBrief?) -> Bool {
        if !eventID.isEmpty && applied.contains(eventID) { return false }
        if let created, let current, current.receivedAt > created { return false }
        return true
    }

    var onResize: () -> Void = {}
    /// Fit the window now, in this run-loop turn (onResize waits one turn).
    /// For a change whose animation starts on the next frame, so a grow lands
    /// before the first frame instead of after it.
    var onResizeNow: () -> Void = {}
    // Collapse/expand resizes animate; streaming resizes (onResize) are instant.
    var onCollapsedChanged: () -> Void = {}
    // Set by the app delegate: expand / collapse WITH the drop animation.
    // The view calls these instead of flipping `collapsed` directly, so the
    // delegate can roll the panel up before swapping to the bar.
    var requestExpand: () -> Void = {}
    var requestCollapse: () -> Void = {}

    // True while the attach-file dialog is up — it takes key focus from the
    // panel, which must not be mistaken for "the user switched apps".
    var pickingFile = false

    // True while auto-collapse must hold off: the panel is pinned, there is
    // an edit the person hasn't finished (typed input, a message draft being
    // edited), or the setup card is open. A mid-task generation does not count;
    // the notch bar's dot shows it is still working.
    var holdsOpen: Bool {
        pinned || !input.trimmingCharacters(in: .whitespaces).isEmpty || messageDraft?.isEditing == true
            || (tab == .day && !todayTaskTitle.trimmingCharacters(in: .whitespaces).isEmpty)
            || knowYou != nil
            || setupCard == .open || report.isOpen || (tab == .day && todayReport?.isOpen == true)
    }

    /// Today's report, owned by the shared TodayModel. While it is open on the
    /// Day tab the panel stays open, like any other edit in progress.
    weak var todayReport: ReportDraft?

    /// Saves a report from the notch. The save panel takes focus from the
    /// panel, which must not read as the person leaving.
    func saveReport(_ draft: ReportDraft) {
        pickingFile = true
        defer { pickingFile = false }
        draft.chooseLocationAndSave()
    }

    /// Which report a reply's "Report a problem" opens: its trace (or, for a
    /// reply without one, its row), so a rebuilt row finds the same draft.
    static func reportKey(for turn: Turn) -> String { "chat:" + (turn.traceID ?? turn.id.uuidString) }

    /// The open report isn't about any reply on screen (the last dropped turn,
    /// no reply yet, or a reply the chat no longer shows): show it at the top of Ask.
    var reportIsUnattached: Bool {
        guard let key = report.snapshot?.key, key.hasPrefix("chat:") else { return false }
        return !turns.contains { Self.reportKey(for: $0) == key }
    }

    /// Open Getting to know you in the panel (first launch, /about, the menu
    /// bar, About you's Revisit). Picks up where the person left off.
    func startKnowYou() {
        if knowYou == nil {
            let flow = KnowYouModel(client: client)
            flow.onClose = { [weak self, weak flow] notice in
                guard let self, let flow, self.knowYou === flow else { return }
                self.endKnowYou(notice: notice)
            }
            flow.onShowBrief = { [weak self, weak flow] in
                guard let self, let flow, self.knowYou === flow else { return }
                self.endKnowYou(notice: nil)
                self.runDailySummary()
            }
            knowYou = flow
        }
        showingQuickSettings = false
        tab = .ask
        requestExpand()
        onResize()
    }

    private func endKnowYou(notice: String?) {
        knowYou = nil
        onResize()
        if let notice { showTransientNotice(notice, duration: 8, info: true) }
    }

    /// The person moved on (a tab) before Done. The flow saves after every
    /// step, so nothing is lost; the service counts it as Later.
    func leaveKnowYou() {
        guard let flow = knowYou else { return }
        // Drop it now so a second gesture finds nothing to leave; tell the
        // service in the background.
        knowYou = nil
        onResize()
        Task { await flow.leave() }
    }

    /// Pick the tab for an opening panel: Ask, or for a hover soon after the
    /// panel closed, the tab it closed on. An open setup card keeps it on Ask.
    func prepareTab(byHover: Bool, at date: Date = Date()) {
        tab = setupCard.tabForOpening(tabMemory.tabForOpening(current: tab, byHover: byHover, at: date))
    }

    /// False while a turn is in flight. The Wisp window and the chat switcher
    /// read the saved history, which the service writes only at the end of a turn.
    var conversationSettled: Bool {
        !corpusBusy && ![Phase.routing, .working, .streaming, .confirming].contains(phase)
    }

    /// What the chat switcher calls the conversation open here.
    var chatTitle: String {
        if let reopenedChatTitle { return reopenedChatTitle }
        if let first = turns.first(where: { $0.role == "user" }) { return ChatTitle.make(from: first.text, limit: 40) }
        return "New chat"
    }

    /// Slash commands the Ask field's text could still become. /corpus only in Debug Mode.
    var commandSuggestions: [NotchCommand] { NotchCommand.suggestions(for: input, includeCorpus: debugMode) }

    /// Sources Wisp can't read right now ("calendar", "email"), refreshed each
    /// time the panel opens, so a starter never asks about a denied source.
    @Published private(set) var unreadableSources: Set<String> = []

    // The tappable questions under an empty Ask: ones a fresh install can
    // answer, even before any source is allowed (AgentSend.swift's AskStarters).
    var suggestions: [String] { AskStarters.prompts(unreadable: unreadableSources) }

    /// Starters not yet asked in this chat (derived from its user turns, so New chat resets them).
    var remainingSuggestions: [String] {
        AskStarters.remaining(suggestions, askedTexts: turns.filter { $0.role == "user" }.map(\.text))
    }

    /// The compact "Try another" row under a conversation: only while Ask is at rest
    /// (a finished reply is the last turn; nothing streaming, queued, typed, awaiting
    /// approval, drafted or being reported) and some starter is still unasked.
    var offersTryAnother: Bool {
        guard phase == .idle || phase == .done, turns.last?.role == "assistant",
              !isProcessing, !willQueuePrompt, answer.isEmpty,
              input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              pending == nil, messageDraft == nil, report.snapshot == nil
        else { return false }
        return !remainingSuggestions.isEmpty
    }

    func refreshStarters() {
        Task { [weak self] in
            guard let self, let status = await self.client.assistantSyncStatus(sources: ["calendar", "email"]) else { return }
            self.unreadableSources = AskStarters.unreadable(
                states: Dictionary(status.sources.map { ($0.id, $0.state) }, uniquingKeysWith: { first, _ in first }))
        }
    }

    private let client = WispClient()
    private var sessionId = ""
    private var attachedImage: String?
    private var attachmentReadTask: Task<Void, Never>?
    private var attachmentReadID = UUID()
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
    private var lastDroppedReport: [String: Any]?
    /// Dropped prompts the server may have started on (their stream had begun),
    /// set aside because the user had typed something else meanwhile. Never
    /// sent by itself: Return on an empty box only puts the latest back in the box.
    private var setAside = SetAsidePrompts()
    private var lastDroppedReportKey: String?
    private var turnTraceID: String?
    private var turnStartedAt: Date?
    private var turnFirstTokenAt: Date?
    private var turnModel: String?
    private var turnRouteRole: String?
    private var turnRouteReason: String?
    private var turnNeededTools: Bool?
    private var turnToolCalls: [ToolCallRecord] = []
    private var turnRawIO: [RawModelIO] = []

    var healthy: Bool { phase != .error }
    var willQueuePrompt: Bool { submissionState.isActive }

    // True between submit and the first visible ANSWER token — drives the
    // "working" indicator. Keyed on `answer` alone (not `reasoning`): reasoning
    // starting doesn't mean the model is about to answer — there's often a
    // long silent gap between reasoning finishing and the answer starting to
    // stream. Previously isProcessing flipped false as soon as reasoning had
    // any text, which hid the ONLY status indicator right during that gap —
    // the reasoning disclosure is collapsed by default, so the UI looked like
    // a dead void with nothing visibly happening.
    var isProcessing: Bool {
        if corpusBusy { return true }
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
            // The model's name is a detail for Debug Mode, not a first-time user.
            let base = modelAbbrev.isEmpty || !debugMode ? "Thinking…" : "Thinking with \(modelAbbrev)…"
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
        guard !prompt.isEmpty else {
            // Return on an empty box first brings back a prompt set aside after
            // a drop (to read, not to send), then sends on prompts held behind it.
            if let back = setAside.restore() {
                cancelAttachmentRead()
                input = back.text
                attachedImage = back.image
                attachedImageName = back.imageName
                researchMode = back.researchMode
            } else if !corpusBusy, let next = submissionState.resume() { syncQueue(); start(next) }
            return
        }
        cancelAttachmentRead()
        if debugMode, NotchCommand.isCorpusRequest(prompt) {
            startCorpusCommand(prompt)
            return
        }
        if let command = NotchCommand.match(prompt, includeCorpus: debugMode) {
            input = ""
            run(command)
            return
        }
        // Part of a command that could still be one of several ("/re"): keep the
        // list showing rather than sending a fragment to the model.
        if !NotchCommand.suggestions(for: prompt, includeCorpus: debugMode).isEmpty { return }
        guard !corpusBusy else {
            showTransientNotice("Stop the corpus run before asking another question.")
            return
        }
        let submission = QueuedPrompt(text: prompt, image: attachedImage,
                                      imageName: attachedImageName, researchMode: researchMode)
        input = ""
        removeAttachment()
        if serviceStarting && !submission.researchMode {
            // Nothing is listening yet: hold it and send it the moment Wisp is up.
            heldForStartup.append(submission)
            syncQueue()
            showTransientNotice("Wisp is still starting… I'll send this as soon as it's ready.", duration: 8, info: true)
            onResize()
            return
        }
        let dispatch = submissionState.submit(submission)
        syncQueue()
        if let dispatch { start(dispatch) } else {
            // Queued behind something slow: say so, rather than a silent "1 queued".
            switch submissionState.held ? nil : submissionState.waiting {
            case .summary: showTransientNotice("Waiting for the daily summary to finish. Your message sends right after it.", duration: 6)
            case .research: showTransientNotice("Research is open. Your message sends when you go back to chat.", duration: 6)
            default: break
            }
            onResize()
        }
    }

    private func syncQueue() {
        queuedPromptCount = submissionState.queuedCount + heldForStartup.count
        let count = submissionState.queuedCount
        if !heldForStartup.isEmpty {
            queueLabel = "\(heldForStartup.count) waiting for Wisp to start"
            return
        }
        switch submissionState.waiting {
        case .summary: queueLabel = "\(count) waiting for the summary"
        case .research: queueLabel = "\(count) waiting for research"
        case .resend: queueLabel = "\(count) waiting for your message"
        case .reply: queueLabel = "\(count) queued"
        case nil: queueLabel = ""
        }
    }

    func canStartCorpusCommand() -> Bool {
        guard !submissionState.isActive, !briefComposing, !freeingMemory,
              ![Phase.routing, .working, .streaming, .confirming].contains(phase) else {
            showTransientNotice("Wait for the current request to finish before opening the corpus.")
            return false
        }
        guard attachedImage == nil, attachedImageName == nil else {
            showTransientNotice("Remove the attachment before starting the corpus runner.")
            return false
        }
        return true
    }

    private func startCorpusCommand(_ command: String) {
        guard canStartCorpusCommand() else { return }
        guard let onCorpusCommand else {
            showTransientNotice("The corpus runner is unavailable in this build.")
            return
        }
        input = ""
        leaveKnowYou()
        showingQuickSettings = false
        tab = .ask
        onCorpusCommand(command)
        onResize()
    }

    private func start(_ dispatch: PromptDispatch) {
        guard case .prompt(let submission) = dispatch.work else { return }
        // Includes empty-Return recovery and queued dispatches, not just typed
        // prompts: their answers must not render behind the corpus surface.
        onHideCorpus()
        stopSyncProgress()
        if submission.researchMode {
            researchDispatchID = dispatch.id
            showingResearch = true
            if let onStartResearch { onStartResearch(submission.text) }
            else { completeSubmission(dispatch.id) }
            return
        }
        let prompt = submission.text
        turns.append(Turn(role: "user", text: prompt))
        answer = ""; reasoning = ""; showReasoning = false; activity = []
        role = ""; modelAbbrev = ""; routeSource = ""; statusText = ""
        tokPerSec = 0; pending = nil; messageDraft = nil
        streamStart = nil; streamChars = 0; heartbeats = 0
        pendingDelta = ""; flushScheduled = false
        lastDroppedReport = nil
        lastDroppedReportKey = nil
        turnTraceID = nil
        turnStartedAt = Date(); turnFirstTokenAt = nil
        turnModel = nil; turnRouteRole = nil; turnRouteReason = nil; turnNeededTools = nil
        turnToolCalls = []
        turnRawIO = []
        phase = .routing
        let image = submission.image
        let sid = sessionId   // continue the same conversation (empty -> server starts one)
        let wantsDebug = debugMode
        streamTask?.cancel()
        streamTask = Task { [weak self] in
            await self?.client.runAgent(prompt: prompt, image: image, sessionId: sid,
                                        debug: wantsDebug) { ev in
                Task { @MainActor in self?.handle(ev, dispatchID: dispatch.id) }
            }
        }
    }

    // The stream of the turn in flight, retained so New Chat can stop it instead of
    // leaving a detached request running (and, if it is waiting on a confirmation
    // card, waiting up to five minutes for an answer nobody can give any more).
    private var streamTask: Task<Void, Never>?

    // Start a brand-new conversation: drop the session so the server opens a fresh one.
    // A confirmation card that is still open belongs to the conversation being left:
    // it is DENIED explicitly (never left to time out, never approved by omission).
    func newChat() {
        if let abandoned = pending {
            Task { [client] in
                await client.approve(sessionId: abandoned.sessionId, actionId: abandoned.actionId,
                                     approved: false, scope: "once", requestId: abandoned.requestId)
            }
        }
        sessionId = ""
        reset()
        // The chat just left belongs in the switcher now.
        refreshRecentChats()
    }

    /// Saved history followed by the turns asked here while it loaded. A load that
    /// finished after one of those turns was saved already ends with it, so the
    /// overlap is kept once.
    static func joining(history: [Turn], before local: [Turn]) -> [Turn] {
        var overlap = min(history.count, local.count)
        while overlap > 0 {
            let tail = history.suffix(overlap), head = local.prefix(overlap)
            if zip(tail, head).allSatisfy({ $0.role == $1.role && $0.text == $1.text }) { break }
            overlap -= 1
        }
        return Array(history.dropLast(overlap)) + local
    }

    func run(_ command: NotchCommand) {
        switch command {
        case .corpus: startCorpusCommand("/corpus list")
        case .today: tab = .day
        case .memory: WispRouter.shared.open(.memory(.facts))
        case .research: tab = .research
        case .settings: showingQuickSettings = true
        case .report: reportCurrentProblem()
        case .window: openTabInWindow()
        case .new: newChat()
        case .about: startKnowYou()
        case .resetwisp: openResetCard()
        }
    }

    /// /resetwisp typed in Ask: the confirmation card, never the reset itself.
    func openResetCard() {
        // A reset already under way keeps its card; typing the command again changes nothing.
        guard resetCard != .running else { return }
        if let refusal = WispReset.refusal(corpusBusy: corpusBusy, replying: !conversationSettled) {
            showTransientNotice(refusal)
            return
        }
        showingQuickSettings = false
        tab = .ask
        resetCard = .confirm
        onResize()
    }

    /// The card's Reset Wisp button, and only it.
    func confirmReset() {
        guard resetCard == .confirm, let onResetWisp else { return }
        // Something may have started since the card opened (a reply, a corpus run).
        if let refusal = WispReset.refusal(corpusBusy: corpusBusy, replying: !conversationSettled) {
            resetCard = .failed(refusal)
            onResize()
            return
        }
        resetCard = .running
        onResize()
        Task { [weak self] in
            let stopped = await onResetWisp()
            guard let self else { return }
            self.resetCard = .failed(stopped)
            self.onResize()
        }
    }

    func cancelReset() {
        guard resetCard != .running else { return }
        resetCard = nil
        onResize()
    }

    /// Quick settings' Model memory: unload the models now. Wisp reloads them on
    /// the next ask. Not while a reply is being written, which needs its model.
    func freeUpMemory() {
        guard conversationSettled, !briefComposing, !freeingMemory else { return }
        freeingMemory = true
        Task { [weak self] in
            guard let self else { return }
            await self.client.unloadAll()
            self.modelMemory = ModelMemory.parse(await self.client.modelMemoryReply())
            self.freeingMemory = false
            self.showTransientNotice("Models unloaded. Wisp reloads them on the next ask.", info: true)
        }
    }

    #if WISP_NOTCH_QA
    func qaShow(modelMemory: ModelMemory?) { self.modelMemory = modelMemory }
    #endif

    #if WISP_NOTCH_QA
    func qaShow(brief text: String) { brief = DailyBrief(text: text, receivedAt: Date(), traceID: nil, sessionId: "") }
    #endif

    func refreshModelMemory() async {
        guard !freeingMemory else { return }
        let read = ModelMemory.parse(await client.modelMemoryReply())
        if !freeingMemory { modelMemory = read }
    }

    /// ⤢, ⌘O and /window: open what this tab shows in the Wisp window.
    func openTabInWindow() {
        if tab == .ask && showingCorpus { onOpenCorpus(); return }
        switch tab {
        case .ask:
            if conversationSettled { openInChat() }
            else { showTransientNotice("Wisp is still answering. Open the window when it finishes.") }
        case .brief, .day: WispRouter.shared.open(.today(focus: .plan))
        case .research: WispRouter.shared.open(.research)
        }
    }

    /// Continue from the brief in Ask, in the saved chat it was recorded in.
    func askAboutBrief() {
        guard let brief, !brief.sessionId.isEmpty, conversationSettled else { return }
        newChat()
        sessionId = brief.sessionId
        reopenedChatTitle = "Daily summary"
        turns = [Turn(role: "user", text: "Daily summary"), Turn(role: "assistant", text: brief.text)]
        tab = .ask
        onResize()
    }

    /// Open Ask with a request already typed, for the person to finish and send
    /// (the check-in's Move and Cancel). Nothing is sent here.
    func askAbout(_ text: String) {
        input = text
        tab = .ask
        onResize()
    }

    /// Ask Wisp about something Today says needs you: Ask opens with it quoted.
    func askAbout(_ title: String, reference: String) {
        let quoted = "About “\(title)”" + (reference.isEmpty ? "" : " (\(reference))") + ": "
        input = quoted
        tab = .ask
    }

    // Refreshes overlap (panel open, New chat, a finished turn); only the newest
    // request's answer is used, so a slow older one cannot replace a newer list.
    private var recentChatsGeneration = 0

    func refreshRecentChats() {
        recentChatsGeneration += 1
        let generation = recentChatsGeneration
        Task { [weak self] in
            guard let self else { return }
            let loaded: [[String: Any]]?
            if let loader = self.recentChatsLoader { loaded = await loader(9) }
            else { loaded = await self.client.chatList(limit: 9) }
            guard let rows = loaded,
                  generation == self.recentChatsGeneration else { return }
            let current = self.sessionId
            self.recentChats = Array(rows.compactMap(ChatSummary.parse).filter { $0.id != current }.prefix(8))
        }
    }

    /// Continue a saved chat here. Like New chat, an open confirmation card in the
    /// conversation being left is denied, never left to time out.
    func reopenChat(_ summary: ChatSummary) {
        guard conversationSettled, summary.id != sessionId else { return }
        let target = summary.id
        newChat()
        sessionId = target
        reopenedChatTitle = summary.title
        recentChats.removeAll { $0.id == target }
        Task { [weak self] in
            guard let self else { return }
            let rows = await self.client.chatTurns(id: target)
            // Another chat was opened meanwhile; this history is not for it.
            guard self.sessionId == target else { return }
            guard let rows else {
                self.showTransientNotice("Couldn't load that chat. New messages still continue it.")
                return
            }
            // The saved history keeps which tools an answer used, not their results.
            // It goes before anything already asked here while it loaded, which
            // continued the same session.
            let history = ChatHistory.messages(from: rows).map { message in
                Turn(role: message.role == .user ? "user" : "assistant", text: message.text,
                     activity: message.tools.map { "\($0.name) — used" })
            }
            self.turns = Self.joining(history: history, before: self.turns)
            self.onResize()
        }
    }

    /// A starter question was tapped: ask it, exactly as if typed.
    func pick(_ s: String) {
        // A starter comes from the service, not the person's typing: never the reset.
        guard !NotchCommand.isReset(s) else { return }
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
                                 approved: approved, scope: scope, requestId: p.requestId)
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

    /// A read belongs to this composer generation. Cancellation is advisory for
    /// file I/O; the identity check also rejects a reader that finishes late.
    @discardableResult
    func loadAttachment(name: String, read: @escaping @Sendable () async -> String?) -> Task<Void, Never> {
        cancelAttachmentRead()
        let id = attachmentReadID
        let task = Task { [weak self] in
            let dataURL = await read()
            guard let self, self.attachmentReadID == id, !Task.isCancelled else { return }
            self.attachmentReadTask = nil
            guard let dataURL else { return }
            self.attachedImageName = name
            self.attachedImage = dataURL
        }
        attachmentReadTask = task
        return task
    }

    func loadAttachment(url: URL) {
        let ext = url.pathExtension.lowercased()
        let mime = (ext == "jpg" || ext == "jpeg") ? "jpeg" : ext
        loadAttachment(name: url.lastPathComponent) {
            let reader = Task.detached(priority: .userInitiated) { () -> String? in
                guard !Task.isCancelled, let data = try? Data(contentsOf: url), !Task.isCancelled else { return nil }
                return "data:image/\(mime);base64,\(data.base64EncodedString())"
            }
            return await withTaskCancellationHandler {
                await reader.value
            } onCancel: {
                reader.cancel()
            }
        }
    }

    func cancelAttachmentRead() {
        attachmentReadID = UUID()
        attachmentReadTask?.cancel()
        attachmentReadTask = nil
    }

    func removeAttachment() {
        cancelAttachmentRead()
        attachedImage = nil
        attachedImageName = nil
    }

    func attach(name: String, dataURL: String) {
        cancelAttachmentRead()
        attachedImageName = name
        attachedImage = dataURL
    }

    func reset() {
        removeAttachment()
        onResetCorpus()
        // A report belongs to the conversation being left.
        report.close()
        streamTask?.cancel()
        streamTask = nil
        pending = nil
        dailySummaryID = UUID()
        dailySummaryRunning = false
        briefComposing = false
        submissionState.reset()
        setAside.reset()
        heldForStartup = []
        notReadyRetry?.cancel(); notReadyRetry = nil
        researchDispatchID = nil
        syncQueue()
        stopSyncProgress()
        input = ""; answer = ""; reasoning = ""; activity = []
        routeSource = ""; statusText = ""
        attachedImage = nil; attachedImageName = nil; messageDraft = nil; phase = .idle
        collapsed = false; turns = []
        reopenedChatTitle = nil
    }

    /// Leave the inline Research surface without cancelling its persisted job.
    /// The backend run may continue; starting a new research prompt returns to
    /// this same in-panel workflow.
    func returnToChat() {
        showingResearch = false
        researchMode = false
        releaseResearchDispatch()
    }

    private func releaseResearchDispatch() {
        guard let dispatchID = researchDispatchID else { return }
        researchDispatchID = nil
        completeSubmission(dispatchID)
    }

    // Writes the full current conversation — every turn's text, model, route
    // decision, timing, token rate, tool calls, and any errors — to disk as
    // both a complete JSON file and a human-readable companion transcript.
    // Debug metadata is tracked unconditionally (see applyDebugFields), not
    // just while Debug Mode is on, so this captures the whole session even if
    // Debug Mode was only switched on partway through. Returns the JSON file's
    // URL (nil if there's nothing to export yet) so the caller can reveal it
    // in Finder.
    func reportCurrentProblem() {
        tab = .ask
        showingQuickSettings = false
        if var metadata = lastDroppedReport {
            let traceID = metadata["trace_id"] as? String
            if let journal = DiagnosticReport.journal(traceID: traceID) {
                metadata["trace"] = journal
                metadata["journal_available"] = true
            }
            // Each dropped turn has its own key (set when it dropped), so a later drop never
            // reuses an older snapshot and reopening this one keeps its note.
            report.open(key: lastDroppedReportKey ?? "chat:dropped:" + (traceID ?? UUID().uuidString), metadata: metadata)
        } else if let turn = turns.last(where: { $0.role == "assistant" }) {
            reportProblem(for: turn)
        } else {
            report.open(key: "chat:none",
                        metadata: ["surface": "chat", "client_outcome": "no_completed_reply", "journal_available": false])
        }
    }

    func reportProblem(for turn: Turn) {
        var metadata: [String: Any] = ["surface": "chat", "client_outcome": turn.isError ? "error" : "completed",
                                      "journal_available": false]
        if let traceID = turn.traceID {
            metadata["trace_id"] = traceID
            if let journal = DiagnosticReport.journal(traceID: traceID) {
                metadata["trace"] = journal
                metadata["journal_available"] = true
            }
        }
        if let duration = turn.durationSec { metadata["client_duration_seconds"] = duration }
        if let first = turn.timeToFirstTokenSec { metadata["client_first_token_seconds"] = first }
        var details: [String: Any] = ["answer": turn.text, "activity": turn.activity,
                                      "reasoning": turn.reasoningText,
                                      "tool_calls": turn.toolCalls.map { ["name": $0.name, "args": Self.parsedJSON($0.argsJSON), "result": $0.result, "decision": $0.decision,
                                                                          "debug": $0.debugRecords.map { Self.parsedJSON($0.json) }] },
                                      "model_io": turn.rawIO.map { ["model": $0.model, "request": Self.parsedJSON($0.requestJSON), "response": Self.parsedJSON($0.responseJSON)] }]
        if let index = turns.firstIndex(where: { $0.id == turn.id }), index > 0, turns[index - 1].role == "user" {
            details["prompt"] = turns[index - 1].text
        }
        if let error = turn.errorDetail { details["error_detail"] = error }
        report.open(key: Self.reportKey(for: turn), metadata: metadata, details: details)
    }

    func exportDebugLog() -> URL? {
        guard !turns.isEmpty else { return nil }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        func rounded(_ v: Double) -> Double { (v * 100).rounded() / 100 }

        func turnJSON(_ t: Turn) -> [String: Any] {
            var d: [String: Any] = ["role": t.role, "text": t.text]
            if let traceID = t.traceID { d["trace_id"] = traceID }
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
        guard JSONSerialization.isValidJSONObject(payload), let jsonData = try? JSONSerialization.data(
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

    private func handle(_ ev: WispClient.Event, dispatchID: UInt64) {
        guard submissionState.isCurrent(dispatchID) else { return }
        if let traceID = ev.payload["trace_id"] as? String { turnTraceID = traceID }
        // Any event other than another `status` means whatever it was
        // narrating (a model load/swap — see ensure_only) is over or was
        // superseded by real progress; clear it so processingLabel falls
        // back to the normal phase-based text instead of going stale.
        if ev.type != "status" { statusText = "" }
        var completedDispatch = false
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
                              requestId: ev.str("request_id"),
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
            // no tokens, no tool activity): put the prompt back and remove the
            // dead user bubble so a single Return re-sends it, and say so. Only
            // runAgent resends by itself, and only a prompt the server provably
            // never ran ("unsent"); a tool command ("delete …") may already have
            // run when the stream had begun.
            flushPendingDelta()
            let gotNothing = answer.isEmpty && activity.isEmpty && role.isEmpty
            let notReady = AskNotReady.classify(code: ev.str("code"), message: ev.str("message"))
            if let notReady, answer.isEmpty, activity.isEmpty,
               input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
               let back = submissionState.dropForRetry(dispatchID) {
                // The model isn't ready and nothing ran: not an error. The question
                // goes back in the box, the user bubble goes, and Ask says plainly
                // what happens next. A still-starting engine gets one automatic retry.
                cancelAttachmentRead()
                input = back.text
                attachedImage = back.image
                attachedImageName = back.imageName
                researchMode = back.researchMode
                if turns.last?.role == "user" { turns.removeLast() }
                phase = .idle
                syncQueue()
                let willRetry = notReady == .starting && !notReadyRetried.contains(back.text)
                if notReady == .setup { setupCard = .open }
                if willRetry { scheduleNotReadyRetry(back.text) }
                showTransientNotice(notReady.notice(willRetry: willRetry), duration: 10, info: true)
            } else if ev.bool("dropped") && gotNothing {
                lastDroppedReport = ["surface": "chat", "client_outcome": "connection_dropped", "journal_available": false]
                if let id = turnTraceID { lastDroppedReport?["trace_id"] = id }
                // One key per dropped turn, so reopening its report keeps the note.
                lastDroppedReportKey = "chat:dropped:" + (turnTraceID ?? UUID().uuidString)
                // Text typed meanwhile stays in the box. A prompt the server never
                // ran then waits first in line; one whose stream had begun (a tool
                // may have run) is only set aside, never queued to send again.
                let typed = !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                let unsent = ev.bool("unsent")
                var place = AgentSend.Place.box
                if typed && unsent {
                    _ = submissionState.dropToQueue(dispatchID)
                    place = .queued
                } else if typed {
                    setAside.push(submissionState.dropForRetry(dispatchID))
                    place = .setAside
                } else if let retry = submissionState.dropForRetry(dispatchID) {
                    cancelAttachmentRead()
                    input = retry.text
                    attachedImage = retry.image
                    attachedImageName = retry.imageName
                    researchMode = retry.researchMode
                }
                syncQueue()
                if turns.last?.role == "user" {
                    turns.removeLast()
                }
                phase = .idle
                // Never silent: say what happened and how to send it again.
                showTransientNotice(AgentSend.notice(message: ev.str("message"), unsent: unsent,
                                                     queued: submissionState.queuedCount, place: place,
                                                     setAside: setAside.count),
                                    duration: 12)
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
                completedDispatch = true
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
            completedDispatch = true
        default: break
        }
        onResize()
        if completedDispatch { completeSubmission(dispatchID) }
    }

    /// Questions already retried once after "still starting", so a model that
    /// never comes up can't loop.
    private var notReadyRetried: Set<String> = []
    private var notReadyRetry: Task<Void, Never>?

    /// Send `text` again once, after a short wait, if it is still the only thing
    /// in the box and nothing else is running. Typing or sending anything first
    /// cancels it.
    private func scheduleNotReadyRetry(_ text: String) {
        notReadyRetried.insert(text)
        notReadyRetry?.cancel()
        notReadyRetry = Task { [weak self] in
            try? await Task.sleep(nanoseconds: AskNotReady.retryDelay)
            guard !Task.isCancelled, let self, self.input == text, self.conversationSettled,
                  !self.submissionState.isActive, !self.serviceDown else { return }
            self.submit()
        }
    }

    /// Try again on an error reply that ran nothing: the question that led to it
    /// goes back in the box and is sent again.
    func tryAgain(_ turn: Turn) {
        guard turn.isError, turn.activity.isEmpty, conversationSettled,
              let index = turns.firstIndex(where: { $0.id == turn.id }), index > 0,
              turns[index - 1].role == "user" else { return }
        let question = turns[index - 1].text
        turns.removeSubrange((index - 1)...index)
        input = question
        submit()
    }

    private func completeSubmission(_ dispatchID: UInt64) {
        let next = submissionState.complete(dispatchID)
        syncQueue()
        if let next { start(next) } else { refreshRecentChats() }
    }

    // Folds this turn's tracked debug metadata into the Turn being finalized.
    private func applyDebugFields(to turn: inout Turn) {
        turn.traceID = turnTraceID
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
        // "-1e999" parses to -inf, which JSONSerialization cannot write back and
        // reports as an uncatchable exception; neutralize before it is exported.
        return DiagnosticReport.finiteJSON(obj)
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
    private let assistantDelivery = AssistantDelivery()

    func startAssistant() {
        // Nudge notification buttons (Useful / Not now / Wrong / Undo / Do it)
        // route back here on the main actor.
        Notifications.onNudgeResponse = { [weak self] nudgeID, action, info in
            guard let self, let response = NudgeResponse.from(notificationAction: action, info: info)
            else { return }
            self.respondToNudge(nudgeID, response, fallbackPrompt: info["prompt"] ?? "",
                                fromNotification: true)
        }
        Notifications.registerNudgeCategories()
        assistantTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.client.assistantEvents { [weak self] ev in
                    guard let self else { return false }
                    return await self.assistantDelivery.handle(ev, client: self.client,
                        perform: { await self.handleAssistantEvent($0) },
                        calendar: { await self.handleCalendarAction($0) },
                        reconcileReminder: { await self.reconcileReminderAction($0) })
                }
                // Stream ended (routine server rotation or backend restart) —
                // back off, then re-subscribe. Backend actions wait out this gap.
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    /// `info`: a plain status ("Thank you, Sam.", "Wisp is still starting…"),
    /// drawn quietly; otherwise a problem, drawn in the error color.
    func showTransientNotice(_ message: String, duration: TimeInterval = 4, info: Bool = false) {
        transientNoticeTask?.cancel()
        transientNoticeIsInfo = info
        transientNotice = message
        transientNoticeTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, duration) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.transientNotice = nil
        }
    }

    // Refresh on the Brief tab (and the menu bar's Daily Summary): compose the
    // combined calendar, email and messages brief now and show it on Brief.
    // It waits its turn with prompts, so it never reads the sources while a
    // reply is being written. A prompt sent meanwhile is queued behind it.
    func runDailySummary() {
        guard !corpusBusy else {
            showTransientNotice("Stop the corpus run before refreshing the brief.")
            return
        }
        tab = .brief
        guard !briefComposing else { return }
        guard let dispatch = submissionState.beginDailySummary() else {
            showTransientNotice("Wisp is answering. Refresh the brief when it finishes.")
            return
        }
        requestExpand()
        briefComposing = true
        dailySummaryRunning = true
        let summaryID = UUID()
        dailySummaryID = summaryID
        startSyncProgress(sources: ["calendar", "reminders", "email", "messages"])
        onResize()
        Task { [weak self] in
            guard let self else { return }
            // No session: the service records the brief in a new saved chat, so
            // Ask about this continues from it without joining the open chat.
            let result = await self.client.dailySummary()
            guard self.dailySummaryID == summaryID,
                  self.submissionState.isCurrent(dispatch.id) else { return }
            self.dailySummaryRunning = false
            self.briefComposing = false
            // A failed compose must not leave the 1 Hz sync poll running.
            if result.text == nil { self.stopSyncProgress() }
            // Do not stop source polling here. A "still syncing" response is
            // a bounded wait, not completion of the background sync itself.
            if let text = result.text {
                self.brief = DailyBrief(text: text, receivedAt: Date(), traceID: nil, sessionId: result.sessionId)
            } else {
                self.showTransientNotice("Couldn't build a summary right now.")
            }
            self.onResize()
            self.completeSubmission(dispatch.id)
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
            // Bounded: 10 minutes overall (a local brief can take minutes), or
            // 5 unanswered polls in a row. A downed service must not be polled
            // at 1 Hz for the rest of the session.
            let deadline = Date().addingTimeInterval(600)
            var misses = 0
            while !Task.isCancelled {
                guard let self else { return }
                let status = await self.client.assistantSyncStatus(sources: wanted)
                guard !Task.isCancelled, self.sourceSyncID == syncID else { return }
                if status == nil { misses += 1 } else { misses = 0 }
                if misses >= 5 || Date() > deadline {
                    self.stopSyncProgress()
                    self.onResize()
                    return
                }
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

    // Set by the app delegate: create/remove real macOS Calendar events (the app
    // holds the Calendar grant; the backend can't touch Calendar directly).
    var onCreateCalendarEvent: ((_ title: String, _ startTs: Double,
                                 _ durationMin: Int, _ location: String) -> [String: Any])?
    var onCreateAllDayCalendarEvent: ((_ title: String, _ day: String,
                                       _ timezone: String, _ location: String) -> [String: Any])?
    var onDeleteCalendarEvent: ((_ identifier: String, _ occurrenceTs: Double?) -> [String: Any])?
    /// Calendar's deep link to one synced occurrence, for the check-in card's
    /// "Decline in Calendar" / "Open in Calendar"; nil when it is gone.
    var showCalendarEventURL: ((_ identifier: String, _ startTs: Double) -> URL?)?
    /// Verified cancel of one occurrence (CalendarReader.cancelEvent).
    var onCancelCalendarEvent: ((_ identifier: String, _ expectedTitle: String, _ expectedStart: Double,
                                 _ expectedInvitees: Int) -> [String: Any])?
    /// Verified move of one occurrence (CalendarReader.moveEvent). nil duration keeps its length.
    var onMoveCalendarEvent: ((_ identifier: String, _ expectedTitle: String, _ expectedStart: Double,
                               _ newStart: Double, _ durationMin: Int?, _ expectedInvitees: Int) -> [String: Any])?
    var onVerifiedReminderAction: ((_ kind: String, _ actionID: String, _ sourceID: String,
                                    _ expectedTitle: String, _ expectedDueTs: Double?,
                                    _ title: String, _ dueTs: Double,
                                    _ commitmentKind: String, _ dueDate: String, _ expectedDueDate: String) -> [String: Any])?
    var onReconcileReminderAction: ((_ kind: String, _ actionID: String, _ sourceID: String,
                                     _ title: String, _ dueTs: Double,
                                     _ commitmentKind: String, _ dueDate: String) async -> [String: Any])?
    var onStartResearch: ((_ prompt: String) -> Void)?
    /// Opens this conversation in the Chat window. Called with the service
    /// session id, which is empty until the first reply of a new chat.
    var onOpenChat: ((_ sessionId: String) -> Void)?
    func openInChat() { onOpenChat?(sessionId) }
    // Backend asks (via the assistant event stream) for an immediate Mail
    // re-sync when an email query hits a cold cache — beats waiting on the
    // MailReader's 5-min timer so the FIRST "check my emails" works.
    var onSyncEmails: (() -> Void)?
    // The backend can be restarted independently of the app, so its in-memory
    // readiness resets even when the readers already ran. This callback lets a
    // summary/tool request current source reads immediately instead of waiting
    // for the next 1-5 minute timer.
    var onSyncAssistantSources: (([String]) -> Void)?

    private func handleCalendarAction(_ ev: WispClient.Event) async -> [String: Any] {
        if ["create_reminder", "update_reminder", "complete_reminder", "delete_reminder"].contains(ev.type) {
            guard let action = onVerifiedReminderAction else {
                return ["ok": false, "status": "failed", "error": "Reminders handler unavailable"]
            }
            // JSON null means "no due date" (an undated delete); anything that is not a
            // number becomes NaN, which the writer rejects, never a stand-in value.
            let rawDue = ev.payload["expected_due_ts"]
            let expectedDue: Double? = rawDue is NSNull ? nil : (rawDue as? Double ?? .nan)
            return action(ev.type, ev.str("action_id"), ev.str("source_id"),
                          ev.str("expected_title"), expectedDue,
                          ev.str("title"), ev.payload["due_ts"] as? Double ?? 0,
                          ev.str("commitment_kind"), ev.str("due_date"), ev.str("expected_due_date"))
        }
        if ev.type == "cancel_calendar_event" {
            guard !ev.str("source_id").isEmpty, !ev.str("expected_title").isEmpty,
                  let expected = ev.payload["expected_start_ts"] as? Double, expected.isFinite, expected > 0,
                  let invitees = ev.payload["invitee_count"] as? Int, invitees >= 0,
                  let cancel = onCancelCalendarEvent else {
                return ["ok": false, "error": "Calendar cancel identity or handler is unavailable"]
            }
            return cancel(ev.str("source_id"), ev.str("expected_title"), expected, invitees)
        }
        guard let ts = ev.payload["when_ts"] as? Double, ts.isFinite, ts > 0 else {
            return ["ok": false, "error": "Calendar time is invalid"]
        }
        if ev.type == "move_calendar_event" {
            guard !ev.str("source_id").isEmpty, !ev.str("expected_title").isEmpty,
                  let expected = ev.payload["expected_start_ts"] as? Double, expected.isFinite, expected > 0,
                  let invitees = ev.payload["invitee_count"] as? Int, invitees >= 0,
                  let move = onMoveCalendarEvent else {
                return ["ok": false, "error": "Calendar move identity or handler is unavailable"]
            }
            // JSON null keeps the event's own length; anything else must be a whole minute count.
            let rawDuration = ev.payload["duration_min"]
            var duration: Int?
            if !(rawDuration is NSNull) {
                guard let minutes = rawDuration as? Int, (1...10080).contains(minutes) else {
                    return ["ok": false, "error": "Calendar move length is invalid"]
                }
                duration = minutes
            }
            return move(ev.str("source_id"), ev.str("expected_title"), expected, ts, duration, invitees)
        }
        if ev.type == "create_calendar_event" {
            if ev.payload["all_day"] != nil {
                guard AssistantDelivery.jsonBoolean(ev.payload["all_day"]) == true,
                      let bounds = AssistantDelivery.allDayCalendarBounds(day: ev.str("due_date"), timezone: ev.str("timezone")),
                      abs(bounds.start - ts) < 1,
                      ev.payload["duration_min"] as? Int == bounds.minutes,
                      !ev.str("title").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      let create = onCreateAllDayCalendarEvent else {
                    return ["ok": false, "error": "All-day Calendar creation payload or handler is unavailable"]
                }
                return create(ev.str("title"), ev.str("due_date"), ev.str("timezone"), ev.str("location"))
            }
            guard !ev.str("title").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let duration = ev.payload["duration_min"] as? Int, (1...10080).contains(duration),
                  let create = onCreateCalendarEvent else {
                return ["ok": false, "error": "Calendar creation payload or handler is unavailable"]
            }
            return create(ev.str("title"), ts, duration, ev.str("location"))
        }
        guard !ev.str("source_id").isEmpty, let delete = onDeleteCalendarEvent else {
            return ["ok": false, "error": "Calendar deletion identity or handler is unavailable"]
        }
        return delete(ev.str("source_id"), ts)
    }

    private func reconcileReminderAction(_ ev: WispClient.Event) async -> [String: Any] {
        guard let reconcile = onReconcileReminderAction else {
            return ["ok": false, "status": "unknown", "error": "Readback handler unavailable"]
        }
        return await reconcile(ev.type, ev.str("action_id"), ev.str("source_id"),
                               ev.str("title"), ev.payload["due_ts"] as? Double ?? 0,
                               ev.str("commitment_kind"), ev.str("due_date"))
    }

    private func handleAssistantEvent(_ ev: WispClient.Event) async -> Bool {
        var notificationOK = true
        func post(title: String, body: String) async {
            let identity = ev.str("event_id")
            let ok = await Notifications.deliver(id: identity.isEmpty ? "" : identity + ":" + title,
                                                title: title, body: body)
            notificationOK = notificationOK && ok
        }
        // Durable Calendar actions only execute through the claimed bridge.
        switch ev.type {
        case "hello", "heartbeat": return true
        case "changed": NotificationCenter.default.post(name: .wispAssistantChanged, object: nil)
        case "calendar_action_unknown":
            return await Notifications.deliver(id: ev.str("event_id") + ":unknown",
                title: "Calendar outcome unknown", body: ev.str("error"))
        case "reminder_action_unknown":
            return await Notifications.deliver(id: ev.str("event_id") + ":unknown",
                title: "Reminders outcome unknown", body: ev.str("error"))
        case "reminder":
            guard !ev.str("title").isEmpty, !ev.str("commitment_id").isEmpty,
                  !ev.str("stage").isEmpty, ev.payload["when_ts"] as? Double != nil else { return false }
            let sub = [ev.str("context"), ev.str("when_label")].filter { !$0.isEmpty }.joined(separator: " · ")
            return await Notifications.deliver(id: ev.str("event_id"), title: ev.str("title"), body: sub)
        case "nudge":
            // Behavior engine (service/behavior, Nudge.to_event()). Auto
            // actions were already performed by the backend; the card and the
            // notification say so and offer Undo. Acked like "reminder".
            var payload = ev.payload
            if (payload["evidence_version"] as? Int) != 2,
               ["habit_missed", "habit_conflict", "cancel_likely", "day_preview", "contact_window"].contains(payload["kind"] as? String ?? "") {
                let contact = payload["kind"] as? String == "contact_window"
                payload["title"] = contact ? "Review an older routine suggestion" : "Review an older calendar suggestion"
                payload["body"] = contact ? "Source details for this older contact suggestion are unavailable; actual communication is unknown." : "This suggestion used calendar plans. Whether the activity happened is unknown."
                payload["because"] = contact ? "Source details unavailable" : "Calendar evidence; completion unknown"
                payload["actions"] = [[String: Any]]()
            }
            guard let nudge = BehaviorNudge.parse(payload) else { return false }
            // A replay that outlived its moment is acknowledged silently.
            if nudge.isExpired() { return true }
            showNudge(nudge)
            return await Notifications.deliver(id: nudge.notificationID, title: nudge.title,
                                               body: nudge.notificationBody,
                                               subtitle: nudge.notificationSubtitle,
                                               category: nudge.notificationCategory,
                                               userInfo: nudge.notificationInfo)
        case "attention_added":
            // The attention runner added a reminder for a plan stated in a text that was on
            // neither Calendar nor Reminders. Say what was added and quote the source; Undo
            // lives in Today's "Caught for you" section.
            let title = ev.str("title")
            guard !title.isEmpty, !ev.str("source_id").isEmpty else { return false }
            let quote = ev.str("quote")
            let body = [ev.str("when_label"), quote.isEmpty ? "" : "“\(quote)”"]
                .filter { !$0.isEmpty }.joined(separator: "\n")
            return await Notifications.deliver(id: "attention:" + ev.str("source_id"),
                                               title: "Added a reminder: \(title)", body: body)
        case "create_apple_reminder":
            return false
        case "update_apple_reminder":
            return false
        case "delete_apple_reminder":
            return false
        case "sync_emails_now":
            if !dailySummaryRunning { startSyncProgress(sources: ["email"]) }
            guard let sync = onSyncEmails else { return false }
            sync()
        case "sync_assistant_sources_now":
            let sources = ev.payload["sources"] as? [String] ?? []
            if !dailySummaryRunning { startSyncProgress(sources: sources) }
            guard !sources.isEmpty, let sync = onSyncAssistantSources else { return false }
            sync(sources)
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
                await post(title: "✅ \(kind) sent", body: "Your scheduled \(kind.lowercased()) to \(who) went out.")
            } else {
                await post(title: "⚠️ Scheduled \(kind.lowercased()) failed",
                                   body: "Couldn't send to \(who): \(ev.str("error"))")
            }
        case "scheduled_send_missed":
            // Came due while Wisp wasn't running and is now too late to send
            // (see outbound_queue). Never delivered late and never dropped
            // silently — the user decides whether it still makes sense.
            let who = ev.str("display")
            let kind = ev.str("channel") == "email" ? "email" : "text"
            await post(title: "⏰ Scheduled \(kind) missed",
                               body: "Wisp wasn't running when your \(kind) to \(who) was due, so it wasn't sent.")
        case "scheduled_send_unknown":
            guard !ev.str("notice_id").isEmpty, !ev.str("display").isEmpty else { return false }
            let kind = ev.str("channel") == "email" ? "email" : "text"
            guard await Notifications.deliver(id: ev.str("event_id"),
                title: "❓ Scheduled \(kind) outcome unknown",
                body: "Wisp was interrupted while sending your \(kind) to \(ev.str("display")). It wasn't sent again — check whether it arrived.") else { return false }
            if ev.str("event_id").isEmpty { return await client.ackScheduledSendNotice(ev.str("notice_id")) }
        case "prepare_email_reply":
            OutboundSender.prepareEmailReply(
                actionId: ev.str("action_id"), messageId: ev.str("message_id"),
                body: ev.str("body"), replyAll: ev.payload["reply_all"] as? Bool ?? false,
                account: ev.str("account"), accountID: ev.str("account_id"))
        case "reply_to_email":
            OutboundSender.replyToEmail(
                actionId: ev.str("action_id"),
                messageId: ev.str("message_id"), body: ev.str("body"),
                replyAll: ev.payload["reply_all"] as? Bool ?? false,
                account: ev.str("account"),
                expected: ev.payload["expected_reply"] as? [String: Any] ?? [:])
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
            await post(title: "📧 Morning email summary", body: ev.str("summary"))
        case "node_result":
            guard let presentation = NodePresentation(ev.payload) else { return false }
            if !turns.contains(where: { $0.nodeResultID == presentation.eventID }) {
                turns.append(Turn(role: "assistant", text: presentation.literalText, nodeResultID: presentation.eventID))
            }
            // A failed notification must retry even when its transcript exists.
            await post(title: presentation.title, body: Self.notificationBody(from: presentation.body))
        case "codex_task_update":
            let kind = ev.str("kind")
            let task = ev.str("title")
            let latest = ev.str("latest_update")
            let title: String
            switch kind {
            case "completed": title = "✅ Codex task finished"
            case "failed": title = "⚠️ Codex task needs attention"
            default: title = "⏳ Codex task may be stalled"
            }
            await post(title: title,
                               body: latest.isEmpty ? task : "\(task)\n\(latest)")
        case "checkin":
            // The evening check-in is due (service/assistant/scheduler._checkin_tick):
            // one notification; the card itself loads on the Day tab.
            let text = ev.str("text")
            guard !text.isEmpty else { return false }
            checkInDue = true
            await post(title: "🌙 Evening check-in", body: text)
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
            // that (see brief.run_scheduled_brief). AssistantDelivery receipts
            // suppress replays across reconnects and app restarts.
            let briefText = ev.str("text")
            guard !briefText.isEmpty else { return false }
            let today = ev.str("today_summary")
            let messages = ev.str("messages_summary")
            // A day-one rebuild of a summary already announced (sources that
            // finished reading, or Getting to know you answered) updates the
            // Brief tab quietly: no second "ready" banner.
            let recomposed = ev.bool("recomposed")
            if !recomposed, !today.isEmpty { await post(title: "📅 Today", body: today) }
            if !recomposed, !messages.isEmpty { await post(title: "💬 Messages", body: messages) }
            if !recomposed && today.isEmpty && messages.isEmpty {
                // No split available: notify with the brief's own opening lines
                // rather than announcing that something is ready elsewhere.
                let part = ev.str("part_of_day") == "evening" ? "Evening" : "Morning"
                await post(title: "🗞️ \(part) brief",
                                   body: Self.notificationBody(from: briefText))
            }
            // The full write-up replaces the brief on the Brief tab, which shows
            // when it arrived, so an older one never reads as current. A replay
            // of a brief already applied (the stream re-subscribes after any
            // drop) changes nothing: not the text, not its time, not the chat a
            // refreshed brief was recorded in.
            let eventID = ev.str("event_id")
            let created = (ev.payload["created_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
            if Self.appliesBrief(eventID: eventID, created: created, applied: appliedBriefEvents, current: brief) {
                if !eventID.isEmpty { appliedBriefEvents.insert(eventID) }
                brief = DailyBrief(text: briefText, receivedAt: created ?? Date(),
                                   traceID: ev.payload["trace_id"] as? String, sessionId: "")
            }
        case "google_signin_expired":
            // A Testing-mode Google sign-in ended after 7 days
            // (gmail_source._announce_expired): one durable event per expiry,
            // so one notification. Settings ▸ Google shows Reconnect.
            let account = ev.str("account")
            guard !account.isEmpty else { return false }
            return await Notifications.deliver(
                id: "google-signin-expired:\(account):\(ev.payload["expired_at"] as? Int ?? 0)",
                title: "Google sign-in expired", body: "Reconnect in Settings ▸ Google.",
                subtitle: account)
        default:
            return false
        }
        return notificationOK
    }

    // --- behavior nudges ---------------------------------------------------

    private func showNudge(_ nudge: BehaviorNudge) {
        if let i = nudges.firstIndex(where: { $0.id == nudge.id }) {
            let old = nudges[i]
            if !nudge.eventID.isEmpty, !old.eventID.isEmpty, nudge.eventID != old.eventID {
                // Same id from a NEW hub event: it re-fired (e.g. after a state
                // reset), so the card starts fresh; Notifications.deliver
                // replaces the old banner the same way.
                nudges[i] = nudge
            } else {
                // Same deterministic id re-delivered: keep the card's local state.
                var merged = nudge
                merged.status = old.status
                merged.undone = old.undone
                merged.performed = old.performed
                nudges[i] = merged
            }
        } else {
            nudges.insert(nudge, at: 0)
            if nudges.count > 3 { nudges.removeLast(nudges.count - 3) }
        }
        onResize()
    }

    func dismissNudge(_ nudgeID: String) {
        nudges.removeAll { $0.id == nudgeID }
        onResize()
    }

    private func setNudge(_ nudgeID: String, status: String? = nil, busy: Bool? = nil,
                          undone: Bool? = nil, performed: Bool? = nil) {
        guard let i = nudges.firstIndex(where: { $0.id == nudgeID }) else { return }
        if let status { nudges[i].status = status }
        if let busy { nudges[i].busy = busy }
        if let undone { nudges[i].undone = undone }
        if let performed { nudges[i].performed = performed }
        onResize()
    }

    /// One handler for card buttons and notification actions. Feedback and
    /// undo go to the backend; an open_prompt action asks the backend for the
    /// prompt and prefills the composer WITHOUT sending it; an offered
    /// add_reminder/add_hold is created by the backend (and can be undone).
    func respondToNudge(_ nudgeID: String, _ response: NudgeResponse,
                        fallbackPrompt: String = "", fromNotification: Bool = false) {
        let onCard = nudges.contains { $0.id == nudgeID }
        if !fromNotification { Notifications.removeDelivered(id: "wisp-nudge:" + nudgeID) }
        switch response {
        case .open:
            requestExpand()
        case .feedback(let verdict):
            guard NudgeResponse.verdicts.contains(verdict) else { return }
            setNudge(nudgeID, status: "Saving…", busy: true)
            Task { [weak self] in
                guard let self else { return }
                let ok = await self.client.behaviorFeedback(nudgeID: nudgeID, verdict: verdict)
                if ok {
                    self.dismissNudge(nudgeID)
                } else {
                    self.setNudge(nudgeID, status: "Couldn't reach Wisp. Try again.", busy: false)
                    if fromNotification && !onCard {
                        Notifications.post(title: "Wisp couldn't save that", body: "Feedback wasn't recorded.")
                    }
                }
            }
        case .undo:
            setNudge(nudgeID, status: "Undoing…", busy: true)
            Task { [weak self] in
                guard let self else { return }
                let ok = await self.client.behaviorUndo(nudgeID: nudgeID)
                self.setNudge(nudgeID, status: ok ? "Undone. Wisp removed what it added." :
                                                    "Couldn't undo. Check Wisp's schedule.",
                              busy: false, undone: ok ? true : nil)
                if fromNotification && !onCard {
                    Notifications.post(title: ok ? "Undone" : "Couldn't undo",
                                       body: ok ? "Wisp removed what it added." :
                                                  "Wisp couldn't reach its backend. Open Wisp to try again.")
                }
            }
        case .act(let index):
            setNudge(nudgeID, status: "Working…", busy: true)
            let action = nudges.first(where: { $0.id == nudgeID })?.actions.first(where: { $0.index == index })
            // Notification "Do it" only exists for open_prompt actions; its
            // userInfo carries the prompt as a fallback.
            let isPrompt = action?.isPrompt ?? !fallbackPrompt.isEmpty
            let localPrompt = action?.prompt ?? fallbackPrompt
            Task { [weak self] in
                guard let self else { return }
                let reply = await self.client.behaviorAct(nudgeID: nudgeID, actionIndex: index)
                let served = (reply?["prompt"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if isPrompt || !served.isEmpty {
                    let prompt = served.isEmpty ? localPrompt : served
                    guard !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                        self.setNudge(nudgeID, status: "Couldn't prepare that request.", busy: false)
                        return
                    }
                    // Prefill only: the user reviews and presses Return themselves.
                    let draft = self.input.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.input = draft.isEmpty ? prompt : self.input + "\n\n" + prompt
                    self.setNudge(nudgeID, status: "Drafted in the composer. Review it, then press Return.",
                                  busy: false)
                    self.requestExpand()
                    return
                }
                if let reply, reply["ok"] as? Bool == true {
                    let created = reply["created"] as? [String: Any]
                    let title = created?["title"] as? String ?? action?.title ?? ""
                    self.setNudge(nudgeID, status: title.isEmpty ? "Added to Wisp's schedule." :
                                    "Added “\(title)” to Wisp's schedule.", busy: false, performed: true)
                } else {
                    let detail = reply?["detail"] as? String ?? "Couldn't reach Wisp. Try again."
                    self.setNudge(nudgeID, status: detail, busy: false)
                }
            }
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
