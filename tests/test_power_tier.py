"""GPU power tiers and the "Save power" switch; no engine is contacted."""
import asyncio
import contextlib
import json
import stat
from types import SimpleNamespace

import httpx
import pytest

from service.inference import power_tier as P
from service.inference.omlx_client import OMLXClient, _lift_gpu_power, _power_fields


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in P._ENV.values():
        monkeypatch.delenv(name, raising=False)
    # Every test starts with the switch at its default, in a private settings
    # file, the user present and on AC (no readings yet), no turn and no call
    # anyone is waiting on.
    monkeypatch.setattr(P, "SETTINGS_PATH", tmp_path / "power" / "settings.json")
    monkeypatch.setattr(P, "_away", None)
    monkeypatch.setattr(P, "_battery", None)
    monkeypatch.setattr(P, "_watched_calls", 0)
    monkeypatch.setattr(P, "_uncapped_watched", 0)
    monkeypatch.setattr(P, "_capped", {})
    monkeypatch.setattr(P, "_lift_ok", None)
    monkeypatch.setattr(P, "_lift_retry_at", 0.0)
    monkeypatch.setattr(P, "_watch_retry_at", 0.0)
    monkeypatch.setattr(P, "_lifted", False)
    monkeypatch.setattr(P, "_watch_task", None)
    monkeypatch.setattr(P, "_service_loop", None)
    monkeypatch.setattr(P, "_on_battery_probe", lambda: False)
    monkeypatch.setattr(P, "_remote_only", lambda: False)
    monkeypatch.setattr(P, "_bundled_engine", lambda: True)    # Wisp's own engine, which can lift
    from service import idle
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    # No other app on the GPU, unless a test says so (the real check is tested
    # on its own below, against a fake ioreg).
    monkeypatch.setattr(P, "check_other_gpu", lambda: True)
    monkeypatch.setattr(P, "keep_gpu_baseline", lambda: None)
    monkeypatch.setattr(P, "gpu_quiet", lambda: True)
    monkeypatch.setattr(P, "_gpu_sample", None)
    monkeypatch.setattr(P, "_gpu_answer", None)
    monkeypatch.setattr(P, "_gpu_failed_at", None)


_REAL_CHECK = P.check_other_gpu
_REAL_BASELINE = P.keep_gpu_baseline
_REAL_GPU_QUIET = P.gpu_quiet
_REAL_BATTERY_PROBE = P._on_battery_probe


class _OtherApp:
    """Another app's GPU use on a fake clock: ``run(seconds, share)`` advances
    time with the app holding ``share`` of the GPU; the look's own pause runs at
    the current share. ``fail`` makes ioreg fail; ``reads`` counts the reads."""

    def __init__(self):
        self.now, self.ns, self.share, self.fail, self.reads = 1000.0, 0, 0.0, False, 0

    def run(self, seconds, share):
        self.share = share
        self.now += seconds
        self.ns += int(seconds * share * 1e9)

    def times(self):
        self.reads += 1
        return None if self.fail else {200: self.ns}


@pytest.fixture
def real_gpu(monkeypatch):
    """The real looks, against _OtherApp instead of ioreg."""
    app = _OtherApp()
    monkeypatch.setattr(P, "check_other_gpu", _REAL_CHECK)
    monkeypatch.setattr(P, "keep_gpu_baseline", _REAL_BASELINE)
    monkeypatch.setattr(P, "gpu_quiet", _REAL_GPU_QUIET)
    monkeypatch.setattr(P.time, "monotonic", lambda: app.now)
    monkeypatch.setattr(P.time, "sleep", lambda seconds: app.run(seconds, app.share))
    monkeypatch.setattr(P, "_other_gpu_times", app.times)
    return app


@pytest.fixture
def away():
    """Nobody has touched the keyboard or mouse for longer than AWAY_AFTER_S."""
    P.refresh_away(P.AWAY_AFTER_S + 1)


@pytest.fixture
def battery():
    """The user is at the Mac, and it is on battery."""
    P.refresh_away(5, on_battery=True)


# --- other apps on the GPU --------------------------------------------------------

_IOREG = '''+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x1>
    {
      "AppUsage" = ({"API"="Metal","accumulatedGPUTime"=%d},{"API"="Metal","accumulatedGPUTime"=5})
      "IOUserClientCreator" = "pid 200, Game"
    }
+-o AGXDeviceUserClient  <class AGXDeviceUserClient, id 0x2>
    {
      "AppUsage" = ()
      "IOUserClientCreator" = "pid 300, Idle"
    }
'''


def test_gpu_times_are_summed_per_process():
    assert P._parse_gpu_times(_IOREG % 100) == {200: 105, 300: 0}
    assert P._parse_gpu_times("") == {}


def test_another_app_using_seven_and_a_half_percent_of_the_gpu_keeps_every_cap_off(real_gpu, away):
    app = real_gpu
    assert P.OTHER_GPU_BUSY == 0.075                            # the user's choice
    assert P.check_other_gpu() is False                         # first look: no baseline yet
    assert _caps()[P.BACKGROUND] is None
    app.run(30, 0.5)                                            # a game held half the GPU
    assert P.check_other_gpu() is False and _caps()[P.BACKGROUND] is None
    app.run(30, 0.08)                                           # a video, a browser animating
    assert P.check_other_gpu() is False and _caps()[P.BACKGROUND] is None
    app.run(30, 0.05)                                           # an app redrawing its window
    assert P.check_other_gpu() is True and _caps()[P.BACKGROUND] == 10
    app.run(30, 0.01)                                           # nothing but a blinking cursor
    assert P.check_other_gpu() is True and _caps()[P.BACKGROUND] == 10


def test_an_app_that_started_a_moment_ago_is_seen_over_a_short_window(real_gpu, away):
    # Averaged over the 30 s since the tick, half a second of a game is 1.7%.
    app = real_gpu
    P.keep_gpu_baseline()
    reads = app.reads
    app.run(29.5, 0.0)
    app.run(0.5, 1.0)
    assert P.check_other_gpu() is False and _caps()[P.BACKGROUND] is None
    app.run(30, 0.0)                                            # the game was closed
    assert P.check_other_gpu() is True
    assert app.reads == reads + 4                               # after the tick, two pairs


def test_a_failed_ioreg_read_means_uncapped_and_no_read_for_a_while(real_gpu, away):
    app = real_gpu
    P.keep_gpu_baseline()
    app.run(30, 0.0)
    app.fail = True
    assert P.check_other_gpu() is False                         # an unreadable ioreg: uncapped
    app.fail = False
    app.run(P._GPU_RETRY_S - 1, 0.0)
    reads = app.reads
    assert P.check_other_gpu() is False and app.reads == reads  # not read again yet
    app.run(2, 0.0)
    assert P.check_other_gpu() is False and app.reads == reads + 1   # read: a new baseline only
    app.run(1, 0.0)
    assert P.check_other_gpu() is True


