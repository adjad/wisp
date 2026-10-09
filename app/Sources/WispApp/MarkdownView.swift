import SwiftUI

/// The colors and body size MarkdownView draws with. The default is the notch
/// panel's monochrome look; other surfaces (the Chat window) set their own
/// through the environment instead of forking the renderer.
struct MarkdownPalette {
    var text: Color
    var secondary: Color
    var muted: Color
    var chipFill: Color
    var chipStroke: Color
    var hairline: Color
    var codeFill: Color
    var tableFill: Color
    var bodySize: CGFloat = 14

    static let notch = MarkdownPalette(
        text: Theme.textPrimary, secondary: Theme.textSecondary, muted: Theme.textMuted,
        chipFill: Theme.chipFill, chipStroke: Theme.chipStroke, hairline: Theme.hairline,
        codeFill: Color.white.opacity(0.06), tableFill: Color.white.opacity(0.04))
}

private struct MarkdownPaletteKey: EnvironmentKey {
    static let defaultValue = MarkdownPalette.notch
}

extension EnvironmentValues {
    var markdownPalette: MarkdownPalette {
        get { self[MarkdownPaletteKey.self] }
        set { self[MarkdownPaletteKey.self] = newValue }
    }
}

// Markdown renderer with real block-level parsing: fenced code blocks become
// monospaced cards, GFM tables render as an actual grid, lists get proper
// bullets/numbers, and prose renders inline markdown (bold, italic, inline
// code, links). `<br>` tags (common in LLM-generated table cells) become real
// line breaks instead of literal text.
struct MarkdownView: View {
    let text: String
    @Environment(\.markdownPalette) private var palette

    private enum Kind { case code, table, list, paragraph }

