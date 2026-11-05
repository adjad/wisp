import Foundation

// Makes math in a reply readable without a typesetter or a network: `$…$`, `$$…$$`,
// `\(…\)` and `\[…\]` become Unicode (ŷ, β₀, Σ, √, ≤, a/b), display math on its own
// line, and a bare caret exponent (x^2, 3^-2, x^{12}, a^n) becomes a superscript.
// Fenced code and inline code are never touched, and a dollar sign is only a
// delimiter when it can't be a price (see MathSpans.dollarSpan).
private let superMap: [Character: Character] = [
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
    "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻", "+": "⁺",
    "=": "⁼", "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ", "h": "ʰ",
    "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ",
    "t": "ᵗ", "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
    "T": "ᵀ", "α": "ᵅ", "β": "ᵝ", "γ": "ᵞ", "δ": "ᵟ", "θ": "ᶿ", "φ": "ᵠ", "χ": "ᵡ",
    "′": "′", "∘": "°", "⊤": "ᵀ", "*": "*", "†": "†",
]

private let subMap: [Character: Character] = [
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅",
    "6": "₆", "7": "₇", "8": "₈", "9": "₉", "-": "₋", "+": "₊",
    "=": "₌", "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ",
    "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ", "β": "ᵦ", "γ": "ᵧ", "ρ": "ᵨ", "φ": "ᵩ", "χ": "ᵪ",
]

private let caretRegex = try? NSRegularExpression(pattern: "\\^\\{?([0-9ni+\\-]+)\\}?")

private func superscriptify(_ s: String) -> String {
    guard s.contains("^"), let re = caretRegex else { return s }
    let ns = s as NSString
    var result = ""
    var last = 0
    re.enumerateMatches(in: s, range: NSRange(location: 0, length: ns.length)) { m, _, _ in
        guard let m = m else { return }
        result += ns.substring(with: NSRange(location: last, length: m.range.location - last))
        let exp = ns.substring(with: m.range(at: 1))
        result += String(exp.map { superMap[$0] ?? $0 })
        last = m.range.location + m.range.length
    }
    result += ns.substring(from: last)
    return result
}

/// Readable math for display, or for read-aloud with `forSpeech` (which leaves out
/// the Markdown escapes the display text needs). The source text stays LaTeX:
/// only the view of it changes, so history and exports keep what the model wrote.
func prettifyMath(_ s: String, forSpeech: Bool = false) -> String {
    // Split on ``` fences; transform only the even (non-code) segments.
    let segments = s.components(separatedBy: "```")
    return segments.enumerated()
        .map { i, seg in i % 2 == 0 ? MathSpans.render(seg, escapingMarkdown: !forSpeech) : seg }
        .joined(separator: "```")
}

/// Finds math spans in prose. A reply re-renders on every streamed token, so an
/// opener with no closer yet is shown converted when it can only be math, and
/// left as written (and revisited on the next token) when it could be a price.
private enum MathSpans {
    private struct Span {
        let content: String
        let display: Bool
        let end: Int
        let closed: Bool
    }

    static func render(_ text: String, escapingMarkdown: Bool) -> String {
        guard text.contains("$") || text.contains("\\") || text.contains("`") else {
            return superscriptify(text)
        }
        let c = Array(text)
        var i = 0
        var out = ""
        var literal = ""
        func flush() {
            out += superscriptify(literal)
            literal = ""
        }
        while i < c.count {
            let ch = c[i]
            if ch == "`" {
                var run = 0
                while i + run < c.count, c[i + run] == "`" { run += 1 }
                // Unclosed (still streaming): the rest of the line stays as written.
                let end = closingBackticks(c, from: i + run, run: run) ?? lineEnd(c, from: i + run)
                flush()
                out += String(c[i..<end])
                i = end
                continue
            }
            var span: Span?
            if ch == "\\", i + 1 < c.count {
                span = bracketSpan(c, at: i)
                if span == nil {
                    // An escape such as \$ is not a delimiter; Markdown resolves it.
                    literal.append(ch)
                    literal.append(c[i + 1])
                    i += 2
                    continue
                }
            } else if ch == "$" {
                span = dollarSpan(c, at: i)
            }
            guard let span else {
                literal.append(ch)
                i += 1
                continue
            }
            flush()
            var converted = LaTeXText.unicode(span.content, partial: !span.closed)
            if escapingMarkdown { converted = escaped(converted) }
            if span.display {
                while out.last == " " { out.removeLast() }
                if !opensLine(out) { out += "\n" }
                out += converted
                i = span.end
                while i < c.count, c[i] == " " { i += 1 }
                if i < c.count, !c[i].isNewline, !".,;:".contains(c[i]) { out += "\n" }
            } else {
                out += converted
                i = span.end
            }
        }
        flush()
        return out
    }

