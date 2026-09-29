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


def _search_rows() -> list[dict]:
    due = time.time() + 3600
    return [{"id": "native", "source": "reminders", "title": "synthetic native",
             "when_ts": due},
            {"id": "local", "source": "manual", "title": "synthetic local",
             "when_ts": due, "duplicate_sources": ["reminders"]}]


def test_search_refuses_stale_rows_and_prior_memory() -> None:
    stale = {"sources": [{"id": "reminders", "state": "ready"}],
             "reminders_fresh": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=stale), \
         patch.object(assistant_tools, "reminders_matching", return_value=_search_rows()):
        answer = asyncio.run(assistant_tools.search_reminders("synthetic"))
    assert "haven't received a current Reminders read" in answer
    assert "synthetic native" not in answer
    assert "- synthetic local" in answer
    assert "Apple Reminders incomplete-item matches" not in answer

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
    assert "Synthetic local record [Wisp-only; Apple status unverified]" in agenda


def test_search_keeps_wisp_records_when_apple_reminders_cannot_be_checked() -> None:
    cases = {"unavailable": ("could not check Apple Reminders", True),
             "syncing": ("still syncing or waiting for access", True)}
    for state, (message, fresh) in cases.items():
        readiness = {"sources": [{"id": "reminders", "state": state}],
                     "reminders_fresh": fresh}
        with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                          return_value=readiness), \
             patch.object(assistant_tools, "reminders_matching",
                          return_value=_search_rows()):
            answer = asyncio.run(assistant_tools.search_reminders("synthetic"))
        assert message in answer, state
        assert "synthetic native" not in answer
        assert "- synthetic local" in answer
        assert "they may still be active" in answer
    readiness = {"sources": [{"id": "reminders", "state": "unavailable"}],
                 "reminders_fresh": True}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=readiness), \
         patch.object(assistant_tools, "reminders_matching", return_value=[]):
        answer = asyncio.run(assistant_tools.search_reminders("synthetic"))
    assert "No Wisp-only reminder record matches" in answer


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


def _route(text: str, **context):
    from service.router.router import route
    return asyncio.run(route(text, **context))


def _schedule_read(text: str, **context) -> bool:
    from service import main
    return main._current_schedule_source_route(_route(text, **context), text)


def test_current_schedule_routes_exclude_stale_memory_but_recall_keeps_it() -> None:
    from types import SimpleNamespace
    from service import main

    def allowed(*, schedule_read: bool, verified: bool = False) -> bool:
        decision = SimpleNamespace(verified_results_only=verified)
        return main._agent_memory_context_allowed(
            decision, cloud=False, grounded_workflow=False,
            schedule_read=schedule_read)

    assert allowed(schedule_read=True) is False
    assert allowed(schedule_read=False) is True
    assert allowed(schedule_read=False, verified=True) is False
    # Standalone live reads of Reminders or Calendar are schedule reads.
    assert _schedule_read("which reminders are active?")
    assert _schedule_read("what reminders do I have")
    assert _schedule_read("what's on my calendar today")
    assert not _schedule_read("what do you remember about my car")


def test_reminder_repair_route_keeps_the_previous_turn() -> None:
    """The repair route writes, so it must see which reminder to fix."""
    from service import main

    prior = {"last_user": "remind me to call mom tomorrow at 5pm",
             "last_assistant": "Your reminder to call mom is set for today at 5 PM.",
             "last_tools": "get_upcoming,add_reminder"}
    for text in ("fix it", "still set for today"):
        decision = _route(text, **prior)
        assert "update_reminder" in (decision.tool_subset or ())
        assert not main._current_schedule_source_route(decision, text)
        user_msg = {"role": "user", "content": text}
        earlier = [{"role": "user", "content": prior["last_user"]},
                   {"role": "assistant", "content": prior["last_assistant"]}]
        with patch.object(main, "build_messages", return_value=earlier):
            messages = main._tool_turn_messages(
                "synthetic-session", user_msg, max_tokens=1500, test_mode=False,
                verified_results_only=(decision.verified_results_only or
                                       main._current_schedule_source_route(decision, text)))
        assert messages == earlier + [user_msg]


