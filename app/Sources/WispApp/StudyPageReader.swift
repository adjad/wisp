import AppKit
import ApplicationServices
import Foundation

/// Reads the school pages the person has open, through Accessibility, for
/// Study's "Read my school tabs" (docs/STUDY_TAB.md).
///
/// For every running browser and every one of its windows, the front tab's web
/// area is read: its URL, title and visible text. Only school pages
/// (`isSchool(url:)`) are kept. Read-only by design: it never activates,
/// clicks, scrolls, types, or changes anything in the browser or on the page.
/// The walk follows PageReader's (its helpers are private there): short
/// per-element messaging timeouts, an overall time budget and an element
/// budget, so a huge page or a browser that is not answering cannot hang Wisp.
enum StudyPageReader {
    /// True once the person has granted Accessibility. Never prompts.
    static var hasAccessibility: Bool {
        #if WISP_NOTCH_QA
        return true
        #else
        return AXIsProcessTrusted()
        #endif
    }

    /// The backend's school-site rule, mirrored.
    static func isSchool(url: String) -> Bool {
        StudySchoolURL.isSchool(url)
    }

    /// The most pages one import sends (the backend takes 12).
    static let maxPages = 12
    /// The most text kept from one page.
    static let maxCharacters = 150_000

    /// Every open school page, front tab of each window. Empty without Accessibility.
    static func read() async -> [StudyPage] {
        #if WISP_NOTCH_QA
        return []
        #else
        guard AXIsProcessTrusted() else { return [] }
        let browsers: [Browser] = await MainActor.run { () -> [Browser] in
            let me = ProcessInfo.processInfo.processIdentifier
            return NSWorkspace.shared.runningApplications.compactMap { app -> Browser? in
                guard app.processIdentifier != me, !app.isTerminated,
                      let bundle = app.bundleIdentifier, StudyPageReader.isBrowser(bundle)
                else { return nil }
                return Browser(pid: app.processIdentifier, bundleID: bundle)
            }
        }
        guard !browsers.isEmpty else { return [] }
        return await Task.detached(priority: .userInitiated) { () -> [StudyPage] in
            StudyPageReader.readAll(browsers)
        }.value
        #endif
    }

    #if !WISP_NOTCH_QA

    private struct Browser: Sendable {
        let pid: pid_t
        let bundleID: String
    }

    /// Safari, Chrome, Arc, Edge, Brave, Firefox, Orion and the other browsers
    /// Look knows (ScreenLook.browserBundleIDs).
    private static let extraBrowsers: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview", "com.google.Chrome",
        "company.thebrowser.Browser", "com.microsoft.edgemac", "com.brave.Browser",
        "org.mozilla.firefox", "com.kagi.kagimacOS",
    ]

    private static func isBrowser(_ bundleID: String) -> Bool {
        extraBrowsers.contains(bundleID) || ScreenLook.browserBundleIDs.contains(bundleID)
    }

    // MARK: Budgets

    /// How long one Accessibility call may wait for the browser.
    private static let messagingTimeout: Float = 0.25
    /// The whole read, every browser and window.
    private static let totalBudgetNanoseconds: UInt64 = 6_000_000_000
    /// Elements visited per page, and how deep.
    private static let maxElements = 25_000
    private static let maxDepth = 80
    /// Elements visited while looking for a window's web area.
    private static let maxSearchElements = 600
    private static let maxSearchDepth = 14
    private static let maxWindowsPerApp = 40

    private static func now() -> UInt64 { DispatchTime.now().uptimeNanoseconds }

    // MARK: Walk

    /// Never call on the main thread.
    private static func readAll(_ browsers: [Browser]) -> [StudyPage] {
        let deadline = now() + totalBudgetNanoseconds
        var pages: [StudyPage] = []
        var seen = Set<String>()
        for browser in browsers {
            guard now() < deadline, pages.count < maxPages else { break }
            let app = AXUIElementCreateApplication(browser.pid)
            AXUIElementSetMessagingTimeout(app, messagingTimeout)
            // Chromium apps expose an empty tree until this is set (PageReader
            // does the same). Harmless elsewhere; nothing else is ever written.
            AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString,
                                         kCFBooleanTrue)
            let windows: [AXUIElement] = elements(app, kAXWindowsAttribute as String)
            for window in windows.prefix(maxWindowsPerApp) {
                guard now() < deadline, pages.count < maxPages else { break }
                AXUIElementSetMessagingTimeout(window, messagingTimeout)
                guard let area = webArea(in: window, deadline: deadline) else { continue }
                let url = pageURL(area: area, window: window)
                guard !url.isEmpty, isSchool(url: url), !seen.contains(url) else { continue }
                let title = stringValue(area, kAXTitleAttribute as String)
                    ?? stringValue(window, kAXTitleAttribute as String) ?? ""
                let text = collectText(area, deadline: deadline)
                guard !text.isEmpty else { continue }
                seen.insert(url)
                pages.append(StudyPage(url: url, title: String(title.prefix(500)), text: text))
            }
        }
        return pages
    }

    /// The window's first AXWebArea, breadth first: the front tab's page.
    /// Background tabs are not in the tree.
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

    /// The web area's AXURL, else the window's AXDocument (Safari).
    private static func pageURL(area: AXUIElement, window: AXUIElement) -> String {
        if let url = urlValue(area, "AXURL") { return url }
        if let url = urlValue(window, kAXDocumentAttribute as String) { return url }
        if let url = urlValue(window, "AXURL") { return url }
        return ""
    }

    private struct Collected {
        var lines: [String] = []
        var last = ""
        var characters = 0
        var visited = 0
        let deadline: UInt64
    }

    /// Visible text runs in document order, one per line, consecutive repeats
    /// dropped, capped at `maxCharacters`. Password fields are never read.
    private static func collectText(_ area: AXUIElement, deadline: UInt64) -> String {
        var out = Collected(deadline: deadline)
        harvest(area, into: &out, depth: 0)
        var text = out.lines.joined(separator: "\n")
        if text.count > maxCharacters { text = String(text.prefix(maxCharacters)) }
        return text
    }

    private static func harvest(_ element: AXUIElement, into out: inout Collected, depth: Int) {
        guard depth < maxDepth, out.visited < maxElements, out.characters < maxCharacters,
              now() < out.deadline else { return }
        out.visited += 1
        AXUIElementSetMessagingTimeout(element, messagingTimeout)
        let role = stringValue(element, kAXRoleAttribute as String) ?? ""
        let subrole = stringValue(element, kAXSubroleAttribute as String) ?? ""
        // Never read a password field, or what someone is typing.
        if role == "AXSecureTextField" || subrole == "AXSecureTextField" { return }
        if role == (kAXTextFieldRole as String) || role == (kAXTextAreaRole as String) { return }
        if role == (kAXStaticTextRole as String) {
            if let value = stringValue(element, kAXValueAttribute as String)
                ?? stringValue(element, kAXTitleAttribute as String) {
                append(value, to: &out)
            }
            return
        }
        for child in elements(element, kAXChildrenAttribute as String) {
            harvest(child, into: &out, depth: depth + 1)
            if out.visited >= maxElements || out.characters >= maxCharacters { return }
        }
    }

    private static func append(_ value: String, to out: inout Collected) {
        for raw in value.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty, line != out.last else { continue }
            out.lines.append(line)
            out.last = line
            out.characters += line.count + 1
            if out.characters >= maxCharacters { return }
        }
    }

    // MARK: Accessibility values

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
}
