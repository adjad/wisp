import Foundation
import Dispatch

// The Python contract injects byte-for-byte production scan/capture/post bodies.
// Every collaborator here is inert; URLSession is a recording fake, not Foundation's.
enum Scan { case headers }
enum Enumeration: Equatable { case accounts([String: String]), unavailable, duplicateLabels }
enum AccountLabelCache { static func learn(accounts: [String: String]) {} }
enum WispClient { static let baseURL = URL(string: "http://127.0.0.1:8765")! }
struct RecordedTask { let perform: () -> Void; func resume() { perform() } }
final class URLSession {
    static let shared = URLSession()
    var requests: [[String: Any]] = []
    var acknowledged = false
    var pending: ((Data?, URLResponse?, Error?) -> Void)?
    func dataTask(with request: URLRequest,
                  completionHandler: @escaping (Data?, URLResponse?, Error?) -> Void = { _, _, _ in }) -> RecordedTask {
        RecordedTask {
            self.requests.append(["path": request.url!.path,
                "body": request.httpBody.flatMap { try? JSONSerialization.jsonObject(with: $0) } ?? NSNull()])
            if request.url!.path.hasSuffix("/identity/begin") { self.pending = completionHandler }
            else { completionHandler(nil, nil, nil) }
        }
    }
    func acknowledge(_ kind: String) {
        let callback = pending!; pending = nil
        acknowledged = true
        let data = kind == "bad_json" ? Data("{}".utf8) :
            try! JSONSerialization.data(withJSONObject: ["session": "native-flow-test-session", "generation": 1])
        callback(data, HTTPURLResponse(url: WispClient.baseURL, statusCode: kind == "bad_http" ? 500 : 200,
                                      httpVersion: nil, headerFields: nil), nil)
    }
}

// WISP_NATIVE_TRANSPORT

final class FakeHistory {
    let which: String
    init(_ which: String) { self.which = which }
    func capture() -> (generation: UInt64, lastKnownEmpty: Bool) { (0, which == "closed_empty") }
    func publishEmptyInventory(_ enqueue: () -> Void) { enqueue() }
    func replace(generation: UInt64?, enqueue: () -> Void) -> Bool { enqueue(); return true }
}
final class FakeDB {
    let which: String
    init(_ which: String) { self.which = which }
    func readHeadersAndHistory() -> (String, String)? { which == "closed_failed_index" ? nil : ("", "") }
}
final class Reader {
    let which: String
    let done = DispatchSemaphore(value: 0)
    let historyState: FakeHistory
    let dbReader: FakeDB
    let headerLimit = 200
    let identityScript = "identity"
    var events: [String] = []
    var runningChecks = 0
    var inFlight = false
    init(_ which: String) {
        self.which = which; historyState = FakeHistory(which); dbReader = FakeDB(which)
    }
    func claimScan(_ scan: Scan) -> Bool { if inFlight { return false }; inFlight = true; return true }
    func releaseScan(_ scan: Scan) { inFlight = false; done.signal() }
    func touch(_ event: String) {
        precondition(URLSession.shared.acknowledged, "Native work preceded capture acknowledgement")
        events.append(event)
    }
    func isMailRunning() -> Bool {
        touch("running"); runningChecks += 1
        if ["closed_index", "closed_failed_index", "closed_empty"].contains(which) { return false }
        if which == "closed_during_header" { return runningChecks < 2 }
        if which == "closed_between_accounts" { return runningChecks < 3 }
        if which == "closed_before_identity" { return runningChecks < 3 }
        return true
    }
    func accountNames() -> Enumeration {
        touch("accounts")
        switch which {
        case "empty": return .accounts([:])
        case "duplicate": return .duplicateLabels
        case "unavailable": return .unavailable
        case "partial", "closed_between_accounts": return .accounts(["one": "A", "two": "B"])
        default: return .accounts(["one": "A"])
        }
    }
    func run(_ script: String, tag: String) -> (String?, Int) {
        touch(tag)
        if tag == "identity" {
            if which == "identity_failed" { return (nil, -1743) }
            if which == "identity_malformed" { return ("me@example.test\nnot-an-address", 0) }
            if which == "identity_empty" { return ("", 0) }
            return ("me@example.test", 0)
        }
        return which == "failed" || (which == "partial" && script == "B") ? (nil, -1743) : ("synthetic header", 0)
    }
    func headerScript(account: String?, limit: Int) -> String { account ?? "unified" }
    func setLastAccountNames(_ names: [String]?) {}
    func incompleteMarker(account: String?, reason: String) -> String { "synthetic incomplete marker" }
    func mergeHeaderChunks(_ chunks: [String]) -> String { chunks.joined(separator: "\n") }
    func markHeadersCompleted() {}

    // WISP_NATIVE_READER_METHODS
}

@main struct NativeMailIdentityFlowChecks {
    static func main() throws {
        var results: [[String: Any]] = []
        for which in ["success", "empty", "duplicate", "unavailable", "failed", "partial",
                      "closed_index", "closed_failed_index", "closed_empty", "closed_during_header",
                      "closed_between_accounts", "closed_before_identity", "identity_failed", "identity_malformed", "identity_empty",
                      "bad_http", "bad_json"] {
            let session = URLSession.shared
            session.requests = []; session.acknowledged = false
            let reader = Reader(which)
            reader.sync()
            precondition(session.requests.count == 1 && reader.events.isEmpty)
            reader.sync() // Coalesce while begin is pending.
            precondition(session.requests.count == 1 && reader.inFlight)
            session.acknowledge(which)
            precondition(reader.done.wait(timeout: .now() + 5) == .success, "Scan claim stuck")
            precondition(!reader.inFlight)
            let terminal = session.requests.filter { ($0["path"] as! String).hasSuffix("/email/identity") }
            if which.hasPrefix("bad_") {
                precondition(reader.events.isEmpty && terminal.isEmpty && session.requests.count == 1)
            } else {
                precondition(terminal.count == 1)
                let body = terminal[0]["body"] as! [String: Any]
                let addresses = body["addresses"] as! [String]
                precondition(addresses == (which == "success" ? ["me@example.test"] : []))
                precondition(body["complete"] as! Bool == ["success", "empty", "identity_empty"].contains(which))
            }
            if which == "closed_during_header" {
                precondition(reader.events == ["running", "accounts", "running"])
            }
            if which == "closed_between_accounts" || which == "closed_before_identity" {
                precondition(reader.events == ["running", "accounts", "running", "headers", "running"])
                precondition(reader.runningChecks == 3)
                let legacy = session.requests.first { ($0["path"] as! String) == "/assistant/sync/emails" }!
                let payload = legacy["body"] as! [String: Any]
                let diagnostic = payload["diagnostics"] as! [String: Any]
                precondition(diagnostic["available"] as! Bool == (which == "closed_before_identity"))
            }
            results.append(["case": which, "requests": session.requests, "events": reader.events])
        }
        print(String(data: try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys]), encoding: .utf8)!)
    }
}
