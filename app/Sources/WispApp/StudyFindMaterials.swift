import AppKit
import ApplicationServices
import CryptoKit
import Foundation
import SwiftUI

// Study's "Find materials" (Phase 1): for one course, Wisp opens the course's
// Canvas Modules page in the student's own signed-in browser, reads its links
// through Accessibility, visits up to 100 of the course's own item pages one
// level deep, asks the backend to classify what it found
// (`POST /study/find/resolve`), shows the results grouped by kind with
// checkboxes, and on Add imports the ticked ones (`POST /study/find/add`).
//
// Call site (the integrator adds it; nothing else needs wiring). Recommended:
// StudyDriveViews.swift, `StudyMakeOptions.body`, as its last line, right
// after the `if sourceCount > 0 || whole != nil { HStack { … } }` block that
// holds the per-course "Whole course" pill (so it also shows for a course
// with no sources yet, which is when it matters most):
//
//     if let course { StudyFindMaterialsSection(course: course, onFinished: { await study.refresh() }) }
//
// StudyMakeOptions has no `onResize`, so with that line the panel refits on
// its next resize. Where the overlay model is in reach (NotchStudyView's
// `content(_:)`, the line after `StudyMakeOptions(...)`), pass it as well:
// `StudyFindMaterialsSection(course: course, onFinished: { await study.refresh() }, onResize: model.onResize)`.
//
// Public API:
// - `StudyFindMaterialsSection(course:onFinished:onResize:)`: the "Find
//   materials" pill on its own row plus the card under it.
// - `StudyFindMaterialsButton(course:onFinished:)`: the pill alone (styled
//   like "Whole course"); it opens or closes the course's card.
// - `StudyFindMaterialsView(course:onFinished:onResize:)`: the card alone; it
//   shows only while that course's run is open.
// - `onFinished` runs after an Add (and after a stopped Add). StudyModel's
//   `state` is `private(set)` and its transport private, so pass
//   `{ await study.refresh() }` (re-reads `GET /study/state`); the `state`
//   the add route returns is not applied here.
//
// Safety, by design: the only thing Wisp does to the browser is open https
// URLs that pass `StudyFindTarget.openableURL` (the course's Modules page and
// at most 100 of the course's own pages) through `NSWorkspace.open` without
// activating it, and closes the tabs it opened (StudyFindTabCloser: lists the
// browser's tabs around each open to claim the one new tab, and closes only
// that tab, only while it is still at the address opened or read). No
// JavaScript, key or mouse events, and no Accessibility actions; the only
// Accessibility write is `AXManualAccessibility = true`, exactly as
// StudyPageReader does for Chromium.
// Wisp never opens quizzes, assignments or outside tools (External Tool / LTI
// items): Canvas would post the student's identity to the tool's site, start
// the quiz, or launch an assignment's tool as its page opens. Each link is
// sent to resolve with its Canvas module item type so the server drops tools
// and quizzes; only items it returns as via "browser" are visited, by an
// allowlist: direct `/pages/…` and `/discussion_topics/…` links, and
// `/modules/items/<n>` only when its type was read as Page or Discussion
// (External URL items are never opened; an unreadable type is refused, and a refused type is never
// replaced by a plainer label); and the client refuses any path with a tool,
// LTI, quiz or `assignments` segment and ignores what such a page holds
// (assignment instructions already come from Study's own dashboard read).
// Course pages Wisp does open may still load tools embedded in them (a video
// player, say); the consent card says so. A visit is skipped when the tab
// snapshot before it didn't finish within its time budget. Only tabs new
// since Wisp's snapshot (or exactly at the opened URL) are read, so tabs the
// student already had open never are. Accepted gap: a background tab the
// student switches to during the wait looks new and may be read, but only if
// it is on this same course (it is the student's own course page). Nothing is
// opened that sounds important (StudyFindGuard). Nothing is added until the
// student ticks it and presses Add. Under WISP_NOTCH_QA the reader returns
// nothing and opening is a no-op.

// MARK: - Allow-check

/// The course's Canvas site and course number, parsed from the course's
/// stored `url` (never from `StudyCourse.id`), and the one rule every URL Wisp
/// opens or reads must pass.
// The parts of StudyFindTarget that read Study's own course data; the address
// rules themselves are in StudyFindRules.swift.
extension StudyFindTarget {
    /// The course's own Canvas address, else one taken from links Study already
    /// holds for that course (its assignments' Canvas URLs). The fallback is used
    /// only when every such link names the same host and `/courses/<n>`.
    init?(course: StudyCourse, canvasLinks: [String] = []) {
        guard !course.sample else { return nil }
        let found: (host: String, id: String)?
        if let raw = course.url, let own = Self.courseRef(raw) {
            found = own
        } else {
            let refs = canvasLinks.compactMap(Self.courseRef)
            if let first = refs.first, refs.allSatisfy({ $0 == first }) { found = first } else { found = nil }
        }
        guard let found else { return nil }
        host = found.host
        canvasID = found.id
        courseID = course.id
    }

    /// The Canvas links Study already holds for `course` (its assignments' URLs),
    /// kept only when the host is a Canvas host: one of the hosts of the courses'
    /// own stored Canvas URLs, `*.instructure.com`, or a host with a `canvas`
    /// label. Gradescope, Ed and other school sites that also use `/courses/<n>`
    /// are never taken for Canvas.
    static func canvasLinks(for course: StudyCourse, in state: StudyState) -> [String] {
        guard course.url.flatMap(courseRef) == nil else { return [] }
        let known = Set(state.courses.compactMap { $0.url.flatMap(courseRef)?.host })
        return state.assignments(in: course).compactMap(\.url).filter { raw in
            guard let host = courseRef(raw)?.host else { return false }
            return known.contains(host) || host.hasSuffix(".instructure.com")
                || host.split(separator: ".").contains("canvas")
        }
    }
}

// MARK: - What the reader finds

struct StudyFindLink: Hashable, Sendable {
    let title: String
    let url: String
    /// The Canvas module item type the row showed ("Page", "File",
    /// "Discussion", "Assignment", "Quiz", "ExternalUrl", "ExternalTool"), if any.
    let type: String?
}

/// A browser tab's page, by its Accessibility element (AXWebArea), so a tab
/// that was open before Wisp opened anything is never taken for a new one.
struct StudyFindTab: @unchecked Sendable {
    let element: AXUIElement

    func isAmong(_ tabs: [StudyFindTab]) -> Bool {
        tabs.contains { CFEqual($0.element, element) }
    }
}

/// One browser window's front tab.
struct StudyFindPage: Sendable {
    let tab: StudyFindTab
    /// Not among the tabs Wisp noted just before it opened something.
    let isNew: Bool
    let url: String
    let links: [StudyFindLink]
    /// AXURLs of web areas nested in the page (iframes, e.g. embedded Slides).
    let frames: [String]
    let text: String
    /// The read stopped at its element or time budget, so the page may hold more.
    var truncated = false
}

// MARK: - Accessibility reader

/// Read-only Accessibility walk of every browser window's front tab, after
/// StudyPageReader (whose helpers are private): short messaging timeouts and
/// overall time, element and depth budgets. Unlike StudyPageReader it walks
/// the whole web area, so links and nested web areas (iframes) are found.
enum StudyFindReader {
    static let maxLinks = 400
    /// Page text kept from one course page (the backend takes 150,000).
    static let maxCharacters = 140_000

    /// What to do with one front tab.
    enum Want: Sendable {
        /// Leave it alone.
        case skip
        /// Its address only; nothing on the page is read.
        case address
        /// Read its links, nested pages and (when asked) text.
        case walk
    }

    /// One scan's pages, and whether every browser window was looked at
    /// within the time budget.
    struct Scan: Sendable {
        let pages: [StudyFindPage]
        let complete: Bool
    }

    /// The front tab of every browser window, reading no URL or text: the
    /// snapshot taken just before each open. Nil when the browsers didn't all
    /// answer within the budget, so a tab missed here can't later look new.
    static func tabs() async -> [StudyFindTab]? {
        #if WISP_NOTCH_QA
        return []
        #else
        guard AXIsProcessTrusted() else { return [] }
        let pids = await browserPIDs()
        guard !pids.isEmpty else { return [] }
        return await Task.detached(priority: .userInitiated) { () -> [StudyFindTab]? in
            StudyFindReader.frontAreas(pids)
        }.value
        #endif
    }

    /// Front tabs as `want` decides from whether the tab is new since
    /// `known` and its URL. Only `.walk` tabs are read past their address.
    /// `stop` ends a read in progress at its next element, so Stop answers at once.
    static func scan(text: Bool, known: [StudyFindTab], stop: StudyFindStop? = nil,
                     want: @escaping @Sendable (_ isNew: Bool, _ url: String) -> Want) async -> Scan {
        #if WISP_NOTCH_QA
        return Scan(pages: [], complete: true)
        #else
        guard AXIsProcessTrusted() else { return Scan(pages: [], complete: true) }
        let pids = await browserPIDs()
        guard !pids.isEmpty else { return Scan(pages: [], complete: true) }
        return await Task.detached(priority: .userInitiated) { () -> Scan in
            StudyFindReader.scanAll(pids, text: text, known: known, stop: stop, want: want)
        }.value
        #endif
    }

    #if !WISP_NOTCH_QA

