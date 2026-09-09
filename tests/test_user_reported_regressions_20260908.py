"""Wisp debug export 2026-09-08 20:59 — "I made it very clear on who to send
the message to."

The user opened with "send my calender to trishe(the next two weeks)". Wisp
asked which channel (fair — they had not said), then asked "Who should I
message it to?" about a person already named in that first sentence, and only
after they retyped "trishe" did it get as far as a confirmation card.

Cause: every recipient pattern in the compiler assumed the sentence's direct
object is the word "message" — "send a message to trishe with my calendar",
which the 2026-09-03 fix covers. Naming the ARTIFACT instead ("send my
CALENDAR to trishe") matched none of them, and the remaining catch-all
required a capitalised name, so lowercase "trishe" fell through to a question.

What must keep holding:
  * a lowercase contact named after any delivery verb is picked up even when
    the object is the artifact, with or without a space before whatever
    follows the name;
  * the recipient survives the channel clarification, so the second question
    never appears and the plan reaches its confirmation card in one reply;
  * payload nouns, channels and the user's own inbox still never become a
    person.

    .venv/bin/python -m pytest tests/test_user_reported_regressions_20260908.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.memory.store import SessionStore  # noqa: E402
from service.workflows import prepare_turn  # noqa: E402
from service.workflows.compiler import compile_new, extract_recipient  # noqa: E402

REPORTED = "send my calender to trishe(the next two weeks)"


def test_artifact_first_phrasing_names_its_recipient():
    assert extract_recipient(REPORTED) == "trishe"
    assert extract_recipient("send my calendar to trishe for the next two weeks") == "trishe"
    assert extract_recipient("text my schedule to trishe") == "trishe"
    assert extract_recipient("share my calendar with trishe") == "trishe"
    assert extract_recipient("forward this email to john") == "john"
    assert extract_recipient("send my calendar to john morada tomorrow") == "john morada"


def test_a_name_stops_where_the_sentence_continues():
    # Greedy three-word captures used to address "trishe via messages".
    assert extract_recipient("send this to trishe via messages") == "trishe"
    assert extract_recipient("text it to dan now") == "dan"


def test_payload_and_channel_nouns_are_still_not_people():
    assert extract_recipient("send a message with my calendar", "messages") == ""
    assert extract_recipient("send my calendar summary", "messages") == ""
    assert extract_recipient("send my calendar to my email") == "me"


def test_the_reported_turn_asks_only_about_the_channel():
    plan = compile_new(REPORTED)
    assert plan is not None
    assert plan.recipient == "trishe"
    assert plan.status == "waiting_for_channel"

    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db")
        sid = store.create_session()
        first = prepare_turn(store, sid, REPORTED)
        assert first.response == "Should I deliver that through Messages or email?"
        store.add_turn(sid, "user", REPORTED)
        store.add_turn(sid, "assistant", first.response)

        second = prepare_turn(store, sid, "messages")

    # No second question: the recipient was never lost, so this reply executes.
    assert second.response == ""
    assert second.plan.recipient == "trishe"
    assert second.plan.status == "running"
    assert second.decision is not None
    assert second.decision.tool_argument_bindings["send_message"] == {"to": "trishe"}
