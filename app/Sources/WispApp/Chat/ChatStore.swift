import SwiftUI

/// Everything the Chat window needs from the Wisp service. The live
/// implementation talks to the local backend; the QA harness supplies a
/// fixture so the window can be rendered without one.
@MainActor
protocol ChatBackend: AnyObject {
    func listChats() async -> [ChatSummary]?
    func loadChat(id: String) async -> [ChatMessage]?
    func deleteChat(id: String) async -> Bool
    func liveCorpusCases(selector: String) async throws -> [AssistantTaskCorpusCase]
    /// `normal` runs the case through the ordinary Ask path instead of the bounded scope.
    func runLiveCorpusCase(id: String, token: String, normal: Bool,
                           onEvent: @escaping @Sendable (ChatEvent) -> Void) async
    /// The newest saved corpus run as Markdown and CSV; nil when none is saved.
    func savedCorpusExport() async throws -> (markdown: String, csv: String)?
    /// One /corpus prompts command, answered with the job's progress.
    func corpusPrompts(_ command: CorpusPromptsCommand) async throws -> CorpusPromptsStatus
    func corpusPromptsResults(limit: Int) async throws -> [CorpusPromptsResult]
    func corpusPromptsExport() async throws -> (markdown: String, csv: String)?
    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             testMode: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async
    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async
    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String)
    /// True when Wisp may act without asking; nil when it can't be confirmed.
    func fullAccess() async -> Bool?
    /// The flag the service confirmed after the change; nil when it can't be confirmed.
    func setFullAccess(_ on: Bool) async -> Bool?
    /// True when this launch may send turns to a cloud provider; nil when unknown.
    func cloudInUse() async -> Bool?
}

extension ChatBackend {
    func liveCorpusCases(selector: String) async throws -> [AssistantTaskCorpusCase] {
        throw AssistantTaskCorpusCommand.CommandError.resourceUnavailable
    }
    func runLiveCorpusCase(id: String, token: String, normal: Bool,
                           onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        onEvent(ChatEvent(type: "error", payload: ["message": "This backend does not support live corpus evaluation."]))
    }
    func savedCorpusExport() async throws -> (markdown: String, csv: String)? { nil }
    func corpusPrompts(_ command: CorpusPromptsCommand) async throws -> CorpusPromptsStatus {
        throw AssistantTaskCorpusCommand.CommandError.resourceUnavailable
    }
    func corpusPromptsResults(limit: Int) async throws -> [CorpusPromptsResult] { [] }
    func corpusPromptsExport() async throws -> (markdown: String, csv: String)? { nil }
    func cloudInUse() async -> Bool? { nil }
}

/// Where an export landed.
struct CorpusExportFiles: Equatable {
    let markdown: URL
    let csv: URL
    /// False when the run came from the screen rather than the saved runs.
    let saved: Bool
}

/// One conversation in the sidebar and the window.
@MainActor
final class ChatConversation: ObservableObject, Identifiable {
    let id = UUID()
    /// The backend session. Empty until the first reply opens one.
    @Published var sessionId: String
    @Published var title: String
    @Published var preview: String
    @Published var lastUsed: Date
    @Published var messages: [ChatMessage] = []
    @Published var loaded: Bool
    @Published var loading = false
    @Published var busy = false
    @Published fileprivate(set) var corpusRunning = false
    @Published fileprivate(set) var corpusTotal = 0
    /// This run's cases go through the ordinary Ask path (/corpus … normal).
    fileprivate(set) var corpusNormal = false
    /// Text to put back in the composer after a dropped request.
    @Published var restoredInput: String?

    // Per-run streaming state, outside the published surface.
    fileprivate var task: Task<Void, Never>?
    fileprivate var corpusTask: Task<Void, Never>?
    fileprivate var pendingDelta = ""
    fileprivate var flushScheduled = false
    fileprivate var streamStart: Date?
    fileprivate var streamChars = 0
    /// Identifies the reply being streamed, so late events from a stopped or
    /// replaced run cannot touch the next one.
    fileprivate var runID = UUID()

    init(sessionId: String = "", title: String = "New chat", preview: String = "",
         lastUsed: Date = Date(), loaded: Bool = true) {
        self.sessionId = sessionId; self.title = title; self.preview = preview
        self.lastUsed = lastUsed; self.loaded = loaded
    }

