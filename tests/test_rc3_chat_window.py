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


def test_details_says_this_mac_only_on_a_confirmed_local_launch():
    views = _read("Chat/ChatViews.swift")
    inspector = _body(views, "struct ChatInspector: View")
    assert 'row("Ran on", ChatCloudUse.ranOn(store.cloudInUse),' in inspector
    assert "color: store.cloudInUse == false ? ChatTheme.ok : ChatTheme.text2)" in inspector
    assert '"This Mac", color: ChatTheme.ok' not in views
    ran_on = _body(_read("Chat/ChatTypes.swift"), "static func ranOn(_ cloudInUse: Bool?) -> String")
    assert ran_on.count('"This Mac"') == 1 and "case .some(false): return \"This Mac\"" in ran_on


def test_what_is_typed_survives_a_rebuilt_composer():
    views = _read("Chat/ChatViews.swift")
    # The draft lives outside the view: with the chat, or with the store when no chat is open.
    assert "ChatComposer(store: store, draft: conversation.composerDraft," in views
    assert "ChatComposer(store: store, draft: store.welcomeDraft," in views
    composer = _body(views, "struct ChatComposer: View")
    assert "let draft: ChatComposerDraft" in composer
    appear = _body(composer, ".onAppear")
    # Read back first; a message that came back after a drop still wins.
    assert appear.index("text = draft.text") < appear.index("if let restored = restoredInput")
    assert "image = draft.image" in appear
    # The came-back note stays only while that message is still in the box.
    assert "notice != nil, draft.text.isEmpty || draft.text != draft.restoredText" in appear
    typed = _body(composer, ".onChange(of: text)")
    assert typed.index("draft.text = edited") < typed.index("guard notice != nil")
    assert ".onChange(of: image?.dataURL) { _, _ in draft.image = image }" in composer
    send = _body(composer, "private func send()")
    assert send.index("draft.clear()") < send.index("store.submit(")
    # Typing must not redraw the window, and nothing is written to disk.
    store = _read("Chat/ChatStore.swift")
    draft = _body(store, "final class ChatComposerDraft")
    assert "@Published" not in draft and "UserDefaults" not in draft and "write(" not in draft
    assert "let composerDraft = ChatComposerDraft()" in _body(store, "final class ChatConversation: ObservableObject")
    # The rebuilds this has to survive are still there, unchanged.
    assert ".id(textSize)" in views and ".id(conversation.id)" in views


def test_the_card_says_what_always_allow_covers_whenever_it_is_offered():
    card = _body(_read("Chat/ChatViews.swift"), "struct ChatApprovalCard: View")
    # A rule with no folder or site covers every action of the tool, and the card says so.
    assert '"Always allow applies to every \\(approval.tool) action"' in card
    assert '"Always allow applies to \\(approval.tool) on \\(approval.scopeHint)"' in card
    body = _body(card, "var body: some View")
    pending = _body(body, "if approval.state == .pending")
    line = pending[pending.index("if approval.grantable {"):pending.index("HStack(spacing: 8)")]
    assert "Text(alwaysAllowLine)" in line and "scopeHint.isEmpty" not in line
    # The buttons and what they send are as before: the line is the only change.
    assert 'ChatCardButton(title: "Deny") { resolve(false, "once") }' in pending
    assert 'if approval.grantable { ChatCardButton(title: "Always allow") { resolve(true, "always") } }' in pending
    assert 'ChatCardButton(title: "Allow once", primary: true) { resolve(true, "once") }' in pending
