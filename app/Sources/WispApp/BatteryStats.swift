import Foundation
import IOKit.ps

// Battery state straight from IOPowerSources (what `pmset -g batt` reads) —
// no LLM, no estimation of our own beyond what macOS already computes.
enum BatteryStats {
    struct Snapshot {
        let percentage: Int
        let isCharging: Bool
        let onACPower: Bool
        let timeToEmptyMin: Int?   // nil when unplugged-but-charging-status unknown, or fully charged
        let timeToFullMin: Int?
    }

    static func current() -> Snapshot? {
        guard let snapshotRef = IOPSCopyPowerSourcesInfo() else { return nil }
        let snapshot = snapshotRef.takeRetainedValue()
        guard let sourcesRef = IOPSCopyPowerSourcesList(snapshot) else { return nil }
        let sources = sourcesRef.takeRetainedValue() as [CFTypeRef]
        guard let source = sources.first,
              let descRef = IOPSGetPowerSourceDescription(snapshot, source),
              let desc = descRef.takeUnretainedValue() as? [String: Any]
        else { return nil }

        let pct = desc[kIOPSCurrentCapacityKey as String] as? Int ?? 0
        let charging = (desc[kIOPSIsChargingKey as String] as? Bool) ?? false
        let powerState = desc[kIOPSPowerSourceStateKey as String] as? String
        let onAC = powerState == kIOPSACPowerValue
        let toEmpty = desc[kIOPSTimeToEmptyKey as String] as? Int    // minutes; -1 = still calculating
        let toFull = desc[kIOPSTimeToFullChargeKey as String] as? Int
        return Snapshot(percentage: pct, isCharging: charging, onACPower: onAC,
                         timeToEmptyMin: (toEmpty ?? -1) >= 0 ? toEmpty : nil,
                         timeToFullMin: (toFull ?? -1) >= 0 ? toFull : nil)
    }
}

// Rolling history for the menu-bar sparkline. Sampled every ~60s by whoever
// owns the refresh timer (BatteryWidgetController); persisted to disk so the
// graph doesn't reset to empty on every relaunch.
struct BatterySample: Codable { let ts: Date; let percentage: Int }

final class BatteryHistory {
    static let shared = BatteryHistory()

    private var samples: [BatterySample] = []
    private let maxAge: TimeInterval = 24 * 3600
    private let fileURL: URL

    private init() {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Wisp", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        fileURL = dir.appendingPathComponent("battery_history.json")
        load()
    }

    func record(percentage: Int) {
        samples.append(BatterySample(ts: Date(), percentage: percentage))
        let cutoff = Date().addingTimeInterval(-maxAge)
        samples.removeAll { $0.ts < cutoff }
        save()
    }

    func recent(_ window: TimeInterval) -> [BatterySample] {
        let cutoff = Date().addingTimeInterval(-window)
        return samples.filter { $0.ts >= cutoff }
    }

    private func load() {
        guard let data = try? Data(contentsOf: fileURL),
              let decoded = try? JSONDecoder().decode([BatterySample].self, from: data)
        else { return }
        samples = decoded
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(samples) else { return }
        try? data.write(to: fileURL)
    }
}