def test_a_cap_needs_a_look_from_the_last_two_seconds(real_gpu, away):
    app = real_gpu
    P.check_other_gpu()
    app.run(30, 0.0)
    assert P.check_other_gpu() is True and _caps()[P.BACKGROUND] == 10
    app.run(P._GPU_FRESH_S, 0.0)
    assert _caps()[P.BACKGROUND] is None                        # the last look is too old


def test_a_look_right_after_another_reuses_it_and_an_old_baseline_cant_tell(real_gpu):
    app = real_gpu
    P.check_other_gpu()
    app.run(30, 0.0)
    assert P.check_other_gpu() is True
    reads = app.reads
    app.run(0.2, 1.0)                                           # would be busy, if read
    assert P.check_other_gpu() is True and app.reads == reads   # too soon: the last answer
    app.run(1.0, 1.0)
    assert P.check_other_gpu() is False and app.reads == reads + 1   # a short window: one read
    app.run(P._GPU_BASELINE_MAX_S + 1, 0.0)
    assert P.check_other_gpu() is False                         # a baseline that old can't tell


def test_a_look_already_running_or_too_slow_means_uncapped(monkeypatch, away):
    import threading
    monkeypatch.setattr(P, "check_other_gpu", _REAL_CHECK)
    monkeypatch.setattr(P, "_GPU_LOCK_WAIT_S", 0.01)
    with P._gpu_lock:                                           # another look stuck in ioreg
        assert P.check_other_gpu() is False
    stuck = threading.Event()
    monkeypatch.setattr(P, "check_other_gpu", lambda: stuck.wait(5) or True)
    monkeypatch.setattr(P, "_LOOK_TIMEOUT_S", 0.05)

    async def run():
        try:
            with P.power_tier(P.BACKGROUND):
                return await P.checked_payload_fields(_lift_ok_now)
        finally:
            stuck.set()                                         # let the stuck look finish
    assert asyncio.run(run()) == {}                             # the request didn't wait for it


async def _lift_ok_now():
    return True


def test_the_tick_keeps_a_gpu_baseline_whenever_caps_are_possible(monkeypatch):
    # Background work can be capped at any time, so the baseline is kept on AC too.
    looks = []
    monkeypatch.setattr(P, "keep_gpu_baseline", lambda: looks.append("baseline"))
    monkeypatch.setattr(P, "check_other_gpu", lambda: looks.append("look") or True)
    P.refresh_away(5, on_battery=False)                         # present, on AC
    P.refresh_away(5, on_battery=True)
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=False)
    assert looks == ["baseline", "baseline", "baseline"]
    P.set_save_when_away(False)
    P.refresh_away(5, on_battery=False)
    assert looks == ["baseline", "baseline", "baseline"]        # the switch off: none


@pytest.mark.parametrize("failure", [OSError("no ioreg"),
                                     __import__("subprocess").TimeoutExpired("ioreg", 5),
                                     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")])
def test_a_failed_ioreg_read_is_none(monkeypatch, failure):
    import subprocess

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", run)
    assert P._other_gpu_times() is None


def test_ioreg_output_that_isnt_utf8_is_still_read(monkeypatch):
    import subprocess
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=_IOREG % 7, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(P, "_own", lambda pid: False)
    assert P._other_gpu_times() == {200: 12, 300: 0}
    assert seen.get("errors") == "replace"


def test_wisp_its_bundled_engine_and_windowserver_dont_count(monkeypatch):
    from service.inference import local_peer
    paths = {1: "/Applications/oMLX.app/Contents/Resources/Python/cpython-3.11/bin/python3.11",
             2: "/Applications/Wisp.app/Contents/MacOS/Wisp",
             3: "/System/Library/PrivateFrameworks/SkyLight.framework/Versions/A/Resources/WindowServer",
             4: "/Applications/Blender.app/Contents/MacOS/Blender",
             6: "/Applications/Wisp.app/Contents/Resources/engine/py/bin/python3.11",
             7: "/Users/me/MOE/engine/py/bin/python3.11"}           # a source checkout's engine

    def path(pid):
        if pid not in paths:
            raise ProcessLookupError(pid)
        return paths[pid]

    monkeypatch.setattr(local_peer, "process_path", path)
    monkeypatch.setattr(P, "_engine_root", lambda: "/Users/me/MOE/engine/")
    # The user's oMLX.app is never Wisp's capped engine, so its GPU use counts.
    assert [pid for pid in (1, 2, 3, 4, 5, 6, 7) if not P._own(pid)] == [1, 4, 5]


# --- the switch -------------------------------------------------------------------

def test_the_switch_is_on_by_default_persists_and_reads_unreadable_as_the_default():
    assert P.save_when_away() is True
    assert P.set_save_when_away(False) is False and P.save_when_away() is False
    assert json.loads(P.SETTINGS_PATH.read_text()) == {"save_when_away": False}
    assert stat.S_IMODE(P.SETTINGS_PATH.stat().st_mode) == 0o600
    assert P.set_save_when_away(True) is True and P.save_when_away() is True
    P.SETTINGS_PATH.write_text("{not json")
    assert P.save_when_away() is True
    P.SETTINGS_PATH.write_text('{"save_when_away": "no"}')
    assert P.save_when_away() is True
    with pytest.raises(ValueError):
        P.set_save_when_away("false")


def test_the_endpoint_reads_and_writes_the_switch_from_the_app_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from service.power_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/power").json() == {"save_when_away": True}
    assert client.post("/power", json={"save_when_away": False}).json() == {"save_when_away": False}
    assert P.save_when_away() is False
    # A web page (Origin header), a non-JSON body or a non-bool value can't change it.
    assert client.post("/power", json={"save_when_away": True},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert client.post("/power", content="save_when_away=true",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/power", json={"save_when_away": "true"}).status_code == 422
    assert client.post("/power", json={"save_when_away": True, "watts": 5}).status_code == 422
    assert P.save_when_away() is False
    # The old Low Power Mode endpoints are gone.
    assert client.post("/power/other_app", json={"event": "launched"}).status_code in (404, 405)
    assert client.post("/power/activity", json={}).status_code in (404, 405)


# --- away -------------------------------------------------------------------------

def test_away_needs_ten_idle_input_minutes_a_fresh_reading_and_no_turn(monkeypatch):
    from service import idle
    assert P.away() is False                                   # never read: present
    assert P.refresh_away(P.AWAY_AFTER_S - 5) is False and P.away() is False
    assert P.refresh_away(P.AWAY_AFTER_S) is True and P.away() is True
    assert P.refresh_away(None, probe=False) is False and P.away() is False  # unreadable: present
    P.refresh_away(P.AWAY_AFTER_S + 1)
    monkeypatch.setattr(idle, "_foreground_turns", 1)
    assert P.away() is False                                   # a turn is running
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    mono, wall, value = P._away
    P._away = (mono - P._AWAY_STALE_S - 1, wall, value)
    assert P.away() is False                                   # a stale reading


def test_a_reading_from_before_sleep_is_stale_at_wake(away):
    # time.monotonic() doesn't advance while the Mac sleeps, so only the wall
    # clock shows the night: the wake backfill must not run capped on it.
    assert P.away() is True
    mono, wall, value = P._away
    P._away = (mono, wall - 8 * 3600, value)
    assert P.away() is False
    P._away = (mono, wall + 3600, value)                       # wall clock set back
    assert P.away() is False


def test_the_scheduler_refreshes_away_off_the_event_loop():
    import inspect

    from service.assistant import scheduler
    source = inspect.getsource(scheduler)
    assert "await asyncio.to_thread(refresh_away)" in source


# --- tiers ------------------------------------------------------------------------

def test_the_caps_are_10_w_and_15_w():
    assert P.watts(P.LOW) == 10
    assert P.watts(P.BATTERY) == 15
    assert P.watts(None) is None and P.watts(P.CHAT) is None
    with pytest.raises(ValueError):
        with P.power_tier("intensive"):
            pass


def test_env_overrides_zero_turns_off_and_out_of_range_is_ignored(monkeypatch):
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "7")
    assert P.watts(P.LOW) == 7
    monkeypatch.setenv("WISP_BACKGROUND_GPU_W", "0")
    assert P.watts(P.LOW) is None
    for bad in ("1", "5000", "ten"):
        monkeypatch.setenv("WISP_BACKGROUND_GPU_W", bad)
        assert P.watts(P.LOW) == 10
    monkeypatch.setenv("WISP_BATTERY_GPU_W", "12")
    assert P.watts(P.BATTERY) == 12


@contextlib.contextmanager
def _turn():
    """A chat turn in flight (what main.py's runner and the image and document
    paths bracket with idle.begin_foreground/end_foreground)."""
    from service import idle
    idle.begin_foreground()
    try:
        yield
    finally:
        idle.end_foreground()


def _caps():
    out = {}
    for tier in (None, P.CHAT, P.BRIEF, P.BACKGROUND):
        if tier is None:
            out[tier] = P.payload_fields().get("gpu_power_limit_w")
        elif tier == P.CHAT:
            with _turn(), P.power_tier(tier):
                out[tier] = P.payload_fields().get("gpu_power_limit_w")
        else:
            with P.power_tier(tier):
                out[tier] = P.payload_fields().get("gpu_power_limit_w")
    return out


def test_on_ac_with_the_user_present_only_background_work_is_capped():
    # The user's answer (2026-10-28): "Always 10 W" for background work.
    P.refresh_away(5, on_battery=False)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: 10}


def test_on_battery_chat_and_brief_run_at_15_w_and_background_at_10_w(battery):
    assert _caps() == {None: None, P.CHAT: 15, P.BRIEF: 15, P.BACKGROUND: 10}


def test_away_brief_and_background_run_at_10_w_and_chat_uncapped(away):
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: 10, P.BACKGROUND: 10}


def test_away_on_battery_takes_the_lower_cap():
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: 15, P.BRIEF: 10, P.BACKGROUND: 10}


