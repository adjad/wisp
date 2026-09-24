import Foundation

// Inert dependency: all requests are captured by the injected transport below.
enum WispClient { static let baseURL = URL(string: "http://127.0.0.1:1")! }

@main
struct TodayPlanChecks {
    @MainActor
    static func main() async throws {
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let model = TodayModel(timezone: zone)
        model.selectedDate = Date(timeIntervalSince1970: 1790290800) // Sep 24 2026, local
        let day = model.day
        let fixture: [String: Any] = [
            "day": day, "timezone": zone.identifier, "generated_at": 1000,
            "revision": 3, "provisional": false, "warnings": [], "tasks": [],
            "blocks": [], "deadlines": [], "all_day": [], "unscheduled": [],
            "sources": ["calendar": ["ready": true, "last_sync": 1000, "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1080, "not_before": NSNull()]
        ]
        let data = try JSONSerialization.data(withJSONObject: fixture)
        var captured: [URLRequest] = []
        model.transport = { req in
            captured.append(req)
            return (data, HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        await model.refresh()
        precondition(model.plan?.revision == 3)
        precondition(URLComponents(url: captured[0].url!, resolvingAgainstBaseURL: true)?.queryItems?.contains(URLQueryItem(name: "timezone", value: zone.identifier)) == true)
        precondition(model.day == day)
        let added = await model.add(title: "Study", kind: "study", minutes: 45, priority: 1, due: nil)
        precondition(added)
        let post = captured.first { $0.httpMethod == "POST" }!
        let payload = try JSONSerialization.jsonObject(with: post.httpBody!) as! [String: Any]
        precondition(payload["duration_minutes"] as? Int == 45)
        precondition(payload["day"] as? String == day)
        captured = []
        var refreshedWhileBusy = false
        model.transport = { req in
            captured.append(req)
            if req.httpMethod == "GET" { refreshedWhileBusy = model.busy }
            return (data, HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        await model.replan(start: 540, end: 1080)
        precondition(refreshedWhileBusy && !model.busy, "Controls must stay disabled through mutation refresh")
        let replan = try JSONSerialization.jsonObject(with: captured[0].httpBody!) as! [String: Any]
        precondition(replan["revision"] as? Int == 3)
        precondition(replan["not_before"] is NSNull)
        model.transport = { req in
            (Data("{\"detail\":\"Refresh before editing\"}".utf8),
             HTTPURLResponse(url: req.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
        }
        let changed = await model.mutate("/assistant/today/tasks/x", method: "PATCH", body: ["revision": 1])
        precondition(!changed && model.error == "Refresh before editing" && model.plan == nil)
        model.transport = { req in
            model.selectedDate = model.selectedDate.addingTimeInterval(86400)
            return (data, HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        await model.refresh()
        precondition(model.plan == nil, "Response for previous day must not replace current day")
        print("Today native contract: PASS")
    }
}
