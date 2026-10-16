import Foundation
import AppKit
import CoreGraphics

// Pure interval builder: tests inject samples without touching native sources.
struct ActivitySample {
    var wall: Double
    var uptime: Double
    var bundle: String
    var idle: Double
    var awake: Bool?
    var active: Bool?
    var timezone: String
}

struct ActivityIntervals {
    private var previous: ActivitySample?
    private var sequence = 0

    mutating func reset() { previous = nil }

    mutating func take(_ sample: ActivitySample) -> [String: Any]? {
        defer { previous = sample }
        guard let old = previous else { return nil }
        let elapsed = sample.wall - old.wall
        guard elapsed > 0, elapsed <= 60,
              abs(elapsed - (sample.uptime - old.uptime)) < 2,
              old.timezone == sample.timezone else { return nil }
        sequence += 1
        return ["start": old.wall, "end": sample.wall, "sequence": sequence,
                "bundle": old.bundle, "idle_seconds": min(elapsed, max(0, sample.idle)),
                "screen_awake": old.awake.map { $0 as Any } ?? NSNull(),
                "session_active": old.active.map { $0 as Any } ?? NSNull(),
                "timezone": old.timezone]
    }
}

@MainActor
final class ActivityReader {
    private let client = WispClient()
    private let sessionID = UUID().uuidString
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var intervals = ActivityIntervals()
    private var polling = false
    private var receiptIdentity = ""
    private var awake: Bool?
    private var active: Bool?

    func start() {
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.poll() }
        }
        Task { await poll() }
    }

    private func watchTransitions() {
        guard observers.isEmpty else { return }
        let center = NSWorkspace.shared.notificationCenter
        let states: [(Notification.Name, Bool?, Bool?)] = [
            (NSWorkspace.screensDidSleepNotification, false, nil),
            (NSWorkspace.screensDidWakeNotification, true, nil),
            (NSWorkspace.sessionDidResignActiveNotification, nil, false),
            (NSWorkspace.sessionDidBecomeActiveNotification, nil, true)]
        for (name, screen, session) in states {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    self.intervals.reset() // never extend the old state through a transition
                    if let screen { self.awake = screen }
                    if let session { self.active = session }
                }
            })
        }
        observers.append(center.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                            object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.intervals.reset() }
        })
    }

    private func stopWatching() {
        guard !observers.isEmpty else { intervals.reset(); return }
        let center = NSWorkspace.shared.notificationCenter
        for observer in observers { center.removeObserver(observer) }
        observers = []
        awake = nil
        active = nil
        intervals.reset()
    }

    private func poll() async {
        guard !polling else { return }
        polling = true
        defer { polling = false }
        // Obtain fresh server consent BEFORE touching foreground/idle sources.
        guard let lease = await client.behaviorActivityLease(sessionID),
              lease["enabled"] as? Bool == true else { stopWatching(); return }
        watchTransitions()
        let identity = "\(lease["process"] ?? ""):\(lease["epoch"] ?? ""):\(lease["token"] ?? "")"
        if identity != receiptIdentity { intervals.reset(); receiptIdentity = identity }
        // Public session/display state describes this Mac, not the person's
        // location, attention, or interruptibility. No display pixels are read.
        let console = CGSessionCopyCurrentDictionary() as? [String: Any]
        active = console?[kCGSessionOnConsoleKey as String] as? Bool
        awake = CGDisplayIsAsleep(CGMainDisplayID()) == 0
        let sample = ActivitySample(wall: Date().timeIntervalSince1970,
            uptime: ProcessInfo.processInfo.systemUptime,
            bundle: NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? "",
            idle: CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: UInt32.max)!),
            awake: awake, active: active, timezone: TimeZone.current.identifier)
        guard let row = intervals.take(sample) else { return }
        // No durable queue: failed/expired/offline captures are unknown coverage.
        if !(await client.behaviorActivityBatch(lease: lease, intervals: [row])) { intervals.reset() }
    }
}
