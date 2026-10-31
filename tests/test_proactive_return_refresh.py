"""Coming back to a stale Brief (service/proactive/return_refresh.py): a relaunch,
an unpark or the notch opening owes one catch-up, which runs only on a model
already resident, yields to a turn, and says "updating" meanwhile.

No live model, no native sources, no writes outside a scratch WISP_HOME.
"""
import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-return-refresh-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service import idle
from service import memory_saver as M
from service.proactive import cycle
from service.proactive import return_refresh as R
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0          # 2027-01-15 08:00 UTC
MIN = 60.0


class Recorder:
    def __init__(self, status="done", sleep=0.0):
        self.status, self.sleep, self.cycles, self.gists, self.announced = status, sleep, [], 0, 0
        self.held_during = []

    async def cycle(self, **kwargs):
        self.cycles.append(kwargs)
        self.held_during.append(M.held())
        if self.sleep:
            await asyncio.sleep(self.sleep)
        return {"ok": self.status == "done", "status": self.status}

    async def refresh_gists(self):
        self.gists += 1

    async def announce(self):
        self.announced += 1


async def _ready():
    return True


async def _not_ready():
    return False


@pytest.fixture
def store(tmp_path, monkeypatch):
    R._reset_for_tests()
    monkeypatch.setattr(M, "_holds", 0)
    s = ProactiveStore(tmp_path)
    s.save_state(as_of=NOW - 2 * 3600, timezone="UTC")
    yield s
    R._reset_for_tests()


def run(rec, store, now=NOW, ready=_ready):
    return asyncio.run(R.run(now=now, store=store, cycle=rec.cycle, gists=rec.refresh_gists,
                             ready=ready, announce=rec.announce))


# --- what counts as stale ----------------------------------------------------------

def test_a_brief_read_recently_today_owes_nothing(store):
    store.save_state(as_of=NOW - 10 * MIN)
    assert not R.arm("launch", now=NOW, store=store)
    assert not R.pending()


def test_a_brief_never_built_is_the_ordinary_cycles_job(tmp_path):
    R._reset_for_tests()
    assert not R.arm("opened", now=NOW, store=ProactiveStore(tmp_path))
    assert not R.pending()


def test_an_old_brief_is_owed_a_catch_up(store):
    assert R.arm("launch", now=NOW, store=store)
    assert R.pending() and R.status()["reason"] == "launch"


def test_a_new_day_is_stale_even_minutes_later(store):
    midnight = 1_800_057_600.0                     # 2027-01-16 00:00 UTC
    store.save_state(as_of=midnight - 10 * MIN)
    assert R.arm("opened", now=midnight + 5 * MIN, store=store)


def test_the_first_reason_is_kept_and_launch_is_checked_once(store):
    assert R.arm_on_launch(now=NOW, store=store)
    assert not R.arm_on_launch(now=NOW, store=store)
    assert R.arm("unparked", now=NOW, store=store)
    assert R.status()["reason"] == "launch"


# --- the catch-up -------------------------------------------------------------------

def test_it_regenerates_the_whole_brief_once_on_a_resident_model(store):
    R.arm("unparked", now=NOW, store=store)
    rec = Recorder()
    assert run(rec, store) == "ran"
    assert rec.cycles == [{"force": True}] and rec.gists == 1 and rec.announced == 1
    # It held the model between calls, and let go after.
    assert rec.held_during == [True] and not M.held()
    assert not R.pending()
    # It counts as the hour's idle refresh, so that doesn't redo it at once.
    assert store.state()["idle_refresh_at"] == NOW
    assert store.state()["idle_refresh_inputs"]
    assert run(rec, store, now=NOW + MIN) == "idle"
    assert len(rec.cycles) == 1


def test_it_never_loads_a_parked_model_it_waits(store):
    R.arm("launch", now=NOW, store=store)
    rec = Recorder()
    assert run(rec, store, ready=_not_ready) == "waiting"
    assert rec.cycles == [] and R.pending()
    # Backs off instead of asking the engine every tick.
    assert not R.due(NOW + R.WAIT_S - 1)
    assert run(rec, store, now=NOW + R.WAIT_S, ready=_ready) == "ran"


def test_a_turn_wins_and_the_catch_up_stays_owed(store):
    R.arm("opened", now=NOW, store=store)
    rec = Recorder(status="yielded")
    assert run(rec, store) == "yielded"
    assert R.pending() and rec.gists == 0 and rec.announced == 0
    assert "idle_refresh_at" not in store.state()
    assert not M.held()
    assert not R.due(NOW + R.YIELD_S - 1) and R.due(NOW + R.YIELD_S)


def test_a_run_that_takes_too_long_lets_the_model_go(store, monkeypatch):
    monkeypatch.setattr(R, "RUN_MAX_S", 0.05)
    R.arm("opened", now=NOW, store=store)
    rec = Recorder(sleep=5)
    assert run(rec, store) == "yielded"
    assert R.pending() and not M.held() and not R.status()["running"]


def test_a_batch_window_or_tight_memory_never_starts_one(monkeypatch):
    monkeypatch.setattr(M, "_state", M._State(started=0.0))
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    import service.config

    class Client:
        async def loaded_models(self):
            return ["chat-model"]

    monkeypatch.setattr(service.config, "role_to_model", lambda role: "chat-model")
    from service.assistant import brief
    monkeypatch.setattr(brief, "_c", lambda: Client())
    assert asyncio.run(R._model_ready())
    M._state.window_started = 1.0
    assert not asyncio.run(R._model_ready())
    M._state.window_started = None
    monkeypatch.setattr(M, "pressure_level", lambda: M.WARN)
    assert not asyncio.run(R._model_ready())
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    monkeypatch.setattr(service.config, "role_to_model", lambda role: "other-model")
    assert not asyncio.run(R._model_ready())


