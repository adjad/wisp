"""Quiet hours hold what asks for attention until they end, and drop only what no longer
matters (service/quiet_hold.py, at Hub.events). Disposable store and a real Hub."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from service import quiet_hold as Q
from service.assistant import scheduler
from service.assistant.hub import Hub
from service.assistant.store import AssistantStore

NOW = time.time()


@pytest.fixture
def quiet(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(Q, "_quiet_now", lambda: state["on"])
    return state


@pytest.fixture
def hub(tmp_path):
    return Hub(AssistantStore(tmp_path / "assistant.db"))


def drain(hub: Hub) -> list[dict]:
    """One stream, as the app sees it: replay, then the server rotates it."""
    async def run():
        got = []
        async for event in hub.events(rotate_after=0.05):
            if event.get("type") != "heartbeat":
                got.append(event)
        return got
    return asyncio.run(run())


def state(hub, event) -> str:
    return hub.store.event(event["event_id"])["state"]


def today() -> str:
    return datetime.fromtimestamp(NOW).date().isoformat()


def brief(**extra) -> dict:
    return {"type": "daily_brief", "part_of_day": "morning", "text": "Morning.", "created_at": NOW} | extra


ATTENTION = [
    {"type": "nudge", "title": "t"},
    {"type": "checkin", "day": today(), "text": "How did it go?"},
    brief(),
    {"type": "attention_added", "source_id": "msg:1", "title": "t", "relevant_until": NOW + 86400},
    {"type": "email_summary", "summary": "s"},
    {"type": "node_result", "title": "t", "text": "x"},
    {"type": "codex_task_update", "kind": "completed", "title": "t"},
    {"type": "google_signin_expired", "account": "a@example.com", "expired_at": 1},
    {"type": "reminder", "title": "Exam", "commitment_id": "c", "stage": "T-1d"},
]
ids = lambda rows: [row["type"] + ":" + str(row.get("stage", "")) for row in rows]  # noqa: E731


@pytest.mark.parametrize("event", ATTENTION, ids=ids(ATTENTION))
def test_held_during_quiet_hours_and_delivered_once_they_end(hub, quiet, event):
    published = hub.publish_sync(dict(event), target={"type": "note"})
    assert drain(hub) == [], "nothing reaches the app"
    assert state(hub, published) == "pending", "and nothing is lost"
    assert hub.store.event(published["event_id"])["attempts"] == 0, "held is not a failed delivery"
    quiet["on"] = False
    assert [e["event_id"] for e in drain(hub)] == [published["event_id"]]


@pytest.mark.parametrize("event", [
    {"type": "reminder", "title": "Water", "commitment_id": "c", "stage": "due"},
    {"type": "reminder", "title": "Standup", "commitment_id": "c", "stage": "T-10m"},
    {"type": "reminder", "title": "Exam", "commitment_id": "c", "stage": "T-3h"},
    {"type": "scheduled_send_result", "id": "s"},
    {"type": "scheduled_send_missed", "id": "s"},
    {"type": "calendar_action_unknown", "error": "e"},
    {"type": "reminder_action_unknown", "error": "e"},
    {"type": "sync_emails_now"},
    {"type": "changed"},
    brief(recomposed=True),
], ids=lambda e: e["type"] + ":" + str(e.get("stage", e.get("recomposed", ""))))
def test_what_the_person_set_or_started_arrives_as_usual(hub, quiet, event):
    published = hub.publish_sync(dict(event), target={"type": "note"})
    assert [e["event_id"] for e in drain(hub)] == [published["event_id"]]


def test_a_live_event_is_held_too_not_only_a_replay(hub, quiet):
    async def run():
        got = []
        async for event in hub.events(rotate_after=0.1):
            got.append(event)
        return got
    async def scenario():
        task = asyncio.create_task(run())
        await asyncio.sleep(0.02)
        published = hub.publish_sync({"type": "email_summary", "summary": "s"})
        return published, await task
    published, got = asyncio.run(scenario())
    assert got == [] and state(hub, published) == "pending"


def test_no_quiet_hours_holds_nothing(hub, quiet):
    quiet["on"] = False
    published = hub.publish_sync(brief())
    assert [e["event_id"] for e in drain(hub)] == [published["event_id"]]


def test_transient_events_cannot_wait_so_they_pass(hub, quiet):
    async def scenario():
        queue = hub.subscribe()
        hub.publish_sync({"type": "attention_added", "source_id": "m"}, durable=False)
        return queue.get_nowait()
    assert asyncio.run(scenario())["type"] == "attention_added"
    assert not Q.held({"type": "attention_added"})


# --- dropped only when it no longer matters ---------------------------------


YESTERDAY = (datetime.fromtimestamp(NOW) - timedelta(days=1))


def test_last_nights_check_in_is_dropped_once_it_is_tomorrow(hub, quiet):
    published = hub.publish_sync({"type": "checkin", "day": YESTERDAY.date().isoformat(), "text": "How did today go?"},
                                 target={"type": "note"})
    assert drain(hub) == [] and state(hub, published) == "pending"
    quiet["on"] = False
    assert drain(hub) == []
    assert state(hub, published) == "ack", "finished, so it never comes back"


def test_last_nights_brief_gives_way_to_the_morning(hub, quiet):
    published = hub.publish_sync(brief(part_of_day="evening", created_at=YESTERDAY.timestamp()),
                                 target={"type": "note"})
    quiet["on"] = False
    assert drain(hub) == [] and state(hub, published) == "ack"


def test_a_reminder_alert_is_dropped_once_the_plan_has_passed(hub, quiet):
    gone = hub.publish_sync({"type": "attention_added", "source_id": "a", "relevant_until": NOW - 60},
                            target={"type": "note"})
    still = hub.publish_sync({"type": "attention_added", "source_id": "b", "relevant_until": NOW + 3600},
                             target={"type": "note"})
    quiet["on"] = False
    assert [e["event_id"] for e in drain(hub)] == [still["event_id"]]
    assert state(hub, gone) == "ack"


def test_an_alert_with_no_clock_is_kept(hub, quiet):
    assert not Q.stale({"type": "google_signin_expired"}) and not Q.stale({"type": "email_summary"})
    assert not Q.stale({"type": "attention_added"}) and not Q.stale(brief(created_at="soon"))


def test_nothing_outside_the_list_is_ever_called_stale():
    assert not Q.stale({"type": "reminder", "stage": "due", "day": "1999-01-01"})
    assert not Q.stale({"type": "send_email", "created_at": 1.0})


# --- the first Brief --------------------------------------------------------


def test_someone_finishing_onboarding_at_night_still_gets_their_first_brief(hub, quiet):
    hub.store.record_completion(scheduler.ONBOARDING_ANSWERED_KEY, repr(time.time() - 60))
    published = hub.publish_sync(brief())
    assert [e["event_id"] for e in drain(hub)] == [published["event_id"]]


def test_the_grace_ends(hub, quiet):
    hub.store.record_completion(scheduler.ONBOARDING_ANSWERED_KEY, repr(time.time() - 3 * 3600))
    hub.publish_sync(brief())
    assert drain(hub) == []


def test_only_the_first_brief_gets_the_grace(hub, quiet):
    hub.store.record_completion(scheduler.ONBOARDING_ANSWERED_KEY, repr(time.time()))
    hub.publish_sync({"type": "email_summary", "summary": "s"})
    assert drain(hub) == []


# --- reading the setting ----------------------------------------------------


def test_the_hold_follows_the_one_setting(monkeypatch, tmp_path):
    from service import user_hours as U
    monkeypatch.undo()                    # the real _quiet_now, on this test's own file
    monkeypatch.setattr(U, "path", lambda: tmp_path / "user_hours.json")
    minute = datetime.now()
    start = minute.hour * 60 + minute.minute
    U.save(quiet={"start_minute": start, "end_minute": (start + 5) % 1440})
    assert Q._quiet_now() is True
    U.save(quiet={"start_minute": 0, "end_minute": 0})
    assert Q._quiet_now() is False


def test_attention_alerts_are_stored_only_while_they_must_wait(monkeypatch):
    from service.assistant import attention_runner as wiring
    seen = []
    async def publish(event, **kwargs):
        seen.append(kwargs)
    monkeypatch.setattr(wiring.hub, "publish", publish)
    monkeypatch.setattr(Q, "_quiet_now", lambda: False)
    asyncio.run(wiring._publish({"type": "attention_added", "source_id": "m"}))
    monkeypatch.setattr(Q, "_quiet_now", lambda: True)
    asyncio.run(wiring._publish({"type": "attention_added", "source_id": "m"}))
    assert seen == [{"durable": False}, {"dedupe_key": "attention:m"}]


# --- A held event's words stay only while it waits ------------------------------------------

WORD = "Marigold"           # stands for what a message, Brief or reminder really said
PERSON = "sam@example.com"


def words(event: dict) -> dict:
    return event | {"quote": WORD, "sender": PERSON, "title": WORD, "text": WORD, "summary": WORD,
                    "today_summary": WORD, "messages_summary": WORD, "context": WORD}


def stored(store, event) -> str:
    row = store._db.execute("SELECT payload,target,dedupe_key FROM assistant_events WHERE id=?",
                            (event["event_id"],)).fetchone()
    return " ".join(row)


def on_disk(store) -> bytes:
    """The file itself, with nothing left in a journal, as anyone reading the Mac would find it."""
    store._db.commit()
    path = store.path
    store._db.close()
    return path.read_bytes() + b"".join(p.read_bytes() for p in path.parent.glob(path.name + "-*"))


def test_the_list_here_is_the_list_that_is_held():
    from service.assistant.store import HELD_EVENT_KINDS
    assert set(HELD_EVENT_KINDS) == Q.HELD_TYPES | {"reminder"}


@pytest.mark.parametrize("event", ATTENTION, ids=ids(ATTENTION))
def test_no_words_left_once_acknowledged(hub, quiet, event):
    published = hub.publish_sync(words(event), target={"type": "note"})
    assert WORD in stored(hub.store, published), "it has to wait with its words"
    quiet["on"] = False
    assert drain(hub)[0]["quote"] == WORD, "they reach the person"
    hub.store.acknowledge_event(published["event_id"], event["type"])
    row = hub.store.event(published["event_id"])
    assert row["state"] == "ack" and row["payload"]["type"] == event["type"]
    assert WORD not in stored(hub.store, published) and PERSON not in stored(hub.store, published)
    assert hub.store.acknowledge_event(published["event_id"], event["type"]) is True, "a lost ack retries"


@pytest.mark.parametrize("event", [
    {"type": "attention_added", "source_id": "msg:1", "relevant_until": NOW - 60},
    {"type": "checkin", "day": "2020-01-01"},
    {"type": "daily_brief", "part_of_day": "evening", "created_at": NOW - 3 * 86400},
], ids=lambda e: e["type"])
def test_no_words_left_once_dropped_as_stale(hub, quiet, event):
    published = hub.publish_sync(words(event))
    quiet["on"] = False
    assert drain(hub) == []
    assert state(hub, published) == "ack"
    assert WORD not in stored(hub.store, published)


def test_no_words_left_once_a_newer_one_replaces_it(hub, quiet):
    old = hub.publish_sync(words(brief()), dedupe_key="daily_brief:d:morning",
                           target={"type": "brief", "date": "d", "part": "morning"})
    hub.publish_sync(brief(part_of_day="evening"), dedupe_key="daily_brief:d:evening",
                     target={"type": "brief", "date": "d", "part": "evening"})
    assert state(hub, old) == "superseded"
    assert WORD not in stored(hub.store, old)


def test_no_words_left_when_finished_by_code_outside_the_store(hub, quiet):
    """The behavior runtime retires and expires nudges with its own SQL."""
    from service.assistant.behavior_runtime import BehaviorRuntime
    event = hub.publish_sync(words({"type": "nudge", "nudge_id": "n"}), dedupe_key="behavior:n", expires_at=NOW - 1)
    BehaviorRuntime.expire_events(object(), hub.store, NOW)
    assert state(hub, event) == "superseded" and WORD not in stored(hub.store, event)


def test_a_reminder_target_loses_its_title_too(hub, quiet):
    event = hub.store.enqueue_event(words({"type": "reminder", "commitment_id": "c", "stage": "T-1d"}),
                                    dedupe_key="reminder:abc",
                                    target={"type": "reminder", "identity": [WORD.casefold(), 1], "stages": ["T-1d"]})
    with hub.store.transaction() as db:
        db.execute("UPDATE assistant_events SET state='superseded' WHERE id=?", (event["id"],))
    assert hub.store.event(event["id"])["target"] == {"type": "reminder"}
    assert WORD.casefold() not in stored(hub.store, {"event_id": event["id"]})


def test_the_words_are_gone_from_the_file_not_only_the_row(hub, quiet):
    published = hub.publish_sync(words({"type": "attention_added", "source_id": "msg:1", "relevant_until": NOW + 60}))
    hub.store.acknowledge_event(published["event_id"], "attention_added")
    disk = on_disk(hub.store)
    assert WORD.encode() not in disk and PERSON.encode() not in disk


def test_other_events_keep_what_they_said(hub, quiet):
    event = hub.publish_sync({"type": "action_outcome", "text": WORD}, target={"type": "note"})
    hub.store.acknowledge_event(event["event_id"], "action_outcome")
    assert hub.store.event(event["event_id"])["payload"]["text"] == WORD


def test_a_finished_event_still_stops_a_repeat(hub, quiet):
    quiet["on"] = False
    first = hub.publish_sync(words({"type": "attention_added", "source_id": "msg:1"}), dedupe_key="attention:msg:1")
    assert len(drain(hub)) == 1
    hub.store.acknowledge_event(first["event_id"], "attention_added")
    again = hub.publish_sync(words({"type": "attention_added", "source_id": "msg:1"}), dedupe_key="attention:msg:1")
    assert again["event_id"] == first["event_id"] and drain(hub) == []


def test_a_brief_receipt_still_says_which_run_it_was(hub, quiet, monkeypatch):
    """The day of an upgrade reads an older version's once-a-day receipt."""
    day = datetime.fromtimestamp(NOW).date()
    event = hub.publish_sync(words(brief(part_of_day="evening")), dedupe_key=f"daily_brief:{day.isoformat()}")
    hub.store.acknowledge_event(event["event_id"], "daily_brief")
    monkeypatch.setattr(scheduler, "assistant_store", hub.store)
    assert scheduler._slot_delivered(day, "evening") is True
    assert scheduler._slot_delivered(day, "morning") is False


