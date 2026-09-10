"""Offline route/async/backend regressions for SIM-ROUTE-1..4 and ROUTE19-1..9."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Import-time service stores must be disposable even when this file is run
# directly, rather than through the independently isolated simulation runner.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-routing-imports-")
os.environ["WISP_HOME"] = _scratch.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import service.tools  # noqa: F401
from service.reminder_intent import (
    has_alert_time, has_unsupported_alert_clock, reminder_temporal_text, resolve_alert_datetime,
)
from service.router import router as R
from service.assistant.store import AssistantStore
from service.memory.store import SessionStore
from service.tasks.compiler import compile_reminder_create, compile_reminder_update, compile_task
from service.tasks.engine import prepare_task_turn
from service.tasks.reply_engine import prepare_task_turn_async
from service.workflows.engine import prepare_turn as prepare_legacy_turn
from service.tasks.planner import InvalidTaskPlan, plan_task
from service.agent import loop
from service.tools.registry import (
    EVENT_UPDATE_UNAVAILABLE, REGISTRY, UNAVAILABLE_TOOL_REASONS,
    classify_tool_outcome, run_tool, tool_schemas,
)
from service.tools import assistant_tools
from service.tools.misc_t1 import wisp_capabilities


NOW = datetime(2026, 9, 9, 10)
CREATION = {"add_reminder", "add_calendar_event", "set_alarm"}
UPDATE_RECEIPT = (
    'Cancelled “team meeting”.\nAdded “team meeting” to your calendar for Thu Sep 10 at 3:00 PM. '
    "(If it doesn't appear, make sure the Wisp app is running and has Calendar access.)")


class ScriptedClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def ensure_only(self, *args, **kwargs):
        pass

    async def stream_events(self, model, messages, *, tools=None, **kwargs):
        self.requests.append({"tools": tools, **kwargs})
        reply = next(self.replies, "What exact time should I use for the reminder?")
        if isinstance(reply, (tuple, list)):
            calls = [reply] if isinstance(reply, tuple) else reply
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{len(self.requests)}-{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}
                for index, (name, arguments) in enumerate(calls)]}
        else:
            message = {"role": "assistant", "content": reply}
        yield {"kind": "final", "message": message}


class RoutingContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(R, "models_config", return_value={
            "tool_retrieval": {"provider": "lexical"}}))
        self.enterContext(patch.object(R, "resolve_alert_datetime", side_effect=
            lambda text: resolve_alert_datetime(text, now=NOW)))
        self.enterContext(patch.object(loop, "audit"))

    async def run_loop(self, prompt, replies, *, approve=False, test_mode=False, max_steps=4):
        d = await R.route(prompt)
        client = ScriptedClient(replies)
        emit = AsyncMock()
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=approve)})()
        result = await loop.run_agent(
            client, "Ling-3.0-tiny-oQ4e", [{"role": "user", "content": prompt}],
            emit, approver, tools=d.tool_subset, expect_tool_first=d.expect_tool_first,
            force_first_tool=d.force_first_tool, multi_round=d.multi_round,
            direct_calls=d.direct_calls,
            required_tool_groups=d.required_tool_groups, forbidden_tools=d.forbidden_tools,
            tool_argument_bindings=d.tool_argument_bindings,
            reminder_action=d.reminder_action, include_memory_context=False,
            test_mode=test_mode, max_steps=max_steps)
        return result, client, approver

    async def test_existing_note_edit_is_not_calendar_creation(self):
        for prompt in ("add a line to the meeting note",
                       "add agenda items to my meeting note"):
            with self.subTest(prompt=prompt):
                self.assertIn("append_note", R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                self.assertEqual(set(d.tool_subset), {"search_notes", "append_note"})
                self.assertEqual(d.required_tool_groups, (
                    frozenset({"search_notes"}), frozenset({"append_note"})))
                self.assertFalse(CREATION.intersection(d.tool_subset))
        for prompt in ("add a meeting to my calendar tomorrow at 3pm",
                       "add a calendar event titled meeting notes tomorrow"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["add_calendar_event"])

    async def test_reminder_reads_reach_the_overdue_inclusive_source(self):
        for prompt in ("when is my vaccine reminder", "find my dentist reminder",
                       "what does my reminder say", "find my overdue dentist reminder"):
            with self.subTest(prompt=prompt):
                self.assertIn("search_reminders", R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["search_reminders"])
                self.assertEqual(d.force_first_tool, "search_reminders")
                self.assertEqual(d.direct_calls, [])
                self.assertEqual(d.reminder_action, "")
        d = await R.route("what is on my calendar")
        self.assertEqual(d.direct_calls, [("get_upcoming", {"days": 60})])
        d = await R.route("find free time between my reminders")
        self.assertIn("find_free_time", d.tool_subset)

    async def test_reschedule_is_an_update_not_creation(self):
        for word in ("reschedule", "rescheduled", "rescheduling", "reschedules"):
            self.assertEqual(R._normalize_typos(word), word)
        for prompt, target in (
                ("reschedule the meeting to tomorrow", "update_event"),
                ("please reschedule my appointment to tomorrow", "update_event"),
                ("I need you to reschedule my reminder to tomorrow", "update_reminder"),
                ("reschedule my reminder", "update_reminder")):
            with self.subTest(prompt=prompt):
                self.assertIn(target, R.rule_route(prompt).tool_subset)
                d = await R.route(prompt)
                if target == "update_event":
                    self.assertEqual(d.tool_subset, [])
                    self.assertEqual(d.required_tool_groups, (frozenset({"update_event"}),))
                    self.assertIsNone(d.force_first_tool)
                else:
                    self.assertEqual(set(d.tool_subset), {"get_upcoming", target})
                    self.assertEqual(d.required_tool_groups, ())
                    self.assertIsNone(d.force_first_tool)
                self.assertEqual(d.reminder_action, "")
                self.assertEqual(d.tool_argument_bindings, {})
                self.assertFalse(CREATION.intersection(d.tool_subset))
        both = await R.route("reschedule my meeting and my reminder to tomorrow")
        self.assertEqual(set(both.tool_subset), {"get_upcoming", "update_reminder"})
        self.assertEqual(both.required_tool_groups, (frozenset({"update_event"}),))

    async def test_incomplete_reschedule_can_ask_without_forcing_an_update(self):
        question = "Which reminder should I reschedule, and when?"
        result, client, approver = await self.run_loop("reschedule my reminder", [question])
        self.assertEqual(result, question)
        self.assertEqual(len(client.requests), 1)
        self.assertNotEqual(client.requests[0].get("tool_choice"), "required")
        approver.confirm.assert_not_awaited()

    async def test_complete_reschedule_cannot_claim_success_without_update(self):
        prompt = "reschedule the team meeting to tomorrow at 3pm"
        d = await R.route(prompt)
        self.assertEqual(d.required_tool_groups, (frozenset({"update_event"}),))
        self.assertIsNone(d.force_first_tool)
        self.assertNotIn("update_event", d.tool_subset)
        self.assertEqual(d.tool_argument_bindings, {})
        claim = "Done — I've rescheduled the team meeting to tomorrow at 3 PM."
        result, client, approver = await self.run_loop(prompt, [claim] * 4)
        self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)
        self.assertEqual(client.requests, [])
        approver.confirm.assert_not_awaited()
        read = AsyncMock(return_value="Calendar events: 1. team meeting tomorrow at noon.")
        with patch.object(REGISTRY["get_upcoming"], "func", read):
            result, _, approver = await self.run_loop(prompt, [("get_upcoming", {}), *([claim] * 3)])
        read.assert_not_awaited()
        self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)
        approver.confirm.assert_not_awaited()

    async def test_reschedule_does_not_bind_partially_understood_destinations(self):
        for when in ("October 1 at 3pm", "next week at 3pm", "tomorrow at 3pm UTC",
                     "tomorrow at 3pm or Friday at 4pm", "tomorrow at 3pm for 30 minutes",
                     "tomorrow at 25pm", "tomorrow at 6:7"):
            with self.subTest(when=when):
                d = await R.route("reschedule the team meeting to " + when)
                self.assertEqual(d.tool_subset, [])
                self.assertIn("cancel_event", d.forbidden_tools)
                self.assertEqual(d.tool_argument_bindings, {})
                self.assertEqual(d.required_tool_groups, (frozenset({"update_event"}),))
        for when in ("tomorrow 3pm", "15:00 tomorrow", "tomorrow noon", "noon tomorrow"):
            with self.subTest(when=when):
                d = await R.route("reschedule the team meeting to " + when)
                self.assertEqual(d.tool_argument_bindings, {})
                result, _, approver = await self.run_loop(
                    "reschedule the team meeting to " + when, ["Done."])
                self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)
                approver.confirm.assert_not_awaited()

    async def test_unavailable_inventory_is_non_routable_and_never_dispatches(self):
        expected = {
            "country_info", "find_local_events", "get_lyrics", "identify_song",
            "live_captions", "lookup_media_title", "set_hotkey",
            "set_keyboard_backlight", "track_flight", "track_package",
            "transcribe_audio", "transit_info",
        }
        self.assertEqual(set(UNAVAILABLE_TOOL_REASONS), expected)
        advertised = {schema["function"]["name"] for schema in tool_schemas(None)}
        self.assertTrue(expected.isdisjoint(advertised))
        for name in sorted(expected):
            with self.subTest(tool=name):
                self.assertEqual(REGISTRY[name].unavailable_reason,
                                 UNAVAILABLE_TOOL_REASONS[name])
                self.assertNotIn("settings under api keys",
                                 (REGISTRY[name].description + " "
                                  + REGISTRY[name].unavailable_reason).lower())
                implementation = AsyncMock(side_effect=AssertionError(
                    "an unavailable compatibility implementation must not run"))
                with patch.object(REGISTRY[name], "func", implementation):
                    result = await run_tool(REGISTRY[name], {})
                implementation.assert_not_awaited()
                self.assertEqual(result, UNAVAILABLE_TOOL_REASONS[name])
                self.assertEqual(classify_tool_outcome(name, result).status, "unsupported")
                self.assertRegex(result, r"(?i)(no |cannot|nothing was|does not)")

    async def test_legacy_mixed_menu_keeps_genuine_tool_and_filters_unavailable_one(self):
        receipt = "Calendar fixture: team meeting tomorrow at noon."
        calendar = AsyncMock(return_value=receipt)
        unavailable = AsyncMock(side_effect=AssertionError(
            "unavailable compatibility implementation must not run"))
        client = ScriptedClient([("get_upcoming", {"days": 7}), "Done."])
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
        with patch.object(REGISTRY["get_upcoming"], "func", calendar), \
                patch.object(REGISTRY["country_info"], "func", unavailable):
            result = await loop.run_agent(
                client, "fixture-model", [{"role": "user", "content": "show my calendar"}],
                AsyncMock(), approver, tools=["get_upcoming", "country_info"],
                include_memory_context=False, max_steps=2)
        self.assertEqual(result, "Done.")
        calendar.assert_awaited_once_with(days=7)
        unavailable.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        self.assertGreaterEqual(len(client.requests), 1)
        for request in client.requests:
            offered = {schema["function"]["name"] for schema in request.get("tools") or []}
            self.assertIn("get_upcoming", offered)
            self.assertNotIn("country_info", offered)

    async def test_legacy_forced_unavailable_tool_returns_without_model_or_dispatch(self):
        client = ScriptedClient(["unrelated model prose"])
        unavailable = AsyncMock(side_effect=AssertionError(
            "unavailable compatibility implementation must not run"))
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
        emit = AsyncMock()
        with patch.object(REGISTRY["country_info"], "func", unavailable):
            result = await loop.run_agent(
                client, "fixture-model", [{"role": "user", "content": "show my calendar"}],
                emit, approver, tools=None, force_first_tool="country_info",
                include_memory_context=False, max_steps=1)
        self.assertEqual(result, UNAVAILABLE_TOOL_REASONS["country_info"])
        self.assertEqual(client.requests, [])
        unavailable.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        self.assertFalse(any(call.args[0].get("type") == "tool_call"
                             for call in emit.await_args_list))

    async def test_explicit_unavailable_device_route_reports_before_execution(self):
        prompt = "turn up my keyboard backlight"
        decision = await R.route(prompt)
        self.assertNotIn("set_keyboard_backlight", decision.tool_subset or ())
        self.assertEqual(decision.direct_calls, [])
        self.assertIn(frozenset({"set_keyboard_backlight"}),
                      decision.required_tool_groups)
        result, client, approver = await self.run_loop(prompt, ["Done."], approve=True)
        self.assertEqual(result, UNAVAILABLE_TOOL_REASONS["set_keyboard_backlight"])
        self.assertEqual(client.requests, [])
        approver.confirm.assert_not_awaited()

    def test_capability_report_distinguishes_scheduled_email_from_scheduled_reply(self):
        report = wisp_capabilities("email scheduling")
        self.assertIn("Yes — Schedule a new email or text for later", report)
        self.assertIn("No — schedule a reply inside an existing email thread", report)
        self.assertIn("unavailable and non-routable", report)
        self.assertNotIn("settings under API keys", report)

    async def test_compound_reminder_lookup_requires_both_actual_sources(self):
        prompt = "find my overdue dentist reminder and search my notes for dentist"
        d = await R.route(prompt)
        self.assertEqual(set(d.tool_subset), {"search_reminders", "search_notes"})
        self.assertEqual(d.required_tool_groups, (
            frozenset({"search_reminders"}), frozenset({"search_notes"})))
        reminder = AsyncMock(return_value="dentist overdue reminder fixture")
        notes = AsyncMock(return_value="dentist note fixture")
        claim = "I checked both sources."
        with patch.object(REGISTRY["search_reminders"], "func", reminder), \
                patch.object(REGISTRY["search_notes"], "func", notes):
            result, _, approver = await self.run_loop(prompt, [
                ("search_reminders", {"query": "dentist"}), *([claim] * 3)])
        self.assertNotEqual(result, claim)
        reminder.assert_awaited_once()
        notes.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        reminder.reset_mock()
        with patch.object(REGISTRY["search_reminders"], "func", reminder), \
                patch.object(REGISTRY["search_notes"], "func", notes):
            result, _, approver = await self.run_loop(prompt, [
                ("search_reminders", {"query": "dentist"}),
                ("search_notes", {"query": "dentist"}), claim])
        self.assertEqual(result, claim)
        reminder.assert_awaited_once()
        notes.assert_awaited_once()
        approver.confirm.assert_not_awaited()

    async def test_source_names_in_notes_query_are_content_not_obligations(self):
        for prompt, expected in (
                ("search my notes for reminder ideas", {"search_notes"}),
                ("search my notes about overdue reminders", {"search_notes"}),
                ("find reminder ideas in my notes", {"search_notes"}),
                ("search my notes for dentist and reminder ideas", {"search_notes"}),
                ('search my notes for "find my overdue dentist reminder"', {"search_notes"}),
                ("search my notes for 'dentist and check my reminders'", {"search_notes"}),
                ("search my notes for ‘dentist and check my reminders’", {"search_notes"}),
                ("search my notes for ideas from my reminders", {"search_notes"}),
                ("search my reminders for quotes from my notes", {"search_reminders"}),
                ("search my reminders for notes ideas", {"search_reminders"}),
                ("search my notes and reminders for dentist", {"search_notes", "search_reminders"}),
                ("search my reminders and notes for dentist", {"search_notes", "search_reminders"}),
                ("search my notes for dentist and find my overdue dentist reminder",
                 {"search_notes", "search_reminders"})):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(set(d.tool_subset), expected)
                self.assertTrue(all(group <= expected for group in d.required_tool_groups))
        prompt = "search my notes for reminder ideas"
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-note-entry-")))
        sessions, assistant = SessionStore(root / "sessions.db"), AssistantStore(root / "assistant.db")
        self.addCleanup(sessions._db.close)
        self.addCleanup(assistant._db.close)
        sid = sessions.create_session()
        self.assertIsNone(await prepare_task_turn_async(
            sessions, sid, prompt, assistant_store=assistant, now=NOW, allow_native=False))
        self.assertIsNone(prepare_legacy_turn(sessions, sid, prompt))
        notes = AsyncMock(return_value="Fixture note: reminder ideas include drinking water.")
        reminders = AsyncMock(side_effect=AssertionError("Reminders was not requested"))
        answer = "Your note suggests drinking water."
        with patch.object(REGISTRY["search_notes"], "func", notes), \
                patch.object(REGISTRY["search_reminders"], "func", reminders):
            result, _, approver = await self.run_loop(prompt, [
                ("search_notes", {"query": "reminder ideas"}), answer])
        self.assertEqual(result, answer)
        notes.assert_awaited_once()
        reminders.assert_not_awaited()
        approver.confirm.assert_not_awaited()

    async def test_real_calendar_backend_is_unavailable_without_preservation_safe_update(self):
        from service.assistant.hub import hub
        from service.assistant import sync_status

        # Real disposable SQLite rows, including collapsed cross-source
        # duplicates and repeated EventKit occurrence identifiers. The only
        # intercepted effects are approval and the Hub/native bridge.
        fixtures = (
            ("empty", [], "available"),
            ("reminder-only", [("reminders", "team meeting", 0)], "available"),
            ("calendar-metadata", [("calendar", "Quarterly team meeting", 0)], "available"),
            ("cross-source-duplicate", [("calendar", "team meeting", 0),
                                        ("reminders", "team meeting", 0)], "available"),
            ("ambiguous", [("calendar", "team meeting", 0),
                            ("calendar", "team meeting", 3600)], "available"),
            ("syncing", [("calendar", "team meeting", 0)], "syncing"),
            ("unavailable", [("calendar", "team meeting", 0)], "unavailable"),
            ("stale", [("calendar", "team meeting", -172800)], "available"),
        )
        for label, rows, state in fixtures:
            for entry in ("routed-loop", "direct-backend", "registry"):
                with self.subTest(fixture=label, entry=entry):
                    root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-event-guard-")))
                    store = AssistantStore(root / "assistant.db")
                    sessions = SessionStore(root / "sessions.db")
                    self.addCleanup(store._db.close)
                    self.addCleanup(sessions._db.close)
                    for source in {row[0] for row in rows}:
                        store.sync_source(source, [{
                            "source_id": "fixture-occurrence", "kind": "event" if source == "calendar" else "reminder",
                            "title": title, "when_ts": (NOW + timedelta(days=1)).timestamp() + offset,
                            "location": "Room 42", "context": "Work calendar", "account": "fixture",
                            "organizer": "Fixture organizer", "url": "https://example.invalid/event",
                            # Intentionally ignored by the current store: this
                            # missing preservation metadata must not become 60.
                            "duration_min": 90,
                        } for row_source, title, offset in rows if row_source == source])
                    before = [tuple(row) for row in store._db.execute("SELECT * FROM commitments ORDER BY id")]
                    ready = {"sources": [
                        {"id": "calendar", "label": "Calendar", "state": state},
                        {"id": "reminders", "label": "Reminders", "state": "available"}]}
                    publish = AsyncMock()
                    args = {"title": "team meeting", "when_iso": "2026-09-10T15:00",
                            "duration_min": 90, "location": "Room 42", "new_title": "Quarterly team meeting"}
                    with patch.object(assistant_tools, "assistant_store", store), \
                            patch.object(assistant_tools.time, "time", return_value=NOW.timestamp()), \
                            patch.object(hub, "publish", publish), \
                            patch.object(sync_status, "ensure_sources", AsyncMock(return_value=ready)):
                        if entry == "routed-loop":
                            prompt = "reschedule the team meeting to tomorrow at 3pm"
                            sid = sessions.create_session()
                            self.assertIsNone(await prepare_task_turn_async(
                                sessions, sid, prompt, assistant_store=store, now=NOW, allow_native=False))
                            self.assertIsNone(prepare_legacy_turn(sessions, sid, prompt))
                            result, _, approver = await self.run_loop(prompt, [
                                ("get_upcoming", {}), ("update_event", args), "Confirmed rescheduled in Calendar."],
                                approve=True)
                        elif entry == "direct-backend":
                            result = await assistant_tools.update_event(**args)
                        else:
                            result = await run_tool(REGISTRY["update_event"], args)
                    after = [tuple(row) for row in store._db.execute("SELECT * FROM commitments ORDER BY id")]
                    self.assertEqual(after, before, "All stored event and Reminder metadata must remain unchanged")
                    publish.assert_not_awaited()
                    if entry == "routed-loop":
                        approver.confirm.assert_not_awaited()
                    self.assertIn("unavailable", result.lower())
                    self.assertIn("preserv", result.lower())
                    self.assertNotIn("Confirmed rescheduled", result)
                    self.assertEqual(classify_tool_outcome("update_event", result).status, "needs_input")

    async def test_calendar_discovery_empty_or_degraded_is_not_success(self):
        from service.assistant.hub import hub
        from service.assistant import sync_status

        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-calendar-read-")))
        store = AssistantStore(root / "assistant.db")
        self.addCleanup(store._db.close)
        publish = AsyncMock()
        for state, expected in (("available", "no_match"), ("syncing", "needs_input"),
                                ("unavailable", "failed")):
            with self.subTest(state=state):
                ready = {"sources": [{"id": "calendar", "label": "Calendar", "state": state},
                                     {"id": "reminders", "label": "Reminders", "state": "available"}]}
                with patch.object(assistant_tools, "assistant_store", store), \
                        patch.object(assistant_tools.time, "time", return_value=NOW.timestamp()), \
                        patch.object(hub, "publish", publish), \
                        patch.object(sync_status, "ensure_sources", AsyncMock(return_value=ready)):
                    result = await assistant_tools.get_upcoming(days=60, query="team meeting", calendar_only=True)
                self.assertEqual(classify_tool_outcome("get_upcoming", result).status, expected)
        publish.assert_not_awaited()

    def test_update_event_old_cancel_recreate_receipts_are_not_success(self):
        for receipt, expected in (
                (EVENT_UPDATE_UNAVAILABLE, "needs_input"),
                (UPDATE_RECEIPT, "failed"),
                ('Nothing upcoming or past matches “team meeting”.', "needs_input"),
                ('Several items match “team meeting”. Which one?', "needs_input"),
                ('Cancelled “team meeting”.', "failed"),
                (UPDATE_RECEIPT.split("\n")[1], "failed"),
                ('Cancelled “team meeting”.\n(bad when_iso)', "failed"),
                ("The event was updated.", "failed"), ("", "failed")):
            with self.subTest(receipt=receipt):
                outcome = classify_tool_outcome("update_event", receipt)
                self.assertEqual(outcome.status, expected)
                self.assertEqual(outcome.effect, "updated")
        self.assertEqual(classify_tool_outcome("update_event", UPDATE_RECEIPT,
                                              planned=True).status, "planned")
        self.assertEqual(classify_tool_outcome("update_event", UPDATE_RECEIPT,
                                              denied=True).status, "denied")

    async def test_reschedule_unavailability_precedes_model_and_effect_dispatch(self):
        prompt = "reschedule the team meeting to tomorrow at 3pm"
        read = AsyncMock(return_value="Calendar events: 1. team meeting tomorrow at noon.")
        update = AsyncMock(return_value=UPDATE_RECEIPT)
        redirected = {"title": "unrelated event", "when_iso": "2030-01-01T00:00",
                      "new_title": "injected rename", "location": "injected location", "duration_min": 999}
        with patch.object(REGISTRY["get_upcoming"], "func", read), \
                patch.object(REGISTRY["update_event"], "func", update):
            result, _, approver = await self.run_loop(prompt, [
                ("update_event", redirected),
                ("get_upcoming", {"days": 1, "period": "tomorrow", "query": "unrelated event",
                                  "calendar_only": False}),
                ("update_event", redirected), ("update_event", redirected),
                "Done — confirmed in the native Calendar app."], approve=True, max_steps=6)
        read.assert_not_awaited()
        update.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)

    async def test_reschedule_model_and_direct_calls_cannot_bypass_unavailability(self):
        prompt = "reschedule the team meeting to tomorrow at 3pm"
        args = {"title": "team meeting", "when_iso": "2026-09-10T15:00"}
        for direct in (False, True):
            for dry_run in (False, True):
                with self.subTest(direct=direct, dry_run=dry_run):
                    approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
                    update = AsyncMock(side_effect=AssertionError("unavailable effect must not dispatch"))
                    emit = AsyncMock()
                    with patch.object(REGISTRY["update_event"], "func", update):
                        result = await loop.run_agent(
                            ScriptedClient([("update_event", args), "I've rescheduled it."]),
                            "fixture-model", [{"role": "user", "content": prompt}], emit, approver,
                            tools=["update_event"], direct_calls=[("update_event", args)] if direct else [],
                            include_memory_context=False, test_mode=dry_run, max_steps=3)
                    update.assert_not_awaited()
                    approver.confirm.assert_not_awaited()
                    self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)
                    self.assertFalse(any(call.args[0].get("type") == "tool_call" for call in emit.await_args_list))

    async def test_unavailable_update_preflights_legacy_direct_mixed_tool_batch(self):
        args = {"title": "team meeting", "when_iso": "2026-09-10T15:00"}
        create = ("add_calendar_event", args)
        unavailable = ("update_event", args)
        receipt = "Added “separate event” to your calendar for tomorrow. (Native fixture only.)"
        approver = type("Approver", (), {"confirm": AsyncMock(return_value=True)})()
        created, updated = AsyncMock(return_value=receipt), AsyncMock(return_value=UPDATE_RECEIPT)
        with patch.object(REGISTRY["add_calendar_event"], "func", created), \
                patch.object(REGISTRY["update_event"], "func", updated):
            result = await loop.run_agent(
                ScriptedClient([[create, create, unavailable]]), "fixture-model",
                [{"role": "user", "content": "Create two events and reschedule the meeting."}],
                AsyncMock(), approver, tools=["add_calendar_event", "update_event"],
                direct_calls=[create, create, unavailable],
                include_memory_context=False, max_steps=3)
        self.assertEqual(result, EVENT_UPDATE_UNAVAILABLE)
        approver.confirm.assert_not_awaited()
        created.assert_not_awaited()
        updated.assert_not_awaited()

    async def test_embedded_future_task_verbs_do_not_hijack_creation(self):
        for prompt in ("create a reminder tomorrow to reschedule my meeting",
                       "remind me tomorrow to reschedule my dentist reminder",
                       "remind me to find my dentist reminder tomorrow",
                       "remind me tomorrow to append a line to my meeting note"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.reminder_action, "create")
                self.assertIn("add_reminder", d.tool_subset)
                self.assertFalse({"update_event", "update_reminder", "append_note"}
                                 .intersection(d.tool_subset))

    async def test_capability_lists_cannot_act_or_start_reminder_clarification(self):
        for prompt in ("Can you send texts and create reminders?",
                       "Can you send text messages, create reminders, and read browser history?"):
            with self.subTest(prompt=prompt):
                d = await R.route(prompt)
                self.assertEqual(d.tool_subset, ["wisp_capabilities"])
                self.assertEqual(d.direct_calls[0][0], "wisp_capabilities")
                self.assertEqual(d.required_tool_groups, (frozenset({"wisp_capabilities"}),))
                self.assertEqual(d.reminder_action, "")
                self.assertIsNone(d.force_first_tool)
                self.assertEqual(d.tool_argument_bindings, {})
        d = await R.route("can you send texts to Mom and create reminders for tomorrow")
        self.assertNotIn("wisp_capabilities", d.tool_subset)
        self.assertIn("send_message", d.tool_subset)

    def assertClockClarification(self, d):
        self.assertEqual(d.reminder_action, "clarify_time")
        self.assertEqual(d.tool_subset, ["get_upcoming"])
        self.assertFalse(CREATION.intersection(d.tool_subset))
        self.assertEqual(d.direct_calls, [])
        self.assertIsNone(d.force_first_tool)
        self.assertFalse(d.expect_tool_first)
        self.assertEqual(d.required_tool_groups, ())
        self.assertEqual(d.tool_argument_bindings, {})

    async def test_unsupported_clock_initial_and_followup_cannot_create(self):
        for clock in ("half six tomorrow", "half past six tomorrow", "quarter to six tomorrow",
                      "tomorrow at six", "tomorrow at 6:7", "tomorrow at 25:00",
                      "6 or 7 tomorrow", "six pm tomorrow", "25pm tomorrow"):
            with self.subTest(clock=clock):
                self.assertTrue(has_unsupported_alert_clock(clock))
                self.assertFalse(has_alert_time(clock))
                self.assertIsNone(resolve_alert_datetime(clock, now=NOW))
                self.assertClockClarification(await R.route("set an alarm for " + clock))
                self.assertClockClarification(await R.route(
                    clock, last_user="set an alarm for my medicine",
                    last_assistant="What time should I set the reminder?"))
        for prompt in ("set an alarm tomorrow 25:00 to take medicine",
                       "set an alarm tomorrow 6:7 to take medicine",
                       "set an alarm at six thirty tomorrow to take medicine",
                       "set an alarm six thirty tomorrow to take medicine"):
            with self.subTest(prompt=prompt):
                self.assertClockClarification(await R.route(prompt))
        self.assertClockClarification(await R.route("set an alarm at half six"))
        unrelated = await R.route("show my notes about half six",
                                  last_assistant="What time should I set the reminder?")
        self.assertEqual(unrelated.tool_subset, ["search_notes"])
        self.assertEqual(unrelated.reminder_action, "")
        question = await R.route("what does half six mean?",
                                 last_assistant="What time should I set the reminder?")
        self.assertEqual(question.reminder_action, "")

    async def test_supported_clock_date_defaults_and_relative_times_are_preserved(self):
        for prompt, expected in (
                ("set an alarm at 6:30 am tomorrow", "2026-09-10T06:30"),
                ("set an alarm tomorrow", "2026-09-10T09:00"),
                ("set an alarm tomorrow at 6", "2026-09-10T18:00"),
                ("remind me tomorrow at 9am to prepare for my 1:1", "2026-09-10T09:00"),
                ("set an alarm at noon tomorrow", "2026-09-10T12:00"),
                ("set an alarm at 6pm tomorrow for half an hour of exercise", "2026-09-10T18:00")):
            with self.subTest(prompt=prompt):
                self.assertFalse(has_unsupported_alert_clock(prompt))
                self.assertEqual(resolve_alert_datetime(prompt, now=NOW).isoformat(timespec="minutes"), expected)
                d = await R.route(prompt)
                self.assertEqual(d.reminder_action, "create")
                self.assertEqual(d.tool_argument_bindings["add_reminder"]["when_iso"], expected)
        for text in ("in half an hour", "30 minutes before", "the day before"):
            with self.subTest(text=text):
                self.assertFalse(has_unsupported_alert_clock(text))
                self.assertTrue(has_alert_time(text))
        d = await R.route("set an alarm for my repair appointment tomorrow at 1pm")
        self.assertEqual(d.reminder_action, "clarify_time")

    async def test_attempted_creation_during_clock_clarification_is_never_executed(self):
        effect = AsyncMock(side_effect=AssertionError("creation must not execute"))
        # Real dispatch rejection, not test_mode's dry-run interception.
        with patch.object(REGISTRY["add_reminder"], "func", effect):
            result, _, approver = await self.run_loop(
                "set an alarm for half six tomorrow", [
                    ("add_reminder", {"title": "medicine", "when_iso": "2026-09-10T09:00"}),
                    "What exact time should I use for the reminder?"])
        effect.assert_not_awaited()
        approver.confirm.assert_not_awaited()
        self.assertNotIn("Reminder set:", result)
        self.assertIn("?", result)


class TypedClockContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-routing-contract-")))
        self.sessions = SessionStore(root / "sessions.db")
        self.assistant = AssistantStore(root / "assistant.db")
        self.addCleanup(self.sessions._db.close)
        self.addCleanup(self.assistant._db.close)
        self.sid = self.sessions.create_session()

    def prepare(self, prompt):
        return prepare_task_turn(self.sessions, self.sid, prompt,
                                 assistant_store=self.assistant, now=NOW)

    def test_reminder_search_reads_overdue_records_and_excludes_calendar_events(self):
        self.assistant.add_manual("dentist overdue", (NOW - timedelta(days=1)).timestamp())
        self.assistant.add_manual("dentist upcoming", (NOW + timedelta(days=1)).timestamp())
        self.assistant.sync_source("calendar", [{
            "source_id": "fixture-meeting", "kind": "event", "title": "dentist calendar event",
            "when_ts": (NOW + timedelta(days=1)).timestamp()}])
        with patch.object(assistant_tools, "assistant_store", self.assistant):
            result = asyncio.run(assistant_tools.search_reminders("dentist"))
        self.assertIn("dentist overdue", result)
        self.assertIn("dentist upcoming", result)
        self.assertNotIn("dentist calendar event", result)

    def test_initial_typed_creation_and_update_have_no_guessed_time(self):
        for prompt in ("set an alarm for half six tomorrow",
                       "set an alarm at half six tomorrow to take medicine",
                       "set an alarm for 6 or 7 tomorrow",
                       "set an alarm for six pm tomorrow", "set an alarm for 25pm tomorrow"):
            with self.subTest(prompt=prompt):
                plan = compile_reminder_create(prompt, now=NOW)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.status, "waiting_for_input")
                self.assertEqual(plan.temporal.absolute_iso, "")
                self.assertEqual(plan.temporal.defaulted_part_of_day, "")
                self.assertIn("temporal.time", plan.missing_slots)
                with self.assertRaises(InvalidTaskPlan):
                    plan_task(plan)
        plan = compile_reminder_update("reschedule my medicine reminder to tomorrow at half six", now=NOW)
        self.assertEqual(plan.target.value, "medicine")
        self.assertEqual(plan.temporal.absolute_iso, "")
        self.assertIn("update.change", plan.missing_slots)
        valid = compile_reminder_create("remind me tomorrow at 9am to prepare for my 1:1", now=NOW)
        self.assertEqual(valid.status, "ready")
        self.assertEqual(valid.temporal.absolute_iso, "2026-09-10T09:00")
        self.assertEqual(valid.subject.value, "prepare for my 1:1")

    def test_pending_create_preserves_plan_then_accepts_precise_reply(self):
        first = self.prepare("set an alarm at half six tomorrow to take medicine")
        self.assertFalse(first.executable)
        self.assertIn("temporal.time", first.plan.missing_slots)
        snapshot = self.sessions.active_task(self.sid)
        for reply in ("half six tomorrow", "tomorrow at 6:7", "6 or 7 tomorrow"):
            second = self.prepare(reply)
            self.assertIsNotNone(second)
            self.assertFalse(second.executable)
            self.assertEqual(second.plan.steps, [])
            self.assertEqual(second.event, "clock_clarification")
            self.assertEqual(self.sessions.active_task(self.sid), snapshot)
        final = self.prepare("6:30 am tomorrow")
        self.assertTrue(final.executable)
        self.assertEqual(final.plan.id, first.plan.id)
        self.assertEqual(final.plan.subject.value, first.plan.subject.value)
        self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T06:30")
        self.assertEqual([step.tool for step in final.plan.steps], ["add_reminder"])

    def test_pending_update_preserves_target_then_accepts_precise_reply(self):
        row = self.assistant.add_manual("medicine", (NOW + timedelta(days=2)).timestamp())
        first = self.prepare("reschedule my medicine reminder")
        self.assertFalse(first.executable)
        snapshot = self.sessions.active_task(self.sid)
        second = self.prepare("half six tomorrow")
        self.assertFalse(second.executable)
        self.assertEqual(second.event, "clock_clarification")
        self.assertEqual(self.sessions.active_task(self.sid), snapshot)
        final = self.prepare("6:30 am tomorrow")
        self.assertTrue(final.executable)
        self.assertEqual(final.plan.id, first.plan.id)
        self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T06:30")
        self.assertEqual(final.plan.steps[0].tool, "update_reminder")
        self.assertEqual(final.plan.steps[0].args["expected_id"], row["id"])

    def test_pending_clock_does_not_swallow_cancellation_or_new_task(self):
        self.prepare("set an alarm at half six tomorrow to take medicine")
        self.assertIsNone(self.prepare("check my email"))
        self.assertIsNone(self.prepare("show my notes about half six"))
        cancelled = self.prepare("cancel")
        self.assertEqual(cancelled.plan.status, "cancelled")
        self.assertFalse(cancelled.executable)
        fresh = self.prepare("remind me tomorrow at noon to drink water")
        self.assertTrue(fresh.executable)
        self.assertEqual(fresh.plan.temporal.absolute_iso, "2026-09-10T12:00")


class AsyncEntryContractTests(unittest.IsolatedAsyncioTestCase):
    """Use production entry order, not just the downstream route oracle."""

    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="wisp-routing-entry-")))
        self.sessions = SessionStore(root / "sessions.db")
        self.assistant = AssistantStore(root / "assistant.db")
        self.addCleanup(self.sessions._db.close)
        self.addCleanup(self.assistant._db.close)
        self.enterContext(patch.object(R, "models_config", return_value={
            "tool_retrieval": {"provider": "lexical"}}))

    async def prepare(self, sid, prompt):
        return await prepare_task_turn_async(
            self.sessions, sid, prompt, assistant_store=self.assistant,
            now=NOW, allow_native=False,
            contacts_resolver=lambda name: [])

    async def test_scheduled_email_reply_stops_before_native_source_or_send_execution(self):
        for prompt in (
                "reply to Dan's email tomorrow saying Thanks",
                "reply all to the email from Dan at 6pm saying Thanks everyone",
                "respond to the email from Dan in 20 minutes saying Got it",
                "schedule a reply to Dan's email for tomorrow saying Thanks",
                "schedule an email response to Dan's email saying Thanks",
                "reply to Dan's email next Monday saying Thanks",
                "reply to Dan's email this evening saying Thanks",
                "reply to Dan's email Friday saying Thanks"):
            with self.subTest(prompt=prompt):
                reply = AsyncMock(side_effect=AssertionError("reply must not send"))
                scheduled = AsyncMock(side_effect=AssertionError("standalone send must not schedule"))
                prepare = AsyncMock(side_effect=AssertionError("native reply must not prepare"))
                warm = AsyncMock(side_effect=AssertionError("Mail must not warm"))
                with patch.object(REGISTRY["reply_to_email"], "func", reply), \
                        patch.object(REGISTRY["schedule_send"], "func", scheduled), \
                        patch("service.tools.email_tools.ensure_reply_source", warm), \
                        patch("service.tasks.source_readers.current_mail_reader",
                              side_effect=AssertionError("Mail reader must not be constructed")) as read:
                    turn = await prepare_task_turn_async(
                        self.sessions, self.sessions.create_session(), prompt,
                        assistant_store=self.assistant, now=NOW, allow_native=True,
                        reply_preparer=prepare, contacts_resolver=lambda name: [])
                self.assertIsNotNone(turn)
                self.assertFalse(turn.executable)
                self.assertEqual(turn.event, "reply_schedule_unsupported")
                self.assertEqual(
                    turn.response,
                    "Scheduling an email reply isn’t supported yet. Nothing was sent.",
                )
                self.assertIn("reply.schedule", turn.plan.missing_slots)
                self.assertEqual(turn.plan.steps, [])
                warm.assert_not_awaited()
                read.assert_not_called()
                prepare.assert_not_awaited()
                reply.assert_not_awaited()
                scheduled.assert_not_awaited()

    async def test_scheduled_reply_clarification_stops_before_native_mail_access(self):
        sid = self.sessions.create_session()
        asked = await prepare_task_turn_async(
            self.sessions, sid, "reply to Dan's email saying Thanks at 6pm",
            assistant_store=self.assistant, now=NOW, allow_native=True,
            mail_reader=object(), reply_preparer=AsyncMock())
        self.assertEqual(asked.event, "language_clarification")

        for answer in ("send then", "when to send", "delivery time"):
            with self.subTest(answer=answer):
                # Restore the same pending clarification for each independent
                # production-mode ordering check.
                if answer != "send then":
                    sid = self.sessions.create_session()
                    await prepare_task_turn_async(
                        self.sessions, sid, "reply to Dan's email saying Thanks at 6pm",
                        assistant_store=self.assistant, now=NOW, allow_native=True,
                        mail_reader=object(), reply_preparer=AsyncMock())
                warm = AsyncMock(side_effect=AssertionError("Mail must not warm"))
                prepare = AsyncMock(side_effect=AssertionError("native reply must not prepare"))
                with patch("service.tools.email_tools.ensure_reply_source", warm), \
                        patch("service.tasks.source_readers.current_mail_reader",
                              side_effect=AssertionError("Mail reader must not be constructed")) as read:
                    turn = await prepare_task_turn_async(
                        self.sessions, sid, answer, assistant_store=self.assistant,
                        now=NOW, allow_native=True, reply_preparer=prepare)
                self.assertEqual(turn.event, "reply_schedule_unsupported")
                self.assertFalse(turn.executable)
                self.assertEqual(
                    turn.response,
                    "Scheduling an email reply isn’t supported yet. Nothing was sent.",
                )
                self.assertEqual(turn.plan.steps, [])
                warm.assert_not_awaited()
                read.assert_not_called()
                prepare.assert_not_awaited()

    def test_future_reply_phrases_are_typed_as_unsupported_scheduling(self):
        for when in ("next Monday", "this evening", "Friday", "next Friday morning",
                     "in six hours", "at noon", "on Tuesday"):
            with self.subTest(when=when):
                plan = compile_task(f"reply to Dan's email {when} saying Thanks", now=NOW)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.intent, "email.reply")
                self.assertIn("reply.schedule", plan.missing_slots)

    def test_scheduled_reply_limitation_is_in_both_tool_contracts(self):
        schedule = REGISTRY["schedule_send"].description
        reply = REGISTRY["reply_to_email"].description
        self.assertIn("NEW standalone email or text", schedule)
        self.assertIn("cannot schedule a reply", schedule)
        self.assertIn("scheduling a reply in-thread is not supported", reply)
        self.assertIn("do not substitute schedule_send", reply)

    async def test_trailing_unsupported_clock_evidence_survives_courtesy_and_minute_words(self):
        for prompt in ("remind me tomorrow to take medicine at half six please",
                       "remind me tomorrow to take medicine at 25pm please",
                       "remind me tomorrow to take medicine at 6:7 please",
                       "remind me to take medicine at six thirty tomorrow",
                "remind me tomorrow to take medicine at 25pm, thanks",
                "remind me tomorrow to take medicine six thirty tomorrow please",
                "remind me tomorrow to take medicine at six please",
                "remind me tomorrow to take medicine at six oh five please",
                "remind me tomorrow to take medicine at 6 oh five please"):
            with self.subTest(prompt=prompt):
                sid = self.sessions.create_session()
                turn = await self.prepare(sid, prompt)
                self.assertFalse(turn.executable)
                self.assertEqual(turn.plan.temporal.absolute_iso, "")
                self.assertEqual(turn.plan.steps, [])
                self.assertIn("temporal.time", turn.plan.missing_slots)
                self.assertTrue(has_unsupported_alert_clock(prompt))
                scoped = reminder_temporal_text(prompt)
                self.assertEqual(reminder_temporal_text(scoped), scoped)

    async def test_subject_clock_words_do_not_override_explicit_schedule(self):
        for subject in ("reserve a table for six", "discuss half six with my tutor",
                        "review the invalid timestamp 25pm", "prepare for my 1:1",
                        "meet at 6pm and prepare for my 1:1",
                        "meet at 6pm and reserve a table for six",
                        "look at tenacity in my notes about half six"):
            with self.subTest(subject=subject):
                sid = self.sessions.create_session()
                prompt = "remind me tomorrow at 9am to " + subject
                turn = await self.prepare(sid, prompt)
                self.assertTrue(turn.executable)
                self.assertEqual(turn.plan.intent, "reminder.create")
                self.assertEqual(turn.plan.subject.value, subject)
                self.assertEqual(turn.plan.temporal.absolute_iso, "2026-09-10T09:00")
                self.assertEqual([step.tool for step in turn.plan.steps], ["add_reminder"])
                self.assertFalse(has_unsupported_alert_clock(prompt))
                self.assertEqual(resolve_alert_datetime(prompt, now=NOW).hour, 9)
        for prompt, subject in (
                ("remind me to reserve a table for six tomorrow at 9am", "reserve a table for six"),
                ("remind me at 9am tomorrow to reserve a table for six", "reserve a table for six")):
            with self.subTest(prompt=prompt):
                turn = await self.prepare(self.sessions.create_session(), prompt)
                self.assertTrue(turn.executable)
                self.assertEqual(turn.plan.subject.value, subject)
                self.assertEqual(turn.plan.temporal.absolute_iso, "2026-09-10T09:00")
        for prompt in ("remind me to take medicine tomorrow at half six",
                       "remind me tomorrow at 9am to take medicine tomorrow at half six",
                       "remind me to take medicine for six tomorrow",
                       "remind me tomorrow at 9am to take medicine at 123:45 tomorrow",
                       "remind me at quarter to six tomorrow to take medicine",
                       "set an alarm tomorrow 25:00 to take medicine",
                       "set an alarm tomorrow 6:7 to take medicine",
                       "set an alarm at six thirty tomorrow to take medicine",
                       "set an alarm six thirty tomorrow to take medicine"):
            with self.subTest(prompt=prompt):
                turn = await self.prepare(self.sessions.create_session(), prompt)
                self.assertFalse(turn.executable)
                self.assertIn("temporal.time", turn.plan.missing_slots)
                self.assertEqual(turn.plan.temporal.absolute_iso, "")

    def test_overlapping_clock_and_date_spans_are_idempotent(self):
        for prompt in (
                "remind me to reserve a table for six tomorrow at 9am",
                "remind me to take medicine tomorrow at half six",
                "remind me to take medicine tomorrow at 9am",
                "remind me tomorrow to take medicine at 9am",
                "remind me to take medicine at six thirty tomorrow please"):
            with self.subTest(prompt=prompt):
                scoped = reminder_temporal_text(prompt)
                self.assertEqual(reminder_temporal_text(scoped), scoped)

    async def test_correction_prefix_cannot_default_an_unsupported_clock(self):
        for initial in ("set an alarm at half six tomorrow to take medicine",
                        "reschedule my medicine reminder"):
            self.assistant.add_manual("medicine", (NOW + timedelta(days=2)).timestamp())
            for reply in ("actually half six tomorrow", "please make it half six tomorrow",
                          "around half six tomorrow", "make it around half six tomorrow",
                          "I meant half six tomorrow", "actually tomorrow at 6:7",
                          "actually 6:7 tomorrow", "I meant 25:00 tomorrow",
                          "please make it tomorrow at 25:00"):
                with self.subTest(initial=initial, reply=reply):
                    sid = self.sessions.create_session()
                    first = await self.prepare(sid, initial)
                    self.assertFalse(first.executable)
                    snapshot = self.sessions.active_task(sid)
                    second = await self.prepare(sid, reply)
                    self.assertFalse(second.executable)
                    self.assertEqual(second.event, "clock_clarification")
                    self.assertEqual(second.plan.steps, [])
                    self.assertEqual(self.sessions.active_task(sid), snapshot)
                    final = await self.prepare(sid, "6:30 am tomorrow")
                    self.assertTrue(final.executable)
                    self.assertEqual(final.plan.id, first.plan.id)
                    self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T06:30")

    async def test_subject_answer_is_not_consumed_as_a_time_answer(self):
        for subject in ("reserve a table for six", "prepare for my 1:1",
                        "discuss half six with my tutor", "review 25pm tomorrow"):
            with self.subTest(subject=subject):
                sid = self.sessions.create_session()
                first = await self.prepare(sid, "remind me")
                self.assertIn("subject", first.plan.missing_slots)
                second = await self.prepare(sid, subject)
                self.assertFalse(second.executable)
                self.assertEqual(second.plan.subject.value, subject)
                self.assertEqual(second.plan.temporal.absolute_iso, "")
                self.assertIn("temporal.time", second.plan.missing_slots)
                final = await self.prepare(sid, "9am tomorrow")
                self.assertTrue(final.executable)
                self.assertEqual(final.plan.subject.value, subject)
                self.assertEqual(final.plan.temporal.absolute_iso, "2026-09-10T09:00")

    async def test_pending_clock_preserves_new_requests_questions_and_cancellation(self):
        sid = self.sessions.create_session()
        await self.prepare(sid, "set an alarm at half six tomorrow to take medicine")
        snapshot = self.sessions.active_task(sid)
        for prompt in ("actually show my notes about half six", "what does half six mean?",
                       "please explain what half six means", "check my email"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(await self.prepare(sid, prompt))
                self.assertEqual(self.sessions.active_task(sid), snapshot)
        cancelled = await self.prepare(sid, "cancel")
        self.assertFalse(cancelled.executable)
        self.assertEqual(cancelled.plan.status, "cancelled")
        fresh = await self.prepare(sid, "remind me tomorrow at noon to drink water")
        self.assertTrue(fresh.executable)
        self.assertEqual(fresh.plan.temporal.absolute_iso, "2026-09-10T12:00")

    async def test_capability_inventory_declines_typed_entry_then_routes_read_only(self):
        for prompt in ("Can you send texts and create reminders?",
                       "Can you send texts or create reminders for tomorrow at 3pm?",
                       "Are you able to send texts and create reminders for tomorrow at 3pm?",
                       "Can you send text messages, create reminders, and read browser history?"):
            for pending in (False, True):
                with self.subTest(prompt=prompt, pending=pending):
                    sid = self.sessions.create_session()
                    if pending:
                        await self.prepare(sid, "remind me to drink water")
                    snapshot = self.sessions.active_task(sid)
                    self.assertIsNone(await self.prepare(sid, prompt))
                    self.assertEqual(self.sessions.active_task(sid), snapshot)
                    d = await R.route(prompt)
                    self.assertEqual(d.tool_subset, ["wisp_capabilities"])
                    self.assertEqual(d.reminder_action, "")
                    self.assertFalse(CREATION.intersection(d.tool_subset))

    async def test_capability_inventory_preserves_pending_legacy_workflow(self):
        for initial, answer, waiting in (
                ("schedule a message to Mom with my calendar summary", "tomorrow at 3pm", "waiting_for_time"),
                ("send Mom my email summaries and my calendar for this month", "Messages", "waiting_for_channel"),
                ("send my calendar summary via Messages", "Mom", "waiting_for_recipient")):
            with self.subTest(initial=initial):
                sid = self.sessions.create_session()
                self.assertIsNone(await self.prepare(sid, initial))
                first = prepare_legacy_turn(self.sessions, sid, initial)
                self.assertEqual(first.plan.status, waiting)
                snapshot = self.sessions.active_workflow(sid)
                for inventory in ("Can you send texts and create reminders?",
                                  "Are you able to send texts and create reminders for tomorrow at 3pm?"):
                    self.assertIsNone(await self.prepare(sid, inventory))
                    self.assertIsNone(prepare_legacy_turn(self.sessions, sid, inventory))
                    self.assertEqual(self.sessions.active_workflow(sid), snapshot)
                    self.assertIsNone(self.sessions.active_task(sid))
                    decision = await R.route(inventory)
                    self.assertEqual(decision.tool_subset, ["wisp_capabilities"])
                self.assertIsNone(await self.prepare(sid, answer))
                resumed = prepare_legacy_turn(self.sessions, sid, answer)
                self.assertEqual(resumed.plan.id, first.plan.id)
                self.assertIsNotNone(resumed.decision)
                self.assertEqual(resumed.plan.status, "running")
                self.assertIn("lookup_contact", resumed.decision.tool_subset)
                self.assertIn("schedule_send" if waiting == "waiting_for_time" else "send_message",
                              resumed.decision.tool_subset)
                if waiting == "waiting_for_time":
                    self.assertEqual(resumed.plan.when, answer)

    async def test_capability_words_inside_real_request_remain_content(self):
        prompt = "text Mom saying Can you send texts and create reminders?"
        turn = await self.prepare(self.sessions.create_session(), prompt)
        self.assertIsNotNone(turn)
        self.assertEqual(turn.plan.intent, "message.send")
        self.assertEqual(turn.plan.subject.value, "Can you send texts and create reminders?")
        turn = await self.prepare(self.sessions.create_session(),
                                  "remind me tomorrow to ask what can you do")
        self.assertEqual(turn.plan.intent, "reminder.create")
        self.assertEqual(turn.plan.subject.value, "ask what can you do")

    async def test_future_reminder_content_never_mutates_existing_target(self):
        row = self.assistant.add_manual("dentist", (NOW + timedelta(days=2)).timestamp())
        snapshot = self.assistant.get(row["id"])
        for verb in ("reschedule", "delete", "complete", "rename", "update"):
            with self.subTest(verb=verb):
                sid = self.sessions.create_session()
                subject = verb + " my dentist reminder"
                turn = await self.prepare(sid, "remind me tomorrow to " + subject)
                self.assertEqual(turn.plan.intent, "reminder.create")
                self.assertEqual(turn.plan.subject.value, subject)
                self.assertTrue(turn.executable)
                self.assertEqual([step.tool for step in turn.plan.steps], ["add_reminder"])
                self.assertEqual(turn.plan.resolved_targets, [])
                self.assertEqual(self.assistant.get(row["id"]), snapshot)
                # The same subject with no time is still creation after a
                # time follow-up; it cannot switch to an existing-item edit.
                sid = self.sessions.create_session()
                first = await self.prepare(sid, "remind me to " + subject)
                self.assertFalse(first.executable)
                final = await self.prepare(sid, "9am tomorrow")
                self.assertTrue(final.executable)
                self.assertEqual(final.plan.intent, "reminder.create")
                self.assertEqual(final.plan.subject.value, subject)
                self.assertEqual([step.tool for step in final.plan.steps], ["add_reminder"])
                self.assertEqual(self.assistant.get(row["id"]), snapshot)
        # Actual present-tense operations retain their existing typed intent.
        for prompt, intent in (("reschedule my dentist reminder", "reminder.update"),
                               ("delete my dentist reminder", "reminder.delete"),
                               ("delete the reminder named remind me", "reminder.delete"),
                               ("complete my dentist reminder", "reminder.complete")):
            with self.subTest(prompt=prompt):
                turn = await self.prepare(self.sessions.create_session(), prompt)
                self.assertEqual(turn.plan.intent, intent)

    async def test_plural_note_edits_reach_note_route_without_calendar_authority(self):
        for prompt in ("add a line to my meeting notes", "add agenda items to my meeting notes",
                       "add a bullet to the meeting note", "add a sentence to my meeting notes",
                       "add this line to the meeting note"):
            with self.subTest(prompt=prompt):
                sid = self.sessions.create_session()
                self.assertIsNone(await self.prepare(sid, prompt))
                self.assertIsNone(self.sessions.active_task(sid))
                d = await R.route(prompt)
                self.assertEqual(set(d.tool_subset), {"search_notes", "append_note"})
                self.assertFalse(CREATION.intersection(d.tool_subset))
                self.assertEqual(d.force_first_tool, "append_note")
        sid = self.sessions.create_session()
        self.assertIsNone(await self.prepare(
            sid, "find my overdue dentist reminder and search my notes for dentist"))


if __name__ == "__main__":
    unittest.main()
