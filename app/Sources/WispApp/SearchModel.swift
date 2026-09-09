import AppKit
import SwiftUI

// State for one Smart Search session: the extracted page, the live query, and
// whatever tiers have landed so far.
//
// The tier results are kept in SEPARATE arrays rather than one merged list, on
// purpose: each tier arrives at a different time, and a merged list would make
// a late semantic result reorder rows out from under the user's arrow keys
// mid-selection. They're fused only at render time.
@MainActor
final class SearchModel: ObservableObject {
    struct Result: Identifiable, Equatable {
        let id = UUID()
        var kind: String        // literal | lexical | semantic
        var prefix: String
        var match: String
        var suffix: String
        var start: Int
        var end: Int
        var line: Int
        var score: Double

        static func == (a: Result, b: Result) -> Bool { a.id == b.id }
    }

    struct Citation: Identifiable, Equatable {
        let id = UUID()
        var n: Int
        var start: Int
        var end: Int
        var preview: String
    }

    @Published var query = ""
    @Published var literal: [Result] = []
    @Published var lexical: [Result] = []
    @Published var semantic: [Result] = []
    @Published var answer = ""
    @Published var citations: [Citation] = []
    @Published var selection = 0

    @Published var busy = false
    @Published var answering = false
    @Published var notFound = false
    @Published var canWiden = false       // not-found, but a whole-doc read is still untried
    @Published var answerScope = ""       // "span" | "global" — labels overview answers
    @Published var expandedCitation: Int? // citation chip whose source is shown inline
    @Published var canNavigate = true     // did the last reveal actually move the source app
    @Published var upgradingModel = ""    // stronger model being loaded for a doc-level answer
    @Published var semanticOff = false
    @Published var status = ""            // "reading window · Safari"
    @Published var indexingStatus = ""    // "indexing 812 passages…" — long docs only
    @Published var errorText = ""
    @Published var elapsedMs = 0
    @Published var needsAccessibility = false

    // Fired whenever laid-out content height changes, so the host OverlayPanel
    // can re-fit its native window — same convention as OverlayModel.onResize.
    var onResize: () -> Void = {}

    private var page = PageText.empty
    private let client: WispClient
    private var searchTask: Task<Void, Never>?
    private var debounce: Task<Void, Never>?
    private var requestID = UUID()

    init(client: WispClient) {
        self.client = client
    }

    /// Merged, de-duplicated rows in the order the user navigates them.
    /// Literal hits pin to the top always — that's the ⌘F floor: if a literal
    /// match exists, it is never buried under a semantic guess.
    var rows: [Result] {
        var seen = Set<Int>()
        var out: [Result] = []
        for r in literal + lexical + semantic {
            // Spans that start within a few chars of one already shown are the
            // same hit found by two tiers; keep the first (strongest) one.
            let bucket = r.start / 24
            if seen.contains(bucket) { continue }
            seen.insert(bucket)
            out.append(r)
        }
        return out
    }

    var pageSourceLabel: String { page.source.label }
    var hasPage: Bool { !page.text.isEmpty }

    /// Called when the panel opens: grab the focused window's text BEFORE Wisp
    /// takes focus, so "frontmost app" still means the user's document.
    func capture(_ captured: PageText) {
        cancelSearch()
        clearResults()
        page = captured
        needsAccessibility = !PageReader.hasAccessibility && captured.source != .document
        status = captured.appName.isEmpty
            ? captured.source.label
            : "\(captured.source.label) · \(captured.appName)"
        if captured.text.isEmpty {
            errorText = needsAccessibility
                ? "Wisp needs Accessibility permission to read this window."
                : "Couldn't read any text from \(captured.appName)."
        } else {
            errorText = ""
            // Fire-and-forget: for a long document (a novel-length PDF) the
            // embedding pass over hundreds of chunks takes real time. Kicking
            // it off now, against the moment the panel opens, spends the
            // seconds the user takes to read the field and type their
            // question — by the time they finish, the doc is often already
            // indexed and the first real search hits a warm cache instead of
            // paying the embed cost inline.
            let text = captured.text
            Task { [client] in await client.prewarmIndex(text: text) }
        }
    }

    func reset() {
        cancelSearch()
        query = ""; clearResults()
        page = .empty; status = ""; errorText = ""
        needsAccessibility = false
    }

    private func cancelSearch() {
        // Query text is not an identity: A → B → A and whole-document retries
        // can receive late events from a different request with the same text.
        requestID = UUID()
        searchTask?.cancel(); searchTask = nil
        debounce?.cancel(); debounce = nil
        finishProgress()
    }

    private func finishProgress() {
        busy = false; answering = false
        indexingStatus = ""; upgradingModel = ""
    }

    private func clearResults() {
        literal = []; lexical = []; semantic = []
        answer = ""; citations = []; selection = 0
        notFound = false; canWiden = false; answerScope = ""
        expandedCitation = nil; canNavigate = true; upgradingModel = ""
        semanticOff = false; finishProgress()
        elapsedMs = 0
        // A failed capture still needs its explanation while the user types.
        // Only request-level errors can be cleared by retrying a readable page.
        if !page.text.isEmpty { errorText = "" }
    }

    /// Debounced search. Fires the cheap tiers on every keystroke and lets the
    /// backend decide whether the query is question-shaped enough to synthesize.
    func queryChanged() {
        // Stop obsolete work immediately, including during the debounce gap.
        cancelSearch()
        clearResults()
        let q = query
        let id = requestID
        guard !q.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        debounce = Task { [weak self] in
            // Long enough that a fast typist doesn't fire a request per letter,
            // short enough to feel immediate.
            try? await Task.sleep(nanoseconds: 180_000_000)
            guard !Task.isCancelled, self?.requestID == id else { return }
            self?.run(q)
        }
    }

