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

struct PromptSubmissionState {
    private(set) var active: PromptDispatch?
    private var queued: [QueuedPrompt] = []
    private var nextID: UInt64 = 0

    var queuedCount: Int { queued.count }
    var isActive: Bool { active != nil }

    func isCurrent(_ id: UInt64) -> Bool {
        active?.id == id
    }

    mutating func submit(_ prompt: QueuedPrompt) -> PromptDispatch? {
        guard active == nil else {
            queued.append(prompt)
            return nil
        }
        return begin(.prompt(prompt))
    }

    mutating func beginDailySummary() -> PromptDispatch? {
        guard active == nil else { return nil }
        return begin(.dailySummary)
    }

    mutating func complete(_ id: UInt64) -> PromptDispatch? {
        guard active?.id == id else { return nil }
        active = nil
        guard !queued.isEmpty else { return nil }
        return begin(.prompt(queued.removeFirst()))
    }

    mutating func dropForRetry(_ id: UInt64) -> QueuedPrompt? {
        guard active?.id == id, case .prompt(let prompt) = active?.work else { return nil }
        active = nil
        return prompt
    }

    mutating func reset() {
        active = nil
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
