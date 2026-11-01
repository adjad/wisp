// Contract for "Look" (app/Sources/WispApp/ScreenLook.swift): which window is
// captured, what the chip says, drag regions and the permission-denied path.
// Pure logic: never captures the screen, launches Wisp or contacts its service.
import CoreGraphics
import Foundation

@main
struct ScreenLookChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        let wisp: Int32 = 100, safari: Int32 = 200, mail: Int32 = 300
        func window(_ number: UInt32, _ pid: Int32, _ owner: String, _ title: String = "",
                    w: CGFloat = 800, h: CGFloat = 600, layer: Int = 0, alpha: Double = 1) -> ScreenLook.Window {
            ScreenLook.Window(number: number, ownerPID: pid, ownerName: owner, title: title,
                              bounds: CGRect(x: 0, y: 0, width: w, height: h), layer: layer, alpha: alpha)
        }
        let list = [
            window(1, wisp, "Wisp", "Ask"),                          // Wisp's own panel, frontmost
            window(2, mail, "Mail", "Badge", w: 20, h: 20),          // too small
            window(3, safari, "Safari", "Menu", layer: 25),          // not an ordinary window
            window(4, safari, "Safari", "Hidden", alpha: 0),         // invisible
            window(5, mail, "Mail", "Inbox"),
            window(6, safari, "Safari", "Pricing"),
        ]

        // The front window: the front app's first ordinary window, never Wisp's.
        check(ScreenLook.frontWindow(list, frontPID: safari, excludingPID: wisp)?.number == 6, "front app's window")
        check(ScreenLook.frontWindow(list, frontPID: mail, excludingPID: wisp)?.number == 5, "front app is Mail")
        check(ScreenLook.frontWindow(list, frontPID: wisp, excludingPID: wisp)?.number == 5,
              "with Wisp in front, the next app's window")
        check(ScreenLook.frontWindow(list, frontPID: nil, excludingPID: wisp)?.number == 5, "no front app")
        check(ScreenLook.frontWindow(list, frontPID: 999, excludingPID: wisp)?.number == 5, "front app has no window")
        check(ScreenLook.frontWindow([window(1, wisp, "Wisp")], frontPID: wisp, excludingPID: wisp) == nil,
              "only Wisp on screen: nothing to look at")
        check(ScreenLook.frontWindow([], frontPID: safari, excludingPID: wisp) == nil, "empty list")

        // The window list's dictionaries parse; malformed rows are skipped.
        let infos: [[String: Any]] = [
            [kCGWindowNumber as String: NSNumber(value: 7), kCGWindowOwnerPID as String: NSNumber(value: safari),
             kCGWindowOwnerName as String: "Safari", kCGWindowName as String: "Docs",
             kCGWindowLayer as String: NSNumber(value: 0), kCGWindowAlpha as String: NSNumber(value: 1.0),
             kCGWindowBounds as String: CGRect(x: 10, y: 20, width: 300, height: 200).dictionaryRepresentation],
            [kCGWindowNumber as String: NSNumber(value: 8)],
        ]
        let parsed = ScreenLook.windows(from: infos)
        check(parsed.count == 1 && parsed[0].number == 7 && parsed[0].title == "Docs", "window info parses")
        check(parsed[0].bounds == CGRect(x: 10, y: 20, width: 300, height: 200), "bounds parse")

        // The chip names what Wisp saw, as one plain line.
        check(ScreenLook.label(app: "Safari", title: "Pricing") == "Safari — Pricing", "app and title")
        check(ScreenLook.label(app: "Finder", title: "") == "Finder", "app alone")
        check(ScreenLook.label(app: "Notes", title: "Notes") == "Notes", "title repeating the app")
        check(ScreenLook.label(app: "", title: "") == nil, "nothing to name")
        check(ScreenLook.label(app: "Mail", title: "Hi\n\tthere\u{202E}gnp.exe") == "Mail — Hi there gnp.exe",
              "controls and display-changing characters never reach the chip")
        check(ScreenLook.label(app: "Messages", title: "👩‍👩‍👧 Family") == "Messages — 👩‍👩‍👧 Family",
              "emoji joiners are real text")
        let long = ScreenLook.label(app: "Safari", title: String(repeating: "x", count: 500))!
        check(long.count == ScreenLook.maxLabelLength && long.hasSuffix("…"), "long titles are cut")
        check(ScreenLook.chipText(label: "Safari — Pricing") == "Wisp saw: Safari — Pricing", "chip text")
        check(ScreenLook.chipText(label: nil) == "Wisp saw: your screen", "chip text without a label")
        check(ScreenLook.regionLabel(app: "Preview") == "part of Preview", "region label")
        check(ScreenLook.regionLabel(app: nil) == "part of your screen", "region label without an app")

        // A drag covers a normalized rectangle; a click is not a region.
        check(ScreenLook.region(from: CGPoint(x: 300, y: 400), to: CGPoint(x: 100, y: 100))
              == CGRect(x: 100, y: 100, width: 200, height: 300), "any drag direction")
        check(ScreenLook.region(from: CGPoint(x: 10, y: 10), to: CGPoint(x: 12, y: 300)) == nil, "too narrow")
        check(ScreenLook.region(from: .zero, to: .zero) == nil, "a click")
        check(ScreenLook.displayRect(fromAppKit: CGRect(x: 50, y: 100, width: 200, height: 300), primaryHeight: 1000)
              == CGRect(x: 50, y: 600, width: 200, height: 300), "AppKit to display coordinates")

        // Without Screen Recording, Look explains how to turn it on and captures nothing.
        check(ScreenLook.deniedNotice.contains("Screen Recording") && ScreenLook.deniedNotice.contains("Permissions")
              && ScreenLook.deniedNotice.contains("reopen"), "a denial says where to turn it on")

        // Private browsing is never captured silently.
        do {
        func app(_ number: UInt32, _ owner: String, _ bundle: String, _ title: String = "Page",
                 x: CGFloat = 0) -> ScreenLook.Window {
            var w = ScreenLook.Window(number: number, ownerPID: Int32(number), ownerName: owner, title: title,
                                      bounds: CGRect(x: x, y: 40, width: 900, height: 700), layer: 0, alpha: 1)
            w.bundleID = bundle
            return w
        }
        let notes = app(10, "Notes", "com.apple.Notes")
        let safari = app(11, "Safari", "com.apple.Safari")
        let chrome = app(12, "Google Chrome", "com.google.Chrome")
        let renamedChrome = app(13, "Chrome Beta Thing", "com.google.Chrome.beta")
        let unknownBrowser = app(14, "Foo Browser", "")
        for browser in [safari, chrome, renamedChrome, unknownBrowser,
                        app(15, "Arc", "company.thebrowser.Browser"), app(16, "Firefox", "org.mozilla.firefox"),
                        app(17, "Microsoft Edge", "com.microsoft.edgemac"), app(18, "Brave Browser", "")] {
            check(ScreenLook.isBrowser(browser), "\(browser.ownerName) is a browser")
        }
        check(!ScreenLook.isBrowser(notes), "Notes is not a browser")
        check(ScreenLook.browserVerdict([notes], incognito: []) == .clear, "no browser: no extra tap")
        check(ScreenLook.browserVerdict([], incognito: []) == .clear, "nothing in the capture")
        if case .confirm(let text) = ScreenLook.browserVerdict([safari], incognito: []) {
            check(text.contains("Safari") && text.contains("private") && text.contains("Look anyway"),
                  "a browser window warns and asks for one more tap")
        } else { check(false, "a Safari window needs a confirm tap (private Safari can't be detected)") }
        if case .confirm(let text) = ScreenLook.browserVerdict([notes, chrome, safari, chrome], incognito: []) {
            check(text.hasPrefix("Google Chrome and Safari"), "each browser named once, in order")
        } else { check(false, "several browsers still need the tap") }
        if case .refuse(let text) = ScreenLook.browserVerdict([notes, chrome], incognito: [12]) {
            check(text.contains("private") && text.contains("Google Chrome"), "Chrome-reported incognito is refused")
        } else { check(false, "an incognito window Chrome reports is refused") }
        for title in ["Docs — Private Browsing", "New InPrivate tab - Microsoft Edge", "Incognito tab",
                      "Page (Private)", "Mozilla Firefox Private Browsing"] {
            if case .refuse = ScreenLook.browserVerdict([app(19, "Firefox", "org.mozilla.firefox", title)], incognito: []) {
                check(true, "title marker refused")
            } else { check(false, "a private title marker is refused: \(title)") }
        }
        if case .refuse = ScreenLook.browserVerdict([safari, chrome], incognito: [12]) {
            check(true, "one private window refuses the whole capture")
        } else { check(false, "a region or screen with a private window is refused") }
        check(ScreenLook.browserVerdict([app(20, "Notes", "com.apple.Notes", "Incognito recipes")], incognito: []) == .clear,
              "a non-browser title that mentions incognito is just a title")
        check(!ScreenLook.privateTitle("Private equity news"), "the word private alone is not a marker")

        // Incognito bounds from the browser map onto window-list windows.
        check(ScreenLook.matchesIncognito(chrome, incognitoBounds: [CGRect(x: 1, y: 41, width: 899, height: 701)]),
              "bounds within 2 pt match")
        check(!ScreenLook.matchesIncognito(chrome, incognitoBounds: [CGRect(x: 400, y: 40, width: 900, height: 700)]),
              "another window's bounds do not")

        // A region or display capture checks every ordinary window it overlaps.
        let left = app(21, "Safari", "com.apple.Safari", x: 0), right = app(22, "Notes", "com.apple.Notes", x: 2000)
        let over = ScreenLook.windows([left, right, window(23, wisp, "Wisp")],
                                      overlapping: CGRect(x: 100, y: 100, width: 200, height: 200), excludingPID: wisp)
        check(over.map(\.number) == [21], "only overlapping, non-Wisp windows are checked")
        }

        print("ScreenLookChecks: \(count) checks passed")
    }
}
