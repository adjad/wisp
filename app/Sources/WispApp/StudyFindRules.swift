import CryptoKit
import Foundation

// The pure rules of Study's "Find materials" walk, with no AppKit, SwiftUI or
// Accessibility, so tests/StudyFindChecks.swift can compile them alone:
// - StudyFindTarget: which addresses are the course's own, which are sections,
//   which launch a tool, quiz or assignment;
// - StudyFindGuard: what Wisp never opens (tests, quizzes, surveys, graded or
//   proctored work, submissions, starts, timed items, payment, meetings, accounts);
// - StudyFindRules: module item types and clean titles;
// - StudyFindTabCloser's claim: which tab is Wisp's own, and whether it still is.
// StudyFindMaterials.swift holds the reader, the run and the views.

struct StudyFindTarget: Equatable, Sendable {
    /// Lowercased Canvas host, e.g. "canvas.school.edu".
    let host: String
    /// The digits in `/courses/<n>`.
    let canvasID: String
    /// `StudyCourse.id`, sent as `course_id`.
    let courseID: String

    /// Host and `/courses/<n>` id of an https Canvas course URL.
    static func courseRef(_ raw: String) -> (host: String, id: String)? {
        guard let parts = Self.parts(raw) else { return nil }
        let segments = parts.segments
        guard segments.count >= 2, segments[0] == "courses", Self.isDigits(segments[1]) else { return nil }
        return (parts.host, segments[1])
    }

    init(host: String, canvasID: String, courseID: String) {
        self.host = host.lowercased()
        self.canvasID = canvasID
        self.courseID = courseID
    }

    /// `https://<host>/courses/<id>/modules`.
    var modulesURL: URL? {
        var c = URLComponents()
        c.scheme = "https"
        c.host = host
        c.path = "/courses/\(canvasID)/modules"
        return c.url
    }

    /// The URL to open when `raw` is https on the course's host, default port,
    /// no user info, a clean path under `/courses/<id>/`; else nil.
    func allowedURL(_ raw: String) -> URL? {
        guard let parts = Self.parts(raw), parts.host == host else { return nil }
        let s = parts.segments
        guard s.count >= 2, s[0] == "courses", s[1] == canvasID else { return nil }
        return parts.url
    }

    /// The course's own navigation sections Wisp reads, besides Modules: Canvas's
    /// list pages, read-only. Opening a list never opens an item on it.
    static let sections: [(name: String, path: [String])] = [
        ("Home", []), ("Announcements", ["announcements"]), ("Syllabus", ["assignments", "syllabus"]),
        ("Assignments", ["assignments"]), ("Pages", ["pages"]), ("Discussions", ["discussion_topics"]),
        ("Files", ["files"]), ("Grades", ["grades"]),
    ]
    private static let sectionPaths: Set<[String]> = Set(sections.map(\.path) + [["modules"], ["wiki"]])

    /// Each section's address on this course.
    var sectionURLs: [(name: String, url: URL)] {
        Self.sections.compactMap { section in
            var c = URLComponents()
            c.scheme = "https"
            c.host = host
            c.path = "/" + (["courses", canvasID] + section.path).joined(separator: "/")
            guard let url = c.url, isNavSection(url.absoluteString) else { return nil }
            return (section.name, url)
        }
    }

    /// Exactly one of the course's section list pages (Home, Announcements,
    /// Syllabus, the Assignments list, Pages, Discussions, Files, Grades, Modules).
    func isNavSection(_ raw: String) -> Bool {
        guard let parts = Self.parts(raw), parts.host == host else { return false }
        let s = parts.segments
        guard s.count >= 2, s[0] == "courses", s[1] == canvasID else { return false }
        return Self.sectionPaths.contains(Array(s.dropFirst(2)))
    }

    /// A page on the course that launches a tool, LTI, quiz or assignment item.
    /// A section list page (the Assignments list, the Syllabus) is not one.
    func isLaunchPage(_ raw: String) -> Bool {
        Self.launchesTool(raw) && !isNavSection(raw)
    }

    func allows(_ raw: String) -> Bool { allowedURL(raw) != nil }

