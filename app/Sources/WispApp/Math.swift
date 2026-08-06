import Foundation

// Turn caret exponents (x^2, 3^-2, x^{12}, a^n) into Unicode superscripts (x², 3⁻², x¹², aⁿ)
// for legibility. Leaves fenced code blocks untouched.
private let superMap: [Character: Character] = [
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
    "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻", "+": "⁺",
    "n": "ⁿ", "i": "ⁱ",
]

private let caretRegex = try? NSRegularExpression(pattern: "\\^\\{?([0-9ni+\\-]+)\\}?")

private func superscriptify(_ s: String) -> String {
    guard let re = caretRegex else { return s }
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

func prettifyMath(_ s: String) -> String {
    // Split on ``` fences; transform only the even (non-code) segments.
    let segments = s.components(separatedBy: "```")
    return segments.enumerated()
        .map { i, seg in i % 2 == 0 ? superscriptify(seg) : seg }
        .joined(separator: "```")
}