def test_a_restart_during_quiet_hours_loses_nothing(tmp_path, quiet):
    path = tmp_path / "assistant.db"
    first = Hub(AssistantStore(path))
    published = [first.publish_sync(words(event), target={"type": "note"}) for event in ATTENTION]
    assert drain(first) == []
    first.store._db.close()

    second = Hub(AssistantStore(path))
    assert drain(second) == [], "still quiet after the restart"
    third = Hub(AssistantStore(path))
    quiet["on"] = False
    got = {event["event_id"]: event for event in drain(third)}
    assert set(got) == {event["event_id"] for event in published}
    assert all(event["quote"] == WORD for event in got.values()), "with what they said"


def _earlier_build(path, *rows) -> None:
    """Rows as a build without the trigger left them."""
    AssistantStore(path)._db.close()
    import json
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER assistant_events_scrub_finished")
    for index, (kind, state_, key) in enumerate(rows):
        db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at,state) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (f"old{index}", kind, json.dumps(words({"type": kind, "part_of_day": "morning"})), key,
                    json.dumps({"type": "reminder", "identity": [WORD]} if kind == "reminder" else {}),
                    NOW, state_))
    db.commit()
    db.close()


def test_rows_an_earlier_build_finished_are_scrubbed(tmp_path):
    path = tmp_path / "assistant.db"
    _earlier_build(path, ("attention_added", "ack", "attention:msg:1"), ("nudge", "superseded", "behavior:n"),
                   ("daily_brief", "ack", "daily_brief:2026-11-01"), ("reminder", "ack", "reminder:a"),
                   ("email_summary", "ack", "e"), ("node_result", "ack", "node:1"), ("checkin", "ack", "checkin:d"),
                   ("codex_task_update", "ack", "c"),
                   ("google_signin_expired", "ack", f"google-signin-expired:{PERSON}:17"),
                   ("attention_added", "pending", "attention:msg:2"), ("action_outcome", "ack", "o"))
    store = AssistantStore(path)
    rows = {row["id"]: row for row in store._db.execute("SELECT * FROM assistant_events")}
    for key in [f"old{i}" for i in range(9)]:
        assert WORD not in rows[key]["payload"] + rows[key]["target"], rows[key]["kind"]
        assert PERSON not in rows[key]["payload"] + rows[key]["dedupe_key"], rows[key]["kind"]
    assert "morning" in rows["old2"]["payload"], "a Brief's receipt still says which run"
    assert WORD in rows["old9"]["payload"], "one still waiting keeps its words"
    assert WORD in rows["old10"]["payload"], "and other kinds are left alone"

    from service.assistant.store import signin_expired_key
    assert rows["old8"]["dedupe_key"] == signin_expired_key(PERSON, 17), "the same key a new notice uses"

    before = [tuple(row) for row in store._db.execute("SELECT * FROM assistant_events ORDER BY id")]
    store._db.close()
    again = AssistantStore(path)
    assert [tuple(row) for row in again._db.execute("SELECT * FROM assistant_events ORDER BY id")] == before
    again._db.close()


