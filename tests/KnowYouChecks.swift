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

        // The steps, in order: a welcome, eight questions, a summary.
        check(KnowYouStep.allCases.first == .welcome && KnowYouStep.allCases.last == .done, "welcome first, summary last")
        check(KnowYouStep.questions.count == 8, "eight questions")
        check(KnowYouStep.questions == [.name, .about, .goals, .struggles, .people, .tone, .boundaries, .notes],
              "question order")
        for step in KnowYouStep.allCases {
            check(!step.title.isEmpty && !step.why.isEmpty, "\(step) has a title and a reason")
            check(step.isQuestion == !step.fields.isEmpty, "\(step) saves fields exactly when it is a question")
        }
        check(KnowYouStep.tone.fields == ["tone", "detail"], "the tone step saves tone and detail")
        check(KnowYouStep.people.why.contains("never changes what I alert you about"),
              "the people question says it is not an alert list")

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
        check(flow.step == .notes && flow.isLastQuestion, "back from the summary returns to the last question")
        check(KnowYouFlow(step: .goals).progress == 2.0 / 8.0, "progress counts answered questions")

        // Resuming: pick up at the first unanswered question.
        var profile = UserProfile()
        check(KnowYouFlow.resume(for: profile).isWelcome, "an empty profile starts at the welcome")
        profile.name = "Adi"
        check(KnowYouFlow.resume(for: profile).step == .about, "a named profile resumes at who you are")
        profile.about = "A student"; profile.goals = ["Ship it"]; profile.struggles = ["Email"]
        profile.people = [.init(name: "Mom", relation: "mother")]
        profile.boundaries = "x"
        check(KnowYouFlow.resume(for: profile).step == .notes, "tone counts as answered once anything else is")
        profile.notes = "y"
        check(KnowYouFlow.resume(for: profile).isDone, "a full profile resumes at the summary")

        // Patches: only the step's fields, trimmed.
        profile.name = "  Adi  "
        let namePatch = profile.patch(for: .name)
        check(namePatch.count == 1 && namePatch["name"] as? String == "Adi", "the name step saves the trimmed name")
        let peoplePatch = profile.patch(for: .people)
        let rows = peoplePatch["people"] as? [[String: Any]]
        check(rows?.count == 1 && rows?[0]["name"] as? String == "Mom" && rows?[0]["relation"] as? String == "mother",
              "people serialise as name/relation/note rows")
        check(profile.patch(for: .welcome).isEmpty && profile.patch(for: .done).isEmpty, "welcome and summary save nothing")
        check(Set(profile.fullPatch.keys) == ["name", "about", "goals", "struggles", "people", "tone", "detail", "boundaries", "notes"],
              "About you saves every editable field")
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

        // The Mac account's name is offered, not saved.
        check(UserProfile.suggestedName(fullName: " Adi Jain ") == "Adi", "first name from the account")
        check(UserProfile.suggestedName(fullName: "") == "", "no name, no suggestion")

        print("\(count) know-you checks passed.")
    }
}
