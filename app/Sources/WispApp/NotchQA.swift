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
        // WISP_NOTCH_QA_ONBOARDING=1: only the onboarding window's steps (NotchQAOnboarding).
        if ProcessInfo.processInfo.environment["WISP_NOTCH_QA_ONBOARDING"] == "1" {
            NotchQAOnboarding.run(outDir: outDir, offscreen: offscreen)
            return
        }
        let app = NSApplication.shared
        app.setActivationPolicy(offscreen ? .accessory : .regular)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let midnight = calendar.startOfDay(for: Date())
        let now = midnight.addingTimeInterval(10 * 3600 + 32 * 60)
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
        // First-day states: a new user's empty day, the same day with Calendar,
        // Reminders, Mail and Messages unreadable, and the service still starting.
        func variantModel(_ variant: DayVariant) -> TodayModel {
            TodayModel(now: { now }, timeZoneProvider: { zone }, transport: { request in
                let components = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: true) }
                if variant == .starting { throw URLError(.cannotConnectToHost) }
                if components?.path == "/assistant/daily_summary/latest" {
                    return (Data(#"{"detail":"no composed summary for that day"}"#.utf8),
                            HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!)
                }
                let body = components?.path == "/assistant/upcoming" ? Data(#"{"commitments":[]}"#.utf8)
                    : try fixtureVariantPlan(variant, day: components?.queryItems?.first { $0.name == "day" }?.value ?? "",
                                             dayStart: dayStart)
                return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            })
        }
        let dayOne = variantModel(.dayOne)
        let blocked = variantModel(.blocked)
        let blockedProactive = variantModel(.blockedProactive)
        let starting = variantModel(.starting)
        starting.retrySleep = { _ in try await Task.sleep(for: .seconds(3600)) }
        let startingModel = OverlayModel()
        startingModel.serviceStarting = true
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
        let width = SurfaceLayout.maxExpandedWidth
        // Each Brief scene keeps its own card and to-do state.
        func state(_ configure: (ProactiveActions) -> Void = { _ in }) -> ProactiveActions {
            let actions = ProactiveActions()
            configure(actions)
            return actions
        }
        // The reply composer answers from its own canned fixture, never the service.
        BriefReplyHook.composer = { thread, close in
            AnyView(BriefReplyComposer(replyRef: thread.replyRef, account: thread.replyAccount, transport: { request in
                let draft = request.url?.lastPathComponent == "draft"
                let body: [String: Any] = draft
                    ? ["ok": true, "draft": "Hi Ed,\n\nTuesday morning works for me. I can be there at 9.\n\nThanks,\nAdi",
                       "to": ["echen@ucsc.edu"], "cc": [], "subject": "Re: Exam 1 timing", "account": "me",
                       "envelope": "qa", "can_send": true]
                    : ["ok": true, "status": "sent"]
                return (try JSONSerialization.data(withJSONObject: body),
                        HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }, onDismiss: close))
        }
        let scenes: [(name: String, tab: NotchTab, view: () -> AnyView)] = [
            ("brief", .brief, { AnyView(NotchBriefView(model: model, today: today, actions: state())) }),
            ("brief-todos", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true,
                                                             actions: state { $0.qaReset(openRow: "todos") })) }),
            ("brief-threads", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true,
                                                               actions: state { $0.qaReset(expanded: ["t-ed"], replying: "t-ed") })) }),
            ("brief-draft", .brief, { AnyView(NotchBriefView(model: model, today: today,
                                                             actions: state { $0.qaReset(openDraft: "card-ed", hero: "card-ed") })) }),
            ("brief-working", .brief, { AnyView(NotchBriefView(model: model, today: working, actions: state())) }),
            ("brief-judge", .brief, { AnyView(NotchBriefView(model: model, today: today,
                                                             actions: state { $0.qaReset(account: judgeAccount) })) }),
            ("brief-legacy", .brief, { AnyView(NotchBriefView(model: model, today: legacy, open: nil)) }),
            ("day", .day, { AnyView(NotchDayView(model: model, today: today)) }),
            ("brief-dayone", .brief, { AnyView(NotchBriefView(model: model, today: dayOne, open: nil)) }),
            ("day-dayone", .day, { AnyView(NotchDayView(model: model, today: dayOne)) }),
            ("brief-blocked", .brief, { AnyView(NotchBriefView(model: model, today: blocked, open: nil)) }),
            ("brief-blocked-proactive", .brief, { AnyView(NotchBriefView(model: model, today: blockedProactive, actions: state())) }),
            ("day-blocked", .day, { AnyView(NotchDayView(model: model, today: blocked)) }),
            ("day-starting", .day, { AnyView(NotchDayView(model: startingModel, today: starting)) }),
        ] + NotchQASetup.scenes()
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
            for variant in [dayOne, blocked, blockedProactive] {
                await variant.refresh()
                await variant.refreshUpcoming()
                await variant.refreshStoredBrief()
            }
            await starting.refresh()
            let deadline = Date().addingTimeInterval(5)
            @MainActor func populated() -> Bool {
                today.currentPlan?.workspace?.proactive?.cards.count == 3 && today.upcomingReply != nil && today.storedBriefChecked &&
                working.currentPlan?.workspace?.proactive?.isWorking == true &&
                legacy.currentPlan?.workspace != nil && legacy.currentPlan?.workspace?.proactive == nil &&
                dayOne.currentPlan != nil && blocked.currentPlan?.unreadSources.count == 4 &&
                blockedProactive.currentPlan?.workspace?.proactive?.hasReadNothing == true &&
                starting.currentPlan == nil && starting.error.isEmpty &&
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

    /// A first-day state: `dayOne` is a new user's empty day with every source
    /// read; `blocked` is that day with Calendar and Reminders access off and
    /// Mail and Messages unreadable; `starting` never answers (cold launch).
    enum DayVariant { case dayOne, blocked, blockedProactive, starting }

    static func fixtureVariantPlan(_ variant: DayVariant, day: String, dayStart: Double) throws -> Data {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        let isBlocked = variant == .blocked || variant == .blockedProactive
        let coverage: [[String: Any]] = isBlocked ? [
            ["source": "calendar", "ready": false, "detail": "", "inspected": 0, "omitted": 0, "state": "unavailable",
             "blocked": "calendar_access",
             "reason": "Calendar access is off for Wisp. Turn on Calendar for Wisp in System Settings > Privacy & Security > Calendars."],
            ["source": "reminders", "ready": false, "detail": "", "inspected": 0, "omitted": 0, "state": "unavailable",
             "blocked": "reminders_access"],
            ["source": "email", "ready": false, "detail": "", "inspected": 0, "omitted": 0, "state": "unavailable",
             "blocked": "mail_closed"],
            ["source": "messages", "ready": false, "detail": "", "inspected": 0, "omitted": 0, "state": "unavailable",
             "blocked": "full_disk_access"],
        ] : ["calendar", "reminders", "email", "messages"].map {
            ["source": $0, "ready": true, "detail": "", "inspected": 0, "omitted": 0]
        }
        var workspace: [String: Any] = [
            "attention": [], "attention_omitted": 0, "needs_you": ["items": [], "more": 0],
            "source_coverage": coverage, "behavior_detail": "", "explanation": "",
            "proposed_plan": ["blocks": [], "unscheduled": [],
                              "warnings": isBlocked ? ["Calendar is not current. Flexible tasks are waiting for a successful sync.",
                                                       "Reminders are not current; some deadlines may be missing."] : []],
        ]
        if variant == .blockedProactive {
            // The proactive Brief on the same day: nothing read, so no all-clear.
            workspace["proactive"] = [
                "state": "ready", "as_of": at(10.3), "accounts": [String](), "owner": ["id": "me", "label": "Adi"],
                "lede": "Wisp can't see your calendar yet.", "summary": "Wisp can't see your calendar yet.",
                "stats": ["read": 0, "found": 0, "drafted": 0], "cards": [], "todos": [], "threads": [],
                "unread": ["calendar", "reminders", "email", "messages"],
            ] as [String: Any]
        }
        func source(_ blockedKind: String) -> [String: Any] {
            isBlocked ? ["ready": false, "last_sync": at(10.4), "state": "unavailable", "blocked": blockedKind,
                         "reason": blockedKind == "calendar_access"
                             ? "Calendar access is off for Wisp. Turn on Calendar for Wisp in System Settings > Privacy & Security > Calendars."
                             : "Reminders access is off for Wisp. Turn on Reminders for Wisp in System Settings > Privacy & Security > Reminders."]
                : ["ready": true, "last_sync": at(10.4), "reason": "Up to date", "state": "ready"]
        }
        let payload: [String: Any] = [
            "day": day, "timezone": zone.identifier, "generated_at": at(10.5), "revision": 0, "provisional": isBlocked,
            "warnings": [], "tasks": [], "blocks": [], "deadlines": [], "all_day": [], "unscheduled": [],
            "sources": ["calendar": source("calendar_access"), "reminders": source("reminders_access")],
            "preferences": ["start_minute": 540, "end_minute": 1080, "not_before": NSNull()],
            "workspace": workspace,
        ]
        return try JSONSerialization.data(withJSONObject: payload)
    }

    static let judgeAccount = "judge.demo@gmail.com"

    /// A demo day: Hack Day and Hacktoberfest at midday, a STAT 5 worksheet to
    /// block time for, Ed's exam email, "Mom: let's talk tmrw", a recruiter's
    /// coffee chat and a Trader Joe's reminder; three cards, seven to-dos and
    /// six thread summaries (one from a linked judge account).
    static func fixtureProactive(dayStart: Double, working: Bool) -> [String: Any] {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        let me = "me"
        let cards: [[String: Any]] = [
            ["id": "card-stat", "meta": "Due tomorrow · Reminders", "account": me,
             "title": "Worksheet 3 for STAT 5",
             "body": "You're free 2–3 PM after Hack Day. I'll hold that hour.",
             "why": "It's due tomorrow at 11:59 PM and nothing on your day covers it.",
             "sources": [["kind": "reminders", "ref": "rem:stat5", "who": "", "quote": "STAT 5 worksheet 3"]],
             "action": ["method": "block_time", "button": "", "draft": "", "recipient": "",
                        "slot": ["start": at(14), "end": at(15)], "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-ed", "meta": "Waiting 1 day · Mail", "account": me,
             "title": "Reply to Ed's exam email",
             "body": "Ed asked if you can take Exam 1 early on Tuesday. You're free 9–11 AM, so I've drafted a yes.",
             "why": "Ed asked yesterday and you haven't replied.",
             "sources": [["kind": "mail", "ref": "mail:ed-1", "who": "Ed Chen",
                          "quote": "Would you be able to take Exam 1 early, Tuesday morning?"]],
             "action": ["method": "email_reply", "button": "Send the yes?",
                        "recipient": "Ed Chen <echen@ucsc.edu>",
                        "draft": "Hi Ed,\n\nYes, Tuesday morning works. I can come in at 9. Thanks for setting it up.\n\nBest,\nAdi",
                        "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-exam", "meta": "Wednesday · Mail", "account": me,
             "title": "Remind me: Exam 1 Wed",
             "body": "Ed's email puts Exam 1 on Wednesday at 10 AM in Kresge 321. I'll remind you Tuesday at 7 PM to review.",
             "why": "An exam date in your mail isn't on your calendar or reminders.",
             "sources": [["kind": "mail", "ref": "mail:ed-1", "who": "Ed Chen", "quote": "Exam 1 is Wednesday at 10 in Kresge 321"]],
             "action": ["method": "reminder", "button": "Set the reminder?", "draft": "", "recipient": "",
                        "event": NSNull(), "envelope": "opaque"]],
        ]
        func todo(_ id: String, _ title: String, _ source: String, who: String = "", quote: String = "",
                  due: Double?, label: String, kind: String = "task", card: String? = nil, account: String = "me") -> [String: Any] {
            ["id": id, "title": title, "source": source, "account": account, "who": who, "quote": quote,
             "due_ts": due.map { at($0) } ?? NSNull(), "due_label": label, "kind": kind, "done": false,
             "card_id": card ?? NSNull()]
        }
        let todos: [[String: Any]] = [
            todo("todo-read-cse", "Readings", "reminders", who: "CSE 30", due: 17, label: "5 PM"),
            todo("todo-read-stat", "Readings", "reminders", who: "STAT 5", due: 24 + 12, label: "Tomorrow"),
            todo("todo-stat", "Worksheet 3", "reminders", who: "STAT 5", due: 24 + 23.98, label: "Tomorrow", card: "card-stat"),
            todo("todo-mom", "Call Mom", "messages", who: "Mom", quote: "let's talk tmrw", due: 24, label: "Tomorrow", kind: "call"),
            todo("todo-tj", "Go to Trader Joe's", "reminders", due: 17.5, label: "5:30 PM", kind: "errand"),
            todo("todo-devarsh", "Coffee chat with Devarsh", "mail", who: "Devarsh, Stripe",
                 quote: "30-minute coffee chat next week?", due: nil, label: "This week"),
            todo("todo-hack", "Hack Day", "calendar", due: 11.75, label: "11:45 AM", kind: "event"),
        ]
        let threads: [[String: Any]] = [
            ["id": "t-ed", "source": "mail", "account": me, "who": "Ed", "title": "Exam 1 timing",
             "summary": "Asks if you can take Exam 1 early on Tuesday morning; the exam itself is Wednesday at 10.",
             "needs_reply": true, "received_ts": at(-14),
             "reply_ref": ["message_id": "<ed-1@ucsc.edu>", "account": me, "account_id": "acct-1"]],
            ["id": "t-mom", "source": "messages", "account": me, "who": "Mom", "title": "",
             "summary": "Wants to talk tomorrow; nothing urgent, just catching up.",
             "needs_reply": true, "received_ts": at(-15), "reply_ref": [String: String]()],
            ["id": "t-devarsh", "source": "mail", "account": me, "who": "Devarsh", "title": "Coffee chat next week?",
             "summary": "Stripe recruiter confirmed your 30-minute coffee chat for Thursday at 3; a calendar invite is coming.",
             "needs_reply": false, "received_ts": at(-36),
             "reply_ref": ["message_id": "<devarsh-1@stripe.com>", "account": me, "account_id": "acct-1"]],
            ["id": "t-priya", "source": "messages", "account": me, "who": "Priya", "title": "",
             "summary": "Sent the club pitch deck; says no rush on feedback.",
             "needs_reply": false, "received_ts": at(9.8), "reply_ref": [String: String]()],
            ["id": "t-lee", "source": "mail", "account": me, "who": "Prof. Lee", "title": "Thesis meeting moved",
             "summary": "Moved your thesis meeting to Tuesday at 2 PM.",
             "needs_reply": false, "received_ts": at(8.5),
             "reply_ref": ["message_id": "<lee-4@ucsc.edu>", "account": me, "account_id": "acct-1"]],
            ["id": "t-hack", "source": "mail", "account": judgeAccount, "who": "Hackathon Team", "title": "Judging schedule",
             "summary": "Judging starts at noon in Hall B; each demo gets five minutes plus questions.",
             "needs_reply": false, "received_ts": at(8),
             "reply_ref": ["message_id": "<hack-1@gmail.com>", "account": judgeAccount]],
        ]
        return [
            "state": working ? "working" : "ready", "as_of": at(10.4), "progress": ["done": 12, "total": 40],
            "accounts": [me, judgeAccount], "owner": ["id": "me", "label": "Adi"],
            "lede": "Hack Day fills your midday, and Worksheet 3 is the one thing to get ahead of.",
            "summary": "Hack Day runs 11:45 to 2 with Hacktoberfest at noon. Worksheet 3 for STAT 5 is due tomorrow, Ed is waiting on an exam reply, and Mom wants to talk tomorrow.",
            "stats": ["read": 49, "found": 3, "drafted": 1, "as_of": at(10.4)],
            "cards": working ? Array(cards.prefix(1)) : cards, "todos": todos, "threads": threads,
        ]
    }

    /// Without `proactive` (the default) this is the Brief from before the
    /// proactive engine, which tests/BriefTimeViewChecks.swift exercises.
    static func fixturePlan(day: String, dayStart: Double, proactive: ProactiveFixture = .none) throws -> Data {
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
        let blocks = proactive == .none ? [
            block("lunch", "Lunch with Sam", 11.5, 12.5, "calendar"),
            block("lecture", "CSE 101 lecture", 14, 15.25, "calendar"),
            block("club", "Startup club meeting", 18, 19, "calendar"),
            block("pa2-a", "Work on PA 2", 20, 21.5, "suggestion", evidence: "pa2"),
        ] : [
            block("hackday", "Hack Day", 11.75, 14, "calendar"),
            block("hacktober", "Hacktoberfest", 12, 13, "calendar"),
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
