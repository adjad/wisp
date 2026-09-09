// Real app models and wire parsing, synthetic store output and inert actions.
import AppKit
import Foundation
import SwiftUI

@MainActor
final class FixtureResearchClient: ResearchClient {
    var rows: [String: [String: Any]]
    var jobs: [ResearchLibraryItem]
    var actions: [(String, String)] = []
    var snapshotCalls: [String] = []
    var blockedSnapshots: Set<String> = []
    var waitingSnapshots: [String: [CheckedContinuation<WispClient.ResearchSnapshot?, Never>]] = [:]
    var blockedPlan = false
    var waitingPlan: CheckedContinuation<WispClient.ResearchSnapshot?, Never>?
    var blockedSave = false
    var waitingSave: CheckedContinuation<WispClient.ResearchSnapshot?, Never>?
    var listFailure = false
    var blockList = false
    var waitingLists: [CheckedContinuation<[ResearchLibraryItem], Error>] = []
    var actionResult = true
    var blockedAction = ""
    var waitingAction: CheckedContinuation<Bool, Never>?
    var deleteResult = true
    struct Stream {
        let id: String
        let after: Int
        let callback: @Sendable (WispClient.Event) -> Void
        var continuation: CheckedContinuation<Void, Never>?
    }
    var streams: [Stream] = []

    init(_ data: [String: Any]) {
        let fixtures = data["snapshots"] as! [String: [String: Any]]
        rows = Dictionary(uniqueKeysWithValues: fixtures.values.map { ($0["id"] as! String, $0) })
        jobs = (data["jobs"] as! [[String: Any]]).compactMap(ResearchLibraryItem.parse)
    }

    func researchJobs() async throws -> [ResearchLibraryItem] {
        if blockList { return try await withCheckedThrowingContinuation { waitingLists.append($0) } }
        if listFailure { throw URLError(.cannotConnectToHost) }
        return jobs
    }

    func researchSnapshot(jobId: String) async -> WispClient.ResearchSnapshot? {
        snapshotCalls.append(jobId)
        if blockedSnapshots.contains(jobId) {
            return await withCheckedContinuation { waitingSnapshots[jobId, default: []].append($0) }
        }
        return rows[jobId].flatMap(WispClient.ResearchSnapshot.parse)
    }

    func createResearchPlan(prompt: String, depth: String) async -> WispClient.ResearchSnapshot? {
        actions.append((prompt, "create"))
        if blockedPlan { return await withCheckedContinuation { waitingPlan = $0 } }
        return rows.values.first(where: { $0["state"] as? String == "awaiting_approval" }).flatMap(WispClient.ResearchSnapshot.parse)
    }

    func updateResearchPlan(jobId: String, plan: WispClient.ResearchPlanPayload) async -> WispClient.ResearchSnapshot? {
        actions.append((jobId, "save"))
        if blockedSave { return await withCheckedContinuation { waitingSave = $0 } }
        return rows[jobId].flatMap(WispClient.ResearchSnapshot.parse)
    }

    func researchAction(jobId: String, action: String, body: [String: Any]) async -> Bool {
        actions.append((jobId, action))
        if action == blockedAction { return await withCheckedContinuation { waitingAction = $0 } }
        if actionResult {
            if action == "start" { rows[jobId]?["state"] = "running" }
            if action == "pause" { rows[jobId]?["state"] = "paused" }
        }
        return actionResult
    }

    func streamResearch(jobId: String, after: Int, onEvent: @escaping @Sendable (WispClient.Event) -> Void) async {
        await withCheckedContinuation {
            streams.append(Stream(id: jobId, after: after, callback: onEvent, continuation: $0))
        }
    }

    func emit(_ index: Int, _ type: String, _ payload: [String: Any] = [:]) {
        streams[index].callback(WispClient.Event(type: type, payload: payload))
    }

    func finishStream(_ index: Int) {
        streams[index].continuation?.resume(); streams[index].continuation = nil
    }

    func finishStreams() { for index in streams.indices { finishStream(index) } }
    func pinResearch(jobId: String, pinned: Bool) async -> Bool { actions.append((jobId, "pin")); return actionResult }
    func deleteResearch(jobId: String) async -> Bool { actions.append((jobId, "delete")); return deleteResult }
    func updateResearchDomains(jobId: String, allowed: [String]?, blocked: [String]?) async -> WispClient.ResearchSnapshot? {
        actions.append((jobId, "domains")); return rows[jobId].flatMap(WispClient.ResearchSnapshot.parse)
    }
    func exportResearch(jobId: String, title: String) async -> URL? {
        actions.append((jobId, "export")); return nil // Never writes files or opens Finder.
    }
}