    private static let extraBrowsers: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview", "com.google.Chrome",
        "company.thebrowser.Browser", "com.microsoft.edgemac", "com.brave.Browser",
        "org.mozilla.firefox", "com.kagi.kagimacOS",
    ]

    private static func isBrowser(_ bundleID: String) -> Bool {
        extraBrowsers.contains(bundleID) || ScreenLook.browserBundleIDs.contains(bundleID)
    }

    // Budgets, as StudyPageReader.
    private static let messagingTimeout: Float = 0.25
    private static let totalBudgetNanoseconds: UInt64 = 6_000_000_000
    /// The walked tab's own read budget, on top of finding the tabs: a long
    /// Canvas page (side menus first, then the module list) needs more than 6 s.
    private static let walkBudgetNanoseconds: UInt64 = 15_000_000_000
    private static let maxElements = 25_000
    private static let maxDepth = 80
    private static let maxSearchElements = 600
    private static let maxSearchDepth = 14
    private static let maxWindowsPerApp = 40
    private static let maxFrames = 100
    private static let maxTitle = 300

    private static func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

    @MainActor private static func runningBrowserPIDs() -> [pid_t] {
        let me = ProcessInfo.processInfo.processIdentifier
        return NSWorkspace.shared.runningApplications.compactMap { app -> pid_t? in
            guard app.processIdentifier != me, !app.isTerminated,
                  let bundle = app.bundleIdentifier, isBrowser(bundle)
            else { return nil }
            return app.processIdentifier
        }
    }

    private static func browserPIDs() async -> [pid_t] {
        await MainActor.run { runningBrowserPIDs() }
    }

    /// Each browser window and its front tab's web area. True when every
    /// window was looked at within the budget. Never call on the main thread.
    @discardableResult
    private static func eachFrontArea(_ pids: [pid_t], deadline: () -> UInt64,
                                      _ body: (_ window: AXUIElement, _ area: AXUIElement) -> Void) -> Bool {
        var complete = true
        for pid in pids {
            guard now() < deadline() else { return false }
            let app = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(app, messagingTimeout)
            // Chromium apps expose an empty tree until this is set (as
            // StudyPageReader and PageReader do). Nothing else is ever written.
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
            let windows: [AXUIElement] = elements(app, kAXWindowsAttribute as String)
            if windows.count > maxWindowsPerApp { complete = false }
            for window in windows.prefix(maxWindowsPerApp) {
                guard now() < deadline() else { return false }
                AXUIElementSetMessagingTimeout(window, messagingTimeout)
                guard let area = webArea(in: window, deadline: deadline()) else {
                    if now() >= deadline() { return false }
                    continue
                }
                body(window, area)
            }
        }
        return complete && now() < deadline()
    }

    /// Nil when not every window was looked at. Never call on the main thread.
    private static func frontAreas(_ pids: [pid_t]) -> [StudyFindTab]? {
        var tabs: [StudyFindTab] = []
        let deadline = now() + totalBudgetNanoseconds
        let complete = eachFrontArea(pids, deadline: { deadline }) { _, area in
            tabs.append(StudyFindTab(element: area))
        }
        return complete ? tabs : nil
    }

    /// Never call on the main thread.
    private static func scanAll(_ pids: [pid_t], text: Bool, known: [StudyFindTab], stop: StudyFindStop?,
                                want: (Bool, String) -> Want) -> Scan {
        let clock = Clock(deadline: now() + totalBudgetNanoseconds)
        var pages: [StudyFindPage] = []
        let complete = eachFrontArea(pids, deadline: { stop?.isSet == true ? 0 : clock.deadline }) { window, area in
            let tab = StudyFindTab(element: area)
            let isNew = !tab.isAmong(known)
            let url = pageURL(area: area, window: window)
            guard !url.isEmpty else {
                // Still counted as a new tab, with nothing read.
                if isNew { pages.append(StudyFindPage(tab: tab, isNew: true, url: "", links: [], frames: [], text: "")) }
                return
            }
            switch want(isNew, url) {
            case .skip:
                return
            case .address:
                pages.append(StudyFindPage(tab: tab, isNew: isNew, url: url, links: [], frames: [], text: ""))
            case .walk:
                // Its own budget; the time it takes is added back for the other windows.
                let started = now()
                var w = Walk(deadline: started + walkBudgetNanoseconds, base: URL(string: url), wantText: text, stop: stop)
                harvest(area, into: &w, depth: 0, root: true)
                clock.deadline += now() - started
                var body = w.lines.joined(separator: "\n")
                if body.count > maxCharacters { body = String(body.prefix(maxCharacters)) }
                pages.append(StudyFindPage(tab: tab, isNew: isNew, url: url, links: w.links,
                                           frames: w.frames, text: body, truncated: w.truncated))
            }
        }
        return Scan(pages: pages, complete: complete)
    }

    private final class Clock {
        var deadline: UInt64
        init(deadline: UInt64) { self.deadline = deadline }
    }

    /// The window's first AXWebArea, breadth first: the front tab's page.
    private static func webArea(in window: AXUIElement, deadline: UInt64) -> AXUIElement? {
        var frontier: [(AXUIElement, Int)] = [(window, 0)]
        var visited = 0
        var cursor = 0
        while cursor < frontier.count {
            guard visited < maxSearchElements, now() < deadline else { return nil }
            let (element, depth) = frontier[cursor]
            cursor += 1
            visited += 1
            AXUIElementSetMessagingTimeout(element, messagingTimeout)
            let role = stringValue(element, kAXRoleAttribute as String) ?? ""
            if role == "AXWebArea" { return element }
            if role == (kAXMenuBarRole as String) || role == (kAXToolbarRole as String) { continue }
            guard depth < maxSearchDepth else { continue }
            for child in elements(element, kAXChildrenAttribute as String) {
                frontier.append((child, depth + 1))
            }
        }
        return nil
    }

    private static func pageURL(area: AXUIElement, window: AXUIElement) -> String {
        if let url = urlValue(area, "AXURL") { return url }
        if let url = urlValue(window, kAXDocumentAttribute as String) { return url }
        if let url = urlValue(window, "AXURL") { return url }
        return ""
    }

    private struct Walk {
        var lines: [String] = []
        var last = ""
        var characters = 0
        var visited = 0
        var links: [StudyFindLink] = []
        var seenLinks = Set<String>()
        var frames: [String] = []
        var seenFrames = Set<String>()
        /// Text runs inside the link being walked, for its title.
        var captured: [String]?
        var capturedCharacters = 0
        /// A module row's type label seen just before its link.
        var pendingType: String?
        var pendingAt = 0
        /// Stopped at the element or time budget.
        var truncated = false
        let deadline: UInt64
        let base: URL?
        let wantText: Bool
        let stop: StudyFindStop?
    }

    /// Document order, depth first. Links give URL and title; nested web areas
    /// their URL; static text the page text (when wanted). Password and text
    /// fields are never read.
    private static func harvest(_ element: AXUIElement, into w: inout Walk, depth: Int, root: Bool) {
        guard w.visited < maxElements, now() < w.deadline, w.stop?.isSet != true else {
            w.truncated = true
            return
        }
        guard depth < maxDepth else { return }
        w.visited += 1
        AXUIElementSetMessagingTimeout(element, messagingTimeout)
        let role = stringValue(element, kAXRoleAttribute as String) ?? ""
        let subrole = stringValue(element, kAXSubroleAttribute as String) ?? ""
        if role == "AXSecureTextField" || subrole == "AXSecureTextField" { return }
        if role == (kAXTextFieldRole as String) || role == (kAXTextAreaRole as String) { return }

        if role == "AXWebArea", !root, w.frames.count < maxFrames,
           let raw = urlValue(element, "AXURL"), let url = absolute(raw, base: w.base),
           !w.seenFrames.contains(url) {
            w.seenFrames.insert(url)
            w.frames.append(url)
        }

        // A module row's type icon: its label is the icon's description or
        // tooltip ("Assignment", "Quiz", "External Tool", …).
        if role == (kAXImageRole as String) || role == (kAXGroupRole as String) {
            let label = stringValue(element, kAXDescriptionAttribute as String).flatMap(moduleType)
                ?? stringValue(element, kAXHelpAttribute as String).flatMap(moduleType)
            if let label { note(label, in: &w) }
        }

        if role == (kAXStaticTextRole as String) {
            if let value = stringValue(element, kAXValueAttribute as String)
                ?? stringValue(element, kAXTitleAttribute as String) {
                // An item's own title (static text inside its link) never sets its type.
                if w.captured == nil, let type = moduleType(value) { note(type, in: &w) }
                if w.captured != nil, w.capturedCharacters < maxTitle {
                    w.captured?.append(value)
                    w.capturedCharacters += value.count + 1
                }
                if w.wantText { append(value, to: &w) }
            }
            return
        }

        if role == "AXLink" {
            let outer = w.captured
            let outerCount = w.capturedCharacters
            w.captured = []
            w.capturedCharacters = 0
            for child in elements(element, kAXChildrenAttribute as String) {
                harvest(child, into: &w, depth: depth + 1, root: false)
                if w.visited >= maxElements { break }
            }
            let inner = (w.captured ?? []).joined(separator: " ")
            w.captured = outer.map { $0 + [inner] }
            w.capturedCharacters = outerCount + inner.count
            if w.links.count < maxLinks, let raw = urlValue(element, "AXURL"),
               let url = absolute(raw, base: w.base), !w.seenLinks.contains(url) {
                let description = stringValue(element, kAXDescriptionAttribute as String)
                let help = stringValue(element, kAXHelpAttribute as String)
                let title = StudyFindText.cleanTitle(clean(stringValue(element, kAXTitleAttribute as String))
                    ?? clean(description) ?? clean(inner) ?? "")
                // The row's label, then the link's own description or tooltip.
                // A tool or quiz label wins, so the server drops the item.
                let pending = w.visited - w.pendingAt < 60 ? w.pendingType : nil
                let candidates = [pending, description.flatMap(moduleType), help.flatMap(moduleType)].compactMap { $0 }
                let type = candidates.first(where: isNeverOpened) ?? candidates.first
                w.pendingType = nil
                w.seenLinks.insert(url)
                w.links.append(StudyFindLink(title: title, url: url, type: type))
            }
            return
        }

        for child in elements(element, kAXChildrenAttribute as String) {
            harvest(child, into: &w, depth: depth + 1, root: false)
            if w.visited >= maxElements { return }
        }
    }

    private static func append(_ value: String, to w: inout Walk) {
        guard w.characters < maxCharacters else { return }
        for raw in value.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, line != w.last else { continue }
            w.lines.append(line)
            w.last = line
            w.characters += line.count + 1
            if w.characters >= maxCharacters { return }
        }
    }

    /// A type label for the next link. A label Wisp never opens (tool, quiz,
    /// assignment) is never replaced by a plainer one before a link takes it.
    private static func note(_ type: String, in w: inout Walk) {
        if let pending = w.pendingType, isNeverOpened(pending), !isNeverOpened(type) { return }
        w.pendingType = type
        w.pendingAt = w.visited
    }

    /// Canvas module rows carry a hidden type label next to their icon.
    /// Keys are lower-case letters and single spaces.
    private static let typeLabels: [String: String] = [
        "page": "Page", "wiki page": "Page", "wikipage": "Page",
        "file": "File", "attachment": "File",
        "discussion": "Discussion", "discussion topic": "Discussion", "discussiontopic": "Discussion",
        "assignment": "Assignment",
        "quiz": "Quiz", "quizzes": "Quiz", "new quiz": "Quiz", "new quizzes": "Quiz", "newquiz": "Quiz",
        "quizzes next": "Quiz", "quiz lti": "Quiz", "quizzeslti": "Quiz", "assessment": "Quiz",
        "external url": "ExternalUrl", "external link": "ExternalUrl", "externalurl": "ExternalUrl",
        "external tool": "ExternalTool", "externaltool": "ExternalTool",
        "context external tool": "ExternalTool", "contextexternaltool": "ExternalTool",
        "lti": "ExternalTool", "lti tool": "ExternalTool", "lti link": "ExternalTool",
        "text header": "SubHeader", "sub header": "SubHeader", "subheader": "SubHeader",
        "context module sub header": "SubHeader", "contextmodulesubheader": "SubHeader",
    ]

    private static func moduleType(_ text: String) -> String? {
        let letters = text.lowercased().map { $0.isLetter && $0.isASCII ? $0 : " " }
        let key = String(letters).split(separator: " ").joined(separator: " ")
        guard !key.isEmpty, key.count <= 30 else { return nil }
        return typeLabels[key]
    }

    #endif

    // Module item type rules are in StudyFindRules.swift; these forward to them.
    static func isLaunchType(_ type: String) -> Bool { StudyFindRules.isLaunchType(type) }
    static func isNeverOpened(_ type: String) -> Bool { StudyFindRules.isNeverOpened(type) }
    static func isOpenableModuleType(_ type: String?) -> Bool { StudyFindRules.isOpenableModuleType(type) }
    static func typeKey(_ type: String) -> String { StudyFindRules.typeKey(type) }

    #if !WISP_NOTCH_QA

    private static func clean(_ text: String?) -> String? {
        guard let text else { return nil }
        let joined = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard !joined.isEmpty else { return nil }
        return String(joined.prefix(maxTitle))
    }

    private static func copyValue(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    private static func elements(_ element: AXUIElement, _ name: String) -> [AXUIElement] {
        guard let value = copyValue(element, name), let list = value as? [AXUIElement] else { return [] }
        return list
    }

    private static func stringValue(_ element: AXUIElement, _ name: String) -> String? {
        guard let value = copyValue(element, name) else { return nil }
        if let text = value as? String { return text }
        if let attributed = value as? NSAttributedString { return attributed.string }
        return nil
    }

    private static func urlValue(_ element: AXUIElement, _ name: String) -> String? {
        guard let value = copyValue(element, name) else { return nil }
        if let url = value as? URL { return url.absoluteString }
        if let url = value as? NSURL { return url.absoluteString }
        if let text = value as? String, !text.isEmpty { return text }
        return nil
    }

    #endif

    /// Absolute http(s) URL without its fragment, or nil.
    static func absolute(_ raw: String, base: URL?) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 4_000,
              let url = URL(string: trimmed, relativeTo: base)?.absoluteURL,
              var c = URLComponents(url: url, resolvingAgainstBaseURL: true),
              let scheme = c.scheme?.lowercased(), scheme == "https" || scheme == "http",
              c.host?.isEmpty == false
        else { return nil }
        c.fragment = nil
        return c.string
    }
}

