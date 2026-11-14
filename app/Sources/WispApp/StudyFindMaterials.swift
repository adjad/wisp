import AppKit
import ApplicationServices
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
// activating it. No AppleScript, Apple Events, JavaScript, key or mouse events,
// and no Accessibility actions; the only Accessibility write is
// `AXManualAccessibility = true`, exactly as StudyPageReader does for Chromium.
// Wisp never opens quizzes, assignments or outside tools (External Tool / LTI
// items): Canvas would post the student's identity to the tool's site, start
// the quiz, or launch an assignment's tool as its page opens. Each link is
// sent to resolve with its Canvas module item type so the server drops tools
// and quizzes; only items it returns as via "browser" are visited, by an
// allowlist: direct `/pages/…` and `/discussion_topics/…` links, and
// `/modules/items/<n>` only when its type was read as Page, Discussion or
// External URL (an unreadable type is refused, and a refused type is never
// replaced by a plainer label); and the client refuses any path with a tool,
// LTI, quiz or `assignments` segment and ignores what such a page holds
// (assignment instructions already come from Study's own dashboard read).
// Course pages Wisp does open may still load tools embedded in them (a video
// player, say); the consent card says so. A visit is skipped when the tab
// snapshot before it didn't finish within its time budget. Only tabs new
// since Wisp's snapshot (or exactly at the opened URL) are read, so tabs the
// student already had open never are. Accepted gap: a background tab the
// student switches to during the wait looks new and may be read, but only if
// it is on this same course (it is the student's own course page). An
// External URL item that lands off the course gives only its address to
// resolve, and only when exactly one new tab appeared since the snapshot. Nothing is added until the
// student ticks it and presses Add. Under WISP_NOTCH_QA the reader returns
// nothing and opening is a no-op.

// MARK: - Allow-check

/// The course's Canvas site and course number, parsed from the course's
/// stored `url` (never from `StudyCourse.id`), and the one rule every URL Wisp
/// opens or reads must pass.
struct StudyFindTarget: Equatable, Sendable {
    /// Lowercased Canvas host, e.g. "canvas.school.edu".
    let host: String
    /// The digits in `/courses/<n>`.
    let canvasID: String
    /// `StudyCourse.id`, sent as `course_id`.
    let courseID: String

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

    /// Host and `/courses/<n>` id of an https Canvas course URL.
    private static func courseRef(_ raw: String) -> (host: String, id: String)? {
        guard let parts = Self.parts(raw) else { return nil }
        let segments = parts.segments
        guard segments.count >= 2, segments[0] == "courses", Self.isDigits(segments[1]) else { return nil }
        return (parts.host, segments[1])
    }

    init(host: String, canvasID: String, courseID: String) {
        self.host = host.lowercased()
        self.canvasID = canvasID
        self.courseID = courseID
    }

    /// `https://<host>/courses/<id>/modules`.
    var modulesURL: URL? {
        var c = URLComponents()
        c.scheme = "https"
        c.host = host
        c.path = "/courses/\(canvasID)/modules"
        return c.url
    }

    /// The URL to open when `raw` is https on the course's host, default port,
    /// no user info, a clean path under `/courses/<id>/`; else nil.
    func allowedURL(_ raw: String) -> URL? {
        guard let parts = Self.parts(raw), parts.host == host else { return nil }
        let s = parts.segments
        guard s.count >= 2, s[0] == "courses", s[1] == canvasID else { return nil }
        return parts.url
    }

    /// The course's own navigation sections Wisp reads, besides Modules: Canvas's
    /// list pages, read-only. Opening a list never opens an item on it.
    static let sections: [(name: String, path: [String])] = [
        ("Home", []), ("Announcements", ["announcements"]), ("Syllabus", ["assignments", "syllabus"]),
        ("Assignments", ["assignments"]), ("Pages", ["pages"]), ("Discussions", ["discussion_topics"]),
        ("Files", ["files"]), ("Grades", ["grades"]),
    ]
    private static let sectionPaths: Set<[String]> = Set(sections.map(\.path) + [["modules"], ["wiki"]])

