// Compile with the production SearchModel.swift, using inert dependencies.
// No HTTP, Accessibility, clipboard, notification, or source-app access occurs.
import Foundation

@MainActor
final class WispClient {
    struct Event {
        let type: String
        let payload: [String: Any]
        func str(_ key: String) -> String { payload[key] as? String ?? "" }
    }

    struct Request {
        let text: String
        let query: String
        let forceAnswer: Bool
        let forceGlobal: Bool
        let onEvent: @Sendable (Event) -> Void
        var continuation: CheckedContinuation<Void, Never>?
        var cancelled = false
    }

    var requests: [Request] = []
    var prewarms: [String] = []

    func search(text: String, query: String, wantAnswer: Bool,
                forceAnswer: Bool = false, forceGlobal: Bool = false,
                onEvent: @escaping @Sendable (Event) -> Void) async {
        let index = requests.count
        await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                requests.append(Request(text: text, query: query,
                    forceAnswer: forceAnswer, forceGlobal: forceGlobal,
                    onEvent: onEvent, continuation: continuation))
            }
        } onCancel: {
            Task { @MainActor in self.requests[index].cancelled = true }
        }
        // Intentionally allow late callbacks and completion after cancellation.
        // SearchModel must reject them even if a transport cooperates slowly.
    }

    func prewarmIndex(text: String) async { prewarms.append(text) }

    func emit(_ index: Int, _ type: String, _ payload: [String: Any] = [:]) {
        requests[index].onEvent(Event(type: type, payload: payload))
    }

    func finish(_ index: Int) {
        requests[index].continuation?.resume()
        requests[index].continuation = nil
    }

    func finishAll() {
        for index in requests.indices { finish(index) }
    }
}

enum PageSource {
    case document, none
    var label: String { self == .document ? "document" : "nothing to read" }
}

struct PageText {
    var text: String
    var source: PageSource = .document
    var appName = "Fixture"
    var title = "Fixture document"
    static let empty = PageText(text: "", source: .none, appName: "", title: "")
}

@MainActor
enum PageReader {
    static let hasAccessibility = true
    @discardableResult
    static func reveal(_ page: PageText, offset: Int, length: Int) -> Bool {
        preconditionFailure("The state harness must not navigate a source app")
    }
}

