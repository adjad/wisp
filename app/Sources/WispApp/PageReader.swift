import AppKit
import ApplicationServices
import Carbon
import PDFKit
import Vision

// Pull the text out of whatever window the user is looking at.
//
// Three paths, tried in order of fidelity (docs/SMART_SEARCH_DESIGN.md §3):
//
//   C. file-native — AXDocument resolves to a real file → PDFKit / disk read.
//      Full text, perfect reading order, no extra permission.
//   A. accessibility — walk the focused window's AXUIElement tree. Includes
//      text scrolled off-screen, and gives us bounds for highlighting.
//   B. OCR — ScreenCaptureKit-free CGWindowList capture + Vision. Works
//      everywhere, but only sees the visible viewport.
//
// Whichever path won is reported back to the UI, because a degraded result the
// user can see the reason for is forgivable and a mysterious one is not.
enum PageSource: String {
    case document   // C — parsed from the file on disk
    case window     // A — accessibility tree
    case screen     // B — OCR of visible pixels
    case none

    var label: String {
        switch self {
        case .document: return "reading document"
        case .window:   return "reading window"
        case .screen:   return "reading visible screen only"
        case .none:     return "nothing to read"
        }
    }
}

struct PageText {
    var text: String
    var source: PageSource
    var appName: String
    var title: String
    // Absolute char offset → the AX element it came from, so a result can be
    // scrolled to in the host app. Empty for the OCR path.
    var anchors: [(range: Range<Int>, element: AXUIElement)]
    // The app the text was read from, so revealing a citation can bring it
    // forward. 0 when unknown.
    var pid: pid_t = 0

    static let empty = PageText(text: "", source: .none, appName: "",
                                title: "", anchors: [], pid: 0)
}

@MainActor
enum PageReader {

    /// True once the user has granted Accessibility. Never prompts.
    static var hasAccessibility: Bool {
        AXIsProcessTrusted()
    }

