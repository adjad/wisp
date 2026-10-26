// Contract for the Brief's Needs you Clear and Mark done
// (app/Sources/WispApp/NeedsYouActions.swift). Compiled with NotchTabChecks.swift,
// which calls needsYouActionChecks. A fake transport stands in for the service:
// nothing is launched, contacted, completed or deleted.
import Foundation

@MainActor
func needsYouActionChecks(_ check: (Bool, String) -> Void) {
    let reminder = NeedsYouRow(id: "reminders:aaaaaaaaaaaaaaaaaaaaaaaa", label: "OVERDUE",
                               text: "Wisp verification timed 2113", overdue: true,
                               fingerprint: "bbbbbbbbbbbbbbbbbbbbbbbb", canComplete: true)
    let mail = NeedsYouRow(id: "email:cccccccccccccccccccccccc", label: "MAIL", text: "“Thesis” from Advisor",
                           overdue: false, fingerprint: "dddddddddddddddddddddddd")

    var requests: [(String, String?)] = []
    var status = 200
    var throwsError = false
    var hold = false
    let model = NeedsYouActionsModel(baseURL: URL(string: "http://fixture.invalid")!) { request in
        let body = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
        requests.append(((request.httpMethod ?? "GET") + " " + (request.url?.path ?? ""), body?["fingerprint"] as? String))
        while hold { try await Task.sleep(nanoseconds: 5_000_000) }
        if throwsError { throw URLError(.timedOut) }
        let reply: [String: Any] = status == 200 ? ["ok": true] : ["detail": status == 502 ? "Native completion was not verified." : "Gone."]
        return (try JSONSerialization.data(withJSONObject: reply),
                HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }
    func run(_ work: @escaping @MainActor () async -> Void) {
        var done = false
        Task { @MainActor in await work(); done = true }
        let deadline = Date().addingTimeInterval(5)
        while !done, Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        precondition(done, "Timed out waiting for the Needs you actions model")
    }

    // The × clears at once; a reminder also offers its ✓ Mark done.
    check(model.actions(for: reminder).contains(.markDone), "a reminder offers Mark done")
    check(model.actions(for: mail) == [.clear], "mail offers Clear only")

    // Clear: one POST with the row's fingerprint, then the row hides.
    run { await model.perform(.clear, on: mail) }
    check(requests.map(\.0) == ["POST /assistant/needs_you/email:cccccccccccccccccccccccc/clear"]
          && requests.first?.1 == mail.fingerprint, "Clear posts the id and fingerprint: \(requests)")
    check(model.isHidden(mail) && model.failures.isEmpty, "a confirmed Clear hides the row")
    let changed = NeedsYouRow(id: mail.id, label: "MAIL", text: "“Thesis v2” from Advisor", overdue: false,
                              fingerprint: "eeeeeeeeeeeeeeeeeeeeeeee")
    check(!model.isHidden(changed), "the same item, changed, shows again")

    // Mark done in flight says so and blocks a second answer.
    requests = []
    hold = true
    Task { @MainActor in await model.perform(.markDone, on: reminder) }
    let wait = Date().addingTimeInterval(5)
    while model.busy.isEmpty, Date() < wait { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    check(model.busy[reminder.id] == .markDone && NeedsYouRowAction.markDone.progress == "Marking done…",
          "Mark done shows progress while the service completes it")
    check(!model.isHidden(reminder), "the row stays until the completion is confirmed")
    run { await model.perform(.clear, on: reminder) }
    check(requests.count == 1, "a second answer while one is pending sends nothing")
    hold = false
    let settle = Date().addingTimeInterval(5)
    while !model.busy.isEmpty, Date() < settle { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    check(model.isHidden(reminder) && requests.map(\.0) == ["POST /assistant/needs_you/reminders:aaaaaaaaaaaaaaaaaaaaaaaa/done"],
          "a verified Mark done removes the row: \(requests)")

    // A failed or unanswered Mark done keeps the row and offers no second try.
    let other = NeedsYouRow(id: "reminders:ffffffffffffffffffffffff", label: "DUE", text: "Pay rent", overdue: false,
                            fingerprint: "111111111111111111111111", canComplete: true)
    status = 502
    run { await model.perform(.markDone, on: other) }
    check(!model.isHidden(other) && model.failures[other.id] == "Native completion was not verified."
          && model.actions(for: other) == [.clear], "an unverified completion says so and keeps Clear only")
    let sent = requests.count
    run { await model.perform(.markDone, on: other) }
    check(requests.count == sent, "a blocked Mark done sends nothing")
    let third = NeedsYouRow(id: "reminders:222222222222222222222222", label: "DUE", text: "Call dentist", overdue: false,
                            fingerprint: "333333333333333333333333", canComplete: true)
    throwsError = true
    run { await model.perform(.markDone, on: third) }
    check(model.failures[third.id]?.contains("may or may not be done") == true && !model.isHidden(third),
          "no reply is unknown, never 'try again'")
    throwsError = false
    status = 500
    run { await model.perform(.clear, on: third) }
    check(model.failures[third.id] == "Couldn't clear it. Try again." && !model.isHidden(third), "a failed Clear")

    // The proactive Brief's rows post to their own routes.
    status = 200
    requests = []
    let todo = NeedsYouRow(id: "todo:abc", label: nil, text: "Wisp verification timed 2113", overdue: false,
                           fingerprint: "555555555555555555555555", canComplete: true, route: NeedsYouRow.todoRoute)
    let thread = NeedsYouRow(id: "thread:t1", label: nil, text: "Mom: Dinner?", overdue: false,
                             fingerprint: "666666666666666666666666", route: NeedsYouRow.threadRoute)
    run { await model.perform(.markDone, on: todo) }
    run { await model.perform(.clear, on: thread) }
    check(requests.map(\.0) == ["POST /assistant/proactive/todos/todo:abc/done",
                                "POST /assistant/proactive/threads/thread:t1/clear"]
          && model.isHidden(todo) && model.isHidden(thread), "to-do and thread routes: \(requests.map(\.0))")
    check(thread.actions == [.clear], "a thread never offers Mark done")

    // A row from an older service (no fingerprint) offers nothing and sends nothing.
    let bare = NeedsYouRow(id: "reminders:444444444444444444444444", label: "DUE", text: "Old", overdue: false)
    let before = requests.count
    run { await model.perform(.clear, on: bare) }
    check(requests.count == before && bare.actions.isEmpty, "no fingerprint, no Clear")
    check(NeedsYouActionsModel.failure(status: 404, detail: nil, action: .clear) == "That's no longer in Needs you.",
          "a gone row")
}
