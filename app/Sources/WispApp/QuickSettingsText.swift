import Foundation

/// What `GET /memory_saver` says beyond the switch itself: whether Save memory can
/// act right now, so its row never promises memory it can't free.
struct SaveMemoryDetail: Equatable {
    var applies: Bool
    /// `not_gemma`, `pinned`, `not_reported` or `not_omlx`; nil when `applies`.
    var whyNot: String?
    /// Another app used the model on the user's oMLX in the last 30 minutes.
    var otherApp: Bool
    /// Wisp runs on the user's own oMLX app, not its built-in engine.
    var onOMLX: Bool

    init(applies: Bool, whyNot: String? = nil, otherApp: Bool = false, onOMLX: Bool = false) {
        self.applies = applies
        self.whyNot = whyNot
        self.otherApp = otherApp
        self.onOMLX = onOMLX
    }

    /// Nil when the reply doesn't say whether Save memory applies.
    init?(json data: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let applies = obj["applies"] as? NSNumber,
              CFGetTypeID(applies) == CFBooleanGetTypeID() else { return nil }
        self.init(applies: applies.boolValue,
                  whyNot: obj["why_not"] as? String,
                  otherApp: (obj["other_app"] as? NSNumber)?.boolValue ?? false,
                  onOMLX: (obj["engine"] as? String) == "omlx")
    }
}

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

/// The Ask model's audio and vision encoders on Wisp's own engine, from
/// `/engine`'s `encoders`. Nil when the service can't say (the user's oMLX, an
/// older engine, no answer). On demand, each loads when a clip or picture needs
/// it and leaves memory after `idleSeconds` unused.
struct EncoderResidency: Equatable {
    struct Tower: Equatable { let name: String; let resident: Bool; let bytes: Int64? }
    let onDemand: Bool
    let idleSeconds: Double?
    let towers: [Tower]

    static func parse(_ value: Any?) -> EncoderResidency? {
        guard let obj = value as? [String: Any],
              let onDemand = obj["on_demand"] as? NSNumber, CFGetTypeID(onDemand) == CFBooleanGetTypeID(),
              let rows = obj["towers"] as? [String: Any] else { return nil }
        let idle = (obj["idle_release_s"] as? NSNumber).flatMap {
            CFGetTypeID($0) == CFBooleanGetTypeID() || $0.doubleValue <= 0 ? nil : $0.doubleValue }
        let towers = ["audio", "vision"].compactMap { name -> Tower? in
            guard let row = rows[name] as? [String: Any], let state = row["state"] as? String else { return nil }
            let number = row["bytes"] as? NSNumber
            let bytes = number.flatMap { CFGetTypeID($0) == CFBooleanGetTypeID() || $0.int64Value <= 0 ? nil : $0.int64Value }
            return Tower(name: name, resident: state == "resident" || state == "loading", bytes: bytes)
        }
        return EncoderResidency(onDemand: onDemand.boolValue, idleSeconds: idle, towers: towers)
    }
}

/// Quick settings' one-line descriptions. Each keeps the states the service can
/// be in (waiting for confirmation, not confirmed) instead of guessing "off".
enum QuickSettingsText {
    /// The notch's Setup row: always there, unlike the menu bar's two-day Change Setup….
    static let setupHelp = "Open setup again to change permissions, Google, About you or open at login. "
        + "Your model stays as it is."

    /// Full access's line: a status word ("On" shows amber) and the rest.
    static func fullAccess(on: Bool?, pending: Bool) -> (status: String?, rest: String) {
        if pending { return (nil, "Updating · waiting for confirmation…") }
        switch on {
        case true?: return ("On", "Wisp acts without asking first")
        case false?: return ("Off", "Risky actions wait for you")
        case nil: return (nil, "Not confirmed · Wisp may still act without asking")
        }
    }

    /// "Save power"'s line. Neutral wording in every state: it is a
    /// preference, not a warning (red/amber stays for status).
    static func saveWhenAway(on: Bool?, pending: Bool) -> String {
        if pending { return "Updating · waiting for confirmation…" }
        switch on {
        case true?: return "Uses less power, most of all on battery and while you're away"
        case false?: return "Wisp always runs at full speed"
        case nil: return "Not confirmed"
        }
    }

    static let saveWhenAwayHelp = "Wisp's background work always runs its GPU at 10 W, plugged in too. "
        + "On battery, it caps the GPU at 15 W while it writes a chat reply or updates your Brief. After "
        + "10 minutes away from the keyboard and mouse, the Brief runs at 10 W too. The cap ends as soon as "
        + "that work is done. It's set only while other apps use little of the GPU, and lifts within about "
        + "a second when another app, like a game or a video, starts using more. Only Wisp's built-in "
        + "engine is capped; "
        + "with your own oMLX app, nothing is."