def test_anaphoric_reminder_lookups_keep_history() -> None:
    from service import main
    ctx = {"last_user": "remind me to call mom tomorrow at 5pm",
           "last_assistant": "I set the reminder for 5 PM.",
           "last_tools": "add_reminder"}
    for text in ("when is that reminder due?", "is that reminder set?"):
        assert not _schedule_read(text, **ctx)
    assert not main._current_schedule_source_route(
        _route("when is that reminder due?", **ctx), "is it set")
    assert _schedule_read("which reminders are active?")


def test_context_dependent_schedule_followups_keep_history() -> None:
    from service.router import router

    schedule = {"last_user": "what's on my calendar this week",
                "last_assistant": "You have standup, the dentist and gym.",
                "last_tools": "get_upcoming"}
    # A context-derived read ("which reminder?") and a scope continuation.
    assert not _schedule_read("I don't see it", last_user="remind me to stretch",
                              last_assistant="I set the reminder for 5 PM.",
                              last_tools="add_reminder")
    assert not _schedule_read("what about tomorrow", **schedule)
    # The ambiguous fallback may offer get_upcoming (retrieval or the static
    # core list) without being a live schedule read.
    for core in (["get_upcoming", "search_notes", "recall"], ["get_upcoming"]):
        with patch.object(router, "_semantic_core", new_callable=AsyncMock,
                          return_value=list(core)):
            for text in ("tell me more about the second one",
                         "ok and what about next week"):
                decision = _route(text, **schedule)
                assert decision.source == "default", decision.reason
                assert not _schedule_read(text, **schedule)


def _schedule_rows() -> list[dict]:
    due = time.time() + 3600
    return [{"id": "event", "source": "calendar", "kind": "event",
             "title": "Synthetic calendar event", "when_ts": due},
            {"id": "native", "source": "reminders", "kind": "reminder",
             "title": "Synthetic stale native reminder", "when_ts": due + 60},
            {"id": "local", "source": "manual", "kind": "reminder",
             "title": "Synthetic Wisp reminder", "when_ts": due + 120,
             "duplicate_sources": ["reminders"], "duplicate_ids": ["native-twin"]}]


def test_calendar_failure_is_visible_while_reminders_refresh_is_pending() -> None:
    pending = {"sources": [{"id": "calendar", "label": "Calendar", "state": "unavailable"},
                           {"id": "reminders", "label": "Reminders", "state": "ready"}],
               "reminders_fresh": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=pending), \
         patch.object(assistant_tools.assistant_store, "upcoming",
                      return_value=_schedule_rows()):
        answer = asyncio.run(assistant_tools.get_upcoming())
    assert "could not check Calendar" in answer
    assert "has not received a current Reminders read" in answer
    assert "Synthetic calendar event" not in answer
    assert "Synthetic stale native reminder" not in answer
    assert "Synthetic Wisp reminder [Wisp-only; Apple status unverified]" in answer
    assert "they may still be active" in answer
    assert "before deletion" not in answer


def test_pending_reminders_read_does_not_hide_current_calendar() -> None:
    pending = {"sources": [{"id": "calendar", "label": "Calendar", "state": "ready"},
                           {"id": "reminders", "label": "Reminders", "state": "ready"}],
               "reminders_fresh": False}
    with patch.object(sync_status, "ensure_sources", new_callable=AsyncMock,
                      return_value=pending), \
         patch.object(assistant_tools.assistant_store, "upcoming",
                      return_value=_schedule_rows()):
        answer = asyncio.run(assistant_tools.get_upcoming())
    assert "Synthetic calendar event" in answer
    assert "Synthetic stale native reminder" not in answer
    assert "has not received a current Reminders read" in answer
    assert "Apple Reminders items are not shown" in answer
    assert "Synthetic Wisp reminder [Wisp-only; Apple status unverified]" in answer


def test_wisp_only_classification_matches_line_tags() -> None:
    now = time.time()
    merged = {"id": "m", "source": "manual", "duplicate_sources": ["calendar"],
              "title": "Synthetic merged event", "when_ts": now + 3600}
    agenda = assistant_tools._format_forward_agenda([merged], now=now,
                                                      window_label="next day")
    assert "[Wisp-only" not in agenda
    assert "Wisp-only items" not in agenda
    local = {"id": "l", "source": "manual", "title": "Synthetic local",
             "when_ts": now + 3600}
    agenda = assistant_tools._format_forward_agenda([local], now=now,
                                                      window_label="next day")
    assert "Synthetic local [Wisp-only; Apple status unverified]" in agenda
    assert "others live Wisp reminders" in agenda
    assert "before deletion" not in agenda


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
    assert "Synthetic retained item [Wisp-only; Apple status unverified]" in answer
    assert "could not check Reminders" in answer


