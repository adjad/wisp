import Foundation
import AppKit

// The Ideate tab's client model. Ideate builds a small project with a coding agent the
// service runs (service/ideate) on a local model, inside the project's own folder. This
// talks to the service's /ideate routes, polls a run about every second, and holds what
// the tab shows. Nothing here runs a command or writes a file: the service does, behind
// Wisp's approval layer, and asks here (an approval card) before anything risky.

/// What `GET /ideate/status` says: whether Ideate can start, and why not when it cannot.
struct IdeateAvailability: Equatable {
    var available = false
    /// Plain words on why Ideate is unavailable and what to do (model not downloaded, harness missing).
    var reason = ""
    var harness = ""
    var model = ""
    /// The coding model is already loaded. When it is not, starting loads it (about 13 GB).
    var modelLoaded = false
    var projectsRoot = ""
    /// A copy of the model is already on this Mac and can be linked into Wisp's model folder.
    var canLink = false
    /// Linked already; the engine lists it the next time it starts.
    var linkPendingRestart = false

    init() {}

    init(json: [String: Any]) {
        available = json["available"] as? Bool ?? false
        reason = json["reason"] as? String ?? ""
        harness = json["harness"] as? String ?? ""
        model = json["model"] as? String ?? ""
        modelLoaded = json["model_loaded"] as? Bool ?? false
        projectsRoot = json["projects_root"] as? String ?? ""
        canLink = json["can_link"] as? Bool ?? false
        linkPendingRestart = json["link_pending_restart"] as? Bool ?? false
    }
}

/// One row of `GET /ideate/projects`.
struct IdeateProjectSummary: Identifiable, Equatable {
    var id: String
    var name: String
    var kind: IdeateKind
    var status: IdeateProjectStatus
    var updatedAt: Double

    init(id: String, name: String, kind: IdeateKind, status: IdeateProjectStatus, updatedAt: Double = 0) {
        self.id = id; self.name = name; self.kind = kind; self.status = status; self.updatedAt = updatedAt
    }

