import Foundation

// Intel v5: the Aria demo toggle maps to exactly one backend mode. Compiled
// with app/Sources/WispApp/AriaDemoMode.swift and -DWISP_REMOTE_ONLY_DEMO
// -DWISP_REMOTE_FULL (see tests/test_intel_remote_full.py).
@main
enum AriaDemoModeChecks {
    static var checks = 0
    static func check(_ condition: Bool, _ message: String) {
        guard condition else { fatalError(message) }
        checks += 1
    }

    static func main() {
        let suite = "wisp.aria.checks.\(UUID().uuidString)"
        guard let defaults = UserDefaults(suiteName: suite) else { fatalError("no defaults suite") }
        defer { defaults.removePersistentDomain(forName: suite) }

        check(AriaDemoMode.available, "the v5 build offers the runtime switch")
        check(!AriaDemoMode.isOn(defaults: defaults, environment: [:]), "real mode is the default")
        check(AriaDemoMode.setSaved(true, defaults: defaults, environment: [:]), "toggle on persists demo")
        check(AriaDemoMode.isOn(defaults: defaults, environment: [:]), "saved demo survives a relaunch")
        check(!AriaDemoMode.setSaved(false, defaults: defaults, environment: [:]), "toggle off returns to real")
        check(AriaDemoMode.isOn(defaults: defaults, environment: ["WISP_ARIA_DEMO": "1"]),
              "WISP_ARIA_DEMO=1 at launch forces demo")
        check(AriaDemoMode.isOn(defaults: defaults, environment: ["WISP_FOUNDER_DEMO": "1"]),
              "WISP_FOUNDER_DEMO=1 at launch forces demo")
        check(AriaDemoMode.setSaved(false, defaults: defaults, environment: ["WISP_FOUNDER_DEMO": "1"]),
              "an environment force wins over the saved toggle")
        check(!AriaDemoMode.isOn(defaults: defaults, environment: ["WISP_ARIA_DEMO": "0"]),
              "only the exact value 1 forces Aria demo")
        check(!AriaDemoMode.isOn(defaults: defaults, environment: ["WISP_FOUNDER_DEMO": "0"]),
              "only the exact value 1 forces founder demo")

        var real = ["WISP_FOUNDER_DEMO": "1", "WISP_ARIA_DEMO": "1",
                    "WISP_REMOTE_ONLY": "0", "WISP_REMOTE_FULL": "0", "OTHER": "kept"]
        AriaDemoMode.applyBackendMode(demo: false, to: &real)
        check(real == ["WISP_REMOTE_ONLY": "1", "WISP_REMOTE_FULL": "1", "OTHER": "kept"],
              "real mode: remote-only + full, neither demo flag: \(real)")

        var demo = ["WISP_REMOTE_ONLY": "0", "WISP_REMOTE_FULL": "0",
                    "WISP_FOUNDER_DEMO": "1", "WISP_ARIA_DEMO": "0", "OTHER": "kept"]
        AriaDemoMode.applyBackendMode(demo: true, to: &demo)
        check(demo == ["WISP_REMOTE_ONLY": "1", "WISP_REMOTE_FULL": "1",
                       "WISP_ARIA_DEMO": "1", "OTHER": "kept"],
              "demo mode: remote-only + full + Aria, never founder: \(demo)")

        print("AriaDemoModeChecks passed (\(checks) checks)")
    }
}
