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

        // Outside Debug Mode the developer corpus runner is invisible: not listed,
        // not matched, so "/corpus" is just a prompt like any other text.
        check(!NotchCommand.suggestions(for: "/", includeCorpus: false).contains(.corpus), "/corpus is not listed")
        check(NotchCommand.match("/corpus", includeCorpus: false) == nil, "/corpus does not run")
        check(NotchCommand.match("/c", includeCorpus: false) == nil, "no other command starts with c")
        check(NotchCommand.match("/about", includeCorpus: false) == .about, "other commands still run")
        check(NotchCommand.suggestions(for: "/", includeCorpus: true).contains(.corpus), "Debug Mode lists /corpus")

        // Suggestions appear only while typing a command.
        check(NotchCommand.suggestions(for: "/") == NotchCommand.allCases, "a bare slash lists every command")
        check(NotchCommand.suggestions(for: "/re") == [.research, .report, .resetwisp], "a prefix narrows the list in order")
        check(NotchCommand.suggestions(for: "/tmp").isEmpty, "no list for a non-command word")
        check(NotchCommand.suggestions(for: "/Users/me").isEmpty, "no list for a path")
        check(NotchCommand.suggestions(for: "hello").isEmpty, "no list for a prompt")
        check(NotchCommand.suggestions(for: "/today now").isEmpty, "no list once words follow")

        // /resetwisp erases Wisp's data (after its confirmation card): it runs only
        // when typed in full, never from a prefix, and never makes another prefix ambiguous.
        check(NotchCommand.match("/resetwisp") == .resetwisp, "/resetwisp runs when typed in full")
        check(NotchCommand.match(" /ResetWisp ") == .resetwisp, "case and surrounding space are ignored")
        check(NotchCommand.match("/resetwisp", includeCorpus: false) == .resetwisp, "available outside Debug Mode")
        for prefix in ["/reset", "/resetw", "/resetwis"] {
            check(NotchCommand.match(prefix) == nil, "\(prefix) never stands in for /resetwisp")
        }
        check(NotchCommand.match("/res") == .research && NotchCommand.match("/rese") == .research,
              "prefixes research shares with it still open research")
        check(NotchCommand.match("/resetwisp now") == nil, "extra words make it a prompt")
        check(NotchCommand.match("/resetwisp!") == nil, "punctuation makes it a prompt")
        check(NotchCommand.resetwisp.exactOnly && NotchCommand.allCases.filter(\.exactOnly) == [.resetwisp],
              "only /resetwisp is exact-only")
        check(NotchCommand.isReset("/resetwisp") && !NotchCommand.isReset("/reset") && !NotchCommand.isReset("resetwisp"),
              "isReset is the whole command only")
        check(NotchCommand.suggestions(for: "/rese") == [.research, .resetwisp], "it is listed while typing it")

        print("\(count) notch command checks passed.")
    }
}
