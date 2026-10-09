"""The live wiring and endpoints around the attention runner, with every source and effect faked.

`conftest.py` points WISP_HOME at a temp dir, and these tests also redirect MOE_DIR, so the
real ~/.moe, Reminders and the hub are never touched.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

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