def test_an_earlier_builds_words_are_gone_from_the_file(tmp_path):
    path = tmp_path / "assistant.db"
    _earlier_build(path, ("attention_added", "ack", "attention:msg:1"))
    assert WORD.encode() in path.read_bytes()
    assert WORD.encode() not in on_disk(AssistantStore(path))


# --- Forget, account erase and reset --------------------------------------------------------

def test_disconnecting_an_account_withdraws_what_could_have_come_from_it(hub, quiet, monkeypatch):
    from service.assistant.store import signin_expired_key
    from service import gmail_source, google_calendar_source as calendar
    monkeypatch.setattr(calendar, "_store_override", hub.store)
    theirs = hub.publish_sync({"type": "google_signin_expired", "account": PERSON, "expired_at": 1},
                              dedupe_key=signin_expired_key(PERSON, 1))
    other = hub.publish_sync({"type": "google_signin_expired", "account": "me@example.com", "expired_at": 1},
                             dedupe_key=signin_expired_key("me@example.com", 1))
    composed = [hub.publish_sync(words(event)) for event in
                (brief(), {"type": "email_summary"}, {"type": "nudge", "nudge_id": "n"})]
    event = hub.publish_sync(words({"type": "reminder", "stage": "T-1d", "commitment_id": "c",
                                    "source_id": f"{calendar.PREFIX}{PERSON}:ev"}))
    unrelated = [hub.publish_sync(words(event)) for event in
                 ({"type": "attention_added", "source_id": "msg:1", "relevant_until": NOW + 86400},
                  {"type": "node_result"})]
    assert gmail_source.erase_account_data(PERSON.upper())["waiting_notices"] == "erased"
    assert hub.store.event(theirs["event_id"]) is None
    assert hub.store.event(event["event_id"]) is None, "the calendar's own erase covers its reminders"
    for row in composed:
        assert state(hub, row) == "superseded" and WORD not in stored(hub.store, row)
    quiet["on"] = False
    shown = {row["event_id"] for row in drain(hub)}
    assert shown == {other["event_id"], *(row["event_id"] for row in unrelated)}, "what isn't theirs still arrives"


