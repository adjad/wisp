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
        check(NotchTab.strip == [.ask, .brief, .day], "the strip reads Ask, Brief, Day")
        check(NotchTab.strip.map(\.shortcutDigit) == ["1", "2", "3"], "⌘1 to ⌘3 follow the strip")
        check(NotchTab.strip.map(\.title) == ["Ask", "Brief", "Day"], "tab titles")
        check(!NotchTab.strip.contains(.research) && NotchTab.research.shortcutDigit == nil, "Research is out of the strip")
        check(NotchTab.research.stripTab == .ask && NotchTab.day.stripTab == .day, "Research shows as Ask in the strip")
        check(NotchTab.brief.showsDay && NotchTab.day.showsDay && !NotchTab.ask.showsDay && !NotchTab.research.showsDay,
              "Brief and Day show the day; Ask and Research take a prompt")

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
        check(NotchSetupCard.open.tabForOpening(.day) == .ask, "an open setup card opens the panel on Ask")
        check(NotchSetupCard.later.tabForOpening(.day) == .day, "a card set aside keeps hover's last tab")
        check(NotchSetupCard.hidden.tabForOpening(.research) == .research, "no card, no change to the tab")

        dayChecks(check)
        quickSettingsChecks(check)
        staleReminderChecks(check)

        print("\(count) notch tab, day, quick settings and stale reminder checks passed.")
    }
}
