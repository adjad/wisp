import SwiftUI
import AppKit

@MainActor
final class ResearchModel: ObservableObject {
    enum Phase { case idle, planning, awaitingApproval, running, paused, complete, partial, cancelled, error }

    @Published var phase: Phase = .idle
    @Published var jobId = ""
    @Published var title = ""
    @Published var objective = ""
    @Published var depth = "standard"
    @Published var subquestions: [String] = []
    @Published var status = ""
    @Published var activity: [String] = []
    @Published var sources: [WispClient.ResearchSource] = []
    @Published var citations: [WispClient.ResearchCitation] = []
    @Published var contradictions: [WispClient.ResearchContradiction] = []
    @Published var evidenceCount = 0
    @Published var report = ""
    @Published var steering = ""
    @Published var errorText = ""
    @Published var paused = false
    @Published var pinned = false
    @Published var stopReason = ""
    @Published var elapsedText = ""
    @Published var allowedDomainsText = ""
    @Published var blockedDomainsText = ""
    @Published private(set) var actionInProgress = false

    private let client: ResearchClient
    private var streamTask: Task<Void, Never>?
    private var loadTask: Task<Void, Never>?
    private var selectionID = UUID()
    private var streamID = UUID()
    private var elapsedTimer: Timer?
    private var startedAt: Date?
    private var lastSeq = 0

    init(client: ResearchClient) { self.client = client }

    var isRunning: Bool { phase == .running || phase == .paused }
    var isFinished: Bool { phase == .complete || phase == .partial }

    private func prepareSelection() -> UUID {
        selectionID = UUID(); streamID = UUID()
        streamTask?.cancel(); loadTask?.cancel()
        stopElapsedTimer()
        startedAt = nil; actionInProgress = false
        jobId = ""; title = ""; objective = ""; subquestions = []
        activity = []; sources = []; citations = []; contradictions = []
        evidenceCount = 0; report = ""; errorText = ""
        paused = false; pinned = false; stopReason = ""; elapsedText = ""; steering = ""
        allowedDomainsText = ""; blockedDomainsText = ""; lastSeq = 0
        return selectionID
    }

    /// Opening is read-only: attach to existing work, never start a new run.
    func openJob(_ id: String) {
        guard !id.isEmpty else { return }
        let selection = prepareSelection()
        jobId = id; phase = .planning; status = "Opening saved research…"
        loadTask = Task { [weak self] in
            guard let self else { return }
            let snapshot = await client.researchSnapshot(jobId: id)
            guard selectionID == selection, !Task.isCancelled else { return }
            guard let snapshot, snapshot.id == id else {
                phase = .error
                errorText = "Couldn't open this saved job. It may have been removed, or Wisp may be unavailable."
                status = "Saved research unavailable"
                return
            }
            restore(snapshot)
            if isRunning || phase == .planning { listen() }
        }
    }

    func refreshSavedJob() { if !jobId.isEmpty { openJob(jobId) } }

    func createPlan(prompt: String) {
        let selection = prepareSelection(), requestedDepth = depth
        phase = .planning; status = "Drafting a research plan…"; objective = prompt
        loadTask = Task { [weak self] in
            guard let self else { return }
            let snapshot = await client.createResearchPlan(prompt: prompt, depth: requestedDepth)
            guard selectionID == selection, !Task.isCancelled else { return }
            guard let snapshot else {
                phase = .error; errorText = "Wisp couldn't create the research plan."
                return
            }
            apply(snapshot)
            phase = .awaitingApproval
            status = "Review the plan before research begins"
        }
    }

