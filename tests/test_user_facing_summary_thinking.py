"""Regression coverage for Ling reasoning on user-visible summaries."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from service.assistant import brief
from service.config import no_thinking_kwargs, user_facing_summary_kwargs
from service.tools import email_tools, imessage_tools


LING = "Ling-3.0-tiny-oQ4e"
OTHER_THINKING_CAPABLE = "Agents-A1-4B-oQe6"


def _header(ts: float, name: str, address: str, subject: str) -> str:
    return "\x01".join(["H2", str(ts), "U", "Personal", "account-1", name,
                          address, f"<{ts}@fixture>", subject])


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


def test_daily_summary_never_requests_free_form_model_output(monkeypatch):
    malicious = {
        "choices": [{"finish_reason": "stop", "message": {"content": (
            "===TODAY===\nPersonal | alex@example.com | Secret subject\n"
            "===FULL===\nYou have a fabricated dentist appointment at 4 PM.")}}]
    }
    chat = AsyncMock(return_value=malicious)
    client_factory = Mock(return_value=SimpleNamespace(ensure_only=AsyncMock(), chat=chat))
    monkeypatch.setattr(brief, "_c", client_factory)
    monkeypatch.setattr(brief, "role_to_model", lambda _role: LING)
    monkeypatch.setattr(brief, "_messages_section", lambda _now: "Alex -> you: Dinner tonight.")
    monkeypatch.setattr(brief, "_schedule_section", lambda _now: "**📅 Today**\n- Clear.")
    monkeypatch.setattr(brief, "_email_section", lambda _now: "**📧 Inbox**\n- Nothing new.")
    from service.assistant import sync_status
    monkeypatch.setattr(sync_status, "ensure_daily_sources", AsyncMock(return_value={"syncing": False}))
    monkeypatch.setattr(email_tools, "email_freshness_warning", lambda: "")
    monkeypatch.setattr(email_tools, "with_email_freshness_note", lambda text, _warning: text)

    output = asyncio.run(brief.build_daily_brief())

    assert "Alex -> you: Dinner tonight." in output
    assert "alex@example.com" not in output
    assert "fabricated dentist" not in output
    client_factory.assert_not_called()
    chat.assert_not_awaited()


def test_non_ling_summary_paths_never_construct_model_client(monkeypatch):
    daily_client = Mock(side_effect=AssertionError("Daily Summary must stay model-free"))
    monkeypatch.setattr(brief, "_c", daily_client)
    monkeypatch.setattr(brief, "role_to_model", lambda _role: OTHER_THINKING_CAPABLE)
    monkeypatch.setattr(brief, "_messages_section", lambda _now: "Alex -> you: Dinner tonight.")
    monkeypatch.setattr(brief, "_schedule_section", lambda _now: "**📅 Today**\n- Clear.")
    monkeypatch.setattr(brief, "_email_section", lambda _now: "**📧 Inbox**\n- Nothing new.")
    from service.assistant import sync_status
    monkeypatch.setattr(sync_status, "ensure_daily_sources", AsyncMock(return_value={"syncing": False}))
    monkeypatch.setattr(email_tools, "email_freshness_warning", lambda: "")
    monkeypatch.setattr(email_tools, "with_email_freshness_note", lambda text, _warning: text)

    daily = asyncio.run(brief.build_daily_brief())

    assert "Alex -> you: Dinner tonight." in daily
    daily_client.assert_not_called()

    email_client = Mock(side_effect=AssertionError("Email summary must stay model-free"))
    monkeypatch.setattr(email_tools, "_c", email_client, raising=False)
    monkeypatch.setattr(email_tools, "_cache_ready", lambda: True)
    monkeypatch.setattr(email_tools, "_headers", "\n".join([
        _header(2.0, "Alex", "alex@fixture.test", "Project update"),
        _header(1.0, "Bea", "bea@fixture.test", "Project notes"),
    ]))

    digest = asyncio.run(email_tools.summarize_inbox_recent())

    assert digest.index("Project update") < digest.index("Project notes")
    email_client.assert_not_called()


def test_email_invalid_ids_and_schema_fall_back_deterministically(monkeypatch):
    chat = AsyncMock(side_effect=[
        {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"prioritize": ["999"]}'}}]},
        {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"prioritize": "0"}'}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": (
            '{"prioritize": ["0"], "raw": "Personal | alex@example.com", '
            '"claim": "fabricated dentist appointment"}')}}]},
    ])
    client = SimpleNamespace(ensure_only=AsyncMock(), chat=chat)
    monkeypatch.setattr(email_tools, "_c", lambda: client, raising=False)
    monkeypatch.setattr(email_tools, "_cache_ready", lambda: True)
    monkeypatch.setattr(email_tools, "_headers", "\n".join([
        _header(2.0, "Alex", "alex@fixture.test", "Project update"),
        _header(1.0, "Bea", "bea@fixture.test", "Project notes"),
    ]))

    for _response in range(3):
        output = asyncio.run(email_tools.summarize_inbox_recent())
        assert output.index("Project update") < output.index("Project notes")
        assert "alex@fixture.test" in output
        assert "fabricated dentist" not in output
    chat.assert_not_awaited()


def test_email_summary_production_path_keeps_ling_thinking(monkeypatch):
    chat = AsyncMock(return_value={"choices": [{
        "finish_reason": "stop", "message": {"content": '{"prioritize": ["0"]}'}}]})
    client = SimpleNamespace(ensure_only=AsyncMock(), chat=chat)
    monkeypatch.setattr(email_tools, "_c", lambda: client, raising=False)
    monkeypatch.setattr(email_tools, "_cache_ready", lambda: True)
    monkeypatch.setattr(email_tools, "_headers", _header(
        1.0, "Alex", "alex@fixture.test", "Please review the project update"))

    output = asyncio.run(email_tools.summarize_inbox_recent())

    assert "Alex" in output
    # Sender digests are fully deterministic; even a configured Ling model
    # receives no subject data and cannot add unsupported facts.
    chat.assert_not_awaited()
