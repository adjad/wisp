// Contract for the notch's tabs (app/Sources/WispApp/NotchTab.swift).
// Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct NotchTabChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        // Tabs, in strip order, with ⌘1 to ⌘3.
        check(NotchTab.allCases == [.ask, .today, .research], "the strip reads Ask, Today, Research")
        check(NotchTab.allCases.map(\.shortcutDigit) == ["1", "2", "3"], "⌘1 to ⌘3 follow the strip")
        check(NotchTab.allCases.map(\.title) == ["Ask", "Today", "Research"], "tab titles")

        // Opening: ⌥Space always Ask; hover the last tab for ten minutes after closing.
        let closed = Date(timeIntervalSince1970: 1_000_000)
        var memory = NotchTabMemory()
        check(memory.tabForOpening(current: .today, byHover: true, at: closed) == .ask,
              "a panel never closed opens on Ask")
        memory.panelClosed(at: closed)
        check(memory.tabForOpening(current: .today, byHover: true, at: closed.addingTimeInterval(60)) == .today,
              "hover a minute later opens the last tab")
        check(memory.tabForOpening(current: .research, byHover: true, at: closed.addingTimeInterval(600)) == .research,
              "hover at exactly ten minutes still opens the last tab")
        check(memory.tabForOpening(current: .today, byHover: true, at: closed.addingTimeInterval(601)) == .ask,
              "hover after ten minutes opens Ask")
        check(memory.tabForOpening(current: .today, byHover: false, at: closed.addingTimeInterval(60)) == .ask,
              "⌥Space opens Ask even right after closing on Today")
        check(memory.tabForOpening(current: .ask, byHover: true, at: closed.addingTimeInterval(60)) == .ask,
              "the last tab was Ask")
        check(memory.tabForOpening(current: .today, byHover: true, at: closed.addingTimeInterval(-5)) == .today,
              "a clock that moved back a little keeps the last tab")

        // Up next: what is under way or ahead, in order, at most three.
        struct Block { let name: String; let start: Double; let end: Double? }
        let now = 10_000.0
        let blocks = [Block(name: "over", start: 1_000, end: 2_000),
                      Block(name: "ends now", start: 9_000, end: now),
                      Block(name: "under way", start: 9_500, end: 11_000),
                      Block(name: "open, recent", start: now - 60, end: nil),
                      Block(name: "open, stale", start: now - 3_600, end: nil),
                      Block(name: "next", start: 12_000, end: 13_000),
                      Block(name: "later", start: 14_000, end: 15_000)]
        let next = NotchUpNext.upcoming(blocks, start: \.start, end: \.end, now: now).map(\.name)
        check(next == ["under way", "open, recent", "next"], "up next keeps order and caps at three: \(next)")
        let all = NotchUpNext.upcoming(blocks, start: \.start, end: \.end, now: now, limit: 10).map(\.name)
        check(all == ["under way", "open, recent", "next", "later"], "finished blocks are left out: \(all)")
        check(NotchUpNext.upcoming([Block](), start: \.start, end: \.end, now: now).isEmpty, "no blocks, nothing next")

        // The setup card: an open card waits for Done; one set aside leaves when Wisp is ready.
        check(NotchSetupCard.open.afterReadiness(true) == .open, "an open setup card waits for Done once ready")
        check(NotchSetupCard.later.afterReadiness(true) == .hidden, "a card set aside leaves once Wisp is ready")
        check(NotchSetupCard.later.afterReadiness(false) == .later, "a card set aside stays while Wisp can't answer")
        check(NotchSetupCard.hidden.afterReadiness(false) == .hidden, "losing readiness never reopens setup by itself")
        check(NotchSetupCard.open.tabForOpening(.today) == .ask, "an open setup card opens the panel on Ask")
        check(NotchSetupCard.later.tabForOpening(.today) == .today, "a card set aside keeps hover's last tab")
        check(NotchSetupCard.hidden.tabForOpening(.research) == .research, "no card, no change to the tab")

        print("\(count) notch tab checks passed.")
    }
}
