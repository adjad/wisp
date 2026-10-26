// Contract for the notch's slash commands (app/Sources/WispApp/NotchCommand.swift).
// Pure logic: never launches Wisp or contacts its service.
import Foundation

@main
struct NotchCommandChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            count += 1
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
        }

        // Whole commands run.
        check(NotchCommand.match("/today") == .today, "/today runs")
        check(NotchCommand.match("  /Today \n") == .today, "case and surrounding space are ignored")
        check(NotchCommand.match("/new") == .new, "/new runs")
        check(NotchCommand.match("/window") == .window, "/window runs")
        check(NotchCommand.match("/demo") == .demo, "/demo runs")
        check(NotchCommand.match("/d") == .demo, "/d is only /demo")
        for command in NotchCommand.allCases {
            check(NotchCommand.match(command.title) == command, "\(command.title) matches itself")
        }

        // A prefix only one command starts with runs it; an ambiguous one does not.
        check(NotchCommand.match("/to") == .today, "unique prefix runs")
        check(NotchCommand.match("/m") == .memory, "single-letter unique prefix runs")
        check(NotchCommand.match("/re") == nil, "/re is ambiguous (research, report)")
        check(NotchCommand.match("/") == nil, "a bare slash is not a command")

        // Anything else is a prompt.
        check(NotchCommand.match("today") == nil, "no slash, no command")
        check(NotchCommand.match("/today please") == nil, "extra words make it a prompt")
        check(NotchCommand.match("/Users/me/notes.txt") == nil, "a file path is a prompt")
        check(NotchCommand.match("/tmp") == nil, "a word that starts no command is a prompt")
        check(NotchCommand.match("/todayx") == nil, "a longer word is not a command")
        check(NotchCommand.match("/tødåy") == nil, "non-ASCII letters are not commands")
        check(NotchCommand.match("") == nil, "empty input is nothing")

        check(NotchCommand.match("/cor") == .corpus, "unique corpus prefix opens the runner")
        check(NotchCommand.isCorpusRequest("/corpus all"), "corpus arguments use dedicated dispatch")
        check(NotchCommand.isCorpusRequest(" /CORPUS LIVE-100 "), "case IDs and whitespace are accepted")
        check(NotchCommand.isCorpusRequest("/corpus bad-selector"), "bad selectors stay out of normal inference")
        check(!NotchCommand.isCorpusRequest("/corpus/file.txt"), "paths stay ordinary prompts")
        check(!NotchCommand.isCorpusRequest("please /corpus all"), "embedded text is not a corpus command")
        check(NotchCommand.isCorpusRequest("/corpus export"), "export uses the corpus dispatch, not inference")
        check(NotchCommand.corpus.summary.contains("export"), "the /corpus summary mentions export")

        // Suggestions appear only while typing a command.
        check(NotchCommand.suggestions(for: "/") == NotchCommand.allCases, "a bare slash lists every command")
        check(NotchCommand.suggestions(for: "/re") == [.research, .report], "a prefix narrows the list in order")
        check(NotchCommand.suggestions(for: "/tmp").isEmpty, "no list for a non-command word")
        check(NotchCommand.suggestions(for: "/Users/me").isEmpty, "no list for a path")
        check(NotchCommand.suggestions(for: "hello").isEmpty, "no list for a prompt")
        check(NotchCommand.suggestions(for: "/today now").isEmpty, "no list once words follow")

        print("\(count) notch command checks passed.")
    }
}
