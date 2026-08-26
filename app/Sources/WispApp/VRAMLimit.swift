import Foundation

// Temporarily raises Apple Silicon's GPU "wired memory limit" (the effective
// VRAM ceiling for Metal/MLX) while Super Model is active, then restores the
// automatic default when it's turned off. Writing this sysctl needs root —
// this goes through macOS's own Authorization Services dialog
// (`do shell script ... with administrator privileges`). Wisp never sees,
// stores, or handles the credential itself; the OS owns that UI end to end.
//
// Runs the AppleScript via the `/usr/bin/osascript` SYSTEM BINARY (Apple-
// signed) as a subprocess, not NSAppleScript in-process. That's not
// stylistic — SecurityAgent's dialog appears to decide Touch ID eligibility
// partly from the REQUESTING PROCESS's code signature, and Wisp is
// self-signed (the "Wisp Dev" dev cert, not a proper Apple Developer ID).
// Running in-process under Wisp's own signature was the likely reason the
// dialog fell back to password-only; attributing the request to osascript
// (a trusted Apple binary) instead is the fix being tried here.
enum VRAMLimit {
    // 20 GiB, as configured for Super Model. Note: this Mac's observed
    // automatic default was already ~20480MB before Wisp ever touched this
    // sysctl, so raising it explicitly here mostly guarantees the ceiling
    // stays exactly here regardless of what else is running, rather than
    // trusting the dynamic default.
    static let superModelLimitMB = 20480

    static func raise(completion: @escaping (Bool) -> Void) {
        run("sysctl iogpu.wired_limit_mb=\(superModelLimitMB)", completion: completion)
    }

    // 0 restores macOS's own automatic default (it stops overriding the limit).
    static func restoreDefault(completion: @escaping (Bool) -> Void) {
        run("sysctl iogpu.wired_limit_mb=0", completion: completion)
    }

    private static func run(_ shellCommand: String, completion: @escaping (Bool) -> Void) {
        // Escape for embedding inside the AppleScript string literal.
        let escaped = shellCommand
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "do shell script \"\(escaped)\" with administrator privileges"

        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = ["-e", script]   // no shell involved, no further escaping needed
            process.standardOutput = Pipe()      // discard — silences noisy stdout in logs
            process.standardError = Pipe()

            do {
                try process.run()
                process.waitUntilExit()           // blocks until the auth dialog is resolved
                DispatchQueue.main.async { completion(process.terminationStatus == 0) }
            } catch {
                DispatchQueue.main.async { completion(false) }
            }
        }
    }
}
