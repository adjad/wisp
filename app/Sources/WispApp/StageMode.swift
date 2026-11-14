import Combine
import Foundation

/// Demo mode ("stage mode" in code): Wisp answers as fast as this Mac can for a live demo.
/// The service lifts its power and memory savings while it is on (service/stage_mode.py).
///
/// IMPORTANT rules:
/// - The app is the authority. It is memory only: off at every launch, never saved anywhere.
/// - The service forgets it when it restarts, so `pushIfOn` tells it again when it comes back.
/// - It turns itself off after `maxSeconds`. The app's own timer is the real limit; the
///   service's clock is only a backstop and restarts with the service.
/// - While on, the Mac stays awake and the app stays out of App Nap.
@MainActor
final class StageMode: ObservableObject {
    static let shared = StageMode()

    static let menuTitle = "Demo mode"
    static let maxSeconds: TimeInterval = 4 * 60 * 60
    private static let path = "stage_mode"
    private static let notRunning = "Wisp's service isn't running yet."

    @Published private(set) var isOn = false
    @Published private(set) var problem: String?
    private(set) var startedAt: Date?

    private var activity: NSObjectProtocol?
    private var autoOff: Task<Void, Never>?
    /// Bumped on every change, so a slow reply to an older push never undoes a newer one.
    private var generation = 0

    private init() {}

    /// Time left before it turns itself off; 0 while off.
    var remaining: TimeInterval {
        guard isOn, let startedAt else { return 0 }
        return max(0, Self.maxSeconds - Date().timeIntervalSince(startedAt))
    }

    func set(_ on: Bool) {
        generation += 1
        problem = nil
        if on {
            if !isOn {
                isOn = true
                startedAt = Date()
                holdAwake()
                startAutoOff()
            }
            push(true)
        } else {
            stopLocally()
            push(false)
        }
    }

    /// The service just came (back) up and has forgotten: tell it again.
    func pushIfOn() {
        guard isOn else { return }
        push(true)
    }

    private func push(_ on: Bool) {
        let mine = generation
        Task { @MainActor [weak self] in
            let told = await MeetingClient.sendTold("PUT", Self.path, body: ["enabled": on], timeout: 10)
            guard let self, on, mine == self.generation, self.isOn else { return }
            // Turning off is best effort: the app is already off and the service's own
            // clock ends it anyway. Turning on must be confirmed, or the app says off.
            if case .refused(let why) = told.reply {
                self.stopLocally()
                self.problem = told.answered ? why : Self.notRunning
            }
        }
    }

    private func stopLocally() {
        isOn = false
        startedAt = nil
        autoOff?.cancel()
        autoOff = nil
        if let activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
    }

    private func holdAwake() {
        guard activity == nil else { return }
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiated, .idleSystemSleepDisabled, .idleDisplaySleepDisabled, .latencyCritical],
            reason: "Wisp Demo mode")
    }

    private func startAutoOff() {
        autoOff?.cancel()
        autoOff = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.maxSeconds * 1_000_000_000))
            guard !Task.isCancelled, let self, self.isOn else { return }
            self.set(false)
        }
    }

    /// The plain line under the menu item.
    static func menuNote(on: Bool, remaining: TimeInterval, problem: String?) -> String {
        if on {
            return "Demo mode is on: fastest answers, uses more power. Turns off by itself in \(span(remaining))."
        }
        if let problem, !problem.isEmpty { return problem }
        return "Fastest answers for a live demo. Uses more power."
    }

    /// "3 h 12 min", "2 h", "12 min" or "1 min" (rounded up, never 0).
    static func span(_ seconds: TimeInterval) -> String {
        let minutes = max(1, Int((seconds / 60).rounded(.up)))
        let hours = minutes / 60
        let rest = minutes % 60
        if hours == 0 { return "\(rest) min" }
        return rest == 0 ? "\(hours) h" : "\(hours) h \(rest) min"
    }
}
