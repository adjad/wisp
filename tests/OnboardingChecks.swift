import Foundation

// Pure checks for the onboarding window's rules (app/Sources/WispApp/Onboarding.swift):
// the step order, the one window a Dock click brings forward, the Ask prompts it
// offers, and how a Google sign-in's grants are named. No window, service or engine.
@main
struct OnboardingChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func main() {
        // One window, these steps, in this order.
        check(OnboardingStep.allCases == [.brain, .permissions, .google, .knowYou, .tour],
              "Model, Permissions, Google, About you, How to use")
        check(OnboardingStep.allCases.map(\.title) == ["Model", "Permissions", "Google", "About you", "How to use"], "Titles")
        check(OnboardingStep.brain.next == .permissions && OnboardingStep.permissions.next == .google
              && OnboardingStep.google.next == .knowYou && OnboardingStep.knowYou.next == .tour
              && OnboardingStep.tour.next == nil, "Continue walks the order")
        check(OnboardingStep.brain.previous == nil && OnboardingStep.tour.previous == .knowYou, "Back walks it backwards")
        check(OnboardingStep.restored(nil) == .brain && OnboardingStep.restored(99) == .brain
              && OnboardingStep.restored(2) == .google, "A saved step resumes; nothing or junk starts at the model")

        // The Dock brings forward exactly one window.
        typealias C = WispReopen.Candidate
        check(WispReopen.choose(setupUnfinished: true, windows: [C(id: 1, visible: true, minimized: false, lastKey: 9)])
              == .onboarding, "Setup unfinished: the onboarding window, never chat as well")
        check(WispReopen.choose(setupUnfinished: false, windows: []) == .chat, "Nothing open: the Wisp window")
        check(WispReopen.choose(setupUnfinished: false, windows: [C(id: 1, visible: false, minimized: false, lastKey: 5)])
              == .chat, "A closed window is not brought back: the Wisp window")
        check(WispReopen.choose(setupUnfinished: false, windows: [C(id: 1, visible: false, minimized: true, lastKey: 3)])
              == .window(id: 1, deminiaturize: true), "A minimized window comes back out of the Dock")
        check(WispReopen.choose(setupUnfinished: false, windows: [
            C(id: 1, visible: true, minimized: false, lastKey: 2),
            C(id: 2, visible: false, minimized: true, lastKey: 7),
            C(id: 3, visible: false, minimized: false, lastKey: 9),
        ]) == .window(id: 2, deminiaturize: true), "The one used most recently, among those still open")
        check(WispReopen.choose(setupUnfinished: false, windows: [
            C(id: 1, visible: false, minimized: true, lastKey: 0),
            C(id: 2, visible: true, minimized: false, lastKey: 0),
        ]) == .window(id: 2, deminiaturize: false), "A tie goes to the visible window")

        // Prompts: only what a fresh install answers with what is live.
        let none = OnboardingPrompts.prompts(OnboardingLiveSources())
        check(none == ["Remind me to call Mom at 6pm", "What's 18% of 2,340?", "What can you do?", "Is my data private?"],
              "Nothing allowed: four that need no source")
        check(!none.contains("What's on my calendar today?") && !none.contains("Any emails I need to reply to?"),
              "No calendar or mail question without a way to read them")
        let all = OnboardingPrompts.prompts(OnboardingLiveSources(calendar: true, mail: true))
        check(all.count == 6 && all.first == "What's on my calendar today?" && all[1] == "Any emails I need to reply to?",
              "Calendar and Mail allowed: they come first, six in all")
        check(OnboardingPrompts.prompts(OnboardingLiveSources(google: true)).contains("Any emails I need to reply to?")
              && !OnboardingPrompts.prompts(OnboardingLiveSources(google: true)).contains("What's on my calendar today?"),
              "Google without Calendar shared: mail, not calendar")
        check(OnboardingPrompts.prompts(OnboardingLiveSources(google: true, googleCalendar: true))
                .contains("What's on my calendar today?"), "Google Calendar shared: the calendar question")
        for live in [OnboardingLiveSources(), OnboardingLiveSources(calendar: true, mail: true, google: true, googleCalendar: true)] {
            let prompts = OnboardingPrompts.prompts(live)
            check((4...6).contains(prompts.count) && Set(prompts).count == prompts.count, "Four to six, no repeats")
            check(prompts.allSatisfy { !$0.lowercased().contains("weather") }, "No weather: it needs a city Wisp doesn't know")
        }

        // Google: what a sign-in shared, by name.
        let declined = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/chat.spaces.readonly"]
        let split = GoogleGrantNames.split(calendar: true, workspaceRequested: true, declinedScopes: declined)
        check(split.granted == ["Gmail", "Calendar", "Docs", "Sheets", "Slides", "Tasks", "Contacts"]
              && split.declined == ["Drive", "Chat"], "Declined tools are named; Gmail always shared")
        let noCalendar = GoogleGrantNames.split(calendar: false, workspaceRequested: false, declinedScopes: declined)
        check(noCalendar.granted == ["Gmail"] && noCalendar.declined == ["Calendar"],
              "A sign-in that never asked for the tools names only Gmail and Calendar")
        check(GoogleGrantNames.split(calendar: nil, workspaceRequested: false, declinedScopes: []).declined.isEmpty,
              "An older service that doesn't say: nothing called declined")
        // The service's own wording when Gmail's box was left unticked.
        let error = "Wisp could not connect Gmail: Gmail read access wasn't granted — on Google's consent screen, "
            + "tick the box to view your email messages, then try again"
        check(GoogleGrantNames.gmailNotGranted(error) && !GoogleGrantNames.gmailNotGranted("Sign-in did not finish. Try again."),
              "Gmail not shared is recognised")
        check(GoogleGrantNames.gmailNotGrantedMessage.contains("tick the box to view your email messages"),
              "And told in Google's own words")

        print("PASS: onboarding — step order, one window per Dock click, live-only prompts, Google grants")
    }
}