// MARK: - Never opened: anything that sounds important

/// The owner's rule: Wisp never opens, or adds, anything that is or sounds like
/// a test, quiz, exam, survey or course evaluation, a graded or proctored
/// activity, a submission or upload page, a link that starts, attempts or
/// resumes something, anything timed, a payment or enrolment page, a meeting,
/// or an account or settings page. Checked on the Canvas item type first, then
/// on words in the title and the address, then on the site. When in doubt it
/// counts as important. Refused items are still listed, with the reason, so
/// the student can open them by hand.
// StudyFindGuard (what Wisp never opens) is in StudyFindRules.swift.

// MARK: - Closing the tabs a run opened

/// The owner asked for each tab a walk opens to be closed once it has been
/// read ("make it close it after its done instead of opening like 50 tabs").
/// The one Apple Events use in Find materials, bound to Wisp's own tab:
/// - right before each open, Wisp lists the default browser's tabs (window id,
///   tab id, or the tab's position where the browser has no tab ids, and URL);
/// - right after, it lists them again and claims the ONE tab that is new, only
///   if exactly one is and it is at the address Wisp opened, still loading, or
///   on the course; none or several means no claim, and the tab stays open;
/// - once that tab has been read, Wisp closes the claimed tab, found by window
///   and tab id (or position), only if its address right now is the one Wisp
///   opened or the one it read (case-sensitive).
/// A tab the student had, switched to or navigated is never a candidate.
/// Never runs JavaScript, never reads a page, never touches a window, never
/// relaunches a browser. macOS asks the student once to let Wisp control the
/// browser; if they say no, tabs stay open and the card says so. Stop and
/// failures close nothing. Leaving a tab open is the safe failure.
extension StudyFindTabCloser {
    // Outcome, Tab, digest, Listing, the browser sets, the tab claim and its
    // close-time check are in StudyFindRules.swift.

    /// The browser `url` opens in, as NSWorkspace.open would pick it.
    @MainActor static func browser(for url: URL) -> (id: String, name: String)? {
        #if WISP_NOTCH_QA
        return nil
        #else
        guard let app = NSWorkspace.shared.urlForApplication(toOpen: url),
              let id = Bundle(url: app)?.bundleIdentifier else { return nil }
        return (id, FileManager.default.displayName(atPath: app.path).replacingOccurrences(of: ".app", with: ""))
        #endif
    }

    /// Every tab of `browser`, or why not. Empty when it isn't running.
    static func list(browser: String, wait: TimeInterval,
                     keep: @Sendable (String) -> Bool) async -> Listing {
        #if WISP_NOTCH_QA
        return .failed
        #else
        guard scriptable.contains(browser) else { return .failed }
        switch await run(listScript(browser, byID: tabIDs.contains(browser)), arguments: [], wait: wait) {
        case .text(let out):
            let tabs = out.split(separator: "\n").compactMap { line -> Tab? in
                let parts = line.split(separator: "\t", maxSplits: 2, omittingEmptySubsequences: false).map(String.init)
                guard parts.count >= 2, !parts[0].isEmpty, !parts[1].isEmpty else { return nil }
                let url = parts.count > 2 ? parts[2] : ""
                return Tab(window: parts[0], key: parts[1], digest: digest(url), url: keep(url) ? url : nil)
            }
            return .tabs(tabs)
        case .denied: return .denied
        case .timedOut: return .timedOut
        case .failed: return .failed
        }
        #endif
    }


    /// Closes `tab` only if its address right now is one of `addresses`.
    static func close(_ tab: Tab, addresses: [String], browser: String, wait: TimeInterval) async -> Outcome {
        #if WISP_NOTCH_QA
        return .unsupported
        #else
        guard scriptable.contains(browser), let first = addresses.first else { return .unsupported }
        let second = addresses.count > 1 ? addresses[1] : first
        // The address check is repeated inside the script, right before close.
        switch await run(closeScript(browser, byID: tabIDs.contains(browser)),
                         arguments: [tab.window, tab.key, first, second], wait: wait) {
        case .text(let said): return said == "closed" ? .closed : .notOne
        case .denied: return .denied
        case .timedOut: return .timedOut
        case .failed: return .failed
        }
        #endif
    }

    private enum Reply { case text(String), denied, timedOut, failed }

    /// The scripts carry only the browser's id from `scriptable` (never page
    /// data); addresses and ids come in as arguments after "--". Both return
    /// at once if the browser isn't running (never relaunching it).
    private static func listScript(_ browser: String, byID: Bool) -> [String] {
        [
            "on run argv",
            "if application id \"\(browser)\" is not running then return \"\"",
            "tell application id \"\(browser)\"",
            "set out to \"\"",
            "repeat with w in windows",
            "try",
            "set wid to (id of w) as text",
            "set i to 0",
            "repeat with t in tabs of w",
            "set i to i + 1",
            "set u to \"\"",
            "try",
            "set u to (URL of t) as text",
            "end try",
            byID ? "set k to (id of t) as text" : "set k to i as text",
            "set out to out & wid & (character id 9) & k & (character id 9) & u & (character id 10)",
            "end repeat",
            "end try",
            "end repeat",
            "return out",
            "end tell",
            "end run",
        ]
    }

    private static func closeScript(_ browser: String, byID: Bool) -> [String] {
        [
            "on run argv",
            "set wid to item -4 of argv",
            "set k to item -3 of argv",
            "set a1 to item -2 of argv",
            "set a2 to item -1 of argv",
            "if application id \"\(browser)\" is not running then return \"gone\"",
            "tell application id \"\(browser)\"",
            "considering case",
            "repeat with w in windows",
            "if ((id of w) as text) is wid then",
            "set i to 0",
            "repeat with t in tabs of w",
            "set i to i + 1",
            byID ? "set here to ((id of t) as text)" : "set here to (i as text)",
            "if here is k then",
            "set u to \"\"",
            "try",
            "set u to (URL of t) as text",
            "end try",
            "if u is a1 or u is a2 then",
            "close t",
            "return \"closed\"",
            "end if",
            "return \"moved\"",
            "end if",
            "end repeat",
            "end if",
            "end repeat",
            "end considering",
            "return \"gone\"",
            "end tell",
            "end run",
        ]
    }

    private static func run(_ lines: [String], arguments: [String], wait: TimeInterval) async -> Reply {
        await Task.detached(priority: .utility) { () -> Reply in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            process.arguments = lines.flatMap { ["-e", $0] } + ["--"] + arguments
            let out = Pipe()
            let err = Pipe()
            process.standardOutput = out
            process.standardError = err
            do { try process.run() } catch { return .failed }
            // The first call may wait on macOS's "Wisp wants to control…" prompt.
            let timedOut = ManagedFlag()
            let timer = DispatchWorkItem {
                if process.isRunning {
                    timedOut.set()
                    process.terminate()
                }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + wait, execute: timer)
            let said = out.fileHandleForReading.readDataToEndOfFile()
            let problem = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            process.waitUntilExit()
            timer.cancel()
            if timedOut.isSet { return .timedOut }
            if problem.contains("-1743") { return .denied }
            guard process.terminationStatus == 0 else { return .failed }
            let text = String(data: said, encoding: .utf8) ?? ""
            return .text(text.hasSuffix("\n") ? String(text.dropLast()) : text)
        }.value
    }
}

/// Set by Stop: a page read in progress ends at its next element.
final class StudyFindStop: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

/// A thread-safe one-way flag, for the close timer.
private final class ManagedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.lock(); value = true; lock.unlock() }
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

// MARK: - Backend shapes

/// One classified link from `POST /study/find/resolve`.
struct StudyFoundItem: Decodable, Equatable, Identifiable, Sendable {
    let id: String
    let kind: String
    let title: String
    let url: String
    let via: String
    var known: Bool
    /// Why the server refused a course item it still lists: "tool", "quiz",
    /// "assignment" or "unknown". Such an item is never opened or added.
    let reason: String?
    /// A lecture recording the server lists by title and link only: it can be
    /// added as a video source but is never opened (it launches an outside tool).
    let recording: Bool

    enum CodingKeys: String, CodingKey { case id, kind, title, url, via, known, reason, recording }

    init(id: String, kind: String, title: String, url: String, via: String, known: Bool, reason: String? = nil,
         recording: Bool = false) {
        self.id = id
        self.kind = kind
        self.title = StudyFindText.cleanTitle(title)
        self.url = url
        self.via = via
        self.known = known
        self.reason = reason
        self.recording = recording
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        url = try c.decode(String.self, forKey: .url)
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil)?.lowercased() ?? "other"
        title = StudyFindText.cleanTitle(((try? c.decodeIfPresent(String.self, forKey: .title)) ?? nil) ?? "")
        via = ((try? c.decodeIfPresent(String.self, forKey: .via)) ?? nil)?.lowercased() ?? "none"
        known = ((try? c.decodeIfPresent(Bool.self, forKey: .known)) ?? nil) ?? false
        reason = ((try? c.decodeIfPresent(String.self, forKey: .reason)) ?? nil)?.lowercased()
        recording = ((try? c.decodeIfPresent(Bool.self, forKey: .recording)) ?? nil) ?? false
    }

    var host: String { URL(string: url)?.host?.lowercased() ?? "" }
}

/// Decodes an element if it can, else nil, so one bad entry drops alone.
private struct StudyFindLossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}

