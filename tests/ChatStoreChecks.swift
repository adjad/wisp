import Foundation

// Synthetic contract for ChatStore: sending, streaming, approvals, recovery from
// a dropped request, list merging, and deletion, against a scripted backend.
// No window, no network, no Wisp service.

@MainActor
final class ScriptedBackend: ChatBackend {
    var chats: [ChatSummary]? = []
    /// The next this-many list requests fail, like a service that is still starting.
    var listFailures = 0
    var history: [String: [ChatMessage]] = [:]
    var script: (String) -> [ChatEvent] = { _ in [] }
    var approvals: [(actionId: String, approved: Bool, scope: String, requestId: String, session: String)] = []
    var deleted: [String] = []
    var runs: [(prompt: String, session: String)] = []
    var liveCases: [AssistantTaskCorpusCase] = []
    var savedExport: (markdown: String, csv: String)?
    var liveRuns: [String] = []
    var normalRuns: [String] = []
    var promptsCommands: [CorpusPromptsCommand] = []
    /// Answers in turn; the last one repeats.
    var promptsStatuses: [CorpusPromptsStatus] = []
    var promptResults: [CorpusPromptsResult] = []
    var promptsExport: (markdown: String, csv: String)?
    var caseDelayMs: UInt64 = 0
    var liveDelayMs: UInt64 = 0
    var draftSends = 0
    var fullAccessValue = false
    var loadDelayMs: UInt64 = 0
    var holdNextHistoryLoad = false
    private var heldHistoryRelease: CheckedContinuation<Void, Never>?
    private var historyEntryWaiter: CheckedContinuation<Void, Never>?

    func waitForHeldHistoryLoad() async {
        if heldHistoryRelease != nil { return }
        await withCheckedContinuation { historyEntryWaiter = $0 }
    }

    func releaseHeldHistoryLoad() {
        guard let release = heldHistoryRelease else {
            preconditionFailure("Expected a held history load")
        }
        heldHistoryRelease = nil
        release.resume()
    }

    func listChats() async -> [ChatSummary]? {
        if listFailures > 0 { listFailures -= 1; return nil }
        return chats
    }
    func loadChat(id: String) async -> [ChatMessage]? {
        if holdNextHistoryLoad {
            holdNextHistoryLoad = false
            await withCheckedContinuation { release in
                precondition(heldHistoryRelease == nil, "Only one held history load")
                heldHistoryRelease = release
                let entered = historyEntryWaiter
                historyEntryWaiter = nil
                entered?.resume()
            }
        }
        if loadDelayMs > 0 { try? await Task.sleep(nanoseconds: loadDelayMs * 1_000_000) }
        return history[id]
    }
    func deleteChat(id: String) async -> Bool { deleted.append(id); return true }
    func liveCorpusCases(selector: String) async throws -> [AssistantTaskCorpusCase] {
        if caseDelayMs > 0 { try? await Task.sleep(nanoseconds: caseDelayMs * 1_000_000) }
        return liveCases // deliberately return even after cancellation
    }
    func savedCorpusExport() async throws -> (markdown: String, csv: String)? { savedExport }
    func corpusPrompts(_ command: CorpusPromptsCommand) async throws -> CorpusPromptsStatus {
        promptsCommands.append(command)
        return promptsStatuses.count > 1 ? promptsStatuses.removeFirst() : promptsStatuses[0]
    }
    func corpusPromptsResults(limit: Int) async throws -> [CorpusPromptsResult] { promptResults }
    func corpusPromptsExport() async throws -> (markdown: String, csv: String)? { promptsExport }
    func runLiveCorpusCase(id: String, token: String, normal: Bool,
                           onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        if normal { normalRuns.append(id) }
        liveRuns.append(id)
        if liveDelayMs > 0 { try? await Task.sleep(nanoseconds: liveDelayMs * 1_000_000) }
        for event in script(id) { onEvent(event) }
    }
    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             testMode: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        runs.append((prompt, sessionId))
        for event in script(prompt) { onEvent(event) }
    }
    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async {
        approvals.append((actionId, approved, scope, requestId, sessionId))
    }
    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String) { draftSends += 1; return (true, "Sent") }
    func fullAccess() async -> Bool? { fullAccessValue }
    func setFullAccess(_ on: Bool) async -> Bool? { fullAccessValue = on; return on }
}

@main
struct ChatStoreChecks {
    nonisolated(unsafe) static var count = 0
    static func check(_ condition: Bool, _ message: String) {
        precondition(condition, message)
        count += 1
    }
    static func ev(_ type: String, _ fields: [String: Any] = [:]) -> ChatEvent {
        var payload = fields; payload["type"] = type
        return ChatEvent(type: type, payload: payload)
    }
    static func settle(_ ms: UInt64 = 150) async { try? await Task.sleep(nanoseconds: ms * 1_000_000) }

    static func main() async {
        await MainActor.run { }
        await run()
        print("Chat store checks passed (\(count)).")
    }