    // NOT Identifiable via UUID on purpose: during streaming, `text` grows on
    // every token and `blocks()` re-parses the WHOLE thing from scratch each
    // time. A fresh UUID() per block per parse meant SwiftUI could never
    // recognize "the paragraph I'm already showing" across renders — it saw
    // an entirely new set of blocks every token and tore down/rebuilt the
    // whole tree, which is what made streaming look flickery/janky instead of
    // text just growing in place. The ForEach below keys on array position
    // instead (blocks only ever get APPENDED to as text streams in, so a
    // block's position is stable even as its own content keeps growing) —
    // that lets SwiftUI diff a block's Text content in place like any normal
    // growing string, the same trick already used for table rows/list items
    // below (`id: \.offset`).
    private struct Block {
        let kind: Kind
        let lang: String = ""
        var body: String = ""          // code / paragraph
        var tableRows: [[String]] = [] // table: first row is the header
        var listItems: [(marker: String, text: String)] = []
        var codeLang: String = ""
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks().enumerated()), id: \.offset) { _, b in
                switch b.kind {
                case .code:
                    codeCard(b)
                case .table:
                    tableGrid(b.tableRows)
                case .list:
                    listView(b.listItems)
                case .paragraph:
                    Text(inline(b.body))
                        .font(.system(size: palette.bodySize))
                        .foregroundStyle(palette.text)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    // MARK: - Block rendering

    private func codeCard(_ b: Block) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                if !b.codeLang.isEmpty {
                    Text(b.codeLang).font(.system(size: 10, weight: .medium))
                        .foregroundStyle(palette.muted)
                }
                Spacer(minLength: 8)
                codeActionButton(icon: "doc.on.doc", label: "Copy") {
                    ScriptRunner.copy(b.body)
                }
                // Untagged blocks (no ```lang```) land on Pages as plain
                // content; most languages with no sensible single-file "run"
                // (json/css/swift/…) get no run button at all — just Copy.
                if let target = ScriptRunner.runTarget(forLang: b.codeLang) {
                    codeActionButton(icon: target.icon, label: target.label) {
                        ScriptRunner.perform(target, code: b.body)
                    }
                }
            }
            Text(b.body)
                .font(.system(size: 12.5, design: .monospaced))
                .foregroundStyle(palette.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(palette.codeFill))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(palette.chipStroke, lineWidth: 1))
    }

    private func codeActionButton(icon: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: icon).font(.system(size: 9))
                Text(label).font(.system(size: 10, weight: .medium))
            }
            .foregroundStyle(palette.secondary)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(Capsule().fill(palette.chipFill))
            .overlay(Capsule().stroke(palette.chipStroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .help(label)
    }

    // A real grid instead of literal "| a | b |" text — the header row is bold
    // with a bottom rule; every column left-aligns and wraps rather than
    // overflowing the panel width.
    private func tableGrid(_ rows: [[String]]) -> some View {
        let colCount = rows.map(\.count).max() ?? 0
        return Grid(alignment: .topLeading, horizontalSpacing: 14, verticalSpacing: 7) {
            ForEach(Array(rows.enumerated()), id: \.offset) { rowIdx, row in
                GridRow {
                    ForEach(0..<colCount, id: \.self) { col in
                        let cell = col < row.count ? row[col] : ""
                        Text(inline(cell))
                            .font(.system(size: 13, weight: rowIdx == 0 ? .semibold : .regular))
                            .foregroundStyle(rowIdx == 0 ? palette.text : palette.text.opacity(0.9))
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                if rowIdx == 0 {
                    Divider().overlay(palette.hairline).gridCellColumns(colCount)
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(palette.tableFill))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(palette.chipStroke, lineWidth: 1))
    }

    private func listView(_ items: [(marker: String, text: String)]) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top, spacing: 7) {
                    Text(item.marker)
                        .font(.system(size: 13)).foregroundStyle(palette.muted)
                        .frame(minWidth: 14, alignment: .trailing)
                    Text(inline(item.text))
                        .font(.system(size: palette.bodySize))
                        .foregroundStyle(palette.text)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    // MARK: - Parsing

    private static let tableRowRE = try! NSRegularExpression(pattern: #"^\s*\|.*\|\s*$"#)
    private static let tableSepRE = try! NSRegularExpression(pattern: #"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$"#)
    private static let bulletRE = try! NSRegularExpression(pattern: #"^\s*[-*•]\s+(.*)$"#)
    private static let numberedRE = try! NSRegularExpression(pattern: #"^\s*(\d+)[.)]\s+(.*)$"#)

    private func matches(_ re: NSRegularExpression, _ s: String) -> Bool {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    private func blocks() -> [Block] {
        var out: [Block] = []
        let segs = text.components(separatedBy: "```")
        for (i, seg) in segs.enumerated() {
            if i % 2 == 1 {
                var lang = ""
                var code = seg
                if let nl = code.firstIndex(of: "\n") {
                    let first = code[code.startIndex..<nl].trimmingCharacters(in: .whitespaces)
                    if !first.contains(" ") && first.count < 16 {
                        lang = first
                        code = String(code[code.index(after: nl)...])
                    }
                }
                var b = Block(kind: .code)
                b.body = code.trimmingCharacters(in: .newlines)
                b.codeLang = lang
                out.append(b)
            } else {
                out.append(contentsOf: parseProse(seg))
            }
        }
        return out
    }

    // Splits a non-code segment into table / list / paragraph blocks by
    // scanning line-by-line and grouping consecutive lines of the same kind.
    private func parseProse(_ seg: String) -> [Block] {
        let lines = seg.components(separatedBy: "\n")
        var out: [Block] = []
        var para: [String] = []
        var tableLines: [String] = []
        var listItems: [(String, String)] = []

        func flushPara() {
            let joined = para.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty {
                var b = Block(kind: .paragraph)
                b.body = prettifyMath(deHTML(joined))
                out.append(b)
            }
            para.removeAll()
        }
        func flushTable() {
            guard tableLines.count >= 2 else { tableLines.removeAll(); return }
            let rows = tableLines.enumerated().compactMap { idx, line -> [String]? in
                if idx == 1 && matches(Self.tableSepRE, line) { return nil }  // drop the |---|---| rule
                var trimmed = line.trimmingCharacters(in: .whitespaces)
                if trimmed.hasPrefix("|") { trimmed.removeFirst() }
                if trimmed.hasSuffix("|") { trimmed.removeLast() }
                return trimmed.components(separatedBy: "|").map { deHTML($0.trimmingCharacters(in: .whitespaces)) }
            }
            if rows.count >= 1 {
                var b = Block(kind: .table)
                b.tableRows = rows
                out.append(b)
            }
            tableLines.removeAll()
        }
        func flushList() {
            guard !listItems.isEmpty else { return }
            var b = Block(kind: .list)
            b.listItems = listItems.map { (marker: $0.0, text: deHTML($0.1)) }
            out.append(b)
            listItems.removeAll()
        }

        for line in lines {
            if matches(Self.tableRowRE, line) {
                flushPara(); flushList()
                tableLines.append(line)
                continue
            } else if !tableLines.isEmpty {
                flushTable()
            }

            if let m = firstMatch(Self.bulletRE, line) {
                flushPara()
                listItems.append(("•", m))
                continue
            }
            if let m = firstMatchPair(Self.numberedRE, line) {
                flushPara()
                listItems.append(("\(m.0).", m.1))
                continue
            }
            if !listItems.isEmpty { flushList() }

            para.append(line)
        }
        flushTable(); flushList(); flushPara()
        return out
    }

    private func firstMatch(_ re: NSRegularExpression, _ s: String) -> String? {
        guard let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              let r = Range(m.range(at: 1), in: s) else { return nil }
        return String(s[r])
    }

    private func firstMatchPair(_ re: NSRegularExpression, _ s: String) -> (String, String)? {
        guard let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)),
              let r1 = Range(m.range(at: 1), in: s), let r2 = Range(m.range(at: 2), in: s) else { return nil }
        return (String(s[r1]), String(s[r2]))
    }

    // LLM output (esp. inside table cells) sometimes uses HTML line breaks —
    // turn them into real newlines so SwiftUI Text wraps them instead of
    // showing "<br>" as literal characters.
    private func deHTML(_ s: String) -> String {
        s.replacingOccurrences(of: "<br/>", with: "\n", options: .caseInsensitive)
         .replacingOccurrences(of: "<br>", with: "\n", options: .caseInsensitive)
    }

    private func inline(_ s: String) -> AttributedString {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: s, options: opts)) ?? AttributedString(s)
    }
}