private struct StudyFindResolveReply: Decodable {
    let items: [StudyFoundItem]
    enum CodingKeys: String, CodingKey { case items }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let list = (try? c.decodeIfPresent([StudyFindLossy<StudyFoundItem>].self, forKey: .items)) ?? nil
        items = (list ?? []).compactMap(\.value)
    }
}

private struct StudyFindAddedSummary: Decodable {
    let url: String?
    let deckStatus: String?
    enum CodingKeys: String, CodingKey { case url, deckStatus = "deck_status" }
    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        url = ((try? c?.decodeIfPresent(String.self, forKey: .url)) ?? nil) ?? nil
        deckStatus = ((try? c?.decodeIfPresent(String.self, forKey: .deckStatus)) ?? nil) ?? nil
    }
}

struct StudyFindFailure: Decodable, Equatable, Sendable {
    let url: String
    let code: String
    let message: String
    enum CodingKeys: String, CodingKey { case url, code, message }
    init(url: String, code: String, message: String) {
        self.url = url
        self.code = code
        self.message = message
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        url = ((try? c.decodeIfPresent(String.self, forKey: .url)) ?? nil) ?? ""
        code = ((try? c.decodeIfPresent(String.self, forKey: .code)) ?? nil) ?? ""
        message = ((try? c.decodeIfPresent(String.self, forKey: .message)) ?? nil) ?? ""
    }
}

private struct StudyFindAddReply: Decodable {
    let added: [StudyFindAddedSummary]
    let failed: [StudyFindFailure]
    enum CodingKeys: String, CodingKey { case added, failed }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let a = (try? c.decodeIfPresent([StudyFindLossy<StudyFindAddedSummary>].self, forKey: .added)) ?? nil
        let f = (try? c.decodeIfPresent([StudyFindLossy<StudyFindFailure>].self, forKey: .failed)) ?? nil
        added = (a ?? []).compactMap(\.value)
        failed = (f ?? []).compactMap(\.value)
    }
}

private struct StudyFindResolveBody: Encodable {
    let courseID: String
    let links: [StudyFindLink]
    enum CodingKeys: String, CodingKey { case courseID = "course_id", links }
    enum LinkKeys: String, CodingKey { case title, url, type }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(courseID, forKey: .courseID)
        var list = c.nestedUnkeyedContainer(forKey: .links)
        for link in links {
            var item = list.nestedContainer(keyedBy: LinkKeys.self)
            try item.encode(link.title, forKey: .title)
            try item.encode(link.url, forKey: .url)
            if let type = link.type { try item.encode(type, forKey: .type) } else { try item.encodeNil(forKey: .type) }
        }
    }
}

private struct StudyFindAddBody: Encodable {
    struct Item {
        let url: String
        let title: String
        let kind: String
        let text: String?
    }
    let courseID: String
    let items: [Item]
    enum CodingKeys: String, CodingKey { case courseID = "course_id", items }
    enum ItemKeys: String, CodingKey { case url, title, kind, text }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(courseID, forKey: .courseID)
        var list = c.nestedUnkeyedContainer(forKey: .items)
        for entry in items {
            var item = list.nestedContainer(keyedBy: ItemKeys.self)
            try item.encode(entry.url, forKey: .url)
            try item.encode(entry.title, forKey: .title)
            try item.encode(entry.kind, forKey: .kind)
            if let text = entry.text { try item.encode(text, forKey: .text) } else { try item.encodeNil(forKey: .text) }
        }
    }
}

/// What one Add did.
struct StudyFindAddResult: Equatable, Sendable {
    var added = 0
    var failures: [StudyFindFailure] = []
    /// A whole request that failed (not one item), in the server's words.
    var problem: String?
    var stopped = false
    /// Some added slides or PDFs are still being read in the background.
    var reading = false
}

// MARK: - Copy

enum StudyFindText {
    static let button = "Find materials"
    static let help = "Opens this course's Canvas pages in your browser and finds its slides, docs and readings"
    static let consentKey = "study.findMaterials.consented.v4"  // v4: Wisp closes the tabs it opens
    static let consent = "Wisp will open this course's Modules page and its sections (Home, Announcements, Syllabus, "
        + "Assignments, Pages, Discussions, Files, Grades) in your browser, read the links on them and on up "
        + "to 100 of the course's own pages, and show you what it found. Each page opens as a tab, and Wisp closes it once "
        + "it has read it (macOS asks once to let Wisp control your browser; Wisp only ever closes tabs it opened). "
        + "Nothing is added until you choose. "
        + "Linked Google files are read with your Drive sign-in; public articles are fetched without your cookies. "
        + "Opening an item may count as viewing it in Canvas. Course pages may load tools embedded in them, such as "
        + "video players. Wisp never opens quizzes, assignments or outside tools."
    static let noCanvasLink = "Wisp doesn't have a Canvas link for this course yet. Read your school tabs first."
    static let needsAddress = "Find materials: open this course in Canvas and press Read again so Wisp knows its address."
    static let openingModules = "Opening the Modules page in your browser"
    static let readingModules = "Reading the Modules page"
    static func sorting(_ n: Int) -> String { "Sorting \(n) \(n == 1 ? "link" : "links")" }
    static func readingPage(_ i: Int, of n: Int) -> String { "Reading course page \(i) of \(n)" }
    static func readingSection(_ name: String, _ i: Int, of n: Int) -> String { "Reading \(name) (\(i) of \(n))" }
    /// One line of counts on the found card, so a run can be judged without seeing
    /// the page. Counts only: no titles or addresses.
    static func summary(_ c: StudyFindCounts) -> String {
        var parts: [String] = []
        if let links = c.modulesLinks {
            parts.append("Modules: \(links) \(links == 1 ? "link" : "links"), \(c.moduleItems) module "
                + (c.moduleItems == 1 ? "item" : "items"))
        } else {
            parts.append("Modules: not read")
        }
        parts.append("Sections: \(c.sectionsRead) of \(c.sections) read, \(c.sectionLinks) "
            + (c.sectionLinks == 1 ? "link" : "links"))
        parts.append("Kept \(c.kept) of \(c.sent)")
        if c.pagesPlanned > 0 { parts.append("Opened \(c.pagesRead) of \(c.pagesPlanned) pages") }
        if c.truncated > 0 {
            parts.append("\(c.truncated) \(c.truncated == 1 ? "page" : "pages") too large to read in full")
        }
        return parts.joined(separator: " · ")
    }
    static let sortingMore = "Sorting what the course pages link to"
    static let couldntOpen = "Wisp couldn't open the Modules page in your browser. Try again."
    static let couldntRead = "Wisp couldn't read the Modules page. Make sure you're signed in to Canvas in your "
        + "browser and the page has finished loading, then try again."
    static func tabs(_ n: Int) -> String {
        "Wisp opened \(n) \(n == 1 ? "tab" : "tabs") in your browser — you can close \(n == 1 ? "it" : "them")."
    }
    /// The tabs line once Wisp closes what it opens.
    static func tabs(opened n: Int, closed: Int, problem: StudyFindTabCloser.Outcome?, browser: String) -> String {
        StudyFindTabCloser.tabsLine(opened: n, closed: closed, problem: problem, browser: browser)
    }
    static let nothingFound = "Wisp didn't find any course materials on this course's Canvas pages."
    static let stoppedEarly = "Stopped. This is what Wisp found so far."
    static let stopped = "Stopped. Nothing was added."
    static let timeCap = "Wisp reached its time limit for one run. This is what it found."
    static let added = "Added"
    static let cantAdd = "Can't add yet"
    static let couldntReadItem = "Couldn't read"
    static func adding(_ n: Int) -> String { "Adding \(n) \(n == 1 ? "item" : "items")" }
    static func result(_ r: StudyFindAddResult) -> String {
        var text = "Added \(r.added)"
        if !r.failures.isEmpty { text += " · \(r.failures.count) couldn't be read" }
        if r.reading { text += " · Reading slides…" }
        return text
    }
    static let addStopped = "Stopped. Anything already added stays in your sources."
    static let itemFailed = "Wisp couldn't read this one."
    /// The backend's own sentence for a failed item, else a gentle generic
    /// one (Study's code sentences are about the Study model, not these).
    static func failureLine(_ failure: StudyFindFailure) -> String {
        let said = failure.message.trimmingCharacters(in: .whitespacesAndNewlines)
        return said.isEmpty ? itemFailed : String(said.prefix(300))
    }

    static let order = ["slides", "pdf", "notebook", "doc", "file", "article", "page", "discussion", "video", "other"]
    static func header(_ kind: String) -> String {
        switch kind {
        case "slides": return "Slides"
        case "pdf": return "PDFs"
        case "notebook": return "Notebooks"
        case "doc": return "Docs"
        case "file": return "Files"
        case "article": return "Articles"
        case "page": return "Course pages"
        case "discussion": return "Discussions"
        case "video": return "Videos"
        default: return "Can't open"
        }
    }

    /// The row note for an item the server refused.
    static func refused(_ reason: String) -> String {
        switch reason {
        case "quiz": return "Can't open: test or quiz"
        case "survey": return "Can't open: survey or evaluation"
        case "graded": return "Can't open: graded or proctored"
        case "submission": return "Can't open: submission or upload"
        case "start": return "Can't open: starts something"
        case "timed": return "Can't open: timed"
        case "payment": return "Can't open: payment or enrolment"
        case "meeting": return "Can't open: meeting or live session"
        case "account": return "Can't open: account or settings"
        case "outside": return "Can't open: outside link"
        case "tool": return "Can't open: outside tool"
        case "assignment": return "Can't open: assignment"
        default: return "Can't open: unknown type"
        }
    }

    /// Canvas's screen-reader notes on links ("Links to an external site.",
    /// "(opens in a new tab)") taken off a title, so sources get clean names.
    static func cleanTitle(_ title: String) -> String { StudyFindRules.cleanTitle(title) }

    static let recordingLabel = "Recording · link only"

    /// The row note for an item Wisp never opens because it sounds important.
    static func notOpened(_ reason: String) -> String { "Not opened: \(reason)" }
    static func kindName(_ kind: String) -> String {
        switch kind {
        case "slides": return "Slides"
        case "pdf": return "PDF"
        case "notebook": return "Notebook"
        case "doc": return "Doc"
        case "file": return "File"
        case "article": return "Article"
        case "page": return "Course page"
        case "discussion": return "Discussion"
        case "video": return "Video"
        default: return "Link"
        }
    }
    static func symbol(_ kind: String) -> String {
        switch kind {
        case "slides": return "rectangle.on.rectangle"
        case "pdf": return "doc.richtext"
        case "notebook": return "chevron.left.forwardslash.chevron.right"
        case "doc": return "doc.text"
        case "file": return "doc"
        case "article": return "newspaper"
        case "page": return "doc.plaintext"
        case "discussion": return "bubble.left.and.bubble.right"
        case "video": return "play.rectangle"
        default: return "link"
        }
    }
}

// MARK: - The run

/// What one run read, for the found card's summary line.
struct StudyFindCounts: Equatable, Sendable {
    var modulesLinks: Int?
    var moduleItems = 0
    var sections = 0
    var sectionsRead = 0
    var sectionLinks = 0
    var sent = 0
    var kept = 0
    var pagesPlanned = 0
    var pagesRead = 0
    var truncated = 0
}