    /// Each section's address on this course.
    var sectionURLs: [(name: String, url: URL)] {
        Self.sections.compactMap { section in
            var c = URLComponents()
            c.scheme = "https"
            c.host = host
            c.path = "/" + (["courses", canvasID] + section.path).joined(separator: "/")
            guard let url = c.url, isNavSection(url.absoluteString) else { return nil }
            return (section.name, url)
        }
    }

    /// Exactly one of the course's section list pages (Home, Announcements,
    /// Syllabus, the Assignments list, Pages, Discussions, Files, Grades, Modules).
    func isNavSection(_ raw: String) -> Bool {
        guard let parts = Self.parts(raw), parts.host == host else { return false }
        let s = parts.segments
        guard s.count >= 2, s[0] == "courses", s[1] == canvasID else { return false }
        return Self.sectionPaths.contains(Array(s.dropFirst(2)))
    }

    /// A page on the course that launches a tool, LTI, quiz or assignment item.
    /// A section list page (the Assignments list, the Syllabus) is not one.
    func isLaunchPage(_ raw: String) -> Bool {
        Self.launchesTool(raw) && !isNavSection(raw)
    }

    func allows(_ raw: String) -> Bool { allowedURL(raw) != nil }

    /// What Wisp may open: an allowed URL that doesn't launch a tool or quiz.
    /// Checked before every open, whatever the server said.
    func openableURL(_ raw: String) -> URL? {
        guard !Self.launchesTool(raw) else { return nil }
        return allowedURL(raw)
    }

    /// Path segments of pages that launch an External Tool / LTI or a quiz,
    /// where Canvas posts the student's identity to another site (or starts
    /// the quiz), and assignments (an External Tool assignment or a New Quiz
    /// launches its tool as the page opens). Wisp never opens these, nor uses
    /// anything read from them. Study already reads assignment instructions
    /// through its own dashboard read.
    private static let launchSegments: Set<String> = [
        "external_tools", "quizzes", "lti", "quiz_lti", "quizzes_next", "lti_resource_links",
        "external_content", "launch", "assignments",
    ]

