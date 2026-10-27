import Foundation

// The onboarding window's pure rules: its steps and their order, which window a
// Dock click brings forward, and the Ask prompts it offers. Foundation only, so
// tests/OnboardingChecks.swift compiles it alone (build-support/simulation.py's
// "onboarding" gate and scripts/test_first_run_setup.sh).

/// The steps of the one onboarding window, in order. The raw value is what is
/// saved under FirstRunSetup.stepKey, so a quit comes back to the same step.
enum OnboardingStep: Int, CaseIterable, Equatable {
    case brain, permissions, google, knowYou, tour

    var title: String {
        switch self {
        case .brain: return "Model"
        case .permissions: return "Permissions"
        case .google: return "Google"
        case .knowYou: return "About you"
        case .tour: return "How to use"
        }
    }

    var next: OnboardingStep? { OnboardingStep(rawValue: rawValue + 1) }
    var previous: OnboardingStep? { OnboardingStep(rawValue: rawValue - 1) }

    /// The saved step, or the first one when nothing (or something unknown) was saved.
    static func restored(_ raw: Int?) -> OnboardingStep {
        raw.flatMap(OnboardingStep.init(rawValue:)) ?? .brain
    }
}

/// What a Dock click (or opening Wisp again from Finder or Spotlight) brings
/// forward. Exactly one window, never two: the onboarding window while setup
/// is unfinished; otherwise the Wisp window the person used last, brought back
/// from the Dock if it was minimized; otherwise the Wisp window (chat).
enum WispReopen {
    struct Candidate: Equatable {
        let id: Int
        let visible: Bool
        let minimized: Bool
        /// Higher is more recently key. Windows never made key are 0.
        let lastKey: Int
    }

    enum Target: Equatable {
        case onboarding
        case window(id: Int, deminiaturize: Bool)
        case chat
    }

    static func choose(setupUnfinished: Bool, windows: [Candidate]) -> Target {
        if setupUnfinished { return .onboarding }
        let shown = windows.filter { $0.visible || $0.minimized }
        guard let pick = shown.max(by: { a, b in
            // Most recently key first; a visible window beats a minimized one on a tie.
            a.lastKey != b.lastKey ? a.lastKey < b.lastKey : (!a.visible && b.visible)
        }) else { return .chat }
        return .window(id: pick.id, deminiaturize: pick.minimized)
    }
}

/// What Wisp can read right now, for the prompts the last step offers.
struct OnboardingLiveSources: Equatable {
    var calendar = false
    var mail = false
    var google = false
    var googleCalendar = false
}

/// The "Try asking" prompts on the last step: only ones a fresh install really
/// answers with what is live now. Reminders and arithmetic need no permission
/// (a reminder without Reminders access is kept and alerted by Wisp itself);
/// calendar and mail questions appear only once something can read them.
enum OnboardingPrompts {
    static let limit = 6

    static func prompts(_ live: OnboardingLiveSources) -> [String] {
        var out: [String] = []
        if live.calendar || live.googleCalendar { out.append("What's on my calendar today?") }
        if live.mail || live.google { out.append("Any emails I need to reply to?") }
        out.append("Remind me to call Mom at 6pm")
        out.append("What's 18% of 2,340?")
        out.append("What can you do?")
        out.append("Is my data private?")
        return Array(out.prefix(limit))
    }
}

/// What each Google Workspace scope gives Ask, by name, for the Google step's
/// "granted / not shared" list. Gmail is required by every sign-in, so a
/// connected account always has it.
enum GoogleGrantNames {
    static let gmailNotGrantedMessage =
        "Gmail wasn't shared. On Google's screen, tick the box to view your email messages, then try again."

    /// The service's sign-in error when Gmail's box was left unticked
    /// (service/gmail_source.py `_finish`).
    static func gmailNotGranted(_ error: String) -> Bool {
        let low = error.lowercased()
        return low.contains("tick the box to view your email messages") || low.contains("gmail read access wasn't granted")
    }

    /// The tools in the order they are listed, each with the scopes it needs.
    static let workspaceTools: [(name: String, scopes: [String])] = [
        ("Drive", ["https://www.googleapis.com/auth/drive.readonly"]),
        ("Docs", ["https://www.googleapis.com/auth/documents.readonly"]),
        ("Sheets", ["https://www.googleapis.com/auth/spreadsheets.readonly"]),
        ("Slides", ["https://www.googleapis.com/auth/presentations.readonly"]),
        ("Chat", ["https://www.googleapis.com/auth/chat.messages.readonly",
                  "https://www.googleapis.com/auth/chat.spaces.readonly"]),
        ("Tasks", ["https://www.googleapis.com/auth/tasks.readonly"]),
        ("Contacts", ["https://www.googleapis.com/auth/contacts.readonly"]),
    ]

    /// (granted, declined) tool names for one account. `calendar` is nil when
    /// the service didn't say; `declinedScopes` is the service's
    /// `workspace_declined`, meaningful only when the sign-in asked for them.
    static func split(calendar: Bool?, workspaceRequested: Bool, declinedScopes: [String]) -> (granted: [String], declined: [String]) {
        var granted = ["Gmail"], declined: [String] = []
        if let calendar { if calendar { granted.append("Calendar") } else { declined.append("Calendar") } }
        guard workspaceRequested else { return (granted, declined) }
        let off = Set(declinedScopes)
        for tool in workspaceTools {
            if tool.scopes.contains(where: off.contains) { declined.append(tool.name) } else { granted.append(tool.name) }
        }
        return (granted, declined)
    }
}