def test_current_schedule_context_excludes_stale_session_assistant_claim() -> None:
    from types import SimpleNamespace
    from service import main

    text = "Which reminders are active?"
    user_msg = {"role": "user", "content": text}
    stale = {"role": "assistant", "content": "Synthetic stale reminder assertion"}
    decision = _route(text, last_user="what reminders do I have",
                      last_assistant=stale["content"], last_tools="search_reminders")
    assert decision.tool_subset == ["search_reminders"]
    with patch.object(main, "build_messages", return_value=[stale]) as history:
        messages = main._tool_turn_messages(
            "synthetic-session", user_msg, max_tokens=1500, test_mode=False,
            verified_results_only=(decision.verified_results_only or
                                   main._current_schedule_source_route(decision, text)))
    assert messages == [user_msg]
    history.assert_not_called()

    decision = SimpleNamespace(tool_subset=["recall"], verified_results_only=False,
                               source="rules", reminder_action="", direct_calls=[],
                               force_first_tool=None, reason="synthetic recall")
    with patch.object(main, "build_messages", return_value=[stale]) as history:
        messages = main._tool_turn_messages(
            "synthetic-session", user_msg, max_tokens=1500, test_mode=False,
            verified_results_only=(decision.verified_results_only or
                                   main._current_schedule_source_route(decision, text)))
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


def test_daily_notification_never_claims_empty_calendar_when_unavailable() -> None:
    from service.assistant import brief
    from service.tools import email_tools

    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    states = {"calendar": {"id": "calendar", "label": "Calendar",
                           "state": "unavailable"},
              "reminders": {"id": "reminders", "label": "Reminders",
                            "state": "ready"}}
    snapshot = {"sources": [{**states["calendar"], "progress": None},
                            {**states["reminders"], "progress": 1.0},
                            {"id": "email", "label": "Email", "state": "unavailable",
                             "progress": None},
                            {"id": "messages", "label": "Messages",
                             "state": "unavailable", "progress": None}],
                "total": 4, "completed": 4, "progress": 1.0,
                "pending": [], "pending_labels": [], "syncing": False}
    for cached_event in (False, True):
        for current_reminder in (False, True):
            name = f"calendar-unavailable-{cached_event}-{current_reminder}.db"
            store = AssistantStore(Path(_SCRATCH.name) / name)
            store.sync_source("calendar", [{"source_id": "cached-event",
                                            "title": "Synthetic cached appointment",
                                            "kind": "event", "when_ts": now + 1800}]
                              if cached_event else [],
                              diagnostics={"snapshot_started_at": time.time() - 60})
            store.sync_source("reminders", [{"source_id": "current-reminder",
                                             "title": "Synthetic current reminder",
                                             "kind": "reminder", "when_ts": now + 3600}]
                              if current_reminder else [],
                              diagnostics={"snapshot_started_at": time.time()})
            with patch.object(brief, "assistant_store", store), \
                 patch.object(sync_status, "source_status", side_effect=states.get), \
                 patch.object(brief, "_mail_split", return_value={"state": "unavailable"}), \
                 patch.object(brief, "_email_section", return_value="- Mail unavailable."), \
                 patch.object(brief, "_messages_section",
                              return_value="- Messages unavailable."), \
                 patch.object(brief, "_messages_card", return_value=""), \
                 patch.object(email_tools, "email_freshness_warning", return_value=""), \
                 patch.object(email_tools, "with_email_freshness_note",
                              side_effect=lambda text, _: text):
                card = brief._today_card(now)
                delivered = asyncio.run(brief._sections("morning", snapshot=snapshot))
            for output in (card, delivered["TODAY"]):
                assert "Calendar couldn't be read" in output
                assert "nothing on your calendar" not in output
            assert delivered["READY"] == "1"
            assert "Calendar couldn't be read" in delivered["FULL"]
            if current_reminder:
                assert "Synthetic current reminder" in card
            if cached_event:
                assert "Synthetic cached appointment" not in card