/// One course's Find materials run. Kept per course across view rebuilds (the
/// notch can close and reopen mid-run), so the walk carries on and the card
/// shows where it is when it comes back.
@MainActor
final class StudyFindRun: ObservableObject {
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)
    typealias Opener = (URL) async -> Bool

    enum Phase: Equatable {
        case idle, consent, needsAccess, working, found, adding, done
        case failed(String)
    }

    @Published var visible = false
    @Published private(set) var phase: Phase = .idle {
        // [notch hold] Tells the notch a walk started or ended (AppDelegate.watchForStudyJobEnding).
        didSet { if phase != oldValue { NotificationCenter.default.post(name: Self.phaseChanged, object: self) } }
    }
    @Published private(set) var step = ""
    @Published private(set) var tabsOpened = 0
    @Published private(set) var tabsClosed = 0
    /// This run's tab closing: once the browser refuses, fails or doesn't
    /// answer, no further call is made this run (StudyFindTabCloser.Gate).
    @Published private(set) var closeGate = StudyFindTabCloser.Gate(browser: nil)
    /// Why tabs stopped being closed this run (not allowed, failed, timed out, or no tab scripting).
    var closeProblem: StudyFindTabCloser.Outcome? { closeGate.problem }
    private(set) var browserName = "your browser"
    private var browserID: String?
    /// This run's Stop signal for page reads in progress.
    private var stopToken = StudyFindStop()
    /// The tab Wisp claimed as its own right after the last open, and its
    /// window's tabs at that moment (hashes only, for the re-check at close).
    private var claim: (gen: Int, tab: StudyFindTabCloser.Tab, window: [StudyFindTabCloser.Tab], opened: String)?
    @Published private(set) var items: [StudyFoundItem] = []
    /// Item id → the text Wisp read from that course page.
    @Published private(set) var texts: [String: String] = [:]
    @Published var selected: Set<String> = []
    @Published private(set) var note: String?
    @Published private(set) var result: StudyFindAddResult?
    @Published private(set) var counts: StudyFindCounts?

    let target: StudyFindTarget
    private let transport: Transport
    private let opener: Opener
    private var task: Task<Void, Never>?
    /// Bumped on every start and stop, so a stopped run's late answers drop.
    private var generation = 0
    private var onFinished: (() async -> Void)?
    private var titlesByURL: [String: String] = [:]
    /// `StudyFindTarget.pathKey` of each link sent → its Canvas module item type.
    private var sentTypes: [String: String] = [:]
    /// Item id → why it is never opened or added (StudyFindGuard).
    private var important: [String: String] = [:]

    /// The owner asked for up to 100 opened pages (was 10). The run limit scales with
    /// the cap (5 minutes plus 9 s a page: 20 minutes at 100), and Wisp pauses
    /// `openPause` between page opens so the course site isn't hammered.
    static let maxVisits = 100
    static let runSeconds: TimeInterval = 300 + TimeInterval(maxVisits) * 9
    static let openPause: UInt64 = 1_500_000_000
    static let modulesWait: TimeInterval = 60
    static let pageWait: TimeInterval = 20
    static let maxAddBatch = 60
    static let resolveTimeout: TimeInterval = 30
    static let addTimeout: TimeInterval = 120

    private static var runs: [String: StudyFindRun] = [:]

    // [notch hold] Read by the notch's automatic close (OverlayModel.studyJobRunningNow, studyHoldsOpen).
    static let phaseChanged = Notification.Name("WispStudyFindRunPhaseChanged")
    /// A walk or an add is under way for some course.
    static var anyRunning: Bool { runs.values.contains { $0.running } }
    /// Some course's found card is waiting for Add or Done.
    static var anyAwaitingChoice: Bool { runs.values.contains { $0.visible && $0.phase == .found } }

    /// The run for this course (one per course and Canvas site).
    static func shared(for target: StudyFindTarget) -> StudyFindRun {
        let key = target.courseID + "|" + target.host + "|" + target.canvasID
        if let run = runs[key] { return run }
        let run = StudyFindRun(target: target)
        runs[key] = run
        return run
    }

    init(target: StudyFindTarget, transport: Transport? = nil, opener: Opener? = nil) {
        self.target = target
        #if WISP_NOTCH_QA
        self.transport = transport ?? { (_: URLRequest) async throws -> (Data, URLResponse) in
            throw URLError(.cancelled)
        }
        #else
        self.transport = transport ?? { (request: URLRequest) async throws -> (Data, URLResponse) in
            try await URLSession.shared.data(for: request)
        }
        #endif
        self.opener = opener ?? StudyFindRun.openInBrowser
        // Opening the card shows consent first, or goes straight on.
    }

    var running: Bool { phase == .working || phase == .adding }

    // MARK: Lifecycle

    /// Shows the card: consent the first time, then the run.
    func show(onFinished: @escaping () async -> Void) {
        self.onFinished = onFinished
        visible = true
        guard !running, phase != .found, phase != .done else { return }
        if UserDefaults.standard.bool(forKey: StudyFindText.consentKey) {
            start()
        } else {
            phase = .consent
        }
    }

    func setFinished(_ action: @escaping () async -> Void) { onFinished = action }

    func consent() {
        UserDefaults.standard.set(true, forKey: StudyFindText.consentKey)
        start()
    }

    /// Cancel on the consent card, Done, or Close: hides the card and resets.
    func close() {
        if running { stop() }
        generation += 1
        task = nil
        visible = false
        phase = .idle
        step = ""
        items = []
        texts = [:]
        selected = []
        note = nil
        result = nil
        counts = nil
        tabsOpened = 0
    }

    /// Stops at once. The walk keeps what it found; an add in flight may
    /// already have added some items, so the state is refreshed.
    func stop() {
        let wasAdding = phase == .adding
        stopToken.set()
        generation += 1
        task?.cancel()
        task = nil
        if wasAdding {
            var r = result ?? StudyFindAddResult()
            r.stopped = true
            result = r
            phase = .done
            if let finish = onFinished { Task { await finish() } }
        } else if phase == .working {
            if items.isEmpty {
                phase = .failed(StudyFindText.stopped)
            } else {
                note = StudyFindText.stoppedEarly
                settle()
            }
        }
    }

    func start() {
        guard StudyPageReader.hasAccessibility else {
            phase = .needsAccess
            return
        }
        guard target.modulesURL != nil else {
            phase = .failed(StudyFindText.noCanvasLink)
            return
        }
        generation += 1
        let gen = generation
        task?.cancel()
        items = []
        texts = [:]
        selected = []
        note = nil
        result = nil
        tabsOpened = 0
        tabsClosed = 0
        claim = nil
        stopToken = StudyFindStop()
        counts = nil
        titlesByURL = [:]
        sentTypes = [:]
        important = [:]
        phase = .working
        step = StudyFindText.openingModules
        if let modules = target.modulesURL, let found = StudyFindTabCloser.browser(for: modules) {
            browserID = found.id
            browserName = found.name
        } else {
            browserID = nil
        }
        closeGate = StudyFindTabCloser.Gate(browser: browserID)
        task = Task { [weak self] in await self?.walk(gen) }
    }

    private func live(_ gen: Int) -> Bool { gen == generation && !Task.isCancelled }

    // MARK: Walk

    private func walk(_ gen: Int) async {
        let runDeadline = Date().addingTimeInterval(Self.runSeconds)
        let t = target
        guard let modules = t.modulesURL, let modulesURL = t.openableURL(modules.absoluteString) else {
            phase = .failed(StudyFindText.noCanvasLink)
            return
        }
        let modulesString = modulesURL.absoluteString

        // 1. Open the Modules page and wait for it: the tab Wisp opened (new
        //    since the snapshot) or one already at exactly that address.
        let snapshot = await StudyFindReader.tabs()
        guard live(gen) else { return }
        // The browsers didn't all answer in time: no open, since a tab missed
        // here could later look like Wisp's own.
        guard let beforeModules = snapshot else {
            phase = .failed(StudyFindText.couldntRead)
            return
        }
        guard await open(modulesURL) else {
            if live(gen) { phase = .failed(StudyFindText.couldntOpen) }
            return
        }
        guard live(gen) else { return }
        step = StudyFindText.readingModules
        var c = StudyFindCounts()
        let openedAt = Date()
        var page: StudyFindPage?
        var best: StudyFindPage?
        var previousCount: Int?
        let modulesDeadline = min(runDeadline, Date().addingTimeInterval(Self.modulesWait))
        while Date() < modulesDeadline {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard live(gen) else { return }
            let pages = await StudyFindReader.scan(text: false, known: beforeModules, stop: stopToken) { isNew, url in
                if (isNew || StudyFindTarget.samePage(url, modulesString)) && t.isModules(url) { return .walk }
                // Where a new tab went instead (Modules hidden): its address only.
                return isNew && t.allows(url) ? .address : .skip
            }.pages
            guard live(gen) else { return }
            let elapsed = Date().timeIntervalSince(openedAt)
            guard let now = pages.filter({ t.isModules($0.url) && !$0.links.isEmpty })
                .max(by: { $0.links.count < $1.links.count })
            else {
                // The course sent its new tab elsewhere: no Modules page to read.
                if best == nil, elapsed >= 8,
                   pages.contains(where: { $0.isNew && t.allows($0.url) && !t.isModules($0.url) }) { break }
                continue
            }
            if best.map({ now.links.count >= $0.links.count }) ?? true { best = now }
            // Read once a complete read's link count is the same across two
            // polls, at least 3 s after opening. A read cut short never settles.
            if !now.truncated, previousCount == now.links.count, elapsed >= 3 {
                page = now
                break
            }
            previousCount = now.truncated ? nil : now.links.count
        }
        let modulesPage = page ?? best
        await closeLast(landed: modulesPage?.url, gen: gen)
        guard live(gen) else { return }
        if let modulesPage {
            c.modulesLinks = modulesPage.links.count
            c.moduleItems = modulesPage.links.filter { StudyFindTarget.pathKey($0.url).contains("/modules/items/") }.count
            if modulesPage.truncated { c.truncated += 1 }
        }
        counts = c

        // 2. The course's own sections (Home, Syllabus, the Assignments list…):
        //    read-only list pages, whose links join the Modules page's.
        var sectionLinks: [StudyFindLink] = []
        var sectionFrames: [String] = []
        let sections = t.sectionURLs
        c.sections = sections.count
        for (index, section) in sections.enumerated() {
            guard live(gen) else { return }
            if Date() >= runDeadline {
                note = StudyFindText.timeCap
                break
            }
            // Re-checked here: exactly one of this course's section pages.
            guard t.allows(section.url.absoluteString), t.isNavSection(section.url.absoluteString) else { continue }
            step = StudyFindText.readingSection(section.name, index + 1, of: sections.count)
            try? await Task.sleep(nanoseconds: Self.openPause)
            guard live(gen) else { return }
            let snapshot = await StudyFindReader.tabs()
            guard live(gen) else { return }
            guard let before = snapshot else { continue }
            guard await open(section.url) else { continue }
            guard live(gen) else { return }
            let landing = await land(opened: section.url.absoluteString, before: before, allowsOff: false, gen: gen,
                                     until: min(runDeadline, Date().addingTimeInterval(Self.pageWait)))
            guard live(gen) else { return }
            if case .course(let read)? = landing { await closeLast(landed: read.url, gen: gen) } else {
                await closeLast(landed: nil, gen: gen)
            }
            guard live(gen) else { return }
            guard case .course(let landed)? = landing, t.allows(landed.url), !t.isLaunchPage(landed.url),
                  StudyFindGuard.refusal(title: "", url: landed.url, type: nil) == nil
            else { continue }
            c.sectionsRead += 1
            c.sectionLinks += landed.links.count
            if landed.truncated { c.truncated += 1 }
            sectionLinks += landed.links
            sectionFrames += landed.frames
            counts = c
        }
        guard modulesPage != nil || c.sectionsRead > 0 else {
            phase = .failed(StudyFindText.couldntRead)
            return
        }

        // 3. Classify their links, with each module item's Canvas type.
        var sent = Set<String>()
        let first = Self.links((modulesPage?.links ?? []) + sectionLinks,
                               frames: (modulesPage?.frames ?? []) + sectionFrames, except: &sent)
        for link in first {
            guard let type = link.type else { continue }
            let key = StudyFindTarget.pathKey(link.url)
            // Once refused, always refused: a later, plainer label for the
            // same item never replaces a tool, quiz or assignment type.
            if let stored = sentTypes[key], StudyFindReader.isNeverOpened(stored) { continue }
            sentTypes[key] = type
        }
        step = StudyFindText.sorting(first.count)
        guard let found = await resolve(first, gen: gen, quiet: false) else { return }
        guard live(gen) else { return }
        merge(found)
        c.sent = first.count
        c.kept = found.count
        counts = c

        // 4. One level deep: up to 100 of the course's own pages, only from
        //    what this resolve returned as via "browser".
        let visits = Self.visits(found, target: t, types: sentTypes).filter { important[$0.id] == nil }
        c.pagesPlanned = visits.count
        counts = c
        var deeper: [StudyFindLink] = []
        var deeperFrames: [String] = []
        for (index, item) in visits.enumerated() {
            guard live(gen) else { return }
            if Date() >= runDeadline {
                note = StudyFindText.timeCap
                break
            }
            // Re-checked here, whatever the server said: the course's own
            // page, and never a tool, LTI or quiz launch.
            guard let url = t.openableURL(item.url),
                  StudyFindGuard.refusal(title: item.title, url: item.url,
                                         type: sentTypes[StudyFindTarget.pathKey(item.url)]) == nil
            else { continue }
            step = StudyFindText.readingPage(index + 1, of: visits.count)
            if index > 0 {
                // A short pause between opens, so the course site isn't hammered.
                try? await Task.sleep(nanoseconds: Self.openPause)
                guard live(gen) else { return }
            }
            let snapshot = await StudyFindReader.tabs()
            guard live(gen) else { return }
            // Skip this one when the browsers didn't all answer in time.
            guard let before = snapshot else { continue }
            guard await open(url) else { continue }
            guard live(gen) else { return }
            // Nothing Wisp opens may land off the course (External URL items are never opened).
            let landing = await land(opened: url.absoluteString, before: before, allowsOff: false, gen: gen,
                                     until: min(runDeadline, Date().addingTimeInterval(Self.pageWait)))
            guard live(gen) else { return }
            switch landing {
            case .course(let read)?: await closeLast(landed: read.url, gen: gen)
            case .off(let address)?: await closeLast(landed: address, gen: gen)
            case nil: await closeLast(landed: nil, gen: gen)
            }
            guard live(gen) else { return }
            switch landing {
            case .course(let landed)?:
                // Re-checked before anything read from the landed page is
                // used: this course, and not a tool or quiz launch page (its
                // links and frames are ignored too).
                guard t.allows(landed.url), !t.isLaunchPage(landed.url),
                      StudyFindGuard.refusal(title: "", url: landed.url, type: nil) == nil else { continue }
                c.pagesRead += 1
                if landed.truncated { c.truncated += 1 }
                counts = c
                if (item.kind == "page" || item.kind == "discussion"), !landed.text.isEmpty {
                    texts[item.id] = String(landed.text.prefix(StudyFindReader.maxCharacters))
                }
                deeper += landed.links
                deeperFrames += landed.frames
            case .off(let address)?:
                // Off the course (an External URL item): only its address
                // goes to resolve. It is never read and nothing is opened from it.
                if let clean = StudyFindReader.absolute(address, base: nil) {
                    deeper.append(StudyFindLink(title: item.title, url: clean, type: nil))
                }
            case nil:
                continue
            }
        }

        // 5. Classify what those pages link to, then show it all. Nothing
        //    from this round is opened.
        let second = Self.links(deeper, frames: deeperFrames, except: &sent)
        if !second.isEmpty, live(gen), Date() < runDeadline {
            step = StudyFindText.sortingMore
            if let more = await resolve(second, gen: gen, quiet: true), live(gen) {
                merge(more)
                c.sent += second.count
                c.kept += more.count
                counts = c
            }
        }
        guard live(gen) else { return }
        settle()
    }

    /// Links plus nested page URLs, deduped against `sent`, at most 400.
    private static func links(_ links: [StudyFindLink], frames: [String],
                              except sent: inout Set<String>) -> [StudyFindLink] {
        var out: [StudyFindLink] = []
        for link in links + frames.map({ StudyFindLink(title: "", url: $0, type: nil) }) {
            guard out.count < StudyFindReader.maxLinks, !sent.contains(link.url) else { continue }
            sent.insert(link.url)
            out.append(link)
        }
        return out
    }

    /// The items worth opening, an allowlist: only what the server returned
    /// as via "browser", on this course, and openable by path. Direct
    /// `/courses/<n>/pages/…` and `/discussion_topics/…` links first; then
    /// `/modules/items/<n>` only when the Canvas type Wisp sent for it was
    /// Page or Discussion (unknown, File, Quiz, Assignment, External URL,
    /// External Tool and SubHeader are all refused). Nothing else is opened.
    private static func visits(_ items: [StudyFoundItem], target: StudyFindTarget,
                               types: [String: String]) -> [StudyFoundItem] {
        func rank(_ item: StudyFoundItem) -> Int? {
            guard item.via == "browser", item.reason == nil, !item.known, let parts = StudyFindTarget.parts(item.url),
                  target.openableURL(item.url) != nil else { return nil }
            let type = types[StudyFindTarget.pathKey(item.url)]
            if let type, StudyFindReader.isNeverOpened(type) { return nil }
            let s = parts.segments
            if s.count == 5, s[2] == "modules", s[3] == "items" {
                return StudyFindReader.isOpenableModuleType(type) ? 1 : nil
            }
            if s.count == 4, s[2] == "pages" || s[2] == "discussion_topics",
               item.kind == "page" || item.kind == "discussion" {
                return 0
            }
            return nil
        }
        return items.compactMap { item in rank(item).map { (item, $0) } }
            .enumerated()
            .sorted { ($0.element.1, $0.offset) < ($1.element.1, $1.offset) }
            .prefix(maxVisits)
            .map { $0.element.0 }
    }

    private enum Landing {
        /// A page on this course, read.
        case course(StudyFindPage)
        /// Where an item went off the course: its address only.
        case off(String)
    }

    /// The tab the open produced: one that is new since `before` (Wisp's own
    /// tab) or is exactly at the opened URL. A tab that was already open is
    /// never re-matched, so the student's own tabs are never read. On the
    /// course it is read once its text settles. Off the course (only when
    /// `allowsOff`, for an External URL item) only its address is taken, and
    /// only when a complete scan shows exactly one new tab since the snapshot,
    /// at the same address for two polls.
    private func land(opened: String, before: [StudyFindTab], allowsOff: Bool,
                      gen: Int, until: Date) async -> Landing? {
        let t = target
        let started = Date()
        var lastCount: [String: Int] = [:]
        var latest: StudyFindPage?
        var offSeen: String?
        var offPolls = 0
        while Date() < until {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard live(gen) else { return nil }
            let scan = await StudyFindReader.scan(text: true, known: before, stop: stopToken) { isNew, url in
                guard isNew || StudyFindTarget.samePage(url, opened) else { return .skip }
                if t.allows(url) { return t.isLaunchPage(url) ? .address : .walk }
                // Off the course: a new tab's address only (so new tabs can be
                // counted), never its page.
                return isNew ? .address : .skip
            }
            guard live(gen) else { return nil }
            let fresh = scan.pages
            let elapsed = Date().timeIntervalSince(started)
            // A tool or quiz launch page on the course: nothing from it is used.
            if fresh.contains(where: { t.allows($0.url) && t.isLaunchPage($0.url) }) {
                return nil
            }
            if let page = fresh.first(where: { t.allows($0.url) && !$0.text.isEmpty }) {
                let key = page.url
                let stillRedirecting = page.url.contains("/modules/items/") && elapsed < 8
                if !stillRedirecting {
                    latest = page
                    if (lastCount[key] == page.text.count && !page.truncated) || elapsed >= 10 { return .course(page) }
                }
                lastCount[key] = page.text.count
                continue
            }
            guard allowsOff, latest == nil, elapsed >= 3, scan.complete else { continue }
            let newTabs = fresh.filter(\.isNew)
            guard newTabs.count == 1, let off = newTabs.first?.url, !t.allows(off),
                  let parts = StudyFindTarget.parts(off), parts.host != t.host
            else {
                offSeen = nil
                offPolls = 0
                continue
            }
            if off == offSeen { offPolls += 1 } else { offSeen = off; offPolls = 1 }
            if offPolls >= 2 { return .off(off) }
        }
        if let latest { return .course(latest) }
        return nil
    }

    /// Opens `url`; when tabs can be closed, lists the browser's tabs just
    /// before and claims the one new tab just after (StudyFindTabCloser).
    private func open(_ url: URL) async -> Bool {
        let gen = generation
        claim = nil
        let browser = closeProblem == nil ? browserID : nil
        var before: [StudyFindTabCloser.Tab]?
        let opened = url.absoluteString
        if let browser { before = await listTabs(browser, gen: gen, opened: opened) }
        let ok = await opener(url)
        guard ok else { return false }
        tabsOpened += 1
        if let browser, let before, gen == generation, closeProblem == nil {
            for _ in 0..<4 {
                try? await Task.sleep(nanoseconds: 400_000_000)
                guard gen == generation, let after = await listTabs(browser, gen: gen, opened: opened) else { break }
                // Its address is kept only when it is the opened one, loading, or on the course.
                if let tab = StudyFindTabCloser.newTab(before: before, after: after,
                                                       byID: StudyFindTabCloser.tabIDs.contains(browser)),
                   tab.url != nil {
                    claim = (gen, tab, after.filter { $0.window == tab.window }, opened)
                    break
                }
            }
        }
        return ok
    }

    /// The browser's tabs, addresses as hashes except one Wisp may claim.
    private func listTabs(_ browser: String, gen: Int, opened: String) async -> [StudyFindTabCloser.Tab]? {
        guard closeGate.mayCall else { return nil }
        let t = target
        let listing = await StudyFindTabCloser.list(browser: browser, wait: closeGate.wait) { url in
            url.isEmpty || StudyFindTarget.samePage(url, opened) || t.allows(url)
        }
        guard gen == generation else { return nil }
        return closeGate.record(listing)
    }

    /// Closes the tab Wisp claimed at the last open, now that it has been read
    /// (or given up on), for run `gen`: only if it is still at the address Wisp
    /// opened or the one it read (`landed`). No claim, no close.
    private func closeLast(landed: String?, gen: Int) async {
        guard let held = claim, held.gen == gen, gen == generation else { return }
        claim = nil
        guard closeGate.mayCall, let browser = browserID else { return }
        let addresses = [held.opened, landed ?? held.opened]
        // Re-listed first: the claimed tab must still be the same tab, at one of
        // those addresses, with its window otherwise unchanged (by position).
        guard let now = await listTabs(browser, gen: gen, opened: held.opened), gen == generation,
              StudyFindTabCloser.stillOurs(held.tab, claimWindow: held.window, now: now, addresses: addresses,
                                           byID: StudyFindTabCloser.tabIDs.contains(browser))
        else { return }
        let outcome = await StudyFindTabCloser.close(held.tab, addresses: addresses,
                                                     browser: browser, wait: closeGate.wait)
        guard gen == generation else { return }
        if closeGate.record(outcome) { tabsClosed += 1 }
    }

    /// In the student's default browser, without bringing it forward.
    nonisolated private static func openInBrowser(_ url: URL) async -> Bool {
        #if WISP_NOTCH_QA
        return false
        #else
        guard url.scheme?.lowercased() == "https" else { return false }
        return await withCheckedContinuation { (done: CheckedContinuation<Bool, Never>) in
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = false
            NSWorkspace.shared.open(url, configuration: configuration) { _, error in
                done.resume(returning: error == nil)
            }
        }
        #endif
    }

    private func merge(_ found: [StudyFoundItem]) {
        for item in found where item.reason == nil && important[item.id] == nil {
            if let why = StudyFindGuard.refusal(title: item.title, url: item.url,
                                                type: sentTypes[StudyFindTarget.pathKey(item.url)]) {
                important[item.id] = why
            }
        }
        var index: [String: Int] = [:]
        for (i, item) in items.enumerated() { index[item.id] = i }
        for item in found {
            if let i = index[item.id] {
                if item.known { items[i].known = true }
            } else {
                index[item.id] = items.count
                items.append(item)
            }
        }
    }

    /// The walk is over: tick everything that can be added.
    private func settle() {
        selected = Set(items.filter { blocker($0) == nil }.map(\.id))
        step = ""
        phase = .found
    }

    // MARK: Rows

    /// Why a row can't be ticked, or nil.
    func blocker(_ item: StudyFoundItem) -> String? {
        if let reason = item.reason { return StudyFindText.refused(reason) }
        if item.known { return StudyFindText.added }
        if let why = important[item.id] { return StudyFindText.notOpened(why) }
        // A lecture recording: added by title and link, never opened.
        if item.recording, item.via == "none", item.reason == nil { return nil }
        switch item.via {
        case "drive", "web":
            return item.kind == "video" || item.kind == "other" ? StudyFindText.cantAdd : nil
        case "browser":
            if item.kind == "video" || item.kind == "other" { return StudyFindText.cantAdd }
            return (texts[item.id]?.isEmpty ?? true) ? StudyFindText.couldntReadItem : nil
        default:
            return StudyFindText.cantAdd
        }
    }

    /// The list group an item shows under: refused items all go under "Can't open".
    func group(_ item: StudyFoundItem) -> String {
        if item.reason != nil || important[item.id] != nil { return "other" }
        if item.recording { return "video" }
        return StudyFindText.order.contains(item.kind) ? item.kind : "other"
    }

    func toggle(_ item: StudyFoundItem) {
        guard blocker(item) == nil else { return }
        if selected.contains(item.id) { selected.remove(item.id) } else { selected.insert(item.id) }
    }

    var chosen: [StudyFoundItem] { items.filter { selected.contains($0.id) && blocker($0) == nil } }

    // MARK: Add

    func add() {
        let picks = chosen
        guard !picks.isEmpty, !running else { return }
        generation += 1
        let gen = generation
        phase = .adding
        result = StudyFindAddResult()
        step = StudyFindText.adding(picks.count)
        for item in picks { titlesByURL[item.url] = item.title }
        task = Task { [weak self] in await self?.send(picks, gen: gen) }
    }

    private func send(_ picks: [StudyFoundItem], gen: Int) async {
        var r = StudyFindAddResult()
        var start = 0
        while start < picks.count {
            guard live(gen) else { return }
            let batch = Array(picks[start..<min(start + Self.maxAddBatch, picks.count)])
            start += batch.count
            let body = StudyFindAddBody(courseID: target.courseID, items: batch.map { item in
                StudyFindAddBody.Item(url: item.url, title: String(item.title.prefix(300)), kind: item.kind,
                                      text: item.via == "browser" ? texts[item.id] : nil)
            })
            guard let data = try? JSONEncoder().encode(body) else {
                r.problem = StudyText.problem(code: "bad_request")
                break
            }
            switch await call("study/find/add", body: data, timeout: Self.addTimeout) {
            case .ok(let reply):
                guard live(gen) else { return }
                guard let parsed = try? JSONDecoder().decode(StudyFindAddReply.self, from: reply) else {
                    r.problem = StudyText.problem(code: "")
                    break
                }
                r.added += parsed.added.count
                if parsed.added.contains(where: { $0.deckStatus == "reading" }) { r.reading = true }
                r.failures += parsed.failed
                let failedURLs = Set(parsed.failed.map(\.url))
                let addedURLs = Set(parsed.added.compactMap(\.url))
                // The server may canonicalize URLs. When every failure names a
                // URL Wisp sent, the rest of the batch went in; otherwise only
                // the URLs it echoed as added are marked.
                let failuresMatch = failedURLs.isSubset(of: Set(batch.map(\.url)))
                for item in batch where addedURLs.contains(item.url)
                    || (failuresMatch && !failedURLs.contains(item.url)) {
                    if let i = items.firstIndex(where: { $0.id == item.id }) { items[i].known = true }
                    selected.remove(item.id)
                }
                result = r
            case .failed(let status, let reply):
                guard live(gen) else { return }
                r.problem = Self.message(status: status, data: reply)
            case .offline:
                guard live(gen) else { return }
                r.problem = StudyText.problem(code: "offline")
            }
            if r.problem != nil { break }
        }
        guard live(gen) else { return }
        result = r
        step = ""
        phase = .done
        if let finish = onFinished { await finish() }
    }

    func title(forFailure failure: StudyFindFailure) -> String {
        let title = titlesByURL[failure.url] ?? items.first { $0.url == failure.url }?.title ?? ""
        if !title.isEmpty { return title }
        return URL(string: failure.url)?.host ?? failure.url
    }

    // MARK: Transport

    private func resolve(_ links: [StudyFindLink], gen: Int, quiet: Bool) async -> [StudyFoundItem]? {
        guard !links.isEmpty else { return [] }
        let body = StudyFindResolveBody(courseID: target.courseID, links: Array(links.prefix(StudyFindReader.maxLinks)))
        guard let data = try? JSONEncoder().encode(body) else {
            if !quiet { phase = .failed(StudyText.problem(code: "bad_request")) }
            return nil
        }
        let reply = await call("study/find/resolve", body: data, timeout: Self.resolveTimeout)
        guard live(gen) else { return nil }
        switch reply {
        case .ok(let answer):
            guard let parsed = try? JSONDecoder().decode(StudyFindResolveReply.self, from: answer) else {
                if !quiet { phase = .failed(StudyText.problem(code: "")) }
                return nil
            }
            return parsed.items
        case .failed(let status, let answer):
            if !quiet { phase = .failed(Self.message(status: status, data: answer)) }
            return nil
        case .offline:
            if !quiet { phase = .failed(StudyText.problem(code: "offline")) }
            return nil
        }
    }

    private enum Reply {
        case ok(Data)
        case failed(status: Int, data: Data)
        case offline
    }

    /// Native-only, like StudyModel's requests: JSON content type, no Origin.
    private func call(_ path: String, body: Data, timeout: TimeInterval) async -> Reply {
        var root = WispClient.baseURL.absoluteString
        while root.hasSuffix("/") { root.removeLast() }
        let url = URL(string: root + "/" + path) ?? WispClient.baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = body
        let result: (Data, URLResponse)
        do {
            result = try await transport(request)
        } catch {
            return .offline
        }
        let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(status) { return .ok(result.0) }
        return .failed(status: status, data: result.0)
    }

    /// The server's own sentence when it sent one, else Study's gentle one.
    private static func message(status: Int, data: Data) -> String {
        if let reply = try? JSONDecoder().decode(StudyErrorReply.self, from: data),
           let said = reply.message?.trimmingCharacters(in: .whitespacesAndNewlines), !said.isEmpty {
            return String(said.prefix(300))
        }
        return StudyModel.problem(status: status, data: data)
    }
}

