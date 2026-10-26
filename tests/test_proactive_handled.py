"""Handled by the user, outside Wisp: a card, to-do or Needs attention row
leaves once the user answered the conversation themselves (a text sent in
Messages, mail sent from Gmail/Mail/a phone), or put the reminder or event on
their own list.

Synthetic stores, records and sent rows only; no live backend, no Google.
"""
from __future__ import annotations

import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-handled-")
os.environ.setdefault("WISP_HOME", _scratch.name)

import pytest  # noqa: E402

from service.assistant.daily_workspace import needs_you  # noqa: E402
from service.proactive import cycle, handled, sources  # noqa: E402
from service.proactive.store import ProactiveStore  # noqa: E402

NOW = 1_793_000_000.0
TZ = "America/Los_Angeles"
ME = "me@example.com"
MOM = "messages:" + sources._digest("Mom")
PROF = "mail:prof-thread"


def sent(subject, ts, *, account=ME, in_reply_to=""):
    return {"ts": ts, "to": "prof@school.edu", "subject": subject, "message_id": f"<s{ts}@x>",
            "in_reply_to": in_reply_to, "account": account}


def card(cid, ref, *, method="text_reply", title="Reply to Mom", todo_id=None, quote="call me"):
    return {"id": cid, "title": title, "account": ME, "todo_id": todo_id,
            "sources": [{"kind": ref.split(":")[0], "ref": ref, "who": "Mom", "quote": quote}],
            "fingerprints": [f"{ref}:h1"], "action": {"method": method}}


def todo(tid, ref, *, kind="reply", title="Reply to Mom", source="messages"):
    return {"id": tid, "title": title, "source": source, "account": ME, "kind": kind, "done": False,
            "due_ts": None, "card_id": None, "fingerprints": [f"{ref}:h1"]}


@pytest.fixture()
def store(tmp_path):
    s = ProactiveStore(tmp_path / "proactive")
    s.save_state(as_of=NOW - 600, timezone=TZ, accounts=[ME], cards=[], todos=[], threads=[], seen={
        MOM: {"source": "messages", "conversation": "Mom", "received_ts": NOW - 900},
        PROF: {"source": "mail", "account": ME, "subject": "Thesis draft", "message_id": "<in1@school.edu>",
               "received_ts": NOW - 3600}})
    return s


# --- seeing the user's own replies ---------------------------------------------------

def test_sent_mail_answers_a_thread_by_reply_header_or_subject():
    after = NOW - 3600
    assert sources.answered_by_sent([sent("anything", NOW, in_reply_to="<in1@school.edu>")],
                                    account="other@x", subject="Thesis draft", message_id="<in1@school.edu>",
                                    after=after)
    assert sources.answered_by_sent([sent("Re: Thesis draft", NOW)], account=ME, subject="Thesis draft",
                                    message_id="", after=after)
    assert not sources.answered_by_sent([sent("Re: Thesis draft", after - 60)], account=ME,
                                        subject="Thesis draft", message_id="", after=after), "sent before it arrived"
    assert not sources.answered_by_sent([sent("Re: Thesis draft", NOW, account="work@x")], account=ME,
                                        subject="Thesis draft", message_id="", after=after), "another account"
    assert not sources.answered_by_sent([sent("Lunch?", NOW)], account=ME, subject="Thesis draft",
                                        message_id="", after=after)


def test_mail_thread_answered_from_gmail_reads_as_replied():
    rows = [{"ts": NOW - 3600, "sender": "Dr. Patel <prof@school.edu>", "sender_address": "prof@school.edu",
             "subject": "Thesis draft", "account": ME, "message_id": "<in1@school.edu>",
             "body": "Can you send the next chapter by Friday?"}]
    before = sources.mail_items(rows, mine={ME}, now=NOW, tz=TZ, own=ME, linked=set())
    after = sources.mail_items(rows, mine={ME}, now=NOW, tz=TZ, own=ME, linked=set(),
                               sent=[sent("Re: Thesis draft", NOW - 60)])
    assert [i["replied"] for i in before] == [False]
    assert [i["replied"] for i in after] == [True]


def test_latest_sent_texts_and_seen_entries():
    records = [(NOW - 900, "Mom", "Mom: call me", "g1", False), (NOW - 60, "Mom", "Me: calling now", "g1", False),
               (NOW - 30, "Ruhi", "Ruhi: hi", "g2", True)]
    assert handled.latest_sent_texts(records) == {"Mom": NOW - 60}
    one = {"id": MOM, "source": "messages", "received_ts": NOW - 900, "ref": {"conversation": "Mom"}}
    assert handled.seen_entry(one) == {"source": "messages", "conversation": "Mom", "received_ts": NOW - 900}
    assert handled.seen_entry({**one, "group": True}) is None, "a group reply isn't an answer to everyone"
    assert handled.seen_entry({"id": "mail:x", "source": "mail", "critical": True, "received_ts": NOW}) is None


