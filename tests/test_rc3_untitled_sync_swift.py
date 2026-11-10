"""Source contracts for how Calendar events and dated reminders with no title
are synced (CalendarReader.swift, RemindersWriter.swift).

Pure text checks on the Swift source; tests/CalendarReaderChecks.swift and
tests/RemindersPolicyChecks.swift run the rule itself, which needs the Swift
compiler. No Calendar, no Reminders, no server.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "Sources" / "WispApp"


def _code(text: str) -> str:
    """`text` without its // comments, so a comment can't satisfy a check."""
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`, comments removed."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return _code(text[open_at:i + 1])
    raise AssertionError(f"unbalanced body after {signature!r}")


def _swift_whitespace() -> set[int]:
    writer = (APP / "RemindersWriter.swift").read_text()
    declared = re.search(r"private static let pythonWhitespace: Set<UInt32> = Set\(\s*\[([^\]]+)\]"
                         r" \+ Array\(0x([0-9A-Fa-f]+)\.\.\.0x([0-9A-Fa-f]+)\)\)", writer)
    assert declared, "update this contract with the whitespace set it reads"
    listed = {int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]+)", declared.group(1))}
    return listed | set(range(int(declared.group(2), 16), int(declared.group(3), 16) + 1))


def test_an_untitled_event_or_dated_reminder_is_still_synced():
    """EventKit reads a missing title back as "", not nil, so `title ?? ...`
    never supplied the placeholder, and the service keeps no row with an empty
    title: the meeting was missing from the day and its time read as free."""
    reader = (APP / "CalendarReader.swift").read_text()
    # The rows are built in readSnapshot, which sync() runs off the main thread.
    sync = _body(reader, "static func readSnapshot(")
    assert "let title = RemindersWriter.syncedTitle(ev.title)" in sync
    assert '?? "(untitled)"' not in sync
    # The title sent, the kind and the fallback id all come from that one value.
    assert "CalendarEventPolicy.classify(title)" in sync and '"title": title,' in sync
    assert 'ev.eventIdentifier ?? "\\(title)-\\(sd.timeIntervalSince1970)"' in sync

    writer = (APP / "RemindersWriter.swift").read_text()
    dated = _body(writer, "func sync()")
    assert '"title": Self.syncedTitle(r.title),' in dated
    assert '?? "(untitled)"' not in dated
    # Undated reminders keep their own, stricter form.
    assert "Self.undatedEntry(id: r.calendarItemIdentifier, title: r.title," in dated


def test_blank_means_what_the_service_calls_blank():
    """A real title is sent unchanged. Blank is what Python's strip() leaves
    empty, so the app and the service agree on which titles have none."""
    writer = (APP / "RemindersWriter.swift").read_text()
    rule = _body(writer, "static func syncedTitle(_ raw: String?) -> String")
    assert "raw.unicodeScalars.contains(where: { !pythonWhitespace.contains($0.value) })" in rule
    assert 'return "(untitled)"' in rule and "return raw" in rule
    assert "prefix(" not in rule and "trimmed" not in rule
    # The Swift set is exactly Python's whitespace.
    assert _swift_whitespace() == {point for point in range(0x110000) if chr(point).isspace()}

    def synced(raw):
        """The Swift rule, over the Swift set."""
        blank = raw is None or all(ord(char) in _swift_whitespace() for char in raw)
        return "(untitled)" if blank else raw

    blanks = [None, "", " ", " \n\t ", " 　", "\u001c\u0085"]
    reals = ["Standup", "  Padded title ", "1:1", "☕️", "(untitled)", "CS 101 Exam"]
    for raw in blanks:
        assert synced(raw) == "(untitled)" and not (raw or "").strip(), repr(raw)
    for raw in reals:
        assert synced(raw) == raw and raw.strip(), raw
    # The compiled checks use these same cases.
    checks = (ROOT / "tests" / "CalendarReaderChecks.swift").read_text()
    assert "        titleChecks()\n" in checks
    assert r'[nil, "", " ", " \n\t ", "\u{A0}\u{3000}", "\u{1C}\u{85}"]' in checks
    assert r'["Standup", "  Padded title ", "1:1", "\u{2615}\u{FE0F}", "(untitled)", "CS 101 Exam"]' in checks
    assert 'RemindersWriter.syncedTitle("") == "(untitled)"' in (
        ROOT / "tests" / "RemindersPolicyChecks.swift").read_text()
