"""Daily Summary delivery — regression tests.

Reported live (Wisp debug export, 2026-09-08 11:46): pressing Daily Summary
"takes way too long to read the sources", produced "multiple false 'daily summary
is ready in wisp' notifications while reading the sources", the brief itself was
"hard to read", and a follow-up referring to it "is also not handled correctly".

Four separate mechanisms, one per test class below:

  * READABILITY — `_generate_brief` concatenated get_upcoming, summarize_emails
    and summarize_messages verbatim, and those strings are written for a model:
    the exported brief opened three of its four sections with "each row is tagged
    relative to today:", "A calendar event alone is not a reminder." and "Source
    excerpts (not inferred outcomes); …", tagged every row "[Apple Reminder]",
    and carried a reminder titled "send my vaccine report to UCSC.**" whose stray
    asterisks opened a bold run.

  * LATENCY — composing one brief awaited FOUR source-readiness waits (the
    endpoint's, `_sections`' duplicate of it, then summarize_emails' and
    summarize_messages' own), each of which publishes a sync request the Swift
    readers answer with another serialized AppleScript walk of the same source.

  * FALSE NOTIFICATIONS — the scheduler marked the day done before it knew
    whether a brief had been produced, and published the hold-back message ("Wisp
    is still syncing …") as a `daily_brief` event, which the app announced as
    "Your daily summary is ready in Wisp." The fired-date lived only in memory,
    and the backend is a child of Wisp.app, so every relaunch inside the 4-hour
    window repeated it — with the sources always mid-sync right after a launch.

  * FOLLOW-UPS — the button's brief was never recorded in a session, so the
    backend answering the next message had no previous assistant turn to refer
    to (see the referenced_report case in scripts/verify_tool_calling.py).

    .venv/bin/python -m pytest tests/test_daily_summary_delivery.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_scratch = tempfile.TemporaryDirectory(prefix="wisp-daily-summary-")
os.environ["WISP_HOME"] = _scratch.name

from service.assistant import brief as B                       # noqa: E402
from service.assistant import scheduler                        # noqa: E402
from service.assistant.hub import hub                          # noqa: E402
from service.tools import email_tools as E, imessage_tools as M  # noqa: E402

# Text that only ever belonged in a prompt. Any of it in the brief is the leak.
SCAFFOLD = (
    "each row is tagged relative to today",
    "A calendar event alone is not a reminder",
    "Source excerpts",
    "not inferred outcomes",
    "quoted from the original messages",
    "Quoted from the source",
    "[Apple Reminder]",
    "Wisp reminder; Apple mirror not verified",
    "ON THE CALENDAR TODAY",
    "FROM PEOPLE",
    "do NOT",
)


@pytest.fixture
def sources(monkeypatch):
    """Calendar, Reminders, Mail and Messages, all ready, with fixed rows.

    Shaped after the exported failure: no calendar events, three Apple Reminders
    (two of them already past due), an inbox that is mostly automated senders
    across two accounts, and one outgoing plus one group message.
    """
    # Build the fixed wall-clock time in the runner's local timezone. A raw
    # epoch made the final reminder cross midnight on UTC CI but not in PDT.
    now = datetime(2026, 9, 8, 11, 46).timestamp()
    monkeypatch.setattr(scheduler, "_sync_status", {
        source: {"available": True, "count": 1, "last_sync": now}
        for source in ("calendar", "reminders")})
    monkeypatch.setattr(B.assistant_store, "upcoming", lambda now=0, days=7: [
        {"source": "reminders", "kind": "reminder", "when_ts": now - 9_500,
         "title": "the iphone repair thing", "account": "iCloud"},
        {"source": "reminders", "kind": "reminder", "when_ts": now - 9_400,
         "title": "send my vaccine report to UCSC.**", "account": "iCloud"},
        {"source": "reminders", "kind": "reminder", "when_ts": now + 29_000,
         "title": "finish a Canvas assignment", "account": "iCloud"},
    ])
    monkeypatch.setattr(E, "_headers", "\n".join([
        f"{now - 1_000} | U | Google | PayPal | Confirmed: you've been invited to apply",
        f"{now - 2_000} | U | adnjain@ucsc.edu | Trishe Rao | Are you free Thursday?",
    ]))
    monkeypatch.setattr(E, "_headers_sync_generation", 1)
    monkeypatch.setattr(E, "_email_available", True)
    monkeypatch.setattr(E, "_email_sync_pending", False)
    monkeypatch.setattr(E, "_email_read_source", "mail_app")
    monkeypatch.setattr(M, "_parse_lines", lambda: [
        (now - 500, "Trishe", "Me: When are you getting the ChatGPT max plan"),
        (now - 900, 'Group "Grad GC"', "+19255231832: So thrity min workout?"),
    ])
    monkeypatch.setattr(M, "_sync_completed", True)
    monkeypatch.setattr(M, "_available", True)
    return now


class TestReadability:
    def test_no_model_facing_scaffolding_reaches_the_user(self, sources):
        text = B._render_brief(sources, B._messages_section(sources))
        leaked = [s for s in SCAFFOLD if s in text]
        assert not leaked, f"leaked {leaked}"

    def test_a_stray_asterisk_in_a_title_cannot_open_a_bold_run(self, sources):
        text = B._render_brief(sources, B._messages_section(sources))
        assert "send my vaccine report to UCSC" in text
        assert "UCSC.**" not in text
        # Every ** left in the brief is one of its own section headers, so they
        # pair up. An odd count means an unclosed run.
        assert text.count("**") % 2 == 0

    def test_events_and_reminders_are_separate_groups(self, sources):
        section = B._schedule_section(sources)
        assert "**📅 Today**" in section and "Nothing on your calendar" in section
        assert "**✅ Reminders due today**" in section
        # The distinction the tool output spent a sentence explaining.
        assert "A calendar event alone is not a reminder" not in section

    def test_a_past_due_reminder_is_not_labelled_now(self, sources):
        section = B._schedule_section(sources)
        assert "overdue" in section and "(now)" not in section

    def test_mail_from_a_person_leads_and_automated_mail_is_grouped(self, sources):
        section = B._email_section(sources)
        assert section.index("Trishe Rao") < section.index("PayPal")
        assert "**📬 Notices**" in section

    def test_messages_name_their_speaker_without_routing_markers(self, sources):
        section = B._messages_section(sources)
        assert "you: “When are you getting the ChatGPT max plan”" in section
        assert "->" not in section and "means" not in section

    def test_notification_cards_are_plain_text(self, sources):
        for card in (B._today_card(sources), B._messages_card(sources)):
            assert card and "**" not in card and "- " not in card
            assert not any(s in card for s in SCAFFOLD)
        assert "3 reminders due" in B._today_card(sources)


class TestLatency:
    @pytest.mark.asyncio
    async def test_one_readiness_wait_per_press(self, sources, monkeypatch):
        """The endpoint's wait is the only wait, and no tool adds another."""
        from service import main
        from service.memory.store import SessionStore
        from service.tools import assistant_tools, email_tools, imessage_tools
        ensure = AsyncMock(return_value={"syncing": False, "sources": []})
        monkeypatch.setattr("service.assistant.sync_status.ensure_daily_sources", ensure)
        monkeypatch.setattr("service.assistant.sync_status.ensure_sources",
                            AsyncMock(side_effect=AssertionError("no per-source wait")))
        monkeypatch.setattr(main, "store",
                            SessionStore(Path(_scratch.name) / "one-wait.db"))
        for module, name in ((assistant_tools, "get_upcoming"),
                             (email_tools, "summarize_emails"),
                             (imessage_tools, "summarize_messages")):
            monkeypatch.setattr(module, name,
                                AsyncMock(side_effect=AssertionError(f"{name} must not run")))
        result = await main.assistant_daily_summary()
        assert result["ok"] and result["text"]
        assert ensure.await_count == 1

    @pytest.mark.asyncio
    async def test_composing_the_brief_publishes_no_sync_requests(self, sources):
        with patch.object(hub, "publish", new_callable=AsyncMock) as publish:
            sections = await B._generate_brief("morning")
        assert sections["FULL"]
        publish.assert_not_awaited()


