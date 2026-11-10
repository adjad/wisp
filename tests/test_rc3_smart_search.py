"""Source contracts for Smart Search's result list (SearchModel.swift, SearchPanel.swift).

Pure text checks on the Swift source. The behaviour itself is exercised by
tests/SearchModelChecks.swift, which needs the Swift compiler; these pin the
same rules where no compiler runs. No app, no window, no server.
"""

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


def test_every_literal_match_keeps_its_row():
    """Two real matches a few characters apart are two rows. Rows used to be
    merged on a fixed 24-character grid, which dropped the second of two close
    matches from the list, the count and the arrow keys."""
    model = (APP / "SearchModel.swift").read_text()
    rows = _body(model, "var rows: [Result]")
    assert "/ 24" not in rows and "bucket" not in rows
    # Literal rows go in whole, first; only the later tiers are merged.
    assert "var out = literal" in rows
    assert "for r in lexical + semantic" in rows
    assert "Self.sameHit(r, $0)" in rows
    same = _body(model, "static func sameHit(_ a: Result, _ b: Result) -> Bool")
    assert "a.start < b.end && b.start < a.end" in same
    assert "abs(a.start - b.start) < 4" in same
    # The compiled check covers the behaviour.
    checks = (ROOT / "tests" / "SearchModelChecks.swift").read_text()
    assert "await everyLiteralMatchKeepsItsRow()" in checks


def test_the_selected_row_is_scrolled_into_sight():
    """The arrow keys and Return move the selection from outside the view, and
    the list holds more rows than the panel shows. Nothing scrolled, so past
    about the ninth row the highlight was out of sight."""
    panel = (APP / "SearchPanel.swift").read_text()
    content = _body(panel, "@ViewBuilder private var content: some View")
    assert content.index("ScrollViewReader { proxy in") < content.index("ScrollView {")
    assert content.count(".frame(maxHeight: 360)") == 1
    change = content[content.index(".onChange(of: model.selection)"):]
    # Rows are rebuilt on every read and can shrink: never index past them.
    assert change.index("guard rows.indices.contains(selected) else { return }") \
        < change.index("proxy.scrollTo(rows[selected].id)")
    rows = _body(panel, "private var resultList: some View")
    assert ".id(r.id)" in rows
    # VoiceOver hears which row is current.
    assert ".accessibilityAddTraits(i == model.selection ? .isSelected : [])" in rows
    # Clicking a row still selects and reveals it.
    assert "model.selection = i" in rows and "model.revealSelected()" in rows