def test_the_switch_off_caps_nothing(battery):
    P.set_save_when_away(False)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}


def test_another_app_on_the_gpu_lifts_every_cap(monkeypatch):
    monkeypatch.setattr(P, "gpu_quiet", lambda: False)
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}


def test_the_users_own_omlx_app_is_never_capped_nor_probed(monkeypatch):
    # It holds a cap 15 s past its request and can't lift one mid-request.
    looks = []
    monkeypatch.setattr(P, "_bundled_engine", lambda: False)
    monkeypatch.setattr(P, "check_other_gpu", lambda: looks.append("look") or True)
    monkeypatch.setattr(P, "keep_gpu_baseline", lambda: looks.append("baseline"))
    P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
    assert _caps() == {None: None, P.CHAT: None, P.BRIEF: None, P.BACKGROUND: None}
    assert looks == [] and P.away() is False and P.on_battery() is False


def test_a_stale_battery_reading_counts_as_ac(battery):
    mono, wall, value = P._battery
    P._battery = (mono, wall - 8 * 3600, value)                # read before a night asleep
    assert P.on_battery() is False and _caps()[P.CHAT] is None


def test_the_battery_probe_reads_the_cached_power_source(monkeypatch):
    from service.mail_memory import runner
    monkeypatch.setattr(P, "_on_battery_probe", _REAL_BATTERY_PROBE)
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner, "_power", lambda: (False, 80, False))
    assert P._on_battery_probe() is True
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner, "_power", lambda: (True, None, False))   # AC, or a failed read
    assert P._on_battery_probe() is False


def test_brief_and_background_go_uncapped_while_a_watched_call_runs(away):
    started = P.call_started()                                 # a reply, research, a phone turn
    assert started == (True, None) and P.watched_running()
    with P.power_tier(P.BRIEF):
        assert P.payload_fields() == {}
        assert P.call_started() == (False, None)               # Brief calls aren't watched
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {}
    P.call_ended(started)
    assert not P.watched_running()
    with P.power_tier(P.BACKGROUND):
        assert P.payload_fields() == {"gpu_power_limit_w": 10}


def test_a_chat_on_battery_keeps_its_cap_beside_another_capped_chat_call(battery):
    async def run():
        with _turn(), P.power_tier(P.CHAT):
            started = P.call_started(15, _lifter([]))     # the turn's other call, at 15 W
            assert P.payload_fields() == {"gpu_power_limit_w": 15}
            P.call_ended(started)

    asyncio.run(run())


def test_a_task_left_running_after_its_chat_turn_is_not_capped(battery):
    # CHAT is a ContextVar, so a task spawned inside a turn inherits it; once
    # the turn has ended, that task's calls are not chat and go uncapped.
    async def run():
        release = asyncio.Event()
        seen = []

        async def spawned():
            seen.append(P.payload_fields())
            await release.wait()
            seen.append(P.payload_fields())

        with _turn(), P.power_tier(P.CHAT):
            task = asyncio.ensure_future(spawned())
            await asyncio.sleep(0)
        release.set()
        await task
        return seen

    assert asyncio.run(run()) == [{"gpu_power_limit_w": 15}, {}]


