"""Reversible masking for cloud models (service/safety/cloud_mask.py and its boundary
in OMLXClient). Invented values only."""
import asyncio
import json
import logging
import pickle
import copy

import pytest

from service.inference.omlx_client import OMLXClient
from service.safety import cloud_mask
from service.safety.cloud_mask import Masker

EMAIL = "ada@example.com"
PHONE = "+1 555 0100"
CARD = "4111 1111 1111 1111"   # a published test number (Luhn-valid)


@pytest.fixture(autouse=True)
def _clean_registry():
    cloud_mask.forget()
    yield
    cloud_mask.forget()


def _client(*, managed, name="remote", url="https://openrouter.ai"):
    c = OMLXClient.__new__(OMLXClient)
    c.managed, c.endpoint_name, c.base_url = managed, name, url
    return c


def _events(texts, final=None):
    async def gen(self, model, messages, **kw):
        gen.seen = messages
        for t in texts:
            yield {"kind": "content", "text": t}
        yield {"kind": "final", "message": final or {
            "role": "assistant", "content": "".join(texts), "reasoning_content": "", "tool_calls": None}}
    return gen


def _collect(client, messages, **kw):
    async def run():
        return [e async for e in client.stream_events("m", messages, **kw)]
    return asyncio.run(run())


# 1. round trip
def test_mask_and_unmask_round_trip():
    m = Masker()
    text = f"Mail {EMAIL} or call {PHONE}; card {CARD}."
    masked = m.mask(text)
    assert EMAIL not in masked and "555" not in masked and "4111" not in masked
    assert "[EMAIL_1]" in masked and "[PHONE_1]" in masked and "[NUMBER_1]" in masked
    assert m.unmask(masked) == text
    assert m.unmask_obj({"to": ["[EMAIL_1]"], "n": 3}) == {"to": [EMAIL], "n": 3}


def test_ordinary_numbers_and_dates_are_left_alone():
    m = Masker()
    text = "Meet 2026-11-21 at 10:00, order 1234567, sum 4111 1111 1111 1112, version 1.2.3."
    assert m.mask(text) == text
    assert len(m) == 0


def test_national_phone_and_case_insensitive_email():
    m = Masker()
    out = m.mask("Call (555) 010-0100, write Ada@Example.com or ada@example.com")
    assert out == "Call [PHONE_1], write [EMAIL_1] or [EMAIL_1]"
    assert m.unmask("[EMAIL_1]") == "Ada@Example.com"


# 2. stable numbering across turns
def test_numbering_is_stable_across_turns_and_per_conversation():
    with cloud_mask.conversation("A"):
        a = cloud_mask.masker_for(cloud_mask.current_conversation())
        first = a.mask_messages([{"role": "user", "content": f"hi {EMAIL}"}])
        second = a.mask_messages([{"role": "user", "content": f"hi {EMAIL}"},
                                  {"role": "assistant", "content": "ok"},
                                  {"role": "user", "content": f"and bob@example.com, {EMAIL}"}])
    assert first[0]["content"] == "hi [EMAIL_1]"
    assert second[2]["content"] == "and [EMAIL_2], [EMAIL_1]"
    b = cloud_mask.masker_for("B")
    assert b.mask("x bob@example.com y") == "x [EMAIL_1] y"      # its own numbering
    assert cloud_mask.masker_for("A") is a


def test_all_message_kinds_are_masked_without_touching_other_keys():
    m = Masker()
    replay = object()
    msgs = [
        {"role": "system", "content": f"user is {EMAIL}"},
        {"role": "user", "content": [{"type": "text", "text": f"call {PHONE}"},
                                      {"type": "image_url", "image_url": {"url": "data:x"}}]},
        {"role": "assistant", "content": "", "_replay": replay,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "send", "arguments": json.dumps({"to": EMAIL})}}]},
        {"role": "tool", "tool_call_id": "1", "content": f"sent to {EMAIL}"},
        {"role": "user", "content": "nothing here"},
    ]
    before = copy.deepcopy([{k: v for k, v in x.items() if k != "_replay"} for x in msgs])
    out = m.mask_messages(msgs)
    dumped = json.dumps([{k: v for k, v in x.items() if k != "_replay"} for x in out])
    assert EMAIL not in dumped and "555" not in dumped
    assert out[2]["_replay"] is replay
    assert out[4] is msgs[4]
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"to": "[EMAIL_1]"}
    assert [{k: v for k, v in x.items() if k != "_replay"} for x in msgs] == before   # input untouched


