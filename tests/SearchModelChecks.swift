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

    /// The Accessibility permission's name, which every screen takes from
    /// PrivacyNames: renamed on macOS 27, its old name alongside where there is room.
    static func privacyNamesFollowTheOS() {
        func version(_ major: Int, _ minor: Int) -> OperatingSystemVersion {
            OperatingSystemVersion(majorVersion: major, minorVersion: minor, patchVersion: 0)
        }
        check(PrivacyNames.accessibility(for: version(26, 0)) == "Accessibility", "before macOS 27 the pane is Accessibility")
        check(PrivacyNames.accessibility(for: version(27, 0)) == "Device Control and Data Access",
              "macOS 27.0 calls it Device Control and Data Access")
        check(PrivacyNames.accessibility(for: version(27, 3)) == "Accessibility", "macOS 27.3 calls it Accessibility again")
        check(PrivacyNames.accessibilityWithFormerName(for: version(27, 1))
                == "Device Control and Data Access (called Accessibility before macOS 27)",
              "with room, the new name says the old one")
        check(PrivacyNames.accessibilityWithFormerName(for: version(26, 0)) == "Accessibility", "before macOS 27, no note")
        check(PrivacyNames.accessibilityPath.hasPrefix("System Settings › Privacy & Security › ")
                && PrivacyNames.accessibilityPath.hasSuffix(PrivacyNames.accessibility),
              "the path names this Mac's pane")
    }

    static func main() async {
        privacyNamesFollowTheOS()
        await repeatedQueryRejectsOldEvents()
        await explicitGlobalRetiresDebounce()
        await editsResetAndCaptureRetireWork()
        await blankAndCompletionClearProgress()
        await healthyDebounceAndFallback()
        await partialFailuresKeepCompletedContent()
        await explicitRetryCanRecover()
        await unreadablePageKeepsExplanation()
        await emptyGlobalFailureExplainsNoAnswer()
        await everyLiteralMatchKeepsItsRow()
        offsetsCountWhatTheBackendCounts()
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

    static func partialFailuresKeepCompletedContent() async {
        let (model, client) = fixture()
        let request = await start(model, client)
        client.emit(request, "lexical", ["results": [["match": "violet", "start": 19, "end": 25]]])
        client.emit(request, "answer_error")
        await settle()
        check(model.rows.first?.match == "violet", "answer failure retains successful search rows")
        check(model.errorText == "Couldn't generate an answer.", "answer failure remains available beside rows")

        client.emit(request, "answer", ["text": "The code is violet.", "scope": "span"])
        await settle()
        check(model.answer == "The code is violet.", "successful answer is retained")
        check(model.errorText.isEmpty, "successful answer clears an obsolete warning")

        client.emit(request, "error", ["message": "The connection was interrupted."])
        await settle()
        check(model.answer == "The code is violet.", "transport failure retains a completed answer")
        check(model.errorText == "The connection was interrupted.", "transport failure remains visible with the answer")
        client.finish(request)
        await settle()
        model.reset()
    }

    static func explicitRetryCanRecover() async {
        let (model, client) = fixture()
        let failed = await start(model, client)
        client.emit(failed, "lexical", ["results": [["match": "violet", "start": 19, "end": 25]]])
        client.emit(failed, "error", ["message": "The connection was interrupted."])
        await settle()
        check(model.rows.first?.match == "violet" && !model.errorText.isEmpty,
              "failed request keeps partial results and its warning")

        model.askAnyway() // Explicit ⌘↵ retry repeats the answer request for the same query.
        await waitUntil("explicit retry starts") { client.requests.count == 2 }
        check(client.requests[1].query == client.requests[0].query
              && client.requests[1].forceAnswer,
              "explicit retry uses the existing same-query answer semantics")
        check(model.errorText.isEmpty, "new retry clears the previous warning")
        client.emit(1, "answer", ["text": "The code is violet.", "scope": "span"])
        await settle()
        check(model.answer == "The code is violet." && model.errorText.isEmpty,
              "successful retry replaces failure state with its answer")
        client.emit(failed, "error", ["message": "STALE FAILURE"])
        await settle()
        check(model.answer == "The code is violet." && model.errorText.isEmpty,
              "late failure from retried request cannot replace success")
        client.finishAll()
        await settle()
        model.reset()
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

    static func hit(_ match: String, _ start: Int, _ end: Int) -> [String: Any] {
        ["match": match, "start": start, "end": end]
    }

    // Reads rows only: moving the selection would navigate the source app.
    static func everyLiteralMatchKeepsItsRow() async {
        let (model, client) = fixture()
        let request = await start(model, client, query: "amber")
        // Three real matches a few characters apart, as in a list of names.
        let literal: [[String: Any]] = [hit("amber", 100, 105), hit("amber", 118, 123),
                                        hit("amber", 123, 128)]
        client.emit(request, "literal", ["results": literal])
        await settle()
        check(model.rows.map(\.start) == [100, 118, 123], "close literal matches each keep a row")

        // A passage around matches already shown is the same hit; one elsewhere is new.
        let lexical: [[String: Any]] = [hit("amber stone", 96, 140), hit("resin", 400, 460)]
        client.emit(request, "lexical", ["results": lexical])
        await settle()
        check(model.rows.map(\.start) == [100, 118, 123, 400],
              "a passage overlapping a literal match is merged into it")

        let semantic: [[String: Any]] = [hit("fossil resin", 402, 470), hit("tree sap", 430, 500),
                                         hit("jewellery", 900, 960), hit("beads", 960, 1000)]
        client.emit(request, "semantic", ["results": semantic])
        await settle()
        check(model.rows.map(\.start) == [100, 118, 123, 400, 900, 960],
              "passages overlapping one already shown are merged, the rest are kept")
        check(model.rows.map(\.kind) == ["literal", "literal", "literal", "lexical", "semantic", "semantic"],
              "literal rows stay first, then lexical, then semantic")

        // Rows follow the tiers as they stand: nothing stays hidden by a row that left.
        client.emit(request, "lexical", ["results": [[String: Any]]()])
        await settle()
        check(model.rows.map(\.start) == [100, 118, 123, 402, 900, 960],
              "with the lexical row gone the passage it hid shows, after the literal rows")
        client.finish(request)
        await settle()
        model.reset()
    }

    // A result's offset counts Unicode scalars, as the backend's Python strings
    // do. Selecting it in the source app counts UTF-16 units. The numbers here
    // are Python's own: len() and len(s.encode("utf-16-le")) // 2.
    static func offsetsCountWhatTheBackendCounts() {
        let first = "Guest list"
        // A family, " from ", a flag, " at the cafe" with a combining accent, " by noon".
        let second = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467} from \u{1F1E8}\u{1F1E6} at the cafe\u{301} by noon"
        check(second.count < SearchOffset.length(second), "Swift counts fewer characters than Python does")
        check(SearchOffset.length(first) == 10 && SearchOffset.length(second) == 34,
              "lengths are Python's len()")

        // The page as the reader joins it: each element, then a newline.
        var text = ""
        var ranges: [Range<Int>] = []
        for piece in [first, second] {
            let start = SearchOffset.length(text)
            ranges.append(start..<(start + SearchOffset.length(piece)))
            text += piece + "\n"
        }
        check(ranges == [0..<10, 11..<45] && SearchOffset.length(text) == 46,
              "element ranges are Python offsets into the joined text")

        // Python finds "noon" at 41: inside the second element, 30 scalars in.
        check(ranges[1].contains(41) && !ranges[0].contains(41), "the result lands on its own element")
        let noon = SearchOffset.utf16Range(in: second, offset: 41 - ranges[1].lowerBound, length: 4)
        check(noon.location == 35 && noon.length == 4, "the selection starts 35 UTF-16 units in")
        check((second as NSString).substring(with: NSRange(location: noon.location, length: noon.length)) == "noon",
              "and selects the match itself")
        let flag = SearchOffset.utf16Range(in: second, offset: 11, length: 2)
        check(flag.location == 14 && flag.length == 4, "a flag is two scalars and four UTF-16 units")

        let plain = SearchOffset.utf16Range(in: first, offset: 6, length: 4)
        check(plain.location == 6 && plain.length == 4, "plain text is unchanged")
        let tail = SearchOffset.utf16Range(in: second, offset: 30, length: 400)
        check(tail.location == 35 && tail.length == 4, "a range is cut at the end of the text")
        let past = SearchOffset.utf16Range(in: second, offset: 400, length: 4)
        check(past.location == 39 && past.length == 0, "an offset past the end selects nothing")
        let negative = SearchOffset.utf16Range(in: first, offset: -3, length: -1)
        check(negative.location == 0 && negative.length == 0, "bad numbers select nothing")
    }
}
