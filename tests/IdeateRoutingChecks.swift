// Contract for Ask -> Ideate routing (app/Sources/WispApp/IdeateRouting.swift).
// Pure logic: never launches Wisp or contacts its service. Every prompt is invented.
import Foundation

@main
struct IdeateRoutingChecks {
    static func main() {
        var count = 0
        func fail(_ m: String) -> Never { fputs("FAIL: \(m)\n", stderr); exit(1) }
        func check(_ c: Bool, _ m: String) { count += 1; if !c { fail(m) } }

        let route = [
            "make a small flask app with login and a todo list",
            "build a pomodoro timer web page with start/pause and a sound",
            "create a python CLI that renames files by date, with tests",
            "prototype a chrome extension that hides youtube shorts",
            "build me a snake game in python with a high score table",
            "let's make a markdown note-taking app with tags and search",
            "can you code up a discord bot that posts a daily trivia question",
            "scaffold a fastapi server with a users endpoint and sqlite storage",
            "create a landing page for my bakery with a menu and an order form",
            "build a dashboard that shows cpu and memory usage with live charts",
            "write a script that watches a folder and uploads new files to s3 with retries",
            "I want you to build a weather app in react with a five day forecast and dark mode",
            "make a tic tac toe game in html and javascript with a score counter",
            "build a rust cli that counts words in text files and prints a summary table",
            "please create a browser extension that blocks distracting sites during work hours",
            "develop a REST API for a library catalog with auth and pagination",
            "whip up a quick expense tracker app with charts and csv export",
            "spin up a node server that serves a chat room with websockets and usernames",
            "put together a static portfolio website with a gallery, about page and contact form",
            "implement a url shortener service in go with a sqlite database and unit tests",
            "build a flashcard app with spaced repetition and a deck editor",
            "make a personal blog site with markdown posts and an rss feed",
            "create a swift command line tool that resizes images in a folder and logs results",
            "code up a minesweeper game with three difficulty levels and a timer",
            "build a web scraper that collects product prices and saves them to a csv file",
            "write a python script that merges pdf files, adds page numbers and sorts them by name",
            "could you make a calculator app with history and keyboard support",
            "create a kanban board web app with drag and drop and local storage",
            "build a telegram bot that reminds a group about chores and tracks who did them",
            "prototype a menu bar app in swift that shows the current phase of the moon",
            "I need a small website for my band with a tour dates page and a mailing list form",
            "make me a typing speed game with a leaderboard and difficulty settings",
            "scaffold an express api with routes for books and authors plus tests",
            "build a habit tracker app with streaks, reminders and a weekly chart",
            "create a mortgage calculator page with sliders and a payment breakdown graph",
            "generate a flask backend and a simple react frontend for a recipe box",
            "develop a tui file manager in python with vim keys and a preview pane",
            "let's build a platformer game with two levels and a simple level editor",
            "write a go program that pings a list of hosts and shows uptime in a small dashboard",
            "build a plugin for a text editor that sorts imports and removes unused ones",
            "create a quiz app with multiple choice questions, a timer and a results screen",
            "make a bookmark manager extension with folders, tags and import from html",
            "Build a budget splitter app:\n- add friends\n- add expenses\n- show who owes whom",
            "fix the crash in my project: Traceback in server.py line 12 and models.py line 40, fix it in my project",
            "debug my repo, errors in parser.js and lexer.js and index.html keep breaking the build",
            "implement a sudoku solver web app with a puzzle generator and hint button",
            "set up a small django app with user accounts and a polls page",
        ]
        let stay = [
            "build a study schedule for Friday",
            "make a reminder to call the plumber",
            "write an email to my landlord about the broken heater",
            "what does `TypeError: undefined is not a function` mean",
            "regex for an email address",
            "how do I reverse a list in python",
            "create an event for dinner with Sam on Saturday at seven",
            "make a playlist for a long drive with upbeat songs",
            "build muscle fast without going to a gym",
            "what is the difference between a thread and a process",
            "explain how a hash map handles collisions",
            "why does my python script say module not found",
            "what's the best way to learn rust in a month",
            "show me a bash one-liner to count the lines in every file",
            "convert this to python: for (let i = 0; i < 3; i++) { print(i) }",
            "write a sql query that counts orders per customer",
            "how can I build a habit of reading every night?",
            "find the invoice pdf I saved last week in my files",
            "summarize my unread mail from this morning",
            "what is on my calendar tomorrow afternoon",
            "add milk and eggs to my reminders list for tonight",
            "write a thank you note to my neighbor for watering the plants",
            "research the best open source note apps and compare them",
            "create a schedule for the week with gym, study and chores",
            "make a packing list for a three day trip to the mountains",
            "explain the build process of a swift package in simple words",
            "what does a segmentation fault mean in c",
            "is it better to use a class or a struct in swift",
            "give me a quick snippet to debounce a function in javascript",
            "tell me a joke about programmers and coffee machines",
            "build me a plan to save money for a new laptop",
            "write a poem about autumn leaves and quiet streets",
            "can you explain what a closure is with an example",
            "how do I make a git branch from my last commit?",
            "where did I put the notes from last tuesday's meeting",
            "draft a message to my team about the delayed release",
            "create a note called ideas for the weekend trip",
            "what's the difference between a mutex and a semaphore",
            "make coffee reminders every morning at eight",
            "build",
            "make an app",
            "create a game",
            "what is a pomodoro timer",
            "translate good morning into french and spanish please",
            "write a script to back up my notes folder",
            "I want to build a better sleep routine this month",
            "which is faster, a for loop or a list comprehension in python?",
            "open my calendar and make a meeting with the design team on monday",
            "fix this error: ValueError invalid literal for int with base 10",
            "write a python function that reverses a string",
            "make a budget for my rent, food and travel this month",
            "what does the swift keyword inout do exactly",
        ]
        precondition(route.count >= 40 && stay.count >= 40)

        for p in route {
            let d = IdeateRoute.classify(p)
            if case .ideate = d { count += 1 } else { fail("should route to Ideate: \(p)") }
            check(IdeateRoute.classify(p, ideateEnabled: false) == .ask, "disabled must stay in Ask: \(p)")
        }
        for p in stay {
            check(IdeateRoute.classify(p) == .ask, "should stay in Ask: \(p)")
        }

        // Overrides.
        check(IdeateRoute.classify("/ideate a pomodoro timer") == .ideate(reason: "You asked for Ideate", projectName: "pomodoro-timer"),
              "/ideate forces the route")
        check(IdeateRoute.classify("/ideate fix it") != .ask, "/ideate routes even short text")
        check(IdeateRoute.classify("/ideate a pomodoro timer", ideateEnabled: false) == .ask, "/ideate off when Ideate is not in the strip")
        check(IdeateRoute.classify("/ideate") == .ask, "bare /ideate has no idea to send")
        check(IdeateRoute.classify("/ideatex a game") == .ask, "/ideatex is not the command")
        check(IdeateRoute.classify("/ask build a flask app with login and a todo list") == .ask, "/ask keeps it in Ask")
        check(IdeateRoute.classify("ask: build a flask app with login and a todo list") == .ask, "ask: keeps it in Ask")
        check(IdeateRoute.classify("Ask: make a small flask app with login and a todo list") == .ask, "Ask: is case-insensitive")
        check(IdeateRoute.promptBody("/ideate a pomodoro timer") == "a pomodoro timer", "body of /ideate")
        check(IdeateRoute.promptBody("ask: how are you") == "how are you", "body of ask:")
        check(IdeateRoute.promptBody("build a thing") == "build a thing", "plain prompt unchanged")

        // Slugs.
        let slugs: [(String, String)] = [
            ("build a pomodoro timer web page with start/pause and a sound", "pomodoro-timer"),
            ("make a small flask app with login and a todo list", "flask"),
            ("create a python CLI that renames files by date, with tests", "python-cli"),
            ("prototype a chrome extension that hides youtube shorts", "chrome-extension"),
            ("build me a snake game in python with a high score table", "snake-game"),
            ("create a landing page for my bakery with a menu and an order form", "landing-page"),
        ]
        for (p, want) in slugs {
            guard case .ideate(_, let name) = IdeateRoute.classify(p) else { fail("slug case did not route: \(p)") }
            // The flask case is allowed to be "flask-app"; others exact.
            check(name == want || (want == "flask" && name == "flask-app"), "slug for '\(p)' was '\(name)', wanted '\(want)'")
            check(name.range(of: #"^[a-z0-9]+(-[a-z0-9]+)*$"#, options: .regularExpression) != nil, "slug is folder-safe: \(name)")
            check(name.count <= 32, "slug is short: \(name)")
        }
        check(IdeateRoute.slug(for: "") == "project", "empty slug falls back")
        check(IdeateRoute.slug(for: "build me a Café Menü app??") == "caf-men-app" || IdeateRoute.slug(for: "build me a Café Menü app??").allSatisfy { $0.isASCII }, "non-ASCII is folder-safe")

        // Hand-off line.
        let d = IdeateRoute.classify("build a pomodoro timer web page with start/pause and a sound")
        check(IdeateRoute.handoffLine(d) == "Sent to Ideate — it's building pomodoro-timer there.", "hand-off line")
        check(IdeateRoute.handoffLine(.ask).isEmpty, "no line for Ask")
        if case .ideate(let reason, _) = IdeateRoute.classify("fix the crash in my project: Traceback in server.py line 12 and models.py line 40, fix it in my project") {
            check(reason.hasPrefix("Fix"), "fix reason")
        }

        print("\(count) ideate routing checks passed.")
    }
}
