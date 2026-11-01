import CoreGraphics
import Foundation

/// "Look": the person asks Wisp to look at the front window (the notch's eye
/// button or ⌥⇧L) or at a part of the screen they drag over. The capture
/// becomes the pending attachment with a "Wisp saw: <app — window>" chip, so
/// they always see what will be read before they send it.
///
/// Rules:
///  - Only on the person's own action, and only with Screen Recording already on.
///    Without it, Look explains and opens setup's Permissions page, whose Screen
///    Recording row is the only thing that asks macOS; nothing is captured.
///  - Wisp's own windows are never in the picture.
///  - Memory only, like every attachment (MediaAttachment): never written to disk.
///  - A window title is written by whoever made the page or file. It is shown and
///    sent as a plain, short label, never as an instruction.
///  - Private browsing is never captured silently (browserVerdict). A browser
///    window in the capture needs one extra tap after a warning, because a
///    Safari private window, for one, can't be told apart. A window that is
///    detectably private (a title marker, or a Chromium browser reporting it
///    incognito) is refused outright.
///
/// Pure logic, checked by tests/ScreenLookChecks.swift without the app. The
/// capture itself is ScreenSnapshot (PageReader.swift); the drag is RegionPicker.
enum ScreenLook {
    static let maxLabelLength = 120
    /// A drag smaller than this on either side is a click, not a region.
    static let minRegionSide: CGFloat = 24
    /// Windows smaller than this (menu extras, tooltips, badges) are never "the front window".
    static let minWindowSide: CGFloat = 100

    /// One on-screen window, from CGWindowListCopyWindowInfo.
    struct Window: Equatable {
        let number: UInt32
        let ownerPID: Int32
        let ownerName: String
        let title: String
        let bounds: CGRect
        let layer: Int
        let alpha: Double
        /// The owning app's bundle ID (from NSRunningApplication), when known.
        var bundleID: String = ""
    }

