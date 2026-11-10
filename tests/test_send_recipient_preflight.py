"""A send that can't find its recipient is "not sent", and never locks the chat.

Real send tools, real agent loop and real /agent turns; a fake app bridge,
synthetic contacts and a scripted model. Both sides of the safety line are here:
a failure that definitely came before any send frees the chat, and an outcome
that may have gone out still locks it, whatever the user replies.
2026-11-04 Ask export: send_email to a contact with no saved address.
"""
from __future__ import annotations

import pytest

from service import main
from service.agent import loop
from service.assistant import outbound_queue as queue_module
from service.memory import subject
from service.memory.store import SessionStore
from service.router.model_led import family_for, registry_specs
from service.tools import action_tools, imessage_tools, registry
from tests.test_model_led_entrypoint import endpoint, tool_call  # noqa: F401

REAL_TOOLS = {name: registry.REGISTRY[name] for name in ("send_email", "send_message")}
SUBJECT, BODY = "My week", "Hi Sam,\n\nThursday at 10 works.\n\nAdi"
ADDRESS = "sam.fixture@example.com"
NOTICE = main.UNRESOLVED_ACTION_NOTICE


class Cards:
    """Stand-in for the interactive approver: records every card it is shown."""

    def __init__(self):
        self.allow, self.shown = True, []

    async def confirm(self, action):
        self.shown.append(action)
        return self.allow


@pytest.fixture
def chat(endpoint, monkeypatch):  # noqa: F811
    """Real run_agent and real send tools behind a fake bridge."""
    from service.safety import policy
    monkeypatch.delenv("WISP_MODEL_ONLY", raising=False)
    monkeypatch.setattr(main, "run_agent", loop.run_agent)
    monkeypatch.setattr(policy, "_FULL_ACCESS", True)
    monkeypatch.setattr(policy, "_READ_ONLY", False)
    monkeypatch.setattr(policy, "_CFG", {})
    monkeypatch.setattr("service.safety.grants.check", lambda *args: None)
    monkeypatch.setattr(loop, "_BLOCKS_CACHE", None)
    monkeypatch.setattr(loop, "narration_mode", lambda: "off")
    monkeypatch.setattr(loop, "no_thinking_kwargs", lambda *args, **kwargs: {})
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kwargs: "")
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: ["me.fixture@example.com"])
    monkeypatch.setattr("service.skills.skills_context_block", lambda *args: "")
    monkeypatch.setattr(imessage_tools, "_name_handles", {
        "phone only": ["+15550100123"],
        "one address": ["one.fixture@example.com"],
        "two addresses": ["a.fixture@example.com", "b.fixture@example.com"],
        "my card": ["me.fixture@example.com"],
        "sam north": ["sam.north@example.com"],
        "sam south": ["sam.south@example.com"],
    })
    registry.REGISTRY.update(REAL_TOOLS)
    cards = Cards()
    monkeypatch.setattr(main, "InteractiveApprover", lambda emit: cards)
    endpoint.cards = cards
    endpoint.dispatched = []
    endpoint.bridge = lambda op, payload: {"ok": True, "status": "succeeded"}

    async def bridge(op, payload, **kwargs):
        endpoint.dispatched.append((op, payload))
        return endpoint.bridge(op, payload)
    monkeypatch.setattr(action_tools, "app_request", bridge)
    endpoint.sid = endpoint.store.create_session()
    return endpoint


def _email_call(to, subject_line=SUBJECT, body=BODY, **extra):
    return tool_call("send_email", to=to, subject=subject_line, body=body, **extra)


def _attempt(chat, to="Phone Only", prompt="can you email phone only my plan for the week"):
    """One turn where the model tries to email `to`."""
    family = family_for(registry_specs({"send_email": REAL_TOOLS["send_email"]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=["send_email"])],
                   [_email_call(to)], "Unused."]
    return chat.request(prompt, chat.sid)


def _text(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "text")


