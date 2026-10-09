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

TITLE = "Mom: Meet me in the Quad at 6PM (6:00 PM)"
OWN = {"source": "reminders", "source_id": "native-A"}


def created_row(title=TITLE, due=NOW + 3600, created=OWN):
    """A ledger row for a reminder the runner made, with the identity recorded at creation."""
    led = wiring.get_ledger()
    led.claim("msg:m", title=title, due_ts=due, event_ts=due + 1800, now=NOW)
    detail = {"quote": "q", "result": {"ok": True, "created": created}} if created else {"quote": "q"}
    led.finish("msg:m", "created", detail, NOW)


def native(source_id="native-A", **kw):
    """A current commitment row as the store returns it."""
    return {"id": "c-" + source_id, "title": TITLE, "when_ts": NOW + 3600, "source": "reminders",
            "source_id": source_id, "status": "active", **kw}


@pytest.fixture
def store(monkeypatch):
    """The commitments store as a dict of rows: `upcoming` returns the heads, `get` the rows."""
    from service.tools import assistant_tools
    box = {"heads": [], "rows": {}, "retired": [], "error": None}

    def upcoming(*a, **k):
        return box["heads"]

    async def retire(c):
        box["retired"].append(c)
        return box["error"]
    monkeypatch.setattr(wiring.assistant_store, "upcoming", upcoming)
    monkeypatch.setattr(wiring.assistant_store, "get", lambda cid: box["rows"].get(cid))
    monkeypatch.setattr(assistant_tools, "_retire", retire)
    return box


async def test_undo_removes_exactly_the_object_it_created(store):
    created_row()
    store["heads"] = [native()]
    assert await wiring.undo("msg:m") == {"ok": True}
    assert store["retired"] == [{**native(), "duplicate_ids": []}]
    assert wiring.get_ledger().get("msg:m")["state"] == "undone"
    assert (await wiring.undo("msg:m"))["ok"] is False             # already undone: nothing to do
    assert len(store["retired"]) == 1


async def test_a_lookalike_made_after_the_original_is_gone_is_never_deleted(store):
    """Review ATT182-01: A is removed elsewhere, the user makes B with the same title and time."""
    created_row()
    store["heads"] = [native("native-B")]                              # same title, same time, other object
    got = await wiring.undo("msg:m")
    assert got["ok"] is False and store["retired"] == []
    assert wiring.get_ledger().get("msg:m")["state"] == "created"


async def test_an_unowned_sibling_collapsed_into_the_same_row_is_not_passed_to_the_deletion(store):
    created_row()
    own = native()
    head = {"id": "m1", "title": TITLE, "when_ts": NOW + 3600, "source": "manual", "source_id": "m1",
            "status": "active", "duplicate_ids": [own["id"]]}          # the user's own manual twin is the head
    store["heads"], store["rows"] = [head], {own["id"]: own, "m1": head}
    assert (await wiring.undo("msg:m"))["ok"] is True
    assert store["retired"] == [{**own, "duplicate_ids": []}]            # only the recorded object


async def test_an_owned_row_hiding_unowned_duplicates_does_not_take_them_along(store):
    created_row()
    store["heads"] = [native(duplicate_ids=["u1", "u2"])]
    store["rows"] = {"u1": native("native-U", id="u1"), "u2": {**native(), "id": "u2", "source": "manual"}}
    assert (await wiring.undo("msg:m"))["ok"] is True
    assert store["retired"][0]["duplicate_ids"] == [] and store["retired"][0]["source_id"] == "native-A"


async def test_a_group_that_does_not_contain_the_recorded_object_is_refused(store):
    created_row()
    other = native("native-B")
    head = {"id": "m1", "title": TITLE, "when_ts": NOW + 3600, "source": "manual", "source_id": "m1",
            "status": "active", "duplicate_ids": [other["id"]]}
    store["heads"], store["rows"] = [head], {other["id"]: other}
    assert (await wiring.undo("msg:m"))["ok"] is False and store["retired"] == []


async def test_a_reminder_with_no_recorded_identity_cannot_be_undone(store):
    created_row(created=None)
    store["heads"] = [native()]
    got = await wiring.undo("msg:m")
    assert got["ok"] is False and "identity" in got["error"] and store["retired"] == []
    for bad in ({"source": "calendar", "source_id": "native-A"}, {"source": "reminders"}, "native-A", []):
        wiring.get_ledger().finish("msg:m", "created", {"result": {"created": bad}}, NOW)
        assert (await wiring.undo("msg:m"))["ok"] is False and store["retired"] == [], bad


