"""A send the person aimed at themselves goes straight to its card.

Live run: "Email myself a test with the subject …" was refused by the
own-address guard and the model gave up; "Text myself: Wisp test message 1"
looked the person's own name up as a contact and asked for a number. The
person's own words this turn (self_send.mark_turn) now settle both: the guard
stands down and "me"/"myself"/their own name resolves to their own handle. The
card is still the approval. Synthetic identity, contacts and app bridge;
nothing is sent.
"""
from __future__ import annotations

import pytest

import service.main  # noqa: F401 — registers every tool
from service.tools import action_tools, imessage_tools, registry, self_send

ME = "sam.fixture@example.com"
APPLE_ID = "sam.fixture@icloud.com"
DANA = "dana.fixture@example.com"
TEXT, EMAIL = registry.REGISTRY["send_message"], registry.REGISTRY["send_email"]


@pytest.mark.parametrize("text, names_self", [
    ("Email myself a test with the subject Hello", True),
    ("Email me the notes", True),
    ("email me a summary of today", True),
    ("Send me an email with the weather", True),
    ("Send me a text that says hi", True),
    ("Send me a message when it's done", True),
    ("Send myself a reminder text", True),
    ("Text me: Wisp test message 2", True),
    ("Text myself: Wisp test message 1", True),
    ("Message me the address", True),
    ("iMessage myself the link", True),
    ("Forward me the email from Dana", True),
    ("Forward this to my email", True),
    ("Send the notes to me", True),
    ("Send an email to myself with the subject Test", True),
    ("Email this to my own address", True),
    ("Text it to my number", True),
    ("Can you please email me the report?", True),
    ("Hey Wisp, text me the code", True),
    ("Email Dana the notes and send a copy to me", True),
    ("Email Dana and tell her to call me", False),
    ("Text mom that I'm late", False),
    ("Send Sam the file he sent me", False),
    ("Ask Dana to email me the report", False),
    ("Tell Sam to text me back", False),
    ("Text Dana: email me later", False),
    ("Email Dana \"send it to me\"", False),
    ("Text mom 'email me when you land'", False),
    ("Text mom that I'm late and tell her to come to me", False),
    ("What did Dana email me yesterday?", False),
    ("Remind me to email Dana", False),
    ("Email Dana the notes", False),
    ("", False),
])
def test_wording(text, names_self):
    assert self_send.user_names_self(text) is names_self


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"dana": [DANA]})
    monkeypatch.setattr(imessage_tools, "_contacts", {DANA: "Dana"})
    monkeypatch.setattr(imessage_tools, "_lines", "")
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: [ME, APPLE_ID])
    monkeypatch.setattr("service.memory.identity.user_name", lambda: "Sam Fixture")
    monkeypatch.setattr("service.memory.identity._preferred_name", lambda: "")
    sent: list[tuple[str, dict]] = []

    async def request(op, payload, **kwargs):
        sent.append((op, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(action_tools, "app_request", request)
    yield sent
    self_send.mark_turn("")


async def test_email_myself_passes_the_guard_and_gets_its_card(world):
    self_send.mark_turn("Email myself a test with the subject Hello")
    args, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": ME, "subject": "Hello", "body": "test"})
    assert problem is None
    assert action_tools.confirm_preview("send_email", args).startswith(f"To: {ME}\n")
    assert await registry.run_tool(EMAIL, args) == f"Email sent to {ME}."
    assert world == [("send_email", {"to": [ME], "cc": [], "subject": "Hello", "body": "test"})]


async def test_email_to_me_by_name_resolves_to_the_primary_address(world):
    self_send.mark_turn("Email me the notes")
    for typed in ("me", "myself", "Sam", "Sam Fixture", ""):
        args, problem = action_tools.prepare_send_recipient(
            EMAIL, {"to": typed, "subject": "Notes", "body": "here"})
        assert problem is None and args["to"] == ME, typed


async def test_the_guard_is_as_strict_when_the_person_named_someone_else(world):
    self_send.mark_turn("Email Dana the notes")
    _, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": ME, "subject": "Notes", "body": "here"})
    assert problem is not None and problem.needs_recipient and "OWN" in problem
    refused = await action_tools.send_email(ME, "Notes", "here")
    assert refused.needs_recipient and world == []
    # And "me" is not the person's own handle on such a turn: it is a contact lookup.
    _, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": "me", "subject": "Notes", "body": "here"})
    assert problem is not None and ME not in problem


@pytest.mark.parametrize("turn", ["Text myself: Wisp test message 1", "Send me a text that says hi"])
@pytest.mark.parametrize("typed", ["me", "myself", "my number", "Sam", "Sam Fixture"])
async def test_a_text_to_myself_goes_to_my_own_handle(world, turn, typed):
    self_send.mark_turn(turn)
    args, problem = action_tools.prepare_send_recipient(TEXT, {"to": typed, "text": "hi"})
    assert problem is None
    # An Apple ID address first: iMessage reaches those. Bound to the card's handle.
    assert args["resolved_to"] == APPLE_ID and registry.is_host_bound(args["resolved_to"])
    assert APPLE_ID in action_tools.confirm_preview("send_message", args)
    result = await registry.run_tool(TEXT, args)
    assert not registry.is_tool_error(result) and APPLE_ID in result
    assert world == [("send_message", {"to": APPLE_ID, "text": "hi"})]


async def test_a_text_with_no_recipient_passed_goes_to_me_too(world):
    self_send.mark_turn("Text myself: Wisp test message 1")
    args, problem = action_tools.prepare_send_recipient(TEXT, {"to": "", "text": "hi"})
    assert problem is None and args["to"] == "me" and args["resolved_to"] == APPLE_ID


async def test_no_own_handle_known_is_a_clear_not_sent(world, monkeypatch):
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: [])
    self_send.mark_turn("Text myself: Wisp test message 1")
    _, problem = action_tools.prepare_send_recipient(TEXT, {"to": "Sam", "text": "hi"})
    assert problem is not None and registry.not_sent_receipt(TEXT, problem)
    assert problem.question == (
        "I don't know your own number or address: Wisp has no phone number for you, "
        "and takes your address from the accounts set up in Mail, where none has "
        "synced yet. What number or address should I use?")
    self_send.mark_turn("Email myself a test")
    _, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": "myself", "subject": "s", "body": "b"})
    assert problem.question == (
        "I don't know your own email address: Wisp takes it from the accounts set up "
        "in Mail, and none has synced yet. What address should I use?")
    assert world == []


async def test_someone_else_on_a_self_turn_is_still_a_contact(world):
    self_send.mark_turn("Send me a text that says hi")
    args, problem = action_tools.prepare_send_recipient(TEXT, {"to": "Dana", "text": "hi"})
    assert problem is None and args["resolved_to"] == DANA


# --- an address the model made up never stands in for "myself" --------------------
# 2026-11-13 real-model run, Full access on: "Email myself…" with no own address
# known reached a card addressed to an invented address, and it was approved.

async def test_an_invented_address_for_myself_is_refused_before_any_card(world, monkeypatch):
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: [])
    self_send.mark_turn("Email myself a test with the subject Hello")
    _, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": "test@test.com", "subject": "Hello", "body": "test"})
    assert problem is not None and registry.not_sent_receipt(EMAIL, problem)
    assert problem.question.startswith("I don't know your own email address")
    self_send.mark_turn("Text myself: hi")
    _, problem = action_tools.prepare_send_recipient(TEXT, {"to": "+1 555 010 0199", "text": "hi"})
    assert problem is not None and problem.question.startswith("I don't know your own number")
    assert world == []


async def test_an_invented_address_becomes_my_own_when_one_is_known(world):
    self_send.mark_turn("Email myself a test")
    args, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": "test@test.com", "subject": "Hello", "body": "test"})
    assert problem is None and args["to"] == ME


async def test_an_address_i_typed_or_a_saved_contact_keeps_its_place(world):
    self_send.mark_turn("Email me and pat@example.org the notes")
    args, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": "me, pat@example.org", "subject": "Notes", "body": "here"})
    assert problem is None and args["to"] == f"{ME}, pat@example.org"
    self_send.mark_turn("Email me and Dana the notes")
    args, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": f"me, {DANA}", "subject": "Notes", "body": "here"})
    assert problem is None and args["to"] == f"{ME}, {DANA}"                 # Dana is a saved contact
    # Not a turn that names me: nothing is rewritten.
    self_send.mark_turn("Email pat@example.org the notes")
    assert action_tools._self_not_invented("someone@else.test") == "someone@else.test"