@pytest.mark.parametrize("to, expected", [
    ("Phone Only", "Phone Only has no email address saved"),
    ("Nobody Here", "I don't have a saved contact for Nobody Here"),
    ("Sam", "Sam matches several contacts"),
    ("Two Addresses", "several email addresses saved"),
])
def test_unresolved_recipient_asks_instead_of_showing_a_card(chat, to, expected):
    events = _attempt(chat, to)
    assert chat.cards.shown == [] and chat.dispatched == []  # no card, nothing sent
    text = _text(events)
    assert text.startswith("I haven't sent anything. ") and expected in text and text.endswith("?")
    assert "verified" not in text and "(" not in text
    # Nothing was claimed, so nothing can lock the chat.
    assert chat.store.unresolved_action(chat.sid) is None
    assert chat.store.pending_model_effects(chat.sid) == []


def test_an_address_reply_sends_the_kept_draft_through_the_normal_card(chat):
    _attempt(chat)
    # The model need only call the tool; what it typed never reaches the card.
    chat.script = [[_email_call(ADDRESS, "junk subject", "junk body")], "Sent it."]
    events = chat.request(ADDRESS, chat.sid)
    assert NOTICE not in _text(events)
    request = chat.owned[-1].requests[0]
    assert [t for t in request["tools"] if t["function"]["name"] == "send_email"
            and "description" in t["function"]], "the send's real schema is on offer"
    assert any(m["role"] == "system" and "nothing has been sent yet" in str(m["content"])
               for m in request["messages"])
    assert len(chat.cards.shown) == 1, "the confirmation is not weakened"
    card = chat.cards.shown[0]
    assert card["tool"] == "send_email" and card["args"]["to"] == ADDRESS
    assert f"To: {ADDRESS}" in card["preview"] and SUBJECT in card["preview"] and BODY in card["preview"]
    assert "junk" not in card["preview"]
    assert chat.dispatched == [("send_email", {"to": [ADDRESS], "cc": [], "subject": SUBJECT, "body": BODY})]
    assert chat.store.unresolved_action(chat.sid) is None


def test_the_kept_draft_is_never_sent_without_the_card(chat):
    _attempt(chat)
    chat.cards.allow = False
    chat.script = [[_email_call(ADDRESS)], "Okay."]
    chat.request(ADDRESS, chat.sid)
    assert len(chat.cards.shown) == 1 and chat.dispatched == []


def test_a_bare_address_does_nothing_unless_the_model_calls_the_tool(chat):
    _attempt(chat)
    chat.script = ["What would you like me to do with that address?"]
    chat.request(ADDRESS, chat.sid)
    assert chat.cards.shown == [] and chat.dispatched == []  # no auto-send


def test_the_draft_lasts_one_turn(chat):
    _attempt(chat)
    chat.script = ["It's sunny."]
    chat.request("what's the weather like", chat.sid)
    chat.script = [[_email_call(ADDRESS, "New subject", "A new message.")], "Okay."]
    chat.request(f"email {ADDRESS} a new message", chat.sid)
    # Nothing kept: the model's own new email goes to the card untouched.
    assert "A new message." in chat.cards.shown[0]["preview"]
    assert not any("could not go last turn" in str(m["content"]) for m in chat.agent_calls)
    assert subject.awaiting_send(chat.store, chat.sid) is None


@pytest.mark.parametrize("reply", ["not sam.fixture@example.com", "sam.fixture@example.com and add a PS"])
def test_a_reply_that_says_more_is_left_to_the_model(chat, reply):
    _attempt(chat)
    chat.script = [[_email_call(ADDRESS, "Their subject", "Their own wording.")], "Okay."]
    chat.request(reply, chat.sid)
    request = chat.owned[-1].requests[0]
    # The model sees the draft, and what it then writes is what the card shows.
    assert any(m["role"] == "system" and "could not go last turn" in str(m["content"])
               and "Thursday at 10" in str(m["content"]) for m in request["messages"])
    assert "Their own wording." in chat.cards.shown[0]["preview"]


def test_a_saved_address_goes_on_the_card(chat):
    _attempt(chat, "One Address")
    assert chat.cards.shown[0]["args"]["to"] == "one.fixture@example.com"
    assert "To: one.fixture@example.com" in chat.cards.shown[0]["preview"]
    assert [p["to"] for _, p in chat.dispatched] == [["one.fixture@example.com"]]


@pytest.mark.parametrize("to, expected", [
    ("My Card", "your own address"),  # a contact that resolves to the user's own address
    ("me.fixture@example.com", "didn’t send it"),  # refused earlier, by policy
])
def test_the_users_own_address_gets_no_card_and_does_not_lock(chat, to, expected):
    events = _attempt(chat, to)
    assert chat.cards.shown == [] and chat.dispatched == []
    assert expected in _text(events)
    assert chat.store.unresolved_action(chat.sid) is None


