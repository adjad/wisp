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
        // WISP_NOTCH_QA_PAGES=1: only Permissions and Settings, measured (NotchQAPages).
        if ProcessInfo.processInfo.environment["WISP_NOTCH_QA_PAGES"] == "1" {
            NotchQAPages.run(outDir: outDir)
            return
        }
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
        func dayModel(_ proactive: ProactiveFixture, study: Bool = false) -> TodayModel {
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
                                      proactive: proactive, study: study)
                return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            })
        }
        let today = dayModel(.ready)
        let working = dayModel(.working)
        let legacy = dayModel(.none)
        // The same day with school work from the Study tab: study blocks, school
        // deadlines and the Brief's SCHOOL line (only the "-study" scenes).
        let schoolDay = dayModel(.ready, study: true)
        // The Brief from before the proactive engine, with the same school work.
        let schoolLegacy = dayModel(.none, study: true)
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
                    ? ["ok": true, "draft": "Hi Sam,\n\nThursday at 3 works for me. See you then.\n\nThanks,\nAlex",
                       "to": ["sam.rivera@example.com"], "cc": [], "subject": "Re: Coffee chat next week?", "account": "me",
                       "envelope": "qa", "can_send": true]
                    : ["ok": true, "status": "sent"]
                return (try JSONSerialization.data(withJSONObject: body),
                        HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }, onDismiss: close))
        }
        let scenes: [(name: String, tab: NotchTab, view: () -> AnyView)] = [
            ("brief", .brief, { AnyView(NotchBriefView(model: model, today: today, actions: state())) }),
            ("brief-wide", .brief, { AnyView(NotchBriefView(model: model, today: today, actions: state(), wide: true)) }),
            // R6 side by side with school work, and with nothing in either zone.
            ("brief-wide-study", .brief, { AnyView(NotchBriefView(model: model, today: schoolDay, actions: state(), wide: true)) }),
            ("brief-wide-empty", .brief, { AnyView(NotchBriefView(model: model, today: blockedProactive, actions: state(), wide: true)) }),
            ("brief-todos", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true,
                                                             actions: state { $0.qaReset(openRow: "todos") })) }),
            ("brief-threads", .brief, { AnyView(NotchBriefView(model: model, today: today, uncapped: true,
                                                               actions: state { $0.qaReset(expanded: ["t-sam"], replying: "t-sam") })) }),
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
            ("brief-study", .brief, { AnyView(NotchBriefView(model: model, today: schoolDay, actions: state())) }),
            ("day-study", .day, { AnyView(NotchDayView(model: model, today: schoolDay)) }),
            ("brief-legacy-study", .brief, { AnyView(NotchBriefView(model: model, today: schoolLegacy, open: nil)) }),
        ] + NotchQASetup.scenes() + panelScenes(model: model, today: today, state: state)
            + studyScenes(model: model)
        for scene in scenes {
            let name = scene.name
            // The Brief's two columns render at the wide panel's width.
            let sceneWidth = name.hasPrefix("brief-wide") ? SurfaceLayout.wideExpandedWidth : width
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: sceneWidth, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                scene.view()
            }
            .padding(EdgeInsets(top: 18, leading: 22, bottom: 16, trailing: 22))
            .frame(width: sceneWidth, alignment: .topLeading)
            .background(Theme.surface)
            .environment(\.colorScheme, .dark)
            let host = NSHostingView(rootView: content)
            window.contentView = host
            window.setContentSize(host.fittingSize)
            if !offscreen { window.makeKeyAndOrderFront(nil) }
            windows[name] = window
        }
        quickSettings(width: width, model: model, offscreen: offscreen)
        permissionPages(width: width, offscreen: offscreen)
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
            // The "-study" scenes' days load like the rest (they rendered "Starting Wisp…" before).
            for school in [schoolDay, schoolLegacy] {
                await school.refresh()
                await school.refreshUpcoming()
                await school.refreshStoredBrief()
            }
            let deadline = Date().addingTimeInterval(5)
            @MainActor func populated() -> Bool {
                today.currentPlan?.workspace?.proactive?.cards.count == 3 && today.upcomingReply != nil && today.storedBriefChecked &&
                working.currentPlan?.workspace?.proactive?.isWorking == true &&
                legacy.currentPlan?.workspace != nil && legacy.currentPlan?.workspace?.proactive == nil &&
                dayOne.currentPlan != nil && blocked.currentPlan?.unreadSources.count == 4 &&
                blockedProactive.currentPlan?.workspace?.proactive?.hasReadNothing == true &&
                starting.currentPlan == nil && starting.error.isEmpty &&
                attention.items.count == 1 && stale.asks.count == 2 &&
                schoolDay.currentPlan?.study?.upcoming.isEmpty == false &&
                schoolLegacy.currentPlan?.study?.upcoming.isEmpty == false
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
                if let actions = panelReplies[name] {
                    actions.replying = "t-sam"
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    window.setContentSize(view.fittingSize)
                    view.layoutSubtreeIfNeeded()
                    try? await Task.sleep(nanoseconds: 400_000_000)
                }
                if let limit = panelLimits[name] {
                    // The panel never passes the display's room, and what it holds
                    // past the cap is reachable by scrolling to the end.
                    precondition(view.bounds.height <= limit + 0.5,
                                 "\(name): \(view.bounds.height) pt passes the panel's \(limit) pt")
                    let scroll = tabScrollView(in: view)
                    precondition(scroll != nil, "\(name): Brief must sit in a scroll view")
                    if let scroll, let document = scroll.documentView {
                        let overflow = document.frame.height - scroll.contentView.bounds.height
                        precondition(overflow > 0, "\(name): the fixture must overflow the cap")
                        if name.hasSuffix("-end") {
                            scroll.contentView.scroll(to: NSPoint(x: 0, y: document.isFlipped ? overflow : 0))
                            scroll.reflectScrolledClipView(scroll.contentView)
                            try? await Task.sleep(nanoseconds: 300_000_000)
                        }
                        print("\(name): cap \(Int(scroll.frame.height)) pt, content \(Int(document.frame.height)) pt")
                    }
                }
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
    /// The panel scenes' tallest allowed render, by scene name.
    private static var panelLimits: [String: CGFloat] = [:]
    /// Panel scenes whose reply composer opens just before capture.
    private static var panelReplies: [String: ProactiveActions] = [:]

    /// The Brief as the panel holds it on a 14" MacBook Pro's default (982 pt)
    /// and larger-text (878 pt) screens: a header-sized band, then the tab's
    /// capped scroll (OverlayView.tabScroll), with a reply composer and a draft
    /// open and every thread shown, so it overflows. "-end" is scrolled to the end;
    /// "-reveal" opens the composer after layout, which scrolls it into sight.
    static func panelScenes(model: OverlayModel, today: TodayModel,
                            state: @escaping ((ProactiveActions) -> Void) -> ProactiveActions)
        -> [(name: String, tab: NotchTab, view: () -> AnyView)] {
        let header: CGFloat = 30
        var scenes: [(name: String, tab: NotchTab, view: () -> AnyView)] = []
        for (w, h) in [(1512, 982), (1352, 878)] as [(CGFloat, CGFloat)] {
            let side = (w - 185) / 2
            let layout = SurfaceLayout.make(for: DisplayMetrics(
                frame: CGRect(x: 0, y: 0, width: w, height: h),
                visibleFrame: CGRect(x: 0, y: 0, width: w, height: h - 32),
                safeAreaTop: 32, auxLeftWidth: side, auxRightWidth: side, isBuiltIn: true))
            for suffix in ["", "-end", "-reveal"] {
                let name = "brief-panel-\(Int(h))\(suffix)"
                panelLimits[name] = layout.maxPanelHeight
                scenes.append((name, .brief, {
                    // "-reveal" opens the composer only once the panel is laid out,
                    // as a Reply tap does, and must scroll it fully into sight.
                    let reveal = suffix == "-reveal"
                    let actions = state {
                        $0.qaReset(openDraft: "card-ed", expanded: ["t-sam"], replying: reveal ? nil : "t-sam",
                                   hero: "card-ed")
                        $0.showAllThreads = true
                    }
                    if reveal { panelReplies[name] = actions }
                    return AnyView(VStack(alignment: .leading, spacing: 0) {
                        Text("HEADER (fixed) · \(Int(w))×\(Int(h)) pt · panel ≤ \(Int(layout.maxPanelHeight))")
                            .font(.wisp(size: 11, design: .monospaced)).foregroundStyle(Theme.textMuted)
                            .frame(maxWidth: .infinity, minHeight: header, maxHeight: header, alignment: .leading)
                        CappedScroll(cap: layout.tabScrollCap(headerHeight: header), reveals: true) {
                            NotchBriefView(model: model, today: today, actions: actions)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .padding(.top, SurfaceLayout.tabTopPadding)
                    }
                    // The driver pads every scene 18 pt; the panel's top inset is the notch's.
                    .padding(.top, layout.contentTopInset - 18))
                }))
            }
        }
        return scenes
    }

    /// The outermost vertical scroll view: the tab's, not an opened row's.
    private static func tabScrollView(in view: NSView) -> NSScrollView? {
        if let scroll = view as? NSScrollView { return scroll }
        for sub in view.subviews { if let found = tabScrollView(in: sub) { return found } }
        return nil
    }

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
                                  section: unconfirmed ? .brief : .general,
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

    /// The permission page, each state a row can be in. `narrow` is the panel on
    /// the smallest screen it opens on (SurfaceLayout's 320 pt floor), `light` the
    /// same page with the Mac in light appearance, which the notch's tokens ignore.
    static func permissionPages(width: CGFloat, offscreen: Bool = false) {
        typealias Item = FirstRunPermissions.Item
        var every: [Item: FirstRunPermissions.State] = [.calendar: .notAsked, .reminders: .notAllowed, .mail: .allowed,
            .messages: .notAsked, .fullDiskAccess: .notAllowed, .contacts: .notAsked, .notifications: .allowed,
            .screenRecording: .notAsked, .accessibility: .notAllowed]
        var off = every
        off[.fullDiskAccess] = .notAllowed
        every[.calendar] = .allowed
        let scenes: [(name: String, focus: Item?, states: [Item: FirstRunPermissions.State], asking: Set<Item>,
                      waiting: Set<Item>, readable: Bool, width: CGFloat, scheme: ColorScheme)] = [
            ("permission-calendar", .calendar, off, [], [], true, width, .dark),
            ("permission-calendar-allowed", .calendar, every, [], [], true, width, .dark),
            ("permission-fda", .fullDiskAccess, off, [], [], true, width, .dark),
            ("permission-mail-waiting", .mail, off.merging([.mail: .notAsked]) { $1 }, [], [.mail], true, width, .dark),
            ("permission-asking", .reminders, off.merging([.reminders: .notAsked]) { $1 }, [.reminders], [], true, width, .dark),
            ("permission-contacts-waits", .contacts, off, [], [], false, width, .dark),
            ("permission-list", nil, off, [], [], true, width, .dark),
            ("permission-fda-narrow", .fullDiskAccess, off, [], [], true, 320, .dark),
            ("permission-screen-narrow", .screenRecording, off, [], [], true, 320, .dark),
            ("permission-calendar-light", .calendar, off, [], [], true, width, .light),
        ]
        for scene in scenes {
            let pageModel = OverlayModel()
            pageModel.tab = .brief
            pageModel.permissionPage.opened(focus: scene.focus)
            pageModel.permissionPage.access.qaShow(states: scene.states, asking: scene.asking, waiting: scene.waiting,
                                                   messagesReadable: scene.readable)
            let window = NSWindow(contentRect: NSRect(x: 80, y: 80, width: scene.width, height: 760),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            let content = VStack(alignment: .leading, spacing: 0) {
                Color.clear.frame(height: 44)
                NotchPermissionsView(model: pageModel).padding(.top, 12)
            }
            .padding(EdgeInsets(top: 18, leading: 22, bottom: 16, trailing: 22))
            .frame(width: scene.width, alignment: .topLeading)
            .background(Theme.surface)
            .environment(\.colorScheme, scene.scheme)
            let host = NSHostingView(rootView: content)
            window.contentView = host
            window.setContentSize(host.fittingSize)
            if !offscreen { window.makeKeyAndOrderFront(nil) }
            windows[scene.name] = window
            permissionModels.append(pageModel)
        }
    }
    /// The page's models outlive the capture, as the windows do.
    private static var permissionModels: [OverlayModel] = []

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
                "state": "ready", "as_of": at(10.3), "accounts": [String](), "owner": ["id": "me", "label": "Alex"],
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

    /// A demo day: Hack Day and Hacktoberfest at midday, a QNT 210 worksheet to
    /// block time for, Dana's exam email, "Jo: let's talk tmrw", a recruiter's
    /// coffee chat and a Trader Joe's reminder; three cards, seven to-dos and
    /// six thread summaries (one from a linked judge account).
    static func fixtureProactive(dayStart: Double, working: Bool) -> [String: Any] {
        func at(_ h: Double) -> Double { dayStart + h * 3600 }
        let me = "me"
        let cards: [[String: Any]] = [
            ["id": "card-stat", "meta": "Due tomorrow · Reminders", "account": me,
             "title": "Worksheet 3 for QNT 210",
             "body": "You're free 2–3 PM after Hack Day. I'll hold that hour.",
             "why": "It's due tomorrow at 11:59 PM and nothing on your day covers it.",
             "sources": [["kind": "reminders", "ref": "rem:stat5", "who": "", "quote": "QNT 210 worksheet 3"]],
             "action": ["method": "block_time", "button": "", "draft": "", "recipient": "",
                        "slot": ["start": at(14), "end": at(15)], "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-ed", "meta": "Waiting 1 day · Mail", "account": me,
             "title": "Reply to Dana's exam email",
             "body": "Dana asked if you can take Exam 1 early on Tuesday. You're free 9–11 AM, so I've drafted a yes.",
             "why": "Dana asked yesterday and you haven't replied.",
             "sources": [["kind": "mail", "ref": "mail:ed-1", "who": "Dana Okafor",
                          "quote": "Would you be able to take Exam 1 early, Tuesday morning?"]],
             // Dana's thread's own message: the Brief shows this reply as the card only.
             "reply_ref": ["message_id": "<dana-1@example.edu>", "account": me, "account_id": "acct-1"],
             "action": ["method": "email_reply", "button": "Send the yes?",
                        "recipient": "Dana Okafor <dana.okafor@example.edu>",
                        "draft": "Hi Dana,\n\nYes, Tuesday morning works. I can come in at 9. Thanks for setting it up.\n\nBest,\nAlex",
                        "event": NSNull(), "envelope": "opaque"]],
            ["id": "card-exam", "meta": "Wednesday · Mail", "account": me,
             "title": "Remind me: Exam 1 Wed",
             "body": "Dana's email puts Exam 1 on Wednesday at 10 AM in Room 321. I'll remind you Tuesday at 7 PM to review.",
             "why": "An exam date in your mail isn't on your calendar or reminders.",
             "sources": [["kind": "mail", "ref": "mail:ed-1", "who": "Dana Okafor", "quote": "Exam 1 is Wednesday at 10 in Room 321"]],
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
            todo("todo-read-cse", "Readings", "reminders", who: "CMP 140", due: 17, label: "5 PM"),
            todo("todo-read-stat", "Readings", "reminders", who: "QNT 210", due: 24 + 12, label: "Tomorrow"),
            todo("todo-stat", "Worksheet 3", "reminders", who: "QNT 210", due: 24 + 23.98, label: "Tomorrow", card: "card-stat"),
            todo("todo-jo", "Call Jo", "messages", who: "Jo Tanaka", quote: "let's talk tmrw", due: 24, label: "Tomorrow", kind: "call"),
            todo("todo-tj", "Go to Trader Joe's", "reminders", due: 17.5, label: "5:30 PM", kind: "errand"),
            todo("todo-sam", "Coffee chat with Sam", "mail", who: "Sam Rivera, recruiter",
                 quote: "30-minute coffee chat next week?", due: nil, label: "This week"),
            todo("todo-hack", "Hack Day", "calendar", due: 11.75, label: "11:45 AM", kind: "event"),
        ]
        let threads: [[String: Any]] = [
            ["id": "t-ed", "source": "mail", "account": me, "who": "Dana", "title": "Exam 1 timing",
             "summary": "Asks if you can take Exam 1 early on Tuesday morning; the exam itself is Wednesday at 10.",
             "needs_reply": true, "received_ts": at(-14),
             "reply_ref": ["message_id": "<dana-1@example.edu>", "account": me, "account_id": "acct-1"]],
            ["id": "t-jo", "source": "messages", "account": me, "who": "Jo Tanaka", "title": "",
             "summary": "Wants to talk tomorrow; nothing urgent, just catching up.",
             "needs_reply": true, "received_ts": at(-15), "reply_ref": [String: String]()],
            ["id": "t-sam", "source": "mail", "account": me, "who": "Sam Rivera", "title": "Coffee chat next week?",
             "summary": "A recruiter confirmed your 30-minute coffee chat for Thursday at 3; a calendar invite is coming.",
             "needs_reply": false, "received_ts": at(-36),
             "reply_ref": ["message_id": "<sam-1@example.com>", "account": me, "account_id": "acct-1"]],
            ["id": "t-lee-park", "source": "messages", "account": me, "who": "Lee Park", "title": "",
             "summary": "Sent the club pitch deck; says no rush on feedback.",
             "needs_reply": false, "received_ts": at(9.8), "reply_ref": [String: String]()],
            ["id": "t-lee", "source": "mail", "account": me, "who": "Prof. Marlow", "title": "Thesis meeting moved",
             "summary": "Moved your thesis meeting to Tuesday at 2 PM.",
             "needs_reply": false, "received_ts": at(8.5),
             "reply_ref": ["message_id": "<marlow-4@example.edu>", "account": me, "account_id": "acct-1"]],
            ["id": "t-hack", "source": "mail", "account": judgeAccount, "who": "Hackathon Team", "title": "Judging schedule",
             "summary": "Judging starts at noon in Hall B; each demo gets five minutes plus questions.",
             "needs_reply": false, "received_ts": at(8),
             "reply_ref": ["message_id": "<hack-1@gmail.com>", "account": judgeAccount]],
        ]
        return [
            "state": working ? "working" : "ready", "as_of": at(10.4), "progress": ["done": 12, "total": 40],
            "accounts": [me, judgeAccount], "owner": ["id": "me", "label": "Alex"],
            "lede": "Hack Day fills your midday, and Worksheet 3 is the one thing to get ahead of.",
            "summary": "Hack Day runs 11:45 to 2 with Hacktoberfest at noon. Worksheet 3 for QNT 210 is due tomorrow, Dana is waiting on an exam reply, and Jo wants to talk tomorrow.",
            "stats": ["read": 49, "found": 3, "drafted": 1, "as_of": at(10.4)],
            "cards": working ? Array(cards.prefix(1)) : cards, "todos": todos, "threads": threads,
        ]
    }

    /// Without `proactive` (the default) this is the Brief from before the
    /// proactive engine, which tests/BriefTimeViewChecks.swift exercises.
    /// With `study` the day also carries the Study tab's blocks, school
    /// deadlines and SCHOOL summary (the "-study" scenes); without it the plan
    /// is exactly what it was before Study.
    static func fixturePlan(day: String, dayStart: Double, proactive: ProactiveFixture = .none,
                            study withStudy: Bool = false) throws -> Data {
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
        // Study (service/study/day_hook.py): two study blocks before what they're
        // for, both deadlines, and the summary behind the Brief's SCHOOL line.
        let night = 23 + 59.0 / 60
        // Lab 5's block and deadline name the slides that cover it, and the
        // deadline offers a 15-minute review of them.
        let listSlides: [[String: Any]] = [["source_id": "qa-deck-6", "deck": "Lecture 6: Lists", "pages": [2, 3, 4, 5, 6]]]
        var lab5Block = block("study-block:qa:lab5", "Study: Lab 5: Lists", 16.5, 17.25, "study")
        lab5Block["slides"] = listSlides
        lab5Block["covers_text"] = "Slides 2–6 of Lecture 6 cover it."
        // "Add to calendar": the service sets `added` on each study block. Lab 5's
        // is already on the calendar (Undo); Problem set 6 offers the add.
        lab5Block["added"] = true
        lab5Block["event_source_id"] = "qa-cal-event-lab5"
        var pset6Block = block("study-block:qa:pset6", "Study: Problem set 6", 15.5, 16.25, "study")
        pset6Block["added"] = false
        pset6Block["event_source_id"] = NSNull()
        let studyBlocks = [pset6Block, lab5Block]
        let studyDeadlines: [[String: Any]] = [
            ["id": "study:qa:pset6", "title": "Problem set 6 · MATH 110", "due_ts": at(17), "source": "study"],
            ["id": "study:qa:lab5", "title": "Lab 5: Lists · CS 101", "due_ts": at(night), "source": "study",
             "slides": listSlides, "covers_text": "Slides 2–6 of Lecture 6 cover it.",
             "review": ["minutes": 15, "pages": [2, 3, 4, 5, 6].map { ["source_id": "qa-deck-6", "page": $0] }]],
        ]
        let study: [String: Any] = [
            "due_today": [
                ["id": "qa:pset6", "title": "Problem set 6", "course_name": "MATH 110", "due_ts": at(17),
                 "all_day": false, "kind": "assignment", "status": "upcoming"],
                ["id": "qa:lab5", "title": "Lab 5: Lists", "course_name": "CS 101", "due_ts": at(night),
                 "all_day": false, "kind": "assignment", "status": "upcoming"],
            ],
            "due_tomorrow_count": 2, "due_week_count": 6, "missing_count": 1,
            "next_test": ["id": "qa:quiz3", "title": "Quiz 3: Loops", "course_name": "CS 101", "due_ts": at(24 + 10)],
            "line": "2 due today: Problem set 6 (MATH 110), Lab 5: Lists (CS 101)", "sample": false,
            // This week's open work, soonest first (today's two among it): the
            // Brief lists three, Day lists what isn't due today. One course has
            // no short label, so its row names the course.
            "upcoming": [
                ["id": "qa:pset6", "title": "Problem set 6", "course_name": "MATH 110", "course_label": "MATH 110",
                 "due_ts": at(17), "all_day": false, "kind": "assignment", "status": "upcoming"],
                ["id": "qa:lab5", "title": "Lab 5: Lists", "course_name": "CS 101", "course_label": "CS 101",
                 "due_ts": at(night), "all_day": false, "kind": "assignment", "status": "upcoming"],
                ["id": "qa:quiz3", "title": "Quiz 3: Loops", "course_name": "CS 101", "course_label": "CS 101",
                 "due_ts": at(24 + 10), "all_day": false, "kind": "quiz", "status": "upcoming"],
                ["id": "qa:reading4", "title": "Reading response 4: Ways of seeing", "course_name": "Writing and Rhetoric: Voices and Global Perspectives",
                 "course_label": "", "due_ts": at(24 + night), "all_day": false, "kind": "reading", "status": "upcoming"],
                ["id": "qa:pset7", "title": "Problem set 7", "course_name": "MATH 110", "course_label": "MATH 110",
                 "due_ts": at(24 * 3), "all_day": true, "kind": "assignment", "status": "upcoming"],
                ["id": "qa:proposal", "title": "Project proposal", "course_name": "CS 101", "course_label": "CS 101",
                 "due_ts": at(24 * 5 + 12), "all_day": false, "kind": "project", "status": "upcoming"],
            ],
            "upcoming_more": 1,
            // What Study got ready: a quiz it made, and an assignment's slides
            // (three decks, so the line says "+1"); then two weak topics.
            "ready": [
                ["id": "ready:qa:quiz3", "kind": "quiz", "assignment_id": "qa:quiz3", "course": "CS 101",
                 "title": "Quiz 3: Loops", "due_ts": at(24 + 10),
                 "text": "CS 101 quiz tomorrow. I made a 10-question quiz from weeks 5 and 6.", "material_id": "m-quiz",
                 "slides": [["source_id": "qa-deck-5", "deck": "Week 5: Loops", "pages": [2, 3, 4, 5]],
                            ["source_id": "qa-deck-w6", "deck": "Week 6: While loops", "pages": [1, 2, 6]]]],
                ["id": "ready:qa:pa3", "kind": "assignment", "assignment_id": "qa:pa3", "course": "CS 101",
                 "title": "PA 3", "due_ts": at(24 * 4 + 23),
                 "text": "PA 3 is due Friday. Lectures 7 and 8 cover it.", "material_id": NSNull(),
                 "slides": [["source_id": "qa-deck-7", "deck": "Lecture 7: Pointers", "pages": [4, 5, 6, 7, 8, 9]],
                            ["source_id": "qa-deck-8", "deck": "Lecture 8: Arrays", "pages": [2, 5, 7]],
                            ["source_id": "qa-deck-9", "deck": "Lecture 9: Strings", "pages": [1]]]],
            ],
            "weak": [
                ["id": "weak:qa:pointers", "course": "CS 101", "topic": "Pointers", "count": 3,
                 "text": "You missed 3 on pointers; here are those two slides again.",
                 "slides": [["source_id": "qa-deck-7", "deck": "Lecture 7: Pointers", "pages": [6, 8]]]],
                ["id": "weak:qa:limits", "course": "MATH 110", "topic": "Limits", "count": 2,
                 "text": "You missed 2 on limits.", "slides": []],
            ],
        ]
        var payload: [String: Any] = [
            "day": day, "timezone": zone.identifier, "generated_at": at(13.1), "revision": 3, "provisional": false,
            "warnings": [], "tasks": [], "blocks": [], "deadlines": [],
            "all_day": [["id": "allday", "title": "PA 2 [CSE-20-04]", "start": at(0), "kind": "calendar", "warnings": []]],
            "unscheduled": [],
            "sources": ["calendar": ["ready": true, "last_sync": at(13), "reason": "Up to date"]],
            "preferences": ["start_minute": 540, "end_minute": 1320, "not_before": NSNull()],
            "workspace": workspace,
        ]
        if withStudy {
            payload["blocks"] = studyBlocks
            payload["deadlines"] = studyDeadlines
            payload["study"] = study
        }
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

// MARK: - Study (docs/STUDY_TAB.md)

/// The Study tab against synthetic coursework: four courses, eight assignments
/// spread over Missing, Today, Tomorrow and This week (by this Mac's clock, as
/// the tab groups them), four saved materials about Python lists and loops, and
/// one full material of each kind. Each scene has its own StudyModel whose
/// transport answers here; nothing reaches the service, a browser or a key.
extension NotchQADriver {
    static func studyScenes(model: OverlayModel) -> [(name: String, tab: NotchTab, view: () -> AnyView)] {
        let midnight = Calendar.current.startOfDay(for: Date()).timeIntervalSince1970
        let state = studyState(midnight: midnight)
        let empty: [String: Any] = [
            "connector": studyConnector(ready: false), "sample": false, "last_import": NSNull(),
            "courses": [], "assignments": [], "materials": [],
        ]
        // The panel on a 14" MacBook Pro's default screen: a header-sized band, then
        // the tab capped at half the screen (SurfaceLayout.studyContentCap).
        let header: CGFloat = 30
        let side: CGFloat = (1512 - 185) / 2
        let layout = SurfaceLayout.make(for: DisplayMetrics(
            frame: CGRect(x: 0, y: 0, width: 1512, height: 982),
            visibleFrame: CGRect(x: 0, y: 0, width: 1512, height: 950),
            safeAreaTop: 32, auxLeftWidth: side, auxRightWidth: side, isBuiltIn: true))
        func scene(_ name: String, state: [String: Any], opened: String? = nil, deck: Bool = false,
                   connector: Bool = false) -> (name: String, tab: NotchTab, view: () -> AnyView) {
            (name, .study, {
                let base = studyTransport(state: state, midnight: midnight)
                // The deck scene also answers the pages and settings routes, so a
                // refresh in the view keeps the same deck and toggle.
                let transport: StudyModel.Transport = deck ? { request in
                    let path = request.url?.path ?? ""
                    func reply(_ object: Any) throws -> (Data, URLResponse) {
                        (try JSONSerialization.data(withJSONObject: object),
                         HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
                    }
                    if path.hasSuffix("/pages") { return try reply(["pages": studyDeckPages()]) }
                    if path == "/study/settings" { return try reply(["auto_prepare": true]) }
                    return try await base(request)
                } : base
                let study = StudyModel(transport: transport, reader: { [] })
                let decoded = try? JSONDecoder().decode(StudyState.self, from: JSONSerialization.data(withJSONObject: state))
                let material = opened.flatMap { id in
                    (try? JSONSerialization.data(withJSONObject: studyMaterial(id, midnight: midnight)))
                        .flatMap { try? JSONDecoder().decode(StudyMaterial.self, from: $0) }
                }
                precondition(decoded != nil && (opened == nil || material != nil), "\(name): the Study fixture must decode")
                study.show(state: decoded, opened: material)
                study.showingConnector = connector
                if deck, let source = decoded?.sources.first(where: { $0.id == "src-7" }) {
                    let pages = (try? JSONSerialization.data(withJSONObject: ["pages": studyDeckPages()]))
                        .flatMap { try? JSONDecoder().decode(StudyDeckPages.self, from: $0) }?.pages ?? []
                    precondition(pages.count == 12, "\(name): the deck fixture must decode")
                    study.show(deck: StudyOpenDeck(source: source, pages: pages, loading: false), autoPrepare: true)
                }
                return AnyView(VStack(alignment: .leading, spacing: 0) {
                    Text("HEADER (fixed) · 1512×982 pt · Study panel ≤ \(Int(layout.maxStudyPanelHeight))")
                        .font(.wisp(size: 11, design: .monospaced)).foregroundStyle(Theme.textMuted)
                        .frame(maxWidth: .infinity, minHeight: header, maxHeight: header, alignment: .leading)
                    NotchStudyView(model: model, study: study, cap: layout.studyContentCap(headerHeight: header))
                        .padding(.top, SurfaceLayout.tabTopPadding)
                }
                // The driver pads every scene 18 pt; the panel's top inset is the notch's.
                .padding(.top, layout.contentTopInset - 18))
            })
        }
        return [
            scene("study", state: state),
            scene("study-quiz", state: state, opened: "m-quiz"),
            scene("study-cards", state: state, opened: "m-cards"),
            scene("study-guide", state: state, opened: "m-guide"),
            scene("study-lesson", state: state, opened: "m-lesson"),
            scene("study-setup", state: empty, connector: true),
            scene("study-deck", state: studyDeckState(state, midnight: midnight), deck: true),
            scene("study-sources", state: studySourcesState(state, midnight: midnight)),
        ]
    }

    /// The Study state plus one Drive deck read as pages (made-up course data).
    static func studyDeckState(_ state: [String: Any], midnight: Double) -> [String: Any] {
        var withDeck = state
        withDeck["sources"] = [[
            "id": "src-7", "course_id": "qa:cs101", "kind": "drive", "title": "Lecture 7: Pointers",
            "url": "https://docs.google.com/presentation/d/qa-lecture-7", "chars": 9400,
            "created_at": midnight - 2 * 86400, "pages": 12, "deck_status": "ready",
        ]]
        return withDeck
    }

    /// The Study state with one deck in each read state: ready, still reading,
    /// and failed with the service's reason (made-up course data).
    static func studySourcesState(_ state: [String: Any], midnight: Double) -> [String: Any] {
        var withSources = state
        // One assignment and one made item, so the sources sit inside the panel's cap.
        withSources["assignments"] = Array((state["assignments"] as? [[String: Any]] ?? []).prefix(1))
        withSources["materials"] = Array((state["materials"] as? [[String: Any]] ?? []).prefix(1))
        withSources["sources"] = [
            ["id": "src-7", "course_id": "qa:cs101", "kind": "drive", "title": "Lecture 7: Pointers",
             "url": "https://docs.google.com/presentation/d/qa-lecture-7", "chars": 9400,
             "created_at": midnight - 2 * 86400, "pages": 12, "deck_status": "ready"],
            ["id": "src-8", "course_id": "qa:cs101", "kind": "drive", "title": "Lecture 8: Structs",
             "url": "https://docs.google.com/presentation/d/qa-lecture-8", "chars": 7100,
             "created_at": midnight - 86400, "pages": 0, "deck_status": "reading"],
            ["id": "src-9", "course_id": "qa:cs101", "kind": "drive", "title": "Lecture 9: Linked lists",
             "url": "https://docs.google.com/presentation/d/qa-lecture-9", "chars": 6200,
             "created_at": midnight - 3600, "pages": 0, "deck_status": "failed",
             "deck_error": "Google took too long to send the slides."],
        ]
        return withSources
    }

    /// Twelve pages of the made-up "Lecture 7: Pointers" deck.
    static func studyDeckPages() -> [[String: Any]] {
        let titles = ["Pointers", "Memory as boxes", "Addresses", "The & operator", "The * operator",
                      "Null pointers", "Pointer arithmetic", "Arrays and pointers", "Passing by pointer",
                      "Dangling pointers", "Common mistakes", "Summary"]
        return titles.enumerated().map { index, title -> [String: Any] in
            var page: [String: Any] = [
                "page": index + 1, "title": title,
                "points": ["\(title): the main idea in one line", "A short example in C"],
                "topics": ["pointers"],
            ]
            if index == 0 { page["figure"] = "A box and arrow diagram" }
            return page
        }
    }

    /// Answers the routes the tab calls; a write is acknowledged and never kept.
    static func studyTransport(state: [String: Any], midnight: Double) -> StudyModel.Transport {
        { request in
            let path = request.url?.path ?? ""
            func reply(_ object: Any, status: Int = 200) throws -> (Data, URLResponse) {
                (try JSONSerialization.data(withJSONObject: object),
                 HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
            }
            let prefix = "/study/materials/"
            if path.hasPrefix(prefix) {
                let rest = String(path.dropFirst(prefix.count))
                if rest.hasSuffix("/attempt") { return try reply(["best_score": 0.83]) }
                if request.httpMethod == "DELETE" { return try reply(["deleted": true]) }
                let id = rest.removingPercentEncoding ?? rest
                if ["m-guide", "m-quiz", "m-lesson", "m-cards"].contains(id) {
                    return try reply(studyMaterial(id, midnight: midnight))
                }
                return try reply(["detail": "not_found", "message": "That material is gone."], status: 404)
            }
            if path == "/study/generate" { return try reply(studyMaterial("m-quiz", midnight: midnight)) }
            if path == "/study/connector" { return try reply(state["connector"] ?? [String: Any]()) }
            if path == "/study/import" {
                return try reply(["courses": 0, "assignments": 0, "pages": [], "state": state])
            }
            return try reply(state)
        }
    }

    static func studyConnector(ready: Bool) -> [String: Any] {
        [
            "provider": "openrouter", "model_id": "google/gemini-3.8-flash", "ready": ready,
            "source": ready ? "study" : "none",
            "providers": [
                ["id": "openrouter", "label": "OpenRouter", "default_model": "google/gemini-3.8-flash",
                 "models": [["id": "google/gemini-3.8-flash", "label": "Gemini 3.8 Flash"]],
                 "key_url": "https://openrouter.ai/keys", "key_hint": "sk-or-…"],
                ["id": "gemini", "label": "Gemini API", "default_model": "gemini-3.8-flash",
                 "models": [["id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"]],
                 "key_url": "https://aistudio.google.com/apikey", "key_hint": "AIza…"],
            ],
        ]
    }

    static func studyState(midnight: Double) -> [String: Any] {
        func at(_ days: Double, _ hour: Double) -> Double { midnight + days * 86400 + hour * 3600 }
        let night = 23 + 59.0 / 60
        let courses: [[String: Any]] = [
            ["id": "qa:cs101", "name": "Introduction to Programming", "code": "CS 101", "grade": "B+", "score": 87.5,
             "url": "https://canvas.example.edu/courses/101", "due_count": 3, "sample": false],
            ["id": "qa:math110", "name": "Calculus I", "code": "MATH 110",
             "grade": "B+", "score": 88.4, "url": "https://canvas.example.edu/courses/102", "due_count": 2, "sample": false],
            ["id": "qa:writ100", "name": "Writing and Rhetoric", "code": "WRIT 100", "grade": "A", "score": 95.0,
             "url": "https://canvas.example.edu/courses/103", "due_count": 2, "sample": false],
            ["id": "qa:phys120", "name": "Introduction to Physics I", "code": "PHYS 120", "grade": "B", "score": 84.1,
             "url": "https://canvas.example.edu/courses/104", "due_count": 1, "sample": false],
        ]
        func item(_ id: String, _ course: String, _ courseName: String, _ title: String, kind: String = "assignment",
                  due: Double, points: Double, status: String = "upcoming") -> [String: Any] {
            ["id": "qa:" + id, "course_id": course, "course_name": courseName, "title": title, "kind": kind,
             "due_ts": due, "all_day": false, "points": points, "score": NSNull(), "status": status,
             "url": "https://canvas.example.edu/assignments/" + id, "sample": false]
        }
        let assignments: [[String: Any]] = [
            item("lab4", "qa:cs101", "Introduction to Programming", "Lab 4: Strings", due: at(-2, night), points: 20,
                 status: "missing"),
            item("pset6", "qa:math110", "Calculus I", "Problem set 6",
                 due: at(0, 17), points: 30),
            item("lab5", "qa:cs101", "Introduction to Programming", "Lab 5: Lists", due: at(0, night), points: 20),
            item("quiz3", "qa:cs101", "Introduction to Programming", "Quiz 3: Loops", kind: "quiz",
                 due: at(1, 10), points: 10),
            item("essay", "qa:writ100", "Writing and Rhetoric", "Essay draft: rhetorical analysis", due: at(1, night),
                 points: 50),
            item("midterm", "qa:phys120", "Introduction to Physics I", "Midterm 1", kind: "exam", due: at(3, 14),
                 points: 100),
            item("reading7", "qa:math110", "Calculus I", "Reading: Section 3.4",
                 kind: "reading", due: at(4, night), points: 0),
            item("post5", "qa:writ100", "Writing and Rhetoric", "Discussion post 5", kind: "discussion",
                 due: at(5, night), points: 5),
        ]
        func summary(_ id: String, _ kind: String, _ title: String, count: Int, best: Double?, hoursAgo: Double) -> [String: Any] {
            ["id": id, "kind": kind, "title": title, "course_id": "qa:cs101", "assignment_id": "qa:lab5",
             "created_at": midnight + 10 * 3600 - hoursAgo * 3600, "count": count,
             "best_score": best.map { $0 as Any } ?? (NSNull() as Any), "sample": false]
        }
        let materials: [[String: Any]] = [
            summary("m-quiz", "quiz", "Lists and loops: practice quiz", count: 6, best: 0.83, hoursAgo: 1),
            summary("m-cards", "flashcards", "Python lists: flash cards", count: 8, best: nil, hoursAgo: 5),
            summary("m-lesson", "lesson", "Looping over a list, step by step", count: 4, best: nil, hoursAgo: 26),
            summary("m-guide", "guide", "Lab 5 study guide: lists and loops", count: 4, best: nil, hoursAgo: 50),
        ]
        return ["connector": studyConnector(ready: true), "sample": false, "last_import": midnight + 9 * 3600,
                "courses": courses, "assignments": assignments, "materials": materials]
    }

    /// One full material of each kind, written for CS 101's Lab 5.
    static func studyMaterial(_ id: String, midnight: Double) -> [String: Any] {
        let body: [String: Any]
        let kind: String
        let title: String
        switch id {
        case "m-guide":
            kind = "guide"; title = "Lab 5 study guide: lists and loops"
            body = [
                "summary": "A list holds values in order, can change after it is made, and is read by position starting at 0. Loops let you visit each item without writing the same line again, which is most of what Lab 5 asks for.",
                "sections": [
                    ["heading": "Making and reading lists", "points": [
                        "Square brackets make a list: scores = [88, 92, 75].",
                        "Indexing starts at 0, so scores[0] is 88 and scores[-1] is the last item, 75.",
                        "len(scores) is the number of items, here 3; the last index is always len(scores) - 1."]],
                    ["heading": "Changing a list", "points": [
                        "append(x) adds one item to the end; insert(i, x) puts it at position i.",
                        "pop() removes and returns the last item; remove(x) deletes the first item equal to x.",
                        "Lists are mutable: scores[1] = 95 changes the list in place. Strings and tuples are not."]],
                    ["heading": "Looping over a list", "points": [
                        "for score in scores: visits each item in order; you get the value, not the position.",
                        "for i in range(len(scores)): gives you positions, useful when you need to change items.",
                        "enumerate(scores) gives both: for i, score in enumerate(scores)."]],
                    ["heading": "Common mistakes", "points": [
                        "scores[len(scores)] raises IndexError: the last index is one less than the length.",
                        "Removing items from a list while looping over it skips items; loop over a copy instead.",
                        "b = a does not copy a list; both names point at the same list. Use a.copy() or a[:]."]],
                ],
                "key_terms": [
                    ["term": "index", "meaning": "An item's position in a list, counted from 0."],
                    ["term": "mutable", "meaning": "Can be changed after it is created, like a list."],
                    ["term": "slice", "meaning": "Part of a list, such as scores[1:3], which makes a new list."],
                    ["term": "iteration", "meaning": "One pass through the body of a loop."],
                ],
                "practice": [
                    "Write a loop that adds up the numbers in [4, 8, 15, 16, 23, 42] without using sum().",
                    "Given names = [\"Ana\", \"Bo\", \"Cy\"], print each name with its position, starting at 1.",
                    "Make a new list holding only the even numbers from range(1, 21).",
                ],
            ]
        case "m-quiz":
            kind = "quiz"; title = "Lists and loops: practice quiz"
            body = ["questions": [
                ["prompt": "What does nums[-1] give for nums = [3, 6, 9]?",
                 "choices": ["3", "6", "9", "An IndexError"], "answer": 2,
                 "why": "A negative index counts from the end, so -1 is the last item, 9."],
                ["prompt": "After nums = [1, 2] and nums.append([3, 4]), what is len(nums)?",
                 "choices": ["2", "3", "4", "It raises an error"], "answer": 1,
                 "why": "append adds its argument as one item, so the list becomes [1, 2, [3, 4]] with 3 items. extend would add 3 and 4 separately."],
                ["prompt": "How many times does the body of for i in range(2, 10, 3): run?",
                 "choices": ["2", "3", "4", "8"], "answer": 1,
                 "why": "range(2, 10, 3) gives 2, 5 and 8, then stops before 10: three passes."],
                ["prompt": "Which line makes a separate copy of the list a?",
                 "choices": ["b = a", "b = a.copy()", "b == a", "b = [a]"], "answer": 1,
                 "why": "b = a gives the same list a second name; a.copy() (or a[:]) makes a new list with the same items."],
                ["prompt": "What does this print? total = 0, then for x in [2, 4, 6]: total += x, then print(total)",
                 "choices": ["6", "12", "246", "[2, 4, 6]"], "answer": 1,
                 "why": "The loop adds 2, then 4, then 6 to total: 0 + 2 + 4 + 6 = 12."],
                ["prompt": "What is [n * n for n in range(4)]?",
                 "choices": ["[1, 4, 9, 16]", "[0, 1, 4, 9]", "[0, 1, 2, 3]", "[0, 2, 4, 6]"], "answer": 1,
                 "why": "range(4) is 0, 1, 2, 3, and squaring each gives 0, 1, 4, 9."],
            ]]
        case "m-lesson":
            kind = "lesson"; title = "Looping over a list, step by step"
            body = ["steps": [
                ["title": "A list is a row of numbered boxes",
                 "explain": "A list keeps values in order. Each one sits at a position called its index, and the first index is 0, not 1.",
                 "example": "pets = [\"cat\", \"dog\", \"fish\"]\nprint(pets[0])   # cat\nprint(pets[2])   # fish",
                 "check": ["prompt": "For pets = [\"cat\", \"dog\", \"fish\"], what is pets[1]?",
                           "choices": ["\"cat\"", "\"dog\"", "\"fish\""], "answer": 1,
                           "why": "Index 0 is \"cat\", so index 1 is the second item, \"dog\"."]],
                ["title": "A for loop visits every item",
                 "explain": "for name in pets: runs the indented lines once for each item, with name set to that item. You don't manage the index yourself.",
                 "example": "for name in pets:\n    print(\"I have a\", name)",
                 "check": ["prompt": "How many lines does that loop print?",
                           "choices": ["1", "2", "3"], "answer": 2,
                           "why": "The list has three items, so the body runs three times."]],
                ["title": "Keep a running total",
                 "explain": "Make a variable before the loop and update it inside. After the loop it holds the answer. This is the pattern behind sums, counts and averages.",
                 "example": "scores = [80, 90, 100]\ntotal = 0\nfor s in scores:\n    total += s\nprint(total / len(scores))   # 90.0",
                 "check": ["prompt": "Where must total = 0 go?",
                           "choices": ["Before the loop", "Inside the loop", "After the loop"], "answer": 0,
                           "why": "Inside the loop it would reset to 0 on every pass and only the last score would count."]],
                ["title": "When you need the position too",
                 "explain": "enumerate gives each item with its index, which you need to change items in place or to print numbered lines.",
                 "example": "for i, s in enumerate(scores):\n    scores[i] = s + 5   # a 5-point curve",
                 "check": NSNull()],
            ]]
        default:
            kind = "flashcards"; title = "Python lists: flash cards"
            body = ["cards": [
                ["front": "What index does a Python list start at?", "back": "0. The last item is at len(list) - 1, or -1."],
                ["front": "append vs. extend", "back": "append adds one item (even a whole list); extend adds each item of another list."],
                ["front": "What does pop() return?", "back": "The item it removes, the last one unless you give an index."],
                ["front": "nums[1:3] for nums = [10, 20, 30, 40]", "back": "[20, 30]: from index 1 up to, not including, index 3."],
                ["front": "How do you copy a list?", "back": "a.copy() or a[:]. Plain b = a shares the same list."],
                ["front": "range(5) gives…", "back": "0, 1, 2, 3, 4: five numbers, stopping before 5."],
                ["front": "Loop with index and value", "back": "for i, x in enumerate(items):"],
                ["front": "Why not remove items while looping over a list?", "back": "The list shifts under the loop and items get skipped. Loop over a copy or build a new list."],
            ]]
        }
        return ["id": id, "kind": kind, "title": title, "course_id": "qa:cs101", "assignment_id": "qa:lab5",
                "created_at": midnight + 9 * 3600, "model": "google/gemini-3.8-flash", "sample": false,
                "best_score": id == "m-quiz" ? (0.83 as Any) : (NSNull() as Any), "body": body]
    }
}
#endif
