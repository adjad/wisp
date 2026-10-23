"""Every AppleScript a live reader can run passes the evaluation write check.

The OS profile can limit Apple Events only by destination (Mail, Notes), not
by verb, so the evaluation app checks each script before it runs
(NotchEvaluationScriptPolicy, enforced in MailReader.run and NotesReader).
This test mirrors that rule, reading its word lists from the Swift source so
the two cannot drift, and applies it to the readers' actual script literals.
The native checks cover the Swift implementation on the same kinds of input.
Nothing here runs a script or contacts an app.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "Sources" / "WispApp"
POLICY_SOURCE = (APP / "NotchEvaluation.swift").read_text(encoding="utf-8")
LIVE_READERS = ("MailReader.swift", "NotesReader.swift")


def _swift_set(name: str) -> set[str]:
    match = re.search(rf"static let {name}: Set<String> = \[(.*?)\]", POLICY_SOURCE, re.S)
    assert match, name
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


FORBIDDEN = _swift_set("forbiddenWords")
APP_PROPERTIES = _swift_set("applicationProperties")
DATE_PARTS = _swift_set("dateParts")
APPLICATIONS = _swift_set("applications")


def _stripped(text: str) -> str:
    out, i, depth = [], 0, 0
    while i < len(text):
        c, nxt = text[i], text[i + 1] if i + 1 < len(text) else ""
        if depth:
            if c == "(" and nxt == "*":
                depth, i = depth + 1, i + 2
            elif c == "*" and nxt == ")":
                depth, i = depth - 1, i + 2
            else:
                i += 1
        elif c == "(" and nxt == "*":
            depth, i = 1, i + 2
        elif (c == "-" and nxt == "-") or c == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif c == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            out.append(' "" ')
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _allowed_set(w: list[str]) -> bool:
    if len(w) >= 2 and w[1] == "to":
        return w[0] not in APP_PROPERTIES
    if len(w) >= 4 and w[1] == "of" and w[3] == "to":
        return w[0] == "end" or w[0] in DATE_PARTS
    return w[:5] == ["applescript", "s", "text", "item", "delimiters"]


def refusal(source: str) -> str | None:
    lowered = source.lower()
    for match in re.finditer(r"(?<![a-z_])tell\s+", lowered):
        if lowered[:match.start()].endswith("end "):
            continue
        target = re.match(r'application\s+"([a-z]+)"', lowered[match.end():])
        if not target or target.group(1) not in APPLICATIONS:
            return "tell"
    words = re.findall(r"[^\W]+", _stripped(lowered))
    if hit := next((w for w in words if w in FORBIDDEN), None):
        return hit
    for i, word in enumerate(words):
        if word == "set" and not _allowed_set(words[i + 1:i + 6]):
            return "set"
    return None


def reader_scripts() -> list[tuple[str, int, str]]:
    """Every multi-line literal in the live readers that scripts an app."""
    found = []
    for name in LIVE_READERS:
        text = (APP / name).read_text(encoding="utf-8")
        for match in re.finditer(r'"""\n(.*?)"""', text, re.S):
            body = match.group(1)
            if "tell application" in body:
                # Interpolations become a neutral value; real values are
                # quoted AppleScript strings, which the rule removes anyway.
                body = re.sub(r"\\\((?:[^()]|\([^()]*\))*\)", "0", body)
                found.append((name, text[:match.start()].count("\n") + 1, body))
    return found


def test_every_live_reader_script_passes_the_write_check():
    scripts = reader_scripts()
    assert {name for name, _, _ in scripts} == set(LIVE_READERS)
    assert len(scripts) >= 6                      # accounts, identity, header, raw, history; notes
    failures = [(name, line, refusal(body)) for name, line, body in scripts if refusal(body)]
    assert failures == []


def test_every_reader_script_call_site_is_checked():
    mail = (APP / "MailReader.swift").read_text(encoding="utf-8")
    notes = (APP / "NotesReader.swift").read_text(encoding="utf-8")
    # MailReader runs every script through run(_:tag:), which checks first.
    assert mail.count("NSAppleScript(source:") == 1
    run = mail.split("private func run(_ source: String", 1)[1]
    assert run.index("NotchEvaluationReaders.refuseScript") < run.index("NSAppleScript(source:")
    assert notes.count("NSAppleScript(source:") == 1
    assert notes.index("NotchEvaluationReaders.refuseScript") < notes.index("NSAppleScript(source:")


def test_the_mirror_refuses_writes_and_keeps_reads():
    assert refusal('tell application "Mail"\n set read status of m to true\nend tell') == "set"
    assert refusal('tell application "Mail"\n delete m\nend tell') == "delete"
    assert refusal('tell application "Finder"\n get name\nend tell') == "tell"
    assert refusal('tell application "Mail"\n set capName to "Delete -- send"\nend tell') is None
    assert {"send", "delete", "make", "move", "copy"} <= FORBIDDEN and "set" not in FORBIDDEN