    /// Ask for Accessibility, showing the system prompt. Returns immediately;
    /// the grant lands out of band (macOS requires a relaunch for some apps).
    static func requestAccessibility() {
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true]
        PrivacyRelaunch.notePaneOpened()
        _ = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    }

    /// Read the frontmost app's focused window. Falls back down the chain until
    /// something yields usable text.
    static func read() async -> PageText {
        guard let app = NSWorkspace.shared.frontmostApplication,
              app.processIdentifier != ProcessInfo.processInfo.processIdentifier
        else { return .empty }

        let appName = app.localizedName ?? "app"
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        // Chromium apps expose an empty tree until this is set. Harmless
        // elsewhere — it's just an unknown attribute.
        AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString,
                                     kCFBooleanTrue)

        let window = focusedWindow(of: axApp)
        let title = window.flatMap { stringValue($0, kAXTitleAttribute) } ?? ""

        if AXIsProcessTrusted() {
            // C — a real file behind the window beats anything we can scrape.
            if let w = window, let url = documentURL(of: w),
               let text = readFile(at: url), text.count > 40 {
                return PageText(text: text, source: .document, appName: appName,
                                title: title.isEmpty ? url.lastPathComponent : title,
                                anchors: [], pid: app.processIdentifier)
            }
            // A — walk the tree.
            if let w = window {
                var collected = Collected()
                harvest(w, into: &collected, depth: 0)
                if collected.text.count > 40 {
                    return PageText(text: collected.text, source: .window,
                                    appName: appName, title: title,
                                    anchors: collected.anchors,
                                    pid: app.processIdentifier)
                }
            }
        }

        // B — OCR whatever is on screen.
        if let text = await ocrFrontWindow(pid: app.processIdentifier), text.count > 20 {
            return PageText(text: text, source: .screen, appName: appName,
                            title: title, anchors: [], pid: app.processIdentifier)
        }
        return PageText(text: "", source: .none, appName: appName,
                        title: title, anchors: [], pid: app.processIdentifier)
    }

    // MARK: - Accessibility

    private static func focusedWindow(of axApp: AXUIElement) -> AXUIElement? {
        if let w: AXUIElement = attribute(axApp, kAXFocusedWindowAttribute) { return w }
        if let w: AXUIElement = attribute(axApp, kAXMainWindowAttribute) { return w }
        return nil
    }

    private static func attribute<T>(_ el: AXUIElement, _ name: String) -> T? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success
        else { return nil }
        return value as? T
    }

    private static func stringValue(_ el: AXUIElement, _ name: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success
        else { return nil }
        if let s = value as? String { return s }
        if let n = value as? NSNumber { return n.stringValue }
        if let attr = value as? NSAttributedString { return attr.string }
        return nil
    }

    private static func documentURL(of window: AXUIElement) -> URL? {
        guard let doc = stringValue(window, kAXDocumentAttribute) else { return nil }
        if let url = URL(string: doc), url.isFileURL { return url }
        return URL(fileURLWithPath: doc)
    }

    private struct Collected {
        var text = ""
        var anchors: [(range: Range<Int>, element: AXUIElement)] = []
        // Guardrails: a runaway tree (huge web page, deeply nested Electron
        // app) must not hang the hotkey. Both limits are generous for real
        // documents and hard stops for pathological ones.
        var visited = 0
    }

    private static let maxNodes = 12_000
    private static let maxDepth = 60
    private static let maxChars = 400_000

    /// Depth-first walk in document order, appending every text-bearing node.
    private static func harvest(_ el: AXUIElement, into out: inout Collected, depth: Int) {
        guard depth < maxDepth, out.visited < maxNodes, out.text.count < maxChars
        else { return }
        out.visited += 1

        let role = stringValue(el, kAXRoleAttribute) ?? ""
        // Skip chrome that would pollute the document with UI labels.
        if role == kAXMenuBarRole || role == kAXToolbarRole { return }

        // A text area holds the whole document in one value — take it and stop
        // descending, or we'd duplicate every line via its children.
        if role == kAXTextAreaRole || role == kAXTextFieldRole {
            if let v = stringValue(el, kAXValueAttribute), !v.isEmpty {
                append(v, el, to: &out)
                return
            }
        }
        if role == kAXStaticTextRole {
            if let v = stringValue(el, kAXValueAttribute) ?? stringValue(el, kAXTitleAttribute),
               !v.isEmpty {
                append(v, el, to: &out)
                return
            }
        }

        guard let children: [AXUIElement] = attribute(el, kAXChildrenAttribute) else { return }
        for child in children {
            harvest(child, into: &out, depth: depth + 1)
        }
    }

    private static func append(_ s: String, _ el: AXUIElement, to out: inout Collected) {
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let start = out.text.count
        out.text += trimmed
        out.anchors.append((start..<(start + trimmed.count), el))
        // Blank line between nodes so the chunker sees real paragraph breaks.
        out.text += trimmed.count > 60 ? "\n\n" : "\n"
    }

    // MARK: - File-native

    private static func readFile(at url: URL) -> String? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        if url.pathExtension.lowercased() == "pdf" {
            guard let doc = PDFDocument(url: url) else { return nil }
            return doc.string
        }
        let plain: Set<String> = ["txt", "md", "markdown", "rtf", "csv", "json",
                                  "log", "swift", "py", "js", "ts", "html", "xml"]
        guard plain.contains(url.pathExtension.lowercased()) else { return nil }
        return try? String(contentsOf: url, encoding: .utf8)
    }

    // MARK: - OCR

    /// Capture the frontmost window of `pid` and run Vision text recognition.
    private static func ocrFrontWindow(pid: pid_t) async -> String? {
        guard let image = captureWindow(pid: pid) else { return nil }
        return await withCheckedContinuation { (cont: CheckedContinuation<String?, Never>) in
            let request = VNRecognizeTextRequest { req, _ in
                guard let obs = req.results as? [VNRecognizedTextObservation] else {
                    cont.resume(returning: nil); return
                }
                // Vision returns observations in no particular order; sort into
                // reading order (top-to-bottom, then left-to-right) or the
                // chunker will interleave unrelated columns.
                let lines = obs
                    .sorted { a, b in
                        let dy = abs(a.boundingBox.midY - b.boundingBox.midY)
                        if dy > 0.01 { return a.boundingBox.midY > b.boundingBox.midY }
                        return a.boundingBox.minX < b.boundingBox.minX
                    }
                    .compactMap { $0.topCandidates(1).first?.string }
                cont.resume(returning: lines.joined(separator: "\n"))
            }
            request.recognitionLevel = .accurate
            request.usesLanguageCorrection = true
            let handler = VNImageRequestHandler(cgImage: image, options: [:])
            DispatchQueue.global(qos: .userInitiated).async {
                do { try handler.perform([request]) }
                catch { cont.resume(returning: nil) }
            }
        }
    }

    private static func captureWindow(pid: pid_t) -> CGImage? {
        // Capturing another app's window without Screen Recording makes macOS ask,
        // and Wisp never asks on its own (Permissions.swift): without it, no OCR
        // fallback. Settings › Privacy's More Access… turns it on.
        guard CGPreflightScreenCaptureAccess() else { return nil }
        let infoList = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []
        // Frontmost on-screen window belonging to this pid with real size.
        for info in infoList {
            guard let owner = info[kCGWindowOwnerPID as String] as? pid_t, owner == pid,
                  let number = info[kCGWindowNumber as String] as? CGWindowID,
                  let boundsDict = info[kCGWindowBounds as String] as? [String: Any],
                  let h = boundsDict["Height"] as? CGFloat, h > 100,
                  let w = boundsDict["Width"] as? CGFloat, w > 100
            else { continue }
            return CGWindowListCreateImage(
                .null, .optionIncludingWindow, number,
                [.boundsIgnoreFraming, .nominalResolution])
        }
        return nil
    }

    // MARK: - Navigation

    /// Scroll the host app to the element backing `offset`, and select the
    /// matched range where the app supports it. Best-effort by design: many
    /// apps expose text but not range selection, and a failed scroll must never
    /// be worse than not trying.
    /// Scroll the source app to `offset` and select it. Returns whether that
    /// actually worked.
    ///
    /// It often doesn't, and the caller needs to know. Two common ways it
    /// fails: the offset came from text with no AX anchor behind it (the OCR
    /// and file-parsing paths have no live elements to point at), and viewers
    /// that expose text as read-only AXStaticText — Safari's built-in PDF view
    /// among them — accept the selection call and ignore it. Previously this
    /// returned silently in both cases, so clicking a citation did nothing at
    /// all with no indication why; the UI now falls back to showing the source
    /// passage inline instead. See SearchModel.reveal(citation:).
    @discardableResult
    static func reveal(_ page: PageText, offset: Int, length: Int) -> Bool {
        guard let anchor = page.anchors.first(where: { $0.range.contains(offset) })
        else { return false }
        let local = offset - anchor.range.lowerBound
        var range = CFRange(location: local, length: min(length, anchor.range.count - local))
        var selected = false
        if let axRange = AXValueCreate(.cfRange, &range) {
            selected = AXUIElementSetAttributeValue(
                anchor.element, kAXSelectedTextRangeAttribute as CFString, axRange) == .success
        }
        let scrolled = AXUIElementPerformAction(
            anchor.element, "AXScrollToVisible" as CFString) == .success
        // Bring the source app forward — scrolling something the user can't
        // see isn't navigation. Only when we actually moved it.
        if scrolled || selected {
            NSRunningApplication(processIdentifier: page.pid)?
                .activate(options: [])
        }
        return scrolled || selected
    }
}

