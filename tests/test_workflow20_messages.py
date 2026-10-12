"""Offline unread and strict-query regressions over fictional Messages records."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from service.tools import imessage_tools as M
from service.tools.registry import REGISTRY


def stamp(day=12, hour=9):
    return datetime(2026, 10, day, hour).timestamp()


def row(text, *, state="U", chat=1, context="Trisha Jain", day=12, hour=9):
    return f"V2 | {stamp(day, hour)} | {state} | chat:{chat} | {context} | {text}"


@pytest.fixture(autouse=True)
def world(monkeypatch):
    from service.assistant import sync_status
    from service.memory import identity

    monkeypatch.setattr(sync_status, "ensure_sources", AsyncMock())
    monkeypatch.setattr(identity, "user_name", lambda: "Adi")
    monkeypatch.setattr(M, "_contacts", {})
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    monkeypatch.setattr(M, "_unavailable_reason", "")
    monkeypatch.setattr(M, "_lines", "\n".join([
        row("Trisha Jain: Pickup is Tuesday at 6 PM."),
        row("Trisha Jain: Bring the blue folder.", state="R", hour=8),
        row("Jordan Ellis: Thanks!", chat=2, context="Jordan Ellis", hour=10),
        row("Dan Smith: Review Project Apollo by Thursday.", state="R", chat=3, context="Dan Smith"),
        row("Mom: Dinner is Sunday at 7 PM.", chat=4, context="Mom", day=11),
    ]))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Unread read must not call a model or persist data")

    monkeypatch.setattr(M, "_c", forbidden)
    monkeypatch.setattr(M.cache_store, "save", forbidden)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_unread_includes_routine_replies_and_excludes_read_work_requests(tool):
    before = M._lines
    result = run(tool(unread=True))
    assert "Pickup is Tuesday" in result and "Thanks!" in result
    assert "blue folder" not in result and "Project Apollo" not in result
    assert M._lines == before


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_unread_date_scope_is_applied_before_selection(tool):
    result = run(tool(unread=True, day="2026-10-11"))
    assert "Dinner is Sunday" in result
    assert "Pickup" not in result and "Thanks!" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_period_overrides_day_and_still_filters_unread(tool):
    result = run(tool(unread=True, day="2026-10-11", period="2026-10-12"))
    assert "Pickup" in result and "Dinner" not in result
    assert "Project Apollo" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_unread_named_conversation_is_exact_and_supports_typo(tool):
    result = run(tool(unread=True, conversation="trishe"))
    assert "Pickup" in result
    assert "Thanks!" not in result and "blue folder" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_named_conversation_with_only_read_messages_is_empty(tool):
    result = run(tool(unread=True, conversation="Dan Smith"))
    assert "No " in result and "unread" in result.lower()
    assert "Project Apollo" not in result and "Pickup" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_ambiguous_current_conversation_names_never_select_one(tool, monkeypatch):
    monkeypatch.setattr(M, "_lines", "\n".join([
        row("Alex: First private text.", chat=7, context="Alex"),
        row("Alex: Second private text.", chat=8, context="Alex"),
    ]))
    result = run(tool(unread=True, conversation="Alex"))
    assert "More than one conversation" in result
    assert "private text" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_current_conversation_miss_never_broadens(tool):
    result = run(tool(unread=True, conversation="Zebedee"))
    assert "No conversation matched" in result
    assert "Pickup" not in result and "Thanks!" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_unknown_read_state_is_not_zero_unread_or_a_private_excerpt(tool, monkeypatch):
    monkeypatch.setattr(M, "_lines", f"{stamp()} | Trisha Jain | Trisha Jain: Private legacy content.")
    result = run(tool(unread=True))
    assert "status is unavailable" in result
    assert "Private legacy content" not in result
    assert "No unread messages found" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_mixed_unknown_state_discloses_incomplete_read_coverage(tool, monkeypatch):
    monkeypatch.setattr(M, "_lines", M._lines + f"\n{stamp()} | Lee | Lee: Unknown private content.")
    result = run(tool(unread=True))
    assert "unknown read status" in result and "Pickup" in result
    assert "Unknown private content" not in result


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_unread_result_limit_is_disclosed_even_without_period(tool):
    result = run(tool(unread=True, count=1))
    assert "Thanks!" in result
    assert "not shown" in result.lower()


@pytest.mark.parametrize("tool", [M.summarize_messages, M.view_messages])
def test_current_launch_and_permission_boundaries_are_preserved(tool, monkeypatch):
    monkeypatch.setattr(M, "_sync_completed", False)
    assert "still syncing" in run(tool(unread=True))
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", False)
    monkeypatch.setattr(M, "_unavailable_reason", "Synthetic permission denial")
    result = run(tool(unread=True))
    assert "Can't read Messages" in result and "Pickup" not in result


def test_strict_raw_query_miss_returns_no_unrelated_records():
    result = run(M.view_messages(query="nonexistent Ocean rescue"))
    assert "No messages matching" in result
    assert "Pickup" not in result and "Project Apollo" not in result


def test_strict_raw_query_and_unread_compose_without_broadening():
    result = run(M.view_messages(query="Project Apollo", unread=True))
    assert "No messages matching" in result and "Project Apollo by" not in result
    assert "Pickup" not in result


def test_named_identity_and_topic_miss_never_broaden():
    scoped = run(M.view_messages(query="nonexistent", conversation="Dan Smith"))
    assert "No messages matching" in scoped and "Project Apollo by" not in scoped


def test_original_positional_api_still_reads_unfiltered_messages():
    result = run(M.view_messages(None, "2026-10-12", 20, None))
    assert "Pickup" in result and "Project Apollo" in result


def test_unread_summary_keeps_sender_and_group_addressee(monkeypatch):
    monkeypatch.setattr(M, "_contacts", {"5555550123": "Trisha"})
    monkeypatch.setattr(M, "_lines", row(
        "Mom: @Trisha - Your conference starts Wednesday at 9 AM.",
        context='Group "Family"', chat=12))
    result = run(M.summarize_messages(unread=True, conversation="Family"))
    assert "Mom" in result and "Trisha" in result
    assert "NOT the user" in result


def test_unread_summary_uses_existing_code_redaction(monkeypatch):
    monkeypatch.setattr(M, "_lines", row("Bank: Your verification code is 123456.", context="Bank", chat=12))
    result = run(M.summarize_messages(unread=True))
    assert "123456" not in result


def test_partial_native_scan_does_not_claim_complete_unread_inventory(monkeypatch):
    coverage = {"version": 1, "kind": "coverage", "attempted": 4, "emitted": 0,
                "skipped": 4, "truncated": 0, "limit": 2000, "byte_limit": 4000000,
                "window_days": 365, "row_limit": 20000, "reached_row_limit": False}
    monkeypatch.setattr(M, "_lines", "V3 | " + json.dumps(coverage) + "\n" + M._lines)
    result = run(M.summarize_messages(unread=True))
    assert "scan coverage is partial" in result and "Pickup" in result


def test_unread_options_are_registered_for_model_selection():
    for name in ("summarize_messages", "view_messages"):
        properties = REGISTRY[name].parameters["properties"]
        assert properties["unread"]["type"] == "boolean"
        assert properties["conversation"]["type"] == "string"


@pytest.fixture
def chat_summary(monkeypatch):
    """Actual registry/summary code with deterministic topic selection only."""
    import socket
    import subprocess
    from service import debug_capture
    from service.tools.registry import run_tool

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Chat coverage tests must not access network/native effects")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    calls, diagnostics, inference = [], [], []

    class Client:
        async def chat(self, _model, request, **_kwargs):
            inference.append(request)
            candidates = json.loads(request[-1]["content"])
            topics = {key: values[:1] for key, values in candidates.items()}
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(topics)}}]}

    monkeypatch.setattr(M, "_c", lambda: Client())
    monkeypatch.setattr(debug_capture, "record", lambda *args, **kw: diagnostics.append((args, kw)))
    original = M._summarize

    async def summarize(rows, label):
        calls.append((list(rows), label))
        return await original(rows, label)

    monkeypatch.setattr(M, "_summarize", summarize)

    def dispatch(**args):
        assert REGISTRY["summarize_messages"].func is M.summarize_messages
        return run(run_tool(REGISTRY["summarize_messages"], args))

    return dispatch, calls, diagnostics, inference


def chat_rows(n, *, chat=1, context="Trisha Jain", day=12):
    return [f"V2 | {stamp(day) + i} | R | chat:{chat} | {context} | "
            f"Trisha Jain: Review project document {i} by Tuesday."
            for i in range(n)]


@pytest.mark.parametrize("total,args,shown", [(31, {}, 30), (301, {"count": 999}, 300),
                                             (3, {"count": 1}, 1)])
def test_named_chat_registry_discloses_display_limit_before_digest_cap(
        monkeypatch, chat_summary, total, args, shown):
    from service.tools import message_digest
    dispatch, calls, _diagnostics, _inference = chat_summary
    monkeypatch.setattr(M, "_lines", "\n".join(reversed(chat_rows(total))))
    monkeypatch.setattr(message_digest, "MAX_OUTPUT_CHARS", 240)
    result = dispatch(conversation="Trisha Jain", **args)
    assert f"{total} cached messages matched" in result
    assert f"{total} eligible after" in result
    assert f"{shown} included in the digest input" in result
    assert f"{total - shown} older eligible messages omitted" in result
    assert result.startswith("Summary input coverage:")
    assert "not every included message" in result
    assert len(calls[0][0]) == shown
    assert [r[0] for r in calls[0][0]] == [stamp() + i for i in range(total - shown, total)]
    assert "cache freshness and complete chat history are unverified" in result


def test_named_chat_helper_default_stays_one_hundred(monkeypatch, chat_summary):
    _dispatch, calls, _diagnostics, _inference = chat_summary
    monkeypatch.setattr(M, "_lines", "\n".join(chat_rows(101)))
    result = run(M.summarize_messages_for_conversation("Trisha Jain"))
    assert len(calls[0][0]) == 100
    assert "1 older eligible messages omitted" in result


@pytest.mark.parametrize("scope", [{"day": "2026-10-12"},
    {"period": "2026-10-12", "day": "2026-10-11"}])
def test_named_chat_coverage_counts_only_selected_identity_and_date(monkeypatch, chat_summary, scope):
    dispatch, calls, _diagnostics, _inference = chat_summary
    monkeypatch.setattr(M, "_lines", "\n".join(chat_rows(3) + chat_rows(5, day=11)
                                             + chat_rows(7, chat=2, context="Other chat")))
    result = dispatch(conversation="Trisha Jain", count=2, **scope)
    assert "3 cached messages matched" in result and "3 eligible after" in result
    assert "2 included" in result and "1 older eligible messages omitted" in result
    assert [r[0] for r in calls[0][0]] == [stamp() + 1, stamp() + 2]
    assert all(r[1] == "Trisha Jain" for r in calls[0][0])


def test_named_chat_counts_existing_filters_and_preserves_eligible_chronology(monkeypatch, chat_summary):
    dispatch, calls, _diagnostics, _inference = chat_summary
    monkeypatch.setattr(M, "_lines", "\n".join([
        row("Trisha Jain: Bring the projector to Room 204.", hour=8),
        row("Trisha Jain: Correction: Room 305, by 1 PM Tuesday.", hour=9),
        row("Trisha Jain: Correction: Room 305, by 1 PM Tuesday.", hour=10),
        row("Trisha Jain: Your verification code is 123456.", hour=11),
    ]))
    result = dispatch(conversation="Trisha Jain")
    assert "4 cached messages matched" in result and "2 eligible after" in result
    assert "2 included" in result and "2 messages excluded by summary filters" in result
    assert [r[2] for r in calls[0][0]] == [
        "Trisha Jain: Bring the projector to Room 204.",
        "Trisha Jain: Correction: Room 305, by 1 PM Tuesday."]
    assert "123456" not in result


def test_named_chat_all_filtered_differs_from_missing_identity(monkeypatch, chat_summary):
    dispatch, calls, _diagnostics, inference = chat_summary
    monkeypatch.setattr(M, "_lines", row("Bank: Your verification code is 123456.", context="Bank"))
    result = dispatch(conversation="Bank")
    assert "1 cached messages matched" in result and "0 eligible after" in result
    assert "all excluded by the existing summary filters" in result
    assert "does not establish that the chat or its history is empty" in result
    missing = dispatch(conversation="Zebedee")
    assert "No conversation matched" in missing and "Summary input coverage" not in missing
    assert not calls and not inference
    assert "123456" not in result + missing


def test_named_chat_empty_date_scope_is_not_missing_chat(monkeypatch, chat_summary):
    dispatch, calls, _diagnostics, inference = chat_summary
    result = dispatch(conversation="Trisha Jain", day="2026-10-11")
    assert "0 cached messages matched" in result
    assert "No cached messages matched this chat/date scope" in result
    assert "No conversation matched" not in result
    assert "older or unsynced messages may be missing" in result
    assert not calls and not inference


@pytest.mark.parametrize("partial", [False, True])
def test_named_chat_discloses_legacy_or_partial_native_history(monkeypatch, chat_summary, partial):
    dispatch, _calls, _diagnostics, _inference = chat_summary
    if partial:
        coverage = {"version": 1, "kind": "coverage", "attempted": 4, "emitted": 0,
                    "skipped": 4, "truncated": 0, "limit": 2000, "byte_limit": 4000000,
                    "window_days": 365, "row_limit": 20000, "reached_row_limit": False}
        monkeypatch.setattr(M, "_lines", "V3 | " + json.dumps(coverage) + "\n" + M._lines)
    result = dispatch(conversation="Trisha Jain")
    assert ("Messages scan coverage is partial" if partial else
            "Native scan coverage is unknown for this legacy feed") in result
    assert "Only the available bounded cache was searched" in result
    assert "complete chat history are unverified" in result


def test_named_chat_preserves_privacy_in_digest_input_diagnostics_and_fake_inference(monkeypatch, chat_summary):
    dispatch, calls, diagnostics, inference = chat_summary
    monkeypatch.setattr(M, "_lines", row(
        "Trisha Jain: Review the project report by Tuesday. Your verification code is 123456."))
    result = dispatch(conversation="Trisha Jain")
    assert "1 eligible after" in result and "1 included" in result
    assert "123456" not in result + repr(calls) + repr(diagnostics) + repr(inference)
    assert "review the original request with a stated deadline" in calls[0][0][0][2]
    assert "private details omitted" in calls[0][0][0][2]


def test_named_chat_ambiguity_never_reads_one_candidate(monkeypatch, chat_summary):
    dispatch, calls, _diagnostics, inference = chat_summary
    monkeypatch.setattr(M, "_lines", "\n".join([
        row("Alex: First private text.", chat=7, context="Alex"),
        row("Alex: Second private text.", chat=8, context="Alex")]))
    result = dispatch(conversation="Alex")
    assert "More than one conversation" in result
    assert "private text" not in result and not calls and not inference


def test_named_chat_legacy_missing_identity_still_requires_refresh(monkeypatch, chat_summary):
    dispatch, calls, _diagnostics, inference = chat_summary
    monkeypatch.setattr(M, "_lines", f"{stamp()} | Trisha Jain | Trisha Jain: Private legacy content.")
    result = dispatch(conversation="Trisha Jain")
    assert "refreshing conversation identities" in result
    assert "Private legacy content" not in result
    assert "Summary input coverage" not in result and not calls and not inference
