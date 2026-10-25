"""Deadline (block_time) cards, Brief stats and lede, and thread attention.

Synthetic calendars and a fake Day store only: no live store, no model.
"""
import asyncio
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-deadlines-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.proactive import cycle, deadlines, executor, triage
from service.proactive.prepare import todo_id
from service.proactive.store import ProactiveStore

TZ = "America/Los_Angeles"
ZONE = ZoneInfo(TZ)
DAY = "2026-10-24"


def at(hour, minute=0, day=24):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


NOON = at(13)
HACK = {"id": "cal-hack", "title": "CruzHacks Hack Day", "source": "calendar", "kind": "meeting",
        "when_ts": at(11, 45), "end_ts": at(17), "all_day": 0}
WORKSHEET = {"id": "cal-ws3", "title": "Worksheet 3 [STAT-5-02]", "source": "calendar", "kind": "event",
             "when_ts": at(0, 0, 25), "all_day": 1, "account": "me"}
READING = {"id": "cal-read", "title": "Week 4 Reading: Strings [CSE-20-04]", "source": "calendar", "kind": "event",
           "when_ts": at(0), "all_day": 1, "account": "me"}


class FakeDay:
    """today_snapshot / today_save_task / today_task over in-memory rows."""

    def __init__(self, commitments=(), tasks=(), fresh=True, now=NOON):
        self.commitments = list(commitments)
        self.tasks = {t["id"]: dict(t) for t in tasks}
        self.fresh, self.now = fresh, now

    def today_snapshot(self, day, timezone):
        lo = datetime.fromisoformat(day).replace(tzinfo=ZONE).timestamp()
        stamp = self.now - 5 if self.fresh else self.now - 3600
        return {"tasks": [t for t in self.tasks.values() if t["day"] == day],
                "commitments": [c for c in self.commitments if lo - 86400 <= c["when_ts"] < lo + 86400],
                "preferences": {"start_minute": 540, "end_minute": 1080, "not_before": None},
                "sources": {"calendar": {"available": True, "last_sync": stamp,
                                         "diagnostics": {"coverage_start": lo - 86400, "coverage_end": lo + 3 * 86400}},
                            "reminders": {"available": True, "last_sync": stamp}}}

    def today_save_task(self, values):
        from service.assistant.today import validate_task
        task = validate_task({"kind": "task", "priority": 2, "status": "active", **values,
                              "id": f"task-{len(self.tasks) + 1}", "revision": 1})
        self.tasks[task["id"]] = task
        return task

    def today_task(self, task_id):
        return self.tasks.get(task_id)


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


# --- candidates and slots ----------------------------------------------------------


def test_candidates_are_work_due_soon_never_test_reminders_or_places_to_be():
    rows = [WORKSHEET, READING, HACK,
            {"id": "r-test", "title": "Wisp verification batch beta 2115", "source": "reminders", "kind": "reminder",
             "when_ts": at(16), "all_day": 0},
            {"id": "r-live", "title": "Wisp live reminder test 2102", "source": "reminders", "kind": "reminder",
             "when_ts": at(16), "all_day": 0},
            {"id": "r-buy", "title": "Return library books", "source": "reminders", "kind": "reminder",
             "when_ts": at(16, 0, 26), "all_day": 0},
            {"id": "r-far", "title": "Essay draft", "source": "reminders", "kind": "reminder",
             "when_ts": at(16, 0, 29), "all_day": 0},
            {"id": "r-junk", "title": "New Reminder", "source": "reminders", "kind": "reminder",
             "when_ts": at(16), "all_day": 0}]
    got = deadlines.candidates(rows, NOON, TZ)
    assert [c["title"] for c in got] == ["Week 4 Reading: Strings for CSE 20", "Worksheet 3 for STAT 5",
                                         "Return library books"]
    assert [c["minutes"] for c in got] == [30, 60, 30]
    assert deadlines.is_test("Wisp verification timed 2113") and not deadlines.is_test("Worksheet 3")


def test_a_deadline_card_holds_a_real_free_hour_after_the_event():
    day = FakeDay([HACK])
    cards = deadlines.deadline_cards([WORKSHEET], day.today_snapshot, NOON, TZ)
    assert len(cards) == 1
    card = cards[0]
    assert card["meta"] == "DUE TOMORROW · CALENDAR"
    assert card["title"] == "Worksheet 3 for STAT 5"
    assert card["body"] == "You're free 5–6 PM after CruzHacks Hack Day. I can hold that hour."
    assert card["action"]["button"] == "Block 5–6 PM?"
    assert card["action"]["method"] == "block_time"
    assert card["action"]["slot"] == {"start": at(17), "end": at(18)}
    assert card["todo_id"] == todo_id("cal-ws3")