/// Intercept the actual list client's URLSession so even wire checks cannot
/// contact the user's backend. A missing fixture is an explicit HTTP failure.
final class LibraryURLProtocol: URLProtocol {
    static let lock = NSLock()
    static var body = Data()
    static var status = 200
    static func configure(_ data: Data, status: Int = 200) {
        lock.lock(); body = data; self.status = status; lock.unlock()
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); let body = Self.body, status = Self.status; Self.lock.unlock()
        guard let url = request.url, url.path == "/research/jobs", request.httpMethod == "GET" else {
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL)); return
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: url, statusCode: status,
            httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
@MainActor
enum ResearchLibraryChecks {
    static var checks = 0
    static var data: [String: Any] = [:]
    static var snapshots: [String: [String: Any]] { data["snapshots"] as! [String: [String: Any]] }
    static func id(_ state: String) -> String { snapshots[state]!["id"] as! String }
    static func check(_ value: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        if !value() { print("FAIL: \(message)"); exit(1) }
    }
    static func wait(_ message: String, _ condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(3)
        while !condition() && Date() < deadline { try? await Task.sleep(nanoseconds: 1_000_000) }
        check(condition(), message)
    }
    static func settle() async { try? await Task.sleep(nanoseconds: 20_000_000) }
    static func open(_ model: ResearchModel, _ state: String) async {
        model.openJob(id(state))
        await wait("saved \(state) opens") { model.title == "Study tools — \(state)" }
    }
    static func cleanup(_ model: ResearchModel, _ client: FixtureResearchClient) async {
        model.openJob(id("complete"))
        client.finishStreams()
        await settle()
    }

    static func main() async throws {
        data = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
        await libraryFilteringAndRefresh()
        try await actualListWireParsing()
        await restoreAllStatesReadOnly()
        await selectedJobRejectsOldSnapshotsAndCreation()
        await resumeAndReconnect()
        await selectionDuringSaveAndDeleteFailure()
        await steeringStaysWithItsJob()
        await completionWinsOverDelayedPause()
        try renderPreviewIfRequested()
        print("Research Library: \(checks) native checks passed (store fixtures, no live actions)")
    }

    static func libraryFilteringAndRefresh() async {
        let client = FixtureResearchClient(data), model = ResearchLibraryModel(client: FixtureResearchClient(data))
        model.refresh(); await wait("library loads") { !model.loading }
        check(model.items.count == 8, "all wire fixtures parsed")
        model.filter = .pinned
        check(model.visibleItems.count == 1 && model.visibleItems[0].state == "complete", "pinned filter")
        model.filter = .unfinished
        check(model.visibleItems.count == 4, "unfinished includes drafts, active, paused")
        model.filter = .recent; model.query = "STUDY complete"
        check(model.visibleItems.count == 1, "title/question search is case-insensitive and all-terms")
        model.query = "unrelated phrase"
        check(model.visibleItems.isEmpty, "unmatched query has no rows")
        let refreshing = ResearchLibraryModel(client: client)
        refreshing.refresh(); await wait("initial refresh") { !refreshing.loading }
        client.listFailure = true
        refreshing.refresh(); await wait("failed refresh finishes") { !refreshing.loading }
        check(!refreshing.errorText.isEmpty && refreshing.items.count == 8, "failure keeps previous rows and retry explanation")
        client.listFailure = false; client.blockList = true
        refreshing.refresh(); await wait("old list request pending") { client.waitingLists.count == 1 }
        refreshing.refresh(); await wait("new list request pending") { client.waitingLists.count == 2 }
        client.waitingLists[1].resume(returning: [client.jobs[0]])
        await wait("new list applied") { !refreshing.loading }
        client.waitingLists[0].resume(returning: [])
        await settle()
        check(refreshing.items.count == 1 && refreshing.errorText.isEmpty, "late old list cannot erase current rows")
    }