    func start() {
        guard !jobId.isEmpty, phase == .awaitingApproval, !actionInProgress else { return }
        let cleanQuestions = subquestions.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !objective.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              cleanQuestions.count >= 2 else {
            errorText = "The plan needs an objective and at least two subquestions."
            return
        }
        status = "Saving plan…"; errorText = ""
        let splitDomains: (String) -> [String] = { text in
            text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
        let plan = WispClient.ResearchPlanPayload(title: title, objective: objective,
                                                   depth: depth, subquestions: cleanQuestions,
                                                   allowedDomains: splitDomains(allowedDomainsText),
                                                   blockedDomains: splitDomains(blockedDomainsText))
        let id = jobId, selection = selectionID
        actionInProgress = true
        Task { [weak self] in
            guard let self else { return }
            defer { if selectionID == selection { actionInProgress = false } }
            let snapshot = await client.updateResearchPlan(jobId: id, plan: plan)
            guard selectionID == selection else { return }
            guard let snapshot else {
                errorText = "Wisp couldn't save the research plan. Try again."; return
            }
            apply(snapshot)
            let started = await client.researchAction(jobId: id, action: "start")
            guard selectionID == selection else { return }
            guard started else {
                errorText = "Wisp couldn't start this research job. Try again."; return
            }
            phase = .running; status = "Research started"
            startElapsedTimer()
            listen()
        }
    }

    func togglePause() {
        guard isRunning, !actionInProgress else { return }
        let next = !paused
        let id = jobId, selection = selectionID
        actionInProgress = true; errorText = ""
        Task { [weak self] in
            guard let self else { return }
            defer { if selectionID == selection { actionInProgress = false } }
            let updated = next
                ? await client.researchAction(jobId: id, action: "pause", body: ["paused": true])
                : await client.researchAction(jobId: id, action: "start")
            // Completion can arrive while this control request is in flight.
            // A terminal job ignores stale controls server-side; preserve the
            // terminal view too, rather than resurrecting a paused spinner.
            guard selectionID == selection, isRunning else { return }
            guard updated else {
                errorText = "Couldn't \(next ? "pause" : "resume") this research. Try again."; return
            }
            paused = next
            phase = next ? .paused : .running
            status = next ? "Paused" : "Resuming…"
            if next { stopElapsedTimer() } else { startElapsedTimer(); listen() }
        }
    }

    func cancel() {
        guard !jobId.isEmpty, !actionInProgress else { return }
        let id = jobId, selection = selectionID
        actionInProgress = true
        Task { [weak self] in
            guard let self else { return }
            defer { if selectionID == selection { actionInProgress = false } }
            let ok = await client.researchAction(jobId: id, action: "cancel")
            guard selectionID == selection, isRunning else { return }
            if ok { status = "Cancelling after the current step…" }
            else { errorText = "Couldn't cancel this research. Try again." }
        }
    }

    func sendSteering() {
        let text = steering.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !jobId.isEmpty, !actionInProgress else { return }
        let id = jobId, selection = selectionID
        steering = ""
        Task { [weak self] in
            guard let self else { return }
            let ok = await client.researchAction(jobId: id, action: "steer", body: ["text": text])
            guard selectionID == selection else { return }
            if ok {
                activity.append("Steered: \(text)")
            } else {
                if steering.isEmpty { steering = text }
                errorText = "Couldn't send the research direction. Try again."
            }
        }
    }

    func export() {
        guard isFinished else { return }
        let id = jobId, name = title, selection = selectionID
        Task { [weak self] in
            guard let self, let url = await client.exportResearch(jobId: id, title: name),
                  selectionID == selection else { return }
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func togglePin() {
        guard !jobId.isEmpty, !actionInProgress else { return }
        let next = !pinned
        let id = jobId, selection = selectionID
        actionInProgress = true
        Task { [weak self] in
            guard let self else { return }
            defer { if selectionID == selection { actionInProgress = false } }
            let ok = await client.pinResearch(jobId: id, pinned: next)
            guard selectionID == selection else { return }
            if ok { pinned = next }
            else { errorText = "Couldn't change the pin. Try again." }
        }
    }

    func deleteJob(then dismissed: @escaping () -> Void) {
        guard !actionInProgress else { return }
        guard !jobId.isEmpty else { dismissed(); return }
        let id = jobId, selection = selectionID
        actionInProgress = true
        Task { [weak self] in
            guard let self else { return }
            defer { if selectionID == selection { actionInProgress = false } }
            let ok = await client.deleteResearch(jobId: id)
            guard selectionID == selection else { return }
            guard ok else { errorText = "Couldn't delete this research. Try again."; return }
            _ = prepareSelection(); phase = .idle; status = ""
            dismissed()
        }
    }

    func updateDomains() {
        guard !jobId.isEmpty, !actionInProgress else { return }
        let id = jobId, selection = selectionID
        let allowed = allowedDomainsText.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let blocked = blockedDomainsText.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        Task { [weak self] in
            guard let self else { return }
            let snapshot = await client.updateResearchDomains(jobId: id, allowed: allowed, blocked: blocked)
            guard selectionID == selection else { return }
            if let snapshot {
                activity.append("Updated source domains")
                allowedDomainsText = snapshot.allowedDomains.joined(separator: ", ")
                blockedDomainsText = snapshot.blockedDomains.joined(separator: ", ")
            }
        }
    }

    private func startElapsedTimer() {
        stopElapsedTimer()
        if startedAt == nil { startedAt = Date() }
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, let started = self.startedAt else { return }
                let seconds = Int(Date().timeIntervalSince(started))
                self.elapsedText = seconds < 60 ? "\(seconds)s"
                    : "\(seconds / 60)m \(seconds % 60)s"
            }
        }
    }

    private func stopElapsedTimer() {
        elapsedTimer?.invalidate(); elapsedTimer = nil
    }

    func open(_ source: WispClient.ResearchSource) {
        guard let url = URL(string: source.url) else { return }
        NSWorkspace.shared.open(url)
    }

    func open(_ citation: WispClient.ResearchCitation) {
        guard let url = URL(string: citation.url) else { return }
        NSWorkspace.shared.open(url)
    }

    private func listen() {
        streamTask?.cancel()
        let id = jobId, selection = selectionID, connection = UUID()
        streamID = connection
        streamTask = Task { [weak self] in
            guard let self else { return }
            var retryDelay: UInt64 = 500_000_000
            while !Task.isCancelled && selectionID == selection && streamID == connection {
                await client.streamResearch(jobId: id, after: lastSeq) { [weak self] event in
                    Task { @MainActor in
                        guard let self, self.selectionID == selection, self.streamID == connection else { return }
                        self.handle(event)
                    }
                }
                guard !Task.isCancelled, selectionID == selection, streamID == connection else { return }
                if let snapshot = await client.researchSnapshot(jobId: id) {
                    guard !Task.isCancelled, selectionID == selection, streamID == connection else { return }
                    restore(snapshot)
                    if !isRunning && phase != .planning { return }
                }
                guard !Task.isCancelled, selectionID == selection, streamID == connection else { return }
                if phase != .paused { status = "Reconnecting to research…" }
                do { try await Task.sleep(nanoseconds: retryDelay) }
                catch { return }
                retryDelay = min(retryDelay * 2, 8_000_000_000)
            }
        }
    }

    private func restore(_ snapshot: WispClient.ResearchSnapshot) {
        guard snapshot.id == jobId, snapshot.lastSeq >= lastSeq else { return }
        apply(snapshot)
        paused = snapshot.state == "paused"
        switch snapshot.state {
        case "planning": phase = .planning; status = "Preparing research plan…"
        case "awaiting_approval": phase = .awaitingApproval; status = "Review the saved plan before research begins"
        case "running": phase = .running; status = "Research in progress"
        case "paused": phase = .paused; status = "Paused — choose Resume to continue"
        case "complete": phase = .complete; status = "Research complete"
        case "partial": phase = .partial; status = "Finished with evidence gaps"
        case "cancelled": phase = .cancelled; status = "Cancelled"
        case "failed":
            phase = .error; status = "Research failed"
            if errorText.isEmpty { errorText = "This research job couldn't finish." }
        default:
            phase = .error; status = "Saved research"
            errorText = "This saved job has an unsupported state."
        }
        if phase == .running { startElapsedTimer() } else { stopElapsedTimer() }
    }

    private func apply(_ snapshot: WispClient.ResearchSnapshot) {
        jobId = snapshot.id; title = snapshot.title; objective = snapshot.objective
        depth = snapshot.depth; subquestions = snapshot.subquestions
        sources = snapshot.sources; citations = snapshot.citations
        contradictions = snapshot.contradictions
        evidenceCount = snapshot.evidenceCount
        report = snapshot.report; lastSeq = max(lastSeq, snapshot.lastSeq)
        pinned = snapshot.pinned; stopReason = snapshot.stopReason
        allowedDomainsText = snapshot.allowedDomains.joined(separator: ", ")
        blockedDomainsText = snapshot.blockedDomains.joined(separator: ", ")
        if snapshot.createdAt > 0 { startedAt = Date(timeIntervalSince1970: snapshot.createdAt) }
        errorText = snapshot.error
    }

    private func upsertSource(id: String, title: String, url: String, domain: String,
                              status: String, error: String = "", qualityClass: String? = nil,
                              qualityReason: String? = nil) {
        let old = sources.first(where: { $0.id == id })
        let source = WispClient.ResearchSource(id: id, title: title, url: url, domain: domain,
            status: status, error: error, qualityClass: qualityClass ?? old?.qualityClass ?? "",
            qualityReason: qualityReason ?? old?.qualityReason ?? "")
        if let idx = sources.firstIndex(where: { $0.id == id }) { sources[idx] = source }
        else { sources.append(source) }
    }

    private func handle(_ ev: WispClient.Event) {
        let seq = ev.int("seq")
        if seq > 0 && seq <= lastSeq { return }
        lastSeq = max(lastSeq, seq)
        switch ev.type {
        case "status":
            status = ev.str("text")
            if ev.str("stage") == "paused" { paused = true; phase = .paused; stopElapsedTimer() }
            else if ev.str("stage") == "resumed" || ev.str("stage") == "starting" {
                paused = false; phase = .running; startElapsedTimer()
            } else if phase != .paused { phase = .running }
        case "query":
            activity.append("Searched: \(ev.str("query"))")
        case "source_found":
            upsertSource(id: ev.str("source_id"), title: ev.str("title"),
                         url: ev.str("url"), domain: ev.str("domain"), status: "found")
        case "source_read":
            let id = ev.str("source_id")
            let old = sources.first(where: { $0.id == id })
            upsertSource(id: id, title: ev.str("title"), url: ev.str("url"),
                         domain: old?.domain ?? "", status: "read",
                         qualityClass: ev.str("quality_class"))
        case "source_failed":
            let id = ev.str("source_id")
            let old = sources.first(where: { $0.id == id })
            upsertSource(id: id, title: ev.str("title"), url: old?.url ?? "",
                         domain: old?.domain ?? "", status: "failed", error: ev.str("error"))
        case "evidence": evidenceCount += 1
        case "warning": activity.append("Note: \(ev.str("text"))")
        case "steering": activity.append("Direction updated")
        case "report":
            report = ev.str("report")
            let state = ev.str("state")
            phase = state == "partial" ? .partial : .complete
            status = state == "partial" ? "Finished with evidence gaps" : "Research complete"
            stopElapsedTimer()
        case "error":
            // Transport errors have no persisted sequence. Preserve gathered
            // work and let listen() reconcile/reconnect instead of declaring
            // the backend job itself failed.
            if seq == 0 { status = "Connection interrupted. Reconnecting…"; return }
            phase = .error; errorText = ev.str("message"); status = "Research failed"
            stopElapsedTimer()
        case "plan", "done":
            if ev.type == "done" { stopElapsedTimer() }
            let state = ev.str("state")
            if state == "cancelled" { phase = .cancelled; status = "Cancelled" }
            let id = jobId, selection = selectionID, connection = streamID
            Task { [weak self] in
                guard let self, let snapshot = await client.researchSnapshot(jobId: id),
                      selectionID == selection, streamID == connection else { return }
                restore(snapshot)
                if !isRunning && phase != .planning { streamTask?.cancel() }
            }
        default: break
        }
        if activity.count > 80 { activity.removeFirst(activity.count - 80) }
    }
}