def test_forget_removes_learned_nudges_and_held_alerts(hub, quiet):
    nudge = hub.publish_sync(words({"type": "nudge", "nudge_id": "n"}), dedupe_key="behavior:n")
    alert = hub.publish_sync(words({"type": "attention_added", "source_id": "msg:1", "relevant_until": NOW + 86400}),
                             dedupe_key="attention:msg:1")
    brief_row = hub.publish_sync(brief())
    assert hub.store.drop_behavior_events() == 1, "the count still means nudges"
    assert hub.store.event(nudge["event_id"]) is None and hub.store.event(alert["event_id"]) is None
    assert state(hub, brief_row) == "pending"
    assert WORD.encode() not in on_disk(hub.store)


def test_the_forget_tool_withdraws_what_says_the_same_thing(hub, quiet, monkeypatch):
    from service.assistant import store as assistant
    from service.tools import memory_tools
    monkeypatch.setattr(assistant, "assistant_store", hub.store)
    monkeypatch.setattr(memory_tools.store, "delete_matching", lambda query: 0)
    names = hub.publish_sync({"type": "attention_added", "source_id": "m", "quote": "my gate code is 1234",
                              "relevant_until": NOW + 86400})
    says_less = hub.publish_sync({"type": "email_summary", "summary": "the gate was repaired"})
    reminder = hub.publish_sync({"type": "reminder", "stage": "T-1d", "commitment_id": "c", "title": "gate code"})
    reply = asyncio.run(memory_tools.forget("my gate code"))
    assert "1 notice" in reply and "No exact matching" in reply
    assert state(hub, names) == "superseded" and "1234" not in stored(hub.store, names)
    assert state(hub, says_less) == "pending", "every word has to match, as it does for a memory"
    assert state(hub, reminder) == "pending", "the reminder is theirs and stays"
    assert "Use recall" in asyncio.run(memory_tools.forget("nothing like this")), "the old answer otherwise"
    assert hub.store.forget_held_matching(lambda text: False) == 0


