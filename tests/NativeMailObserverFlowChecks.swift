import Foundation
import CoreFoundation
import Dispatch

// Only inert collaborators execute here. Python inserts the exact production
// scan/capture/order/post/account-observation and pure history-state bodies.
// This URLSession records requests; it cannot create a network connection.
enum WispClient { static let baseURL = URL(string: "http://127.0.0.1:8765")! }
final class SyncProgress {
    static let shared = SyncProgress()
    var mailHistoryFraction: Double?
    var mailHistoryLastSynced: Date?
}
enum AccountLabelCache {
    enum Enumeration: Equatable { case accounts([String: String]), unavailable, duplicateLabels }
    static var onLearn: (() -> Void)?
    static func learn(accounts: [String: String]) { onLearn?() }
    // WISP_OBSERVER_ACCOUNT_PARSER
}
struct RecordedTask { let perform: () -> Void; func resume() { perform() } }
final class URLSession {
    static let shared = URLSession()
    let lock = NSLock()
    var requests: [[String: Any]] = []
    var pending: [(String, Int, (Data?, URLResponse?, Error?) -> Void)] = []
    var held: [[String: Any]] = []
    var holdTerminal = false
    var session = "native-observer-session-one"
    var generation = 0
    var acknowledged = false
    func dataTask(with request: URLRequest,
                  completionHandler: @escaping (Data?, URLResponse?, Error?) -> Void = { _, _, _ in }) -> RecordedTask {
        RecordedTask {
            self.lock.lock(); defer { self.lock.unlock() }
            let path = request.url!.path
            let record: [String: Any] = ["path": path, "timeout": request.timeoutInterval,
                "body": request.httpBody.flatMap { try? JSONSerialization.jsonObject(with: $0) } ?? NSNull()]
            if path.hasSuffix("/identity/begin") {
                self.generation += 1
                self.requests.append(record.merging(["reply_session": self.session, "reply_generation": self.generation]) { _, new in new })
                self.pending.append((self.session, self.generation, completionHandler))
            } else if path.hasSuffix("/email/identity") && self.holdTerminal {
                self.held.append(record)
            } else {
                self.requests.append(record)
                completionHandler(nil, nil, nil)
            }
        }
    }
    func acknowledge(_ index: Int = 0, kind: String = "success") {
        lock.lock()
        let (session, generation, callback) = pending.remove(at: index)
        acknowledged = true
        lock.unlock()
        var payload: [String: Any] = ["session": session, "generation": generation]
        switch kind {
        case "session_empty": payload["session"] = ""
        case "session_short": payload["session"] = "x"
        case "session_19": payload["session"] = String(repeating: "x", count: 19)
        case "session_101": payload["session"] = String(repeating: "x", count: 101)
        case "session_number": payload["session"] = 42
        case "session_null": payload["session"] = NSNull()
        case "session_missing": payload.removeValue(forKey: "session")
        case "session_unicode_19": payload["session"] = "e" + String(repeating: "\u{0301}", count: 18)
        case "session_unicode_101": payload["session"] = "e" + String(repeating: "\u{0301}", count: 100)
        case "session_unicode_200":
            let text = String(repeating: "e\u{0301}", count: 100)
            precondition(text.count == 100 && text.unicodeScalars.count == 200)
            payload["session"] = text
        case "generation_true": payload["generation"] = true
        case "generation_false": payload["generation"] = false
        case "generation_zero": payload["generation"] = 0
        case "generation_negative": payload["generation"] = -1
        case "generation_string": payload["generation"] = "1"
        case "generation_null": payload["generation"] = NSNull()
        case "generation_missing": payload.removeValue(forKey: "generation")
        case "generation_fraction": payload["generation"] = 1.5
        case "generation_overflow": payload["generation"] = NSNumber(value: UInt64.max)
        default: break
        }
        let data = kind == "bad_json" ? Data("{}".utf8) :
            try! JSONSerialization.data(withJSONObject: payload)
        callback(["transport", "cancel"].contains(kind) ? nil : data,
            HTTPURLResponse(url: WispClient.baseURL, statusCode: kind == "bad_http" ? 500 : 200,
                            httpVersion: nil, headerFields: nil),
            ["cancel", "cancel_data"].contains(kind) ? NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled) : nil)
    }
    func flush() { lock.lock(); requests += held; held = []; lock.unlock() }
    func restart() {
        lock.lock(); defer { lock.unlock() }
        session = "native-observer-session-two"; generation = 0
        requests.append(["restart": session])
    }
    func reset() {
        lock.lock(); defer { lock.unlock() }
        precondition(pending.isEmpty && held.isEmpty)
        requests = []; generation = 0; acknowledged = false; holdTerminal = false
        session = "native-observer-session-one"
    }
}

