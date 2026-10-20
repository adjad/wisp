"""Unread presentation intent through the actual streaming entrypoint."""
import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,expected", [
    ("Summarize my unread mail", [("summarize_emails", {"unread": True})]),
    ("Please summarise our unread texts from yesterday", [("summarize_messages", {"unread": True, "day": "yesterday"})]),
    ("Catch me up on unread email.", [("summarize_emails", {"unread": True})]),
    ("Summarize unread email and messages today", [
        ("summarize_emails", {"unread": True, "day": "today"}),
        ("summarize_messages", {"unread": True, "day": "today"})]),
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
    calls = []
    async def synthetic_read(tool, args):
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
