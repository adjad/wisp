import Foundation

/// The kind of ideator an Ideate tab is set up for. The raw value is the id
/// shared with the service (service/ideate), saved in UserDefaults, and sent
/// with every new project, so never rename one.
///
/// Pure logic (Foundation only), checked by tests/IdeateChecks.swift.
enum IdeateKind: String, CaseIterable, Equatable, Codable {
    /// Idea → short plan → scaffold. The default.
    case general
    /// UI developer: a page or component first, with a live preview.
    case ui
    /// Debugger: paste an error or a log; reproduce first, read before editing.
    case debug
    /// CS student: smaller steps, explains as it goes, tests visible.
    case learn
    /// Scripts, data and automation: one file that runs, with a sample input.
    case script

    static let defaultKind: IdeateKind = .general

    /// The UserDefaults key the chosen kind is saved under.
    static let defaultsKey = "wisp.ideate.kind"

    var title: String {
        switch self {
        case .general: return "General ideator"
        case .ui: return "UI developer"
        case .debug: return "Debugger"
        case .learn: return "CS student"
        case .script: return "Scripts & data"
        }
    }

    /// One line on what Ideate does for this kind, shown on the empty tab.
    var subtitle: String {
        switch self {
        case .general: return "Describe an idea. Ideate plans it, then builds a first small version."
        case .ui: return "Describe a page or a component. Ideate builds it and shows you a preview."
        case .debug: return "Paste an error. Ideate reproduces it first, then makes the smallest fix."
        case .learn: return "Name an exercise. Ideate builds it in small steps, explains each one and keeps tests in view."
        case .script: return "Describe a chore. Ideate writes one script and tries it on a sample first."
        }
    }

    /// Three small starter ideas, shown as chips.
    var starters: [String] {
        switch self {
        case .general: return ["A habit tracker as a single page", "A tip splitter for dinners", "A reading list with notes"]
        case .ui: return ["A pricing card with three tiers", "A settings form with toggles", "A photo grid with a lightbox"]
        case .debug: return ["Paste an error and the file it came from",
                             "A test that fails and I don't know why",
                             "A script that crashes on one input"]
        case .learn: return ["Linked list with tests, explained step by step",
                             "Binary search with a few tests",
                             "A stack-based bracket checker"]
        case .script: return ["Rename photos by date taken", "Merge two CSV files by a shared column", "Count words in every note"]
        }
    }

    /// The prompt field's placeholder.
    var placeholder: String {
        switch self {
        case .general: return "Describe what you want to make…"
        case .ui: return "Describe the page or component…"
        case .debug: return "Paste the error, the log or the stack trace…"
        case .learn: return "What do you want to practice?"
        case .script: return "What should the script do?"
        }
    }

    /// The tab shows a live preview of index.html when there is one.
    var showsPreview: Bool { self == .ui }
    /// Tests are always visible, with their result.
    var showsTests: Bool { self == .learn }
    /// The prompt field is a multi-line paste area.
    var pastesLog: Bool { self == .debug }
    /// A sample run of the result is offered.
    var runsSample: Bool { self == .script }
    /// Each step is explained as it is made.
    var explainsSteps: Bool { self == .learn }
}

/// How an Ideate project is doing. The raw value is the service's status.
enum IdeateProjectStatus: String, CaseIterable, Equatable, Codable {
    case idle, starting, running, waiting, done, failed, cancelled

    /// A run is under way: the tab polls while this is true.
    var isActive: Bool { self == .starting || self == .running || self == .waiting }

    var text: String {
        switch self {
        case .idle: return "Ready"
        case .starting: return "Starting…"
        case .running: return "Working…"
        case .waiting: return "Waiting for you"
        case .done: return "Done"
        case .failed: return "Stopped with an error"
        case .cancelled: return "Stopped"
        }
    }

    /// Reads the service's status, treating anything unknown as idle.
    init(service: String?) {
        self = IdeateProjectStatus(rawValue: (service ?? "").lowercased()) ?? .idle
    }
}

// MARK: - Service events

/// One event from `GET /ideate/projects/{id}`: `{seq, kind, text, tool, path, status, ts}`.
struct IdeateEvent: Equatable {
    enum Kind: String, Equatable {
        case text, thinking, tool, approval, file, done, error
    }

    var seq: Int
    var kind: Kind
    var text: String = ""
    var tool: String = ""
    var path: String = ""
    var status: String = ""
    var ts: Double = 0

    init(seq: Int, kind: Kind, tool: String = "", text: String = "", path: String = "",
         status: String = "", ts: Double = 0) {
        self.seq = seq; self.kind = kind; self.text = text; self.tool = tool
        self.path = path; self.status = status; self.ts = ts
    }

    /// Reads one decoded JSON event; nil when it has no seq or an unknown kind.
    init?(json: [String: Any]) {
        guard let seq = (json["seq"] as? Int) ?? (json["seq"] as? Double).map({ Int($0) }),
              let kind = Kind(rawValue: (json["kind"] as? String ?? "").lowercased()) else { return nil }
        self.init(seq: seq, kind: kind, tool: json["tool"] as? String ?? "",
                  text: json["text"] as? String ?? "", path: json["path"] as? String ?? "",
                  status: json["status"] as? String ?? "", ts: json["ts"] as? Double ?? 0)
    }
}

/// One compact row of the tab's timeline.
struct IdeateRow: Equatable, Identifiable {
    enum Style: Equatable { case text, thinking, edit, run, read, approval, file, done, error }

    /// The seq of the row's first event, so a row keeps its identity as it changes.
    var id: Int
    var style: Style
    var text: String
    /// The same action's latest state: running, passed, failed. Empty for plain rows.
    var state: String = ""
}

