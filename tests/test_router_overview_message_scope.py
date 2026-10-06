"""Opt-in compiled Messages reads never broaden a literal query on a miss."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from service.tools import imessage_tools as M

NOW = datetime(2026, 10, 5, 12)


def source(monkeypatch, rows):
    monkeypatch.setattr("service.assistant.sync_status.ensure_sources", AsyncMock(return_value={}))
    monkeypatch.setattr(M, "messages_sync_state", lambda: "ready")
    monkeypatch.setattr(M, "_lines", "synthetic ready marker")
    monkeypatch.setattr(M, "_parse_lines", lambda: rows)


def test_strict_keyword_miss_never_returns_unrelated_rows(monkeypatch):
    source(monkeypatch, [(NOW.timestamp(), "Alex", "Alex: Grab the package."),
                         (NOW.timestamp() + 1, "Blair", "Blair: Other source content.")])
    output = asyncio.run(M.view_messages(query="pickup", count=1, strict_match=True))
    assert output == "No messages matched 'pickup' in the available cache for your recent messages."
    assert "Grab" not in output and "Other source" not in output
    assert "No messages found" not in output


def test_legacy_default_retains_original_keyword_miss_behavior(monkeypatch):
    rows = [(NOW.timestamp() + i, "Alex", f"Alex: Synthetic row {i}.") for i in range(20)]
    source(monkeypatch, rows)
    output = asyncio.run(M.view_messages(query="pickup", count=1))
    assert "no exact match for 'pickup'" in output
    assert output.count("Synthetic row") == 15


def test_strict_match_preserves_requested_count_and_excludes_other_conversations(monkeypatch):
    source(monkeypatch, [(NOW.timestamp(), "Alex", "Alex: Pickup at noon."),
                         (NOW.timestamp() + 1, "Alex", "Alex: Pickup at one."),
                         (NOW.timestamp() + 2, "Blair", "Blair: Unrelated.")])
    output = asyncio.run(M.view_messages(query="Alex", count=1, strict_match=True))
    assert "Pickup at one" in output
    assert "Pickup at noon" not in output and "Unrelated" not in output
    assert output.count("[Mon Oct 5") == 1


def test_strict_miss_is_scoped_to_the_requested_day(monkeypatch):
    source(monkeypatch, [(NOW.timestamp(), "Alex", "Alex: Lunch."),
                         ((NOW + timedelta(days=1)).timestamp(), "Alex", "Alex: Pickup.")])
    monkeypatch.setattr(M, "_day_bounds", lambda day: (NOW.replace(hour=0).timestamp(),
                                                      (NOW.replace(hour=0) + timedelta(days=1)).timestamp(),
                                                      "Oct 5, 2026"))
    output = asyncio.run(M.view_messages(query="pickup", day="today", count=1, strict_match=True))
    assert output == "No messages matched 'pickup' in the available cache for Oct 5, 2026."
    assert "Lunch" not in output and "Pickup" not in output


def test_strict_miss_keeps_period_precedence_and_does_not_read_a_different_day(monkeypatch):
    source(monkeypatch, [(NOW.timestamp(), "Alex", "Alex: Lunch.")])
    monkeypatch.setattr(M, "resolve_span", lambda value: (NOW.replace(hour=0).timestamp(),
                                                         (NOW.replace(hour=0) + timedelta(days=7)).timestamp(),
                                                         "Oct 5–11, 2026"))
    def forbidden(*args):
        raise AssertionError("period must win over day")
    monkeypatch.setattr(M, "_day_bounds", forbidden)
    output = asyncio.run(M.view_messages(query="pickup", day="yesterday", period="this week",
                                         count=1, strict_match=True))
    assert "available cache for Oct 5–11, 2026" in output


def test_strict_permission_failure_is_not_a_keyword_miss(monkeypatch):
    source(monkeypatch, [(NOW.timestamp(), "Alex", "Alex: Pickup.")])
    monkeypatch.setattr(M, "messages_sync_state", lambda: "unavailable")
    monkeypatch.setattr(M, "_unavailable_message", lambda: "Messages permission is unavailable.")
    output = asyncio.run(M.view_messages(query="pickup", count=1, strict_match=True))
    assert output == "Messages permission is unavailable."


def test_registered_schema_and_callable_accept_opt_in_strict_match():
    import inspect
    from service.tools.registry import get_tool, _validate_args
    tool = get_tool("view_messages")
    assert tool.parameters["properties"]["strict_match"]["type"] == "boolean"
    assert inspect.signature(M.view_messages).parameters["strict_match"].default is False
    assert inspect.signature(M.view_messages_impl).parameters["strict_match"].default is False
    assert _validate_args(tool, {"query": "pickup", "count": 1, "strict_match": True}) is None
    assert _validate_args(tool, {"strict_match": "true"}) is not None