// MARK: - Views

/// The "Find materials" pill for one course plus its card underneath.
struct StudyFindMaterialsSection: View {
    let course: StudyCourse
    var canvasLinks: [String] = []
    let onFinished: () async -> Void
    var onResize: () -> Void = {}

    var body: some View {
        if let target = StudyFindTarget(course: course, canvasLinks: canvasLinks) {
            StudyFindMaterialsHost(run: StudyFindRun.shared(for: target), course: course,
                                   canvasLinks: canvasLinks, onFinished: onFinished, onResize: onResize)
                .id(course.id)
        } else if !course.sample {
            // No Canvas address yet: say how to get one, instead of showing nothing.
            Label(StudyFindText.needsAddress, systemImage: "doc.text.magnifyingglass")
                .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(StudyFindText.needsAddress)
        }
    }
}

private struct StudyFindMaterialsHost: View {
    @ObservedObject var run: StudyFindRun
    let course: StudyCourse
    let canvasLinks: [String]
    let onFinished: () async -> Void
    let onResize: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                StudyFindMaterialsButton(course: course, canvasLinks: canvasLinks, onFinished: onFinished)
                Spacer(minLength: 0)
            }
            StudyFindMaterialsView(course: course, canvasLinks: canvasLinks, onFinished: onFinished, onResize: onResize)
        }
        .studyReportsHeight(onResize)
        .onChange(of: run.visible) { _, _ in onResize() }
    }
}