@pytest.mark.parametrize("response", [
    {"ok": False, "status": "not_attempted", "dispatch_started": False, "error": "Mail is closed."}])
def test_the_bridge_saying_it_never_started_is_not_sent(chat, response):
    chat.bridge = lambda op, payload: response
    events = _attempt(chat, ADDRESS)
    assert len(chat.cards.shown) == 1
    assert "I haven't sent anything" in _text(events) and "Mail is closed" in _text(events)
    assert chat.store.unresolved_action(chat.sid) is None


# -- The other side: an outcome that may have gone out still locks the chat. --

@pytest.mark.parametrize("response", [
    {"ok": False, "error": "lost the bridge"},
    {"ok": False, "status": "not_attempted", "dispatch_started": True},  # it did start
    {"ok": False, "status": "not_attempted"},  # no proof that it didn't
    {"ok": True, "status": "unknown"},
    {},
])
def test_an_unknown_send_still_locks_the_chat_and_an_address_cannot_lift_it(chat, response):
    chat.bridge = lambda op, payload: response
    events = _attempt(chat, ADDRESS, prompt=f"email {ADDRESS} my plan")
    assert len(chat.dispatched) == 1 and "haven't sent" not in _text(events)
    pending = chat.store.unresolved_action(chat.sid)
    assert pending is not None
    assert chat.store.pending_model_effects(chat.sid)[0]["status"] == "uncertain"

    # A different address, and the model trying anyway: nothing goes out.
    chat.script = [[_email_call("other.fixture@example.com")], "Okay."]
    events = chat.request("other.fixture@example.com", chat.sid)
    assert len(chat.dispatched) == 1 and len(chat.cards.shown) == 1
    assert _text(events).startswith(NOTICE)
    refused = [e["result"] for e in events if e["type"] == "tool_result"]
    assert refused and "unconfirmed outcome" in refused[0] and "NOT run" in refused[0]
    assert "constraints" not in refused[0]


def test_a_bridge_exception_is_not_proof_of_anything(chat):
    def boom(op, payload):
        raise TimeoutError("synthetic bridge timeout")
    chat.bridge = boom
    _attempt(chat, ADDRESS, prompt=f"email {ADDRESS} my plan")
    assert len(chat.dispatched) == 1
    assert chat.store.unresolved_action(chat.sid) is not None


def test_a_lock_keeps_a_kept_draft_from_being_offered(chat):
    _attempt(chat)
    # An unknown send elsewhere in the chat locks it while a draft is kept.
    claim = chat.store.claim_model_effect(chat.sid, "other", "send_message", {"to": "x"}, outbound=True)
    assert chat.store.finish_model_effect(chat.sid, claim, verified=False)
    chat.script = [[_email_call(ADDRESS)], "Okay."]
    events = chat.request(ADDRESS, chat.sid)
    assert chat.cards.shown == [] and chat.dispatched == []
    assert _text(events).startswith(NOTICE)
    assert not any("could not go last turn" in str(m["content"]) or "nothing has been sent yet" in str(m["content"])
                   for m in chat.owned[-1].requests[0]["messages"])


def test_only_the_typed_result_from_the_real_tool_is_proof():
    from dataclasses import replace
    tool = REAL_TOOLS["send_email"]
    proof = action_tools._not_sent("(no such contact)")
    assert registry.not_sent_receipt(tool, proof)
    assert not registry.not_sent_receipt(tool, str(proof))  # plain text is no proof
    assert not registry.not_sent_receipt(tool, "(the email was NOT sent: fixture)")
    assert not registry.not_sent_receipt(tool, proof[:100])  # a slice is plain text

    async def elsewhere(**args):
        return proof
    assert not registry.not_sent_receipt(replace(tool, func=elsewhere), proof)
    other = registry.Tool("run_shell", "fixture", {}, "shell", tool.func)
    assert not registry.not_sent_receipt(other, proof)
    # Unknown-outcome text never reads as not sent, and vice versa.
    unknown = registry._OutboundUnknownResult("Email outcome unknown.", operation="send_email", action_id="a")
    assert not registry.not_sent_receipt(tool, unknown)