@pytest.mark.parametrize("ending", [ValueError("boom"), asyncio.CancelledError()])
def test_the_chat_tier_resets_on_an_exception_or_cancellation(battery, ending):
    with pytest.raises(type(ending)):
        with P.power_tier(P.CHAT):
            assert P.current_tier() == P.CHAT
            raise ending
    assert P.current_tier() is None

    async def run():
        async def turn():
            with P.power_tier(P.CHAT):
                await asyncio.sleep(10)

        task = asyncio.ensure_future(turn())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return P.current_tier()

    assert asyncio.run(run()) is None


def test_with_the_switch_off_nothing_is_probed(monkeypatch):
    called = []
    monkeypatch.setattr(P, "_on_battery_probe", lambda: called.append("pmset") or True)
    monkeypatch.setattr(P, "check_other_gpu", lambda: called.append("gpu") or True)
    monkeypatch.setattr(P, "keep_gpu_baseline", lambda: called.append("baseline"))
    from service.mail_memory import runner
    monkeypatch.setattr(runner, "_hid_idle_s", lambda: called.append("hid") or 9999.0)
    P._gpu_sample = (1.0, {200: 5})
    P.set_save_when_away(False)
    assert P.refresh_away() is False
    assert called == [] and P._gpu_sample is None              # only the baseline is dropped
    assert P.away() is False and P.on_battery() is False


def test_with_a_remote_only_profile_nothing_is_probed(monkeypatch):
    called = []
    monkeypatch.setattr(P, "_remote_only", lambda: True)
    monkeypatch.setattr(P, "_on_battery_probe", lambda: called.append("pmset") or True)
    from service.mail_memory import runner
    monkeypatch.setattr(runner, "_hid_idle_s", lambda: called.append("hid") or 9999.0)
    assert P.refresh_away() is False
    assert called == []


_PMSET_GENERAL = " lowpowermode         0\n"


@pytest.mark.parametrize("batt, battery", [
    ("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t72%; discharging; 4:10 remaining present: true\n", True),
    ("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging present: true\n", False),
    ("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t64%; charging; 1:02 remaining present: true\n", False),
    ("Now drawing from 'AC Power'\n", False),                                  # a desktop: no battery
    ("Now drawing from 'UPS Power'\n -CyberPower UPS (id=2)\t90%; discharging present: true\n", True),
])
def test_the_power_source_is_read_from_pmset(monkeypatch, batt, battery):
    import subprocess

    from service.mail_memory import runner

    def run(args, **kwargs):
        out = batt if args[-1] == "batt" else _PMSET_GENERAL
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(P, "_on_battery_probe", _REAL_BATTERY_PROBE)
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner.subprocess, "run", run)
    assert P._on_battery_probe() is battery


def test_tier_is_scoped_to_the_block_and_inherited_by_tasks(away):
    async def seen():
        return P.payload_fields()

    async def run():
        with P.power_tier(P.BACKGROUND):
            task = asyncio.ensure_future(seen())
        return await task

    assert asyncio.run(run()) == {"gpu_power_limit_w": 10}
    assert P.current_tier() is None
    with pytest.raises(ValueError):
        with P.power_tier("turbo"):
            pass


# --- the client -------------------------------------------------------------------

def _client(managed, reply_hook=None, lift_status=200):
    """An OMLXClient on a fake engine; the lift requests it gets land in client.lifts."""
    bodies = []
    lifts = []

    def reply(request):
        if request.url.path == "/v1/wisp/gpu-power/lift":
            lifts.append(request.method)
            return httpx.Response(lift_status, json={"status": "ok"})
        bodies.append(json.loads(request.content))
        if reply_hook:
            reply_hook()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": "ok"}}]})

    client = OMLXClient.__new__(OMLXClient)
    client.managed = managed
    client.api_prefix = "/v1"
    client.provider = SimpleNamespace(name="omlx")
    client._fit_request = lambda m, messages, tools, tokens, **kw: (m, messages, tools, tokens)
    client._client = httpx.AsyncClient(base_url="http://test.invalid",
                                       transport=httpx.MockTransport(reply))
    client.lifts = lifts
    return client, bodies


def test_the_client_sends_each_call_its_per_request_cap():
    client, bodies = _client(managed=True)
    hi = [{"role": "user", "content": "hi"}]

    async def run():
        try:
            await client.chat("m", hi)                        # 0: untiered (research...), AC
            with _turn(), P.power_tier(P.CHAT):
                await client.chat("m", hi)                    # 1: chat, AC
                P.refresh_away(5, on_battery=True)
                await client.chat("m", hi)                    # 2: chat, battery
            with P.power_tier(P.BRIEF):
                await client.chat("m", hi)                    # 3: Brief, battery
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", hi)                    # 4: background, battery (10 W)
                P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=False)
                await client.chat("m", hi)                    # 5: background, away
                await client.chat("m", hi, gpu_power_limit_w=12)  # 6: explicit wins
            await client.chat("m", hi)                        # 7: untiered, away
        finally:
            await client._client.aclose()

    asyncio.run(run())
    caps = [body.get("gpu_power_limit_w") for body in bodies]
    assert caps == [None, None, 15, 15, 10, 10, 12, None]
    assert client.lifts == ["POST"]                           # asked once, before the first cap


def test_the_client_counts_a_watched_call_only_while_it_runs(away):
    during = []
    client, _ = _client(managed=True, reply_hook=lambda: during.append(P.watched_running()))

    async def run():
        try:
            with P.power_tier(P.CHAT):
                await client.chat("m", [{"role": "user", "content": "hi"}])
            with P.power_tier(P.BRIEF):
                await client.chat("m", [{"role": "user", "content": "brief"}])
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", [{"role": "user", "content": "bg"}])
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert during == [True, False, False] and not P.watched_running()


def test_a_remote_provider_never_gets_the_field(away):
    client = OMLXClient.__new__(OMLXClient)
    client.managed = False
    with P.power_tier(P.BACKGROUND):
        assert asyncio.run(_power_fields(client)) == {}
    assert asyncio.run(_power_fields(client)) == {}


_HI = [{"role": "user", "content": "hi"}]


def _engine(release=None, lift=200):
    """An OMLXClient on a fake bundled engine whose replies wait for ``release``.
    Returns (client, bodies, lifts): each lift records how many requests came first."""
    bodies, lifts = [], []

    async def reply(request):
        if request.url.path == "/v1/wisp/gpu-power/lift":
            if lift == "unreachable":
                raise httpx.ConnectError("engine gone", request=request)
            lifts.append(len(bodies))
            return httpx.Response(lift, json={"status": "ok"})
        bodies.append(json.loads(request.content))
        if release is not None:
            await release.wait()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": "ok"}}]})

    client, _ = _client(managed=True)
    client._client = httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(reply))
    return client, bodies, lifts