    /// What Wisp may open: an allowed URL that doesn't launch a tool or quiz.
    /// Checked before every open, whatever the server said.
    func openableURL(_ raw: String) -> URL? {
        guard !Self.launchesTool(raw) else { return nil }
        return allowedURL(raw)
    }

    /// Path segments of pages that launch an External Tool / LTI or a quiz,
    /// where Canvas posts the student's identity to another site (or starts
    /// the quiz), and assignments (an External Tool assignment or a New Quiz
    /// launches its tool as the page opens). Wisp never opens these, nor uses
    /// anything read from them. Study already reads assignment instructions
    /// through its own dashboard read.
    private static let launchSegments: Set<String> = [
        "external_tools", "quizzes", "lti", "quiz_lti", "quizzes_next", "lti_resource_links",
        "external_content", "launch", "assignments",
    ]

    /// True when `raw`'s path has a tool, LTI or quiz segment (any case), or
    /// when it can't be parsed at all.
    static func launchesTool(_ raw: String) -> Bool {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let decoded = StudyFindGuard.fullyDecoded(c.percentEncodedPath)
        else { return true }
        // Decoded until stable first, so "external_tool%2573" is "external_tools".
        let path = decoded.lowercased()
        return path.split(separator: "/").contains { segment in
            launchSegments.contains(String(segment)) || segment.hasPrefix("lti")
        }
    }

    /// Exactly the course's Modules page, `/courses/<id>/modules`.
    func isModules(_ raw: String) -> Bool {
        guard let parts = Self.parts(raw), parts.host == host else { return false }
        let s = parts.segments
        return s.count == 3 && s[0] == "courses" && s[1] == canvasID && s[2] == "modules"
    }

    /// Same page: scheme and host in any case, no fragment, no trailing "/".
    static func samePage(_ a: String, _ b: String) -> Bool {
        func normal(_ raw: String) -> String? {
            guard var c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return nil }
            c.fragment = nil
            c.scheme = c.scheme?.lowercased()
            c.host = c.host?.lowercased()
            var path = c.percentEncodedPath
            while path.count > 1, path.hasSuffix("/") { path.removeLast() }
            c.percentEncodedPath = path
            return c.string
        }
        guard let x = normal(a), let y = normal(b) else { return false }
        return x == y
    }

    /// Host and path without query or fragment, lowercased: how a link Wisp
    /// sent is matched to the item the server returned (it drops Canvas queries).
    static func pathKey(_ raw: String) -> String {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)) else { return raw }
        var path = c.percentEncodedPath
        while path.count > 1, path.hasSuffix("/") { path.removeLast() }
        return (c.host ?? "").lowercased() + path.lowercased()
    }

    struct Parts {
        let url: URL
        let host: String
        /// Path segments, percent-encoded, without empty ones at the end.
        let segments: [String]
    }

    /// Parsed with URLComponents, never by string prefix. Refuses anything not
    /// https, a non-default port, user or password, an encoded "/" "." or "\",
    /// and "." or ".." or empty segments inside the path.
    static func parts(_ raw: String) -> Parts? {
        guard let c = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              c.scheme?.lowercased() == "https",
              c.user == nil, c.password == nil,
              let host = c.host?.lowercased(), !host.isEmpty,
              c.port == nil || c.port == 443,
              let url = c.url
        else { return nil }
        let path = c.percentEncodedPath
        let lower = path.lowercased()
        if lower.contains("%2f") || lower.contains("%2e") || lower.contains("%5c") || path.contains("\\") {
            return nil
        }
        if path.isEmpty { return Parts(url: url, host: host, segments: []) }
        guard path.hasPrefix("/") else { return nil }
        var segments = path.split(separator: "/", omittingEmptySubsequences: false).dropFirst().map(String.init)
        while segments.last == "" { segments.removeLast() }
        if segments.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) { return nil }
        return Parts(url: url, host: host, segments: segments)
    }

    private static func isDigits(_ text: String) -> Bool {
        !text.isEmpty && text.count <= 20 && text.allSatisfy { $0.isASCII && $0.isNumber }
    }
}

