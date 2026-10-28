import Foundation

/// What the local engine has resident, from the service's `/models/memory`.
/// `bytes` is nil when the engine measured no footprint for a model, and the
/// whole value is nil when the service can't say (a remote engine, or the read
/// failed). Never an estimate.
struct ModelMemory: Equatable {
    struct Resident: Equatable { let id: String; let bytes: Int64? }
    let models: [Resident]

    /// The sum, only when every resident model has a measured size.
    var totalBytes: Int64? {
        let sizes = models.compactMap(\.bytes)
        return sizes.count == models.count ? sizes.reduce(0, +) : nil
    }

    static func parse(_ obj: [String: Any]?) -> ModelMemory? {
        guard let obj, obj["available"] as? Bool == true, let rows = obj["models"] as? [[String: Any]] else { return nil }
        return ModelMemory(models: rows.compactMap { row in
            guard let id = row["id"] as? String, !id.isEmpty else { return nil }
            let number = row["bytes"] as? NSNumber
            let bytes = number.flatMap { CFGetTypeID($0) == CFBooleanGetTypeID() ? nil : $0.int64Value }
            return Resident(id: id, bytes: bytes.flatMap { $0 > 0 ? $0 : nil })
        })
    }
}

/// Which other-app launches and activations turn Low Power Mode off. Only
/// regular apps (the ones with a Dock icon) the user brings up count: Wisp,
/// its engine, and system UI that comes forward on its own never do.
enum LowPowerTrigger {
    static let ignored: Set<String> = [
        "app.omlx",                              // Wisp's engine
        "com.apple.loginwindow",
        "com.apple.dock",
        "com.apple.Spotlight",
        "com.apple.controlcenter",
        "com.apple.notificationcenterui",
        "com.apple.systemuiserver",
        "com.apple.WindowManager",
        "com.apple.SecurityAgent",
        "com.apple.UserNotificationCenter",
        "com.apple.screencaptureui",
        "com.apple.CoreServicesUIAgent",
    ]

    /// Closing a Wisp window (or hiding Wisp) makes macOS bring the previous app
    /// forward on its own. An activation this soon after one is not a switch.
    static let dismissalGrace: TimeInterval = 1.0

    /// `regular` is NSRunningApplication.activationPolicy == .regular.
    /// `sinceWispDismissed` is the time since a Wisp window last closed or Wisp
    /// hid (nil when never); it only matters for an activation.
    static func counts(bundleID: String?, regular: Bool, ownBundleID: String?,
                       activated: Bool = false, sinceWispDismissed: TimeInterval? = nil) -> Bool {
        guard regular, let bundleID, !bundleID.isEmpty else { return false }
        guard bundleID != ownBundleID && !ignored.contains(bundleID) else { return false }
        if activated, let since = sinceWispDismissed, since >= 0, since < dismissalGrace { return false }
        return true
    }
}

/// Quick settings' one-line descriptions. Each keeps the states the service can
/// be in (waiting for confirmation, not confirmed) instead of guessing "off".
enum QuickSettingsText {
    /// Full access's line: a status word ("On" shows amber) and the rest.
    static func fullAccess(on: Bool?, pending: Bool) -> (status: String?, rest: String) {
        if pending { return (nil, "Updating · waiting for confirmation…") }
        switch on {
        case true?: return ("On", "Wisp acts without asking first")
        case false?: return ("Off", "Risky actions wait for you")
        case nil: return (nil, "Not confirmed · Wisp may still act without asking")
        }
    }

    /// Learn my routines' line, and the trailing "stays on this Mac" when
    /// nothing learned is sent anywhere.
    /// Low Power Mode's line. Neutral wording in every state: it is a
    /// preference, not a warning (red/amber stays for status).
    static func lowPower(on: Bool?, pending: Bool) -> String {
        if pending { return "Updating · waiting for confirmation…" }
        switch on {
        case true?: return "Caps GPU power only while Wisp is working"
        case false?: return "Wisp runs at full speed"
        case nil: return "Not confirmed"
        }
    }

    static let lowPowerHelp = "Limits the GPU power Wisp's model can draw, only while it is generating: "
        + "replies about 20 W, background work 10–15 W. Nothing is limited while Wisp is idle. "
        + "Replies start slightly slower. Turns itself off after 10 minutes without using Wisp, "
        + "when Wisp restarts, or when you open or switch to another app."

    static func learning(enabled: Bool?, changing: Bool, status: String?,
                         remote: Bool, phraser: Bool) -> (text: String, local: String?) {
        if changing { return (status ?? "Updating · waiting for confirmation…", nil) }
        let sources = "From calendar, reminders and how you use Wisp"
        switch enabled {
        case nil: return ("Not confirmed · Wisp may still be learning", nil)
        case false?: return ("Off · no learning or nudges", nil)
        case true?:
            guard remote else { return (sources, "stays on this Mac") }
            return (sources + (phraser ? " · titles go to your inference server" : " · a summary goes to your inference server"), nil)
        }
    }

    static func brief(pending: Bool, confirmed: Bool) -> String {
        if pending { return "Saving · waiting for confirmation…" }
        return confirmed ? "Shows up in Brief, and in a notification" : "Times not confirmed · check again"
    }

    /// Model memory's line, and how full the bar is (nil hides it): only from
    /// sizes the engine measured.
    static func memory(_ memory: ModelMemory?, physicalBytes: UInt64) -> (text: String, fraction: Double?) {
        guard let memory else { return ("Unloads models now · reloads on next ask", nil) }
        guard let first = memory.models.first else { return ("Not loaded", nil) }
        let name = shortName(first.id) + (memory.models.count > 1 ? " +\(memory.models.count - 1)" : "")
        guard let total = memory.totalBytes, total > 0 else { return ("\(name) · loaded · reloads on next ask", nil) }
        let fraction = physicalBytes > 0 ? min(1, Double(total) / Double(physicalBytes)) : nil
        return ("\(name) · \(gigabytes(total)) GB in use · reloads on next ask", fraction)
    }

    /// "Ling-3.0-tiny-oQ6e" → "Ling"; "org/Mellum-2.1" → "Mellum".
    static func shortName(_ id: String) -> String {
        let base = id.split(separator: "/").last.map(String.init) ?? id
        let head = base.split(separator: "-").first.map(String.init) ?? base
        return head.isEmpty ? id : head
    }

    /// One decimal, in the GB Activity Monitor uses (2^30 bytes).
    static func gigabytes(_ bytes: Int64) -> String {
        String(format: "%.1f", Double(bytes) / 1_073_741_824)
    }

    /// A brief time pill: ("AM", "8:00").
    static func pill(_ hour: Int) -> (period: String, time: String) {
        let display = hour % 12 == 0 ? 12 : hour % 12
        return (hour < 12 ? "AM" : "PM", "\(display):00")
    }

    static func hourLabel(_ hour: Int) -> String {
        let display = hour % 12 == 0 ? 12 : hour % 12
        return "\(display) \(hour < 12 ? "AM" : "PM")"
    }
}