def test_reset_leaves_no_held_event_behind(tmp_path, quiet):
    from service import wisp_reset
    user_home = tmp_path / "user"
    home = user_home / ".moe"
    home.mkdir(parents=True)
    store = AssistantStore(home / "assistant.db")
    Hub(store).publish_sync(words({"type": "attention_added", "source_id": "m", "relevant_until": NOW + 86400}))
    store._db.close()
    out = wisp_reset.erase_home(home, user_home)
    assert "assistant.db" in out["removed"] and not (home / "assistant.db").exists()
    assert AssistantStore(home / "assistant.db").pending_events() == []


# --- start-up cost and what earlier builds left in the file ----------------------------------

def _finished_behind_the_triggers_back(path) -> None:
    """A finished row the trigger never saw: if a start scrubs it, that start went over the table."""
    import json
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("INSERT INTO assistant_events(id,kind,payload,dedupe_key,target,created_at,state) "
               "VALUES('unseen','nudge',?,'behavior:unseen','{}',?,'ack')", (json.dumps(words({"type": "nudge"})), NOW))
    db.commit()
    db.close()


def _payload(path, row_id: str) -> str:
    import sqlite3
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT payload FROM assistant_events WHERE id=?", (row_id,)).fetchone()[0]
    finally:
        db.close()


