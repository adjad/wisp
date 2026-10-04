import AppKit
import SwiftUI
import UniformTypeIdentifiers
import Darwin

/// A local, reviewable report. No upload, agent call, or native action occurs.
enum DiagnosticReport {
    static let maximumBytes = 2 * 1024 * 1024

    static func journal(traceID: String?) -> [String: Any]? {
        guard let traceID, traceID.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil else { return nil }
        let root = ProcessInfo.processInfo.environment["WISP_HOME"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".moe")
        let directory = root.appendingPathComponent("diagnostics")
        let dirFD = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard dirFD >= 0 else { return nil }
        defer { close(dirFD) }
        var dirInfo = stat()
        guard fstat(dirFD, &dirInfo) == 0, dirInfo.st_uid == getuid(), dirInfo.st_mode & 0o077 == 0 else { return nil }
        let fd = openat(dirFD, traceID + ".json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_mode & 0o077 == 0,
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

    /// Defense in depth for explicitly included content. Preview remains the
    /// authority: private prose and unfamiliar credential formats may remain.
    static func scrub(_ value: Any) -> Any {
        if let object = value as? [String: Any] {
            return object.mapValues { scrub($0) }.map { key, value in
                let sensitive = key.range(of: "(?i)^(password|secret|token|authorization|api[_-]?key|credentials?|cookies?|access[_-]?token|refresh[_-]?token|client[_-]?secret)$", options: .regularExpression) != nil
                return (key, sensitive ? "[REDACTED]" as Any : value)
            }.reduce(into: [String: Any]()) { $0[$1.0] = $1.1 }
        }
        if let values = value as? [Any] { return values.map { scrub($0) } }
        if var text = value as? String {
            if let data = text.data(using: .utf8), let parsed = try? JSONSerialization.jsonObject(with: data),
               parsed is [String: Any] || parsed is [Any] {
                if let sanitized = try? JSONSerialization.data(withJSONObject: scrub(parsed), options: [.sortedKeys]),
                   let encoded = String(data: sanitized, encoding: .utf8) { return encoded }
            }
            for pattern in ["(?i)Bearer\\s+[A-Za-z0-9._~+/-]+=*", "\\bsk-[A-Za-z0-9_-]{8,}", "\\bgh[pousr]_[A-Za-z0-9_]{10,}", "(?i)(api[_-]?key|password|secret|access[_-]?token)\\s*[:=]\\s*[^\\s,;]+", "https?://[^/\\s:@]+:[^@\\s]+@"] {
                if let regex = try? NSRegularExpression(pattern: pattern) {
                    text = regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "[REDACTED]")
                }
            }
            return text
        }
        return value
    }

    static func payload(metadata: [String: Any], details: [String: Any]?, note: String, includeDetails: Bool) -> [String: Any] {
        var result = metadata
        result["schema_version"] = 1
        result["expected_result"] = note
        result["details_included"] = includeDetails
        result["os_version"] = ProcessInfo.processInfo.operatingSystemVersionString
        result["app_build"] = ["version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development",
                               "build": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "unknown",
                               "commit": Bundle.main.object(forInfoDictionaryKey: "WispSourceCommit") as? String ?? "unknown"]
        if includeDetails, let details { result["details"] = details }
        return scrub(result) as! [String: Any]
    }

    static func encoded(_ payload: [String: Any]) throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        guard data.count <= maximumBytes else { throw NSError(domain: "Diagnostics", code: 1, userInfo: [NSLocalizedDescriptionKey: "Report exceeds 2 MB. Turn off detailed content or shorten your note."]) }
        return data
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
    @State private var note = ""
    @State private var includeDetails = false
    @State private var status = ""
    private var payload: [String: Any] { DiagnosticReport.payload(metadata: metadata, details: details, note: note, includeDetails: includeDetails) }
    private var preview: String { (try? DiagnosticReport.encoded(payload)).flatMap { String(data: $0, encoding: .utf8) } ?? "Report is too large to save. Turn off detailed content or shorten your note." }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("What did you expect Wisp to do? (optional)").font(.headline)
            TextEditor(text: $note).frame(height: 65).border(Color.secondary.opacity(0.3))
            if details != nil {
                Toggle("Include conversation or planner details", isOn: $includeDetails)
                Text("Detailed content can include private messages, titles, and model inputs. Review the preview before sharing.").font(.caption).foregroundStyle(.secondary)
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
                        // Atomic replacement and private permissions; failures are visible.
                        try data.write(to: url, options: [.atomic, .completeFileProtection])
                        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
                        status = "Saved \(url.lastPathComponent)"
                    } catch { status = "Could not save report: \(error.localizedDescription)" }
                }.keyboardShortcut(.defaultAction)
            }
        }.padding(20).frame(minWidth: 520, minHeight: 460)
    }
}
