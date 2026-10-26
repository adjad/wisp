import Foundation
import AppKit

// Inert dependency: all requests are captured by the injected transport below.
enum WispClient { static let baseURL = URL(string: "http://127.0.0.1:1")! }

@main
struct TodayPlanChecks {
    @MainActor static var count = 0
    @MainActor static func check(_ condition: Bool, _ message: String = "Today contract failed", line: Int = #line) {
        precondition(condition, message + " (TodayPlanChecks.swift:\(line))")
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
        let offscreen = ProcessInfo.processInfo.environment["WISP_QA_OFFSCREEN"] == "1"
        let la = TimeZone(identifier: "America/Los_Angeles")!
        var zone = la
        var now = instant("2026-09-25T06:59:00Z")
        let model = TodayModel(now: { now }, timeZoneProvider: { zone })
        defer { model.stopBackgroundRefresh() }
        var presentations: [TodayPresentation] = []
        var queries: [(String, String)] = []
        var supplementalPaths: Set<String> = []
        var holdYesterday = false
        var pending: CheckedContinuation<(Data, URLResponse), Error>?
        model.transport = { request in
            switch request.url!.path {
            case "/assistant/upcoming":
                supplementalPaths.insert(request.url!.path)
                return (Data(#"{"commitments":[]}"#.utf8), response(request))
            case "/assistant/daily_summary/latest":
                supplementalPaths.insert(request.url!.path)
                return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
            case "/assistant/today": break
            default: preconditionFailure("Unexpected window fixture endpoint: \(request.url!.path)")
            }
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
            presentations.last?.blockTitles == ["Yesterday's event"] &&
            supplementalPaths == ["/assistant/upcoming", "/assistant/daily_summary/latest"]
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
        let reopened = TodayWindow.show(present: !offscreen)
        check(reopened === window && reopened.isVisible == !offscreen, "Today must reuse its window on reopen")
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
        let nextReopen = TodayWindow.show(present: !offscreen)
        check(nextReopen === window)
        await waitFor("Reopening after another local day must advance the displayed date") {
            presentations.last?.day == "2026-09-30" && presentations.last?.pickerDay == "2026-09-30" &&
            presentations.last?.blockTitles.isEmpty == true && queries.count > queriesBeforeNextReopen
        }
        nextReopen.close()
    }
    /// The composed summary the service kept: parsed, chosen against the live
    /// one, read on a relaunch, forgotten on a 404 and kept through other failures.
    @MainActor
    static func storedBriefChecks() async {
        let now = 1_800_043_200.0, hour = 3600.0  // noon, 2027-01-15, Los Angeles
        let kept = StoredBrief.parse(["text": "**📧 Inbox — 3 emails**\n- Ann: Lunch", "composed_at": now - hour, "part_of_day": "morning"],
                                     day: "2027-01-15")
        check(kept?.composedAt == Date(timeIntervalSince1970: now - hour) && StoredBrief.parse(["text": "  "], day: "d") == nil
              && StoredBrief.parse(["text": "x", "composed_at": "soon"], day: "d") == nil, "kept summaries parse, junk doesn't")
        let sameDay: (Date) -> Bool = { $0.timeIntervalSince1970 > now - 12 * hour }
        check(StoredBrief.newest(live: nil, stored: kept, sameDay: sameDay)?.text == kept?.text
              && StoredBrief.newest(live: (text: "live", at: Date(timeIntervalSince1970: now)), stored: kept, sameDay: sameDay)?.text == "live"
              && StoredBrief.newest(live: (text: "old", at: Date(timeIntervalSince1970: now - 2 * hour)), stored: kept, sameDay: sameDay)?.text == kept?.text
              && StoredBrief.newest(live: (text: "yesterday", at: Date(timeIntervalSince1970: now - 20 * hour)), stored: nil, sameDay: sameDay) == nil,
              "the newest of today's live and kept summaries")
        // "Ask about this" continues the live brief's chat only when that brief
        // is the one on screen: a newer kept brief, or yesterday's live one,
        // must not be continued in place of what is shown.
        let liveNow = (text: "live", at: Date(timeIntervalSince1970: now))
        let yesterday = (text: "yesterday", at: Date(timeIntervalSince1970: now - 20 * hour))
        let stale = (text: "old", at: Date(timeIntervalSince1970: now - 2 * hour))
        check(StoredBrief.shownIsLive(shown: StoredBrief.newest(live: liveNow, stored: kept, sameDay: sameDay), live: liveNow)
              && !StoredBrief.shownIsLive(shown: StoredBrief.newest(live: stale, stored: kept, sameDay: sameDay), live: stale)
              && !StoredBrief.shownIsLive(shown: StoredBrief.newest(live: yesterday, stored: kept, sameDay: sameDay), live: yesterday)
              && !StoredBrief.shownIsLive(shown: nil, live: liveNow)
              && !StoredBrief.shownIsLive(shown: (text: "live", at: liveNow.at.addingTimeInterval(1)), live: liveNow),
              "Ask about this follows the brief on screen")
        var status = 200
        var asked: [String] = []
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let model = TodayModel(now: { Date(timeIntervalSince1970: now) }, timeZoneProvider: { zone }) { request in
            asked.append(request.url?.absoluteString ?? "")
            let body = status == 200 ? #"{"text":"**🧭 Keeping in mind**\n- PA 2","composed_at":1800039600,"part_of_day":"morning"}"#
                : #"{"detail":"no composed summary for that day"}"#
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
        check(!model.storedBriefChecked, "unknown until asked")
        await model.refreshStoredBrief()
        check(model.storedBriefChecked && model.storedBrief?.text == "**🧭 Keeping in mind**\n- PA 2" && model.storedBrief?.day == "2027-01-15"
              && asked.last?.contains("/assistant/daily_summary/latest?day=2027-01-15") == true,
              "the kept summary is read for today's local date: \(asked)")
        status = 500
        await model.refreshStoredBrief()
        check(model.storedBrief != nil, "a failed read keeps the summary read before")
        status = 404
        await model.refreshStoredBrief()
        check(model.storedBrief == nil, "none composed today: forgotten")
    }

    @MainActor
    static func refreshLifecycleChecks() async throws {
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        var now = instant("2026-10-22T19:00:00Z")
        var currentZone = zone
        let model = TodayModel(now: { now }, timeZoneProvider: { currentZone })
        let good: (URLRequest) async throws -> (Data, URLResponse) = { request in
            (try planData(day: query(request, "day"), zone: query(request, "timezone"), blocks: ["Retained event"]), response(request))
        }
        model.transport = good
        await model.refresh()
        for failure: Error in [CancellationError(), URLError(.cancelled)] {
            model.transport = { _ in throw failure }
            await model.refresh()
            check(model.plan?.blocks.first?.title == "Retained event" && model.error.isEmpty,
                  "Navigation cancellation must preserve Brief and Day content")
            check(model.diagnosticSnapshot().metadata["client_outcome"] as? String == "completed",
                  "Cancellation must not replace a successful diagnostic with refresh_failed")
        }
        let empty = TodayModel(timezone: zone)
        empty.transport = { _ in throw URLError(.cancelled) }
        await empty.refresh()
        check(empty.plan == nil && empty.error.isEmpty, "Initial cancellation must not publish a cancelled page")

        var reads = 0
        var pending: CheckedContinuation<(Data, URLResponse), Error>?
        model.transport = { request in
            reads += 1
            return try await withCheckedThrowingContinuation { pending = $0 }
        }
        let first = Task { await model.refresh() }
        await waitFor("First read must start") { pending != nil }
        await model.refresh()
        check(reads == 1, "Concurrent same-day refreshes must share one request")
        let held = pending!
        pending = nil
        first.cancel()
        let request = URLRequest(url: URL(string: "http://127.0.0.1:1/assistant/today")!)
        held.resume(returning: (try planData(day: model.day, zone: zone.identifier, blocks: ["Cancelled response"]), response(request)))
        await first.value
        check(model.plan?.blocks.first?.title == "Retained event" && model.error.isEmpty,
              "A transport completing after cancellation cannot publish its response")

        // Mutation's post-write read must supersede a pre-write background read.
        model.transport = { request in
            if request.httpMethod == "GET" {
                return try await withCheckedThrowingContinuation { pending = $0 }
            }
            return (Data("{}".utf8), response(request))
        }
        let stale = Task { await model.refresh() }
        await waitFor("Pre-mutation read must be held") { pending != nil }
        let preMutation = pending!
        pending = nil
        let mutation = Task { await model.mutate("/assistant/today/tasks", method: "POST", body: [:]) }
        await waitFor("Mutation must start its own follow-up read") { pending != nil }
        check(model.busy, "Mutation controls stay disabled during follow-up")
        preMutation.resume(throwing: URLError(.timedOut))
        await stale.value
        await model.refresh()
        check(model.error.isEmpty, "An old read cannot clear or fail the mutation's new read")
        pending!.resume(returning: (try planData(day: model.day, zone: zone.identifier, blocks: ["After edit"]), response(request)))
        pending = nil
        check(await mutation.value && !model.busy && model.plan?.blocks.first?.title == "After edit")
        var retryNaps: [Duration] = []
        var retryWakes: [CheckedContinuation<Void, Error>] = []
        model.retrySleep = { nap in
            retryNaps.append(nap)
            try await withCheckedThrowingContinuation { retryWakes.append($0) }
        }
        model.transport = { _ in throw URLError(.timedOut) }
        await model.refresh()
        check(model.plan?.blocks.first?.title == "After edit" && model.error.isEmpty,
              "A timeout keeps the last good plan on screen and shows no error")
        check(model.transientFailures == 1 && model.outageNotice == nil)
        check(model.staleNotice == nil && !model.planIsStale, "a short outage keeps the plan without a notice")
        now = now.addingTimeInterval(TodayModel.staleAfter)
        check(model.staleNotice?.hasPrefix("Wisp isn't answering right now. Showing your day as of ") == true && model.planIsStale,
              "a long outage says how old the day on screen is: \(model.staleNotice ?? "nil")")
        await waitFor("A transient failure schedules a quiet retry") { retryWakes.count == 1 }
        check(retryNaps == [.seconds(1)], "The first retry comes after a second")
        model.transport = { req in
            (try planData(day: model.day, zone: zone.identifier, blocks: ["Recovered"]), response(req))
        }
        retryWakes.removeFirst().resume()
        await waitFor("The retry reloads the day by itself") { model.plan?.blocks.first?.title == "Recovered" }
        check(model.transientFailures == 0 && model.error.isEmpty && retryWakes.isEmpty)
        check(model.staleNotice == nil && !model.planIsStale, "a good read clears the stale notice")
        model.transport = { _ in throw URLError(.cannotConnectToHost, userInfo: [NSLocalizedDescriptionKey: "Wisp's service isn't running yet."]) }
        await model.refresh()
        check(model.plan?.blocks.first?.title == "Recovered" && model.error.isEmpty,
              "The identity check's 'not running yet' never reaches the Brief")
        await waitFor("A second failure retries again") { retryWakes.count == 1 }
        model.transport = { req in
            (Data("{\"detail\":\"Today is unavailable\"}".utf8),
             HTTPURLResponse(url: req.url!, statusCode: 503, httpVersion: nil, headerFields: nil)!)
        }
        retryWakes.removeFirst().resume()
        await waitFor("A 503 is also retried quietly") { retryWakes.count == 1 && model.transientFailures == 2 }
        check(retryNaps.last == .seconds(2) && model.error.isEmpty, "Retries back off")
        check(TodayModel.retryDelay(after: 3) == .seconds(4) && TodayModel.retryDelay(after: 9) == .seconds(15))
        model.transport = { req in
            (Data("{\"detail\":\"Refresh before editing\"}".utf8),
             HTTPURLResponse(url: req.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!)
        }
        retryWakes.removeFirst().resume()
        await waitFor("A refusal is a real error") { model.error == "Refresh before editing" }
        check(model.plan == nil && model.transientFailures == 0 && retryWakes.isEmpty,
              "Real failures still clear unsafe planner actions, once, without retrying")
        model.transport = { req in (Data("{\"day\": 3}".utf8), response(req)) }
        await model.refresh()
        check(model.error == "Wisp couldn't read today's plan from its service.",
              "An unreadable reply is said plainly")
        // Nothing on screen and the service still not answering: the loading row says so.
        let starting = TodayModel(now: { now }, timeZoneProvider: { currentZone })
        var startingWakes: [CheckedContinuation<Void, Error>] = []
        starting.retrySleep = { _ in try await withCheckedThrowingContinuation { startingWakes.append($0) } }
        starting.transport = { _ in throw URLError(.cannotConnectToHost) }
        await starting.refresh()
        for attempt in 2...4 {
            await waitFor("Each failed start retries") { startingWakes.count == 1 }
            startingWakes.removeFirst().resume()
            await waitFor("The retry ran") { starting.transientFailures == attempt }
        }
        check(starting.plan == nil && starting.error.isEmpty
              && starting.outageNotice == "Wisp is taking a while to start. Still trying…")
        await waitFor("It keeps trying") { startingWakes.count == 1 }
        starting.transport = good
        startingWakes.removeFirst().resume()
        await waitFor("The day loads once the service answers") { starting.plan != nil }
        check(starting.outageNotice == nil && starting.transientFailures == 0)
        model.retrySleep = { try await Task.sleep(for: $0) }

        // Drive the model's app-owned loop without a view or wall-clock waits.
        var todayReads = 0, upcomingReads = 0, briefReads = 0
        var sleeps: [CheckedContinuation<Void, Error>] = []
        var cycles = 0
        model.transport = { request in
            switch request.url!.path {
            case "/assistant/today":
                todayReads += 1
                return try await good(request)
            case "/assistant/upcoming":
                upcomingReads += 1
                return (Data(#"{"commitments":[]}"#.utf8), response(request))
            default:
                briefReads += 1
                return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
            }
        }
        var naps: [Duration] = []
        model.startBackgroundRefresh(sleep: { nap in
            naps.append(nap)
            try await withCheckedThrowingContinuation { sleeps.append($0) }
        }, afterRefresh: { cycles += 1 })
        await waitFor("Background refresh must load without a mounted tab") { cycles == 1 && sleeps.count == 1 }
        check(todayReads == 1 && upcomingReads == 1 && briefReads == 1)
        check(naps == [.seconds(30)], "A loaded Brief waits the full interval")
        model.startBackgroundRefresh()
        await Task.yield()
        check(todayReads == 1 && sleeps.count == 1, "Reopening must not restart the loop or refetch")
        for tick in 1...4 {
            now = now.addingTimeInterval(30)
            sleeps.removeFirst().resume()
            await waitFor("Each background tick must complete") { cycles == tick + 1 && sleeps.count == 1 }
            check(todayReads == tick + 1)
            check(upcomingReads == (tick == 4 ? 2 : 1) && briefReads == (tick == 4 ? 2 : 1),
                  "Supplementary Brief reads stay on the two-minute cadence")
        }
        now = instant("2026-10-24T19:00:00Z")
        sleeps.removeFirst().resume()
        await waitFor("Background tick must reconcile a multi-day wake") { cycles == 6 && sleeps.count == 1 }
        check(model.plan?.day == "2026-10-24" && upcomingReads == 3 && briefReads == 3)
        model.selectDate(instant("2026-10-20T19:00:00Z"))
        currentZone = TimeZone(identifier: "Asia/Tokyo")!
        sleeps.removeFirst().resume()
        await waitFor("Background refresh must follow a zone change without a mounted tab") { cycles == 7 && sleeps.count == 1 }
        check(model.plan?.timezone == currentZone.identifier && model.day == "2026-10-20" && !model.followsToday,
              "Background refresh must preserve a manually selected civil day")
        check(upcomingReads == 4 && briefReads == 4, "Zone changes invalidate the slower Brief cadence")
        model.stopBackgroundRefresh()
        sleeps.removeFirst().resume()
        await Task.yield()
        check(todayReads == 7, "Stopping the app refresh must prevent another read")
    }

    @MainActor
    static func storedBriefRolloverChecks() async throws {
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        for oldStatus in [200, 404] {
            var now = instant("2026-10-23T06:59:00Z")
            let model = TodayModel(timezone: zone, now: { now })
            var pending: CheckedContinuation<(Data, URLResponse), Error>?
            var oldRequest: URLRequest?
            model.transport = { request in
                if query(request, "day") == "2026-10-22" {
                    oldRequest = request
                    return try await withCheckedThrowingContinuation { pending = $0 }
                }
                return (try JSONSerialization.data(withJSONObject: ["text": "Today's brief", "composed_at": now.timeIntervalSince1970]), response(request))
            }
            let old = Task { await model.refreshStoredBrief() }
            await waitFor("Yesterday's summary read must be held") { pending != nil }
            now = instant("2026-10-23T07:01:00Z")
            await model.refreshStoredBrief()
            let body = try JSONSerialization.data(withJSONObject: ["text": "Yesterday's brief", "composed_at": now.addingTimeInterval(-120).timeIntervalSince1970])
            pending!.resume(returning: (body, HTTPURLResponse(url: oldRequest!.url!, statusCode: oldStatus, httpVersion: nil, headerFields: nil)!))
            await old.value
            check(model.storedBrief?.text == "Today's brief" && model.storedBrief?.day == "2026-10-23",
                  "A delayed old-day success or 404 cannot overwrite today's summary")
        }
    }

    /// The live /assistant/today shape (2026-10-24, proactive engine on), every
    /// value replaced with a synthetic one.
    static let liveShape = #"""
    {"day":"2026-10-24","timezone":"America/Los_Angeles","generated_at":1792905200.27,
     "preferences":{"start_minute":540,"end_minute":1080,"not_before":null},
     "sources":{"calendar":{"ready":true,"last_sync":1792905168.6,"reason":"Up to date"},
                "reminders":{"ready":true,"last_sync":1792905168.7,"reason":"Up to date"}},
     "provisional":false,"warnings":[],
     "tasks":[{"kind":"task","priority":2,"status":"active","title":"Fixture task","day":"2026-10-24",
               "timezone":"America/Los_Angeles","duration_minutes":30,"due_ts":1792950000.0,"id":"task-1",
               "revision":4,"pinned_start":null}],
     "blocks":[{"id":"b1","title":"Fixture block","start":1792940000.0,"end":1792941800.0,"kind":"task",
                "task_id":"task-1","warnings":["Estimated"]}],
     "deadlines":[],
     "all_day":[{"id":"a1","title":"Fixture holiday","start":1792911600.0,"end":1792998000.0,"kind":"calendar",
                 "task_id":null,"warnings":[]}],
     "unscheduled":[],"revision":4,"trace_id":"0123456789abcdef0123456789abcdef",
     "workspace":{
       "day":"2026-10-24","timezone":"America/Los_Angeles","generated_at":1792905200.3,
       "attention":[{"id":"att-1","source":"email","title":"Fixture mail","reason":"Due soon","quote":"Fixture quote",
                     "due_ts":1792950000.0,"priority":1,"reference":"ref-1","suggested_minutes":15,"needs_review":false}],
       "attention_omitted":0,
       "source_coverage":[{"source":"email","ready":true,"detail":"Read 50","inspected":50,"omitted":0}],
       "behavior_detail":"","behavior_context":{"any":"new field"},"explanation":"Fixture explanation",
       "suggested_count":0,"tentative_count":0,
       "proposed_plan":{"day":"2026-10-24","timezone":"America/Los_Angeles","blocks":[
           {"id":"b1","title":"Fixture block","start":1792940000.0,"end":1792941800.0,"kind":"task",
            "task_id":"task-1","warnings":[]}],"unscheduled":[],"warnings":[]},
       "mail_overview":{"text":"Two fixture emails.","source":"model"},
       "texts_overview":{"text":"One fixture text.","source":"summary"},
       "mail_summary":{"text":"Fixture inbox summary.","source":"summary"},
       "needs_you":{"items":[{"id":"n1","kind":"alert","text":"Fixture security alert","due_ts":null,"all_day":false},
                             {"id":"n2","kind":"overdue","text":"Fixture overdue","due_ts":null,"all_day":false},
                             {"id":"n3","kind":"plan","text":"Fixture plan","due_ts":null,"all_day":false}],"more":0},
       "proactive":{"state":"ready","as_of":1792905000.0,"progress":{"done":0,"total":0},
         "accounts":["owner@example.com"],"owner":{"id":"owner@example.com","label":"You"},"summary":"Fixture summary",
         "cards":[{"id":"card:1","meta":"Mail","title":"Fixture alert card","body":"Fixture body","why":"Fixture why",
                   "account":"owner@example.com","critical":true,"todo_id":"todo:1","due_ts":null,
                   "sources":[{"kind":"mail","ref":"m1","who":"Fixture Sender","quote":"Fixture quote"}],
                   "action":{"method":"read","button":"Open","draft":"","recipient":"","event":null,
                             "envelope":{"kind":"none"},"to":[],"cc":[],"subject":"","reply_all":false,
                             "reply_ref":{"message_id":"m1","account":"owner@example.com","account_id":"acct"},
                             "open_url":"message://fixture","open_label":"Open in Mail"}},
                  {"id":"card:2","meta":"Added","title":"Fixture auto-added event","body":"Fixture body","why":"",
                   "account":"owner@example.com","todo_id":null,"due_ts":1792950000.0,"sources":[],
                   "action":{"method":"undo_add","button":"Undo","undo":{"kind":"calendar","id":"evt-1"}}}],
         "todos":[{"id":"todo:1","title":"Fixture to-do","source":"mail","account":"owner@example.com","who":"Fixture",
                   "quote":"Fixture","due_ts":1792950000.0,"due_label":"Today","kind":"reply","done":false,
                   "card_id":null,"timed":true,"added_to_day":false}],
         "threads":[{"id":"t1","source":"mail","account":"owner@example.com","who":"Fixture","title":"Fixture thread",
                     "summary":"Fixture summary","user_wrote":false,"needs_reply":true,"replied":false,
                     "received_ts":1792900000.0,"reply_ref":"m1","attention":"high","attention_reason":"Fixture"}],
         "lede":"Fixture lede",
         "stats":{"read":10,"found":3,"drafted":1,"filtered":2,"read_breakdown":{"mail":5,"texts":4,"notes":1},
                  "as_of":1792905000.0},
         "debug":{"waiting":[{"who":"Fixture","waiting":false,"reason":"read, under a day"}]}}}}
    """#

    /// Decoding stays whole when the service adds, drops or reshapes a field.
    static func tolerantDecodeChecks() throws {
        func decode(_ text: String) throws -> TodayPlan { try JSONDecoder().decode(TodayPlan.self, from: Data(text.utf8)) }
        func edited(_ edit: (inout [String: Any]) -> Void) throws -> TodayPlan {
            var object = try JSONSerialization.jsonObject(with: Data(liveShape.utf8)) as! [String: Any]
            edit(&object)
            return try JSONDecoder().decode(TodayPlan.self, from: JSONSerialization.data(withJSONObject: object))
        }
        let live = try decode(liveShape)
        precondition(live.workspace?.proactive?.cards.count == 2, "The live shape decodes with every card")
        precondition(live.workspace?.needs_you?.items.map(\.kind) == ["alert", "overdue", "plan"])
        precondition(live.workspace?.mail_overview?.text == "Two fixture emails." && live.workspace_error == nil)
        precondition(live.tasks.count == 1 && live.displayBlocks.count == 1 && live.all_day.count == 1)

        // A needs-you item, a block and a task this build can't read are dropped, not the plan.
        let rows = try edited { plan in
            var workspace = plan["workspace"] as! [String: Any]
            var needs = workspace["needs_you"] as! [String: Any]
            needs["items"] = (needs["items"] as! [[String: Any]]) + [["id": "n4", "kind": "brand_new"]]
            workspace["needs_you"] = needs
            workspace["mail_overview"] = "a string where an object was"
            workspace["attention"] = (workspace["attention"] as! [[String: Any]]) + [["id": "broken"]]
            plan["workspace"] = workspace
            plan["blocks"] = (plan["blocks"] as! [[String: Any]]) + [["id": "no-start", "title": "x", "kind": "task"]]
            plan["tasks"] = (plan["tasks"] as! [[String: Any]]) + [["id": "half"]]
            plan["brand_new_top_level"] = ["anything": true]
        }
        precondition(rows.workspace?.needs_you?.items.count == 3 && rows.workspace?.mail_overview == nil
                     && rows.workspace?.attention.count == 1 && rows.blocks.count == 1 && rows.tasks.count == 1
                     && rows.workspace?.proactive?.cards.count == 2 && rows.workspace_error == nil,
                     "Unreadable rows are skipped and the rest of the Brief stays")

        // Missing optional parts: a minimal service reply still decodes.
        let minimal = try decode(#"{"day":"2026-10-24","timezone":"America/Los_Angeles"}"#)
        precondition(minimal.blocks.isEmpty && minimal.workspace == nil && minimal.revision == 0
                     && minimal.preferences.start_minute == 540 && minimal.workspace_error == nil)

        // A workspace whose plan can't be read is left out with a note; the saved plan shows.
        let reshaped = try edited { plan in
            var workspace = plan["workspace"] as! [String: Any]
            workspace["proposed_plan"] = ["blocks": "not a list"]
            plan["workspace"] = workspace
        }
        precondition(reshaped.workspace == nil && reshaped.workspace_error == TodayPlan.unreadableWorkspace
                     && reshaped.displayBlocks.count == 1, "An unreadable workspace never blanks the Day")
        // Only the day and zone it is for are required.
        precondition((try? decode(#"{"timezone":"America/Los_Angeles"}"#)) == nil)
    }

    @MainActor
    /// What the Brief and Day know about sources Wisp couldn't read.
    static func sourceAccessChecks() throws {
        let payload: [String: Any] = [
            "day": "2026-10-22", "timezone": "UTC",
            "sources": ["calendar": ["ready": false, "last_sync": 1000, "reason": "Calendar access is off for Wisp.",
                                     "state": "unavailable", "blocked": "calendar_access"],
                        "reminders": ["ready": true, "last_sync": 1000, "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1080, "not_before": NSNull(), "plan_all_day": true],
            "workspace": ["attention": [], "attention_omitted": 0,
                          "source_coverage": [["source": "email", "ready": false, "detail": "", "inspected": 0, "omitted": 0,
                                               "state": "syncing"],
                                              ["source": "messages", "ready": false, "detail": "", "inspected": 0, "omitted": 0,
                                               "blocked": "full_disk_access"]],
                          "proposed_plan": ["blocks": [], "unscheduled": [], "warnings": []]],
        ]
        let plan = try JSONDecoder().decode(TodayPlan.self, from: try JSONSerialization.data(withJSONObject: payload))
        check(plan.access("calendar")?.blocked == "calendar_access" && plan.access("calendar")?.state == .unavailable
              && plan.access("calendar")?.detail == "Calendar access is off for Wisp.", "a blocked calendar decodes with its reason")
        check(plan.access("reminders")?.readable == true, "ready reminders")
        check(plan.access("email")?.state == .syncing && plan.access("messages")?.fix == BriefDetail.fullDiskAccess,
              "coverage carries the rest")
        check(plan.access("notes") == nil, "a source the service didn't mention is unknown, not unread")
        check(plan.unreadSources == ["calendar", "email", "messages"], "unread sources in reading order: \(plan.unreadSources)")
        check(plan.preferences.plan_all_day == true, "the plan-anyway preference decodes")
        // An older service: no state, no blocked; a missing preference flag is false.
        let older = try JSONDecoder().decode(TodayPlan.self, from: try planData(day: "2026-10-22", zone: "UTC"))
        check(older.access("calendar")?.readable == true && older.preferences.plan_all_day == nil, "an older service still decodes")
        // No workspace (assess failed, or it didn't decode): mail and texts were
        // never read, so they are unread, never an all-clear.
        check(older.workspace == nil && older.unreadSources == ["email", "messages"],
              "a missing workspace leaves mail and texts unread: \(older.unreadSources)")
        check(older.access("email")?.state == .unavailable && older.access("notes") == nil,
              "mail without a workspace is unavailable; notes stay unknown")
        var broken = payload
        broken["workspace"] = ["attention": "not a list"]
        let undecodable = try JSONDecoder().decode(TodayPlan.self, from: try JSONSerialization.data(withJSONObject: broken))
        check(undecodable.workspace == nil && undecodable.unreadSources == ["calendar", "email", "messages"],
              "an undecodable workspace leaves mail and texts unread: \(undecodable.unreadSources)")
        let lede = DayTimeline.lede(weekday: "Thursday", isToday: true, load: nil, dueToday: [], overdue: [], next: nil,
                                   unread: older.unreadSources)
        check(!lede.contains("Nothing needs you"), "no all-clear without a workspace: \(lede)")
    }

    static func main() async throws {
        try tolerantDecodeChecks()
        try sourceAccessChecks()
        try evidenceLabelChecks()
        await storedBriefChecks()
        try await refreshLifecycleChecks()
        try await storedBriefRolloverChecks()
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let model = TodayModel(timezone: zone)
        model.clockLocale = WispClock.fixedLocale
        check(model.clock(instant("2026-09-24T07:00:00Z").timeIntervalSince1970) == "12:00 AM")
        check(model.clock(instant("2026-09-24T19:05:00Z").timeIntervalSince1970) == "12:05 PM")
        check(model.clock(instant("2026-09-24T22:05:00Z").timeIntervalSince1970) == "3:05 PM")
        check(TodayModel.workingHourLabel(0, locale: WispClock.fixedLocale) == "12:00 AM")
        check(TodayModel.workingHourLabel(12, locale: WispClock.fixedLocale) == "12:00 PM")
        check(TodayModel.workingHourLabel(18, locale: WispClock.fixedLocale) == "6:00 PM")
        check(TodayModel.workingHourLabel(24, locale: WispClock.fixedLocale) == "12:00 AM (next day)")
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
        check(!model.captureEvents, "Legacy live settings never imply Calendar capture authority")
        var captureEvents = false
        var pairRequests: [URLRequest] = []
        var pairRow: [String: Any] = ["source_id": "preview:mail:synthetic", "state": "shadow",
            "title": "STAT 5 Exam 1", "due_ts": t + 86400, "decided_at": t,
            "when": "2026-10-21 · all-day Calendar; reminder at 09:00",
            "paired": true, "captureable": true, "fingerprint": String(repeating: "a", count: 64)]
        let pairModel = AttentionModel(now: { now }) { request in
            pairRequests.append(request)
            if request.httpMethod == "PUT" {
                let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                check(body["mode"] == nil, "Calendar opt-in must preserve the legacy reminders mode")
                captureEvents = body["capture_events"] as! Bool
            }
            let data = try JSONSerialization.data(withJSONObject: [
                "settings": ["mode": "live", "daily_cap": 3, "capture_events": captureEvents],
                "created_today": 0, "recent": [pairRow]])
            return (data, response(request))
        }
        await pairModel.refresh()
        check(pairModel.live && !pairModel.captureEvents)
        await pairModel.setCaptureEvents(true)
        check(pairModel.live && pairModel.captureEvents)
        await pairModel.capture(pairModel.items[0])
        let captureRequest = pairRequests.first { $0.url!.path.hasSuffix("/capture") }!
        let captureBody = try JSONSerialization.jsonObject(with: captureRequest.httpBody!) as! [String: Any]
        check(captureRequest.httpMethod == "POST" && captureRequest.timeoutInterval == 120)
        check(Set(captureBody.keys) == ["source_id", "fingerprint"],
              "Reviewed Add sends exact source binding; neither native payload nor approval comes from JSON")
        check(captureBody["source_id"] as? String == "preview:mail:synthetic")
        pairRow["state"] = "partial"
        pairRow["captureable"] = false
        pairRow["effects"] = ["calendar": ["status": "succeeded"], "reminder": ["status": "failed"]]
        await pairModel.refresh()
        check(pairModel.items[0].label.hasPrefix("Partially added"))
        check(pairModel.items[0].label.contains("Calendar: added") && pairModel.items[0].label.contains("Reminder: couldn't add"))
        pairRow["state"] = "unknown"
        pairRow["effects"] = ["calendar": ["status": "unknown"], "reminder": ["status": "succeeded"]]
        await pairModel.refresh()
        check(pairModel.items[0].label.hasPrefix("Check both apps"))
        check(pairModel.items[0].label.contains("outcome unknown"))
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