struct IdeateTimelineResult: Equatable {
    /// The last rows, oldest first.
    var rows: [IdeateRow]
    /// How many earlier rows are not shown.
    var earlier: Int
}

/// Builds the rows the tab shows from the service's events. Pure.
enum IdeateTimeline {
    /// Rows kept on screen; the rest are counted.
    static let keep = 8
    /// The longest a plain text row gets.
    static let longestLine = 140

    static func reduce(_ events: [IdeateEvent], keep: Int = IdeateTimeline.keep) -> IdeateTimelineResult {
        // Dedupe by seq (the first copy wins), then put them in order.
        var seen = Set<Int>()
        let ordered = events.filter { seen.insert($0.seq).inserted }.sorted { $0.seq < $1.seq }

        var rows: [IdeateRow] = []
        for event in ordered {
            switch event.kind {
            case .thinking:
                if rows.last?.style == .thinking { continue }
                rows.append(IdeateRow(id: event.seq, style: .thinking, text: "Thinking…"))
            case .text:
                let line = oneLine(event.text)
                if line.isEmpty { continue }
                rows.append(IdeateRow(id: event.seq, style: .text, text: line))
            case .tool:
                let row = toolRow(event)
                // The same action reporting again (running, then passed) is one row.
                if let last = rows.last, last.style == row.style, last.text == row.text {
                    rows[rows.count - 1].state = row.state
                } else {
                    rows.append(row)
                }
            case .file:
                let name = baseName(event.path.isEmpty ? event.text : event.path)
                if name.isEmpty { continue }
                // An edit just reported for the same file already says it.
                if let last = rows.last, last.style == .edit, last.text == "Edited \(name)" { continue }
                rows.append(IdeateRow(id: event.seq, style: .file, text: "Saved \(name)"))
            case .approval:
                rows.append(IdeateRow(id: event.seq, style: .approval,
                                      text: "Waiting for your OK" + (event.text.isEmpty ? "" : ": \(oneLine(event.text))")))
            case .done:
                rows.append(IdeateRow(id: event.seq, style: .done, text: "Done"))
            case .error:
                let line = oneLine(event.text)
                rows.append(IdeateRow(id: event.seq, style: .error, text: line.isEmpty ? "Something went wrong" : line))
            }
        }
        let cap = max(keep, 0)
        guard rows.count > cap else { return IdeateTimelineResult(rows: rows, earlier: 0) }
        return IdeateTimelineResult(rows: Array(rows.suffix(cap)), earlier: rows.count - cap)
    }

    private static func toolRow(_ event: IdeateEvent) -> IdeateRow {
        let tool = event.tool.lowercased()
        let state = normalizedState(event.status)
        let name = baseName(event.path)
        if ["edit", "write", "write_file", "edit_file", "patch", "apply_patch", "create", "multiedit"].contains(tool) {
            return IdeateRow(id: event.seq, style: .edit, text: name.isEmpty ? "Edited a file" : "Edited \(name)", state: state)
        }
        if ["read", "read_file", "cat", "view", "ls", "list", "find", "grep", "glob", "find_files"].contains(tool) {
            return IdeateRow(id: event.seq, style: .read, text: name.isEmpty ? "Looked around" : "Read \(name)", state: state)
        }
        if ["bash", "shell", "run_shell", "run", "exec", "command"].contains(tool) {
            let command = oneLine(event.text)
            return IdeateRow(id: event.seq, style: .run, text: command.isEmpty ? "Ran a command" : "Ran `\(command)`", state: state)
        }
        let label = oneLine(event.text)
        return IdeateRow(id: event.seq, style: .run, text: label.isEmpty ? "Used \(event.tool.isEmpty ? "a tool" : event.tool)" : label,
                         state: state)
    }

    /// The row's text with its state, as shown: "Ran `pytest` — passed".
    static func display(_ row: IdeateRow) -> String {
        guard row.style == .run || row.style == .edit || row.style == .read, !row.state.isEmpty,
              row.state != "running" else { return row.text }
        return row.text + " — " + row.state
    }

    static func normalizedState(_ status: String) -> String {
        switch status.lowercased() {
        case "ok", "success", "passed", "pass", "completed", "complete", "done", "0": return "passed"
        case "error", "failed", "fail", "failure": return "failed"
        case "running", "started", "start", "pending", "in_progress": return "running"
        case "denied", "declined", "rejected": return "not allowed"
        default: return ""
        }
    }

    static func baseName(_ path: String) -> String {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.split(separator: "/").last.map(String.init) ?? ""
    }

    static func oneLine(_ text: String) -> String {
        let first = text.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty } ?? ""
        return first.count > longestLine ? String(first.prefix(longestLine - 1)) + "…" : first
    }

    /// The latest test run in the rows, for the CS student's pass/fail line.
    static func lastTestRun(_ rows: [IdeateRow]) -> IdeateRow? {
        rows.last { $0.style == .run && $0.text.lowercased().contains("test") && ($0.state == "passed" || $0.state == "failed") }
    }
}

/// The wording of an approval card.
enum IdeateApprovalText {
    /// "Ideate wants to run `npm install`" or "Ideate wants to write outside the project".
    static func title(tool: String, text: String, path: String) -> String {
        let lowered = tool.lowercased()
        let command = IdeateTimeline.oneLine(text)
        if lowered.contains("write") || lowered.contains("outside") || lowered.contains("edit") {
            let name = IdeateTimeline.baseName(path)
            return name.isEmpty ? "Ideate wants to write outside the project"
                                : "Ideate wants to write outside the project: \(name)"
        }
        return command.isEmpty ? "Ideate wants to run a command" : "Ideate wants to run `\(command)`"
    }
}
