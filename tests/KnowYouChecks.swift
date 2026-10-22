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

        // The steps, in order: a welcome, three questions, a summary. Under a minute.
        check(KnowYouStep.allCases.first == .welcome && KnowYouStep.allCases.last == .done, "welcome first, summary last")
        check(KnowYouStep.questions == [.name, .goals, .tone], "three questions: name, goals, tone")
        for step in KnowYouStep.allCases {
            check(!step.title.isEmpty && !step.why.isEmpty, "\(step) has a title and a reason")
            check(step.isQuestion == !step.fields.isEmpty, "\(step) saves fields exactly when it is a question")
        }
        check(KnowYouStep.tone.fields == ["tone", "detail"], "the tone step saves tone and detail")
        check(KnowYouStep.welcome.why.contains("Three quick questions"), "the welcome promises three questions")
        check(KnowYouStep.goals.why.contains("one tap"), "the goals question says the weekly pulse is one tap")
        check(KnowYouStep.done.why.contains("Each evening"), "the summary says the evening check-in follows")

        // Moving through the flow.
        var flow = KnowYouFlow()
        check(flow.isWelcome && flow.progress == 0 && flow.questionNumber == nil, "starts at the welcome")
        check(!flow.canGoBack, "nothing before the welcome")
        flow.back()
        check(flow.isWelcome, "back on the welcome stays")
        flow.begin()
        check(flow.step == .name && flow.questionNumber == 1 && flow.progress == 0, "begin opens the first question")
        check(!flow.canGoBack, "the first question has no back")
        flow.back()
        check(flow.step == .name, "back on the first question stays; the welcome is not a question")
        var last = flow.progress
        var seen = 1
        while !flow.isDone {
            flow.advance()
            seen += 1
            check(flow.progress >= last, "progress never goes backwards")
            last = flow.progress
        }
        check(seen == KnowYouStep.allCases.count - 1, "advance visits every step after the welcome once")
        check(flow.progress == 1 && flow.questionNumber == nil, "the summary is complete")
        flow.advance()
        check(flow.isDone, "advance on the summary stays")
        flow.back()
        check(flow.step == .tone && flow.isLastQuestion, "back from the summary returns to the last question")
        check(KnowYouFlow(step: .goals).progress == 1.0 / 3.0, "progress counts answered questions")

        // Resuming: pick up at the first unanswered question.
        var profile = UserProfile()
        check(KnowYouFlow.resume(for: profile).isWelcome, "an empty profile starts at the welcome")
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
        check(KnowYouStep.goals.why.hasPrefix("Pick everything"), "the goals question says to pick all that apply")

        // Patches: only the step's fields, trimmed.
        profile.name = "  Adi  "
        let namePatch = profile.patch(for: .name)
        check(namePatch.count == 1 && namePatch["name"] as? String == "Adi", "the name step saves the trimmed name")
        profile.goals = ["Ship it", "Run far"]
        check(profile.patch(for: .goals)["goals"] as? [String] == ["Ship it", "Run far"], "the goals step saves the list")
        check(profile.patch(for: .welcome).isEmpty && profile.patch(for: .done).isEmpty, "welcome and summary save nothing")
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
        check(envelope?.profile.tone == "direct" && envelope?.profile.detail == "short", "missing choices take the defaults")
        check(envelope?.profile.completedAt == 12.5 && envelope?.profile.updatedAt == nil && envelope?.profile.dismissCount == 1,
              "numbers parse; a boolean is not a timestamp")
        check(envelope?.status.completed == true && envelope?.status.filled == ["name", "goals"]
              && envelope?.status.daysSinceUpdate == 3, "status parses")
        check(envelope?.preview == ["I'll call you Adi."], "preview keeps strings only")
        check(ProfileEnvelope(json: ["profile": 1]) == nil, "a malformed envelope is refused")
        check(UserProfile(json: [:]).hasContent == false, "an empty profile has no content")
        check(!UserProfile(json: ["tone": "warm", "detail": "short"]).hasContent,
              "default choices alone keep a fresh profile empty")
        let preferenceOnlyProfiles: [[String: Any]] = [["tone": "direct"], ["tone": "gentle"], ["detail": "thorough"]]
        for choices in preferenceOnlyProfiles {
            let styled = UserProfile(json: choices)
            check(styled.hasContent, "a nondefault reply preference alone is profile content: \(choices)")
            check(UserProfile(json: styled.fullPatch).hasContent,
                  "About you preserves a preference-only profile when it saves")
        }

        // The Mac account's name is offered, not saved.
        check(UserProfile.suggestedName(fullName: " Adi Jain ") == "Adi", "first name from the account")
        check(UserProfile.suggestedName(fullName: "") == "", "no name, no suggestion")

        print("\(count) know-you checks passed.")
    }
}
