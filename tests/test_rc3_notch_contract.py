"""Source contracts for the notch's rc3 repairs.

Pure text checks on the Swift sources: no compiler, no window, no app. The
reveal's behavior is exercised natively by scripts/test_overlay_transition.sh
(tests/OverlayTransitionChecks.swift), which CI does not run, so this holds the
shape in CI.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWIFT = ROOT / "app" / "Sources" / "WispApp"


def swift(name: str) -> str:
    return (SWIFT / name).read_text()


def region(source: str, start: str, end: str) -> str:
    """The text from `start` up to the next `end` after it."""
    begin = source.index(start)
    return source[begin:source.index(end, begin + len(start))]


# --- A display change during an open or close (OverlayPanel) -----------------


def test_display_change_finishes_the_reveal_it_cancels():
    panel = swift("OverlayPanel.swift")
    change = region(panel, "func applyDisplayChange() {", "\n    }\n")
    # The waiting completion is taken before the cancel that drops it, and
    # called after the re-fit, so the open or close it belongs to can end.
    taken = change.index("let interrupted = pendingReveal")
    cancelled = change.index("cancelReveal()")
    refit = change.index("fillMask()")
    called = change.index("interrupted.completion()")
    assert taken < cancelled < refit < called
    # An interrupted open then fits the content, unless a newer reveal began.
    assert "if interrupted.opening, revealGeneration == token { resizeToFit() }" in change


def test_reveal_remembers_its_completion_until_it_runs():
    panel = swift("OverlayPanel.swift")
    reveal = region(panel, "private func reveal(opening: Bool, completion: @escaping () -> Void) {", "\n    }\n")
    remembered = reveal.index("pendingReveal = (opening: opening, completion: completion)")
    finish = reveal.index("let finish = {")
    assert remembered < finish
    # The normal finish forgets it before calling it, so it can only run once.
    body = reveal[finish:]
    assert body.index("self.pendingReveal = nil") < body.index("completion()")


def test_only_a_display_change_runs_a_cancelled_completion():
    panel = swift("OverlayPanel.swift")
    cancel = region(panel, "private func cancelReveal() {", "\n    }\n")
    # Every cancel drops it: a superseding reveal or a hidden panel must not
    # run a stale completion (OverlayTransitionChecks: "Hidden panel rejects
    # stale callbacks").
    assert "pendingReveal = nil" in cancel and "completion()" not in cancel
    hide = region(panel, "override func orderOut(_ sender: Any?) {", "\n    }\n")
    assert "cancelReveal()" in hide and "completion" not in hide
    assert panel.count(".completion()") == 1