enum StudyFindGuard {
    private static let wordRules: [(reason: String, words: Set<String>)] = [
        ("sounds like a test or quiz", ["test", "tests", "pretest", "posttest", "quiz", "quizzes", "quizzing", "exam",
                                         "exams", "midterm", "midterms", "final", "finals", "assessment", "assessments",
                                         "assessing", "examination", "examinations", "pretests", "posttests",
                                         "placement", "examsoft"]),
        ("survey or evaluation", ["survey", "surveys", "evaluation", "evaluations", "eval", "evals", "questionnaire",
                                  "questionnaires", "poll", "polls", "polling", "feedback"]),
        ("graded or proctored", ["graded", "grading", "proctor", "proctored", "proctoring", "proctorio", "honorlock",
                                 "respondus", "lockdown", "examity", "turnitin", "gradescope", "proctoru"]),
        ("submission or upload", ["submit", "submits", "submission", "submissions", "upload", "uploads", "uploading",
                                  "dropbox"]),
        ("starts something", ["attempt", "attempts", "start", "starts", "begin", "resume", "launch", "take", "retake"]),
        ("timed", ["timed", "timer", "countdown"]),
        ("payment or enrolment", ["pay", "payment", "payments", "purchase", "buy", "checkout", "tuition", "fee", "fees",
                                  "billing", "invoice", "enroll", "enrol", "enrolls", "enrols", "enrollment", "enrolment",
                                  "enrollments", "enrolments", "register", "registration", "subscribe", "subscription"]),
        ("meeting or live session", ["meeting", "meetings", "zoom", "webex", "teams", "meet", "live", "webinar",
                                     "conference", "conferences", "livestream", "join", "collaborate"]),
        ("account or settings", ["account", "accounts", "settings", "profile", "password", "login", "logout", "signin",
                                 "signout", "sso", "preferences", "notifications", "permissions", "passwords"]),
    ]

    /// Sites that only host forms, meetings, proctoring, graded homework or payments.
    private static let hostRules: [(reason: String, hosts: [String])] = [
        ("survey or evaluation", ["forms.gle", "forms.office.com", "forms.microsoft.com", "qualtrics.com",
                                  "surveymonkey.com", "typeform.com", "jotform.com", "menti.com", "mentimeter.com",
                                  "polleverywhere.com", "slido.com", "evaluationkit.com", "explorance.com"]),
        ("meeting or live session", ["zoom.us", "zoom.com", "meet.google.com", "teams.microsoft.com", "teams.live.com",
                                     "webex.com", "gotomeeting.com", "bigbluebutton.org", "collaborate.blackboard.com"]),
        ("graded or proctored", ["proctorio.com", "honorlock.com", "examity.com", "respondus.com", "proctoru.com",
                                 "gradescope.com", "turnitin.com", "webassign.net", "zybooks.com", "aleks.com",
                                 "mheducation.com", "mylabmastering.com", "pearson.com", "tophat.com", "iclicker.com",
                                 "kahoot.it", "kahoot.com", "wileyplus.com", "cengage.com", "achieve.macmillanlearning.com"]),
        ("payment or enrolment", ["paypal.com", "stripe.com", "checkout.stripe.com", "touchnet.com", "touchnet.net",
                                  "flywire.com", "cashnet.com"]),
    ]

    /// Why this item is never opened or added, or nil.
    static func refusal(title: String, url: String, type: String?) -> String? {
        if let type {
            if StudyFindRules.isLaunchType(type) { return "sounds like a test or quiz" }
            if StudyFindRules.typeKey(type) == "assignment" { return "submission or upload" }
        }
        if let reason = words(in: title) { return reason }
        guard let c = URLComponents(string: url.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return "unknown address"
        }
        let host = (c.host ?? "").lowercased()
        for rule in hostRules where rule.hosts.contains(where: { host == $0 || host.hasSuffix("." + $0) }) {
            return rule.reason
        }
        if host == "docs.google.com", c.path.lowercased().hasPrefix("/forms") { return "survey or evaluation" }
        // An address that won't decode cleanly is refused, never read raw.
        guard let path = fullyDecoded(c.percentEncodedPath),
              let query = fullyDecoded(c.percentEncodedQuery ?? "") else { return "unknown address" }
        return words(in: path + " " + query)
    }

