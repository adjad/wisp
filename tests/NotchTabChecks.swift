// Contract for the notch's tabs (app/Sources/WispApp/NotchTab.swift) and the
// Brief and Day logic (app/Sources/WispApp/NotchDay.swift), and quick settings'
// lines (app/Sources/WispApp/QuickSettingsText.swift), and the stale-reminder
// questions (app/Sources/WispApp/StaleReminders.swift).
// Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct NotchTabChecks {
    @MainActor static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        // Tabs, in strip order, with ⌘1 to ⌘3. Research is a mode of Ask, not a tab.
        check(NotchTab.strip == [.ask, .brief, .day, .meet, .study], "the strip reads Ask, Brief, Day, Meet, Study")
        check(NotchTab.strip.map(\.shortcutDigit) == ["1", "2", "3", "4", "5"], "⌘1 to ⌘5 follow the strip")
        check(NotchTab.strip.map(\.title) == ["Ask", "Brief", "Day", "Meet", "Study"], "tab titles")
        check(!NotchTab.strip.contains(.research) && NotchTab.research.shortcutDigit == nil, "Research is out of the strip")
        check(NotchTab.research.stripTab == .ask && NotchTab.day.stripTab == .day, "Research shows as Ask in the strip")
        check(NotchTab.brief.showsDay && NotchTab.day.showsDay && !NotchTab.ask.showsDay && !NotchTab.research.showsDay,
              "Brief and Day show the day; Ask and Research take a prompt")
        check(NotchTab.ask.hasPrompt && NotchTab.research.hasPrompt && !NotchTab.meet.hasPrompt && !NotchTab.day.hasPrompt,
              "Meet has no prompt field and shows as itself")
        check(!NotchTab.study.showsDay && !NotchTab.study.hasPrompt && NotchTab.study.stripTab == .study
              && NotchTab.meet.rawValue == 4 && NotchTab.study.rawValue == 5,
              "Study is its own tab, last, with no prompt field; earlier raw values keep")

        // Opening: ⌥Space always Ask; hover the last tab for ten minutes after closing.
        let closed = Date(timeIntervalSince1970: 1_000_000)
        var memory = NotchTabMemory()
        check(memory.tabForOpening(current: .day, byHover: true, at: closed) == .ask,
              "a panel never closed opens on Ask")
        memory.panelClosed(at: closed)
        check(memory.tabForOpening(current: .day, byHover: true, at: closed.addingTimeInterval(60)) == .day,
              "hover a minute later opens the last tab")
        check(memory.tabForOpening(current: .research, byHover: true, at: closed.addingTimeInterval(600)) == .research,
              "hover at exactly ten minutes still opens the last tab")
        check(memory.tabForOpening(current: .day, byHover: true, at: closed.addingTimeInterval(601)) == .ask,
              "hover after ten minutes opens Ask")
        check(memory.tabForOpening(current: .day, byHover: false, at: closed.addingTimeInterval(60)) == .ask,
              "⌥Space opens Ask even right after closing on Today")
        check(memory.tabForOpening(current: .ask, byHover: true, at: closed.addingTimeInterval(60)) == .ask,
              "the last tab was Ask")
        check(memory.tabForOpening(current: .day, byHover: true, at: closed.addingTimeInterval(-5)) == .day,
              "a clock that moved back a little keeps the last tab")

        // The setup card: an open card waits for Done; one set aside leaves when Wisp is ready.
        check(NotchSetupCard.open.afterReadiness(true) == .open, "an open setup card waits for Done once ready")
        check(NotchSetupCard.later.afterReadiness(true) == .hidden, "a card set aside leaves once Wisp is ready")
        check(NotchSetupCard.later.afterReadiness(false) == .later, "a card set aside stays while Wisp can't answer")
        check(NotchSetupCard.hidden.afterReadiness(false) == .hidden, "losing readiness never reopens setup by itself")
        check(NotchSetupCard.ready.afterReadiness(true) == .ready, "the ready dot waits for the panel to open")
        check(NotchSetupCard.ready.afterReadiness(false) == .hidden, "the ready dot never outlives readiness")
        check(NotchSetupCard.open.showsOnAsk && NotchSetupCard.later.showsOnAsk
              && !NotchSetupCard.ready.showsOnAsk && !NotchSetupCard.hidden.showsOnAsk,
              "the ready dot is on the closed bar only, never a setup card on Ask")
        check(NotchSetupCard.open.tabForOpening(.day) == .ask, "an open setup card opens the panel on Ask")
        check(NotchSetupCard.later.tabForOpening(.day) == .day, "a card set aside keeps hover's last tab")
        check(NotchSetupCard.hidden.tabForOpening(.research) == .research, "no card, no change to the tab")

        // Pages: what shows, how deep, and how a change slides.
        let strip = NotchTab.strip
        func shown(_ tab: NotchTab = .ask, settings: Bool = false, permissions: Bool = false,
                   reset: Bool = false, knowYou: Bool = false) -> NotchPage {
            NotchPage.shown(tab: tab, settings: settings, permissions: permissions, reset: reset, knowYou: knowYou)
        }
        check(shown(.day) == .tab(.day), "no overlay shows the tab")
        check(shown(settings: true) == .settings, "settings over a tab")
        check(shown(settings: true, permissions: true) == .permissions, "permissions beat settings")
        check(shown(settings: true, permissions: true, knowYou: true) == .knowYou, "Getting to know you beats permissions")
        check(shown(settings: true, permissions: true, reset: true, knowYou: true) == .reset, "reset beats everything")
        check(shown(permissions: true, reset: true) == .reset, "reset beats permissions")
        check(shown(.brief, knowYou: true) == .knowYou && shown(.brief, reset: true) == .reset, "cards replace the tab")

        for tab in NotchTab.allCases { check(NotchPage.tab(tab).depth == 0, "\(tab) is depth 0") }
        check(NotchPage.reset.depth == 0 && NotchPage.knowYou.depth == 0, "cards are depth 0")
        check(NotchPage.settings.depth == 1 && NotchPage.permissions.depth == 2, "settings 1, permissions 2")

        let allPages: [NotchPage] = NotchTab.allCases.map { .tab($0) } + [.settings, .permissions, .reset, .knowYou]
        for page in allPages {
            check(NotchPage.motion(from: page, to: page) == .none, "\(page) to itself does not move")
        }
        check(NotchPageMotion.none == NotchPageMotion(insertion: 0, removal: 0), "none is zero")

        let forward = NotchPageMotion(insertion: 1, removal: -1)
        let back = NotchPageMotion(insertion: -1, removal: 1)
        let push = NotchPageMotion(insertion: 3, removal: -1)
        let pop = NotchPageMotion(insertion: -1, removal: 3)
        for (i, a) in strip.enumerated() {
            for (j, b) in strip.enumerated() where i != j {
                check(NotchPage.motion(from: .tab(a), to: .tab(b)) == (j > i ? forward : back),
                      "\(a) to \(b) slides \(j > i ? "forward" : "back")")
            }
        }
        if strip.count >= 2 {
            check(NotchPage.motion(from: .tab(strip[0]), to: .tab(strip[1])) == forward, "first to second tab is forward")
            check(NotchPage.motion(from: .tab(strip[strip.count - 1]), to: .tab(strip[0])) == back,
                  "last tab to first is back")
        }
        check(NotchPage.motion(from: .tab(.ask), to: .tab(.brief)) == NotchPageMotion(insertion: 1, removal: -1), "Ask to Brief")
        check(NotchPage.motion(from: .tab(.day), to: .tab(.ask)) == NotchPageMotion(insertion: -1, removal: 1), "Day to Ask")
        check(NotchPage.motion(from: .tab(.research), to: .tab(.ask)) == .none, "Research to Ask is no move")
        check(NotchPage.motion(from: .tab(.ask), to: .tab(.research)) == .none, "Ask to Research is no move")
        check(NotchPage.motion(from: .tab(.research), to: .tab(.day)) == forward, "Research to Day is forward")
        check(NotchPage.motion(from: .tab(.day), to: .tab(.research)) == back, "Day to Research is back")
        for tab in strip {
            check(NotchPage.motion(from: .tab(tab), to: .settings) == push, "\(tab) to settings pushes")
            check(NotchPage.motion(from: .settings, to: .tab(tab)) == pop, "settings to \(tab) pops")
            check(NotchPage.motion(from: .tab(tab), to: .reset) == .none && NotchPage.motion(from: .reset, to: .tab(tab)) == .none,
                  "\(tab) and reset crossfade in place")
            check(NotchPage.motion(from: .tab(tab), to: .knowYou) == .none && NotchPage.motion(from: .knowYou, to: .tab(tab)) == .none,
                  "\(tab) and Getting to know you crossfade in place")
        }
        check(NotchPage.motion(from: .settings, to: .permissions) == push, "settings to permissions pushes")
        check(NotchPage.motion(from: .permissions, to: .settings) == pop, "permissions to settings pops")
        check(NotchPage.motion(from: .reset, to: .knowYou) == .none, "card to card crossfades")

        dayChecks(check)
        quickSettingsChecks(check)
        staleReminderChecks(check)
        needsYouActionChecks(check)
        studyChecks(check)

        print("\(count) notch tab, day, quick settings and stale reminder checks passed.")
    }
}
