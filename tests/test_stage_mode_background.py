"""Demo mode (service/stage_mode.py) holds heavy SCHEDULED background model work, and
nothing else: mail gists' scheduled tick, the mail memory walk, conversation-memory
extraction, the behavior engine's tick and model calls, and the attachment reader.

The proactive cycle (scheduled and "Analyze now"), its judge call, the Brief's compose
and anything the person asks for are never held. No model, network, subprocess or real
sleep: every boundary is the fake the module's own tests use."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from service import idle, stage_mode

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def demo_off_after():
    stage_mode.set_active(False)
    yield
    stage_mode.set_active(False)


# --- mail gists: the scheduled tick waits; the cycle's own calls do not ----------------

@pytest.fixture
def gists(monkeypatch, tmp_path):
    from service.assistant import mail_gists as G
    from service.assistant.hub import hub
    from service import brief_pace
    monkeypatch.setattr(G, "_PATH", Path(tmp_path) / "cache" / "mail_gists.json")
    monkeypatch.setattr(G, "_cache", None)
    monkeypatch.setattr(G, "_next_call_at", 0.0)
    monkeypatch.setattr(G, "_attempts", {})
    monkeypatch.setattr(G, "_retry_at", {})
    monkeypatch.setattr(G, "_last_standdown", "")
    monkeypatch.setattr(G, "_task", None)
    monkeypatch.setattr(G, "recent_mail", lambda now: [])
    monkeypatch.setattr(G, "recent_texts", lambda now: [])
    monkeypatch.setattr(G, "summary_lines", lambda rows: [])
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    monkeypatch.setattr(brief_pace, "may_start", lambda kind, mono=None: True)
    monkeypatch.setattr(brief_pace, "between_builds", lambda *a, **k: False)
    return G


def test_gists_scheduled_tick_waits_in_demo_mode_and_runs_when_off(gists, capsys):
    G = gists
    stage_mode.set_active(True)
    assert G.schedule_tick() is None and G._task is None
    assert "paused while Demo mode is on" in capsys.readouterr().out
    assert G._next_call_at == 0.0                  # nothing pushed back: due again at the next tick

    async def off():
        stage_mode.set_active(False)
        task = G.schedule_tick()
        assert task is not None
        await task
    asyncio.run(off())


def test_gists_scheduled_call_yields_when_demo_mode_turns_on_mid_call(gists):
    G = gists
    assert G._scheduled_abort() is False
    stage_mode.set_active(True)
    assert G._scheduled_abort() is True            # the 0.5 s check in _resident_reply cancels


def test_the_cycles_gist_refresh_still_writes_in_demo_mode(gists, monkeypatch):
    G = gists
    from tests.test_mail_gists import row
    monkeypatch.setattr(G, "recent_mail", lambda now: [row(1, "Tech Week starts today"), row(2, "Invitation: Demo day")])
    stage_mode.set_active(True)
    seen = []

    async def synth(system, material):
        seen.append(material)
        return "Mostly events this week"
    assert asyncio.run(G.refresh_changed(synth=synth)) >= 1          # the Brief's own build path
    assert seen and G._load()["inbox"]["gist"] == "Mostly events this week"


# --- mail memory: the walk stands down with a plain reason, marks nothing --------------

@pytest.fixture
def walk(tmp_path, monkeypatch):
    from service.mail_memory import runner
    from service.mail_memory import store as store_mod
    from service.mail_memory.store import Store
    from service.assistant.hub import hub
    from service import brief_pace
    st = Store(tmp_path / "t.db")
    monkeypatch.setattr(store_mod, "_store", st)
    monkeypatch.setattr(runner, "store", lambda: st)
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 0)
    monkeypatch.setattr(idle, "last_used", lambda key: None)
    monkeypatch.setattr(runner, "_last_own_end", 0.0)
    monkeypatch.setattr(runner, "_probe", {})
    monkeypatch.setattr(runner, "_skip", None)
    monkeypatch.setattr(runner, "_next_at", 0.0)
    monkeypatch.setattr(runner, "_task", None)
    monkeypatch.setattr(runner, "mode", lambda path=None: "live")
    monkeypatch.setattr(runner, "_other_background_work", lambda: False)
    monkeypatch.setattr(brief_pace, "may_start", lambda kind, mono=None: True)
    monkeypatch.setattr(type(hub), "has_subscribers", property(lambda self: True))
    return runner, st


def test_mail_memory_walk_waits_in_demo_mode_and_runs_when_off(walk, monkeypatch):
    runner, st = walk
    started = []

    async def fake_run():
        started.append(1)
    monkeypatch.setattr(runner, "_run", fake_run)
    stage_mode.set_active(True)
    runner.schedule_tick()
    assert runner._task is None and started == []
    assert st.health()["last_skip"] == "demo_mode"
    assert runner.busy_reason("m", power=(True, 90, False)) == "demo_mode"
    assert runner._model_free("m") is False

    async def off():
        stage_mode.set_active(False)
        assert runner.busy_reason("m", power=(True, 90, False)) is None
        assert runner._model_free("m") is True
        runner.schedule_tick()
        assert runner._task is not None
        await runner._task
    asyncio.run(off())
    assert started == [1]


def test_mail_memory_call_in_flight_is_cancelled_when_demo_mode_turns_on(walk, monkeypatch):
    runner, _ = walk
    from service.assistant import brief
    from service import config
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast-model")
    monkeypatch.setattr(config, "thinking_kwargs", lambda model, on=False: {})
    monkeypatch.setattr(runner, "_model_key", lambda: "fast-model")
    cancelled = []

    class Client:
        async def loaded_models(self):
            return ["fast-model"]

        def keep_warm(self):
            return []

        async def chat(self, *a, **k):
            stage_mode.set_active(True)            # Demo mode turns on while the model runs
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(1)
    monkeypatch.setattr(brief, "_c", lambda: Client())

    async def main():
        return await asyncio.wait_for(runner._reply([{"role": "user", "content": "x"}]), 5)
    assert asyncio.run(main()) is None and cancelled == [1]


# --- conversation memory: extraction waits, the job stays queued -----------------------

@pytest.fixture
def memory_world(tmp_path):
    from service.memory.facts import FactStore
    from service.memory.queue import MemoryQueue
    from service.memory.store import SessionStore
    facts = FactStore(tmp_path / "facts.db")
    sessions = SessionStore(tmp_path / "sessions.db")
    yield facts, sessions, MemoryQueue(sessions)
    facts._db.close()
    sessions._db.close()


def _one_tick(monkeypatch):
    import service.memory.capture as capture
    ticks = 0

    async def one_tick(delay):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(capture.asyncio, "sleep", one_tick)


def test_memory_worker_waits_in_demo_mode_and_leaves_the_job_queued(memory_world, monkeypatch):
    from service.memory.capture import MemoryWorker
    facts, sessions, _ = memory_world
    sid = sessions.create_session()
    sessions.add_turn(sid, "user", "I prefer tea.")
    worker = MemoryWorker(sessions, facts)
    _one_tick(monkeypatch)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_in_flight", {})

    class Client:
        calls = 0

        async def loaded_models(self):
            self.calls += 1
            return []
    client = Client()
    stage_mode.set_active(True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run(client))
    row = dict(sessions._db.execute("SELECT * FROM memory_jobs").fetchone())
    assert client.calls == 0 and worker.state == "paused while Demo mode is on"
    assert row["status"] == "queued" and row["attempts"] == 0 and row["claim"] == ""

    stage_mode.set_active(False)                   # off: the worker goes on to its usual checks
    _one_tick(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run(client))
    assert client.calls == 1 and worker.state == "waiting for resident model"


def test_memory_extraction_in_flight_yields_when_demo_mode_turns_on(memory_world, monkeypatch):
    import service.memory.capture as capture
    from service.memory.capture import MemoryWorker
    facts, sessions, q = memory_world
    sid = sessions.create_session()
    sessions.add_turn(sid, "user", "I prefer tea.")
    worker = MemoryWorker(sessions, facts)
    actual_sleep = asyncio.sleep

    async def fast_tick(delay):
        await actual_sleep(0)
    monkeypatch.setattr(capture.asyncio, "sleep", fast_tick)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_in_flight", {})

    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Resident:
            async def loaded_models(self):
                from service.config import role_to_model
                return [role_to_model("agent")]

            async def chat(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        task = asyncio.create_task(worker.run(Resident()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            stage_mode.set_active(True)
            await asyncio.wait_for(cancelled.wait(), 1)
            await actual_sleep(0.02)
            assert q.stats()["jobs"].get("queued") == 1     # nothing lost: retried after
            assert facts.count() == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


# --- behavior engine: no new tick, and its model calls stand down ----------------------

def test_behavior_tick_waits_in_demo_mode_and_starts_when_off(monkeypatch):
    from service.assistant import behavior_runtime as br
    from service.assistant import scheduler
    monkeypatch.setenv("WISP_BEHAVIOR", "1")
    started = []

    async def fake_tick(store, hub, **kw):
        started.append(1)
        return []
    monkeypatch.setattr(br, "tick", fake_tick)
    monkeypatch.setattr(scheduler, "_behavior_task", None)

    async def scenario():
        stage_mode.set_active(True)
        assert scheduler._start_behavior_tick() is None
        stage_mode.set_active(False)
        task = scheduler._start_behavior_tick()
        assert task is not None
        await task
    asyncio.run(scenario())
    assert started == [1]


def test_behavior_phraser_makes_no_model_call_in_demo_mode(monkeypatch):
    from service.assistant.behavior_phraser import WispPhraser
    from tests.test_behavior_runtime import FakeClient, _target
    _target(monkeypatch)
    monkeypatch.setattr("service.inference.remote_policy.remote_only_profile", lambda: None)
    monkeypatch.setattr("service.config.no_thinking_kwargs", lambda model: {})
    client = FakeClient("You usually hit the gym around 6:30 — want me to protect that slot?")
    ph = WispPhraser(client_factory=lambda t: client)
    facts = {"habit": "gym", "time": "6:30"}
    stage_mode.set_active(True)
    assert ph.phrase("habit_conflict", facts) is None and client.calls == []
    stage_mode.set_active(False)
    assert ph.phrase("habit_conflict", facts) == client.content and len(client.calls) == 1


# --- attachment reader: waits, nothing is marked tried --------------------------------

async def test_attachment_reader_waits_in_demo_mode_and_reads_when_off(monkeypatch, tmp_path):
    from service import attachment_index as ai
    from tests.test_attachment_index import W, mrow
    w = W(monkeypatch, tmp_path)
    ai.set_enabled(True)
    w.rows = [mrow(1)]
    stage_mode.set_active(True)
    assert await w.tick() is False
    assert ai._last_skip == "demo_mode" and w.list_calls == [] and w.read == []
    stage_mode.set_active(False)
    assert await w.tick() is True
    assert w.read == ["report.pdf"]


# --- the demo itself is never held ------------------------------------------------------

def test_the_proactive_cycle_never_consults_demo_mode():
    """The cycle (scheduled and "Analyze now"), autoadd, autohold, followups and the
    Brief's compose must run while Demo mode is on: no proactive file may hold them."""
    files = sorted((ROOT / "service" / "proactive").glob("*.py"))
    assert any(f.name == "cycle.py" for f in files) and any(f.name == "api.py" for f in files)
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "stage_mode" not in text and "holds_background" not in text, f.name
    brief = (ROOT / "service" / "assistant" / "brief.py").read_text(encoding="utf-8")
    assert "stage_mode" not in brief and "holds_background" not in brief


def test_this_file_is_in_the_simulation_manifest():
    manifest = (ROOT / "scripts" / "run_simulation_qa.py").read_text()
    assert '"tests/test_stage_mode_background.py"' in manifest
