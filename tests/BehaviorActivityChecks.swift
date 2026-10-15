import Foundation

// No native reader or transport is instantiated or called.
final class WispClient {
    func behaviorActivityLease(_ client: String) async -> [String: Any]? { fatalError("native/network forbidden") }
    func behaviorActivityBatch(lease: [String: Any], intervals: [[String: Any]]) async -> Bool { fatalError("network forbidden") }
}

@main struct ActivityChecks {
    static func main() {
        var builder = ActivityIntervals()
        func sample(_ wall: Double, _ uptime: Double, _ idle: Double = 0) -> ActivitySample {
            ActivitySample(wall: wall, uptime: uptime, bundle: "org.unseen.App", idle: idle,
                           awake: nil, active: nil, timezone: "UTC")
        }
        precondition(builder.take(sample(100, 100)) == nil)
        let row = builder.take(sample(115, 115, 1000))!
        precondition(row["start"] as? Double == 100)
        precondition(row["idle_seconds"] as? Double == 15)
        precondition(row["screen_awake"] is NSNull)
        precondition(builder.take(sample(1000, 130)) == nil) // wake or clock jump
        precondition(builder.take(sample(1015, 145)) != nil)
        builder.reset()
        precondition(builder.take(sample(1030, 160)) == nil)
        precondition(builder.take(sample(1020, 170)) == nil) // backwards wall clock
        print("synthetic activity interval checks passed")
    }
}
