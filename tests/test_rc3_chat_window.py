"""Source checks for the Chat window's rc3 repairs, for the parts that live in
views and so are outside the standalone Swift checks (tests/ChatChecks.swift,
tests/ChatStoreChecks.swift cover the logic).
Reads source only; no app, service or network."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app/Sources/WispApp"


def _read(name: str) -> str:
    return (APP / name).read_text()


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


def test_voiceover_hears_a_reply_an_error_and_a_waiting_card():
    store = _read("Chat/ChatStore.swift")
    handle = _body(store, "private func handle(_ event: ChatEvent")
    # A corpus card is declined and the handler returns before any announcement.
    assert handle.index("message.approval?.state = .denied") < handle.index("ChatAnnouncement.approval(message)")
    assert "ChatAnnouncement.reply(message, plain: spokenText)" in handle
    # Streamed text is buffered and returns first: it is never announced word by word.
    streamed = handle[handle.index('if event.type == "delta"'):handle.index("flush(conversation)")]
    assert "return" in streamed and "announce" not in streamed
    # Only when VoiceOver is on, as the notch does for its own answers.
    assert "NSWorkspace.shared.isVoiceOverEnabled" in store and ".announcementRequested" in store
    # The store is compiled for its checks without the speech cleanup; the window supplies it.
    assert "SpokenText" not in store
    assert "store.spokenText = SpokenText.plain" in _body(_read("Chat/ChatViews.swift"), "struct ChatRootView: View")


def test_the_card_is_one_named_group_and_the_sidebar_marks_a_waiting_chat():
    views = _read("Chat/ChatViews.swift")
    card = _body(views, "struct ChatApprovalCard: View")
    assert ".accessibilityElement(children: .contain)" in card and '"Wisp needs your OK"' in card
    row = _body(views, "private struct ChatHistoryRow: View")
    assert "if conversation.hasPendingApproval {" in row
    assert '.accessibilityValue(conversation.hasPendingApproval ? "Waiting for your OK" : "")' in row
    # Waiting means a reply that can still be answered, not a card left by an ended one.
    assert "var hasPendingApproval: Bool { busy && " in _read("Chat/ChatStore.swift")
