import Foundation

/// Decides whether a prompt typed in Ask is a request to build something (a small
/// project, in its own folder, in the Ideate tab) or an ordinary Ask turn.
///
/// Pure, deterministic and local: no model call, so Ask stays instant. It is
/// deliberately conservative: it adds up weighted cues (a build verb, an artifact
/// noun, the size of the spec) and only routes when they clearly add up. When in
/// doubt the answer is `.ask`, which is what Ask did before Ideate existed.
///
/// Hook for the Ask submit path (OverlayModel), once the prompt is known to be a
/// plain prompt (not a slash command) and before it is queued/sent:
///
///     if case .ideate(_, let name) = IdeateRoute.classify(prompt, ideateEnabled: NotchTab.strip.contains(.ideate)) {
///         // open the Ideate tab with IdeateRoute.promptBody(prompt) as the idea and `name` as the folder,
///         // and show IdeateRoute.handoffLine(decision) as the Ask row.
///     }
///
/// Overrides: "/ideate <idea>" always routes (when Ideate is available);
/// "/ask <text>" or "ask: <text>" always stays in Ask.
///
/// Checked by tests/IdeateRoutingChecks.swift without the app.
enum IdeateRoute {
    enum Decision: Equatable {
        case ideate(reason: String, projectName: String)
        case ask
    }

    /// Total cue weight needed to route. Typical build requests land at 6 to 9.
    static let threshold = 5
    /// A little lower when the request names a whole thing (strong noun) at length.
    static let longRequestThreshold = 4

    // MARK: Public API

    static func classify(_ text: String, ideateEnabled: Bool = true) -> Decision {
        guard ideateEnabled else { return .ask }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return .ask }

        switch override(trimmed) {
        case .ask?: return .ask
        case .ideate(let body)?:
            guard !body.isEmpty else { return .ask }
            return .ideate(reason: "You asked for Ideate", projectName: slug(for: body))
        case nil: break
        }

        // Short text is a quick question or a command to a person's own data.
        guard words(trimmed).count >= 6 else { return .ask }
        let core = stripPolite(trimmed.lowercased())
        let tokens = words(core)
        guard let first = tokens.first else { return .ask }

        if let fix = classifyFix(core, tokens: tokens, original: trimmed) { return fix }

        // Questions, explanations, research and one-off conversions stay in Ask.
        if questionStarters.contains(first) { return .ask }
        var verb = verbWeight(tokens)
        // "I need a small website for ..." asks for the thing without a build verb.
        if verb == 0, ["i need a", "i need an", "i want a", "i want an", "i'd like a", "i'd like an"]
            .contains(where: { trimmed.lowercased().hasPrefix($0 + " ") }) { verb = 2 }
        if trimmed.hasSuffix("?") && verb == 0 { return .ask }

        guard verb > 0 else { return .ask }
        let (nounScore, hasStrong) = nounWeight(tokens)
        guard nounScore > 0 else { return .ask }
        // The person's own data ("write an email", "make a reminder") is not a project.
        if !hasStrong && !personalTokens.isDisjoint(with: Set(tokens)) { return .ask }

