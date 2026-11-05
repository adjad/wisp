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