    /// Percent-decoded until it stops changing (at most 4 rounds); nil when it
    /// won't decode or a %XX is still left.
    static func fullyDecoded(_ text: String) -> String? {
        var current = text
        for _ in 0..<4 {
            guard let next = current.removingPercentEncoding else { return nil }
            if next == current { break }
            current = next
        }
        if current.range(of: "%[0-9A-Fa-f]{2}", options: .regularExpression) != nil { return nil }
        return current
    }

    /// Words run together as stems: "MidtermReview", "finalexam", "quiz3".
    private static let stems = ["quiz", "midterm", "proctor", "survey", "questionnaire", "pretest", "posttest"]

    private static let phrases: [(phrase: String, reason: String)] = [
        ("sign in", "account or settings"), ("log in", "account or settings"), ("sign out", "account or settings"),
        ("log out", "account or settings"), ("check in", "account or settings"),
        ("turn in", "submission or upload"), ("hand in", "submission or upload"),
        ("time limit", "timed"), ("for credit", "graded or proctored"), ("points possible", "graded or proctored"),
        ("mid term", "sounds like a test or quiz"), ("final exam", "sounds like a test or quiz"),
    ]

    private static func words(in text: String) -> String? {
        // camelCase apart ("MidtermReview" → "Midterm Review"), then every
        // non-letter is a space: "sign-in", "log_in", "turn/in" are phrases too.
        var spaced = ""
        var previous: Character = " "
        for ch in text {
            if ch.isUppercase, previous.isLowercase { spaced.append(" ") }
            spaced.append(ch)
            previous = ch
        }
        let lower = spaced.lowercased()
        let tokens = lower.split(whereSeparator: { !($0.isASCII && $0.isLetter) }).map(String.init)
        guard !tokens.isEmpty else { return nil }
        let set = Set(tokens)
        for rule in wordRules where !rule.words.isDisjoint(with: set) { return rule.reason }
        let joined = " " + tokens.joined(separator: " ") + " "
        for item in phrases where joined.contains(" " + item.phrase + " ") { return item.reason }
        // Separators as spaces first, so "due_in_3" and "due-in-3" match too.
        let plain = String(lower.map { $0.isASCII && ($0.isLetter || $0.isNumber) ? $0 : " " })
        if plain.range(of: #"\bdue\s+in\s+\d"#, options: .regularExpression) != nil { return "timed" }
        for token in tokens {
            if stems.contains(where: { token.contains($0) }) { return "sounds like a test or quiz" }
            if token.contains("exam"), !token.hasPrefix("exampl") { return "sounds like a test or quiz" }
        }
        return nil
    }
}

/// Module item types and titles.
enum StudyFindRules {
    /// Module item types that launch a tool or quiz (the server drops them too).
    static func isLaunchType(_ type: String) -> Bool {
        let key = typeKey(type)
        return key == "externaltool" || key == "contextexternaltool" || key == "lti" || key.hasPrefix("quiz")
            || key == "newquiz" || key == "newquizzes" || key == "assessment"
    }

    /// Module item types Wisp never opens: tools, quizzes and assignments.
    static func isNeverOpened(_ type: String) -> Bool {
        isLaunchType(type) || typeKey(type) == "assignment"
    }

    /// The only module item types Wisp opens behind `/modules/items/<n>`:
    /// pages and discussions. Unknown, File, Quiz, Assignment, External URL,
    /// ExternalTool and SubHeader are all refused.
    static func isOpenableModuleType(_ type: String?) -> Bool {
        guard let type else { return false }
        // External URL items are never opened: only their title and
        // /modules/items/<n> are known before the open (the server refuses them too).
        return ["page", "wikipage", "discussion", "discussiontopic"].contains(typeKey(type))
    }

    /// Lower-case ASCII letters only: "External URL", "external_url" and
    /// "ExternalUrl" are all "externalurl".
    static func typeKey(_ type: String) -> String {
        String(type.lowercased().filter { $0.isASCII && $0.isLetter })
    }

