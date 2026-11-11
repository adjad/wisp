"""Only the host can choose where an approved text goes (`resolved_to`).

PR #506 binds a text's card to the one number it showed, carried to the send in
the host-only argument `resolved_to`. run_tool used to accept that key from any
caller, so a call that ARRIVED with one (a model on the ALLOW path, a stored
workflow or task step, a router-direct call) chose the number. Now only the
value action_tools.prepare_send_recipient sets counts: a plain string is refused
at dispatch and ignored by the tool itself. Real send_message, synthetic
contacts and a fake app bridge; nothing is sent.
"""
from __future__ import annotations

import json

import pytest

import service.main  # noqa: F401 — registers every tool
from service.tools import action_tools, imessage_tools, registry

PHONE, FORGED = "+15550101234", "+15550109999"
TOOL = registry.REGISTRY["send_message"]


@pytest.fixture
def contacts(monkeypatch):
    monkeypatch.setattr(imessage_tools, "_name_handles", {"brian chen": [PHONE]})
    monkeypatch.setattr(imessage_tools, "_contacts", {"5550101234": "Brian Chen"})
    monkeypatch.setattr(imessage_tools, "_lines", "")
    monkeypatch.setattr("service.memory.identity.user_emails", lambda: [])


@pytest.fixture
def bridge(monkeypatch):
    sent: list[tuple[str, dict]] = []

    async def request(op, payload, **kwargs):
        sent.append((op, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(action_tools, "app_request", request)
    return sent


async def test_run_tool_refuses_a_caller_supplied_number(contacts, bridge):
    # Whatever path the call came by, a plain resolved_to never reaches the tool.
    result = await registry.run_tool(TOOL, {"to": "Brian", "text": "Hi", "resolved_to": FORGED})
    assert registry.is_tool_error(result) and "unexpected argument" in result
    assert bridge == []


async def test_a_forged_number_for_a_literal_handle_is_refused_too(contacts, bridge):
    result = await registry.run_tool(TOOL, {"to": PHONE, "text": "Hi", "resolved_to": FORGED})
    assert registry.is_tool_error(result) and bridge == []


async def test_the_tool_itself_ignores_a_plain_number(contacts, bridge):
    # A direct call (API, proactive executor, skills) that passes the key anyway:
    # the name is looked up as if no card existed, and the forged number is unused.
    receipt = await action_tools.send_message(to="Brian", text="Hi", resolved_to=FORGED)
    assert bridge == [("send_message", {"to": PHONE, "text": "Hi"})]
    assert FORGED not in receipt


async def test_the_hosts_own_binding_still_goes_to_the_card_number(contacts, bridge, monkeypatch):
    args, problem = action_tools.prepare_send_recipient(
        TOOL, {"to": "Brian", "text": "Hi", "resolved_to": FORGED})
    assert problem is None and args["resolved_to"] == PHONE
    assert registry.is_host_bound(args["resolved_to"])
    # Contacts change between the card and the tap; the approved number holds.
    monkeypatch.setattr(imessage_tools, "_name_handles", {"brian chen": ["+15550107777"]})
    result = await registry.run_tool(TOOL, args)
    assert not registry.is_tool_error(result)
    assert bridge == [("send_message", {"to": PHONE, "text": "Hi"})]


async def test_a_binding_that_went_through_json_is_no_longer_the_hosts(contacts, bridge):
    # A stored workflow step, an HTTP body or a model echo is plain JSON.
    args, _ = action_tools.prepare_send_recipient(TOOL, {"to": "Brian", "text": "Hi"})
    echoed = json.loads(json.dumps(args))
    assert echoed == args and not registry.is_host_bound(echoed["resolved_to"])
    result = await registry.run_tool(TOOL, echoed)
    assert registry.is_tool_error(result) and bridge == []


def test_no_model_check_accepts_the_key_and_it_is_never_offered():
    call = {"to": "Brian", "text": "Hi", "resolved_to": registry.host_bound_arg(PHONE)}
    # A model-written call is checked without host_bound: refused even as the host type.
    assert "unexpected argument" in (registry._validate_args(TOOL, call) or "")
    assert not registry.runnable_stub_call(TOOL, {"to": "Brian", "text": "Hi", "resolved_to": PHONE})
    assert "resolved_to" not in TOOL.parameters["properties"]
    assert "resolved_to" not in json.dumps(registry.tool_schemas(["send_message"]))


def test_host_bound_values_are_ordinary_strings_to_everything_else():
    value = registry.host_bound_arg(PHONE)
    assert value == PHONE and isinstance(value, str) and json.dumps(value) == json.dumps(PHONE)
    assert not registry.is_host_bound(PHONE) and not registry.is_host_bound(str(value))


# ---- Through the real agent loop: the model's own resolved_to is never used ----

from service.router.model_led import family_for, registry_specs  # noqa: E402
from tests.test_model_led_entrypoint import endpoint, tool_call  # noqa: E402,F401
from tests.test_send_recipient_preflight import REAL_TOOLS, chat  # noqa: E402,F401


def _schemas():
    family = family_for(registry_specs({"send_message": REAL_TOOLS["send_message"]})[0])
    return [tool_call("get_tool_schemas", families=[family], tools=["send_message"])]


@pytest.mark.parametrize("to, goes_to", [
    ("Phone Only", "+15550100123"),       # a saved name: its own number, not the model's
    ("+15550100199", "+15550100199"),     # a literal number: that number
])
def test_a_model_supplied_number_cannot_choose_the_recipient(chat, to, goes_to):
    # The model adds its own resolved_to, then retries without it.
    chat.script = [_schemas(),
                   [tool_call("send_message", to=to, text="On my way", resolved_to=FORGED)],
                   [tool_call("send_message", to=to, text="On my way")], "Sent."]
    events = chat.request(f"text {to} that I'm on my way", chat.sid)
    refused = [e["result"] for e in events if e["type"] == "tool_result"][0]
    assert "unexpected argument" in refused and "'resolved_to'" in refused
    # One card, for the retry, showing the real number; one send, to that number.
    assert len(chat.cards.shown) == 1
    assert goes_to in chat.cards.shown[0]["preview"] and FORGED not in chat.cards.shown[0]["preview"]
    assert chat.dispatched == [("send_message", {"to": goes_to, "text": "On my way"})]