# --- "Updating…" ------------------------------------------------------------------

def test_updating_shows_after_the_notch_opens_on_a_stale_brief_and_while_running(store, monkeypatch):
    R.arm("launch", now=NOW, store=store)
    assert not R.updating(NOW)                     # nobody is looking: nothing to say
    R.arm("opened", now=NOW, store=store)
    assert R.updating(NOW + 10)
    # It never claims an update it couldn't start for long.
    assert not R.updating(NOW + R.UPDATING_FOR_S)

    seen = []

    async def cycle_(**kwargs):
        seen.append(R.updating(NOW + 10 * R.UPDATING_FOR_S))
        return {"status": "done"}

    async def nothing():
        return None

    asyncio.run(R.run(now=NOW, store=store, cycle=cycle_, gists=nothing, ready=_ready, announce=nothing))
    assert seen == [True]
    assert not R.updating(NOW + 1)


def test_the_snapshot_carries_updating(store):
    snap = cycle.snapshot(now=NOW, tz="UTC", store=store, schedule_fn=lambda n, z: {"calendar": [], "reminders": []},
                          own="me", linked=set())
    assert snap["updating"] is False
    R.arm("opened", now=time_now(), store=store)
    snap = cycle.snapshot(now=NOW, tz="UTC", store=store, schedule_fn=lambda n, z: {"calendar": [], "reminders": []},
                          own="me", linked=set())
    assert snap["updating"] is True


def time_now():
    import time
    return time.time()


# --- the triggers --------------------------------------------------------------------

def test_a_turn_that_reloads_a_parked_model_owes_a_catch_up(store, monkeypatch, tmp_path):
    from tests.test_memory_saver import FakeClient, MODEL
    monkeypatch.setattr(M, "SETTINGS_PATH", tmp_path / "memory_saver" / "settings.json")
    monkeypatch.setattr(M, "_state", M._State(started=0.0))
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    monkeypatch.setattr(idle, "_last_turn", 0.0)
    monkeypatch.setattr(idle, "_in_flight", {})
    monkeypatch.setattr(idle, "_last_used", {})
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: MODEL)
    armed = []
    monkeypatch.setattr(R, "arm", lambda reason, **kw: armed.append(reason) or True)
    c = FakeClient()
    asyncio.run(M.tick(c, 30 * MIN, pressure=M.NORMAL))            # parks
    assert M._state.parked and armed == []
    # An hour later a batch window loads it: background work, nobody is back.
    asyncio.run(M.tick(c, 90 * MIN, pressure=M.NORMAL))
    asyncio.run(M.tick(c, 91 * MIN, pressure=M.NORMAL))
    assert armed == []
    # The user comes back during the window: theirs now, and the Brief is owed.
    idle._last_turn = 92 * MIN
    asyncio.run(M.tick(c, 93 * MIN, pressure=M.NORMAL))
    assert armed == ["unparked"]
    # A plain reload by a turn after a park owes one too.
    asyncio.run(M.tick(c, 200 * MIN, pressure=M.NORMAL))            # parks again
    c.loaded.add(MODEL)
    idle._last_turn = 210 * MIN
    asyncio.run(M.tick(c, 211 * MIN, pressure=M.NORMAL))
    assert armed == ["unparked", "unparked"]


def test_the_scheduler_tick_runs_the_catch_up_first_and_never_during_a_turn(store, monkeypatch):
    from service.assistant.hub import hub
    monkeypatch.setattr(cycle, "_task", None)
    monkeypatch.setattr(cycle, "_running", False)
    monkeypatch.setattr(cycle, "_rerun", None)
    monkeypatch.setattr(cycle, "_next_tick_at", time_now() + 3600)    # the ordinary tick isn't due
    monkeypatch.setattr(cycle, "_remote_background_paused", lambda: False)
    monkeypatch.setattr(cycle.handled, "due", lambda: False)
    monkeypatch.setattr(cycle, "idle_refresh_due", lambda now, s: False)
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    from service.proactive import store as store_mod
    monkeypatch.setattr(store_mod, "store", lambda: store)
    ran = []

    async def fake_return_tick():
        ran.append("return")

    monkeypatch.setattr(cycle, "_return_tick", fake_return_tick)
    # Nothing owed and nothing due: no task.
    store.save_state(as_of=time_now())
    R._reset_for_tests()

    async def go():
        task = cycle.schedule_tick()
        if task is not None:
            await task
        return task

    assert asyncio.run(go()) is None and ran == []
    # Owed: the catch-up runs even though the ordinary tick isn't due.
    store.save_state(as_of=time_now() - 3 * 3600)
    R.arm("opened", store=store)
    monkeypatch.setattr(cycle, "_foreground", lambda: True)
    assert asyncio.run(go()) is None and ran == []                   # a turn: not now
    monkeypatch.setattr(cycle, "_foreground", lambda: False)
    assert asyncio.run(go()) is not None and ran == ["return"]


def test_the_endpoint_arms_from_the_app_only(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from service.proactive import store as store_mod
    from service.proactive.api import router
    monkeypatch.setattr(store_mod, "store", lambda: store)
    store.save_state(as_of=time_now() - 3 * 3600)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/assistant/proactive/return", json={},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert not R.pending()
    reply = client.post("/assistant/proactive/return", json={}).json()
    assert reply == {"ok": True, "updating": True} and R.pending()
