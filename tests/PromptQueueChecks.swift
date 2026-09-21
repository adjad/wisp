@main
enum PromptQueueChecks {
    static func main() {
        let first = QueuedPrompt(text: "first", image: nil, imageName: nil, researchMode: false)
        let research = QueuedPrompt(text: "research", image: nil, imageName: nil, researchMode: true)
        let image = QueuedPrompt(text: "image", image: "data:image/png;base64,fixture",
                                 imageName: "fixture.png", researchMode: false)
        let last = QueuedPrompt(text: "last", image: nil, imageName: nil, researchMode: false)

        var fifo = PromptSubmissionState()
        let firstDispatch = fifo.submit(first)!
        precondition(fifo.isActive && fifo.queuedCount == 0)
        precondition(fifo.submit(research) == nil)
        precondition(fifo.submit(image) == nil && fifo.queuedCount == 2)
        let researchDispatch = fifo.complete(firstDispatch.id)!
        precondition(researchDispatch.work == .prompt(research))
        precondition(fifo.submit(last) == nil && fifo.queuedCount == 2)
        let imageDispatch = fifo.complete(researchDispatch.id)!
        precondition(imageDispatch.work == .prompt(image))
        let lastDispatch = fifo.complete(imageDispatch.id)!
        precondition(lastDispatch.work == .prompt(last))
        precondition(fifo.complete(lastDispatch.id) == nil && !fifo.isActive)

        var retry = PromptSubmissionState()
        let imageDispatchForRetry = retry.submit(image)!
        precondition(retry.submit(last) == nil)
        precondition(retry.dropForRetry(imageDispatchForRetry.id) == image)
        precondition(!retry.isActive && retry.queuedCount == 1)
        let retried = retry.submit(image)!
        precondition(retried.id != imageDispatchForRetry.id)
        precondition(retry.complete(retried.id)?.work == .prompt(last))

        var reset = PromptSubmissionState()
        let stale = reset.submit(first)!
        reset.reset()
        let current = reset.submit(last)!
        precondition(current.id != stale.id)
        precondition(reset.complete(stale.id) == nil && reset.isCurrent(current.id))

        var summary = PromptSubmissionState()
        let summaryDispatch = summary.beginDailySummary()!
        precondition(summaryDispatch.work == .dailySummary)
        precondition(summary.submit(first) == nil)
        precondition(summary.beginDailySummary() == nil)
        precondition(summary.complete(summaryDispatch.id)?.work == .prompt(first))

        print("PromptSubmissionState: 24 regression checks passed")
    }
}