    var corpusFinishedCount: Int {
        messages.filter { $0.corpusEvaluation != nil && ($0.corpusEvaluation?.completed == true || $0.isError) }.count
    }
    var corpusVerifiedCount: Int {
        messages.filter { $0.corpusEvaluation?.completed == true && ($0.corpusEvaluation?.modelCalls ?? 0) > 0 }.count
    }
    var isEmpty: Bool { messages.isEmpty }
    var latestAssistant: ChatMessage? { messages.last(where: { $0.role == .assistant }) }
}

@MainActor
final class ChatStore: ObservableObject {
    @Published private(set) var conversations: [ChatConversation] = []
    @Published var selected: ChatConversation?
    @Published var inspectedMessage: UUID?
    @Published var showInspector = false
    @Published var search = ""
    @Published private(set) var reachable = true
    @Published private(set) var fullAccess = false
    // False while Full access can't be confirmed: the composer then says so
    // rather than showing the last value, or "off", as if it were current.
    @Published private(set) var fullAccessKnown = false
    /// Whether answers may go to a cloud provider: nil until the service says,
    /// and the window claims "all local" only on a confirmed false.
    @Published private(set) var cloudInUse: Bool?
    @Published private(set) var loadingList = false

    let backend: ChatBackend
    private let debug: () -> Bool
    /// Chats deleted this session, so a list fetched before the delete finished cannot bring one back.
    private var deletedSessionIds = Set<String>()

    init(backend: ChatBackend, debug: @escaping () -> Bool = { false }) {
        self.backend = backend
        self.debug = debug
    }

    /// Debug Mode, read when a view is built (it gates debug-only tools such as image attach).
    var debugMode: Bool { debug() }

    // MARK: - List

    /// Grouped for the sidebar, newest first, narrowed by the search field.
    var sections: [(title: String, items: [ChatConversation])] {
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        let visible = conversations
            .filter { !$0.isEmpty || !$0.sessionId.isEmpty || $0 === selected }
            .filter { query.isEmpty || $0.title.lowercased().contains(query) || $0.preview.lowercased().contains(query) }
            .sorted { $0.lastUsed > $1.lastUsed }
        return ChatGrouping.order.compactMap { name in
            let items = visible.filter { ChatGrouping.section(for: $0.lastUsed) == name }
            return items.isEmpty ? nil : (name, items)
        }
    }

    /// Load the list, and keep trying while the service is not answering. The Chat
    /// window opens as Wisp launches, before its backend is up, so the first load
    /// usually fails; this retries until the service is reachable or the attempts
    /// run out (about a minute by default). A later `refresh()` still works.
    func refreshWhenReady(maxAttempts: Int = 40, retryDelay: Duration = .milliseconds(1500)) async {
        // One retry loop at a time: the window and the view both ask for one.
        guard !retrying else { return }
        retrying = true
        defer { retrying = false }
        for _ in 0..<maxAttempts {
            // A cancelled request returns nothing, which must not read as "service down".
            if Task.isCancelled { return }
            await refresh()
            if reachable { return }
            do { try await Task.sleep(for: retryDelay) } catch { return }
        }
    }
    private var retrying = false

    func refresh() async {
        loadingList = true
        defer { loadingList = false }
        async let access = backend.fullAccess()
        async let cloud: Void = refreshCloudUse()
        guard let rows = await backend.listChats() else {
            reachable = false
            await cloud
            return
        }
        reachable = true
        applyFullAccess(await access)
        await cloud
        var byId = Dictionary(conversations.compactMap { c in c.sessionId.isEmpty ? nil : (c.sessionId, c) },
                              uniquingKeysWith: { first, _ in first })
        for row in rows where !deletedSessionIds.contains(row.id) {
            if let existing = byId[row.id] {
                // A reply streaming right now owns its own title and preview.
                if !existing.busy {
                    existing.title = row.title; existing.preview = row.preview; existing.lastUsed = row.lastUsed
                }
            } else {
                let fresh = ChatConversation(sessionId: row.id, title: row.title, preview: row.preview,
                                             lastUsed: row.lastUsed, loaded: false)
                conversations.append(fresh)
                byId[row.id] = fresh
            }
        }
        // Chats the service no longer lists (deleted elsewhere) go away unless
        // they hold something the user is looking at or typing into.
        let listed = Set(rows.map(\.id))
        conversations.removeAll { c in
            !c.sessionId.isEmpty && !listed.contains(c.sessionId) && !c.busy && c.loaded && c !== selected && c.messages.isEmpty
        }
    }