def test_answered_refs_needs_a_reply_after_the_last_incoming_message(store):
    seen = store.state()["seen"]
    assert handled.answered_refs(seen, texts={"Mom": NOW - 60}, sent=[]) == {MOM}
    assert handled.answered_refs(seen, texts={"Mom": NOW - 1000}, sent=[]) == set()
    assert handled.answered_refs(seen, texts={}, sent=[sent("Re: Thesis draft", NOW)]) == {PROF}


# --- retiring what the user handled -------------------------------------------------

def test_a_reply_card_leaves_and_its_todo_is_checked(store):
    store.save_state(cards=[card("card:mom", MOM, todo_id="todo:mom")], todos=[todo("todo:mom", MOM)])
    changed = handled.sweep(store, now=NOW, texts={"Mom": NOW - 60}, sent=[])
    assert changed["cards"] == ["card:mom"] and changed["todos"] == ["todo:mom"]
    assert store.state()["cards"] == []
    assert "todo:mom" in store.done_todos(NOW)
    assert "card:mom" in store.decisions()["dismissed"]


def test_nothing_changes_without_a_reply(store):
    store.save_state(cards=[card("card:mom", MOM, todo_id="todo:mom")], todos=[todo("todo:mom", MOM)])
    assert handled.sweep(store, now=NOW, texts={"Mom": NOW - 1200}, sent=[]) == \
        {"cards": [], "todos": [], "threads": []}
    assert [c["id"] for c in store.state()["cards"]] == ["card:mom"]


def test_a_reminder_card_about_replying_leaves_but_a_plan_card_stays(store):
    store.save_state(cards=[
        card("card:reply", MOM, method="reminder", title="Reply to Mom about dinner", todo_id="todo:a"),
        card("card:meet", MOM, method="reminder", title="Coffee with Mom tomorrow", todo_id="todo:b")],
        todos=[todo("todo:a", MOM, kind="meet"), todo("todo:b", MOM, kind="meet", title="Coffee with Mom")])
    changed = handled.sweep(store, now=NOW, texts={"Mom": NOW - 60}, sent=[])
    assert changed["cards"] == ["card:reply"], "answering doesn't put the coffee on a calendar"
    assert [c["id"] for c in store.state()["cards"]] == ["card:meet"]
    assert "todo:b" not in store.done_todos(NOW)


def test_a_mail_card_leaves_once_answered_from_gmail(store):
    store.save_state(cards=[card("card:prof", PROF, method="email_reply", title="Reply to Dr. Patel")])
    changed = handled.sweep(store, now=NOW, texts={}, sent=[sent("RE: Thesis draft", NOW - 120)])
    assert changed["cards"] == ["card:prof"]


def test_a_reminder_card_leaves_once_the_user_added_it_themselves(store):
    offer = card("card:rem", MOM, method="reminder", title="Pick up dry cleaning", quote="dry cleaning is ready")
    store.save_state(cards=[offer])
    sched = {"calendar": [], "reminders": [{"id": "r1", "title": "Pick up dry cleaning"}]}
    assert handled.sweep(store, now=NOW, texts={}, sent=[], sched=sched)["cards"] == ["card:rem"]


def test_wisps_own_added_notice_and_held_time_are_never_retired(store):
    store.save_state(cards=[card("card:added", MOM, method="undo_add", title="Reply to Mom"),
                            card("card:block", MOM, method="block_time", title="Reply to Mom")])
    assert handled.sweep(store, now=NOW, texts={"Mom": NOW - 60}, sent=[])["cards"] == []


def test_answered_threads_leave_needs_attention_unless_a_deadline_is_ahead(store):
    store.save_state(threads=[
        {"id": "thread:" + MOM, "source": "messages", "account": ME, "who": "Mom", "title": "Mom",
         "summary": "Mom asked you to call her.", "needs_reply": True, "replied": False, "received_ts": NOW - 900},
        {"id": "thread:" + PROF, "source": "mail", "account": ME, "who": "Dr. Patel", "title": "Thesis draft",
         "summary": "Dr. Patel asked for the chapter.", "needs_reply": True, "replied": False,
         "deadline": {"due_ts": NOW + 86400, "word": "due"}, "received_ts": NOW - 3600}])
    changed = handled.sweep(store, now=NOW, texts={"Mom": NOW - 60}, sent=[sent("Re: Thesis draft", NOW - 60)])
    assert sorted(changed["threads"]) == sorted(["thread:" + MOM, "thread:" + PROF])
    shown = cycle.snapshot(now=NOW, tz=TZ, store=store, own=ME, linked=set(),
                           schedule_fn=lambda n, z: {"calendar": [], "reminders": []})
    assert [t["id"] for t in shown["threads"]] == ["thread:" + PROF], "the deadline still stands"
    assert shown["threads"][0]["needs_reply"] is False


def test_needs_you_leaves_out_mail_the_user_answered():
    rows = [dict(id="email:a", source="email", title="Thesis draft", person=True, sender="Dr. Patel",
                 received_ts=NOW - 3600, reason="Review."),
            dict(id="email:b", source="email", title="Lunch?", person=True, sender="Ruhi",
                 received_ts=NOW - 1800, reason="Review.", replied=True)]
    picked = needs_you(rows, now=NOW, day_end=NOW + 6 * 3600)
    assert [i["id"] for i in picked["items"]] == ["email:a"]
