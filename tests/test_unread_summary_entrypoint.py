"""Unread presentation intent through the actual streaming entrypoint."""
import asyncio
import json
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

# The shared inert /agent harness: every registry tool is a recorder, the model
# client is a fake that captures each inference input, and nothing opens a socket.
from tests.test_fast_path_intent import inert_endpoint  # noqa: F401

# A fully admitted labeled contact receipt (agent.loop._contact_receipt_destination
# accepts exactly this shape; a bare address alone is not host evidence).
CONTACT_RECEIPT = "Mom: recipient@example.invalid"
DIGEST = "SYNTHETIC-DIGEST-7: 3 emails, 1 unread."
OUTBOUND_TOOL = re.compile(
    r"send_\w+|draft_\w+|reply_to_\w+|forward_\w+|schedule_send|cancel_scheduled_send|lookup_contact|list_contacts")
FORCED_LOOKUP = "You must call the lookup_contact tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,expected", [
    ("Summarize my unread mail", [("summarize_emails", {"unread": True})]),
    ("Please summarise our unread texts from yesterday", [("summarize_messages", {"unread": True, "day": "yesterday"})]),
    ("Catch me up on unread email.", [("summarize_emails", {"unread": True})]),
    ("Summarize unread email and messages today", [
        ("summarize_emails", {"unread": True, "day": "today"}),
        ("summarize_messages", {"unread": True, "day": "today"})]),
    ("email summary", [("summarize_emails", {})]),
    ("text summary", [("summarize_messages", {})]),
    ("Show unread mail", [("view_emails", {"unread": True})]),
    ("Read unread messages yesterday", [("view_messages", {"unread": True, "day": "yesterday"})]),
    ("List unread email and messages", [
        ("view_emails", {"unread": True}), ("view_messages", {"unread": True})]),
])
async def test_unread_stream_preserves_summary_or_raw_presentation(tmp_path, monkeypatch, prompt, expected):
    from service import main
    from service.memory.store import SessionStore
    from service.assistant.store import AssistantStore
    from service.workflows import reads
    from service.safety.policy import Decision, Tier

    sessions = SessionStore(tmp_path / "sessions.sqlite")
    assistant = AssistantStore(tmp_path / "assistant.sqlite")
    monkeypatch.setattr(main, "store", sessions)
    monkeypatch.setattr(main, "assistant_store", assistant)
    monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    monkeypatch.setattr(main, "ensure_omlx", AsyncMock(side_effect=AssertionError("unexpected inference")))
    monkeypatch.setattr(main, "route", AsyncMock(side_effect=AssertionError("unexpected routing")))
    monkeypatch.setattr(reads, "decide", lambda *a, **k: Decision(Tier.ALLOW, "synthetic read"))
    # A bare summary is a read: no contact lookup or send may be attempted.
    # Policy is ALLOW and the synthetic contact is a valid email, so a send
    # attempt could not hide behind an invalid-recipient refusal.
    forbidden = []
    calls = []
    async def synthetic_read(tool, args):
        if tool.name in {"lookup_contact", "send_email", "send_message"}:
            forbidden.append((tool.name, args))
            return CONTACT_RECEIPT
        calls.append((tool.name, args))
        return ("Bounded unread digest; coverage incomplete." if tool.name.startswith("summarize_")
                else "RAW BODY: synthetic private detail; Message-ID: synthetic-id")
    monkeypatch.setattr(reads, "run_tool", synthetic_read)
    try:
        response = await main.agent({"prompt": prompt, "debug": False})
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        events = [json.loads(line[6:]) for line in "".join(chunks).splitlines() if line.startswith("data: ")]
        assert calls == expected
        assert forbidden == []
        answer = "".join(e.get("text", "") for e in events if e["type"] == "text")
        if expected[0][0].startswith("summarize_"):
            assert "Bounded unread digest" in answer and "coverage incomplete" in answer
            assert "RAW BODY" not in answer and "Message-ID" not in answer
        else:
            assert "RAW BODY" in answer
        assert events[-1]["type"] == "done"
        main.ensure_omlx.assert_not_awaited()
        main.route.assert_not_awaited()
    finally:
        sessions._db.close()
        assistant._db.close()