    // MARK: - Selection

    func select(_ conversation: ChatConversation) {
        selected = conversation
        inspectedMessage = nil
        Task { await loadHistoryIfNeeded(conversation) }
    }

    /// Open a saved chat by its backend session id (the notch's hand-off).
    func open(sessionId: String) async {
        if sessionId.isEmpty { newChat(); return }
        deletedSessionIds.remove(sessionId)   // the notch is still using it, so it is not gone
        if conversations.first(where: { $0.sessionId == sessionId }) == nil { await refresh() }
        if let found = conversations.first(where: { $0.sessionId == sessionId }) {
            select(found)
        } else {
            let placeholder = ChatConversation(sessionId: sessionId, title: "Chat", loaded: false)
            conversations.append(placeholder)
            select(placeholder)
        }
    }

    func loadHistoryIfNeeded(_ conversation: ChatConversation) async {
        guard !conversation.loaded, !conversation.loading, !conversation.sessionId.isEmpty else { return }
        conversation.loading = true
        let messages = await backend.loadChat(id: conversation.sessionId)
        conversation.loading = false
        guard let messages else { return }
        // Never replace a conversation the user has already started typing into.
        if !conversation.busy && conversation.messages.isEmpty { conversation.messages = messages }
        conversation.loaded = true
        if conversation.title == "Chat", let first = messages.first(where: { $0.role == .user }) {
            conversation.title = ChatTitle.make(from: first.text)
        }
    }

    @discardableResult
    func newChat() -> ChatConversation {
        if let current = selected, current.isEmpty, current.sessionId.isEmpty { return current }
        let fresh = ChatConversation()
        conversations.insert(fresh, at: 0)
        selected = fresh
        inspectedMessage = nil
        return fresh
    }

    func delete(_ conversation: ChatConversation) {
        let wasBusy = conversation.busy
        stop(conversation)   // also denies a card that is still waiting for an answer
        let id = conversation.sessionId
        conversations.removeAll { $0 === conversation }
        if selected === conversation {
            if let next = conversations.sorted(by: { $0.lastUsed > $1.lastUsed }).first { select(next) } else { selected = nil }
        }
        guard !id.isEmpty else { return }
        deletedSessionIds.insert(id)
        Task {
            // A reply that was streaming is still being wound down by the service,
            // which saves its last turns on the way out. Wait for that to finish, so
            // the delete does not race it and leave the prompt behind as orphaned rows.
            if wasBusy { try? await Task.sleep(nanoseconds: 3_500_000_000) }
            if await backend.deleteChat(id: id) == false {
                // The chat still exists on the service, so it goes back in the list.
                deletedSessionIds.remove(id)
                await refresh()
            }
        }
    }

    // MARK: - Sending

    /// Interprets the Ask composer command and otherwise sends a normal prompt.
    func submit(_ text: String, attachmentName: String? = nil, image: String? = nil) {
        // Cloud settings can change in another window; re-read them as the turn starts.
        Task { await refreshCloudUse() }
        do {
            guard let selection = try AssistantTaskCorpusCommand.resolve(text) else {
                send(text, attachmentName: attachmentName, image: image)
                return
            }
            guard attachmentName == nil, image == nil else {
                appendLocalNotice("Remove the image attachment before starting the corpus runner.")
                return
            }
            Task {
                do { _ = try await performCorpus(selection, command: text) }
                catch is CancellationError { }
                catch { appendLocalNotice("Could not load corpus: \(error.localizedDescription)") }
            }
        } catch {
            appendLocalNotice(error.localizedDescription)
        }
    }

    private var loadingCorpus = false