@pytest.mark.parametrize("change", [
    {"title": "Renamed"},                       # renamed since
    {"when_ts": NOW + 9000},                    # rescheduled since
    {"status": "dismissed"},                    # already dismissed
    {"source": "calendar"},                     # not a reminder at all
])
async def test_a_changed_or_gone_object_is_refused(store, change):
    created_row()
    store["heads"] = [native(**change)]
    assert (await wiring.undo("msg:m"))["ok"] is False and store["retired"] == []
    assert wiring.get_ledger().get("msg:m")["state"] == "created"


async def test_a_wisp_only_reminder_is_found_by_its_local_row_id(store):
    created_row(created={"source": "manual", "id": "local-1"})
    mine = {"id": "local-1", "title": TITLE, "when_ts": NOW + 3600, "source": "manual", "source_id": "local-1",
            "status": "active"}
    twin = {**mine, "id": "local-2", "source_id": "local-2"}             # identical, but not made by Wisp
    store["heads"] = [twin]
    assert (await wiring.undo("msg:m"))["ok"] is False and store["retired"] == []
    store["heads"] = [twin, mine]
    assert (await wiring.undo("msg:m"))["ok"] is True
    assert [r["id"] for r in store["retired"]] == ["local-1"]


@pytest.mark.parametrize("error", ["Reminders cancellation was not confirmed", "native timeout"])
async def test_a_failed_or_unconfirmed_delete_leaves_the_reminder_undoable(store, error):
    created_row()
    store["heads"], store["error"] = [native()], error
    got = await wiring.undo("msg:m")
    assert got == {"ok": False, "error": error}
    assert wiring.get_ledger().get("msg:m")["state"] == "created"
    store["error"] = None
    assert (await wiring.undo("msg:m"))["ok"] is True                    # a retry can still succeed


async def test_two_simultaneous_undo_requests_delete_once(store, monkeypatch):
    from service.tools import assistant_tools
    created_row()
    store["heads"] = [native()]
    gate, calls = asyncio.Event(), []

    async def slow(c):
        calls.append(c)
        await gate.wait()
    monkeypatch.setattr(assistant_tools, "_retire", slow)
    first = asyncio.create_task(wiring.undo("msg:m"))
    await asyncio.sleep(0)
    second = await wiring.undo("msg:m")
    gate.set()
    assert (await first) == {"ok": True} and second["ok"] is False and "in progress" in second["error"]
    assert len(calls) == 1


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
    rows = [c for c in wiring.assistant_store.upcoming(due - 600, days=1) if c["title"] == title]
    assert len(rows) == 1 and rows[0]["source"] == "manual"
    assert got == {"ok": True, "status": "succeeded", "where": "wisp_only",
                   "created": {"source": "manual", "id": rows[0]["id"]}}


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


# ------------------------------------------- the identity recorded at creation (review ATT182-01)

APPLE = "Reminder set: “t” — Mon at 5:30 PM in Apple Reminders and Wisp."


def fake_receipts(monkeypatch, rows_for):
    """Fake add_reminder (returning the Apple success prose) and the action store behind it."""
    import time as clock
    from types import SimpleNamespace
    from service.assistant.outbox import _reminder_base_action_id
    from service.tools import assistant_tools
    seen = {}

    async def add(title, when_iso, kind="reminder"):
        seen["when_iso"], seen["title"] = when_iso, title
        return APPLE

    def by_prefix(prefix):
        payload = {"title": seen["title"].strip(), "commitment_kind": "reminder",
                   "due_ts": datetime.fromisoformat(seen["when_iso"]).timestamp()}
        assert prefix == _reminder_base_action_id("create_reminder", payload)      # bound to THIS payload
        return rows_for(clock.time())
    monkeypatch.setattr(assistant_tools, "add_reminder", add)
    fake_store = SimpleNamespace(reminder_actions_by_prefix=by_prefix)
    monkeypatch.setattr(type(wiring.hub), "store", property(lambda self: fake_store))


def receipt(created_at, source_id="native-A", ok=True):
    return {"created_at": created_at, "result": {"ok": ok, "status": "succeeded", "source_id": source_id}}


async def test_the_native_receipt_written_during_the_call_is_the_recorded_identity(monkeypatch):
    fake_receipts(monkeypatch, lambda now: [receipt(now + 1)])
    got = await wiring.create_reminder(live.Plan("t", NOW + 3600, None, "today", "q"))
    assert got["created"] == {"source": "reminders", "source_id": "native-A"}


