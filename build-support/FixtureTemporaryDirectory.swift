// Linked only into native fixture programs, never into WispApp.
// Foundation on this host ignores TMPDIR for FileManager.temporaryDirectory.
// Honor the runner's per-gate directory without granting writes to shared temp.
import Foundation

extension FileManager {
    var temporaryDirectory: URL {
        guard let path = ProcessInfo.processInfo.environment["TMPDIR"],
              path.hasPrefix("/"), fileExists(atPath: path) else {
            preconditionFailure("Native fixture requires an existing absolute TMPDIR")
        }
        return URL(fileURLWithPath: path, isDirectory: true)
    }
}
