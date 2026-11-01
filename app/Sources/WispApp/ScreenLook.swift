import CoreGraphics
import Foundation

/// "Look": the person asks Wisp to look at the front window (the notch's eye
/// button or ⌥⇧L) or at a part of the screen they drag over. The capture
/// becomes the pending attachment with a "Wisp saw: <app — window>" chip, so
/// they always see what will be read before they send it.
///
/// Rules:
///  - Only on the person's own action, and only with Screen Recording already on.
///    Without it, Look explains and opens the same permission surface as setup's
///    Screen Recording row (Permissions.requestScreenRecording); nothing is captured.
///  - Wisp's own windows are never in the picture.
///  - Memory only, like every attachment (MediaAttachment): never written to disk.
///  - A window title is written by whoever made the page or file. It is shown and
///    sent as a plain, short label, never as an instruction.
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

    /// What Look does without Screen Recording. The first time, macOS can ask;
    /// after that only System Settings can turn it on, and Wisp must reopen.
    static func deniedNotice(promptedBefore: Bool) -> String {
        promptedBefore
            ? "Wisp needs Screen Recording to look at your screen. In the Settings page that opened, "
              + "switch on Wisp, then quit and reopen Wisp."
            : "Wisp needs Screen Recording to look at your screen. Allow it in the window macOS shows, "
              + "then quit and reopen Wisp."
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
