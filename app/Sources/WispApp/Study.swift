import Foundation

// The Study tab's pure layer (docs/STUDY_TAB.md): the decoded shapes of
// /study, due grouping, the quiz and flash-card state machines, and every
// user-facing sentence. Foundation only: it is compiled standalone with the
// notch's other pure files and tests/StudyChecks.swift, so it must not reach
// for any other app file.
//
// Decoding is deliberately forgiving: a missing optional falls back to a
// default, an unknown assignment kind or status is kept as its string, and a
// material of a kind this build does not know is skipped instead of failing
// the whole state.

// MARK: - Decoding helpers

private struct StudyLossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws {
        value = try? T(from: decoder)
    }
}

extension KeyedDecodingContainer {
    func studyString(_ key: Key, _ fallback: String = "") -> String {
        if let value = try? decodeIfPresent(String.self, forKey: key) { return value }
        return fallback
    }

    func studyOptionalString(_ key: Key) -> String? {
        guard let value = try? decodeIfPresent(String.self, forKey: key) else { return nil }
        return value
    }

    func studyDouble(_ key: Key) -> Double? {
        if let value = try? decodeIfPresent(Double.self, forKey: key) { return value }
        if let text = try? decodeIfPresent(String.self, forKey: key) { return Double(text) }
        return nil
    }

    func studyInt(_ key: Key, _ fallback: Int = 0) -> Int {
        if let value = try? decodeIfPresent(Int.self, forKey: key) { return value }
        if let value = try? decodeIfPresent(Double.self, forKey: key), value.isFinite,
           abs(value) < 1_000_000_000 { return Int(value) }
        return fallback
    }

    func studyOptionalInt(_ key: Key) -> Int? {
        if let value = try? decodeIfPresent(Int.self, forKey: key) { return value }
        if let value = try? decodeIfPresent(Double.self, forKey: key), value.isFinite,
           abs(value) < 1_000_000_000 { return Int(value) }
        return nil
    }

    func studyBool(_ key: Key, _ fallback: Bool = false) -> Bool {
        if let value = try? decodeIfPresent(Bool.self, forKey: key) { return value }
        return fallback
    }

    func studyStrings(_ key: Key) -> [String] {
        guard let items = try? decodeIfPresent([StudyLossy<String>].self, forKey: key) else { return [] }
        return items.compactMap { $0.value }
    }

    /// Every element that decodes; the rest are dropped.
    func studyList<T: Decodable>(_ type: T.Type, _ key: Key) -> [T] {
        guard let items = try? decodeIfPresent([StudyLossy<T>].self, forKey: key) else { return [] }
        return items.compactMap { $0.value }
    }
}

// MARK: - Kinds

enum StudyKind: String, Codable, CaseIterable, Identifiable {
    case guide, quiz, lesson, flashcards

    var id: String { rawValue }

    var title: String {
        switch self {
        case .guide: return "Study guide"
        case .quiz: return "Quiz"
        case .lesson: return "Lesson"
        case .flashcards: return "Flash cards"
        }
    }

    /// Short label for buttons in the narrow notch.
    var label: String {
        switch self {
        case .guide: return "Guide"
        case .quiz: return "Quiz"
        case .lesson: return "Lesson"
        case .flashcards: return "Cards"
        }
    }

    /// SF Symbol name.
    var symbol: String {
        switch self {
        case .guide: return "doc.text"
        case .quiz: return "checklist"
        case .lesson: return "graduationcap"
        case .flashcards: return "rectangle.on.rectangle.angled"
        }
    }

    /// What the busy row says while this kind is being written.
    var verb: String {
        switch self {
        case .guide: return "Writing your study guide…"
        case .quiz: return "Writing a quiz…"
        case .lesson: return "Writing a lesson…"
        case .flashcards: return "Making flash cards…"
        }
    }

    /// What `count` counts for this kind: "question", "card", "step", "section".
    var unit: String {
        switch self {
        case .guide: return "section"
        case .quiz: return "question"
        case .lesson: return "step"
        case .flashcards: return "card"
        }
    }
}

/// What the Study model is doing; nil means idle.
enum StudyBusy: Equatable {
    case loading
    case reading
    case making(StudyKind)
    case saving
}

// MARK: - Connector

struct StudyModelOption: Decodable, Equatable, Identifiable {
    let id: String
    let label: String

    enum CodingKeys: String, CodingKey { case id, label }
}

extension StudyModelOption {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = c.studyString(.label, id)
    }
}

struct StudyProvider: Decodable, Equatable, Identifiable {
    let id: String
    let label: String
    let defaultModel: String
    let models: [StudyModelOption]
    /// Where the person gets a key, as text ("" when unknown). `keyLink` is the URL.
    let keyURL: String
    /// What a key looks like, for the field's placeholder ("sk-or-…").
    let keyHint: String

    enum CodingKeys: String, CodingKey {
        case id, label, models
        case defaultModel = "default_model"
        case keyURL = "key_url"
        case keyHint = "key_hint"
    }

    /// The key page, only when it is https.
    var keyLink: URL? {
        guard let url = URL(string: keyURL), url.scheme?.lowercased() == "https" else { return nil }
        return url
    }

