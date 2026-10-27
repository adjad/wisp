import Foundation

/// "Getting to know you": the three questions Wisp asks up front so it can work
/// for a person rather than a user. About you in the Memory window shows and
/// edits every field. The answers live in the service's profile
/// (service/memory/profile.py, GET/PATCH /profile) and feed every local system
/// prompt and the morning brief's Keeping in mind section. This file is the
/// flow's pure logic: the steps, their copy, the chips, and the patch each step
/// saves. KnowYouView.swift draws it in the notch.
///
/// The flow opens straight on the first question. Whoever opened it (the
/// offer, /about, the menu bar) already said yes, so there is no welcome screen
/// to confirm again; the first question carries the reassurance instead.
///
/// Checked by tests/KnowYouChecks.swift without the app.
enum KnowYouStep: Int, CaseIterable, Equatable {
    case name, goals, tone, done

    var isQuestion: Bool { self != .done }
    static var questions: [KnowYouStep] { allCases.filter(\.isQuestion) }

    /// The question, asked the way a kind colleague would.
    var title: String {
        switch self {
        case .name: return "What should I call you?"
        case .goals: return "What are you working toward?"
        case .tone: return "How should I talk to you?"
        case .done: return "All set"
        }
    }

    /// Why Wisp asks, which is also exactly what it will do with the answer.
    var why: String {
        switch self {
        case .name:
            return "Three quick questions so I can help you better. Skip any you like."
        case .goals:
            return "Pick up to five. Your morning brief keeps one in view each day, and once a week I'll ask, with one tap, how it's going."
        case .tone: return "Pick what feels right. You can change it any time."
        case .done:
            return "Here's what changes now. To change or erase any of it, type /about, or open About you in the Wisp window's Memory."
        }
    }

    var placeholder: String {
        switch self {
        case .name: return "Your first name or a nickname"
        case .goals: return "Or say it in your own words"
        default: return ""
        }
    }

    /// The profile fields this step saves. Skipping a step saves none of them.
    var fields: [String] {
        switch self {
        case .name: return ["name"]
        case .goals: return ["goals"]
        case .tone: return ["tone", "detail"]
        case .done: return []
        }
    }
}

/// Where the flow is and how it moves. Progress counts questions, so the
/// first question is 0 and the summary is 1.
struct KnowYouFlow: Equatable {
    private(set) var step: KnowYouStep = .name

    init(step: KnowYouStep = .name) { self.step = step }

    var isFirst: Bool { step == KnowYouStep.questions.first }
    var isDone: Bool { step == .done }
    var questionNumber: Int? {
        guard let index = KnowYouStep.questions.firstIndex(of: step) else { return nil }
        return index + 1
    }
    static var questionCount: Int { KnowYouStep.questions.count }

    var progress: Double {
        if step == .done { return 1 }
        return Double((questionNumber ?? 1) - 1) / Double(Self.questionCount)
    }

    var canGoBack: Bool { !isFirst }
    var isLastQuestion: Bool { step == KnowYouStep.questions.last }

    /// Next step. Skip and Continue both land here; from the last question it
    /// is the summary, and the summary stays put.
    mutating func advance() {
        guard let next = KnowYouStep(rawValue: step.rawValue + 1) else { return }
        step = next
    }

    /// The step before, nothing before the first question.
    mutating func back() {
        guard canGoBack, let previous = KnowYouStep(rawValue: step.rawValue - 1) else { return }
        step = previous
    }

    /// Where to pick up for someone who already answered some questions: the
    /// first question they have not answered, or the summary when all are. A
    /// profile with nothing in it starts at the first question.
    static func resume(for profile: UserProfile) -> KnowYouFlow {
        guard profile.hasContent else { return KnowYouFlow() }
        for step in KnowYouStep.questions where !profile.answered(step) {
            return KnowYouFlow(step: step)
        }
        return KnowYouFlow(step: .done)
    }
}

/// One card in "What are you working toward?": a title, a line that says what
/// it covers, and the goal Wisp stores when it is picked. The goal is a plain
/// sentence because it is what the brief says back ("You're working toward:
/// Doing well in school") and what the weekly pulse asks about.
struct FocusArea: Equatable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let goal: String
}

