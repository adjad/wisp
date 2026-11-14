import Foundation

/// What macOS calls a privacy permission, for every screen that names one, so
/// they all agree. macOS 27 renamed the Accessibility pane in Privacy &
/// Security to "Device Control and Data Access": the same switches (the
/// TCCServiceAccessibility records) and the same System Settings link
/// (Privacy_Accessibility). Screen Recording and Full Disk Access are still
/// their own panes. Never used for System Settings › Accessibility itself
/// (Spoken Content and the like), which kept its name.
enum PrivacyNames {
    /// Whether this Mac shows the new name.
    static var renamesAccessibility: Bool {
        ProcessInfo.processInfo.isOperatingSystemAtLeast(
            OperatingSystemVersion(majorVersion: 27, minorVersion: 0, patchVersion: 0))
    }

    /// The Accessibility permission's name on this Mac, for titles and buttons.
    static var accessibility: String { accessibility(renamed: renamesAccessibility) }

    /// The name with its old one alongside, for sentences with room for it.
    static var accessibilityWithFormerName: String { accessibilityWithFormerName(renamed: renamesAccessibility) }

    /// Where to switch it on: System Settings › Privacy & Security › its pane.
    static var accessibilityPath: String { "System Settings › Privacy & Security › " + accessibility }

    static func accessibility(renamed: Bool) -> String {
        renamed ? "Device Control and Data Access" : "Accessibility"
    }

    static func accessibilityWithFormerName(renamed: Bool) -> String {
        renamed ? "Device Control and Data Access (called Accessibility before macOS 27)" : "Accessibility"
    }
}