    init?(json: [String: Any]) {
        guard let id = json["id"] as? String, !id.isEmpty else { return nil }
        self.init(id: id, name: (json["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "Untitled idea",
                  kind: IdeateKind(rawValue: json["kind"] as? String ?? "") ?? .general,
                  status: IdeateProjectStatus(service: json["status"] as? String),
                  updatedAt: json["updated_at"] as? Double ?? 0)
    }
}

/// Something Ideate asked to do and is waiting on.
struct IdeateApproval: Identifiable, Equatable {
    var id: String
    /// "Ideate wants to run `npm install`".
    var title: String

    init(id: String, title: String) { self.id = id; self.title = title }

    init?(json: [String: Any]) {
        guard let id = (json["id"] as? String) ?? (json["approval_id"] as? String), !id.isEmpty else { return nil }
        let tool = (json["tool"] as? String) ?? (json["kind"] as? String) ?? ""
        let text = (json["text"] as? String) ?? (json["command"] as? String) ?? (json["summary"] as? String) ?? ""
        self.init(id: id, title: IdeateApprovalText.title(tool: tool, text: text, path: json["path"] as? String ?? ""))
    }
}

struct IdeateFile: Identifiable, Equatable {
    var path: String
    var id: String { path }
    var name: String { IdeateTimeline.baseName(path) }
}

/// A canned state for the notch QA renderer.
enum IdeateQAState: CaseIterable {
    case empty, running, approval, done, failed, unavailable, modelSwap, needsLink
}

@MainActor
final class IdeateModel: ObservableObject {
    static let shared = IdeateModel()

    @Published private(set) var availability = IdeateAvailability()
    /// The service has answered `GET /ideate/status` at least once.
    @Published private(set) var availabilityKnown = false
    @Published private(set) var projects: [IdeateProjectSummary] = []
    /// The project the tab shows; nil on the empty tab.
    @Published private(set) var current: IdeateProjectSummary?
    @Published private(set) var status: IdeateProjectStatus = .idle
    @Published private(set) var timeline = IdeateTimelineResult(rows: [], earlier: 0)
    @Published private(set) var approvals: [IdeateApproval] = []
    @Published private(set) var files: [IdeateFile] = []
    /// A line to show under the tab's content (the service refused or did not answer).
    @Published private(set) var message: String?
    /// The error that ended the run, one line.
    @Published private(set) var failure: String?
    /// Linking the on-Mac copy of the model is under way.
    @Published private(set) var linking = false
    /// What the last link attempt said, shown inline under the button (success or why not).
    @Published private(set) var linkNote: String?
    @Published private(set) var linkFailed = false

    /// The current ideator kind, saved under `wisp.ideate.kind`.
    @Published var kind: IdeateKind {
        didSet { if persistsKind { UserDefaults.standard.set(kind.rawValue, forKey: IdeateKind.defaultsKey) } }
    }

    private var events: [IdeateEvent] = []
    private var lastSeq = 0
    private var projectPath: String?
    private var pollTask: Task<Void, Never>?
    private var visible = false
    private let networking: Bool
    private let persistsKind: Bool

    init() {
        networking = true
        persistsKind = true
        kind = IdeateKind(rawValue: UserDefaults.standard.string(forKey: IdeateKind.defaultsKey) ?? "") ?? .defaultKind
    }

    private init(fixtureKind: IdeateKind) {
        networking = false
        persistsKind = false
        kind = fixtureKind
    }

    var isActive: Bool { status.isActive }
    /// Starting needs the model loaded first, and the Ask model steps aside for it.
    var swapsModel: Bool { availabilityKnown && availability.available && !availability.modelLoaded && !status.isActive }

    // MARK: Visibility and refresh

    /// The tab appeared: refresh and resume polling if a run is active.
    func appeared() {
        visible = true
        Task { await refresh() }
    }

    /// The tab went away. A run keeps polling, so it is up to date on return; an idle tab stops.
    func disappeared() {
        visible = false
    }

    func refresh() async {
        guard networking else { return }
        if case .ok(let json) = await call("GET", "status") {
            availability = IdeateAvailability(json: json)
            availabilityKnown = true
        } else if !availabilityKnown {
            availability = IdeateAvailability()
            availability.reason = "Wisp isn't answering yet. Try again in a moment."
        }
        if case .ok(let json) = await call("GET", "projects") {
            projects = (json["projects"] as? [[String: Any]] ?? []).compactMap(IdeateProjectSummary.init)
                .sorted { $0.updatedAt > $1.updatedAt }
        }
        if let id = current?.id { await poll(id) }
        ensurePolling()
    }

    /// Opens an earlier project on the tab.
    func open(_ project: IdeateProjectSummary) {
        reset(to: project)
        Task { await poll(project.id); ensurePolling() }
    }

    /// Back to the empty tab for a new idea.
    func newProject() {
        guard !status.isActive else { return }
        reset(to: nil)
    }

    // MARK: Actions

    /// Whether start(prompt:kind:) would begin a run now. Ask routes to Ideate only when
    /// this is true, so a routed prompt is never dropped.
    var canStart: Bool { networking && !status.isActive }

    func start(prompt: String, kind: IdeateKind) {
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !status.isActive else { return }
        self.kind = kind
        message = nil
        guard networking else { return }
        status = .starting
        Task {
            switch await call("POST", "projects", body: ["prompt": text, "kind": kind.rawValue]) {
            case .refused(let why):
                status = current == nil ? .idle : status
                message = why
            case .ok(let json):
                let project = (json["project"] as? [String: Any]) ?? json
                guard let summary = IdeateProjectSummary(json: project) else {
                    status = .idle
                    message = "Wisp couldn't start that. Try again."
                    return
                }
                reset(to: summary)
                status = summary.status == .idle ? .starting : summary.status
                apply(project)
                ensurePolling()
            }
        }
    }

    func send(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let id = current?.id, !text.isEmpty, !status.isActive else { return }
        message = nil
        failure = nil
        guard networking else { return }
        status = .starting
        Task {
            switch await call("POST", "projects/\(id)/message", body: ["text": text]) {
            case .refused(let why): status = .done; message = why
            case .ok(let json): apply((json["project"] as? [String: Any]) ?? json); ensurePolling()
            }
        }
    }

    func cancel() {
        guard let id = current?.id, status.isActive, networking else { return }
        Task {
            switch await call("POST", "projects/\(id)/cancel", body: [:]) {
            case .refused(let why): message = why
            case .ok: await poll(id)
            }
        }
    }

    func answer(approvalId: String, allow: Bool) {
        guard let id = current?.id, networking else { return }
        approvals.removeAll { $0.id == approvalId }
        Task {
            switch await call("POST", "projects/\(id)/approvals/\(approvalId)", body: ["allow": allow]) {
            case .refused(let why): message = why; await poll(id)
            case .ok: await poll(id); ensurePolling()
            }
        }
    }

    /// Clones the copy of the model that is already on this Mac into Wisp's model folder
    /// (service: POST /ideate/model/link), then refreshes the status.
    func linkModel() {
        guard networking, !linking else { return }
        linking = true
        linkNote = nil
        linkFailed = false
        Task {
            switch await call("POST", "model/link", body: [:]) {
            case .refused(let why):
                linkFailed = true
                linkNote = why
            case .ok(let json):
                linkNote = json["message"] as? String
            }
            linking = false
            await refresh()
        }
    }

    /// Opens the project's folder in Finder.
    func openFolder() {
        guard let folder = folderURL else { return }
        NSWorkspace.shared.open(folder)
    }

    /// Opens the project's index.html, when it has one.
    func openPreview() {
        guard let page = previewURL else { return }
        NSWorkspace.shared.open(page)
    }

    var hasPreview: Bool { previewURL != nil }

    // MARK: Folder

    private var folderURL: URL? {
        if let path = projectPath, !path.isEmpty { return URL(fileURLWithPath: path) }
        guard let id = current?.id, !availability.projectsRoot.isEmpty else { return nil }
        return URL(fileURLWithPath: availability.projectsRoot).appendingPathComponent(id)
    }

    private var previewURL: URL? {
        guard let folder = folderURL else { return nil }
        let page = folder.appendingPathComponent("index.html")
        if networking { return FileManager.default.fileExists(atPath: page.path) ? page : nil }
        return files.contains { $0.name == "index.html" } ? page : nil
    }

    // MARK: Polling

    private func ensurePolling() {
        guard networking, status.isActive, pollTask == nil, let id = current?.id else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard let self, !Task.isCancelled else { break }
                guard self.current?.id == id, self.status.isActive else { break }
                await self.poll(id)
            }
            self?.pollTask = nil
        }
    }