    /// Force the answer tier even for a non-question query (⌘↵).
    func askAnyway() {
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        run(query, forceAnswer: true)
    }

    /// Escalate a span lookup that found no direct answer into a
    /// whole-document read (front matter + a spread across the text) — the
    /// difference between "that exact sentence isn't here" and "this document
    /// can't tell you". Offered on the not-found card.
    func searchWholeDocument() {
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        run(query, forceAnswer: true, forceGlobal: true)
    }

    private func run(_ q: String, forceAnswer: Bool = false,
                     forceGlobal: Bool = false) {
        // Explicit execution also retires a pending ordinary-search debounce.
        cancelSearch()
        guard !page.text.isEmpty else { return }
        clearResults()
        busy = true
        let text = page.text
        let id = requestID
        searchTask = Task { [weak self] in
            guard let self, !Task.isCancelled, self.requestID == id else { return }
            await client.search(text: text, query: q, wantAnswer: true,
                                forceAnswer: forceAnswer,
                                forceGlobal: forceGlobal) { [weak self] ev in
                Task { @MainActor in self?.handle(ev, requestID: id, forQuery: q) }
            }
            guard self.requestID == id else { return }
            self.finishProgress()
        }
    }

    private func handle(_ ev: WispClient.Event, requestID id: UUID, forQuery q: String) {
        // A late event from a superseded query must not overwrite fresh results.
        guard id == requestID, q == query else { return }
        switch ev.type {
        case "literal":
            literal = Self.parseResults(ev.payload["results"], kind: "literal")
        case "lexical":
            lexical = Self.parseResults(ev.payload["results"], kind: "lexical")
        case "indexing":
            let n = ev.payload["chunks"] as? Int ?? 0
            indexingStatus = "indexing \(n) passages…"
        case "semantic":
            indexingStatus = ""
            semantic = Self.parseResults(ev.payload["results"], kind: "semantic")
        case "semantic_unavailable":
            semanticOff = true; indexingStatus = ""
        case "answering":
            answering = true
            answerScope = ev.str("scope")
        case "upgrading_model":
            // A stronger model is being loaded for a document-level question;
            // say so, because this is the one path that can take ~20s.
            upgradingModel = ev.str("model")
        case "answer":
            answering = false; upgradingModel = ""
            answerScope = ev.str("scope")
            answer = ev.str("text")
            citations = (ev.payload["citations"] as? [[String: Any]] ?? []).map {
                Citation(n: $0["n"] as? Int ?? 0,
                         start: $0["start"] as? Int ?? 0,
                         end: $0["end"] as? Int ?? 0,
                         preview: $0["preview"] as? String ?? "")
            }
            elapsedMs = ev.payload["ms"] as? Int ?? 0
        case "answer_not_found":
            answering = false; notFound = true; upgradingModel = ""
            // Only offer the widen affordance when a whole-document read isn't
            // what just ran — otherwise the card promises an escalation that
            // has already been tried.
            canWiden = ev.payload["can_widen"] as? Bool ?? false
        case "answer_error":
            answering = false; upgradingModel = ""
            errorText = "Couldn't generate an answer. Try another question."
        case "done":
            finishProgress()
            elapsedMs = ev.payload["ms"] as? Int ?? elapsedMs
        case "error":
            finishProgress()
            errorText = ev.str("message")
        default:
            break
        }
    }

    private static func parseResults(_ raw: Any?, kind: String) -> [Result] {
        (raw as? [[String: Any]] ?? []).map {
            Result(kind: $0["kind"] as? String ?? kind,
                   prefix: $0["prefix"] as? String ?? "",
                   match: $0["match"] as? String ?? "",
                   suffix: $0["suffix"] as? String ?? "",
                   start: $0["start"] as? Int ?? 0,
                   end: $0["end"] as? Int ?? 0,
                   line: $0["line"] as? Int ?? 0,
                   score: $0["score"] as? Double ?? 0)
        }
    }

    // MARK: - Navigation

    func move(_ delta: Int) {
        let all = rows
        guard !all.isEmpty else { return }
        selection = max(0, min(all.count - 1, selection + delta))
        revealSelected()
    }

    func revealSelected() {
        let all = rows
        guard selection < all.count else { return }
        let r = all[selection]
        PageReader.reveal(page, offset: r.start, length: max(1, r.end - r.start))
    }

    /// Clicking a citation chip. Tries to scroll the source app to the passage,
    /// and ALWAYS shows the passage inline in the panel.
    ///
    /// The inline expansion isn't a consolation prize — for the common case of
    /// a PDF in Safari (read-only AXStaticText, which accepts the AX selection
    /// call and ignores it) navigation cannot work at all, and a chip that
    /// silently does nothing is worse than no chip. Showing the source text
    /// where the user is already looking makes the citation verifiable, which
    /// is the actual point of having one.
    func reveal(citation: Citation) {
        let moved = PageReader.reveal(page, offset: citation.start,
                                      length: max(1, citation.end - citation.start))
        // Toggle: clicking the open chip again collapses it.
        expandedCitation = (expandedCitation == citation.n) ? nil : citation.n
        canNavigate = moved
    }

    /// Copy just the cited passage — the reliable way to find it in an app we
    /// can't drive: paste into that app's own ⌘F.
    func copyCitation(_ c: Citation) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(c.preview, forType: .string)
    }

    /// Copy the answer together with the passage it rests on — an answer
    /// without its source is exactly what we're trying not to ship.
    func copyAnswer() {
        guard !answer.isEmpty else { return }
        var out = answer
        if let first = citations.first {
            out += "\n\nSource: \"\(first.preview)\""
        }
        if !page.title.isEmpty { out += "\n— \(page.title)" }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(out, forType: .string)
    }
}
