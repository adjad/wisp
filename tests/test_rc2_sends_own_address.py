"""A send is judged on where it really goes, not only on the name typed.

The own-address guard used to look only at `to` as written. A contact name that
resolves to the user's own address (a "Me" card, or a fact that saved the
user's address under someone else's name) slipped past it and the message went
to the user. Now the resolved recipient is judged too, before the card and
inside the tool, with the same confirmed_self_send escape. Synthetic contacts,
a synthetic identity and a fake app bridge; nothing is sent.
"""
from __future__ import annotations

import pytest

import service.main  # noqa: F401 — registers every tool
from service.tools import action_tools, imessage_tools, registry

ME = "me.fixture@example.com"
FRIEND = "friend.fixture@example.com"
TEXT, EMAIL = registry.REGISTRY["send_message"], registry.REGISTRY["send_email"]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {
        "my card": [ME],                       # the user's own card
        "mom": [ME],                           # a contact wrongly saved with the user's address
        "friend": [FRIEND],
    })
    monkeypatch.setattr(imessage_tools, "_contacts", {ME: "My Card", FRIEND: "Friend"})
    monkeypatch.setattr(imessage_tools, "_lines", "")
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: [ME])
    sent: list[tuple[str, dict]] = []

    async def request(op, payload, **kwargs):
        sent.append((op, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(action_tools, "app_request", request)
    return sent


@pytest.mark.parametrize("name", ["Mom", "My Card"])
def test_a_text_to_a_name_that_resolves_to_me_gets_no_card(world, name):
    args, problem = action_tools.prepare_send_recipient(TEXT, {"to": name, "text": "Hi"})
    assert problem is not None and problem.needs_recipient
    assert registry.not_sent_receipt(TEXT, problem)
    assert ME in problem and "OWN" in problem and name in problem.question
    assert world == []


async def test_the_text_tool_itself_refuses_a_name_that_resolves_to_me(world):
    refused = await action_tools.send_message(to="Mom", text="Hi")
    assert refused.needs_recipient and ME in refused and world == []
    # The same as typing the address.
    typed = await action_tools.send_message(to=ME, text="Hi")
    assert typed.needs_recipient and world == []


async def test_a_confirmed_self_send_still_goes(world):
    args, problem = action_tools.prepare_send_recipient(
        TEXT, {"to": "My Card", "text": "Hi", "confirmed_self_send": True})
    assert problem is None
    assert await action_tools.send_message(**args) == f"Message sent to My Card ({ME})."
    assert world == [("send_message", {"to": ME, "text": "Hi"})]


async def test_someone_else_is_unaffected(world):
    args, problem = action_tools.prepare_send_recipient(TEXT, {"to": "Friend", "text": "Hi"})
    assert problem is None
    await action_tools.send_message(**args)
    assert world == [("send_message", {"to": FRIEND, "text": "Hi"})]


async def test_an_email_to_a_name_that_resolves_to_me_is_caught_before_and_inside(world):
    base = {"subject": "s", "body": "b"}
    _, problem = action_tools.prepare_send_recipient(EMAIL, {**base, "to": "Mom"})
    assert problem is not None and problem.needs_recipient and ME in problem
    refused = await action_tools.send_email("Mom", "s", "b")
    assert refused.needs_recipient and world == []
    assert await action_tools.send_email("Mom", "s", "b", confirmed_self_send=True) == f"Email sent to {ME}."


async def test_my_address_among_several_recipients_is_caught(world):
    _, problem = action_tools.prepare_send_recipient(
        EMAIL, {"to": f"Friend, {ME}", "subject": "s", "body": "b"})
    assert problem is not None and problem.needs_recipient
    assert "resolves to" not in problem      # typed literally, so worded as typed
    refused = await action_tools.send_email(f"{FRIEND}, Mom", "s", "b")
    assert refused.needs_recipient and world == []


def test_the_guard_judges_only_what_it_is_given():
    # No identity: nothing is the user's own address.
    assert action_tools._own_address_guard("Mom", "send_message") is None