/// The pill, styled like "Whole course". It opens or closes the course's card.
struct StudyFindMaterialsButton: View {
    let course: StudyCourse
    var canvasLinks: [String] = []
    var onFinished: () async -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    var body: some View {
        let target = StudyFindTarget(course: course, canvasLinks: canvasLinks)
        StudyChrome.pill(StudyFindText.button, icon: "doc.text.magnifyingglass") {
            guard let target else { return }
            let run = StudyFindRun.shared(for: target)
            withAnimation(Theme.Motion.panel(reduceMotion: reduceMotion)) {
                if run.visible, !run.running {
                    run.close()
                } else {
                    run.show(onFinished: onFinished)
                }
            }
        }
        .disabled(target == nil)
        .help(target == nil ? StudyFindText.noCanvasLink : StudyFindText.help)
        .accessibilityLabel("Find materials for \(course.name)")
        .accessibilityHint("Opens the course's Modules page in your browser and lists what it links to")
    }
}

/// The card: consent, progress with Stop, the found list, and the result.
/// Shows only while its run is open. No scroll of its own: it sits inside the
/// home screen's capped scroll.
struct StudyFindMaterialsView: View {
    let course: StudyCourse
    var canvasLinks: [String] = []
    let onFinished: () async -> Void
    var onResize: () -> Void = {}

