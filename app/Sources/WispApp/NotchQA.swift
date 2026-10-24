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
        let offscreen = ProcessInfo.processInfo.environment["WISP_QA_OFFSCREEN"] == "1"
        let app = NSApplication.shared
        app.setActivationPolicy(offscreen ? .accessory : .regular)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let midnight = calendar.startOfDay(for: Date())
        let now = midnight.addingTimeInterval(13 * 3600 + 7 * 60)
        let dayStart = midnight.timeIntervalSince1970
        func dayModel(_ proactive: ProactiveFixture) -> TodayModel {
            TodayModel(now: { now }, timeZoneProvider: { zone }, transport: { request in
                let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: true) }
                let ok = { (body: String) in
                    (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
                }
                // Card fires, dismissals, to-do changes and Analyze now: answered here, never sent.
                if request.httpMethod == "POST", components?.path.hasPrefix("/assistant/proactive/") == true {
                    return ok(#"{"ok":true,"detail":"Done"}"#)
                }
                if components?.path == "/assistant/daily_summary/latest" {
                    // No kept summary: the "brief" scenes use the live one, "brief-nosummary" shows the note.
                    return (Data(#"{"detail":"no composed summary for that day"}"#.utf8),
                            HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
                }
                let body = components?.path == "/assistant/upcoming" ? try fixtureUpcoming(dayStart: dayStart)
                    : try fixturePlan(day: components?.queryItems?.first { $0.name == "day" }?.value ?? "", dayStart: dayStart,
                                      proactive: proactive)
                return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            })
        }
        let today = dayModel(.ready)
        let working = dayModel(.working)
        let legacy = dayModel(.none)
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
        DayCards.qaUse(attention: attention, staleReminders: stale)
        // This driver has no AppDelegate. Own the same refresh lifecycle using
        // only its injected transports, instead of relying on a mounted tab.
        today.startBackgroundRefresh(afterRefresh: { await DayCards.refresh() })
        let model = OverlayModel()
        model.qaShow(brief: fixtureBrief)
        let unsummarized = OverlayModel()
        let width = SurfaceLayout.maxExpandedWidth
        // Each Brief scene keeps its own card and to-do state.
        let draft = ProactiveActions()
        draft.qaReset(openDraft: "card-dana", expanded: ["t-dana"], replying: "t-dana")
        let judge = ProactiveActions()
        judge.qaReset(account: "judge.demo@gmail.com")
        let scenes: [(name: String, tab: NotchTab, view: () -> AnyView)] = [
            ("brief", .brief, { AnyView(NotchBriefView(model: model, today: today, actions: ProactiveActions())) }),
            ("brief-full", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true, actions: ProactiveActions())) }),
            ("brief-draft", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true, actions: draft)) }),
            ("brief-working", .brief, { AnyView(NotchBriefView(model: model, today: working, actions: ProactiveActions())) }),
            ("brief-judge", .brief, { AnyView(NotchBriefView(model: model, today: today, actions: judge)) }),
            ("brief-legacy", .brief, { AnyView(NotchBriefView(model: model, today: legacy, open: nil)) }),
            ("brief-legacy-open", .brief, { AnyView(NotchBriefView(model: unsummarized, today: legacy, open: "MAIL")) }),
            ("day", .day, { AnyView(NotchDayView(model: model, today: today)) }),
        ]
        for scene in scenes {
            let name = scene.name
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: width, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                scene.view()
            }
            .padding(EdgeInsets(top: 18, leading: 22, bottom: 16, trailing: 22))
            .frame(width: width, alignment: .topLeading)
            .background(Theme.surface)
            .environment(\.colorScheme, .dark)
            let host = NSHostingView(rootView: content)
            window.contentView = host
            window.setContentSize(host.fittingSize)
            if !offscreen { window.makeKeyAndOrderFront(nil) }
            windows[name] = window
        }
        quickSettings(width: width, model: model, offscreen: offscreen)
        if !offscreen { app.activate(ignoringOtherApps: true) }
        Task { @MainActor in
            await working.refresh()
            await legacy.refresh()
            await legacy.refreshUpcoming()
            let deadline = Date().addingTimeInterval(5)
            @MainActor func populated() -> Bool {
                today.currentPlan?.workspace?.proactive?.cards.count == 3 && today.upcomingReply != nil && today.storedBriefChecked &&
                working.currentPlan?.workspace?.proactive?.isWorking == true &&
                legacy.currentPlan?.workspace != nil && legacy.currentPlan?.workspace?.proactive == nil &&
                attention.items.count == 1 && stale.asks.count == 2
            }
            while !populated() && Date() < deadline {
                try? await Task.sleep(for: .milliseconds(20))
            }
            precondition(populated(), "Notch fixtures must populate Brief, Day and shared cards before capture")
            print("Notch fixture population: passed")
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
            today.stopBackgroundRefresh()
            app.terminate(nil)
        }
        app.run()
    }

    private static var windows: [String: NSWindow] = [:]

    /// Quick settings in its two tallest states: every switch known, and Full access
    /// unconfirmed with the morning time list open. Also measured with the notch header.
    static func quickSettings(width: CGFloat, model: OverlayModel, offscreen: Bool = false) {
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
            if !offscreen { window.makeKeyAndOrderFront(nil) }
            windows[name] = window
        }
    }

    /// Which `workspace.proactive` the fixture carries.
    enum ProactiveFixture { case ready, working, none }

    static let judgeAccount = "judge.demo@gmail.com"

    /// A demo day: a recruiter's coffee-chat email, "Mom: let's talk tmrw", a
    /// Trader Joe's reminder, two calendar events, three cards, six to-dos and
    /// five thread summaries (one from a linked judge account).
    static func fixtureProactive(dayStart: Double, working: Bool) -> [String: Any] {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        let me = "adi@ucsc.edu"
        let cards: [[String: Any]] = [
            ["id": "card-dana", "meta": "Waiting 2 days · Mail", "account": me,
             "title": "Dana from Stripe wants a coffee chat",
             "body": "I checked your calendar: you're free Thursday 3–4 PM. I've drafted a reply offering Thursday at 3, and I'll hold that hour for you.",
             "why": "A recruiter asked two days ago and you haven't replied.",
             "sources": [["kind": "mail", "ref": "mail:dana-1", "who": "Dana Lee",
                          "quote": "Would you be up for a 30-minute coffee chat next week about the PM internship?"]],
             "action": ["method": "email_reply", "button": "Reply & hold Thursday 3 PM?",
                        "recipient": "Dana Lee <dana.lee@stripe.com>",
                        "draft": "Hi Dana,\n\nThanks for reaching out — I'd love to chat about the PM internship. Would Thursday at 3 PM work? Happy to meet at the campus coffee bar or hop on a call.\n\nBest,\nAdi",
                        "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-mom", "meta": "Tomorrow · Texts", "account": me,
             "title": "Mom wants to talk tomorrow",
             "body": "Your calendar is clear tomorrow after 5 PM. I've drafted a text suggesting 6:30, and I'll remind you to call.",
             "why": "Mom asked to talk tomorrow.",
             "sources": [["kind": "messages", "ref": "msg:mom", "who": "Mom", "quote": "let's talk tmrw"]],
             "action": ["method": "text_reply", "button": "Text Mom & set a reminder?", "recipient": "Mom",
                        "draft": "Sounds good! How about 6:30 tomorrow? I'll call you then.", "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-tj", "meta": "Today · 5:30 PM · Reminders", "account": me,
             "title": "Your Trader Joe's run fits before club",
             "body": "You're free 4:45–6 PM. I can put 5:00–5:30 on your day so the errand doesn't slip past startup club.",
             "why": "Your reminder is due at 5:30 PM today.",
             "sources": [["kind": "reminders", "ref": "rem:tj", "who": "", "quote": "Go to Trader Joe's"]],
             "action": ["method": "add_to_day", "button": "Add it to my day?", "draft": "", "recipient": "",
                        "event": NSNull(), "envelope": "opaque"]],
        ]
        let todos: [[String: Any]] = [
            ["id": "todo-dana", "title": "Reply to Dana about a coffee chat", "source": "mail", "account": me,
             "who": "Dana Lee, Stripe", "quote": "30-minute coffee chat next week?", "due_ts": NSNull(),
             "due_label": "This week", "kind": "reply", "done": false, "card_id": "card-dana"],
            ["id": "todo-mom", "title": "Call Mom", "source": "messages", "account": me, "who": "Mom",
             "quote": "let's talk tmrw", "due_ts": at(24), "due_label": "Tomorrow", "kind": "call", "done": false,
             "card_id": "card-mom"],
            ["id": "todo-priya", "title": "Call Priya about the pitch deck", "source": "messages", "account": me,
             "who": "Priya", "quote": "can you call at 4:15?", "due_ts": at(16.25), "due_label": "4:15 PM",
             "kind": "call", "done": false, "card_id": NSNull()],
            ["id": "todo-tj", "title": "Go to Trader Joe's", "source": "reminders", "account": me, "who": "",
             "quote": "", "due_ts": at(17.5), "due_label": "5:30 PM", "kind": "errand", "done": false, "card_id": "card-tj"],
            ["id": "todo-lecture", "title": "CSE 101 lecture", "source": "calendar", "account": me, "who": "",
             "quote": "", "due_ts": at(14), "due_label": "2 PM", "kind": "event", "done": false, "card_id": NSNull()],
            ["id": "todo-hack", "title": "Bring a 5-minute demo to Hall B", "source": "mail", "account": judgeAccount,
             "who": "Hackathon Team", "quote": "each demo gets five minutes", "due_ts": NSNull(), "due_label": "Noon",
             "kind": "event", "done": false, "card_id": NSNull()],
            ["id": "todo-charger", "title": "Bring a laptop charger to club", "source": "notes", "account": me,
             "who": "", "quote": "- [ ] charger for club", "due_ts": NSNull(), "due_label": "", "kind": "task",
             "done": false, "card_id": NSNull()],
        ]
        let threads: [[String: Any]] = [
            ["id": "t-dana", "source": "mail", "account": me, "who": "Dana Lee", "title": "Coffee chat next week?",
             "summary": "Stripe recruiter asking for a 30-minute coffee chat about the PM internship next week.",
             "needs_reply": true, "received_ts": at(-36), "reply_ref": ["message_id": "<dana-1@stripe.com>", "account": "adi@ucsc.edu", "account_id": "acct-1"]],
            ["id": "t-priya", "source": "messages", "account": me, "who": "Priya", "title": "",
             "summary": "Wants a quick call at 4:15 about the club pitch deck; she's sending slides first.",
             "needs_reply": true, "received_ts": at(12.9), "reply_ref": [String: String]()],
            ["id": "t-mom", "source": "messages", "account": me, "who": "Mom", "title": "",
             "summary": "Wants to talk tomorrow; nothing urgent, just catching up.",
             "needs_reply": true, "received_ts": at(9.2), "reply_ref": [String: String]()],
            ["id": "t-lee", "source": "mail", "account": me, "who": "Prof. Lee", "title": "Thesis meeting moved",
             "summary": "Moved your thesis meeting to Tuesday at 2 PM and asked for the outline beforehand.",
             "needs_reply": false, "received_ts": at(12.2), "reply_ref": ["message_id": "<lee-4@ucsc.edu>", "account": "adi@ucsc.edu", "account_id": "acct-1"]],
            ["id": "t-hack", "source": "mail", "account": judgeAccount, "who": "Hackathon Team", "title": "Judging schedule",
             "summary": "Judging starts at noon in Hall B; each demo gets five minutes plus questions.",
             "needs_reply": false, "received_ts": at(8), "reply_ref": ["message_id": "<hack-1@gmail.com>", "account": "judge.demo@gmail.com"]],
        ]
        return [
            "state": working ? "working" : "ready", "as_of": at(13), "progress": ["done": 12, "total": 40],
            "accounts": [me, judgeAccount],
            "summary": "Two things on the clock: CSE 101 at 2 and startup club at 6. Dana from Stripe is still waiting on a coffee-chat reply, Priya wants a call at 4:15, and Mom wants to talk tomorrow. PA 2 is due tonight.",
            "cards": working ? Array(cards.prefix(1)) : cards, "todos": todos, "threads": threads,
        ]
    }

    static func fixturePlan(day: String, dayStart: Double, proactive: ProactiveFixture = .ready) throws -> Data {
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
            block("lecture", "CSE 101 lecture", 14, 15.25, "calendar"),
            block("club", "Startup club meeting", 18, 19, "calendar"),
            block("pa2-a", "Work on PA 2", 20, 21.5, "suggestion", evidence: "pa2"),
        ]
        var workspace: [String: Any] = [
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
                     extra: ["sender": "Jordan Kim", "person": true, "received_ts": at(11)]),
                need("thesis", "email", "Thesis meeting", due: nil,
                     extra: ["sender": "Prof. Lee", "person": true, "received_ts": at(12.2)]),
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
            "needs_you": ["items": [
                ["id": "doordash", "kind": "overdue", "text": "Get DoorDash order", "due_ts": at(-20), "all_day": false],
                ["id": "pa2", "kind": "due", "text": "PA 2", "due_ts": at(23.98), "all_day": false],
                ["id": "thesis", "kind": "mail", "text": "“Thesis meeting” from Prof. Lee", "due_ts": NSNull(), "all_day": false],
                ["id": "draft", "kind": "mail", "text": "“Can you review my draft?” from Jordan Kim", "due_ts": NSNull(), "all_day": false],
            ], "more": 4],
            "source_coverage": [["source": "email", "ready": true, "detail": "5 unread", "inspected": 5, "omitted": 0],
                                ["source": "messages", "ready": true, "detail": "", "inspected": 3, "omitted": 0]],
            "behavior_detail": "Tentative", "explanation": "",
            "mail_overview": ["text": "Mostly event updates and tech-week promos", "source": "model"],
            "texts_overview": ["text": "Mostly dinner plans and family check-ins", "source": "model"],
            "mail_summary": ["text": "Prof. Lee moved your thesis meeting to Tuesday and Jordan Kim wants feedback on an essay draft by Monday. John Morada canceled Dublin Community Office Hours. The rest is Tech Week promos and newsletters.", "source": "model"],
            "proposed_plan": ["blocks": blocks, "unscheduled": [["task_id": "t-essay", "title": "Essay outline",
                                                                "reason": "Needs 45 min; the largest gap is 30 min"]],
                              "warnings": ["Reminders are not current; some deadlines may be missing."]],
        ]
        if proactive != .none {
            workspace["proactive"] = fixtureProactive(dayStart: dayStart, working: proactive == .working)
        }
        let payload: [String: Any] = [
            "day": day, "timezone": zone.identifier, "generated_at": at(13.1), "revision": 3, "provisional": false,
            "warnings": [], "tasks": [], "blocks": [], "deadlines": [],
            "all_day": [["id": "allday", "title": "PA 2 [CSE-20-04]", "start": at(0), "kind": "calendar", "warnings": []]],
            "unscheduled": [],
            "sources": ["calendar": ["ready": true, "last_sync": at(13), "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1320, "not_before": NSNull()],
            "workspace": workspace,
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
    - **Priya** · 2 messages, latest 12:54 PM — Priya: “lmk!”
    - **Mom** · 1 message, latest 9:12 AM — “call me when you can”
    - **Sam** · 2 messages, latest 10:00 AM — Me: “np”

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