/// "Explain my screen" and "Look": captures with Wisp's own windows left out,
/// downscaled to JPEG in memory (never written to disk). Callers check
/// `CGPreflightScreenCaptureAccess()` first; this never asks for the permission.
enum ScreenSnapshot {
    /// On-screen windows, front to back, each with its app's bundle ID.
    /// With `includeDesktop`, the wallpaper, menu bar and Dock too.
    static func onScreenWindows(includeDesktop: Bool = false) -> [ScreenLook.Window] {
        let options: CGWindowListOption = includeDesktop ? [.optionOnScreenOnly] : [.optionOnScreenOnly, .excludeDesktopElements]
        let infoList = CGWindowListCopyWindowInfo(options, kCGNullWindowID)
            as? [[String: Any]] ?? []
        var bundles: [Int32: String] = [:]
        return ScreenLook.windows(from: infoList).map { window in
            var window = window
            if bundles[window.ownerPID] == nil {
                bundles[window.ownerPID] = NSRunningApplication(processIdentifier: window.ownerPID)?.bundleIdentifier ?? ""
            }
            window.bundleID = bundles[window.ownerPID] ?? ""
            return window
        }
    }

    /// "Look": the window the person is looking at (ScreenLook.frontWindow).
    /// `frontPID` is read before Wisp's panel opens.
    static func frontWindow(frontPID: pid_t?) -> ScreenLook.Window? {
        guard CGPreflightScreenCaptureAccess() else { return nil }
        return ScreenLook.frontWindow(onScreenWindows(), frontPID: frontPID,
                                      excludingPID: ProcessInfo.processInfo.processIdentifier)
    }

    static func captureWindowJPEG(_ window: ScreenLook.Window) -> Data? {
        guard CGPreflightScreenCaptureAccess(),
              let image = CGWindowListCreateImage(.null, .optionIncludingWindow, CGWindowID(window.number),
                                                  [.boundsIgnoreFraming, .nominalResolution])
        else { return nil }
        return MediaAttachment.downscaleJPEG(image)
    }

