"""Source contracts for what a "Before you ask" card does after its Send tap.

The card's view and its fire live in SwiftUI code that no standalone check
compiles, so these read the source text. They run nothing and contact nothing.
The pure rules (which sentence, which state) are checked natively in
tests/ProactiveChecks.swift.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS = (ROOT / "app/Sources/WispApp/ProactiveCards.swift").read_text(encoding="utf-8")
MODELS = (ROOT / "app/Sources/WispApp/ProactiveModels.swift").read_text(encoding="utf-8")


def _body(text: str, signature: str) -> str:
    """The brace-balanced body that follows `signature`."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced body after {signature!r}")


FIRE = _body(CARDS, "func fire(_ card: ProactiveCard, today: TodayModel) async")
CARD_VIEW = _body(CARDS, "private func cardView(_ card: ProactiveCard, position: Int, of cards: [ProactiveCard]) -> some View")
DRAFT_EDITOR = _body(CARDS, "private func draftEditor(_ card: ProactiveCard, _ action: ProactiveCard.Action")


def test_a_reply_that_was_not_sent_never_shows_the_services_code():
    """a06-brief-send#3: `error` is a code (recipients_changed, unconfirmed) and the
    sentence is in `result`; the card showed the code in red with "Try again"."""
    parse = _body(MODELS, "static func parse(_ data: Data) -> ProactiveOutcome")
    assert 'obj["result"] as? String' in parse and "isSentence(message)" in parse
    assert 'status: (obj["status"] as? String) ?? ""' in parse
    # The card's state comes from the service's status, not from the message.
    assert "switch outcome.refusal(method: action.method)" in FIRE
    assert "fires[card.id] = .failed(outcome.message" not in FIRE


def test_a_recipient_change_shows_the_new_recipient_before_send_comes_back():
    """The service rewrote the stored card's recipient; the next tap must repeat that
    one, or the service refuses it ("can't be changed here")."""
    confirm = FIRE[FIRE.index("case .confirm(let line):"):FIRE.index("case .unconfirmed(let line):")]
    assert confirm.index("await today.refresh(afterMutation: true)") < confirm.index("fires[card.id] = .confirm(line)")
    # Every reply the service didn't send reads the plan again (a refused recipient too).
    retry = FIRE[FIRE.index("case .retry(let line):"):FIRE.index("return", FIRE.index("case .retry(let line):"))]
    assert "if action.sendsDraft { await today.refresh(afterMutation: true) }" in retry
    # A confirm is not drawn as a failure, and its button still says Send.
    assert "if case .confirm(let line) = fire" in CARD_VIEW
    note = CARD_VIEW[CARD_VIEW.index("if case .confirm(let line) = fire"):CARD_VIEW.index("if case .unconfirmed(let line) = fire")]
    assert "Theme.bad" not in note
    assert "if case .failed = fire { return true }" in _body(CARDS, "private func isFailed(")


def test_a_reply_that_may_have_gone_out_offers_no_second_send():
    unconfirmed = FIRE[FIRE.index("case .unconfirmed(let line):"):FIRE.index("case .retry(let line):")]
    assert "fires[card.id] = .unconfirmed(line)" in unconfirmed
    # Neither the card's own button nor the draft's Send is drawn in that state.
    assert "if let action, !sendInDraft, !isUnconfirmed(fire) { fireButton(card, action, fire) }" in CARD_VIEW
    assert "if withSend, !isUnconfirmed(fire) {" in DRAFT_EDITOR
    assert CARDS.count("fireButton(card, action, fire)") == 2
    assert "if case .unconfirmed(let line) = fire" in CARD_VIEW