    func model(_ id: String) -> StudyModelOption? { models.first { $0.id == id } }
}

extension StudyProvider {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = c.studyString(.label, id)
        defaultModel = c.studyString(.defaultModel)
        models = c.studyList(StudyModelOption.self, .models)
        keyURL = c.studyString(.keyURL)
        keyHint = c.studyString(.keyHint)
    }
}

struct StudyConnectorState: Decodable, Equatable {
    let provider: String
    let modelID: String
    let ready: Bool
    /// "study" (its own key), "cloud" (the Intel build's cloud fallback) or "none".
    let source: String
    let providers: [StudyProvider]

    enum CodingKeys: String, CodingKey {
        case provider, ready, source, providers
        case modelID = "model_id"
    }

    static let empty = StudyConnectorState(provider: "", modelID: "", ready: false, source: "none", providers: [])

    var current: StudyProvider? { providers.first { $0.id == provider } }
    var usesCloudFallback: Bool { source == "cloud" }
    var hasOwnKey: Bool { source == "study" }
}

extension StudyConnectorState {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = c.studyString(.provider)
        modelID = c.studyString(.modelID)
        ready = c.studyBool(.ready)
        source = c.studyString(.source, "none")
        providers = c.studyList(StudyProvider.self, .providers)
    }
}

// MARK: - Courses and assignments

struct StudyCourse: Decodable, Equatable, Identifiable {
    let id: String
    let name: String
    let code: String?
    let grade: String?
    /// Percent, 0...100 (and more with extra credit).
    let score: Double?
    let url: String?
    let dueCount: Int
    let sample: Bool
    /// Has something due or a grade (the backend sorts these first). Courses
    /// that are not active fold behind "+N more" in the chips row.
    var active: Bool = true

    enum CodingKeys: String, CodingKey {
        case id, name, code, grade, score, url, sample, active
        case dueCount = "due_count"
    }

    /// "CS 101" when there is a code, else the name cut to its first three
    /// words and about 22 characters.
    var shortName: String {
        if let code = code, !code.trimmingCharacters(in: .whitespaces).isEmpty { return code }
        return Self.short(name)
    }

    static let shortNameLimit = 22

    /// The first three words of `name`, at most `shortNameLimit` characters,
    /// with an ellipsis when anything was cut.
    static func short(_ name: String) -> String {
        let words = name.split(whereSeparator: { $0.isWhitespace })
        guard !words.isEmpty else { return name }
        var text = words.prefix(3).joined(separator: " ")
        var cut = words.count > 3
        if text.count > shortNameLimit {
            text = String(text.prefix(shortNameLimit - 1)).trimmingCharacters(in: .whitespaces)
            cut = true
        }
        return cut ? text + "…" : text
    }
}

extension StudyCourse {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = c.studyString(.name, "Course")
        code = c.studyOptionalString(.code)
        grade = c.studyOptionalString(.grade)
        score = c.studyDouble(.score)
        url = c.studyOptionalString(.url)
        dueCount = c.studyInt(.dueCount)
        sample = c.studyBool(.sample)
        active = c.studyBool(.active, true)
    }
}

struct StudyAssignment: Decodable, Equatable, Identifiable {
    let id: String
    let courseID: String
    let courseName: String
    let title: String
    /// assignment | quiz | exam | project | discussion | reading (others kept as sent).
    let kind: String
    /// Epoch seconds, or nil.
    let dueTS: Double?
    let allDay: Bool
    let points: Double?
    /// Score in points, or nil.
    let score: Double?
    /// upcoming | submitted | graded | missing | late (others kept as sent).
    let status: String
    let url: String?
    let sample: Bool

    enum CodingKeys: String, CodingKey {
        case id, title, kind, points, score, status, url, sample
        case courseID = "course_id"
        case courseName = "course_name"
        case dueTS = "due_ts"
        case allDay = "all_day"
    }

    var dueDate: Date? { dueTS.map { Date(timeIntervalSince1970: $0) } }
    /// Submitted or graded: nothing left to do.
    var isDone: Bool { status == "submitted" || status == "graded" }
    var isTest: Bool { kind == "quiz" || kind == "exam" }

    /// Past its due time. An all-day item is due through the end of its day.
    func isPast(now: Date) -> Bool {
        guard let due = dueTS else { return false }
        return (allDay ? due + 86_400 : due) <= now.timeIntervalSince1970
    }

    /// Not done, and either marked missing or past its due time.
    func isMissing(now: Date) -> Bool {
        if isDone { return false }
        if status == "missing" { return true }
        return isPast(now: now)
    }

    /// SF Symbol for the assignment's kind.
    var symbol: String {
        switch kind {
        case "quiz": return "checklist"
        case "exam": return "pencil.circle"
        case "project": return "hammer"
        case "discussion": return "bubble.left.and.bubble.right"
        case "reading": return "book"
        default: return "doc.text"
        }
    }
}

extension StudyAssignment {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        courseID = c.studyString(.courseID)
        courseName = c.studyString(.courseName)
        title = c.studyString(.title, "Untitled")
        kind = c.studyString(.kind, "assignment")
        dueTS = c.studyDouble(.dueTS)
        allDay = c.studyBool(.allDay)
        points = c.studyDouble(.points)
        score = c.studyDouble(.score)
        status = c.studyString(.status, "upcoming")
        url = c.studyOptionalString(.url)
        sample = c.studyBool(.sample)
    }
}

