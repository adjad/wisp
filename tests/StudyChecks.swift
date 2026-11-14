// Contract for the Study tab's pure layer (app/Sources/WispApp/Study.swift).
// Compiled with NotchTabChecks.swift, which calls studyChecks. Pure logic: never
// launches Wisp, reads a browser, or contacts its service.
import Foundation

func studyChecks(_ check: (Bool, String) -> Void) {
    func decode<T: Decodable>(_ type: T.Type, _ json: String) -> T? {
        try? JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
    func at(_ day: Int, _ hour: Int, _ minute: Int = 0, month: Int = 11, year: Int = 2026) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
    }
    // Saturday, November 14, 2026, 10:00 AM Pacific.
    let now = at(14, 10)

    // MARK: State decoding

    let stateJSON = """
    {
      "connector": {"provider": "openrouter", "model_id": "google/gemini-3.8-flash", "ready": true, "source": "study",
        "providers": [{"id": "openrouter", "label": "OpenRouter", "default_model": "google/gemini-3.8-flash",
          "models": [{"id": "google/gemini-3.8-flash", "label": "Gemini 3.8 Flash"}],
          "key_url": "https://openrouter.ai/keys", "key_hint": "sk-or-…"},
          {"id": "gemini", "label": "Gemini API", "default_model": "gemini-3.8-flash", "models": [{"id": "gemini-3.8-flash"}],
           "key_url": "http://example.com", "key_hint": ""}]},
      "sample": false,
      "last_import": 1794650000.0,
      "courses": [
        {"id": "canvas:12345", "name": "Introduction to Programming", "code": "CS 101", "grade": "B+",
         "score": 87.5, "url": "https://canvas.example.edu/courses/12345", "due_count": 2, "sample": false},
        {"id": "canvas:2", "name": "Writing 100", "code": null, "grade": null, "score": null, "url": null, "due_count": 0}
      ],
      "assignments": [
        {"id": "canvas:12345:678", "course_id": "canvas:12345", "course_name": "CS 101", "title": "Lab 5: Lists",
         "kind": "assignment", "due_ts": 1794700740.0, "all_day": false, "points": 20.0, "score": null,
         "status": "upcoming", "url": "https://canvas.example.edu/courses/12345/assignments/678", "sample": false},
        {"id": "a2", "title": "Peer review", "kind": "peer_review", "status": "excused", "due_ts": null},
        {"title": "no id, dropped"}
      ],
      "materials": [
        {"id": "m_1", "kind": "quiz", "title": "Lists quiz", "course_id": "canvas:12345", "assignment_id": null,
         "created_at": 1794650000.0, "count": 8, "best_score": 0.75, "sample": false},
        {"id": "m_2", "kind": "mind_map", "title": "Unknown kind", "created_at": 1, "count": 1}
      ]
    }
    """
    let state = decode(StudyState.self, stateJSON)
    check(state != nil, "a full state decodes")
    if let state = state {
        check(state.connector.provider == "openrouter" && state.connector.modelID == "google/gemini-3.8-flash"
              && state.connector.ready && state.connector.source == "study", "connector fields decode")
        check(state.connector.providers.count == 2 && state.connector.current?.label == "OpenRouter",
              "providers decode and the current one is found")
        check(state.connector.providers.first?.keyLink?.absoluteString == "https://openrouter.ai/keys"
              && state.connector.providers.last?.keyLink == nil, "only an https key page becomes a link")
        check(state.connector.providers.last?.models.first?.label == "gemini-3.8-flash",
              "a model without a label falls back to its id")
        check(state.lastImport == 1794650000.0 && !state.sample, "last_import and sample decode")
        check(state.courses.count == 2 && state.courses[0].dueCount == 2 && state.courses[0].score == 87.5,
              "courses decode with snake_case keys")
        check(state.courses[1].code == nil && state.courses[1].grade == nil && !state.courses[1].sample,
              "null and missing course fields default")
        check(state.assignments.count == 2, "an assignment without an id is dropped, the rest kept")
        check(state.assignments[0].courseID == "canvas:12345" && state.assignments[0].dueTS == 1794700740.0
              && state.assignments[0].points == 20 && state.assignments[0].score == nil,
              "assignment fields decode")
        check(state.assignments[1].kind == "peer_review" && state.assignments[1].status == "excused"
              && state.assignments[1].courseName == "" && !state.assignments[1].allDay,
              "unknown kind and status are kept as sent; missing fields default")
        check(state.materials.count == 1 && state.materials[0].kind == .quiz && state.materials[0].bestScore == 0.75,
              "materials decode; one of an unknown kind is skipped")
        check(state.hasData && state.course("canvas:12345")?.shortName == "CS 101"
              && state.courses[1].shortName == "Writing 100", "lookups and short names")
        check(state.courses.allSatisfy { $0.active }, "a course without \"active\" counts as active")
    }
    let bare = decode(StudyState.self, "{}")
    check(bare == StudyState.empty && bare?.connector.source == "none", "an empty object decodes to the empty state")

    // MARK: Materials

    let guideJSON = """
    {"id": "m_g", "kind": "guide", "title": "Lists guide", "course_id": "canvas:12345", "created_at": 10,
     "model": "google/gemini-3.8-flash", "sample": false, "best_score": null,
     "body": {"summary": "Lists hold values.", "sections": [{"heading": "Indexing", "points": ["Start at 0", 3]},
       {"heading": "Slicing", "points": []}], "key_terms": [{"term": "list", "meaning": "an ordered sequence"}],
       "practice": ["Reverse a list"]}}
    """
    let guide = decode(StudyMaterial.self, guideJSON)
    if case .guide(let body)? = guide?.body {
        check(body.sections.count == 2 && body.sections[0].points == ["Start at 0"]
              && body.keyTerms.first?.meaning == "an ordered sequence" && body.practice == ["Reverse a list"],
              "a guide decodes; a non-string point is dropped")
    } else {
        check(false, "a guide decodes")
    }
    check(guide?.count == 2 && guide?.summary.count == 2 && guide?.summary.kind == .guide
          && guide?.model == "google/gemini-3.8-flash", "a material's summary carries its count and kind")

    let quizJSON = """
    {"id": "m_q", "kind": "quiz", "title": "Lists quiz", "created_at": 10,
     "body": {"questions": [
       {"prompt": "First index?", "choices": ["0", "1", "-1", "len"], "answer": 0, "why": "Python counts from 0."},
       {"prompt": "len([1,2])?", "choices": ["1", "2", "3", "0"], "answer": 1, "why": "Two items."},
       {"prompt": "Last item?", "choices": ["a[0]", "a[1]", "a[-1]", "a[len]"], "answer": 2, "why": "-1 is the end."}]}}
    """
    let quizMaterial = decode(StudyMaterial.self, quizJSON)
    var quiz = StudyQuiz(questions: [])
    if case .quiz(let body)? = quizMaterial?.body { quiz = body }
    check(quiz.questions.count == 3 && quiz.questions[2].answer == 2 && quizMaterial?.bestScore == nil
          && quizMaterial?.courseID == nil, "a quiz decodes; missing optionals are nil")

    let lessonJSON = """
    {"id": "m_l", "kind": "lesson", "title": "Loops", "created_at": 10,
     "body": {"steps": [{"title": "for", "explain": "Repeat for each item.", "example": "for x in a: print(x)",
       "check": {"prompt": "Runs how often?", "choices": ["once", "per item"], "answer": 1, "why": "Each item."}},
       {"title": "while", "explain": "Repeat while true.", "example": null, "check": null},
       {"title": "empty", "explain": "x", "example": "", "check": {"prompt": "no choices", "choices": []}}]}}
    """
    if case .lesson(let body)? = decode(StudyMaterial.self, lessonJSON)?.body {
        check(body.steps.count == 3 && body.steps[0].check?.answer == 1 && body.steps[1].example == nil
              && body.steps[1].check == nil, "a lesson decodes with optional example and check")
        check(body.steps[2].example == nil && body.steps[2].check == nil,
              "an empty example and a check without choices are dropped")
    } else {
        check(false, "a lesson decodes")
    }

    let deckJSON = """
    {"id": "m_f", "kind": "flashcards", "title": "Terms", "created_at": 10,
     "body": {"cards": [{"front": "list", "back": "ordered"}, {"front": "dict", "back": "keyed"},
       {"front": "set", "back": "unique"}]}}
    """
    var deck = StudyDeck(cards: [])
    if case .flashcards(let body)? = decode(StudyMaterial.self, deckJSON)?.body { deck = body }
    check(deck.cards.count == 3 && deck.cards[1].back == "keyed", "flash cards decode")
    check(decode(StudyMaterial.self, #"{"id": "x", "kind": "poster", "body": {}}"#) == nil,
          "a full material of an unknown kind does not decode")
    check(decode(StudyMaterial.self, #"{"id": "x", "kind": "quiz"}"#) == nil, "a material without a body does not decode")

    let importReply = decode(StudyImportReply.self, """
    {"courses": 5, "assignments": 9, "pages": [{"url": "https://canvas.example.edu/grades", "kind": "grades",
      "recognized": true}, {"url": "https://canvas.example.edu/x", "kind": "other", "recognized": false}],
     "state": {}}
    """)
    check(importReply?.pages.count == 2 && importReply?.pages[1].recognized == false && importReply?.courses == 5,
          "the import reply decodes")
    check(importReply.map { StudyText.readResult($0) } == "Read 2 pages: 5 courses, 9 assignments.",
          "the import notice counts pages, courses and assignments")
    check(StudyText.readResult(pages: 1, courses: 1, assignments: 1) == "Read 1 page: 1 course, 1 assignment.",
          "the import notice is singular for one")
    check(StudyText.readResult(pages: 1, courses: 0, assignments: 0).hasPrefix("Wisp read 1 page but didn't find"),
          "an import that found nothing says so gently")
    check(decode(StudyAttemptReply.self, #"{"best_score": 0.8}"#)?.bestScore == 0.8, "the attempt reply decodes")

    let errorReply = decode(StudyErrorReply.self, #"{"detail": "missing", "message": "Add a Study key."}"#)
    check(errorReply?.code == "missing" && errorReply?.message == "Add a Study key.", "an error body decodes")
    let validation = decode(StudyErrorReply.self, #"{"detail": [{"loc": ["body"], "msg": "field required"}]}"#)
    check(validation != nil && validation?.code == nil, "a FastAPI 422 detail array is tolerated")

    let page = StudyPage(url: "https://canvas.example.edu", title: "Dashboard", text: "CS 101")
    let encoded = (try? JSONEncoder().encode(page)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
    check(encoded == ["url": "https://canvas.example.edu", "title": "Dashboard", "text": "CS 101"],
          "a page encodes as url, title and text")

    // MARK: School pages

    let school = ["https://canvas.example.edu/courses/1", "https://school.instructure.com/", "https://classroom.google.com/u/0",
                  "https://app.schoology.com/home", "https://district.powerschool.com/", "https://learn.d2l.com/",
                  "https://moodle.school.org/my", "https://edstem.org/us/dashboard", "https://www.gradescope.com/",
                  "https://my.example.edu/", "https://district.k12.ca.us/grades", "https://blackboard.example.com/"]
    let notSchool = ["http://canvas.example.edu/", "https://example.com/", "https://google.com/classroom",
                     "https://canvasfake.com/", "https://edu.example.com/", "file:///Users/x/edu", "not a url",
                     "https://k12.ca.us/", "https://instructure.com.evil.net/"]
    check(school.allSatisfy { StudySchoolURL.isSchool($0) }, "school sites are recognised: "
          + school.filter { !StudySchoolURL.isSchool($0) }.joined(separator: ", "))
    check(!notSchool.contains { StudySchoolURL.isSchool($0) }, "other pages are not: "
          + notSchool.filter { StudySchoolURL.isSchool($0) }.joined(separator: ", "))

    // MARK: Due grouping

    func assignment(_ id: String, _ due: Date?, status: String = "upcoming", allDay: Bool = false,
                    kind: String = "assignment", points: Double? = nil, score: Double? = nil) -> StudyAssignment {
        StudyAssignment(id: id, courseID: "c", courseName: "CS 101", title: id, kind: kind,
                        dueTS: due?.timeIntervalSince1970, allDay: allDay, points: points, score: score,
                        status: status, url: nil, sample: false)
    }
    let items = [
        assignment("later", at(21, 0)),
        assignment("week-end", at(20, 23, 59)),
        assignment("tomorrow-late", at(15, 23, 59)),
        assignment("tomorrow", at(15, 0, 0)),
        assignment("today", at(14, 23, 59)),
        assignment("today-done", at(14, 18), status: "submitted"),
        assignment("past-undone", at(14, 9)),
        assignment("missing", at(12, 23, 59), status: "missing"),
        assignment("graded", at(10, 12), status: "graded"),
        assignment("submitted-missing", at(11, 12), status: "submitted"),
        assignment("late", at(13, 12), status: "late"),
        assignment("undated", nil),
        assignment("undated-missing", nil, status: "missing"),
        assignment("monday", at(16, 9)),
    ]
    let groups = StudyDue.groups(items, now: now, calendar: calendar)
    func ids(_ id: String) -> [String] { groups.first { $0.id == id }?.items.map { $0.id } ?? [] }
    check(groups.map { $0.title } == ["Missing", "Today", "Tomorrow", "This week", "Later", "No date"],
          "groups run Missing, Today, Tomorrow, This week, Later, No date: \(groups.map { $0.title })")
    check(ids("missing") == ["missing", "late", "past-undone", "undated-missing"],
          "missing holds past-due undone work in due order, undated last: \(ids("missing"))")
    check(!ids("missing").contains("graded") && !ids("missing").contains("submitted-missing"),
          "submitted and graded work is never missing")
    check(ids("today") == ["today-done", "today"], "today holds what is due before midnight: \(ids("today"))")
    check(ids("tomorrow") == ["tomorrow", "tomorrow-late"], "midnight starts tomorrow: \(ids("tomorrow"))")
    check(ids("week") == ["monday", "week-end"], "this week is the five days after tomorrow: \(ids("week"))")
    check(ids("later") == ["later"], "a week from today's midnight is later")
    check(ids("none") == ["undated"], "undated undone work goes under No date")
    let quiet = StudyDue.groups([assignment("a", at(30, 9))], now: now, calendar: calendar)
    check(quiet.map { $0.id } == ["later"], "empty groups are dropped")
    check(StudyDue.groups([], now: now, calendar: calendar).isEmpty, "nothing gives no groups")
    let lateNight = at(14, 23, 50)
    let midnight = StudyDue.groups([assignment("a", at(14, 23, 55)), assignment("b", at(15, 0, 5)),
                                    assignment("c", at(14, 23, 40))], now: lateNight, calendar: calendar)
    check(midnight.map { "\($0.id):\($0.items.map { $0.id })" } == ["missing:[\"c\"]", "today:[\"a\"]", "tomorrow:[\"b\"]"],
          "just before midnight: \(midnight.map { $0.id })")

    // MARK: Copy

    check(StudyText.due(assignment("a", at(14, 23, 59)), now: now, calendar: calendar) == "Due today, 11:59 PM",
          "due today with its time")
    check(StudyText.due(assignment("a", at(14, 0), allDay: true), now: now, calendar: calendar) == "Due today",
          "all-day due today")
    check(StudyText.due(assignment("a", at(15, 0), allDay: true), now: now, calendar: calendar) == "Due tomorrow",
          "all-day due tomorrow")
    check(StudyText.due(assignment("a", at(15, 9, 30)), now: now, calendar: calendar) == "Due tomorrow, 9:30 AM",
          "timed due tomorrow")
    check(StudyText.due(assignment("a", at(16, 9)), now: now, calendar: calendar) == "Due Mon, Nov 16",
          "later this week shows the weekday and date")
    check(StudyText.due(assignment("a", at(4, 9, month: 1, year: 2027)), now: now, calendar: calendar)
          == "Due Mon, Jan 4, 2027", "next year's date carries the year")
    check(StudyText.due(assignment("a", at(12, 23, 59), status: "missing"), now: now, calendar: calendar)
          == "Missing since Nov 12", "missing since its due date")
    check(StudyText.due(assignment("a", at(13, 9)), now: now, calendar: calendar) == "Missing since Nov 13",
          "past due and not submitted reads as missing")
    check(StudyText.due(assignment("a", nil), now: now, calendar: calendar) == "No due date", "no due date")
    check(StudyText.due(assignment("a", at(13, 9), status: "submitted"), now: now, calendar: calendar) == "Submitted"
          && StudyText.due(assignment("a", at(13, 9), status: "graded"), now: now, calendar: calendar) == "Graded",
          "past work that is in reads as submitted or graded")
    check(StudyText.dueShort(assignment("a", at(14, 23, 59)), now: now, calendar: calendar) == "Today 11:59 PM"
          && StudyText.dueShort(assignment("a", at(14, 0), allDay: true), now: now, calendar: calendar) == "Today",
          "short due: today with and without a time")
    check(StudyText.dueShort(assignment("a", at(15, 9, 30)), now: now, calendar: calendar) == "Tomorrow 9:30 AM"
          && StudyText.dueShort(assignment("a", at(15, 0), allDay: true), now: now, calendar: calendar) == "Tomorrow",
          "short due: tomorrow with and without a time")
    check(StudyText.dueShort(assignment("a", at(16, 9)), now: now, calendar: calendar) == "Mon, Nov 16"
          && StudyText.dueShort(assignment("a", at(4, 9, month: 1, year: 2027)), now: now, calendar: calendar)
          == "Mon, Jan 4, 2027", "short due: a later date drops \"Due\" and keeps the year when it differs")
    check(StudyText.dueShort(assignment("a", at(12, 23, 59), status: "missing"), now: now, calendar: calendar)
          == "Missing since Nov 12"
          && StudyText.dueShort(assignment("a", at(13, 9), status: "graded"), now: now, calendar: calendar) == "Graded"
          && StudyText.dueShort(assignment("a", nil), now: now, calendar: calendar) == "No date",
          "short due: missing and handed-in work read as before; no date is short")

    // Brief and Day's school rows (StudyDaySummary.upcoming) word their due time as Study's list does.
    let schoolQuiz = StudyDaySummary.Due(id: "q3", title: "Quiz 3", courseName: "Intro to Programming", courseLabel: "CS 101",
                                   dueTS: at(15, 10).timeIntervalSince1970, kind: "quiz")
    let blank = StudyDaySummary.Due(id: "r", title: " ", courseName: "Writing 100", dueTS: at(16, 0).timeIntervalSince1970,
                                    allDay: true)
    check(StudyText.dueShort(schoolQuiz.assignment, now: now, calendar: calendar) == "Tomorrow 10:00 AM"
          && StudyText.dueShort(blank.assignment, now: now, calendar: calendar) == "Mon, Nov 16",
          "a Brief or Day school row's due column")
    check(schoolQuiz.assignment.courseName == "CS 101" && schoolQuiz.assignment.symbol == "checklist"
          && blank.assignment.title == "Untitled" && blank.assignment.kind == "assignment"
          && blank.assignment.status == "upcoming" && blank.assignment.points == nil && blank.assignment.url == nil,
          "the bridge names the course by its label and carries no points or link")
    check(StudyText.upcomingSpoken(schoolQuiz, now: now, calendar: calendar) == "Quiz 3, CS 101, Due tomorrow, 10:00 AM",
          "a school row read aloud: \(StudyText.upcomingSpoken(schoolQuiz, now: now, calendar: calendar))")
    check(StudyText.upcomingSpoken(StudyUpcomingRows(items: [schoolQuiz], more: 2), now: now, calendar: calendar)
          == "Quiz 3, CS 101, Due tomorrow, 10:00 AM; 2 more in Study", "the list read aloud names what Study holds")

    func course(grade: String?, score: Double?) -> StudyCourse {
        StudyCourse(id: "c", name: "Course", code: nil, grade: grade, score: score, url: nil, dueCount: 0, sample: false)
    }
    check(StudyText.grade(course(grade: "B+", score: 87.5)) == "B+ · 88%", "letter and percent")
    check(StudyText.grade(course(grade: nil, score: 87.5)) == "88%", "percent only")
    check(StudyText.grade(course(grade: "B+", score: nil)) == "B+", "letter only")
    check(StudyText.grade(course(grade: nil, score: 113.4)) == "113%", "extra credit above 100% is shown")
    check(StudyText.grade(course(grade: "87.5%", score: 87.5)) == "88%", "a percent grade is said once")
    check(StudyText.grade(course(grade: " ", score: nil)) == nil, "no grade, no chip")
    check(StudyCourse.short("Writing 100") == "Writing 100"
          && StudyCourse.short("Calculus for Science, Business and Economics") == "Calculus for Science,…"
          && StudyCourse.short("Introductory Microeconomics Workshop") == "Introductory Microeco…",
          "a course without a code gets a short name: three words, about 22 characters")
    check(StudyCourse(id: "c", name: "Writing and Rhetoric", code: " ", grade: nil, score: nil, url: nil,
                      dueCount: 0, sample: false).shortName == "Writing and Rhetoric", "a blank code falls back to the name")
    check(decode(StudyCourse.self, #"{"id": "c", "name": "Old course", "active": false}"#)?.active == false,
          "an inactive course decodes as inactive")
    check(StudyText.points(assignment("a", nil, points: 20, score: 18)) == "18 of 20 pts"
          && StudyText.points(assignment("a", nil, points: 20)) == "20 pts"
          && StudyText.points(assignment("a", nil, points: 10, score: 8.5)) == "8.5 of 10 pts"
          && StudyText.points(assignment("a", nil)) == nil, "points")

    func summary(_ kind: StudyKind, _ n: Int) -> StudyMaterialSummary {
        StudyMaterialSummary(id: "m", kind: kind, title: "t", courseID: nil, assignmentID: nil, createdAt: 0,
                             count: n, bestScore: nil, sample: false)
    }
    check(StudyText.count(summary(.quiz, 8)) == "8 questions" && StudyText.count(summary(.flashcards, 12)) == "12 cards"
          && StudyText.count(summary(.lesson, 5)) == "5 steps" && StudyText.count(summary(.guide, 5)) == "5 sections",
          "counts name what they count")
    check(StudyText.count(summary(.quiz, 1)) == "1 question", "a count of one is singular")
    check(StudyText.best(0.875) == "Best 88%" && StudyText.best(nil) == nil && StudyText.best(1) == "Best 100%",
          "best score")
    let nowTS = now.timeIntervalSince1970
    check(StudyText.lastRead(nil, now: now) == "Not read yet", "never read")
    check(StudyText.lastRead(nowTS - 10, now: now) == "Read just now", "read just now")
    check(StudyText.lastRead(nowTS - 60, now: now) == "Read 1 minute ago"
          && StudyText.lastRead(nowTS - 300, now: now) == "Read 5 minutes ago", "read minutes ago")
    check(StudyText.lastRead(nowTS - 3 * 3600, now: now) == "Read 3 hours ago", "read hours ago")
    check(StudyText.lastRead(nowTS - 30 * 3600, now: now) == "Read yesterday"
          && StudyText.lastRead(nowTS - 4 * 86400, now: now) == "Read 4 days ago", "read days ago")
    check(StudyText.lastRead(nowTS + 100, now: now) == "Read just now", "a clock ahead of the Mac reads as just now")
    check(StudyText.made(nowTS - 7200, now: now) == "Made 2 hours ago", "made hours ago")

    check(StudyKind.allCases.map { $0.title } == ["Study guide", "Quiz", "Lesson", "Flash cards"], "kind titles")
    check(StudyKind.allCases.map { $0.label } == ["Guide", "Quiz", "Lesson", "Cards"], "kind labels")
    check(StudyKind.allCases.allSatisfy { !$0.symbol.isEmpty && $0.verb.hasSuffix("…") }, "every kind has a symbol and verb")
    check(StudyText.busy(.making(.quiz)) == "Writing a quiz…" && StudyText.busy(.reading) == "Reading your school tabs…",
          "busy words")

    let codes = ["missing", "rejected", "busy", "unreachable", "bad_reply"]
    let problems = codes.map { StudyText.problem(code: $0) }
    check(Set(problems).count == codes.count && !problems.contains(StudyText.problem(code: "nope")),
          "each error code has its own sentence")
    check(StudyText.problem(code: "missing").contains("Study key"), "missing asks for a Study key")
    check(StudyText.connectorIntro == "Study uses its own cloud model. Your key is kept in the Keychain and used for Study only.",
          "the connector sentence")
    let copy = problems + [StudyText.emptyBody, StudyText.needsAccessibility, StudyText.noSchoolTabs, StudyText.nothingDue,
                           StudyText.noMaterials, StudyText.connectorIntro, StudyText.connectorFallback, StudyText.keyNotSaved,
                           StudyText.readOnlyNote, StudyText.clearConfirm, StudyText.sampleNote,
                           StudyText.removeKeyConfirm, StudyText.keyRemoved]
    check(!copy.contains { $0.contains("!") }, "Study's copy never exclaims")
    check(StudyText.emptyBody == "Open your school site in your browser, then press Read my school tabs."
          && StudyText.sampleBadge == "Sample data" && StudyText.sampleButton == "Try with sample data",
          "empty state, sample button and sample badge")

    // MARK: QuizRun

    var run = QuizRun(quiz: quiz)
    check(run.total == 3 && run.index == 0 && run.current?.prompt == "First index?" && run.chosen == nil && !run.finished,
          "a quiz run starts on the first question")
    run.next()
    check(run.index == 0, "next does nothing before an answer")
    run.choose(9)
    check(run.chosen == nil, "an out-of-range choice is ignored")
    run.choose(0)
    run.choose(1)
    check(run.chosen == 0 && run.isCorrect == true && run.correct == 1, "the first answer is locked in and scored")
    run.next()
    run.choose(3)
    check(run.isCorrect == false && run.correct == 1 && !run.isLast, "a wrong answer scores nothing")
    run.next()
    check(run.isLast && run.progressText == "Question 3 of 3", "the last question knows it is last")
    run.choose(2)
    run.next()
    check(run.finished && run.current == nil && run.chosen == nil, "next after the last question finishes the run")
    check(run.scoreText == "2 of 3" && abs(run.fraction - 2.0 / 3.0) < 0.0001, "score text and fraction")
    check(run.answers == [0, 3, 2], "answers are kept for review")
    check(StudyText.quizResult(run) == "2 of 3 right. One more pass on the ones you missed would help.", "quiz result line")
    run.next()
    check(run.finished && run.index == 2, "next after finishing does nothing")
    run.restart()
    check(run.index == 0 && !run.finished && run.answers == [nil, nil, nil] && run.correct == 0, "restart clears the run")
    let empty = QuizRun(quiz: StudyQuiz(questions: []))
    check(empty.finished && empty.current == nil && empty.fraction == 0, "an empty quiz is finished at once")

    // MARK: FlashRun

    var cards = FlashRun(deck: deck)
    check(cards.total == 3 && cards.remaining == 3 && cards.current?.front == "list" && !cards.flipped,
          "a deck starts on the first card, front up")
    cards.flip()
    check(cards.flipped, "flip shows the back")
    cards.mark(known: true)
    check(cards.knownCount == 1 && cards.current?.front == "dict" && !cards.flipped, "known moves on, front up")
    cards.mark(known: false)
    check(cards.current?.front == "set" && cards.remaining == 2 && cards.again == [1],
          "an unknown card goes to the back of the queue")
    cards.mark(known: true)
    check(cards.current?.front == "dict" && cards.remaining == 1, "the missed card comes round again")
    cards.mark(known: false)
    check(cards.current?.front == "dict" && cards.again == [1] && !cards.finished, "it keeps coming until known")
    cards.mark(known: true)
    check(cards.finished && cards.current == nil && cards.knownCount == 3 && cards.againCount == 1,
          "the deck finishes when every card is known")
    check(cards.firstPassKnown == 2, "a card missed twice counts once against the first pass")
    check(StudyText.flashResult(cards) == "All 3 cards known. 1 card took another look.", "flash result line")
    cards.mark(known: true)
    cards.flip()
    check(cards.finished && !cards.flipped, "a finished deck ignores marks and flips")
    cards.restart()
    check(cards.remaining == 3 && cards.knownCount == 0 && cards.again.isEmpty && cards.current?.front == "list",
          "restart deals the deck again")
    var clean = FlashRun(deck: deck)
    clean.mark(known: true); clean.mark(known: true); clean.mark(known: true)
    check(StudyText.flashResult(clean) == "You knew all 3 cards.", "a clean pass")
    check(clean.firstPassKnown == 3 && FlashRun(deck: deck).firstPassKnown == 3, "a clean pass knows every card first time")
}