    /// The window list's dictionaries as Windows, front to back (the order macOS gives).
    static func windows(from infoList: [[String: Any]]) -> [Window] {
        infoList.compactMap { info in
            guard let number = (info[kCGWindowNumber as String] as? NSNumber)?.uint32Value,
                  let pid = (info[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value,
                  let boundsDict = info[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsDict as CFDictionary)
            else { return nil }
            return Window(number: number, ownerPID: pid,
                          ownerName: info[kCGWindowOwnerName as String] as? String ?? "",
                          title: info[kCGWindowName as String] as? String ?? "",
                          bounds: bounds,
                          layer: (info[kCGWindowLayer as String] as? NSNumber)?.intValue ?? 0,
                          alpha: (info[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1)
        }
    }

    /// The window the person is looking at: the frontmost ordinary window of the
    /// front app, or, when that app is Wisp or has none, the frontmost ordinary
    /// window of any other app. Never one of Wisp's own.
    static func frontWindow(_ windows: [Window], frontPID: Int32?, excludingPID: Int32) -> Window? {
        let candidates = windows.filter {
            $0.ownerPID != excludingPID && $0.layer == 0 && $0.alpha > 0.01
                && $0.bounds.width >= minWindowSide && $0.bounds.height >= minWindowSide
        }
        if let frontPID, frontPID != excludingPID,
           let window = candidates.first(where: { $0.ownerPID == frontPID }) {
            return window
        }
        return candidates.first
    }

    /// "Safari — Pricing", one plain line: control and display-changing characters
    /// removed, spaces collapsed, at most maxLabelLength characters. The app name
    /// alone when the title is empty or repeats it; nil when both are empty.
    static func label(app: String, title: String) -> String? {
        let app = plain(app), title = plain(title)
        let text: String
        if title.isEmpty || title == app { text = app }
        else if app.isEmpty { text = title }
        else { text = app + " — " + title }
        guard !text.isEmpty else { return nil }
        return text.count > maxLabelLength ? String(text.prefix(maxLabelLength - 1)) + "…" : text
    }

    /// What the chip says about a capture.
    static func chipText(label: String?) -> String {
        "Wisp saw: " + (label ?? "your screen")
    }

    /// The label for a dragged region.
    static func regionLabel(app: String?) -> String {
        if let app = app.flatMap({ label(app: $0, title: "") }) { return "part of " + app }
        return "part of your screen"
    }

    /// The rectangle a drag from `a` to `b` covers, or nil when it is too small.
    static func region(from a: CGPoint, to b: CGPoint) -> CGRect? {
        let rect = CGRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
        return rect.width >= minRegionSide && rect.height >= minRegionSide ? rect : nil
    }

    /// AppKit screen coordinates (origin bottom-left of the primary display) to
    /// the global display coordinates CoreGraphics captures in (origin top-left).
    static func displayRect(fromAppKit rect: CGRect, primaryHeight: CGFloat) -> CGRect {
        CGRect(x: rect.minX, y: primaryHeight - rect.maxY, width: rect.width, height: rect.height)
    }

    /// What Look says without Screen Recording, as it opens setup's Permissions
    /// page (whose Screen Recording row asks macOS). Nothing is captured.
    static let deniedNotice = "Wisp needs Screen Recording to look at your screen. Turn it on with "
        + "Screen Recording in the Permissions window that just opened, then quit and reopen Wisp."

    // MARK: - Private browsing

    static let browserBundleIDs: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview",
        "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
        "org.chromium.Chromium", "company.thebrowser.Browser", "com.brave.Browser", "com.brave.Browser.beta",
        "com.microsoft.edgemac", "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev",
        "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "com.operasoftware.OperaGX",
        "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly",
        "org.torproject.torbrowser", "app.zen-browser.zen", "com.kagi.kagimacOS",
        "com.duckduckgo.macos.browser", "ai.perplexity.comet", "com.openai.atlas",
    ]
    /// Owner names, for a browser whose bundle ID isn't known (or a renamed build).
    static let browserNames: Set<String> = [
        "safari", "safari technology preview", "google chrome", "chromium", "arc", "brave browser",
        "microsoft edge", "vivaldi", "opera", "firefox", "firefox developer edition", "firefox nightly",
        "tor browser", "zen", "zen browser", "orion", "duckduckgo", "comet", "chatgpt atlas",
    ]
    /// Chromium browsers whose AppleScript `mode` says "incognito" per window.
    /// Asked only when Wisp already holds Automation for that browser; never prompts.
    static let incognitoScriptable: Set<String> = [
        "com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
        "org.chromium.Chromium", "com.brave.Browser", "com.brave.Browser.beta",
        "com.microsoft.edgemac", "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev",
        "com.vivaldi.Vivaldi",
    ]

    static func isBrowser(_ window: Window) -> Bool {
        browserBundleIDs.contains(window.bundleID) || browserNames.contains(window.ownerName.lowercased())
            || window.ownerName.lowercased().hasSuffix(" browser")
    }

    /// Title words browsers put on private windows: Firefox and Tor "Private
    /// Browsing", Edge "InPrivate", Chromium builds that show "Incognito".
    /// Safari puts none in the title, so it is never detected this way.
    static func privateTitle(_ title: String) -> Bool {
        let lower = title.lowercased()
        return ["private browsing", "inprivate", "incognito", "(private)", "private window"]
            .contains { lower.contains($0) }
    }

    /// Whether a window's bounds are one a browser reported incognito (AppleScript
    /// bounds are the same top-left global coordinates; 2 pt tolerance).
    static func matchesIncognito(_ window: Window, incognitoBounds: [CGRect]) -> Bool {
        incognitoBounds.contains { other in
            abs(other.minX - window.bounds.minX) <= 2 && abs(other.minY - window.bounds.minY) <= 2
                && abs(other.width - window.bounds.width) <= 2 && abs(other.height - window.bounds.height) <= 2
        }
    }

    enum Verdict: Equatable {
        case clear
        /// Show this warning; capture only after the person taps Look anyway.
        case confirm(String)
        /// Never capture; say this.
        case refuse(String)
    }

    /// What Look does with the windows a capture would include. `incognito` holds
    /// the window numbers a browser itself reported private.
    static func browserVerdict(_ windows: [Window], incognito: Set<UInt32>) -> Verdict {
        let browsers = windows.filter(isBrowser)
        if let hit = browsers.first(where: { incognito.contains($0.number) || privateTitle($0.title) }) {
            let app = label(app: hit.ownerName, title: "") ?? "a browser"
            return .refuse("That's a private browsing window in \(app). Wisp doesn't look at private windows.")
        }
        guard !browsers.isEmpty else { return .clear }
        var apps: [String] = []
        for window in browsers {
            if let name = label(app: window.ownerName, title: ""), !apps.contains(name) { apps.append(name) }
        }
        let named = apps.isEmpty ? "A browser window" : apps.joined(separator: " and ")
        return .confirm("\(named) will be in this picture. If it's a private or incognito window, "
                        + "cancel: Wisp can't always tell. Look anyway?")
    }

    /// The windows a region or display capture would include (ordinary windows
    /// that overlap it, not Wisp's own).
    static func windows(_ windows: [Window], overlapping rect: CGRect, excludingPID: Int32) -> [Window] {
        windows.filter { $0.ownerPID != excludingPID && $0.layer == 0 && $0.alpha > 0.01 && $0.bounds.intersects(rect) }
    }

    private static func plain(_ value: String) -> String {
        let hidden: Set<Unicode.Scalar> = ["\u{202A}", "\u{202B}", "\u{202C}", "\u{202D}", "\u{202E}",
                                           "\u{2066}", "\u{2067}", "\u{2068}", "\u{2069}",
                                           "\u{200E}", "\u{200F}", "\u{061C}", "\u{FEFF}"]
        var scalars = String.UnicodeScalarView()
        for scalar in value.unicodeScalars where !hidden.contains(scalar) {
            scalars.append(scalar.properties.generalCategory == .control ? " " : scalar)
        }
        return String(scalars).split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}
