struct QueuedPrompt: Equatable {
    let text: String
    let image: String?
    let imageName: String?
    let researchMode: Bool
}

struct PromptQueue {
    private var items: [QueuedPrompt] = []

    var count: Int { items.count }

    mutating func enqueue(_ prompt: QueuedPrompt) {
        items.append(prompt)
    }

    mutating func dequeue() -> QueuedPrompt? {
        items.isEmpty ? nil : items.removeFirst()
    }

    mutating func clear() {
        items.removeAll(keepingCapacity: false)
    }
}