    private func poll(_ id: String) async {
        guard networking else { return }
        switch await call("GET", "projects/\(id)", query: [URLQueryItem(name: "after", value: String(lastSeq))]) {
        case .refused(let why):
            if current?.id == id, status.isActive { message = why }
        case .ok(let json):
            guard current?.id == id else { return }
            message = nil
            apply(json)
        }
    }

    /// Folds one project reply (the project plus its new events, files and approvals) into the state.
    private func apply(_ json: [String: Any]) {
        if let state = json["status"] as? String { status = IdeateProjectStatus(service: state) }
        if let path = json["path"] as? String, !path.isEmpty { projectPath = path }
        if let name = json["name"] as? String, !name.isEmpty, var project = current {
            project.name = name
            project.status = status
            current = project
        }
        let fresh = (json["events"] as? [[String: Any]] ?? []).compactMap(IdeateEvent.init)
        if !fresh.isEmpty {
            let known = Set(events.map(\.seq))
            events += fresh.filter { !known.contains($0.seq) }
            lastSeq = max(lastSeq, events.map(\.seq).max() ?? 0)
            timeline = IdeateTimeline.reduce(events)
        }
        failure = status == .failed || status == .cancelled
            ? events.last { $0.kind == .error }.map { IdeateTimeline.oneLine($0.text) } : nil
        if let list = json["files"] as? [Any] {
            files = list.compactMap { item -> IdeateFile? in
                if let path = item as? String { return IdeateFile(path: path) }
                if let dict = item as? [String: Any], let path = dict["path"] as? String { return IdeateFile(path: path) }
                return nil
            }
        }
        if let list = json["approvals"] as? [[String: Any]] {
            approvals = list.compactMap(IdeateApproval.init)
        }
        if !status.isActive { approvals = [] }
    }

    private func reset(to project: IdeateProjectSummary?) {
        pollTask?.cancel()
        pollTask = nil
        current = project
        status = project?.status ?? .idle
        events = []
        lastSeq = 0
        projectPath = nil
        timeline = IdeateTimelineResult(rows: [], earlier: 0)
        approvals = []
        files = []
        message = nil
        failure = nil
    }

    // MARK: Service