// MARK: - Materials

struct StudyMaterialSummary: Decodable, Equatable, Identifiable {
    let id: String
    let kind: StudyKind
    let title: String
    let courseID: String?
    let assignmentID: String?
    let createdAt: Double
    /// Questions, cards, steps or sections.
    let count: Int
    /// Best quiz score, 0...1, or nil.
    var bestScore: Double?
    let sample: Bool

    enum CodingKeys: String, CodingKey {
        case id, kind, title, count, sample
        case courseID = "course_id"
        case assignmentID = "assignment_id"
        case createdAt = "created_at"
        case bestScore = "best_score"
    }
}

extension StudyMaterialSummary {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        kind = try c.decode(StudyKind.self, forKey: .kind)
        title = c.studyString(.title, kind.title)
        courseID = c.studyOptionalString(.courseID)
        assignmentID = c.studyOptionalString(.assignmentID)
        createdAt = c.studyDouble(.createdAt) ?? 0
        count = c.studyInt(.count)
        bestScore = c.studyDouble(.bestScore)
        sample = c.studyBool(.sample)
    }
}

struct StudyGuideSection: Decodable, Equatable {
    let heading: String
    let points: [String]

    enum CodingKeys: String, CodingKey { case heading, points }
}

extension StudyGuideSection {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        heading = c.studyString(.heading)
        points = c.studyStrings(.points)
    }
}

struct StudyTerm: Decodable, Equatable {
    let term: String
    let meaning: String

    enum CodingKeys: String, CodingKey { case term, meaning }
}

extension StudyTerm {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        term = c.studyString(.term)
        meaning = c.studyString(.meaning)
    }
}

struct StudyGuide: Decodable, Equatable {
    let summary: String
    let sections: [StudyGuideSection]
    let keyTerms: [StudyTerm]
    let practice: [String]

    enum CodingKeys: String, CodingKey {
        case summary, sections, practice
        case keyTerms = "key_terms"
    }
}

extension StudyGuide {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        summary = c.studyString(.summary)
        sections = c.studyList(StudyGuideSection.self, .sections)
        keyTerms = c.studyList(StudyTerm.self, .keyTerms)
        practice = c.studyStrings(.practice)
    }
}

struct StudyQuestion: Decodable, Equatable {
    let prompt: String
    let choices: [String]
    /// 0-based index into `choices`.
    let answer: Int
    let why: String

    enum CodingKeys: String, CodingKey { case prompt, choices, answer, why }
}

extension StudyQuestion {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        prompt = c.studyString(.prompt)
        choices = c.studyStrings(.choices)
        answer = c.studyInt(.answer)
        why = c.studyString(.why)
    }
}

struct StudyQuiz: Decodable, Equatable {
    let questions: [StudyQuestion]

    enum CodingKeys: String, CodingKey { case questions }
}

extension StudyQuiz {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        questions = c.studyList(StudyQuestion.self, .questions)
    }
}

struct StudyLessonStep: Decodable, Equatable {
    let title: String
    let explain: String
    let example: String?
    let check: StudyQuestion?

    enum CodingKeys: String, CodingKey { case title, explain, example, check }
}

extension StudyLessonStep {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = c.studyString(.title)
        explain = c.studyString(.explain)
        let example = c.studyOptionalString(.example)
        self.example = (example?.isEmpty ?? true) ? nil : example
        if let check = try? c.decodeIfPresent(StudyQuestion.self, forKey: .check), !check.choices.isEmpty {
            self.check = check
        } else {
            self.check = nil
        }
    }
}

struct StudyLesson: Decodable, Equatable {
    let steps: [StudyLessonStep]

    enum CodingKeys: String, CodingKey { case steps }
}

extension StudyLesson {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        steps = c.studyList(StudyLessonStep.self, .steps)
    }
}

struct StudyCard: Decodable, Equatable {
    let front: String
    let back: String

    enum CodingKeys: String, CodingKey { case front, back }
}

extension StudyCard {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        front = c.studyString(.front)
        back = c.studyString(.back)
    }
}

struct StudyDeck: Decodable, Equatable {
    let cards: [StudyCard]

    enum CodingKeys: String, CodingKey { case cards }
}

extension StudyDeck {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cards = c.studyList(StudyCard.self, .cards)
    }
}

enum StudyBody: Equatable {
    case guide(StudyGuide)
    case quiz(StudyQuiz)
    case lesson(StudyLesson)
    case flashcards(StudyDeck)

    var kind: StudyKind {
        switch self {
        case .guide: return .guide
        case .quiz: return .quiz
        case .lesson: return .lesson
        case .flashcards: return .flashcards
        }
    }

    /// Sections, questions, steps or cards.
    var count: Int {
        switch self {
        case .guide(let guide): return guide.sections.count
        case .quiz(let quiz): return quiz.questions.count
        case .lesson(let lesson): return lesson.steps.count
        case .flashcards(let deck): return deck.cards.count
        }
    }
}