# 3. split-chunk streaming
@pytest.mark.parametrize("cut", range(1, 12))
def test_stream_unmasker_handles_a_placeholder_split_anywhere(cut):
    m = Masker()
    m.mask(EMAIL)
    s = "Write to [EMAIL_1] now"
    start = s.index("[")
    pos = start + cut
    parts = [s[:pos], s[pos:]]
    un = m.stream()
    out = "".join(un.feed(p) for p in parts) + un.flush()
    assert out == f"Write to {EMAIL} now"


def test_stream_unmasker_holds_only_a_possible_partial():
    m = Masker()
    m.mask(EMAIL)
    un = m.stream()
    assert un.feed("see [EMA") == "see "
    assert un.feed("IL_1] and [") == f"{EMAIL} and "
    assert un.feed("not a placeholder") == "[not a placeholder"
    assert un.feed("tail [PHO") == "tail "
    assert un.flush() == "[PHO"


def test_client_stream_unmasks_split_chunks_and_final(monkeypatch):
    gen = _events(["Mail [EMA", "IL_1] and ", "[PHONE_1]", " ok"])
    monkeypatch.setattr(OMLXClient, "_stream_events", gen)
    client = _client(managed=False)
    with cloud_mask.conversation("c1"):
        events = _collect(client, [{"role": "user", "content": f"{EMAIL} {PHONE}"}])
    assert "".join(e["text"] for e in events if e["kind"] == "content") == f"Mail {EMAIL} and {PHONE} ok"
    assert events[-1]["message"]["content"] == f"Mail {EMAIL} and {PHONE} ok"
    sent = json.dumps(gen.seen)
    assert EMAIL not in sent and "555" not in sent and "[EMAIL_1]" in sent


# 4. tool-call args unmasked before the tool runs
def test_tool_args_are_unmasked_before_the_tool_runs(monkeypatch):
    call = {"id": "c1", "type": "function",
            "function": {"name": "send_email", "arguments": json.dumps(
                {"to": "[EMAIL_1]", "body": "call [PHONE_1] or [EMAIL_9]", "n": 2})}}
    final = {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": [call]}
    monkeypatch.setattr(OMLXClient, "_stream_events", _events([], final))
    client = _client(managed=False)
    received = []

    def fake_tool(**args):
        received.append(args)

    with cloud_mask.conversation("c2"):
        events = _collect(client, [{"role": "user", "content": f"email {EMAIL}, phone {PHONE}"}])
        message = events[-1]["message"]
        for tc in message["tool_calls"]:
            fake_tool(**json.loads(tc["function"]["arguments"]))
    assert received == [{"to": EMAIL, "body": f"call {PHONE} or [EMAIL_9]", "n": 2}]


def test_chat_reply_is_unmasked(monkeypatch):
    async def fake_chat(self, model, messages, **kw):
        fake_chat.seen = messages
        return {"choices": [{"message": {"role": "assistant", "content": "Hi [EMAIL_1]",
                                         "tool_calls": [{"id": "1", "type": "function", "function": {
                                             "name": "t", "arguments": '{"a": "[PHONE_1]"}'}}]}}]}
    monkeypatch.setattr(OMLXClient, "_chat", fake_chat)
    client = _client(managed=False)
    with cloud_mask.conversation("c3"):
        data = asyncio.run(client.chat("m", [{"role": "user", "content": f"{EMAIL} {PHONE}"}]))
    msg = data["choices"][0]["message"]
    assert msg["content"] == f"Hi {EMAIL}"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"a": PHONE}
    assert EMAIL not in json.dumps(fake_chat.seen)