def test_the_watch_lifts_a_running_cap_the_moment_another_app_uses_the_gpu(monkeypatch, away):
    gpu = {"quiet": True}
    monkeypatch.setattr(P, "check_other_gpu", lambda: gpu["quiet"])
    monkeypatch.setattr(P, "gpu_quiet", lambda: gpu["quiet"])
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)

    async def run():
        release = asyncio.Event()
        client, bodies, lifts = _engine(release)
        try:
            with P.power_tier(P.BACKGROUND):
                call = asyncio.ensure_future(client.chat("m", _HI))
                await asyncio.sleep(0.05)
                assert bodies[0]["gpu_power_limit_w"] == 10 and lifts == [0]   # asked once, first
                await asyncio.sleep(0.05)
                assert lifts == [0]                       # nothing else on the GPU: the cap stays
                gpu["quiet"] = False                      # a game starts mid-request
                await asyncio.sleep(0.05)
                assert lifts == [0, 1]                    # lifted while the request still runs
                await asyncio.sleep(0.05)
                assert lifts == [0, 1]                    # once
                release.set()
                await call
                await client.chat("m", _HI)               # the game still runs: uncapped
                assert "gpu_power_limit_w" not in bodies[1]
            await asyncio.sleep(0.05)
            assert P._watch_task is None and not P._capped   # nothing capped: no more looks
        finally:
            await client._client.aclose()

    asyncio.run(run())


def _lifter(log, answer=True):
    async def lift():
        log.append(answer)
        return answer
    return lift


def test_a_call_someone_waits_on_never_runs_under_a_lower_cap(battery):
    lifted = []
    lift = _lifter(lifted)

    async def run():
        with P.power_tier(P.BRIEF):
            brief = P.call_started(15, lift)
        with _turn(), P.power_tier(P.CHAT):
            chat = P.call_started(15, lift)               # the same cap: nothing to lift
            await asyncio.sleep(0)
            assert lifted == []
        with P.power_tier(P.BACKGROUND):
            background = P.call_started(10, lift)
            await asyncio.sleep(0)
            assert lifted == []                           # nobody waits on background work
        with _turn(), P.power_tier(P.CHAT):
            chat_too = P.call_started(15, lift)           # would run at 10 W: lifted
            await asyncio.sleep(0)
            assert len(lifted) == 1
        research = P.call_started(None, lift)             # research, never capped: lifted
        await asyncio.sleep(0)
        assert len(lifted) == 2
        assert P._lifted is False                         # only the watch marks a cap lifted
        for started in (brief, chat, background, chat_too, research):
            P.call_ended(started)
        assert not P._capped and not P.watched_running() and P._uncapped_watched == 0

    asyncio.run(run())


def test_a_chat_lift_racing_its_own_cap_still_leaves_the_watch_armed(monkeypatch, battery):
    # The lift for a chat starting beside a 10 W call may reach the engine
    # before the chat's own 15 W does; the watch must still look after it.
    gpu = {"quiet": True}
    lifted = []
    monkeypatch.setattr(P, "check_other_gpu", lambda: gpu["quiet"])
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)

    async def run():
        lift = _lifter(lifted)
        with P.power_tier(P.BACKGROUND):
            background = P.call_started(10, lift)
        with _turn(), P.power_tier(P.CHAT):
            chat = P.call_started(15, lift)
            await asyncio.sleep(0.03)
            assert len(lifted) == 1                       # the chat's lift
            P.call_ended(background)
            gpu["quiet"] = False                          # then a game starts
            await asyncio.sleep(0.05)
            assert len(lifted) == 2                       # the watch lifts the chat's own cap
            P.call_ended(chat)

    asyncio.run(run())


def test_a_lift_through_a_client_that_just_closed_goes_through_another(away):
    # The newest capped call's client closed mid-POST (its call ended): the
    # engine is still reachable through the other call's client.
    asked = []

    async def run():
        with P.power_tier(P.BACKGROUND):
            older = P.call_started(10, _lifter(asked, True))
            newest = P.call_started(10, _lifter(asked, None))
        await P._lift_now(watch=True)
        P.call_ended(newest)
        P.call_ended(older)

    asyncio.run(run())
    assert asked == [None, True]                               # newest first, then the other
    assert P._lifted is True and P._lift_ok is True and P._lift_retry_at == 0.0


def _lift_warnings(caplog):
    return [r for r in caplog.records if "lift" in r.getMessage()]


def test_after_an_unanswered_lift_the_watch_asks_again_within_its_short_backoff(monkeypatch, away, caplog):
    # A game makes the engine slow, so lifts time out, but the cap in flight
    # stays protected: the watch asks again after _WATCH_RETRY_S, not on every
    # look and not after the 60 s cooldown, which only stops new caps.
    answers, asked, looks = [None, None, True], [], []
    monkeypatch.setattr(P, "check_other_gpu", lambda: looks.append(1) or False)   # a game is running
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)
    monkeypatch.setattr(P, "_WATCH_RETRY_S", 0.03)

    async def lift():
        asked.append(answers[len(asked)])
        return asked[-1]

    async def run():
        with P.power_tier(P.BACKGROUND):
            started = P.call_started(10, lift)
        await asyncio.sleep(0.3)
        P.call_ended(started)

    with caplog.at_level("WARNING", logger=P.logger.name):
        asyncio.run(run())
    assert asked == [None, None, True] and P._lifted is True    # asked again until it went through
    assert len(looks) == 3                                      # no look during the backoff
    assert len(_lift_warnings(caplog)) == 1                     # one warning per cooldown
    assert not P._lift_askable()                                # no new caps for the cooldown


def test_after_a_refused_lift_the_watch_neither_looks_nor_asks_again(monkeypatch, away, caplog):
    looks, asked = [], []
    monkeypatch.setattr(P, "check_other_gpu", lambda: looks.append(1) or False)   # a game is running
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)

    async def run():
        with P.power_tier(P.BACKGROUND):
            started = P.call_started(10, _lifter(asked, False))
        await asyncio.sleep(0.1)                                # about ten watch rounds
        P.call_ended(started)

    with caplog.at_level("WARNING", logger=P.logger.name):
        asyncio.run(run())
    assert asked == [False] and len(looks) == 1                 # one look, one ask, then quiet
    assert len(_lift_warnings(caplog)) == 1
    assert P._lift_ok is False and not P._lift_askable()        # no more caps this run


