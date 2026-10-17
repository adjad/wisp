import Foundation

/// "Getting to know you": the three questions Wisp asks up front so it can work
/// for a person rather than a user. Everything else it learns from the daily
/// check-ins, and About you in the Memory window edits every field. The answers live in the service's profile
/// (service/memory/profile.py, GET/PATCH /profile) and feed every local system
/// prompt, the morning brief's Keeping in mind section, and the order of Today's
/// Needs you. This file is the flow's pure logic: the steps, their copy, the
/// chips, and the patch each step saves. KnowYouView.swift draws it in the notch.
///
/// Checked by tests/KnowYouChecks.swift without the app.
enum KnowYouStep: Int, CaseIterable, Equatable {
    case welcome, name, goals, tone, done

    var isQuestion: Bool { self != .welcome && self != .done }
    static var questions: [KnowYouStep] { allCases.filter(\.isQuestion) }

    /// The question, asked the way a kind colleague would.
    var title: String {
        switch self {
        case .welcome: return "Let me get to know you"
        case .name: return "What should I call you?"
        case .goals: return "What are you working toward?"
        case .tone: return "How do you like to be talked to?"
        case .done: return "Here's how I'll use this"
        }
    }

    /// Why Wisp asks, which is also exactly what it will do with the answer.
    var why: String {
        switch self {
        case .welcome:
            return "Three quick questions, under a minute. Every one is optional. Everything stays on this Mac, and you can change or erase it any time in Memory → About you."
        case .name: return "I'll use it in your morning brief and when we talk."
        case .goals: return "Up to five. Each morning's brief keeps one in view, taking turns, and once a week I'll ask with one tap whether you're on track."
        case .tone: return "Say the word and I'll match it, everywhere."
        case .done: return "Everything below is already in effect. Each evening I'll ask how the day went, one tap per task. It all lives on this Mac; change or erase it any time in Memory → About you."
        }
    }

    var placeholder: String {
        switch self {
        case .name: return "Your first name, or a nickname"
        case .goals: return "Add something you're working toward"
        default: return ""
        }
    }

    /// The profile fields this step saves. Skipping a step saves none of them.
    var fields: [String] {
        switch self {
        case .name: return ["name"]
        case .goals: return ["goals"]
        case .tone: return ["tone", "detail"]
        default: return []
        }
    }
}

/// Where the flow is and how it moves. Progress counts questions, so the
/// welcome screen is 0 and the summary is 1.
struct KnowYouFlow: Equatable {
    private(set) var step: KnowYouStep = .welcome

    init(step: KnowYouStep = .welcome) { self.step = step }

    var isWelcome: Bool { step == .welcome }
    var isDone: Bool { step == .done }
    var questionNumber: Int? {
        guard let index = KnowYouStep.questions.firstIndex(of: step) else { return nil }
        return index + 1
    }
    static var questionCount: Int { KnowYouStep.questions.count }

    var progress: Double {
        switch step {
        case .welcome: return 0
        case .done: return 1
        default: return Double((questionNumber ?? 1) - 1) / Double(Self.questionCount)
        }
    }

    var canGoBack: Bool { step != .welcome && step != .name }
    var isLastQuestion: Bool { step == KnowYouStep.questions.last }

    mutating func begin() { if step == .welcome { step = .name } }
    mutating func advance() {
        guard let next = KnowYouStep(rawValue: step.rawValue + 1) else { return }
        step = next
    }
    mutating func back() {
        guard canGoBack, let previous = KnowYouStep(rawValue: step.rawValue - 1), previous != .welcome else { return }
        step = previous
    }

    /// Where to pick up for someone who already answered some questions: the
    /// first question they have not answered, or the summary when all are. A
    /// profile with nothing in it starts at the welcome.
    static func resume(for profile: UserProfile) -> KnowYouFlow {
        guard profile.hasContent else { return KnowYouFlow() }
        for step in KnowYouStep.questions where !profile.answered(step) {
            return KnowYouFlow(step: step)
        }
        return KnowYouFlow(step: .done)
    }
}

/// The quick answers offered beside each free-text field. Tapping one adds the
/// phrase; nothing is chosen for the user.
enum KnowYouChips {
    static let tones: [(value: String, label: String, detail: String)] = [
        ("gentle", "Gently", "Lead with what's going well. Never scold."),
        ("warm", "Warm and plain", "Friendly, direct, to the point."),
        ("direct", "Straight to the point", "Skip the preamble. Answer first."),
    ]
    static let details: [(value: String, label: String, detail: String)] = [
        ("short", "Short", "Replies stay brief unless I ask for more."),
        ("thorough", "Thorough", "Include the detail and the reasoning."),
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

    var name = ""
    var about = ""
    var goals: [String] = []
    var struggles: [String] = []
    var people: [Person] = []
    var tone = "warm"
    var detail = "short"
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
        tone = json["tone"] as? String ?? "warm"
        detail = json["detail"] as? String ?? "short"
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
    }

    /// Whether the user gave this step an answer. Tone always holds a default,
    /// so it counts as answered once the name or a goal has been.
    func answered(_ step: KnowYouStep) -> Bool {
        switch step {
        case .name: return !name.isEmpty
        case .goals: return !goals.isEmpty
        case .tone: return !name.isEmpty || !goals.isEmpty
        default: return true
        }
    }

    /// The PATCH body for one step: only that step's fields, so Skip never
    /// touches another answer and a saved step never resends the whole profile.
    func patch(for step: KnowYouStep) -> [String: Any] {
        var body: [String: Any] = [:]
        for field in step.fields { body[field] = value(for: field) }
        return body
    }

    /// Everything editable, for About you's Save.
    var fullPatch: [String: Any] {
        var body: [String: Any] = [:]
        for field in ["name", "about", "goals", "struggles", "people", "tone", "detail", "boundaries", "notes"] {
            body[field] = value(for: field)
        }
        return body
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

    /// The first name from the Mac account's full name, offered (never saved)
    /// as the answer to "What should I call you?".
    static func suggestedName(fullName: String) -> String {
        let trimmed = fullName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.split(separator: " ").first else { return "" }
        return String(first)
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
