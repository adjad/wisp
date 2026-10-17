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

    @MainActor
    static func evidenceLabelChecks() throws {
        func block(_ kind: String) throws -> TodayBlock {
            let data = try JSONSerialization.data(withJSONObject: [
                "id": "fixture-hold", "title": "Possible time away", "start": 1000.0, "end": 2000.0,
                "kind": kind, "warnings": ["Availability is unknown"], "pattern_id": "opaque-pattern"
            ] as [String: Any])
            return try JSONDecoder().decode(TodayBlock.self, from: data)
        }
        let away = try block("behavior_away")
        check(away.evidenceLabel == "Computer inactivity · availability unknown")
        check(!away.evidenceLabel.lowercased().contains("busy"))
        check(away.inferenceIgnoreHelp == "Ignore this possible time away for the selected day")
        check(away.pattern_id == "opaque-pattern" && away.task_id == nil)
        let busy = try block("behavior")
        check(busy.evidenceLabel == "Tentative busy period · behavioral evidence")
        check(busy.inferenceIgnoreHelp == "Ignore this tentative busy period for the selected day")
        check(try block("calendar").evidenceLabel == "Calendar")
        check(try block("suggestion").evidenceLabel == "Source-backed suggestion · estimated duration")
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
        try evidenceLabelChecks()
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let model = TodayModel(timezone: zone)
        check(model.clock(instant("2026-09-24T07:00:00Z").timeIntervalSince1970) == "12:00 AM")
        check(model.clock(instant("2026-09-24T19:05:00Z").timeIntervalSince1970) == "12:05 PM")
        check(model.clock(instant("2026-09-24T22:05:00Z").timeIntervalSince1970) == "3:05 PM")
        check(TodayModel.workingHourLabel(0) == "12:00 AM")
        check(TodayModel.workingHourLabel(12) == "12:00 PM")
        check(TodayModel.workingHourLabel(18) == "6:00 PM")
        check(TodayModel.workingHourLabel(24) == "12:00 AM (next day)")
        check(TodayModel.defaultDeadline(for: "2026-03-08", in: zone) == instant("2026-03-09T07:00:00Z"),
              "Spring-forward deadline must use the next civil midnight after a 23-hour day")
        check(TodayModel.defaultDeadline(for: "2026-11-01", in: zone) == instant("2026-11-02T08:00:00Z"),
              "Fall-back deadline must use the next civil midnight after a 25-hour day")
        let tokyo = TimeZone(identifier: "Asia/Tokyo")!
        check(TodayModel.defaultDeadline(for: "2026-09-24", in: tokyo) == instant("2026-09-24T15:00:00Z"),
              "Deadline must use the task's zone")

        // Select tomorrow, enable the default deadline, then return to Today before adding.
        let draftNow = instant("2026-09-24T19:00:00Z")
        let draftZone = zone
        let draftModel = TodayModel(now: { draftNow }, timeZoneProvider: { draftZone })
        var deadlineDraft = TodayDeadlineDraft()
        draftModel.selectDate(instant("2026-09-25T19:00:00Z"))
        deadlineDraft.setEnabled(true, day: draftModel.day, zone: draftModel.timezone)
        check(deadlineDraft.date == instant("2026-09-26T07:00:00Z"))
        draftModel.returnToToday()
        deadlineDraft.rebaseDefault(day: draftModel.day, zone: draftModel.timezone)
        check(draftModel.day == "2026-09-24" && deadlineDraft.date == instant("2026-09-25T07:00:00Z"),
              "Today button must rebase an untouched default deadline to today's ending midnight")
        var sequenceRequests: [URLRequest] = []
        draftModel.transport = { request in
            sequenceRequests.append(request)
            if request.httpMethod == "GET" {
                return (try planData(day: query(request, "day"), zone: query(request, "timezone")), response(request))
            }
            return (Data(), response(request))
        }
        check(await draftModel.add(title: "Today task", kind: "task", minutes: 30,
                                   priority: 2, due: deadlineDraft.selectedDate))
        let sequencePost = sequenceRequests.first { $0.httpMethod == "POST" }!
        let sequencePayload = try JSONSerialization.jsonObject(with: sequencePost.httpBody!) as! [String: Any]
        check(sequencePayload["day"] as? String == "2026-09-24" &&
              sequencePayload["due_ts"] as? Double == instant("2026-09-25T07:00:00Z").timeIntervalSince1970,
              "Task day and default deadline must stay aligned after returning to Today")

        var manualNow = instant("2026-09-24T19:00:00Z")
        var manualZone = zone
        let manualModel = TodayModel(now: { manualNow }, timeZoneProvider: { manualZone })
        var manualDraft = TodayDeadlineDraft()
        manualDraft.setEnabled(true, day: manualModel.day, zone: manualModel.timezone)
        let chosenDue = Date(timeIntervalSince1970: 1_790_281_800)
        manualDraft.selectDate(chosenDue)
        manualDraft.setEnabled(false, day: manualModel.day, zone: manualModel.timezone)
        manualDraft.setEnabled(true, day: manualModel.day, zone: manualModel.timezone)
        check(manualDraft.date == chosenDue && !manualDraft.usesDefault,
              "Toggling a manually selected deadline off and on must preserve it")
        var manualRequests: [URLRequest] = []
        manualModel.transport = { request in
            manualRequests.append(request)
            if request.httpMethod == "GET" {
                return (try planData(day: query(request, "day"), zone: query(request, "timezone")), response(request))
            }
            return (Data(), response(request))
        }
        check(await manualModel.add(title: "Manual deadline", kind: "task", minutes: 30,
                                    priority: 2, due: manualDraft.selectedDate))
        let manualPost = manualRequests.first { $0.httpMethod == "POST" }!
        let manualPayload = try JSONSerialization.jsonObject(with: manualPost.httpBody!) as! [String: Any]
        check(manualPayload["due_ts"] as? Double == chosenDue.timeIntervalSince1970,
              "Adding after toggling the deadline off and on must save the chosen instant")
        manualDraft.setEnabled(false, day: manualModel.day, zone: manualModel.timezone)
        manualModel.selectDate(instant("2026-09-25T19:00:00Z"))
        manualDraft.rebaseDefault(day: manualModel.day, zone: manualModel.timezone)
        manualZone = tokyo
        check(manualModel.reconcileClock())
        manualDraft.rebaseDefault(day: manualModel.day, zone: manualModel.timezone)
        manualNow = instant("2026-09-25T19:00:00Z")
        manualModel.returnToToday()
        manualDraft.rebaseDefault(day: manualModel.day, zone: manualModel.timezone)
        manualDraft.setEnabled(true, day: manualModel.day, zone: manualModel.timezone)
        check(manualDraft.date == chosenDue && !manualDraft.usesDefault,
              "Manual deadline must survive day, timezone, and clock changes while disabled")
        check(manualDraft.resetToDefault(day: manualModel.day, zone: manualModel.timezone) &&
              manualDraft.date == TodayModel.defaultDeadline(for: manualModel.day, in: manualModel.timezone) &&
              manualDraft.usesDefault,
              "Explicit reset must restore the current day's automatic default")

        var disabledNow = instant("2026-09-24T19:00:00Z")
        var disabledZone = zone
        let disabledModel = TodayModel(now: { disabledNow }, timeZoneProvider: { disabledZone })
        var disabledDraft = TodayDeadlineDraft()
        disabledDraft.setEnabled(true, day: disabledModel.day, zone: disabledModel.timezone)
        disabledDraft.setEnabled(false, day: disabledModel.day, zone: disabledModel.timezone)
        disabledModel.selectDate(instant("2026-09-25T19:00:00Z"))
        disabledDraft.rebaseDefault(day: disabledModel.day, zone: disabledModel.timezone)
        check(disabledDraft.date == instant("2026-09-26T07:00:00Z"),
              "Automatic default must track a selected-day change while disabled")
        disabledZone = tokyo
        check(disabledModel.reconcileClock())
        disabledDraft.rebaseDefault(day: disabledModel.day, zone: disabledModel.timezone)
        check(disabledDraft.date == instant("2026-09-25T15:00:00Z"),
              "Automatic default must track a timezone change while disabled")
        disabledNow = instant("2026-09-25T19:00:00Z")
        disabledModel.returnToToday()
        disabledDraft.rebaseDefault(day: disabledModel.day, zone: disabledModel.timezone)
        check(disabledModel.day == "2026-09-26" && disabledDraft.date == instant("2026-09-26T15:00:00Z"),
              "Automatic default must track a clock-day change while disabled")
        disabledDraft.setEnabled(true, day: disabledModel.day, zone: disabledModel.timezone)
        check(disabledDraft.date == instant("2026-09-26T15:00:00Z") && disabledDraft.usesDefault,
              "Re-enabling must retain the rebased automatic default")

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
        let defaultDue = TodayModel.defaultDeadline(for: day, in: zone)!
        let addedWithDeadline = await model.add(title: "Project", kind: "project", minutes: 45,
                                                priority: 2, due: defaultDue)
        check(addedWithDeadline)
        let duePost = captured.last { $0.httpMethod == "POST" }!
        let duePayload = try JSONSerialization.jsonObject(with: duePost.httpBody!) as! [String: Any]
        check(duePayload["due_ts"] as? Double == defaultDue.timeIntervalSince1970,
              "New task must send the selected day's ending midnight")
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
        check(pinned.initialDeadline(fallback: instant("2026-09-24T20:00:00Z")) ==
              TodayModel.defaultDeadline(for: pinned.day, in: pinned.editingTimeZone),
              "Enabling a deadline in the editor must default to the task day's ending midnight")
        let edits = pinned.editFields(title: "Renamed", kind: "study", priority: 1, minutes: 90,
                                     due: nil, pin: Date(timeIntervalSince1970: pinned.pinned_start!))
        check(edits["timezone"] == nil && edits["day"] == nil, "Ordinary edits must preserve the task's timezone and date")
        check(edits["pinned_start"] as? Double == pinned.pinned_start)
        let existingDue = instant("2026-09-24T21:30:00Z")
        let taskWithDueData = try JSONSerialization.data(withJSONObject: [
            "id": "existing", "title": "Existing", "day": "2026-09-24", "timezone": "America/Los_Angeles",
            "kind": "study", "duration_minutes": 60, "priority": 2, "status": "active", "revision": 1,
            "due_ts": existingDue.timeIntervalSince1970
        ])
        let taskWithDue = try JSONDecoder().decode(TodayTask.self, from: taskWithDueData)
        check(taskWithDue.initialDeadline(fallback: instant("2026-09-25T20:00:00Z")) == existingDue,
              "Opening the editor must retain an existing deadline")
        let preserved = taskWithDue.editFields(title: taskWithDue.title, kind: taskWithDue.kind,
                                               priority: taskWithDue.priority, minutes: taskWithDue.duration_minutes,
                                               due: Date(timeIntervalSince1970: taskWithDue.due_ts!), pin: nil)
        check(preserved["due_ts"] as? Double == existingDue.timeIntervalSince1970,
              "Editing an existing deadline must preserve its exact instant")
        let diagnosticsModel = TodayModel(timezone: TimeZone(secondsFromGMT: 0), now: { instant("2026-09-24T08:00:00Z") })
        var debugRequested = false
        diagnosticsModel.transport = { request in
            debugRequested = (URLComponents(url: request.url!, resolvingAgainstBaseURL: true)!.queryItems ?? []).contains { $0.name == "debug" && $0.value == "true" }
            if request.httpMethod == "POST" {
                return (Data("{\"detail\":\"PRIVATE-MUTATION-FAILURE\"}".utf8), HTTPURLResponse(url: request.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
            }
            var object = try JSONSerialization.jsonObject(with: planData(day: query(request, "day"), zone: query(request, "timezone"))) as! [String: Any]
            object["trace_id"] = String(repeating: "a", count: 32)
            object["replay"] = ["private_title": "PRIVATE-TASK"]
            return (try JSONSerialization.data(withJSONObject: object), response(request))
        }
        await diagnosticsModel.refresh()
        check(!debugRequested && diagnosticsModel.diagnosticSnapshot().details == nil)
        diagnosticsModel.capturePlannerDetails = true
        await diagnosticsModel.refresh()
        check(debugRequested && diagnosticsModel.diagnosticSnapshot().details != nil)
        // Report a problem opens inline, keyed to this plan's trace, with details off.
        diagnosticsModel.reportProblem()
        check(diagnosticsModel.report.snapshot?.key == "today:" + String(repeating: "a", count: 32)
              && diagnosticsModel.report.snapshot?.hasDetails == true && !diagnosticsModel.report.includeDetails)
        check(!diagnosticsModel.report.preview.contains("PRIVATE-TASK"), "Planner details stay out until included")
        diagnosticsModel.report.includeDetails = true
        check(diagnosticsModel.report.preview.contains("PRIVATE-TASK"), "Included planner details are previewed")
        diagnosticsModel.capturePlannerDetails = false
        diagnosticsModel.clearPlannerDetails()
        check(!diagnosticsModel.report.isOpen, "Turning capture off closes a report holding planner details")
        check(diagnosticsModel.diagnosticSnapshot().details == nil)
        let saved = await diagnosticsModel.mutate("/assistant/today/tasks", method: "POST", body: [:])
        check(!saved && diagnosticsModel.diagnosticSnapshot().metadata["client_outcome"] as? String == "mutation_failed")
        check(diagnosticsModel.diagnosticSnapshot().metadata["client_error_code"] as? Int == 409)
        check(diagnosticsModel.diagnosticSnapshot().metadata["trace_id"] == nil)
        check(diagnosticsModel.diagnosticSnapshot().metadata["refresh_trace_id"] != nil)
        check(!String(describing: diagnosticsModel.diagnosticSnapshot().metadata).contains("PRIVATE"))
        diagnosticsModel.selectDate(instant("2026-09-25T08:00:00Z"))
        check(diagnosticsModel.diagnosticSnapshot().metadata["client_outcome"] as? String == "not_loaded")
        try await dailyWorkspaceChecks()
        try await attentionChecks()
        if ProcessInfo.processInfo.environment["WISP_TODAY_WINDOW_CHECK"] == "1" {
            try await windowChecks()
        }
        print("\(count) passed, 0 failed, 0 skipped")
    }

    @MainActor
    static func dailyWorkspaceChecks() async throws {
        let original = try planData(day: "2026-10-14", zone: "America/Los_Angeles", blocks: ["Saved calendar event"])
        var object = try JSONSerialization.jsonObject(with: original) as! [String: Any]
        object["workspace"] = [
            "attention": [["id": "r", "source": "reminders", "title": "Assignment", "reason": "Due today",
                           "quote": "", "due_ts": 2000.0, "priority": 1, "reference": "reminders:native-id",
                           "suggested_minutes": 25, "needs_review": false]],
            "attention_omitted": 2,
            "source_coverage": [["source": "notes", "ready": false, "detail": "Unavailable", "inspected": 0, "omitted": 3]],
            "behavior_detail": "Tentative supported evidence", "explanation": "Sources first",
            "proposed_plan": ["blocks": [["id": "busy", "title": "Likely busy", "start": 1000.0, "end": 2000.0,
                "kind": "behavior", "pattern_id": "routine", "warnings": ["Tentative"]]],
                "unscheduled": [["task_id": "task", "title": "Study", "reason": "No contiguous gap"]], "warnings": ["Source incomplete"]]
        ]
        let enriched = try JSONDecoder().decode(TodayPlan.self, from: JSONSerialization.data(withJSONObject: object))
        check(enriched.blocks.first?.title == "Saved calendar event", "Workspace must preserve original planner output")
        check(enriched.displayBlocks.first?.pattern_id == "routine" && enriched.displayBlocks.first?.kind == "behavior")
        check(enriched.displayWarnings == ["Source incomplete"] && enriched.displayUnscheduled.first?.title == "Study")
        check(enriched.workspace?.attention.first?.reference == "reminders:native-id" && enriched.workspace?.attention_omitted == 2)
        let fallback = try JSONDecoder().decode(TodayPlan.self, from: original)
        check(fallback.displayBlocks.first?.title == "Saved calendar event" && fallback.workspace == nil)

        let today = TodayModel(timezone: TimeZone(identifier: "America/Los_Angeles"), now: { instant("2026-10-14T15:00:00Z") })
        func settingsData() throws -> Data {
            try JSONSerialization.data(withJSONObject: ["settings": ["enabled": false, "revision": 3,
                "recipient": "me@example.test", "timezone": "America/Los_Angeles", "minute": 495, "start_day": "2026-10-15"],
                "own_addresses": ["me@example.test"], "identity_ready": true, "identity_detail": "Synthetic account",
                "consent_current": false, "delivery_allowed": true] as [String: Any])
        }
        let email = DailyEmailModel()
        var pending: CheckedContinuation<(Data, URLResponse), Error>?
        var requests: [String] = []
        var hold = true
        today.transport = { request in
            requests.append(request.url!.path)
            if request.url!.path.hasSuffix("/enable") {
                let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                check(body["confirmed"] as? Bool == true && body["token"] as? String == String(repeating: "x", count: 32))
                return (Data("{\"detail\":\"Review expired\"}".utf8), HTTPURLResponse(url: request.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
            }
            if request.url!.path.hasSuffix("/preview") {
                return (try JSONSerialization.data(withJSONObject: ["token": String(repeating: "x", count: 32),
                    "subject": "Example plan", "body": "Synthetic sources", "explanation": "Future plans change"]), response(request))
            }
            if hold {
                return try await withCheckedThrowingContinuation { continuation in pending = continuation }
            }
            return (try settingsData(), response(request))
        }
        let loading = Task { await email.load(today) }
        while pending == nil { await Task.yield() }
        check(email.busy && email.status == nil)
        await email.review(today)
        check(requests.count == 1 && email.preview == nil, "No mutation may race a pending settings read")
        let request = URLRequest(url: URL(string: "http://127.0.0.1:1/assistant/today/workspace/email")!)
        hold = false
        pending!.resume(returning: (try settingsData(), response(request)))
        await loading.value
        check(!email.busy && email.recipient == "me@example.test" && email.hour == 8 && email.minute == 15)
        await email.review(today)
        check(email.preview?.body == "Synthetic sources")
        await email.enable(today)
        check(email.error == "Review expired", "Successful settings refresh must preserve an enable error")
    }

    @MainActor
    static func attentionChecks() async throws {
        let now = instant("2026-10-16T19:00:00Z")
        let t = now.timeIntervalSince1970
        var mode = "shadow"
        var requests: [URLRequest] = []
        func status() throws -> Data {
            let row: (String, String, Double) -> [String: Any] = { id, state, at in
                ["source_id": id, "state": state, "title": "Mom: Quad at 6PM", "due_ts": t + 3600,
                 "event_ts": t + 5400, "decided_at": at, "when": "today 6:00 PM", "quote": "meet me in the Quad at 6PM",
                 "undoable": id == "made"]
            }
            return try JSONSerialization.data(withJSONObject: [
                "settings": ["mode": mode, "daily_cap": 3, "quiet_start": 22, "quiet_end": 8,
                             "lead_minutes": 30, "default_hour": 9],
                "modes": ["off", "shadow", "live"], "created_today": 1,
                "recent": [row("old", "created", t - 8 * 86400), row("known", "already_on_file", t - 60),
                           row("made", "created", t - 120), row("preview", "shadow", t - 30)]])
        }
        let model = AttentionModel(now: { now }) { request in
            requests.append(request)
            if request.httpMethod == "PUT" {
                let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                mode = body["mode"] as! String
            }
            return (try status(), response(request))
        }
        await model.refresh()
        check(model.error.isEmpty && !model.live)
        check(model.items.map(\.id) == ["preview", "made"],
              "Caught for you shows the last week's meaningful decisions, newest first, and hides already_on_file")
        check(model.items.first?.label == "Would add · preview")
        await model.undo(model.items.first!)
        check(requests.count == 1, "Undo is only offered for a reminder Wisp created")
        await model.undo(model.items[1])
        let undo = requests[1]
        check(undo.httpMethod == "POST" && undo.url!.path == "/assistant/attention/undo")
        check((try JSONSerialization.jsonObject(with: undo.httpBody!) as! [String: Any])["source_id"] as? String == "made")
        await model.setLive(true)
        let put = requests.first { $0.httpMethod == "PUT" }!
        check(put.url!.path == "/assistant/attention/settings" && model.live)
        await model.setLive(false)
        check(!model.live && mode == "shadow", "Turning it off returns to preview, never to a hidden mode")
        // A refusal from Undo stays on screen after the reload that follows it.
        var stale: CheckedContinuation<(Data, URLResponse), Error>?
        var holdNextGet = false
        let refusing = AttentionModel(now: { now }) { request in
            if request.httpMethod == "POST" {
                return (Data("{\"detail\":\"That reminder was changed after Wisp created it; nothing was removed.\"}".utf8),
                        HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
            }
            if holdNextGet {
                holdNextGet = false
                return try await withCheckedThrowingContinuation { stale = $0 }
            }
            return (try status(), response(request))
        }
        await refusing.refresh()
        await refusing.undo(refusing.items[1])
        check(refusing.error.hasPrefix("That reminder was changed"), "Undo's refusal must survive the reload")
        // An older poll that resolves after a newer refresh must not overwrite it.
        holdNextGet = true
        let poll = Task { await refusing.refresh() }
        while stale == nil { await Task.yield() }
        mode = "live"
        await refusing.refresh()
        check(refusing.live)
        mode = "shadow"
        stale!.resume(returning: (try status(), response(URLRequest(url: URL(string: "http://127.0.0.1:1/assistant/attention")!))))
        await poll.value
        check(refusing.live, "A stale poll must not undo a newer status")
        let failing = AttentionModel(now: { now }) { request in
            (Data("{\"detail\":\"The reminder was not found.\"}".utf8),
             HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
        }
        await failing.refresh()
        check(failing.error == "The reminder was not found." && failing.status == nil)
    }
}
