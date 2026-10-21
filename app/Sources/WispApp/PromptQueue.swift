struct QueuedPrompt: Equatable {
    let text: String
    let image: String?
    let imageName: String?
    let researchMode: Bool
}

enum PromptWork: Equatable {
    case prompt(QueuedPrompt)
    case dailySummary
}

struct PromptDispatch: Equatable {
    let id: UInt64
    let work: PromptWork
}

/// Dropped prompts the server may have started on, set aside because the user
/// had typed something else meanwhile. Never sent by itself: restoring one only
/// hands it back to be put in the box. A later drop doesn't displace an earlier
/// one; Return on an empty box brings back the most recent first.
struct SetAsidePrompts {
    private var stack: [QueuedPrompt] = []

    var count: Int { stack.count }

    mutating func push(_ prompt: QueuedPrompt?) {
        if let prompt { stack.append(prompt) }
    }

    mutating func restore() -> QueuedPrompt? {
        stack.popLast()
    }

    mutating func reset() {
        stack.removeAll(keepingCapacity: false)
    }
}

struct PromptSubmissionState {
    private(set) var active: PromptDispatch?
    private var queued: [QueuedPrompt] = []
    private var nextID: UInt64 = 0
    /// Queued prompts wait for the dropped one they followed: it is back in the
    /// box, and they go after it, in order, once it is sent again (or Return is
    /// pressed on an empty box).
    private(set) var held = false

    var queuedCount: Int { queued.count }
    var isActive: Bool { active != nil }

    /// What queued prompts are waiting on, for the line beside the box.
    enum Waiting: Equatable { case reply, summary, research, resend }
    var waiting: Waiting? {
        guard !queued.isEmpty else { return nil }
        switch active?.work {
        case .dailySummary: return .summary
        case .prompt(let prompt): return prompt.researchMode ? .research : .reply
        case nil: return held ? .resend : nil
        }
    }

    func isCurrent(_ id: UInt64) -> Bool {
        active?.id == id
    }

    mutating func submit(_ prompt: QueuedPrompt) -> PromptDispatch? {
        guard active == nil else {
            queued.append(prompt)
            return nil
        }
        held = false
        return begin(.prompt(prompt))
    }

    /// Sends the held queue on: Return on an empty box after a drop.
    mutating func resume() -> PromptDispatch? {
        guard active == nil, !queued.isEmpty else { return nil }
        held = false
        return begin(.prompt(queued.removeFirst()))
    }

    mutating func beginDailySummary() -> PromptDispatch? {
        guard active == nil else { return nil }
        return begin(.dailySummary)
    }

    mutating func complete(_ id: UInt64) -> PromptDispatch? {
        guard active?.id == id else { return nil }
        active = nil
        // Held prompts wait for the dropped one, even past a summary run meanwhile.
        guard !queued.isEmpty, !held else { return nil }
        return begin(.prompt(queued.removeFirst()))
    }

    mutating func dropForRetry(_ id: UInt64) -> QueuedPrompt? {
        guard active?.id == id, case .prompt(let prompt) = active?.work else { return nil }
        active = nil
        held = !queued.isEmpty
        return prompt
    }

    /// The dropped prompt goes first in the held queue instead of back in the
    /// box, when the box already holds something typed meanwhile.
    mutating func dropToQueue(_ id: UInt64) -> Bool {
        guard active?.id == id, case .prompt(let prompt) = active?.work else { return false }
        active = nil
        queued.insert(prompt, at: 0)
        held = true
        return true
    }

    mutating func reset() {
        active = nil
        held = false
        queued.removeAll(keepingCapacity: false)
    }

    private mutating func begin(_ work: PromptWork) -> PromptDispatch {
        nextID &+= 1
        if nextID == 0 { nextID &+= 1 }
        let dispatch = PromptDispatch(id: nextID, work: work)
        active = dispatch
        return dispatch
    }
}
