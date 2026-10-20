import Foundation

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

        // A drop with prompts queued behind it holds them for the dropped one:
        // they wait (even past a summary run meanwhile) until it is sent again.
        var held = PromptSubmissionState()
        let dropped = held.submit(first)!
        precondition(held.submit(last) == nil && held.waiting == .reply)
        precondition(held.dropForRetry(dropped.id) == first && held.held && held.waiting == .resend)
        let summaryWhileHeld = held.beginDailySummary()!
        precondition(held.waiting == .summary && held.complete(summaryWhileHeld.id) == nil, "held past a summary")
        let resent = held.submit(first)!
        precondition(!held.held && held.complete(resent.id)?.work == .prompt(last), "the queue follows the resend")

        // Return on an empty box sends the held queue on.
        var resume = PromptSubmissionState()
        let lost = resume.submit(first)!
        _ = resume.submit(last)
        _ = resume.dropForRetry(lost.id)
        precondition(resume.resume()?.work == .prompt(last) && !resume.held)
        var empty = PromptSubmissionState()
        precondition(empty.resume() == nil)

        // Text typed meanwhile keeps the box; the dropped prompt goes first in line.
        var typed = PromptSubmissionState()
        let inFlight = typed.submit(first)!
        _ = typed.submit(last)
        precondition(typed.dropToQueue(inFlight.id) && typed.held && typed.queuedCount == 2)
        let typedDispatch = typed.submit(image)!
        precondition(typedDispatch.work == .prompt(image))
        precondition(typed.complete(typedDispatch.id)?.work == .prompt(first), "the dropped prompt is next")

        // What queued prompts wait on.
        var waits = PromptSubmissionState()
        precondition(waits.waiting == nil)
        let researching = waits.submit(research)!
        _ = waits.submit(first)
        precondition(waits.waiting == .research)
        _ = waits.complete(researching.id)
        var summaryWait = PromptSubmissionState()
        _ = summaryWait.beginDailySummary()
        _ = summaryWait.submit(first)
        precondition(summaryWait.waiting == .summary)

        // Only a provably unsent request is sent again.
        precondition(AgentSend.retryable(status: 503) && AgentSend.retryable(status: 500))
        precondition(!AgentSend.retryable(status: 422) && !AgentSend.retryable(status: 413))
        precondition(AgentSend.retryable(error: URLError(.cannotConnectToHost))
                     && AgentSend.retryable(error: URLError(.networkConnectionLost)))
        precondition(!AgentSend.retryable(error: URLError(.timedOut)) && !AgentSend.retryable(error: URLError(.cancelled))
                     && !AgentSend.retryable(error: NSError(domain: "x", code: 1)))
        precondition(AgentSend.unsent(status: 503)["unsent"] as? Bool == true
                     && (AgentSend.unsent(status: 503)["message"] as? String ?? "").contains("503"))
        precondition(AgentSend.unsent(error: URLError(.cannotConnectToHost))["message"] as? String == "Wisp isn't running right now.")
        let notice = AgentSend.notice(message: "Couldn't reach Wisp.", unsent: true, queued: 2)
        precondition(notice == "Couldn't reach Wisp. Your message is back in the box. Press Return to send it again. "
                     + "2 queued messages will follow it.", notice)
        precondition(AgentSend.notice(message: "socket closed", unsent: false, queued: 0)
                     .hasPrefix("Wisp didn't answer. Your message is back in the box."))
        precondition(AgentSend.notice(message: "", unsent: true, queued: 0, place: .queued).contains("first in line"))
        // A prompt whose stream had begun is set aside, never queued to resend.
        precondition(AgentSend.notice(message: "", unsent: false, queued: 1, place: .setAside)
                     .contains("won't be sent again by itself"))

        print("PromptSubmissionState: 51 regression checks passed")
    }
}
