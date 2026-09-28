import Foundation

/// One owner for chat/search presentation. Input updates the destination even
/// while a step is running; completion reconciles toward the latest request.
struct OverlayTransition {
    enum Surface: Equatable { case bar, chat, search, hidden }
    struct Step: Equatable {
        let id: UInt64
        let from: Surface
        let to: Surface
    }
    private(set) var settled: Surface = .bar
    private(set) var desired: Surface = .bar
    private(set) var active: Step?
    private var generation: UInt64 = 0

    mutating func request(_ surface: Surface) { desired = surface }

    mutating func next(searchReady: Bool = true) -> Step? {
        guard active == nil, settled != desired else { return nil }
        let target: Surface
        // Always finish retracting one surface before revealing another.
        if settled == .chat || settled == .search {
            target = .bar
        } else {
            guard desired != .search || searchReady else { return nil }
            target = desired
        }
        generation &+= 1
        let step = Step(id: generation, from: settled, to: target)
        active = step
        return step
    }

    @discardableResult
    mutating func finish(_ step: Step) -> Bool {
        guard active == step else { return false }
        settled = step.to
        active = nil
        return true
    }
}
