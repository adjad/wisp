"""Every AppleScript fragment a live reader can run passes the evaluation allowlist.

The OS profile can limit Apple Events only by destination (Mail, Notes), not
by verb, so the evaluation app checks each script before it runs
(NotchEvaluationScriptPolicy, enforced in MailReader.run and NotesReader).
The rule is an allowlist built from the readers' own scripts. This test
collects every script fragment in the readers: multi-line literals,
single-line literals and the helpers spliced into them. It requires the
Swift allowlists to equal exactly the words and locals those fragments use,
so a new word in a reader script needs a deliberate allowlist change. It
then checks each fragment with a mirror of the rule. The native checks cover
the Swift implementation itself. Nothing here runs a script or contacts an app.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "Sources" / "WispApp"
POLICY_SOURCE = (APP / "NotchEvaluation.swift").read_text(encoding="utf-8")
# Every file whose literals reach NSAppleScript in the live evaluation build.
SCRIPT_SOURCES = ("MailReader.swift", "NotesReader.swift", "MailHistoryMerge.swift")
LIVE_READERS = ("MailReader.swift", "NotesReader.swift")
SCRIPTISH = re.compile(r"^\s*(?:set|tell|if|repeat|try|on error|end|considering|error|return|exit)\b"
                       r"|current date")
INTERPOLATION = re.compile(r"\\\((?:[^()]|\([^()]*\))*\)")


def _swift_set(name: str) -> set[str]:
    match = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", POLICY_SOURCE, re.S)
    assert match, name
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


ALLOWED = _swift_set("allowedWords")
LOCALS = _swift_set("allowedLocals")
FORBIDDEN = _swift_set("forbiddenWords")
DATE_PARTS = _swift_set("dateParts")
APPLICATIONS = _swift_set("applications")


def _stripped(text: str) -> str | None:
    """Comments and literals removed; a literal that is exactly mail/notes is kept.

    None when a non-ASCII character appears outside a string literal.
    """
    out, i, depth = [], 0, 0
    while i < len(text):
        c, nxt = text[i], text[i + 1] if i + 1 < len(text) else ""
        if depth:
            if not c.isascii():
                return None
            if c == "(" and nxt == "*":
                depth, i = depth + 1, i + 2
            elif c == "*" and nxt == ")":
                depth, i = depth - 1, i + 2
            else:
                i += 1
        elif c == "(" and nxt == "*":
            depth, i = 1, i + 2
        elif (c == "-" and nxt == "-") or c == "#":
            while i < len(text) and text[i] not in "\n\r":
                if not text[i].isascii():
                    return None
                i += 1
        elif c == '"':
            i += 1
            start = i
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            content = text[start:i]
            i += 1
            out.append(f' "{content}" ' if content in ("mail", "notes") else ' "" ')
        else:
            if not c.isascii():
                return None
            out.append(c)
            i += 1
    return "".join(out)


def _words(text: str) -> list[str]:
    kept = _stripped(text.lower())
    assert kept is not None, text
    return re.findall(r"[^\W]+", re.sub(r'"(mail|notes)"', '""', kept))


def _allowed_set(w: list[str]) -> bool:
    if len(w) >= 2 and w[1] == "to":
        return w[0] in LOCALS
    if len(w) >= 4 and w[1] == "of" and w[3] == "to":
        return (w[0] == "end" and w[2] in LOCALS) or (w[0] in DATE_PARTS and w[2] == "refdate")
    return w[:5] == ["applescript", "s", "text", "item", "delimiters"]


def refusal(source: str) -> str | None:
    lowered = source.lower()
    if "«" in lowered or "»" in lowered:
        return "raw"
    for match in re.finditer(r"(?<![a-z_])tell\s+", lowered):
        if lowered[:match.start()].endswith("end "):
            continue
        target = re.match(r'application\s+"([a-z]+)"', lowered[match.end():])
        if not target or target.group(1) not in APPLICATIONS:
            return "tell"
    kept = _stripped(lowered)
    if kept is None:
        return "non-ascii"
    if re.search(r"current\s+application", kept):
        return "current application"
    for match in re.finditer(r"(?<![a-z0-9_])application(?![a-z0-9_])", kept):
        if not re.match(r'\s+"(mail|notes)"', kept[match.end():]):
            return "application"
    text = re.sub(r'"(mail|notes)"', '""', kept)
    rest = re.sub(r"applescript\s*['’]s\s+text\s+item\s+delimiters", "", text)
    if re.search(r"['’]\s*s\b", rest):
        return "possessive"
    words = re.findall(r"[^\W]+", text)
    if hit := next((w for w in words if w in FORBIDDEN), None):
        return hit
    if hit := next((w for w in words if w not in ALLOWED and not w.isdigit()), None):
        return "word:" + hit
    for i, word in enumerate(words):
        if word == "set" and not _allowed_set(words[i + 1:i + 6]):
            return "set"
    return None


def _literals(text: str):
    for match in re.finditer(r'"""\n(.*?)"""', text, re.S):
        yield "multi-line", text[:match.start()].count("\n") + 1, match.group(1)
    # Single-line literals, with the multi-line ones blanked out first.
    rest = re.sub(r'"""\n.*?"""', lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    for number, line in enumerate(rest.splitlines(), 1):
        if line.strip().startswith("//"):
            continue
        for match in re.finditer(r'"((?:\\.|[^"\\])*)"', line):
            yield "single-line", number, match.group(1).replace('\\"', '"')


def script_fragments() -> list[tuple[str, str, int, str]]:
    """Every literal that is (part of) a reader script, interpolations neutralized."""
    found = []
    for name in SCRIPT_SOURCES:
        for kind, line, body in _literals((APP / name).read_text(encoding="utf-8")):
            if kind == "multi-line" or SCRIPTISH.search(body):
                # Spliced values are numbers or quoted, escaped strings at run
                # time; the run-time check sees them, and here they become 0.
                found.append((name, kind, line, INTERPOLATION.sub("0", body)))
    return found


def test_the_allowlists_are_exactly_what_the_reader_scripts_use():
    fragments = script_fragments()
    kinds = {(name, kind) for name, kind, _, _ in fragments}
    assert {("MailReader.swift", "multi-line"), ("MailReader.swift", "single-line"),
            ("NotesReader.swift", "multi-line"), ("MailHistoryMerge.swift", "single-line")} <= kinds
    assert sum(1 for f in fragments if "tell application" in f[3]) == 6     # the six reader scripts
    used = {w for f in fragments for w in _words(f[3]) if not w.isdigit()}
    assigned = set()
    for f in fragments:
        words = _words(f[3])
        assigned |= {words[i + 1] for i, w in enumerate(words) if w == "set" and words[i + 2:i + 3] == ["to"]}
    assert used == ALLOWED, (sorted(used - ALLOWED), sorted(ALLOWED - used))
    assert assigned == LOCALS, (sorted(assigned - LOCALS), sorted(LOCALS - assigned))


def test_every_reader_script_fragment_passes():
    failures = [(name, kind, line, refusal(body)) for name, kind, line, body in script_fragments() if refusal(body)]
    assert failures == []


def test_the_review_list_is_refused_and_never_allowed():
    called_out = {"send", "delete", "make", "move", "copy", "use", "framework", "load", "write",
                  "display", "do", "perform", "check", "script"}
    assert called_out <= FORBIDDEN and not FORBIDDEN & ALLOWED and "set" not in FORBIDDEN
    mail = 'tell application "Mail"\n{}\nend tell'
    for body, why in [
        ("«event coremsnd» m", "raw"),
        ("use framework \"Foundation\"", "use"),
        ("set x to current application's NSString's stringWithString:\"a\"", "current application"),
        ("set acctName to m's name", "possessive"),
        ('load script file "x"', "load"),
        ('write "x" to f', "write"),
        ('display dialog "x"', "display"),
        ('do shell script "true"', "do"),
        ("set read status of m to true", "set"),
        ("set name of nt to \"x\"", "set"),
        ("set selection to {}", "set"),
        ("set brandnew to 1", "set"),
        ("set end of themessages_x to 1", "word:themessages_x"),
        ("mark m", "word:mark"),
        ('set n to count of application "Finder"', "application"),
        ('set n to count of (every item of application id "com.apple.finder")', "application"),
        ("set n to 1 -- note\rdelete m", "delete"),                       # CR ends a comment
        ("set n to 1 -- note\r\ndelete m", "delete"),                     # so does CRLF
        ("set n to 1 -- note\u2028delete m", "non-ascii"),                # U+2028 is refused
        ("set n to 1\u2029delete m", "non-ascii"),
        ("del\u200bete m", "non-ascii"),                                  # zero-width split
        ("s\u0435nd m", "non-ascii"),                                      # Cyrillic e
        ("set n to 1 # x\u200b", "non-ascii"),                            # even in a comment
    ]:
        assert refusal(mail.format(body)) is not None, (body, why)
    assert refusal('tell application "Finder"\n get name\nend tell') == "tell"
    assert refusal(mail.format('set capName to "Delete -- send é\u200b\u2028"')) is None   # inside a string
    assert refusal(mail.format('set n to count of messages of inbox -- fine\r\nset n to 1')) is None
    assert refusal(mail.format('set n to name of application "Notes"')) is None


def test_every_reader_script_call_site_is_checked():
    mail = (APP / "MailReader.swift").read_text(encoding="utf-8")
    notes = (APP / "NotesReader.swift").read_text(encoding="utf-8")
    # MailReader runs every script through run(_:tag:), which checks first.
    assert mail.count("NSAppleScript(source:") == 1
    run = mail.split("private func run(_ source: String", 1)[1]
    assert run.index("NotchEvaluationReaders.refuseScript") < run.index("NSAppleScript(source:")
    assert notes.count("NSAppleScript(source:") == 1
    assert notes.index("NotchEvaluationReaders.refuseScript") < notes.index("NSAppleScript(source:")


LAUNCH = re.compile(r"OSAScript|NSUserAppleScriptTask|NSUserUnixTask|NSUserScriptTask|NSUserAutomatorTask|NSTask"
                    r"|(?<![A-Za-z])Process\(|osascript|NSAppleScript\(|posix_spawn|\bexecv|(?<![.\w])system\(|popen\(")
EVENTKIT_WRITE = re.compile(r"\.(?:save|remove)\([^)\n]*(?:commit|span):|(?<!CATransaction)\.commit\(\)")
GUARD = re.compile(r"#if WISP_NOTCH_EVALUATION\n(?:(?!#endif).)*?(?:fatalError\(\"EVALUATION_BLOCKED|return\b)", re.S)


def _hits(pattern):
    for path in sorted(APP.rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line = text[text.rfind("\n", 0, match.start()) + 1:text.find("\n", match.start())]
            if not line.lstrip().startswith("//"):
                yield path.name, text, match.start(), line.strip()


def _guarded(text: str, pos: int) -> bool:
    """The enclosing function stops (fatalError or return) under the evaluation flag before pos."""
    head = text[text.rfind("func ", 0, pos):pos]
    return bool(GUARD.search(head))


def test_no_other_script_or_process_runs_in_the_evaluation_build():
    """Every AppleScript, OSA, user-script-task or process launch is checked or stopped first."""
    sender = (APP / "OutboundSender.swift").read_text(encoding="utf-8")
    real, stub = sender.split("#else", 1)
    assert real.lstrip().startswith("import Foundation") and "#if !WISP_NOTCH_EVALUATION" in real
    assert "NSAppleScript" in real and not LAUNCH.search(stub)
    assert stub.count("refuse(") >= 11 and "fatalError" in stub
    unchecked = []
    for name, text, pos, line in _hits(LAUNCH):
        if name in LIVE_READERS and line.startswith("guard let s = NSAppleScript(source:"):
            continue                    # the policy check runs first (test above)
        if name == "OutboundSender.swift" and pos < len(real):
            continue                    # compiled out under the evaluation flag
        if name == "ScriptRunner.swift" and line.startswith('case "applescript", "osascript": return .terminal('):
            continue                    # an interpreter name; only runInTerminal uses it, and it stops
        if not _guarded(text, pos):
            unchecked.append((name, line))
    assert unchecked == []
    runner = (APP / "ScriptRunner.swift").read_text(encoding="utf-8")
    for function, call in (("private static func runInTerminal(", "NSAppleScript(source:"),
                           ("private static func openWithApp(", "Process()"),
                           ("private static func chmodExecutable(", "Process()")):
        body = runner.split(function, 1)[1]
        guard = body.index("#if WISP_NOTCH_EVALUATION")
        assert guard < body.index(call) < body.index("\n    }\n")
        assert "fatalError(\"EVALUATION_BLOCKED" in body[guard:body.index("#endif")]
    perform = runner.split("static func perform(", 1)[1]
    assert perform.index("#if WISP_NOTCH_EVALUATION") < perform.index("\n    }\n")


def test_eventkit_write_paths_stop_in_the_evaluation_build():
    """Reminders and Calendar only read here: every save, remove and commit stops first."""
    hits = list(_hits(EVENTKIT_WRITE))
    assert {name for name, *_ in hits} == {"RemindersWriter.swift", "CalendarReader.swift"}
    assert len(hits) >= 8
    assert [(name, line) for name, text, pos, line in hits if not _guarded(text, pos)] == []
    writer = (APP / "RemindersWriter.swift").read_text(encoding="utf-8")
    for function in ("func performVerified(", "func reconcileVerified("):
        body = writer.split(function, 1)[1]
        assert body.index("#if WISP_NOTCH_EVALUATION") < body.index("guard isAuthorized")
    for function in ("func start()", "func sync()"):           # the reads stay
        body = writer.split(function, 1)[1].split("\n    }\n", 1)[0]
        assert "WISP_NOTCH_EVALUATION" not in body
