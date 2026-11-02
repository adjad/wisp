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
///  - Private browsing and password managers are never captured silently
///    (browserVerdict, regionPlan). A password manager's window, or a window
///    that is detectably private (a title marker, or a Chromium browser
///    reporting it incognito), is refused as the front window and left out of
///    a region or screen capture. Any other browser window needs one extra tap
///    after a warning, because a Safari private window, for one, can't be told
///    apart. The check runs again right before the capture (recheck), so a
///    window raised while the warning was up is caught too.
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

    // MARK: - Password managers

    /// Apps that show passwords, keys and vault items. Look refuses their
    /// windows outright and leaves them out of every region or screen capture,
    /// whatever the person taps.
    static let sensitiveBundleIDs: Set<String> = [
        "com.1password.1password", "com.agilebits.onepassword7", "com.agilebits.onepassword-osx",
        "com.agilebits.onepassword4", "com.bitwarden.desktop", "com.apple.keychainaccess",
        "com.apple.passwords", "com.dashlane.dashlanephonefinal", "com.dashlane.dashlane",
        "com.lastpass.lastpass", "com.lastpass.lastpassmacdesktop", "org.keepassxc.keepassxc",
        "com.hicknhacksoftware.macpass", "in.sinew.enpass-desktop", "me.proton.pass.electron",
        "com.nordsec.nordpass", "com.markmcguill.strongbox.mac", "com.keepersecurity.passwordmanager",
        "com.siber.roboform", "com.outercorner.secrets",
    ]
    /// Vendors that make only password products: any of their bundles (helpers,
    /// launchers, mini windows) counts.
    static let sensitiveBundlePrefixes: [String] = [
        "com.1password.", "com.agilebits.", "com.bitwarden.", "com.lastpass.", "com.dashlane.",
        "org.keepassxc.", "com.keepersecurity.", "com.siber.roboform", "me.proton.pass",
    ]
    /// Owner-name prefixes, lower-cased, for a build or helper whose bundle ID
    /// isn't known ("1Password Helper", "KeePassXC-Browser").
    static let sensitiveNames: Set<String> = [
        "1password", "1password 7", "1password 8", "bitwarden", "keychain access", "passwords", "dashlane",
        "lastpass", "keepassxc", "keepass", "macpass", "enpass", "proton pass", "nordpass", "strongbox",
        "keeper password manager", "roboform", "secrets",
    ]
    static let systemSettingsBundleIDs: Set<String> = ["com.apple.systempreferences", "com.apple.settings"]

    /// A password manager's window, or System Settings showing its Passwords pane
    /// (its window title is the pane's name).
    static func isSensitive(_ window: Window) -> Bool {
        passwordApp(window) || (isSystemSettings(window) && window.title.lowercased().contains("password"))
    }

    private static func passwordApp(_ window: Window) -> Bool {
        let bundle = window.bundleID.lowercased(), owner = window.ownerName.lowercased()
        return sensitiveBundleIDs.contains(bundle) || sensitiveBundlePrefixes.contains { bundle.hasPrefix($0) }
            || sensitiveNames.contains { owner.hasPrefix($0) }
    }

    private static func isSystemSettings(_ window: Window) -> Bool {
        let owner = window.ownerName.lowercased()
        return systemSettingsBundleIDs.contains(window.bundleID.lowercased())
            || owner == "system settings" || owner == "system preferences"
    }

    /// A browser window that is detectably private: a title marker, or its
    /// browser reported it incognito (`incognito` holds those window numbers).
    static func isPrivate(_ window: Window, incognito: Set<UInt32>) -> Bool {
        isBrowser(window) && (incognito.contains(window.number) || privateTitle(window.title))
    }

    enum Verdict: Equatable, Sendable {
        case clear
        /// Show this warning; capture only after the person taps Look anyway.
        case confirm(String)
        /// Never capture; say this.
        case refuse(String)
    }

    /// The browser warning, and what it says Wisp can't detect.
    static func browserWarning(_ windows: [Window]) -> Verdict {
        var apps: [String] = []
        for window in windows where isBrowser(window) {
            if let name = label(app: window.ownerName, title: ""), !apps.contains(name) { apps.append(name) }
        }
        guard !windows.filter(isBrowser).isEmpty else { return .clear }
        let named = apps.isEmpty ? "A browser window" : apps.joined(separator: " and ")
        return .confirm("\(named) will be in this picture. Wisp can't tell when a Safari window is private, "
                        + "and can't always tell for other browsers. If this is a private window, cancel. Look anyway?")
    }

    /// What Look does with the windows a capture would include. `incognito` holds
    /// the window numbers a browser itself reported private. A password manager
    /// or a detectably private window refuses the capture; any other browser
    /// window needs the extra tap.
    static func browserVerdict(_ windows: [Window], incognito: Set<UInt32>) -> Verdict {
        if let hit = windows.first(where: isSensitive) {
            let app = label(app: hit.ownerName, title: "") ?? "That app"
            return .refuse("\(app) holds passwords, so Wisp doesn't look at it.")
        }
        if let hit = windows.first(where: { isPrivate($0, incognito: incognito) }) {
            let app = label(app: hit.ownerName, title: "") ?? "a browser"
            return .refuse("That's a private browsing window in \(app). Wisp doesn't look at private windows.")
        }
        return browserWarning(windows)
    }

    /// The windows a region or display capture would include (ordinary windows
    /// that overlap it, not Wisp's own).
    static func windows(_ windows: [Window], overlapping rect: CGRect, excludingPID: Int32) -> [Window] {
        windows.filter { $0.ownerPID != excludingPID && $0.layer == 0 && $0.alpha > 0.01 && $0.bounds.intersects(rect) }
    }

    /// What a region or display capture draws. Only windows that overlapped the
    /// rectangle when it was checked are drawn (a window raised later is not in
    /// the list, so it is never in the picture). Password-manager windows and
    /// detectably private ones are left out rather than refusing the whole
    /// capture; any browser window that stays needs the extra tap.
    struct RegionPlan: Equatable {
        /// Window numbers to draw, front to back.
        var include: [UInt32]
        /// What was left out, for the notice ("1Password", "a private Google Chrome window").
        var leftOut: [String]
        /// .clear or .confirm; never .refuse.
        var verdict: Verdict
    }

    static func regionPlan(_ windows: [Window], rect: CGRect, incognito: Set<UInt32>,
                           excludingPID: Int32) -> RegionPlan {
        var include: [UInt32] = [], kept: [Window] = [], leftOut: [String] = []
        // Every window of a password manager is left out, menus and panels too.
        let sensitivePIDs = Set(windows.filter(passwordApp).map(\.ownerPID))
        for window in windows where window.ownerPID != excludingPID && window.bounds.intersects(rect) {
            let app = label(app: window.ownerName, title: "") ?? "an app"
            var note: String?
            if isSensitive(window) || sensitivePIDs.contains(window.ownerPID) { note = app }
            else if isPrivate(window, incognito: incognito) { note = "a private \(app) window" }
            if let note {
                if !leftOut.contains(note) { leftOut.append(note) }
                continue
            }
            include.append(window.number)
            if window.layer == 0 && window.alpha > 0.01 { kept.append(window) }
        }
        return RegionPlan(include: include, leftOut: leftOut, verdict: browserWarning(kept))
    }

    /// The notice after a capture that left windows out.
    static func leftOutNotice(_ leftOut: [String]) -> String? {
        guard !leftOut.isEmpty else { return nil }
        return "Wisp left \(leftOut.joined(separator: " and ")) out of the picture."
    }

    // MARK: - Check again right before capturing

    enum Recheck: Equatable {
        case proceed
        case refuse(String)
        /// The warning changed (another browser came up): ask again.
        case ask(String)
    }

    /// The verdict is computed again right before the capture, in the same step.
    /// `accepted` is the warning the person tapped Look anyway on (nil before
    /// any tap). A refusal now always wins; a warning they haven't seen asks again.
    static func recheck(accepted: Verdict?, now: Verdict) -> Recheck {
        switch now {
        case .clear: return .proceed
        case .refuse(let message): return .refuse(message)
        case .confirm(let message): return accepted == now ? .proceed : .ask(message)
        }
    }

    /// What one Look attempt ended with.
    enum Outcome: Sendable {
        case captured(Data, label: String?, leftOut: [String])
        case refused(String)
        case ask(String)
        case failed(String)
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
