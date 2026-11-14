"""Cloud-everything mode (a remote-only profile plus remote_parity): the inbox
summary, mail facts and chat-memory capture run on the chosen cloud model.
Without parity, or on a local engine, they behave as before."""
from __future__ import annotations

import asyncio

import pytest

from service.assistant import mail_gists as G
from service.inference import remote_policy
from service.mail_memory import runner


class Client:
    managed = True

    def __init__(self, loaded=()):
        self.loaded = list(loaded)
        self.requests = []

    async def loaded_models(self):
        return self.loaded

    def keep_warm(self):
        return []

    def activity_key(self, model):
        return model

    async def chat(self, model, messages, **kw):
        self.requests.append(model)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Jordan wrote."}}]}


def cloud(monkeypatch, *, parity):
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: object())
    monkeypatch.setattr(remote_policy, "remote_parity", lambda: parity)


@pytest.fixture
def quiet(monkeypatch):
    from service import idle
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "in_flight", lambda key: 0)
    monkeypatch.setattr(runner, "_demo_hold", lambda: False)
    monkeypatch.setattr(runner, "_last_own_end", 0.0)


def _use(monkeypatch, client):
    from service.assistant import brief
    import service.config as config
    monkeypatch.setattr(brief, "_c", lambda: client)
    monkeypatch.setattr(config, "role_to_model", lambda role: "fast")
    return client


def test_summary_goes_to_the_cloud_model_only_with_parity(monkeypatch):
    client = _use(monkeypatch, Client(loaded=[]))     # nothing "loaded": the cloud has no residency
    cloud(monkeypatch, parity=True)
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) == "Jordan wrote."
    assert client.requests == ["fast"]
    off = _use(monkeypatch, Client(loaded=["fast"]))
    cloud(monkeypatch, parity=False)
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) is None
    assert off.requests == []


def test_summary_on_a_local_engine_is_unchanged(monkeypatch):
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(remote_policy, "remote_parity", lambda: True)   # parity alone changes nothing
    near = _use(monkeypatch, Client(loaded=["fast"]))
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) == "Jordan wrote."
    assert near.requests == ["fast"]
    far = Client(loaded=["fast"])
    far.managed = False
    _use(monkeypatch, far)
    assert asyncio.run(G._resident_reply("sys", "mail", lambda: False, local_only=True)) is None
    assert far.requests == []


async def test_mail_memory_reads_on_the_cloud_with_parity(monkeypatch, quiet):
    client = _use(monkeypatch, Client(loaded=[]))
    cloud(monkeypatch, parity=True)
    assert await runner._stand_down() is None
    assert await runner._reply([{"role": "user", "content": "x"}]) == "Jordan wrote."
    assert client.requests == ["fast"]


async def test_mail_memory_stands_down_without_parity(monkeypatch, quiet):
    client = _use(monkeypatch, Client(loaded=["fast"]))
    cloud(monkeypatch, parity=False)
    assert await runner._stand_down() == "remote_only"
    assert await runner._reply([{"role": "user", "content": "x"}]) is None
    assert client.requests == []


async def test_mail_memory_local_engine_is_unchanged(monkeypatch, quiet):
    client = _use(monkeypatch, Client(loaded=[]))
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(remote_policy, "remote_parity", lambda: True)
    assert await runner._stand_down() == "not_resident"
    assert client.requests == []


# --- chat-memory capture -----------------------------------------------------------

class _Q:
    def enabled(self): return True
    def has_ready(self): return True
    def next(self): return {"id": 1}
    def current(self, job): return True
    def finish(self, job, *a): self.finished = a


class _Facts:
    def setting(self, name, default=None): return False


def _worker(monkeypatch, models):
    from service import idle
    from service.memory.capture import MemoryWorker
    from service.config import endpoints
    monkeypatch.setattr(idle, "foreground_busy", lambda: False)
    monkeypatch.setattr(idle, "_in_flight", {})
    w = object.__new__(MemoryWorker)
    w.queue, w.facts, w.generation, w.state = _Q(), _Facts(), None, ""
    w.reconcile_deleted_sources = lambda: None
    w.connections = type("C", (), {"recover": lambda s: None})()
    w.queue.recover = lambda: None
    seen = []

    async def process(job, client, model):
        seen.append(model)
        raise asyncio.CancelledError   # end the loop after one job

    w.process = process
    sleeps = []

    async def fake_sleep(_):
        sleeps.append(1)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return w, seen


async def _drive(w, client):
    try:
        await w.run(client)
    except asyncio.CancelledError:
        pass


async def test_capture_uses_the_cloud_target_with_parity(monkeypatch):
    from service.config import endpoints
    from service.inference import remote_policy as rp
    target = type("T", (), {"endpoint": type("E", (), {"managed": False})(), "model": "cloud-m"})()
    monkeypatch.setattr(endpoints, "role_target", lambda role: target)
    cloud(monkeypatch, parity=True)
    w, seen = _worker(monkeypatch, None)
    await _drive(w, Client(loaded=[]))
    assert seen == ["cloud-m"]


async def test_capture_waits_without_parity(monkeypatch):
    from service.config import endpoints
    target = type("T", (), {"endpoint": type("E", (), {"managed": False})(), "model": "cloud-m"})()
    monkeypatch.setattr(endpoints, "role_target", lambda role: target)
    cloud(monkeypatch, parity=False)
    w, seen = _worker(monkeypatch, None)
    await _drive(w, Client(loaded=[]))
    assert seen == [] and w.state == "waiting for local investigation model"


async def test_capture_local_engine_needs_a_resident_model(monkeypatch):
    from service.config import endpoints
    target = type("T", (), {"endpoint": type("E", (), {"managed": True})(), "model": "local-m"})()
    monkeypatch.setattr(endpoints, "role_target", lambda role: target)
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)
    monkeypatch.setattr(remote_policy, "remote_parity", lambda: True)
    w, seen = _worker(monkeypatch, None)
    await _drive(w, Client(loaded=[]))
    assert seen == [] and w.state == "waiting for resident model"
    w, seen = _worker(monkeypatch, None)
    await _drive(w, Client(loaded=["local-m"]))
    assert seen == ["local-m"]
