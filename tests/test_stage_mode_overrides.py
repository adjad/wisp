"""Demo mode (service/stage_mode.py) over the power and memory savers: while it is on,
no GPU cap, no park, no batch window, no idle unload; the saved switches are never
written, and turning it off puts everything back. Fakes only; no engine is contacted."""
import asyncio

import pytest

from service import idle, idle_unloader, stage_mode
from service import memory_saver as M
from service.inference import power_tier as P

MODEL = "chat-model"
OTHER = "embedder"
MIN = 60.0


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    # Demo mode off before and after every test, whatever the test did.
    monkeypatch.setattr(stage_mode, "_since", None)
    monkeypatch.setattr(stage_mode, "_listeners", [])
    # Power: as tests/test_power_tier.py sets it up.
    for name in P._ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(P, "SETTINGS_PATH", tmp_path / "power" / "settings.json")
    for name, value in (("_away", None), ("_battery", None), ("_watched_calls", 0),
                        ("_uncapped_watched", 0), ("_capped", {}), ("_lift_ok", None),
                        ("_lift_retry_at", 0.0), ("_watch_retry_at", 0.0), ("_lifted", False),
                        ("_quiesced", False), ("_watch_task", None), ("_service_loop", None)):
        monkeypatch.setattr(P, name, value)
    monkeypatch.setattr(P, "_remote_only", lambda: False)
    monkeypatch.setattr(P, "_bundled_engine", lambda: True)
    monkeypatch.setattr(P, "_on_service_loop", lambda: True)
    monkeypatch.setattr(P, "gpu_quiet", lambda: True)
    # Memory: as tests/test_memory_saver.py sets it up.
    monkeypatch.setattr(M, "SETTINGS_PATH", tmp_path / "memory_saver" / "settings.json")
    monkeypatch.setattr(M, "_state", M._State(started=0.0))
    monkeypatch.setattr(M, "_prewarm_task", None)
    monkeypatch.setattr(M, "_holds", 0)
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_last_turn", None)
    monkeypatch.setattr(idle, "_in_flight", {})
    monkeypatch.setattr(idle, "_last_used", {})
    monkeypatch.setattr(idle, "_idle_minutes", 10.0)
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: MODEL)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    yield
    stage_mode._since = None


def demo(on):
    stage_mode.set_active(on)


class FakeClient:
    managed = True

    def __init__(self, loaded=(MODEL,)):
        from service.engine import BUNDLED_PORT
        self.base_url = f"http://127.0.0.1:{BUNDLED_PORT}"
        self.loaded = set(loaded)
        self.calls = []

    def keep_warm(self):
        return {MODEL}

    async def loaded_models(self):
        return sorted(self.loaded)

    async def unload(self, model):
        self.calls.append(("unload", model))
        self.loaded.discard(model)

    async def load(self, model):
        self.calls.append(("load", model))
        self.loaded.add(model)


# --- power ------------------------------------------------------------------------

def _capping_conditions(monkeypatch, *, away, battery, turn):
    monkeypatch.setattr(P, "away", lambda: away)
    monkeypatch.setattr(P, "on_battery", lambda: battery)
    monkeypatch.setattr(P, "_turn_running", lambda: turn)


@pytest.mark.parametrize("away,battery,turn,expected", [
    (True, True, False, {P.CHAT: None, P.BRIEF: P.LOW, P.BACKGROUND: P.LOW}),
    (False, True, True, {P.CHAT: P.BATTERY, P.BRIEF: P.BATTERY, P.BACKGROUND: P.LOW}),
    (False, False, True, {P.CHAT: None, P.BRIEF: None, P.BACKGROUND: P.LOW}),
])
def test_no_tier_is_capped_while_demo_mode_is_on_and_every_cap_comes_back_after(
        monkeypatch, away, battery, turn, expected):
    _capping_conditions(monkeypatch, away=away, battery=battery, turn=turn)
    assert {t: P._wanted(t) for t in P._TIERS} == expected
    demo(True)
    assert {t: P._wanted(t) for t in P._TIERS} == dict.fromkeys(P._TIERS)
    for tier in P._TIERS:
        with P.power_tier(tier):
            assert P.payload_fields() == {}
            assert asyncio.run(P.checked_payload_fields(_never_lift)) == {}
    demo(False)
    assert {t: P._wanted(t) for t in P._TIERS} == expected


async def _never_lift():
    raise AssertionError("no lift is asked when nothing is capped")


def test_save_power_reads_off_in_demo_mode_but_its_saved_file_never_changes(monkeypatch):
    P.set_save_when_away(True)
    before = P.SETTINGS_PATH.read_bytes()
    assert P._capping_possible()
    demo(True)
    assert not P._capping_possible()
    assert P.save_when_away() is True              # what Settings shows: the saved choice
    # The scheduler's refresh reads nothing and stores present, on AC.
    monkeypatch.setattr(P, "drop_gpu_readings", lambda: None)
    assert P.refresh_away(hid_idle_s=9999, on_battery=True) is False
    assert not P.on_battery() and not P.away()
    demo(False)
    assert P._capping_possible()
    assert P.SETTINGS_PATH.read_bytes() == before


def test_demo_mode_turning_on_lifts_the_caps_already_in_flight(monkeypatch):
    lifts = []

    async def lift():
        lifts.append(True)
        return True

    async def scenario():
        P._service_loop = asyncio.get_running_loop()
        with P._calls_lock:
            P._capped[0] = (10, lift)
        P.stage_mode_changed(True)
        await asyncio.gather(*list(P._lift_tasks))

    asyncio.run(scenario())
    assert lifts == [True]


