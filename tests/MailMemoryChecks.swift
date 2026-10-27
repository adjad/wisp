// Contract for the Brief's mail suggestions (app/Sources/WispApp/MailMemory.swift).
// A fake transport stands in for the service: nothing is launched, contacted or added.
// swiftc -parse-as-library app/Sources/WispApp/MailMemory.swift tests/MailMemoryChecks.swift -o /tmp/<dir>/mail-memory-checks
import Foundation

@main
enum MailMemoryChecks {
    static var failures = 0
    static var total = 0

    static func check(_ ok: Bool, _ label: String) {
        total += 1
        if !ok { failures += 1; print("FAIL: \(label)") }
    }

    static func row(_ id: Int, _ what: String, due: String = "2027-07-02", precision: String = "day",
                    from: String = "Harbor", actions: [String] = ["accept", "dismiss", "open"]) -> [String: Any] {
        ["id": id, "what": what, "due": due, "precision": precision, "quote": "q", "from": from,
         "subject": "Your lease", "message_id": "<lease-1@harbor.example>", "actions": actions]
    }

    @MainActor
    static func main() async {
        // Parsing.
        let parsed = MailSuggestion.parse(["mode": "live", "suggestions": [
            row(1, "Give notice"), row(2, " "), row(3, "Bad date", due: "2027-13-40"), row(4, "No dismiss", actions: ["accept"]),
        ]])
        check(parsed?.mode == "live" && parsed?.items.map(\.id) == [1], "only complete rows: \(parsed?.items.map(\.id) ?? [])")
        check(MailSuggestion.parse(["detail": "x"]) == nil, "an unreadable reply is not an empty list")

        // Labels.
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        let now = cal.date(from: DateComponents(year: 2026, month: 10, day: 26))!
        let us = Locale(identifier: "en_US")
        let item = parsed!.items[0]
        check(item.dueLabel(now: now, calendar: cal, locale: us) == "Fri, Jul 2, 2027", "day label: \(item.dueLabel(now: now, calendar: cal, locale: us))")
        let month = MailSuggestion.parse(["mode": "live", "suggestions": [row(5, "Book cleaning", due: "2026-10-31", precision: "month")]])!.items[0]
        check(month.dueLabel(now: now, calendar: cal, locale: us) == "in October", "month label")
        let week = MailSuggestion.parse(["mode": "live", "suggestions": [row(6, "Send ch. 3", due: "2026-11-08", precision: "week")]])!.items[0]
        check(week.dueLabel(now: now, calendar: cal, locale: us) == "week of Nov 2", "week label: \(week.dueLabel(now: now, calendar: cal, locale: us))")
        check(item.detail == "From Harbor · “Your lease”", "detail")
        let mine = MailSuggestion.parse(["mode": "live", "suggestions": [row(7, "Q4 forecast", from: "you")]])!.items[0]
        check(mine.detail.hasPrefix("From your email"), "own sent mail")
        check(item.mailURL?.absoluteString == "message://%3Clease-1@harbor.example%3E", "message link: \(item.mailURL?.absoluteString ?? "nil")")
        let unicode = MailSuggestion(id: 10, what: "x", due: "2027-01-01", precision: "day", quote: "", from: "", subject: "",
                                     messageID: "<café-1@exämple.example>")
        check(unicode.mailURL?.absoluteString == "message://%3Ccaf%C3%A9-1@ex%C3%A4mple.example%3E",
              "a non-ASCII Message-ID is percent-encoded: \(unicode.mailURL?.absoluteString ?? "nil")")
        let odd = MailSuggestion(id: 11, what: "x", due: "2027-01-01", precision: "day", quote: "", from: "", subject: "",
                                 messageID: "<a/b?c#d+e=f$g@h.example>")
        check(odd.mailURL?.absoluteString == "message://%3Ca%2Fb%3Fc%23d+e=f$g@h.example%3E", "reserved characters: \(odd.mailURL?.absoluteString ?? "nil")")
        let noID = MailSuggestion(id: 9, what: "x", due: "2027-01-01", precision: "day", quote: "", from: "", subject: "", messageID: "")
        check(noID.mailURL == nil, "no Message-ID, no link")

        // The model, through a fake service.
        var requests: [URLRequest] = []
        var replies: [(Int, [String: Any])] = []
        let transport: (URLRequest) async throws -> (Data, URLResponse) = { request in
            requests.append(request)
            let (status, body) = replies.isEmpty ? (500, [:]) : replies.removeFirst()
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (try JSONSerialization.data(withJSONObject: body), response)
        }
        let model = MailMemoryModel(baseURL: URL(string: "http://127.0.0.1:1/")!, transport: transport)
        replies = [(200, ["mode": "shadow", "suggestions": [row(1, "Give notice")]])]
        await model.refresh()
        check(model.mode == "shadow" && model.suggestions.isEmpty, "shadow shows nothing even if rows arrive")
        replies = [(200, ["mode": "live", "suggestions": [row(1, "Give notice"), row(2, "Pay tax")]])]
        await model.refresh()
        check(model.suggestions.map(\.id) == [1, 2], "live shows rows")
        replies = [(500, [:])]
        await model.refresh()
        check(model.suggestions.map(\.id) == [1, 2], "a failed read keeps what was shown")

        replies = [(200, ["ok": true, "decision": "accepted"]),
                   (200, ["mode": "live", "suggestions": [row(2, "Pay tax")]])]
        await model.accept(model.suggestions[0])
        let post = requests[requests.count - 2]
        check(post.httpMethod == "POST" && post.url?.path == "/assistant/mail_memory/accept"
              && post.value(forHTTPHeaderField: "Content-Type") == "application/json", "accept request")
        check(((try? JSONSerialization.jsonObject(with: post.httpBody ?? Data())) as? [String: Int]) == ["id": 1], "accept body")
        check(model.suggestions.map(\.id) == [2] && model.confirmation == "Added “Give notice” to Reminders.", "accepted")

        replies = [(503, ["detail": "Wisp's app isn't connected; nothing was added."])]
        await model.accept(model.suggestions[0])
        check(model.failures[2] == "Wisp's app isn't connected; nothing was added." && model.suggestions.count == 1,
              "a refusal says why and keeps the row")
        replies = [(200, ["ok": true, "decision": "dismissed"]), (200, ["mode": "live", "suggestions": []])]
        await model.dismiss(model.suggestions[0])
        check(model.suggestions.isEmpty && model.failures.isEmpty && model.answering == nil, "dismissed")

        // A confirmation is said once: a refresh more than 8 seconds later clears it.
        var clockNow = Date(timeIntervalSince1970: 1_000_000)
        let timed = MailMemoryModel(baseURL: URL(string: "http://127.0.0.1:1/")!, transport: transport, clock: { clockNow })
        replies = [(200, ["mode": "live", "suggestions": [row(1, "Give notice"), row(2, "Pay tax")]])]
        await timed.refresh()
        replies = [(200, ["ok": true, "decision": "accepted"]),
                   (200, ["mode": "live", "suggestions": [row(2, "Pay tax")]])]
        await timed.accept(timed.suggestions[0])
        check(timed.confirmation == "Added “Give notice” to Reminders.", "confirmation survives its own refresh")
        clockNow += 5
        replies = [(200, ["mode": "live", "suggestions": [row(2, "Pay tax")]])]
        await timed.refresh()
        check(timed.confirmation != nil, "a refresh within 8 seconds keeps the confirmation")
        clockNow += 4
        replies = [(200, ["mode": "live", "suggestions": [row(2, "Pay tax")]])]
        await timed.refresh()
        check(timed.confirmation == nil, "a refresh after 8 seconds clears the confirmation")

        // The Settings switch.
        let fresh = MailMemoryModel(baseURL: URL(string: "http://127.0.0.1:1/")!, transport: transport)
        check(!fresh.known, "nothing known before the service answers")
        replies = [(200, ["ok": true, "mode": "shadow"]), (200, ["mode": "shadow", "suggestions": []])]
        await model.setMode("shadow")
        let put = requests[requests.count - 2]
        check(put.httpMethod == "PUT" && put.url?.path == "/assistant/mail_memory/settings"
              && ((try? JSONSerialization.jsonObject(with: put.httpBody ?? Data())) as? [String: String]) == ["mode": "shadow"],
              "mode request")
        check(model.mode == "shadow" && model.known && model.modeStatus == nil, "mode changed")
        let sent = requests.count
        await model.setMode("everything")
        check(requests.count == sent, "an unknown mode is never sent")
        replies = [(409, ["detail": "x"])]
        await model.setMode("live")
        check(model.mode == "shadow" && model.modeStatus == "Couldn't change it. Try again.", "a refused change keeps the mode")

        if failures > 0 { print("\(failures) failure(s)"); exit(1) }
        print("MailMemory: \(total) checks passed")
    }
}
