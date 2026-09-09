"""No model/network/user-data writes: exercise launch readiness end-to-end."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The endpoint test imports the full app, whose modules open local stores.
# Keep those stores as well as the mocked sync caches away from user data.
_scratch = tempfile.TemporaryDirectory(prefix="wisp-sync-contract-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant import brief, scheduler, sync_status as S
from service.assistant.hub import hub
from service.tools import assistant_tools as A, cache_store
from service.tools import browser_history_tools as H, email_tools as E
from service.tools import imessage_tools as M, notes_tools as N


class SourceSyncContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(scheduler, "_sync_status", {}))
        self.enterContext(patch.multiple(E, _headers_sync_generation=0,
                                       _headers="", _headers_at=0, _history="", _history_at=0,
                                       _email_available=None, _email_sync_pending=False,
                                       _email_reason="", _email_read_source=""))
        self.enterContext(patch.multiple(M, _sync_completed=False, _available=False,
                                       _unavailable_reason="", _lines="1 | Mom | Mom: old"))
        self.enterContext(patch.multiple(N, _available=None, _reason="",
                                       _notes="old note", _notes_at=time.time()))
        self.enterContext(patch.multiple(H, _enabled=None, _completed=set(),
                                       _available={"safari": False, "chrome": False},
                                       _reason={"safari": "", "chrome": ""},
                                       _raw={"safari": "1 | example.com | / | old", "chrome": ""},
                                       _synced_at={"safari": time.time(), "chrome": 0}))
        self.enterContext(patch.object(cache_store, "save"))
        self.publish = self.enterContext(patch.object(hub, "publish", new_callable=AsyncMock))

    def ready_core(self):
        scheduler.record_sync("calendar", 0, {"authorized": True})
        scheduler.record_sync("reminders", 0, {"authorized": True})
        E._headers_sync_generation, E._email_available = 1, True
        M.cache_messages("", available=True)

    async def post_mail(self, headers="", history="", source="local_index"):
        from service.main import assistant_sync_emails
        await assistant_sync_emails({
            "headers": headers, "history": history,
            "diagnostics": {"available": True, "syncing": False, "read_source": source},
        })

    async def test_completed_old_local_read_finishes_with_warning(self):
        self.ready_core()
        E._email_sync_pending = True  # previous build's stuck state
        old_row = f"{time.time() - 86400} | U | Test | Sender | Older email"
        await self.post_mail(old_row, old_row)
        snapshot = S.summary_snapshot()
        row = next(row for row in snapshot["sources"] if row["id"] == "email")
        self.assertFalse(snapshot["syncing"])
        self.assertEqual(snapshot["progress"], 1)
        self.assertEqual(row["state"], "ready")
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["read_source"], "local_index")
        self.assertIn("Newer messages may be missing", row["warning"])
        self.assertNotIn("Current data", row["progress_detail"])

    async def test_successful_empty_local_read_clears_old_headers_and_history(self):
        E._headers = E._history = "1 | U | Test | Sender | Old cached email"
        await self.post_mail()
        self.assertEqual(E.email_sync_state(), "ready")
        self.assertEqual((E._headers, E._history), ("", ""))
        text = await E.summarize_inbox_for_day("today")
        self.assertIn("local cache", text)
        self.assertIn("No emails found", text)
        self.assertNotIn("Automation", brief._email_block(time.time()))

    async def test_local_retry_requests_new_push_and_clears_warning_on_mail_read(self):
        await self.post_mail()
        generation = E._headers_sync_generation

        async def complete(event):
            self.assertEqual(event["sources"], ["email"])
            await self.post_mail(source="mail_app")
        self.publish.side_effect = complete
        result = await S.ensure_sources(["email"], timeout_seconds=.1)
        self.assertFalse(result["syncing"])
        self.publish.assert_awaited_once()
        self.assertGreater(E._headers_sync_generation, generation)
        self.assertEqual(E.email_freshness_warning(), "")

    async def test_local_retry_timeout_keeps_warning_without_stuck_progress(self):
        await self.post_mail()
        result = await S.ensure_sources(["email"], timeout_seconds=0)
        self.publish.assert_awaited_once()
        self.assertFalse(result["syncing"])
        self.assertIn("local cache", E.email_freshness_warning())

    async def test_interactive_email_retry_also_refreshes_local_cache(self):
        await self.post_mail()

        async def complete(event):
            self.assertEqual(event["type"], "sync_emails_now")
            await self.post_mail(source="mail_app")
        self.publish.side_effect = complete
        await E._ensure_email_cache(timeout_seconds=.1)
        self.publish.assert_awaited_once()
        self.assertEqual(E.email_freshness_warning(), "")

    async def test_daily_and_notification_text_keep_input_freshness_warning(self):
        self.ready_core()
        await self.post_mail()
        snapshot = S.summary_snapshot()
        warning = E.email_freshness_warning()

        async def generate(_):
            await self.post_mail(source="mail_app")  # lands during generation
            return {"FULL": "Your day.", "TODAY": "Your schedule.", "MESSAGES": "Your texts."}
        with patch.object(S, "ensure_daily_sources", AsyncMock(return_value=snapshot)), \
             patch.object(brief, "_generate_brief", side_effect=generate):
            sections = await brief._sections("morning")
        self.assertTrue(sections["FULL"].startswith(warning))
        self.assertTrue(sections["TODAY"].startswith(warning))
        self.assertNotIn(warning, sections["MESSAGES"])

    async def test_model_free_brief_has_freshness_warning(self):
        self.ready_core()
        await self.post_mail()
        with patch.object(A.assistant_store, "upcoming", return_value=[]):
            text = brief.brief_without_model()
        self.assertIn(E.email_freshness_warning(), text)
        self.assertNotIn("still syncing", text)

    async def test_day_range_and_recent_summaries_disclose_local_snapshot(self):
        old_row = f"{time.time() - 3 * 86400} | U | Test | Sender | Older email"
        await self.post_mail(old_row, old_row)
        with patch.object(E, "_summarize", AsyncMock(return_value="A summary.")):
            for text in (await E.summarize_inbox_for_day("today"),
                         await E.summarize_inbox_for_period("today"),
                         await E.summarize_inbox_recent()):
                self.assertTrue(text.startswith(E.email_freshness_warning()))
                self.assertIn("open Mail", text)

    async def test_failed_read_is_terminal_even_with_syncing_flag(self):
        self.ready_core()
        E.set_email_availability(False, "read failed", syncing=True)
        row = S.sources_snapshot(["email"])["sources"][0]
        self.assertEqual(row["state"], "unavailable")
        self.assertIsNone(row["progress"])
        self.assertEqual(row["reason"], "read failed")
        with patch.object(E, "_ensure_email_cache", new_callable=AsyncMock), \
             patch.object(E, "_summarize", new_callable=AsyncMock) as generate:
            text = await E.summarize_emails()
        generate.assert_not_awaited()
        self.assertIn("read failed", text)

    async def test_pending_permission_is_not_denial_or_ready(self):
        scheduler.record_sync("calendar", 0, {"authorized": False, "syncing": True})
        self.assertEqual(S.source_status("calendar")["state"], "syncing")
        scheduler.record_sync("calendar", 0, {"authorized": False})
        self.assertEqual(S.source_status("calendar")["state"], "unavailable")

    async def test_progress_counts_actual_terminal_sources(self):
        scheduler.record_sync("calendar", 42, {"authorized": True})
        scheduler.record_sync("reminders", 0, {"authorized": False})
        snapshot = S.summary_snapshot()
        self.assertEqual(snapshot["progress"], .5)
        self.assertEqual(snapshot["pending"], ["email", "messages"])
        self.assertEqual(S.sources_snapshot(["messages", "messages"])["total"], 1)
        self.assertEqual(S.sources_snapshot([])["progress"], 1)

    async def test_each_source_has_its_own_confirmed_percentage(self):
        scheduler.record_sync("calendar", 42, {"authorized": True})
        scheduler.record_sync("reminders", 0, {"authorized": False})
        rows = {row["id"]: row for row in S.summary_snapshot()["sources"]}
        self.assertEqual(rows["calendar"]["progress"], 1)
        self.assertEqual(rows["messages"]["progress"], 0)
        self.assertEqual(rows["email"]["progress"], 0)
        self.assertIsNone(rows["reminders"]["progress"])
        self.assertIn("intermediate progress unavailable", rows["email"]["progress_detail"])
        M.cache_messages("", available=True)
        self.assertEqual(S.sources_snapshot(["messages"])["sources"][0]["progress"], 1)

    async def test_browser_percentage_advances_on_completed_checks(self):
        H.set_browser_history_enabled(True)
        self.assertEqual(S.sources_snapshot(["browser_history"])["sources"][0]["progress"], 0)
        H.cache_browser_history("safari", "", available=True)
        row = S.sources_snapshot(["browser_history"])["sources"][0]
        self.assertEqual(row["progress"], .5)
        self.assertEqual(row["progress_detail"], "1 of 2 browser checks finished")
        H.cache_browser_history("chrome", "", available=True)
        self.assertEqual(S.sources_snapshot(["browser_history"])["sources"][0]["progress"], 1)

    async def test_disabled_source_does_not_show_successful_percentage(self):
        H.set_browser_history_enabled(False)
        row = S.sources_snapshot(["browser_history"])["sources"][0]
        self.assertIsNone(row["progress"])
        self.assertEqual(row["progress_detail"], "Turned off")

    async def test_on_demand_request_can_complete_without_retry(self):
        async def complete(event):
            self.assertEqual(event["sources"], ["messages"])
            M.cache_messages("", available=True)
        self.publish.side_effect = complete
        result = await S.ensure_sources(["messages"], timeout_seconds=.1)
        self.assertFalse(result["syncing"])
        self.assertEqual(await M.view_messages_impl(), "No messages found.")
        self.publish.assert_awaited_once()

    async def test_timeout_preserves_pending_not_false_empty(self):
        result = await S.ensure_sources(["messages"], timeout_seconds=0)
        self.assertTrue(result["syncing"])
        self.assertIn("still syncing", M._unavailable_message())
        self.assertNotIn("old", brief._plain_messages_section(time.time()))

    async def test_daily_summary_holds_back_without_a_model(self):
        snapshot = S.summary_snapshot()
        with patch.object(S, "ensure_daily_sources", AsyncMock(return_value=snapshot)), \
             patch.object(brief, "_generate_brief", new_callable=AsyncMock) as generate:
            text = await brief.build_daily_brief()
        generate.assert_not_awaited()
        for name in ("calendar", "reminders", "email", "messages"):
            self.assertIn(name, text)
        self.assertIn("incomplete", text)
        self.assertIn("still syncing", brief.brief_without_model())

    async def test_failed_calendar_does_not_expose_saved_events(self):
        self.ready_core()
        scheduler.record_sync("calendar", 0, {"authorized": False})
        with patch.object(A.assistant_store, "upcoming", return_value=[{
            "source": "calendar", "title": "stale meeting", "when_ts": time.time(),
        }]):
            text = await A.get_upcoming()
            block = brief._calendar_block(time.time())
        self.assertIn("could not check Calendar", text)
        self.assertNotIn("stale meeting", text + block)
        self.assertNotIn("day is clear", block)

    async def test_failed_mail_report_overrides_earlier_success(self):
        self.ready_core()
        E.set_email_availability(False, "read failed")
        self.assertEqual(E.email_sync_state(), "unavailable")

    async def test_optional_sources_never_block_daily_summary(self):
        self.ready_core()
        self.assertFalse(S.summary_snapshot()["syncing"])
        self.assertEqual(N.notes_sync_state(), "syncing")
        self.assertEqual(H.browser_history_sync_state(), "syncing")

    async def test_empty_notes_read_finishes_and_failure_is_not_empty(self):
        N.cache_notes("", available=True)
        self.assertEqual(N.notes_sync_state(), "ready")
        self.assertIn("completed", await N.search_notes_impl())
        N.cache_notes("", available=False, reason="access denied")
        self.assertIn("could not read", await N.search_notes_impl())

    async def test_disabled_browser_history_is_terminal_without_reading(self):
        H.set_browser_history_enabled(False)
        self.assertEqual(H.browser_history_sync_state(), "disabled")
        self.assertEqual(H._all_rows(), [])
        self.assertIn("turned off", await H.search_browser_history_impl())
        self.publish.assert_not_awaited()

    async def test_empty_browser_read_replaces_stale_rows(self):
        H.set_browser_history_enabled(True)
        H.cache_browser_history("safari", "", available=True)
        self.assertEqual(H.browser_history_sync_state(), "syncing")
        H.cache_browser_history("chrome", "", available=False)
        self.assertEqual(H.browser_history_sync_state(), "ready")
        self.assertEqual(H._raw["safari"], "")
        self.assertIn("completed sync", await H.search_browser_history_impl())


if __name__ == "__main__":
    unittest.main()