def test_demo_mode_listener_asks_nothing_without_a_cap_or_when_turning_off(monkeypatch):
    called = []
    monkeypatch.setattr(P, "_schedule_lift", lambda: called.append(True))
    P.stage_mode_changed(True)                     # nothing capped
    P._capped[0] = (10, _never_lift)
    P.stage_mode_changed(False)                    # turning off lifts nothing
    assert called == []


# --- memory -----------------------------------------------------------------------

def tick(client, now, pressure=M.NORMAL):
    return asyncio.run(M.tick(client, now, pressure=pressure))


def test_decide_never_parks_or_closes_a_window_in_demo_mode():
    idle._last_turn = 0.0
    args = dict(loaded=True, busy=False, enabled=True, last_used=None)
    assert M.decide(now=60 * MIN, pressure=M.CRITICAL, **args) == M.PARK
    M._state.window_started = 0.0
    assert M.decide(now=60 * MIN, pressure=M.NORMAL, **args) == M.CLOSE_WINDOW
    demo(True)
    for pressure in (M.NORMAL, M.WARN, M.CRITICAL):
        for window in (None, 0.0):
            M._state.window_started = window
            assert M.decide(now=60 * MIN, pressure=pressure, **args) == M.LEAVE
    M._state.parked, M._state.parked_at, M._state.window_started = True, 0.0, None
    assert M.decide(now=600 * MIN, loaded=False, busy=False, pressure=M.NORMAL,
                    enabled=True, last_used=None) == M.LEAVE      # no window opens


def test_tick_keeps_the_model_in_demo_mode_and_the_saved_switch_is_untouched():
    M.set_save_memory(True)
    before = M.SETTINGS_PATH.read_bytes()
    c = FakeClient()
    idle._last_turn = 0.0
    demo(True)
    for minutes in (10, 30, 60, 120):
        assert tick(c, minutes * MIN, pressure=M.CRITICAL) == M.LEAVE
    assert c.calls == [] and MODEL in c.loaded
    state = M.state()
    assert state["save_memory"] is True and state["paused_by_demo"] is True
    demo(False)
    assert M.state()["paused_by_demo"] is False
    assert M.SETTINGS_PATH.read_bytes() == before


def test_after_demo_mode_the_park_timer_starts_over_instead_of_parking_at_once():
    c = FakeClient()
    idle._last_turn = 0.0
    demo(True)
    assert tick(c, 60 * MIN) == M.LEAVE            # an hour-long demo, no turn
    demo(False)
    assert tick(c, 61 * MIN) == M.KEEP             # not parked the moment it ends
    assert tick(c, 64.5 * MIN) == M.KEEP
    assert tick(c, 65 * MIN) == M.PARK             # PARK_AFTER_S after the last demo tick
    assert c.calls == [("unload", MODEL)] and M._state.parked


def test_a_window_open_when_demo_mode_starts_becomes_the_demos():
    c = FakeClient()
    M.note_parked("idle", 0.0)
    M._state.last_window = M._state.window_started = 60 * MIN    # a window loaded it
    demo(True)
    assert tick(c, 61 * MIN) == M.LEAVE
    assert M._state.window_started is None and M._state.parked is False
    assert tick(c, 80 * MIN) == M.LEAVE            # the window would have closed by now
    assert ("unload", MODEL) not in c.calls and not M.batch_window_open()


def test_a_parked_model_is_left_for_the_warm_up_and_bookkeeping_follows_it():
    c = FakeClient(loaded=())
    M.note_parked("idle", 0.0)
    demo(True)
    assert tick(c, 1 * MIN) == M.LEAVE
    assert c.calls == [] and M._state.parked       # still unloaded, and says so
    c.loaded.add(MODEL)                            # stage_warm loaded it
    assert tick(c, 2 * MIN) == M.LEAVE
    assert M._state.parked is False and c.calls == []


def test_prewarm_loads_under_critical_pressure_only_in_demo_mode(monkeypatch):
    monkeypatch.setattr(M, "pressure_level", lambda: M.CRITICAL)
    c = FakeClient(loaded=())

    async def go():
        answer = await M.prewarm(c)
        if M._prewarm_task is not None:
            await M._prewarm_task
        return answer

    assert asyncio.run(go()) == "pressure"
    demo(True)
    assert asyncio.run(go()) == "loading" and MODEL in c.loaded


# --- idle unload ------------------------------------------------------------------

def test_the_idle_timer_unloads_nothing_in_demo_mode_and_resumes_after():
    import time
    c = FakeClient(loaded=(MODEL, OTHER))
    idle._last_used[OTHER] = time.monotonic() - 60 * MIN
    minutes = idle.get_idle_minutes()
    demo(True)
    asyncio.run(idle_unloader._pass(c))
    assert c.calls == [] and OTHER in c.loaded
    assert idle.get_idle_minutes() == minutes
    demo(False)
    asyncio.run(idle_unloader._pass(c))
    assert ("unload", OTHER) in c.calls and ("unload", MODEL) not in c.calls


def test_the_idle_pass_still_ticks_save_memory_in_demo_mode(monkeypatch):
    seen = []

    async def fake_tick(client):
        seen.append(client)
        return M.LEAVE

    monkeypatch.setattr(M, "tick", fake_tick)
    c = FakeClient()
    demo(True)
    asyncio.run(idle_unloader._pass(c))
    assert seen == [c]


def test_this_file_is_in_the_simulation_manifest():
    from pathlib import Path
    manifest = (Path(__file__).resolve().parents[1] / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_stage_mode_overrides.py"' in manifest
