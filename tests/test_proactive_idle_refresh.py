"""The hourly idle refresh: the whole Brief regenerates about once an hour,
only while the machine sits idle, only when something it reads changed (or
the day rolled over), only when someone has been back since the last one, and
never on a failed idle probe.

No live model, no native sources, no writes outside a scratch WISP_HOME.
"""
import asyncio
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="wisp-proactive-idle-tests-")
os.environ.setdefault("WISP_HOME", _scratch.name)
os.environ.setdefault("MOE_DIR", _scratch.name)

import pytest

from service.assistant import mail_gists
from service.proactive import cycle
from service.proactive.store import ProactiveStore

NOW = 1_800_000_000.0
AWAY = lambda: 600.0          # noqa: E731 — ten minutes without input
IN_USE = lambda: 3.0          # noqa: E731


def _broken_probe():
    raise OSError("ioreg failed")


class Recorder:
    def __init__(self, status="done"):
        self.status, self.cycles, self.gists = status, [], 0

    async def cycle(self, **kwargs):
        self.cycles.append(kwargs)
        return {"ok": self.status == "done", "status": self.status}

    async def refresh_gists(self):
        self.gists += 1


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "_idle_retry_at", 0.0)
    monkeypatch.setattr(cycle, "_foreground", lambda: False)
    return ProactiveStore(tmp_path)


def run(rec, store, probe, now=NOW):
    return asyncio.run(cycle.idle_refresh(now=now, probe=probe, store=store, cycle=rec.cycle,
                                          gists=rec.refresh_gists))


def test_due_and_idle_forces_a_full_cycle_then_the_gists(store):
    rec = Recorder()
    assert run(rec, store, AWAY) == "ran"
    assert rec.cycles == [{"force": True}]
    assert rec.gists == 1
    assert store.state()["idle_refresh_at"] == NOW


def test_due_but_in_use_waits_for_the_first_idle_tick(store):
    rec = Recorder()
    assert run(rec, store, IN_USE) == "active"
    # Three seconds of quiet: the user can't be away for another 597 s, so the
    # probe doesn't run before then (it used to run every scheduler tick).
    assert run(rec, store, _broken_probe, now=NOW + 30) == "not_due"
    assert rec.cycles == [] and rec.gists == 0
    assert run(rec, store, AWAY, now=NOW + cycle.IDLE_AFTER_S - 3) == "ran"
    assert rec.cycles == [{"force": True}] and rec.gists == 1


def test_away_means_ten_minutes_the_same_as_the_background_gpu_cap(store):
    from service.inference import power_tier
    assert cycle.IDLE_AFTER_S == power_tier.AWAY_AFTER_S == 600
    rec = Recorder()
    assert run(rec, store, lambda: 5 * 60.0) == "active"               # the old 5-minute threshold
    assert rec.cycles == []
    assert run(rec, store, AWAY, now=NOW + 5 * 60) == "ran"


def test_a_failed_probe_backs_off_one_tick_gap(store):
    rec = Recorder()
    assert run(rec, store, lambda: None) == "active"
    assert run(rec, store, AWAY, now=NOW + cycle.TICK_GAP_S - 1) == "not_due"
    assert run(rec, store, AWAY, now=NOW + cycle.TICK_GAP_S) == "ran"


def _never_probed():
    raise AssertionError("the idle probe ran")


def test_unchanged_inputs_on_the_same_day_skip_it_without_a_probe(store):
    store.save_state(timezone="UTC", judged={"me@example.test": {"key": "k1"}})
    rec = Recorder()
    assert run(rec, store, AWAY) == "ran"
    later = NOW + cycle.IDLE_REFRESH_S
    assert run(rec, store, _never_probed, now=later) == "unchanged"
    assert len(rec.cycles) == 1 and rec.gists == 1
    # Counted as a refresh: nothing more until the next hour.
    assert store.state()["idle_refresh_at"] == later
    assert run(rec, store, _never_probed, now=later + 60) == "not_due"


@pytest.mark.parametrize("change", ["judged", "gists"])
def test_changed_inputs_force_it(store, monkeypatch, change):
    monkeypatch.setattr(mail_gists, "_cache", {"inbox": {"gist": "g", "at": NOW, "key": "old"}})
    store.save_state(timezone="UTC", judged={"me@example.test": {"key": "k1"}})
    rec = Recorder()
    assert run(rec, store, AWAY) == "ran"
    if change == "judged":
        store.save_state(judged={"me@example.test": {"key": "k2"}})      # a new email survived triage
    else:
        mail_gists._cache["inbox"]["key"] = "new"                        # the inbox overview's mail changed
    later = NOW + cycle.IDLE_REFRESH_S
    assert run(rec, store, AWAY, now=later) == "ran"
    assert rec.cycles == [{"force": True}, {"force": True}] and rec.gists == 2


