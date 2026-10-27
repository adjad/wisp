import Foundation

// Tracks in-flight writes to the backend that persist to ~/.moe/config.yaml
// (role/model changes, …). Quitting Wisp stops the
// backend process outright (see AppDelegate.quit), which would otherwise race
// a just-fired settings save: the SwiftUI change lands in @Published state
// immediately, but the POST that actually persists it runs in a detached
// Task with nothing awaiting completion, so a quit a moment later could kill
// the backend mid-request and silently lose the change. quit() awaits
// waitUntilIdle() first so a pending save gets to land before the process dies.
actor PendingConfigWrites {
    static let shared = PendingConfigWrites()
    private var count = 0
    func begin() { count += 1 }
    func end() { count = max(0, count - 1) }
    func waitUntilIdle(timeoutMs: Int = 3000) async {
        let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000)
        while count > 0 && Date() < deadline {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }
}

// Streams events from the local Wisp service (FastAPI on :8765).
final class WispClient {
    #if WISP_SETTINGS_QA
    static let baseURL = SettingsQAEnvironment.baseURL
    #else
    static let baseURL = URL(string: "http://127.0.0.1:8765")!
    #endif
    private let deliverySession: URLSession
    private let deliveryURL: URL

    init(deliverySession: URLSession = .shared, deliveryURL: URL = WispClient.baseURL) {
        self.deliverySession = deliverySession
        self.deliveryURL = deliveryURL
    }

