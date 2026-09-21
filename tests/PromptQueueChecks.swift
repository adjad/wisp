@main
enum PromptQueueChecks {
    static func main() {
        let first = QueuedPrompt(text: "first", image: nil, imageName: nil, researchMode: false)
        let second = QueuedPrompt(text: "second", image: "data:image/png;base64,fixture",
                                  imageName: "fixture.png", researchMode: true)
        var queue = PromptQueue()
        precondition(queue.count == 0)
        precondition(queue.dequeue() == nil)
        queue.enqueue(first)
        queue.enqueue(second)
        precondition(queue.count == 2)
        precondition(queue.dequeue() == first)
        precondition(queue.count == 1)
        precondition(queue.dequeue() == second)
        precondition(queue.count == 0)
        queue.enqueue(first)
        queue.clear()
        precondition(queue.count == 0 && queue.dequeue() == nil)
        print("PromptQueue: 8 regression checks passed")
    }
}
