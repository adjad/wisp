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
            let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: true) }
            let body = components?.path == "/assistant/upcoming" ? try fixtureUpcoming(dayStart: dayStart)
                : try fixturePlan(day: components?.queryItems?.first { $0.name == "day" }?.value ?? "", dayStart: dayStart)
            return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let attention = AttentionModel(now: { now }, transport: { request in
            (try fixtureAttention(dayStart: dayStart),
             HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        let stale = StaleRemindersModel(baseURL: URL(string: "http://fixture.invalid")!) { request in
            let rows: [[String: Any]] = [
                ["source_id": "s1", "title": "Return library book", "context": "Errands", "age_days": 9, "actions": ["keep", "delete"]],
                ["source_id": "s2", "title": "Look into summer internships", "context": "", "age_days": 12, "actions": ["keep", "delete"]],
            ]
            return (try JSONSerialization.data(withJSONObject: ["asks": rows]),
                    HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
        let model = OverlayModel()
        model.qaShow(brief: fixtureBrief)
        let width = SurfaceLayout.maxExpandedWidth
        for (name, tab, open) in [("brief", NotchTab.brief, nil), ("brief-open", NotchTab.brief, "MAIL"),
                                  ("brief-texts", NotchTab.brief, "TEXTS"),
                                  ("day", NotchTab.day, nil)] as [(String, NotchTab, String?)] {
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                if tab == .brief { NotchBriefView(model: model, today: today, open: open) }
                else { NotchDayView(model: model, today: today, attention: attention, staleReminders: stale) }
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
        func need(_ id: String, _ source: String, _ title: String, due: Double?, extra: [String: Any] = [:]) -> [String: Any] {
            ["id": id, "source": source, "title": title, "reason": "", "quote": "", "due_ts": due.map { at($0) } ?? NSNull(),
             "priority": 1, "reference": source + ":" + id, "suggested_minutes": 25, "needs_review": source != "reminders"]
                .merging(extra) { $1 }
        }
        let blocks = [
            block("lunch", "Lunch with Sam", 11.5, 12.5, "calendar"),
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
                    need("priya", "messages", "Priya: are we still on for dinner at 7?", due: nil,
                         extra: ["sender": "Priya", "received_ts": at(12.6)]),
                    need("priya-2", "messages", "Priya: lmk!", due: nil, extra: ["sender": "Priya", "received_ts": at(12.9)]),
                    need("mom", "messages", "Mom: call me when you can", due: nil, extra: ["sender": "Mom", "received_ts": at(9.2)]),
                    need("sam", "messages", "Sam: thanks!", due: nil,
                         extra: ["sender": "Sam", "received_ts": at(10), "replied": true]),
                    need("club", "calendar", "UCSC startup club, E2-506", due: 24 * 4 + 18),
                    need("essay", "reminders", "Essay outline", due: 24 * 4 + 18),
                    need("draft", "email", "Can you review my draft?", due: nil,
                         extra: ["sender": "Jordan Kim", "person": true, "received_ts": at(11),
                                 "gist": "Jordan wants your feedback on their essay draft by Monday"]),
                    need("thesis", "email", "Thesis meeting", due: nil,
                         extra: ["sender": "Prof. Lee", "person": true, "received_ts": at(12.2),
                                 "gist": "Prof. Lee moved your thesis meeting to Tuesday at 3"]),
                    need("office-1", "email", "Dublin Community Office Hours", due: nil,
                         extra: ["sender": "John Morada <john@example.edu>", "person": true, "received_ts": at(10.5)]),
                    need("office-2", "email", "Canceled: Dublin Community Office Hours", due: nil,
                         extra: ["sender": "John Morada <john@example.edu>", "person": true, "received_ts": at(10.4)]),
                    need("private", "email", "Private details omitted.", due: nil,
                         extra: ["sender": "Bank", "person": false, "received_ts": at(9)]),
                    need("techweek", "email", "Tech Week starts today", due: nil,
                         extra: ["sender": "UCSC Events", "person": false, "received_ts": at(8)]),
                    need("subscribed", "email", "You're subscribed: The Download", due: nil,
                         extra: ["sender": "MIT Technology Review", "person": false, "received_ts": at(7)]),
                ],
                "attention_omitted": 0,
                "source_coverage": [["source": "email", "ready": true, "detail": "5 unread", "inspected": 5, "omitted": 0],
                                    ["source": "messages", "ready": true, "detail": "", "inspected": 3, "omitted": 0]],
                "behavior_detail": "Tentative", "explanation": "",
                "proposed_plan": ["blocks": blocks, "unscheduled": [],
                                  "warnings": ["Reminders are not current; some deadlines may be missing."]],
            ],
        ]
        return try JSONSerialization.data(withJSONObject: payload)
    }

    static func fixtureUpcoming(dayStart: Double) throws -> Data {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        let rows: [[String: Any]] = [
            ["id": "u1", "title": "Work on PA 2", "when_ts": at(14), "source": "calendar", "kind": "event"],
            ["id": "u2", "title": "Essay outline", "when_ts": at(24 * 4 + 18), "source": "reminders", "kind": "reminder"],
            ["id": "u3", "title": "UCSC startup club, E2-506", "when_ts": at(24 * 4 + 18), "source": "calendar", "kind": "event"],
            ["id": "u4", "title": "CSE 20 midterm", "when_ts": at(24 * 6 + 10), "source": "calendar", "kind": "exam"],
        ]
        return try JSONSerialization.data(withJSONObject: ["commitments": rows])
    }

    static let fixtureBrief = """
    Good afternoon — Saturday, October 17. ☀️

    **🧭 Keeping in mind**
    - PA 2 is due tonight, and you said finishing CSE 20 strong matters this quarter.

    **📅 Today**
    - All day – PA 2 [CSE-20-04]

    **📧 Inbox — 12 emails · 5 unread**
    **Worth a look**
    - Advisor: Thesis meeting moved to Tuesday
    - Tech Week: starts today

    **💬 Messages**
    **👤 Priya**
    - Asked about dinner at 7; no reply yet.

    Let me know if you'd like me to dig into any of these or draft a reply.
    """

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