class TestScheduledDelivery:
    @pytest.fixture(autouse=True)
    def _quiet_clock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scheduler, "_brief_state_path",
                            lambda: tmp_path / "brief_state.json")
        monkeypatch.setattr(scheduler, "_last_brief", None)
        monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
        monkeypatch.setattr(scheduler, "_brief_attempts", {})

    @pytest.mark.asyncio
    async def test_a_holdback_publishes_nothing_and_keeps_the_day_open(self, monkeypatch):
        holdback = {"FULL": "Wisp is still syncing your email after launch."}
        with patch.object(B, "_sections", AsyncMock(return_value=holdback)), \
             patch.object(hub, "publish", new_callable=AsyncMock) as publish:
            delivered = await B.run_scheduled_brief("morning")
        assert delivered is False
        publish.assert_not_awaited()

        # …and the day stays open, so the real brief can still go out.
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=False)), \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            await scheduler._maybe_daily_brief()
        assert scheduler._brief_date() is None

    @pytest.mark.asyncio
    async def test_a_delivered_brief_publishes_once_and_survives_a_restart(self, monkeypatch):
        ready = {"FULL": "Your day.", "TODAY": "Today: nothing.",
                 "MESSAGES": "1 recent message in Trishe.", "READY": "1"}
        with patch.object(B, "_sections", AsyncMock(return_value=ready)), \
             patch.object(hub, "publish", new_callable=AsyncMock) as publish, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            await scheduler._maybe_daily_brief()
            assert publish.await_count == 1
            event = publish.await_args.args[0]
            assert event["type"] == "daily_brief" and event["text"] == "Your day."

            # A relaunch inside the window re-reads the date from disk instead of
            # firing again. This is the run of false "ready" pings.
            monkeypatch.setattr(scheduler, "_last_brief", None)
            monkeypatch.setattr(scheduler, "_last_brief_loaded", False)
            await scheduler._maybe_daily_brief()
            assert publish.await_count == 1

    @pytest.mark.asyncio
    async def test_a_source_that_never_finishes_gives_the_day_up(self):
        with patch.object(B, "run_scheduled_brief", AsyncMock(return_value=False)) as run, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(8, 5)):
            for _ in range(scheduler._MAX_BRIEF_ATTEMPTS + 5):
                await scheduler._maybe_daily_brief()
        assert run.await_count == scheduler._MAX_BRIEF_ATTEMPTS
        assert scheduler._brief_date() == _clock_at(8, 5).now().date()

    @pytest.mark.asyncio
    async def test_nothing_fires_outside_the_window(self):
        with patch.object(B, "run_scheduled_brief", AsyncMock()) as run, \
             patch("service.config.get_daily_summary_hour", lambda: 8), \
             patch.object(scheduler, "datetime", _clock_at(15, 0)):
            await scheduler._maybe_daily_brief()
        run.assert_not_awaited()


class TestFollowUps:
    @pytest.mark.asyncio
    async def test_the_brief_is_recorded_in_the_conversation(self, sources, monkeypatch):
        from service import main
        from service.memory.store import SessionStore
        store = SessionStore(Path(_scratch.name) / "follow-ups.db")
        monkeypatch.setattr(main, "store", store)
        monkeypatch.setattr("service.assistant.sync_status.ensure_daily_sources",
                            AsyncMock(return_value={"syncing": False, "sources": []}))
        result = await main.assistant_daily_summary({"session_id": ""})
        sid = result["session_id"]
        assert sid, "the endpoint must hand back a session to continue"
        # What a follow-up like "send Trishe my daily summary" routes off.
        assert store.last_user_turn(sid) == "Daily summary"
        assert store.last_assistant_turn(sid) == result["text"]

        # A second press continues the SAME session rather than orphaning it.
        again = await main.assistant_daily_summary({"session_id": sid})
        assert again["session_id"] == sid and store.turn_count(sid) == 4


def _clock_at(hour: int, minute: int):
    """`datetime` with now() pinned, for the scheduler's window arithmetic."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, hour, minute)
    return Clock
