"""Synthetic controls for current Reminders reads and ambiguous legacy rows."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import datetime
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
    assert "Apple Reminders incomplete-item matches:\nNone." in answer
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


def test_daily_summary_holds_back_when_new_reminder_snapshot_times_out() -> None:
    from service.assistant import brief

    old = {"sources": [{"id": "calendar", "label": "Calendar", "state": "ready",
                         "progress": 1.0},
                       {"id": "reminders", "label": "Reminders", "state": "ready",
                         "progress": 1.0}],
           "total": 2, "completed": 2, "progress": 1.0,
           "pending": [], "pending_labels": [], "syncing": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value={"reminders_fresh": False}), \
         patch.object(sync_status, "summary_snapshot", return_value=old):
        snapshot = asyncio.run(sync_status.ensure_daily_sources(timeout_seconds=0))
    assert snapshot["syncing"] is True
    assert snapshot["pending"] == ["reminders"]
    assert snapshot["completed"] == 1
    with patch.object(brief, "_plain_brief",
                      side_effect=AssertionError("stale brief must not render")):
        sections = asyncio.run(brief._sections("morning", snapshot=snapshot))
    assert sections.get("READY") != "1"
    assert "still syncing" in sections["FULL"]


def test_unavailable_native_twin_is_presented_as_unverified_wisp_only() -> None:
    store = AssistantStore(Path(_SCRATCH.name) / "unavailable-twin.db")
    due = time.time() + 3600
    store.add_manual("Synthetic retained item", due)
    store.sync_source("reminders", [{"source_id": "native", "title": "Synthetic retained item",
                                     "kind": "reminder", "when_ts": due}],
                      diagnostics={"snapshot_started_at": time.time()})
    collapsed = store.upcoming()
    assert collapsed[0]["source"] == "manual"
    assert "reminders" in collapsed[0]["duplicate_sources"]
    status = {"sources": [{"id": "calendar", "label": "Calendar", "state": "ready"},
                          {"id": "reminders", "label": "Reminders", "state": "unavailable"}],
              "reminders_fresh": True}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=status), \
         patch.object(assistant_tools, "assistant_store", store):
        answer = asyncio.run(assistant_tools.get_upcoming(days=2))
    assert "Synthetic retained item [Wisp-only; Apple status unverified — review]" in answer
    assert "could not check Reminders" in answer


def test_current_schedule_context_excludes_stale_session_assistant_claim() -> None:
    from types import SimpleNamespace
    from service import main

    user_msg = {"role": "user", "content": "Which reminders are active?"}
    stale = {"role": "assistant", "content": "Synthetic stale reminder assertion"}
    decision = SimpleNamespace(tool_subset=["search_reminders"],
                               verified_results_only=False)
    with patch.object(main, "build_messages", return_value=[stale]) as history:
        messages = main._tool_turn_messages(
            "synthetic-session", user_msg, max_tokens=1500, test_mode=False,
            verified_results_only=(decision.verified_results_only or
                                   main._current_schedule_source_route(decision)))
    assert messages == [user_msg]
    history.assert_not_called()

    decision.tool_subset = ["recall"]
    with patch.object(main, "build_messages", return_value=[stale]) as history:
        messages = main._tool_turn_messages(
            "synthetic-session", user_msg, max_tokens=1500, test_mode=False,
            verified_results_only=(decision.verified_results_only or
                                   main._current_schedule_source_route(decision)))
    assert messages == [stale, user_msg]
    history.assert_called_once()


def test_daily_summary_endpoint_does_not_present_cached_reminder_as_current() -> None:
    from service import main
    from service.assistant import brief

    sources = [{"id": name, "label": name.title(), "state": "ready", "progress": 1.0}
               for name in ("calendar", "reminders", "email", "messages")]
    old_ready = {"sources": sources, "total": 4, "completed": 4,
                 "progress": 1.0, "pending": [], "pending_labels": [], "syncing": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value={"reminders_fresh": False}), \
         patch.object(sync_status, "summary_snapshot", return_value=old_ready), \
         patch.object(brief, "_generate_brief", new_callable=AsyncMock) as generate:
        response = asyncio.run(main.assistant_daily_summary({"session_id": ""}))
    generate.assert_not_awaited()
    assert response["ok"] is False
    assert "still syncing" in response["text"]


def test_daily_summary_and_card_disclose_unavailable_native_twin() -> None:
    from service.assistant import brief

    store = AssistantStore(Path(_SCRATCH.name) / "daily-unavailable-twin.db")
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    due = now + 3600
    store.add_manual("Synthetic daily retained twin", due)
    store.sync_source("reminders", [{"source_id": "native",
                                     "title": "Synthetic daily retained twin",
                                     "kind": "reminder", "when_ts": due}],
                      diagnostics={"snapshot_started_at": time.time()})
    assert "reminders" in store.upcoming(now=now, days=7)[0]["duplicate_sources"]
    states = {"calendar": {"state": "ready"},
              "reminders": {"state": "unavailable"}}
    with patch.object(brief, "assistant_store", store), \
         patch.object(sync_status, "source_status", side_effect=states.get), \
         patch.object(brief, "_mail_split", return_value={"state": "unavailable"}):
        section = brief._schedule_section(now)
        card = brief._today_card(now)
    for output in (section, card):
        assert "Synthetic daily retained twin" in output
        assert "Wisp-only" in output
        assert "Apple status unverified" in output
        assert "Reminders couldn't be read" in output

    # The notification must still label Wisp-only rows when a calendar event
    # takes the single "Next" slot and hides the reminder title.
    store.sync_source("calendar", [{"source_id": "event", "title": "Synthetic earlier event",
                                    "kind": "event", "when_ts": now + 1800}],
                      diagnostics={"snapshot_started_at": time.time()})
    with patch.object(brief, "assistant_store", store), \
         patch.object(sync_status, "source_status", side_effect=states.get), \
         patch.object(brief, "_mail_split", return_value={"state": "unavailable"}):
        card = brief._today_card(now)
    assert "Next: Synthetic earlier event" in card
    assert "Wisp-only reminders: Apple status unverified" in card
    assert "Reminders couldn't be read" in card


def test_daily_summary_and_card_qualify_native_deletion_status() -> None:
    from service.assistant import brief

    store = AssistantStore(Path(_SCRATCH.name) / "daily-native-candidate.db")
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    due = now + 3600
    store.sync_source("reminders", [{"source_id": "native",
                                     "title": "Synthetic native candidate",
                                     "kind": "reminder", "when_ts": due}],
                      diagnostics={"snapshot_started_at": time.time()})
    states = {"calendar": {"state": "ready"},
              "reminders": {"state": "ready"}}
    with patch.object(brief, "assistant_store", store), \
         patch.object(sync_status, "source_status", side_effect=states.get), \
         patch.object(brief, "_mail_split", return_value={"state": "unavailable"}):
        section = brief._schedule_section(now)
        card = brief._today_card(now)
    for output in (section, card):
        assert "Synthetic native candidate" in output
        assert "deletion status" in output.lower()
        assert "unverified" in output.lower()
