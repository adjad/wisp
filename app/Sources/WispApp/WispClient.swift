import Foundation

// Tracks in-flight writes to the backend that persist to ~/.moe/config.yaml
// (role/model changes, Super Model choice, …). Quitting Wisp stops the
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

// Streams events from the local Wisp service (FastAPI, :8765 by default —
// see WispConfig for the host/port and their environment overrides).
final class WispClient {
    static let baseURL = WispConfig.backendBaseURL

    struct Event {
        let type: String
        let payload: [String: Any]
        func str(_ k: String) -> String { payload[k] as? String ?? "" }
        func bool(_ k: String) -> Bool { payload[k] as? Bool ?? false }
        func int(_ k: String) -> Int { payload[k] as? Int ?? 0 }
    }

    struct AirCompute {
        var enabled: Bool
        var baseURL: String
        var timeoutMs: Int
        var routing: Bool
        var summaries: Bool
        var draft: Bool
        var status: String
        var statusMessage: String
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
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let c = obj["commitment"] as? [String: Any] else { return nil }
        return Commitment.parse(c)
    }

    // GET /assistant/upcoming -> next N days.
    func assistantUpcoming(days: Int = 7) async -> [Commitment] {
        let url = Self.baseURL.appendingPathComponent("assistant/upcoming")
            .appending(queryItems: [URLQueryItem(name: "days", value: String(days))])
        guard let (data, _) = try? await WispSession.shared.data(from: url),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let c = obj["commitment"] as? [String: Any] else { return nil }
        return Commitment.parse(c)
    }

    // POST /assistant/daily_summary -> combined calendar+email brief (full text).
    func dailySummary() async -> String? {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/daily_summary"))
        req.httpMethod = "POST"
        req.timeoutInterval = 120
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let text = obj["text"] as? String else { return nil }
        return text
    }