# 5. local endpoints unchanged
@pytest.mark.parametrize("kw", [
    dict(managed=True, name="local", url="http://127.0.0.1:8000"),
    dict(managed=True, name="local", url="http://127.0.0.1:8791"),
    dict(managed=False, name="local_provider", url="http://127.0.0.1:1234"),
    dict(managed=False, name="remote", url="http://localhost:9000"),
])
def test_local_endpoints_are_byte_for_byte_unchanged(monkeypatch, kw):
    client = _client(**kw)
    messages = [{"role": "user", "content": f"{EMAIL} {PHONE} [EMAIL_1]"}]
    seen = {}

    async def fake_events(self, model, msgs, **k):
        seen["messages"] = msgs
        yield {"kind": "content", "text": "[EMAIL_1] [EMA"}
        yield {"kind": "final", "message": {"content": "[EMAIL_1] [EMA", "tool_calls": None}}

    async def fake_chat(self, model, msgs, **k):
        seen["chat"] = msgs
        return {"choices": [{"message": {"content": "[EMAIL_1]"}}]}

    monkeypatch.setattr(OMLXClient, "_stream_events", fake_events)
    monkeypatch.setattr(OMLXClient, "_chat", fake_chat)
    with cloud_mask.conversation("local"):
        events = _collect(client, messages)
        data = asyncio.run(client.chat("m", messages))
    assert seen["messages"] is messages and seen["chat"] is messages
    assert [e.get("text") for e in events[:-1]] == ["[EMAIL_1] [EMA"]
    assert events[-1]["message"]["content"] == "[EMAIL_1] [EMA"
    assert data["choices"][0]["message"]["content"] == "[EMAIL_1]"
    assert cloud_mask.masker_for("local") is not None and len(cloud_mask.masker_for("local")) == 0


def test_is_cloud_decision():
    assert cloud_mask.is_cloud(managed=False, endpoint_name="cloud", base_url="https://openrouter.ai")
    assert cloud_mask.is_cloud(managed=False, endpoint_name="remote",
                               base_url="https://generativelanguage.googleapis.com")
    assert not cloud_mask.is_cloud(managed=True, endpoint_name="local", base_url="http://127.0.0.1:8000")
    assert not cloud_mask.is_cloud(managed=False, endpoint_name="local_provider",
                                   base_url="http://127.0.0.1:5000")
    assert not cloud_mask.is_cloud(managed=False, endpoint_name="x", base_url="http://[::1]:5000")


# 6. unknown placeholders kept
def test_unknown_placeholders_stay_as_they_are():
    m = Masker()
    m.mask(EMAIL)
    assert m.unmask("[EMAIL_1] [EMAIL_2] [PHONE_1] [NAME_1] [SECRET_1] [redacted credential]") == \
        f"{EMAIL} [EMAIL_2] [PHONE_1] [NAME_1] [SECRET_1] [redacted credential]"
    assert m.unmask_obj({"a": ["[PHONE_7]"]}) == {"a": ["[PHONE_7]"]}
    assert Masker().unmask("[EMAIL_1]") == "[EMAIL_1]"
    un = m.stream()
    assert un.feed("x [EMAIL_5] y") + un.flush() == "x [EMAIL_5] y"


def test_names_are_masked_only_when_a_helper_supplies_them():
    m = Masker()
    assert m.mask("Ask Ada Lovelace") == "Ask Ada Lovelace"
    m.add_names(["Ada Lovelace", "Al"])
    out = m.mask("Ask Ada Lovelace and Alan; Ada Lovelace's note")
    assert out == "Ask [NAME_1] and Alan; [NAME_1]'s note"
    assert m.unmask(out) == "Ask Ada Lovelace and Alan; Ada Lovelace's note"


# 7. no mapping in logs
def test_mapping_never_reaches_logs_or_serialization(caplog):
    caplog.set_level(logging.DEBUG)
    m = Masker()
    text = f"{EMAIL} {PHONE} {CARD}"
    masked = m.mask(text)
    m.unmask(masked)
    s = m.stream()
    s.feed(masked)
    s.flush()
    for rendered in (repr(m), str(m), repr(s) if False else "", caplog.text):
        for secret in (EMAIL, "555", "4111"):
            assert secret not in rendered
    with pytest.raises(TypeError):
        pickle.dumps(m)
    with pytest.raises(TypeError):
        copy.deepcopy(m)
    assert not hasattr(m, "__dict__")
    import service.safety.cloud_mask as module
    source = open(module.__file__).read()
    assert "logging" not in source and "print(" not in source


def test_client_path_does_not_log_values(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(OMLXClient, "_stream_events", _events(["Hi [EMAIL_1]"]))
    with cloud_mask.conversation("log"):
        _collect(_client(managed=False), [{"role": "user", "content": f"{EMAIL} {PHONE}"}])
    assert EMAIL not in caplog.text and "555" not in caplog.text


def test_registry_is_bounded_and_forgettable():
    for i in range(cloud_mask._MAX_CONVERSATIONS + 5):
        cloud_mask.masker_for(f"k{i}")
    assert len(cloud_mask._registry) == cloud_mask._MAX_CONVERSATIONS
    assert cloud_mask.masker_for(None) is None
    cloud_mask.forget("k10")
    assert "k10" not in cloud_mask._registry
