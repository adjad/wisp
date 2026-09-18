"""Regression coverage for Ling reasoning on user-visible summaries."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from service.assistant import brief
from service.config import no_thinking_kwargs, user_facing_summary_kwargs
from service.tools import imessage_tools


LING = "Ling-3.0-tiny-oQ4e"
OTHER_THINKING_CAPABLE = "Agents-A1-4B-oQe6"


def test_user_facing_summary_kwargs_exempts_ling_without_changing_global_helper():
    assert user_facing_summary_kwargs(LING) == {}
    assert no_thinking_kwargs(LING) == {
        "chat_template_kwargs": {"enable_thinking": False}}
    assert user_facing_summary_kwargs(OTHER_THINKING_CAPABLE) == {
        "chat_template_kwargs": {"enable_thinking": False}}


def test_messages_summary_keeps_ling_thinking(monkeypatch):
    chat = AsyncMock(return_value={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"0": ["dinner"]})}}]})
    client = type("Client", (), {"chat": chat})()
    monkeypatch.setattr(imessage_tools, "_c", lambda: client)
    monkeypatch.setattr(imessage_tools, "role_to_model", lambda _role: LING)

    output = asyncio.run(imessage_tools._summarize(
        [(1, "Alex", "Alex: Let’s have dinner tonight.")], "today"))

    assert "Messages digest" in output
    assert "enable_thinking" not in chat.await_args.kwargs.get("chat_template_kwargs", {})


def test_daily_brief_messages_rundown_keeps_ling_thinking(monkeypatch):
    chat = AsyncMock(return_value={"choices": [{"message": {"content": "Alex wants dinner."}}]})
    client = SimpleNamespace(ensure_only=AsyncMock(), chat=chat)
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(brief, "role_to_model", lambda _role: LING)
    monkeypatch.setattr(brief, "_messages_block", lambda: "[Alex -> you] Dinner tonight.")
    monkeypatch.setattr("service.memory.identity.identity_prompt_block", lambda: "")

    assert asyncio.run(brief._messages_rundown(0)) == "Alex wants dinner."
    assert "enable_thinking" not in chat.await_args.kwargs.get("chat_template_kwargs", {})