// WISP_OBSERVER_PURE_HISTORY
// WISP_OBSERVER_CAPTURE_ORDER

final class FakeDB {
    weak var reader: Reader?
    func readHeadersAndHistory() -> (String, String)? { ("old headers", "old history") }
    func readTrustedHistory(forAccounts accounts: [String: String]) -> String? {
        let reader = reader!
        reader.touch("index"); reader.indexSeen.signal()
        if reader.which == "index_superseded" { reader.historyState.observe(["replacement": "Other"]) }
        return ["index", "index_superseded"].contains(reader.which) ? "trusted synthetic history" : nil
    }
}
final class Reader {
    var which: String
    let identityScript = "identity"
    let accountsScript = "accounts"
    let headerLimit = 200
    let historyCap = 2
    let historyBatchSize = 1
    let historyState = MailHistoryState()
    let dbReader = FakeDB()
    private let identityCaptureOrder = MailIdentityCaptureOrder()
    let scanLock = NSLock()
    var headersCompleted = true
    var waitingScans = 0
    var historyWaitScheduled = false
    var lastAccountNames: [String]? = ["A"]
    var headersInFlight = false
    var rawInFlight = false
    var historyInFlight = false
    var events: [String] = []
    var runningChecks = 0
    var retries = 0
    var contentCalls = 0
    let indexSeen = DispatchSemaphore(value: 0)
    init(_ which: String) {
        self.which = which; dbReader.reader = self
        AccountLabelCache.onLearn = { [weak self] in
            guard let self, self.which == "enumeration_superseded" else { return }
            self.historyState.observe(["replacement": "Other"])
        }
    }
    func touch(_ event: String) {
        precondition(URLSession.shared.acknowledged, "Native work preceded begin acknowledgement")
        events.append(event)
    }
    func isMailRunning() -> Bool {
        touch("running"); runningChecks += 1
        if which == "closed" { return false }
        if which == "closed_mid" { return runningChecks < 2 }
        if ["closed_between", "closed_batch"].contains(which) { return runningChecks != 3 }
        if which == "closed_reopened" { return runningChecks != 2 }
        if which == "closed_before_identity" { return runningChecks < 3 }
        return true
    }
    func run(_ script: String, tag: String) -> (String?, Int) {
        touch(tag)
        if tag == "accounts" {
            if which == "unavailable" { return (nil, -1743) }
            if which == "empty" { return ("", 0) }
            if which == "duplicate" { return ("one\u{01}A\ntwo\u{01}A", 0) }
            if ["partial", "two", "closed_between"].contains(which) { return ("one\u{01}A\ntwo\u{01}B", 0) }
            return ("one\u{01}A", 0)
        }
        if tag == "identity" {
            if which == "identity_failed" { return (nil, -1743) }
            if which == "identity_malformed" { return ("me@example.test\ninvalid", 0) }
            if which == "identity_empty" { return ("", 0) }
            if which == "expanded" { return ("me@example.test\nother@example.test", 0) }
            return (which == "changed" ? "other@example.test" : "me@example.test", 0)
        }
        contentCalls += 1
        if which == "body_failed" || (which == "partial" && script == "B") { return (nil, -1743) }
        if tag.hasPrefix("history") {
            if which == "walk_malformed" { return ("INVALID\nnot a batch", 0) }
            if which == "commit_superseded" { historyState.observe(["replacement": "Other"]) }
            return (["capped", "closed_batch"].contains(which) ? "CONTINUE\n" : "DONE\n", 0)
        }
        return (tag == "headers" ? "synthetic header" : "synthetic body", 0)
    }
    func rawLimit(accountCount: Int) -> Int { 2 }
    func rawScript(account: String?, limit: Int) -> String { account ?? "unified" }
    func headerScript(account: String?, limit: Int) -> String { account ?? "unified" }
    func historyBatchScript(account: String?, start: Int, count: Int, cutoff: Double?) -> String { account ?? "unified" }
    func mergeRawChunks(_ chunks: [String]) -> String { chunks.joined(separator: "\n") }
    func mergeHeaderChunks(_ chunks: [String]) -> String { chunks.joined(separator: "\n") }
    func incompleteMarker(account: String?, reason: String) -> String { "incomplete" }
    func scheduleHistoryRetry() { retries += 1 }
    func start(_ lane: String) {
        runningChecks = 0; contentCalls = 0
        if lane == "headers" { sync() }
        else if lane == "raw" { syncRaw() }
        else { syncHistory() }
    }
    func wait(_ lane: String) {
        let deadline = Date().addingTimeInterval(5)
        while true {
            scanLock.lock()
            let active = lane == "headers" ? headersInFlight : (lane == "raw" ? rawInFlight : historyInFlight)
            scanLock.unlock()
            if !active { break }
            precondition(Date() < deadline, "Stuck scan claim: \(lane) \(which)")
            Thread.sleep(forTimeInterval: 0.001)
        }
    }
    // WISP_OBSERVER_SCAN_BOOKKEEPING
    // WISP_OBSERVER_ACCOUNT_NAMES
    // WISP_OBSERVER_SCAN_METHODS
    // WISP_OBSERVER_HISTORY_HELPERS
    // WISP_OBSERVER_POST_METHODS
}

