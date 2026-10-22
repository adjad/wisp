import SwiftUI
import AppKit

/// Synthetic Brief integration checks and offscreen renders. No AppDelegate,
/// live service, EventKit calls, or changes to an installed app.
@main
struct BriefTimeViewChecks {
    @MainActor static func main() {
        if CommandLine.arguments.contains("--print-real-home") {
            print(FileManager.default.homeDirectoryForCurrentUser.path)
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        Task { @MainActor in
            do {
                try await run()
                app.terminate(nil)
            } catch {
                fputs("FAIL: \(error)\n", stderr)
                exit(1)
            }
        }
        app.run()
    }

    struct Failure: Error, CustomStringConvertible { let description: String }

    @MainActor static func run() async throws {
        var checks = 0
        func check(_ condition: Bool, _ label: String) throws {
            checks += 1
            if !condition { throw Failure(description: label) }
        }
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let start = calendar.date(from: DateComponents(year: 2026, month: 10, day: 22))!
        let output = URL(fileURLWithPath: ProcessInfo.processInfo.environment["WISP_BRIEF_TIME_QA_OUT"]!)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        checks += try await isolationChecks()

        for (name, hour) in [("morning", 8), ("afternoon", 14), ("evening", 18), ("night", 0), ("late-night", 22)] {
            let now = calendar.date(bySettingHour: hour, minute: 30, second: 0, of: start)!
            let today = TodayModel(now: { now }, timeZoneProvider: { zone }, transport: { request in
                let parts = URLComponents(url: request.url!, resolvingAgainstBaseURL: true)!
                let day = parts.queryItems?.first { $0.name == "day" }?.value ?? "2026-10-22"
                let data: Data
                if parts.path == "/assistant/upcoming" {
                    data = try NotchQADriver.fixtureUpcoming(dayStart: start.timeIntervalSince1970)
                } else if parts.path == "/assistant/daily_summary/latest" {
                    return (Data(#"{"detail":"No summary in fixture"}"#.utf8),
                            HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
                } else {
                    var object = try JSONSerialization.jsonObject(with: NotchQADriver.fixturePlan(day: day, dayStart: start.timeIntervalSince1970)) as! [String: Any]
                    var workspace = object["workspace"] as! [String: Any]
                    workspace["needs_you"] = ["items": [
                        ["id": "pa2", "kind": "due", "text": "PA 2", "due_ts": start.timeIntervalSince1970 + 23.98 * 3600],
                        ["id": "doordash", "kind": "overdue", "text": "Get DoorDash order", "due_ts": start.timeIntervalSince1970 - 3600]
                    ], "more": 0]
                    object["workspace"] = workspace
                    data = try JSONSerialization.data(withJSONObject: object)
                }
                return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            })
            await today.refresh()
            guard let plan = today.currentPlan else { throw Failure(description: "\(name) fixture plan did not load") }
            let content = BriefContent(plan: plan, today: today, composed: nil)
            try check(content.suggestions.count == 3, "\(name): three suggestions reach the actual Brief")
            try check(content.lede.contains("Thursday"), "\(name): weekday stays consistent")
            try check(content.needsYou?.rows.count == 2, "\(name): Spotlight attention rows are preserved")

            let model = OverlayModel()
            model.tab = .brief
            let initialTurns = model.turns.count
            let initialAnswer = model.answer
            model.askAbout(content.suggestions[0].prompt)
            try check(model.tab == .ask && model.input == content.suggestions[0].prompt, "\(name): suggestion opens editable Ask draft")
            try check(model.turns.count == initialTurns && model.answer == initialAnswer && model.conversationSettled,
                      "\(name): suggestion does not submit a turn")
            model.tab = .brief

            let view = NotchBriefView(model: model, today: today)
                .padding(20).frame(width: 640).background(Theme.surface).environment(\.colorScheme, .dark)
            let host = NSHostingView(rootView: view)
            let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 640, height: 900),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = host
            window.setContentSize(host.fittingSize)
            window.orderFrontRegardless()
            try await Task.sleep(for: .milliseconds(250))
            window.setContentSize(host.fittingSize)
            host.layoutSubtreeIfNeeded()
            try check(host.bounds.height > 200 && host.bounds.height < 1400, "\(name): native layout has a bounded content height")
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
                throw Failure(description: "\(name): no offscreen bitmap")
            }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { throw Failure(description: "\(name): no PNG") }
            try png.write(to: output.appendingPathComponent("brief-\(name).png"))
            print("\(name): \(content.lede)")
            window.close()

            var unknownObject = try JSONSerialization.jsonObject(with: NotchQADriver.fixturePlan(day: "2026-10-22", dayStart: start.timeIntervalSince1970)) as! [String: Any]
            unknownObject["sources"] = [String: Any]()
            unknownObject["blocks"] = [[String: Any]]()
            unknownObject["all_day"] = [[String: Any]]()
            var unknownWorkspace = unknownObject["workspace"] as! [String: Any]
            unknownWorkspace["proposed_plan"] = ["blocks": [[String: Any]](), "unscheduled": [[String: Any]](), "warnings": [String]()]
            unknownObject["workspace"] = unknownWorkspace
            let unknownPlan = try JSONDecoder().decode(TodayPlan.self, from: JSONSerialization.data(withJSONObject: unknownObject))
            let unknownContent = BriefContent(plan: unknownPlan, today: today, composed: nil)
            try check(!unknownContent.lede.contains("light") && !unknownContent.lede.contains("clear") && !unknownContent.lede.contains("quiet"),
                      "\(name): an unread calendar makes no workload claim in the actual Brief")

            var placeholderObject = unknownObject
            let placeholder: [String: Any] = ["id": "junk-calendar", "title": "nothing", "start": now.timeIntervalSince1970 + 300,
                                              "end": now.timeIntervalSince1970 + 900, "kind": "calendar", "warnings": [String]()]
            var placeholderWorkspace = unknownWorkspace
            placeholderWorkspace["proposed_plan"] = ["blocks": [placeholder], "unscheduled": [[String: Any]](), "warnings": [String]()]
            placeholderObject["workspace"] = placeholderWorkspace
            placeholderObject["all_day"] = [["id": "junk-all-day", "title": "n/a", "start": start.timeIntervalSince1970,
                                               "kind": "calendar", "warnings": [String]()]]
            let placeholderPlan = try JSONDecoder().decode(TodayPlan.self, from: JSONSerialization.data(withJSONObject: placeholderObject))
            let placeholderContent = BriefContent(plan: placeholderPlan, today: today, composed: nil)
            try check(!placeholderContent.lede.contains("light") && !placeholderContent.lede.contains("clear") && !placeholderContent.lede.contains("quiet")
                      && !placeholderContent.lede.contains("Next up: nothing"), "\(name): placeholder rows do not establish calendar coverage")

            if name == "night" {
                var overnightObject = unknownObject
                overnightObject["sources"] = ["calendar": ["ready": true, "last_sync": now.timeIntervalSince1970, "reason": "Fixture"]]
                let event: [String: Any] = ["id": "overnight", "title": "Exam", "start": start.timeIntervalSince1970 + 3600,
                                             "end": start.timeIntervalSince1970 + 7200, "kind": "calendar", "warnings": [String]()]
                var overnightWorkspace = unknownWorkspace
                overnightWorkspace["proposed_plan"] = ["blocks": [event], "unscheduled": [[String: Any]](), "warnings": [String]()]
                overnightObject["workspace"] = overnightWorkspace
                let overnightPlan = try JSONDecoder().decode(TodayPlan.self, from: JSONSerialization.data(withJSONObject: overnightObject))
                let overnightContent = BriefContent(plan: overnightPlan, today: today, composed: nil)
                try check(!overnightContent.lede.contains("quiet"), "an actual 1 AM event prevents quiet-start wording through BriefContent")
            }

            // A picked date outside today must not inherit the current hour's
            // suggestions, even when it is visible in the same Brief surface.
            today.selectDate(calendar.date(byAdding: .day, value: -1, to: now)!)
            await today.refresh()
            guard let previous = today.currentPlan else { throw Failure(description: "previous-day fixture did not load") }
            let oldContent = BriefContent(plan: previous, today: today, composed: nil)
            try check(oldContent.suggestions.isEmpty && !oldContent.lede.contains("morning"), "\(name): another selected day stays neutral")

        }
        checks += try await mountedClockChecks(output: output, calendar: calendar)
        print("Brief view integration: \(checks) checks passed")
    }

    @MainActor final class FixtureClock { var value = Date() }

    /// Prove the launcher denies host state before constructing OverlayModel,
    /// whose eager delivery store otherwise uses Foundation's real home.
    @MainActor static func isolationChecks() async throws -> Int {
        let env = ProcessInfo.processInfo.environment
        guard let rootPath = env["WISP_BRIEF_TIME_QA_ROOT"],
              let statePath = env["WISP_HOME"],
              let deniedPath = env["WISP_BRIEF_TIME_QA_DENIED_RECEIPT"],
              let realHome = env["WISP_BRIEF_TIME_QA_REAL_HOME"] else {
            throw Failure(description: "Isolation launcher configuration is missing")
        }
        // Foundation normalizes /private/tmp differently when a file is denied.
        // Validate the launcher's exact temporary paths without probing them.
        let root = URL(fileURLWithPath: rootPath)
        guard rootPath.range(of: "^/private/tmp/wisp-brief-view\\.[A-Za-z0-9]+$", options: .regularExpression) != nil,
              statePath == root.appendingPathComponent("state").path,
              deniedPath == root.appendingPathComponent("denied/.moe/receipts.json").path,
              realHome.hasPrefix("/") && !realHome.hasPrefix(root.path) else {
            throw Failure(description: "Isolation paths must belong to the disposable fixture")
        }
        // This is a seeded synthetic receipt, never a probe of real user data.
        do {
            _ = try Data(contentsOf: URL(fileURLWithPath: deniedPath))
            throw Failure(description: "Filesystem denial is not active")
        } catch let failure as Failure {
            throw failure
        } catch {
            let denied = error as NSError
            guard (denied.domain == NSCocoaErrorDomain && denied.code == CocoaError.fileReadNoPermission.rawValue)
                    || (denied.domain == NSPOSIXErrorDomain && [EPERM, EACCES].contains(Int32(denied.code))) else {
                throw Failure(description: "Expected permission denial for the synthetic host receipt")
            }
        }
        let path = root.appendingPathComponent("state/receipt-control.json")
        let data = try JSONSerialization.data(withJSONObject: ["brief-fixture": ["kind": "changed", "done": true]])
        try data.write(to: path)
        guard try Data(contentsOf: path) == data else { throw Failure(description: "Synthetic receipts must remain readable") }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ReceiptControlProtocol.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        var callbacks = 0
        let handled = await AssistantDelivery(path: path).handle(
            .init(type: "reminder", payload: ["event_id": "brief-fixture"]),
            client: WispClient(deliverySession: session, deliveryURL: URL(string: "http://fixture.invalid")!),
            perform: { _ in callbacks += 1; return true }, calendar: { _ in callbacks += 1; return [:] })
        guard !handled && callbacks == 0 else { throw Failure(description: "Synthetic receipt collision must prevent dispatch") }
        guard try Data(contentsOf: path) == data else { throw Failure(description: "Receipt control must not mutate its fixture") }
        print("Isolation: real home denied; synthetic denial and receipt-read controls passed")
        return 5
    }

    final class ReceiptControlProtocol: URLProtocol {
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { fatalError("Receipt control must not make a request") }
        override func stopLoading() {}
    }

    /// Change an unobserved clock while keeping the same mounted view, plan and
    /// models. Only TimelineView's minute tick can refresh the rendered copy.
    @MainActor static func mountedClockChecks(output: URL, calendar: Calendar) async throws -> Int {
        let clock = FixtureClock()
        let zone = calendar.timeZone
        let start = calendar.startOfDay(for: clock.value)
        var requests = 0
        let today = TodayModel(now: { clock.value }, timeZoneProvider: { zone }, transport: { request in
            requests += 1
            guard let day = URLComponents(url: request.url!, resolvingAgainstBaseURL: true)?.queryItems?.first(where: { $0.name == "day" })?.value else {
                throw Failure(description: "mounted clock: expected a dated fixture request")
            }
            let data = try NotchQADriver.fixturePlan(day: day, dayStart: start.timeIntervalSince1970)
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        await today.refresh()
        guard today.currentPlan != nil else { throw Failure(description: "mounted-clock fixture did not load") }
        let model = OverlayModel()
        let view = NotchBriefView(model: model, today: today)
            .padding(20).frame(width: 640).background(Theme.surface).environment(\.colorScheme, .dark)
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 640, height: 900),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.setContentSize(host.fittingSize)
        window.orderFrontRegardless()
        defer { window.close() }
        try await Task.sleep(for: .milliseconds(250))

        func snapshot() throws -> Data {
            window.setContentSize(host.fittingSize)
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
                throw Failure(description: "mounted clock: no bitmap")
            }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else {
                throw Failure(description: "mounted clock: no PNG")
            }
            return data
        }
        let before = try snapshot()
        let initialPeriod = BriefTimeContext(date: clock.value, timezone: zone).period
        let targetHour: Int
        switch initialPeriod {
        case .morning: targetHour = 12
        case .afternoon: targetHour = 17
        case .evening: targetHour = 21
        case .night: targetHour = 5
        }
        clock.value = calendar.date(bySettingHour: targetHour, minute: 0, second: 0, of: clock.value)!
        let nextPeriod = BriefTimeContext(date: clock.value, timezone: zone).period
        guard nextPeriod != initialPeriod else { throw Failure(description: "mounted clock: fixture did not change daypart") }
        guard today.currentPlan != nil else { throw Failure(description: "mounted clock: clock change lost the current day") }
        let requestCount = requests
        // The schedule was anchored to the real minute when the view mounted.
        // Wait through its next tick without publishing a model or plan change.
        let delay = 61.5 - Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 60)
        try await Task.sleep(for: .seconds(delay))
        let after = try snapshot()
        guard after != before else { throw Failure(description: "mounted clock: rendered Brief did not change at the minute tick") }
        guard requests == requestCount else { throw Failure(description: "mounted clock: a fetch refreshed the view instead of its local clock") }
        try after.write(to: output.appendingPathComponent("brief-clock-transition.png"))
        print("Mounted clock: \(initialPeriod.rawValue) → \(nextPeriod.rawValue), without a data refresh")
        return 4
    }
}