@pytest.mark.parametrize("rows", [
    lambda now: [receipt(now - 600)],                                  # an older reminder's receipt was reused
    lambda now: [],                                                    # no receipt at all
    lambda now: [receipt(now + 1, ok=False)],                          # not a verified success
    lambda now: [receipt(now + 1, "native-A"), receipt(now + 2, "native-B")],   # ambiguous
])
async def test_without_one_fresh_unambiguous_receipt_no_identity_is_recorded(monkeypatch, rows):
    fake_receipts(monkeypatch, rows)
    got = await wiring.create_reminder(live.Plan("t", NOW + 3600, None, "today", "q"))
    assert got["ok"] is True and got["created"] is None                # still created; just not undoable


async def test_a_wisp_only_identity_is_the_one_new_local_row_not_an_identical_older_one(monkeypatch):
    from service.tools import assistant_tools
    due = datetime.now().replace(microsecond=0).timestamp() + 7200
    older = wiring.assistant_store.add_manual("same", due)              # made by the user earlier
    made = []

    async def add(title, when_iso, kind="reminder"):
        made.append(wiring.assistant_store.add_manual(title, due))
        return "Reminder set: “same” — Mon in Wisp only; Apple Reminders was not changed (x)."
    monkeypatch.setattr(assistant_tools, "add_reminder", add)
    got = await wiring.create_reminder(live.Plan("same", due, None, "today", "q"))
    assert got["created"] == {"source": "manual", "id": made[0]["id"]} and made[0]["id"] != older["id"]


async def test_the_identity_is_persisted_in_the_ledger_through_a_live_pass(isolated, feed, monkeypatch):
    go_live(isolated)
    fake_receipts(monkeypatch, lambda now: [receipt(now + 1, "native-Z")])

    async def quiet(event):
        pass
    monkeypatch.setattr(wiring, "_publish", quiet)
    monkeypatch.setattr(wiring.assistant_store, "get", lambda cid: None)
    monkeypatch.setattr(wiring.assistant_store, "upcoming", lambda *a, **k: [])
    out = await wiring.run_tick(NOW)
    assert [o.state for o in out] == ["created"]
    row = wiring.get_ledger().get(out[0].source_id)
    assert row["detail"]["result"]["created"] == {"source": "reminders", "source_id": "native-Z"}


# ----------------------------- the API's enable stamp survives the first live pass (review ATT182-03)

def api_switch(client, monkeypatch, at):
    from types import SimpleNamespace
    monkeypatch.setattr(attention_api, "time", SimpleNamespace(time=lambda: at))
    assert client.put("/assistant/attention/settings", json={"mode": "live"}).status_code == 200


@pytest.mark.parametrize("before", ["shadow", "off"])
@pytest.mark.parametrize("restart", [False, True])
async def test_a_message_between_the_switch_and_the_first_live_pass_is_still_acted_on(
        client, isolated, feed, effects, monkeypatch, before, restart):
    led = wiring.get_ledger()
    led.set_meta("last_mode", before)                                  # the previous pass ran in `before`
    live.save_settings(live.settings_path(isolated), Settings(mode=before))
    api_switch(client, monkeypatch, NOW + 10)
    feed["value"] = {**FEED, "records": FEED["records"] + [
        rec("pre", "Mom", "Lunch today at 12pm", NOW + 5),             # arrived before the switch
        rec("post", "Mom", "Dinner today at 7pm", NOW + 20)]}          # after it, before the first pass
    if restart:
        wiring.reset()                                                 # app relaunched; the ledger file persists
    out = await wiring.run_tick(NOW + 300)                             # throttled / not ready until now
    titles = [o.title or "" for o in out]
    assert any("Dinner" in x for x in titles), out                     # the post-switch message is acted on
    assert not any("Lunch" in x for x in titles), out                  # the pre-switch one stays excluded
    assert float(wiring.get_ledger().meta("enabled_at")) == NOW + 10   # not moved forward to the pass


async def test_a_change_made_outside_the_endpoint_still_re_baselines(isolated, feed, effects):
    wiring.get_ledger().set_meta("last_mode", "shadow")
    go_live(isolated)                                                  # settings file edited by hand
    feed["value"] = {**FEED, "records": FEED["records"] + [rec("x", "Mom", "Lunch today at 12pm", NOW + 5)]}
    assert await wiring.run_tick(NOW + 300) == []                      # conservative fallback
    assert float(wiring.get_ledger().meta("enabled_at")) == NOW + 300