    // GET/POST /assistant/summary_schedule -> the 8am(AM) / 8pm(PM) digest time.
    func summarySchedule() async -> String {
        let url = Self.baseURL.appendingPathComponent("assistant/summary_schedule")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return period }
        return obj["period"] as? String ?? period
    }

    // POST /assistant/profile/build -> scan synced Mail/Messages/Notes/
    // Calendar and (re)build the profile (service/memory/profile.py, pinned
    // to gpt-oss). Several local-model calls, so this can take a while —
    // callers should run it off the main flow and report completion async
    // (see AppDelegate.buildProfile, which posts a notification).
    @discardableResult
    func buildProfile() async -> (ok: Bool, summary: String) {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/profile/build"))
        req.httpMethod = "POST"
        req.timeoutInterval = 600
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (false, "Couldn't reach Wisp's backend.") }
        if obj["ok"] as? Bool == true {
            let sources = obj["sources"] as? [String: Any] ?? [:]
            let names = sources.keys.sorted().joined(separator: ", ")
            return (true, names.isEmpty ? "Profile updated." : "Profile updated from \(names).")
        }
        return (false, obj["reason"] as? String ?? "Nothing to build yet.")
    }

    // GET /assistant/profile -> the current profile text (empty until
    // buildProfile has run at least once), for the debug-export download.
    func fetchProfile() async -> String? {
        let url = Self.baseURL.appendingPathComponent("assistant/profile")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return obj["text"] as? String
    }

    struct ProfileSourceMeta {
        let name: String
        let lastScanned: Date?
    }

    // Same /assistant/profile call as fetchProfile, just reading `meta`
    // instead of `text` — per-source last-scanned bookkeeping (see
    // service/memory/profile.py's build_profile) for the Settings
    // "Automation" section. Fixed source order, not whatever keys happen to
    // be in the dict, so the list doesn't reshuffle between refreshes.
    func fetchProfileMeta() async -> (sources: [ProfileSourceMeta], missing: [String]) {
        let url = Self.baseURL.appendingPathComponent("assistant/profile")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let meta = obj["meta"] as? [String: Any]
        else { return ([], []) }
        let sources = ["messages", "email", "notes", "calendar"].map { name -> ProfileSourceMeta in
            let entry = meta[name] as? [String: Any]
            let ts = entry?["last_scanned"] as? Double
            return ProfileSourceMeta(name: name, lastScanned: ts.map(Date.init(timeIntervalSince1970:)))
        }
        return (sources, meta["missing"] as? [String] ?? [])
    }

    // GET/POST /thinking_level -> gpt-oss's reasoning_effort
    // (auto/low/medium/high), applied to every completion (general chat,
    // reasoning, agent loop). "auto" (the default) has the router pick effort
    // per-request instead of pinning every request to one fixed level.
    func thinkingLevel() async -> String {
        let url = Self.baseURL.appendingPathComponent("thinking_level")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return "auto" }
        return obj["level"] as? String ?? "auto"
    }

    @discardableResult
    func setThinkingLevel(_ level: String) async -> String {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("thinking_level"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["level": level])
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return level }
        return obj["level"] as? String ?? level
    }

    // GET /assistant/events (SSE) -> reminders + `changed` pings.
    func assistantEvents(onEvent: @escaping @Sendable (Event) -> Void) async {
        let req = URLRequest(url: Self.baseURL.appendingPathComponent("assistant/events"))
        do {
            let (bytes, _) = try await WispSession.shared.bytes(for: req)
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
                  onEvent: @escaping @Sendable (Event) -> Void) async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("agent"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Default URLRequest timeout is 60s of no data. Hard reasoning prompts can
        // run silent (no SSE traffic) for well over a minute — a genuinely long
        // answer looked identical to a hung request. 10 min covers the slowest
        // model swap + generation while still failing visibly if truly stuck.
        // Super Model runs a large model on the hardest, longest agentic work
        // (and now self-tests its code, adding run/verify round-trips), so it
        // gets 30 min before we call it stuck.
        req.timeoutInterval = SuperModelState.shared.active ? 1800 : 600
        var body: [String: Any] = ["prompt": prompt]
        if !sessionId.isEmpty { body["session_id"] = sessionId }
        if let image { body["image"] = image }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (bytes, response) = try await WispSession.shared.bytes(for: req)
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
        _ = try? await WispSession.shared.data(for: req)
    }

    // Unload all resident models from oMLX to free memory/power.
    func unloadAll() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("unload_all"))
        req.httpMethod = "POST"
        _ = try? await WispSession.shared.data(for: req)
    }

    // Unload just the heavy agent model (gpt-oss) — keeps the small always-on
    // router warm. Used when dismissing to menu-bar-only (the X button).
    func unloadAgent() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("unload_agent"))
        req.httpMethod = "POST"
        req.timeoutInterval = 15
        _ = try? await WispSession.shared.data(for: req)
    }

    // Stop the oMLX engine entirely (frees its ~2GB baseline). Used on quit.
    func shutdownOMLX() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("shutdown_omlx"))
        req.httpMethod = "POST"
        req.timeoutInterval = 5
        _ = try? await WispSession.shared.data(for: req)
    }

    // GET /mode -> current safety mode flags.
    func mode() async -> (readOnly: Bool, fullAccess: Bool) {
        let url = Self.baseURL.appendingPathComponent("mode")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return (true, false) }
        return (obj["read_only"] as? Bool ?? true, obj["full_access"] as? Bool ?? false)
    }

    // GET /idle_timeout -> minutes of inactivity before an idle model auto-unloads (0 = disabled).
    func idleTimeout() async -> Double {
        let url = Self.baseURL.appendingPathComponent("idle_timeout")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
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
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return minutes }
        return obj["idle_minutes"] as? Double ?? minutes
    }

    // GET /super_model -> current Super Model state (never set by the router —
    // only ever flipped on by an explicit user action, see AppDelegate).
    struct SuperModelInfo {
        let active: Bool
        let model: String
        let installed: [String]
        // Starred in oMLX's own settings — read straight from its config
        // file server-side, so this is populated even when oMLX's server
        // subprocess isn't currently running (unlike `installed`, above).
        let favorites: [String]
    }

    func superModelInfo() async -> SuperModelInfo {
        let url = Self.baseURL.appendingPathComponent("super_model")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return SuperModelInfo(active: false, model: "", installed: [], favorites: []) }
        return SuperModelInfo(active: obj["active"] as? Bool ?? false,
                               model: obj["model"] as? String ?? "",
                               installed: obj["installed"] as? [String] ?? [],
                               favorites: obj["favorites"] as? [String] ?? [])
    }

    // POST /super_model/model -> persist which model Super Model uses.
    @discardableResult
    func setSuperModelName(_ model: String) async -> String {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("super_model/model"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["model": model])
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return model }
        return obj["model"] as? String ?? model
    }

    // POST /super_model/toggle -> engage/disengage the override. Engaging is
    // ONLY ever called right after AppQuitter.quitOtherApps() on the Swift
    // side — this endpoint itself doesn't quit anything.
    @discardableResult
    func setSuperModelActive(_ active: Bool) async -> Bool {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("super_model/toggle"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["active": active])
        // Turning ON evicts whatever's resident and loads the Super Model
        // target synchronously server-side (see /super_model/toggle) — a cold
        // oMLX start plus a large model load can take a while, so this needs
        // real headroom beyond URLSession's 60s default, not just the usual
        // couple-second settings round trip.
        req.timeoutInterval = 120
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return active }
        return obj["active"] as? Bool ?? active
    }

    // GET /models -> (installed ids, role->model map)
    func models() async -> (installed: [String], roles: [String: String]) {
        let url = Self.baseURL.appendingPathComponent("models")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return ([], [:]) }
        let installed = obj["installed"] as? [String] ?? []
        let roles = (obj["roles"] as? [String: Any] ?? [:]).compactMapValues { $0 as? String }
        return (installed, roles)
    }

    func airCompute() async -> AirCompute {
        let url = Self.baseURL.appendingPathComponent("air_compute")
        guard let (data, _) = try? await WispSession.shared.data(from: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return Self.defaultAirCompute() }
        return Self.parseAirCompute(obj)
    }

    func checkAirCompute() async -> AirCompute {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("air_compute/check"))
        req.httpMethod = "POST"
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return Self.defaultAirCompute() }
        return Self.parseAirCompute(obj)
    }

    @discardableResult
    func setAirCompute(enabled: Bool? = nil, baseURL: String? = nil,
                       routing: Bool? = nil, summaries: Bool? = nil,
                       draft: Bool? = nil) async -> AirCompute {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("air_compute"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = [:]
        if let enabled { body["enabled"] = enabled }
        if let baseURL { body["base_url"] = baseURL }
        var caps: [String: Bool] = [:]
        if let routing { caps["routing"] = routing }
        if let summaries { caps["summaries"] = summaries }
        if let draft { caps["draft"] = draft }
        if !caps.isEmpty { body["capabilities"] = caps }
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, _) = try? await WispSession.shared.data(for: req),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return Self.defaultAirCompute() }
        return Self.parseAirCompute(obj)
    }

    private static func parseAirCompute(_ obj: [String: Any]) -> AirCompute {
        let cfg = obj["config"] as? [String: Any] ?? [:]
        let caps = cfg["capabilities"] as? [String: Any] ?? [:]
        let status = obj["status"] as? [String: Any] ?? [:]
        return AirCompute(
            enabled: cfg["enabled"] as? Bool ?? false,
            baseURL: cfg["base_url"] as? String ?? WispConfig.airBaseURLDefault,
            timeoutMs: cfg["timeout_ms"] as? Int ?? 700,
            routing: caps["routing"] as? Bool ?? true,
            summaries: caps["summaries"] as? Bool ?? false,
            draft: caps["draft"] as? Bool ?? false,
            status: status["state"] as? String ?? "not_checked",
            statusMessage: status["message"] as? String ?? ""
        )
    }

    private static func defaultAirCompute() -> AirCompute {
        AirCompute(enabled: false, baseURL: WispConfig.airBaseURLDefault,
                   timeoutMs: 700, routing: true, summaries: false, draft: false,
                   status: "not_checked", statusMessage: "")
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
            let (bytes, response) = try await WispSession.shared.bytes(for: req)
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

    /// Preload the embedder so the first ⌘⇧F doesn't pay a cold model load.
    func warmSearch() async {
        var req = URLRequest(url: Self.baseURL.appendingPathComponent("search/warm"))
        req.httpMethod = "POST"
        req.timeoutInterval = 240
        _ = try? await WispSession.shared.data(for: req)
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
        _ = try? await WispSession.shared.data(for: req)
    }
}
