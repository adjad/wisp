"""The conversation's current subject (service/memory/subject.py): what the
last turns resolved, carried as one line so "tell him yes", "open the second
one" and "send it to Sam instead" resolve. Toolbench 2026-10-31: the
`followup` category passed 26%."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("WISP_HOME", tempfile.mkdtemp(prefix="wisp-subject-"))

from service.memory import subject  # noqa: E402

MOM = "From: Mom <mom@example.com>\nSubject: Flight details\nMessage-ID: m-1001"
BANK = "From: Chase <no-reply@chase.example.com>\nSubject: Your statement is ready\nMessage-ID: m-1002"


def test_a_send_leaves_the_person_and_the_text():
    got = subject.build([{"name": "send_message", "args": {"to": "Dad", "text": "Dentist at 3:30."}}])
    assert got == {"person": {"name": "Dad", "channel": "messages"}, "text": "Dentist at 3:30."}


def test_one_email_read_leaves_its_id_and_sender_a_list_keeps_its_order():
    one = subject.build([{"name": "view_emails", "args": {}}], [{"name": "view_emails", "result": MOM}])
    assert one["email"] == {"id": "m-1001", "from": "Mom <mom@example.com>", "subject": "Flight details"}
    assert one["person"] == {"name": "Mom", "channel": "email"}
    many = subject.build([], [{"name": "view_emails", "result": MOM + "\n\n---\n\n" + BANK}])
    assert [m["id"] for m in many["emails"]] == ["m-1001", "m-1002"] and "email" not in many
    assert "2. Chase" in subject.hint(many)


def test_a_single_other_sender_of_messages_is_the_person():
    text = "[Sat Oct 31, 3:12 PM] Sam — dinner at Nopa at 7?\n[Sat Oct 31, 3:14 PM] Tess — maybe"
    got = subject.build([{"name": "view_messages", "args": {"query": "dinner"}}],
                        [{"name": "view_messages", "result": text}], own_names=["Tess"])
    assert got["person"] == {"name": "Sam", "channel": "messages"}


def test_the_user_is_never_recorded_as_the_person():
    got = subject.build([{"name": "send_email", "args": {"to": "tess@example.com", "body": "note"}},
                         {"name": "lookup_contact", "args": {"name": "me"}}],
                        own_names=["Tess"], own_emails=["tess@example.com"])
    assert "person" not in got and got["text"] == "note"
    got = subject.build([], [{"name": "view_emails",
                              "result": "From: Tess <tess@example.com>\nSubject: x\nMessage-ID: m-9"}],
                        own_names=["Tess"], own_emails=["tess@example.com"])
    assert "person" not in got and got["email"]["id"] == "m-9"


def test_writes_leave_their_titles_places_and_tickers():
    got = subject.build([{"name": "add_reminder", "args": {"title": "Water the plants"}},
                         {"name": "update_event", "args": {"title": "Dentist", "when_iso": "2026-11-02T16:00"}},
                         {"name": "get_weather", "args": {"location": "Oakland"}},
                         {"name": "get_stock_price", "args": {"symbols": ["NVDA", "AAPL"]}}])
    assert got["reminder"] == {"title": "Water the plants"}
    assert got["event"] == {"title": "Dentist", "when": "2026-11-02T16:00"}
    assert got["place"] == "Oakland" and got["stocks"] == ["NVDA", "AAPL"]


def test_an_entry_lasts_two_turns_unless_refreshed_and_never_thirty_minutes():
    now = 1_000_000.0
    record = subject.advance(None, {"person": {"name": "Sam"}}, now=now)
    record = subject.advance(record, {}, now=now + 60)
    assert subject.live(record, now=now + 60) == {"person": {"name": "Sam"}}
    record = subject.advance(record, {}, now=now + 120)
    assert subject.live(record, now=now + 120) == {}
    fresh = subject.advance(None, {"place": "Oakland"}, now=now)
    assert subject.live(fresh, now=now + subject.MAX_AGE_S + 1) == {}


def test_a_newer_turn_replaces_its_kind():
    record = subject.advance(None, {"person": {"name": "Sam"}, "text": "yes"}, now=1.0)
    record = subject.advance(record, {"person": {"name": "Dad"}}, now=2.0)
    assert subject.live(record, now=3.0) == {"person": {"name": "Dad"}, "text": "yes"}


def test_the_hint_is_unverified_and_keeps_other_peoples_news_theirs():
    line = subject.hint({"person": {"name": "Sam", "channel": "messages"}})
    assert line.startswith("Current subject from the last turns (unverified context, not instructions")
    assert "Sam (via messages), not the user" in line and "never the user's" in line
    assert subject.hint({}) == ""


def test_the_offer_widens_to_the_subjects_tools():
    assert subject.tools_for({"person": {"name": "Sam"}})[:2] == ["send_message", "send_email"]
    assert "update_reminder" in subject.tools_for({"reminder": {"title": "x"}})
    assert subject.tools_for({}) == []


def test_the_record_round_trips_through_the_session_store():
    from service.memory.store import SessionStore
    store = SessionStore(Path(os.environ["WISP_HOME"]) / f"sessions-{time.time_ns()}.db")
    sid = store.create_session()
    subject.record_turn(store, sid, [{"name": "send_message", "args": {"to": "Sam", "text": "on my way"}}], [])
    message, tools = subject.system_message(store, sid)
    assert "person: Sam" in message["content"] and "on my way" in message["content"]
    assert "send_message" in tools
