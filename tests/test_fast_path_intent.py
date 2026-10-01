"""Fast paths may act only on a request they fully understand.

Independent audit of #129/#131 reproduced these against the complete candidates:
  * negated / quoted device text ("Do not lock my screen.") direct-dispatched a call;
  * a quoted sentence ('Say "remind me to call Mom tomorrow".') became a real reminder;
  * calendar shortcuts dropped a second source, a named date and an exclusion;
  * an inbox shortcut flattened modifiers into the sender query;
  * authored prose ("Email Sam asking to move our meeting") forced a Calendar read.
Each case below is asserted at the layer that owned the bug, with controls proving
the supported plain requests still take their fast path.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, datetime

import pytest

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-fastpath-"))

from service.skills import load as load_skills  # noqa: E402

load_skills()

import service.router.router as R  # noqa: E402
from service import utterance_shape as U  # noqa: E402
from service.tasks.compiler import compile_task  # noqa: E402
from service.workflows import reads  # noqa: E402
from service.workflows.compiler import compile_new  # noqa: E402

NOW = datetime(2026, 9, 30, 10, 0)


def route(text):
    return asyncio.run(R.route(text))


# --- the shared definition ---------------------------------------------------------------

PROHIBITIONS = ["Do not lock my screen.", "don't lock my screen", "Please do not email Sam.",
                "Never delete that file", "No need to send it", "Hey Wisp, don't text Mom"]
MENTIONS = ['Explain the phrase "lock my screen".', 'Read the word "clipboard" aloud.',
            'Say "remind me to call Mom tomorrow".', 'What does "lock my screen" mean?',
            'Translate "send an email to Sam"', 'Define the word clipboard', 'Repeat "delete everything"']
PLAIN = ["lock my screen", "what's on my clipboard", "remind me to call Mom tomorrow at 5pm",
         "Don't forget to call Mom tomorrow", "Don't let me forget the dentist", "Never mind, I did it",
         "Stop the music", "email Sam saying \"running late\"", 'Say "hi" to Mom', "text Mom \"I'm on my way\"",
         "what's on my calendar, don't include reminders", "turn on do not disturb",
         "remind me not to forget my keys", "Don't worry about it"]


@pytest.mark.parametrize("text", PROHIBITIONS)
def test_a_prohibition_is_not_an_instruction(text):
    assert U.deliberate(text), text


@pytest.mark.parametrize("text", MENTIONS)
def test_a_sentence_about_words_is_not_an_instruction(text):
    assert U.deliberate(text), text


@pytest.mark.parametrize("text", PLAIN)
def test_plain_requests_are_left_alone(text):
    assert U.deliberate(text) is None, text


def test_quoted_words_are_masked_for_retrieval():
    assert U.mask_quoted('Say "remind me to call Mom" now') == 'Say "…" now'
    assert U.mask_quoted("it's Sam's, don't") == "it's Sam's, don't"


# --- P1: direct device dispatch ----------------------------------------------------------

@pytest.mark.parametrize("text", ["Do not lock my screen.", 'Explain the phrase "lock my screen".',
                                  'Read the word "clipboard" aloud.', "Don't read my clipboard."])
def test_negated_or_quoted_device_text_makes_no_call(text):
    decision = route(text)
    assert decision.direct_calls == [], decision.reason
    assert decision.force_first_tool is None and not decision.expect_tool_first, decision.reason


@pytest.mark.parametrize("text,tool", [("lock my screen", "lock_screen"),
                                       ("what's on my clipboard", "clipboard_read"),
                                       ("what's my battery level", "get_battery_status")])
def test_a_plain_device_request_still_takes_the_direct_path(text, tool):
    assert [n for n, _ in route(text).direct_calls] == [tool]


# --- P1: a quoted request must not become a task ----------------------------------------

def test_a_quoted_reminder_is_not_a_reminder():
    text = 'Say "remind me to call Mom tomorrow".'
    assert compile_task(text, now=NOW) is None
    assert compile_new(text) is None
    assert reads.compile_read(text) is None
    decision = route(text)
    assert decision.force_first_tool is None and not decision.expect_tool_first, decision.reason


def test_a_real_reminder_still_compiles():
    assert compile_task("remind me to call Mom tomorrow at 5pm", now=NOW) is not None


# --- P2: calendar shortcuts keep the whole request --------------------------------------

def args(text):
    compiled = reads.compile_read(text)
    return None if compiled is None else compiled[0][0][1]


def test_calendar_and_messages_is_not_answered_with_calendar_only():
    assert reads.compile_read("What's on my calendar and messages tomorrow?") is None
    assert reads.compile_read("Check my email and calendar for tomorrow") is None
    assert reads.compile_read("what's on my calendar and who emailed me") is None


def test_today_and_tomorrow_covers_both_days():
    assert args("What's on my calendar today and tomorrow?") == {"period": "today and tomorrow"}


def test_a_named_date_is_resolved_exactly(monkeypatch):
    assert reads._calendar_read_args("what's on my calendar for October 12", "", date(2026, 9, 30)) == {
        "period": "2026-10-12"}
    assert reads._calendar_read_args("show my calendar on 12th of oct", "", date(2026, 9, 30)) == {
        "period": "2026-10-12"}
    # A date already past this year is not guessed into next year, and an invalid one is refused.
    assert reads._calendar_read_args("what's on my calendar for March 3", "", date(2026, 9, 30)) is None
    assert reads._calendar_read_args("what's on my calendar for February 30", "", date(2026, 9, 30)) is None


@pytest.mark.parametrize("text", ["what's on my calendar next Friday", "show my calendar on the 12th",
                                  "what is on my calendar on Monday", "what's on my calendar in 3 weeks"])
def test_dates_the_shortcut_cannot_resolve_are_handed_on(text):
    assert reads.compile_read(text) is None, text


def test_a_reminder_exclusion_narrows_the_read():
    assert args("What's on my calendar this week, do not include reminders") == {
        "period": "this week", "calendar_only": True}
    decision = route("What's on my calendar this week, do not include reminders")
    assert decision.direct_calls and all(a.get("calendar_only") for n, a in decision.direct_calls
                                         if n == "get_upcoming"), decision.direct_calls


@pytest.mark.parametrize("text,expected", [
    ("what's on my calendar", {"days": 7}),
    ("what's on my calendar tomorrow", {"period": "tomorrow"}),
    ("what's on my calendar this week", {"period": "this week"}),
    ("show my schedule next week", {"period": "next week"})])
def test_supported_calendar_reads_keep_their_shortcut(text, expected):
    assert args(text) == expected


# --- P2: inbox shortcut does not swallow modifiers --------------------------------------

@pytest.mark.parametrize("text", ["Show my email purchases from Apple yesterday",
                                  "Show my email purchases from Apple and delete the old ones",
                                  "show my email purchases from Apple and archive them",
                                  "show my email purchases from Apple last week"])
def test_inbox_modifiers_are_not_flattened_into_the_sender(text):
    assert reads.compile_read(text) is None, text


def test_a_plain_sender_keeps_its_shortcut():
    assert args("show my email purchases from Apple") == {"query": "Apple", "strict_match": True}


# --- P2: authored prose does not force an unrelated Calendar read -----------------------

def test_authored_prose_does_not_force_a_calendar_read():
    decision = route("Email Sam asking to move our meeting")
    assert decision.force_first_tool is None, decision.reason
    assert "get_upcoming" not in {n for n, _ in decision.direct_calls}
    assert "send_email" in (decision.tool_subset or []) or "draft_email" in (decision.tool_subset or [])


def test_an_unresolved_topic_still_forces_the_lookup():
    # "email Mom about my move-in date" has no dictated content: the fact must be looked up first.
    assert route("email Mom about my move-in date").force_first_tool == "get_upcoming"
