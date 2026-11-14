import AppKit
import ApplicationServices
import Foundation
import SwiftUI

// Study's "Find materials" (Phase 1): for one course, Wisp opens the course's
// Canvas Modules page in the student's own signed-in browser, reads its links
// through Accessibility, visits up to 10 of the course's own item pages one
// level deep, asks the backend to classify what it found
// (`POST /study/find/resolve`), shows the results grouped by kind with
// checkboxes, and on Add imports the ticked ones (`POST /study/find/add`).
//
// Call site (the integrator adds it; nothing else needs wiring). In
// NotchStudyView.swift, `content(_:)`, on the line right after
// `StudyMakeOptions(study: study, state: state, course: selectedCourse(state))`:
//
//     if let course = selectedCourse(state) { StudyFindMaterialsSection(course: course, onFinished: { await study.refresh() }, onResize: model.onResize) }
//
// `StudyFindMaterialsSection` is the "Find materials" pill plus the card under
// it. For other layouts the pieces are public too:
// `StudyFindMaterialsButton(course:)` toggles the card, and
// `StudyFindMaterialsView(course:onFinished:)` is the card itself (it shows
// only while its run is open). State refresh: StudyModel's `state` is
// `private(set)` and its transport private, so `onFinished` should call
// `StudyModel.refresh()` (internal, re-reads `GET /study/state`); the `state`
// the add route returns is not applied here.
//
// Safety, by design: the only thing Wisp does to the browser is open https
// URLs that pass `StudyFindTarget`'s allow-check (the course's Modules page and
// at most 10 of the course's own pages) through `NSWorkspace.open` without
// activating it. No AppleScript, Apple Events, JavaScript, key or mouse events,
// and no Accessibility actions; the only Accessibility write is
// `AXManualAccessibility = true`, exactly as StudyPageReader does for Chromium.
// Nothing is added until the student ticks it and presses Add. Under
// WISP_NOTCH_QA the reader returns nothing and opening is a no-op.

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

    init?(course: StudyCourse) {
        guard !course.sample, let raw = course.url, let parts = Self.parts(raw) else { return nil }
        let segments = parts.segments
        guard segments.count >= 2, segments[0] == "courses", Self.isDigits(segments[1]) else { return nil }
        host = parts.host
        canvasID = segments[1]
        courseID = course.id
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
        guard s.count >= 3, s[0] == "courses", s[1] == canvasID else { return nil }
        return parts.url
    }

    func allows(_ raw: String) -> Bool { allowedURL(raw) != nil }

    /// The course's Modules page (or a deeper path under it).
    func isModules(_ raw: String) -> Bool {
        guard let parts = Self.parts(raw), parts.host == host else { return false }
        let s = parts.segments
        return s.count >= 3 && s[0] == "courses" && s[1] == canvasID && s[2] == "modules"
    }

    /// Any https URL without user info: where an allowed item may redirect.
    static func isPlainHTTPS(_ raw: String) -> Bool { parts(raw) != nil }

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

/// One browser window's front tab.
struct StudyFindPage: Sendable {
    /// "<pid>:<window index>", to tell a tab that changed from one that didn't.
    let slot: String
    let url: String
    let links: [StudyFindLink]
    /// AXURLs of web areas nested in the page (iframes, e.g. embedded Slides).
    let frames: [String]
    let text: String
}

// MARK: - Accessibility reader

/// Read-only Accessibility walk of every browser window's front tab, after
/// StudyPageReader (whose helpers are private): short messaging timeouts and
/// overall time, element and depth budgets. Unlike StudyPageReader it walks
/// the whole web area, so links and nested web areas (iframes) are found.
enum StudyFindReader {
    static let maxLinks = 400
    static let maxCharacters = 150_000