@main struct NativeMailObserverFlowChecks {
    static func main() throws {
        let session = URLSession.shared
        var results: [[String: Any]] = []
        func save(_ name: String, _ reader: Reader) {
            reader.scanLock.lock()
            precondition(!reader.headersInFlight && !reader.rawInFlight && !reader.historyInFlight && reader.waitingScans == 0)
            reader.scanLock.unlock()
            precondition(session.pending.isEmpty && session.held.isEmpty)
            results.append(["case": name, "requests": session.requests, "events": reader.events,
                            "retries": reader.retries, "content_calls": reader.contentCalls])
        }
        for lane in ["raw", "history"] {
            for which in ["success", "empty", "duplicate", "unavailable", "closed", "closed_mid", "closed_reopened",
                          "closed_before_identity", "closed_between", "closed_batch", "body_failed", "partial", "two", "identity_failed",
                          "identity_malformed", "identity_empty", "changed", "expanded", "bad_http", "bad_json", "transport",
                          "index", "index_superseded", "enumeration_superseded", "walk_malformed", "capped", "commit_superseded", "deferred"] {
                if lane == "raw" && ["closed_batch", "index", "index_superseded", "enumeration_superseded", "walk_malformed", "capped", "commit_superseded", "deferred"].contains(which) { continue }
                session.reset()
                let reader = Reader(which)
                if which == "deferred" { reader.headersCompleted = false }
                reader.start(lane)
                precondition(session.requests.count == 1 && reader.events.isEmpty)
                reader.start(lane)
                precondition(session.requests.count == 1)
                session.acknowledge(kind: which)
                reader.wait(lane)
                let terminals = session.requests.filter { ($0["path"] as? String)?.hasSuffix("/email/identity") == true }
                if ["bad_http", "bad_json", "transport"].contains(which) {
                    precondition(reader.events.isEmpty && terminals.isEmpty)
                    // The same lane can be claimed again after acknowledgement failure.
                    reader.which = "success"; reader.start(lane)
                    precondition(session.requests.count == 2 && reader.events.isEmpty)
                    session.acknowledge(); reader.wait(lane)
                } else {
                    precondition(terminals.count == 1)
                    let body = terminals[0]["body"] as! [String: Any]
                    let blocked = ["duplicate", "unavailable", "closed", "closed_mid", "closed_reopened", "closed_before_identity", "closed_between", "closed_batch", "identity_failed", "identity_malformed"].contains(which)
                    precondition(body["complete"] as! Bool == !blocked, "Wrong completeness: \(lane) \(which)")
                    let expected = blocked || ["empty", "identity_empty"].contains(which) ? [] : (which == "expanded" ? ["me@example.test", "other@example.test"] : [which == "changed" ? "other@example.test" : "me@example.test"])
                    precondition(body["addresses"] as! [String] == expected)
                    if ["closed_mid", "closed_reopened"].contains(which) {
                        precondition(!reader.events.contains("identity") && reader.contentCalls == 0)
                    }
                    if ["closed_between", "closed_batch"].contains(which) {
                        precondition(!reader.events.contains("identity") && reader.contentCalls == 1 && reader.runningChecks == 3)
                    }
                    if which == "closed" { precondition(reader.events == ["running"]) }
                    if which == "empty" { precondition(reader.events == ["running", "accounts"]) }
                    if which == "body_failed" { precondition(reader.events.contains("identity") && reader.contentCalls == 1) }
                    if which == "partial" {
                        precondition(reader.contentCalls == 2)
                        if lane == "raw" {
                            let raw = session.requests.compactMap { $0["body"] as? [String: Any] }.first { $0["raw"] != nil }!
                            let coverage = raw["raw_coverage"] as! [String: Any]
                            precondition(coverage["accounts"] as! [String] == ["A"] && coverage["failed_accounts"] as! [String] == ["B"])
                            precondition(coverage["complete"] as! Bool == false)
                        }
                    }
                    if which == "capped" { precondition(reader.contentCalls == 2) }
                    if ["enumeration_superseded", "index_superseded", "commit_superseded"].contains(which) {
                        precondition(!session.requests.contains { ($0["body"] as? [String: Any])?["history"] != nil })
                    }
                    if which == "enumeration_superseded" {
                        precondition(!reader.events.contains("index") && reader.contentCalls == 0 && reader.events.contains("identity"))
                    }
                    if which == "deferred" { precondition(reader.retries == 1 && reader.contentCalls == 0) }
                    if lane == "history" && ["index", "index_superseded"].contains(which) {
                        precondition(reader.events.contains("index") && reader.contentCalls == 0 && reader.events.contains("identity"))
                    }
                }
                save(lane + "_" + which, reader)
            }
        }
        // A corrupt ACK must never mutate accepted order, retire the actual
        // backend session, read native data or trigger header reacquisition.
        let invalidACKs = ["session_empty", "session_short", "session_19", "session_101", "session_number", "session_null", "session_missing", "session_unicode_19", "session_unicode_101", "session_unicode_200",
                           "generation_true", "generation_false", "generation_zero", "generation_negative", "generation_string", "generation_null",
                           "generation_missing", "generation_fraction", "generation_overflow"]
        for lane in ["headers", "raw", "history"] {
            for invalid in invalidACKs {
                session.reset()
                let reader = Reader("success")
                reader.start(lane); session.acknowledge(); reader.wait(lane)
                let before = reader.events
                reader.start(lane); session.acknowledge(kind: invalid); reader.wait(lane)
                precondition(reader.events == before, "Malformed ACK performed native work: \(lane) \(invalid)")
                precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }.count == 2,
                             "Malformed ACK retried: \(lane) \(invalid)")
                precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/email/identity") == true }.count == 1)
                for _ in 0..<2 {
                    let old = reader.events.count
                    reader.start(lane); session.acknowledge(); reader.wait(lane)
                    precondition(reader.events.count > old, "Valid session recovery poisoned: \(lane) \(invalid)")
                }
                precondition(reader.events.filter { $0 == "accounts" }.count == 3)
                precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/email/identity") == true }.count == 3)
                save("recover_" + lane + "_" + invalid, reader)

                session.reset()
                let initial = Reader("success")
                initial.start(lane); session.acknowledge(kind: invalid); initial.wait(lane)
                precondition(initial.events.isEmpty && session.requests.count == 1 && session.pending.isEmpty)
                initial.start(lane); session.acknowledge(); initial.wait(lane)
                precondition(initial.events.filter { $0 == "accounts" }.count == 1)
                save("invalid_" + lane + "_" + invalid, initial)
            }
            for length in [20, 100] {
                session.reset(); session.session = String(repeating: "s", count: length)
                let reader = Reader("success")
                reader.start(lane); session.acknowledge(); reader.wait(lane)
                precondition(reader.events.filter { $0 == "accounts" }.count == 1)
                save("valid_session_" + lane + "_" + String(length), reader)
            }
            for length in [20, 100] {
                session.reset(); session.session = "e" + String(repeating: "\u{0301}", count: length - 1)
                precondition(session.session.count == 1 && session.session.unicodeScalars.count == length)
                let reader = Reader("success")
                reader.start(lane); session.acknowledge(); reader.wait(lane)
                precondition(reader.events.filter { $0 == "accounts" }.count == 1)
                save("valid_unicode_session_" + lane + "_" + String(length), reader)
            }
            session.reset(); session.generation = Int.max - 1
            let reader = Reader("success")
            reader.start(lane); session.acknowledge(); reader.wait(lane)
            let terminal = session.requests.first { ($0["path"] as? String)?.hasSuffix("/email/identity") == true }!
            precondition((terminal["body"] as! [String: Any])["generation"] as! Int == Int.max)
            save("valid_max_generation_" + lane, reader)
        }
        // No account observation at all before launch header readiness.
        session.reset()
        let preflight = Reader("success"); preflight.lastAccountNames = nil; preflight.headersCompleted = false
        preflight.start("history")
        precondition(session.requests.isEmpty && preflight.events.isEmpty && preflight.retries == 1)
        save("history_preflight", preflight)

        // Reordered begin ACK: an older raw capture must not start new work after
        // a newer header/history capture has completed.
        for newer in ["headers", "history"] {
            session.reset()
            let reader = Reader("index")
            reader.start("raw"); reader.start(newer)
            session.acknowledge(1); reader.wait(newer)
            let events = reader.events
            session.acknowledge(); reader.wait("raw")
            precondition(reader.events == events && !events.contains("raw"))
            precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/email/identity") == true }.count == 1)
            save("reordered_begin_" + newer, reader)
        }
        // At cold start a stale header ACK gets one fresh capture within the
        // existing claimed scan, preserving readiness without a new scan/timer.
        for retry in ["success", "bad_http", "bad_json", "transport", "cancel", "cancel_data"] {
            session.reset()
            let reader = Reader("success")
            reader.headersCompleted = false; reader.lastAccountNames = nil
            reader.start("headers"); reader.start("raw")
            session.acknowledge(1); reader.wait("raw")
            precondition(!reader.headersCompleted && reader.lastAccountNames == nil)
            let before = reader.events
            session.acknowledge()
            precondition(session.pending.count == 1 && session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }.count == 3,
                         "Superseded initial header capture must reacquire its fence once")
            precondition(reader.events == before && reader.headersInFlight)
            session.acknowledge(kind: retry); reader.wait("headers")
            precondition(reader.headersCompleted == (retry == "success"))
            precondition(reader.events.contains("headers") == (retry == "success"))
            let begins = session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }
            precondition((begins[2]["timeout"] as! Double) <= (begins[0]["timeout"] as! Double))
            save("cold_header_retry_" + retry, reader)
            if retry != "success" {
                reader.start("headers"); session.acknowledge(); reader.wait("headers")
                precondition(reader.headersCompleted && reader.events.filter { $0 == "headers" }.count == 1)
                save("cold_header_recover_" + retry, reader)
            }
        }
        session.reset()
        let expired = Reader("success")
        expired.headersCompleted = false; expired.lastAccountNames = nil
        expired.start("headers"); expired.start("raw")
        session.acknowledge(1); expired.wait("raw")
        Thread.sleep(forTimeInterval: 8.01) // Exhaust the original capture budget.
        session.acknowledge(); expired.wait("headers")
        precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }.count == 2)
        precondition(!expired.headersCompleted && !expired.events.contains("headers"))
        save("cold_header_retry_expired", expired)
        expired.start("headers"); session.acknowledge(); expired.wait("headers")
        precondition(expired.headersCompleted)
        save("cold_header_recover_expired", expired)
        session.reset()
        let late = Reader("success")
        late.headersCompleted = false; late.lastAccountNames = nil
        late.start("headers"); late.start("raw")
        session.acknowledge(1); late.wait("raw")
        session.acknowledge() // Fresh retry requested while the original budget is live.
        precondition(session.pending.count == 1)
        Thread.sleep(forTimeInterval: 8.01)
        session.acknowledge(); late.wait("headers") // Valid JSON/2xx, late ACK.
        precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }.count == 3)
        precondition(!late.headersCompleted && !late.events.contains("headers"))
        save("cold_header_retry_late_ack", late)
        late.start("headers"); session.acknowledge(); late.wait("headers")
        precondition(late.headersCompleted)
        save("cold_header_recover_late_ack", late)
        for foreign in [false, true] {
            session.reset()
            let reader = Reader("success")
            reader.headersCompleted = false; reader.lastAccountNames = nil
            reader.start("headers"); reader.start("raw")
            session.acknowledge(1); reader.wait("raw")
            session.acknowledge() // Header's sole reacquisition is now pending.
            if foreign { session.restart() }
            reader.start("raw"); session.acknowledge(1); reader.wait("raw")
            session.acknowledge(); reader.wait("headers")
            precondition(session.requests.filter { ($0["path"] as? String)?.hasSuffix("/identity/begin") == true }.count == 4)
            precondition(!reader.headersCompleted && !reader.events.contains("headers"))
            save(foreign ? "cold_header_retry_foreign" : "cold_header_retry_superseded", reader)
            reader.start("headers"); session.acknowledge(); reader.wait("headers")
            precondition(reader.headersCompleted && reader.events.filter { $0 == "headers" }.count == 1)
            save(foreign ? "cold_header_recover_foreign" : "cold_header_recover_superseded", reader)
        }
        // Actual terminal transport delays across all entry kinds. Older success,
        // empty and failure cannot change a newer settled identity.
        for (older, newer) in [("headers", "raw"), ("raw", "history"), ("history", "headers")] {
            for (first, second) in [("success", "empty"), ("empty", "success"), ("closed", "success"), ("success", "closed")] {
                session.reset()
                let reader = Reader(first)
                session.holdTerminal = true
                reader.start(older); session.acknowledge(); reader.wait(older)
                precondition(session.held.count == 1)
                session.holdTerminal = false; reader.which = second
                reader.start(newer); session.acknowledge(); reader.wait(newer)
                session.flush()
                save("delayed_" + older + "_" + newer + "_" + first + "_" + second, reader)
            }
        }
        // A history walk yields to the existing pending raw slot, then resumes
        // after the stale raw ACK releases it. History never waits on itself.
        session.reset()
        let yielding = Reader("success")
        yielding.start("raw"); yielding.start("history")
        session.acknowledge(1)
        precondition(yielding.indexSeen.wait(timeout: .now() + 5) == .success)
        precondition(yielding.contentCalls == 0)
        session.acknowledge(); yielding.wait("raw"); yielding.wait("history")
        precondition(yielding.events.contains("history") && !yielding.events.contains("raw"))
        save("history_yields_to_raw", yielding)

        // Independent fresh observations preserve same-account continuity.
        session.reset()
        let continuous = Reader("success")
        for lane in ["raw", "history", "headers"] {
            continuous.start(lane); session.acknowledge(); continuous.wait(lane)
        }
        save("continuous_fresh", continuous)

        // Server restart admits new-session g1 after old-session g2 and rejects
        // a late old-session begin ACK without new native work.
        session.reset()
        let restarted = Reader("index")
        restarted.start("raw"); restarted.start("headers")
        session.acknowledge(1); restarted.wait("headers")
        session.restart()
        restarted.start("history"); session.acknowledge(1); restarted.wait("history")
        let events = restarted.events
        session.acknowledge(); restarted.wait("raw")
        precondition(restarted.events == events)
        save("restart_reordered", restarted)
        print(String(data: try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
