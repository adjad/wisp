import Foundation

// Thread-safe flag mirroring OverlayModel.superModelActive, for components
// that can't easily hold a reference to it — OverlayModel is @MainActor, but
// MailReader/NotesReader's periodic AppleScript syncs run on background
// queues. Without this, those timers kept firing `tell application "Mail"` /
// `tell application "Notes"` every 5-15 minutes regardless of Super Model,
// and AppleScript's `tell application` auto-launches the target app if it
// isn't running — silently relaunching an app Super Model had just quit to
// free memory. Set by OverlayModel's enable/disableSuperModel; read by
// MailReader/NotesReader before each sync.
final class SuperModelState: @unchecked Sendable {
    static let shared = SuperModelState()
    private let lock = NSLock()
    private var _active = false

    var active: Bool {
        get { lock.lock(); defer { lock.unlock() }; return _active }
        set { lock.lock(); _active = newValue; lock.unlock() }
    }
}
