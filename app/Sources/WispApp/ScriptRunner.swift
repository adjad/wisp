import AppKit
import Foundation

// Copy/run/open actions for a code block MarkdownView renders. Every action
// here is triggered by an explicit button click the user can see and choose
// not to press — nothing executes automatically. "Run" first shows the whole
// script and asks (Cancel is the default), then opens a new, visible Terminal
// window running the exact command (via `do script`), never silently in the
// background, so what runs is always in plain sight.
enum ScriptRunner {
    // Where a code block's language tag says it belongs. `interpreter == nil`
    // for shell/sh/bash/zsh means "run the file directly" (chmod +x + execute);
    // otherwise the interpreter is prefixed onto the temp file's path.
    enum RunTarget {
        case terminal(interpreter: String?, ext: String)
        case safari
        case pages

        var label: String {
            switch self {
            case .terminal: return "Run in Terminal"
            case .safari: return "Open in Safari"
            case .pages: return "Open in Pages"
            }
        }
        var icon: String {
            switch self {
            case .terminal: return "terminal"
            case .safari: return "safari"
            case .pages: return "doc.richtext"
            }
        }
    }

    // nil (not `.pages`) means "no run button" — most languages (json/yaml/
    // css/swift/etc.) aren't sensibly "runnable" from a single file, so only
    // Copy shows for those. An UNTAGGED block (b.codeLang == "") is treated
    // as prose/document content bound for Pages, not code.
    static func runTarget(forLang lang: String) -> RunTarget? {
        switch lang.lowercased() {
        case "bash", "sh", "zsh", "shell": return .terminal(interpreter: nil, ext: "sh")
        case "python", "py": return .terminal(interpreter: "python3", ext: "py")
        case "javascript", "js", "node": return .terminal(interpreter: "node", ext: "js")
        case "applescript", "osascript": return .terminal(interpreter: "osascript", ext: "applescript")
        case "html", "htm": return .safari
        case "": return .pages
        default: return nil
        }
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    static func perform(_ target: RunTarget, code: String) {
        switch target {
        case .terminal(let interpreter, let ext):
            // The model wrote this code: nothing runs until the person has seen all of it
            // and chosen to. Button actions arrive on the main thread.
            let confirmed = MainActor.assumeIsolated { ScriptRunner.confirmTerminalRun(code, interpreter: interpreter) }
            guard confirmed else { return }
            runInTerminal(code, interpreter: interpreter, ext: ext)
        case .safari:
            guard let path = writeTemp(code, ext: "html") else { return }
            openWithApp("Safari", path: path)
        case .pages:
            guard let path = writeTemp(code, ext: "txt") else { return }
            openWithApp("Pages", path: path)
        }
    }

    /// What runs the script, in the person's words.
    static func interpreterName(_ interpreter: String?) -> String {
        guard let interpreter else { return "the shell (sh/zsh)" }
        switch interpreter {
        case "python3": return "Python (python3)"
        case "node": return "JavaScript (node)"
        case "osascript": return "AppleScript (osascript)"
        default: return interpreter
        }
    }

    /// Shows the full script, scrollable and selectable, and asks. Cancel is the default
    /// button (Return cancels); only choosing "Run in Terminal" runs it.
    @MainActor
    static func confirmTerminalRun(_ code: String, interpreter: String?) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Run this script in Terminal?"
        alert.informativeText = "It runs with \(interpreterName(interpreter)) on this Mac, with Terminal's "
            + "permissions, so it can read, change or delete your files. Read it before you run it."
        let cancel = alert.addButton(withTitle: "Cancel")
        cancel.keyEquivalent = "\r"
        let run = alert.addButton(withTitle: "Run in Terminal")
        run.keyEquivalent = ""
        run.hasDestructiveAction = true

        let scroll = NSTextView.scrollableTextView()
        scroll.frame = NSRect(x: 0, y: 0, width: 520, height: 260)
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        if let text = scroll.documentView as? NSTextView {
            text.isEditable = false
            text.isSelectable = true
            text.isRichText = false
            text.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
            text.textContainerInset = NSSize(width: 6, height: 6)
            text.string = code
            text.setAccessibilityLabel("The script that will run")
        }
        alert.accessoryView = scroll
        alert.layout()
        // The notch and the chat sit above ordinary windows; the question must not hide behind them.
        let above = NSWindow.Level(rawValue: max(NSWindow.Level.statusBar.rawValue,
                                                 DisplayGeometry.shared.layout.windowLevel.rawValue) + 1)
        alert.window.level = above
        // runModal puts the alert back at the modal-panel level, under an open notch, which
        // then covers the script. Raise it again once the modal session is running.
        let window = alert.window
        RunLoop.main.perform(inModes: [.modalPanel, .common]) {
            window.level = above
            window.orderFrontRegardless()
        }
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertSecondButtonReturn
    }

    private static func runInTerminal(_ code: String, interpreter: String?, ext: String) {
        guard let path = writeTemp(code, ext: ext) else { return }
        let command: String
        if let interpreter {
            command = "\(interpreter) \(shellQuote(path))"
        } else {
            chmodExecutable(path)
            command = shellQuote(path)
        }
        // AppleScript string literal: escape backslashes first, then quotes.
        let escaped = command
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let source = """
        tell application "Terminal"
            activate
            do script "\(escaped)"
        end tell
        """
        guard let script = NSAppleScript(source: source) else { return }
        var err: NSDictionary?
        script.executeAndReturnError(&err)
    }

    private static func openWithApp(_ appName: String, path: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        p.arguments = ["-a", appName, path]
        try? p.run()
    }

    private static func writeTemp(_ content: String, ext: String) -> String? {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("wisp-\(UUID().uuidString).\(ext)")
        do {
            try content.write(to: path, atomically: true, encoding: .utf8)
            return path.path
        } catch {
            return nil
        }
    }

    private static func chmodExecutable(_ path: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/chmod")
        p.arguments = ["+x", path]
        try? p.run()
        p.waitUntilExit()
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
