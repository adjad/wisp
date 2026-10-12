"""Draft identity regressions: synthetic contacts, scripted model, fake native sink only."""
import asyncio
import json
import importlib
import socket
import subprocess

import pytest

from service.tools import action_tools as A, registry as R

PHONE = "+15555550123"
OTHER = "+15555550456"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No native, network, provider, or personal effects in draft fixtures")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(A, "app_request", forbidden)
    monkeypatch.setattr(A, "_resolve_recipient", forbidden)
    monkeypatch.setattr(A, "_own_address_guard", lambda *a: "")
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda **kw: "")
    monkeypatch.setattr(importlib.import_module("service.safety.audit"), "audit", lambda *a, **kw: None)


def run_card(monkeypatch, *, direct=True, to="Mom", text="Hi Mom", result=None,
             test_mode=False, denied=False):
    from service.agent import loop
    from service.safety.policy import Decision, Tier
    from tests.test_direct_dispatch_exec import ScriptedClient, Approver, _tool_call
    monkeypatch.setattr(A, "_resolve_recipient", lambda *a, **kw: (PHONE, ""))
    if result is not None:
        async def fake(**args):
            return result
        monkeypatch.setitem(R.REGISTRY, "draft_message", R.Tool(
            "draft_message", "synthetic", R.REGISTRY["draft_message"].parameters,
            "messages_draft", fake))
    if denied:
        monkeypatch.setattr(loop, "decide", lambda *a, **kw: Decision(Tier.DENY, "Synthetic denial"))
    client = ScriptedClient([{"tool_calls": [_tool_call("draft_message", {"to": to, "text": text}, "draft-1")]}])
    events = []
    async def emit(event):
        events.append(event)
    answer = asyncio.run(loop.run_agent(
        client, "synthetic-model", [{"role": "user", "content": "Draft a message to Mom"}],
        emit, Approver(), tools=["draft_message"], max_steps=2,
        direct_calls=[("draft_message", {"to": to, "text": text})] if direct else None,
        test_mode=test_mode, include_memory_context=False))
    return answer, client, [e for e in events if e.get("type") == "message_draft"]


@pytest.mark.parametrize("direct", [True, False])
def test_card_uses_prepared_recipient_and_body(monkeypatch, direct):
    answer, client, cards = run_card(monkeypatch, direct=direct, text=r"Hi\nMom")
    assert cards == [{"type": "message_draft", "to": PHONE, "text": "Hi\nMom"}]
    assert answer == "" and len(client.calls) == (0 if direct else 1)