    /// The windows among `windows` that their own browser reports incognito.
    /// Asked only of a running Chromium browser (ScreenLook.incognitoScriptable)
    /// that Wisp already holds Automation for: never a prompt, never a launch,
    /// two seconds at most. Anything unanswered is simply not known.
    static func incognitoWindows(among windows: [ScreenLook.Window]) -> Set<UInt32> {
        var found = Set<UInt32>()
        let bundles = Set(windows.map(\.bundleID)).intersection(ScreenLook.incognitoScriptable)
        for bundle in bundles where automationAllowed(bundle) {
            let source = "with timeout of 2 seconds\ntell application id \"\(bundle)\" to get bounds of "
                + "(every window whose mode is \"incognito\")\nend timeout"
            guard let script = NSAppleScript(source: source) else { continue }
            var error: NSDictionary?
            let result = script.executeAndReturnError(&error)
            guard error == nil, result.numberOfItems > 0 else { continue }
            var rects: [CGRect] = []
            for index in 1...result.numberOfItems {
                guard let item = result.atIndex(index), item.numberOfItems == 4 else { continue }
                let v = (1...4).map { CGFloat(item.atIndex($0)?.int32Value ?? 0) }
                rects.append(CGRect(x: v[0], y: v[1], width: v[2] - v[0], height: v[3] - v[1]))
            }
            for window in windows where window.bundleID == bundle
                && ScreenLook.matchesIncognito(window, incognitoBounds: rects) {
                found.insert(window.number)
            }
        }
        return found
    }

    /// True only when macOS already allows Wisp to script `bundleID` (never asks).
    private static func automationAllowed(_ bundleID: String) -> Bool {
        var target = AEAddressDesc()
        let created = bundleID.withCString { pointer in
            AECreateDesc(DescType(typeApplicationBundleID), pointer, strlen(pointer), &target)
        }
        guard created == noErr else { return false }
        defer { AEDisposeDesc(&target) }
        return AEDeterminePermissionToAutomateTarget(&target, AEEventClass(typeWildCard),
                                                     AEEventID(typeWildCard), false) == noErr
    }

    /// One Look at a window, checked right before it is captured: the window is
    /// looked up again (closed: nothing), its verdict computed now
    /// (ScreenLook.recheck against the warning the person accepted, if any),
    /// and only then captured, in this same step.
    static func lookAtWindow(_ number: UInt32, accepted: ScreenLook.Verdict?) -> ScreenLook.Outcome {
        guard CGPreflightScreenCaptureAccess() else { return .failed(ScreenLook.deniedNotice) }
        guard let window = onScreenWindows().first(where: { $0.number == number }) else {
            return .failed("That window isn't on screen anymore.")
        }
        let incognito = ScreenLook.isBrowser(window) ? incognitoWindows(among: [window]) : []
        switch ScreenLook.recheck(accepted: accepted, now: ScreenLook.browserVerdict([window], incognito: incognito)) {
        case .refuse(let message): return .refused(message)
        case .ask(let message): return .ask(message)
        case .proceed:
            guard let jpeg = captureWindowJPEG(window) else { return .failed("Wisp couldn't capture that window.") }
            return .captured(jpeg, label: ScreenLook.label(app: window.ownerName, title: window.title), leftOut: [])
        }
    }

    /// One Look at a region (or the main display), checked right before it is
    /// captured: one window list, the plan made from it (password managers and
    /// detectably private windows left out), the recheck, then a capture that
    /// draws only the plan's windows. A window raised after the list was read is
    /// never drawn.
    static func lookAtRegion(_ rect: CGRect, label: String?, accepted: ScreenLook.Verdict?) -> ScreenLook.Outcome {
        guard CGPreflightScreenCaptureAccess() else { return .failed(ScreenLook.deniedNotice) }
        let me = ProcessInfo.processInfo.processIdentifier
        let windows = onScreenWindows(includeDesktop: true)
        let incognito = incognitoWindows(among: windows.filter { ScreenLook.isBrowser($0) && $0.bounds.intersects(rect) })
        let plan = ScreenLook.regionPlan(windows, rect: rect, incognito: incognito, excludingPID: me)
        switch ScreenLook.recheck(accepted: accepted, now: plan.verdict) {
        case .refuse(let message): return .refused(message)
        case .ask(let message): return .ask(message)
        case .proceed:
            guard let jpeg = captureJPEG(rect, windows: plan.include) else {
                return .failed("Wisp couldn't capture that part of the screen.")
            }
            return .captured(jpeg, label: label, leftOut: plan.leftOut)
        }
    }

    /// The listed windows only, drawn within `rect` (global display coordinates).
    private static func captureJPEG(_ rect: CGRect, windows: [UInt32]) -> Data? {
        guard !rect.isEmpty, !windows.isEmpty else { return nil }
        var ids = windows.map { UnsafeRawPointer(bitPattern: UInt($0)) }
        let created: CFArray? = ids.withUnsafeMutableBufferPointer { CFArrayCreate(nil, $0.baseAddress, $0.count, nil) }
        guard let array = created,
              let image = CGImage(windowListFromArrayScreenBounds: rect, windowArray: array,
                                  imageOption: [.boundsIgnoreFraming, .nominalResolution])
        else { return nil }
        return MediaAttachment.downscaleJPEG(image)
    }
}
