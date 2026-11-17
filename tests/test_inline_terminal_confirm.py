"""The terminal run is confirmed inline, in the code block, never in an alert window."""
import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parent.parent / "app" / "Sources"
RUNNER = (SOURCES / "WispApp" / "ScriptRunner.swift").read_text()
MARKDOWN = (SOURCES / "WispApp" / "MarkdownView.swift").read_text()


def test_script_runner_has_no_alert():
    assert "NSAlert" not in RUNNER
    assert "confirmTerminalRun" not in RUNNER
    assert "runModal" not in RUNNER


def test_run_in_terminal_only_from_confirmed_path():
    calls = [m.start() for m in re.finditer(r"\brunInTerminal\(", RUNNER)]
    # One definition plus one call, inside runInTerminalConfirmed.
    assert len(calls) == 2
    confirmed = RUNNER[RUNNER.index("static func runInTerminalConfirmed"):]
    confirmed = confirmed[: confirmed.index("\n    }\n")]
    assert "runInTerminal(code" in confirmed
    # perform() must not reach the terminal.
    perform = RUNNER[RUNNER.index("static func perform"):RUNNER.index("static func runInTerminalConfirmed")]
    assert "runInTerminal" not in perform
    assert "case .terminal:\n            return" in perform


def test_no_caller_outside_the_confirm_card():
    for path in SOURCES.rglob("*.swift"):
        if path.name == "ScriptRunner.swift":
            continue
        text = path.read_text()
        assert "runInTerminal(" not in text, path
        if "runInTerminalConfirmed" in text:
            assert path.name == "MarkdownView.swift"
    assert MARKDOWN.count("runInTerminalConfirmed") == 1
    block = MARKDOWN[MARKDOWN.index('Button("Run in Terminal")'):]
    assert "runInTerminalConfirmed" in block[:200]


def test_first_press_only_confirms():
    first = MARKDOWN[MARKDOWN.index("if target.runsScript {"):]
    first = first[: first.index("} else {")]
    assert "confirming = true" in first
    assert "ScriptRunner" not in first


def test_confirm_card_contents():
    assert "Run this script in Terminal?" in MARKDOWN
    assert "The script that will run" in MARKDOWN
    assert 'Button("Cancel")' in MARKDOWN and ".keyboardShortcut(.defaultAction)" in MARKDOWN
    assert "@State private var confirming" in MARKDOWN
    assert ".frame(maxHeight: 200)" in MARKDOWN