def test_endpoint_retains_displayed_recipient_after_contact_change(monkeypatch):
    from service import main
    monkeypatch.setattr(A, "_resolve_recipient", lambda *a, **kw: (PHONE, ""))
    prepared = asyncio.run(R.run_tool(R.get_tool("draft_message"), {"to": "Mom", "text": "Hi Mom"}))
    calls = []
    def changed(*a, **kw):
        calls.append("contact")
        return OTHER, ""
    async def sink(operation, payload):
        calls.append((operation, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "_resolve_recipient", changed)
    monkeypatch.setattr(A, "app_request", sink)
    assert hasattr(prepared, "resolved_to"), "preparation must retain a host-resolved handle"
    result = asyncio.run(main.assistant_send_message_draft({"to": prepared.resolved_to, "text": "Edited"}))
    assert result["ok"] is True
    assert calls == [("send_message", {"to": PHONE, "text": "Edited"})]


@pytest.mark.parametrize("text", [r"literal\n\t\'\"", "  café\n\t\"quoted\"  ", r"two\\\"quotes"])
def test_real_send_preserves_reviewed_exact_body(monkeypatch, text):
    from service import main
    calls = []
    async def sink(operation, payload):
        calls.append((operation, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "app_request", sink)
    result = asyncio.run(main.assistant_send_message_draft({"to": PHONE, "text": text}))
    assert result["ok"] is True
    assert calls == [("send_message", {"to": PHONE, "text": text})]
    assert A._unreviewed()


@pytest.mark.parametrize("handle", [PHONE, "15555550123", "+1 (555) 555-0123", "(555) 555-0123",
                                   "555.555.0123", "mom+home@synthetic.invalid", " MOM@synthetic.invalid "])
def test_preparation_and_endpoint_share_literal_contract(monkeypatch, handle):
    from service import main
    receipt = asyncio.run(R.run_tool(R.get_tool("draft_message"), {"to": handle, "text": "Hi"}))
    assert R._valid_message_draft(receipt)
    assert receipt.resolved_to == handle.strip(" ")
    calls = []
    async def sink(operation, payload):
        calls.append((operation, payload))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "app_request", sink)
    assert asyncio.run(main.assistant_send_message_draft({"to": handle, "text": "Hi"}))["ok"]
    assert calls == [("send_message", {"to": handle.strip(" "), "text": "Hi"})]


@pytest.mark.parametrize("to", ["Mom", "", " ", None, 1234567890, True, [], {},
                                ".......", "()---...", "+", "123", "1234567,7654321",
                                "a@b.invalid;c@d.invalid", "Mom <mom@synthetic.invalid>",
                                "bad@@synthetic.invalid", ".bad@synthetic.invalid", "bad..name@synthetic.invalid",
                                "a@-bad.invalid", "a@bad-.invalid", "+15555550123\n", "\t+15555550123",
                                "+1555\x005550123", "+15555550123\r\nBcc: other", "1234567890123456", "1234567.", "1234567-", "１２３４５６７", "((1234567))"])
def test_endpoint_rejects_nonliteral_before_any_effect(monkeypatch, to):
    from service import main
    async def forbidden_send(**kwargs):
        raise AssertionError("Rejected payload must not call send_message")
    monkeypatch.setattr(A, "send_message", forbidden_send)
    assert asyncio.run(main.assistant_send_message_draft({"to": to, "text": "Hi"}))["ok"] is False
    assert A._unreviewed()


@pytest.mark.parametrize("text", [None, 42, True, [], {}, "", " \t\n "])
def test_endpoint_rejects_nontext_or_empty_before_effect(monkeypatch, text):
    from service import main
    async def forbidden_send(**kwargs):
        raise AssertionError("Invalid body must not call send_message")
    monkeypatch.setattr(A, "send_message", forbidden_send)
    assert asyncio.run(main.assistant_send_message_draft({"to": PHONE, "text": text}))["ok"] is False
    assert A._unreviewed()


@pytest.mark.parametrize("mode", ["async", "sync", "awaitable"])
def test_registry_preserves_raw_host_receipt_all_dispatch_forms(monkeypatch, mode):
    receipt = asyncio.run(A.draft_message(PHONE, "Hi"))
    async def af(): return receipt
    def sf(): return receipt
    def aw(): return af()
    tool = R.Tool("draft_message", "synthetic", {"type": "object", "properties": {}},
                  "messages_draft", {"async": af, "sync": sf, "awaitable": aw}[mode])
    result = asyncio.run(R.run_tool(tool, {}))
    assert result is receipt and R.classify_tool_outcome(tool.name, result).status == "succeeded"
    tool.name = "fake_other"
    other = asyncio.run(R.run_tool(tool, {}))
    assert type(other) is str and not R._valid_message_draft(other, tool.name)


def test_receipt_is_immutable_and_loses_authority_when_serialized():
    receipt = asyncio.run(A.draft_message(PHONE, "Hi"))
    assert type(receipt) is R._PreparedMessageDraft
    for field in ("resolved_to", "prepared_text", "draftorigin", "_resolved_to"):
        with pytest.raises(AttributeError): setattr(receipt, field, "changed")
        with pytest.raises(AttributeError): delattr(receipt, field)
    with pytest.raises(ValueError):
        R._PreparedMessageDraft(resolved_to=PHONE, prepared_text="Hi")
    for result in (str(receipt), receipt[:], json.dumps({"resolved_to": PHONE, "prepared_text": "Hi"}),
                   json.dumps(str(receipt))):
        assert not R._valid_message_draft(result)
        assert R.classify_tool_outcome("draft_message", result).status == "failed"
    assert not R._valid_message_draft(receipt, "send_message")


@pytest.mark.parametrize("corruption", ["origin", "recipient", "recipient_none", "body", "body_none", "missing", "subclass"])
def test_corrupted_or_subclass_receipt_is_not_success(corruption):
    receipt = asyncio.run(A.draft_message(PHONE, "Hi"))
    if corruption == "subclass":
        class Lookalike(R._PreparedMessageDraft): pass
        receipt = str.__new__(Lookalike, str(receipt))
    elif corruption == "missing":
        object.__delattr__(receipt, "_draftorigin")
    else:
        field, value = {"origin": ("_draftorigin", object()), "recipient": ("_resolved_to", "Mom"),
                        "recipient_none": ("_resolved_to", None), "body": ("_prepared_text", " "),
                        "body_none": ("_prepared_text", None)}[corruption]
        # Deliberately emulate trusted host corruption; normal setters are blocked.
        object.__setattr__(receipt, field, value)
    assert not R._valid_message_draft(receipt)
    assert R.classify_tool_outcome("draft_message", receipt).status == "failed"


@pytest.mark.parametrize("direct", [True, False])
@pytest.mark.parametrize("fake", ["Message draft prepared in Wisp for Mom — nothing has been sent.",
                                 '{"draftorigin":"host","resolved_to":"+15555550123","prepared_text":"Hi"}',
                                 "(error: preparation failed)"])
def test_plain_prose_json_failure_cannot_emit_card(monkeypatch, direct, fake):
    answer, _, cards = run_card(monkeypatch, direct=direct, result=fake)
    assert cards == [] and "verified success" in answer


@pytest.mark.parametrize("direct", [True, False])
@pytest.mark.parametrize("mode", ["denied", "planned"])
def test_denial_and_planning_never_emit_card(monkeypatch, direct, mode):
    receipt = asyncio.run(A.draft_message(PHONE, "User denied this action (not sent) no matches which one?"))
    assert R.classify_tool_outcome("draft_message", receipt).status == "succeeded"
    assert R.classify_tool_outcome("draft_message", receipt, denied=True).status == "denied"
    assert R.classify_tool_outcome("draft_message", receipt, planned=True).status == "planned"
    _, _, cards = run_card(monkeypatch, direct=direct, result=receipt,
                            test_mode=mode == "planned", denied=mode == "denied")
    assert cards == []


@pytest.mark.parametrize("preferred,problem", [("Mom", ""), (None, ""), ("a@b.invalid,c@d.invalid", ""),
                                             ("", "Which one? Ask the user."), ("", "(error: missing contact)")])
def test_preparation_failures_never_mint_receipt(monkeypatch, preferred, problem):
    monkeypatch.setattr(A, "_resolve_recipient", lambda *a, **kw: (preferred, problem))
    result = asyncio.run(R.run_tool(R.get_tool("draft_message"), {"to": "Mom", "text": "Hi"}))
    assert not R._valid_message_draft(result)
    assert R.classify_tool_outcome("draft_message", result).status != "succeeded"


@pytest.mark.parametrize("to,text", [(None, "Hi"), (PHONE, None), (PHONE, " "), ("", "Hi")])
def test_direct_preparation_invalid_inputs_are_not_receipts(to, text):
    assert not R._valid_message_draft(asyncio.run(A.draft_message(to, text)))


def test_unreviewed_normalization_is_unchanged(monkeypatch):
    calls = []
    async def sink(operation, payload):
        calls.append(payload)
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "app_request", sink)
    result = asyncio.run(A.send_message(PHONE, r"Hello\nthere\tfriend"))
    assert result.startswith("Message sent to ")
    assert calls == [{"to": PHONE, "text": "Hello\nthere\tfriend"}]