        let score = verb + min(3, nounScore) + min(1, languageWeight(tokens)) + complexity(core, tokens: tokens)
        let need = (hasStrong && tokens.count >= 7) ? longRequestThreshold : threshold
        guard score >= need else { return .ask }
        return .ideate(reason: "A build request with several parts", projectName: slug(for: core))
    }

    /// The idea to hand to Ideate: the prompt without a leading "/ideate ".
    static func promptBody(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        switch override(trimmed) {
        case .ideate(let body)?: return body
        case .ask(let body)?: return body
        default: return trimmed
        }
    }

    /// The line the Ask row shows after a hand-off. Empty for `.ask`.
    static func handoffLine(_ decision: Decision) -> String {
        switch decision {
        case .ask: return ""
        case .ideate(let reason, let name):
            if reason.hasPrefix("Fix") { return "Sent to Ideate — it's working on \(name) there." }
            return "Sent to Ideate — it's building \(name) there."
        }
    }

    // MARK: Overrides

    private enum Override { case ideate(String), ask(String) }

    private static func override(_ trimmed: String) -> Override? {
        let lower = trimmed.lowercased()
        if lower.hasPrefix("/ideate"), let rest = afterWord("/ideate", in: trimmed) { return .ideate(rest) }
        if lower.hasPrefix("/ask"), let rest = afterWord("/ask", in: trimmed) { return .ask(rest) }
        if lower.hasPrefix("ask:") { return .ask(String(trimmed.dropFirst(4)).trimmingCharacters(in: .whitespacesAndNewlines)) }
        return nil
    }

    /// The text after `word` when it is a whole word ("/ideate x"), nil for "/ideatex".
    private static func afterWord(_ word: String, in trimmed: String) -> String? {
        let rest = trimmed.dropFirst(word.count)
        guard rest.isEmpty || rest.first!.isWhitespace else { return nil }
        return rest.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: Cues

    private static let questionStarters: Set<String> = [
        "what", "whats", "what's", "how", "why", "when", "where", "which", "who", "is", "are", "does", "do", "did",
        "should", "explain", "difference", "define", "tell", "show", "describe", "list", "compare", "summarize",
        "summarise", "translate", "convert", "find", "search", "look", "research", "give", "suggest", "recommend",
        "any", "was", "were", "will", "would", "has", "have", "if",
    ]

    private static let politePrefixes = [
        "hey wisp", "wisp", "hey", "okay", "ok", "so", "please", "can you please", "could you please", "can you",
        "could you", "would you", "will you", "i want you to", "i want to", "i'd like you to", "i'd like to",
        "i would like to", "i need you to", "i need to", "i need", "let's", "lets", "let us", "help me",
        "go ahead and", "i want", "i'd like", "now",
    ]

    private static func stripPolite(_ lower: String) -> String {
        var s = lower.trimmingCharacters(in: .whitespacesAndNewlines)
        var changed = true
        while changed {
            changed = false
            for prefix in politePrefixes {
                guard s.hasPrefix(prefix) else { continue }
                let rest = s.dropFirst(prefix.count)
                guard let c = rest.first, c.isWhitespace || c == "," else { continue }
                s = rest.drop(while: { $0.isWhitespace || $0 == "," }).description
                changed = true
                break
            }
        }
        return s
    }

    private static let verbs: [String: Int] = [
        "build": 2, "make": 2, "create": 2, "scaffold": 2, "prototype": 2, "develop": 2, "implement": 2,
        "generate": 2, "whip": 2, "spin": 2, "put": 2, "write": 1, "code": 2, "set": 2,
    ]

    /// Weight of a build verb in the first few words. "code" needs "up", "set" needs "up",
    /// "spin" needs "up", "put" needs "together", "whip" needs "up".
    private static func verbWeight(_ tokens: [String]) -> Int {
        let head = Array(tokens.prefix(4))
        for (i, t) in head.enumerated() {
            guard let w = verbs[t] else { continue }
            let next = i + 1 < head.count ? head[i + 1] : ""
            switch t {
            case "code", "set", "spin", "whip": if next != "up" { continue }
            case "put": if next != "together" { continue }
            default: break
            }
            return w
        }
        return 0
    }

    private static let strongNouns: Set<String> = [
        "app", "apps", "webapp", "website", "site", "game", "cli", "extension", "plugin", "bot", "api", "server",
        "backend", "frontend", "dashboard", "scraper", "crawler", "library", "package", "application",
        "microservice", "webpage", "simulator", "emulator", "compiler", "interpreter",
    ]
    private static let weakNouns: Set<String> = [
        "tool", "script", "page", "program", "timer", "tracker", "calculator", "widget", "component", "service",
        "project", "utility", "generator", "converter", "parser", "tui", "daemon",
    ]

    private static func nounWeight(_ tokens: [String]) -> (Int, Bool) {
        var score = 0
        var strong = false
        var seen = Set<String>()
        for t in tokens where !seen.contains(t) {
            if strongNouns.contains(t) { score += 2; strong = true; seen.insert(t) }
            else if weakNouns.contains(t) { score += 1; seen.insert(t) }
        }
        return (score, strong)
    }

    private static let languages: Set<String> = [
        "python", "flask", "django", "react", "swift", "swiftui", "node", "nodejs", "js", "javascript", "typescript",
        "rust", "go", "golang", "html", "css", "ruby", "rails", "java", "kotlin", "vue", "svelte", "fastapi",
        "express", "sqlite", "bash", "c++", "php", "next", "nextjs",
    ]
    private static func languageWeight(_ tokens: [String]) -> Int { tokens.contains(where: languages.contains) ? 1 : 0 }

    private static let personalTokens: Set<String> = [
        "mail", "emails", "email", "inbox", "calendar", "reminder", "reminders", "event", "events", "note", "notes",
        "playlist", "schedule", "meeting", "meetings", "appointment", "contacts", "message", "messages", "muscle",
        "workout", "diet", "budget", "itinerary", "agenda",
    ]

    private static let featureWords: Set<String> = [
        "login", "auth", "authentication", "database", "sqlite", "persist", "persists", "save", "saves", "export",
        "search", "filter", "sound", "tests", "test", "dark", "responsive", "settings", "sync", "upload", "drag",
        "retries", "logging", "config", "endpoint", "endpoints", "routes", "unit",
    ]

    /// How big the spec is, 0 to 3.
    private static func complexity(_ core: String, tokens: [String]) -> Int {
        var cues = 0
        let set = Set(tokens)
        if set.contains("with") { cues += 1 }
        if set.contains("and") || set.contains("plus") || set.contains("also") { cues += 1 }
        if core.contains(",") { cues += 1 }
        if set.contains("that") || set.contains("which") || set.contains("should") { cues += 1 }
        if !featureWords.isDisjoint(with: set) { cues += 1 }
        if tokens.count >= 10 { cues += 1 }
        let bullets = core.split(separator: "\n").filter { l in
            let t = l.trimmingCharacters(in: .whitespaces)
            return t.hasPrefix("-") || t.hasPrefix("*") || t.hasPrefix("•") || (t.first?.isNumber ?? false)
        }
        if bullets.count >= 2 { cues += 2 }
        return min(3, cues)
    }

    // MARK: Fix-this-project

    private static func classifyFix(_ core: String, tokens: [String], original: String) -> Decision? {
        guard let first = tokens.first, ["fix", "debug", "repair", "resolve"].contains(first) else { return nil }
        let set = Set(tokens)
        guard !set.isDisjoint(with: ["project", "repo", "repository", "codebase"]) else { return nil }
        let files = fileRefs(in: original)
        let trace = core.contains("traceback") || core.contains("stack trace") || core.contains("stacktrace")
            || original.range(of: #"\bat \S+:\d+"#, options: .regularExpression) != nil
        guard files.count >= 2 || (trace && !files.isEmpty) || (trace && core.contains("my project")) else { return nil }
        let stem = files.first.map { ($0 as NSString).lastPathComponent }
            .map { ($0 as NSString).deletingPathExtension } ?? ""
        let name = sanitize(stem.isEmpty ? "project-fix" : "fix-" + stem)
        return .ideate(reason: "Fix across a project", projectName: name)
    }

    private static func fileRefs(in text: String) -> [String] {
        let pattern = #"[\w/\-]+\.(?:py|js|ts|tsx|jsx|swift|rs|go|java|rb|css|html|cpp|kt|php)\b"#
        guard let re = try? NSRegularExpression(pattern: pattern) else { return [] }
        var seen = [String]()
        for m in re.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            if let r = Range(m.range, in: text) {
                let s = String(text[r])
                if !seen.contains(s) { seen.append(s) }
            }
        }
        return seen
    }

    // MARK: Words and slugs

    private static func words(_ s: String) -> [String] {
        var out = [String](), cur = ""
        for ch in s.lowercased() {
            if ch.isLetter || ch.isNumber || ch == "'" || ch == "+" { cur.append(ch) }
            else if !cur.isEmpty { out.append(cur); cur = "" }
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    private static let slugSkip: Set<String> = [
        "a", "an", "the", "me", "my", "some", "small", "simple", "basic", "little", "new", "quick", "tiny", "cool",
        "custom", "nice", "neat", "full", "fun", "up", "together", "of", "i", "us", "our", "one", "your", "own",
    ]
    private static let slugGeneric: Set<String> = [
        "app", "apps", "application", "site", "website", "page", "webpage", "script", "tool", "program", "cli",
        "project", "web",
    ]
    private static let clauseWords: Set<String> = [
        "with", "that", "which", "using", "so", "and", "in", "by", "for", "to", "where", "whose", "plus", "including",
        "featuring", "on", "from", "using", "it", "then", "also", "should",
    ]

    /// A short, folder-safe name from the request: "pomodoro-timer".
    static func slug(for text: String) -> String {
        var tokens = words(stripPolite(text.lowercased()))
        // Drop the leading verb (and its particle).
        if let i = tokens.prefix(4).firstIndex(where: { verbs[$0] != nil }) { tokens.removeFirst(i + 1) }
        var pre = [String]()
        for t in tokens {
            if clauseWords.contains(t) && !pre.isEmpty { break }
            if clauseWords.contains(t) { continue }
            pre.append(t)
        }
        let kept = pre.filter { !slugSkip.contains($0) }
        var primary = kept.filter { !slugGeneric.contains($0) && !languages.contains($0) }
        if primary.count == 1, let generic = kept.first(where: { slugGeneric.contains($0) && $0 != "web" }) {
            primary.append(generic)
        }
        if primary.isEmpty { primary = Array(kept.prefix(2)) }
        if primary.isEmpty { return "project" }
        return sanitize(primary.prefix(3).joined(separator: "-"))
    }

    private static func sanitize(_ s: String) -> String {
        var out = ""
        for ch in s.lowercased() {
            if (ch.isASCII && (ch.isLetter || ch.isNumber)) { out.append(ch) }
            else if !out.isEmpty && out.last != "-" { out.append("-") }
        }
        while out.hasSuffix("-") { out.removeLast() }
        if out.count > 32 { out = String(out.prefix(32)); while out.hasSuffix("-") { out.removeLast() } }
        return out.isEmpty ? "project" : out
    }
}