def _schema_version(path) -> int:
    import sqlite3
    db = sqlite3.connect(path)
    try:
        return db.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        db.close()


def test_a_later_start_doesnt_go_over_the_table_or_rewrite_the_schema(tmp_path):
    path = tmp_path / "assistant.db"
    AssistantStore(path)._db.close()
    _finished_behind_the_triggers_back(path)
    version = _schema_version(path)
    AssistantStore(path)._db.close()
    assert WORD in _payload(path, "unseen")
    assert _schema_version(path) == version


def test_a_changed_trigger_goes_over_the_table_again(tmp_path):
    import sqlite3
    path = tmp_path / "assistant.db"
    AssistantStore(path)._db.close()
    _finished_behind_the_triggers_back(path)
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER assistant_events_scrub_finished")
    db.execute("CREATE TRIGGER assistant_events_scrub_finished AFTER UPDATE OF state ON assistant_events "
               "WHEN NEW.kind='nudge' BEGIN UPDATE assistant_events SET payload='{}' WHERE id=NEW.id; END")
    db.commit()
    db.close()
    AssistantStore(path)._db.close()
    assert WORD not in _payload(path, "unseen"), "an earlier build's list of kinds"
    db = sqlite3.connect(path)
    db.execute("UPDATE assistant_events SET state='pending',payload=? WHERE id='unseen'", (WORD,))
    db.execute("UPDATE assistant_events SET state='ack' WHERE id='unseen'")
    db.commit()
    db.close()
    assert WORD not in _payload(path, "unseen"), "and the trigger now is this build's"


