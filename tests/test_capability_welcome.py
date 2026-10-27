"""'What can you do?' is a short welcome built from live state (rc1).

It used to be a 17-row Yes/No table that opened with a tool count. A general
question now gets: one line on what Wisp does, which sources are connected
right now (service/assistant/sync_status), three try-it prompts that fit those
sources, and where the hotkey, the / shortcuts, Brief, Day and Getting to know
you live. A question about a specific ability still gets the Yes/No report,
because its honest "No" rows are the answer to "can you read my screen?".
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_scratch = tempfile.TemporaryDirectory()
os.environ["WISP_HOME"] = _scratch.name

import pytest  # noqa: E402

import service.tools  # noqa: E402,F401
from service.assistant import sync_status  # noqa: E402
from service.router.router import route  # noqa: E402
from service.tools.misc_t1 import wisp_capabilities  # noqa: E402


def _states(monkeypatch, **states):
    def fake(source_id):
        state = states.get(source_id, "unavailable")
        reason = "Turn on Full Disk Access for Wisp." if state == "unavailable" else ""
        return {"id": source_id, "label": sync_status._LABELS[source_id],
                "state": state, "reason": reason}
    monkeypatch.setattr(sync_status, "source_status", fake)


@pytest.mark.parametrize("question", [
    "What can you do?", "what can you do", "Hey, what else can you do for me?",
    "What tools are available to you?", "which assistant capabilities do you have",
    "What can you help me with?",
])
def test_general_question_gets_the_welcome(question, monkeypatch):
    _states(monkeypatch, calendar="ready", email="ready", reminders="syncing")
    text = wisp_capabilities(question)
    assert "Connected: Calendar, Email." in text
    assert "Still reading for the first time: Reminders." in text
    assert "Not connected: Messages. Turn on Full Disk Access for Wisp." in text
    # Try-its follow what is connected, then fall back to source-free ones.
    assert '- "What\'s on my calendar tomorrow?"' in text
    assert '- "Any emails I need to reply to?"' in text
    assert '- "Set a timer for 10 minutes."' in text
    assert text.count('\n- "') == 3
    for place in ("⌥ Space", "Type /", "Brief and Day", "Getting to know you", "/about"):
        assert place in text
    # No tool count, no table of "No" rows.
    assert "usable tools" not in text and "No —" not in text and "Yes —" not in text


def test_welcome_survives_unreadable_sources(monkeypatch):
    def broken(source_id):
        raise RuntimeError("boom")
    monkeypatch.setattr(sync_status, "source_status", broken)
    text = wisp_capabilities("What can you do?")
    assert "couldn't check which of your apps are connected" in text
    assert text.count('\n- "') == 3


def test_specific_question_keeps_the_honest_report():
    text = wisp_capabilities("Can you send texts and create reminders?")
    assert "Yes — Send text messages (confirmation required)" in text
    assert "No — inspect or understand the screen visually" in text
    assert "usable tools" not in text


def test_router_still_sends_what_can_you_do_to_the_tool():
    decision = asyncio.run(route("What can you do?"))
    assert decision.direct_calls[0][0] == "wisp_capabilities"


@pytest.mark.parametrize("prompt", [
    "What's on my calendar tomorrow?", "Any emails I need to reply to?",
    "Remind me at 5pm to call Mom.", "Any texts I need to respond to?",
    "Set a timer for 10 minutes.", "What time is it in Tokyo?",
    "What's the weather in Paris this weekend?",
])
def test_every_try_it_prompt_routes_to_a_scoped_tool(prompt):
    from service.tools.misc_t1 import _TRY_IT
    assert prompt in {p for _, p in _TRY_IT}
    decision = asyncio.run(route(prompt))
    assert decision.tool_subset and len(decision.tool_subset) <= 3
