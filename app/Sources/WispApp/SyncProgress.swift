import Foundation

// Live, observable sync/build progress — shown in Settings (see
// SettingsView's "Sync Status" section). A standalone singleton rather than
// living on OverlayModel: SettingsView is a separate SwiftUI root
// (NSHostingView(rootView: SettingsView())) that doesn't currently receive
// the shared OverlayModel, and this is simple enough not to need threading
// it through — same rationale as SuperModelState.shared.
//
// Mutated from two very different places:
//   - MailReader.syncHistory() updates `mailHistoryFraction` directly as its
//     batch loop progresses (Swift-local — no backend round trip needed,
//     since the scan itself runs in this same process).
//   - `profileBuildFraction`/`profileBuildLabel` are updated by
//     OverlayModel.handleAssistantEvent's "profile_progress" case, relaying
//     the backend's SSE events (service/memory/profile.py's build_profile,
//     which runs server-side and has no other way to report progress here).
// All mutations must happen on the main actor (SwiftUI @Published requires
// it) — background callers hop over via `Task { @MainActor in ... }`.
@MainActor
final class SyncProgress: ObservableObject {
    static let shared = SyncProgress()
    private init() {}

    // nil = not currently running.
    @Published var mailHistoryFraction: Double? = nil
    @Published var mailHistoryLastSynced: Date? = nil

    @Published var profileBuildFraction: Double? = nil
    @Published var profileBuildLabel: String = ""
    @Published var profileLastBuilt: Date? = nil
}