def test_store_settles_not_sent_apart_from_a_completed_send(tmp_path):
    store = SessionStore(tmp_path / "sessions.db")
    try:
        sid = store.create_session()
        claim = store.claim_model_effect(sid, "one", "send_email", {"to": "Sam"}, outbound=True)
        assert store.finish_model_effect(sid, claim, verified=False, not_sent=True)
        assert store.unresolved_action(sid) is None and store.pending_model_effects(sid) == []
        row = store._db.execute("SELECT status, json_extract(state_json,'$.status') FROM workflows").fetchone()
        assert tuple(row) == ("completed", "not_sent")
        # Settling again is refused, and the same request can't claim it twice.
        assert not store.finish_model_effect(sid, claim, verified=False)
        assert not store.claim_model_effect(sid, "one", "send_email", {"to": "Sam"}, outbound=True)["admitted"]
        # A later request may send, and an unverified one still blocks.
        retry = store.claim_model_effect(sid, "two", "send_email", {"to": "sam@example.com"}, outbound=True)
        assert retry["admitted"]
        assert store.finish_model_effect(sid, retry, verified=False)
        assert store.unresolved_action(sid) is not None
    finally:
        store._db.close()


@pytest.mark.parametrize("prompt, tool, expected", [
    (ADDRESS, "send_email", ADDRESS),
    (f"  {ADDRESS}. ", "send_email", ADDRESS),
    (f"it's {ADDRESS}", "send_email", ADDRESS),
    (f"her email is {ADDRESS}", "send_email", ADDRESS),
    (f"use {ADDRESS} please", "send_email", ADDRESS),
    (f"send it to {ADDRESS}", "send_email", ADDRESS),
    (f"not {ADDRESS}", "send_email", ""),
    (f"{ADDRESS} but change the subject", "send_email", ""),
    (f"{ADDRESS}, {ADDRESS}x", "send_email", ""),
    ("+1 555 010 0123", "send_email", ""),
    ("+15550100123", "send_message", "+15550100123"),
    (ADDRESS, "send_message", ADDRESS),
    ("sam", "send_email", ""),
    ("", "send_email", ""),
])
def test_reply_address(prompt, tool, expected):
    assert subject.reply_address(prompt, tool) == expected


def test_too_long_a_draft_or_several_recipients_is_not_kept():
    assert subject._draft("send_email", {"to": "Sam", "subject": "s", "body": "b" * 9000}) is None
    assert subject._draft("send_email", {"to": "Sam, Ana", "subject": "s", "body": "b"}) is None
    assert subject._draft("forward_email", {"to": "Sam", "note": "n"}) is None
    assert subject._draft("send_message", {"to": "Sam", "text": "t"}) == {
        "tool": "send_message", "name": "Sam", "text": "t"}


def test_the_draft_never_rides_in_the_one_line_subject():
    fresh = subject.build(
        [{"id": "1", "name": "send_email", "args": {"to": "Sam", "subject": SUBJECT, "body": BODY}}],
        [{"id": "1", "name": "send_email", "result": "(fixture)", "awaiting_recipient": True}])
    record = subject.advance(None, fresh, now=100.0)
    assert record["entries"][subject.AWAITING_SEND]["value"]["body"] == BODY
    assert subject.AWAITING_SEND not in subject.live(record, now=100.0)
    assert BODY not in subject.hint(subject.live(record, now=100.0))
    # Gone after the next assistant turn, whatever it did.
    assert subject.AWAITING_SEND not in subject.advance(record, {}, now=101.0)["entries"]


# -- "and on my reminders?": what was on offer that turn. --

def test_a_reminders_follow_up_offers_the_reminders_tool_first():
    preload, _ = main._ask_tool_rank("and on my reminders?", "and emails?")
    assert preload[0] == "search_reminders" and "get_upcoming" in preload


def test_get_upcoming_steers_a_reminders_question_away_from_calendar_only():
    from service.tools.assistant_tools import get_upcoming  # noqa: F401 - registers it
    text = registry.get_tool("get_upcoming").parameters["properties"]["calendar_only"]["description"]
    assert "search_reminders" in text and "leaving reminders out" in text


