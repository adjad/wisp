"""Source contracts for the notch's rc3 repairs.

Pure text checks on the Swift sources: no compiler, no window, no app. The
reveal's behavior is exercised natively by scripts/test_overlay_transition.sh
(tests/OverlayTransitionChecks.swift), which CI does not run, so this holds the
shape in CI.
"""

import re
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


# --- VoiceOver hears approval cards and notices (OverlayModel) ---------------


def _announcements(source: str) -> str:
    return region(source, "private func announceForVoiceOver(", "\n    }\n")


def test_one_helper_announces_and_only_while_voiceover_is_on():
    model = swift("OverlayModel.swift")
    helper = _announcements(model)
    guard = helper.index("guard NSWorkspace.shared.isVoiceOverEnabled else { return }")
    # The line is an autoclosure built after the guard, so nothing is composed
    # or posted with VoiceOver off.
    assert "_ line: @autoclosure () -> String" in helper
    assert guard < helper.index("line()") < helper.index("NSAccessibility.post(")
    assert model.count(".announcementRequested") == 1


def test_an_approval_card_is_announced_when_it_appears():
    model = swift("OverlayModel.swift")
    confirm = region(model, 'case "confirm":', "case \"message_draft\":")
    shown = confirm.index("pending = card")
    said = confirm.index(
        "announceForVoiceOver(Self.approvalAnnouncement(reason: card.reason, grantable: card.grantable))")
    assert shown < said
    # The announcement adds nothing to the card's own flow: answering it is
    # still the card's buttons and resolve().
    assert "resolve(" not in confirm and "approve(" not in confirm


def test_the_announcement_names_only_the_buttons_the_card_shows():
    model = swift("OverlayModel.swift")
    builder = region(model, "nonisolated static func approvalAnnouncement(", "\n    }\n")
    assert '(grantable ? " Choices: Deny, Always allow, Allow once." : " Choices: Deny, Allow once.")' in builder
    # The preview can be a long script; it stays on the card.
    assert "preview" not in builder.replace("/// ", "")
    # The card shows "Always allow" under the same condition.
    view = swift("OverlayView.swift")
    card = region(view, "private func confirmCard(_ p: OverlayModel.Pending) -> some View {", "\n    }\n")
    assert 'if p.grantable {\n                    Button("Always allow")' in card
    assert 'Button("Deny")' in card and 'Button("Allow once")' in card


def test_a_notice_is_announced_once():
    model = swift("OverlayModel.swift")
    notice = region(model, "func showTransientNotice(", "\n    }\n")
    # Before the new line is stored, so a line shown again to change how long
    # it stays (the ready line) is not said twice.
    said = notice.index("if transientNotice != message {")
    assert "announceForVoiceOver(message, priority: info ? .medium : .high)" in notice
    assert said < notice.index("transientNotice = message")


# --- "still generating…" only for a real stall, in real seconds --------------


def _handle(model: str) -> str:
    return region(model, "private func handle(_ ev: WispClient.Event, dispatchID: UInt64) {",
                  "private var notReadyRetried")


def test_real_content_restarts_the_stall_count():
    model = swift("OverlayModel.swift")
    handle = _handle(model)
    cases = {
        "delta": region(handle, 'case "delta":', 'case "text":'),
        "text": region(handle, 'case "text":', 'case "error":'),
        "reasoning": region(handle, 'case "reasoning":', 'case "delta":'),
        "clear_answer": region(handle, 'case "clear_answer":', 'case "confirm":'),
    }
    for name, body in cases.items():
        assert body.count("answerMoved()") == 1, name
    # The delta case leaves early, so the reset has to come before that.
    delta = cases["delta"]
    assert delta.index("answerMoved()") < delta.index("return")
    # It runs per streamed token: written only when it changes.
    moved = region(model, "private func answerMoved() {", "\n    }\n")
    assert "if heartbeats != 0 { heartbeats = 0 }" in moved


def test_the_turn_record_keeps_every_heartbeat():
    model = swift("OverlayModel.swift")
    beat = region(_handle(model), 'case "heartbeat":', 'case "reasoning":')
    assert "heartbeats += 1" in beat and "turnHeartbeats += 1" in beat
    assert "heartbeats = 0; turnHeartbeats = 0" in model
    assert "turn.heartbeatCount = turnHeartbeats" in model
    assert "turn.heartbeatCount = heartbeats" not in model
    # Only a new turn clears the turn's count.
    assert model.count("turnHeartbeats = 0") == 2  # its declaration and start()


def test_heartbeat_seconds_come_from_one_place_at_the_senders_pace():
    model = swift("OverlayModel.swift")
    view = swift("OverlayView.swift")
    loop = float(re.search(r"static let loopHeartbeatSeconds = ([\d.]+)", model).group(1))
    silence = float(re.search(r"static let silenceHeartbeatSeconds = ([\d.]+)", model).group(1))
    # The agent loop's throttle and the plain-chat silence wait.
    agent = (ROOT / "service" / "agent" / "loop.py").read_text()
    paces = {float(x) for x in re.findall(r"last_hb > ([\d.]+)", agent)}
    assert paces == {loop}, paces
    waits = (ROOT / "service" / "inference" / "heartbeat.py").read_text()
    assert float(re.search(r"interval: float = ([\d.]+)", waits).group(1)) == silence
    # A turn routed with tools is the loop's; anything else is plain chat.
    assert "Self.heartbeatSeconds(heartbeats, fromLoop: turnNeededTools == true)" in model
    # The stall line, the working label and the thinking label all convert there.
    label = region(model, "var processingLabel: String {", "\n    }\n")
    assert "Self.heartbeatSeconds(heartbeats, fromLoop: true)" in label
    assert "(\\(stallSeconds)s)" in label
    assert "still generating… (\\(model.stallSeconds)s)" in view
    assert "if model.heartbeats > 0 {" in view
    for source in (label, view):
        assert not re.search(r"heartbeats\)? \* \d", source)
