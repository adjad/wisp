// Contract for the Getting to know you flow (app/Sources/WispApp/KnowYouFlow.swift).
// Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct KnowYouChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        // The steps, in order: three questions, then a summary. Under a minute.
        // Whoever opened the flow already said yes, so it starts on the name.
        check(KnowYouStep.allCases.first == .name && KnowYouStep.allCases.last == .done, "the name first, summary last")
        check(KnowYouStep.questions == [.name, .goals, .tone], "three questions: name, goals, tone")
        for step in KnowYouStep.allCases {
            check(!step.title.isEmpty && !step.why.isEmpty, "\(step) has a title and a reason")
            check(step.isQuestion == !step.fields.isEmpty, "\(step) saves fields exactly when it is a question")
        }
        check(KnowYouStep.tone.fields == ["tone", "detail"], "the tone step saves tone and detail")
        check(KnowYouStep.name.why.contains("Three quick questions") && KnowYouStep.name.why.contains("Skip"),
              "the first question promises three quick, skippable questions")
        check(KnowYouStep.goals.why.contains("one tap"), "the goals question says the weekly pulse is one tap")
        check(KnowYouStep.done.why.contains("/about") && !KnowYouStep.done.why.contains("Memory →"),
              "the summary names a way back a notch-first user can find")
        let allCopy = KnowYouStep.allCases.map { $0.title + " " + $0.why }.joined(separator: " ")
        for stale in ["Needs you", "rise to the top", "Each evening", "check-ins"] {
            check(!allCopy.contains(stale), "no promise the app doesn't keep: \(stale)")
        }

        // Moving through the flow.
        var flow = KnowYouFlow()
        check(flow.step == .name && flow.isFirst && flow.questionNumber == 1 && flow.progress == 0,
              "starts on the first question")
        check(!flow.canGoBack, "nothing before the first question")
        flow.back()
        check(flow.step == .name, "back on the first question stays")
        var last = flow.progress
        var seen = 1
        while !flow.isDone {
            flow.advance()
            seen += 1
            check(flow.progress >= last, "progress never goes backwards")
            last = flow.progress
        }
        check(seen == KnowYouStep.allCases.count, "advance (Continue or Skip) visits every step once")
        check(flow.progress == 1 && flow.questionNumber == nil, "the summary is complete")
        flow.advance()
        check(flow.isDone, "advance on the summary stays")
        flow.back()
        check(flow.step == .tone && flow.isLastQuestion, "back from the summary returns to the last question")
        flow.back(); flow.back()
        check(flow.step == .name, "back walks every question to the first")
        check(KnowYouFlow(step: .goals).progress == 1.0 / 3.0, "progress counts answered questions")
        var skipping = KnowYouFlow(step: .tone)
        skipping.advance()
        check(skipping.isDone, "skipping the last question lands on the summary")

        // Resuming: pick up at the first unanswered question.
        var profile = UserProfile()
        check(KnowYouFlow.resume(for: profile).step == .name, "an empty profile starts at the first question")
        profile.name = "Adi"
        check(KnowYouFlow.resume(for: profile).step == .goals, "a named profile resumes at goals")
        profile.goals = ["Ship it"]
        check(KnowYouFlow.resume(for: profile).isDone, "tone counts as answered once a name or goal is; a full profile resumes at the summary")
        profile = UserProfile()
        profile.struggles = ["Email"]
        check(KnowYouFlow.resume(for: profile).step == .name, "fields About you edits do not count as the opening questions")

        // Focus cards: select all that apply, up to five, each a plain goal sentence.
        let areas = KnowYouChips.focusAreas
        check(areas.count == 10, "ten focus areas")
        check(Set(areas.map(\.id)).count == areas.count && Set(areas.map(\.goal)).count == areas.count, "ids and goals are unique")
        check(areas.allSatisfy { !$0.title.isEmpty && !$0.detail.isEmpty && $0.goal.count <= 200 }, "every card has a title, a detail and a storable goal")
        check(areas.contains { $0.title == "School" && $0.detail.contains("exams") }, "School is one of the cards")
        var picker = UserProfile()
        picker.toggleFocus(areas[0])
        check(picker.hasFocus(areas[0]) && picker.goals == [areas[0].goal], "picking a card stores its goal sentence")
        picker.toggleFocus(areas[0])
        check(picker.goals.isEmpty, "picking again unpicks")
        for area in areas.prefix(5) { picker.toggleFocus(area) }
        check(picker.goals.count == 5, "five cards can be picked")
        picker.toggleFocus(areas[5])
        check(picker.goals.count == 5 && !picker.hasFocus(areas[5]), "a sixth pick does nothing")
        picker.goals.append("Finish the thesis by May")
        check(picker.customGoals == ["Finish the thesis by May"], "typed goals are told apart from cards")
        check(KnowYouStep.goals.why.hasPrefix("Pick up to five"), "the goals question says to pick up to five")

        // Patches: only the step's fields, trimmed.
        profile.name = "  Adi  "
        let namePatch = profile.patch(for: .name)
        check(namePatch.count == 1 && namePatch["name"] as? String == "Adi", "the name step saves the trimmed name")
        profile.goals = ["Ship it", "Run far"]
        check(profile.patch(for: .goals)["goals"] as? [String] == ["Ship it", "Run far"], "the goals step saves the list")
        check(profile.patch(for: .done).isEmpty, "the summary saves nothing")
        check(Set(profile.fullPatch.keys) == ["name", "about", "goals", "struggles", "people", "tone", "detail", "boundaries", "notes"],
              "About you still saves every editable field")
        check(UserProfile.Person(name: "Mom", relation: "mother").label == "Mom · mother"
              && UserProfile.Person(name: "Sam").label == "Sam", "person labels")

        // Parsing what the service returns, tolerantly.
        let json: [String: Any] = [
            "profile": ["name": "Adi", "goals": ["A", 3, "B"], "people": [["name": "Mom", "relation": "mother"], ["nope": 1]],
                        "tone": "direct", "completed_at": 12.5, "dismiss_count": 1, "updated_at": true],
            "status": ["completed": true, "has_content": true, "offer": false, "stale": false,
                       "filled": ["name", "goals"], "days_since_update": 3],
            "preview": ["I'll call you Adi.", 7],
        ]
        let envelope = ProfileEnvelope(json: json)
        check(envelope != nil, "a well-formed envelope parses")
        check(envelope?.profile.name == "Adi" && envelope?.profile.goals == ["A", "B"], "strings only in lists")
        check(envelope?.profile.people == [.init(name: "Mom", relation: "mother")], "malformed people rows are dropped")
        check(envelope?.profile.tone == "direct" && envelope?.profile.detail == "thorough", "missing choices take the defaults")
        check(envelope?.profile.completedAt == 12.5 && envelope?.profile.updatedAt == nil && envelope?.profile.dismissCount == 1,
              "numbers parse; a boolean is not a timestamp")
        check(envelope?.status.completed == true && envelope?.status.filled == ["name", "goals"]
              && envelope?.status.daysSinceUpdate == 3, "status parses")
        check(envelope?.preview == ["I'll call you Adi."], "preview keeps strings only")
        check(ProfileEnvelope(json: ["profile": 1]) == nil, "a malformed envelope is refused")
        check(UserProfile(json: [:]).hasContent == false, "an empty profile has no content")
        check(!UserProfile(json: ["tone": "warm", "detail": "thorough"]).hasContent,
              "default choices alone keep a fresh profile empty")
        let preferenceOnlyProfiles: [[String: Any]] = [["tone": "direct"], ["tone": "gentle"], ["detail": "short"]]
        for choices in preferenceOnlyProfiles {
            let styled = UserProfile(json: choices)
            check(styled.hasContent, "a nondefault reply preference alone is profile content: \(choices)")
            check(UserProfile(json: styled.fullPatch).hasContent,
                  "About you preserves a preference-only profile when it saves")
        }

        // About you saves only what changed, so a stale screen never rewrites
        // an untouched answer.
        var loadedProfile = UserProfile()
        loadedProfile.name = "Adi"; loadedProfile.goals = ["Ship it"]
        loadedProfile.people = [.init(name: "Mom", relation: "mother")]
        var edited = loadedProfile
        check(edited.changes(since: loadedProfile).isEmpty, "nothing changed, nothing to send")
        edited.notes = " Likes lists "
        edited.people.append(.init(name: "Sam"))
        let changed = edited.changes(since: loadedProfile)
        check(Set(changed.keys) == ["notes", "people"] && changed["notes"] as? String == "Likes lists",
              "only the edited fields are sent, trimmed")
        edited = loadedProfile
        edited.tone = "direct"
        check(Set(edited.changes(since: loadedProfile).keys) == ["tone"], "a style change is one field")

        // Tone and length: Wisp's own voice is the preselected default, and the
        // labels say what each choice does.
        check(KnowYouChips.tones.first?.value == KnowYouChips.defaultTone
              && KnowYouChips.details.first?.value == KnowYouChips.defaultDetail, "the default is listed first")
        check(KnowYouChips.tones.first { $0.value == "warm" }?.label == "Upbeat", "warm reads as Upbeat, matching the prompt")
        check(Set(KnowYouChips.tones.map(\.label)).count == 3 && Set(KnowYouChips.details.map(\.label)).count == 2,
              "every choice has its own label")

        // Where the answers live: never "Stays on this Mac" while a cloud model answers everything.
        check(ProfileStatus().localOnly && ProfileStatus(json: [:]).whereItLives == "Stays on this Mac",
              "an older service reads as local")
        check(!ProfileStatus(json: ["local_only": false]).whereItLives.contains("this Mac"),
              "a cloud model changes the lock line")
        check(ProfileStatus(json: ["local_only": false, "sent_to": "openrouter.ai"]).whereItLives
              == "Sent with your questions to openrouter.ai", "Cloud Everything names the host")
        check(ProfileStatus(json: ["local_only": true, "sent_to": ""]).whereItLives == "Stays on this Mac",
              "an optional cloud model keeps the profile here")

        // The notices after the flow.
        check(KnowYouNotice.notNow.contains("/about") && KnowYouNotice.notNow.contains("Tell Wisp About You…"),
              "Not now says where the questions are")
        check(KnowYouNotice.thanks(name: "Sam") == "Thank you, Sam. I'll put this to work.", "Done thanks by name")

        // The Mac account's name is offered, not saved.
        check(UserProfile.suggestedName(fullName: " Adi Jain ") == "Adi", "first name from the account")
        check(UserProfile.suggestedName(fullName: "") == "", "no name, no suggestion")
        check(UserProfile.suggestedName(fullName: "Dr. Ana Ruiz") == "Ana", "a title is skipped")
        check(UserProfile.suggestedName(fullName: "Smith, Jane") == "Jane", "Last, First gives the first name")
        check(UserProfile.suggestedName(fullName: "Administrator") == "", "a generic account name is not offered")
        check(UserProfile.suggestedName(fullName: "J. Smith") == "", "an initial and a surname offer nothing")
        check(UserProfile.suggestedName(fullName: "J. Robert Smith") == "Robert", "a leading initial is skipped")

        print("\(count) know-you checks passed.")
    }
}
