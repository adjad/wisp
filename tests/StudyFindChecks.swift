import Foundation

// Pure checks for Study's "Find materials" rules (app/Sources/WispApp/StudyFindRules.swift):
// the course's own addresses, what Wisp never opens, module item types, clean
// titles, and which browser tab is Wisp's own to close. No browser, no
// Accessibility, no network; the course and its addresses are made up.
@main
struct StudyFindChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    typealias C = StudyFindTabCloser
    typealias T = StudyFindTabCloser.Tab

    static func tab(_ window: String, _ key: Int, _ url: String) -> T {
        T(window: window, key: String(key), digest: C.digest(url), url: url)
    }

    static func main() {
        target()
        guardRules()
        types()
        titles()
        tabClaim()
        closingGate()
        print("STUDY_FIND_CHECKS_OK")
    }

    // MARK: - The course's own addresses

    static func target() {
        let course = StudyFindTarget(host: "Canvas.Example.edu", canvasID: "42", courseID: "c1")
        check(course.host == "canvas.example.edu", "host is lowercased")
        check(course.modulesURL?.absoluteString == "https://canvas.example.edu/courses/42/modules", "modules URL")

        let page = "https://canvas.example.edu/courses/42/pages/week-3"
        check(course.allowedURL(page) != nil, "a course page is allowed")
        check(course.openableURL(page) != nil, "a course page is openable")
        check(course.allowedURL("http://canvas.example.edu/courses/42/pages/week-3") == nil, "http is refused")
        check(course.allowedURL("https://canvas.example.edu/courses/43/pages/week-3") == nil, "another course is refused")
        check(course.allowedURL("https://other.example.edu/courses/42/pages/week-3") == nil, "another host is refused")
        check(course.allowedURL("https://canvas.example.edu:8443/courses/42/pages/a") == nil, "a non-default port is refused")
        check(course.allowedURL("https://me@canvas.example.edu/courses/42/pages/a") == nil, "user info is refused")
        check(course.allowedURL("https://canvas.example.edu/courses/42/pages/../../43") == nil, "dot segments are refused")

        // Tools, LTI, quizzes and assignments never open, however the path is spelled.
        for raw in [
            "https://canvas.example.edu/courses/42/external_tools/7",
            "https://canvas.example.edu/courses/42/EXTERNAL_TOOLS/7",
            "https://canvas.example.edu/courses/42/external_tool%2573/7",
            "https://canvas.example.edu/courses/42/quizzes/3",
            "https://canvas.example.edu/courses/42/assignments/9",
            "https://canvas.example.edu/courses/42/lti_resource_links/2",
            "https://canvas.example.edu/courses/42/ltiLaunch",
            "not a url %%",
        ] {
            check(StudyFindTarget.launchesTool(raw), "launches a tool: \(raw)")
            check(course.openableURL(raw) == nil, "never openable: \(raw)")
        }

        // The sections are read as lists; the Assignments list and Syllabus are
        // sections, not launch pages, but they still aren't "openable" items.
        let names = course.sectionURLs.map(\.name)
        check(names == ["Home", "Announcements", "Syllabus", "Assignments", "Pages", "Discussions", "Files", "Grades"],
              "all eight sections, in order")
        check(course.isNavSection("https://canvas.example.edu/courses/42/assignments"), "Assignments list is a section")
        check(course.isNavSection("https://canvas.example.edu/courses/42/modules"), "Modules is a section")
        check(!course.isNavSection("https://canvas.example.edu/courses/42/assignments/9"), "one assignment is not a section")
        check(!course.isLaunchPage("https://canvas.example.edu/courses/42/assignments/syllabus"), "Syllabus is not a launch page")
        check(course.isLaunchPage("https://canvas.example.edu/courses/42/assignments/9"), "an assignment is a launch page")
        check(course.isModules("https://canvas.example.edu/courses/42/modules"), "Modules page")
        check(!course.isModules("https://canvas.example.edu/courses/42/modules/items/5"), "a module item is not Modules")

        check(StudyFindTarget.samePage("HTTPS://Canvas.Example.edu/courses/42/pages/a/#top",
                                       "https://canvas.example.edu/courses/42/pages/a"), "same page ignores case, slash, fragment")
        check(!StudyFindTarget.samePage("https://canvas.example.edu/courses/42/pages/a",
                                        "https://canvas.example.edu/courses/42/pages/b"), "different pages")

        check(StudyFindTarget.courseRef("https://canvas.example.edu/courses/42/assignments/1")! == ("canvas.example.edu", "42"),
              "course reference from an assignment link")
        check(StudyFindTarget.courseRef("https://canvas.example.edu/courses/abc") == nil, "a non-numeric course id")
    }

    // MARK: - What Wisp never opens

    static func guardRules() {
        let cases: [(title: String, url: String, type: String?, refused: Bool)] = [
            ("Week 3 slides", "https://canvas.example.edu/courses/1/modules/items/5", "Page", false),
            ("Lecture 4: Sampling distributions", "https://docs.google.com/presentation/d/1AbC/edit?usp=sharing", nil, false),
            ("Colab: regression notebook", "https://colab.research.google.com/drive/1xyz", nil, false),
            ("News: The housing story", "https://news.example.com/2026/10/01/housing.html", nil, false),
            ("Discussion: framing", "https://canvas.example.edu/courses/1/discussion_topics/7", "Discussion", false),
            ("Worked examples", "https://canvas.example.edu/courses/1/pages/worked-examples", "Page", false),
            ("Reading: Chapter 2", "https://canvas.example.edu/courses/1/files/12/preview", nil, false),
            ("Midterm review", "https://canvas.example.edu/courses/1/pages/midterm-review", "Page", true),
            ("Quiz 3", "https://canvas.example.edu/courses/1/modules/items/9", "Quiz", true),
            ("Essay 1", "https://canvas.example.edu/courses/1/modules/items/10", "Assignment", true),
            ("Unit 2 tool", "https://canvas.example.edu/courses/1/modules/items/11", "ExternalTool", true),
            ("Course feedback", "https://forms.gle/abc", nil, true),
            ("Reading reflection", "https://docs.google.com/forms/d/1x/viewform", nil, true),
            ("Join lecture", "https://school.zoom.us/j/123", nil, true),
            ("Start here", "https://canvas.example.edu/courses/1/pages/start-here", "Page", true),
            ("Pay lab fee", "https://school.touchnet.net/x", nil, true),
            ("Turn in your draft", "https://example.com/a", nil, true),
            ("Hand in lab 2", "https://example.com/b", nil, true),
            ("Homework 2", "https://www.webassign.net/web/Student/x", nil, true),
            ("Final Exam Info", "https://canvas.example.edu/courses/1/pages/info", "Page", true),
            ("MidtermReview", "https://canvas.example.edu/courses/1/pages/x", "Page", true),
            ("notes", "https://canvas.example.edu/courses/1/pages/finalexam-notes", "Page", true),
            ("Sign-in sheet", "https://example.com/a", nil, true),
            ("Quiz3", "https://example.com/q", nil, true),
            ("Due in 3 days", "https://example.com/d", nil, true),
            ("notes", "https://example.com/d?due_in_3", nil, true),
            ("Account settings", "https://canvas.example.edu/profile/settings", nil, true),
            ("notes", "https://canvas.example.edu/courses/1/pages/a%25zz", "Page", true),
            ("notes", "https://canvas.example.edu/courses/1/pages/qu%2569z", "Page", true),
        ]
        for c in cases {
            let reason = StudyFindGuard.refusal(title: c.title, url: c.url, type: c.type)
            check((reason != nil) == c.refused,
                  "\(c.title) at \(c.url): expected \(c.refused ? "refused" : "open"), got \(reason ?? "open")")
        }
        check(StudyFindGuard.refusal(title: "Quiz 3", url: "https://example.com", type: nil) == "sounds like a test or quiz",
              "quiz reason")
        check(StudyFindGuard.refusal(title: "Course survey", url: "https://example.com", type: nil) == "survey or evaluation",
              "survey reason")
        check(StudyFindGuard.fullyDecoded("a%2520b") == "a b", "decoded until stable")
        check(StudyFindGuard.fullyDecoded("a%zz") == nil, "a broken escape won't decode")
    }

    // MARK: - Module item types

    static func types() {
        check(StudyFindRules.typeKey("External URL") == "externalurl", "type key folds spaces")
        check(StudyFindRules.typeKey("external_url") == "externalurl", "type key folds underscores")
        for type in ["ExternalTool", "context_external_tool", "Quiz", "quizzes", "New Quiz", "Assessment", "LTI"] {
            check(StudyFindRules.isLaunchType(type), "launch type: \(type)")
            check(StudyFindRules.isNeverOpened(type), "never opened: \(type)")
            check(!StudyFindRules.isOpenableModuleType(type), "not openable: \(type)")
        }
        check(StudyFindRules.isNeverOpened("Assignment"), "assignments are never opened")
        for type in ["Page", "WikiPage", "Discussion", "DiscussionTopic", "discussion_topic"] {
            check(StudyFindRules.isOpenableModuleType(type), "openable: \(type)")
        }
        for type in ["File", "External URL", "ExternalUrl", "SubHeader", "Unknown"] {
            check(!StudyFindRules.isOpenableModuleType(type), "not opened behind modules/items: \(type)")
        }
        check(!StudyFindRules.isOpenableModuleType(nil), "an unknown type is not opened")
    }

    // MARK: - Clean titles

    static func titles() {
        let cases = [
            ("Lecture 3 Instructor Notes Links to an external site.", "Lecture 3 Instructor Notes"),
            ("Worksheet 2 (opens in a new tab)", "Worksheet 2"),
            ("Slides Link to an external site", "Slides"),
            ("Reading (Links to an external site.)", "Reading"),
            ("Notes Links to an external site. (opens in new window)", "Notes"),
            ("Plain title", "Plain title"),
            ("  Padded  ", "Padded"),
        ]
        for (raw, want) in cases {
            check(StudyFindRules.cleanTitle(raw) == want, "clean title of \(raw): \(StudyFindRules.cleanTitle(raw))")
        }
    }

    // MARK: - Which tab is Wisp's own

    static func tabClaim() {
        let before = [tab("1", 1, "a"), tab("1", 2, "b"), tab("2", 1, "c")]
        // By position (Safari, Orion): one insertion, all else unchanged.
        check(C.newTab(before: before, after: [tab("1", 1, "a"), tab("1", 2, "X"), tab("1", 3, "b"), tab("2", 1, "c")],
                       byID: false) == tab("1", 2, "X"), "a tab inserted in the middle")
        check(C.newTab(before: before, after: before + [tab("2", 2, "X")], byID: false) == tab("2", 2, "X"),
              "a tab added at the end")
        check(C.newTab(before: before, after: before + [tab("9", 1, "X")], byID: false) == tab("9", 1, "X"),
              "a new window with one tab")
        check(C.newTab(before: before, after: before + [tab("2", 2, "X"), tab("9", 1, "Y")], byID: false) == nil,
              "two new tabs: no claim")
        check(C.newTab(before: before, after: [tab("1", 1, "a"), tab("1", 2, "Z"), tab("2", 1, "c"), tab("2", 2, "X")],
                       byID: false) == nil, "the student navigated a tab meanwhile: no claim")
        check(C.newTab(before: before, after: [tab("1", 1, "X"), tab("1", 2, "b"), tab("2", 1, "c")], byID: false) == nil,
              "a closed tab and a new one look alike: no claim")
        check(C.newTab(before: before, after: [tab("1", 1, "a"), tab("1", 2, "b"), tab("1", 3, "X")], byID: false) == nil,
              "a window gone: no claim")
        check(C.newTab(before: before, after: before, byID: false) == nil, "nothing new: no claim")

        // By id (Chromium family).
        let idBefore = [T(window: "1", key: "101", digest: C.digest("a"), url: nil)]
        let mine = T(window: "1", key: "103", digest: C.digest("X"), url: "X")
        check(C.newTab(before: idBefore, after: idBefore + [mine], byID: true) == mine, "one new id")
        check(C.newTab(before: idBefore, after: idBefore + [mine, T(window: "1", key: "104", digest: C.digest("Y"), url: nil)],
                       byID: true) == nil, "two new ids: no claim")

        // At close time.
        let claimWindow = [tab("1", 1, "a"), tab("1", 2, "X"), tab("1", 3, "b")]
        let claimed = tab("1", 2, "X")
        check(C.stillOurs(claimed, claimWindow: claimWindow, now: [tab("1", 1, "a"), tab("1", 2, "L"), tab("1", 3, "b")],
                          addresses: ["X", "L"], byID: false), "still ours after it landed")
        check(!C.stillOurs(claimed, claimWindow: claimWindow, now: [tab("1", 1, "a"), tab("1", 2, "L"), tab("1", 3, "q")],
                           addresses: ["X", "L"], byID: false), "another tab in the window changed")
        check(!C.stillOurs(claimed, claimWindow: claimWindow, now: [tab("1", 1, "L"), tab("1", 2, "b")],
                           addresses: ["X", "L"], byID: false), "tabs shifted by a close")
        check(!C.stillOurs(claimed, claimWindow: claimWindow, now: [tab("1", 1, "a"), tab("1", 2, "M"), tab("1", 3, "b")],
                           addresses: ["X", "L"], byID: false), "the student navigated our tab")
        check(C.stillOurs(mine, claimWindow: [], now: [T(window: "1", key: "103", digest: C.digest("L"), url: nil)],
                          addresses: ["X", "L"], byID: true), "same id, landed address")
        check(!C.stillOurs(mine, claimWindow: [], now: [T(window: "1", key: "104", digest: C.digest("L"), url: nil)],
                           addresses: ["X", "L"], byID: true), "the id is gone")
        check(!C.stillOurs(mine, claimWindow: [], now: [T(window: "1", key: "103", digest: C.digest("M"), url: nil)],
                           addresses: ["X", "L"], byID: true), "same id, someone else's address")

        check(C.digest("a") != C.digest("b") && C.digest("a").count == 64, "addresses are SHA-256 hashes")
        check(C.scriptable.isSuperset(of: C.tabIDs), "every by-id browser is scriptable")
        check(!C.scriptable.contains("org.mozilla.firefox"), "Firefox has no tab scripting")
    }

    // MARK: - When closing stops

    /// A walk with Safari: what one call's answer does to every later call.
    static func walk(_ first: StudyFindTabCloser.Listing) -> StudyFindTabCloser.Gate {
        var gate = StudyFindTabCloser.Gate(browser: "com.apple.Safari")
        check(gate.mayCall && gate.wait == 60, "a new walk may call, waiting for the Automation prompt")
        _ = gate.record(first)
        return gate
    }

    static func closingGate() {
        let browser = "Safari"
        // (a) The student declines the Automation prompt: closing stops, the
        // walk goes on, and the card says the tabs are still open and why.
        var gate = walk(.denied)
        check(!gate.mayCall && gate.problem == .denied, "a refusal stops closing")
        check(gate.record(.tabs([tab("1", 1, "a")])) == nil, "nothing is recorded after a refusal")
        check(!gate.record(.closed) && gate.problem == .denied, "no close counts after a refusal")
        let declined = C.tabsLine(opened: 5, closed: 0, problem: gate.problem, browser: browser)
        check(declined.contains("allow Wisp to control Safari") && declined.contains("Automation")
              && declined.contains("5 tabs are still open"), "the card says why and that the tabs stay open: \(declined)")

        // (b) osascript fails or doesn't answer: the same.
        for (answer, want) in [(StudyFindTabCloser.Listing.failed, C.Outcome.failed), (.timedOut, .timedOut)] {
            let g = walk(answer)
            check(!g.mayCall && g.problem == want, "\(want) stops closing")
            let line = C.tabsLine(opened: 4, closed: 1, problem: g.problem, browser: browser)
            check(line.contains("Wisp stopped closing tabs") && line.contains("It closed 1 of 4"), "card for \(want): \(line)")
        }
        // …and a refusal, failure or timeout at a close stops it the same way.
        for outcome in [C.Outcome.denied, .failed, .timedOut, .unsupported] {
            var g = walk(.tabs([]))
            check(g.mayCall && g.wait == 8, "after one answer, calls wait less")
            check(!g.record(outcome) && !g.mayCall && g.problem == outcome, "a close answering \(outcome) stops closing")
        }

        // (c) After the first problem nothing else changes it: the walk makes no
        // further call (every call site asks mayCall first), so the prompt is
        // never retried in the same walk.
        gate = walk(.timedOut)
        _ = gate.record(.tabs([]))
        _ = gate.record(C.Outcome.denied)
        check(gate.problem == .timedOut && !gate.mayCall, "the first problem stays for the whole walk")

        // A tab that changed hands is left open, but closing goes on.
        var ok = walk(.tabs([]))
        check(!ok.record(.notOne) && ok.mayCall, "a tab no longer ours is left alone; closing continues")
        check(ok.record(.closed) && ok.mayCall, "a closed tab counts")

        // (d) The next walk starts fresh and may try once again.
        let next = StudyFindTabCloser.Gate(browser: "com.apple.Safari")
        check(next.mayCall && next.problem == nil && next.wait == 60, "a later walk may ask again")

        // Browsers without tab scripting never start closing.
        check(StudyFindTabCloser.Gate(browser: "org.mozilla.firefox").problem == .unsupported, "Firefox: no closing")
        check(StudyFindTabCloser.Gate(browser: nil).problem == .unsupported, "no browser: no closing")
        check(C.tabsLine(opened: 3, closed: 3, problem: nil, browser: browser).contains("and closed them"), "all closed")
        check(C.tabsLine(opened: 1, closed: 0, problem: .unsupported, browser: "Firefox").contains("can't close tabs there"),
              "unsupported card")
    }
}