    /// Canvas's screen-reader notes on links ("Links to an external site.",
    /// "(opens in a new tab)") taken off a title, so sources get clean names.
    static func cleanTitle(_ title: String) -> String {
        var t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let notes = [
            #"\(?\s*links?\s+to\s+an\s+external\s+site\.?\s*\)?$"#,
            #"\(?\s*opens\s+in\s+(a\s+)?new\s+(tab|window)\.?\s*\)?$"#,
            #"\(?\s*external\s+link\.?\s*\)?$"#,
        ]
        var changed = true
        while changed {
            changed = false
            for note in notes {
                if let range = t.range(of: note, options: [.regularExpression, .caseInsensitive]) {
                    t = String(t[..<range.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines)
                    changed = true
                }
            }
        }
        return t
    }
}

enum StudyFindTabCloser {
    enum Outcome: Equatable, Sendable {
        case closed
        /// The claimed tab is gone or now elsewhere: left alone.
        case notOne
        /// The student didn't allow Wisp to control the browser.
        case denied
        /// A browser without tab scripting (Firefox).
        case unsupported
        /// The browser didn't answer in time: closing stops for the run.
        case timedOut
        case failed
    }

    /// One tab: its window's id, its own id (or position), and its address
    /// only as a hash, so the student's own tabs (mail, bank…) are never held as
    /// text. `url` is kept only for a tab `keep` allows (the address Wisp
    /// opened, a loading tab, or a page on the course). Nothing here is logged,
    /// shown, or sent to the backend or a model; it lives for one comparison.
    struct Tab: Equatable, Sendable {
        let window: String
        let key: String
        let digest: String
        let url: String?
    }

    static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    enum Listing: Sendable { case tabs([Tab]), denied, timedOut, failed }

    /// Browsers whose scripting dictionary has windows › tabs › URL and close.
    static let scriptable: Set<String> = [
        "com.apple.Safari", "com.apple.SafariTechnologyPreview", "com.google.Chrome", "com.google.Chrome.canary",
        "com.microsoft.edgemac", "com.brave.Browser", "company.thebrowser.Browser", "com.vivaldi.Vivaldi",
        "com.kagi.kagimacOS",
    ]

    /// Of those, the ones whose tabs have an id; the others go by position.
    static let tabIDs: Set<String> = [
        "com.google.Chrome", "com.google.Chrome.canary", "com.microsoft.edgemac", "com.brave.Browser",
        "company.thebrowser.Browser", "com.vivaldi.Vivaldi",
    ]

    static func ordered(_ tabs: [Tab]) -> [Tab] { tabs.sorted { (Int($0.key) ?? 0) < (Int($1.key) ?? 0) } }

    /// Wisp's own tab: the one tab in `after` that wasn't in `before`, only when
    /// exactly one is new and nothing else changed in a way that could hide it.
    static func newTab(before: [Tab], after: [Tab], byID: Bool) -> Tab? {
        if byID {
            let known = Set(before.map { $0.window + "\t" + $0.key })
            let fresh = after.filter { !known.contains($0.window + "\t" + $0.key) }
            return fresh.count == 1 ? fresh[0] : nil
        }
        // By position: one window gained exactly one tab, inserted with every
        // other tab in every window keeping its address and order, no tab or
        // window gone; or a new window holds exactly one tab. Anything else
        // (a closed tab, a navigated tab, a reorder, two new tabs) is ambiguous.
        let was = Dictionary(grouping: before, by: \.window).mapValues(ordered)
        let nowByWindow = Dictionary(grouping: after, by: \.window).mapValues(ordered)
        guard Set(was.keys).isSubset(of: Set(nowByWindow.keys)) else { return nil }
        var fresh: [Tab] = []
        for (window, now) in nowByWindow {
            guard let old = was[window] else {
                if now.count == 1 { fresh.append(now[0]) } else { return nil }
                continue
            }
            let nowSeq = now.map(\.digest)
            let oldSeq = old.map(\.digest)
            if nowSeq == oldSeq { continue }
            guard now.count == old.count + 1 else { return nil }
            let i = (0..<old.count).first(where: { nowSeq[$0] != oldSeq[$0] }) ?? old.count
            guard Array(nowSeq[(i + 1)...]) == Array(oldSeq[i...]) else { return nil }
            fresh.append(now[i])
        }
        return fresh.count == 1 ? fresh[0] : nil
    }

