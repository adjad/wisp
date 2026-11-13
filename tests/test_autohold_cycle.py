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
def setting(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(config, "user_overlay_valid", lambda: True)
    monkeypatch.setattr(config, "models_config", lambda: dict(saved))
    monkeypatch.setattr(config, "_save_overlay", lambda patch: saved.update(patch))
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    # The hours guard has its own test (test_autohold.py); here every slot is inside the person's day.
    from service.proactive import autohold as _autohold
    monkeypatch.setattr(_autohold, "inside_hours", lambda slot, tz: True)
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


def cycle_once(store, now=NOW):
    return asyncio.run(cycle.run_cycle(
        now=now, tz=TZ, force=True, reply_fn=_quiet_model, abort=lambda: False, collect=lambda n, z: [],
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