def test_endpoint_reviewed_context_resets_after_exception(monkeypatch):
    from service import main
    async def broken(operation, payload):
        assert not A._unreviewed()
        raise RuntimeError("synthetic sink exception")
    monkeypatch.setattr(A, "app_request", broken)
    async def scenario():
        with pytest.raises(RuntimeError, match="synthetic sink"):
            await main.assistant_send_message_draft({"to": PHONE, "text": r"Hi\nthere"})
        assert A._unreviewed()
    asyncio.run(scenario())
    assert A._unreviewed()


def test_reviewed_context_is_isolated_across_concurrent_sends(monkeypatch):
    from service import main
    calls = []
    async def sink(operation, payload):
        calls.append((payload, A._unreviewed()))
        await asyncio.sleep(0)
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "app_request", sink)
    async def scenario():
        await asyncio.gather(main.assistant_send_message_draft({"to": PHONE, "text": r"Reviewed\nbody"}),
                             A.send_message(OTHER, r"Unreviewed\nbody"))
        assert A._unreviewed()
    asyncio.run(scenario())
    assert calls == [({"to": PHONE, "text": r"Reviewed\nbody"}, False),
                     ({"to": OTHER, "text": "Unreviewed\nbody"}, True)]


def test_endpoint_preserves_unknown_result_without_retry(monkeypatch):
    from service import main
    calls = []
    async def sink(operation, payload):
        calls.append((operation, payload))
        return {"ok": False, "status": "unknown", "action_id": "synthetic-send", "dispatch_started": True}
    monkeypatch.setattr(A, "app_request", sink)
    result = asyncio.run(main.assistant_send_message_draft({"to": PHONE, "text": r"Hi\nthere"}))
    assert result["ok"] is False and len(calls) == 1
    assert type(result["result"]) is R._OutboundUnknownResult
    assert R.classify_tool_outcome("send_message", result["result"]).status == "unknown"
    assert "not sent" not in result["result"].lower()
    assert not R._valid_message_draft(result["result"])
    assert A._unreviewed()


