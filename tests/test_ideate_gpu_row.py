"""The Ideate GPU-limit row reaches Terminal only through the code block's confirming card.

Source checks (the Swift view can't run here): the row hands the command to MarkdownView as a
bash block; nothing in the row or in IdeateGPULimit runs a process, an AppleScript or sudo; and
a bash block's Run in Terminal button only opens the Cancel-first card."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "Sources" / "WispApp"


def _gpu_row() -> str:
    view = (APP / "NotchIdeateView.swift").read_text()
    start = view.index("private var gpuRow: some View")
    end = view.index("private var modelNote: some View", start)
    return view[start:end]


RUNNERS = ("runInTerminalConfirmed", "NSAppleScript", "Process(", "NSTask", "posix_spawn", "system(",
           "NSWorkspace", "do script", "AuthorizationExecute", "SMJobBless")


def test_row_uses_the_code_block_card_only():
    row = _gpu_row()
    assert "MarkdownView(text: limit.codeBlock)" in row
    for name in RUNNERS:
        assert name not in row, name


def test_the_limit_type_runs_nothing():
    src = (APP / "IdeateGPULimit.swift").read_text()
    code = re.sub(r"//[^\n]*", "", src)
    for name in RUNNERS:
        assert name not in code, name
    assert 'return "```bash\\n\\(command)\\n```"' in src or '"```bash\\n\\(command)\\n```"' in src
    assert 'sysctlbyname("iogpu.wired_limit_mb", &mb, &size, nil, 0)' in src   # read only: no new value


def test_a_bash_block_runs_only_after_the_card():
    md = (APP / "MarkdownView.swift").read_text()
    assert 'case "bash", "sh", "zsh", "shell": return .terminal(interpreter: nil, ext: "sh")' in \
        (APP / "ScriptRunner.swift").read_text()
    assert "if target.runsScript {\n                                confirming = true" in md
    card = md[md.index("private func confirmCard"):md.index("private func actionButton")]
    assert 'Button("Cancel") { confirming = false }' in card and ".keyboardShortcut(.defaultAction)" in card
    assert card.index('Button("Cancel")') < card.index("ScriptRunner.runInTerminalConfirmed")


def test_nothing_makes_it_permanent_or_handles_the_password():
    for path in APP.glob("Ideate*.swift"):
        text = path.read_text()
        for bad in ("sysctl.conf", "LaunchDaemons", "launchctl", "SecKeychain", "password ="):
            assert bad not in text, (path.name, bad)
