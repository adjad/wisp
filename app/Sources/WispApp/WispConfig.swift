import Foundation

// One place for the things that used to be typed literally into half a dozen
// files: which host/port the backend is on, where oMLX lives, and where the
// checkout is when running against a dev tree rather than a packaged bundle.
//
// All of it is overridable by environment variable, and none of it names a
// particular machine. The defaults are what a fresh install gets; the env
// overrides exist so a second checkout, a non-default oMLX location, or a
// port that's already spoken for don't require editing source.
enum WispConfig {

    // MARK: - Backend

    /// Host the Python backend binds to, and that the app talks to. Loopback
    /// by default and deliberately so: the backend has no authentication of
    /// its own worth exposing to a network.
    static let backendHost: String = env("WISP_HOST") ?? "127.0.0.1"

    /// Backend port. `WISP_PORT` is the documented name; `MOE_PORT` is still
    /// read for continuity with scripts/run.sh's older variable.
    static let backendPort: Int = envInt("WISP_PORT") ?? envInt("MOE_PORT") ?? 8765

    static var backendBaseURL: URL {
        URL(string: "http://\(backendHost):\(backendPort)")!
    }

    // MARK: - oMLX

    /// Port oMLX serves its OpenAI-compatible API on.
    static let omlxPort: Int = envInt("WISP_OMLX_PORT") ?? 8000

    /// Places oMLX.app is plausibly installed. Used to tell "oMLX is already
    /// listening on its port" apart from "something else has taken it".
    /// `WISP_OMLX_APP` prepends a custom location.
    static var omlxAppPaths: [String] {
        var paths: [String] = []
        if let custom = env("WISP_OMLX_APP") { paths.append(custom) }
        paths.append("/Applications/oMLX.app")
        paths.append("\(NSHomeDirectory())/Applications/oMLX.app")
        return paths
    }

    // MARK: - Backend source tree

    /// Where to find `service/` when not running from a packaged bundle.
    ///
    /// A packaged Wisp.app carries its own copy in Resources/backend and never
    /// consults this. Running the app straight out of `swift run` does need a
    /// checkout, so: `WISP_DEV_ROOT` if set, otherwise walk up from the
    /// executable looking for a directory that has `service/main.py` in it.
    /// That covers `.build/release/WispApp` inside a normal checkout without
    /// anybody's home directory being written into the source.
    static var devRootCandidates: [URL] {
        var candidates: [URL] = []
        if let explicit = env("WISP_DEV_ROOT") {
            candidates.append(URL(fileURLWithPath: explicit))
        }
        var dir = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0])
            .resolvingSymlinksInPath()
            .deletingLastPathComponent()
        // .build/<config>/WispApp → up to the checkout root is 3 levels, but
        // walk further so `swift run` from a subdirectory also resolves.
        for _ in 0..<6 {
            candidates.append(dir)
            let parent = dir.deletingLastPathComponent()
            if parent == dir { break }
            dir = parent
        }
        return candidates
    }

    // MARK: - Air node

    /// Optional side-compute node. There is no sensible default hostname —
    /// it is whatever the user's other Mac is called — so this is empty until
    /// they fill it in, and the feature stays off until they do.
    static let airBaseURLDefault: String = env("WISP_AIR_URL") ?? ""

    /// Shown greyed in the Settings field, as an example of the expected shape.
    static let airBaseURLPlaceholder = "http://your-other-mac.local:8766"

    // MARK: - helpers

    private static func env(_ key: String) -> String? {
        guard let v = ProcessInfo.processInfo.environment[key],
              !v.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return v
    }

    private static func envInt(_ key: String) -> Int? {
        guard let v = env(key) else { return nil }
        return Int(v)
    }
}