    static func actualListWireParsing() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [LibraryURLProtocol.self]
        config.urlCache = nil; config.httpCookieStorage = nil
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        LibraryURLProtocol.configure(try JSONSerialization.data(withJSONObject: ["jobs": data["jobs"]!]))
        let jobs = try await WispClient().researchJobs(using: session)
        check(jobs.count == 8 && jobs.contains(where: \.pinned), "real client parses backend-generated wire")
        let malformedLists: [[String: Any]] = [["wrong": []], ["jobs": [["state": "complete"]]]]
        for malformed in malformedLists {
            LibraryURLProtocol.configure(try JSONSerialization.data(withJSONObject: malformed))
            do { _ = try await WispClient().researchJobs(using: session); check(false, "invalid list must fail") }
            catch { check(true, "invalid list rejected") }
        }
        LibraryURLProtocol.configure(Data("{}".utf8), status: 503)
        do { _ = try await WispClient().researchJobs(using: session); check(false, "HTTP failure must fail") }
        catch { check(true, "HTTP failure rejected") }
    }

    static func restoreAllStatesReadOnly() async {
        let client = FixtureResearchClient(data), model = ResearchModel(client: FixtureResearchClient(data))
        let phases: [String: ResearchModel.Phase] = ["complete": .complete, "partial": .partial,
            "awaiting_approval": .awaitingApproval, "running": .running, "paused": .paused,
            "planning": .planning, "cancelled": .cancelled, "failed": .error]
        let liveModel = ResearchModel(client: client)
        for (state, phase) in phases {
            await open(liveModel, state)
            check(liveModel.phase == phase, "restored \(state) phase")
            check(liveModel.paused == (state == "paused"), "restored pause flag")
        }
        check(client.actions.isEmpty, "opening every saved state has no mutation/model action")
        await open(liveModel, "complete")
        check(liveModel.report.contains("Saved report") && liveModel.citations.first?.quote == "Exact fixture evidence.", "saved report keeps citations")
        check(liveModel.pinned, "saved pin restored")
        await open(model, "awaiting_approval")
        check(model.subquestions.count >= 2 && !model.objective.isEmpty, "saved plan is editable")
        await cleanup(liveModel, client)
    }

    static func selectedJobRejectsOldSnapshotsAndCreation() async {
        let client = FixtureResearchClient(data), model = ResearchModel(client: FixtureResearchClient(data))
        let selected = ResearchModel(client: client)
        client.blockedSnapshots.insert(id("partial"))
        selected.openJob(id("partial"))
        await wait("old snapshot pending") { client.waitingSnapshots[id("partial")]?.count == 1 }
        await open(selected, "complete")
        client.waitingSnapshots[id("partial")]![0].resume(returning: WispClient.ResearchSnapshot.parse(snapshots["partial"]!))
        await settle()
        check(selected.jobId == id("complete") && selected.phase == .complete, "late selected-job response rejected")
        client.blockedPlan = true
        selected.createPlan(prompt: "Old plan")
        await wait("old plan creation pending") { client.waitingPlan != nil }
        await open(selected, "complete")
        client.waitingPlan?.resume(returning: WispClient.ResearchSnapshot.parse(snapshots["awaiting_approval"]!))
        client.waitingPlan = nil
        await settle()
        check(selected.jobId == id("complete") && selected.phase == .complete, "late plan cannot replace selected report")
        model.openJob("missing")
        await wait("missing job produces recovery state") { model.phase == .error }
        check(!model.errorText.isEmpty && model.jobId == "missing", "missing job is recoverable")
        await cleanup(selected, client)
    }

    static func resumeAndReconnect() async {
        let client = FixtureResearchClient(data), model = ResearchModel(client: FixtureResearchClient(data))
        let active = ResearchModel(client: client)
        await open(active, "running")
        await wait("running job attaches stream") { client.streams.count == 1 }
        let seq = (snapshots["running"]!["last_seq"] as! Int) + 1
        check(client.streams[0].after == seq - 1, "reopened stream starts after snapshot cursor")
        client.emit(0, "evidence", ["seq": seq])
        client.emit(0, "evidence", ["seq": seq])
        await settle()
        check(active.evidenceCount == 1, "replayed evidence counted once")
        await open(active, "paused")
        await wait("paused job attaches read-only stream") { client.streams.count == 2 }
        client.emit(0, "error", ["seq": seq + 1, "message": "OLD JOB ERROR"])
        await settle()
        check(active.phase == .paused && active.errorText.isEmpty, "old stream cannot alter selected job")
        active.togglePause()
        await wait("explicit resume succeeds") { !active.actionInProgress && active.phase == .running }
        check(client.actions.count == 1 && client.actions[0].0 == id("paused") && client.actions[0].1 == "start", "resume uses one start operation on selected job")
        await wait("resume attaches new stream") { client.streams.count == 3 }
        client.emit(2, "error", ["message": "fixture disconnected"])
        client.finishStream(2)
        await wait("interrupted stream reconnects") { client.streams.count == 4 }
        check(active.phase == .running && active.report.isEmpty, "transport failure preserves active job")
        check(client.actions.count == 1, "reconnect never starts work")
        await open(active, "complete")
        client.finishStreams()
        await settle()
        check(active.phase == .complete, "late stream completion cannot alter report")
        await open(model, "complete")
    }

    static func selectionDuringSaveAndDeleteFailure() async {
        let client = FixtureResearchClient(data), model = ResearchModel(client: FixtureResearchClient(data))
        let selected = ResearchModel(client: client)
        await open(selected, "awaiting_approval")
        client.blockedSave = true
        selected.start(); selected.start()
        await wait("one plan save pending") { client.waitingSave != nil }
        check(client.actions.count == 1, "double click cannot save/start twice")
        await open(selected, "complete")
        client.waitingSave?.resume(returning: WispClient.ResearchSnapshot.parse(snapshots["awaiting_approval"]!))
        client.waitingSave = nil
        await settle()
        check(!client.actions.contains(where: { $0.1 == "start" }), "navigating during save prevents delayed start")
        check(selected.phase == .complete && !selected.actionInProgress, "old action cannot alter new report")
        var dismissed = false
        client.deleteResult = false
        selected.deleteJob { dismissed = true }
        await wait("failed deletion finishes") { !selected.actionInProgress }
        check(!dismissed && selected.jobId == id("complete") && !selected.errorText.isEmpty, "failed deletion keeps report open")
        client.deleteResult = true
        selected.deleteJob { dismissed = true }
        await wait("successful deletion dismisses") { dismissed }
        check(selected.phase == .idle && selected.jobId.isEmpty, "successful deletion clears selected job")
        await open(model, "complete")
    }

    static func steeringStaysWithItsJob() async {
        let client = FixtureResearchClient(data)
        let model = ResearchModel(client: client)
        await open(model, "running")
        model.steering = "Direction for the original job"
        await open(model, "complete")
        check(model.steering.isEmpty, "steering draft cannot cross jobs")
        await open(model, "running")
        client.blockedAction = "steer"
        model.steering = "Submitted direction"
        model.sendSteering()
        await wait("direction request pending") { client.waitingAction != nil }
        model.steering = "A newer draft"
        client.waitingAction?.resume(returning: false); client.waitingAction = nil
        await settle()
        check(model.steering == "A newer draft", "failed direction preserves newer typing")
        check(!model.errorText.isEmpty, "failed direction is explained")
        await cleanup(model, client)
    }

    static func completionWinsOverDelayedPause() async {
        let client = FixtureResearchClient(data)
        let model = ResearchModel(client: client)
        await open(model, "running")
        await wait("completion-race stream starts") { client.streams.count == 1 }
        client.blockedAction = "pause"
        model.togglePause()
        await wait("pause is in flight") { client.waitingAction != nil }
        let seq = (snapshots["running"]!["last_seq"] as! Int) + 1
        var completed = snapshots["complete"]!
        completed["id"] = id("running"); completed["last_seq"] = seq + 1
        client.rows[id("running")] = completed
        client.emit(0, "report", ["seq": seq, "state": "complete", "report": "Completed report"])
        client.emit(0, "done", ["seq": seq + 1, "state": "complete"])
        await wait("job completes before pause response") { model.phase == .complete }
        client.waitingAction?.resume(returning: true); client.waitingAction = nil
        await wait("delayed pause finishes") { !model.actionInProgress }
        check(model.phase == .complete && !model.paused, "delayed pause cannot replace finished report")
        await cleanup(model, client)
    }

    static func renderPreviewIfRequested() throws {
        guard let path = ProcessInfo.processInfo.environment["WISP_LIBRARY_PREVIEW_PATH"] else { return }
        _ = NSApplication.shared
        let model = ResearchLibraryModel(client: FixtureResearchClient(data))
        model.items = (data["jobs"] as! [[String: Any]]).compactMap(ResearchLibraryItem.parse)
        let host = NSHostingView(rootView: ResearchLibraryView(model: model, onOpen: { _ in }, onNew: {}))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 620),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host // Offscreen only: never ordered onto the desktop.
        host.frame = NSRect(x: 0, y: 0, width: 720, height: 620)
        host.layoutSubtreeIfNeeded()
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
            check(false, "preview bitmap available"); return
        }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            check(false, "preview PNG available"); return
        }
        try png.write(to: URL(fileURLWithPath: path))
        print("Offscreen library preview: \(path)")
    }
}
