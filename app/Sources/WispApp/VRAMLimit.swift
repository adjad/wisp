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
    // The ceiling to pin while Super Model is engaged, derived from the
    // machine's actual RAM rather than a constant.
    //
    // This used to be a flat 20480 MB, measured on a 24 GB Mac where that was
    // already close to the observed automatic default. On a 16 GB machine the
    // same number is a serious over-commit — it hands the GPU more wired
    // memory than the system can spare and pushes everything else into swap.
    //
    // ~83% of physical memory reproduces the original 20 GB on a 24 GB Mac
    // while scaling sanely down (13 GB on 16 GB) and up (~40 GB on 48 GB).
    // macOS's own automatic default is in this neighbourhood; the point of
    // setting it explicitly is to stop the ceiling moving around under load,
    // not to claim more than the hardware has.
    static var superModelLimitMB: Int {
        let physicalMB = Int(ProcessInfo.processInfo.physicalMemory / (1024 * 1024))
        return max(4096, physicalMB * 83 / 100)
    }

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
