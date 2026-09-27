"""Synthetic controls for current Reminders reads and ambiguous legacy rows."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Import the singleton only after redirecting its default database.
_SCRATCH = tempfile.TemporaryDirectory(prefix="wisp-reminder-truth-")
os.environ["HOME"] = _SCRATCH.name

from service.assistant import sync_status  # noqa: E402
from service.assistant.hub import hub  # noqa: E402
from service.assistant.store import AssistantStore  # noqa: E402
from service.tools import assistant_tools  # noqa: E402


def test_ready_reminders_require_new_snapshot() -> None:
    native = {"available": True, "last_sync": time.time() - 100,
              "diagnostics": {"snapshot_started_at": time.time() - 100}}
    events = []

    async def publish(event: dict) -> None:
        events.append(event)
        native["diagnostics"]["snapshot_started_at"] = time.time() + 0.001
        native["last_sync"] = time.time()

    with patch.object(sync_status.scheduler, "connectors_status",
                      side_effect=lambda: {"reminders": native}), \
         patch.object(hub, "publish", side_effect=publish):
        result = asyncio.run(sync_status.ensure_sources(("reminders",), timeout_seconds=0.1))
    assert events == [{"type": "sync_assistant_sources_now", "sources": ["reminders"]}]
    assert result["reminders_fresh"] is True
    assert result["sources"][0]["state"] == "ready"


def test_previous_ready_snapshot_does_not_satisfy_current_query() -> None:
    native = {"available": True, "last_sync": time.time() - 100,
              "diagnostics": {"snapshot_started_at": time.time() - 100}}
    with patch.object(sync_status.scheduler, "connectors_status",
                      side_effect=lambda: {"reminders": native}), \
         patch.object(hub, "publish", new_callable=AsyncMock):
        result = asyncio.run(sync_status.ensure_sources(("reminders",), timeout_seconds=0))
    assert result["reminders_fresh"] is False
    assert result["syncing"] is True


def test_search_refuses_stale_rows_and_prior_memory() -> None:
    stale = {"sources": [{"id": "reminders", "state": "ready"}],
             "reminders_fresh": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=stale), \
         patch.object(assistant_tools, "reminders_matching",
                      side_effect=AssertionError("stale store must not be read")):
        answer = asyncio.run(assistant_tools.search_reminders("synthetic"))
    assert "can't verify" in answer

    fresh = {"sources": [{"id": "reminders", "state": "ready"}],
             "reminders_fresh": True}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=fresh), \
         patch.object(assistant_tools, "reminders_matching", return_value=[]):
        answer = asyncio.run(assistant_tools.search_reminders("synthetic"))
    assert "current Reminders read" in answer
    assert "older remembered item does not establish" in answer


def test_ambiguous_wisp_rows_are_preserved_after_native_delete() -> None:
    store = AssistantStore(Path(_SCRATCH.name) / "collision.db")
    due = time.time() + 3600
    first = store.add_manual("Synthetic collision", due)
    second = store.add_manual("Synthetic collision", due)
    store.sync_source("reminders", [{"source_id": "native", "title": "Synthetic collision",
                                     "kind": "reminder", "when_ts": due}],
                      diagnostics={"snapshot_started_at": time.time()})
    store.sync_source("reminders", [], diagnostics={"snapshot_started_at": time.time() + 0.01})
    rows = store._db.execute(
        "SELECT id,status FROM commitments WHERE source='manual' ORDER BY id").fetchall()
    assert {row["id"] for row in rows} == {first["id"], second["id"]}
    assert all(row["status"] == "active" for row in rows)
    assert store._db.execute(
        "SELECT COUNT(*) FROM commitments WHERE source='reminders'").fetchone()[0] == 0
    store.sync_source("reminders", [{"source_id": "native", "title": "Synthetic collision",
                                     "kind": "reminder", "when_ts": due}],
                      diagnostics={"snapshot_started_at": time.time() + 0.02})
    assert store._db.execute(
        "SELECT COUNT(*) FROM commitments WHERE source='reminders' AND status='active'"
    ).fetchone()[0] == 1
    assert all(row["status"] == "active" for row in store._db.execute(
        "SELECT status FROM commitments WHERE source='manual'"))


def test_wisp_only_results_are_separate_from_current_apple_items() -> None:
    fresh = {"sources": [{"id": "reminders", "state": "ready"}],
             "reminders_fresh": True}
    row = {"id": "local", "source": "manual", "title": "Synthetic local record",
           "when_ts": time.time() + 3600}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=fresh), \
         patch.object(assistant_tools, "reminders_matching", return_value=[row]):
        answer = asyncio.run(assistant_tools.search_reminders("Synthetic"))
    assert "Current Apple Reminders matches:\nNone." in answer
    assert "Wisp-only records, kept for review" in answer
    assert "not verified active Apple Reminders" in answer
    agenda = assistant_tools._format_forward_agenda([row], now=time.time(),
                                                      window_label="next day")
    assert "Synthetic local record [Wisp-only; Apple status unverified — review]" in agenda


def test_late_receipt_from_old_fetch_cannot_make_query_fresh() -> None:
    old_started = time.time() - 10
    native = {"available": True, "last_sync": time.time() - 10,
              "diagnostics": {"snapshot_started_at": old_started}}

    async def publish(_: dict) -> None:
        # This POST arrived after the query, but its fetch began before it.
        native["last_sync"] = time.time()

    with patch.object(sync_status.scheduler, "connectors_status",
                      side_effect=lambda: {"reminders": native}), \
         patch.object(hub, "publish", side_effect=publish):
        result = asyncio.run(sync_status.ensure_sources(("reminders",), timeout_seconds=0.01))
    assert result["reminders_fresh"] is False


def test_calendar_only_schedule_does_not_wait_for_reminders() -> None:
    ready = {"sources": [{"id": "calendar", "label": "Calendar", "state": "ready"}],
             "syncing": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=ready) as ensure, \
         patch.object(assistant_tools.assistant_store, "upcoming", return_value=[]):
        answer = asyncio.run(assistant_tools.get_upcoming(calendar_only=True))
    ensure.assert_awaited_once_with(("calendar",))
    assert "Nothing scheduled" in answer


def test_current_schedule_routes_exclude_stale_memory_but_recall_keeps_it() -> None:
    from types import SimpleNamespace
    from service import main

    def allowed(*names: str, verified: bool = False) -> bool:
        decision = SimpleNamespace(tool_subset=list(names),
                                   verified_results_only=verified)
        return main._agent_memory_context_allowed(
            decision, cloud=False, grounded_workflow=False)

    assert allowed("search_reminders") is False
    assert allowed("get_upcoming") is False
    assert allowed("search_reminders", "recall") is False
    assert allowed("recall") is True
    assert allowed("remember") is True
    assert allowed("search_notes") is True
    assert allowed("search_notes", verified=True) is False


def test_calendar_failure_is_visible_while_reminders_refresh_is_pending() -> None:
    pending = {"sources": [{"id": "calendar", "label": "Calendar", "state": "unavailable"},
                           {"id": "reminders", "label": "Reminders", "state": "ready"}],
               "reminders_fresh": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=pending), \
         patch.object(assistant_tools.assistant_store, "upcoming",
                      side_effect=AssertionError("saved rows must not be exposed")):
        answer = asyncio.run(assistant_tools.get_upcoming())
    assert "could not check Calendar" in answer
    assert "has not received a current Reminders read" in answer
