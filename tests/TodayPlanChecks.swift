import Foundation
import AppKit

// Inert dependency: all requests are captured by the injected transport below.
enum WispClient { static let baseURL = URL(string: "http://127.0.0.1:1")! }

@main
struct TodayPlanChecks {
    @MainActor static var count = 0
    @MainActor static func check(_ condition: Bool, _ message: String = "Today contract failed") {
        precondition(condition, message)
        count += 1
    }
    static func instant(_ value: String) -> Date {
        ISO8601DateFormatter().date(from: value)!
    }
    static func query(_ request: URLRequest, _ name: String) -> String {
        URLComponents(url: request.url!, resolvingAgainstBaseURL: true)!.queryItems!.first { $0.name == name }!.value!
    }
    static func planData(day: String, zone: String, blocks: [String] = []) throws -> Data {
        let payload: [String: Any] = [
            "day": day, "timezone": zone, "generated_at": 1000,
            "revision": 3, "provisional": false, "warnings": [], "tasks": [],
            "blocks": blocks.enumerated().map { index, title in
                ["id": "event-\(index)", "title": title, "start": 1000.0, "kind": "calendar", "warnings": []] as [String: Any]
            },
            "deadlines": [], "all_day": [], "unscheduled": [],
            "sources": ["calendar": ["ready": true, "last_sync": 1000, "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1080, "not_before": NSNull()]
        ]
        return try JSONSerialization.data(withJSONObject: payload)
    }
    static func response(_ request: URLRequest) -> HTTPURLResponse {
        HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
    }
    @MainActor
    static func waitFor(_ message: String, until condition: () -> Bool) async {
        let deadline = Date().addingTimeInterval(5)
        while Date() < deadline {
            if condition() { check(true); return }
            try? await Task.sleep(for: .milliseconds(20))
        }
        check(false, message)
    }

    // Run with WISP_TODAY_WINDOW_CHECK=1 in an isolated macOS GUI session. The
    // same retained NSWindow and SwiftUI view are exercised without Wisp or a service.
    @MainActor
    static func windowChecks() async throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let la = TimeZone(identifier: "America/Los_Angeles")!
        var zone = la
        var now = instant("2026-09-25T06:59:00Z")
        let model = TodayModel(now: { now }, timeZoneProvider: { zone })
        var presentations: [TodayPresentation] = []
        var queries: [(String, String)] = []
        var holdYesterday = false
        var pending: CheckedContinuation<(Data, URLResponse), Error>?
        model.transport = { request in
            let day = query(request, "day")
            let requestedZone = query(request, "timezone")
            queries.append((day, requestedZone))
            if holdYesterday && day == "2026-09-24" {
                return try await withCheckedThrowingContinuation { continuation in pending = continuation }
            }
            return (try planData(day: day, zone: requestedZone,
                                 blocks: day == "2026-09-24" ? ["Yesterday's event"] : []), response(request))
        }
        let window = TodayWindow.show(model: model, onPresentation: { presentations.append($0) }, present: false)
        await waitFor("Mounted view must display yesterday's event") {
            presentations.last?.day == "2026-09-24" && presentations.last?.pickerDay == "2026-09-24" &&
            presentations.last?.blockTitles == ["Yesterday's event"]
        }
        holdYesterday = true
        let stale = Task { await model.refresh() }
        await waitFor("Old-day request must be held") { pending != nil }
        now = instant("2026-09-25T07:01:00Z")
        let rolloverIndex = presentations.count
        NotificationCenter.default.post(name: .NSCalendarDayChanged, object: nil)
        await waitFor("Mounted view must display the empty current day") {
            presentations.last?.day == "2026-09-25" && presentations.last?.pickerDay == "2026-09-25" &&
            presentations.last?.loading == false &&
            presentations.last?.blockTitles.isEmpty == true
        }
        check(queries.contains { $0.0 == "2026-09-25" && $0.1 == la.identifier })
        let oldRequest = URLRequest(url: URL(string: "http://127.0.0.1:1/assistant/today")!)
        pending!.resume(returning: (try planData(day: "2026-09-24", zone: la.identifier,
                                                  blocks: ["Late yesterday"]), response(oldRequest)))
        await stale.value
        try? await Task.sleep(for: .milliseconds(50))
        check(presentations.dropFirst(rolloverIndex).allSatisfy { !$0.blockTitles.contains("Late yesterday") },
              "Delayed response must never become visible in the mounted view")
        check(presentations.last?.blockTitles.isEmpty == true)

        window.close()
        let queriesBeforeReopen = queries.count
        let reopened = TodayWindow.show(present: true)
        check(reopened === window && reopened.isVisible, "Today must reuse its window on reopen")
        await waitFor("Reopened view must still display the empty current day") {
            presentations.last?.day == "2026-09-25" && presentations.last?.pickerDay == "2026-09-25" &&
            presentations.last?.blockTitles.isEmpty == true && queries.count > queriesBeforeReopen
        }
        zone = TimeZone(identifier: "America/New_York")!
        NotificationCenter.default.post(name: .NSSystemTimeZoneDidChange, object: nil)
        await waitFor("Mounted view must query the new zone") {
            presentations.last?.timezone == zone.identifier && presentations.last?.loading == false &&
            queries.contains { $0.0 == "2026-09-25" && $0.1 == zone.identifier }
        }
        now = instant("2026-09-27T19:00:00Z")
        NSWorkspace.shared.notificationCenter.post(name: NSWorkspace.didWakeNotification, object: nil)
        await waitFor("Wake must show the current local day") {
            presentations.last?.day == "2026-09-27" && presentations.last?.pickerDay == "2026-09-27" &&
            presentations.last?.loading == false
        }
        let queriesBeforeActivation = queries.count
        NotificationCenter.default.post(name: NSApplication.didBecomeActiveNotification, object: app)
        await waitFor("Activation must refresh the mounted current day") {
            queries.count > queriesBeforeActivation && presentations.last?.day == "2026-09-27"
        }

        model.selectDate(instant("2026-09-23T16:00:00Z"))
        await model.refresh()
        await waitFor("Manual date must appear in the mounted view") {
            presentations.last?.day == "2026-09-23" && presentations.last?.pickerDay == "2026-09-23" &&
            presentations.last?.followsToday == false
        }
        now = instant("2026-09-29T19:00:00Z")
        NSWorkspace.shared.notificationCenter.post(name: NSWorkspace.didWakeNotification, object: nil)
        zone = la
        NotificationCenter.default.post(name: .NSSystemTimeZoneDidChange, object: nil)
        await waitFor("Manual civil date must survive wake and zone change") {
            presentations.last?.day == "2026-09-23" && presentations.last?.pickerDay == "2026-09-23" &&
            presentations.last?.timezone == la.identifier &&
            presentations.last?.followsToday == false && presentations.last?.loading == false
        }
        model.returnToToday()
        await model.refresh()
        await waitFor("Returning to Today must resume following") {
            presentations.last?.day == "2026-09-29" && presentations.last?.pickerDay == "2026-09-29" &&
            presentations.last?.followsToday == true &&
            presentations.last?.loading == false
        }
        reopened.close()
        now = instant("2026-09-30T19:00:00Z")
        let queriesBeforeNextReopen = queries.count
        let nextReopen = TodayWindow.show(present: true)
        check(nextReopen === window)
        await waitFor("Reopening after another local day must advance the displayed date") {
            presentations.last?.day == "2026-09-30" && presentations.last?.pickerDay == "2026-09-30" &&
            presentations.last?.blockTitles.isEmpty == true && queries.count > queriesBeforeNextReopen
        }
        nextReopen.close()
    }
    @MainActor
    static func main() async throws {
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let model = TodayModel(timezone: zone)
        model.selectDate(Date(timeIntervalSince1970: 1790290800)) // Sep 24 2026, local
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
        check(model.plan?.revision == 3)
        check(URLComponents(url: captured[0].url!, resolvingAgainstBaseURL: true)?.queryItems?.contains(URLQueryItem(name: "timezone", value: zone.identifier)) == true)
        check(model.day == day)
        let added = await model.add(title: "Study", kind: "study", minutes: 45, priority: 1, due: nil)
        check(added)
        let post = captured.first { $0.httpMethod == "POST" }!
        let payload = try JSONSerialization.jsonObject(with: post.httpBody!) as! [String: Any]
        check(payload["duration_minutes"] as? Int == 45)
        check(payload["day"] as? String == day)
        captured = []
        var refreshedWhileBusy = false
        model.transport = { req in
            captured.append(req)
            if req.httpMethod == "GET" { refreshedWhileBusy = model.busy }
            return (data, HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        await model.replan(start: 540, end: 1080)
        check(refreshedWhileBusy && !model.busy, "Controls must stay disabled through mutation refresh")
        let replan = try JSONSerialization.jsonObject(with: captured[0].httpBody!) as! [String: Any]
        check(replan["revision"] as? Int == 3)
        check(replan["not_before"] is NSNull)
        model.transport = { req in
            (Data("{\"detail\":\"Refresh before editing\"}".utf8),
             HTTPURLResponse(url: req.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
        }
        let changed = await model.mutate("/assistant/today/tasks/x", method: "PATCH", body: ["revision": 1])
        check(!changed && model.error == "Refresh before editing" && model.plan == nil)
        model.transport = { req in
            model.selectDate(model.selectedDate.addingTimeInterval(86400))
            return (data, HTTPURLResponse(url: req.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        await model.refresh()
        check(model.plan == nil, "Response for previous day must not replace current day")

        // Follow local dates across midnight and wake, and never retain yesterday's blocks.
        var now = instant("2026-09-25T06:59:00Z") // Sep 24, 23:59 in Los Angeles
        var currentZone = zone
        let following = TodayModel(now: { now }, timeZoneProvider: { currentZone })
        var queried: [(String, String)] = []
        following.transport = { request in
            let requestedDay = query(request, "day")
            let requestedZone = query(request, "timezone")
            queried.append((requestedDay, requestedZone))
            return (try planData(day: requestedDay, zone: requestedZone,
                                 blocks: requestedDay == "2026-09-24" ? ["Yesterday's event"] : []), response(request))
        }
        await following.refresh()
        check(following.day == "2026-09-24" && following.plan?.blocks.count == 1)
        now = instant("2026-09-25T07:01:00Z")
        check(following.reconcileClock() && following.plan == nil, "Midnight clears old events before fetch")
        await following.refresh()
        check(following.day == "2026-09-25" && following.plan?.blocks.isEmpty == true)
        check(queried.last?.0 == "2026-09-25", "Midnight queries the new local day")
        now = instant("2026-09-27T19:00:00Z") // synthetic multi-day sleep/wake
        await following.refresh()
        check(following.day == "2026-09-27" && following.plan?.day == "2026-09-27")
        currentZone = TimeZone(identifier: "America/New_York")!
        check(following.reconcileClock() && following.plan == nil, "Time-zone change clears the old-zone plan")
        await following.refresh()
        check(queried.last?.1 == currentZone.identifier && following.plan?.timezone == currentZone.identifier)

        // DatePicker selections remain civil dates through zone changes; choosing Today resumes following.
        following.selectDate(instant("2026-09-23T16:00:00Z"))
        await following.refresh()
        check(!following.followsToday && following.day == "2026-09-23")
        currentZone = zone
        await following.refresh()
        check(following.day == "2026-09-23" && following.plan?.timezone == zone.identifier)
        check(Calendar(identifier: .gregorian).dateComponents(in: zone, from: following.selectedDate).day == 23)
        now = instant("2026-09-29T19:00:00Z")
        await following.refresh()
        check(following.day == "2026-09-23", "Explicit date must survive later local days")
        following.returnToToday()
        await following.refresh()
        check(following.followsToday && following.day == "2026-09-29")
        following.selectDate(now)
        check(following.followsToday, "Selecting today's date also resumes following")
        now = instant("2026-09-25T04:00:00Z") // Sep 24 in LA, Sep 25 in New York
        await following.refresh()
        check(following.day == "2026-09-24")
        currentZone = TimeZone(identifier: "America/New_York")!
        await following.refresh()
        check(following.day == "2026-09-25" && queried.last?.0 == "2026-09-25" &&
              queried.last?.1 == currentZone.identifier,
              "Moving across local date zones queries the destination day's events")

        // DST days can last 23 or 25 hours; civil dates, rather than +86400s, choose queries.
        for (before, after, expected) in [
            ("2026-03-08T09:59:00Z", "2026-03-08T10:01:00Z", "2026-03-08"),
            ("2026-03-09T06:59:00Z", "2026-03-09T07:01:00Z", "2026-03-09"),
            ("2026-11-01T08:59:00Z", "2026-11-01T09:01:00Z", "2026-11-01"),
            ("2026-11-02T07:59:00Z", "2026-11-02T08:01:00Z", "2026-11-02")
        ] {
            var instantNow = instant(before)
            let dst = TodayModel(timezone: zone, now: { instantNow })
            let initialDay = dst.day
            instantNow = instant(after)
            _ = dst.reconcileClock()
            check(dst.day == expected)
            check((initialDay == expected) == (before.contains("03-08") || before.contains("11-01")))
        }

        // An old fetch that completes after rollover cannot restore yesterday's results.
        var raceNow = instant("2026-09-25T06:59:00Z")
        let racing = TodayModel(timezone: zone, now: { raceNow })
        var pending: CheckedContinuation<(Data, URLResponse), Error>?
        racing.transport = { request in
            if query(request, "day") == "2026-09-24" {
                return try await withCheckedThrowingContinuation { continuation in pending = continuation }
            }
            return (try planData(day: query(request, "day"), zone: query(request, "timezone")), response(request))
        }
        let stale = Task { await racing.refresh() }
        while pending == nil { await Task.yield() }
        raceNow = instant("2026-09-25T07:01:00Z")
        await racing.refresh()
        check(racing.plan?.day == "2026-09-25" && racing.plan?.blocks.isEmpty == true)
        let oldRequest = URLRequest(url: URL(string: "http://127.0.0.1:1/assistant/today")!)
        pending!.resume(returning: (try planData(day: "2026-09-24", zone: zone.identifier, blocks: ["Old event"]), response(oldRequest)))
        await stale.value
        check(racing.plan?.day == "2026-09-25" && racing.plan?.blocks.isEmpty == true,
              "Delayed old-day response must not replace the empty current day")
        let taskData = try JSONSerialization.data(withJSONObject: [
            "id": "travel", "title": "Study", "day": "2026-09-24", "timezone": "America/Los_Angeles",
            "kind": "study", "duration_minutes": 60, "priority": 2, "status": "active", "revision": 1,
            "pinned_start": 1790317800.0
        ])
        let pinned = try JSONDecoder().decode(TodayTask.self, from: taskData)
        check(pinned.editingTimeZone.identifier == "America/Los_Angeles")
        let edits = pinned.editFields(title: "Renamed", kind: "study", priority: 1, minutes: 90,
                                     due: nil, pin: Date(timeIntervalSince1970: pinned.pinned_start!))
        check(edits["timezone"] == nil && edits["day"] == nil, "Ordinary edits must preserve the task's timezone and date")
        check(edits["pinned_start"] as? Double == pinned.pinned_start)
        if ProcessInfo.processInfo.environment["WISP_TODAY_WINDOW_CHECK"] == "1" {
            try await windowChecks()
        }
        print("\(count) passed, 0 failed, 0 skipped")
    }
}
