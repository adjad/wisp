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
        let reply = "Intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- item"
        let view = MarkdownView(text: reply)
        check(kindNames(view.cachedBlocks()) == kindNames(view.blocks()), "cached blocks match a fresh parse")
        check(kindNames(view.cachedBlocks()) == kindNames(view.blocks()), "a cache hit matches a fresh parse")
        let grown = MarkdownView(text: reply + "\n\n```\ncode\n```")
        check(kindNames(grown.cachedBlocks()) == kindNames(grown.blocks()), "grown text is parsed, not served stale")
    }

    static func main() {
        solitaryPipeAndOrderingChecks()
        tableValidationChecks()
        growingPrefixChecks()
        existingMarkdownChecks()
        print("Markdown contract passed (\(assertions) assertions)")
    }
}
