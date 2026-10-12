import Foundation

#if WISP_REMOTE_FULL && WISP_FOUNDER_DEMO
#error("WISP_REMOTE_FULL builds choose the Aria demo at runtime; do not also compile WISP_FOUNDER_DEMO")
#endif
#if WISP_REMOTE_FULL && !WISP_REMOTE_ONLY_DEMO
#error("WISP_REMOTE_FULL requires WISP_REMOTE_ONLY_DEMO (pinned GX10 inference, bundled backend)")
#endif

/// The Intel v5 (`WISP_REMOTE_FULL`) runtime switch between real mode and the
/// Aria demo. The backend always gets `WISP_REMOTE_ONLY=1` and
/// `WISP_REMOTE_FULL=1`; demo mode additionally gets `WISP_ARIA_DEMO=1`.
/// `WISP_FOUNDER_DEMO` is never passed to this backend.
///
/// The choice persists in UserDefaults (the menu-bar toggle). Launching the app
/// with `WISP_ARIA_DEMO=1` or `WISP_FOUNDER_DEMO=1` forces demo mode regardless
/// of the saved choice.
enum AriaDemoMode {
    static let defaultsKey = "wisp.ariaDemoMode"
    static let ariaKey = "WISP_ARIA_DEMO"
    static let founderKey = "WISP_FOUNDER_DEMO"
    static let fullKey = "WISP_REMOTE_FULL"
    static let remoteOnlyKey = "WISP_REMOTE_ONLY"

    /// True when this binary offers the runtime switch at all.
    static var available: Bool {
#if WISP_REMOTE_FULL
        return true
#else
        return false
#endif
    }

    static func forcedByEnvironment(_ environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        environment[ariaKey] == "1" || environment[founderKey] == "1"
    }

    /// The effective mode for the next backend launch.
    static func isOn(defaults: UserDefaults = .standard,
                     environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
#if WISP_REMOTE_FULL
        return forcedByEnvironment(environment) || defaults.bool(forKey: defaultsKey)
#elseif WISP_FOUNDER_DEMO
        return true
#else
        return false
#endif
    }

    /// Saves the toggle. Returns the new effective mode (an environment force
    /// still wins, so turning the toggle off cannot leave a forced demo).
    @discardableResult
    static func setSaved(_ on: Bool, defaults: UserDefaults = .standard,
                         environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        defaults.set(on, forKey: defaultsKey)
        return isOn(defaults: defaults, environment: environment)
    }

    /// Maps the mode onto the backend's launch environment for a v5 build.
    /// Any inherited Aria/founder setting is replaced, so this mapping alone
    /// decides the backend mode.
    static func applyBackendMode(demo: Bool, to environment: inout [String: String]) {
        environment[remoteOnlyKey] = "1"
        environment[fullKey] = "1"
        environment.removeValue(forKey: founderKey)
        if demo {
            environment[ariaKey] = "1"
        } else {
            environment.removeValue(forKey: ariaKey)
        }
    }
}