def test_self_address_guard_still_prevents_native_dispatch(monkeypatch):
    from service import main
    monkeypatch.setattr(A, "_own_address_guard", lambda *a: "(NOT sent — confirm self-send first)")
    result = asyncio.run(main.assistant_send_message_draft({"to": "me@synthetic.invalid", "text": "Hi"}))
    assert result["ok"] is False and "confirm self-send" in result["result"]
    assert A._unreviewed()


@pytest.mark.parametrize("direct", [True, False])
@pytest.mark.parametrize("corruption", ["origin", "recipient", "body", "subclass"])
def test_invalid_raw_receipts_cannot_emit_card(monkeypatch, direct, corruption):
    receipt = asyncio.run(A.draft_message(PHONE, "Hi"))
    if corruption == "subclass":
        class Lookalike(R._PreparedMessageDraft): pass
        receipt = str.__new__(Lookalike, str(receipt))
    else:
        field, value = {"origin": ("_draftorigin", object()), "recipient": ("_resolved_to", "Mom"),
                        "body": ("_prepared_text", "")}[corruption]
        object.__setattr__(receipt, field, value)
    answer, _, cards = run_card(monkeypatch, direct=direct, result=receipt)
    assert not cards and "verified success" in answer


def test_real_string_required_at_endpoint(monkeypatch):
    from service import main
    class StringLike(str): pass
    async def forbidden_send(**kwargs):
        raise AssertionError("String subclasses are not wire strings")
    monkeypatch.setattr(A, "send_message", forbidden_send)
    for payload in ({"to": StringLike(PHONE), "text": "Hi"},
                    {"to": PHONE, "text": StringLike("Hi")}):
        assert asyncio.run(main.assistant_send_message_draft(payload))["ok"] is False


@pytest.mark.parametrize("direct", [True, False])
def test_complete_card_then_send_freezes_handle_and_edited_body(monkeypatch, direct):
    from service import main
    _, _, cards = run_card(monkeypatch, direct=direct)
    assert len(cards) == 1 and cards[0]["to"] == PHONE
    def changed(*args, **kwargs):
        raise AssertionError("Contact change must not cause re-resolution at Send")
    monkeypatch.setattr(A, "_resolve_recipient", changed)
    accepted = []
    async def sink(operation, payload):
        accepted.append((operation, dict(payload)))
        return {"ok": True, "status": "succeeded"}
    monkeypatch.setattr(A, "app_request", sink)
    edited = "  Edited literal \\n with café\n\tand quotes \\\".  "
    result = asyncio.run(main.assistant_send_message_draft({"to": cards[0]["to"], "text": edited}))
    assert result["ok"] is True
    assert accepted == [("send_message", {"to": PHONE, "text": edited})]