def test_a_failed_lift_for_a_new_call_does_not_quiet_the_watch(monkeypatch, away):
    # Research starts beside a 10 W call and its lift times out. That stops new
    # caps for a minute, but the cap already in flight is still watched.
    gpu = {"quiet": True}
    answers, asked = [None, True], []
    monkeypatch.setattr(P, "check_other_gpu", lambda: gpu["quiet"])
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)

    async def lift():
        asked.append(answers[len(asked)])
        return asked[-1]

    async def run():
        with P.power_tier(P.BACKGROUND):
            background = P.call_started(10, lift)
        research = P.call_started(None, _lifter([]))            # lifts the 10 W cap: times out
        await asyncio.sleep(0.03)
        assert asked == [None] and not P._lift_askable()        # no new caps for the cooldown
        gpu["quiet"] = False                                    # then a game starts
        await asyncio.sleep(0.05)
        assert asked == [None, True] and P._lifted is True      # the watch still lifted it
        P.call_ended(research)
        P.call_ended(background)

    asyncio.run(run())


def _closed(log):
    async def lift():
        log.append("closed")
        raise P.LiftClientClosed
    return lift


def test_closed_clients_are_not_tries_so_an_older_open_one_is_asked(away):
    # The two newest capped calls just ended: their clients closed before the
    # calls left _capped. They ask nothing, so they don't use up the tries.
    asked = []

    async def run():
        with P.power_tier(P.BACKGROUND):
            calls = [P.call_started(10, _lifter(asked, True)), P.call_started(10, _lifter(asked, True)),
                     P.call_started(10, _closed(asked)), P.call_started(10, _closed(asked))]
        await P._lift_now(watch=True)
        for started in calls:
            P.call_ended(started)

    asyncio.run(run())
    assert asked == ["closed", "closed", True] and P._lifted is True and P._lift_ok is True


def test_a_lift_with_every_client_closed_asks_and_notes_nothing(away, caplog):
    asked = []

    async def run():
        with P.power_tier(P.BACKGROUND):
            started = P.call_started(10, _closed(asked))
        await P._lift_now(watch=True)
        P.call_ended(started)

    with caplog.at_level("WARNING", logger=P.logger.name):
        asyncio.run(run())
    assert asked == ["closed"] and P._lift_ok is None and P._lift_retry_at == 0.0
    assert P._watch_retry_at == 0.0 and not _lift_warnings(caplog)


@pytest.mark.parametrize("status, answer", [(200, True), (404, False), (405, False),
                                            (429, None), (500, None), (503, None)])
def test_only_a_missing_lift_route_is_a_refusal(status, answer):
    # A busy engine (429, 5xx) is asked again; only one without the route
    # (an older bundled engine) stops the asks for the run.
    async def run():
        client, _, lifts = _engine(lift=status)
        try:
            return await _lift_gpu_power(client), lifts
        finally:
            await client._client.aclose()

    assert asyncio.run(run()) == (answer, [0])


def test_a_busy_engine_is_asked_again_until_it_says_the_route_is_missing(monkeypatch, away):
    # Through the real client: a 503, then a 429, each backed off and asked
    # again, then a 404, which stops the watch for the run.
    statuses, seen = [503, 429, 404], []
    monkeypatch.setattr(P, "check_other_gpu", lambda: False)    # a game is running
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)
    monkeypatch.setattr(P, "_WATCH_RETRY_S", 0.02)

    async def reply(request):
        seen.append(statuses[min(len(seen), 2)])
        return httpx.Response(seen[-1], json={})

    async def run():
        client, _ = _client(managed=True)
        client._client = httpx.AsyncClient(base_url="http://test.invalid", transport=httpx.MockTransport(reply))
        try:
            with P.power_tier(P.BACKGROUND):
                started = P.call_started(10, lambda: _lift_gpu_power(client))
            await asyncio.sleep(0.3)
            P.call_ended(started)
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert seen == [503, 429, 404] and P._lift_ok is False and P._lifted is False


def test_after_every_client_closed_the_watch_asks_one_that_opens(monkeypatch, away):
    asked = []
    monkeypatch.setattr(P, "check_other_gpu", lambda: False)    # a game is running
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)

    async def run():
        with P.power_tier(P.BACKGROUND):
            closed = P.call_started(10, _closed(asked))
            await asyncio.sleep(0.05)
            assert set(asked) == {"closed"} and P._watch_retry_at == 0.0   # nothing asked, no backoff
            fresh = P.call_started(10, _lifter(asked, True))
        await asyncio.sleep(0.05)
        assert asked[-1] is True and P._lifted is True
        P.call_ended(fresh)
        P.call_ended(closed)

    asyncio.run(run())


def test_a_closed_client_asks_the_engine_nothing():
    async def run():
        client, _, lifts = _engine()
        await client._client.aclose()
        with pytest.raises(P.LiftClientClosed):
            await _lift_gpu_power(client)
        return lifts

    assert asyncio.run(run()) == []


def test_a_lift_asks_at_most_two_clients(away):
    asked = []

    async def run():
        with P.power_tier(P.BACKGROUND):
            calls = [P.call_started(10, _lifter(asked, None)) for _ in range(4)]
        await P._lift_now(watch=True)
        for started in calls:
            P.call_ended(started)

    asyncio.run(run())
    assert asked == [None, None] and P._lift_ok is None and P._lift_retry_at > 0


def test_a_lift_that_timed_out_asks_no_other_client(monkeypatch, away):
    # The engine is the same behind every client: after a timeout another ask
    # would only wait as long again.
    monkeypatch.setattr(P, "_LIFT_TIMEOUT_S", 0.02)
    asked = []

    async def timed_out():
        await asyncio.sleep(0.03)
        asked.append(None)
        return None

    async def run():
        with P.power_tier(P.BACKGROUND):
            older = P.call_started(10, _lifter(asked, True))
            newest = P.call_started(10, timed_out)
        await P._lift_now(watch=True)
        P.call_ended(newest)
        P.call_ended(older)

    asyncio.run(run())
    assert asked == [None] and P._lifted is False and P._lift_retry_at > 0


def test_a_watched_call_on_another_thread_lifts_through_the_service_loop(battery):
    import threading
    lifted = []

    async def run():
        asked = asyncio.Event()

        async def lift():
            lifted.append(True)
            asked.set()
            return True

        with P.power_tier(P.BACKGROUND):
            capped = P.call_started(10, lift)                   # on the service loop
        started = {}
        worker = threading.Thread(target=lambda: started.update(token=P.call_started(None, _lifter([]))))
        worker.start()
        await asyncio.to_thread(worker.join)
        await asyncio.wait_for(asked.wait(), 5)
        assert lifted == [True]                                 # via call_soon_threadsafe
        P.call_ended(started["token"])
        P.call_ended(capped)
        assert P._uncapped_watched == 0 and not P._capped and not P.watched_running()

    asyncio.run(run())


