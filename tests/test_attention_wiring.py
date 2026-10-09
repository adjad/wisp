"""The live wiring and endpoints around the attention runner, with every source and effect faked.

The repository-root `conftest.py` points WISP_HOME at a temp dir before anything is imported,
and these tests also redirect MOE_DIR, so the real ~/.moe, Reminders and the hub are never
touched. The guard below makes that assumption fail loudly instead of silently.
"""
import asyncio
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

assert os.environ.get("WISP_HOME") and \
    Path(os.environ["WISP_HOME"]).resolve() != (Path.home() / ".moe").resolve(), \
    "attention wiring tests must run with an isolated WISP_HOME"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service.assistant import attention_api, attention_runner as wiring
from service.attention import live
from service.attention.live import Settings

TZ = "America/Los_Angeles"
NOW = datetime(2026, 9, 21, 9, 1, tzinfo=ZoneInfo(TZ)).timestamp()
ARRIVED = NOW - 60


def rec(guid, conv, text, ts, direction="incoming"):
    return {"guid": guid, "conversation": conv, "direction": direction, "timestamp": ts, "text": text,
            "sender": "me" if direction == "outgoing" else conv}


FEED = {"state": "ready", "coverage": {"status": "complete"},
        "records": [rec("h", "Mom", "love you", ARRIVED - 3600, "outgoing"),
                    rec("m", "Mom", "Meet me in the Quad at 6PM", ARRIVED)]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(wiring, "MOE_DIR", tmp_path)
    monkeypatch.setattr(attention_api, "MOE_DIR", tmp_path)
    monkeypatch.setattr(wiring, "local_timezone", lambda: TZ)
    monkeypatch.setattr(attention_api, "local_timezone", lambda: TZ)
    monkeypatch.setattr(wiring, "_commitments", lambda now: [])
    wiring.reset()
    wiring.get_ledger().set_meta("enabled_at", repr(NOW - 86400))
    wiring.get_ledger().set_meta("last_mode", "live")        # already live: no re-baseline
    yield tmp_path
    wiring.reset()


@pytest.fixture
def feed(monkeypatch):
    from service.tools import imessage_tools
    box = {"value": FEED}
    monkeypatch.setattr(imessage_tools, "structured_messages_snapshot", lambda: box["value"])
    return box


@pytest.fixture
def effects(monkeypatch):
    calls = {"created": [], "published": []}

    async def create(plan):
        calls["created"].append(plan)
        return {"ok": True, "status": "succeeded"}

    async def publish(event):
        calls["published"].append(event)

    monkeypatch.setattr(wiring, "create_reminder", create)
    monkeypatch.setattr(wiring, "_publish", publish)
    return calls


def go_live(tmp_path, **kw):
    live.save_settings(live.settings_path(tmp_path), Settings(mode="live", **kw))


# ------------------------------------------------------------------- run_tick

async def test_the_default_is_shadow_nothing_is_created(isolated, feed, effects):
    out = await wiring.run_tick(NOW)
    assert [o.state for o in out] == ["shadow"] and effects["created"] == []


async def test_off_and_unreadable_settings_do_nothing(isolated, feed, effects):
    live.save_settings(live.settings_path(isolated), Settings(mode="off"))
    assert await wiring.run_tick(NOW) == []
    live.settings_path(isolated).write_text("garbage")
    assert await wiring.run_tick(NOW + 1000) == [] and effects["created"] == []


async def test_live_creates_and_alerts_and_an_unchanged_feed_is_skipped(isolated, feed, effects):
    go_live(isolated)
    out = await wiring.run_tick(NOW)
    assert [o.state for o in out] == ["created"]
    assert len(effects["created"]) == 1 and effects["published"][0]["type"] == "attention_added"
    assert await wiring.run_tick(NOW + 30) == []                  # throttled: ticks arrive every 30s
    assert await wiring.run_tick(NOW + 200) == []                 # same fingerprint
    assert len(effects["created"]) == 1


async def test_a_feed_that_is_not_ready_is_ignored(isolated, feed, effects):
    go_live(isolated)
    for i, state in enumerate(("syncing", "unavailable")):
        feed["value"] = {**FEED, "state": state}
        assert await wiring.run_tick(NOW + 200 * (i + 1)) == []
    feed["value"] = {**FEED, "coverage": {"status": "unavailable"}}
    assert await wiring.run_tick(NOW + 800) == [] and effects["created"] == []


# ------------------------------------------------------------- create_reminder

@pytest.mark.parametrize("text,expect", [
    ("Reminder set: “x” — Mon at 5:30 PM in Apple Reminders and Wisp.",
     {"ok": True, "status": "succeeded", "where": "apple_and_wisp"}),
    ("Reminder set: “x” — Mon at 5:30 PM in Wisp only; Apple Reminders was not changed (no access).",
     {"ok": True, "status": "succeeded", "where": "wisp_only"}),
])
async def test_create_reminder_recognises_only_the_success_prefix(monkeypatch, text, expect):
    from service.tools import assistant_tools

    async def fake(title, when_iso, kind="reminder"):
        return text
    monkeypatch.setattr(assistant_tools, "add_reminder", fake)
    got = await wiring.create_reminder(live.Plan("t", NOW + 3600, None, "today", "q"))
    assert {k: got[k] for k in expect} == expect


@pytest.mark.parametrize("text,status", [
    ("(error: Reminders creation was not verified; nothing was confirmed.)", "unknown"),
    ("(error: the Wisp app is not connected and an earlier Apple Reminders write is unconfirmed)", "unknown"),
    ("(bad when_iso 'x')", "failed"),
    ("(2026-09-21T09:00 is in the past — not added)", "failed"),
])
async def test_everything_else_is_a_failure_and_unconfirmed_is_unknown(monkeypatch, text, status):
    from service.tools import assistant_tools

    async def fake(title, when_iso, kind="reminder"):
        return text
    monkeypatch.setattr(assistant_tools, "add_reminder", fake)
    got = await wiring.create_reminder(live.Plan("t", NOW + 3600, None, "today", "q"))
    assert got["ok"] is False and got["status"] == status


# ------------------------------------------------------------------------ undo

def created_row(title="Mom: Meet me in the Quad at 6PM (6:00 PM)", due=NOW + 3600):
    led = wiring.get_ledger()
    led.claim("msg:m", title=title, due_ts=due, event_ts=due + 1800, now=NOW)
    led.finish("msg:m", "created", {"quote": "q"}, NOW)
    return {"title": title, "when_ts": due, "source": "reminders", "id": "c1", "source_id": "x"}


async def test_undo_removes_exactly_the_reminder_it_created(monkeypatch):
    from service.tools import assistant_tools
    row = created_row()
    retired = []

    async def fake_retire(c):
        retired.append(c)
    monkeypatch.setattr(wiring.assistant_store, "upcoming", lambda *a, **k: [row])
    monkeypatch.setattr(assistant_tools, "_retire", fake_retire)
    assert await wiring.undo("msg:m") == {"ok": True}
    assert retired == [row] and wiring.get_ledger().get("msg:m")["state"] == "undone"
    assert (await wiring.undo("msg:m"))["ok"] is False             # already undone: nothing to do


@pytest.mark.parametrize("candidates", [
    [],                                                                         # gone already
    [{"title": "Something else", "when_ts": NOW + 3600, "source": "reminders"}],  # different reminder
    [{"title": "Mom: Meet me in the Quad at 6PM (6:00 PM)", "when_ts": NOW + 9000, "source": "reminders"}],
    [{"title": "Mom: Meet me in the Quad at 6PM (6:00 PM)", "when_ts": NOW + 3600, "source": "calendar"}],
])
async def test_undo_never_touches_anything_that_is_not_its_own(monkeypatch, candidates):
    from service.tools import assistant_tools
    created_row()
    monkeypatch.setattr(wiring.assistant_store, "upcoming", lambda *a, **k: candidates)

    async def boom(c):
        raise AssertionError("must not delete")
    monkeypatch.setattr(assistant_tools, "_retire", boom)
    assert (await wiring.undo("msg:m"))["ok"] is False
    assert wiring.get_ledger().get("msg:m")["state"] == "created"


async def test_undo_refuses_when_two_reminders_match(monkeypatch):
    row = created_row()
    monkeypatch.setattr(wiring.assistant_store, "upcoming", lambda *a, **k: [row, dict(row, id="c2")])
    assert "More than one" in (await wiring.undo("msg:m"))["error"]


async def test_undo_only_applies_to_created_reminders():
    wiring.get_ledger().claim("msg:s", now=NOW)
    wiring.get_ledger().finish("msg:s", "shadow", {}, NOW)
    assert (await wiring.undo("msg:s"))["ok"] is False
    assert (await wiring.undo("msg:never"))["ok"] is False


# ------------------------------------------------------------------- endpoints

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(attention_api.router)
    return TestClient(app)


def test_status_reports_mode_counts_and_recent_actions(client):
    led = wiring.get_ledger()
    led.claim("msg:a", title="T", due_ts=NOW, now=NOW)
    led.finish("msg:a", "created", {"when": "today at 6:00 PM", "quote": "q"}, NOW)
    body = client.get("/assistant/attention").json()
    assert body["settings"]["mode"] == "shadow" and body["modes"] == ["off", "shadow", "live"]
    assert body["recent"][0]["source_id"] == "msg:a" and body["recent"][0]["when"] == "today at 6:00 PM"


def test_settings_can_be_changed_and_bad_values_are_refused(client, isolated):
    got = client.put("/assistant/attention/settings", json={"mode": "live", "daily_cap": 2})
    assert got.status_code == 200 and got.json()["mode"] == "live" and got.json()["daily_cap"] == 2
    assert live.load_settings(live.settings_path(isolated)).mode == "live"
    for bad in ({"mode": "loud"}, {"daily_cap": -1}, {"daily_cap": "3"}, {"unknown": 1}, {"quiet_start": 24}):
        assert client.put("/assistant/attention/settings", json=bad).status_code == 422
    assert live.load_settings(live.settings_path(isolated)).daily_cap == 2     # unchanged


def test_undo_endpoint_404s_when_there_is_nothing_to_undo(client):
    assert client.post("/assistant/attention/undo", json={"source_id": "msg:nope"}).status_code == 404
    assert client.post("/assistant/attention/undo", json={}).status_code == 422


async def test_ticks_closer_together_than_the_minimum_interval_are_skipped(isolated, feed, effects):
    go_live(isolated)
    assert await wiring.run_tick(NOW) != []
    feed["value"] = {**FEED, "records": FEED["records"] + [rec("n", "Mom", "Lunch today at 12pm", NOW)]}
    assert await wiring.run_tick(NOW + wiring.MIN_INTERVAL_S - 1) == []
    assert [o.state for o in await wiring.run_tick(NOW + wiring.MIN_INTERVAL_S + 1)] == ["created"]


# ------------------------------------------------ repairs from the independent audit

async def test_the_alert_event_is_transient_so_it_never_piles_up_in_the_database(monkeypatch):
    seen = {}

    async def fake_publish(event, **kw):
        seen.update(kw, event=event)
    monkeypatch.setattr(wiring.hub, "publish", fake_publish)
    await wiring._publish({"type": "attention_added", "source_id": "msg:x", "quote": "q", "sender": "s"})
    assert seen["durable"] is False and seen["event"]["type"] == "attention_added"


async def test_schedule_tick_is_single_flight_and_never_raises(monkeypatch):
    started, release = [], asyncio.Event()

    async def slow_tick(now=None):
        started.append(1)
        await release.wait()
        raise RuntimeError("boom after the wait")
    monkeypatch.setattr(wiring, "run_tick", slow_tick)
    wiring.schedule_tick()
    wiring.schedule_tick()                                   # a pass is running: skipped
    await asyncio.sleep(0)
    assert started == [1]
    release.set()
    await asyncio.sleep(0.01)                                # the failure stays inside the task
    assert wiring._task.done() and wiring._task.exception() is None
    release.clear()
    wiring.schedule_tick()                                   # free again
    await asyncio.sleep(0)
    assert len(started) == 2
    release.set()
    await asyncio.sleep(0.01)


def test_the_scheduler_does_not_wait_for_the_pass():
    import inspect
    from service.assistant import scheduler
    source = inspect.getsource(scheduler.run)
    assert "schedule_tick()" in source and "await run_tick" not in source


def test_all_day_events_earlier_today_are_visible_to_the_on_file_check(monkeypatch):
    seen = {}
    monkeypatch.undo()                                       # use the real _commitments for this one
    monkeypatch.setattr(wiring, "local_timezone", lambda: TZ)
    monkeypatch.setattr(wiring.assistant_store, "upcoming",
                        lambda now, days=7: seen.update(now=now, days=days) or [])
    wiring._commitments(NOW)
    midnight = datetime(2026, 9, 21, 0, 0, tzinfo=ZoneInfo(TZ)).timestamp()
    assert seen["now"] == midnight and seen["days"] >= 5


async def test_the_real_add_reminder_prose_is_classified_as_success_when_wisp_only(isolated, monkeypatch):
    """No fake of add_reminder: run the real tool with the app 'not connected' so it takes its
    Wisp-only path against the (temp) store, and check the wiring reads its real wording."""
    monkeypatch.setattr(wiring.hub, "_subs", set())
    due = datetime.now().replace(microsecond=0).timestamp() + 7200
    title = "Mom: Meet me in the Quad (6:00 PM)"
    got = await wiring.create_reminder(live.Plan(title, due, None, "today", "q"))
    assert got == {"ok": True, "status": "succeeded", "where": "wisp_only"}
    rows = [c for c in wiring.assistant_store.upcoming(due - 600, days=1) if c["title"] == title]
    assert len(rows) == 1 and rows[0]["source"] == "manual"


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Kolkata", "America/Los_Angeles"])
async def test_the_reminder_time_is_exact_whatever_timezone_the_process_runs_in(isolated, monkeypatch, tz):
    """The planner works in the Mac's zone; add_reminder parses in the process's zone. An explicit
    offset makes them agree, so a differing TZ cannot move or refuse the reminder."""
    import time
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        monkeypatch.setattr(wiring.hub, "_subs", set())
        due = time.time() + 7200
        title = f"Mom: Meet me in the Quad ({tz})"
        got = await wiring.create_reminder(live.Plan(title, due, None, "today", "q"))
        assert got["ok"] is True, got
        rows = [c for c in wiring.assistant_store.upcoming(due - 3600, days=1) if c["title"] == title]
        assert len(rows) == 1 and abs(rows[0]["when_ts"] - due) < 60
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_a_limited_pass_is_followed_up_at_the_next_interval_not_the_next_quarter_hour(isolated, feed, effects):
    go_live(isolated)
    feed["value"] = {**FEED, "records": FEED["records"] + [rec("n", "Mom", "Lunch today at 12pm", NOW),
                                                           rec("o", "Mom", "Dinner today at 7pm", NOW + 1)]}
    first = await wiring.run_tick(NOW + 2)
    assert [o.state for o in first] == ["created"] and first.limited is True
    second = await wiring.run_tick(NOW + 2 + wiring.MIN_INTERVAL_S + 1)      # same feed, next interval
    assert [o.state for o in second] == ["created"]


async def test_resuming_live_after_a_corrupt_settings_file_re_baselines(isolated, feed, effects):
    go_live(isolated)
    assert [o.state for o in await wiring.run_tick(NOW)] == ["created"]
    live.settings_path(isolated).write_text("garbage")                      # reads as off
    assert await wiring.run_tick(NOW + 200) == []
    wiring.get_ledger().claim("msg:marker", now=NOW)                       # (keeps the ledger non-empty)
    feed["value"] = {**FEED, "records": FEED["records"] + [rec("w", "Mom", "Lunch today at 12pm", NOW + 250)]}
    go_live(isolated)
    assert await wiring.run_tick(NOW + 400) == []                          # arrived during the pause


def test_switching_to_live_through_the_api_re_baselines_at_that_instant(client, isolated):
    led = wiring.get_ledger()
    led.set_meta("enabled_at", "1.0")
    assert client.put("/assistant/attention/settings", json={"daily_cap": 2}).status_code == 200
    assert led.meta("enabled_at") == "1.0"                                  # not entering live: untouched
    assert client.put("/assistant/attention/settings", json={"mode": "live"}).status_code == 200
    assert float(led.meta("enabled_at")) > 1e9                              # stamped now
    stamped = led.meta("enabled_at")
    assert client.put("/assistant/attention/settings", json={"daily_cap": 3}).status_code == 200
    assert led.meta("enabled_at") == stamped                                # already live: untouched