def test_slots_never_overlap_each_other_and_move_to_the_next_day_when_today_is_full():
    full = dict(HACK, when_ts=at(13), end_ts=at(18))
    day = FakeDay([full])
    two = deadlines.deadline_cards([WORKSHEET, dict(WORKSHEET, id="cal-hw", title="HW3 [STAT-5-02]")],
                                   day.today_snapshot, NOON, TZ)
    starts = [c["action"]["slot"]["start"] for c in two]
    assert starts == [at(9, 0, 25), at(10, 0, 25)]
    assert two[0]["action"]["button"] == "Block 9–10 AM tomorrow?"


def test_no_card_when_the_calendar_is_stale_or_the_work_is_already_on_the_day_plan():
    assert deadlines.deadline_cards([WORKSHEET], FakeDay([HACK], fresh=False).today_snapshot, NOON, TZ) == []
    planned = {"id": "t1", "title": "Worksheet 3 for STAT 5", "day": DAY, "timezone": TZ, "kind": "study",
               "duration_minutes": 60, "priority": 2, "due_ts": None, "pinned_start": None, "status": "active",
               "revision": 1}
    assert deadlines.deadline_cards([WORKSHEET], FakeDay([HACK], [planned]).today_snapshot, NOON, TZ) == []


def test_a_slot_starts_on_the_half_hour():
    day = FakeDay([], now=at(9, 7))
    card = deadlines.deadline_cards([READING], day.today_snapshot, at(9, 7), TZ)[0]
    assert card["action"]["slot"]["start"] == at(9, 30)
    assert card["action"]["button"] == "Block 9:30–10 AM?"


# --- firing ------------------------------------------------------------------------


def _stored(store, card):
    store.save_state(as_of=NOON, timezone=TZ, accounts=["me"], cards=[card], todos=[], notes=[], threads=[],
                     summaries={}, judged={}, model=True)


def test_fire_pins_a_wisp_day_task_and_marks_the_todo_added_not_done(store):
    day = FakeDay([HACK])
    card = deadlines.deadline_cards([WORKSHEET], day.today_snapshot, NOON, TZ)[0]
    _stored(store, card)
    result = asyncio.run(executor.fire(card["id"], store=store, now=NOON,
                                       methods={"block_time": lambda c: deadlines.hold(c, now=NOON, store=day)}))
    assert result["ok"] is True and result["removed"] is True
    task = next(iter(day.tasks.values()))
    assert task["pinned_start"] == at(17) and task["duration_minutes"] == 60 and task["kind"] == "study"
    assert task["title"] == "Worksheet 3 for STAT 5"
    assert store.card(card["id"]) is None
    assert card["todo_id"] in store.added_to_day() and not store.is_done(card["todo_id"])


def test_hold_refuses_a_slot_that_was_taken_or_has_passed_and_never_doubles():
    day = FakeDay([HACK])
    card = deadlines.deadline_cards([WORKSHEET], day.today_snapshot, NOON, TZ)[0]
    taken = FakeDay([HACK, {"id": "cal-new", "title": "Dinner", "source": "calendar", "kind": "meeting",
                            "when_ts": at(17, 30), "end_ts": at(19), "all_day": 0}])
    assert deadlines.hold(card, now=NOON, store=taken)["status"] == "blocked" and not taken.tasks
    assert deadlines.hold(card, now=at(17, 30), store=day)["status"] == "blocked"
    first = deadlines.hold(card, now=NOON, store=day)
    again = deadlines.hold(card, now=NOON, store=day)
    assert first["ok"] and again["ok"] and again["task_id"] == first["task_id"] and len(day.tasks) == 1


# --- ranking, stats, lede ------------------------------------------------------------


def _card(cid, method, **extra):
    return {"id": cid, "title": cid, "action": {"method": method, "draft": "Hi" if "reply" in method else ""},
            "sources": [{"who": extra.pop("who", "")}], **extra}


def test_waiting_people_come_first_then_deadlines_then_the_rest_capped_at_five():
    cards = [_card("remind", "reminder"), _card("text", "text_reply"), _card("mail", "email_reply")]
    blocks = [_card(f"block{i}", "block_time") for i in range(4)]
    ordered = [c["id"] for c in cycle.order_cards(cards, blocks)]
    assert ordered == ["text", "mail", "block0", "block1", "block2"]