    /// Shared by the window and notch. Caller cancellation must survive a
    /// transport that returns late, before any case can start.
    @discardableResult
    func submitCorpusCommand(_ text: String) async throws -> ChatConversation {
        guard let selection = try AssistantTaskCorpusCommand.resolve(text) else {
            throw AssistantTaskCorpusCommand.CommandError.usage
        }
        return try await performCorpus(selection, command: text)
    }

    /// Where exports are written; tests point it at a scratch folder.
    var exportDirectory: () -> URL = {
        FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
    }

    /// The newest conversation holding corpus cases, as it stands on screen.
    var latestCorpusConversation: ChatConversation? {
        conversations.filter { $0.messages.contains { $0.corpusEvaluation != nil } }.max { $0.lastUsed < $1.lastUsed }
    }

    /// The on-screen run as Markdown, for Copy results; nil without one.
    func corpusMarkdown(_ conversation: ChatConversation?) -> String? {
        guard let conversation else { return nil }
        let cases = CorpusExport.cases(from: conversation.messages)
        return cases.isEmpty ? nil : CorpusExport.markdown(cases, title: conversation.title)
    }

    /// Writes the latest run as Markdown and CSV: the newest saved run, or the
    /// run on screen when nothing is saved (a run from before runs were saved).
    func exportCorpus(now: Date = Date()) async throws -> CorpusExportFiles {
        var contents: (markdown: String, csv: String, saved: Bool)?
        if let saved = try? await backend.savedCorpusExport() {
            contents = (saved.markdown, saved.csv, true)
        } else if let conversation = latestCorpusConversation {
            let cases = CorpusExport.cases(from: conversation.messages)
            if !cases.isEmpty {
                contents = (CorpusExport.markdown(cases, title: conversation.title), CorpusExport.csv(cases), false)
            }
        }
        guard let contents else { throw AssistantTaskCorpusCommand.CommandError.nothingToExport }
        return try writeExport((contents.markdown, contents.csv), stem: CorpusExport.fileStem(now), saved: contents.saved)
    }

    private func performCorpus(_ selection: AssistantTaskCorpusCommand.Selection,
                               command: String) async throws -> ChatConversation {
        // Exporting starts nothing, so it doesn't wait for a run to finish.
        if case .export = selection { return try await exportNotice() }
        // A prompts command only talks to the service's job, so no window run blocks it.
        if case .prompts(let command) = selection { return try await promptsNotice(command) }
        guard !loadingCorpus, !conversations.contains(where: { $0.corpusRunning }) else {
            throw AssistantTaskCorpusCommand.CommandError.busy
        }
        loadingCorpus = true
        defer { loadingCorpus = false }
        try Task.checkCancellation()
        switch selection {
        case .help:
            _ = newChat()
            return appendLocalNotice(AssistantTaskCorpusCommand.helpText)
        case .live(let selector):
            let cases = try await backend.liveCorpusCases(selector: selector)
            try Task.checkCancellation()
            return runCorpus(label: selector, cases: cases, command: command)
        case .liveNormal(let selector):
            let cases = try await backend.liveCorpusCases(selector: selector)
            try Task.checkCancellation()
            return runCorpus(label: "\(selector) normal", cases: cases, command: command, normal: true)
        case .catalog, .profile:
            let cases = try await backend.liveCorpusCases(selector: "all")
            try Task.checkCancellation()
            _ = newChat()
            if case .catalog = selection {
                let rows = cases.map { "\($0.id) [\($0.category)] \($0.requestPrompt)\n\($0.setup)" }
                return appendLocalNotices(["Live corpus: \(cases.count) configured requests"] + rows + ["Run /corpus batch-1 through batch-10, or select one case by ID."])
            }
            let contacts = cases.filter { $0.category == "contacts" }.compactMap(\.target).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
            let mail = cases.filter { $0.category == "mail" }.compactMap(\.target).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
            return appendLocalNotice("Local corpus targets\nContacts / Messages: \(contacts.joined(separator: ", "))\nMail identifiers: \(mail.joined(separator: ", "))\nMail identifiers are search terms; an incomplete address is not assumed to be deliverable.")
        case .cases(let label, let cases):
            return runCorpus(label: label, cases: cases, command: command)
        case .export:
            return try await exportNotice()
        case .prompts(let command):
            return try await promptsNotice(command)
        }
    }