    func assistantDeliveryPost(_ path: String, body: [String: Any]) async -> [String: Any]? {
        var request = URLRequest(url: deliveryURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await deliverySession.data(for: request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return object
    }

    struct Event {
        let type: String
        let payload: [String: Any]
        func str(_ k: String) -> String { payload[k] as? String ?? "" }
        func bool(_ k: String) -> Bool { payload[k] as? Bool ?? false }
        func int(_ k: String) -> Int { payload[k] as? Int ?? 0 }

        static func decodeAssistantLine(_ line: String) -> Event? {
            guard line.hasPrefix("data: "),
                  let data = line.dropFirst(6).data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let type = object["type"] as? String, !type.isEmpty else { return nil }
            if let identity = object["event_id"], (identity as? String)?.isEmpty != false { return nil }
            return Event(type: type, payload: object)
        }
    }

    struct SourceSyncStatus: Identifiable {
        let id: String
        let label: String
        let state: String
        let progress: Double?
        let detail: String
        let warning: String
        /// Why an unavailable source can't be read, as its reader classified the
        /// error ("full_disk_access", …); "" when unknown or not unavailable.
        var reasonKind: String = ""

        /// Unavailable because Wisp lacks Full Disk Access, so a button can open it.
        var needsFullDiskAccess: Bool { state == "unavailable" && reasonKind == "full_disk_access" }

        var percentageLabel: String {
            if state == "unavailable" { return "Unavailable" }
            if state == "disabled" { return "Off" }
            guard let progress else { return "Waiting" }
            let percentage = "\(Int((min(max(progress, 0), 1) * 100).rounded(.down)))%"
            return warning.isEmpty ? percentage : "\(percentage) · Local"
        }

        static func parse(_ row: [String: Any]) -> SourceSyncStatus? {
            guard let id = row["id"] as? String,
                  let label = row["label"] as? String,
                  let state = row["state"] as? String else { return nil }
            return SourceSyncStatus(id: id, label: label, state: state,
                                    progress: (row["progress"] as? NSNumber)?.doubleValue,
                                    detail: row["progress_detail"] as? String ?? "",
                                    warning: row["warning"] as? String ?? "",
                                    reasonKind: row["reason_kind"] as? String ?? "")
        }
    }

    struct AssistantSyncStatus {
        let sources: [SourceSyncStatus]
        let progress: Double
        let completed: Int
        let total: Int
        let pendingLabels: [String]
        let unavailableLabels: [String]
        let disabledLabels: [String]

        var label: String {
            let count = "\(completed)/\(total)"
            guard !pendingLabels.isEmpty else {
                if !unavailableLabels.isEmpty {
                    let name = unavailableLabels.count == 1 ? unavailableLabels[0] : "Some sources"
                    return "\(name) unavailable · \(count) checked"
                }
                if !disabledLabels.isEmpty {
                    let name = disabledLabels.count == 1 ? disabledLabels[0] : "Some sources"
                    return "\(name) off · \(count) checked"
                }
                let local = sources.filter { !$0.warning.isEmpty }.map(\.label)
                if !local.isEmpty {
                    return "\(local.joined(separator: ", ")) cached · \(count) checked"
                }
                return "Synced · \(count)"
            }
            let names: String
            if pendingLabels.count == 1 {
                names = pendingLabels[0]
            } else if pendingLabels.count == 2 {
                names = pendingLabels.joined(separator: ", ")
            } else {
                names = "Sources"
            }
            return "\(names) syncing · \(count)"
        }
    }

    // A thing with a time — calendar event, assignment, or a manual reminder.
    struct Commitment: Identifiable, Equatable {
        let id: String
        let kind: String        // event | meeting | assignment | exam | reminder
        let title: String
        let context: String     // calendar name / course / sender — NOT a person
        let organizer: String   // real meeting organizer, empty unless it's someone other than the user
        let whenTs: Double       // epoch seconds
        var when: Date { Date(timeIntervalSince1970: whenTs) }

        static func parse(_ obj: [String: Any]) -> Commitment? {
            guard let id = obj["id"] as? String,
                  let title = obj["title"] as? String,
                  let ts = obj["when_ts"] as? Double else { return nil }
            return Commitment(id: id,
                              kind: obj["kind"] as? String ?? "event",
                              title: title,
                              context: obj["context"] as? String ?? "",
                              organizer: obj["organizer"] as? String ?? "",
                              whenTs: ts)
        }
    }

    // GET /assistant/next -> the soonest active commitment (chip data).
    func assistantNext() async -> Commitment? {
        let url = Self.baseURL.appendingPathComponent("assistant/next")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let c = obj["commitment"] as? [String: Any] else { return nil }
        return Commitment.parse(c)
    }

    // GET /assistant/upcoming -> next N days.
    func assistantUpcoming(days: Int = 7) async -> [Commitment] {
        let url = Self.baseURL.appendingPathComponent("assistant/upcoming")
            .appending(queryItems: [URLQueryItem(name: "days", value: String(days))])
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = obj["commitments"] as? [[String: Any]] else { return [] }
        return arr.compactMap(Commitment.parse)
    }

    // POST /assistant/commitments/{id} -> mark done/dismissed.
    @discardableResult
    func updateCommitment(_ id: String, status: String) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/commitments/\(id)"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["status": status])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["ok"] as? Bool ?? false
    }

    // POST /assistant/scheduled_send/{id}/ack -> confirm this app recorded an
    // unknown-outcome notice. Until it succeeds the backend keeps republishing
    // the notice, so a failure here must leave it unacknowledged rather than
    // being swallowed.
    @discardableResult
    func ackScheduledSendNotice(_ id: String) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent(
            "assistant/scheduled_send/\(id)/ack"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["ok"] as? Bool ?? false
    }

    // DELETE /assistant/commitments/{id} -> cancel/delete (removes real calendar
    // events from macOS Calendar too, via the backend).
    @discardableResult
    func cancelCommitment(_ id: String) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/commitments/\(id)"))
        req.httpMethod = "DELETE"
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return false }
        return obj["ok"] as? Bool ?? false
    }

    // POST /assistant/commitments -> add a manual reminder/event.
    @discardableResult
    func addCommitment(title: String, whenTs: Double, kind: String = "reminder",
                       context: String? = nil) async -> Commitment? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/commitments"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["title": title, "when_ts": whenTs, "kind": kind]
        if let context { body["context"] = context }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let c = obj["commitment"] as? [String: Any] else { return nil }
        return Commitment.parse(c)
    }

    // POST /assistant/daily_summary -> combined calendar+email+messages brief.
    //
    // Longer timeout than the other calls because the brief is TWO model calls
    // over three sources, and the first one of the day may also have to swap the
    // resident model. Measured on Agents-A1-4B: ~7s warm, ~68s when the press
    // triggers a load. 120s left almost no margin over that worst case, and the
    // way a timeout surfaces here — nil, rendered as "Couldn't build a summary
    // right now." — is indistinguishable from the button being broken, which it
    // separately was (see brief._sections).
    // `sessionId` continues the panel's conversation, and the brief is recorded
    // in it so the NEXT message can refer to it — "send Trishe my daily summary",
    // "reply to the second one". Without that the backend, which answers that
    // next message, has no record of the summary the user is looking at.
    func dailySummary(sessionId: String = "") async -> (text: String?, sessionId: String) {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/daily_summary"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["session_id": sessionId])
        req.timeoutInterval = 240
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let text = obj["text"] as? String else { return (nil, sessionId) }
        return (text, obj["session_id"] as? String ?? sessionId)
    }

    // The Daily Summary's four launch-critical sources. A denied source counts
    // as completed (the brief can explicitly degrade it); only an in-flight
    // first read keeps the compact sync status pending.
    func assistantSyncStatus(sources: [String]) async -> AssistantSyncStatus? {
        let url = Self.baseURL.appendingPathComponent("assistant/sync/status")
            .appending(queryItems: [URLQueryItem(name: "sources", value: sources.joined(separator: ","))])
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let sync = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let progress = (sync["progress"] as? NSNumber)?.doubleValue ?? 0
        let completed = (sync["completed"] as? NSNumber)?.intValue ?? 0
        let total = (sync["total"] as? NSNumber)?.intValue ?? 4
        let labels = sync["pending_labels"] as? [String] ?? []
        let unavailable = (sync["sources"] as? [[String: Any]] ?? [])
            .filter { $0["state"] as? String == "unavailable" }
            .compactMap { $0["label"] as? String }
        let disabled = (sync["sources"] as? [[String: Any]] ?? [])
            .filter { $0["state"] as? String == "disabled" }
            .compactMap { $0["label"] as? String }
        let sources = (sync["sources"] as? [[String: Any]] ?? []).compactMap(SourceSyncStatus.parse)
        return AssistantSyncStatus(sources: sources, progress: progress, completed: completed,
                                   total: total, pendingLabels: labels,
                                   unavailableLabels: unavailable, disabledLabels: disabled)
    }

    /// The hours (0-23) of the morning and evening Daily Summary runs, as the
    /// service confirms them. Nil when they can't be read.
    struct BriefTimes: Equatable {
        let morning: Int
        let evening: Int

        static func parse(_ obj: [String: Any]?) -> BriefTimes? {
            guard let times = obj?["times"] as? [String: Any],
                  let morning = times["morning"] as? Int, (0..<12).contains(morning),
                  let evening = times["evening"] as? Int, (12..<24).contains(evening) else { return nil }
            return BriefTimes(morning: morning, evening: evening)
        }
    }

    // GET /assistant/summary_schedule -> {"times": {"morning": 8, "evening": 20}}.
    func briefTimes() async -> BriefTimes? {
        let url = Self.baseURL.appendingPathComponent("assistant/summary_schedule")
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return BriefTimes.parse(try? JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // GET /models/memory -> {"available": true, "models": [{"id": "...", "bytes": 4200000000}]}.
    // The caller reads it with ModelMemory.parse (QuickSettingsText.swift).
    func modelMemoryReply() async -> [String: Any]? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("models/memory"))
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    // POST /assistant/summary_schedule {"times": {...}} -> the times the service saved.
    func setBriefTimes(morning: Int? = nil, evening: Int? = nil) async -> BriefTimes? {
        var times: [String: Int] = [:]
        if let morning { times["morning"] = morning }
        if let evening { times["evening"] = evening }
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/summary_schedule"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["times": times])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return BriefTimes.parse(try? JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // --- behavior engine (service/behavior) ---------------------------------
    // Learned routines, predictions and nudges. Nudges themselves arrive as
    // `nudge` events on the assistant stream; these calls answer them.

    /// Nudge/pattern ids are deterministic ASCII hashes; anything else is
    /// refused rather than spliced into a URL path.
    static func behaviorSafeID(_ id: String) -> Bool {
        guard !id.isEmpty, id.count <= 200 else { return false }
        return id.unicodeScalars.allSatisfy { s in
            (s.value >= 48 && s.value <= 57) || (s.value >= 65 && s.value <= 90)
                || (s.value >= 97 && s.value <= 122) || "-_.:".unicodeScalars.contains(s)
        }
    }

    private func behaviorRequest(_ method: String, _ path: String, body: [String: Any]? = nil,
                                 query: [URLQueryItem] = [],
                                 timeout: TimeInterval = 15) async -> [String: Any]? {
        var url = Self.baseURL.appendingPathComponent(path)
        if !query.isEmpty, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            url = parts.url ?? url
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.timeoutInterval = timeout
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode)
        else { return nil }
        if data.isEmpty { return [:] }
        return ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) ?? [:]
    }

    // POST /behavior/nudges/{id}/feedback {"verdict": useful|not_now|wrong}
    @discardableResult
    func behaviorFeedback(nudgeID: String, verdict: String) async -> Bool {
        guard Self.behaviorSafeID(nudgeID), ["useful", "not_now", "wrong"].contains(verdict),
              let obj = await behaviorRequest("POST", "behavior/nudges/\(nudgeID)/feedback",
                                              body: ["verdict": verdict]) else { return false }
        return obj["ok"] as? Bool ?? true
    }

    // POST /behavior/nudges/{id}/undo -> reverses the auto action(s) the
    // backend already performed (reminder / tentative hold).
    @discardableResult
    func behaviorUndo(nudgeID: String) async -> Bool {
        guard Self.behaviorSafeID(nudgeID),
              let obj = await behaviorRequest("POST", "behavior/nudges/\(nudgeID)/undo",
                                              body: [:], timeout: 30) else { return false }
        return obj["ok"] as? Bool ?? true
    }

    // POST /behavior/nudges/{id}/act {"action_index": n}. open_prompt ->
    // {"ok", "prompt", "label"}: the prompt is only prefilled in the composer,
    // nothing is sent. Offered add_reminder/add_hold -> {"ok", "created":
    // {"title", "when_ts"}, "can_undo"} or {"ok": false, "detail"}.
    func behaviorAct(nudgeID: String, actionIndex: Int) async -> [String: Any]? {
        guard Self.behaviorSafeID(nudgeID), actionIndex >= 0 else { return nil }
        return await behaviorRequest("POST", "behavior/nudges/\(nudgeID)/act",
                                     body: ["action_index": actionIndex])
    }

    // GET /behavior/status | /behavior/patterns | /behavior/predict | /behavior/profile
    func behaviorGet(_ endpoint: String) async -> [String: Any]? {
        guard ["status", "patterns", "predict", "profile", "weekly", "history", "signals", "email-history"].contains(endpoint) else { return nil }
        return await behaviorRequest("GET", "behavior/\(endpoint)")
    }

    func behaviorEmailHistoryControl(_ body: [String: Any]) async -> [String: Any]? {
        guard let command = body["command"] as? String,
              ["start", "pause", "reset"].contains(command) else { return nil }
        return await behaviorRequest("POST", "behavior/email-history/control", body: body)
    }

    func behaviorActivityLease(_ client: String) async -> [String: Any]? {
        await behaviorRequest("GET", "behavior/activity/lease", query: [URLQueryItem(name: "client", value: client)])
    }

    func behaviorActivityBatch(lease: [String: Any], intervals: [[String: Any]]) async -> Bool {
        await behaviorRequest("POST", "behavior/activity", body: ["lease": lease, "intervals": intervals]) != nil
    }

    func setBehaviorSignal(_ name: String, enabled: Bool) async -> [String: Any]? {
        await behaviorRequest("PUT", "behavior/signals", body: [name: enabled])
    }

    func behaviorHistoryNext(_ source: String) async -> [String: Any]? {
        guard ["calendar", "completed_reminders"].contains(source) else { return nil }
        return await behaviorRequest("GET", "behavior/history/\(source)/next")
    }

    func behaviorHistoryBatch(_ source: String, job: [String: Any], rows: [[String: Any]],
                              complete: Bool, reason: String = "") async -> Bool {
        guard ["calendar", "completed_reminders"].contains(source),
              let scanID = job["scan_id"] as? String,
              let start = job["page_start"], let end = job["page_end"] else { return false }
        return await behaviorRequest("POST", "behavior/history/\(source)/batch", body: [
            "scan_id": scanID, "start_ts": start, "end_ts": end,
            "rows": rows, "complete": complete, "reason": reason], timeout: 60) != nil
    }

    func behaviorHistoryControl(_ source: String, command: String) async -> Bool {
        guard ["calendar", "completed_reminders"].contains(source),
              ["pause", "resume", "restart"].contains(command) else { return false }
        return await behaviorRequest("POST", "behavior/history/\(source)/control",
                                     body: ["command": command]) != nil
    }

    // GET /behavior/predict?day=today|tomorrow -> {"predictions": [...]}
    func behaviorPredictions(day: String) async -> [String: Any]? {
        guard ["today", "tomorrow"].contains(day) else { return nil }
        return await behaviorRequest("GET", "behavior/predict", query: [URLQueryItem(name: "day", value: day)])
    }

    // "Learn my routines" (on by default). Mutations require a settled receipt;
    // enabling also requires a fresh one-shot epoch. A plain GET is a snapshot.
    // Returns nil when confirmation is unavailable; a WISP_BEHAVIOR
    // launch override can make the effective state differ from the request.
    func behaviorEnabled() async -> Bool? {
        await behaviorRequest("GET", "behavior/settings")?["enabled"] as? Bool
    }

    func setBehaviorEnabled(_ on: Bool, controlEpoch: String?) async -> [String: Any]? {
        var body: [String: Any] = ["enabled": on]
        if on {
            // Capture the visible receipt at the click; an old intent cannot
            // mint a newer permit after a later status check has fenced it.
            guard let epoch = Self.validBehaviorControlEpoch(controlEpoch) else { return nil }
            body["control_epoch"] = epoch
        }
        // OFF itself fences earlier enables. Send it even when a status check
        // would wait behind a slow update; never require a successful read first.
        return Self.settledBehaviorSettings(
            await behaviorRequest("PUT", "behavior/settings", body: body))
    }

    // POST /behavior/settings/check, whole payload: "enabled", "remote_active" (a remote
    // inference profile is pinned) and "phraser" (nudge wording / event labeling
    // requests are allowed to leave the Mac's local model).
    func behaviorSettings() async -> [String: Any]? {
        Self.settledBehaviorSettings(await behaviorRequest("POST", "behavior/settings/check"))
    }

    private static func settledBehaviorSettings(_ object: [String: Any]?) -> [String: Any]? {
        guard let result = object,
              result["updates_settled"] as? Bool == true,
              result["enabled"] is Bool,
              validBehaviorControlEpoch(result["control_epoch"]) != nil
        else { return nil }
        return result
    }

    private static func validBehaviorControlEpoch(_ value: Any?) -> String? {
        guard let epoch = value as? String, epoch.utf8.count == 64,
              epoch.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) })
        else { return nil }
        return epoch
    }

    // DELETE /behavior/data {"confirm": true} -> deletes what Wisp learned and the
    // untouched reminders/holds it created. Returns the counts, or nil on failure.
    func forgetBehaviorData() async -> [String: Any]? {
        await behaviorRequest("DELETE", "behavior/data", body: ["confirm": true], timeout: 60)
    }

    // POST /behavior/run -> mine + predict + generate nudges now.
    func behaviorRun() async -> [String: Any]? {
        await behaviorRequest("POST", "behavior/run", body: [:], timeout: 60)
    }

    // POST /behavior/patterns/{id} {"status": active|confirmed|rejected}
    @discardableResult
    func setBehaviorPatternStatus(_ patternID: String, status: String) async -> Bool {
        guard Self.behaviorSafeID(patternID), ["active", "confirmed", "rejected"].contains(status),
              let obj = await behaviorRequest("POST", "behavior/patterns/\(patternID)",
                                              body: ["status": status]) else { return false }
        return obj["ok"] as? Bool ?? true
    }

    // GET /assistant/events (SSE) -> reminders + `changed` pings.
    func assistantEvents(onEvent: @escaping (Event) async -> Bool) async {
        let req = URLRequest(url: deliveryURL.appendingPathComponent("assistant/events"))
        do {
            let (bytes, response) = try await deliverySession.bytes(for: req)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return }
            for try await line in bytes.lines {
                guard let event = Event.decodeAssistantLine(line) else { continue }
                _ = await onEvent(event)
            }
            // The server ends each stream (~25s) so failed/undecodable events
            // are retried on a fresh replay. Never cut it from this side: the
            // server would still count this stream as subscribed while nothing
            // reads it, and a send published into that gap was lost.
        } catch {
            // Stream dropped (backend restart, etc.) — the caller re-subscribes.
        }
    }

    // Pass `sessionId` to continue a conversation; the server loads & budgets the
    // history. Omit it (empty) to start a fresh chat — the new id arrives in the
    // first `session` event. `testMode` asks the service to run a stateless dry
    // run where tool calls are reported but never executed.
    func runAgent(prompt: String, image: String?, sessionId: String = "",
                  debug: Bool = false,
                  testMode: Bool = false,
                  corpusCaseID: String? = nil, corpusToken: String? = nil, corpusNormal: Bool = false,
                  onEvent: @escaping @Sendable (Event) -> Void) async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent(corpusCaseID == nil ? "agent" : "corpus/run"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Default URLRequest timeout is 60s of no data. Hard reasoning prompts can
        // run silent (no SSE traffic) for well over a minute — a genuinely long
        // answer looked identical to a hung request. 10 min covers the slowest
        // model swap + generation while still failing visibly if truly stuck.
        req.timeoutInterval = 600
        // `debug` gates the backend's raw_model_io events (full request +
        // response per model call this turn) — real cost, not free, so it
        // only goes out when the user has Debug Mode on and might export.
        var body: [String: Any] = ["prompt": prompt, "debug": debug]
        if testMode { body["test_mode"] = true }
        if !testMode && !sessionId.isEmpty { body["session_id"] = sessionId }
        if let image { body["image"] = image }
        if let corpusCaseID {
            body = ["case_id": corpusCaseID, "token": corpusToken ?? "", "mode": corpusNormal ? "normal" : "bounded"]
        }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        // The request goes out at most twice, and the second time only when the
        // server provably never ran the turn (AgentSend.retryable): no
        // connection, a connection lost before any response, or a 5xx before
        // the stream began. Never once a 200 stream has started, since a tool
        // may already have run, and never after the turn was cancelled.
        var bytes: URLSession.AsyncBytes?
        for attempt in 0..<2 {
            do {
                let (stream, response) = try await URLSession.shared.bytes(for: req)
                if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                    if attempt == 0, !Task.isCancelled, AgentSend.retryable(status: http.statusCode) {
                        try? await Task.sleep(nanoseconds: AgentSend.retryDelay)
                        if Task.isCancelled { return }
                        continue
                    }
                    onEvent(Event(type: "error", payload: AgentSend.unsent(status: http.statusCode)))
                    return
                }
                bytes = stream
                break
            } catch {
                if attempt == 0, !Task.isCancelled, AgentSend.retryable(error: error) {
                    try? await Task.sleep(nanoseconds: AgentSend.retryDelay)
                    if Task.isCancelled { return }
                    continue
                }
                onEvent(Event(type: "error", payload: AgentSend.unsent(error: error)))
                return
            }
        }
        guard let bytes else { return }
        do {
            var sawTerminal = false
            for try await line in bytes.lines {
                guard line.hasPrefix("data: ") else { continue }
                let json = String(line.dropFirst(6))
                guard let data = json.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let type = obj["type"] as? String else { continue }
                onEvent(Event(type: type, payload: obj))
                // Stop on ANY terminal event. Previously only "done" broke the loop,
                // so a server "error" left us waiting for a "done" that never came —
                // the connection hung until the 10-min timeout.
                if type == "done" || type == "error" { sawTerminal = true; break }
            }
            // The stream can close cleanly with no terminal event. Emit a terminal
            // so the model can recover (see handle()). The server may have run the
            // turn, so this is never resent automatically.
            if !sawTerminal {
                onEvent(Event(type: "error",
                              payload: ["message": "The assistant didn't respond.",
                                        "dropped": true]))
            }
        } catch {
            onEvent(Event(type: "error",
                          payload: ["message": error.localizedDescription, "dropped": true]))
        }
    }

    // `scope` is "once" (this call only), "always" (record a standing grant so
    // the same tool+target stops asking), or "never" (record a standing block).
    // See service/safety/grants.py — some tools, notably sending mail and
    // messages, refuse a standing grant and always re-ask regardless.
    func approve(sessionId: String, actionId: String, approved: Bool,
                 scope: String = "once", requestId: String = "") async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("agent/approve"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [
            "session_id": sessionId, "action_id": actionId,
            "approved": approved, "scope": scope,
        ]
        // Routes the answer to the exact request that raised the card.
        if !requestId.isEmpty { body["request_id"] = requestId }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        _ = try? await URLSession.shared.data(for: req)
    }

    // Send the exact text the user reviewed in Wisp's editable draft card.
    // The button press is the confirmation; the backend still resolves the
    // contact and waits for Messages.app to report the real send outcome.
    func sendMessageDraft(to: String, text: String) async -> (ok: Bool, result: String) {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/send_message_draft"))
        req.httpMethod = "POST"
        req.timeoutInterval = 60
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["to": to, "text": text])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (false, "Wisp could not reach the message service.") }
        let result = obj["result"] as? String ?? "The message was not sent."
        return (http.statusCode == 200 && (obj["ok"] as? Bool ?? false), result)
    }

    // Unload all resident models from oMLX to free memory/power.
    func unloadAll() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("unload_all"))
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
    }

    // Unload the resident agent model — keeps the small always-on
    // router warm. Used when dismissing to menu-bar-only (the X button).
    func unloadAgent() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("unload_agent"))
        req.httpMethod = "POST"
        req.timeoutInterval = 15
        _ = try? await URLSession.shared.data(for: req)
    }

    // Stop the oMLX engine entirely: its models and its ~2GB baseline. Used on quit.
    /// POST /assistant/reset_wisp (service/wisp_reset.py): the HTTP status and body,
    /// or a nil status when the service didn't answer. Only after the person's
    /// Reset Wisp click (AppDelegate.performWispReset).
    func resetWisp() async -> (status: Int?, body: [String: Any]?) {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent(WispReset.route))
        req.httpMethod = "POST"
        // Longer than the service's own bounds: stop work, sign out of Google, forget, delete.
        req.timeoutInterval = 180
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["confirm": WispReset.confirmPhrase])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse else { return (nil, nil) }
        return (http.statusCode, (try? JSONSerialization.jsonObject(with: data)) as? [String: Any])
    }

    // The wait is longer than the backend's own bound on the stop, so the engine is
    // gone before the backend is; an engine left running would keep its models loaded.
    func shutdownOMLX() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("shutdown_omlx"))
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        _ = try? await URLSession.shared.data(for: req)
    }

    // GET /mode -> current safety mode flags.
    /// The service's Full access flag (GET /mode), or nil when it can't be read.
    /// An unreadable flag is unknown, never "off": Wisp may still be acting
    /// without asking.
    func fullAccessState() async -> Bool? {
        let url = Self.baseURL.appendingPathComponent("mode")
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return Self.fullAccessFlag(data)
    }

    // POST /mode -> set full-access (no-confirmation) mode. Returns the flag the
    // service confirmed, or nil when the reply was lost or unreadable (the write
    // may still have happened).
    func requestFullAccess(_ on: Bool) async -> Bool? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("mode"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["full_access": on])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return Self.fullAccessFlag(data)
    }

    private static func fullAccessFlag(_ data: Data) -> Bool? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let flag = obj["full_access"] as? NSNumber,
              CFGetTypeID(flag) == CFBooleanGetTypeID() else { return nil }
        return flag.boolValue
    }

    // GET /idle_timeout -> minutes of inactivity before an idle model auto-unloads (0 = disabled).
    func idleTimeout() async -> Double {
        let url = Self.baseURL.appendingPathComponent("idle_timeout")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return 20 }
        return obj["idle_minutes"] as? Double ?? 20
    }

    // POST /idle_timeout -> set the auto-unload idle timeout in minutes (0 disables it).
    @discardableResult
    func setIdleTimeout(_ minutes: Double) async -> Double {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("idle_timeout"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["idle_minutes": minutes])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return minutes }
        return obj["idle_minutes"] as? Double ?? minutes
    }

    // GET /skills -> whether the "humanizer" style skill is enabled. Backend
    // already tracks this generically per-skill (service/skills/__init__.py's
    // `enabled` frontmatter field); this just reads the one skill Settings
    // exposes a toggle for.
    func humanizerEnabled() async -> Bool {
        let url = Self.baseURL.appendingPathComponent("skills")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = obj["skills"] as? [[String: Any]],
              let humanizer = list.first(where: { ($0["name"] as? String) == "humanizer" })
        else { return false }
        return humanizer["enabled"] as? Bool ?? false
    }

    // POST /skills/humanizer/enable -> toggle the humanizer style skill.
    // Returns the requested state on success, or re-reads the real state on
    // failure so the UI never shows a toggle position the backend rejected.
    @discardableResult
    func setHumanizerEnabled(_ on: Bool) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("skills/humanizer/enable"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["enabled": on])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["ok"] as? Bool == true
        else { return await humanizerEnabled() }
        return on
    }


    // GET /models -> (installed ids, role->model map)
    func models() async -> (installed: [String], roles: [String: String]) {
        let url = Self.baseURL.appendingPathComponent("models")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return ([], [:]) }
        let installed = obj["installed"] as? [String] ?? []
        let roles = (obj["roles"] as? [String: Any] ?? [:]).compactMapValues { $0 as? String }
        return (installed, roles)
    }

    // MARK: - Research

    struct ResearchPlanPayload {
        var title: String
        var objective: String
        var depth: String
        var subquestions: [String]
        var allowedDomains: [String] = []
        var blockedDomains: [String] = []

        var dictionary: [String: Any] {
            ["title": title, "objective": objective, "depth": depth,
             "subquestions": subquestions.enumerated().map {
                 ["id": "Q\($0.offset + 1)", "question": $0.element]
             },
             "allowed_domains": allowedDomains, "blocked_domains": blockedDomains]
        }
    }

    struct ResearchSource: Identifiable, Equatable {
        let id: String
        let title: String
        let url: String
        let domain: String
        let status: String
        let error: String
        let qualityClass: String
        let qualityReason: String

        static func parse(_ row: [String: Any]) -> ResearchSource? {
            guard let id = row["source_id"] as? String else { return nil }
            return ResearchSource(id: id, title: row["title"] as? String ?? id,
                                  url: row["url"] as? String ?? "",
                                  domain: row["domain"] as? String ?? "",
                                  status: row["status"] as? String ?? "found",
                                  error: row["error"] as? String ?? "",
                                  qualityClass: row["quality_class"] as? String ?? "",
                                  qualityReason: row["quality_reason"] as? String ?? "")
        }
    }

    struct ResearchCitation: Identifiable, Equatable {
        let id: String
        let number: Int
        let title: String
        let url: String
        let claim: String
        let quote: String
        let publishedAt: String
        let qualityClass: String
        let qualityReason: String

        static func parse(_ row: [String: Any]) -> ResearchCitation? {
            guard let evidenceId = row["evidence_id"] as? String else { return nil }
            return ResearchCitation(id: evidenceId, number: row["n"] as? Int ?? 0,
                title: row["title"] as? String ?? evidenceId,
                url: row["url"] as? String ?? "", claim: row["claim"] as? String ?? "",
                quote: row["quote"] as? String ?? "",
                publishedAt: row["published_at"] as? String ?? "",
                qualityClass: row["quality_class"] as? String ?? "",
                qualityReason: row["quality_reason"] as? String ?? "")
        }
    }

    struct ResearchContradiction: Identifiable, Equatable {
        let id: Int
        let subquestionId: String
        let description: String

        static func parse(_ row: [String: Any]) -> ResearchContradiction? {
            guard let id = row["id"] as? Int else { return nil }
            return ResearchContradiction(id: id,
                subquestionId: row["subquestion_id"] as? String ?? "",
                description: row["description"] as? String ?? "")
        }
    }

    struct ResearchSnapshot {
        let id: String
        let state: String
        let title: String
        let objective: String
        let depth: String
        let subquestions: [String]
        let report: String
        let sources: [ResearchSource]
        let citations: [ResearchCitation]
        let contradictions: [ResearchContradiction]
        let evidenceCount: Int
        let lastSeq: Int
        let error: String
        let stopReason: String
        let pinned: Bool
        let modelCalls: Int
        let allowedDomains: [String]
        let blockedDomains: [String]
        let createdAt: Double

        static func parse(_ obj: [String: Any]) -> ResearchSnapshot? {
            guard let id = obj["id"] as? String else { return nil }
            let plan = obj["plan"] as? [String: Any] ?? [:]
            let questions = (plan["subquestions"] as? [[String: Any]] ?? [])
                .compactMap { $0["question"] as? String }
            let sources = (obj["sources"] as? [[String: Any]] ?? [])
                .compactMap(ResearchSource.parse)
            let reportData = obj["report"] as? [String: Any] ?? [:]
            let citations = (reportData["sources"] as? [[String: Any]] ?? [])
                .compactMap(ResearchCitation.parse)
            let contradictions = (reportData["contradictions"] as? [[String: Any]] ?? [])
                .enumerated().compactMap { index, row -> ResearchContradiction? in
                    var row = row; row["id"] = index
                    return ResearchContradiction.parse(row)
                }
            return ResearchSnapshot(
                id: id, state: obj["state"] as? String ?? "",
                title: plan["title"] as? String ?? "Research",
                objective: plan["objective"] as? String ?? "",
                depth: plan["depth"] as? String ?? "standard",
                subquestions: questions,
                report: obj["report_md"] as? String ?? "",
                sources: sources, citations: citations, contradictions: contradictions,
                evidenceCount: obj["evidence_count"] as? Int ?? 0,
                lastSeq: obj["last_seq"] as? Int ?? 0,
                error: obj["error"] as? String ?? "",
                stopReason: obj["stop_reason"] as? String ?? "",
                pinned: obj["pinned"] as? Bool ?? false,
                modelCalls: obj["model_calls"] as? Int ?? 0,
                allowedDomains: plan["allowed_domains"] as? [String] ?? [],
                blockedDomains: plan["blocked_domains"] as? [String] ?? [],
                createdAt: obj["created_at"] as? Double ?? 0)
        }
    }

    func createResearchPlan(prompt: String, depth: String = "standard") async -> ResearchSnapshot? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 600
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["prompt": prompt, "depth": depth])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return ResearchSnapshot.parse(obj)
    }

    func updateResearchPlan(jobId: String, plan: ResearchPlanPayload) async -> ResearchSnapshot? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/plan"))
        req.httpMethod = "PATCH"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 30
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["plan": plan.dictionary])
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return ResearchSnapshot.parse(obj)
    }

    @discardableResult
    func researchAction(jobId: String, action: String,
                        body: [String: Any] = [:]) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/\(action)"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 120
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (_, response) = try? await URLSession.shared.data(for: req) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    func researchSnapshot(jobId: String) async -> ResearchSnapshot? {
        let url = Self.baseURL.appendingPathComponent("research/jobs/\(jobId)")
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return ResearchSnapshot.parse(obj)
    }

    func streamResearch(jobId: String, after: Int = 0,
                        onEvent: @escaping @Sendable (Event) -> Void) async {
        var components = URLComponents(
            url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/events"),
            resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "after", value: String(after))]
        var req = URLRequest(url: components.url!)
        req.timeoutInterval = 3600
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                onEvent(Event(type: "error", payload: ["message": "Research returned \(http.statusCode)."])); return
            }
            for try await line in bytes.lines {
                guard line.hasPrefix("data: ") else { continue }
                guard let data = String(line.dropFirst(6)).data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let type = obj["type"] as? String else { continue }
                onEvent(Event(type: type, payload: obj))
                if type == "done" || type == "error" { break }
            }
        } catch {
            if (error as? URLError)?.code == .cancelled { return }
            onEvent(Event(type: "error", payload: ["message": error.localizedDescription]))
        }
    }

    func updateResearchDomains(jobId: String, allowed: [String]? = nil,
                               blocked: [String]? = nil) async -> ResearchSnapshot? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/domains"))
        req.httpMethod = "PATCH"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 30
        var body: [String: Any] = [:]
        if let allowed { body["allowed_domains"] = allowed }
        if let blocked { body["blocked_domains"] = blocked }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return ResearchSnapshot.parse(obj)
    }

    @discardableResult
    func pinResearch(jobId: String, pinned: Bool) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/pin"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["pinned": pinned])
        guard let (_, response) = try? await URLSession.shared.data(for: req) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    @discardableResult
    func deleteResearch(jobId: String) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("research/jobs/\(jobId)"))
        req.httpMethod = "DELETE"
        guard let (_, response) = try? await URLSession.shared.data(for: req) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    func exportResearch(jobId: String, title: String) async -> URL? {
        let url = Self.baseURL.appendingPathComponent("research/jobs/\(jobId)/export.md")
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        let safe = title.replacingOccurrences(of: "[^A-Za-z0-9_-]+", with: "-",
                                               options: .regularExpression).prefix(60)
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
        let target = dir.appendingPathComponent("\(safe.isEmpty ? "wisp-research" : String(safe)).md")
        do { try data.write(to: target); return target } catch { return nil }
    }

    // MARK: - Smart Search

    /// Stream one search over `text`. Events arrive tier by tier (literal,
    /// lexical, semantic, answer) so the UI can render each as it lands rather
    /// than waiting on the slowest one. Keyed on "event", not "type", matching
    /// the /search endpoint.
    func search(text: String, query: String, wantAnswer: Bool,
                forceAnswer: Bool = false, forceGlobal: Bool = false,
                onEvent: @escaping @Sendable (Event) -> Void) async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("search"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Generous, but far short of a chat timeout: a search that hasn't
        // produced anything in a minute is broken, not slow.
        req.timeoutInterval = 60
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "text": text, "query": query, "want_answer": wantAnswer,
            "force_answer": forceAnswer, "force_global": forceGlobal,
        ])
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                onEvent(Event(type: "error",
                              payload: ["message": "Search returned \(http.statusCode)."]))
                return
            }
            for try await line in bytes.lines {
                guard line.hasPrefix("data: ") else { continue }
                guard let data = String(line.dropFirst(6)).data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let type = obj["event"] as? String else { continue }
                onEvent(Event(type: type, payload: obj))
                if type == "done" || type == "error" { break }
            }
        } catch {
            // A cancelled task is the normal path when the user keeps typing —
            // it must not surface as a failure.
            if (error as? URLError)?.code == .cancelled { return }
            onEvent(Event(type: "error", payload: ["message": error.localizedDescription]))
        }
    }

    /// Kick off chunking + embedding for `text` without waiting on an answer.
    /// For a novel-length document this is the expensive step (hundreds of
    /// chunks through the embedder); firing it the moment the panel opens
    /// means it's usually done — or well underway — by the time the user
    /// finishes typing, instead of paying that cost inline on the first query.
    func prewarmIndex(text: String) async {
        guard !text.isEmpty else { return }
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("search/prewarm"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 5   // the endpoint returns immediately; indexing continues server-side
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        _ = try? await URLSession.shared.data(for: req)
    }
}