/// The quick answers offered on each step. Tapping one adds the phrase;
/// nothing is chosen for the user.
enum KnowYouChips {
    /// Select all that apply. Ten areas, broad enough that most lives fit in
    /// five, specific enough that each one changes what Wisp pays attention to.
    static let focusAreas: [FocusArea] = [
        FocusArea(id: "school", title: "School", detail: "Classes, assignments, exams, grades.",
                  goal: "Doing well in school"),
        FocusArea(id: "career", title: "Work and career", detail: "Doing well in the job, or finding the next one.",
                  goal: "Moving forward in my career"),
        FocusArea(id: "building", title: "Building something", detail: "A startup, a product, a project of your own.",
                  goal: "Building something of my own"),
        FocusArea(id: "health", title: "Health and fitness", detail: "Sleep, exercise, eating, energy.",
                  goal: "Taking care of my health"),
        FocusArea(id: "people", title: "Relationships", detail: "Family, partner, friends. Showing up for them.",
                  goal: "Showing up for the people I love"),
        FocusArea(id: "money", title: "Money", detail: "Saving, earning more, getting out of debt.",
                  goal: "Getting my finances in order"),
        FocusArea(id: "learning", title: "Learning a skill", detail: "A language, an instrument, a craft, a certification.",
                  goal: "Learning a new skill"),
        FocusArea(id: "organized", title: "Staying organized", detail: "Fewer dropped balls, a calmer inbox, things done on time.",
                  goal: "Staying on top of my life"),
        FocusArea(id: "rest", title: "Rest and balance", detail: "Real time off, evenings and weekends that stay yours.",
                  goal: "Protecting time for rest"),
        FocusArea(id: "creative", title: "Creative work", detail: "Writing, music, art, making things.",
                  goal: "Making creative work"),
    ]
    static let maxGoals = 5

    /// Wisp's own voice is the default: gentle, full answers. Only a different
    /// choice changes what the model is told (service/memory/profile.py), so the
    /// labels describe exactly what each choice does.
    static let defaultTone = "gentle"
    static let defaultDetail = "thorough"

    static let tones: [(value: String, label: String, detail: String)] = [
        ("gentle", "Gentle", "Calm and kind. Good news first, never a scolding."),
        ("warm", "Upbeat", "Warm and lively, with a bit of personality."),
        ("direct", "Straight to the point", "The answer first. No small talk."),
    ]
    static let details: [(value: String, label: String, detail: String)] = [
        ("thorough", "Full answers", "The details that matter, with examples."),
        ("short", "Short", "A line or two, unless I ask for more."),
    ]
}

/// The profile as the service holds it (service/memory/profile.py), parsed
/// tolerantly: a field the service does not send reads as empty.
struct UserProfile: Equatable {
    struct Person: Equatable {
        var name: String
        var relation: String = ""
        var note: String = ""

        var json: [String: Any] { ["name": name, "relation": relation, "note": note] }
        /// "Mom · mother" or just "Mom".
        var label: String { relation.isEmpty ? name : "\(name) · \(relation)" }
    }

    static let editableFields = ["name", "about", "goals", "struggles", "people", "tone", "detail", "boundaries", "notes"]

    var name = ""
    var about = ""
    var goals: [String] = []
    var struggles: [String] = []
    var people: [Person] = []
    var tone = KnowYouChips.defaultTone
    var detail = KnowYouChips.defaultDetail
    var boundaries = ""
    var notes = ""
    var completedAt: Double?
    var updatedAt: Double?
    var dismissCount = 0

    init() {}

    init(json: [String: Any]) {
        name = json["name"] as? String ?? ""
        about = json["about"] as? String ?? ""
        goals = (json["goals"] as? [Any])?.compactMap { $0 as? String } ?? []
        struggles = (json["struggles"] as? [Any])?.compactMap { $0 as? String } ?? []
        people = (json["people"] as? [Any])?.compactMap { raw -> Person? in
            guard let row = raw as? [String: Any], let name = row["name"] as? String, !name.isEmpty else { return nil }
            return Person(name: name, relation: row["relation"] as? String ?? "", note: row["note"] as? String ?? "")
        } ?? []
        tone = json["tone"] as? String ?? KnowYouChips.defaultTone
        detail = json["detail"] as? String ?? KnowYouChips.defaultDetail
        boundaries = json["boundaries"] as? String ?? ""
        notes = json["notes"] as? String ?? ""
        completedAt = Self.number(json["completed_at"])
        updatedAt = Self.number(json["updated_at"])
        dismissCount = Int(Self.number(json["dismiss_count"]) ?? 0)
    }