struct StudyMaterial: Decodable, Equatable, Identifiable {
    let id: String
    let kind: StudyKind
    let title: String
    let courseID: String?
    let assignmentID: String?
    let createdAt: Double
    /// The model that wrote it, or nil (sample material).
    let model: String?
    var bestScore: Double?
    let sample: Bool
    let body: StudyBody

    enum CodingKeys: String, CodingKey {
        case id, kind, title, model, sample, body
        case courseID = "course_id"
        case assignmentID = "assignment_id"
        case createdAt = "created_at"
        case bestScore = "best_score"
    }

    var count: Int { body.count }

    var summary: StudyMaterialSummary {
        StudyMaterialSummary(id: id, kind: kind, title: title, courseID: courseID, assignmentID: assignmentID,
                             createdAt: createdAt, count: count, bestScore: bestScore, sample: sample)
    }
}

extension StudyMaterial {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        let raw = c.studyString(.kind)
        guard let kind = StudyKind(rawValue: raw) else {
            throw DecodingError.dataCorruptedError(forKey: .kind, in: c,
                                                   debugDescription: "Unknown study material kind")
        }
        self.kind = kind
        title = c.studyString(.title, kind.title)
        courseID = c.studyOptionalString(.courseID)
        assignmentID = c.studyOptionalString(.assignmentID)
        createdAt = c.studyDouble(.createdAt) ?? 0
        model = c.studyOptionalString(.model)
        bestScore = c.studyDouble(.bestScore)
        sample = c.studyBool(.sample)
        switch kind {
        case .guide: body = .guide(try c.decode(StudyGuide.self, forKey: .body))
        case .quiz: body = .quiz(try c.decode(StudyQuiz.self, forKey: .body))
        case .lesson: body = .lesson(try c.decode(StudyLesson.self, forKey: .body))
        case .flashcards: body = .flashcards(try c.decode(StudyDeck.self, forKey: .body))
        }
    }
}

// MARK: - State

struct StudyState: Decodable, Equatable {
    var connector: StudyConnectorState
    var sample: Bool
    var lastImport: Double?
    var courses: [StudyCourse]
    var assignments: [StudyAssignment]
    var materials: [StudyMaterialSummary]

    enum CodingKeys: String, CodingKey {
        case connector, sample, courses, assignments, materials
        case lastImport = "last_import"
    }

    static let empty = StudyState(connector: .empty, sample: false, lastImport: nil,
                                  courses: [], assignments: [], materials: [])

    var hasData: Bool { !courses.isEmpty || !assignments.isEmpty || !materials.isEmpty }

    func course(_ id: String?) -> StudyCourse? {
        guard let id = id else { return nil }
        return courses.first { $0.id == id }
    }

    func assignment(_ id: String?) -> StudyAssignment? {
        guard let id = id else { return nil }
        return assignments.first { $0.id == id }
    }

    func assignments(in course: StudyCourse) -> [StudyAssignment] {
        assignments.filter { $0.courseID == course.id }
    }

    func materials(for course: StudyCourse) -> [StudyMaterialSummary] {
        materials.filter { $0.courseID == course.id }
    }
}

extension StudyState {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let connector = try? c.decodeIfPresent(StudyConnectorState.self, forKey: .connector) {
            self.connector = connector
        } else {
            self.connector = .empty
        }
        sample = c.studyBool(.sample)
        lastImport = c.studyDouble(.lastImport)
        courses = c.studyList(StudyCourse.self, .courses)
        assignments = c.studyList(StudyAssignment.self, .assignments)
        materials = c.studyList(StudyMaterialSummary.self, .materials)
    }
}

/// One page the import looked at.
struct StudyImportPage: Decodable, Equatable {
    let url: String
    let kind: String
    let recognized: Bool

    enum CodingKeys: String, CodingKey { case url, kind, recognized }
}

extension StudyImportPage {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        url = c.studyString(.url)
        kind = c.studyString(.kind, "other")
        recognized = c.studyBool(.recognized)
    }
}

/// `POST /study/import`'s reply.
struct StudyImportReply: Decodable, Equatable {
    let courses: Int
    let assignments: Int
    let pages: [StudyImportPage]
    let state: StudyState

    enum CodingKeys: String, CodingKey { case courses, assignments, pages, state }
}

extension StudyImportReply {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        courses = c.studyInt(.courses)
        assignments = c.studyInt(.assignments)
        pages = c.studyList(StudyImportPage.self, .pages)
        state = try c.decode(StudyState.self, forKey: .state)
    }
}

/// `POST /study/materials/{id}/attempt`'s reply.
struct StudyAttemptReply: Decodable, Equatable {
    let bestScore: Double?

    enum CodingKeys: String, CodingKey { case bestScore = "best_score" }
}

extension StudyAttemptReply {
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bestScore = c.studyDouble(.bestScore)
    }
}

/// The backend's error body: `{"detail": "<code>", "message": "…"}`. A FastAPI
/// 422 sends `detail` as an array; then `code` is nil.
struct StudyErrorReply: Decodable, Equatable {
    let code: String?
    let message: String?

    enum CodingKeys: String, CodingKey { case detail, message }

