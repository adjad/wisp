#if WISP_NOTCH_QA
import SwiftUI
import AppKit

// Renders the notch's Brief and Day tabs against a synthetic day and writes
// screenshots. Installs no AppDelegate and never contacts the Wisp service:
// TodayModel and AttentionModel answer from the fixture below.
//
//   swift build -Xswiftc -DWISP_NOTCH_QA && WISP_NOTCH_QA_OUT=/some/dir .build/debug/WispApp
@MainActor
enum NotchQADriver {
    static let zone = TimeZone(identifier: "America/Los_Angeles")!

    static func run() {
        let outDir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["WISP_NOTCH_QA_OUT"]
                         ?? FileManager.default.currentDirectoryPath, isDirectory: true)
        try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let midnight = calendar.startOfDay(for: Date())
        let now = midnight.addingTimeInterval(13 * 3600 + 7 * 60)
        let dayStart = midnight.timeIntervalSince1970
        let today = TodayModel(now: { now }, timeZoneProvider: { zone }, transport: { request in
            (try fixturePlan(day: request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: true) }?
                .queryItems?.first { $0.name == "day" }?.value ?? "", dayStart: dayStart),
             HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let attention = AttentionModel(now: { now }, transport: { request in
            (try fixtureAttention(dayStart: dayStart),
             HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let model = OverlayModel()
        let width = SurfaceLayout.maxExpandedWidth
        for (name, tab) in [("brief", NotchTab.brief), ("day", NotchTab.day)] {
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                if tab == .brief { NotchBriefView(model: model, today: today) }
                else { NotchDayView(model: model, today: today, attention: attention) }
            }
            .padding(EdgeInsets(top: 18, leading: 22, bottom: 16, trailing: 22))
            .frame(width: width, alignment: .topLeading)
            .background(Theme.surface)
            .environment(\.colorScheme, .dark)
            let host = NSHostingView(rootView: content)
            window.contentView = host
            window.setContentSize(host.fittingSize)
            window.makeKeyAndOrderFront(nil)
            windows[name] = window
        }
        quickSettings(width: width, model: model)
        app.activate(ignoringOtherApps: true)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            for (name, window) in windows {
                guard let view = window.contentView else { continue }
                window.setContentSize(view.fittingSize)
                view.layoutSubtreeIfNeeded()
                try? await Task.sleep(nanoseconds: 400_000_000)
                guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
                view.cacheDisplay(in: view.bounds, to: rep)
                try? rep.representation(using: .png, properties: [:])?.write(to: outDir.appendingPathComponent("notch-\(name).png"))
                print("notch-\(name): \(Int(view.bounds.width))×\(Int(view.bounds.height)) pt")
            }
            app.terminate(nil)
        }
        app.run()
    }

    private static var windows: [String: NSWindow] = [:]

    /// Quick settings in its two tallest states: every switch known, and Full access
    /// unconfirmed with the morning time list open. Also measured with the notch header.
    static func quickSettings(width: CGFloat, model: OverlayModel) {
        model.qaShow(modelMemory: ModelMemory(models: [.init(id: "Mellum2.1-8B-oQ4e", bytes: 7_301_444_403)]))
        for (name, unconfirmed) in [("settings", false), ("settings-unconfirmed", true)] {
            let loader = SettingsLoader()
            loader.qaShow(fullAccess: unconfirmed ? nil : true,
                          briefTimes: WispClient.BriefTimes(morning: 8, evening: 20))
            loader.behaviorEnabled = true
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                // The notch header (orb, tabs, buttons) is about 44 pt tall.
                Color.clear.frame(height: 44)
                QuickSettingsView(model: model, settings: loader, refreshes: false,
                                  picking: unconfirmed ? .morning : nil).padding(.top, 12)
            }
            .padding(EdgeInsets(top: 18, leading: 22, bottom: 16, trailing: 22))
            .frame(width: width, alignment: .topLeading)
            .background(Theme.surface)
            .environment(\.colorScheme, .dark)
            let host = NSHostingView(rootView: content)
            window.contentView = host
            window.setContentSize(host.fittingSize)
            window.makeKeyAndOrderFront(nil)
            windows[name] = window
        }
    }

    static func fixturePlan(day: String, dayStart: Double) throws -> Data {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        func block(_ id: String, _ title: String, _ start: Double, _ end: Double, _ kind: String,
                   task: String? = nil, pattern: String? = nil, evidence: String? = nil) -> [String: Any] {
            var b: [String: Any] = ["id": id, "title": title, "start": at(start), "end": at(end), "kind": kind, "warnings": []]
            if let task { b["task_id"] = task }
            if let pattern { b["pattern_id"] = pattern }
            if let evidence { b["evidence_id"] = evidence }
            return b
        }
        func need(_ id: String, _ source: String, _ title: String, due: Double?) -> [String: Any] {
            ["id": id, "source": source, "title": title, "reason": "", "quote": "", "due_ts": due.map { at($0) } ?? NSNull(),
             "priority": 1, "reference": source + ":" + id, "suggested_minutes": 25, "needs_review": source != "reminders"]
        }
        let blocks = [
            block("pa2-a", "Work on PA 2", 13.75, 15.75, "suggestion", evidence: "pa2"),
            block("away", "Possible time away", 16, 17.5, "behavior_away", pattern: "sat-away"),
            block("pa2-b", "Finish PA 2 and submit", 17.5, 18.5, "suggestion", evidence: "pa2"),
            block("club-prep", "Prep for startup club (Wed)", 20.75, 21.25, "suggestion", evidence: "club"),
        ]
        let payload: [String: Any] = [
            "day": day, "timezone": zone.identifier, "generated_at": at(13.1), "revision": 3, "provisional": false,
            "warnings": [], "tasks": [], "blocks": [], "deadlines": [],
            "all_day": [["id": "allday", "title": "PA 2 [CSE-20-04]", "start": at(0), "kind": "calendar", "warnings": []]],
            "unscheduled": [],
            "sources": ["calendar": ["ready": true, "last_sync": at(13), "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1320, "not_before": NSNull()],
            "workspace": [
                "attention": [
                    need("pa2", "reminders", "PA 2", due: 23.98),
                    need("doordash", "reminders", "Get DoorDash order", due: -20),
                    need("junk", "reminders", "nothing", due: 12),
                    need("priya", "messages", "Priya asked about dinner", due: nil),
                    need("club", "calendar", "UCSC startup club, E2-506", due: 24 * 4 + 18),
                    need("essay", "reminders", "Essay outline", due: 24 * 4 + 18),
                ],
                "attention_omitted": 0,
                "source_coverage": [["source": "email", "ready": true, "detail": "5 unread", "inspected": 5, "omitted": 0]],
                "behavior_detail": "Tentative", "explanation": "",
                "proposed_plan": ["blocks": blocks, "unscheduled": [],
                                  "warnings": ["Reminders are not current; some deadlines may be missing."]],
            ],
        ]
        return try JSONSerialization.data(withJSONObject: payload)
    }

    static func fixtureAttention(dayStart: Double) throws -> Data {
        let payload: [String: Any] = [
            "settings": ["mode": "shadow", "daily_cap": 3], "created_today": 0,
            "recent": [["source_id": "msg-1", "state": "shadow", "title": "Dinner with Priya?",
                        "event_ts": dayStart + 19 * 3600, "decided_at": dayStart + 12 * 3600, "when": "7 PM",
                        "quote": "dinner at 7?", "undoable": false]],
        ]
        return try JSONSerialization.data(withJSONObject: payload)
    }
}
#endif
