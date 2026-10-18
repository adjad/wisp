// Contract for the Day tab's stale-reminder questions
// (app/Sources/WispApp/StaleReminders.swift). Compiled with NotchTabChecks.swift,
// which calls staleReminderChecks. A fake transport stands in for the service:
// nothing is launched, contacted or deleted.
import Foundation

@MainActor
func staleReminderChecks(_ check: (Bool, String) -> Void) {
    // The hub's "changed" event reaches the Day tab under this name (OverlayModel posts it).
    check(Notification.Name.wispAssistantChanged.rawValue == "WispAssistantChanged", "the change notification's name")

    // The reply.
    let reply: [String: Any] = ["asks": [
        ["kind": "stale_reminder", "source_id": "r1", "title": "Return library book", "context": "Errands",
         "created_ts": 1, "age_days": 9, "actions": ["keep", "delete"]],
        ["kind": "stale_reminder", "source_id": "r2", "title": "Call dentist", "context": "", "age_days": 1,
         "actions": ["keep", "delete"]],
        ["kind": "stale_reminder", "source_id": "", "title": "no id", "actions": ["keep", "delete"]],
        ["kind": "stale_reminder", "source_id": "r3", "title": "Keep only", "actions": ["keep"]],
    ], "hidden": 4, "freshness": "current"]
    let asks = StaleReminderAsk.parse(reply)
    check(asks?.map(\.sourceID) == ["r1", "r2"], "questions need an id, a title and both answers: \(asks?.map(\.sourceID) ?? [])")
    check(asks?[0].detail == "Added 9 days ago · Errands" && asks?[1].detail == "Added 1 day ago", "row details")
    check(StaleReminderAsk.parse(["detail": "x"]) == nil, "an unreadable reply is not an empty list")

    // Failures in plain words; the service's detail wins.
    check(StaleReminderAsk.failure(status: 409, detail: "Reminders hasn't synced recently; nothing was deleted.", deleting: true)
          == "Reminders hasn't synced recently; nothing was deleted.", "the service's 409 detail")
    check(StaleReminderAsk.failure(status: 409, detail: nil, deleting: true).contains("couldn't confirm"), "an unknown delete is never 'try again'")
    check(StaleReminderAsk.failure(status: 502, detail: nil, deleting: true) == "Reminders didn't delete it.", "a failed delete")
    check(StaleReminderAsk.failure(status: 404, detail: nil, deleting: false) == "That reminder is no longer in the list.", "gone")
    check(StaleReminderAsk.failure(status: 422, detail: "field required", deleting: true).contains("couldn't confirm"),
          "validation errors don't leak")
    check(StaleReminderAsk.failure(status: 500, detail: "Internal Server Error", deleting: true).contains("couldn't confirm")
          && !StaleReminderAsk.failure(status: 500, detail: nil, deleting: true).contains("Nothing was changed"),
          "an unexpected delete failure is unknown, never 'nothing was changed'")

    // The model, against a fake service.
    var requests: [String] = []
    var deleteStatus = 409
    var deleteThrows = false
    var holdDelete = false
    var listed: [[String: Any]] = (reply["asks"] as? [[String: Any]]) ?? []
    let model = StaleRemindersModel(baseURL: URL(string: "http://fixture.invalid")!) { request in
        let path = request.url?.path ?? ""
        requests.append((request.httpMethod ?? "GET") + " " + path)
        func reply(_ status: Int, _ body: [String: Any]) throws -> (Data, URLResponse) {
            (try JSONSerialization.data(withJSONObject: body),
             HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
        let id = ((try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any])?["source_id"] as? String
        switch path {
        case "/assistant/stale_reminders": return try reply(200, ["asks": listed])
        case "/assistant/stale_reminders/keep":
            listed.removeAll { $0["source_id"] as? String == id }
            return try reply(200, ["ok": true])
        default:
            while holdDelete { try await Task.sleep(nanoseconds: 5_000_000) }
            if deleteThrows { throw URLError(.timedOut) }
            if deleteStatus == 200 { listed.removeAll { $0["source_id"] as? String == id } }
            return try reply(deleteStatus, deleteStatus == 200 ? ["ok": true]
                             : ["detail": deleteStatus == 409 ? "Reminders outcome unknown." : deleteStatus == 404 ? "Gone." : "Reminders failed."])
        }
    }
    // Bounded: a task that never finishes fails the gate instead of hanging it.
    func run(_ work: @escaping @MainActor () async -> Void) {
        var done = false
        Task { @MainActor in await work(); done = true }
        let deadline = Date().addingTimeInterval(5)
        while !done, Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        precondition(done, "Timed out waiting for the stale-reminder model")
    }
    run { await model.refresh() }
    check(model.asks.map(\.sourceID) == ["r1", "r2"] && requests == ["GET /assistant/stale_reminders"], "refresh reads the questions only")

    let first = model.asks[0], second = model.asks[1]
    run { await model.delete(first) }
    check(model.failures["r1"] == "Reminders outcome unknown." && model.deleteBlocked.contains("r1")
          && model.asks.map(\.sourceID) == ["r1", "r2"], "an unknown delete says so, stays listed and can't be retried here")
    let before = requests.count
    run { await model.delete(first) }
    check(requests.count == before, "a blocked Delete sends nothing")

    deleteStatus = 502
    deleteThrows = true
    run { await model.delete(second) }
    check(model.failures["r2"]?.contains("may or may not be deleted") == true && model.deleteBlocked.contains("r2"),
          "no reply to a delete is unknown, not a failure to retry")

    run { await model.keep(first) }
    check(model.asks.map(\.sourceID) == ["r2"] && model.failures["r1"] == nil && model.answering == nil,
          "Keep removes the question and reads the list again: \(model.asks.map(\.sourceID))")
    check(requests.filter { $0.hasPrefix("POST") } == ["POST /assistant/stale_reminders/delete", "POST /assistant/stale_reminders/delete",
                                                         "POST /assistant/stale_reminders/keep"],
          "only the clicked answers were sent: \(requests)")

    // A delete that succeeds removes the row; a second answer waits for the first.
    listed = [["source_id": "r4", "title": "Old errand", "age_days": 8, "actions": ["keep", "delete"]],
              ["source_id": "r5", "title": "Gone already", "age_days": 8, "actions": ["keep", "delete"]]]
    run { await model.refresh() }
    let errand = model.asks[0], gone = model.asks[1]
    deleteStatus = 200
    deleteThrows = false
    holdDelete = true
    var started = false
    Task { @MainActor in started = true; await model.delete(errand) }
    let wait = Date().addingTimeInterval(5)
    while model.answering == nil, Date() < wait { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    check(started && model.answering == "r4", "a delete in flight marks its row")
    let sent = requests.count
    run { await model.keep(gone) }
    check(requests.count == sent, "a second answer while one is pending sends nothing")
    holdDelete = false
    let settle = Date().addingTimeInterval(5)
    while model.answering != nil, Date() < settle { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    check(model.answering == nil && model.asks.map(\.sourceID) == ["r5"] && model.failures["r4"] == nil,
          "a confirmed delete removes the row: \(model.asks.map(\.sourceID))")
    deleteStatus = 404
    run { await model.delete(gone) }
    check(model.failures["r5"] == "Gone." && model.deleteBlocked.contains("r5"), "a 404 says so and offers no second Delete")

    // A refresh that fails keeps what was shown.
    let quiet = StaleRemindersModel(baseURL: URL(string: "http://fixture.invalid")!) { _ in throw URLError(.cannotConnectToHost) }
    run { await quiet.refresh() }
    check(quiet.asks.isEmpty && quiet.failures.isEmpty, "an unreachable service changes nothing")
}
