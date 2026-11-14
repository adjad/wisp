"""The background cycle really runs the hold-time step (service/proactive/cycle.py, "Hold time").

autohold.py has its own unit tests; this drives cycle.run_cycle with injected fakes and a fake
deadlines.hold. Nothing reads a calendar, writes anywhere real, calls a model or spawns anything.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-autohold-cycle-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service import config
from service.proactive import acting, autohold, cycle, deadlines
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0
TZ = "UTC"
ME = "me@ucsc.edu"


@pytest.fixture
def setting(monkeypatch, tmp_path):
    saved: dict = {}
    # The gate's own file (acting._path), in this test's folder: never ~/.moe.
    if hasattr(acting, "_path"):
        monkeypatch.setattr(acting, "_path", lambda: tmp_path / "home" / "act_for_me.json")
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    # The hours guard has its own test (test_autohold.py); here every slot is inside the person's day.
    from service.proactive import autohold as _autohold
    monkeypatch.setattr(_autohold, "inside_hours", lambda slot, tz: True)
    # Never the real assistant store: the card's native id is looked up in this fake.
    monkeypatch.setattr(_autohold, "resolve_native", lambda ref: {"cal:1": "evt-1"}.get(ref))
    return saved


@pytest.fixture
def store(tmp_path):
    return ProactiveStore(tmp_path / "proactive")


@pytest.fixture
def holds(monkeypatch):
    calls: list[str] = []

    def fake(card, *, now, store=None):
        calls.append(card["id"])
        return {"ok": True, "status": "succeeded", "task_id": "task-1"}

    monkeypatch.setattr(deadlines, "hold", fake)
    return calls


def block_card():
    start = NOW + 3600
    return {"id": "card:deadline1", "title": "Worksheet 1 for STAT 5", "why": "Worksheet 1 for STAT 5 is due tomorrow.",
            "account": "", "sources": [{"kind": "calendar", "ref": "cal:1", "who": "", "quote": "Worksheet 1"}],
            "action": {"method": "block_time", "button": "Block 2–3 PM?", "slot": {"start": start, "end": start + 3600},
                       "envelope": {"kind": "block_time", "title": "Worksheet 1 for STAT 5", "start": start,
                                    "duration_min": 60, "day": "2027-01-15", "timezone": "UTC"}}}


async def _quiet_model(system, material, abort, tokens):
    return ""


def cycle_once(store, now=NOW, light=False, abort=lambda: False):
    return asyncio.run(cycle.run_cycle(
        now=now, tz=TZ, force=True, reply_fn=_quiet_model, abort=abort, collect=lambda n, z: [], light=light,
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store, resolve=lambda i: None,
        linked=set(), own=ME, allowed=set(), freeze=lambda ref: None,
        deadline_fn=lambda n, z, skip, own, linked: [block_card()], autoadd_fn=None))


def methods(store):
    return [c["action"]["method"] for c in store.state()["cards"]]


def test_the_cycle_holds_the_slot_and_swaps_the_tap_card_for_a_wisp_did_this_card(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    assert cycle_once(store)["status"] == "done"
    assert holds == ["card:deadline1"]
    cards = store.state()["cards"]
    assert "block_time" not in methods(store)
    done = [c for c in cards if c["action"]["method"] == "undo_add"]
    assert len(done) == 1 and done[0]["meta"] == "DAY PLAN"
    assert list(store.auto_added().values())[0]["status"] == "added"


def test_a_held_hours_block_time_card_does_not_come_back_in_the_next_cycle(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    assert cycle_once(store)["status"] == "done"
    assert cycle_once(store, now=NOW + 600)["status"] == "done"            # the same deadline, found again
    assert holds == ["card:deadline1"] and "block_time" not in methods(store)
    assert methods(store).count("undo_add") == 1


def test_without_the_held_set_that_card_would_come_back(setting, store, holds, monkeypatch):
    # The control for the test above: it is the held set, and nothing else, that keeps the card away.
    from service.proactive import autoadd
    acting.set_enabled(True, now=NOW - 60)
    cycle_once(store)
    monkeypatch.setattr(autoadd, "held_items", lambda store: set())
    cycle_once(store, now=NOW + 600)
    assert "block_time" in methods(store)


def test_a_write_left_in_flight_is_settled_before_anything_holds(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    acting.record(store, "f-9", kind="follow_up", status="dispatching", now=NOW - 900, title="Reply to Sam", item_id="m9")
    assert cycle_once(store)["status"] == "done"
    assert store.auto_added()["f-9"]["status"] == "unknown"


def test_with_the_gate_off_the_tap_card_stands_and_nothing_is_recorded(setting, store, holds):
    assert cycle_once(store)["status"] == "done"
    assert holds == [] and store.auto_added() == {}
    assert methods(store) == ["block_time"]


def test_an_undone_hold_is_not_offered_as_a_tap_card_again(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    cycle_once(store)
    [fp] = store.auto_added()
    acting.record(store, fp, kind="hold", status="undone", now=NOW + 10, title="Worksheet 1 for STAT 5")
    holds.clear()
    cycle_once(store, now=NOW + 600)
    assert holds == []
    assert "block_time" not in methods(store)
    assert store.auto_added()[autohold.fingerprint(block_card())]["status"] == "undone"


# --- follow-up reminders are reached through the same background pass ---------------------------

def _cycle_with_default_autoadd(store, abort=lambda: False):
    """run_cycle with the production auto-add step (cycle._default_autoadd), which is what a real
    cycle uses when no schedule is injected; auto-add's and follow-ups' own steps are faked."""
    import tests.test_proactive_auto_add as T
    item = T.text("Maya", T.RECITAL)                     # one real-shaped message, so the cycle has work
    return asyncio.run(cycle.run_cycle(
        now=T.NOW, tz=T.TZ, force=True, reply_fn=_quiet_model, abort=abort, collect=lambda n, z: [item],
        schedule_fn=lambda n, z: {"calendar": [], "reminders": []}, store=store,
        resolve=lambda i: None, linked=set(), own=T.ME, allowed=set(), freeze=lambda ref: None,
        deadline_fn=None, autoadd_fn=cycle._default_autoadd))


def test_the_cycle_runs_follow_ups_after_auto_add_with_the_same_inputs(setting, store, monkeypatch):
    from service.proactive import autoadd, followups
    order, seen = [], {}

    async def fake_autoadd(survivors, sched, *, now, tz, store, abort, **_):
        order.append("autoadd")
        return {"tap": [], "added": [], "items": set()}

    async def fake_followups(survivors, sched, *, now, tz, store, abort, **_):
        order.append("followups")
        seen.update(survivors=survivors, sched=sched, now=now, tz=tz, store=store, abort=abort)
    monkeypatch.setattr(autoadd, "step", fake_autoadd)
    monkeypatch.setattr(followups, "step", fake_followups)
    _cycle_with_default_autoadd(store)
    assert order == ["autoadd", "followups"]
    import tests.test_proactive_auto_add as T
    assert seen["now"] == T.NOW and seen["tz"] == T.TZ and seen["store"] is store and callable(seen["abort"])
    assert seen["sched"] == {"calendar": [], "reminders": []}
    assert [s["item"]["who"] for s in seen["survivors"]] == ["Maya"]


def test_a_follow_up_step_that_raises_never_breaks_the_cycle_or_auto_adds_result(setting, store, monkeypatch):
    from service.proactive import autoadd, followups
    calls = []

    async def fake_autoadd(survivors, sched, **_):
        calls.append("autoadd")
        return {"tap": [], "added": ["fp"], "items": set()}

    async def broken(*_a, **_k):
        calls.append("followups")
        raise RuntimeError("bad input")
    monkeypatch.setattr(autoadd, "step", fake_autoadd)
    monkeypatch.setattr(followups, "step", broken)
    monkeypatch.setattr(cycle.traceback, "print_exc", lambda *a, **k: None)
    result = asyncio.run(cycle._default_autoadd([], {}, now=NOW, tz=TZ, store=store, abort=lambda: False))
    assert result == {"tap": [], "added": ["fp"], "items": set()} and calls == ["autoadd", "followups"]
    _cycle_with_default_autoadd(store)                                   # and the whole cycle still completes
    assert calls == ["autoadd", "followups", "autoadd", "followups"]


def test_a_cycle_that_a_turn_interrupts_runs_neither(setting, store, monkeypatch):
    from service.proactive import autoadd, followups
    calls = []

    async def record(*_a, **_k):
        calls.append(1)
        return {"tap": [], "added": [], "items": set()}
    monkeypatch.setattr(autoadd, "step", record)
    monkeypatch.setattr(followups, "step", record)
    _cycle_with_default_autoadd(store, abort=lambda: True)
    assert calls == []


def test_a_light_cycle_holds_the_hour_and_shows_its_card_at_once(setting, store, holds):
    """Holding uses no model, and light cycles are the ones that run every few minutes: a person
    who turned the switch on gets the held hour without waiting for the next build."""
    acting.set_enabled(True, now=NOW - 60)
    assert cycle_once(store, light=True)["status"] == "done"
    assert holds == ["card:deadline1"]
    assert [r["status"] for r in store.auto_added().values()] == ["added"]
    cards = store.state()["cards"]
    assert methods(store) == ["undo_add"] and cards[0]["meta"] == "DAY PLAN"        # its card, with Undo, now
    # The next cycles, light or full, hold nothing again and keep the one card.
    cycle_once(store, now=NOW + 300, light=True)
    cycle_once(store, now=NOW + 600)
    assert holds == ["card:deadline1"] and methods(store) == ["undo_add"]


def test_a_light_cycle_with_the_switch_off_leaves_the_tap_card(setting, store, holds):
    assert cycle_once(store, light=True)["status"] == "done"
    assert holds == [] and store.auto_added() == {} and methods(store) == ["block_time"]


def test_a_light_cycle_never_holds_while_a_turn_is_running(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    assert cycle_once(store, light=True, abort=lambda: True)["status"] in ("done", "yielded")
    assert holds == [] and store.auto_added() == {}


def test_a_light_cycle_keeps_tap_to_add_cards_and_other_cards_where_they_were(setting, store, holds):
    """Only the "Wisp did this" cards are refreshed between builds: a tap-to-add card the last
    build wrote must not vanish until the next one."""
    acting.set_enabled(True, now=NOW - 60)
    tap = {"id": "card:tap1", "meta": "TEXTS", "title": "Dinner with Maya", "body": "", "account": "",
           "sources": [{"kind": "messages", "ref": "messages:1", "who": "Maya", "quote": ""}], "auto_add_tap": True,
           "action": {"method": "calendar_event", "button": "Add to calendar?", "draft": "", "recipient": "", "event": None}}
    other = {"id": "card:reply1", "meta": "MAIL", "title": "Reply to Priya", "body": "", "account": "",
             "sources": [{"kind": "mail", "ref": "mail:1", "who": "Priya", "quote": ""}],
             "action": {"method": "email_reply", "button": "Send?", "draft": "ok", "recipient": "p@x.test", "event": None}}
    stale_done = {"id": "card:old-done", "meta": "DAY PLAN", "title": "Held earlier", "body": "", "account": "",
                  "sources": [], "auto_added": True, "action": {"method": "undo_add", "record": "gone"}}
    block = block_card()                                    # the tap card for the hour Wisp is about to hold
    assert cycle_once(store, light=True)["status"] == "done" and holds == ["card:deadline1"]
    by_account = {ME: [stale_done, other, block, tap]}
    cycle._merge_done(by_account, [ME], ME, store, NOW)
    ids = [c["id"] for c in by_account[ME]]
    assert ids[1:] == ["card:reply1", "card:tap1"]          # the others, in the order they had
    assert by_account[ME][0]["meta"] == "DAY PLAN" and by_account[ME][0]["action"]["method"] == "undo_add"
    assert "card:old-done" not in ids and block["id"] not in ids      # the stale done card and the held hour's tap card go
    # Nothing Wisp did and no earlier done card: the list is not touched at all.
    fresh = {"other": [other, tap]}
    cycle._merge_done(fresh, ["other"], ME, store, NOW)
    assert fresh == {"other": [other, tap]}


# --- Analyze now is never dropped -------------------------------------------------------------------

@pytest.fixture
def fake_cycles(monkeypatch):
    """cycle.run_cycle replaced by a recorder whose answers the test scripts."""
    calls, answers = [], []

    async def run_cycle(**kw):
        calls.append({k: kw.get(k) for k in ("light", "force", "patient", "account")})
        return answers.pop(0) if answers else {"ok": True, "status": "done"}
    monkeypatch.setattr(cycle, "run_cycle", run_cycle)
    monkeypatch.setattr(cycle, "_task", None)
    monkeypatch.setattr(cycle, "_rerun", None)
    monkeypatch.setattr(cycle, "_running", False)
    monkeypatch.setattr(cycle, "ANALYZE_BUSY_POLL_S", 0.0)
    lines = []
    monkeypatch.setattr(cycle, "_log", lines.append)
    return calls, answers, lines


def test_analyze_that_meets_a_running_cycle_waits_and_still_runs_its_full_cycle(fake_cycles):
    """A light tick that started in the same moment makes run_cycle answer "busy". That used to end
    Analyze now without a word; it must wait and run the full cycle that was asked for."""
    calls, answers, lines = fake_cycles
    answers += [{"ok": False, "status": "busy"}, {"ok": False, "status": "busy"}, {"ok": True, "status": "done"}]

    async def press():
        reply = cycle.analyze()
        assert reply["queued"] is False
        await cycle._task
    asyncio.run(press())
    assert calls == [{"light": None, "force": True, "patient": True, "account": None}] * 3
    assert lines[0] == "analyze: starting" and "analyze: waiting for a running cycle to end" in lines


def test_analyze_pressed_during_a_light_tick_ends_in_one_full_cycle(fake_cycles, monkeypatch):
    calls, answers, lines = fake_cycles

    async def go():
        # A light tick is mid-run when the button is pressed: the request is queued, and said so.
        monkeypatch.setattr(cycle, "_running", True)
        reply = cycle.analyze("me@x.test")
        assert reply["queued"] is True and cycle._rerun == {"account": "me@x.test"}
        assert lines == ["analyze: queued behind a running cycle"] and calls == []
        # The light tick finishes: the full cycle that was asked for runs right after it, once.
        monkeypatch.setattr(cycle, "_running", False)
        await cycle._light_tick()
    asyncio.run(go())
    assert calls == [{"light": True, "force": None, "patient": None, "account": None},
                     {"light": None, "force": True, "patient": True, "account": "me@x.test"}]
    assert cycle._rerun is None and "analyze: starting (it was queued)" in lines


def test_a_hold_made_between_builds_joins_the_grouped_card(setting, store, holds):
    from service.proactive import autoadd
    acting.set_enabled(True, now=NOW - 60)
    # Something Wisp did earlier today already has its card.
    record = {"kind": "calendar", "title": "Dinner with Maya", "quote": "", "item_id": "m1", "source": "messages",
              "who": "Maya Chen", "account": "", "received_ts": NOW - 400, "timezone": TZ, "fingerprint": "fp-1",
              "item_fingerprint": "ifp-1", "at": NOW - 300, "status": "added", "attempts": 1, "source_id": "src-1",
              "start_ts": NOW + 86400, "duration_min": 60, "day": "2027-01-16"}
    store.auto_add_put("fp-1", record)
    assert cycle_once(store, light=True)["status"] == "done"
    assert sorted(methods(store)) == ["undo_add", "undo_add"]                # stored one per action
    shown = autoadd.group_done(store.state()["cards"], store, NOW + 1, TZ)
    assert len(shown) == 1 and shown[0]["title"] == "Wisp did 2 things today"
    assert {m["meta"] for m in shown[0]["members"]} == {"DAY PLAN", "ADDED · CALENDAR"}


def test_a_light_cycle_still_hides_an_undone_deadline(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    acting.record(store, autohold.fingerprint(block_card()), kind="hold", status="undone", now=NOW - 10,
                  title="Worksheet 1 for STAT 5")
    cycle_once(store, light=True)
    assert holds == [] and "block_time" not in methods(store)


def test_a_cycle_that_yielded_to_a_turn_holds_nothing(setting, store, holds):
    acting.set_enabled(True, now=NOW - 60)
    calls = {"n": 0}

    def abort():                       # a turn is running when the judge starts, then it ends
        calls["n"] += 1
        return calls["n"] == 1
    assert cycle_once(store, abort=abort)["status"] == "yielded"
    assert holds == [] and store.auto_added() == {} and methods(store) == ["block_time"]
