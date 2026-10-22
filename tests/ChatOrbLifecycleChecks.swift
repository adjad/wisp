import AppKit
import CryptoKit
import SwiftUI

#if !WISP_ORB_QA
#error("Compile this isolated fixture with -D WISP_ORB_QA")
#endif

// A standalone synthetic fixture: compile with the real Theme.swift,
// Math.swift, ScriptRunner.swift, MarkdownView.swift and Chat/ChatTheme.swift.
// Only ChatOrb is mounted: no Markdown actions, Wisp launcher or backend.
// Set WISP_ORB_QA_OUT, or the fixture bundle's WispOrbQAOutputDirectory key,
// to a disposable output directory before launching it normally.
@MainActor
private final class OrbState: ObservableObject {
    @Published var thinking = false
    @Published var reduceMotion = false
    @Published var present = true
}

private struct OrbFixture: View {
    @ObservedObject var state: OrbState

    var body: some View {
        ZStack {
            Color.black
            if state.present {
                ChatOrb(size: 76, thinking: state.thinking,
                        reducedMotionForQA: state.reduceMotion)
            }
        }
        .frame(width: 240, height: 240)
    }
}

@main
@MainActor
private struct ChatOrbLifecycleChecks {
    private static func cpuSeconds() -> Double {
        var usage = rusage()
        getrusage(RUSAGE_SELF, &usage)
        return Double(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec)
            + Double(usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1_000_000
    }

    private static func frame(_ view: NSView) throws -> Data {
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw NSError(domain: "OrbQA", code: 1, userInfo: [NSLocalizedDescriptionKey: "No view bitmap"])
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "OrbQA", code: 2, userInfo: [NSLocalizedDescriptionKey: "No PNG"])
        }
        return png
    }

    private static func run(state: OrbState, host: NSView, output: URL) async throws -> Bool {
        // Uneven 190ms sampling avoids matching the 2.6-second repeat period.
        // The moving cases are essential positive controls: if view snapshots
        // cannot observe animation, stable idle frames prove nothing.
        let phases: [(String, Bool, Bool, Bool, Bool)] = [
            ("idle", false, false, true, false),
            ("thinking", true, false, true, true),
            ("settled", false, false, true, false),
            ("thinking-restarted", true, false, true, true),
            ("motion-reduced", true, true, true, false),
            ("motion-restored", true, false, true, true),
            ("unmounted", true, false, false, false),
            ("thinking-remounted", true, false, true, true),
            ("idle-again", false, false, true, false),
        ]
        var results: [[String: Any]] = []
        for (name, thinking, reduced, present, moving) in phases {
            state.thinking = thinking
            state.reduceMotion = reduced
            state.present = present
            try await Task.sleep(for: .milliseconds(450))
            let start = Date()
            let cpuStart = cpuSeconds()
            var hashes: [String] = []
            for index in 0..<12 {
                try await Task.sleep(for: .milliseconds(190))
                let png = try frame(host)
                hashes.append(SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined())
                if index == 0 || index == 11 {
                    try png.write(to: output.appendingPathComponent("\(name)-\(index).png"))
                }
            }
            let distinct = Set(hashes).count
            results.append([
                "phase": name, "thinking": thinking, "reduced_motion": reduced,
                "mounted": present, "expected_motion": moving, "frame_hashes": hashes,
                "distinct_frames": distinct, "passed": moving ? distinct >= 3 : distinct == 1,
                "seconds": Date().timeIntervalSince(start), "self_cpu_s": cpuSeconds() - cpuStart,
            ])
        }
        let passed = results.allSatisfy { $0["passed"] as? Bool == true }
        let payload: [String: Any] = [
            "status": passed ? "PASS" : "FAIL_OR_INCONCLUSIVE",
            "fixture": "Real ChatOrb in an isolated synthetic NSHostingView",
            "method": "View-local PNG hashes, including positive moving controls. No desktop pixels or input synthesis.",
            "cpu_scope": "Fixture self CPU includes forced snapshot and PNG encoding; not an app idle benchmark.",
            "safety": "No Wisp AppDelegate, backend, user data, model calls, communications or global settings changes.",
            "phases": results,
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: output.appendingPathComponent("results.json"))
        print(String(decoding: data, as: UTF8.self))
        return passed
    }

    static func main() {
        guard let directory = ProcessInfo.processInfo.environment["WISP_ORB_QA_OUT"]
                ?? Bundle.main.object(forInfoDictionaryKey: "WispOrbQAOutputDirectory") as? String,
              directory.hasPrefix("/") else {
            print("Explicit disposable WISP_ORB_QA_OUT or fixture output key required")
            exit(2)
        }
        let output = URL(fileURLWithPath: directory, isDirectory: true)
        do { try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true) }
        catch { print("Cannot create fixture output"); exit(2) }
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let state = OrbState()
        let host = NSHostingView(rootView: OrbFixture(state: state))
        let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: 240, height: 240),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Wisp synthetic orb lifecycle check"
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        Task { @MainActor in
            do { exit(try await run(state: state, host: host, output: output) ? 0 : 1) }
            catch {
                try? Data("Fixture failed before completing all phases".utf8)
                    .write(to: output.appendingPathComponent("incomplete.txt"))
                exit(1)
            }
        }
        app.run()
    }
}