    private static func number(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite else { return nil }
        return number.doubleValue
    }

    var hasContent: Bool {
        !name.isEmpty || !about.isEmpty || !goals.isEmpty || !struggles.isEmpty || !people.isEmpty
            || !boundaries.isEmpty || !notes.isEmpty
            || tone != KnowYouChips.defaultTone || detail != KnowYouChips.defaultDetail
    }

    /// Whether the user gave this step an answer. Tone always holds a default,
    /// so it counts as answered once the name or a goal has been.
    func answered(_ step: KnowYouStep) -> Bool {
        switch step {
        case .name: return !name.isEmpty
        case .goals: return !goals.isEmpty
        case .tone: return !name.isEmpty || !goals.isEmpty
        case .done: return true
        }
    }

    /// The PATCH body for one step: only that step's fields, so Skip never
    /// touches another answer and a saved step never resends the whole profile.
    func patch(for step: KnowYouStep) -> [String: Any] {
        var body: [String: Any] = [:]
        for field in step.fields { body[field] = value(for: field) }
        return body
    }

    /// Everything editable.
    var fullPatch: [String: Any] {
        var body: [String: Any] = [:]
        for field in Self.editableFields { body[field] = value(for: field) }
        return body
    }

    /// About you's Save: only the fields that differ from what was loaded, so
    /// a field the person never touched is never rewritten (and a stale screen
    /// can't overwrite an answer saved from the notch meanwhile).
    func changes(since original: UserProfile) -> [String: Any] {
        var body: [String: Any] = [:]
        for field in Self.editableFields where !Self.same(value(for: field), original.value(for: field)) {
            body[field] = value(for: field)
        }
        return body
    }

    private static func same(_ a: Any, _ b: Any) -> Bool {
        switch (a, b) {
        case let (a as String, b as String): return a == b
        case let (a as [String], b as [String]): return a == b
        case let (a as [[String: Any]], b as [[String: Any]]):
            return a.count == b.count && zip(a, b).allSatisfy { NSDictionary(dictionary: $0).isEqual(to: $1) }
        default: return false
        }
    }

    private func value(for field: String) -> Any {
        switch field {
        case "name": return name.trimmingCharacters(in: .whitespacesAndNewlines)
        case "about": return about.trimmingCharacters(in: .whitespacesAndNewlines)
        case "goals": return goals
        case "struggles": return struggles
        case "people": return people.map(\.json)
        case "tone": return tone
        case "detail": return detail
        case "boundaries": return boundaries.trimmingCharacters(in: .whitespacesAndNewlines)
        case "notes": return notes.trimmingCharacters(in: .whitespacesAndNewlines)
        default: return ""
        }
    }

    /// Whether a focus card is picked: its goal sentence is among the goals.
    func hasFocus(_ area: FocusArea) -> Bool {
        goals.contains { $0.caseInsensitiveCompare(area.goal) == .orderedSame }
    }

    /// Tap a card: pick it, or unpick it. Picking past the limit does nothing,
    /// so the card simply does not light up.
    mutating func toggleFocus(_ area: FocusArea) {
        if hasFocus(area) {
            goals.removeAll { $0.caseInsensitiveCompare(area.goal) == .orderedSame }
        } else if goals.count < KnowYouChips.maxGoals {
            goals.append(area.goal)
        }
    }

    /// Goals typed in the person's own words, not from a card.
    var customGoals: [String] {
        goals.filter { goal in !KnowYouChips.focusAreas.contains { $0.goal.caseInsensitiveCompare(goal) == .orderedSame } }
    }

    /// Titles and generic account names that are not what anyone is called.
    private static let notNames: Set<String> = [
        "dr", "mr", "mrs", "ms", "mx", "miss", "prof", "sir", "rev",
        "admin", "administrator", "user", "owner", "guest", "mac", "macbook", "apple", "test", "root",
    ]

