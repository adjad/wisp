import Foundation
import Darwin

@main
struct DiagnosticReportChecks {
    static var count = 0

    static func check(_ value: Bool, _ message: @autoclosure () -> String = "", line: UInt = #line) {
        if !value {
            print("FAILED at DiagnosticReportChecks.swift:\(line) \(message())")
            exit(1)
        }
        count += 1
    }

    /// Serialized report for explicitly included details, as the preview shows it.
    static func rendered(_ details: [String: Any]) throws -> String {
        let report = DiagnosticReport.payload(metadata: [:], details: details, note: "", includeDetails: true)
        return String(data: try DiagnosticReport.encoded(report), encoding: .utf8)!
    }

    /// Every fragment must be gone from the preview; the report must still be valid JSON.
    static func removes(_ text: String, _ fragments: [String], line: UInt = #line) throws {
        let output = try rendered(["text": text])
        for fragment in fragments { check(!output.contains(fragment), "leaked \(fragment) from \(text.debugDescription)", line: line) }
        check(output.contains("[REDACTED]"), "no marker for \(text.debugDescription)", line: line)
    }

    static func keeps(_ text: String, line: UInt = #line) throws {
        let output = try rendered(["text": text])
        let expected = String(data: try JSONSerialization.data(withJSONObject: [text], options: [.withoutEscapingSlashes]), encoding: .utf8)!
        check(output.contains(String(expected.dropFirst().dropLast())), "damaged \(text.debugDescription): \(output)", line: line)
    }