# -- Who a send goes to, on its card and its receipt (rc3). --
#
# A typed name can match a contact saved under a different name: "Ian" matches
# only "Brian Chen" when nobody is saved as Ian. The card and the receipt name
# the contact that matched, and several matches still ask.

PHONE, BRIAN = "+15550101234", "brian.chen@example.com"
FUTURE = "2099-01-02T15:04:00"


@pytest.fixture
def contacts(monkeypatch):
    """One contact, saved as Brian Chen. Nobody is saved as Ian."""
    monkeypatch.setattr(imessage_tools, "_name_handles", {"brian chen": [PHONE, BRIAN]})
    monkeypatch.setattr(imessage_tools, "_contacts", {"5550101234": "Brian Chen", BRIAN: "Brian Chen"})
    monkeypatch.setattr(imessage_tools, "_lines", "")


@pytest.fixture
def app_bridge(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def request(op, payload, **kwargs):
        sent.append((op, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(action_tools, "app_request", request)
    return sent


@pytest.fixture
def queued(monkeypatch):
    rows: list[dict] = []

    def add(**row):
        rows.append(row)
        return "fixture-id"
    monkeypatch.setattr(queue_module.outbound_queue, "add", add)
    return rows


# ---- The card names the contact that matched ----

@pytest.mark.parametrize("tool, args, shown", [
    ("send_message", {"to": "Ian", "text": "Running late"}, f"Brian Chen ({PHONE})"),
    ("draft_message", {"to": "Ian", "text": "Running late"}, f"Brian Chen ({PHONE})"),
    ("schedule_send", {"channel": "message", "to": "Ian", "when": FUTURE, "body": "Running late"},
     f"Brian Chen ({PHONE})"),
    ("schedule_send", {"channel": "email", "to": "Ian", "when": FUTURE, "subject": "Late", "body": "Running late"},
     f"Brian Chen ({BRIAN})"),
    ("forward_email", {"message_id": "<m1@example.com>", "to": "Ian"}, f"Brian Chen ({BRIAN})"),
])
def test_a_card_names_the_contact_a_typed_name_matched(contacts, tool, args, shown):
    preview = action_tools.confirm_preview(tool, dict(args))
    assert f'To: {shown} - matched from "Ian"\n' in preview
    assert "To: Ian\n" not in preview


def test_a_card_for_the_saved_name_shows_the_handle_and_no_matched_note(contacts):
    preview = action_tools.confirm_preview("send_message", {"to": "brian chen", "text": "Hi"})
    assert preview == f"To: Brian Chen ({PHONE})\n\nHi"
    # The scheduled card resolves the one recipient the tool will, stray separator or not.
    assert f"\nTo: Brian Chen ({PHONE})\n" in action_tools.confirm_preview(
        "schedule_send", {"channel": "message", "to": "Brian Chen;", "when": FUTURE, "body": "Hi"})


def test_a_card_for_a_literal_handle_names_its_saved_contact(contacts):
    preview = action_tools.confirm_preview("send_message", {"to": PHONE, "text": "Hi"})
    assert preview == f"To: Brian Chen ({PHONE})\n\nHi"
    # The same number typed another way is the same contact.
    assert action_tools.confirm_preview("send_message", {"to": "(555) 010-1234", "text": "Hi"}).startswith(
        "To: Brian Chen ((555) 010-1234)\n")
    forward = action_tools.confirm_preview("forward_email", {
        "message_id": "<m1@example.com>", "to": f"{BRIAN}, other.fixture@example.com"})
    assert f"To: Brian Chen ({BRIAN}), other.fixture@example.com\n" in forward


@pytest.mark.parametrize("to", ["Nobody Here", "+15550109999", "stranger.fixture@example.com"])
def test_a_card_shows_an_unknown_recipient_as_given(contacts, to):
    assert action_tools.confirm_preview("send_message", {"to": to, "text": "Hi"}) == f"To: {to}\n\nHi"
    assert f"\nTo: {to}\n" in action_tools.confirm_preview(
        "schedule_send", {"channel": "message", "to": to, "when": FUTURE, "body": "Hi"})


def test_a_card_does_not_pick_between_several_matches(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {
        "brian chen": [PHONE], "adrian cole": ["+15550105678"]})
    monkeypatch.setattr(imessage_tools, "_contacts", {})
    assert action_tools.confirm_preview("send_message", {"to": "Ian", "text": "Hi"}) == "To: Ian\n\nHi"
    handle, problem = action_tools._resolve_recipient("Ian")
    assert not handle and problem.needs_recipient and "Which one do you mean?" in problem.question


def test_a_saved_name_is_shown_as_saved_and_an_exact_handle_wins(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {
        "dana mcfixture": ["+15550100001"], "old card": ["(555) 010-0001"]})
    # The lossy last-10-digits map holds only one of the two names.
    monkeypatch.setattr(imessage_tools, "_contacts", {"5550100001": "Dana McFixture"})
    assert imessage_tools.contact_name_for_handle("+15550100001") == "Dana McFixture"
    assert imessage_tools.contact_name_for_handle("(555) 010-0001") == "Old Card"
    assert imessage_tools.contact_name_for_handle("555.010.0001") == "Dana McFixture"
    assert imessage_tools.contact_name_for_handle("Dana") == ""
    assert imessage_tools.contact_name_for_handle("") == ""


# ---- The receipt names the contact too ----

async def test_a_text_receipt_names_the_matched_contact(contacts, app_bridge):
    assert await action_tools.send_message("Ian", "Running late") == f"Message sent to Brian Chen ({PHONE})."
    assert await action_tools.send_message(PHONE, "Running late") == f"Message sent to Brian Chen ({PHONE})."
    assert [payload["to"] for _, payload in app_bridge] == [PHONE, PHONE]


async def test_a_text_receipt_for_an_unsaved_number_is_unchanged(contacts, app_bridge):
    assert await action_tools.send_message("+15550109999", "Hi") == "Message sent to +15550109999."


async def test_a_scheduled_send_stores_the_matched_contact(contacts, queued):
    text = await action_tools.schedule_send(channel="message", to="Ian", when=FUTURE, body="Running late")
    mail = await action_tools.schedule_send(channel="email", to="Ian", when=FUTURE, subject="s", body="b")
    assert text.startswith(f"Scheduled: text to Brian Chen ({PHONE}) on ")
    assert mail.startswith(f"Scheduled: email to Brian Chen ({BRIAN}) on ")
    assert [(row["recipient"], row["display"]) for row in queued] == [
        (PHONE, f"Brian Chen ({PHONE})"), (BRIAN, f"Brian Chen ({BRIAN})")]


def test_the_cancel_card_shows_the_handle_once(monkeypatch):
    row = {"id": "fixture-id", "channel": "message", "recipient": PHONE, "display": f"Brian Chen ({PHONE})",
           "when_ts": 4070908800.0, "status": "pending", "subject": "", "body": "Running late"}
    monkeypatch.setattr(queue_module.outbound_queue, "get", lambda sid: row)
    preview = action_tools._cancel_scheduled_preview({"id": "fixture-id"})
    assert f"To: Brian Chen ({PHONE})\n" in preview and preview.count(PHONE) == 1
    # An older row that stored only a name still gains its handle.
    row["display"] = "Ian"
    assert f"To: Ian ({PHONE})\n" in action_tools._cancel_scheduled_preview({"id": "fixture-id"})


async def test_a_forward_receipt_names_the_matched_contact(contacts, app_bridge):
    receipt = await action_tools.forward_email("<m1@example.com>", "Ian, other.fixture@example.com")
    assert receipt == f"Forwarded to Brian Chen ({BRIAN}), other.fixture@example.com."
    assert app_bridge[0][1]["to"] == [BRIAN, "other.fixture@example.com"]


# ---- Through the real loop: the approved card is the one that names them ----

def _text_turn(chat, to):
    family = family_for(registry_specs({"send_message": REAL_TOOLS["send_message"]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=["send_message"])],
                   [tool_call("send_message", to=to, text="Running late")], "Unused."]
    return chat.request(f"text {to} I'm running late", chat.sid)


def test_the_text_card_in_a_chat_names_the_matched_contact(chat, monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"brian chen": [PHONE]})
    monkeypatch.setattr(imessage_tools, "_contacts", {"5550101234": "Brian Chen"})
    events = _text_turn(chat, "Ian")
    assert len(chat.cards.shown) == 1, "a text still asks, exactly once"
    card = chat.cards.shown[0]
    assert card["preview"] == f'To: Brian Chen ({PHONE}) - matched from "Ian"\n\nRunning late'
    # The call keeps the typed name, so a repeat in the same turn is still caught.
    assert card["args"]["to"] == "Ian"
    assert chat.dispatched == [("send_message", {"to": PHONE, "text": "Running late"})]
    results = [e["result"] for e in events if e["type"] == "tool_result" and "Message sent" in e["result"]]
    assert results == [f"Message sent to Brian Chen ({PHONE})."]


def test_several_matches_in_a_chat_still_ask_with_no_card(chat, monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {
        "brian chen": [PHONE], "adrian cole": ["+15550105678"]})
    events = _text_turn(chat, "Ian")
    assert chat.cards.shown == [] and chat.dispatched == []
    text = _text(events)
    assert "Ian matches several contacts" in text and text.endswith("?")
    assert chat.store.unresolved_action(chat.sid) is None


# ---- A name in cc is settled before the card, like a name in `to` ----

def _cc_turn(chat, cc):
    family = family_for(registry_specs({"send_email": REAL_TOOLS["send_email"]})[0])
    chat.script = [[tool_call("get_tool_schemas", families=[family], tools=["send_email"])],
                   [_email_call(ADDRESS, cc=cc)], "Unused."]
    return chat.request(f"email {ADDRESS} my plan and cc {cc}", chat.sid)


def test_a_saved_name_in_cc_goes_on_the_card_as_its_address(chat):
    _cc_turn(chat, "One Address")
    assert len(chat.cards.shown) == 1, "the email still asks, exactly once"
    card = chat.cards.shown[0]
    assert card["args"]["cc"] == "one.fixture@example.com"
    assert f"To: {ADDRESS}\nCc: one.fixture@example.com\nSubject: {SUBJECT}" in card["preview"]
    assert chat.dispatched == [("send_email", {
        "to": [ADDRESS], "cc": ["one.fixture@example.com"], "subject": SUBJECT, "body": BODY})]


@pytest.mark.parametrize("cc, expected", [
    ("Phone Only", "Phone Only has no email address saved"),
    ("Nobody Here", "I don't have a saved contact for Nobody Here"),
    ("Sam", "Sam matches several contacts"),
    ("Two Addresses", "several email addresses saved"),
])
def test_an_unresolved_name_in_cc_asks_instead_of_showing_a_card(chat, cc, expected):
    events = _cc_turn(chat, cc)
    assert chat.cards.shown == [] and chat.dispatched == []  # asked before any approval
    text = _text(events)
    assert text.startswith("I haven't sent anything. ") and expected in text and text.endswith("?")
    assert chat.store.unresolved_action(chat.sid) is None


def test_the_preflight_binds_cc_and_leaves_addresses_alone(contacts):
    tool = REAL_TOOLS["send_email"]
    base = {"to": ADDRESS, "subject": "s", "body": "b"}
    bound, problem = action_tools.prepare_send_recipient(tool, {**base, "cc": "Ian; copy.fixture@example.com"})
    assert problem is None and bound["cc"] == f"{BRIAN}, copy.fixture@example.com" and bound["to"] == ADDRESS
    # No cc: none is added, and an empty one is left as it came.
    assert action_tools.prepare_send_recipient(tool, dict(base)) == (base, None)
    assert action_tools.prepare_send_recipient(tool, {**base, "cc": " "}) == ({**base, "cc": " "}, None)
    args = {**base, "cc": "Nobody Here"}
    unchanged, problem = action_tools.prepare_send_recipient(tool, args)
    assert unchanged is args and problem.needs_recipient and registry.not_sent_receipt(tool, problem)


async def test_send_email_resolves_a_cc_name_without_a_card_too(contacts, app_bridge):
    assert await action_tools.send_email(ADDRESS, "s", "b", cc="Ian") == f"Email sent to {ADDRESS}."
    assert app_bridge == [("send_email", {"to": [ADDRESS], "cc": [BRIAN], "subject": "s", "body": "b"})]
    refused = await action_tools.send_email(ADDRESS, "s", "b", cc="Nobody Here")
    assert refused.needs_recipient and "Nobody Here" in refused.question and len(app_bridge) == 1
    # Still only addresses reach Mail: a cc that is neither is refused as before.
    assert (await action_tools.send_email("not an address, x", "s", "b")).needs_recipient
    assert len(app_bridge) == 1