    /// `$$…$$` or `$…$` at `i`, or nil when this dollar is just a dollar. Inline math
    /// follows Pandoc's rule, which is what keeps "$5 and $10" and "$5-$10" prose: the
    /// opener is followed by non-space, the closer preceded by non-space and not
    /// followed by a digit, and nothing else on the line can pair up in between.
    private static func dollarSpan(_ c: [Character], at i: Int) -> Span? {
        guard i + 1 < c.count else { return nil }
        if c[i + 1] == "$" {
            var j = i + 2
            while j + 1 < c.count, !isBlankLine(c, at: j) {
                if c[j] == "\\" { j += 2; continue }
                if c[j] == "$", c[j + 1] == "$" {
                    let content = String(c[(i + 2)..<j])
                    return looksLikeMath(content) ? Span(content: content, display: true, end: j + 2, closed: true) : nil
                }
                j += 1
            }
            let end = blockEnd(c, from: i + 2)
            // A closer that has only half arrived must not flash a stray dollar.
            let tail = String(c[(i + 2)..<end]).replacingOccurrences(of: "$", with: "")
            return hasMathSign(tail) ? Span(content: tail, display: true, end: end, closed: false) : nil
        }
        let next = c[i + 1]
        if next.isWhitespace { return nil }
        if i > 0, c[i - 1].isLetter || c[i - 1].isNumber { return nil }  // US$5
        var j = i + 1
        while j < c.count, !c[j].isNewline {
            if c[j] == "\\" { j += 2; continue }
            if c[j] == "$" {
                if c[j - 1].isWhitespace { return nil }
                if j + 1 < c.count, c[j + 1].isLetter || c[j + 1].isNumber { return nil }
                let content = String(c[(i + 1)..<j])
                return looksLikeMath(content) ? Span(content: content, display: false, end: j + 1, closed: true) : nil
            }
            j += 1
        }
        // No closer on this line. A backslash command can't be a price.
        guard next == "\\", i + 2 < c.count, c[i + 2].isLetter else { return nil }
        let end = min(j, c.count)
        return Span(content: String(c[(i + 1)..<end]), display: false, end: end, closed: false)
    }

    /// `\(…\)` or `\[…\]` at `i`. `\[` is also how Markdown writes a literal bracket,
    /// so "\[1\]" and "\[link\](url)" stay prose.
    private static func bracketSpan(_ c: [Character], at i: Int) -> Span? {
        let open = c[i + 1]
        guard open == "(" || open == "[" else { return nil }
        let display = open == "["
        let close: Character = display ? "]" : ")"
        var j = i + 2
        while j + 1 < c.count, !isBlankLine(c, at: j) {
            if c[j] == "\\" {
                if c[j + 1] == close {
                    let content = String(c[(i + 2)..<j])
                    let isLink = j + 2 < c.count && c[j + 2] == "("
                    if display, isLink || !hasMathSign(content) { return nil }
                    return Span(content: content, display: display, end: j + 2, closed: true)
                }
                j += 2
                continue
            }
            j += 1
        }
        let end = display ? blockEnd(c, from: i + 2) : lineEnd(c, from: i + 2)
        let tail = String(c[(i + 2)..<end])
        return hasMathSign(tail) ? Span(content: tail, display: display, end: end, closed: false) : nil
    }

    private static func hasMathSign(_ s: String) -> Bool {
        s.contains { "\\^_=<>+-".contains($0) }
    }

    /// Whether what sits between two dollars reads as math rather than prose
    /// that happens to have two dollar signs in it ("$100 for the US$").
    private static func looksLikeMath(_ content: String) -> Bool {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = trimmed.first, !trimmed.contains("$") else { return false }
        if first.isNumber, !trimmed.contains("\\"), hasWord(trimmed) { return false }
        if hasMathSign(trimmed) { return true }
        return trimmed == content && !trimmed.contains { $0.isWhitespace }
    }

    private static func hasWord(_ s: String) -> Bool {
        var run = 0
        for ch in s {
            run = ch.isLetter ? run + 1 : 0
            if run >= 2 { return true }
        }
        return false
    }

    private static func closingBackticks(_ c: [Character], from start: Int, run: Int) -> Int? {
        var j = start
        while j < c.count {
            guard c[j] == "`" else { j += 1; continue }
            var n = 0
            while j + n < c.count, c[j + n] == "`" { n += 1 }
            if n == run { return j + n }
            j += n
        }
        return nil
    }

    private static func lineEnd(_ c: [Character], from start: Int) -> Int {
        var j = start
        while j < c.count, !c[j].isNewline { j += 1 }
        return j
    }

