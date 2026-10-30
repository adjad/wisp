import Foundation

/// The notch's slash commands. Typed alone in the Ask field, they open a part of
/// Wisp instead of sending a prompt. Only "/" followed by letters, with nothing
/// else typed, can be a command, so a prompt that merely starts with "/" (a file
/// path such as "/Users/me/notes.txt") is still sent as a prompt.
///
/// Pure logic, checked by tests/NotchCommandChecks.swift without the app.
enum NotchCommand: String, CaseIterable, Equatable {
    case today, memory, research, settings, report, window, new, about, resetwisp, corpus, screen

    var title: String { "/" + rawValue }

    var summary: String {
        switch self {
        case .screen: return "Explain what's on my screen"
        case .corpus: return "Browse, run or export assistant test cases"
        case .today: return "Open your day"
        case .memory: return "Open Memory"
        case .about: return "Tell Wisp about yourself"
        case .research: return "Research your next prompt"
        case .settings: return "Quick settings"
        case .report: return "Report a problem with the last reply"
        case .window: return "Continue in the Wisp window"
        case .new: return "Start a new chat"
        case .resetwisp: return "Erase Wisp's data and start over (keeps the model)"
        }
    }

    /// The commands a person can use. /corpus is a developer test runner: it is
    /// listed, matched and run only in Debug Mode, so a new user never meets it.
    /// /screen is listed only when Screen Recording is already granted
    /// (`includeScreen`); Wisp never asks for it from here.
    static func available(includeCorpus: Bool, includeScreen: Bool = true) -> [NotchCommand] {
        allCases.filter { ($0 != .corpus || includeCorpus) && ($0 != .screen || includeScreen) }
    }

    /// The commands this input could still become. Empty unless the input looks
    /// like a command, so the list only appears while typing one.
    static func suggestions(for input: String, includeCorpus: Bool = true, includeScreen: Bool = true) -> [NotchCommand] {
        guard let word = commandWord(input) else { return [] }
        return available(includeCorpus: includeCorpus, includeScreen: includeScreen).filter { $0.rawValue.hasPrefix(word) }
    }

    /// The command to run for this input: an exact command, or a prefix only one
    /// command starts with. Nil means it is not a whole command.
    static func match(_ input: String, includeCorpus: Bool = true, includeScreen: Bool = true) -> NotchCommand? {
        guard let word = commandWord(input), !word.isEmpty else { return nil }
        let commands = available(includeCorpus: includeCorpus, includeScreen: includeScreen)
        if let exact = NotchCommand(rawValue: word), commands.contains(exact) { return exact }
        let candidates = commands.filter { !$0.exactOnly && $0.rawValue.hasPrefix(word) }
        return candidates.count == 1 ? candidates[0] : nil
    }

    /// Runs only when typed in full. /resetwisp erases Wisp's data (after its
    /// confirmation card), so a prefix such as "/reset" never stands in for it,
    /// and it never makes a shorter prefix ambiguous. /screen captures the
    /// display, so "/s" stays /settings.
    var exactOnly: Bool { self == .resetwisp || self == .screen }

    /// Whether this input is the reset command, typed in full.
    static func isReset(_ input: String) -> Bool { commandWord(input) == NotchCommand.resetwisp.rawValue }

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