    init(code: String?, message: String?) {
        self.code = code
        self.message = message
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        code = c.studyOptionalString(.detail)
        message = c.studyOptionalString(.message)
    }
}

/// A school page read from the browser, sent to `POST /study/import`.
struct StudyPage: Encodable, Equatable, Sendable {
    let url: String
    let title: String
    let text: String

    enum CodingKeys: String, CodingKey { case url, title, text }
}

// MARK: - School pages

/// The pages Study reads: https only, on a school site. Mirrors the backend's
/// `canvas_parse.is_school_url`.
enum StudySchoolURL {
    private static let namedLabels: Set<String> = [
        "canvas", "schoology", "powerschool", "blackboard", "brightspace", "d2l", "moodle",
    ]

    static func isSchool(_ url: String) -> Bool {
        guard let parts = URLComponents(string: url.trimmingCharacters(in: .whitespacesAndNewlines)),
              parts.scheme?.lowercased() == "https",
              var host = parts.host?.lowercased(), !host.isEmpty
        else { return false }
        while host.hasSuffix(".") { host.removeLast() }
        let labels = host.split(separator: ".").map { String($0) }
        guard labels.count >= 2 else { return false }
        if host.hasSuffix(".instructure.com") { return true }
        if host == "classroom.google.com" { return true }
        for domain in ["edstem.org", "gradescope.com"] where host == domain || host.hasSuffix("." + domain) {
            return true
        }
        if labels.contains(where: { namedLabels.contains($0) }) { return true }
        if labels.last == "edu" { return true }
        // *.k12.<state>.us
        if labels.count >= 4, labels[labels.count - 1] == "us", labels[labels.count - 3] == "k12" { return true }
        return false
    }
}

// MARK: - Due grouping

struct StudyDueGroup: Equatable, Identifiable {
    let id: String
    let title: String
    let items: [StudyAssignment]
}

enum StudyDue {
    /// Missing, Today, Tomorrow, This week (the five days after tomorrow),
    /// Later, No date. Empty groups are dropped. Submitted and graded work is
    /// never Missing: past its due time it is left out; ahead of it, it stays
    /// under its day. Each group is in due order.
    static func groups(_ items: [StudyAssignment], now: Date, calendar: Calendar) -> [StudyDueGroup] {
        let today = calendar.startOfDay(for: now)
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: today) ?? today.addingTimeInterval(86_400)
        let dayAfter = calendar.date(byAdding: .day, value: 2, to: today) ?? today.addingTimeInterval(172_800)
        let weekEnd = calendar.date(byAdding: .day, value: 7, to: today) ?? today.addingTimeInterval(604_800)

        var missing: [StudyAssignment] = []
        var dueToday: [StudyAssignment] = []
        var dueTomorrow: [StudyAssignment] = []
        var week: [StudyAssignment] = []
        var later: [StudyAssignment] = []
        var undated: [StudyAssignment] = []

        for item in items {
            if item.isMissing(now: now) {
                missing.append(item)
                continue
            }
            guard let due = item.dueTS else {
                undated.append(item)
                continue
            }
            if item.isDone && item.isPast(now: now) { continue }
            let date = Date(timeIntervalSince1970: due)
            if date < tomorrow {
                dueToday.append(item)
            } else if date < dayAfter {
                dueTomorrow.append(item)
            } else if date < weekEnd {
                week.append(item)
            } else {
                later.append(item)
            }
        }

        func ordered(_ list: [StudyAssignment]) -> [StudyAssignment] {
            list.enumerated().sorted { a, b in
                let x = a.element.dueTS ?? Double.greatestFiniteMagnitude
                let y = b.element.dueTS ?? Double.greatestFiniteMagnitude
                if x != y { return x < y }
                return a.offset < b.offset
            }.map { $0.element }
        }

        let all: [(String, String, [StudyAssignment])] = [
            ("missing", "Missing", missing),
            ("today", "Today", dueToday),
            ("tomorrow", "Tomorrow", dueTomorrow),
            ("week", "This week", week),
            ("later", "Later", later),
            ("none", "No date", undated),
        ]
        return all.filter { !$0.2.isEmpty }.map { StudyDueGroup(id: $0.0, title: $0.1, items: ordered($0.2)) }
    }
}

// MARK: - Quiz

/// One pass through a quiz. The first answer to a question is locked in.
struct QuizRun: Equatable {
    let quiz: StudyQuiz
    private(set) var index: Int = 0
    /// The answer chosen for each question, nil when not answered yet.
    private(set) var answers: [Int?]
    private(set) var finished: Bool

    init(quiz: StudyQuiz) {
        self.quiz = quiz
        answers = Array(repeating: nil, count: quiz.questions.count)
        finished = quiz.questions.isEmpty
    }

    var total: Int { quiz.questions.count }

    var current: StudyQuestion? {
        guard !finished, index >= 0, index < total else { return nil }
        return quiz.questions[index]
    }

    /// The answer chosen for the current question.
    var chosen: Int? {
        guard !finished, index >= 0, index < answers.count else { return nil }
        return answers[index]
    }

    var answered: Bool { chosen != nil }

    /// Whether the current question was answered right; nil before answering.
    var isCorrect: Bool? {
        guard let chosen = chosen, let question = current else { return nil }
        return chosen == question.answer
    }