@main
@MainActor
enum SearchModelChecks {
    static var checks = 0

    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        if !condition() {
            print("FAIL: \(message)")
            exit(1)
        }
    }

    static func waitUntil(_ message: String, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(2)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 1_000_000)
        }
        check(condition(), message)
    }

    static func settle() async {
        // Let the main-actor callbacks queued by the fake transport run.
        try? await Task.sleep(nanoseconds: 15_000_000)
    }

    static func fixture() -> (SearchModel, WispClient) {
        let client = WispClient()
        let model = SearchModel(client: client)
        model.capture(PageText(text: "The launch code is violet."))
        return (model, client)
    }

    static func start(_ model: SearchModel, _ client: WispClient, query: String = "launch code") async -> Int {
        let index = client.requests.count
        model.query = query
        model.askAnyway()
        await waitUntil("request starts") { client.requests.count == index + 1 }
        return index
    }

    static func main() async {
        await repeatedQueryRejectsOldEvents()
        await explicitGlobalRetiresDebounce()
        await editsResetAndCaptureRetireWork()
        await blankAndCompletionClearProgress()
        await healthyDebounceAndFallback()
        await unreadablePageKeepsExplanation()
        await emptyGlobalFailureExplainsNoAnswer()
        print("SearchModel: \(checks) regression checks passed (synthetic transport only)")
    }

    static func repeatedQueryRejectsOldEvents() async {
        let (model, client) = fixture()
        let old = await start(model, client)
        client.emit(old, "indexing", ["chunks": 100])
        await settle()
        check(!model.indexingStatus.isEmpty, "indexing is visible")
        model.query = "different query"
        model.queryChanged()
        await waitUntil("edit cancels during debounce") { client.requests[old].cancelled }
        check(client.requests.count == 1, "edit cancels before debounce issues replacement")
        check(model.indexingStatus.isEmpty, "edit clears old indexing")
        let current = await start(model, client)
        client.emit(old, "answer", ["text": "STALE ANSWER", "scope": "span"])
        client.emit(old, "lexical", ["results": [["match": "STALE ROW"]]])
        client.emit(old, "error", ["message": "STALE ERROR"])
        client.emit(old, "done")
        client.finish(old)
        await settle()
        check(model.answer.isEmpty && model.rows.isEmpty, "A to B to A rejects old results")
        check(model.errorText.isEmpty, "old errors cannot replace new request")
        check(model.busy, "old completion cannot clear new busy state")
        client.emit(current, "answer", ["text": "Violet [1].", "scope": "span",
            "citations": [["n": 1, "start": 0, "end": 26, "preview": "The launch code is violet."]]])
        await settle()
        check(model.answer == "Violet [1].", "current answer is applied")
        check(model.citations.first?.preview == "The launch code is violet.", "citations survive")
        client.emit(current, "done", ["ms": 42])
        client.finish(current)
        await settle()
        check(!model.busy && model.elapsedMs == 42, "current done finishes progress")
        model.reset()
    }

    static func explicitGlobalRetiresDebounce() async {
        let (model, client) = fixture()
        let old = await start(model, client)
        model.searchWholeDocument()
        await waitUntil("same-text global request starts") { client.requests.count == 2 }
        check(client.requests[1].forceGlobal && client.requests[1].forceAnswer, "global flags preserved")
        client.emit(old, "answer_not_found", ["can_widen": true])
        client.emit(old, "answer", ["text": "STALE SPAN", "scope": "span"])
        client.finish(old)
        await settle()
        check(!model.notFound && model.answer.isEmpty && model.busy, "same-text span request is retired")
        model.queryChanged() // schedule an ordinary request
        model.searchWholeDocument() // explicit action must cancel that debounce
        await waitUntil("explicit global starts") { client.requests.count == 3 }
        try? await Task.sleep(nanoseconds: 250_000_000)
        check(client.requests.count == 3, "pending debounce cannot replace global search")
        check(client.requests[2].forceGlobal, "latest request remains global")
        client.emit(2, "answer", ["text": "GLOBAL ANSWER", "scope": "global"])
        await settle()
        check(model.answer == "GLOBAL ANSWER" && model.answerScope == "global", "global answer wins")
        model.reset()
        client.finishAll()
        await settle()
    }

    static func editsResetAndCaptureRetireWork() async {
        let (model, client) = fixture()
        let old = await start(model, client)
        model.reset()
        check(!model.busy && !model.hasPage && model.query.isEmpty, "reset returns to idle")
        model.capture(PageText(text: "Replacement document has amber."))
        let current = await start(model, client)
        client.emit(old, "answer", ["text": "WRONG DOCUMENT"])
        client.finish(old)
        await settle()
        check(model.answer.isEmpty && model.busy, "old document cannot overwrite reopened same query")
        check(client.requests[current].text == "Replacement document has amber.", "new captured text used")
        model.capture(PageText(text: "Third document")) // capture is independently safe
        client.emit(current, "answer", ["text": "OLD CAPTURE"])
        client.finish(current)
        await settle()
        check(model.answer.isEmpty && !model.busy, "capture retires current results and progress")
        model.queryChanged()
        model.reset()
        try? await Task.sleep(nanoseconds: 250_000_000)
        check(client.requests.count == 2, "reset retires pending debounce")
        client.finishAll()
    }

    static func blankAndCompletionClearProgress() async {
        let (model, client) = fixture()
        let old = await start(model, client)
        client.emit(old, "answering", ["scope": "global"])
        client.emit(old, "upgrading_model", ["model": "fixture-model"])
        await settle()
        check(model.answering && !model.upgradingModel.isEmpty, "active answer progress shown")
        model.query = " \n "
        model.queryChanged()
        check(!model.busy && !model.answering && model.upgradingModel.isEmpty, "blank query clears all progress")
        model.askAnyway()
        model.searchWholeDocument()
        await settle()
        check(client.requests.count == 1, "blank explicit searches do nothing")
        let current = await start(model, client)
        client.emit(current, "indexing", ["chunks": 100])
        client.emit(current, "answering")
        await settle()
        client.finish(current) // EOF without done must also clear progress
        await settle()
        check(!model.busy && !model.answering && model.indexingStatus.isEmpty, "EOF clears all progress")
        model.reset()
        client.finishAll()
        await settle()
    }

    static func healthyDebounceAndFallback() async {
        let (model, client) = fixture()
        model.query = "launch code"
        model.queryChanged()
        await waitUntil("normal debounce starts one request") { client.requests.count == 1 }
        check(!client.requests[0].forceAnswer && !client.requests[0].forceGlobal, "normal search flags preserved")
        client.emit(0, "lexical", ["results": [["match": "violet", "start": 19, "end": 25]]])
        client.emit(0, "semantic_unavailable")
        client.emit(0, "answer_error")
        client.emit(0, "done")
        client.finish(0)
        await settle()
        check(model.rows.first?.match == "violet", "fallback retains lexical match")
        check(model.semanticOff, "fallback is surfaced")
        check(!model.busy && !model.answering, "fallback returns to idle")
        let retry = await start(model, client)
        check(!model.semanticOff && model.errorText.isEmpty, "retry clears obsolete degradation")
        check(retry == 1, "retry issues one request")
        model.reset()
        client.finishAll()
        await settle()
    }

    static func unreadablePageKeepsExplanation() async {
        let (model, client) = fixture()
        model.capture(PageText(text: ""))
        let message = model.errorText
        check(!message.isEmpty, "empty capture explains failure")
        model.query = "launch code"
        model.queryChanged()
        try? await Task.sleep(nanoseconds: 250_000_000)
        check(model.errorText == message, "typing preserves capture failure")
        check(client.requests.isEmpty && !model.busy, "unreadable page starts no request")
        model.askAnyway()
        check(model.errorText == message, "explicit search preserves capture failure")
        model.reset()
        check(model.errorText.isEmpty, "reset clears old capture failure")
    }

    static func emptyGlobalFailureExplainsNoAnswer() async {
        let (model, client) = fixture()
        model.query = "Describe this document"
        model.searchWholeDocument()
        await waitUntil("empty global request starts") { client.requests.count == 1 }
        client.emit(0, "lexical", ["results": []])
        client.emit(0, "semantic_unavailable")
        client.emit(0, "answer_error")
        client.emit(0, "done")
        client.finishAll()
        await settle()
        check(model.rows.isEmpty && !model.errorText.isEmpty, "empty failed answer has explanation")
        check(!model.errorText.contains("matches are still available"), "empty failure does not promise matches")
        check(!model.busy && !model.answering, "empty failed answer finishes")
        model.reset()
    }
}
