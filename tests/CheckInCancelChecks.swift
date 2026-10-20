import Foundation

/// Offline checks for CheckInCancel (app/Sources/WispApp/CheckInCancel.swift): the
/// Undo-bar wording, the exact request the service accepts, how each outcome of
/// POST /assistant/checkin/cancel_event is shown, and the Undo windows: the delete
/// is sent once, only after the window, never after Undo, and never hurried by a
/// second Cancel.
@main struct CheckInCancelCheck {
    static func main() async {
        await MainActor.run { mappingChecks() }
        await queueChecks()
        print("CheckInCancel checks passed")
    }

    @MainActor static func mappingChecks() {
        precondition(CheckInCancel.undoWindow == 5, "the user confirmed a 5-second Undo")
        precondition(CheckInCancel.requestTimeout >= 60, "the service may wait 8 s + 45 s before answering")
        precondition(CheckInCancel.undoLabel(inviteeCount: 0) == "Cancelled")
        precondition(CheckInCancel.undoLabel(inviteeCount: 1) == "Cancelled · its 1 invitee will be notified")
        precondition(CheckInCancel.undoLabel(inviteeCount: 3) == "Cancelled · its 3 invitees will be notified")
        precondition(CheckInCancel.doneLabel(title: "Dentist", inviteeCount: 0) == "“Dentist” cancelled")
        precondition(CheckInCancel.doneLabel(title: "Sync", inviteeCount: 2) == "“Sync” cancelled · 2 invitees notified")
        precondition(CheckInCancel.blockedReason("Only the organizer can cancel it.") == "Only the organizer can cancel it.")
        precondition(CheckInCancel.blockedReason("").contains("Calendar"), "an older service gives no reason")

        // What replaces Cancel: a read-only calendar is final even if the service allowed it;
        // someone else's event is declined in Calendar; other blocks open the event there.
        precondition(CheckInCancel.blockedControl(canCancel: true, calendarWritable: true, organizerIsUser: nil) == .cancel)
        precondition(CheckInCancel.blockedControl(canCancel: true, calendarWritable: nil, organizerIsUser: true) == .cancel,
                     "an older app that didn't say is not read-only")
        precondition(CheckInCancel.blockedControl(canCancel: true, calendarWritable: false, organizerIsUser: nil) == .readOnly,
                     "a subscribed calendar never gets a Cancel, whatever the service row said")
        precondition(CheckInCancel.blockedControl(canCancel: false, calendarWritable: false, organizerIsUser: false) == .readOnly)
        precondition(CheckInCancel.blockedControl(canCancel: false, calendarWritable: true, organizerIsUser: false) == .decline,
                     "an invitation is declined in Calendar")
        precondition(CheckInCancel.blockedControl(canCancel: false, calendarWritable: true, organizerIsUser: nil) == .open)
        precondition(CheckInCancel.blockedControl(canCancel: false, calendarWritable: nil, organizerIsUser: true) == .open)

        // The request: exactly the three fields the service accepts, from the row.
        let body = CheckInCancel.requestBody(sourceId: "abc", startTs: 1_900_000_000, inviteeCount: 2)
        precondition(body?.count == 3)
        precondition(body?["source_id"] as? String == "abc")
        precondition(body?["expected_start_ts"] as? Double == 1_900_000_000)
        precondition(body?["expected_invitees"] as? Int == 2)
        precondition(CheckInCancel.requestBody(sourceId: "", startTs: 1, inviteeCount: 0) == nil, "no calendar id")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: nil, inviteeCount: 0) == nil, "no start")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: .nan, inviteeCount: 0) == nil)
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 0, inviteeCount: 0) == nil)
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 1, inviteeCount: nil) == nil, "invitees not synced")
        precondition(CheckInCancel.requestBody(sourceId: "abc", startTs: 1, inviteeCount: -1) == nil)

        // The answer: only {ok: true} on 2xx is a verified cancel.
        precondition(CheckInCancel.failure(status: 200, body: ["ok": true, "invitee_count": 2]) == nil)
        let odd = CheckInCancel.failure(status: 200, body: ["ok": false])
        precondition(odd?.outcome == "unknown" && odd?.isUncertain == true && odd?.canRetry == false)
        precondition(CheckInCancel.failure(status: 200, body: nil)?.outcome == "unknown")
        // No answer: a timeout may mean it was cancelled, so no retry; a connection
        // that never opened means nothing was sent, so a retry is fine.
        let timedOut = CheckInCancel.failure(status: nil, body: nil, transport: .timedOut)
        precondition(timedOut?.outcome == "no_answer" && timedOut?.isUncertain == true && timedOut?.canRetry == false)
        precondition(timedOut?.message.contains("Check Calendar") == true)
        precondition(CheckInCancel.failure(status: nil, body: nil)?.isUncertain == true, "no error at all is still uncertain")
        let down = CheckInCancel.failure(status: nil, body: nil, transport: .cannotConnectToHost)
        precondition(down?.outcome == "unreachable" && down?.isUncertain == false && down?.canRetry == true)
        precondition(down?.message.contains("nothing was cancelled") == true)
        precondition(CheckInCancel.failure(status: nil, body: nil, transport: .notConnectedToInternet)?.outcome == "unreachable")
        let dropped = CheckInCancel.failure(status: nil, body: nil, transport: .networkConnectionLost)
        precondition(dropped?.isUncertain == true && dropped?.canRetry == false,
                     "a connection lost mid-request may have delivered the cancel")
        func failure(_ status: Int, _ outcome: String, _ message: String = "msg.") -> CheckInCancel.Failure? {
            CheckInCancel.failure(status: status, body: ["detail": ["outcome": outcome, "message": message]])
        }
        precondition(failure(404, "not_found", "Gone.")?.message == "Gone.", "the service's message is shown as is")
        precondition(failure(404, "not_found")?.canRetry == false)
        precondition(failure(403, "not_allowed")?.canRetry == false)
        precondition(failure(409, "not_allowed")?.canRetry == false, "invitees not synced yet")
        precondition(failure(409, "not_current")?.canRetry == true, "a refreshed card may cancel it")
        let unknown = failure(409, "unknown")
        precondition(unknown?.canRetry == false && unknown?.isUncertain == true, "check Calendar first; never a retry button")
        precondition(failure(503, "unavailable")?.canRetry == true)
        precondition(failure(502, "failed")?.canRetry == true)
        precondition(failure(502, "failed")?.isUncertain == false)
        let invalid = CheckInCancel.failure(status: 422, body: ["detail": [["loc": ["body"], "msg": "x"]]])
        precondition(invalid?.outcome == "invalid" && invalid?.message.contains("Nothing was cancelled") == true,
                     "pydantic's list detail gets a plain line")
        precondition(invalid?.canRetry == false, "the same body would fail again")
        let crashed = CheckInCancel.failure(status: 500, body: nil)
        precondition(crashed?.isUncertain == true && crashed?.canRetry == false,
                     "an error without a structured outcome may have come after the app cancelled")
        precondition(CheckInCancel.failure(status: 502, body: ["detail": "plain text"])?.isUncertain == true)
        precondition(CheckInCancel.failure(status: 502, body: ["detail": ["outcome": "failed"]])?.isUncertain == true,
                     "outcome without a message is not the service's structured refusal")
        let changed = CheckInCancel.changed(title: "Sync")
        precondition(changed.canRetry == true && changed.isUncertain == false && changed.message.contains("“Sync”"))

        precondition(CheckInCancel.notifiedCount(body: ["invitee_count": 3], rowCount: 1) == 3, "the service's count wins")
        precondition(CheckInCancel.notifiedCount(body: [:], rowCount: 1) == 1)
        precondition(CheckInCancel.notifiedCount(body: nil, rowCount: nil) == 0)
    }

    /// Drives the queue with a sleep that waits for the test to release it.
    @MainActor final class Harness {
        var slept: [TimeInterval] = []
        var waiters: [String: CheckedContinuation<Void, Error>] = [:]
        var committed: [String] = []
        var inFlightSeen: [String] = []
        var changes = 0
        var current = ""
        var queue: CheckInCancelQueue!

        init() {
            queue = CheckInCancelQueue(window: 5, sleep: { [unowned self] seconds in
                self.slept.append(seconds)
                let id = self.current
                try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in self.waiters[id] = c }
            }, commit: { [unowned self] id in
                self.committed.append(id)
                self.inFlightSeen.append(contentsOf: self.queue.inFlight.sorted())
            })
            queue.onChange = { [unowned self] in self.changes += 1 }
        }

        /// Opens a window and lets its timer task reach the sleep, so the next
        /// begin's id is not what this sleep records.
        func begin(_ id: String) async -> Bool {
            current = id
            let opened = queue.begin(id)
            await settle()
            return opened
        }
        /// Lets the timer task reach its sleep (or finish) before asserting.
        func settle() async { for _ in 0..<200 { await Task.yield() } }
        func elapse(_ id: String) async {
            let waiter = waiters.removeValue(forKey: id)
            precondition(waiter != nil, "no sleep was waiting for \(id)")
            waiter?.resume()
            await settle()
        }
    }

    @MainActor static func queueChecks() async {
        let h = Harness()
        precondition(h.queue.isEmpty)

        // Undo inside the window: the sleep is cancelled and nothing is sent.
        let opened1 = await h.begin("a")
        precondition(opened1)
        precondition(h.queue.pending["a"] != nil && h.queue.isActive("a"))
        await h.settle()
        precondition(h.slept == [5], "the wait is the full Undo window")
        precondition(h.queue.undo("a"))
        precondition(h.queue.pending["a"] == nil && !h.queue.isActive("a"))
        precondition(h.committed.isEmpty, "Undo sends nothing")
        await h.elapse("a")       // the old sleep ending late must not send either
        precondition(h.committed.isEmpty, "a sleep that ends after Undo sends nothing")
        precondition(!h.queue.undo("a"), "nothing left to undo")

        // Two windows side by side: each sends once, the first never hurried.
        let opened2 = await h.begin("a")
        precondition(opened2)
        let opened3 = await h.begin("b")
        precondition(opened3)
        let opened4 = await h.begin("a")
        precondition(!opened4, "a second Cancel on a pending row is refused")
        await h.settle()
        precondition(h.slept == [5, 5, 5])
        precondition(h.committed.isEmpty, "opening b does not send a")
        await h.elapse("b")
        precondition(h.committed == ["b"], "b ran out first; a is still waiting")
        precondition(h.inFlightSeen == ["b"], "b was in flight while its commit ran")
        precondition(h.queue.pending["a"] != nil && h.queue.pending["b"] == nil && !h.queue.isActive("b"))
        await h.elapse("a")
        precondition(h.committed == ["b", "a"])
        precondition(h.queue.isEmpty)

        // A commit in flight blocks a new window for the same id until it returns.
        let slow = Harness()
        var release: CheckedContinuation<Void, Never>?
        slow.queue = CheckInCancelQueue(window: 5, sleep: { [unowned slow] seconds in
            slow.slept.append(seconds)
            try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in slow.waiters[slow.current] = c }
        }, commit: { [unowned slow] id in
            slow.committed.append(id)
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in release = c }
        })
        let opened5 = await slow.begin("c")
        precondition(opened5)
        await slow.settle()
        await slow.elapse("c")
        precondition(slow.committed == ["c"] && slow.queue.inFlight.contains("c"))
        let opened6 = await slow.begin("c")
        precondition(!opened6, "no second request while the first is unanswered")
        precondition(!slow.queue.undo("c"), "too late to undo once sent")
        release?.resume()
        await slow.settle()
        precondition(slow.queue.isEmpty, "the id frees once the service answered")
        let opened7 = await slow.begin("c")
        precondition(opened7, "and may be cancelled again after that")
        precondition(slow.queue.undo("c"))
        slow.waiters.removeValue(forKey: "c")?.resume(throwing: CancellationError())   // tidy the undone sleep
        await slow.settle()
        precondition(slow.committed == ["c"], "the undone window sent nothing more")
        precondition(h.changes > 0, "the model is told about every change")
    }
}