    /// The same JSON-over-HTTP the meeting uses on Wisp's own service: no Origin header,
    /// no credentials of its own. `MeetingClient.send` cannot carry a query string (it
    /// escapes the "?"), so the one GET that needs one is built here.
    private func call(_ method: String, _ path: String, query: [URLQueryItem] = [],
                      body: [String: Any]? = nil) async -> MeetingClient.Reply {
        guard var parts = URLComponents(url: WispClient.baseURL.appendingPathComponent("ideate/" + path),
                                        resolvingAgainstBaseURL: false) else {
            return .refused("Wisp couldn't do that. Try again.")
        }
        if !query.isEmpty { parts.queryItems = query }
        guard let url = parts.url else { return .refused("Wisp couldn't do that. Try again.") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if method != "GET" { request.httpBody = try? JSONSerialization.data(withJSONObject: body ?? [:]) }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return .refused("Wisp isn't answering yet. Try again in a moment.")
        }
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if (200..<300).contains(http.statusCode) { return .ok(json) }
        return .refused(json["detail"] as? String ?? "Wisp couldn't do that. Try again.")
    }

    // MARK: QA

    /// A model filled with invented data and no networking, for the notch QA renderer.
    static func qaFixture(kind: IdeateKind, state: IdeateQAState) -> IdeateModel {
        let model = IdeateModel(fixtureKind: kind)
        var availability = IdeateAvailability()
        availability.available = true
        availability.modelLoaded = true
        availability.harness = "pi"
        availability.model = "Inkling 2 Mini"
        availability.projectsRoot = "/Users/example/Library/Application Support/Wisp/ideate"
        model.availabilityKnown = true

        let recent = [
            IdeateProjectSummary(id: "p3", name: "Tip splitter", kind: .general, status: .done, updatedAt: 3),
            IdeateProjectSummary(id: "p2", name: "Pricing card", kind: .ui, status: .done, updatedAt: 2),
            IdeateProjectSummary(id: "p1", name: "Rename photos", kind: .script, status: .failed, updatedAt: 1),
        ]
        let names: [IdeateKind: String] = [.general: "Habit tracker", .ui: "Pricing card", .debug: "Fix checkout total",
                                           .learn: "Linked list", .script: "Rename by date"]
        let project = IdeateProjectSummary(id: "p4", name: names[kind] ?? "Sketch", kind: kind, status: .running, updatedAt: 4)

        func make(_ status: IdeateProjectStatus, _ events: [IdeateEvent], files: [String] = [], approvals: [IdeateApproval] = []) {
            var summary = project
            summary.status = status
            model.current = summary
            model.status = status
            model.events = events
            model.timeline = IdeateTimeline.reduce(events)
            model.files = files.map { IdeateFile(path: $0) }
            model.approvals = approvals
        }
        let working: [IdeateEvent] = [
            IdeateEvent(seq: 1, kind: .thinking),
            IdeateEvent(seq: 2, kind: .text, text: "Plan: one page, one stylesheet, a small script."),
            IdeateEvent(seq: 3, kind: .tool, tool: "edit", path: "index.html", status: "ok"),
            IdeateEvent(seq: 4, kind: .tool, tool: "edit", path: "style.css", status: "ok"),
            IdeateEvent(seq: 5, kind: .tool, tool: "bash", text: kind.showsTests ? "python3 -m pytest" : "node check.js", status: "passed"),
        ]

        switch state {
        case .empty:
            model.projects = recent
        case .modelSwap:
            availability.modelLoaded = false
            model.projects = recent
        case .needsLink:
            availability.available = false
            availability.canLink = true
            availability.reason = "Inkling 2 Mini is on this Mac but not in Wisp's model folder."
        case .unavailable:
            availability.available = false
            availability.reason = "Inkling 2 Mini isn't downloaded yet. Open Settings and download it, then come back."
        case .running:
            make(.running, working + [IdeateEvent(seq: 6, kind: .thinking)])
        case .approval:
            make(.waiting, working + [IdeateEvent(seq: 6, kind: .approval, text: "npm install")],
                 approvals: [IdeateApproval(id: "a1", title: IdeateApprovalText.title(tool: "bash", text: "npm install", path: ""))])
        case .done:
            make(.done, working + [IdeateEvent(seq: 6, kind: .done)],
                 files: ["index.html", "style.css", "app.js", kind.showsTests ? "test_linked_list.py" : "README.md"])
        case .failed:
            make(.failed, working + [IdeateEvent(seq: 6, kind: .error, text: "The model stopped answering. Nothing was lost.")])
            model.failure = "The model stopped answering. Nothing was lost."
        }
        model.availability = availability
        return model
    }
}
