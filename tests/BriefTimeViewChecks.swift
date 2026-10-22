import SwiftUI
import AppKit

/// Synthetic Brief integration checks and offscreen renders. No AppDelegate,
/// live service, EventKit calls, or changes to an installed app.
@main
struct BriefTimeViewChecks {
    @MainActor static func main() {
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
            let initialTurns = model.turns.count
            let initialAnswer = model.answer
            model.askAbout(content.suggestions[0].prompt)
            try check(model.tab == .ask && model.input == content.suggestions[0].prompt, "\(name): suggestion opens editable Ask draft")
            try check(model.turns.count == initialTurns && model.answer == initialAnswer, "\(name): suggestion does not submit a turn")
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
        print("Brief view integration: \(checks) checks passed")
    }
}