    var body: some View {
        if let target = StudyFindTarget(course: course, canvasLinks: canvasLinks) {
            StudyFindCard(run: StudyFindRun.shared(for: target), course: course,
                          onFinished: onFinished, onResize: onResize)
        }
    }
}

private struct StudyFindCard: View {
    @ObservedObject var run: StudyFindRun
    let course: StudyCourse
    let onFinished: () async -> Void
    let onResize: () -> Void

    @State private var expanded: Set<String> = []
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    /// Rows per kind before "Show all", so the card stays inside the panel's cap.
    private static let rows = 8

    private struct Group: Identifiable {
        let id: String
        let rows: [StudyFoundItem]
    }

    var body: some View {
        if run.visible {
            StudyChrome.card(padding: 10) {
                header
                content
            }
            .revealInPanel("study-find-\(course.id)")
            .transition(.opacity)
            .studyReportsHeight(onResize)
            .onAppear { run.setFinished(onFinished) }
            .onChange(of: run.phase) { _, _ in onResize() }
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: "doc.text.magnifyingglass").font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .accessibilityHidden(true)
            Text("\(StudyFindText.button) · \(course.shortName)")
                .font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1).truncationMode(.tail)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 6)
            if !run.running {
                NotchChrome.iconButton("xmark", help: "Close Find materials") { set { run.close() } }
                    .accessibilityLabel("Close Find materials")
            }
        }
    }

    @ViewBuilder private var content: some View {
        switch run.phase {
        case .idle:
            EmptyView()
        case .consent:
            consent
        case .needsAccess:
            needsAccess
        case .working:
            working
        case .found:
            found
        case .adding:
            adding
        case .done:
            done
        case .failed(let message):
            failed(message)
        }
    }

    private var consent: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "lock").font(.wisp(size: 9)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                Text(StudyFindText.consent).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                StudyChrome.pill("Continue", primary: true) { run.consent() }
                    .accessibilityHint("Opens this course's Canvas pages in your browser and reads their links")
                StudyChrome.pill("Cancel") { set { run.close() } }
                Spacer(minLength: 0)
            }
        }
    }

    private var needsAccess: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "hand.raised").font(.wisp(size: 10.5)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            Text(StudyText.needsAccessibilityNamed(name: PrivacyNames.accessibility, path: PrivacyNames.accessibilityPath))
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            VStack(alignment: .trailing, spacing: 4) {
                NotchChrome.small("Open Settings") {
                    // Through Permissions, like every privacy page Wisp opens.
                    Permissions.openSettingsPane(PrivacyNames.accessibilityPane)
                }
                NotchChrome.small(StudyText.tryAgainButton) { run.start() }
            }
        }
    }

    private var working: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 8) {
                ProgressView().controlSize(.small)
                Text(run.step.isEmpty ? StudyFindText.readingModules + "…" : run.step + "…")
                    .font(.wisp(.footnote)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 6)
                stopButton
            }
            .accessibilityElement(children: .contain)
            tabsLine
        }
    }

    private var stopButton: some View {
        StudyChrome.pill("Stop", icon: "stop.fill") { run.stop() }
            .accessibilityLabel("Stop finding materials")
    }

    @ViewBuilder private var tabsLine: some View {
        if run.tabsOpened > 0 {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "safari").font(.wisp(size: 9)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                Text(StudyFindText.tabs(opened: run.tabsOpened, closed: run.tabsClosed, problem: run.closeProblem,
                                        browser: run.browserName))
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private var found: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let note = run.note {
                Text(note).font(.wisp(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let counts = run.counts {
                Text(StudyFindText.summary(counts)).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if run.items.isEmpty {
                Text(StudyFindText.nothingFound).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(minHeight: 24)
            } else {
                list
            }
            tabsLine
            HStack(spacing: 6) {
                let count = run.chosen.count
                if !run.items.isEmpty {
                    StudyChrome.pill("Add (\(count))", icon: "plus", primary: true) { run.add() }
                        .disabled(count == 0)
                        .accessibilityLabel("Add \(count) \(count == 1 ? "item" : "items") to your sources")
                }
                StudyChrome.pill("Done") { set { run.close() } }
                Spacer(minLength: 0)
            }
        }
    }

    private var list: some View {
        let groups = StudyFindText.order.compactMap { kind -> Group? in
            let rows = run.items.filter { item in run.group(item) == kind }
            return rows.isEmpty ? nil : Group(id: kind, rows: rows)
        }
        return VStack(alignment: .leading, spacing: 2) {
            ForEach(groups) { group in
                let kind = group.id
                let rows = group.rows
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(StudyFindText.header(kind)) · \(rows.count)")
                        .font(.wisp(.caption, weight: .medium)).monospacedDigit().foregroundStyle(Theme.textMuted)
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: 6)
                    if rows.count > Self.rows {
                        Button(expanded.contains(kind) ? "Show fewer" : "Show all (\(rows.count))") {
                            set { if expanded.contains(kind) { expanded.remove(kind) } else { expanded.insert(kind) } }
                        }
                        .buttonStyle(StudyFindRowButtonStyle())
                        .font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textSecondary)
                        .frame(minHeight: 24)
                        .contentShape(Rectangle())
                    }
                }
                .padding(.horizontal, 8).padding(.top, 4)
                ForEach(expanded.contains(kind) ? rows : Array(rows.prefix(Self.rows))) { item in
                    StudyFoundRow(item: item, checked: run.selected.contains(item.id),
                                  blocker: run.blocker(item)) { run.toggle(item) }
                }
            }
        }
        .padding(.horizontal, -8)
    }

    private var adding: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 8) {
                ProgressView().controlSize(.small)
                Text(run.step + "…").font(.wisp(.footnote)).monospacedDigit().foregroundStyle(Theme.textSecondary)
                    .lineLimit(1)
                Spacer(minLength: 6)
                stopButton
            }
            tabsLine
        }
    }

    private var done: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let result = run.result {
                Text(StudyFindText.result(result))
                    .font(.wisp(.footnote, weight: .medium)).monospacedDigit().foregroundStyle(Theme.textPrimary)
                if result.stopped {
                    Text(StudyFindText.addStopped).font(.wisp(.caption)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let problem = result.problem {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "exclamationmark.circle").font(.wisp(size: 10)).foregroundStyle(Theme.warn)
                            .accessibilityHidden(true)
                        Text(problem).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                ForEach(Array(result.failures.enumerated()), id: \.offset) { _, failure in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "exclamationmark.circle").font(.wisp(size: 9)).foregroundStyle(Theme.warn)
                            .accessibilityHidden(true)
                        Text(run.title(forFailure: failure) + ": " + StudyFindText.failureLine(failure))
                            .font(.wisp(.caption)).foregroundStyle(Theme.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            tabsLine
            HStack(spacing: 6) {
                if run.items.contains(where: { run.blocker($0) == nil }) {
                    StudyChrome.pill("Back to list") { run.reopenList() }
                }
                StudyChrome.pill("Done", primary: true) { set { run.close() } }
                Spacer(minLength: 0)
            }
        }
    }

    private func failed(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "exclamationmark.circle").font(.wisp(size: 10.5)).foregroundStyle(Theme.warn)
                    .accessibilityHidden(true)
                Text(message).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let counts = run.counts {
                Text(StudyFindText.summary(counts)).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            tabsLine
            HStack(spacing: 6) {
                StudyChrome.pill(StudyText.tryAgainButton) { run.start() }
                StudyChrome.pill("Close") { set { run.close() } }
                Spacer(minLength: 0)
            }
        }
    }

    private func set(_ change: () -> Void) {
        withAnimation(Theme.Motion.panel(reduceMotion: reduceMotion), change)
    }
}

extension StudyFindRun {
    /// From the result back to the list, to add more of what is left.
    func reopenList() {
        guard phase == .done else { return }
        result = nil
        selected = Set(items.filter { blocker($0) == nil }.map(\.id))
        phase = .found
    }
}

/// One found item: checkbox, title, then its host or why it can't be added.
private struct StudyFoundRow: View {
    let item: StudyFoundItem
    let checked: Bool
    let blocker: String?
    let onToggle: () -> Void
    @State private var hovering = false

    private var title: String {
        let trimmed = item.title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? (item.host.isEmpty ? item.url : item.host) : trimmed
    }

    private var titleText: some View {
        Text(title).font(.wisp(.body, weight: .medium))
            .foregroundStyle(blocker == nil ? Theme.textPrimary : Theme.textSecondary)
            .lineLimit(1)
    }

    private var hostText: some View {
        Text(item.host).font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
    }

    private var glyph: String {
        if item.known { return "checkmark" }
        if blocker != nil { return "minus.square" }
        return checked ? "checkmark.square.fill" : "square"
    }

    var body: some View {
        Button(action: onToggle) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: glyph).font(.wisp(size: 11))
                    .foregroundStyle(blocker == nil ? Theme.textPrimary : Theme.textMuted)
                    .frame(width: 16)
                    .accessibilityHidden(true)
                Image(systemName: StudyFindText.symbol(item.kind)).font(.wisp(size: 10))
                    .foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                // Title and host on one line when they fit; else the host on
                // a second muted line, never squeezed to a letter.
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        titleText.fixedSize()
                        Spacer(minLength: 8)
                        hostText.fixedSize()
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        titleText.truncationMode(.middle)
                        hostText.truncationMode(.middle)
                    }
                }
                .layoutPriority(1)
                if let note = blocker ?? (item.recording && !item.known ? StudyFindText.recordingLabel : nil) {
                    Spacer(minLength: 4)
                    Text(note).font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textMuted)
                        .lineLimit(1).fixedSize()
                }
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyFindRowButtonStyle())
        .disabled(blocker != nil)
        .padding(.horizontal, 8).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: 8)
            .fill(hovering && blocker == nil ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
        .help(blocker ?? item.url)
        .accessibilityLabel("\(StudyFindText.kindName(item.kind)): \(title), \(item.host)")
        .accessibilityValue(blocker ?? (checked ? "Selected" : "Not selected"))
        .accessibilityAddTraits(checked && blocker == nil ? [.isButton, .isSelected] : .isButton)
        .accessibilityHint(blocker == nil ? "Chooses whether Wisp adds it to your sources" : "")
    }
}

/// No press flash: the label as it is (as NotchStudyView's row style).
private struct StudyFindRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { configuration.label }
}