    static func main() throws {
        let privateText = "PRIVATE-PROMPT"
        let details: [String: Any] = ["prompt": privateText, "api_key": "sk-secret-secret", "nested": ["authorization": "Bearer xyz-secret"],
                                      "title": "{\"token\": \"secret-secret\"}", "result": "Bearer secret-secret-secret"]
        let normal = DiagnosticReport.payload(metadata: ["surface": "chat", "client_first_token_seconds": 3.0], details: details, note: "", includeDetails: false)
        let normalData = try DiagnosticReport.encoded(normal)
        check(!String(data: normalData, encoding: .utf8)!.contains(privateText))
        check(normal["client_first_token_seconds"] as? Double == 3.0)
        let selected = DiagnosticReport.payload(metadata: [:], details: details, note: "Expected this", includeDetails: true)
        let selectedJSON = String(data: try DiagnosticReport.encoded(selected), encoding: .utf8)!
        check(selectedJSON.contains(privateText))
        check(!selectedJSON.contains("secret-secret"))
        check((selected["details"] as? [String: Any])?["title"] is String)
        check(selected["expected_result"] as? String == "Expected this")
        do {
            _ = try DiagnosticReport.encoded(["large": String(repeating: "x", count: DiagnosticReport.maximumBytes + 1)])
            check(false, "Oversized report accepted")
        } catch { count += 1 }
        // Details pre-scrubbed by the window are not scrubbed twice, yet metadata still is.
        let pre = DiagnosticReport.payload(metadata: ["note": "token=METADATASECRET1"], details: DiagnosticReport.scrub(details) as? [String: Any],
                                           note: "password is NOTESECRET1", includeDetails: true, detailsAreScrubbed: true)
        let preJSON = String(data: try DiagnosticReport.encoded(pre), encoding: .utf8)!
        check(preJSON.contains(privateText) && !preJSON.contains("secret-secret") && !preJSON.contains("METADATASECRET1") && !preJSON.contains("NOTESECRET1"))

        // MARK: scrubber: keys split by whitespace, newlines and invisible characters
        try removes("api\nkey: SPLITTAIL01", ["SPLITTAIL01"])
        try removes("pass\u{200B}word: SPLITTAIL02", ["SPLITTAIL02"])
        try removes("to ken = SPLITTAIL03", ["SPLITTAIL03"])
        try removes("api_\nkey=SPLITTAIL04", ["SPLITTAIL04"])
        try removes("a p i _ k e y : SPLITTAIL05", ["SPLITTAIL05"])
        try removes("secret\u{200D}: SPLITTAIL06", ["SPLITTAIL06"])
        try removes("api_key\n:\nSPLITTAIL07", ["SPLITTAIL07"])
        try removes("sec\u{FEFF}ret: SPLITTAIL08", ["SPLITTAIL08"])
        try removes("pass\u{0}word: SPLITTAIL09", ["SPLITTAIL09"])
        // MARK: embedded JSON in prose
        try removes("here is my config {\"api_key\": \"EMBEDDED01\", \"name\": \"x\"} thanks", ["EMBEDDED01"])
        try removes("send {\"token\": \"EMBEDDED02\"} to them", ["EMBEDDED02"])
        try removes("config: {'password': 'EMBEDDED03 with spaces'}", ["EMBEDDED03", "with spaces"])
        try removes("{\"auth\": {\"token\": \"EMBEDDED04\"}} done", ["EMBEDDED04"])
        check(try rendered(["text": "see {\"api_key\": \"EMBEDDED05\", \"name\": \"keepme\"}"]).contains("keepme"))
        // MARK: assignments, headers and spoken forms
        try removes("token=ASSIGNED01", ["ASSIGNED01"])
        try removes("?next=1&access_token=ASSIGNED02&x=1", ["ASSIGNED02"])
        try removes("Authorization: Basic dXNlcjpBU1NJR05FRDAz", ["dXNlcjpBU1NJR05FRDAz"])
        try removes("Authorization: Bearer ASSIGNED04abcdef", ["ASSIGNED04abcdef"])
        try removes("authorization=Digest ASSIGNED05abcdef", ["ASSIGNED05abcdef"])
        try removes("curl -H 'Authorization: Bearer ASSIGNED06abcdef' https://example.com", ["ASSIGNED06abcdef"])
        try removes("my password is ASSIGNED07", ["ASSIGNED07"])
        try removes("the password is \"ASSIGNED08 with spaces\" ok", ["ASSIGNED08", "with spaces"])
        try removes("passwd: ASSIGNED09", ["ASSIGNED09"])
        try removes("secret: ASSIGNED10", ["ASSIGNED10"])
        try removes("DB_PASSWORD=ASSIGNED11", ["ASSIGNED11"])
        try removes("openrouter_api_key = ASSIGNED12", ["ASSIGNED12"])
        try removes("X-API-Key: ASSIGNED13", ["ASSIGNED13"])
        try removes("Cookie: session=ASSIGNED14; theme=dark", ["ASSIGNED14", "theme=dark"])
        try removes("Basic dXNlcm5hbWU6QVNTSUdORUQxNQ== was sent", ["dXNlcm5hbWU6QVNTSUdORUQxNQ"])
        try removes("Bearer ASSIGNED16abcdef", ["ASSIGNED16abcdef"])
        try removes("https://user:ASSIGNED17@example.com/path", ["ASSIGNED17"])
        // MARK: provider formats and encodings
        try removes("key AKIAIOSFODNN7EXAMPLE here", ["AKIAIOSFODNN7EXAMPLE"])
        try removes("key AIzaSyA1234567890abcdefghijklmnopqrstuv here", ["AIzaSyA1234567890abcdefghijklmnopqrstuv"])
        try removes("slack xoxb-123456789012-abcdefghijkl here", ["xoxb-123456789012-abcdefghijkl"])
        try removes("slack xoxp-123456789012-abcdefghijkl here", ["xoxp-123456789012"])
        try removes("slack xoxa-123456789012-abcdefghijkl xoxr-123456789012-abcdefghijkl xoxs-123456789012-abcdefghijkl", ["xoxa-1234", "xoxr-1234", "xoxs-1234"])
        try removes("git ghp_abcdefghijklmnopqrstuvwxyz0123456789 ghs_abcdefghijklmnopqrstuvwxyz0123456789 github_pat_abcdefghijklmnopqrstuvwxyz0123", ["ghp_abcdef", "ghs_abcdef", "github_pat_abcdef"])
        let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        try removes("session \(jwt) end", ["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"])
        let pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAPEMBODYLINE1\nPEMBODYLINE2abcdef\n-----END RSA PRIVATE KEY-----"
        try removes("before\n\(pem)\nafter", ["PEMBODYLINE1", "PEMBODYLINE2", "BEGIN RSA", "END RSA"])
        check(try rendered(["text": "before\n\(pem)\nafter"]).contains("after"))
        try removes("-----BEGIN OPENSSH PRIVATE KEY-----\nTRUNCATEDBODY01\nTRUNCATEDBODY02", ["TRUNCATEDBODY01", "TRUNCATEDBODY02"])
        try removes("-----BEGIN PGP PRIVATE KEY BLOCK-----\nPGPBODY01\n-----END PGP PRIVATE KEY BLOCK-----", ["PGPBODY01"])
        try removes("key sk-abcdefgh12345678", ["abcdefgh12345678"])
        try removes("key SK-ABCDEFGH12345678", ["ABCDEFGH12345678"])
        try removes("key sk-proj-abcdefgh_12345678", ["abcdefgh_12345678"])
        try removes("key sk-ant-api03-abcdefgh12345678", ["abcdefgh12345678"])
        try removes("key \u{FF53}\u{FF4B}\u{FF0D}\u{FF21}\u{FF22}\u{FF23}\u{FF24}\u{FF25}\u{FF26}\u{FF27}\u{FF28}\u{FF11}\u{FF12}\u{FF13}", ["\u{FF21}\u{FF22}", "ABCDEFGH123"])
        try removes("key sk%2Dabcdefgh12345678 and SK%2dABCDEFGH12345678", ["abcdefgh12345678", "ABCDEFGH12345678"])
        try removes("sk_live_abcdefghijklmnop", ["abcdefghijklmnop"])
        let base64 = "dGhpcyBpcyBhIHZlcnkgbG9uZyBiYXNlNjQgc2VjcmV0IHZhbHVlIHRoYXQgc2hvdWxkIG5ldmVyIGFwcGVhcg"
        try removes("client_secret: \(base64)==", [base64])
        try removes("secret=YWJj%2BZGVmZ2hpamtsbW5vcA%3D%3D", ["YWJj%2BZGVmZ2hpamtsbW5vcA"])
        try removes("api key: sk%2Dabcdefgh12345678", ["abcdefgh12345678"])
        try removes("client_secret_v2: SUFFIX01 and auth_token_prod=SUFFIX02 and api_key.backup = SUFFIX03", ["SUFFIX01", "SUFFIX02", "SUFFIX03"])
        try removes("secretkey=SHORTKEY01 and AccessKey: SHORTKEY02 and apikey=SHORTKEY03", ["SHORTKEY01", "SHORTKEY02", "SHORTKEY03"])

        // MARK: sensitive keys by substring, and secrets as dictionary keys
        let keyed: [String: Any] = ["openrouter_api_key": "KEYED01", "x-api-key": "KEYED02", "secret_key": "KEYED03", "client_secret": "KEYED04",
                                    "access_token": "KEYED05", "authToken": "KEYED06", "Authorization": "KEYED07", "authHeader": "KEYED08",
                                    "refresh_token": "KEYED09", "cookies": ["a": "KEYED10"], "Set-Cookie": "KEYED11", "db_password": "KEYED12",
                                    "apiKey": "KEYED13", "private_key": "KEYED14", "oauth": "KEYED15", "credentials": ["u": "KEYED16"],
                                    "api\u{200B}_key": "KEYED17", "pass word": "KEYED18",
                                    "sk-DICTKEYABCDEFGH12345": "visible", "https://u:KEYED19@h.example/": "x",
                                    "Bearer KEYED20ABCDEF": 1]
        let keyedJSON = try rendered(keyed)
        for index in 1...20 { check(!keyedJSON.contains(String(format: "KEYED%02d", index)), "keyed \(index)") }
        check(!keyedJSON.contains("DICTKEYABCDEFGH12345"))
        check(keyedJSON.contains("visible"))
        // Distinct secret keys must not overwrite each other's values.
        let collide = DiagnosticReport.scrub(["sk-aaaaaaaaaa": "one", "sk-bbbbbbbbbb": "two"]) as! [String: Any]
        check(collide.count == 2 && Set(collide.values.map { "\($0)" }) == ["one", "two"])
        // Full-width and compatibility forms of a sensitive key are recognized too.
        let wide = DiagnosticReport.scrub(["\u{FF50}\u{FF41}\u{FF53}\u{FF53}\u{FF57}\u{FF4F}\u{FF52}\u{FF44}": "WIDEKEY01", "\u{FF54}\u{FF4F}\u{FF4B}\u{FF45}\u{FF4E}": "WIDEKEY02"])
        check(!"\(wide)".contains("WIDEKEY"))
        // Details are only ever added to the report when the person selected them.
        let withheld = DiagnosticReport.payload(metadata: [:], details: ["prompt": privateText], note: "", includeDetails: false, detailsAreScrubbed: true)
        check(withheld["details"] == nil && withheld["details_included"] as? Bool == false)
        let shown = DiagnosticReport.payload(metadata: [:], details: ["prompt": privateText], note: "", includeDetails: true, detailsAreScrubbed: true)
        check((shown["details"] as? [String: Any])?["prompt"] as? String == privateText)
        // Counters and ordinary words are not secrets.
        let harmless: [String: Any] = ["max_tokens": 4096, "client_first_token_seconds": 3.5, "author": "Jane", "authority": "Rome", "authors": ["Ann"],
                                       "keyboard": "qwerty", "monkey": "banana", "authenticated": true, "tokens": 12, "key": "plain"]
        let harmlessOut = DiagnosticReport.scrub(harmless) as! [String: Any]
        check(harmlessOut.count == harmless.count)
        for (key, value) in harmless { check("\(harmlessOut[key]!)" == "\(value)", "harmless key \(key) changed to \(harmlessOut[key]!)") }
        check(((DiagnosticReport.scrub(["tokens": "abc"]) as! [String: Any])["tokens"] as? String) == "[REDACTED]")
        // Credential values can be numeric; only known token counters are exempt.
        for key in ["token", "access_token", "refresh_token", "id_token", "api_token", "auth_tokens"] {
            let redacted = DiagnosticReport.scrub([key: 123456]) as! [String: Any]
            check(redacted[key] as? String == "[REDACTED]", "numeric credential survived: \(key)")
        }
        for key in ["input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"] {
            let counter = DiagnosticReport.scrub([key: 12]) as! [String: Any]
            check(counter[key] as? Int == 12, "token counter lost: \(key)")
        }

        // MARK: ordinary text survives
        try keeps("The task is to ask about my keyboard and the token budget")
        try keeps("Basic information about the project, Basic administration notes")
        try keeps("desk-12345678 and task-1234567 and disk-ABCDEFGH12")
        try keeps("tokens: 5 and max_tokens: 4096")
        try keeps("bearer of bad news")
        try keeps("Secretary: Bob and tokenizer: fast and author: Jane")
        try keeps("family \u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467} and caf\u{00E9} \u{FF21}")
        try keeps("Meet at 3pm about the password manager rollout")
        try keeps("NaN and Infinity and [NaN] and 1e999 are words")

        // MARK: non-finite numbers cannot crash serialization
        // Foundation parses negative overflow to -inf (the crash) but rejects positive overflow.
        for text in ["[-1e999]", "[-1e400]", " [ -1E999 ] ", "[-1e309, 1]", "{\"a\": -1e999}", "[[1],[-1e999,{\"a\":[-1e999]}]]"] {
            let output = try rendered(["result": text])
            check(output.contains("non-finite number"), "\(text) -> \(output)")
            check(!output.contains("999") && !output.contains("e400") && !output.contains("e309"), output)
        }
        for text in ["[1e999]", "{\"a\": 1e999}", "[[1e999],[2e999,{\"a\":[1e999]}]]", "[1e400]"] { _ = try rendered(["result": text]) }
        for text in ["NaN", "[NaN]", "[Infinity]", "[-Infinity]", "-1e999"] { _ = try rendered(["result": text]) }
        let nested = DiagnosticReport.payload(metadata: ["bad": Double.nan, "worse": [Double.infinity, -Double.infinity], "float": Float.nan],
                                              details: ["deep": [["x": Double.nan]], "ok": 1.5], note: "", includeDetails: true)
        let nestedJSON = try rendered(["a": 1])
        check(nestedJSON.contains("\"a\""))
        let nestedData = try DiagnosticReport.encoded(nested)
        check(String(data: nestedData, encoding: .utf8)!.contains("non-finite number"))
        do {
            _ = try DiagnosticReport.encoded(["x": Double.infinity])
            check(false, "non-finite accepted by encoded")
        } catch { count += 1 }
        do {
            _ = try DiagnosticReport.encoded(["x": Date()])
            check(false, "non-JSON value accepted by encoded")
        } catch { count += 1 }
        let finite = DiagnosticReport.finiteJSON(["a": [Double.nan, 1.0, ["b": -Double.infinity]], "c": "text"]) as! [String: Any]
        check(JSONSerialization.isValidJSONObject(finite))
        check(DiagnosticReport.finiteJSON(Double.nan) as? String == "[non-finite number]")
        check(DiagnosticReport.finiteJSON(2.5) as? Double == 2.5)

        // MARK: large and adversarial input stays responsive (no quadratic patterns)
        let started = Date()
        let big = String(repeating: "ordinary text with token budget and an api key mention. ", count: 20_000)
        _ = DiagnosticReport.scrub(["big": big])
        check(Date().timeIntervalSince(started) < 30, "scrubbing a 1 MB string took \(Date().timeIntervalSince(started)) s")
        for hostile in [String(repeating: "sk-", count: 100_000), String(repeating: "a", count: 200_000), String(repeating: "p a s s w o r d ", count: 10_000),
                        String(repeating: "http", count: 50_000), String(repeating: "a\u{200B}", count: 100_000), String(repeating: "secret: ", count: 20_000)] {
            let began = Date()
            _ = DiagnosticReport.scrubText(hostile)
            check(Date().timeIntervalSince(began) < 20, "adversarial input took \(Date().timeIntervalSince(began)) s")
        }

        // MARK: saving a report
        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["WISP_HOME"]!)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let saveDirectory = root.appendingPathComponent("saved")
        try FileManager.default.createDirectory(at: saveDirectory, withIntermediateDirectories: true)
        let target = saveDirectory.appendingPathComponent("report.json")
        let previousMask = umask(0)             // the most permissive process mask
        var observedTemporaryMode: mode_t = 0
        var observedTemporaryPath = ""
        try DiagnosticReport.save(Data("{\"ok\":true}".utf8), to: target) { temporary in
            var info = stat()
            check(lstat(temporary, &info) == 0)
            observedTemporaryMode = info.st_mode & 0o777
            observedTemporaryPath = temporary
        }
        umask(previousMask)
        check(observedTemporaryMode == 0o600, "temporary report mode \(String(observedTemporaryMode, radix: 8))")
        check(URL(fileURLWithPath: observedTemporaryPath).deletingLastPathComponent().standardizedFileURL.path == saveDirectory.standardizedFileURL.path && !FileManager.default.fileExists(atPath: observedTemporaryPath), "temporary \(observedTemporaryPath) vs \(saveDirectory.path)")
        var savedInfo = stat()
        check(lstat(target.path, &savedInfo) == 0 && savedInfo.st_mode & 0o777 == 0o600 && savedInfo.st_mode & S_IFMT == S_IFREG)
        check(try Data(contentsOf: target) == Data("{\"ok\":true}".utf8))
        // Replacing a world-readable file yields a private one.
        try Data("old".utf8).write(to: target)
        check(chmod(target.path, 0o644) == 0)
        try DiagnosticReport.save(Data("new".utf8), to: target)
        check(lstat(target.path, &savedInfo) == 0 && savedInfo.st_mode & 0o777 == 0o600)
        check(try String(contentsOf: target, encoding: .utf8) == "new")
        // Failure is reported and leaves nothing behind.
        let missing = saveDirectory.appendingPathComponent("no-such-directory/report.json")
        do {
            try DiagnosticReport.save(Data("x".utf8), to: missing)
            check(false, "save into a missing directory succeeded")
        } catch { count += 1 }
        let blocked = saveDirectory.appendingPathComponent("blocked")
        try FileManager.default.createDirectory(at: blocked, withIntermediateDirectories: true)
        do {
            try DiagnosticReport.save(Data("x".utf8), to: blocked)     // a directory cannot be replaced by a file
            check(false, "save over a directory succeeded")
        } catch { count += 1 }
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: saveDirectory.path).filter { $0.hasSuffix(".tmp") }
        check(leftovers.isEmpty, "leftover temporary files \(leftovers)")

        // MARK: journal reader
        let directory = root.appendingPathComponent("diagnostics")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        check(chmod(directory.path, 0o700) == 0)
        let id = String(repeating: "a", count: 32)
        let file = directory.appendingPathComponent(id + ".json")
        let trace: [String: Any] = ["schema_version": 1, "trace_id": id, "kind": "agent", "status": "failed", "started_at": 1000,
                                  "PRIVATE": privateText, "events": [["stage": "error", "elapsed_ms": 10, "message": privateText]]]
        func write(_ object: Any, to url: URL, mode: mode_t = 0o600) throws {
            try JSONSerialization.data(withJSONObject: object).write(to: url)
            check(chmod(url.path, mode) == 0)
        }
        try write(trace, to: file)
        let journal = DiagnosticReport.journal(traceID: id)!
        check(!String(data: try DiagnosticReport.encoded(journal), encoding: .utf8)!.contains(privateText))
        check(journal["status"] as? String == "failed")
        check(DiagnosticReport.journal(traceID: nil) == nil)
        check(DiagnosticReport.journal(traceID: "../bad") == nil)
        check(DiagnosticReport.journal(traceID: id, expectedUID: getuid() + 1) == nil, "files owned by someone else must be refused")
        check(DiagnosticReport.journal(traceID: id, expectedUID: getuid() + 1, expectedDirectoryUID: getuid()) == nil, "a file owned by someone else must be refused on its own")
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: file.path)
        check(DiagnosticReport.journal(traceID: id) == nil)
        try FileManager.default.removeItem(at: file)
        // A link is refused even when its target is perfectly private.
        let outside = root.appendingPathComponent("outside.json")
        try write(trace, to: outside)
        try FileManager.default.createSymbolicLink(at: file, withDestinationURL: outside)
        check(DiagnosticReport.journal(traceID: id) == nil)
        try FileManager.default.removeItem(at: file)

        // Trace id: lowercase 32 hex only, and the file must name itself.
        let upper = String(repeating: "A", count: 32)
        try write(["schema_version": 1, "trace_id": upper, "events": []], to: directory.appendingPathComponent(upper + ".json"))
        check(DiagnosticReport.journal(traceID: upper) == nil)
        try write(["schema_version": 1, "trace_id": "named", "events": []], to: directory.appendingPathComponent("named.json"))
        check(DiagnosticReport.journal(traceID: "named") == nil)
        try write(["schema_version": 1, "trace_id": id, "events": []], to: root.appendingPathComponent("escaped.json"))
        check(DiagnosticReport.journal(traceID: "../escaped") == nil)
        let other = String(repeating: "b", count: 32)
        try write(["schema_version": 1, "trace_id": id, "kind": "agent", "events": []], to: directory.appendingPathComponent(other + ".json"))
        check(DiagnosticReport.journal(traceID: other) == nil, "a journal naming a different trace must be refused")
        let versioned = String(repeating: "c", count: 32)
        try write(["schema_version": 2, "trace_id": versioned, "kind": "agent", "events": []], to: directory.appendingPathComponent(versioned + ".json"))
        check(DiagnosticReport.journal(traceID: versioned) == nil)
        let unversioned = String(repeating: "d", count: 32)
        try write(["trace_id": unversioned, "kind": "agent", "events": []], to: directory.appendingPathComponent(unversioned + ".json"))
        check(DiagnosticReport.journal(traceID: unversioned) == nil)
        let stringVersion = String(repeating: "e", count: 32)
        try write(["schema_version": "1", "trace_id": stringVersion, "events": []], to: directory.appendingPathComponent(stringVersion + ".json"))
        check(DiagnosticReport.journal(traceID: stringVersion) == nil)

        // Reader size cap, special files and empty files.
        let huge = String(repeating: "f", count: 32)
        try write(["schema_version": 1, "trace_id": huge, "kind": "agent", "events": [], "pad": String(repeating: "x", count: DiagnosticReport.maximumBytes)],
                  to: directory.appendingPathComponent(huge + ".json"))
        check(DiagnosticReport.journal(traceID: huge) == nil, "a journal over 2 MB must be refused")
        let empty = String(repeating: "1", count: 32)
        check(FileManager.default.createFile(atPath: directory.appendingPathComponent(empty + ".json").path, contents: Data(), attributes: [.posixPermissions: 0o600]))
        check(DiagnosticReport.journal(traceID: empty) == nil)
        let fifo = String(repeating: "2", count: 32)
        check(mkfifo(directory.appendingPathComponent(fifo + ".json").path, 0o600) == 0)
        check(DiagnosticReport.journal(traceID: fifo) == nil)
        let folder = String(repeating: "3", count: 32)
        try FileManager.default.createDirectory(at: directory.appendingPathComponent(folder + ".json"), withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        check(DiagnosticReport.journal(traceID: folder) == nil)

        // Reader allowlists, even when someone edits a journal on disk.
        var events: [[String: Any]] = [
            ["stage": "send_email", "elapsed_ms": 1],
            ["stage": "routed", "elapsed_ms": 2, "route_role": "evil", "route_source": "rules", "source": "diary", "source_state": "ready",
             "error_kind": "SecretError", "native_operation": "wire_money", "native_outcome": "maybe", "component": "ghost",
             "tasks": 3, "message": privateText],
            ["stage": "tool_call", "elapsed_ms": 3, "tool_name": "Bad Name!"],
            ["stage": "tool_call", "elapsed_ms": 4, "tool_name": "view_emails"],
            ["stage": "tool_call", "elapsed_ms": 5, "tool_name": "a" + String(repeating: "b", count: 64)],
            ["stage": "tool_call", "elapsed_ms": 6, "tool_name": "9starts_with_digit"],
            ["stage": "tool_result", "elapsed_ms": 7, "tasks": "PRIVATE-COUNT", "blocks": 2.5, "ok": true, "route_role": "agent",
             "source": "email", "source_state": "disabled", "error_kind": "TimeoutError", "native_operation": "create_reminder",
             "native_outcome": "unknown", "component": "native", "route_source": "semantic"],
            ["stage": "failed", "elapsed_ms": 8],
        ]
        let tamper = String(repeating: "4", count: 32)
        try write(["schema_version": 1, "trace_id": tamper, "kind": "PRIVATE-KIND", "status": "evil", "client_terminal_event": "other",
                   "started_at": 1000, "events_dropped": 2, "events": events,
                   "backend_build": ["source_fingerprint": String(repeating: "a", count: 64), "commit": String(repeating: "b", count: 40),
                                     "python_version": "3.12.1", "evil": "value", "extra": String(repeating: "c", count: 40)]],
                  to: directory.appendingPathComponent(tamper + ".json"))
        let clean = DiagnosticReport.journal(traceID: tamper)!
        check(clean["kind"] == nil && clean["status"] == nil && clean["client_terminal_event"] == nil)
        check(clean["started_at"] as? Int == 1000 && clean["events_dropped"] as? Int == 2)
        let rows = clean["events"] as! [[String: Any]]
        check(!rows.contains { $0["stage"] as? String == "send_email" })
        let hostile = rows.first { $0["elapsed_ms"] as? Int == 2 }!
        check(Set(hostile.keys) == ["stage", "elapsed_ms", "route_source", "source_state", "tasks"], "hostile row kept \(hostile.keys.sorted())")
        let named = rows.compactMap { $0["tool_name"] as? String }
        check(named == ["view_emails"], "tool names \(named)")
        let good = rows.first { $0["elapsed_ms"] as? Int == 7 }!
        for key in ["route_role", "source", "source_state", "error_kind", "native_operation", "native_outcome", "component", "route_source", "ok"] {
            check(good[key] != nil, "valid \(key) dropped")
        }
        check(good["tasks"] == nil && good["blocks"] != nil)
        let build = clean["backend_build"] as! [String: String]
        check(build == ["source_fingerprint": String(repeating: "a", count: 64), "commit": String(repeating: "b", count: 40), "python_version": "3.12.1"], "build \(build)")
        let badBuild = String(repeating: "5", count: 32)
        try write(["schema_version": 1, "trace_id": badBuild, "events": [],
                   "backend_build": ["commit": "not-a-commit", "python_version": "three.twelve", "source_fingerprint": "short"]],
                  to: directory.appendingPathComponent(badBuild + ".json"))
        check((DiagnosticReport.journal(traceID: badBuild)!["backend_build"] as! [String: String]).isEmpty)
        // Valid kind, status and terminal event are kept.
        let keepers = String(repeating: "6", count: 32)
        try write(["schema_version": 1, "trace_id": keepers, "kind": "today", "status": "cancelled", "client_terminal_event": "done", "events": []],
                  to: directory.appendingPathComponent(keepers + ".json"))
        let kept = DiagnosticReport.journal(traceID: keepers)!
        check(kept["kind"] as? String == "today" && kept["status"] as? String == "cancelled" && kept["client_terminal_event"] as? String == "done")

        // At most the newest 128 events, in order.
        events = (0..<200).map { ["stage": "tool_result", "elapsed_ms": $0] }
        let many = String(repeating: "7", count: 32)
        try write(["schema_version": 1, "trace_id": many, "events": events], to: directory.appendingPathComponent(many + ".json"))
        let tail = (DiagnosticReport.journal(traceID: many)!["events"] as! [[String: Any]]).compactMap { $0["elapsed_ms"] as? Int }
        check(tail.count == 128 && tail.first == 72 && tail.last == 199, "tail \(tail.count) \(String(describing: tail.first))")

        // The directory itself must be private, real, and owned by the user.
        let beforeMode = directory.path
        check(chmod(beforeMode, 0o755) == 0)
        check(DiagnosticReport.journal(traceID: keepers) == nil, "a group/world readable directory must be refused")
        check(chmod(beforeMode, 0o770) == 0)
        check(DiagnosticReport.journal(traceID: keepers) == nil)
        check(chmod(beforeMode, 0o707) == 0)
        check(DiagnosticReport.journal(traceID: keepers) == nil)
        check(chmod(beforeMode, 0o700) == 0)
        check(DiagnosticReport.journal(traceID: keepers) != nil)
        check(DiagnosticReport.journal(traceID: keepers, expectedUID: getuid() + 1) == nil, "a file owned by someone else must be refused")
        check(DiagnosticReport.journal(traceID: keepers, expectedUID: getuid(), expectedDirectoryUID: getuid() + 1) == nil, "a directory owned by someone else must be refused")
        check(DiagnosticReport.journal(traceID: keepers, expectedUID: getuid(), expectedDirectoryUID: getuid()) != nil)
        let moved = root.appendingPathComponent("diagnostics-real")
        try FileManager.default.moveItem(at: directory, to: moved)
        try FileManager.default.createSymbolicLink(at: directory, withDestinationURL: moved)
        check(DiagnosticReport.journal(traceID: keepers) == nil, "a linked diagnostics directory must be refused")
        try FileManager.default.removeItem(at: directory)
        try FileManager.default.moveItem(at: moved, to: directory)
        check(DiagnosticReport.journal(traceID: keepers) != nil)

        print("\(count) diagnostic checks passed")
    }
}
