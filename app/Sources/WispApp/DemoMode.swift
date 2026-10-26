import AppKit

/// The notch's /demo: a fresh-install Wisp that reads the user's real sources, then
/// the user's own Wisp back exactly (service/demo_mode.py, docs/fresh-demo.md).
///
/// Wisp cannot swap ~/.moe from under its own backend and engine, so it hands the
/// work to a detached helper run by its bundled Python, then quits. The helper waits
/// for Wisp to be gone, swaps the homes and reopens this app.
enum DemoMode {
    /// Written by the helper when a demo starts and removed when it ends.
    static var isActive: Bool {
        FileManager.default.fileExists(atPath: NSHomeDirectory() + "/.moe-demo-saved/.wisp-demo.json")
    }

    /// Ask first, then hand off and quit. Returns a message for the notch when
    /// the demo cannot start or end; nil when it is under way or was cancelled.
    @MainActor
    static func toggle() -> String? {
        guard let root = Bundle.main.resourceURL?.appendingPathComponent("backend"),
              FileManager.default.isExecutableFile(atPath: root.appendingPathComponent(".venv/bin/python").path)
        else { return "/demo needs the installed Wisp. From a checkout, run scripts/wisp_demo.py." }

        let ending = isActive
        let alert = NSAlert()
        if ending {
            alert.messageText = "End the demo?"
            alert.informativeText = "Wisp will quit and reopen as your own Wisp, exactly as you left it. "
                + "The demo's state is kept in ~/.moe-demo-last until the next demo."
            alert.addButton(withTitle: "End Demo")
        } else {
            alert.messageText = "Start a fresh demo?"
            alert.informativeText = "Wisp will quit and reopen as if newly installed: no Brief, cards, "
                + "memory or profile yet, rebuilt from your real Calendar, Mail, Messages and Notes. "
                + "Your own Wisp is set aside untouched. Type /demo again to bring it back. "
                + "Sends still ask before anything goes out."
            alert.addButton(withTitle: "Start Demo")
        }
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }

        let process = Process()
        process.executableURL = root.appendingPathComponent(".venv/bin/python")
        process.currentDirectoryURL = root
        process.arguments = ["-m", "service.demo_mode", ending ? "stop" : "start",
                             "--detach", "--wait", "90", "--relaunch", Bundle.main.bundlePath]
        // Never write bytecode into the signed bundle.
        process.environment = ProcessInfo.processInfo.environment
            .merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        let errors = Pipe()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = errors
        do { try process.run() } catch {
            return "Couldn't start the demo helper: \(error.localizedDescription)"
        }
        // Returns as soon as the helper's checks pass and it detaches.
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let text = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            let reason = text.replacingOccurrences(of: "error: ", with: "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return reason.isEmpty ? "The demo couldn't \(ending ? "end" : "start")." : reason
        }
        NSApp.terminate(nil)
        return nil
    }
}