    /// At close time, from a fresh list: the claimed tab is still there (same
    /// id; or, by position, its window holds exactly the same other tabs in the
    /// same order as at the claim), and its address is one of `addresses`.
    static func stillOurs(_ claimed: Tab, claimWindow: [Tab], now: [Tab], addresses: [String], byID: Bool) -> Bool {
        let wanted = Set(addresses.map(digest))
        if byID {
            guard let tab = now.first(where: { $0.window == claimed.window && $0.key == claimed.key }) else { return false }
            return wanted.contains(tab.digest)
        }
        let window = ordered(now.filter { $0.window == claimed.window })
        let then = ordered(claimWindow)
        guard window.count == then.count, let i = then.firstIndex(where: { $0.key == claimed.key }),
              window[i].key == claimed.key, wanted.contains(window[i].digest) else { return false }
        for j in window.indices where j != i { guard window[j].digest == then[j].digest else { return false } }
        return true
    }

    /// One walk's tab closing. The first refusal, failure or timeout from the
    /// browser stops closing for the rest of that walk: no further Apple Events
    /// call is made, so the Automation prompt is never asked twice in one walk.
    /// A new walk starts with a new gate and may try once again.
    struct Gate: Equatable, Sendable {
        private(set) var problem: Outcome?
        /// The browser has answered once: later calls wait less.
        private(set) var answered = false

        /// `browser` is the bundle id the walk opens pages in; nil or one
        /// without tab scripting means closing never starts.
        init(browser: String?) {
            problem = browser.map { StudyFindTabCloser.scriptable.contains($0) } == true ? nil : .unsupported
        }

        /// Whether Wisp may call the browser now.
        var mayCall: Bool { problem == nil }
        /// Seconds to wait for the browser: the first call may sit behind the
        /// Automation prompt.
        var wait: TimeInterval { answered ? 8 : 60 }

        /// Records a tab listing; returns the tabs only when the browser answered.
        mutating func record(_ listing: Listing) -> [Tab]? {
            guard problem == nil else { return nil }
            switch listing {
            case .tabs(let tabs):
                answered = true
                return tabs
            case .denied: problem = .denied
            case .timedOut: problem = .timedOut
            case .failed: problem = .failed
            }
            return nil
        }

        /// Records a close; true when the tab was closed.
        mutating func record(_ outcome: Outcome) -> Bool {
            guard problem == nil else { return false }
            switch outcome {
            case .closed:
                answered = true
                return true
            case .notOne: answered = true
            case .denied, .unsupported, .timedOut, .failed: problem = outcome
            }
            return false
        }
    }

    /// The found card's line about the tabs this walk opened and closed.
    static func tabsLine(opened n: Int, closed: Int, problem: Outcome?, browser: String) -> String {
        let left = n - closed
        switch problem {
        case .denied?:
            return "Wisp couldn't close the tabs it opened: allow Wisp to control \(browser) in System Settings › "
                + "Privacy & Security › Automation. \(left) \(left == 1 ? "tab is" : "tabs are") still open."
        case .unsupported?:
            return "Wisp opened \(n) \(n == 1 ? "tab" : "tabs") in \(browser) and can't close tabs there — you can "
                + "close \(n == 1 ? "it" : "them")."
        case .timedOut?:
            return "Wisp stopped closing tabs: \(browser) didn't answer in time. It closed \(closed) of \(n); you can "
                + "close the rest."
        case .failed?:
            return "Wisp stopped closing tabs: \(browser) didn't let it. It closed \(closed) of \(n); you can "
                + "close the rest."
        default:
            if left <= 0 { return "Wisp opened \(n) \(n == 1 ? "tab" : "tabs") in your browser and closed \(n == 1 ? "it" : "them")." }
            return "Wisp opened \(n) \(n == 1 ? "tab" : "tabs") and closed \(closed) — you can close the other \(left)."
        }
    }
}