    /// Front tabs whose slot and URL pass `match`; only those are walked.
    /// With `walk` false only URLs are read.
    static func scan(walk: Bool, text: Bool,
                     match: @escaping @Sendable (_ slot: String, _ url: String) -> Bool) async -> [StudyFindPage] {
        #if WISP_NOTCH_QA
        return []
        #else
        guard AXIsProcessTrusted() else { return [] }
        let pids: [pid_t] = await MainActor.run { () -> [pid_t] in
            let me = ProcessInfo.processInfo.processIdentifier
            return NSWorkspace.shared.runningApplications.compactMap { app -> pid_t? in
                guard app.processIdentifier != me, !app.isTerminated,
                      let bundle = app.bundleIdentifier, StudyFindReader.isBrowser(bundle)
                else { return nil }
                return app.processIdentifier
            }
        }
        guard !pids.isEmpty else { return [] }
        return await Task.detached(priority: .userInitiated) { () -> [StudyFindPage] in
            StudyFindReader.scanAll(pids, walk: walk, text: text, match: match)
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
    private static let maxElements = 25_000
    private static let maxDepth = 80
    private static let maxSearchElements = 600
    private static let maxSearchDepth = 14
    private static let maxWindowsPerApp = 40
    private static let maxFrames = 100
    private static let maxTitle = 300

    private static func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

    /// Never call on the main thread.
    private static func scanAll(_ pids: [pid_t], walk: Bool, text: Bool,
                                match: (String, String) -> Bool) -> [StudyFindPage] {
        let deadline = now() + totalBudgetNanoseconds
        var pages: [StudyFindPage] = []
        for pid in pids {
            guard now() < deadline else { break }
            let app = AXUIElementCreateApplication(pid)
            AXUIElementSetMessagingTimeout(app, messagingTimeout)
            // Chromium apps expose an empty tree until this is set (as
            // StudyPageReader and PageReader do). Nothing else is ever written.
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
            let windows: [AXUIElement] = elements(app, kAXWindowsAttribute as String)
            for (index, window) in windows.prefix(maxWindowsPerApp).enumerated() {
                guard now() < deadline else { break }
                AXUIElementSetMessagingTimeout(window, messagingTimeout)
                guard let area = webArea(in: window, deadline: deadline) else { continue }
                let url = pageURL(area: area, window: window)
                let slot = "\(pid):\(index)"
                guard !url.isEmpty, match(slot, url) else { continue }
                guard walk else {
                    pages.append(StudyFindPage(slot: slot, url: url, links: [], frames: [], text: ""))
                    continue
                }
                var w = Walk(deadline: deadline, base: URL(string: url), wantText: text)
                harvest(area, into: &w, depth: 0, root: true)
                var body = w.lines.joined(separator: "\n")
                if body.count > maxCharacters { body = String(body.prefix(maxCharacters)) }
                pages.append(StudyFindPage(slot: slot, url: url, links: w.links, frames: w.frames, text: body))
            }
        }
        return pages
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
        let deadline: UInt64
        let base: URL?
        let wantText: Bool
    }

    /// Document order, depth first. Links give URL and title; nested web areas
    /// their URL; static text the page text (when wanted). Password and text
    /// fields are never read.
    private static func harvest(_ element: AXUIElement, into w: inout Walk, depth: Int, root: Bool) {
        guard depth < maxDepth, w.visited < maxElements, now() < w.deadline else { return }
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

        if role == (kAXStaticTextRole as String) {
            if let value = stringValue(element, kAXValueAttribute as String)
                ?? stringValue(element, kAXTitleAttribute as String) {
                if let type = moduleType(value) {
                    w.pendingType = type
                    w.pendingAt = w.visited
                }
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
                let title = clean(stringValue(element, kAXTitleAttribute as String))
                    ?? clean(description) ?? clean(inner) ?? ""
                var type: String? = nil
                if let pending = w.pendingType, w.visited - w.pendingAt < 60 { type = pending }
                if type == nil, let description { type = moduleType(description) }
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

    /// Canvas module rows carry a hidden type label next to their icon.
    private static let typeLabels: [String: String] = [
        "page": "Page", "wiki page": "Page",
        "file": "File", "attachment": "File",
        "discussion": "Discussion", "discussion topic": "Discussion",
        "assignment": "Assignment", "quiz": "Quiz",
        "external url": "ExternalUrl", "external link": "ExternalUrl",
        "external tool": "ExternalTool",
    ]

    private static func moduleType(_ text: String) -> String? {
        let key = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard key.count <= 20 else { return nil }
        return typeLabels[key]
    }

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
    static let help = "Opens this course's Modules page in your browser and finds its slides, docs and readings"
    static let consentKey = "study.findMaterials.consented"
    static let consent = "Wisp will open this course's Modules page in your browser, read the links on it and on up "
        + "to 10 of the course's own pages, and show you what it found. Nothing is added until you choose. "
        + "Linked Google files are read with your Drive sign-in; public articles are fetched without your cookies."
    static let noCanvasLink = "Wisp doesn't have a Canvas link for this course yet. Read your school tabs first."
    static let openingModules = "Opening the Modules page in your browser"
    static let readingModules = "Reading the Modules page"
    static func sorting(_ n: Int) -> String { "Sorting \(n) \(n == 1 ? "link" : "links")" }
    static func readingPage(_ i: Int, of n: Int) -> String { "Reading course page \(i) of \(n)" }
    static let sortingMore = "Sorting what the course pages link to"
    static let couldntOpen = "Wisp couldn't open the Modules page in your browser. Try again."
    static let couldntRead = "Wisp couldn't read the Modules page. Make sure you're signed in to Canvas in your "
        + "browser and the page has finished loading, then try again."
    static func tabs(_ n: Int) -> String {
        "Wisp opened \(n) \(n == 1 ? "tab" : "tabs") in your browser — you can close \(n == 1 ? "it" : "them")."
    }
    static let nothingFound = "Wisp didn't find any course materials on the Modules page."
    static let stoppedEarly = "Stopped. This is what Wisp found so far."
    static let stopped = "Stopped. Nothing was added."
    static let timeCap = "Wisp stopped looking after 5 minutes. This is what it found."
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

    let target: StudyFindTarget
    private let transport: Transport
    private let opener: Opener
    private var task: Task<Void, Never>?
    /// Bumped on every start and stop, so a stopped run's late answers drop.
    private var generation = 0
    private var onFinished: (() async -> Void)?
    private var titlesByURL: [String: String] = [:]

    static let maxVisits = 10
    static let runSeconds: TimeInterval = 300
    static let modulesWait: TimeInterval = 25
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
        titlesByURL = [:]
        phase = .working
        step = StudyFindText.openingModules
        task = Task { [weak self] in await self?.walk(gen) }
    }

    private func live(_ gen: Int) -> Bool { gen == generation && !Task.isCancelled }

    // MARK: Walk

    private func walk(_ gen: Int) async {
        let runDeadline = Date().addingTimeInterval(Self.runSeconds)
        let t = target
        guard let modules = t.modulesURL, t.allows(modules.absoluteString) else {
            phase = .failed(StudyFindText.noCanvasLink)
            return
        }

        // 1. Open the Modules page and wait for it in a front tab.
        guard await open(modules) else {
            if live(gen) { phase = .failed(StudyFindText.couldntOpen) }
            return
        }
        guard live(gen) else { return }
        step = StudyFindText.readingModules
        var page: StudyFindPage?
        let modulesDeadline = Date().addingTimeInterval(Self.modulesWait)
        while page == nil, Date() < modulesDeadline {
            let pages = await StudyFindReader.scan(walk: true, text: false) { _, url in t.isModules(url) }
            guard live(gen) else { return }
            page = pages.first { !$0.links.isEmpty }
            if page == nil {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard live(gen) else { return }
            }
        }
        guard let page else {
            phase = .failed(StudyFindText.couldntRead)
            return
        }

        // 2. Classify its links.
        var sent = Set<String>()
        let first = Self.links(page.links, frames: page.frames, except: &sent)
        step = StudyFindText.sorting(first.count)
        guard let found = await resolve(first, gen: gen, quiet: false) else { return }
        guard live(gen) else { return }
        merge(found)

        // 3. One level deep: up to 10 of the course's own pages.
        let visits = Self.visits(items, target: t)
        var deeper: [StudyFindLink] = []
        var deeperFrames: [String] = []
        for (index, item) in visits.enumerated() {
            guard live(gen) else { return }
            if Date() >= runDeadline {
                note = StudyFindText.timeCap
                break
            }
            // Re-checked here, whatever the server said.
            guard let url = t.allowedURL(item.url) else { continue }
            step = StudyFindText.readingPage(index + 1, of: visits.count)
            let before = await StudyFindReader.scan(walk: false, text: false) { _, _ in true }
            guard live(gen) else { return }
            guard await open(url) else { continue }
            guard live(gen) else { return }
            let landed = await land(opened: url.absoluteString, before: before, gen: gen,
                                    until: min(runDeadline, Date().addingTimeInterval(Self.pageWait)))
            guard live(gen) else { return }
            guard let landed else { continue }
            if (item.kind == "page" || item.kind == "discussion"), !landed.page.text.isEmpty {
                texts[item.id] = String(landed.page.text.prefix(StudyFindReader.maxCharacters))
            }
            // Links are kept only from pages on the course itself, never from
            // where a redirect went.
            if landed.allowed {
                deeper += landed.page.links
                deeperFrames += landed.page.frames
            }
        }

        // 4. Classify what those pages link to, then show it all.
        let second = Self.links(deeper, frames: deeperFrames, except: &sent)
        if !second.isEmpty, live(gen), Date() < runDeadline {
            step = StudyFindText.sortingMore
            if let more = await resolve(second, gen: gen, quiet: true), live(gen) { merge(more) }
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

    /// The browser items worth opening: specific course pages and
    /// discussions first, then module item links; never files or anything
    /// off the course.
    private static func visits(_ items: [StudyFoundItem], target: StudyFindTarget) -> [StudyFoundItem] {
        func rank(_ item: StudyFoundItem) -> Int? {
            guard item.via == "browser", !item.known, let parts = StudyFindTarget.parts(item.url),
                  target.allows(item.url) else { return nil }
            let s = parts.segments
            let moduleItem = s.count >= 5 && s[2] == "modules" && s[3] == "items"
            if item.kind == "page" || item.kind == "discussion" {
                if moduleItem { return 1 }
                return s.count >= 4 ? 0 : 3
            }
            return moduleItem ? 2 : nil
        }
        return items.compactMap { item in rank(item).map { (item, $0) } }
            .enumerated()
            .sorted { ($0.element.1, $0.offset) < ($1.element.1, $1.offset) }
            .prefix(maxVisits)
            .map { $0.element.0 }
    }

    private struct Landing {
        let page: StudyFindPage
        let allowed: Bool
    }

    /// The front tab the open produced: a tab whose URL is new since `before`
    /// (or is the opened URL) on the course, else an https page a redirect
    /// reached in a tab that changed. Waits for its text to settle.
    private func land(opened: String, before: [StudyFindPage], gen: Int, until: Date) async -> Landing? {
        let t = target
        let started = Date()
        var known: [String: String] = [:]
        for page in before { known[page.slot] = page.url }
        let snapshot = known
        var lastCount: [String: Int] = [:]
        var fallback: Landing?
        while Date() < until {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard live(gen) else { return nil }
            // Only tabs the open changed are walked; other tabs are never read.
            let fresh = await StudyFindReader.scan(walk: true, text: true) { slot, url in
                (url == opened || snapshot[slot] != url)
                    && (t.allows(url) || StudyFindTarget.isPlainHTTPS(url))
            }
            guard live(gen) else { return nil }
            let elapsed = Date().timeIntervalSince(started)
            if let page = fresh.first(where: { t.allows($0.url) && !$0.text.isEmpty }) {
                let key = page.slot + " " + page.url
                let stillRedirecting = page.url.contains("/modules/items/") && elapsed < 8
                if !stillRedirecting, lastCount[key] == page.text.count || elapsed >= 10 {
                    return Landing(page: page, allowed: true)
                }
                lastCount[key] = page.text.count
                continue
            }
            // A redirect off the course: only a tab that changed since the
            // open, never the course host's own other pages, text only.
            if elapsed >= 3,
               let page = fresh.first(where: { !t.allows($0.url) && !$0.text.isEmpty
                   && StudyFindTarget.parts($0.url)?.host != t.host }) {
                fallback = Landing(page: page, allowed: false)
            }
        }
        return fallback
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
                for item in batch where addedURLs.contains(item.url)
                    || (addedURLs.isEmpty && !failedURLs.contains(item.url)) {
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
    let onFinished: () async -> Void
    var onResize: () -> Void = {}

    var body: some View {
        if let target = StudyFindTarget(course: course) {
            StudyFindMaterialsHost(run: StudyFindRun.shared(for: target), course: course,
                                   onFinished: onFinished, onResize: onResize)
                .id(course.id)
        }
    }
}

private struct StudyFindMaterialsHost: View {
    @ObservedObject var run: StudyFindRun
    let course: StudyCourse
    let onFinished: () async -> Void
    let onResize: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                StudyFindMaterialsButton(course: course, onFinished: onFinished)
                Spacer(minLength: 0)
            }
            StudyFindMaterialsView(course: course, onFinished: onFinished, onResize: onResize)
        }
        .studyReportsHeight(onResize)
        .onChange(of: run.visible) { _, _ in onResize() }
    }
}

/// The pill, styled like "Whole course". It opens or closes the course's card.
struct StudyFindMaterialsButton: View {
    let course: StudyCourse
    var onFinished: () async -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    var body: some View {
        let target = StudyFindTarget(course: course)
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
    let onFinished: () async -> Void
    var onResize: () -> Void = {}

    var body: some View {
        if let target = StudyFindTarget(course: course) {
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
    private static let rows = 6

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
        let groups = StudyFindText.order.compactMap { kind -> (String, [StudyFoundItem])? in
            let rows = run.items.filter { item in
                (StudyFindText.order.contains(item.kind) ? item.kind : "other") == kind
            }
            return rows.isEmpty ? nil : (kind, rows)
        }
        return VStack(alignment: .leading, spacing: 2) {
            ForEach(groups, id: \.0) { group in
                let kind = group.0
                let rows = group.1
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
                        Text(run.title(forFailure: failure) + ": "
                             + (failure.message.isEmpty ? StudyText.problem(code: failure.code) : failure.message))
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