    @MainActor static func run() async {
        for number in 1...10 {
            if case .live(let selector)? = try! AssistantTaskCorpusCommand.resolve("/corpus batch-\(number)") {
                check(selector == "batch-\(number)", "batch command reaches the live backend")
            } else { check(false, "batch command must be live") }
        }
        if case .catalog? = try! AssistantTaskCorpusCommand.resolve("/corpus list") {
            check(true, "catalog can be browsed without running inference")
        } else { check(false, "list must resolve to browsing") }
        // Live cases use the isolated server corpus path, and only completed
        // inference receipts may claim model use. Source results stay inspectable.
        do {
            let backend = ScriptedBackend()
            let data = Data(#"{"id":"LIVE-001","split":"live","mode":"live","category":"messages","difficulty":"basic","target":"Example Person","token":"t1","conversation":[{"role":"user","content":"Read my recent messages with Example Person."}],"setup":"local","expected":{"tools":["view_messages"],"behavior":["Verify the source quote."]},"risk":"read_only"}"#.utf8)
            backend.liveCases = [try! JSONDecoder().decode(AssistantTaskCorpusCase.self, from: data)]
            let catalogStore = ChatStore(backend: backend)
            catalogStore.submit("/corpus list")
            await settle(200)
            check(catalogStore.selected?.messages.count == 3, "catalog uses separate header, case and command entries")
            check(catalogStore.selected?.messages[1].text.contains("LIVE-001") == true, "catalog entry contains its runnable ID")
            check(backend.liveRuns.isEmpty && backend.runs.isEmpty, "browsing cases never invokes inference")
            backend.script = { _ in [
                ev("corpus_model", ["model": "agent-local", "started": 1, "completed": 0]),
                ev("corpus_model", ["model": "agent-local", "started": 1, "completed": 1]),
                ev("tool_call", ["id": "read1", "name": "view_messages", "decision": "allow"]),
                ev("tool_result", ["id": "read1", "result": "Example Person: source quotation"]),
                ev("text", ["text": "An answer grounded in the source."]), ev("done")
            ] }
            let store = ChatStore(backend: backend)
            store.submit("/corpus smoke")
            await settle(500)
            let conversation = store.selected!
            let reply = conversation.messages.first { $0.corpusEvaluation != nil }!
            check(backend.runs.isEmpty && backend.liveRuns == ["LIVE-001"], "live cases bypass ordinary agent routing")
            check(conversation.messages.contains { $0.text == "Read my recent messages with Example Person." }, "live request has no synthetic wrapper")
            check(reply.corpusEvaluation?.modelCalls == 1, "actual inference receipt is visible")
            check(reply.corpusEvaluation?.toolPathMatches == nil, "live data is reviewed, not falsely graded")
            check(reply.tools.first?.result.contains("source quotation") == true, "real source output stays inspectable")
            check(conversation.sessionId.isEmpty, "corpus does not reuse a saved session")
            check(conversation.messages.last!.text.contains("1/1"), "summary counts verified model completions")
        }
        // /corpus export: parsing, help, and the files it writes.
        do {
            if case .export? = try! AssistantTaskCorpusCommand.resolve("/corpus export") {
                check(true, "/corpus export resolves to export")
            } else { check(false, "/corpus export must resolve to export") }
            check((try? AssistantTaskCorpusCommand.resolve("/corpus export now")) == nil, "export takes no arguments")
            check(AssistantTaskCorpusCommand.helpText.contains("/corpus export"), "help lists /corpus export")
            check(AssistantTaskCorpusCommand.CommandError.usage.errorDescription?.contains("export") == true,
                  "the usage line names export")
            let backend = ScriptedBackend()
            let data = Data(#"{"id":"LIVE-001","split":"live","mode":"live","category":"messages","difficulty":"basic","target":"Example Person","token":"t1","conversation":[{"role":"user","content":"Read my recent messages with Example Person."}],"setup":"local","expected":{"tools":["view_messages"],"behavior":["Verify the source quote."]},"risk":"read_only"}"#.utf8)
            backend.liveCases = [try! JSONDecoder().decode(AssistantTaskCorpusCase.self, from: data)]
            backend.script = { _ in [
                ev("corpus_model", ["model": "agent-local", "started": 1, "completed": 1]),
                ev("tool_call", ["id": "read1", "name": "view_messages", "decision": "allow"]),
                ev("tool_result", ["id": "read1", "result": "Example Person: source quotation"]),
                ev("text", ["text": "Line one\nLine two, \"quoted\""]), ev("done")
            ] }
            let store = ChatStore(backend: backend)
            // TMPDIR, not FileManager's temporaryDirectory: Foundation ignores TMPDIR, and
            // the runner's per-gate directory is the one place a fixture may write.
            let scratch = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory(),
                              isDirectory: true).appendingPathComponent("corpus-export-\(UUID().uuidString)")
            store.exportDirectory = { scratch }
            do {
                _ = try await store.exportCorpus()
                check(false, "an export with no run must fail")
            } catch {
                check(error.localizedDescription.contains("no corpus run"), "no run to export says so")
            }
            check(store.corpusMarkdown(store.latestCorpusConversation) == nil, "nothing to copy without a run")
            store.submit("/corpus smoke")
            await settle(500)
            let files = try! await store.exportCorpus(now: Date(timeIntervalSince1970: 0))
            check(!files.saved && files.markdown.deletingLastPathComponent().path == scratch.path
                    && files.markdown.lastPathComponent.hasPrefix("wisp-corpus-") && files.markdown.pathExtension == "md"
                    && files.csv.deletingPathExtension().lastPathComponent == files.markdown.deletingPathExtension().lastPathComponent,
                  "an unsaved run exports from the screen as a .md and .csv pair")
            let markdown = (try? String(contentsOf: files.markdown, encoding: .utf8)) ?? ""
            check(markdown.contains("## LIVE-001 · messages · done")
                    && markdown.contains("**Prompt:** Read my recent messages with Example Person.")
                    && markdown.contains("- `view_messages`") && markdown.contains("  > Example Person: source quotation")
                    && markdown.contains("> Line one\n> Line two") && markdown.contains("inference verified"),
                  "the Markdown has a section per case: prompt, tools, answer, status")
            let csv = (try? String(contentsOf: files.csv, encoding: .utf8)) ?? ""
            check(csv.hasPrefix(CorpusExport.csvColumns.joined(separator: ",") + "\r\n")
                    && csv.contains("\"Line one\nLine two, \"\"quoted\"\"\""), "the CSV quotes commas, quotes and line breaks")
            check(store.corpusMarkdown(store.latestCorpusConversation)?.contains("LIVE-001") == true,
                  "Copy results uses the run on screen")
            backend.savedExport = ("# saved run", "case_id\r\n")
            let saved = try! await store.exportCorpus(now: Date(timeIntervalSince1970: 60))
            check(saved.saved && (try? String(contentsOf: saved.markdown, encoding: .utf8)) == "# saved run",
                  "a saved run is preferred over the screen")
            let notch = NotchCorpusController(store: store)
            notch.submit("/corpus export")
            await settle(200)
            check(notch.isPresented && notch.exported?.saved == true && notch.notice.isEmpty
                    && backend.liveRuns == ["LIVE-001"], "the notch's /corpus export writes the files and runs nothing")
            try? FileManager.default.removeItem(at: scratch)
        }
        // /corpus prompts …: the user's own prompts file, run by the service.
        do {
            func prompts(_ text: String) -> CorpusPromptsCommand? {
                if case .prompts(let command)? = try? AssistantTaskCorpusCommand.resolve(text) { return command }
                return nil
            }
            check(prompts("/corpus prompts") == .status && prompts("/corpus prompts status") == .status,
                  "/corpus prompts alone shows progress")
            check(prompts("/corpus prompts batch-3") == .start("batch-3") && prompts("/corpus prompts all") == .start("all")
                    && prompts("/corpus prompts 17") == .start("17") && prompts("/corpus prompts P-17") == .start("p-17")
                    && prompts("/corpus prompts section 4") == .start("section 4"), "prompts selectors")
            check(prompts("/corpus prompts resume") == .resume && prompts("/corpus prompts stop") == .stop
                    && prompts("/corpus prompts export") == .export
                    && prompts("/corpus prompts retry-failed") == .retryFailed, "prompts verbs")
            check(prompts("/corpus prompts import ~/Desktop/my prompts.md") == .importFile("~/Desktop/my prompts.md")
                    && prompts("/corpus prompts import \"/tmp/a b.md\"") == .importFile("/tmp/a b.md"),
                  "import keeps the whole path, spaces and all")
            for bad in ["/corpus prompts go", "/corpus prompts section x", "/corpus prompts import", "/corpus prompts batch-1 now"] {
                do {
                    _ = try AssistantTaskCorpusCommand.resolve(bad)
                    check(false, "\(bad) must be refused")
                } catch {
                    check(error.localizedDescription.contains("/corpus prompts import"), "\(bad) explains the prompts commands")
                }
            }
            check(AssistantTaskCorpusCommand.helpText.contains("/corpus prompts import <path>")
                    && AssistantTaskCorpusCommand.helpText.contains("8–15 hours")
                    && AssistantTaskCorpusCommand.helpText.contains("batches of 100"), "help sets expectations for 1,000 prompts")
            check(CorpusPromptsStatus.eta(nil) == nil && CorpusPromptsStatus.eta(90) == "about 2 min"
                    && CorpusPromptsStatus.eta(3600) == "about 1 h" && CorpusPromptsStatus.eta(7_800) == "about 2 h 10 min",
                  "ETA reads in minutes and hours")
            func status(_ json: String) -> CorpusPromptsStatus {
                try! JSONDecoder().decode(CorpusPromptsStatus.self, from: Data(json.utf8))
            }
            let imported = #"{"sha256":"abc","cases":6,"imported_at":"x","sections":[{"number":1,"title":"Reading","count":3}]}"#
            let running = status(#"{"imported":\#(imported),"run":{"run_id":"prompts-1","selector":"batch-1","status":"running","started_at":"x","error":"","total":100,"completed":40,"ok":30,"declined":8,"errors":2,"remaining":60,"eta_s":1800,"waiting_for_idle":false,"current":{"case_id":"P-41","section":"3. Mail","tag":"W","prompt":"Email Example Person","turns":2,"turn":1,"elapsed_s":4.2}}}"#)
            let finished = status(#"{"imported":\#(imported),"run":{"run_id":"prompts-1","selector":"batch-1","status":"finished","started_at":"x","error":"","total":100,"completed":100,"ok":80,"declined":15,"errors":5,"remaining":0,"eta_s":null,"waiting_for_idle":false,"current":null}}"#)
            check(running.summary.contains("40/100 done · 30 OK · 8 declined · 2 failed · Running · about 30 min left")
                    && running.summary.contains("Now: P-41 (3. Mail) Email Example Person"), "the summary reads as progress")
            let retrying = status(#"{"imported":null,"run":{"run_id":"r","selector":"all","status":"running","started_at":"x","error":"","total":10,"completed":10,"ok":7,"declined":1,"errors":2,"retrying":2,"remaining":2,"eta_s":60,"waiting_for_idle":false,"current":null}}"#)
            check(retrying.run?.statusLabel == "Retrying 2 failed", "a retry says how many failed cases it is running again")
            check(status(#"{"imported":null,"run":null}"#).summary.contains("/corpus prompts import"),
                  "without a file, the summary says how to import one")
            let result = try! JSONDecoder().decode(CorpusPromptsResult.self, from: Data(#"{"case_id":"P-3","category":"1. Reading","tag":"R","status":"done","prompt":"A → B","answer":"b","model":"ling","latency_s":12.5,"error":"","confirmations":[{"tool":"send_email","reason":"r"}],"turns":[{"prompt":"A","answer":"a"},{"prompt":"B","answer":"b"}]}"#.utf8))
            check(result.exchanges.map(\.prompt) == ["A", "B"] && result.outcome == "would ask to confirm send_email",
                  "a multi-turn result shows each turn, and its declined confirmation")
            let backend = ScriptedBackend()
            backend.promptsStatuses = [running, running, finished]
            backend.promptResults = [result]
            backend.promptsExport = ("# prompts run", "case_id\r\n")
            let store = ChatStore(backend: backend)
            let scratch = URL(fileURLWithPath: ProcessInfo.processInfo.environment["TMPDIR"] ?? NSTemporaryDirectory(),
                              isDirectory: true).appendingPathComponent("prompts-export-\(UUID().uuidString)")
            store.exportDirectory = { scratch }
            let notch = NotchCorpusController(store: store)
            notch.pollInterval = 20_000_000
            notch.submit("/corpus prompts batch-1")
            await settle(400)
            check(backend.promptsCommands.first == .start("batch-1") && backend.liveRuns.isEmpty && backend.runs.isEmpty,
                  "the notch asks the service to start the job and runs nothing itself")
            check(notch.isPresented && notch.prompts?.run?.status == "finished" && notch.promptResults == [result]
                    && !notch.busy, "it follows progress until the job finishes, with the full answers")
            check(backend.promptsCommands.dropFirst().allSatisfy { $0 == .status }, "following only reads status")
            notch.submit("/corpus prompts export")
            await settle(200)
            check(notch.exported?.markdown.lastPathComponent.hasPrefix("wisp-prompts-") == true
                    && (try? String(contentsOf: notch.exported!.markdown, encoding: .utf8)) == "# prompts run",
                  "export writes the prompts run to Downloads")
            backend.promptsStatuses = [finished]
            store.submit("/corpus prompts stop")
            await settle(200)
            check(backend.promptsCommands.last == .stop
                    && store.selected?.messages.last?.text.contains("100/100 done · 80 OK · 15 declined · 5 failed · Finished") == true,
                  "the window sends the command and shows the progress as text")
            try? FileManager.default.removeItem(at: scratch)
        }
        // /corpus … normal: the same cases through the ordinary Ask path.
        do {
            if case .liveNormal(let selector)? = try! AssistantTaskCorpusCommand.resolve("/corpus batch-2 normal") {
                check(selector == "batch-2", "/corpus batch-2 normal keeps its selector")
            } else { check(false, "/corpus batch-2 normal must resolve to normal mode") }
            if case .live? = try! AssistantTaskCorpusCommand.resolve("/corpus all") {
                check(true, "the bounded mode stays the default")
            } else { check(false, "/corpus all must stay bounded") }
            check((try? AssistantTaskCorpusCommand.resolve("/corpus all loud")) == nil, "only normal is a mode word")
            check(AssistantTaskCorpusCommand.helpText.contains("/corpus all normal")
                    && AssistantTaskCorpusCommand.helpText.contains("would ask to confirm"),
                  "help explains normal mode and its declined confirmations")
            let backend = ScriptedBackend()
            let data = Data(#"{"id":"LIVE-001","split":"live","mode":"live","category":"messages","difficulty":"basic","target":"Example Person","token":"t1","conversation":[{"role":"user","content":"Read my recent messages with Example Person."}],"setup":"local","expected":{"tools":["view_messages"],"behavior":["Verify the source quote."]},"risk":"read_only"}"#.utf8)
            backend.liveCases = [try! JSONDecoder().decode(AssistantTaskCorpusCase.self, from: data)]
            backend.script = { _ in [
                ev("routed", ["model": "ling", "role": "agent", "needs_tools": true]),
                ev("corpus_session", ["id": "abc"]),
                ev("tool_call", ["id": "w", "name": "add_reminder", "decision": "confirm"]),
                ev("corpus_declined", ["id": "w", "tool": "add_reminder", "reason": "review run"]),
                ev("tool_result", ["id": "w", "result": "User denied add_reminder."]),
                ev("text", ["text": "The full answer, as Wisp says it."]), ev("done")
            ] }
            let store = ChatStore(backend: backend)
            store.submit("/corpus smoke normal")
            await settle(500)
            let run = store.selected!
            let reply = run.messages.first { $0.corpusEvaluation != nil }!
            check(backend.normalRuns == ["LIVE-001"] && backend.runs.isEmpty, "normal mode asks the service for the ordinary path")
            check(reply.corpusEvaluation?.normalMode == true && reply.corpusEvaluation?.completed == true,
                  "the case card knows its mode")
            check(run.sessionId.isEmpty && reply.approval == nil, "no session is adopted and no card is offered")
            check(run.messages.last?.text.contains("Normal-mode corpus finished: 1/1") == true, "the summary counts answered cases")
            let exported = CorpusExport.cases(from: run.messages)
            check(exported.first?.mode == "normal" && exported.first?.answer == "The full answer, as Wisp says it."
                    && exported.first?.verified == false, "the export carries the mode and the full answer")
            check(CorpusExport.markdown(exported, title: run.title).contains("**Mode:** normal")
                    && CorpusExport.csv(exported).contains("normal,LIVE-001"), "Markdown and CSV both name the mode")
        }
        // The notch shares guarded execution and pins its own conversation.
        do {
            let backend = ScriptedBackend()
            let data = Data(#"{"id":"LIVE-001","split":"live","mode":"live","category":"mail","difficulty":"basic","target":"example@sender.invalid","token":"t1","conversation":[{"role":"user","content":"Read my recent mail."}],"setup":"local","expected":{"tools":["view_emails"],"behavior":["Verify the source."]},"risk":"read_only"}"#.utf8)
            backend.liveCases = [try! JSONDecoder().decode(AssistantTaskCorpusCase.self, from: data)]
            backend.script = { _ in [
                ev("corpus_model", ["model": "local", "started": 1, "completed": 1]),
                ev("tool_call", ["id": "read", "name": "view_emails", "decision": "allow"]),
                ev("tool_result", ["id": "read", "result": "synthetic mail source"]),
                ev("message_draft", ["to": "Example Person", "text": "must stay blocked"]),
                ev("confirm", ["id": "blocked", "tool": "send_message", "request_id": "request"]),
                ev("send_email", ["to": "example@sender.invalid", "body": "must not send"]),
                ev("text", ["text": "Source-grounded answer"]), ev("done")
            ] }
            let store = ChatStore(backend: backend)
            let notch = NotchCorpusController(store: store)
            notch.canStart = { false } // normal request/brief admission is shared by buttons and typing
            notch.submit("/corpus smoke")
            await settle(100)
            check(!notch.loading && !notch.isPresented && backend.liveRuns.isEmpty,
                  "button submission respects the shared active-request admission check")
            notch.canStart = { true }
            notch.submit("/corpus list")
            await settle(200)
            check(notch.isPresented && notch.conversation?.messages.count == 3, "notch browses the shared catalog")
            check(backend.liveRuns.isEmpty && backend.runs.isEmpty, "notch browsing calls no model")
            notch.submit("/corpus smoke")
            await settle(500)
            let run = notch.conversation!
            check(run === store.selected, "notch and window share one run object")
            check(run.corpusTotal == 1 && run.corpusFinishedCount == 1 && run.corpusVerifiedCount == 1, "notch progress uses finished cards and receipts")
            check(run.messages.contains { $0.tools.first?.result == "synthetic mail source" }, "notch retains source evidence")
            check(run.messages.allSatisfy { $0.draft == nil }, "corpus never renders an actionable draft")
            check(backend.approvals.count == 1 && !backend.approvals[0].approved, "corpus confirmation is denied automatically")
            if let i = run.messages.firstIndex(where: { $0.corpusEvaluation != nil }) {
                run.messages[i].draft = ChatDraft(to: "Example Person", text: "must not send")
                store.sendDraft(run, messageID: run.messages[i].id)
                await settle(100)
                check(backend.draftSends == 0, "even an injected corpus draft cannot send")
                run.messages[i].draft = nil
            }
            check(backend.runs.isEmpty && backend.draftSends == 0, "notch bypasses normal inference and outbound handlers")
            _ = store.newChat()
            check(notch.conversation === run && store.selected !== run, "window selection cannot switch notch evidence")
            notch.selectInWindow()
            check(store.selected === run && backend.liveRuns.count == 1, "window handoff preserves results without replay")
            notch.hide()
            check(!notch.isPresented && notch.conversation === run, "hiding preserves completed evidence")
        }
        do {
            let backend = ScriptedBackend()
            backend.caseDelayMs = 500
            let store = ChatStore(backend: backend)
            let notch = NotchCorpusController(store: store)
            notch.submit("/corpus all")
            check(notch.loading && notch.busy, "loading reserves the notch before transport finishes")
            await settle(100)
            notch.stopAndHide()
            await settle(600)
            check(!notch.busy && !notch.isPresented && notch.conversation == nil, "New chat rejects a late catalog response")
            check(backend.liveRuns.isEmpty && backend.runs.isEmpty, "cancelling loading cannot start a model afterward")
            notch.submit("ordinary question")
            await settle(100)
            check(backend.runs.isEmpty && !notch.notice.isEmpty, "controller accepts corpus commands only")
        }
        do {
            let backend = ScriptedBackend()
            let data = Data(#"{"id":"LIVE-001","split":"live","mode":"live","category":"contacts","difficulty":"basic","token":"t1","conversation":[{"role":"user","content":"Find Example Person."}],"setup":"local","expected":{"tools":["lookup_contact"],"behavior":["Review source."]},"risk":"read_only"}"#.utf8)
            let one = try! JSONDecoder().decode(AssistantTaskCorpusCase.self, from: data)
            backend.liveCases = [one, one]
            backend.liveDelayMs = 700
            backend.script = { _ in [ev("text", ["text":"late answer"]), ev("done")] }
            let store = ChatStore(backend: backend)
            let notch = NotchCorpusController(store: store)
            notch.submit("/corpus all")
            await settle(150)
            notch.submit("/corpus smoke")
            check(backend.liveRuns.count == 1, "another corpus command cannot duplicate an active run")
            notch.stop()
            await settle(800)
            check(!notch.busy && notch.isPresented, "Stop leaves results visible")
            check(backend.liveRuns.count == 1, "Stop cancels remaining cases")
            check(notch.conversation?.messages.contains { $0.text == "late answer" } == false, "late stream events cannot rewrite a stopped result")
        }
        // Current main keeps only a short ordinary-tool preview. Live corpus
        // review deliberately retains separate bounded multiline evidence.
        do {
            let source = String(repeating: "A", count: 120) + "\nsecond line: example@sender.invalid\nthird line"
            let ordinary = ChatToolRow(id: "ordinary", name: "view_emails", decision: "allow", result: source, finished: true)
            check(ordinary.result == String(repeating: "A", count: 61), "ordinary result retains main's preview cap")
            check(ordinary.corpusSourceEvidence == nil, "ordinary rows retain no corpus evidence")
            let testPlan = ChatToolRow(id: "plan", name: "view_emails", decision: "allow", isTestPlan: true)
            check(testPlan.isTestPlan && testPlan.detail == "dry run · not executed", "custom initializer accepts synthetic test plans")
            var reply = ChatMessage(role: .assistant, phase: .waiting)
            reply.corpusEvaluation = ChatCorpusEvaluation(caseID: "LIVE-001", split: "live", category: "mail",
                expectedTools: ["view_emails"], behavior: ["Compare source."], usesLiveData: true)
            _ = ChatTurnReducer.apply(ev("tool_call", ["id": "source", "name": "view_emails", "decision": "allow"]), to: &reply)
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "source", "result": source]), to: &reply)
            check(reply.tools[0].result.count == 61, "live tool trace keeps the same compact preview")
            check(reply.tools[0].corpusSourceEvidence?.text == source, "live source evidence preserves lines and text past character 61")
            check(reply.tools[0].corpusSourceEvidence?.isTruncated == false, "small source reports complete retained evidence")
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "source", "result": "replacement\nfull second line"]), to: &reply)
            check(reply.tools[0].corpusSourceEvidence?.text == "replacement\nfull second line", "replacement source replaces evidence rather than appending")
            _ = ChatTurnReducer.apply(ev("tool_call", ["id": "second", "name": "view_emails", "decision": "allow"]), to: &reply)
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "second", "result": "another\nsource"]), to: &reply)
            reply.corpusEvaluation = nil
            _ = ChatTurnReducer.apply(ev("text", ["text": "ordinary response"]), to: &reply)
            check(reply.tools.allSatisfy { $0.corpusSourceEvidence == nil }, "leaving live mode clears every row even on a non-result event")
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "source", "result": source]), to: &reply)
            check(reply.tools[0].corpusSourceEvidence == nil, "non-corpus updates cannot keep prior corpus evidence")
            var synthetic = ChatMessage(role: .assistant, phase: .waiting)
            synthetic.corpusEvaluation = ChatCorpusEvaluation(caseID: "ATC-001", split: "smoke", category: "mail",
                expectedTools: ["view_emails"], behavior: [], usesLiveData: false)
            _ = ChatTurnReducer.apply(ev("tool_call", ["id": "plan", "name": "view_emails", "decision": "allow", "test_mode": true]), to: &synthetic)
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "plan", "result": source]), to: &synthetic)
            check(synthetic.tools[0].isTestPlan && synthetic.tools[0].corpusSourceEvidence == nil,
                  "synthetic tool plans never opt in to real source retention")
        }
        do {
            let largeSource = String(repeating: "😀", count: 40_000) + "\ntrailing source line"
            var reply = ChatMessage(role: .assistant, phase: .waiting)
            reply.corpusEvaluation = ChatCorpusEvaluation(caseID: "LIVE-002", split: "live", category: "messages",
                expectedTools: ["view_messages"], behavior: [], usesLiveData: true)
            for index in 0..<6 {
                let id = "bounded-\(index)"
                _ = ChatTurnReducer.apply(ev("tool_call", ["id": id, "name": "view_messages", "decision": "allow"]), to: &reply)
                _ = ChatTurnReducer.apply(ev("tool_result", ["id": id, "result": largeSource]), to: &reply)
            }
            let evidence = reply.tools.compactMap(\.corpusSourceEvidence)
            check(evidence.count == 6 && evidence.allSatisfy(\.isTruncated), "every capped or omitted source discloses truncation")
            check(evidence.allSatisfy { $0.retainedByteCount <= ChatCorpusSourceEvidence.perResultByteLimit }, "UTF-8 per-result evidence is bounded")
            check(evidence.reduce(0) { $0 + $1.retainedByteCount } == ChatCorpusSourceEvidence.perCaseByteLimit,
                  "all evidence together obeys the per-case byte budget")
            check(evidence[4].text.isEmpty && evidence[4].sourceByteCount == largeSource.utf8.count,
                  "exhausted budget records omission instead of pretending an empty source")
            let boundary = ChatCorpusSourceEvidence("ab😀z", byteLimit: 5)
            check(boundary.text == "ab" && boundary.isTruncated && !boundary.text.contains("�"), "byte boundary preserves valid UTF-8 without replacement glyphs")
            let empty = ChatCorpusSourceEvidence("", byteLimit: 0)
            check(empty.text.isEmpty && !empty.isTruncated, "genuinely empty source differs from omitted evidence")
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "bounded-0", "result": "short\nreplacement"]), to: &reply)
            _ = ChatTurnReducer.apply(ev("tool_result", ["id": "bounded-4", "result": "now retained\nsecond line"]), to: &reply)
            check(reply.tools[4].corpusSourceEvidence?.text == "now retained\nsecond line", "replacement frees the old source's byte allocation")
            check(reply.tools.reduce(0) { $0 + ($1.corpusSourceEvidence?.retainedByteCount ?? 0) } <= ChatCorpusSourceEvidence.perCaseByteLimit,
                  "replacing results preserves the case budget")
        }
        // A full reply: session, route, tools, streamed text, done.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [
                ev("session", ["id": "s1"]),
                ev("routed", ["model": "Ling-3.0-tiny-oQ6e", "role": "agent", "needs_tools": true, "route_source": "rules", "reason": "mail"]),
                ev("tool_call", ["id": "c1", "name": "summarize_emails", "decision": "allow"]),
                ev("tool_result", ["id": "c1", "result": "4 messages"]),
                ev("delta", ["text": "You have "]), ev("delta", ["text": "4 messages."]),
                ev("done"),
            ] }
            let store = ChatStore(backend: backend)
            store.newChat()
            store.send("  Summarize   my mail  ")
            await settle()
            let chat = store.selected!
            check(chat.title == "Summarize my mail", "title comes from the first prompt: \(chat.title)")
            check(chat.sessionId == "s1", "the session id is adopted")
            check(chat.messages.count == 2 && chat.messages[0].role == .user && chat.messages[0].text == "Summarize   my mail", "user message is trimmed and kept")
            let reply = chat.messages[1]
            check(reply.text == "You have 4 messages.", "streamed text is joined: \(reply.text)")
            check(reply.phase == .done && !chat.busy, "the exchange finishes")
            check(reply.tools.count == 1 && reply.tools[0].finished && reply.tools[0].result == "4 messages", "tool row finished")
            check(reply.meta?.shortModel == "Ling-3.0-tiny" && reply.meta?.duration != nil, "metadata is recorded")
            check(chat.preview == "You have 4 messages.", "preview follows the reply")
            check(backend.runs.count == 1 && backend.runs[0].session == "", "first send starts a new session")
            // A follow-up continues the same session.
            store.send("and my calendar?")
            await settle()
            check(backend.runs.count == 2 && backend.runs[1].session == "s1", "follow-up reuses the session")
            check(chat.messages.count == 4, "history grows by one exchange")
            // A second send while busy is ignored.
            backend.script = { _ in [ev("routed", ["model": "m", "role": "general"]), ev("delta", ["text": "x"])] }
            store.send("one")
            await settle()
            check(chat.busy, "a reply with no done stays busy")
            let before = chat.messages.count
            store.send("two")
            check(chat.messages.count == before, "sending while busy does nothing")
            store.stop(chat)
            check(!chat.busy && chat.messages.last?.phase == .done && chat.messages.last?.text == "x", "stop keeps the text and ends the reply")
        }

        // A dropped request with nothing received is handed back to the composer.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("error", ["message": "The assistant didn't respond.", "dropped": true])] }
            let store = ChatStore(backend: backend)
            store.send("hello there")
            await settle()
            let chat = store.selected!
            check(chat.messages.isEmpty, "the failed exchange is removed")
            check(chat.restoredInput == "hello there", "the prompt is restored")
            check(!chat.busy, "dropped request frees the chat")
        }

        // A real failure keeps the exchange and shows the error.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("routed", ["model": "m", "role": "general"]), ev("error", ["message": "Out of memory", "detail": "oom"])] }
            let store = ChatStore(backend: backend)
            store.send("big question")
            await settle()
            let reply = store.selected!.messages.last!
            check(reply.isError && reply.text == "Out of memory" && reply.errorDetail == "oom", "failure is shown")
            check(!store.selected!.busy, "failure frees the chat")
        }

        // Approvals are answered through the service with the request id.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [
                ev("session", ["id": "s2"]),
                ev("routed", ["model": "m", "role": "agent", "needs_tools": true]),
                ev("confirm", ["id": "a1", "request_id": "r1", "tool": "run_shell", "reason": "Delete files", "grantable": false]),
            ] }
            let store = ChatStore(backend: backend)
            store.send("delete old downloads")
            await settle()
            let chat = store.selected!
            let reply = chat.messages.last!
            check(reply.approval?.state == .pending && chat.busy, "waiting on a yes")
            store.resolve(chat, messageID: reply.id, approved: false, scope: "once")
            await settle()
            check(chat.messages.last?.approval?.state == .denied, "card shows denied")
            check(backend.approvals.count == 1 && backend.approvals[0].approved == false && backend.approvals[0].requestId == "r1"
                  && backend.approvals[0].actionId == "a1" && backend.approvals[0].session == "s2", "service gets the denial for the right request")
            store.resolve(chat, messageID: reply.id, approved: true, scope: "once")
            await settle()
            check(backend.approvals.count == 1 && chat.messages.last?.approval?.state == .denied, "a card cannot be answered twice")
            store.stop(chat)
        }

        // A card still open when the reply ends cannot be answered afterwards.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("session", ["id": "s4"]), ev("confirm", ["id": "a2", "request_id": "r2", "tool": "t", "reason": "r"]),
                                      ev("error", ["message": "The connection dropped.", "dropped": true])] }
            let store = ChatStore(backend: backend)
            store.send("something risky")
            await settle()
            let chat = store.selected!
            check(!chat.busy, "the failed reply frees the chat")
            check(chat.messages.last?.approval?.state == .timedOut, "an unanswered card closes when the reply ends")
            store.resolve(chat, messageID: chat.messages.last!.id, approved: true, scope: "once")
            await settle()
            check(backend.approvals.isEmpty && chat.messages.last?.approval?.state == .timedOut, "a closed card cannot be allowed")
        }

        // Stopping while a card is open denies it.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("session", ["id": "s3"]), ev("confirm", ["id": "a9", "request_id": "r9", "tool": "t", "reason": "r"])] }
            let store = ChatStore(backend: backend)
            store.send("do it")
            await settle()
            store.stop(store.selected!)
            await settle()
            check(backend.approvals.count == 1 && backend.approvals[0].approved == false, "stop denies the open card")
        }

        // Drafts are sent only on request and only once.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in [ev("routed", ["model": "m", "role": "agent"]), ev("message_draft", ["to": "Priya", "text": "Count me in"]), ev("done")] }
            let store = ChatStore(backend: backend)
            store.send("tell Priya I'm in")
            await settle()
            let chat = store.selected!
            let id = chat.messages.last!.id
            check(chat.messages.last?.draft?.sent == false, "draft starts unsent")
            store.sendDraft(chat, messageID: id)
            await settle()
            check(chat.messages.last?.draft?.sent == true && chat.messages.last?.draft?.status == "Sent", "draft sends on request")
            store.updateDraft(chat, messageID: id) { $0.text = "   " }
            store.sendDraft(chat, messageID: id)
            check(chat.messages.last?.draft?.status == "Sent", "a sent draft is not sent again")
        }

        // List merging, sections, search, selection, history, deletion.
        do {
            let backend = ScriptedBackend()
            let now = Date()
            backend.chats = [
                ChatSummary(id: "a", title: "Alpha trip", preview: "pack light", lastUsed: now, turnCount: 2),
                ChatSummary(id: "b", title: "Beta lease", preview: "sixty days", lastUsed: now.addingTimeInterval(-3 * 86_400), turnCount: 2),
                ChatSummary(id: "c", title: "Gamma", preview: "", lastUsed: now.addingTimeInterval(-40 * 86_400), turnCount: 1),
            ]
            backend.history["a"] = [ChatMessage(role: .user, text: "Packing list?"), ChatMessage(role: .assistant, text: "Pack light.")]
            let store = ChatStore(backend: backend)
            await store.refresh()
            check(store.reachable && store.conversations.count == 3, "list loads")
            check(store.sections.map(\.title) == ["Today", "Previous 7 days", "Earlier"], "sections: \(store.sections.map(\.title))")
            await store.refresh()
            check(store.conversations.count == 3, "refreshing again does not duplicate")
            store.search = "lease"
            check(store.sections.flatMap(\.items).map(\.title) == ["Beta lease"], "search narrows the list")
            store.search = "pack"
            check(store.sections.flatMap(\.items).map(\.title) == ["Alpha trip"], "search also matches the preview")
            store.search = ""
            let alpha = store.conversations.first { $0.sessionId == "a" }!
            check(!alpha.loaded, "history waits until a chat is opened")
            store.select(alpha)
            await settle()
            check(alpha.loaded && alpha.messages.count == 2 && store.selected === alpha, "opening a chat loads its history")
            // Sending into a chat that is still loading its history is ignored, and the history is not overwritten.
            backend.history["b"] = [ChatMessage(role: .user, text: "Lease?"), ChatMessage(role: .assistant, text: "Sixty days.")]
            backend.holdNextHistoryLoad = true
            let beta = store.conversations.first { $0.sessionId == "b" }!
            store.select(beta)
            await backend.waitForHeldHistoryLoad()
            check(beta.loading, "history is loading")
            store.send("too early")
            check(beta.messages.isEmpty && !beta.busy, "sending while loading is ignored")
            backend.releaseHeldHistoryLoad()
            await settle(300)
            check(beta.loaded && beta.messages.count == 2, "history arrives intact")
            backend.loadDelayMs = 0
            // A chat open in the app but not yet in the list survives a refresh.
            let fresh = store.newChat()
            check(store.conversations.first === fresh && fresh.isEmpty, "new chat sits first and is empty")
            check(store.newChat() === fresh, "new chat is reused while still empty")
            await store.refresh()
            check(store.conversations.contains { $0 === fresh }, "an unsent chat survives a refresh")
            // Opening by session id (the notch's hand-off), listed or not.
            await store.open(sessionId: "b")
            check(store.selected?.sessionId == "b", "open selects a listed chat")
            backend.history["zz"] = [ChatMessage(role: .user, text: "From the notch"), ChatMessage(role: .assistant, text: "Hi")]
            await store.open(sessionId: "zz")
            await settle()
            check(store.selected?.sessionId == "zz" && store.selected?.messages.count == 2, "open loads an unlisted chat's history")
            check(store.selected?.title == "From the notch", "a placeholder takes its title from the first prompt")
            await store.open(sessionId: "")
            check(store.selected?.sessionId == "" && store.selected?.isEmpty == true, "an empty session id opens a new chat")
            // Delete.
            store.select(alpha)
            store.delete(alpha)
            await settle()
            check(store.selected != nil && store.selected !== alpha, "deleting the open chat selects another")
            await store.refresh()
            check(!store.conversations.contains { $0.sessionId == "a" }, "a deleted chat is not brought back by a stale list")
            check(backend.deleted == ["a"], "delete tells the service")
            // The service going away is reported, not hidden.
            backend.chats = nil
            await store.refresh()
            check(!store.reachable, "an unreachable service is flagged")
        }

        // Deltas that arrive faster than the UI folds them are not lost.
        do {
            let backend = ScriptedBackend()
            backend.script = { _ in
                [ev("routed", ["model": "m", "role": "general"])]
                + (0..<200).map { ev("delta", ["text": "w\($0) "]) } + [ev("done")]
            }
            let store = ChatStore(backend: backend)
            store.send("count")
            await settle(400)
            let text = store.selected!.messages.last!.text
            check(text == (0..<200).map { "w\($0) " }.joined(), "all 200 deltas arrive in order")
            check(store.selected!.messages.last!.phase == .done, "done follows the last delta")
        }

        // The window opens before the service is up: it retries until the service answers.
        do {
            let backend = ScriptedBackend()
            backend.chats = [ChatSummary(id: "late", title: "Came up late", preview: "", lastUsed: Date(), turnCount: 2)]
            backend.listFailures = 3
            let store = ChatStore(backend: backend)
            await store.refreshWhenReady(maxAttempts: 10, retryDelay: .milliseconds(10))
            check(store.reachable && store.conversations.count == 1, "the list loads once the service answers")
            let never = ScriptedBackend()
            never.listFailures = 100
            let giveUp = ChatStore(backend: never)
            await giveUp.refreshWhenReady(maxAttempts: 3, retryDelay: .milliseconds(10))
            check(!giveUp.reachable, "it stops trying when the service never answers")
            check(never.listFailures == 97, "it makes exactly the attempts it was given")
        }

        // Full access is read from and written to the service.
        do {
            let backend = ScriptedBackend()
            backend.fullAccessValue = true
            let store = ChatStore(backend: backend)
            await store.refresh()
            check(store.fullAccess, "access mode is read")
            store.setFullAccess(false)
            await settle()
            check(!store.fullAccess && !backend.fullAccessValue, "access mode is written")
            check(store.fullAccessKnown, "a confirmed mode is known")
            store.applyFullAccess(nil)
            check(!store.fullAccessKnown && !store.fullAccess, "an unconfirmed mode is marked unknown, keeping the last value")
            store.applyFullAccess(true)
            check(store.fullAccessKnown && store.fullAccess, "a later confirmation restores it")
        }
    }
}
