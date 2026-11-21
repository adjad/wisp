import Foundation

/// Which way "Look at a window" captures: Apple's per-selection picker
/// (SCContentSharingPicker, the person chooses the window each time) or the
/// existing window-list capture that needs Screen Recording.
///
/// Whether the picker works without the Screen Recording grant on a given macOS
/// is confirmed live, not assumed: `pickerWorksWithoutGrant` is a stored setting
/// that defaults to false, which keeps today's behaviour exactly (the existing
/// path, and the Screen Recording prompt when it's off). Setting it to true makes
/// Look use the picker and stop asking for Screen Recording.
///
/// Pure; checked by tests/LookCapturePathChecks.swift, compiled with this file alone.
enum LookCapturePath: Equatable {
    /// Present SCContentSharingPicker and capture what the person chose.
    case picker
    /// The existing capture of the front window (Screen Recording already on).
    case legacy
    /// Screen Recording is off and nothing else can look: explain, open setup.
    case needsScreenRecording

    static let pickerWorksWithoutGrantKey = "wisp.lookPickerWorksWithoutGrant"

    /// The stored setting; false (keep asking for Screen Recording) until confirmed.
    static func storedPickerWorksWithoutGrant(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: pickerWorksWithoutGrantKey)
    }

    static func choose(pickerAvailable: Bool, pickerWorksWithoutGrant: Bool,
                       screenRecordingAllowed: Bool) -> LookCapturePath {
        if pickerAvailable && pickerWorksWithoutGrant { return .picker }
        return screenRecordingAllowed ? .legacy : .needsScreenRecording
    }
}