    var isLast: Bool { index >= total - 1 }

    var correct: Int {
        var right = 0
        for (i, answer) in answers.enumerated() where answer != nil && answer == quiz.questions[i].answer {
            right += 1
        }
        return right
    }

    /// "7 of 8".
    var scoreText: String { "\(correct) of \(total)" }

    /// Correct over total, 0...1.
    var fraction: Double { total > 0 ? Double(correct) / Double(total) : 0 }

    /// "Question 3 of 8".
    var progressText: String { "Question \(min(index + 1, max(total, 1))) of \(total)" }

    /// Locks in the first answer to the current question; later taps do nothing.
    mutating func choose(_ choice: Int) {
        guard !finished, let question = current, answers[index] == nil,
              choice >= 0, choice < question.choices.count else { return }
        answers[index] = choice
    }

    /// Moves on once the current question is answered; after the last one the run is finished.
    mutating func next() {
        guard !finished, chosen != nil else { return }
        if isLast {
            finished = true
        } else {
            index += 1
        }
    }

    mutating func restart() {
        index = 0
        answers = Array(repeating: nil, count: total)
        finished = quiz.questions.isEmpty
    }
}

// MARK: - Flash cards

/// One pass through a deck. A card marked not known goes to the back of the
/// queue and comes round again until it is known.
struct FlashRun: Equatable {
    let deck: StudyDeck
    /// Card indices still to go; the first is the current card.
    private(set) var queue: [Int]
    /// Cards that were missed at least once, in the order first missed.
    private(set) var again: [Int] = []
    private(set) var known: Set<Int> = []
    private(set) var flipped = false

    init(deck: StudyDeck) {
        self.deck = deck
        queue = Array(deck.cards.indices)
    }

    var total: Int { deck.cards.count }
    var current: StudyCard? { queue.first.map { deck.cards[$0] } }
    var currentIndex: Int? { queue.first }
    var finished: Bool { queue.isEmpty }
    var knownCount: Int { known.count }
    var remaining: Int { queue.count }
    /// Cards that needed another look.
    var againCount: Int { again.count }
    /// Cards known on the first look: what a finished deck records as its score.
    var firstPassKnown: Int { max(0, total - againCount) }

    mutating func flip() {
        guard !finished else { return }
        flipped.toggle()
    }

    mutating func mark(known isKnown: Bool) {
        guard let card = queue.first else { return }
        queue.removeFirst()
        if isKnown {
            known.insert(card)
        } else {
            if !again.contains(card) { again.append(card) }
            queue.append(card)
        }
        flipped = false
    }

    mutating func restart() {
        queue = Array(deck.cards.indices)
        again = []
        known = []
        flipped = false
    }
}

// MARK: - Copy

/// Every user-facing sentence and formatter in Study. Gentle and plain.
enum StudyText {
    // Buttons and labels
    static let tabTitle = "Study"
    static let readButton = "Read my school tabs"
    static let sampleButton = "Try with sample data"
    static let sampleBadge = "Sample data"
    static let connectorButton = "Study key"
    static let saveKeyButton = "Save"
    static let clearButton = "Remove school data"
    static let doneButton = "Done"
    static let nextButton = "Next"
    static let finishButton = "See score"
    static let tryAgainButton = "Try again"
    static let flipButton = "Flip"
    static let knownButton = "Got it"
    static let againButton = "Again"
    static let coursesHeader = "Courses"
    static let dueHeader = "Coming up"
    static let libraryHeader = "Made for you"
    static let keyTerms = "Key terms"
    static let practice = "Practice"
    static let example = "Example"
    static let check = "Check yourself"

    // Empty states and helpers
    static let emptyTitle = "Nothing from school yet"
    static let emptyBody = "Open your school site in your browser, then press Read my school tabs."
    static let sampleNote = "This is sample data. Read your school tabs to see your own."
    static let nothingDue = "Nothing is due soon."
    static let noMaterials = "Nothing made yet. Choose a course or an assignment, then pick what to make."
    static let readOnlyNote = "Wisp only reads these pages. It never submits or changes anything on your school site."
    static let needsAccessibility = "Wisp needs Accessibility to read your school tabs. "
        + "Turn it on in System Settings › Privacy & Security › Accessibility."
    static let noSchoolTabs = "Wisp didn't find a school page. Open your school site in a browser tab, then try again."
    static let clearConfirm = "This removes your courses, assignments and everything Study made on this Mac."
    static let cleared = "School data removed from this Mac."
    static let topicPlaceholder = "Topic (optional)"

    // Connector
    static let connectorTitle = "Study model"
    static let connectorIntro = "Study uses its own cloud model. Your key is kept in the Keychain and used for Study only."
    static let connectorNeeded = "Add a key so Study can write guides, quizzes, lessons and cards."
    static let connectorFallback = "Study is using this Mac's cloud connection. Add a Study key to keep them separate."
    static let connectorReady = "Study is ready."
    static let keyKept = "A key is saved. Leave this empty to keep it."
    static let keyPlaceholder = "Paste your key"
    static let keyNotSaved = "Wisp couldn't save that key. Check it has no spaces, then try again."
    static let getKey = "Get a key"
    static let removeKeyButton = "Remove key"
    static let removeKeyConfirm = "Remove the saved Study key from this Mac's Keychain?"
    static let removeKeyYes = "Remove"
    static let removeKeyNo = "Keep"
    static let keyRemoved = "Study key removed."