    /// The window shows the prompts job's progress as text; the notch shows it live.
    private func promptsNotice(_ command: CorpusPromptsCommand) async throws -> ChatConversation {
        if command == .export {
            let files = try await exportPrompts()
            return appendLocalNotice("Exported prompts results:\n\(files.markdown.path)\n\(files.csv.path)")
        }
        return appendLocalNotice(try await backend.corpusPrompts(command).summary)
    }

    /// The prompts job, for the notch's live panel.
    func corpusPrompts(_ command: CorpusPromptsCommand) async throws -> CorpusPromptsStatus {
        try await backend.corpusPrompts(command)
    }

    func corpusPromptsResults(limit: Int = 50) async throws -> [CorpusPromptsResult] {
        try await backend.corpusPromptsResults(limit: limit)
    }

    /// Writes the current prompts run to Downloads as Markdown and CSV.
    func exportPrompts(now: Date = Date()) async throws -> CorpusExportFiles {
        guard let contents = try await backend.corpusPromptsExport() else {
            throw AssistantTaskCorpusCommand.CommandError.nothingToExport
        }
        return try writeExport(contents, stem: CorpusExport.fileStem(now).replacingOccurrences(of: "wisp-corpus-", with: "wisp-prompts-"), saved: true)
    }

    private func writeExport(_ contents: (markdown: String, csv: String), stem: String, saved: Bool) throws -> CorpusExportFiles {
        let directory = exportDirectory()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let markdown = directory.appendingPathComponent(stem + ".md")
        let csv = directory.appendingPathComponent(stem + ".csv")
        try Data(contents.markdown.utf8).write(to: markdown, options: .withoutOverwriting)
        try Data(contents.csv.utf8).write(to: csv, options: .withoutOverwriting)
        return CorpusExportFiles(markdown: markdown, csv: csv, saved: saved)
    }

    private func exportNotice() async throws -> ChatConversation {
        let files = try await exportCorpus()
        return appendLocalNotice("Exported corpus results\(files.saved ? "" : " from the run on screen"):\n"
                                 + "\(files.markdown.path)\n\(files.csv.path)")
    }

    func send(_ text: String, attachmentName: String? = nil, image: String? = nil) {
        let conversation = selected ?? newChat()
        send(text, attachmentName: attachmentName, image: image, in: conversation,
             testMode: false, corpusCase: nil)
    }