    private static func isBlankLine(_ c: [Character], at j: Int) -> Bool {
        guard c[j].isNewline else { return false }
        var k = j + 1
        while k < c.count, c[k] == " " || c[k] == "\t" { k += 1 }
        return k < c.count && c[k].isNewline
    }

    private static func blockEnd(_ c: [Character], from start: Int) -> Int {
        var j = start
        while j < c.count, !isBlankLine(c, at: j) { j += 1 }
        return j
    }

    /// Nothing but a list marker (or nothing) so far on the line being written.
    private static func opensLine(_ out: String) -> Bool {
        let line = (out.split(separator: "\n", omittingEmptySubsequences: false).last ?? "")
            .trimmingCharacters(in: .whitespaces)
        if line.isEmpty || ["-", "*", "•", "+"].contains(line) { return true }
        return line.hasSuffix(".") && line.dropLast().allSatisfy(\.isNumber)
    }

    /// The renderer parses inline Markdown afterwards; a converted formula's `*`, `_`
    /// and brackets are math, not emphasis or a link.
    private static func escaped(_ s: String) -> String {
        var out = ""
        for ch in s {
            if "*_[]<`~".contains(ch) { out.append("\\") }
            out.append(ch)
        }
        return out
    }
}

/// A small, forgiving LaTeX-to-Unicode converter for what models actually write.
/// Never fails: an unknown command keeps its name, an unclosed group ends at the
/// end of the input, and anything Unicode has no glyph for falls back to plain
/// notation (x_(ab), (a+b)/c).
private struct LaTeXText {
    private let c: [Character]
    private let partial: Bool
    private var i = 0
    private var depth = 0
    /// Far past any real formula. A model stuck repeating `\frac{` can nest thousands of
    /// levels, and each one is stack on the main thread; past this, the rest reads flat.
    private static let maxDepth = 64

    static func unicode(_ source: String, partial: Bool) -> String {
        var parser = LaTeXText(c: Array(source), partial: partial)
        let raw = parser.sequence(inGroup: false)
        let lines = raw.split(separator: "\n", omittingEmptySubsequences: true).map { line -> String in
            line.split(whereSeparator: { $0 == " " || $0 == "\t" }).joined(separator: " ")
        }.filter { !$0.isEmpty }
        return lines.joined(separator: "\n").precomposedStringWithCanonicalMapping
    }

    private init(c: [Character], partial: Bool) {
        self.c = c
        self.partial = partial
    }

    private mutating func sequence(inGroup: Bool) -> String {
        var out = ""
        while i < c.count {
            let ch = c[i]
            switch ch {
            case "}":
                i += 1
                if inGroup { return out }
            case "{":
                i += 1
                guard depth < LaTeXText.maxDepth else { continue }
                depth += 1
                out += sequence(inGroup: true)
                depth -= 1
            case "\\":
                out += command(after: out)
            case "^":
                i += 1
                out += LaTeXText.script(argument(), map: superMap, mark: "^")
            case "_":
                i += 1
                out += LaTeXText.script(argument(), map: subMap, mark: "_")
            case "&", "~":
                i += 1
                out += " "
            case "'":
                i += 1
                out += "′"
            default:
                i += 1
                out += ch.isNewline ? " " : String(ch)
            }
        }
        return out
    }

    /// A command's or script's argument: a {group}, a command, or one character.
    private mutating func argument() -> String {
        while i < c.count, c[i].isWhitespace { i += 1 }
        guard i < c.count else { return "" }
        guard depth < LaTeXText.maxDepth else {
            i += 1
            return String(c[i - 1])
        }
        depth += 1
        defer { depth -= 1 }
        switch c[i] {
        case "{":
            i += 1
            return sequence(inGroup: true)
        case "\\":
            return command(after: "")
        default:
            i += 1
            return String(c[i - 1])
        }
    }