    /// The busy row's words.
    static func busy(_ busy: StudyBusy) -> String {
        switch busy {
        case .loading: return "Loading…"
        case .reading: return "Reading your school tabs…"
        case .making(let kind): return kind.verb
        case .saving: return "Saving…"
        }
    }

    /// The gentle sentence for a backend error code.
    static func problem(code: String) -> String {
        switch code {
        case "missing": return "Add a Study key first. Study uses its own cloud model."
        case "rejected": return "The Study key was turned down. Check it and try again."
        case "busy": return "The Study model is busy right now. Try again in a minute."
        case "unreachable": return "Wisp couldn't reach the Study model. Check your connection and try again."
        case "bad_reply": return "The Study model sent back something Wisp couldn't use. Try again."
        case "not_found": return "That isn't here anymore."
        case "bad_request": return "Wisp couldn't send that. Try again."
        case "offline": return "Wisp's service isn't answering right now. Try again in a moment."
        default: return "Something went wrong. Try again."
        }
    }

    // MARK: Formatters

    private static func formatter(_ pattern: String, _ calendar: Calendar) -> DateFormatter {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = pattern
        return f
    }

    private static func dayText(_ date: Date, now: Date, calendar: Calendar, weekday: Bool) -> String {
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        let pattern = (weekday ? "EEE, " : "") + (sameYear ? "MMM d" : "MMM d, yyyy")
        return formatter(pattern, calendar).string(from: date)
    }

    /// "11:59 PM".
    static func time(_ date: Date, calendar: Calendar) -> String {
        formatter("h:mm a", calendar).string(from: date)
    }