def test_no_chat_call_is_capped_while_a_call_someone_waits_on_runs_uncapped(battery):
    async def run():
        research = P.call_started(None, _lifter([]))     # research, voice transcription
        with _turn(), P.power_tier(P.CHAT):
            assert P.payload_fields() == {}
        P.call_ended(research)
        with _turn(), P.power_tier(P.CHAT):
            assert P.payload_fields() == {"gpu_power_limit_w": 15}

    asyncio.run(run())


def test_calls_on_another_threads_event_loop_are_never_capped(monkeypatch, battery):
    # The behavior phraser runs asyncio.run in a worker thread with a client
    # that closes when it ends: none of its calls is capped or watched, and the
    # service loop's capped call keeps lifting through its own client.
    import threading
    gpu = {"quiet": True}
    monkeypatch.setattr(P, "check_other_gpu", lambda: gpu["quiet"])
    monkeypatch.setattr(P, "WATCH_EVERY_S", 0.01)
    seen = {}

    def phraser():
        async def go():
            client, bodies, lifts = _engine()
            try:
                with P.power_tier(P.BACKGROUND):
                    await client.chat("m", _HI)
            finally:
                await client._client.aclose()
            seen.update(bodies=bodies, lifts=lifts, watch=P._watch_task)
        asyncio.run(go())

    async def run():
        release = asyncio.Event()
        client, bodies, lifts = _engine(release)
        try:
            with P.power_tier(P.BACKGROUND):
                call = asyncio.ensure_future(client.chat("m", _HI))
                await asyncio.sleep(0.03)
                assert bodies[0]["gpu_power_limit_w"] == 10
                worker = threading.Thread(target=phraser)
                worker.start()
                await asyncio.to_thread(worker.join)
                gpu["quiet"] = False                      # a game starts
                await asyncio.sleep(0.05)
                assert lifts == [0, 1]                    # lifted through the open client
                release.set()
                await call
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert "gpu_power_limit_w" not in seen["bodies"][0] and seen["lifts"] == []
    assert P._lift_ok is True and not P._capped


def test_an_engine_that_cant_lift_is_never_capped(away):
    async def run():
        client, bodies, lifts = _engine(lift=404)         # a bundled engine from before the route
        try:
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", _HI)
                await client.chat("m", _HI)
        finally:
            await client._client.aclose()
        return bodies, lifts

    bodies, lifts = asyncio.run(run())
    assert [b.get("gpu_power_limit_w") for b in bodies] == [None, None]
    assert lifts == [0] and P._lift_ok is False
    assert P.refresh_away(P.AWAY_AFTER_S + 1) is False    # and nothing is probed any more


def test_an_unreachable_engine_leaves_caps_off_for_a_minute_then_is_asked_again(monkeypatch, away):
    clock = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])

    async def run(lift):
        client, bodies, _ = _engine(lift=lift)
        try:
            with P.power_tier(P.BACKGROUND):
                await client.chat("m", _HI)
        finally:
            await client._client.aclose()
        return bodies[0].get("gpu_power_limit_w")

    assert asyncio.run(run("unreachable")) is None and P._lift_ok is None
    clock[0] += 30
    assert asyncio.run(run(200)) is None                   # still within the minute: not asked
    clock[0] += P._LIFT_RETRY_S
    P.refresh_away(P.AWAY_AFTER_S + 1)
    assert asyncio.run(run(200)) == 10 and P._lift_ok is True


@pytest.mark.parametrize("answer, ok", [(False, False), (None, None)])
def test_a_failed_lift_mid_request_stops_capping(away, answer, ok):
    async def run():
        with P.power_tier(P.BACKGROUND):
            started = P.call_started(10, _lifter([], answer))
        await P._lift_now(watch=True)
        P.call_ended(started)

    asyncio.run(run())
    assert P._lift_ok is ok and _caps()[P.BACKGROUND] is None   # refused: the run; unreachable: a minute


def test_the_lift_request_has_a_short_timeout():
    from service.inference.omlx_client import _lift_gpu_power
    sent = {}

    class Engine:
        async def post(self, path, **kwargs):
            sent.update(path=path, **kwargs)
            return httpx.Response(200)

    assert asyncio.run(_lift_gpu_power(SimpleNamespace(_client=Engine()))) is True
    assert sent == {"path": "/v1/wisp/gpu-power/lift", "timeout": P._LIFT_TIMEOUT_S} and P._LIFT_TIMEOUT_S <= 2


def test_the_engine_overlay_adds_the_lift_route():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "build-support" / "engine_guard"
    patch = json.loads((root / "server.py.patch.json").read_text())
    route = [e for e in patch["edits"] if '"/v1/wisp/gpu-power/lift"' in e["new"]]
    assert len(route) == 1 and route[0]["count"] == 1
    assert "Depends(verify_api_key)" in route[0]["new"]
    assert "await limiter.close()" in route[0]["new"]


def test_no_model_level_cap_is_ever_seeded():
    from service.setup import catalog

    for model in catalog.PRIMARY_MODELS:
        assert "gpu_power_limit_w" not in model.engine_profile, model.id


# --- each path carries its tier ---------------------------------------------------

def _seen():
    return (P.current_tier(), P.payload_fields())


@pytest.mark.parametrize("where, cap", [("away", 10), ("battery", 15)])
def test_the_gist_call_runs_at_the_brief_tier(monkeypatch, request, where, cap):
    request.getfixturevalue(where)
    import service.config as config
    from service.assistant import brief, mail_gists

    seen = []

    class Client:
        async def loaded_models(self):
            return ["fast"]

        def keep_warm(self):
            return set()

        async def chat(self, model, messages, **kwargs):
            seen.append(_seen())
            return {"choices": [{"finish_reason": "stop", "message": {"content": "gist"}}]}

    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(config, "no_thinking_capable", lambda: ["fast"])
    monkeypatch.setattr(config, "thinks_explicitly", lambda name: False)
    monkeypatch.setattr(brief, "_c", lambda: Client())
    assert asyncio.run(mail_gists._resident_reply("s", "m", lambda: False)) == "gist"
    assert seen == [(P.BRIEF, {"gpu_power_limit_w": cap})]


def test_the_scheduled_daily_summary_has_no_tier_because_it_calls_no_model(monkeypatch, away):
    import inspect
    from datetime import date

    from service.assistant import brief, scheduler

    seen = []

    async def fake_brief(part, **kwargs):
        seen.append(_seen())
        return None

    monkeypatch.setattr(brief, "run_scheduled_brief", fake_brief)
    monkeypatch.setattr(scheduler.idle, "foreground_busy", lambda: False)
    asyncio.run(scheduler._run_daily_brief(date(2026, 10, 28), "morning"))
    assert seen == [(None, {})]
    # The brief renders from caches; its model-written parts are the gists.
    assert "NO MODEL" in inspect.getsource(brief).split('"""')[1]