def _outbound_names():
    """Canonical contact/send/draft/forward/schedule tools plus THEIR aliases.

    An alias of a read tool (summarize_emails answers "anything to reply to")
    is not an outbound effect, so aliases count only for outbound tools.
    """
    from service.tools.registry import REGISTRY
    canonical = {name for name in REGISTRY if OUTBOUND_TOOL.fullmatch(name)}
    return canonical | {alias for name in canonical for alias in REGISTRY[name].aliases}


async def _post(prompt, session_id=None):
    """Actual ASGI POST /agent: no socket, no lifespan, original handler."""
    from service import main
    body = {"prompt": prompt, "debug": False}
    if session_id:
        body["session_id"] = session_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://fixture.invalid") as client:
        response = await client.post("/agent", json=body)
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert not [e for e in events if e["type"] == "error"], events
    assert events[-1]["type"] == "done"
    return events


def _text(events):
    return "".join(e.get("text", "") for e in events if e["type"] == "text")


@pytest.fixture
def world(inert_endpoint, monkeypatch, tmp_path):  # noqa: F811
    """Inert /agent plus synthetic ALLOW fakes at every outbound boundary."""
    from service import main
    from service.tools import imessage_tools
    from service.tools.registry import REGISTRY
    from service.safety.policy import Decision, Tier
    from service.workflows import executor

    _request, streams, calls = inert_endpoint
    monkeypatch.delenv("WISP_MODEL_LED_ROUTING", raising=False)
    previews = []

    def fake(name, result):
        async def tool(**kwargs):
            calls.append((name, kwargs))
            return result
        monkeypatch.setitem(REGISTRY, name, replace(REGISTRY[name], func=tool))

    fake("summarize_emails", DIGEST)
    fake("summarize_messages", DIGEST)
    fake("get_upcoming", "SYNTHETIC-EVENT-TOMORROW: Standup 09:00")
    fake("search_notes", "SYNTHETIC-NOTE-TOMORROW: buy milk")
    fake("lookup_contact", CONTACT_RECEIPT)
    fake("send_email", "Email sent to recipient@example.invalid.")
    fake("send_message", "Message sent to recipient@example.invalid.")
    monkeypatch.setattr(imessage_tools, "find_contacts", lambda name: [
        {"name": "Mom", "handles": ["recipient@example.invalid"],
         "preferred": "recipient@example.invalid"}])
    allow = lambda *_a, **_k: Decision(Tier.ALLOW, "synthetic only")  # noqa: E731
    monkeypatch.setattr(executor, "decide", allow)

    class Approver:
        def __init__(self, emit):
            pass

        async def confirm(self, action):
            previews.append(action)
            return True

    monkeypatch.setattr(main, "InteractiveApprover", Approver)

    class World:
        pass
    w = World()
    w.streams, w.calls, w.previews = streams, calls, previews
    w.outbound = lambda: [(n, a) for n, a in calls if n in _outbound_names()]
    return w


@pytest.mark.parametrize("prompt,summary_tool", [
    ("email summary", "summarize_emails"), ("Email summary", "summarize_emails"),
    ("text summary", "summarize_messages"), ("Text summary", "summarize_messages"),
])
def test_http_bare_summary_is_a_read_with_no_contact_send_or_followup(world, prompt, summary_tool):
    events = asyncio.run(_post(prompt))
    assert [n for n, _ in world.calls] == [summary_tool]
    assert world.outbound() == [] and world.previews == []
    answer = _text(events)
    assert "SYNTHETIC-DIGEST-7" in answer
    for canned in ("didn't send", "did not send", "couldn't tell exactly what to send",
                   "Messages or email", "Who should I"):
        assert canned not in answer, answer
    # Default (unset) routing answers this from the read shortcut: no inference
    # and no routing at all. This says nothing about model-led routing.
    assert world.streams == []
    for stream in world.streams:
        assert FORCED_LOOKUP not in json.dumps(stream["messages"])


@pytest.mark.parametrize("prompt", ["email summary", "text summary"])
def test_bare_summary_route_has_no_forced_lookup_or_send(monkeypatch, prompt):
    from service.router import router as R
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")  # the explicit legacy route
    decision = asyncio.run(R.route(prompt))
    assert not decision.reason.startswith("grounded outbound")
    outbound = _outbound_names()
    assert not set(decision.tool_subset or []) & outbound
    assert not any(set(group) & outbound for group in decision.required_tool_groups)
    assert decision.force_first_tool not in outbound and not decision.resolved_request
    assert {"summarize_emails", "summarize_messages"} & set(decision.tool_subset or [])


