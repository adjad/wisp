// Compile with -DWISP_NOTCH_EVALUATION_CHECKS and app/Sources/WispApp/NotchEvaluation.swift,
// WispClient.swift and AgentSend.swift. Exercises the evaluation build's
// config validation, source admission, request admission, response identity,
// redirect refusal, single-shot failure, native sink denial, receipt health
// and hash-only receipts. Forwarded requests are answered by an in-process
// stub backend: nothing reaches a socket.
import AppKit
import Darwin
import Foundation

/// The stub evaluation backend behind the guard. Records what it was sent.
final class StubBackend: URLProtocol {
    nonisolated(unsafe) static var headers: [String: String] = [:]
    nonisolated(unsafe) static var seen: [URLRequest] = []
    nonisolated(unsafe) static var redirect = false
    enum Mode { case stream, closeBeforeHeaders, hang }
    nonisolated(unsafe) static var mode = Mode.stream

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.seen.append(request)
        let url = request.url!
        if Self.mode == .closeBeforeHeaders {
            client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost))
            return
        }
        if Self.redirect {
            let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: nil,
                                           headerFields: ["Location": "https://example.com/"])!
            client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: URL(string: "https://example.com/")!),
                                redirectResponse: response)
            return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil,
                                       headerFields: Self.headers.merging(["Content-Type": "text/event-stream"]) { a, _ in a })!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if Self.mode == .hang {
            client?.urlProtocol(self, didLoad: Data("data: {\"type\":\"status\"}\n\n".utf8))
            return  // never finishes; the caller cancels
        }
        for line in [#"{"type":"session","id":"abc123"}"#, #"{"type":"text","text":"SYNTHETIC"}"#, #"{"type":"done"}"#] {
            client?.urlProtocol(self, didLoad: Data("data: \(line)\n\n".utf8))
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@main
struct NotchEvaluationChecks {
    nonisolated(unsafe) static var passed = 0

    static func check(_ condition: Bool, _ label: String) {
        guard condition else { print("FAIL: \(label)"); exit(1) }
        passed += 1
    }

    /// Every NSWorkspace open/launch is refused and recorded. Each method is
    /// confirmed replaced BEFORE it is called, so a broken guard fails here
    /// instead of opening anything. The targets do not exist either way.
    static func workspaceGuardChecks() {
        let ledger = NotchEvaluationLedger(directory: nil)
        NotchEvaluationWorkspaceGuard.install(ledger: ledger)
        check(NotchEvaluationWorkspaceGuard.isInstalled, "the workspace guard installs")
        for (name, _) in NotchEvaluationWorkspaceGuard.guarded {
            guard NotchEvaluationWorkspaceGuard.isGuarded(name) else {
                print("FAIL: \(name) is not guarded; not calling it"); exit(1)
            }
        }
        check(NotchEvaluationWorkspaceGuard.installed.count >= 4, "the four current open APIs are guarded")
        let ws = NSWorkspace.shared
        let url = URL(string: "https://wisp-evaluation.invalid/never")!
        let app = URL(fileURLWithPath: "/nonexistent/WispEvaluationNever.app")
        let file = "/nonexistent/wisp-evaluation-never.txt"
        let configuration = NSWorkspace.OpenConfiguration()
        var completions: [Bool] = []
        func recorded(_ selector: String) -> Bool {
            ledger.entries.contains { $0["kind"] as? String == "blocked" && $0["selector"] as? String == selector }
        }
        check(!ws.open(url) && recorded("openURL:"), "open(_:) is refused and recorded")
        ws.open(url, configuration: configuration) { app, error in completions.append(app == nil && error != nil) }
        check(recorded("openURL:configuration:completionHandler:"), "open(_:configuration:) is recorded")
        ws.openApplication(at: app, configuration: configuration) { app, error in completions.append(app == nil && error != nil) }
        check(recorded("openApplicationAtURL:configuration:completionHandler:"), "openApplication(at:) is recorded")
        ws.open([url], withApplicationAt: app, configuration: configuration) { app, error in
            completions.append(app == nil && error != nil)
        }
        check(recorded("openURLs:withApplicationAtURL:configuration:completionHandler:"), "open(_:withApplicationAt:) is recorded")
        check(completions == [true, true, true], "each completion is called with an error and no app")
        check(!ws.launchApplication("WispEvaluationNever") && recorded("launchApplication:"), "launchApplication is refused")
        check(!ws.openFile(file) && recorded("openFile:"), "openFile is refused")
        check(!ws.openFile(file, withApplication: "WispEvaluationNever") && recorded("openFile:withApplication:"),
              "openFile(_:withApplication:) is refused")
        check(!ws.openFile(file, withApplication: nil, andDeactivate: true)
              && recorded("openFile:withApplication:andDeactivate:"), "openFile(_:withApplication:andDeactivate:) is refused")
        var launched: NSRunningApplication?
        var thrown: Error?
        do { launched = try ws.launchApplication(at: app, options: [], configuration: [:]) } catch { thrown = error }
        check(launched == nil && thrown != nil && recorded("launchApplicationAtURL:options:configuration:error:"),
              "launchApplication(at:options:) throws and is recorded")
        check(!ws.open([url], withAppBundleIdentifier: "com.example.never", options: [], additionalEventParamDescriptor: nil,
                       launchIdentifiers: nil)
              && recorded("openURLs:withAppBundleIdentifier:options:additionalEventParamDescriptor:launchIdentifiers:"),
              "open(_:withAppBundleIdentifier:) is refused")
        check(!ws.launchApplication(withBundleIdentifier: "com.example.never", options: [], additionalEventParamDescriptor: nil,
                                    launchIdentifier: nil)
              && recorded("launchAppWithBundleIdentifier:options:additionalEventParamDescriptor:launchIdentifier:"),
              "launchApplication(withBundleIdentifier:) is refused")
        check(!ws.selectFile(file, inFileViewerRootedAtPath: "") && recorded("selectFile:inFileViewerRootedAtPath:"),
              "selectFile is refused")
        ws.activateFileViewerSelecting([URL(fileURLWithPath: file)])
        check(recorded("activateFileViewerSelectingURLs:"), "activateFileViewerSelecting is recorded")
        // The older and the file-changing entry points.
        launched = nil; thrown = nil
        do { launched = try ws.open(url, options: [], configuration: [:]) } catch { thrown = error }
        check(launched == nil && thrown != nil && recorded("openURL:options:configuration:error:"),
              "open(_:options:configuration:) throws and is recorded")
        launched = nil; thrown = nil
        do { launched = try ws.open([url], withApplicationAt: app, options: [], configuration: [:]) } catch { thrown = error }
        check(launched == nil && thrown != nil && recorded("openURLs:withApplicationAtURL:options:configuration:error:"),
              "open(_:withApplicationAt:options:configuration:) throws and is recorded")
        check(!ws.showSearchResults(forQueryString: "wisp-evaluation-never") && recorded("showSearchResultsForQueryString:"),
              "showSearchResults is refused")
        check(!ws.setIcon(nil, forFile: file, options: []) && recorded("setIcon:forFile:options:"), "setIcon is refused")
        var trashed: [Bool] = []
        ws.recycle([URL(fileURLWithPath: file)]) { moved, error in trashed.append(moved.isEmpty && error != nil) }
        check(recorded("recycleURLs:completionHandler:"), "recycle is recorded")
        ws.duplicate([URL(fileURLWithPath: file)]) { copied, error in trashed.append(copied.isEmpty && error != nil) }
        check(recorded("duplicateURLs:completionHandler:"), "duplicate is recorded")
        check(trashed == [true, true], "recycle and duplicate complete with an error and nothing moved")
        // Deprecated past the Swift overlay: called through the installed IMP.
        func imp(_ name: String) -> IMP? {
            class_getInstanceMethod(NSWorkspace.self, NSSelectorFromString(name)).map(method_getImplementation)
        }
        if let target = imp("openTempFile:") {
            let call = unsafeBitCast(target, to: (@convention(c) (AnyObject, Selector, AnyObject?) -> Bool).self)
            check(!call(ws, NSSelectorFromString("openTempFile:"), file as NSString) && recorded("openTempFile:"),
                  "openTempFile is refused")
        }
        if let target = imp("openFile:fromImage:at:inView:") {
            let call = unsafeBitCast(target, to: (@convention(c) (AnyObject, Selector, AnyObject?, AnyObject?, NSPoint, AnyObject?) -> Bool).self)
            check(!call(ws, NSSelectorFromString("openFile:fromImage:at:inView:"), file as NSString, nil, .zero, nil)
                  && recorded("openFile:fromImage:at:inView:"), "openFile(_:fromImage:at:inView:) is refused")
        }
        if let target = imp("performFileOperation:source:destination:files:tag:") {
            let call = unsafeBitCast(target, to: (@convention(c) (AnyObject, Selector, AnyObject?, AnyObject?, AnyObject?,
                                                                  AnyObject?, UnsafeMutablePointer<Int>?) -> Bool).self)
            var tag = 0
            check(!call(ws, NSSelectorFromString("performFileOperation:source:destination:files:tag:"), "recycle" as NSString,
                        "/nonexistent" as NSString, "" as NSString, ["wisp-evaluation-never.txt"] as NSArray, &tag)
                  && tag == -1 && recorded("performFileOperation:source:destination:files:tag:"),
                  "performFileOperation is refused")
        }
    }

    /// The startup probe: a write the profile would deny must be denied.
    /// Unconfined here, so a write that succeeds is reported and cleaned up.
    static func sandboxProbeChecks(root: URL) {
        let before = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        check(NotchEvaluationSandboxProbe.problem(directory: root.path) == "a write outside the owned root succeeded",
              "an unconfined probe write is reported")
        check(((try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []) == before, "the probe file is removed")
        check(NotchEvaluationSandboxProbe.problem(directory: root.path + "/missing")?.contains("not a denial") == true,
              "a probe that fails for another reason is not taken as confinement")
        let locked = root.appendingPathComponent("locked")
        try? FileManager.default.createDirectory(at: locked, withIntermediateDirectories: true)
        chmod(locked.path, 0o500)
        defer { chmod(locked.path, 0o700) }
        check(NotchEvaluationSandboxProbe.problem(directory: locked.path) == nil, "a denied probe write passes")
    }

    /// The input socket must be a 0600 socket owned by this user.
    static func socketModeChecks(root: URL) {
        var info = stat()
        info.st_mode = S_IFSOCK | 0o600
        info.st_uid = getuid()
        check(NotchEvaluationInput.socketProblem(info) == nil, "a 0600 own socket is fit")
        info.st_mode = S_IFSOCK | 0o666
        check(NotchEvaluationInput.socketProblem(info) == "mode 666, not 600", "a 0666 socket is refused")
        info.st_mode = S_IFSOCK | 0o660
        check(NotchEvaluationInput.socketProblem(info) != nil, "a group-writable socket is refused")
        info.st_mode = S_IFREG | 0o600
        check(NotchEvaluationInput.socketProblem(info) == "not a socket", "a regular file is refused")
        info.st_mode = S_IFSOCK | 0o600
        info.st_uid = getuid() &+ 1
        check(NotchEvaluationInput.socketProblem(info) == "not owned by this user", "another user's socket is refused")
        check(NotchEvaluationInput.socketProblem(path: root.path + "/absent.sock") == "missing", "a missing socket is refused")
        // A real bind, where this host allows one (the CI fixture sandbox refuses it).
        let path = root.appendingPathComponent("s.sock").path
        guard let listener = try? NotchEvaluationInput.bindListener(path: path) else {
            print("note: socket bind unavailable here; the real-bind mode check did not run")
            return
        }
        defer { close(listener); unlink(path) }
        check(NotchEvaluationInput.socketProblem(path: path) == nil, "the bound input socket is 0600")
    }

    static func configJSON(_ overrides: [String: Any] = [:], root: String) -> Data {
        var object: [String: Any] = ["run_id": "checks-1", "owned_root": root, "listen_port": 18891,
                                     "token": String(repeating: "t", count: 40), "nonce": String(repeating: "n", count: 24),
                                     "backend_pid": 4242, "source_roots": [root + "/home"]]
        for (key, value) in overrides { object[key] = value }
        return try! JSONSerialization.data(withJSONObject: object)
    }

    /// A file written in place with one open(2): no atomic temporary item
    /// in a shared folder, which the CI fixture sandbox refuses.
    @discardableResult
    static func writeFile(_ path: String, _ data: Data, mode: mode_t) -> Bool {
        let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        let count = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        return count == data.count && fchmod(fd, mode) == 0
    }

    static func refusals(_ ledger: NotchEvaluationLedger, _ reason: String) -> Int {
        ledger.entries.filter { $0["kind"] as? String == "refused" && $0["reason"] as? String == reason }.count
    }

    static func fetch(_ url: URL) async -> (status: Int?, body: String, error: Bool) {
        do {
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            let (data, response) = try await URLSession.shared.data(for: request)
            return ((response as? HTTPURLResponse)?.statusCode, String(decoding: data, as: UTF8.self), false)
        } catch {
            return (nil, "", true)
        }
    }

    static func main() async {
        let scratch = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-notch-eval-checks-\(getpid())")
        try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }
        // The launcher passes canonical paths; so do the checks.
        let root = URL(fileURLWithPath: NotchEvaluationSources.canonical(scratch.path)!, isDirectory: true)
        let home = root.appendingPathComponent("home")
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)

        // Config: only a private, owned, non-production, loopback run.
        let config = try! NotchEvaluationConfig.parse(configJSON(root: root.path))
        check(config.baseURL.absoluteString == "http://127.0.0.1:18891", "base URL is the run's loopback port")
        for (override, label) in [(["listen_port": 8765], "production backend port"),
                                  (["listen_port": 8000], "engine port"),
                                  (["token": "short"], "short token"), (["nonce": "n"], "short nonce"),
                                  (["run_id": "../x"], "unsafe run id"),
                                  (["backend_pid": Int(getpid())], "the host's own pid"),
                                  (["owned_root": "/nonexistent-wisp-root"], "missing root"),
                                  (["owned_root": scratch.path + "/../" + scratch.lastPathComponent], "a non-canonical root"),
                                  // Must refuse, never trap converting to Int32.
                                  (["backend_pid": Int(Int32.max) + 1], "a pid above Int32.max"),
                                  (["backend_pid": 1 << 40], "a pid far above Int32.max"),
                                  (["backend_pid": -5], "a negative pid"),
                                  (["backend_pid": 3.5], "a fractional pid"),
                                  (["backend_pid": true], "a boolean pid"),
                                  (["backend_pid": "4242"], "a string pid"),
                                  (["source_roots": ["/etc"]], "a source root outside the owned root"),
                                  (["source_roots": [root.path + "/missing"]], "a missing source root")]
                                  as [([String: Any], String)] {
            check((try? NotchEvaluationConfig.parse(configJSON(override, root: root.path))) == nil, "refuses \(label)")
        }
        let file = root.appendingPathComponent("config.json")
        check(writeFile(file.path, configJSON(root: root.path), mode: 0o644), "fixture file written")
        check((try? NotchEvaluationConfig.load(path: file.path)) == nil, "refuses a readable-by-others config")
        chmod(file.path, 0o600)
        check((try? NotchEvaluationConfig.load(path: file.path)) == config, "loads a private config")
        let link = root.appendingPathComponent("link.json")
        try? FileManager.default.createSymbolicLink(at: link, withDestinationURL: file)
        check((try? NotchEvaluationConfig.load(path: link.path)) == nil, "refuses a symlinked config")
        check(config.sourceRoots == [home.path] && config.backendPID == 4242, "source roots and pid parse")
        check(!config.liveSources, "live sources are off unless the config says so")
        let live = try? NotchEvaluationConfig.parse(configJSON(["live_sources": true], root: root.path))
        check(live?.liveSources == true, "live sources parse")
        check((try? NotchEvaluationConfig.parse(configJSON(["live_sources": 1], root: root.path))) == nil,
              "refuses a non-boolean live_sources")

        // Attachments: only regular files inside a source root, links resolved.
        let inside = home.appendingPathComponent("photo.png")
        check(writeFile(inside.path, Data("SYNTHETIC".utf8), mode: 0o600), "fixture file written")
        let outside = root.appendingPathComponent("outside.png")
        check(writeFile(outside.path, Data("SYNTHETIC".utf8), mode: 0o600), "fixture file written")
        let escape = home.appendingPathComponent("escape.png")
        try? FileManager.default.createSymbolicLink(at: escape, withDestinationURL: outside)
        check(NotchEvaluationSources.admit(inside, config: config)?.path == inside.path, "admits a source-root file")
        check(NotchEvaluationSources.admit(outside, config: config) == nil, "refuses a file outside the source roots")
        check(NotchEvaluationSources.admit(escape, config: config) == nil, "refuses a link that escapes the source roots")
        check(NotchEvaluationSources.admit(home, config: config) == nil, "refuses a directory")
        check(NotchEvaluationSources.admit(URL(fileURLWithPath: "/etc/hosts"), config: config) == nil, "refuses a system file")
        check(NotchEvaluationSources.admit(URL(string: "https://example.com/a.png")!, config: config) == nil,
              "refuses a remote URL")

        // The input adapter's wire format: token first, then strict fields.
        func inputRequest(_ fields: [String: Any]) -> Data { try! JSONSerialization.data(withJSONObject: fields) }
        let tokenField: [String: Any] = ["token": config.token]
        let ask = try? NotchEvaluationInput.parse(inputRequest(tokenField.merging(
            ["op": "ask", "case": "17-0", "text": "SYNTHETIC question", "new_chat": false, "timeout_s": 90]) { a, _ in a }),
            token: config.token)
        check(ask == NotchEvaluationInput.Request(op: "ask", caseID: "17-0", text: "SYNTHETIC question",
                                                   newChat: false, timeout: 90), "parses an ask")
        check((try? NotchEvaluationInput.parse(inputRequest(["op": "ping", "token": config.token]), token: config.token))?.op == "ping",
              "parses a ping")
        check((try? NotchEvaluationInput.parse(inputRequest(["op": "quit", "token": config.token]), token: config.token))?.op == "quit",
              "parses a token-checked quit")
        check((try? NotchEvaluationInput.parse(inputRequest(["op": "quit", "token": "wrong"]), token: config.token)) == nil,
              "refuses a quit with a wrong token")
        // AppleScript write check: reads and local assignments pass; app writes do not.
        let allowedScripts = [
            "tell application \"Notes\"\n set refDate to current date\n set year of refDate to 2001\n set output to \"\"\n repeat with nt in notes\n  set output to output & (name of nt)\n end repeat\n return output\nend tell",
            "tell application \"Mail\"\n set FS to ASCII character 1\n set capName to \"Delete Me -- move\" -- send later (* make *)\n set end of toAddrs to (address of r)\n set AppleScript's text item delimiters to linefeed\n if (read status of m) then set readFlag to \"R\"\nend tell",
            "tell application \"Mail\"\r\n set n to count of messages of inbox -- CRLF lines\r\n set capName to \"caf\u{00E9} \u{200B}\u{2028}\"\r\nend tell",
            "tell application \"Notes\"\n set n to count of notes\nend tell\nset n to name of application \"Notes\"",
        ]
        for (index, script) in allowedScripts.enumerated() {
            check(NotchEvaluationScriptPolicy.refusal(script) == nil, "allows read script \(index)")
        }
        let refusedScripts: [(String, String)] = [
            ("tell application \"Mail\"\n delete m\nend tell", "delete"),
            ("tell application \"Mail\"\n set read status of m to true\nend tell", "a property set"),
            ("tell application \"Notes\"\n make new note with properties {name:\"x\"}\nend tell", "make"),
            ("tell application \"Mail\"\n move m to mailbox \"Archive\"\nend tell", "move"),
            ("tell application \"Mail\"\n send newMessage\nend tell", "send"),
            ("tell application \"Mail\"\n set selection to {}\nend tell", "an application property"),
            ("tell application \"Mail\"\n copy true to flagged of m\nend tell", "copy"),
            ("tell application \"System Events\"\n get name\nend tell", "another application"),
            ("tell application \"Mail\"\n tell m\n  set x to 1\n end tell\nend tell", "a nested object tell"),
            ("do shell script \"true\"", "shell"),
            ("tell application \"Mail\"\n DELETE m\nend tell", "an upper-case verb"),
            ("tell application \"Mail\"\n \u{00AB}event coremsnd\u{00BB} m\nend tell", "a raw event code"),
            ("use framework \"Foundation\"", "AppleScriptObjC use"),
            ("set x to current application's NSString", "current application"),
            ("tell application \"Mail\"\n set acctName to m's name\nend tell", "a possessive"),
            ("load script file \"x\"", "load script"),
            ("tell application \"Notes\"\n write \"x\" to nt\nend tell", "write"),
            ("display dialog \"x\"", "display"),
            ("tell application \"Notes\"\n set name of nt to \"x\"\nend tell", "an app property set"),
            ("tell application \"Mail\"\n set brandNew to 1\nend tell", "a set of an unknown local"),
            ("tell application \"Mail\"\n mark m\nend tell", "a word the readers do not use"),
            ("tell application \"Mail\"\n set n to count of application \"Finder\"\nend tell", "an application reference to Finder"),
            ("tell application \"Mail\"\n set n to count of (every item of application id \"com.apple.finder\")\nend tell",
             "an application id reference"),
            ("tell application \"Mail\"\n set n to 1 -- note\rdelete m\nend tell", "a command after a CR-ended comment"),
            ("tell application \"Mail\"\n set n to 1 -- note\r\ndelete m\nend tell", "a command after a CRLF"),
            ("tell application \"Mail\"\n set n to 1 -- note\u{2028}delete m\nend tell", "a U+2028 line break"),
            ("tell application \"Mail\"\n set n to 1\u{2029}delete m\nend tell", "a U+2029 line break"),
            ("tell application \"Mail\"\n del\u{200B}ete m\nend tell", "a zero-width split"),
            ("tell application \"Mail\"\n s\u{0435}nd m\nend tell", "a homoglyph"),
        ]
        for (script, label) in refusedScripts {
            check(NotchEvaluationScriptPolicy.refusal(script) != nil, "refuses a script with \(label)")
        }
        for (fields, label) in [(["op": "ask", "case": "1", "text": "x", "token": "wrong"], "a wrong token"),
                                (["op": "ask", "case": "1", "text": "x"], "no token"),
                                (["op": "shell", "case": "1", "text": "x", "token": config.token], "an unknown op"),
                                (["op": "ask", "case": "../1", "text": "x", "token": config.token], "an unsafe case id"),
                                (["op": "ask", "case": "1", "text": "   ", "token": config.token], "blank text"),
                                (["op": "ask", "case": "1", "text": String(repeating: "x", count: 8001), "token": config.token], "oversized text"),
                                (["op": "ask", "case": "1", "text": "x", "timeout_s": 0, "token": config.token], "a zero timeout")]
                                as [([String: Any], String)] {
            check((try? NotchEvaluationInput.parse(inputRequest(fields), token: config.token)) == nil, "the adapter refuses \(label)")
        }
        check((try? NotchEvaluationInput.parse(Data("not json".utf8), token: config.token)) == nil, "the adapter refuses non-JSON")
        check(NotchEvaluationInput.encode(["status": "ready"]).last == 0x0A, "replies are one line")

        // Admission: exact origin only; the token is added.
        for raw in ["http://127.0.0.1:8765/agent", "https://127.0.0.1:18891/agent", "http://localhost:18891/agent",
                    "http://u:p@127.0.0.1:18891/agent", "https://example.com/?q=private", "http://127.0.0.1:18892/v1/models"] {
            if case .success = EvaluationGuard.admit(URLRequest(url: URL(string: raw)!), config: config) {
                check(false, "admits \(raw)")
            } else { check(true, "refuses \(raw)") }
        }
        if case .success(let admitted) = EvaluationGuard.admit(URLRequest(url: URL(string: "http://127.0.0.1:18891/agent")!), config: config) {
            check(admitted.value(forHTTPHeaderField: EvaluationGuard.tokenHeader) == config.token, "adds the run token")
        } else { check(false, "admits the evaluation origin") }

        // Response identity.
        let url = URL(string: "http://127.0.0.1:18891/agent")!
        func response(_ status: Int, _ headers: [String: String], at target: URL = url) -> HTTPURLResponse {
            HTTPURLResponse(url: target, statusCode: status, httpVersion: nil, headerFields: headers)!
        }
        let good = [EvaluationGuard.nonceHeader: config.nonce, EvaluationGuard.pidHeader: "4242"]
        check(EvaluationGuard.accept(response(200, good), config: config) == nil, "accepts this run's backend")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.pidHeader: "4242"]), config: config) == .identity("nonce"), "no nonce")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.nonceHeader: "x" + config.nonce.dropFirst(),
                                                    EvaluationGuard.pidHeader: "4242"]), config: config) == .identity("nonce"), "wrong nonce")
        check(EvaluationGuard.accept(response(200, [EvaluationGuard.nonceHeader: config.nonce,
                                                    EvaluationGuard.pidHeader: "1"]), config: config) == .identity("pid"), "wrong pid")
        check(EvaluationGuard.accept(response(302, good), config: config) == .redirect, "a redirect")
        check(EvaluationGuard.accept(response(200, good, at: URL(string: "http://127.0.0.1:8765/agent")!), config: config)
              != nil, "a response from the production port")

        // The URLProtocol end to end, against the in-process stub backend.
        let ledger = NotchEvaluationLedger(directory: root.appendingPathComponent("evidence"))
        EvaluationGuardProtocol.config = config
        EvaluationGuardProtocol.ledger = ledger
        EvaluationGuardProtocol.forwardingProtocols = [StubBackend.self]
        check(URLProtocol.registerClass(EvaluationGuardProtocol.self), "guard installs for URLSession.shared")
        StubBackend.headers = good
        var result = await fetch(url)
        check(result.status == 200 && result.body.contains("SYNTHETIC"), "the evaluation backend's stream arrives")
        check(StubBackend.seen.last?.value(forHTTPHeaderField: EvaluationGuard.tokenHeader) == config.token,
              "the backend receives the run token")
        StubBackend.headers = [EvaluationGuard.nonceHeader: "wrong-nonce-wrong-nonce", EvaluationGuard.pidHeader: "4242"]
        result = await fetch(url)
        check(result.error && result.body.isEmpty, "a backend without this run's nonce gets no data through")
        StubBackend.headers = good
        StubBackend.redirect = true
        result = await fetch(url)
        check(result.error, "a redirect fails the request")
        StubBackend.redirect = false
        // A connection that closes before any response ends exactly once, promptly.
        var incomplete = refusals(ledger, "incomplete")
        StubBackend.mode = .closeBeforeHeaders
        var began = Date()
        result = await fetch(url)
        check(result.error && result.body.isEmpty && Date().timeIntervalSince(began) < 4,
              "a close before headers fails promptly with no body")
        check(refusals(ledger, "incomplete") == incomplete + 1, "a close before headers ends exactly once")
        // A cancelled request stops silently: no refusal and no callback after it.
        StubBackend.mode = .hang
        incomplete = refusals(ledger, "incomplete")
        let refusedBefore = ledger.entries.filter { $0["kind"] as? String == "refused" }.count
        began = Date()
        let pending = Task { await fetch(url) }
        try? await Task.sleep(nanoseconds: 200_000_000)
        pending.cancel()
        result = await pending.value
        check(result.error && Date().timeIntervalSince(began) < 4, "a cancelled stream ends promptly")
        try? await Task.sleep(nanoseconds: 200_000_000)
        check(ledger.entries.filter { $0["kind"] as? String == "refused" }.count == refusedBefore,
              "a cancellation is not reported as a second failure")
        StubBackend.mode = .stream
        let before = StubBackend.seen.count
        for raw in ["http://127.0.0.1:8765/agent", "https://example.com/?q=PRIVATE-URL-CANARY"] {
            result = await fetch(URL(string: raw)!)
            check(result.error, "\(raw) never leaves the process")
        }
        check(StubBackend.seen.count == before, "refused requests are never forwarded")

        // The real runAgent: under the checks build its base URL is the
        // production port, which the guard refuses before any byte is sent.
        let events = Box()
        await WispClient().runAgent(prompt: "SYNTHETIC check", image: nil) { events.append($0.type) }
        check(events.values == ["error"] && StubBackend.seen.count == before, "runAgent to the installed backend is refused")

        // Native sinks and hash-only receipts.
        let blocked = NotchEvaluationSinks.blocked("send_email", ledger: ledger)
        check(blocked["ok"] as? Bool == false && blocked["status"] as? String == "blocked", "a native sink refuses")
        check(ledger.input("SYNTHETIC-PRIVATE-PROMPT", dispatch: 7, session: "abc123"), "an input receipt persists")
        ledger.event(type: "tool_call", payload: ["id": "PRIVATE-EVENT-CANARY value", "name": "view_emails",
                                                  "trace_id": "trace_1"], dispatch: 7, current: true)

        // Finalized visible receipts: one per dispatch, the exact displayed
        // text, captured at the terminal event. These drive the ledger the way
        // OverlayModel.handle does (flush, receipt, then clear and dequeue);
        // the real model path is covered by native UI qualification.
        func receipts(_ kind: String, _ dispatch: UInt64) -> [[String: Any]] {
            ledger.entries.filter { $0["kind"] as? String == kind && ($0["dispatch"] as? UInt64) == dispatch }
        }
        var answer = "SYNTHETIC-PRIVATE-"
        let buffered = "ANSWER"
        answer += buffered  // done flushes the coalesced delta first
        check(ledger.terminal("done", visible: answer, dispatch: 7, current: true, session: "abc123", trace: "trace_1"),
              "the finalized answer is receipted")
        check(receipts("visible", 7).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("SYNTHETIC-PRIVATE-ANSWER"),
              "the receipt hashes the complete flushed answer, not an empty buffer")
        check(!ledger.terminal("done", visible: "", dispatch: 7, current: true, session: "", trace: nil)
              && receipts("visible", 7).count == 1, "a second terminal cannot relabel a dispatch")
        check(!ledger.terminal("done", visible: "x", dispatch: 99, current: true, session: "", trace: nil),
              "a dispatch with no input receipt claims no visible result")
        // Queued: the second dispatch starts after the first's receipt.
        ledger.input("SYNTHETIC second", dispatch: 8, session: "abc123")
        ledger.terminal("error", visible: "Something went wrong.", dispatch: 8, current: true, session: "abc123", trace: nil)
        ledger.input("SYNTHETIC queued", dispatch: 9, session: "abc123")
        ledger.terminal("done", visible: "SYNTHETIC third", dispatch: 9, current: true, session: "abc123", trace: nil)
        check(receipts("visible", 8).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("Something went wrong.")
              && receipts("visible", 8).first?["outcome"] as? String == "error", "an error receipt matches the error turn")
        check(receipts("visible", 9).first?["answer_sha256"] as? String == NotchEvaluationLedger.sha256("SYNTHETIC third"),
              "a queued turn's answer is bound to its own dispatch")
        // Stale: a cancelled dispatch's late terminal is telemetry only.
        ledger.input("SYNTHETIC cancelled", dispatch: 10, session: "abc123")
        ledger.terminal("done", visible: "", dispatch: 10, current: false, session: "abc123", trace: nil)
        check(receipts("visible", 10).isEmpty && receipts("late_terminal", 10).count == 1
              && receipts("late_terminal", 10).first?["answer_sha256"] == nil, "a stale terminal labels no visible result")
        check(ledger.terminal("done", visible: "", dispatch: 10, current: true, session: "", trace: nil)
              && receipts("visible", 10).first?["rendered"] as? Bool == false, "an empty reply is receipted as not rendered")

        let written = (try? String(contentsOf: root.appendingPathComponent("evidence/native-receipts.jsonl"), encoding: .utf8)) ?? ""
        check(written.contains(NotchEvaluationLedger.sha256("SYNTHETIC-PRIVATE-PROMPT")), "the input hash is recorded")
        check(!written.contains("SYNTHETIC-PRIVATE-PROMPT") && !written.contains("SYNTHETIC-PRIVATE-ANSWER"),
              "no prompt or answer text is written")
        check(!written.contains("PRIVATE-EVENT-CANARY") && written.contains("id_sha256")
              && written.contains("\"trace_id\":\"trace_1\""), "untrusted event ids are hashed; safe ids kept")
        check(!written.contains("PRIVATE-URL-CANARY") && written.contains("url_sha256"), "refused URLs are hashed")
        let mode = (try? FileManager.default.attributesOfItem(atPath: root.appendingPathComponent("evidence/native-receipts.jsonl").path))?[.posixPermissions] as? NSNumber
        check(mode?.intValue == 0o600, "receipts are private")
        check(ledger.entries.contains { $0["kind"] as? String == "refused" }, "refusals are receipted")
        check(ledger.healthy, "the healthy ledger stayed healthy")

        // Receipt destinations: only a private regular file in a private directory.
        let bad1 = root.appendingPathComponent("bad1")
        try? FileManager.default.createDirectory(at: bad1.appendingPathComponent("native-receipts.jsonl"),
                                                 withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        chmod(bad1.path, 0o700)
        check(!NotchEvaluationLedger(directory: bad1).healthy, "refuses a receipt path that is a directory")
        let bad2 = root.appendingPathComponent("bad2")
        try? FileManager.default.createDirectory(at: bad2, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? FileManager.default.createSymbolicLink(at: bad2.appendingPathComponent("native-receipts.jsonl"),
                                                   withDestinationURL: outside)
        check(!NotchEvaluationLedger(directory: bad2).healthy, "refuses a linked receipt file")
        let bad3 = root.appendingPathComponent("bad3")
        try? FileManager.default.createDirectory(at: bad3, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        check(writeFile(bad3.appendingPathComponent("native-receipts.jsonl").path, Data(), mode: 0o644), "fixture file written")
        check(!NotchEvaluationLedger(directory: bad3).healthy, "refuses a receipt file others can read")
        let bad4 = root.appendingPathComponent("bad4")
        try? FileManager.default.createDirectory(at: bad4, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
        chmod(bad4.path, 0o755)
        check(!NotchEvaluationLedger(directory: bad4).healthy, "refuses a receipt directory others can read")

        // An unhealthy ledger stops everything: no request is forwarded and no turn input is accepted.
        let broken = NotchEvaluationLedger(directory: bad1)
        EvaluationGuardProtocol.ledger = broken
        let forwarded = StubBackend.seen.count
        result = await fetch(url)
        check(result.error && StubBackend.seen.count == forwarded, "no request leaves while evidence is unavailable")
        check(!broken.input("SYNTHETIC", dispatch: 1, session: ""), "no turn starts while evidence is unavailable")
        EvaluationGuardProtocol.ledger = ledger

        // A write that fails is sticky: the file size limit makes write(2) fail (EFBIG).
        let fsz = root.appendingPathComponent("fsz")
        let limited = NotchEvaluationLedger(directory: fsz)
        check(limited.record("probe", [:]), "a fresh ledger writes")
        signal(SIGXFSZ, SIG_IGN)
        var saved = rlimit()
        getrlimit(RLIMIT_FSIZE, &saved)
        var tiny = saved
        tiny.rlim_cur = 16
        setrlimit(RLIMIT_FSIZE, &tiny)
        let wrote = limited.record("probe", ["pad": String(repeating: "x", count: 64)])
        setrlimit(RLIMIT_FSIZE, &saved)
        check(!wrote && !limited.healthy, "a failed receipt write makes the ledger unhealthy")
        check(!limited.record("probe", [:]) && !limited.healthy, "the failure is sticky")

        // Without a file, exhausting in-memory retention is a failure, not silent loss.
        let memory = NotchEvaluationLedger(directory: nil)
        for i in 0..<NotchEvaluationLedger.maxMemoryEntries { memory.record("fill", ["i": i]) }
        check(memory.healthy && !memory.record("over", [:]) && !memory.healthy, "exhausted retention is a sticky failure")

        workspaceGuardChecks()
        sandboxProbeChecks(root: root)
        socketModeChecks(root: root)
        print("NotchEvaluation: \(passed) checks passed")
    }
}

final class Box: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var values: [String] = []
    func append(_ value: String) { lock.lock(); values.append(value); lock.unlock() }
}
