import Foundation

// Inert stand-in: parser checks never build the SwiftUI body or invoke actions.
enum ScriptRunner {
    struct RunTarget { let icon: String; let label: String }
    static func runTarget(forLang: String) -> RunTarget? { nil }
    static func copy(_ text: String) { fatalError("Markdown parser checks must not copy") }
    static func perform(_ target: RunTarget, code: String) { fatalError("Markdown parser checks must not run code") }
}

@main
struct MarkdownChecks {
    static var assertions = 0

    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            exit(1)
        }
        assertions += 1
    }

    static func parse(_ text: String) -> [MarkdownView.Block] {
        MarkdownView(text: text).blocks()
    }

    static func kindNames(_ blocks: [MarkdownView.Block]) -> [String] {
        blocks.map { block in
            switch block.kind {
            case .paragraph: return "paragraph"
            case .table: return "table"
            case .list: return "list"
            case .code: return "code"
            }
        }
    }

    static func paragraphBodies(_ blocks: [MarkdownView.Block]) -> [String] {
        blocks.filter {
            if case .paragraph = $0.kind { return true }
            return false
        }.map(\.body)
    }

    static func visibleText(_ blocks: [MarkdownView.Block]) -> String {
        blocks.map { block in
            switch block.kind {
            case .paragraph, .code:
                return block.body
            case .table:
                return block.tableRows.flatMap { $0 }.joined(separator: " ")
            case .list:
                return block.listItems.map(\.text).joined(separator: " ")
            }
        }.joined(separator: "\n")
    }

    static func withoutPipeSyntax(_ text: String) -> String {
        String(text.filter { $0 != "|" && !$0.isWhitespace })
    }

    static func isFixtureSeparator(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("|"), trimmed.hasSuffix("|") else { return false }
        let cells = trimmed.dropFirst().dropLast().split(separator: "|", omittingEmptySubsequences: false)
        return !cells.isEmpty && cells.allSatisfy { cell in
            let dashes = cell.filter { $0 == "-" }.count
            return dashes >= 2 && cell.allSatisfy { $0.isWhitespace || $0 == "-" || $0 == ":" }
        }
    }

    static func solitaryPipeAndOrderingChecks() {
        let solitary = parse("| A | B |")
        check(kindNames(solitary) == ["paragraph"], "solitary pipe line must be a paragraph")
        check(paragraphBodies(solitary) == ["| A | B |"], "solitary pipe line at end must remain intact")

        let beforeProse = parse("| A | B |\nThen ordinary prose.")
        check(kindNames(beforeProse) == ["paragraph", "paragraph"], "pipe candidate before prose keeps block order")
        check(paragraphBodies(beforeProse) == ["| A | B |", "Then ordinary prose."], "pipe candidate before prose remains intact")

        let beforeList = parse("| A | B |\n- list item")
        check(kindNames(beforeList) == ["paragraph", "list"], "pipe candidate before a list keeps block order")
        check(paragraphBodies(beforeList) == ["| A | B |"], "pipe candidate before a list remains intact")
        check(beforeList.last?.listItems.map(\.text) == ["list item"], "list item remains readable after pipe prose")

        let beforeCode = parse("| A | B |\n```swift\nlet pipe = \"|\"\n```")
        check(kindNames(beforeCode) == ["paragraph", "code"], "pipe candidate before code keeps block order")
        check(paragraphBodies(beforeCode) == ["| A | B |"], "pipe candidate before code remains intact")
        check(beforeCode.last?.body == "let pipe = \"|\"", "code following pipe prose remains intact")
    }

    static func tableValidationChecks() {
        let valid = parse("Before\n| Task | Time |\n| --- | --- |\n| Review | 10:00 |\nAfter")
        check(kindNames(valid) == ["paragraph", "table", "paragraph"], "valid table keeps surrounding prose order")
        check(paragraphBodies(valid) == ["Before", "After"], "surrounding prose remains intact")
        check(valid.count == 3 && valid[1].tableRows == [["Task", "Time"], ["Review", "10:00"]], "valid table retains its header and data cells")

        let noSeparator = parse("| apples | pears |\n| oranges | plums |")
        check(kindNames(noSeparator) == ["paragraph"], "two pipe lines without separator remain prose")
        check(paragraphBodies(noSeparator) == ["| apples | pears |\n| oranges | plums |"], "two pipe lines without separator retain all text")

        let malformed = parse("| A | B |\n| --x | ---- |")
        check(kindNames(malformed) == ["paragraph"], "malformed separator does not make a table")
        check(paragraphBodies(malformed) == ["| A | B |\n| --x | ---- |"], "malformed table candidate retains all text")

        let headerOnly = parse("| A | B |\n| --- | --- |")
        check(kindNames(headerOnly) == ["table"], "header and valid separator remain a table without data rows")
        check(headerOnly.first?.tableRows == [["A", "B"]], "header-only table retains its header")

        let partialRow = parse("| A | B |\n| --- | --- |\n| incomplete")
        check(kindNames(partialRow) == ["table", "paragraph"], "partial row after a table remains prose")
        check(partialRow.first?.tableRows == [["A", "B"]], "table remains intact before a partial row")
        check(paragraphBodies(partialRow) == ["| incomplete"], "partial data row remains readable")
    }

    static func growingPrefixChecks() {
        let source = "| Header | Value |\n| --- | --- |\n| alpha | beta |"
        for length in 1...source.count {
            let prefix = String(source.prefix(length))
            let lines = prefix.components(separatedBy: "\n")
            let expectedLines = lines.enumerated().compactMap { index, line -> String? in
                index == 1 && isFixtureSeparator(line) ? nil : line
            }
            let expected = withoutPipeSyntax(expectedLines.joined())
            let actual = withoutPipeSyntax(visibleText(parse(prefix)))
            check(actual == expected, "growing table prefix \(length) preserves arrived content (expected \(expected), got \(actual))")
        }
    }

    static func existingMarkdownChecks() {
        let blocks = parse("- first\n1. second\n\n```swift\nlet pipe = \"|\"\n```\n\nx^2<br>next")
        check(kindNames(blocks) == ["list", "code", "paragraph"], "list, code, and prose block kinds stay compatible")
        check(blocks[0].listItems.map(\.marker) == ["•", "1."], "bulleted and numbered list markers stay compatible")
        check(blocks[0].listItems.map(\.text) == ["first", "second"], "list text stays compatible")
        check(blocks[1].codeLang == "swift" && blocks[1].body == "let pipe = \"|\"", "fenced code containing a pipe stays unchanged")
        check(blocks[2].body == "x²\nnext", "math superscripts and HTML line breaks stay compatible")

        // The render path remembers a reply's blocks; it must match a fresh parse,
        // including for a reply that keeps growing.
        func same(_ a: [MarkdownView.Block], _ b: [MarkdownView.Block]) -> Bool {
            kindNames(a) == kindNames(b) && a.map(\.body) == b.map(\.body) && a.map(\.tableRows) == b.map(\.tableRows)
                && a.map { $0.listItems.map { $0.marker } } == b.map { $0.listItems.map { $0.marker } }
                && a.map { $0.listItems.map { $0.text } } == b.map { $0.listItems.map { $0.text } }
        }
        let reply = "Intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- item"
        let view = MarkdownView(text: reply)
        // First ask parses; the second is served from the short-lived tier; the third from the cache.
        for pass in 1...3 {
            check(same(view.cachedBlocks(), view.blocks()), "cached blocks match a fresh parse on pass \(pass)")
        }
        let grown = MarkdownView(text: reply + "\n\n```\ncode\n```")
        check(same(grown.cachedBlocks(), grown.blocks()), "grown text is parsed, not served stale")
    }

    static func mathChecks() {
        func body(_ text: String) -> String { visibleText(parse(text)) }
        func same(_ text: String, _ message: String) {
            check(body(text) == text, "\(message): \(body(text))")
        }

        // The reply that prompted this, in all four delimiters.
        check(body("$$\\hat{y} = \\beta_0 + \\beta_1 x$$") == "ŷ = β₀ + β₁ x", "display dollars")
        check(body("\\[\\hat{y} = \\beta_0\\]") == "ŷ = β₀", "display brackets")
        check(body("where $\\beta_1$ is the slope") == "where β₁ is the slope", "inline dollars")
        check(body("where \\(\\beta_1\\) is the slope") == "where β₁ is the slope", "inline parens")

        // List items and table cells convert too, inside bold.
        let items = parse("*   **$\\hat{y}$** is the prediction")
        check(items[0].listItems.first?.text == "**ŷ** is the prediction", "math in a bold list item")
        let table = parse("| term | meaning |\n|---|---|\n| $\\beta_0$ | intercept |")
        check(table[0].tableRows[1][0] == "β₀", "math in a table cell")

        // Display math stands on its own line, even mid-sentence or across lines.
        check(body("So:\n$$\nE = mc^2\n$$\nDone") == "So:\nE = mc²\nDone", "multi-line display")
        check(body("So $$a = b$$ then") == "So\na = b\nthen", "mid-sentence display is its own line")
        check(body("Thus $$a = b$$.") == "Thus\na = b.", "punctuation stays with the formula")

        // Common notation.
        check(body("$\\frac{1}{n}$") == "1/n", "plain fraction")
        check(body("$\\frac{a+b}{2}$") == "(a+b)/2", "compound fraction keeps its parentheses")
        check(body("$\\sqrt{x}$ and $\\sqrt{x+1}$") == "√x and √(x+1)", "roots")
        check(body("$\\sum_{i=1}^{n} x_i$") == "Σᵢ₌₁ⁿ xᵢ", "sum with limits")
        check(body("$a \\leq b \\neq c \\cdot d$") == "a ≤ b ≠ c · d", "operators")
        check(body("$\\bar{x}$") == "x̄", "bar")
        check(body("$\\mathbb{R}^2$") == "ℝ²", "blackboard bold")
        check(body("$x_{ab,c}$") == "x\\_(ab,c)", "no glyph falls back to notation")
        check(body("$\\left( a \\right)$") == "( a )", "sizing commands disappear")
        check(body("$\\text{slope} = 2$") == "slope = 2", "text wrapper")
        check(body("$\\unknowncmd{x}$") == "unknowncmdx", "an unknown command loses only its backslash")
        check(inlineEscapeSurvives(), "a formula's underscore is not Markdown emphasis")

        // Prices and other dollars are prose.
        same("It costs $5 and $10 today", "two prices")
        same("Between $5-$10 or $5/$10", "price ranges")
        same("$20/month and $30/month", "monthly prices")
        same("Pay $100 for the US$", "prose between dollars")
        same("Use $HOME/$PATH and echo $a$b", "shell variables")
        same("That costs $$ and $$ more", "dollar slang")
        check(body("Paid \\$5 for $x$") == "Paid \\$5 for x", "an escaped dollar is not a delimiter")
        check(body("It is $5 and $x^2$ later") == "It is $5 and x² later", "a price before real math")
        check(body("See \\[1\\] and \\[link\\](u)") == "See \\[1\\] and \\[link\\](u)", "escaped brackets stay prose")

        // Code is never touched, in either form.
        same("Run `$x$` and `\\frac{a}{b}` and `x^2` here", "inline code")
        same("Unclosed `$\\beta$", "an inline code span still streaming")
        let fenced = parse("$x$\n```\n$$\\beta$$ costs $5 x^2\n```\n$y$")
        check(fenced.filter { if case .code = $0.kind { return true }; return false }.first?.body
              == "$$\\beta$$ costs $5 x^2", "fenced code")
        check(paragraphBodies(fenced) == ["x", "y"], "prose around a fence still converts")
        check(parse("```\n$$\\beta").first?.body == "$$\\beta", "a fence still streaming")

        // While streaming: never a crash, no half command, and a price is never
        // converted on the way to its final form.
        check(body("$$\\hat{y} = \\beta_0 + \\bet") == "ŷ = β₀ +", "unclosed display, command still arriving")
        check(body("$$a = b$") == "a = b", "half a closing delimiter")
        check(body("\\[a = b") == "a = b", "unclosed display bracket")
        check(body("\\(\\beta") == "β", "unclosed inline parens")
        check(body("where $\\beta_1 is") == "where β₁ is", "an unclosed backslash command can't be a price")
        check(body("$$\\frac{a}{") == "a/", "unclosed group")
        check(body("Text\n\n$$a = b\n\nNext") == "Text\n\na = b\n\nNext", "unclosed display stops at the paragraph")
        same("It costs $5", "an unclosed price")
        same("It costs $", "a lone dollar")
        same("$$", "a lone double dollar")
        let formula = "Slope $\\beta_1$ costs $5 in $$\\hat{y} = \\frac{a}{b}$$ and \\(x^2\\) `$z$`"
        for count in 0...formula.count { _ = parse(String(formula.prefix(count))) }
        check(body(formula) == "Slope β₁ costs $5 in\nŷ = a/b\nand x² `$z$`", "the finished stream")

        // Read-aloud gets the same symbols, without Markdown escapes.
        // A model stuck in a loop: no crash, however deep, finished or still streaming.
        for loop in ["\\frac{", "\\sqrt", "{", "x^{", "\\hat{", "\\frac{a}{", "\\text{"] {
            let deep = String(repeating: loop, count: 50_000)
            for reply in ["$$" + deep + "$$", "\\[" + deep] {
                check(!prettifyMath(reply, forSpeech: true).isEmpty || loop == "{", "survives \(loop) repeated")
            }
        }
        let real = #"$\frac{\sqrt{x^{2}+\frac{1}{\hat{y}_{i}}}}{2}$"#
        check(prettifyMath(real, forSpeech: true) == "(√(x²+1/ŷᵢ))/2", "real nesting reads as before: \(prettifyMath(real, forSpeech: true))")
        check(prettifyMath("$x_{ab,c}$", forSpeech: true) == "x_(ab,c)", "speech has no escapes")
        check(prettifyMath("$x_{ab,c}$") == "x\\_(ab,c)", "display text escapes for the Markdown pass")
    }

    static func inlineEscapeSurvives() -> Bool {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard let parsed = try? AttributedString(markdown: prettifyMath("$a*b*c_{xy,z}$"), options: opts) else { return false }
        return String(parsed.characters) == "a*b*c_(xy,z)"
    }

    /// An answer's inline Markdown after the link rule: its visible text and
    /// the destination of every run that is still a link.
    static func rendered(_ markdown: String) -> (text: String, links: [URL]) {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard let parsed = try? AttributedString(markdown: markdown, options: opts) else {
            check(false, "inline Markdown must parse: \(markdown)")
            return ("", [])
        }
        let safe = MarkdownLinks.safe(parsed)
        return (String(safe.characters), safe.runs.compactMap { $0.link })
    }

    /// A link built by hand, so the rule is checked whatever the parser makes of a scheme.
    static func built(_ label: String, _ destination: String) -> AttributedString {
        var text = AttributedString(label)
        text.link = URL(string: destination)
        return text
    }

    // An answer can repeat text Wisp read, and a click shows only the label: a link
    // may open a web page and nothing else, and never one its own label disowns.
    static func linkChecks() {
        // Web links keep working, with their label.
        let plain = rendered("See [x](https://example.com) for more.")
        check(plain.text == "See x for more." && plain.links.count == 1 && plain.links[0].host == "example.com",
              "an https link stays a link: \(plain)")
        let maps = rendered("[Open in Maps](https://maps.apple.com/?saddr=1,2)")
        check(maps.text == "Open in Maps" && !maps.links.isEmpty && maps.links.allSatisfy { $0.host == "maps.apple.com" },
              "the Maps directions link stays a link: \(maps)")
        let source = rendered("[1] [Quarterly results: what changed](http://news.example.org/a?b=1) — news.example.org")
        check(!source.links.isEmpty && source.links.allSatisfy { $0.scheme == "http" && $0.host == "news.example.org" }
                && source.text.contains("Quarterly results: what changed"),
              "a research source keeps its link: \(source)")
        let bold = rendered("[**Read** the guide](https://example.com/guide)")
        check(bold.text == "Read the guide" && !bold.links.isEmpty && bold.links.allSatisfy { $0.host == "example.com" },
              "a link with bold inside its label stays whole: \(bold)")

        // Anything that is not a web page loses its link and keeps its label.
        for (markdown, label) in [
            ("[Open the invoice](shortcuts://run-shortcut?name=a)", "Open the invoice"),
            ("[a](file:///tmp/x.command)", "a"),
            ("[a](tel:123)", "a"),
            ("[a](smb://h/s)", "a"),
            ("[a](mailto:someone@example.com)", "a"),
        ] {
            let out = rendered(markdown)
            check(out.links.isEmpty && out.text == label, "\(markdown) must be plain text: \(out)")
        }
        let autolink = rendered("Call <facetime:x@y.z> now")
        check(autolink.links.isEmpty && autolink.text.contains("facetime:x@y.z"), "an autolink is held to the same rule: \(autolink)")
        for destination in ["shortcuts://run-shortcut?name=a", "file:///tmp/x.command", "tel:123", "smb://h/s",
                            "mailto:someone@example.com", "facetime:x@y.z", "x-apple.systempreferences:com.apple.preference",
                            "ftp://files.example.com/a", "https:relative"] {
            let out = MarkdownLinks.safe(built("Open the invoice", destination))
            check(out.runs.allSatisfy { $0.link == nil } && String(out.characters) == "Open the invoice",
                  "a hand-built \(destination) link must be plain text")
        }
        let kept = MarkdownLinks.safe(built("Open the page", "HTTPS://Example.com/a"))
        check(kept.runs.contains { $0.link != nil }, "the scheme is matched whatever its case")
        check(MarkdownLinks.mayOpen(URL(string: "https://example.com/a")!) && MarkdownLinks.mayOpen(URL(string: "http://example.com")!),
              "http and https open")
        check(!MarkdownLinks.mayOpen(URL(string: "file:///tmp/x")!) && !MarkdownLinks.mayOpen(URL(string: "tel:123")!)
                && URL(string: "https:///nohost").map(MarkdownLinks.mayOpen) != true, "nothing else opens, nor a link with no host")

        // One refused link never costs its neighbours theirs, or any text.
        let mixed = rendered("Read [the guide](https://example.com/guide) or [run it](shortcuts://run-shortcut?name=a) now.")
        check(mixed.text == "Read the guide or run it now." && mixed.links.count == 1 && mixed.links[0].host == "example.com",
              "only the refused link is taken off: \(mixed)")

        // A label that reads as one site's address while the link opens another.
        let spoof = rendered("[https://accounts.google.com](https://evil.example/login)")
        check(spoof.links.allSatisfy { $0.host != "evil.example" } && spoof.text.contains("https://accounts.google.com"),
              "a label naming one site never opens another: \(spoof)")
        for (label, destination) in [
            ("accounts.example.com", "https://evil.example/login"),
            ("www.example.com", "https://evil.example/"),
            ("Sign in at https://accounts.example.com today", "https://evil.example/"),
            ("example.com", "https://example.com.evil.example/"),
            ("https://example.com@evil.example/", "https://evil.example/"),
        ] {
            let out = MarkdownLinks.safe(built(label, destination))
            check(out.runs.allSatisfy { $0.link == nil } && String(out.characters) == label,
                  "\(label) must not link to \(destination)")
        }
        let split = rendered("[accounts.**example**.com](https://evil.example/login)")
        check(split.links.isEmpty && split.text.contains("example"), "bold inside the label does not hide the mismatch: \(split)")
        // The same site, a subdomain of it, a file name and ordinary words are all fine.
        for (label, destination) in [
            ("https://example.com/a", "https://example.com/b"),
            ("www.example.com", "https://example.com/a"),
            ("Example.com", "https://www.example.com/"),
            ("example.com", "https://maps.example.com/x"),
            ("http://localhost:3000", "http://localhost:3000/status"),
            ("README.md", "https://code.example.com/acme/tool/README.md"),
            ("Example.com raises prices", "https://news.example.org/story"),
            ("Write to someone@example.com", "https://help.example.org/contact"),
            ("Version 2.1 notes, e.g. the fix", "https://example.org/notes"),
        ] {
            let out = MarkdownLinks.safe(built(label, destination))
            check(out.runs.contains { $0.link != nil } && String(out.characters) == label,
                  "\(label) may link to \(destination)")
        }
    }

    /// A streaming answer hides only a bold marker still waiting for its pair.
    static func streamingBoldChecks() {
        let cases: [(String, String, String)] = [
            ("is **bold** here", "is **bold** here", "balanced markers stay"),
            ("is **Worksheet 3 for", "is Worksheet 3 for", "an open marker hides until its pair arrives"),
            ("is **", "is ", "a marker at the very end hides"),
            ("**a** and **b", "**a** and b", "only the unpaired marker hides"),
            ("```py\nf(**kw", "```py\nf(**kw", "a code fence is left alone"),
            ("call `f(**kw)` and **bold", "call `f(**kw)` and bold", "markers in inline code don't count"),
            ("call `f(**kw", "call `f(**kw", "an open inline code span keeps its asterisks"),
            ("2 ** 3 = 8", "2 ** 3 = 8", "asterisks between spaces stay"),
            ("2 ** 3 is **8", "2 ** 3 is 8", "a literal ** doesn't pair with a real marker"),
            ("a * b and *c", "a * b and *c", "single asterisks are never touched"),
            ("done** later", "done** later", "a marker with a space after it opens nothing"),
            ("", "", "empty text"),
        ]
        for (input, expected, what) in cases {
            let got = StreamingMarkdown.withoutOpenBold(input)
            check(got == expected, "streaming bold: \(what): \(input.debugDescription) -> \(got.debugDescription)")
        }
    }

    static func main() {
        linkChecks()
        mathChecks()
        solitaryPipeAndOrderingChecks()
        tableValidationChecks()
        growingPrefixChecks()
        existingMarkdownChecks()
        streamingBoldChecks()
        print("Markdown contract passed (\(assertions) assertions)")
    }
}
