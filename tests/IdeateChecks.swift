// Contract for Ideate's pure layer (app/Sources/WispApp/IdeateKind.swift): the ideator
// kinds, the project status and the timeline reducer. Pure logic: never launches Wisp
// or contacts its service. Compile with IdeateKind.swift only.
import Foundation

@main
struct IdeateChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }
        typealias E = IdeateEvent

        // Kinds: the five shared raw values, in order, unique, with their data.
        check(IdeateKind.allCases.map(\.rawValue) == ["general", "ui", "debug", "learn", "script"], "five kinds, raw values keep")
        check(Set(IdeateKind.allCases.map(\.rawValue)).count == 5, "raw values are unique")
        check(IdeateKind.defaultKind == .general, "the default kind is general")
        check(IdeateKind.defaultsKey == "wisp.ideate.kind", "the saved kind's key")
        for kind in IdeateKind.allCases {
            check(kind.starters.count == 3 && Set(kind.starters).count == 3 && !kind.starters.contains(""), "\(kind) has three starters")
            check(!kind.subtitle.isEmpty && !kind.placeholder.isEmpty && !kind.title.isEmpty, "\(kind) has its words")
        }
        check(IdeateKind.ui.showsPreview && !IdeateKind.general.showsPreview, "only UI shows a preview")
        check(IdeateKind.learn.showsTests && IdeateKind.learn.explainsSteps && !IdeateKind.ui.explainsSteps, "learn shows tests and explains")
        check(IdeateKind.debug.pastesLog && !IdeateKind.learn.pastesLog, "only debug pastes a log")
        check(IdeateKind.script.runsSample && !IdeateKind.debug.runsSample, "only script runs a sample")
        check(IdeateKind.ui.starters[0] == "A pricing card with three tiers", "the UI starter")
        check(IdeateKind(rawValue: "nope") == nil, "an unknown kind is nil")

        // Status.
        check(IdeateProjectStatus.allCases.map(\.rawValue) == ["idle", "starting", "running", "waiting", "done", "failed", "cancelled"],
              "status raw values")
        check(IdeateProjectStatus.running.isActive && IdeateProjectStatus.waiting.isActive && IdeateProjectStatus.starting.isActive,
              "starting, running and waiting are active")
        check(!IdeateProjectStatus.done.isActive && !IdeateProjectStatus.idle.isActive && !IdeateProjectStatus.failed.isActive,
              "idle, done and failed are not")
        check(IdeateProjectStatus.waiting.text == "Waiting for you" && IdeateProjectStatus.running.text == "Working…", "status words")
        check(IdeateProjectStatus(service: "RUNNING") == .running && IdeateProjectStatus(service: "wat") == .idle
              && IdeateProjectStatus(service: nil) == .idle, "service status parsing")
        for status in IdeateProjectStatus.allCases { check(!status.text.isEmpty, "\(status) has text") }

        // Timeline: empty.
        check(IdeateTimeline.reduce([]) == IdeateTimelineResult(rows: [], earlier: 0), "no events, no rows")

        // Collapse consecutive thinking.
        var result = IdeateTimeline.reduce([E(seq: 1, kind: .thinking, text: "a"), E(seq: 2, kind: .thinking, text: "b"),
                                            E(seq: 3, kind: .thinking), E(seq: 4, kind: .text, text: "Plan: three files.")])
        check(result.rows.map(\.text) == ["Thinking…", "Plan: three files."], "consecutive thinking collapses")
        check(result.rows[0].id == 1 && result.rows[0].style == .thinking, "the thinking row keeps its first seq")
        result = IdeateTimeline.reduce([E(seq: 1, kind: .thinking), E(seq: 2, kind: .text, text: "x"), E(seq: 3, kind: .thinking)])
        check(result.rows.map(\.text) == ["Thinking…", "x", "Thinking…"], "thinking apart stays two rows")

        // Group tool calls.
        result = IdeateTimeline.reduce([
            E(seq: 1, kind: .tool, tool: "edit", path: "/p/site/index.html", status: "running"),
            E(seq: 2, kind: .tool, tool: "edit", path: "/p/site/index.html", status: "ok"),
            E(seq: 3, kind: .file, path: "/p/site/index.html"),
            E(seq: 4, kind: .file, path: "/p/site/style.css"),
            E(seq: 5, kind: .tool, tool: "bash", text: "python3 -m pytest", status: "running"),
            E(seq: 6, kind: .tool, tool: "bash", text: "python3 -m pytest", status: "passed"),
            E(seq: 7, kind: .tool, tool: "bash", text: "npm install", status: "failed"),
            E(seq: 8, kind: .tool, tool: "read", path: "notes.md"),
        ])
        check(result.rows.map(\.text) == ["Edited index.html", "Saved style.css", "Ran `python3 -m pytest`", "Ran `npm install`", "Read notes.md"],
              "tool calls group: \(result.rows.map(\.text))")
        check(result.rows[0].state == "passed" && result.rows[2].state == "passed" && result.rows[3].state == "failed", "latest state wins")
        check(IdeateTimeline.display(result.rows[2]) == "Ran `python3 -m pytest` — passed", "a run shows its result")
        check(IdeateTimeline.display(result.rows[0]) == "Edited index.html — passed", "an edit shows its state")
        check(IdeateTimeline.display(result.rows[1]) == "Saved style.css", "a plain row shows only its text")
        check(IdeateTimeline.lastTestRun(result.rows)?.text == "Ran `python3 -m pytest`", "the last test run")
        check(IdeateTimeline.lastTestRun([IdeateRow(id: 1, style: .run, text: "Ran `ls`", state: "passed")]) == nil, "no test run")
        check(IdeateTimeline.display(IdeateRow(id: 1, style: .run, text: "Ran `ls`", state: "running")) == "Ran `ls`", "a running action shows no state")
        result = IdeateTimeline.reduce([E(seq: 1, kind: .tool, tool: "write_file", path: "a.txt"), E(seq: 2, kind: .tool, tool: "mystery", text: "Did a thing")])
        check(result.rows.map(\.text) == ["Edited a.txt", "Did a thing"], "write tools are edits and unknown tools use their text")

        // Dedupe by seq and order.
        result = IdeateTimeline.reduce([E(seq: 2, kind: .text, text: "second"), E(seq: 1, kind: .text, text: "first"),
                                        E(seq: 2, kind: .text, text: "second"), E(seq: 1, kind: .text, text: "first")])
        check(result.rows.map(\.text) == ["first", "second"], "dedupe by seq and sort")

        // Approval, done, error, blank text, long text.
        result = IdeateTimeline.reduce([E(seq: 1, kind: .approval, text: "npm install"), E(seq: 2, kind: .done),
                                        E(seq: 3, kind: .error, text: "The model stopped answering."), E(seq: 4, kind: .error),
                                        E(seq: 5, kind: .text, text: "   \n  ")])
        check(result.rows.map(\.text) == ["Waiting for your OK: npm install", "Done", "The model stopped answering.", "Something went wrong"],
              "approval, done and error rows; blank text is skipped")
        result = IdeateTimeline.reduce([E(seq: 1, kind: .text, text: "First line\nsecond line"), E(seq: 2, kind: .text, text: String(repeating: "x", count: 400))])
        check(result.rows[0].text == "First line" && result.rows[1].text.count == IdeateTimeline.longestLine
              && result.rows[1].text.hasSuffix("…"), "text rows are one short line")

        // Cap at the last eight with a count of the earlier ones.
        let many = (1...20).map { E(seq: $0, kind: .text, text: "step \($0)") }
        result = IdeateTimeline.reduce(many)
        check(result.rows.count == 8 && result.earlier == 12 && result.rows.first?.text == "step 13" && result.rows.last?.text == "step 20",
              "keeps the last eight")
        result = IdeateTimeline.reduce(Array(many.prefix(8)))
        check(result.rows.count == 8 && result.earlier == 0, "exactly eight shows all")
        check(IdeateTimeline.keep == 8, "N is 8")

        // Event decoding.
        let json: [String: Any] = ["seq": 4.0, "kind": "tool", "text": "ls", "tool": "bash", "path": "", "status": "ok", "ts": 1.5]
        check(E(json: json) == E(seq: 4, kind: .tool, tool: "bash", text: "ls", path: "", status: "ok", ts: 1.5), "decodes an event")
        check(E(json: ["kind": "text"]) == nil && E(json: ["seq": 1, "kind": "banana"]) == nil, "a bad event is nil")

        // Approval wording.
        check(IdeateApprovalText.title(tool: "bash", text: "npm install", path: "") == "Ideate wants to run `npm install`", "run approval")
        check(IdeateApprovalText.title(tool: "write_outside", text: "", path: "/Users/x/notes.txt")
              == "Ideate wants to write outside the project: notes.txt", "write approval")
        check(IdeateApprovalText.title(tool: "write", text: "", path: "") == "Ideate wants to write outside the project", "write approval without a path")

        print("IdeateChecks: \(count) checks passed")
    }
}
