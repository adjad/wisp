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
    static let baseURL = URL(string: "http://127.0.0.1:8765")!

    struct Event {
        let type: String
        let payload: [String: Any]
        func str(_ k: String) -> String { payload[k] as? String ?? "" }
        func bool(_ k: String) -> Bool { payload[k] as? Bool ?? false }
        func int(_ k: String) -> Int { payload[k] as? Int ?? 0 }
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
    func dailySummary() async -> String? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/daily_summary"))
        req.httpMethod = "POST"
        req.timeoutInterval = 240
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let text = obj["text"] as? String else { return nil }
        return text
    }

    // GET/POST /assistant/summary_schedule -> the 8am(AM) / 8pm(PM) digest time.
    func summarySchedule() async -> String {
        let url = Self.baseURL.appendingPathComponent("assistant/summary_schedule")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return "AM" }
        return obj["period"] as? String ?? "AM"
    }

    @discardableResult
    func setSummarySchedule(_ period: String) async -> String {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/summary_schedule"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["period": period])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return period }
        return obj["period"] as? String ?? period
    }

    // GET /assistant/events (SSE) -> reminders + `changed` pings.
    func assistantEvents(onEvent: @escaping @Sendable (Event) -> Void) async {
        let req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/events"))
        do {
            let (bytes, _) = try await URLSession.shared.bytes(for: req)
            for try await line in bytes.lines {
                guard line.hasPrefix("data: ") else { continue }
                guard let data = line.dropFirst(6).data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let type = obj["type"] as? String else { continue }
                onEvent(Event(type: type, payload: obj))
            }
        } catch {
            // Stream dropped (backend restart, etc.) — the caller re-subscribes.
        }
    }

    // POST /agent and yield decoded SSE events to `onEvent` (called on a background task).
    // Pass `sessionId` to continue a conversation; the server loads & budgets the
    // history. Omit it (empty) to start a fresh chat — the new id arrives in the
    // first `session` event.
    func runAgent(prompt: String, image: String?, sessionId: String = "",
                  debug: Bool = false,
                  onEvent: @escaping @Sendable (Event) -> Void) async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("agent"))
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
        if !sessionId.isEmpty { body["session_id"] = sessionId }
        if let image { body["image"] = image }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (bytes, response) = try await URLSession.shared.bytes(for: req)
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                onEvent(Event(type: "error",
                              payload: ["message": "Server returned \(http.statusCode).",
                                        "dropped": true]))
                return
            }
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
            // The stream can close cleanly with no terminal event — most often a
            // stale keep-alive socket from URLSession's pool that yields immediate
            // EOF. Without this, the UI sat in "routing…" forever and the user had
            // to retype. Emit a terminal so the model can recover (see handle()).
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
                 scope: String = "once") async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("agent/approve"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "session_id": sessionId, "action_id": actionId,
            "approved": approved, "scope": scope,
        ])
        _ = try? await URLSession.shared.data(for: req)
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

    // Stop the oMLX engine entirely (frees its ~2GB baseline). Used on quit.
    func shutdownOMLX() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("shutdown_omlx"))
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        _ = try? await URLSession.shared.data(for: req)
    }

    // GET /mode -> current safety mode flags.
    func mode() async -> (readOnly: Bool, fullAccess: Bool) {
        let url = Self.baseURL.appendingPathComponent("mode")
        guard let (data, _) = try? await URLSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (true, false) }
        return (obj["read_only"] as? Bool ?? true, obj["full_access"] as? Bool ?? false)
    }

    // POST /mode -> set full-access (no-confirmation) mode. Returns the new flags.
    @discardableResult
    func setFullAccess(_ on: Bool) async -> (readOnly: Bool, fullAccess: Bool) {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("mode"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["full_access": on])
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (true, false) }
        return (obj["read_only"] as? Bool ?? true, obj["full_access"] as? Bool ?? false)
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