    private mutating func command(after preceding: String) -> String {
        i += 1  // the backslash
        guard i < c.count else { return "" }
        guard c[i].isLetter else {
            let symbol = c[i]
            i += 1
            switch symbol {
            case "\\": return "\n"
            case ",", ";", ":", " ": return " "
            case "!": return ""
            case "|": return "‖"
            default: return String(symbol)  // \{ \} \% \$ \# \_ \&
            }
        }
        let start = i
        while i < c.count, c[i].isLetter { i += 1 }
        let name = String(c[start..<i])
        let atEnd = i >= c.count

        if let symbol = LaTeXText.symbols[name] { return symbol }
        if let accent = LaTeXText.accents[name] {
            return argument().map { $0.isWhitespace ? String($0) : String($0) + accent }.joined()
        }
        switch name {
        case "frac", "dfrac", "tfrac", "cfrac":
            let top = argument(), bottom = argument()
            let lead = preceding.last.map { $0.isLetter || $0.isNumber } == true ? " " : ""
            let trail = i < c.count && (c[i].isLetter || c[i].isNumber || c[i] == "\\") ? " " : ""
            return lead + LaTeXText.atom(top) + "/" + LaTeXText.atom(bottom) + trail
        case "sqrt":
            var index = ""
            if i < c.count, c[i] == "[" {
                let close = c[i...].firstIndex(of: "]") ?? c.count
                index = String(c[(i + 1)..<close])
                i = min(close + 1, c.count)
            }
            let root = ["3": "∛", "4": "∜"][index]
                ?? String(index.map { superMap[$0] ?? $0 }) + "√"
            return root + LaTeXText.atom(argument())
        case "binom":
            let n = argument(), k = argument()
            return "C(\(n), \(k))"
        case "mathbb":
            let inner = argument()
            return ["R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "E": "𝔼", "P": "ℙ"][inner] ?? inner
        case "text", "mbox", "textrm", "textbf", "textit", "mathrm", "mathbf", "mathit", "mathsf",
             "mathtt", "mathcal", "mathfrak", "boldsymbol", "bm", "operatorname", "boxed", "cancel",
             "underbrace", "overbrace":
            return argument()
        case "tag":
            return "  (" + argument() + ")"
        case "pmod":
            return " (mod " + argument() + ")"
        case "begin", "end", "label":
            _ = argument()
            return ""
        case "left", "right", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr",
             "big", "Big", "bigg", "Bigg":
            if i < c.count, c[i] == "." { i += 1 }
            return ""
        case "displaystyle", "textstyle", "scriptstyle", "limits", "nolimits", "nonumber", "notag",
             "centering", "quad", "qquad":
            return name.hasSuffix("quad") ? "  " : ""
        default:
            // A command still arriving ("\fra") shows nothing rather than a stray fragment.
            return partial && atEnd ? "" : name
        }
    }

    /// Script characters when every one has a glyph, else plain notation.
    private static func script(_ arg: String, map: [Character: Character], mark: String) -> String {
        let body = arg.filter { !$0.isWhitespace }
        if body.isEmpty { return "" }
        let mapped = body.compactMap { map[$0] }
        if mapped.count == body.count { return String(mapped) }
        return body.count == 1 ? mark + body : mark + "(" + body + ")"
    }

    /// A fraction part or radicand, in parentheses unless it is one unbroken token.
    private static func atom(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespaces)
        return t.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "." }) ? t : "(" + t + ")"
    }

    private static let accents: [String: String] = [
        "hat": "\u{0302}", "widehat": "\u{0302}", "bar": "\u{0304}", "overline": "\u{0304}",
        "underline": "\u{0332}", "vec": "\u{20D7}", "dot": "\u{0307}", "ddot": "\u{0308}",
        "tilde": "\u{0303}", "widetilde": "\u{0303}", "check": "\u{030C}",
    ]

    private static let symbols: [String: String] = [
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
        "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
        "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
        "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
        "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
        "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
        "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
        "sum": "Σ", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭", "oint": "∮",
        "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "ast": "∗", "star": "⋆",
        "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗", "dagger": "†",
        "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
        "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡", "propto": "∝", "ll": "≪", "gg": "≫",
        "infty": "∞", "partial": "∂", "nabla": "∇", "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ",
        "aleph": "ℵ", "prime": "′", "degree": "°", "angle": "∠", "perp": "⊥", "parallel": "∥",
        "top": "⊤", "bot": "⊥", "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
        "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "∖", "emptyset": "∅",
        "varnothing": "∅", "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "lnot": "¬",
        "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "to": "→", "rightarrow": "→",
        "leftarrow": "←", "gets": "←", "leftrightarrow": "↔", "Rightarrow": "⇒", "implies": "⇒",
        "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "mapsto": "↦", "uparrow": "↑",
        "downarrow": "↓", "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
        "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
        "lvert": "|", "rvert": "|", "vert": "|", "mid": "|", "lVert": "‖", "rVert": "‖", "Vert": "‖",
        "lbrace": "{", "rbrace": "}", "backslash": "\\", "therefore": "∴", "because": "∵",
        "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "sec": "sec", "csc": "csc",
        "arcsin": "arcsin", "arccos": "arccos", "arctan": "arctan", "sinh": "sinh", "cosh": "cosh",
        "tanh": "tanh", "log": "log", "ln": "ln", "lg": "lg", "exp": "exp", "lim": "lim", "max": "max",
        "min": "min", "sup": "sup", "inf": "inf", "det": "det", "dim": "dim", "gcd": "gcd", "arg": "arg",
        "Pr": "Pr", "mod": "mod", "bmod": "mod",
    ]
}
