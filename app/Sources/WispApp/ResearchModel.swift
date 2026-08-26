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

    private let client: WispClient
    private var streamTask: Task<Void, Never>?
    private var elapsedTimer: Timer?
    private var startedAt: Date?
    private var lastSeq = 0

    init(client: WispClient) { self.client = client }

    var isRunning: Bool { phase == .running || phase == .paused }
    var isFinished: Bool { phase == .complete || phase == .partial }

    func createPlan(prompt: String) {
        streamTask?.cancel()
        stopElapsedTimer()
        phase = .planning; status = "Drafting a research plan…"
        jobId = ""; title = ""; objective = prompt; subquestions = []
        activity = []; sources = []; citations = []; contradictions = []
        evidenceCount = 0; report = ""; errorText = ""
        paused = false; pinned = false; stopReason = ""; elapsedText = ""
        allowedDomainsText = ""; blockedDomainsText = ""; lastSeq = 0
        Task { [weak self] in
            guard let self else { return }
            guard let snapshot = await client.createResearchPlan(prompt: prompt, depth: depth) else {
                phase = .error; errorText = "Wisp couldn't create the research plan."
                return
            }
            apply(snapshot)
            phase = .awaitingApproval
            status = "Review the plan before research begins"
        }
    }

    func start() {
        guard !jobId.isEmpty else { return }
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
        Task { [weak self] in
            guard let self else { return }
            guard let snapshot = await client.updateResearchPlan(jobId: jobId, plan: plan) else {
                phase = .error; errorText = "Wisp couldn't save the research plan."; return
            }
            apply(snapshot)
            guard await client.researchAction(jobId: jobId, action: "start") else {
                phase = .error; errorText = "Wisp couldn't start this research job."; return
            }
            phase = .running; status = "Research started"
            startElapsedTimer()
            listen()
        }
    }

    func togglePause() {
        guard isRunning else { return }
        let next = !paused
        Task { [weak self] in
            guard let self else { return }
            if await client.researchAction(jobId: jobId, action: "pause", body: ["paused": next]) {
                paused = next
                phase = next ? .paused : .running
                status = next ? "Paused" : "Resuming…"
                if next { stopElapsedTimer() } else { startElapsedTimer(); listen() }
            }
        }
    }

    func cancel() {
        guard !jobId.isEmpty else { return }
        Task { [weak self] in
            guard let self else { return }
            _ = await client.researchAction(jobId: jobId, action: "cancel")
            status = "Cancelling after the current step…"
        }
    }

    func sendSteering() {
        let text = steering.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !jobId.isEmpty else { return }
        steering = ""
        Task { [weak self] in
            guard let self else { return }
            if await client.researchAction(jobId: jobId, action: "steer", body: ["text": text]) {
                activity.append("Steered: \(text)")
            }
        }
    }

    func export() {
        guard isFinished else { return }
        Task { [weak self] in
            guard let self, let url = await client.exportResearch(jobId: jobId, title: title) else { return }
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func togglePin() {
        guard !jobId.isEmpty else { return }
        let next = !pinned
        Task { [weak self] in
            guard let self else { return }
            if await client.pinResearch(jobId: jobId, pinned: next) { pinned = next }
        }
    }

    func deleteJob(then dismissed: @escaping () -> Void) {
        guard !jobId.isEmpty else { dismissed(); return }
        streamTask?.cancel(); stopElapsedTimer()
        Task { [weak self] in
            guard let self else { return }
            _ = await client.deleteResearch(jobId: jobId)
            dismissed()
        }
    }

    func updateDomains() {
        guard !jobId.isEmpty else { return }
        let allowed = allowedDomainsText.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let blocked = blockedDomainsText.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        Task { [weak self] in
            guard let self else { return }
            if let snapshot = await client.updateResearchDomains(jobId: jobId, allowed: allowed, blocked: blocked) {
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
        let id = jobId, cursor = lastSeq
        streamTask = Task { [weak self] in
            guard let self else { return }
            await client.streamResearch(jobId: id, after: cursor) { event in
                Task { @MainActor in self.handle(event) }
            }
        }
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
        if !snapshot.error.isEmpty { errorText = snapshot.error }
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
        lastSeq = max(lastSeq, ev.int("seq"))
        switch ev.type {
        case "status":
            status = ev.str("text")
            if ev.str("stage") == "paused" { paused = true; phase = .paused; stopElapsedTimer() }
            else if phase != .paused { phase = .running }
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
            phase = .error; errorText = ev.str("message"); status = "Research failed"
            stopElapsedTimer()
        case "done":
            stopElapsedTimer()
            let state = ev.str("state")
            if state == "cancelled" { phase = .cancelled; status = "Cancelled" }
            Task { [weak self] in
                guard let self, let snapshot = await client.researchSnapshot(jobId: jobId) else { return }
                apply(snapshot)
                if snapshot.state == "complete" { phase = .complete }
                else if snapshot.state == "partial" { phase = .partial }
                else if snapshot.state == "failed" { phase = .error }
            }
        default: break
        }
        if activity.count > 80 { activity.removeFirst(activity.count - 80) }
    }
}