def test_the_day_rolling_over_forces_it(store):
    store.save_state(timezone="UTC", judged={"me@example.test": {"key": "k1"}})
    midnight = (NOW // 86400 + 1) * 86400
    rec = Recorder()
    first = midnight - 10 * 60 - cycle.IDLE_REFRESH_S
    assert run(rec, store, AWAY, now=first) == "ran"
    assert run(rec, store, _never_probed, now=midnight - 10 * 60) == "unchanged"
    assert run(rec, store, AWAY, now=midnight - 10 * 60 + cycle.IDLE_REFRESH_S) == "ran"   # a new day, nothing else new
    assert len(rec.cycles) == 2


def test_nobody_back_since_the_last_refresh_skips_it(store):
    store.save_state(timezone="UTC", judged={"me@example.test": {"key": "k1"}})
    rec = Recorder()
    assert run(rec, store, AWAY) == "ran"
    store.save_state(judged={"me@example.test": {"key": "k2"}})
    later = NOW + cycle.IDLE_REFRESH_S
    untouched = lambda: cycle.IDLE_REFRESH_S + 60.0                      # noqa: E731 — no input since
    assert run(rec, store, untouched, now=later) == "unseen"
    assert len(rec.cycles) == 1 and rec.gists == 1
    assert store.state()["idle_refresh_at"] == NOW                       # not counted as a refresh
    # Coming back and leaving again takes at least IDLE_AFTER_S: no probe before then.
    assert run(rec, store, _broken_probe, now=later + 60) == "not_due"
    back_and_away = lambda: float(cycle.IDLE_AFTER_S)                    # noqa: E731
    assert run(rec, store, back_and_away, now=later + cycle.IDLE_AFTER_S) == "ran"


def test_not_due_runs_nothing_even_when_idle(store):
    rec = Recorder()
    store.save_state(idle_refresh_at=NOW - 20 * 60)
    assert run(rec, store, AWAY) == "not_due"
    assert run(rec, store, AWAY, now=NOW + 30 * 60) == "not_due"      # 50 minutes: still under 55
    assert rec.cycles == []
    assert run(rec, store, AWAY, now=NOW + 40 * 60) == "ran"           # an hour on


def test_the_last_refresh_survives_a_restart(tmp_path, store):
    rec = Recorder()
    assert run(rec, store, AWAY) == "ran"
    reopened = ProactiveStore(tmp_path)
    assert run(rec, reopened, AWAY, now=NOW + 60) == "not_due"
    assert len(rec.cycles) == 1


@pytest.mark.parametrize("probe", [_broken_probe, lambda: None, lambda: "soon"])
def test_a_failed_idle_probe_never_runs(store, probe):
    rec = Recorder()
    assert run(rec, store, probe) == "active"
    assert rec.cycles == [] and rec.gists == 0


def test_a_foreground_turn_blocks_it(store, monkeypatch):
    monkeypatch.setattr(cycle, "_foreground", lambda: True)
    rec = Recorder()
    assert run(rec, store, AWAY) == "active"
    assert rec.cycles == []


def test_a_yielded_cycle_is_not_recorded_and_retries_later(store):
    rec = Recorder(status="yielded")
    assert run(rec, store, AWAY) == "yielded"
    assert rec.gists == 0 and "idle_refresh_at" not in store.state()
    rec.status = "done"
    assert run(rec, store, AWAY, now=NOW + 30) == "not_due"            # a short backoff, not every tick
    assert run(rec, store, AWAY, now=NOW + cycle.TICK_GAP_S) == "ran"


def test_hid_idle_seconds_reads_nanoseconds(monkeypatch):
    import subprocess

    class Done:
        stdout = '    | |   "HIDIdleTime" = 361000000000\n'
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert cycle.hid_idle_seconds() == pytest.approx(361.0)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ioreg", 2)
    monkeypatch.setattr(subprocess, "run", boom)
    assert cycle.hid_idle_seconds() is None
    assert cycle.user_away(cycle.hid_idle_seconds) is False


def test_mail_gists_refresh_all_rewrites_fresh_overviews(monkeypatch):
    lines = {"summary": ["Sam asked about coffee"], "inbox": ["Sam: coffee chat"], "texts": ["Mom: call"]}
    fresh = {k: {"gist": "old", "at": NOW - 60, "key": mail_gists._key(v)} for k, v in lines.items()}
    monkeypatch.setattr(mail_gists, "_cache", fresh)
    monkeypatch.setattr(mail_gists, "_save", lambda: None)
    monkeypatch.setattr(mail_gists, "_task", None)
    monkeypatch.setattr(mail_gists, "_attempts", {})
    monkeypatch.setattr(mail_gists, "_retry_at", {})
    monkeypatch.setattr(mail_gists, "_remote_background_paused", lambda: False)
    monkeypatch.setattr(mail_gists, "recent_mail", lambda now: [])
    monkeypatch.setattr(mail_gists, "recent_texts", lambda now: [])
    monkeypatch.setattr(mail_gists, "summary_lines", lambda rows: lines["summary"])
    monkeypatch.setattr(mail_gists, "mail_lines", lambda rows: lines["inbox"])
    monkeypatch.setattr(mail_gists, "texts_lines", lambda c: lines["texts"])
    for name in ("summary_ok", "mail_gist_ok", "texts_gist_ok"):
        monkeypatch.setattr(mail_gists, name, lambda *a: True)
    # Unforced, every overview is fresh: nothing is due.
    assert mail_gists._jobs(NOW) == []
    calls = []

    async def synth(system, material):
        calls.append(material)
        return "A new overview of the day's mail and texts for you."

    written = asyncio.run(mail_gists.refresh_all(NOW, synth=synth, abort=lambda: False))
    assert written == 3 and len(calls) == 3
    assert all(fresh[k]["gist"] != "old" for k in lines)
    assert mail_gists._task is None


def test_mail_gists_refresh_all_stands_down_on_a_remote_model(monkeypatch):
    monkeypatch.setattr(mail_gists, "_remote_background_paused", lambda: True)
    monkeypatch.setattr(mail_gists, "_task", None)

    async def synth(system, material):
        raise AssertionError("no call on a remote model")
    assert asyncio.run(mail_gists.refresh_all(NOW, synth=synth, abort=lambda: False)) == 0