def test_the_cycle_adds_deadline_cards_and_counts_what_it_read(store):
    day = FakeDay([HACK])

    async def no_model(system, material, abort, tokens):
        return None
    items = [{"id": "m1", "source": "mail", "account": "me", "who": "Ed", "title": "Exam", "text": "Exam Wed.",
              "received_ts": NOON - 3600, "hash": "h1", "replied": False, "person": True},
             {"id": "n1", "source": "notes", "account": "me", "who": "", "title": "buy milk", "text": "buy milk",
              "received_ts": None, "hash": "h2", "replied": False}]
    asyncio.run(cycle.run_cycle(
        now=NOON, tz=TZ, force=True, reply_fn=no_model, abort=lambda: False, collect=lambda n, z: list(items),
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, linked=set(), own="me",
        allowed=set(), deadline_fn=lambda n, z, skip, own, linked: deadlines.deadline_cards(
            [WORKSHEET], day.today_snapshot, n, z, skip=skip)))
    view = cycle.snapshot(now=NOON, tz=TZ, store=store, schedule_fn=lambda n, z: {"calendar": [], "reminders": []},
                          own="me", linked=set())
    assert [c["action"]["method"] for c in view["cards"]] == ["block_time", "add_to_day"]
    assert view["stats"]["read"] == 1 and view["stats"]["read_breakdown"] == {"mail": 1, "texts": 0, "notes": 1}
    assert view["stats"]["drafted"] == 0 and view["stats"]["as_of"] == NOON
    assert view["stats"]["found"] >= 1
    assert view["lede"] == "Worksheet 3 for STAT 5 is due tomorrow, and I found you an hour for it."
    # A slot that has started is no longer offered.
    later = cycle.snapshot(now=at(17, 5), tz=TZ, store=store,
                           schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, own="me", linked=set())
    assert [c["action"]["method"] for c in later["cards"]] == ["add_to_day"]


def test_the_lede_leads_with_the_next_event_and_a_waiting_person_and_never_a_test_reminder():
    todos = [{"id": "e", "title": "CruzHacks Hack Day", "source": "calendar", "due_ts": at(13, 45), "done": False,
              "timed": True},
             {"id": "t", "title": "Wisp verification timed 2113", "source": "calendar", "due_ts": at(13, 15),
              "done": False, "timed": True}]
    block = {"id": "b", "title": "Worksheet 3 for STAT 5", "due_words": "due tomorrow",
             "action": {"method": "block_time", "envelope": {"duration_min": 60}}}
    reply = {"id": "r", "title": "Reply to Sriram", "sources": [{"who": "Sriram Kannan"}],
             "action": {"method": "text_reply", "draft": "Sounds good!"}}
    assert cycle._lede([block], todos, "summary", NOON, TZ) == \
        "CruzHacks Hack Day at 1:45. Worksheet 3 for STAT 5 is due tomorrow, and I found you an hour for it."
    assert cycle._lede([block, reply], todos, "summary", NOON, TZ) == \
        "CruzHacks Hack Day at 1:45. Sriram is waiting on you, and I drafted a reply."
    assert cycle._lede([], [todos[1]], "Nothing today.", NOON, TZ) == "Nothing today."


def test_stats_count_cards_and_open_non_calendar_todos_once_and_skip_test_reminders():
    todos = [{"id": "a", "title": "Reply to Avika", "source": "messages", "done": False},
             {"id": "b", "title": "Wisp verification batch 2115", "source": "reminders", "done": False},
             {"id": "c", "title": "Hack Day", "source": "calendar", "done": False},
             {"id": "d", "title": "Done thing", "source": "mail", "done": True}]
    cards = [{"id": "x", "todo_id": "a", "action": {"draft": "Hi!"}}, {"id": "y", "todo_id": "z", "action": {}}]
    stats = cycle._stats({"stats": {"me": {"read": 49}}, "as_of": NOON}, cards, todos, None)
    assert stats == {"read": 49, "found": 2, "drafted": 1, "filtered": 0,
                     "read_breakdown": {"mail": 0, "texts": 0, "notes": 0}, "as_of": NOON}