def _freed_by_an_earlier_build(path) -> None:
    """Text deleted without secure_delete, as every build before it did."""
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete=OFF")
    db.execute("CREATE TABLE IF NOT EXISTS gone(text TEXT)")
    db.executemany("INSERT INTO gone VALUES(?)", [(WORD * 200,)] * 20)
    db.commit()
    db.execute("DROP TABLE gone")
    db.commit()
    db.close()


def _forget_it_was_cleared(path) -> None:
    from service.assistant.store import OLD_TEXT_CLEARED_KEY
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("DELETE FROM assistant_completion WHERE key=?", (OLD_TEXT_CLEARED_KEY,))
    db.commit()
    db.close()


def test_text_an_earlier_build_freed_is_cleared_once(tmp_path):
    from service.assistant.store import OLD_TEXT_CLEARED_KEY
    path = tmp_path / "assistant.db"
    first = Hub(AssistantStore(path))
    kept = [first.publish_sync({"type": "nudge", "nudge_id": str(i), "title": "stays"}) for i in range(3)]
    order = [row["id"] for row in first.store._db.execute("SELECT id FROM assistant_events ORDER BY rowid")]
    first.store._db.close()
    _forget_it_was_cleared(path)
    _freed_by_an_earlier_build(path)
    assert WORD.encode() in path.read_bytes()

    upgraded = AssistantStore(path)
    assert upgraded.completion(OLD_TEXT_CLEARED_KEY) is not None
    assert [row["id"] for row in upgraded._db.execute("SELECT id FROM assistant_events ORDER BY rowid")] == order
    assert all(upgraded.event(event["event_id"]) for event in kept), "everything still in use stays"
    assert upgraded._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert WORD.encode() not in on_disk(upgraded)

    _freed_by_an_earlier_build(path)
    size = path.stat().st_size
    AssistantStore(path)._db.close()
    assert WORD.encode() in path.read_bytes() and path.stat().st_size == size, "only the once"


def test_a_new_file_has_nothing_to_clear(tmp_path):
    from service.assistant.store import OLD_TEXT_CLEARED_KEY
    store = AssistantStore(tmp_path / "assistant.db")
    assert store.completion(OLD_TEXT_CLEARED_KEY) is not None
    store._db.close()


def test_someone_holding_the_file_only_puts_the_clearing_off(tmp_path):
    from service.assistant.store import OLD_TEXT_CLEARED_KEY
    import sqlite3
    path = tmp_path / "assistant.db"
    store = AssistantStore(path)
    store._db.execute("DELETE FROM assistant_completion WHERE key=?", (OLD_TEXT_CLEARED_KEY,))
    store._db.commit()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM assistant_completion").fetchone()
    store._clear_old_text()
    assert store.completion(OLD_TEXT_CLEARED_KEY) is None, "it tries again next start"
    assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    reader.rollback()
    reader.close()
    store._clear_old_text()
    assert store.completion(OLD_TEXT_CLEARED_KEY) is not None
    store._db.close()
