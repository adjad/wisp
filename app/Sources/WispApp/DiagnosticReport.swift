import AppKit
import SwiftUI
import UniformTypeIdentifiers
import Darwin

/// A local, reviewable report. No upload, agent call, or native action occurs.
enum DiagnosticReport {
    static let maximumBytes = 2 * 1024 * 1024

    static func journal(traceID: String?, expectedUID: uid_t = getuid(), expectedDirectoryUID: uid_t? = nil) -> [String: Any]? {
        guard let traceID, traceID.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { return nil }
        let root = ProcessInfo.processInfo.environment["WISP_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".moe")
        let directory = root.appendingPathComponent("diagnostics")
        let dirFD = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard dirFD >= 0 else { return nil }
        defer { close(dirFD) }
        var dirInfo = stat()
        guard fstat(dirFD, &dirInfo) == 0, dirInfo.st_uid == (expectedDirectoryUID ?? expectedUID), dirInfo.st_mode & 0o077 == 0 else { return nil }
        let fd = openat(dirFD, traceID + ".json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == expectedUID, info.st_mode & 0o077 == 0,
              info.st_size > 0, info.st_size <= maximumBytes else { return nil }
        var bytes = [UInt8](repeating: 0, count: Int(info.st_size))
        var offset = 0
        while offset < bytes.count {
            let count = bytes.withUnsafeMutableBytes { buffer in
                read(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
            }
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { return nil }
            offset += count
        }
        guard let object = try? JSONSerialization.jsonObject(with: Data(bytes)) as? [String: Any],
              object["trace_id"] as? String == traceID, object["schema_version"] as? Int == 1 else { return nil }
        // Rebuild the allowlist even if someone edited a journal on disk.
        let labels: Set<String> = ["agent", "today", "daily_brief", "source_sync", "native", "started", "completed", "failed", "cancelled", "routed", "tool_call", "tool_result", "confirm", "approved", "denied", "error", "done", "sources", "plan", "requested", "result", "waiting", "unavailable", "unknown"]
        let numeric = ["tasks", "blocks", "unscheduled", "warnings", "revision", "calendar_ready", "reminders_ready", "ok", "events_dropped", "elapsed_ms"]
        var result: [String: Any] = ["schema_version": 1, "trace_id": traceID]
        for key in ["kind", "status"] {
            if let value = object[key] as? String, labels.contains(value) { result[key] = value }
        }
        if let value = object["client_terminal_event"] as? String, ["done", "error"].contains(value) { result["client_terminal_event"] = value }
        for key in ["started_at", "events_dropped"] {
            if let value = object[key] as? NSNumber, value.doubleValue.isFinite { result[key] = value }
        }
        result["events"] = ((object["events"] as? [[String: Any]]) ?? []).suffix(128).compactMap { event -> [String: Any]? in
            guard let stage = event["stage"] as? String, labels.contains(stage) else { return nil }
            var row: [String: Any] = ["stage": stage]
            for key in numeric {
                if let value = event[key] as? NSNumber, value.doubleValue.isFinite { row[key] = value }
            }
            for (key, choices) in ["error_kind": ["TimeoutError", "ConnectionError", "PermissionError", "ValueError", "RuntimeError", "OSError", "KeyError", "HTTPError", "CancelledError"],
                                   "source": ["calendar", "reminders", "email", "messages", "notes", "browser_history"],
                                   "source_state": ["ready", "syncing", "unavailable", "disabled"],
                                   "native_operation": ["create_calendar_event", "delete_calendar_event", "create_reminder", "update_reminder", "complete_reminder", "delete_reminder"],
                                   "native_outcome": ["succeeded", "failed", "unknown"],
                                   "component": ["agent", "today", "daily_brief", "source_sync", "native"],
                                   "route_role": ["fast", "agent", "general", "coding", "reasoning"],
                                   "route_source": ["rules", "fallback", "default", "semantic", "model"]] {
                if let value = event[key] as? String, choices.contains(value) { row[key] = value }
            }
            if let value = event["tool_name"] as? String, value.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil { row["tool_name"] = value }
            return row
        }
        if let build = object["backend_build"] as? [String: Any] {
            result["backend_build"] = build.filter { key, value in
                let pattern = key == "python_version" ? "^[0-9]+\\.[0-9]+\\.[0-9]+$" : "^[a-f0-9]{40,64}$"
                return ["source_fingerprint", "commit", "python_version"].contains(key) && (value as? String)?.range(of: pattern, options: .regularExpression) != nil
            }
        }
        return result
    }

    static let redacted = "[REDACTED]"

    /// Defense in depth for explicitly included content. This is best effort:
    /// the preview remains the authority. It covers the credential shapes listed
    /// in docs/DEBUGGING.md; private prose and unfamiliar formats may remain.
    static func scrub(_ value: Any) -> Any {
        if let object = value as? [String: Any] {
            var result: [String: Any] = [:]
            for key in object.keys.sorted() {
                let child = object[key]!
                let name = scrubText(key)
                let sensitive = isSensitive(key: key, value: child)
                var unique = name, suffix = 2
                while result[unique] != nil { unique = "\(name)#\(suffix)"; suffix += 1 }
                result[unique] = sensitive ? redacted as Any : scrub(child)
            }
            return result
        }
        if let values = value as? [Any] { return values.map { scrub($0) } }
        if let text = value as? String {
            if let data = text.data(using: .utf8), let parsed = try? JSONSerialization.jsonObject(with: data),
               parsed is [String: Any] || parsed is [Any] {
                let clean = scrub(parsed)
                if JSONSerialization.isValidJSONObject(clean),
                   let sanitized = try? JSONSerialization.data(withJSONObject: clean, options: [.sortedKeys, .withoutEscapingSlashes]),
                   let encoded = String(data: sanitized, encoding: .utf8) { return encoded }
            }
            return scrubText(text)
        }
        if let number = value as? NSNumber, !number.doubleValue.isFinite { return "[non-finite number]" }
        return value
    }

    /// Replaces numbers JSON cannot represent (NaN, infinities, overflow such as
    /// 1e999) so serialization can never raise an Objective-C exception.
    static func finiteJSON(_ value: Any) -> Any {
        if let object = value as? [String: Any] { return object.mapValues { finiteJSON($0) } }
        if let values = value as? [Any] { return values.map { finiteJSON($0) } }
        if let number = value as? NSNumber, !number.doubleValue.isFinite { return "[non-finite number]" }
        return value
    }

    private static func normalized(_ text: String) -> String {
        // Invisible format characters (zero width, bidi, soft hyphen) and control
        // characters are removed; compatibility mapping folds full-width letters.
        var scalars = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            let category = scalar.properties.generalCategory
            if category == .format { continue }
            if category == .control && scalar != "\n" && scalar != "\r" && scalar != "\t" { continue }
            scalars.append(scalar)
        }
        return String(scalars).precomposedStringWithCompatibilityMapping
    }

    private static let sensitiveTerms = ["password", "passwd", "passphrase", "passcode", "secret", "token", "authorization", "authorisation",
                                         "apikey", "credential", "cookie", "privatekey", "accesskey", "signingkey", "encryptionkey",
                                         "masterkey", "licensekey", "sessionid", "sessionkey", "bearer", "oauth"]

    /// Substring match on a normalized key (case, separators and invisible
    /// characters ignored), so api_key, x-api-key, openrouterApiKey, client_secret
    /// and access_token are all recognized. A counter such as max_tokens or
    /// client_first_token_seconds is a number, not a secret.
    static func isSensitive(key: String, value: Any) -> Bool {
        let compact = String(normalized(key).lowercased().filter { $0.isLetter || $0.isNumber })
        let terms = sensitiveTerms.filter { compact.contains($0) }
        if !terms.isEmpty {
            if terms == ["token"], value is NSNumber { return false }
            return true
        }
        return !(value is NSNumber) && (compact == "auth" || (compact.hasPrefix("auth") && !compact.hasPrefix("author")))
    }

    private static func loose(_ word: String) -> String {
        word.map { String($0) }.joined(separator: "[\\s_.\\-]*")
    }

    private static let valueAfterLabel = "(?:(?:Bearer|Basic|Token|Digest|Bot)\\s+)?[^\\s,;&\"'<>)\\]}]+"
    private static let quotedValue = "(?:\"(?:\\\\.|[^\"\\\\\\n])*\"|'[^'\\n]*')"

    private static let textRules: [(NSRegularExpression, String)] = {
        let gap: String = "[\\s_.\\-]*"
        let tail: String = "(?:[_\\-.][A-Za-z0-9]{1,20}){0,3}"
        let prefixes: [String] = ["access", "secret", "private", "signing", "encryption", "master", "license", "session", "ssh"]
        let compound: String = "(?:" + prefixes.map { loose($0) }.joined(separator: "|") + ")" + gap + loose("key")
        var names: [String] = []
        for word in ["password", "passwd", "passphrase", "passcode", "secret"] { names.append(loose(word)) }
        names.append(loose("token"))
        names.append("credentials?")
        names.append("cookie")
        names.append(loose("authorization"))
        names.append(loose("authorisation"))
        names.append(loose("api") + gap + loose("key"))
        names.append(compound)
        names.append(loose("session") + gap + loose("id"))
        let label: String = "(?:" + names.joined(separator: "|") + ")" + tail
        let assigned: String = "(" + label + "[\"']?\\s*(?:[:=]>?)\\s*)"
        var spokenNames: [String] = []
        for word in ["password", "passwd", "passphrase", "passcode", "secret"] { spokenNames.append(loose(word)) }
        let spoken: String = "(\\b(?:" + spokenNames.joined(separator: "|") + ")\\s+(?:is|are|was)\\s+:?\\s*)"
        let mark: String = redacted
        var rules: [(String, String)] = []
        // Whole blocks and structured credentials first.
        rules.append(("-----BEGIN [A-Z0-9 ]*KEY[A-Z0-9 ]*-----[\\s\\S]*?(?:-----END [A-Z0-9 ]*-----|\\z)", mark))
        rules.append(("(?i)(\\b[a-z][a-z0-9+.\\-]{1,24}://)[^/\\s:@]+:[^@\\s/]+@", "$1" + mark + "@"))
        rules.append(("(?i)\\b((?:set-)?cookie\\s*:\\s*)[^\\r\\n]+", "$1" + mark))
        rules.append(("(?i)" + assigned + quotedValue, "$1\"" + mark + "\""))
        rules.append(("(?i)" + assigned + "(?!\"|')(?!\\[REDACTED\\])" + valueAfterLabel, "$1" + mark))
        rules.append(("(?i)" + spoken + "(?:" + quotedValue + "|(?!\\[REDACTED\\])[^\\s,;]+)", "$1" + mark))
        // Credential shapes that identify themselves.
        rules.append(("(?i)\\bBearer\\s+[A-Za-z0-9._~+/%\\-]{6,}=*", "Bearer " + mark))
        rules.append(("(?i)\\bBasic\\s+(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{16,}={0,2}", "Basic " + mark))
        rules.append(("\\beyJ[A-Za-z0-9_\\-]{5,}\\.[A-Za-z0-9_\\-]{5,}\\.[A-Za-z0-9_\\-]*", mark))
        rules.append(("\\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[A-Z0-9]{16}\\b", mark))
        rules.append(("\\bAIza[0-9A-Za-z_\\-]{30,}", mark))
        rules.append(("\\bxox[abprs]-[A-Za-z0-9\\-]{8,}", mark))
        rules.append(("\\bxapp-[A-Za-z0-9\\-]{8,}", mark))
        rules.append(("\\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{20,})", mark))
        rules.append(("\\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}", mark))
        rules.append(("(?i)\\bsk(?:-|%2D)(?:[A-Za-z0-9_\\-]|%2D|%5F){8,}", mark))
        var compiled: [(NSRegularExpression, String)] = []
        for (pattern, template) in rules {
            if let regex = try? NSRegularExpression(pattern: pattern) { compiled.append((regex, template)) }
        }
        assert(compiled.count == rules.count, "A redaction pattern failed to compile")
        return compiled
    }()

    static func scrubText(_ text: String) -> String {
        // Match on a normalized copy; keep the original unless something matched,
        // so ordinary text (emoji sequences, accents) is not rewritten.
        var candidate = normalized(text)
        var changed = false
        for (regex, template) in textRules {
            let range = NSRange(candidate.startIndex..., in: candidate)
            let replaced = regex.stringByReplacingMatches(in: candidate, range: range, withTemplate: template)
            if replaced != candidate { candidate = replaced; changed = true }
        }
        return changed ? candidate : text
    }

    static func payload(metadata: [String: Any], details: [String: Any]?, note: String, includeDetails: Bool,
                        detailsAreScrubbed: Bool = false) -> [String: Any] {
        var result = metadata
        result["schema_version"] = 1
        result["expected_result"] = note
        result["details_included"] = includeDetails
        result["os_version"] = ProcessInfo.processInfo.operatingSystemVersionString
        result["app_build"] = ["version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
                               "build": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown",
                               "commit": Bundle.main.object(forInfoDictionaryKey: "WispSourceCommit") as? String ?? "unknown"]
        if includeDetails, let details, !detailsAreScrubbed { result["details"] = details }
        var scrubbed = scrub(result) as! [String: Any]
        if includeDetails, let details, detailsAreScrubbed { scrubbed["details"] = details }
        return scrubbed
    }

    static func encoded(_ payload: [String: Any]) throws -> Data {
        guard JSONSerialization.isValidJSONObject(payload) else {
            throw NSError(domain: "Diagnostics", code: 2, userInfo: [NSLocalizedDescriptionKey: "Report contains content that cannot be written as JSON."])
        }
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        guard data.count <= maximumBytes else { throw NSError(domain: "Diagnostics", code: 1, userInfo: [NSLocalizedDescriptionKey: "Report exceeds 2 MB. Turn off detailed content or shorten your note."]) }
        return data
    }

    /// Writes a report that is private from its first byte: a temporary file in
    /// the destination directory is created exclusively with mode 0600, written,
    /// synced and atomically renamed over the destination. Any failure removes
    /// the temporary file and is reported to the caller.
    static func save(_ data: Data, to url: URL, inspectTemporary: ((String) -> Void)? = nil) throws {
        let destination = url.path
        let directory = (destination as NSString).deletingLastPathComponent
        let temporary = directory + "/.wisp-report-" + UUID().uuidString + ".tmp"
        func failure(_ message: String) -> NSError {
            NSError(domain: "Diagnostics", code: 3, userInfo: [NSLocalizedDescriptionKey: "\(message): \(String(cString: strerror(errno)))"])
        }
        let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw failure("Could not create the report file") }
        var finished = false
        defer {
            if fd >= 0 { close(fd) }
            if !finished { unlink(temporary) }
        }
        inspectTemporary?(temporary)
        guard fchmod(fd, 0o600) == 0 else { throw failure("Could not make the report private") }
        var offset = 0
        try data.withUnsafeBytes { (buffer: UnsafeRawBufferPointer) in
            while offset < buffer.count {
                let written = write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if written < 0 && errno == EINTR { continue }
                guard written > 0 else { throw failure("Could not write the report") }
                offset += written
            }
        }
        guard fsync(fd) == 0 else { throw failure("Could not flush the report") }
        guard rename(temporary, destination) == 0 else { throw failure("Could not save the report") }
        finished = true
        var info = stat()
        guard lstat(destination, &info) == 0, info.st_mode & 0o777 == 0o600 else {
            unlink(destination)
            throw failure("Report file was not private, so it was removed")
        }
    }

    @MainActor private static var windows: [NSWindowController] = []

    @MainActor static func present(metadata: [String: Any], details: [String: Any]? = nil) {
        windows.removeAll { $0.window?.isVisible != true }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 580), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "Report a problem"
        window.contentView = NSHostingView(rootView: DiagnosticReportView(metadata: metadata, details: details))
        window.center()
        let controller = NSWindowController(window: window)
        windows.append(controller)
        controller.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

private struct DiagnosticReportView: View {
    let metadata: [String: Any]
    let details: [String: Any]?
    /// Scrubbed once: redaction of a large report is too slow to repeat per keystroke.
    private let scrubbedDetails: [String: Any]?
    @State private var note = ""
    @State private var includeDetails = false
    @State private var status = ""
    init(metadata: [String: Any], details: [String: Any]?) {
        self.metadata = metadata
        self.details = details
        self.scrubbedDetails = details.flatMap { DiagnosticReport.scrub($0) as? [String: Any] }
    }
    private var payload: [String: Any] {
        DiagnosticReport.payload(metadata: metadata, details: scrubbedDetails, note: note, includeDetails: includeDetails, detailsAreScrubbed: true)
    }
    private var preview: String { (try? DiagnosticReport.encoded(payload)).flatMap { String(data: $0, encoding: .utf8) } ?? "Report is too large to save. Turn off detailed content or shorten your note." }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What did you expect Wisp to do? (optional)").font(.headline)
            TextEditor(text: $note).frame(height: 65).border(Color.secondary.opacity(0.3))
            if details != nil {
                Toggle("Include conversation or planner details", isOn: $includeDetails)
                Text("Detailed content can include private messages, titles, and model inputs. Automatic redaction of passwords, tokens and keys is best effort and cannot catch everything. Review the preview before sharing.").font(.caption).foregroundStyle(.secondary)
            }
            Text("Preview · saved locally, never uploaded").font(.headline)
            ScrollView { Text(verbatim: preview).font(.system(size: 11, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                .padding(8).background(Color.secondary.opacity(0.06))
            HStack {
                Text(status).font(.caption)
                Spacer()
                Button("Save report…") {
                    let panel = NSSavePanel()
                    panel.allowedContentTypes = [.json]
                    panel.nameFieldStringValue = "Wisp-problem-\(Int(Date().timeIntervalSince1970)).json"
                    guard panel.runModal() == .OK, let url = panel.url else { return }
                    do {
                        let data = try DiagnosticReport.encoded(payload)
                        // Private from the first byte, atomically replaced; failures are visible.
                        try DiagnosticReport.save(data, to: url)
                        status = "Saved \(url.lastPathComponent)"
                    } catch { status = "Could not save report: \(error.localizedDescription)" }
                }.keyboardShortcut(.defaultAction)
            }
        }.padding(20).frame(minWidth: 520, minHeight: 460)
    }
}
