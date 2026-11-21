import Foundation

/// What macOS calls a privacy permission, for every screen that names one, so
/// they all agree. What is known about the Accessibility switch (the
/// TCCServiceAccessibility records, one System Settings link, Privacy_Accessibility):
///
///   - macOS 26 and before: the pane is "Accessibility".
///   - macOS 27.0 to 27.2: renamed "Device Control and Data Access".
///   - macOS 27.3 and later: "Accessibility" again, with a separate pane next to
///     it, "System & App Protection Override".
///
/// Only the names are version-aware here; the link is the same for all of them.
/// We can't open System Settings on a Mac of each version from this tree, so the
/// names and the one link are what Apple's release notes and the 27.3 beta report,
/// and each is a pure function of an OperatingSystemVersion so a wrong guess is a
/// one-line change. Screen Recording and Full Disk Access keep their own panes.
/// Never used for System Settings › Accessibility itself (Spoken Content and the
/// like), which kept its name throughout.
enum PrivacyNames {
    /// Which of the three names a Mac shows.
    enum Era: Equatable {
        /// macOS 26 and before.
        case accessibility
        /// macOS 27.0 to 27.2.
        case deviceControl
        /// macOS 27.3 and later.
        case accessibilityWithOverride
    }

    static func era(for version: OperatingSystemVersion) -> Era {
        if version.majorVersion < 27 { return .accessibility }
        if version.majorVersion == 27 && version.minorVersion < 3 { return .deviceControl }
        return .accessibilityWithOverride
    }

    static var currentVersion: OperatingSystemVersion { ProcessInfo.processInfo.operatingSystemVersion }

    /// Whether this Mac shows the renamed pane (27.0 to 27.2 only).
    static var renamesAccessibility: Bool { era(for: currentVersion) == .deviceControl }

    /// The Accessibility permission's name on this Mac, for titles and buttons.
    static var accessibility: String { accessibility(for: currentVersion) }

    /// The name with its old one alongside, for sentences with room for it.
    static var accessibilityWithFormerName: String { accessibilityWithFormerName(for: currentVersion) }

    /// Where to switch it on: System Settings › Privacy & Security › its pane.
    static var accessibilityPath: String { accessibilityPath(for: currentVersion) }

    /// The separate 27.3 pane, nil on a Mac that has none.
    static var protectionOverride: String? { protectionOverride(for: currentVersion) }

    /// The System Settings link for the Accessibility switch, after "x-apple.systempreferences:".
    static var accessibilityPane: String { accessibilityPane(for: currentVersion) }

    static func accessibility(for version: OperatingSystemVersion) -> String {
        era(for: version) == .deviceControl ? "Device Control and Data Access" : "Accessibility"
    }

    static func accessibilityWithFormerName(for version: OperatingSystemVersion) -> String {
        era(for: version) == .deviceControl
            ? "Device Control and Data Access (called Accessibility before macOS 27)" : "Accessibility"
    }

    static func accessibilityPath(for version: OperatingSystemVersion) -> String {
        "System Settings › Privacy & Security › " + accessibility(for: version)
    }

    /// "System & App Protection Override" from macOS 27.3; nothing before.
    static func protectionOverride(for version: OperatingSystemVersion) -> String? {
        era(for: version) == .accessibilityWithOverride ? "System & App Protection Override" : nil
    }

    /// The same link on every version: the Privacy & Security extension's
    /// Accessibility anchor (the older com.apple.preference.security form is
    /// forwarded to it by macOS, but only this one is spelled out for 13 and later).
    static func accessibilityPane(for version: OperatingSystemVersion) -> String {
        "com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
    }
}