def test_the_behavior_tick_runs_at_the_background_tier(monkeypatch, away):
    import service.behavior_switch as switch
    from service.assistant import behavior_runtime, scheduler

    seen = []

    async def fake_tick(store, hub, **kwargs):
        seen.append(_seen())
        return []

    monkeypatch.setattr(switch, "enabled", lambda: True)
    monkeypatch.setattr(behavior_runtime, "tick", fake_tick)
    monkeypatch.setattr(scheduler, "_behavior_task", None)

    async def run():
        await scheduler._start_behavior_tick()

    asyncio.run(run())
    assert seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


class _Recorder:
    """A fake client that records the tier and cap active when chat() runs."""

    managed = True
    base_url = "local"

    def __init__(self):
        self.seen = []

    async def loaded_models(self):
        return ["fast"]

    async def models(self):
        return ["fast"]

    async def ensure_only(self, model, **kwargs):
        return None

    async def chat(self, model, messages, **kwargs):
        self.seen.append(_seen())
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}


def test_the_mail_memory_walk_runs_at_the_background_tier(monkeypatch, away):
    import service.config as config
    from service import idle
    from service.assistant import brief
    from service.mail_memory import runner

    client = _Recorder()

    async def ready():
        return None

    monkeypatch.setattr(runner, "_stand_down", ready)
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 1)
    assert asyncio.run(runner._reply([{"role": "user", "content": "mail"}])) == "ok"
    assert client.seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


def test_the_behavior_phraser_runs_at_the_background_tier(monkeypatch, away):
    from service import idle
    from service.assistant.behavior_phraser import WispPhraser
    from service.config.endpoints import role_target

    client = _Recorder()
    resident = role_target("fast").model

    async def loaded():
        return [resident]

    client.loaded_models = loaded
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    phraser = WispPhraser(client_factory=lambda target: client)
    asyncio.run(phraser._complete_async("s", "u", max_tokens=20, temperature=0))
    assert client.seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]


def test_research_the_user_started_is_never_capped(monkeypatch, away):
    from service import idle
    from service.research import orchestrator as O

    client = _Recorder()
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(O, "_research_model", lambda: "fast")
    monkeypatch.setattr(O, "_select_research_model", lambda configured, available, explicit=False: "fast")
    monkeypatch.setattr(O, "models_config", lambda: {})

    async def run():
        manager = O.ResearchManager.__new__(O.ResearchManager)
        manager._model_lock = asyncio.Lock()
        manager._resolved_model = None
        return await manager._call_on_endpoint(client, "s", "u")

    assert asyncio.run(run()) == "ok"
    assert client.seen == [(None, {})]


def _wav_clip(seconds=0.5, rate=16_000):
    import base64
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(b"\0\0" * int(seconds * rate))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_voice_transcription_is_never_capped_and_holds_chat_caps_off(monkeypatch, battery):
    # On battery, with the foreground gate the transcription itself holds, a
    # chat call would run at 15 W. The transcription goes out uncapped through
    # the real client, and while it runs no chat call is capped either.
    from service import voice
    during = []

    def hook():
        with P.power_tier(P.CHAT):
            during.append((P._uncapped_watched, P.payload_fields()))

    client, bodies = _client(managed=True, reply_hook=hook)

    async def status():
        return {"models": [{"id": "m", "model_type": "vlm", "model_path": "/models/m"}]}

    client.status = status
    client.activity_key = lambda model: f"test:{model}"
    monkeypatch.setattr(voice, "checkpoint_has_audio", lambda path: True)

    async def run():
        try:
            text = await voice.transcribe(_wav_clip(), client=client, model="m")
            with P.power_tier(P.CHAT):
                after = P.payload_fields()               # the gate is still handed off to the turn
            return text, after
        finally:
            await client._client.aclose()

    assert asyncio.run(run()) == ("ok", {"gpu_power_limit_w": 15})
    assert len(bodies) == 1 and "gpu_power_limit_w" not in bodies[0]
    assert during == [(1, {})]                           # counted as a call someone waits on


def test_setup_and_model_qualification_calls_are_never_capped():
    # Setup's engine probes read /v1/models with their own httpx client, so
    # there's no request body to cap; qualifying a local model chats through an
    # OMLXClient without a tier. An untiered call gets no cap even while away
    # on battery, or on battery in a turn, when the tiers would.
    import inspect

    from service.inference import qualify
    from service.setup import engines

    assert "power_tier" not in inspect.getsource(qualify) and "power_tier" not in inspect.getsource(engines)
    client, bodies = _client(managed=True)

    async def run():
        try:
            P.refresh_away(P.AWAY_AFTER_S + 1, on_battery=True)
            with P.power_tier(P.BACKGROUND):
                assert P.payload_fields() == {"gpu_power_limit_w": 10}
            await client.chat("m", _HI)
            P.refresh_away(5, on_battery=True)
            with _turn():
                with P.power_tier(P.CHAT):
                    assert P.payload_fields() == {"gpu_power_limit_w": 15}
                await client.chat("m", _HI)
        finally:
            await client._client.aclose()

    asyncio.run(run())
    assert [body.get("gpu_power_limit_w") for body in bodies] == [None, None]


def test_memory_capture_wraps_its_extraction_call_in_the_background_tier():
    import inspect

    from service.memory.capture import MemoryWorker

    source = inspect.getsource(MemoryWorker.process)
    tier = source.index("with power_tier(BACKGROUND):")
    assert tier < source.index("await client.chat(") < tier + 200


def test_a_chat_turn_runs_at_the_chat_tier():
    import inspect
    import service.main as main
    source = inspect.getsource(main)
    runner = source[source.index("    async def runner():"):]
    head = runner[:runner.index("await runner_body()")]
    assert "power_tier(CHAT)" in head


def test_image_and_document_questions_run_at_the_chat_tier():
    import inspect
    from service import documents, vision
    answer = inspect.getsource(vision.answer)
    assert answer.index("with power_tier(CHAT):") < answer.index("await _answer(")
    text = inspect.getsource(documents.answer_text)
    assert text.index("with power_tier(CHAT):") < text.index("client.chat(")


@pytest.mark.parametrize("on_battery", [True, False])
def test_the_mail_memory_walk_runs_at_10_w_on_battery_and_plugged_in(monkeypatch, on_battery):
    P.refresh_away(5, on_battery=on_battery)
    import service.config as config
    from service import idle
    from service.assistant import brief
    from service.mail_memory import runner

    client = _Recorder()

    async def ready():
        return None

    monkeypatch.setattr(runner, "_stand_down", ready)
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 1)
    assert asyncio.run(runner._reply([{"role": "user", "content": "mail"}])) == "ok"
    assert client.seen == [(P.BACKGROUND, {"gpu_power_limit_w": 10})]
