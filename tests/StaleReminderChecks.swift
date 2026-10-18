// Contract for the Day tab's stale-reminder questions
// (app/Sources/WispApp/StaleReminders.swift). Compiled with NotchTabChecks.swift,
// which calls staleReminderChecks. A fake transport stands in for the service:
// nothing is launched, contacted or deleted.
import Foundation

@MainActor
func staleReminderChecks(_ check: (Bool, String) -> Void) {
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
    check(StaleReminderAsk.failure(status: 422, detail: "field required", deleting: true) == "Couldn't delete it. Nothing was changed.",
          "validation errors don't leak")

    // The model, against a fake service.
    var requests: [String] = []
    var deleteStatus = 409
    var deleteThrows = false
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
            if deleteThrows { throw URLError(.timedOut) }
            return try reply(deleteStatus, ["detail": deleteStatus == 409 ? "Reminders outcome unknown." : "Reminders failed."])
        }
    }
    func run(_ work: @escaping @MainActor () async -> Void) {
        var done = false
        Task { @MainActor in await work(); done = true }
        while !done { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
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

    // A refresh that fails keeps what was shown.
    let quiet = StaleRemindersModel(baseURL: URL(string: "http://fixture.invalid")!) { _ in throw URLError(.cannotConnectToHost) }
    run { await quiet.refresh() }
    check(quiet.asks.isEmpty && quiet.failures.isEmpty, "an unreachable service changes nothing")
}
