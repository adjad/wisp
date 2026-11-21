import Foundation
import Darwin

/// macOS's GPU wired-memory limit, and the one Terminal command that raises it for Ideate.
///
/// Wisp's engine sets its memory ceiling to 90% of this Mac's memory (service/engine/manager.py),
/// but the engine can only use what macOS lets the GPU wire: `iogpu.wired_limit_mb`, or macOS's
/// own default (a smaller share of memory) when it is 0. When the limit is below 90%, Ideate's
/// big model has little room to read a prompt, so the tab offers the command.
///
/// Wisp never runs it: the command is shown as a `bash` block, which goes through the same
/// "Run this script in Terminal?" card as any code block (whole command shown, Cancel is the
/// default, nothing runs before the second press). Terminal asks for the password; Wisp never
/// sees or stores it, and nothing makes the change permanent (it resets when the Mac restarts).
/// Pure apart from `read()`.
struct IdeateGPULimit: Equatable {
    /// The engine's ceiling share (MEMORY_CEILING_SHARE in service/engine/manager.py).
    static let sharePercent: UInt64 = 90

    /// Physical memory in bytes (`hw.memsize`).
    var physicalBytes: UInt64
    /// `iogpu.wired_limit_mb`; 0 means macOS's default, -1 that it couldn't be read.
    var currentMB: Int

    /// 90% of memory in MB, rounded down (22118 on a 24 GB Mac).
    var neededMB: Int { Int(physicalBytes / 100 * Self.sharePercent / 1_048_576) }

    /// The limit is known and below what the engine's ceiling needs. An unreadable limit or
    /// memory size shows nothing.
    var isLow: Bool { physicalBytes > 0 && currentMB >= 0 && currentMB < neededMB }

    var command: String { "sudo sysctl iogpu.wired_limit_mb=\(neededMB)" }

    /// The block MarkdownView draws with its Copy and Run in Terminal buttons.
    var codeBlock: String { "```bash\n\(command)\n```" }

    static let title = "Ideate needs more memory for the GPU"
    static let detail = "Run one command in Terminal to allow it. macOS asks for your password there, "
        + "and Wisp never sees it. It resets when you restart the Mac."
    static let skipNote = "Skip, and Ideate keeps to small steps."
    static let raised = "GPU memory raised for Ideate. It resets when you restart the Mac."

    /// What this Mac says now. Reading needs no password.
    static func read() -> IdeateGPULimit {
        // An int today; an 8-byte buffer also reads it if it grows (little-endian, zeroed).
        var mb: Int64 = 0
        var size = MemoryLayout<Int64>.size
        let ok = sysctlbyname("iogpu.wired_limit_mb", &mb, &size, nil, 0) == 0
        return IdeateGPULimit(physicalBytes: ProcessInfo.processInfo.physicalMemory, currentMB: ok ? Int(mb) : -1)
    }
}