def test_genuine_send_reaches_the_sink_exactly_once_with_bound_recipient_and_payload(world):
    events = asyncio.run(_post("email Mom a summary of my unread emails"))
    sends = [kwargs for name, kwargs in world.calls if name == "send_email"]
    assert len(sends) == 1
    assert sends[0]["to"] == "recipient@example.invalid"
    assert "SYNTHETIC-DIGEST-7" in sends[0]["body"]
    assert [n for n, _ in world.calls if n != "send_email"] == ["summarize_emails"]
    assert [n for n, _ in world.calls].index("summarize_emails") < [n for n, _ in world.calls].index("send_email")
    assert len(world.previews) == 1 and world.previews[0]["args"] == sends[0]
    resolved = [e for e in events if e["type"] == "workflow" and e.get("event") == "recipient_resolved"]
    assert [(e["requested_recipient"], e["resolved_recipient"], e["channel"]) for e in resolved] == [
        ("Mom", "recipient@example.invalid", "email")]


def test_genuine_send_route_still_forces_the_grounded_outbound_plan(monkeypatch):
    from service.router import router as R
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    decision = asyncio.run(R.route("email summary to Mom"))
    assert decision.reason.startswith("grounded outbound report")
    assert "send_email" in decision.tool_subset and "summarize_emails" in decision.tool_subset


@pytest.mark.parametrize("prompt", ["what is up for today", "what is up for tomorrow",
                                    "What's up for me today?"])
def test_personal_agenda_is_not_a_public_web_lookup(monkeypatch, prompt):
    from service.router import router as R
    from service.router import web_request
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    assert web_request.classify(prompt).allowed is False
    decision = asyncio.run(R.route(prompt))
    assert not any(name == "web_search" for name, _ in decision.direct_calls)
    assert frozenset({"web_search"}) not in decision.required_tool_groups
    assert "web_search on the agent model" not in decision.reason


def test_tomorrow_agenda_is_grounded_in_the_calendar(monkeypatch):
    from service.router import router as R
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    decision = asyncio.run(R.route("what is up for tomorrow"))
    assert [name for name, _ in decision.direct_calls] == ["get_upcoming"]
    assert decision.direct_calls[0][1]["period"] == "tomorrow"


@pytest.mark.parametrize("prompt", ["what is up with the Senate vote today",
                                    "what's the latest news today"])
def test_public_subject_today_still_goes_to_the_web(monkeypatch, prompt):
    from service.router import router as R
    from service.router import web_request
    monkeypatch.setenv("WISP_MODEL_LED_ROUTING", "0")
    assert web_request.classify(prompt).allowed is True
    decision = asyncio.run(R.route(prompt))
    assert [name for name, _ in decision.direct_calls] == ["web_search"]
    assert frozenset({"web_search"}) in decision.required_tool_groups


def test_http_stale_channel_only_text_it_does_not_capture_the_next_read(world):
    from service import main
    from service.workflows.compiler import CONTENT_QUESTION
    first = asyncio.run(_post("text it"))
    sid = next(e["id"] for e in first if e["type"] == "session")
    assert "couldn't tell exactly what to send" in _text(first)
    held = main.store.latest_workflow(sid, max_age_seconds=21600)
    assert (held["status"], held["channel"], held["recipient"]) == ("waiting_for_content", "messages", "")
    assert world.calls == []

    second = asyncio.run(_post("what do I have to do tmrow", sid))
    names = [n for n, _ in world.calls]
    assert "get_upcoming" in names
    assert set(names) <= {"get_upcoming", "summarize_messages", "summarize_emails", "search_notes"}
    assert world.outbound() == [] and world.previews == []
    evidence = json.dumps(second) + json.dumps(world.streams)
    assert "SYNTHETIC-EVENT-TOMORROW" in evidence
    assert CONTENT_QUESTION not in _text(second) and "couldn't tell exactly" not in _text(second)
    released = main.store.latest_workflow(sid, max_age_seconds=21600)
    assert (released["id"], released["status"]) == (held["id"], "superseded")
    assert main.store.active_workflow(sid) is None
    offered = {tool["function"]["name"] for stream in world.streams for tool in stream.get("tools", [])}
    assert not offered & _outbound_names()
    for stream in world.streams:
        assert FORCED_LOOKUP not in json.dumps(stream["messages"])