    /// The first name from the Mac account's full name, offered (never saved)
    /// as the answer to "What should I call you?". Skips a title ("Dr. Ana
    /// Ruiz" gives Ana), an initial, a "Last, First" comma, and generic
    /// account names, offering nothing rather than "Administrator".
    static func suggestedName(fullName: String) -> String {
        var words = fullName.split(whereSeparator: \.isWhitespace).map(String.init)
        if let comma = words.firstIndex(where: { $0.hasSuffix(",") }), comma + 1 < words.count {
            words = Array(words[(comma + 1)...])  // "Smith, Jane" is Jane
        }
        var skippedInitial = false
        for (index, word) in words.enumerated() {
            let bare = word.trimmingCharacters(in: CharacterSet(charactersIn: ".,"))
            if notNames.contains(bare.lowercased()) { continue }
            guard bare.count > 1, bare.contains(where: \.isLetter) else { skippedInitial = true; continue }
            // "J. Smith": the only word left is a surname, which nobody wants to be called.
            if skippedInitial && index == words.count - 1 && words.count > 1 { return "" }
            return bare
        }
        return ""
    }
}

/// GET /profile's `status`: what the app needs to decide whether to offer the
/// flow and how to label About you.
struct ProfileStatus: Equatable {
    var completed = false
    var hasContent = false
    var offer = false
    var stale = false
    var filled: [String] = []
    var daysSinceUpdate: Int?
    /// False while Cloud "Everything" is on: the answers then travel with each
    /// question (service/memory/profile.py's may_send_to, the same rule the
    /// prompts follow), so "Stays on this Mac" would not be true. An optional
    /// cloud model never gets them. An older service that doesn't say reads as
    /// local, which it always was.
    var localOnly = true
    /// The host they're sent to while localOnly is false ("" when unnamed).
    var sentTo = ""
    /// The user's own verified Macs on their tailnet that also get them
    /// ("mini"): the user decided their data may cross the tailnet to these.
    var tailnetNodes: [String] = []

    init() {}
    init(json: [String: Any]) {
        completed = json["completed"] as? Bool ?? false
        hasContent = json["has_content"] as? Bool ?? false
        offer = json["offer"] as? Bool ?? false
        stale = json["stale"] as? Bool ?? false
        filled = (json["filled"] as? [Any])?.compactMap { $0 as? String } ?? []
        if let days = json["days_since_update"] as? NSNumber, CFGetTypeID(days) != CFBooleanGetTypeID() {
            daysSinceUpdate = days.intValue
        }
        localOnly = json["local_only"] as? Bool ?? true
        sentTo = (json["sent_to"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        tailnetNodes = ((json["tailnet_nodes"] as? [Any]) ?? []).compactMap { $0 as? String }.filter { !$0.isEmpty }
    }

    /// The lock line under every Getting to know you screen and in About you.
    var whereItLives: String {
        if localOnly {
            guard !tailnetNodes.isEmpty else { return "Stays on this Mac" }
            let names = tailnetNodes.count == 1 ? tailnetNodes[0]
                : tailnetNodes.dropLast().joined(separator: ", ") + " and " + tailnetNodes.last!
            return "Stays on this Mac and your \(names) on your tailnet"
        }
        return sentTo.isEmpty ? "Sent with your questions to your cloud model"
                              : "Sent with your questions to \(sentTo)"
    }
}

/// Every /profile response: the profile, its status and the plain sentences
/// that say what Wisp now does with it.
struct ProfileEnvelope: Equatable {
    var profile: UserProfile
    var status: ProfileStatus
    var preview: [String]

    init?(json: [String: Any]) {
        guard let profile = json["profile"] as? [String: Any], let status = json["status"] as? [String: Any] else { return nil }
        self.profile = UserProfile(json: profile)
        self.status = ProfileStatus(json: status)
        preview = (json["preview"] as? [Any])?.compactMap { $0 as? String } ?? []
    }
}

/// What the notch says after the flow ends without Done: Not now, or the
/// service couldn't take the answer.
enum KnowYouNotice {
    static let notNow = "No problem. Whenever you like, type /about, or choose Tell Wisp About You… from the menu bar icon."
    static let notNowUnsaved = "No problem. Wisp's service didn't answer, so I may ask again later. Type /about any time."
    static func thanks(name: String) -> String {
        name.isEmpty ? "Thank you. I'll put this to work." : "Thank you, \(name). I'll put this to work."
    }
}