    /// "Due today, 11:59 PM", "Due tomorrow", "Due Mon, Nov 16", "Missing since Nov 12", "No due date".
    static func due(_ a: StudyAssignment, now: Date, calendar: Calendar) -> String {
        if a.isMissing(now: now) {
            guard let date = a.dueDate else { return "Missing" }
            return "Missing since " + dayText(date, now: now, calendar: calendar, weekday: false)
        }
        guard let date = a.dueDate else { return "No due date" }
        if a.isDone && a.isPast(now: now) { return a.status == "graded" ? "Graded" : "Submitted" }
        let today = calendar.startOfDay(for: now)
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: today) ?? today.addingTimeInterval(86_400)
        let dayAfter = calendar.date(byAdding: .day, value: 2, to: today) ?? today.addingTimeInterval(172_800)
        if date < tomorrow {
            return a.allDay ? "Due today" : "Due today, " + time(date, calendar: calendar)
        }
        if date < dayAfter {
            return a.allDay ? "Due tomorrow" : "Due tomorrow, " + time(date, calendar: calendar)
        }
        return "Due " + dayText(date, now: now, calendar: calendar, weekday: true)
    }

    /// The due list's right-hand column, where the row already says "due":
    /// "Today 11:59 PM", "Tomorrow", "Mon, Nov 16", "No date". Missing,
    /// submitted and graded work read as in `due`.
    static func dueShort(_ a: StudyAssignment, now: Date, calendar: Calendar) -> String {
        if a.isMissing(now: now) || (a.isDone && a.isPast(now: now)) { return due(a, now: now, calendar: calendar) }
        guard let date = a.dueDate else { return "No date" }
        let today = calendar.startOfDay(for: now)
        let tomorrow = calendar.date(byAdding: .day, value: 1, to: today) ?? today.addingTimeInterval(86_400)
        let dayAfter = calendar.date(byAdding: .day, value: 2, to: today) ?? today.addingTimeInterval(172_800)
        let at = a.allDay ? "" : " " + time(date, calendar: calendar)
        if date < tomorrow { return "Today" + at }
        if date < dayAfter { return "Tomorrow" + at }
        return dayText(date, now: now, calendar: calendar, weekday: true)
    }

    /// "91%"; 113.4 gives "113%".
    static func percent(_ value: Double) -> String {
        guard value.isFinite else { return "" }
        return "\(Int(value.rounded()))%"
    }

    /// The grade chip: "A- · 91%", "91%", "A-", or nil when the course has neither.
    static func grade(_ c: StudyCourse) -> String? {
        let letter = c.grade?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let score = c.score.flatMap { $0.isFinite ? percent($0) : nil }
        if letter.isEmpty { return score }
        // A grade that is already a percentage is said once.
        if letter.contains("%") { return score ?? letter }
        if let score = score { return letter + " · " + score }
        return letter
    }

    /// "20" or "18.5".
    static func number(_ value: Double) -> String {
        guard value.isFinite else { return "" }
        if value == value.rounded() { return String(Int(value)) }
        return String(format: "%.1f", value)
    }

    /// "18 of 20 pts", "20 pts", or nil.
    static func points(_ a: StudyAssignment) -> String? {
        switch (a.score, a.points) {
        case (let score?, let points?): return number(score) + " of " + number(points) + " pts"
        case (nil, let points?): return number(points) + " pts"
        case (let score?, nil): return number(score) + " pts"
        default: return nil
        }
    }

    /// "8 questions", "1 card".
    static func count(kind: StudyKind, _ n: Int) -> String {
        "\(n) " + kind.unit + (n == 1 ? "" : "s")
    }

    /// "8 questions", "12 cards", "5 steps", "5 sections".
    static func count(_ summary: StudyMaterialSummary) -> String {
        count(kind: summary.kind, summary.count)
    }

    /// "Best 88%", or nil before any attempt. `score` is 0...1.
    static func best(_ score: Double?) -> String? {
        guard let score = score, score.isFinite else { return nil }
        return "Best " + percent(score * 100)
    }

    private static func ago(_ seconds: Double, _ verb: String) -> String {
        if seconds < 60 { return verb + " just now" }
        if seconds < 3600 {
            let n = Int(seconds / 60)
            return verb + " \(n) minute" + (n == 1 ? "" : "s") + " ago"
        }
        if seconds < 86_400 {
            let n = Int(seconds / 3600)
            return verb + " \(n) hour" + (n == 1 ? "" : "s") + " ago"
        }
        if seconds < 172_800 { return verb + " yesterday" }
        return verb + " \(Int(seconds / 86_400)) days ago"
    }

    /// When the school tabs were last read: "Read 5 minutes ago", "Not read yet".
    static func lastRead(_ ts: Double?, now: Date) -> String {
        guard let ts = ts, ts > 0 else { return "Not read yet" }
        return ago(max(0, now.timeIntervalSince1970 - ts), "Read")
    }

    /// When a material was made: "Made 3 hours ago".
    static func made(_ ts: Double, now: Date) -> String {
        guard ts > 0 else { return "" }
        return ago(max(0, now.timeIntervalSince1970 - ts), "Made")
    }

    private static func plural(_ n: Int, _ word: String) -> String {
        "\(n) \(word)" + (n == 1 ? "" : "s")
    }

    /// After reading the school tabs: "Read 2 pages: 5 courses, 9 assignments."
    static func readResult(pages: Int, courses: Int, assignments: Int) -> String {
        if courses == 0 && assignments == 0 {
            return "Wisp read " + plural(pages, "page") + " but didn't find courses or assignments. "
                + "Try your grades or assignments page."
        }
        return "Read " + plural(pages, "page") + ": " + plural(courses, "course") + ", "
            + plural(assignments, "assignment") + "."
    }

    static func readResult(_ reply: StudyImportReply) -> String {
        readResult(pages: reply.pages.count, courses: reply.courses, assignments: reply.assignments)
    }

    /// The end-of-quiz line: "7 of 8 right. That's solid."
    static func quizResult(_ run: QuizRun) -> String {
        let head = run.scoreText + " right."
        if run.total > 0 && run.correct == run.total { return head + " Every one." }
        if run.fraction >= 0.8 { return head + " That's solid." }
        if run.fraction >= 0.5 { return head + " One more pass on the ones you missed would help." }
        return head + " Look over the explanations, then try again."
    }

    /// The end-of-deck line.
    static func flashResult(_ run: FlashRun) -> String {
        if run.againCount == 0 { return "You knew all " + plural(run.total, "card") + "." }
        return "All " + plural(run.total, "card") + " known. " + plural(run.againCount, "card")
            + " took another look."
    }

    /// "3 of 12 known".
    static func cardProgress(_ run: FlashRun) -> String {
        "\(run.knownCount) of \(run.total) known"
    }

    /// "2 due" for a course row, or nil.
    static func dueCount(_ c: StudyCourse) -> String? {
        c.dueCount > 0 ? "\(c.dueCount) due" : nil
    }
}

// MARK: - This week's school work in Brief and Day

extension StudyDaySummary.Due {
    /// As a Study assignment, so Brief and Day word its due time as the Study
    /// tab's list does. No points, grade or link: these rows never show them.
    var assignment: StudyAssignment {
        StudyAssignment(id: id, courseID: "", courseName: course, title: shownTitle,
                        kind: kind.isEmpty ? "assignment" : kind, dueTS: dueTS, allDay: allDay,
                        points: nil, score: nil, status: status.isEmpty ? "upcoming" : status,
                        url: nil, sample: false)
    }
}

extension StudyText {
    /// A Brief or Day school row, read aloud: "Quiz 3, CS 101, Due tomorrow, 10:00 AM".
    static func upcomingSpoken(_ item: StudyDaySummary.Due, now: Date, calendar: Calendar) -> String {
        let a = item.assignment
        return ([a.title, item.course, due(a, now: now, calendar: calendar)].filter { !$0.isEmpty })
            .joined(separator: ", ")
    }

    /// The whole list read aloud, then how many more Study holds.
    static func upcomingSpoken(_ rows: StudyUpcomingRows, now: Date, calendar: Calendar) -> String {
        var parts = rows.items.map { upcomingSpoken($0, now: now, calendar: calendar) }
        if rows.more > 0 { parts.append("\(rows.more) more in Study") }
        return parts.joined(separator: "; ")
    }
}