    private func send(_ text: String, attachmentName: String? = nil, image: String? = nil,
                      in conversation: ChatConversation, testMode: Bool,
                      corpusCase: AssistantTaskCorpusCase?) {
        let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }
        guard !conversation.busy, !conversation.loading else { return }
        if conversation.messages.isEmpty && corpusCase == nil { conversation.title = ChatTitle.make(from: prompt) }
        var user = ChatMessage(role: .user, text: prompt)
        user.attachmentName = attachmentName
        var reply = ChatMessage(role: .assistant, phase: .waiting)
        reply.meta = ChatMeta(startedAt: Date())
        if let corpusCase {
            reply.corpusEvaluation = ChatCorpusEvaluation(
                caseID: corpusCase.id, split: corpusCase.split, category: corpusCase.category,
                expectedTools: corpusCase.expected.tools, behavior: corpusCase.expected.behavior,
                usesLiveData: corpusCase.usesLiveData, normalMode: conversation.corpusNormal)
        }
        conversation.messages.append(contentsOf: [user, reply])
        conversation.lastUsed = Date()
        conversation.busy = true
        conversation.pendingDelta = ""; conversation.flushScheduled = false
        conversation.streamStart = nil; conversation.streamChars = 0
        let replyID = reply.id
        let runID = UUID()
        conversation.runID = runID
        let sid = conversation.sessionId
        let corpusNormal = conversation.corpusNormal
        // The Chat window never shows debug records, and the service streams them
        // in full (about 1.6 MB a turn), so ask for none.
        let wantsDebug = false
        conversation.task = Task { [weak self, weak conversation] in
            guard let self else { return }
            let receive: @Sendable (ChatEvent) -> Void = { event in
                Task { @MainActor in
                    guard let conversation else { return }
                    self.handle(event, in: conversation, replyID: replyID, runID: runID)
                }
            }
            if let corpusCase, corpusCase.usesLiveData {
                await self.backend.runLiveCorpusCase(id: corpusCase.id, token: corpusCase.token ?? "",
                                                     normal: corpusNormal, onEvent: receive)
            } else {
                await self.backend.run(prompt: prompt, image: image, sessionId: sid, debug: wantsDebug,
                                       testMode: testMode, onEvent: receive)
            }
        }
    }

    private func runCorpus(label: String, cases: [AssistantTaskCorpusCase], command: String,
                           normal: Bool = false) -> ChatConversation {
        guard !cases.isEmpty else {
            _ = newChat()
            return appendLocalNotice("No corpus cases were found for `\(label)`. Check the bundled corpus and try again.")
        }
        let conversation = newChat()
        conversation.title = "Assistant corpus · \(label)"
        conversation.messages.append(ChatMessage(role: .user, text: command))
        let live = cases.allSatisfy(\.usesLiveData)
        conversation.corpusNormal = normal && live
        let intro = conversation.corpusNormal
            ? "Running \(cases.count) cases the way you use Wisp: the normal Ask path, with your memory, identity and tools. Anything that would change or send something is declined and noted as \"would ask to confirm\". Each case is saved in your chat list as \"Corpus · <case> · <request>\"."
            : live
            ? "Running \(cases.count) live cases with the local agent model and bounded reads of your real Wisp data. Expand source results and review each answer. Completed model calls are counted from inference responses. Writes and sends are blocked. Each case starts without chat history."
            : "Running \(cases.count) synthetic routing plans. Tools are not executed; deterministic routes may skip the model. These plans do not evaluate real-data answers."
        conversation.messages.append(ChatMessage(role: .assistant, text: intro))
        conversation.corpusTotal = cases.count
        conversation.corpusRunning = true
        conversation.lastUsed = Date()

        conversation.corpusTask = Task { [weak self, weak conversation] in
            guard let self, let conversation else { return }
            var matched = 0
            var reviewed = 0
            var modelCompleted = 0
            var answered = 0
            for corpusCase in cases {
                guard !Task.isCancelled else { break }
                self.send(corpusCase.requestPrompt, in: conversation, testMode: !live, corpusCase: corpusCase)
                while conversation.busy && !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                guard !Task.isCancelled else { break }
                if let evaluation = conversation.messages.last(where: { $0.role == .assistant })?.corpusEvaluation,
                   evaluation.completed {
                    answered += 1
                    if evaluation.toolPathMatches == true { matched += 1 } else { reviewed += 1 }
                    if evaluation.modelCalls > 0 { modelCompleted += 1 }
                } else {
                    reviewed += 1
                }
            }
            let wasCancelled = Task.isCancelled
            conversation.corpusRunning = false
            conversation.corpusTask = nil
            if !wasCancelled {
                let summary = conversation.corpusNormal
                    ? "Normal-mode corpus finished: \(answered)/\(cases.count) cases answered through the ordinary Ask path. Each is in your chat list as \"Corpus · <case> · <request>\". Review the answers; they are not automatically graded."
                    : live ? "Live corpus finished: \(modelCompleted)/\(cases.count) cases completed with verified model inference. Review the answers against each card's source results and checklist. Live answers are not automatically graded; a model call alone does not prove correctness."
                : """
                Corpus run finished: \(matched) tool path\(matched == 1 ? "" : "s") matched the expected list; \(reviewed) need review. This compares tool names only. Review the behavior rubric and Wisp's streamed response for each case.
                """
                conversation.messages.append(ChatMessage(role: .assistant, text: summary))
            }
        }
        return conversation
    }

    @discardableResult
    private func appendLocalNotice(_ text: String) -> ChatConversation {
        appendLocalNotices([text])
    }

    @discardableResult
    private func appendLocalNotices(_ texts: [String]) -> ChatConversation {
        let conversation = selected ?? newChat()
        guard !conversation.busy, !conversation.corpusRunning else { return conversation }
        if conversation.messages.isEmpty { conversation.title = "Corpus runner" }
        conversation.messages.append(contentsOf: texts.map { ChatMessage(role: .assistant, text: $0) })
        return conversation
    }

    /// Stop the reply in flight. The text so far stays; a pending approval is denied.
    func stop(_ conversation: ChatConversation) {
        guard conversation.busy || conversation.corpusRunning else { return }
        let stoppedCorpus = conversation.corpusRunning
        conversation.corpusTask?.cancel()
        conversation.corpusTask = nil
        conversation.corpusRunning = false
        guard conversation.busy else {
            if stoppedCorpus {
                conversation.messages.append(ChatMessage(role: .assistant, text: "Corpus run stopped. Completed cases remain above."))
            }
            return
        }
        conversation.task?.cancel()
        conversation.task = nil
        flush(conversation)
        conversation.runID = UUID()
        if let i = conversation.messages.lastIndex(where: { $0.role == .assistant }) {
            if conversation.messages[i].corpusEvaluation != nil {
                conversation.messages[i].corpusEvaluation?.observedTools = conversation.messages[i].tools.map(\.name)
                conversation.messages[i].corpusEvaluation?.stopped = true
            }
            if conversation.messages[i].approval?.state == .pending {
                resolve(conversation, messageID: conversation.messages[i].id, approved: false, scope: "once")
            }
            conversation.messages[i].phase = .done
            conversation.messages[i].meta?.finishedAt = Date()
        }
        conversation.busy = false
        if stoppedCorpus {
            conversation.messages.append(ChatMessage(role: .assistant, text: "Corpus run stopped. Completed cases remain above."))
        }
    }

    private func handle(_ event: ChatEvent, in conversation: ChatConversation, replyID: UUID, runID: UUID) {
        guard conversation.busy, conversation.runID == runID else { return }
        guard conversation.messages.contains(where: { $0.id == replyID }) else {
            conversation.busy = false     // the reply is gone; do not leave the chat stuck
            return
        }
        if event.type == "delta" {
            if conversation.streamStart == nil { conversation.streamStart = Date() }
            conversation.pendingDelta += event.str("text")
            conversation.streamChars += event.str("text").count
            scheduleFlush(conversation, replyID: replyID)
            return
        }
        flush(conversation)
        guard let i = conversation.messages.firstIndex(where: { $0.id == replyID }) else { return }
        var message = conversation.messages[i]
        let outcome = ChatTurnReducer.apply(event, to: &message)
        // The fix button under the reply comes from the tool's own result, never
        // the model's wording (the notch's OverlayModel reads it the same way).
        if event.type == "tool_result", message.corpusEvaluation == nil {
            for kind in AskAccess.kinds(event.payload["needs_access"]) where !message.needsAccess.contains(kind) {
                message.needsAccess.append(kind)
            }
        }
        // Corpus replies are text and read evidence, never actionable drafts.
        if message.corpusEvaluation != nil { message.draft = nil }
        if let start = conversation.streamStart, message.meta != nil {
            let rate = ChatTurnReducer.tokensPerSecond(chars: conversation.streamChars, since: start)
            if rate > 0 { message.meta?.tokensPerSecond = rate }
        }
        if event.type == "confirm", message.corpusEvaluation != nil, let approval = message.approval {
            message.approval?.state = .denied
            conversation.messages[i] = message
            Task {
                await self.backend.approve(sessionId: "", actionId: approval.actionId, approved: false,
                                           scope: "once", requestId: approval.requestId)
            }
            return
        }
        switch outcome {
        case .none:
            conversation.messages[i] = message
        case .session(let id):
            if message.corpusEvaluation == nil && conversation.sessionId.isEmpty { conversation.sessionId = id }
        case .finished, .failed:
            if message.approval?.state == .pending { message.approval?.state = .timedOut }
            conversation.messages[i] = message
            if conversation.messages[i].corpusEvaluation != nil {
                conversation.messages[i].corpusEvaluation?.observedTools = conversation.messages[i].tools.map(\.name)
                conversation.messages[i].corpusEvaluation?.completed = outcome == .finished
            }
            conversation.busy = false
            conversation.lastUsed = Date()
            conversation.preview = message.text.split(whereSeparator: { $0.isNewline }).first.map(String.init) ?? ""
            if message.corpusEvaluation == nil { Task { await self.refresh() } }
        case .droppedEmpty:
            // Nothing arrived: take the exchange back and hand the prompt to the composer.
            if message.corpusEvaluation != nil {
                message.isError = true
                message.text = "Wisp did not return a response. This case was not scored."
                message.phase = .failed
                conversation.messages[i] = message
            } else if i > 0, conversation.messages[i - 1].role == .user {
                conversation.restoredInput = conversation.messages[i - 1].text
                conversation.messages.removeSubrange((i - 1)...i)
            }
            conversation.busy = false
        }
    }

    private func scheduleFlush(_ conversation: ChatConversation, replyID: UUID) {
        guard !conversation.flushScheduled else { return }
        conversation.flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.06) { [weak self, weak conversation] in
            guard let self, let conversation else { return }
            self.flush(conversation)
        }
    }

    /// Fold buffered streamed text into the reply (at most one UI update per ~60 ms).
    private func flush(_ conversation: ChatConversation) {
        conversation.flushScheduled = false
        guard !conversation.pendingDelta.isEmpty,
              let i = conversation.messages.lastIndex(where: { $0.role == .assistant }) else { return }
        var message = conversation.messages[i]
        _ = ChatTurnReducer.apply(ChatEvent(type: "delta", payload: ["text": conversation.pendingDelta]), to: &message)
        conversation.pendingDelta = ""
        if let start = conversation.streamStart {
            let rate = ChatTurnReducer.tokensPerSecond(chars: conversation.streamChars, since: start)
            if rate > 0 { message.meta?.tokensPerSecond = rate }
        }
        conversation.messages[i] = message
    }

    // MARK: - Approvals and drafts

    func resolve(_ conversation: ChatConversation, messageID: UUID, approved: Bool, scope: String) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              conversation.busy,
              var approval = conversation.messages[i].approval, approval.state == .pending else { return }
        guard !(approved && conversation.messages[i].corpusEvaluation != nil) else { return }
        approval.state = approved ? .allowed : .denied
        conversation.messages[i].approval = approval
        conversation.messages[i].phase = .working
        let sid = conversation.sessionId
        Task { await backend.approve(sessionId: sid, actionId: approval.actionId, approved: approved,
                                     scope: scope, requestId: approval.requestId) }
    }

    func updateDraft(_ conversation: ChatConversation, messageID: UUID, _ change: (inout ChatDraft) -> Void) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              var draft = conversation.messages[i].draft else { return }
        change(&draft)
        conversation.messages[i].draft = draft
    }

    func sendDraft(_ conversation: ChatConversation, messageID: UUID) {
        guard let i = conversation.messages.firstIndex(where: { $0.id == messageID }),
              conversation.messages[i].corpusEvaluation == nil,
              let draft = conversation.messages[i].draft, !draft.isSending, !draft.sent, !draft.discarded else { return }
        guard !draft.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            updateDraft(conversation, messageID: messageID) { $0.status = "The message is empty." }
            return
        }
        updateDraft(conversation, messageID: messageID) { $0.isEditing = false; $0.isSending = true; $0.status = "Sending…" }
        Task {
            let outcome = await backend.sendDraft(to: draft.to, text: draft.text)
            updateDraft(conversation, messageID: messageID) {
                $0.isSending = false; $0.sent = outcome.ok
                $0.status = outcome.ok ? "Sent"
                    : outcome.result.contains("could not reach") ? outcome.result + " It may have sent, so check Messages before trying again."
                    : outcome.result
            }
        }
    }

    // MARK: - Access

    /// Re-read whether answers may go to a cloud provider. The only writer of
    /// cloudInUse: a reply that arrives after a newer request's is dropped, so a
    /// slow read from before a Settings change can't put "Local" back.
    func refreshCloudUse() async {
        cloudGeneration &+= 1
        let ticket = cloudGeneration
        let value = await backend.cloudInUse()
        guard ticket == cloudGeneration else { return }
        cloudInUse = value
    }
    private var cloudGeneration = 0

    func setFullAccess(_ on: Bool) {
        Task { applyFullAccess(await backend.setFullAccess(on)) }
    }

    /// Show Full access as the service confirmed it; nil marks it unconfirmed.
    /// The app also calls this when the flag changes elsewhere (quick settings,
    /// Settings), since all of them share one source.
    func applyFullAccess(_ value: Bool?) {
        if let value { fullAccess = value }
        fullAccessKnown = value != nil
    }
}