def test_read_counts_every_mail_and_text_looked_at_including_the_bulk_left_out():
    from service.proactive import sources
    now = NOON
    me = "me@example.com"

    def row(i, sender, address, subject, body):
        return dict(ts=now - 3600 - i, account="iCloud", account_id="a", sender=f"{sender} <{address}>",
                    sender_address=address, to=me, subject=subject, message_id=f"m{i}", body=body, unread=True)
    rows = [row(i, "Shop Weekly", "news@shop.example", f"Sale {i}", "Big sale! Unsubscribe here.") for i in range(5)]
    rows.append(row(9, "Sam Lee", "sam@example.com", "Coffee?", "Want to grab coffee Friday?"))
    texts = [(now - 600, "Mom", "Mom: call me?"), (now - 500, "72345", "72345: Vote YES on Measure I")]
    counts: dict = {}
    mail = sources.mail_items(rows, mine={me}, now=now, tz=TZ, linked=set(), own=me, counts=counts)
    msgs = sources.text_items(texts, now=now, own=me, counts=counts)
    assert [i["who"] for i in mail] == ["Sam Lee"] and [i["who"] for i in msgs] == ["Mom"]
    assert counts[me] == {"mail": 6, "texts": 2, "notes": 0, "filtered": 6}
    assert cycle._looked_stats(counts[me]) == {"read": 8, "filtered": 6,
                                               "read_breakdown": {"mail": 6, "texts": 2, "notes": 0}}
    state = {"stats": {me: cycle._looked_stats(counts[me])}, "as_of": now}
    stats = cycle._stats(state, [], [], None)
    assert stats["read"] == 8 and stats["filtered"] == 6 and stats["read_breakdown"]["mail"] == 6


# --- attention -----------------------------------------------------------------------


def _item(source, text, **extra):
    base = {"id": "i", "source": source, "account": "me", "who": "Sam", "title": "t", "text": text,
            "received_ts": NOON - 2 * 86400 - 60, "replied": False, "person": True}
    return base | extra


def test_needs_reply_only_for_a_person_whose_last_message_is_unanswered():
    assert cycle.needs_reply(_item("messages", "Sam: are you coming?"), {}, NOON)
    assert not cycle.needs_reply(_item("messages", "Me: yes", replied=True), {}, NOON)
    assert not cycle.needs_reply(_item("messages", "Sam: hi", group=True), {}, NOON)
    assert cycle.needs_reply(_item("mail", "Can you send the slides?"), {"asks_of_me": "Send the slides"}, NOON)
    assert not cycle.needs_reply(_item("mail", "Exam is Wednesday."), {"asks_of_me": "Study"}, NOON)
    assert not cycle.needs_reply(_item("mail", "Sale?", person=False), {"asks_of_me": "Buy"}, NOON)


def test_attention_is_reply_then_a_dated_deadline_from_the_source_then_fyi():
    ed = _item("mail", "Reminder: STAT 5 Exam 1 is next Wednesday, closed-book.", received_ts=at(9, 0, 22))
    deadline = cycle.mentioned_deadline(ed, NOON, TZ)
    assert deadline and datetime.fromtimestamp(deadline["due_ts"], ZONE).date().isoformat() == "2026-10-28"
    assert cycle.mentioned_deadline(_item("mail", "Party next Wednesday!", received_ts=at(9, 0, 22)), NOON, TZ) is None
    assert cycle.mentioned_deadline(_item("mail", "The exam was on October 20.", received_ts=at(9, 0, 19)),
                                    NOON, TZ) is None
    threads = [{"who": "Ed", "source": "mail", "deadline": deadline, "needs_reply": False, "received_ts": at(9)},
               {"who": "Sam", "source": "messages", "needs_reply": True, "received_ts": NOON - 2 * 86400 - 60},
               {"who": "Ana", "source": "mail", "needs_reply": True, "asked": True, "received_ts": NOON - 600},
               {"who": "Mom", "source": "messages", "needs_reply": False, "replied": True, "received_ts": NOON}]
    seen = [(t["who"], *cycle.attention(t, NOON, TZ)) for t in threads]
    assert seen == [("Ed", "deadline", "exam next Wed"), ("Sam", "reply", "waiting 2 days"),
                    ("Ana", "reply", "asked you a question"), ("Mom", "fyi", "you answered last")]
    ordered = cycle.order_threads([{**t, "attention": a} for t, (_, a, _r) in zip(threads, seen)])
    assert [t["who"] for t in ordered] == ["Ana", "Sam", "Ed", "Mom"]


def test_summaries_that_describe_or_paste_the_message_are_templated():
    mail = {"who": "Dhruv Kolte", "source": "mail", "user_wrote": False}
    assert not triage.sane("Dhruv Kolte sent this email.", mail)
    assert not triage.sane("You sent Sriram Kannan: I would look into it more.", {"source": "messages",
                                                                                   "user_wrote": True})
    assert not triage.sane("You: “I agree, I'd look into it more to see if you fit, and yes.”",
                           {"source": "messages", "user_wrote": True})
    assert not triage.sane("From Dhruv Kolte: Quality Of Life Updates.", mail)
    assert triage.sane("Devarsh emailed about the 180 Degrees application.", mail)