    /// True when `raw`'s path has a tool, LTI or quiz segment (any case), or
    /// when it can't be parsed at all.
    static func launchesTool(_ raw: String) -> Bool {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return true }
        let path = c.percentEncodedPath.lowercased()
        return path.split(separator: "/").contains { segment in
            launchSegments.contains(String(segment)) || segment.hasPrefix("lti")
        }
    }

    /// Exactly the course's Modules page, `/courses/<id>/modules`.
    func isModules(_ raw: String) -> Bool {
        guard let parts = Self.parts(raw), parts.host == host else { return false }
        let s = parts.segments
        return s.count == 3 && s[0] == "courses" && s[1] == canvasID && s[2] == "modules"
    }

    /// Same page: scheme and host in any case, no fragment, no trailing "/".
    static func samePage(_ a: String, _ b: String) -> Bool {
        func normal(_ raw: String) -> String? {
            guard var c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
            c.fragment = nil
            c.scheme = c.scheme?.lowercased()
            c.host = c.host?.lowercased()
            var path = c.percentEncodedPath
            while path.count > 1, path.hasSuffix("/") { path.removeLast() }
            c.percentEncodedPath = path
            return c.string
        }
        guard let x = normal(a), let y = normal(b) else { return false }
        return x == y
    }

    /// Host and path without query or fragment, lowercased: how a link Wisp
    /// sent is matched to the item the server returned (it drops Canvas queries).
    static func pathKey(_ raw: String) -> String {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return raw }
        var path = c.percentEncodedPath
        while path.count > 1, path.hasSuffix("/") { path.removeLast() }
        return (c.host ?? "").lowercased() + path.lowercased()
    }

    struct Parts {
        let url: URL
        let host: String
        /// Path segments, percent-encoded, without empty ones at the end.
        let segments: [String]
    }

    /// Parsed with URLComponents, never by string prefix. Refuses anything not
    /// https, a non-default port, user or password, an encoded "/" "." or "\",
    /// and "." or ".." or empty segments inside the path.
    static func parts(_ raw: String) -> Parts? {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme?.lowercased() == "https",
              c.user == nil, c.password == nil,
              let host = c.host?.lowercased(), !host.isEmpty,
              c.port == nil || c.port == 443,
              let url = c.url
        else { return nil }
        let path = c.percentEncodedPath
        let lower = path.lowercased()
        if lower.contains("%2f") || lower.contains("%2e") || lower.contains("%5c") || path.contains("\\") {
            return nil
        }
        if path.isEmpty { return Parts(url: url, host: host, segments: []) }
        guard path.hasPrefix("/") else { return nil }
        var segments = path.split(separator: "/", omittingEmptySubsequences: false).dropFirst().map(String.init)
        while segments.last == "" { segments.removeLast() }
        if segments.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) { return nil }
        return Parts(url: url, host: host, segments: segments)
    }

    private static func isDigits(_ text: String) -> Bool {
        !text.isEmpty && text.count <= 20 && text.allSatisfy { $0.isASCII && $0.isNumber }
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
    static func scan(text: Bool, known: [StudyFindTab],
                     want: @escaping @Sendable (_ isNew: Bool, _ url: String) -> Want) async -> Scan {
        #if WISP_NOTCH_QA
        return Scan(pages: [], complete: true)
        #else
        guard AXIsProcessTrusted() else { return Scan(pages: [], complete: true) }
        let pids = await browserPIDs()
        guard !pids.isEmpty else { return Scan(pages: [], complete: true) }
        return await Task.detached(priority: .userInitiated) { () -> Scan in
            StudyFindReader.scanAll(pids, text: text, known: known, want: want)
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
    private static func scanAll(_ pids: [pid_t], text: Bool, known: [StudyFindTab],
                                want: (Bool, String) -> Want) -> Scan {
        let clock = Clock(deadline: now() + totalBudgetNanoseconds)
        var pages: [StudyFindPage] = []
        let complete = eachFrontArea(pids, deadline: { clock.deadline }) { window, area in
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
                var w = Walk(deadline: started + walkBudgetNanoseconds, base: URL(string: url), wantText: text)
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
    }

    /// Document order, depth first. Links give URL and title; nested web areas
    /// their URL; static text the page text (when wanted). Password and text
    /// fields are never read.
    private static func harvest(_ element: AXUIElement, into w: inout Walk, depth: Int, root: Bool) {
        guard w.visited < maxElements, now() < w.deadline else {
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
                let title = clean(stringValue(element, kAXTitleAttribute as String))
                    ?? clean(description) ?? clean(inner) ?? ""
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

    /// Module item types that launch a tool or quiz (the server drops them too).
    static func isLaunchType(_ type: String) -> Bool {
        let key = typeKey(type)
        return key == "externaltool" || key == "contextexternaltool" || key == "lti" || key.hasPrefix("quiz")
            || key == "newquiz" || key == "newquizzes" || key == "assessment"
    }

    /// Module item types Wisp never opens: tools, quizzes and assignments.
    static func isNeverOpened(_ type: String) -> Bool {
        isLaunchType(type) || typeKey(type) == "assignment"
    }

    /// The only module item types Wisp opens behind `/modules/items/<n>`:
    /// pages, discussions and external URLs. Unknown, File, Quiz, Assignment,
    /// ExternalTool and SubHeader are all refused.
    static func isOpenableModuleType(_ type: String?) -> Bool {
        guard let type else { return false }
        return ["page", "wikipage", "discussion", "discussiontopic", "externalurl"].contains(typeKey(type))
    }

    static func isExternalURLType(_ type: String?) -> Bool {
        type.map { typeKey($0) == "externalurl" } ?? false
    }

    /// Lower-case ASCII letters only: "External URL", "external_url" and
    /// "ExternalUrl" are all "externalurl".
    static func typeKey(_ type: String) -> String {
        String(type.lowercased().filter { $0.isASCII && $0.isLetter })
    }

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

// MARK: - Backend shapes

/// One classified link from `POST /study/find/resolve`.
struct StudyFoundItem: Decodable, Equatable, Identifiable, Sendable {
    let id: String
    let kind: String
    let title: String
    let url: String
    let via: String
    var known: Bool

    enum CodingKeys: String, CodingKey { case id, kind, title, url, via, known }

    init(id: String, kind: String, title: String, url: String, via: String, known: Bool) {
        self.id = id
        self.kind = kind
        self.title = title
        self.url = url
        self.via = via
        self.known = known
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        url = try c.decode(String.self, forKey: .url)
        kind = ((try? c.decodeIfPresent(String.self, forKey: .kind)) ?? nil)?.lowercased() ?? "other"
        title = ((try? c.decodeIfPresent(String.self, forKey: .title)) ?? nil) ?? ""
        via = ((try? c.decodeIfPresent(String.self, forKey: .via)) ?? nil)?.lowercased() ?? "none"
        known = ((try? c.decodeIfPresent(Bool.self, forKey: .known)) ?? nil) ?? false
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
    static let consentKey = "study.findMaterials.consented.v3"  // v3: the course's sections too
    static let consent = "Wisp will open this course's Modules page and its sections (Home, Announcements, Syllabus, "
        + "Assignments, Pages, Discussions, Files, Grades) in your browser, read the links on them and on up "
        + "to 100 of the course's own pages, and show you what it found. Each page opens as a tab you close yourself. "
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
        default: return "Other"
        }
    }
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
    @Published private(set) var phase: Phase = .idle
    @Published private(set) var step = ""
    @Published private(set) var tabsOpened = 0
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
        counts = nil
        titlesByURL = [:]
        sentTypes = [:]
        phase = .working
        step = StudyFindText.openingModules
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
            let pages = await StudyFindReader.scan(text: false, known: beforeModules) { isNew, url in
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
            guard case .course(let landed)? = landing, t.allows(landed.url), !t.isLaunchPage(landed.url)
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
        let visits = Self.visits(found, target: t, types: sentTypes)
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
            guard let url = t.openableURL(item.url) else { continue }
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
            // Only an External URL item may land off the course.
            let allowsOff = StudyFindReader.isExternalURLType(sentTypes[StudyFindTarget.pathKey(item.url)])
            let landing = await land(opened: url.absoluteString, before: before, allowsOff: allowsOff, gen: gen,
                                     until: min(runDeadline, Date().addingTimeInterval(Self.pageWait)))
            guard live(gen) else { return }
            switch landing {
            case .course(let landed)?:
                // Re-checked before anything read from the landed page is
                // used: this course, and not a tool or quiz launch page (its
                // links and frames are ignored too).
                guard t.allows(landed.url), !t.isLaunchPage(landed.url) else { continue }
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
    /// Page, Discussion or External URL (unknown, File, Quiz, Assignment,
    /// External Tool and SubHeader are all refused). Nothing else is opened.
    private static func visits(_ items: [StudyFoundItem], target: StudyFindTarget,
                               types: [String: String]) -> [StudyFoundItem] {
        func rank(_ item: StudyFoundItem) -> Int? {
            guard item.via == "browser", !item.known, let parts = StudyFindTarget.parts(item.url),
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
            let scan = await StudyFindReader.scan(text: true, known: before) { isNew, url in
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

    private func open(_ url: URL) async -> Bool {
        let ok = await opener(url)
        if ok { tabsOpened += 1 }
        return ok
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
        if item.known { return StudyFindText.added }
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
            .onExitCommand { if !run.running { set { run.close() } } }
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
                    .accessibilityHint("Opens the Modules page in your browser and reads its links")
                StudyChrome.pill("Cancel") { set { run.close() } }
                Spacer(minLength: 0)
            }
        }
    }

    private var needsAccess: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "hand.raised").font(.wisp(size: 10.5)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            Text(StudyText.needsAccessibility)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            VStack(alignment: .trailing, spacing: 4) {
                NotchChrome.small("Open Settings") {
                    // Through Permissions, like every privacy page Wisp opens.
                    Permissions.openSettingsPane("com.apple.preference.security?Privacy_Accessibility")
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
                Text(StudyFindText.tabs(run.tabsOpened))
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
            let rows = run.items.filter { item in
                (StudyFindText.order.contains(item.kind) ? item.kind : "other") == kind
            }
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
                Text(title).font(.wisp(.body, weight: .medium))
                    .foregroundStyle(blocker == nil ? Theme.textPrimary : Theme.textSecondary)
                    .lineLimit(1).truncationMode(.middle)
                    .layoutPriority(1)
                Spacer(minLength: 8)
                if let blocker {
                    Text(blocker).font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textMuted)
                        .lineLimit(1).fixedSize()
                }
                Text(item.host).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .lineLimit(1).truncationMode(.head)
                    .frame(maxWidth: 120, alignment: .trailing)
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