    /// "Save memory"'s line, neutral like Save power's.
    static func saveMemory(on: Bool?, pending: Bool, detail: SaveMemoryDetail? = nil) -> String {
        if pending { return "Updating · waiting for confirmation…" }
        if on != nil, let detail, !detail.applies {
            switch detail.whyNot {
            case "not_gemma"?: return "Not active · Wisp's chat model in oMLX isn't a Gemma"
            case "pinned"?: return "Not active · Wisp's chat model is pinned in oMLX"
            case "not_omlx"?: return "Not active · Wisp's chat model doesn't run in oMLX"
            default: return "Not active · oMLX isn't reporting this model's use"
            }
        }
        switch on {
        case true?:
            if detail?.otherApp == true { return "Keeping it loaded · another app used it recently" }
            return detail?.onOMLX == true
                ? "Frees Wisp's Gemma memory while you're not using it"
                : "Frees Wisp's model memory while you're not using it"
        case false?: return "Wisp's model stays loaded"
        case nil: return "Not confirmed"
        }
    }

    static let saveMemoryHelp = "Wisp's model uses about 5.5 GB of memory. After 5 minutes without using "
        + "Wisp, or sooner when your Mac runs low on memory, Wisp unloads it so other apps get that "
        + "memory back. Research and Smart Search keep it loaded while they run. Opening Ask or the "
        + "Wisp window loads it again while you type; a question asked right away waits about 6 "
        + "seconds. While it's unloaded, the Brief and other background work catch up about once an "
        + "hour. Off keeps the model loaded, except after you hide the notch with its X."

    /// The help for what Save memory can do right now. No reply yet: the general one.
    static func saveMemoryHelp(detail: SaveMemoryDetail?) -> String {
        guard let detail else { return saveMemoryHelp }
        if !detail.applies {
            switch detail.whyNot {
            case "not_gemma"?:
                return "This switch isn't freeing any memory right now. In your oMLX app, Save memory "
                    + "only unloads a Gemma chat model, and Wisp's chat model isn't one. It starts "
                    + "working if you choose a Gemma as Wisp's chat model."
            case "pinned"?:
                return "This switch isn't freeing any memory right now, because Wisp's chat model is "
                    + "pinned in your oMLX app. It starts working if you unpin it there."
            case "not_omlx"?:
                return "This switch isn't freeing any memory right now, because Wisp's chat model "
                    + "doesn't run in oMLX or Wisp's built-in engine, so Wisp has nothing to unload."
            default:
                return "This switch isn't freeing any memory right now. Wisp needs your oMLX app to "
                    + "report what the model is and when it was last used, so it never unloads a "
                    + "model another app is using, and it isn't reporting that."
            }
        }
        guard detail.onOMLX else { return saveMemoryHelp }
        return """
            Wisp's Gemma chat model uses about 5.5 GB of memory. After 5 minutes without using Wisp, or \
            sooner when your Mac runs low on memory, Wisp unloads it from your oMLX app so other apps get \
            that memory back. Save memory only ever unloads that one model. It waits while another app \
            has used the model in the last 30 minutes. Research and Smart Search keep it loaded while \
            they run. Opening Ask or the Wisp window loads it again while you type, unless another model \
            is loaded in oMLX; a question asked right away waits about 6 seconds. While it's unloaded, \
            the Brief and other background work catch up about once an hour, unless another model is \
            loaded. Off keeps the model loaded, except after you hide the notch with its X.
            """
    }

    /// Learn my routines' line, and the trailing "stays on this Mac" when
    /// nothing learned is sent anywhere.
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

    /// The Models page's line about the user's own oMLX while Wisp runs on its
    /// built-in engine (nil shows none). Only a model it reports loaded is memory
    /// in use; a running but empty server, or one that doesn't say, never warns.
    static func otherEngine(running: Bool, loaded: Bool?, modelBytes: Int64?) -> (text: String, warns: Bool)? {
        guard running else { return nil }
        switch loaded {
        case true?:
            let size = modelBytes.map { " (\(gigabytes($0)) GB)" } ?? ""
            return ("Your own oMLX also has a model loaded\(size); both engines use memory", true)
        case false?:
            return ("Your own oMLX is also running, with no model loaded", false)
        case nil:
            return ("Your own oMLX is also running", false)
        }
    }

    /// The Models page's line about the voice (audio) and picture (vision) encoders.
    static func encoders(_ residency: EncoderResidency?) -> String? {
        guard let residency, !residency.towers.isEmpty else { return nil }
        guard residency.onDemand else { return "Voice and picture encoders stay loaded with the model" }
        let label = ["audio": "voice", "vision": "pictures"]
        let loaded = residency.towers.filter(\.resident).map { tower in
            (label[tower.name] ?? tower.name) + (tower.bytes.map { " \(megabytes($0)) MB" } ?? "")
        }
        let after = residency.idleSeconds.map { " after \(max(1, Int(($0 / 60).rounded()))) min unused" } ?? " when unused"
        if loaded.isEmpty { return "Voice and picture encoders load when you use them; none in memory now" }
        return "Encoders in memory: \(loaded.joined(separator: ", ")); they leave\(after)"
    }

    /// Whole MB (10^6 bytes), as the engine's tower sizes are small.
    static func megabytes(_ bytes: Int64) -> String {
        String(Int((Double(bytes) / 1_000_000).rounded()))
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
