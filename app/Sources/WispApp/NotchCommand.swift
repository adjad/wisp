import Foundation

/// The notch's slash commands. Typed alone in the Ask field, they open a part of
/// Wisp instead of sending a prompt. Only "/" followed by letters, with nothing
/// else typed, can be a command, so a prompt that merely starts with "/" (a file
/// path such as "/Users/me/notes.txt") is still sent as a prompt.
///
/// Pure logic, checked by tests/NotchCommandChecks.swift without the app.
enum NotchCommand: String, CaseIterable, Equatable {
    case today, memory, research, settings, report, window, new, about, corpus, demo

    var title: String { "/" + rawValue }

    var summary: String {
        switch self {
        case .corpus: return "Browse, run or export assistant test cases"
        case .today: return "Open your day"
        case .memory: return "Open Memory"
        case .about: return "Tell Wisp about yourself"
        case .research: return "Research your next prompt"
        case .settings: return "Quick settings"
        case .report: return "Report a problem with the last reply"
        case .window: return "Continue in the Wisp window"
        case .new: return "Start a new chat"
        case .demo: return "Start or end a fresh-install demo"
        }
    }

    /// The commands this input could still become. Empty unless the input looks
    /// like a command, so the list only appears while typing one.
    static func suggestions(for input: String) -> [NotchCommand] {
        guard let word = commandWord(input) else { return [] }
        return allCases.filter { $0.rawValue.hasPrefix(word) }
    }

    /// The command to run for this input: an exact command, or a prefix only one
    /// command starts with. Nil means it is not a whole command.
    static func match(_ input: String) -> NotchCommand? {
        guard let word = commandWord(input), !word.isEmpty else { return nil }
        if let exact = NotchCommand(rawValue: word) { return exact }
        let candidates = allCases.filter { $0.rawValue.hasPrefix(word) }
        return candidates.count == 1 ? candidates[0] : nil
    }

    /// Corpus selectors contain spaces and IDs; recognize only the whole
    /// command word so a file path or ordinary prompt never enters the runner.
    static func isCorpusRequest(_ input: String) -> Bool {
        input.split(whereSeparator: \.isWhitespace).first?.lowercased() == "/corpus"
    }

    /// The lowercased letters after a leading "/", when that is all the input is.
    private static func commandWord(_ input: String) -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("/") else { return nil }
        let word = trimmed.dropFirst()
        guard word.allSatisfy({ $0.isASCII && $0.isLetter }) else { return nil }
        return word.lowercased()
    }
}
