"""PR130 backend A1/A2: revoked qualification evidence and fenced role mutations.

A1  A qualification result must stop being reusable the moment the provider is
    disconnected, a late result must never repopulate the cache, and a slower,
    older probe must never overwrite a newer one for the same app.
A2  Any configuration change that can alter the roles a pending provider
    connection will bind must supersede that connection.

Everything runs through the real ASGI app (httpx.ASGITransport), the real
qualification code and the real atomic YAML writer against a temporary file.
The provider is the in-process fake engine from test_local_provider_tools,
answered through a patched httpx transport: no socket, process, credential,
model, native listener or user data is touched. Completion order is controlled
with asyncio events, not sleeps.
"""
import asyncio

import httpx
import pytest

from service import config
from service.config.endpoints import role_target
from service.inference import qualify as q
from tests.test_local_provider_tools import FakeEngine, engine_factory  # noqa: F401  (fixture)

LOCAL_URL = "http://127.0.0.1:18888"


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


class Gate:
    """Wrap qualification so a test decides when each probe COMPLETES.

    The real probe runs against the engine as it is at call time; only its return
    is held. Also records whether the operation lock is ever held while awaiting.
    """

    def __init__(self, main, monkeypatch):
        self.main = main
        self.real = q.qualify
        self.hold: set[int] = set()
        self.calls: list[dict] = []
        self.lock_held_while_awaiting = False
        monkeypatch.setattr(q, "qualify", self._qualify)

    def _note_lock(self):
        if self.main._local_provider_operation_lock.locked():
            self.lock_held_while_awaiting = True

    async def _qualify(self, endpoint, model, context):
        index = len(self.calls)
        entry = {"started": asyncio.Event(), "release": asyncio.Event()}
        self.calls.append(entry)
        self._note_lock()
        report = await self.real(endpoint, model, context)
        self._note_lock()
        entry["started"].set()
        if index in self.hold:
            await entry["release"].wait()
            self._note_lock()
        return report

    async def started(self, index: int):
        while len(self.calls) <= index:
            await asyncio.sleep(0)
        await asyncio.wait_for(self.calls[index]["started"].wait(), 5)

    def release(self, index: int):
        self.calls[index]["release"].set()


class Harness:
    def __init__(self, main, gate, engine_factory):
        self.main, self.gate, self._factory = main, gate, engine_factory

    def engine(self, **knobs):
        engine, _ = self._factory(**knobs)
        return engine

    @staticmethod
    def body(engine, roles, context=16384):
        return {"base_url": f"http://127.0.0.1:{engine.port}", "api_prefix": "/v1",
                "model_id": "fake-model", "context_window": context, "roles": roles}

    async def call(self, method, path, body=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app),
                                     base_url=LOCAL_URL) as client:
            return await client.request(method, path, json=body)

    def connect(self, engine, roles, **kw):
        return self.call("POST", "/inference/local-provider", self.body(engine, roles, **kw))

    def test_probe(self, engine, roles=("agent",), **kw):
        return self.call("POST", "/inference/local-provider/qualify",
                         self.body(engine, list(roles), **kw))

    def settings(self):
        return self.call("GET", "/inference/local-provider")


@pytest.fixture
def h(tmp_path, monkeypatch, engine_factory):
    import service.main as main

    monkeypatch.setattr(config, "USER_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "OMLX_SETTINGS", tmp_path / "unused-settings.json")
    monkeypatch.setattr(config, "OMLX_MODEL_SETTINGS", tmp_path / "unused-model-settings.json")
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()
    config.set_roles({"general": "managed-general"})
    config.set_roles({"agent": "managed-agent", "coding": "managed-coding",
                      "reasoning": "managed-reasoning"})
    monkeypatch.setattr(main, "_qualification_cache", {})
    monkeypatch.setattr(main, "_local_provider_operation_generation", 0)
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)  # unrelated to the fence
    assert main.set_local_provider is config.set_local_provider
    gate = Gate(main, monkeypatch)
    harness = Harness(main, gate, engine_factory)
    yield harness
    assert not gate.lock_held_while_awaiting, "the operation lock was held across an await"
    config._user_overlay.cache_clear()
    config.models_config.cache_clear()


def _yaml():
    return config.USER_CONFIG.read_bytes()


# ============================================================ A1: revocation

def test_disconnect_discards_completed_evidence_so_reconnect_re_probes(h):
    engine = h.engine()

    async def scenario():
        assert (await h.test_probe(engine)).json()["qualified"] is True
        probes_after_test = len(h.gate.calls)
        assert (await h.call("DELETE", "/inference/local-provider")).status_code == 200
        engine.tools = "none"   # same app, same identity, but it can no longer call tools
        before = _yaml()
        reconnect = await h.connect(engine, ["agent"])
        return probes_after_test, before, reconnect, await h.settings()

    probes_after_test, before, reconnect, settings = run(scenario())
    assert len(h.gate.calls) == probes_after_test + 1, "Disconnect must force a fresh probe"
    assert reconnect.status_code == 400 and "can't run Wisp's tools" in reconnect.json()["detail"]
    assert _yaml() == before, "a failed reconnect must not change the saved configuration"
    assert settings.json()["tools_qualified"] is False and settings.json()["enabled"] is False
    assert role_target("agent").model == "managed-agent"
    assert role_target("agent").endpoint.name == "local"


def test_stale_connect_after_disconnect_is_409_and_leaves_no_reusable_evidence(h):
    engine = h.engine()

    async def scenario():
        h.gate.hold = {0}
        pending = asyncio.create_task(h.connect(engine, ["agent"]))
        await h.gate.started(0)
        assert (await h.call("DELETE", "/inference/local-provider")).status_code == 200
        h.gate.release(0)
        stale = await pending
        fresh = await h.connect(engine, ["agent"])
        return stale, fresh, await h.settings()

    stale, fresh, settings = run(scenario())
    assert stale.status_code == 409
    assert len(h.gate.calls) == 2, "the next Connect must qualify afresh, not reuse the stale result"
    assert fresh.status_code == 200 and settings.json()["tools_qualified"] is True


def test_stale_dry_run_after_disconnect_cannot_populate_the_cache(h):
    engine = h.engine()

    async def scenario():
        h.gate.hold = {0}
        pending = asyncio.create_task(h.test_probe(engine))
        await h.gate.started(0)
        assert (await h.call("DELETE", "/inference/local-provider")).status_code == 200
        h.gate.release(0)
        stale = await pending
        connect = await h.connect(engine, ["agent"])
        return stale, connect

    stale, connect = run(scenario())
    assert stale.status_code == 200   # the caller still gets its own result
    assert len(h.gate.calls) == 2, "a dry run finished after Disconnect must not be reusable"
    assert connect.status_code == 200


@pytest.mark.parametrize("older,newer", [("structured", "none"), ("none", "structured")])
def test_reverse_completion_keeps_only_the_newest_evidence(h, older, newer):
    engine = h.engine(tools=older)

    async def scenario():
        h.gate.hold = {0}
        first = asyncio.create_task(h.test_probe(engine))
        await h.gate.started(0)                 # the OLDER probe has measured, completion held
        engine.tools = newer
        second = await h.test_probe(engine)     # the NEWER probe completes first
        h.gate.release(0)
        first_response = await first            # the older one finishes last
        connect = await h.connect(engine, ["agent"])
        return first_response, second, connect

    first, second, connect = run(scenario())
    assert first.json()["qualified"] is (older == "structured")
    assert second.json()["qualified"] is (newer == "structured")
    assert len(h.gate.calls) == 2, "Connect reuses the newest evidence instead of probing again"
    assert (connect.status_code == 200) is (newer == "structured"), \
        "the older probe finishing last must not overwrite the newer evidence"


# ---------------------------------------------------------------- A1 controls

def test_same_epoch_test_then_connect_still_reuses_the_probe(h):
    engine = h.engine()

    async def scenario():
        await h.test_probe(engine)
        wire = len(engine.requests)
        connect = await h.connect(engine, ["agent"])
        return wire, connect

    wire, connect = run(scenario())
    assert connect.status_code == 200 and len(h.gate.calls) == 1
    assert len(engine.requests) == wire


def test_ttl_expiry_forces_a_new_probe(h, monkeypatch):
    engine = h.engine()

    async def scenario():
        await h.test_probe(engine)
        monkeypatch.setattr(h.main, "_QUALIFICATION_TTL_SECONDS", 0.0)
        return await h.connect(engine, ["agent"])

    assert run(scenario()).status_code == 200
    assert len(h.gate.calls) == 2


@pytest.mark.parametrize("change", ["context", "other_app"])
def test_identity_changes_force_a_new_probe(h, change):
    engine, other = h.engine(), h.engine()

    async def scenario():
        await h.test_probe(engine)
        if change == "context":
            return await h.connect(engine, ["agent"], context=8192)
        return await h.connect(other, ["agent"])

    assert run(scenario()).status_code == 200
    assert len(h.gate.calls) == 2


# ============================================================ A2: role fences

LOCAL_PENDING_CASES = [
    pytest.param(["agent"], "agent", "managed-agent-2", "agent", id="agent-to-managed-agent"),
    pytest.param(["agent"], "general", "managed-general-2", "agent", id="agent-to-managed-general-coupling"),
    pytest.param(["coding"], "coding", "managed-coding-2", "coding", id="coding-to-managed-coding"),
    pytest.param(["reasoning", "agent", "coding"], "coding", "managed-coding-3", "coding",
                 id="multi-role-then-coding"),
    pytest.param(["reasoning", "agent", "coding"], "agent", "managed-agent-3", "agent",
                 id="multi-role-then-agent"),
    pytest.param(["agent"], "reasoning", "managed-reasoning-2", "reasoning",
                 id="existing-reasoning-control"),
]


@pytest.mark.parametrize("pending_roles,role,model,checked", LOCAL_PENDING_CASES)
def test_pending_local_connect_is_superseded_by_a_managed_role_choice(
        h, pending_roles, role, model, checked):
    engine = h.engine()

    async def scenario():
        h.gate.hold = {0}
        pending = asyncio.create_task(h.connect(engine, pending_roles))
        await h.gate.started(0)
        changed = await h.call("POST", "/config", {"role": role, "model": model})
        after_choice = _yaml()
        h.gate.release(0)
        stale = await pending
        return changed, after_choice, stale, await h.settings()

    changed, after_choice, stale, settings = run(scenario())
    assert changed.status_code == 200
    assert stale.status_code == 409
    assert _yaml() == after_choice, "the stale connect must not rewrite the newer persisted binding"
    assert settings.json()["enabled"] is False and settings.json()["tools_qualified"] is False
    target = role_target(checked)
    assert target.endpoint.name == "local" and target.model == model


@pytest.mark.parametrize("role", ["fast", "router"])
def test_unrelated_role_updates_do_not_supersede_a_pending_connect(h, role):
    engine = h.engine()

    async def scenario():
        h.gate.hold = {0}
        pending = asyncio.create_task(h.connect(engine, ["agent"]))
        await h.gate.started(0)
        changed = await h.call("POST", "/config", {"role": role, "model": "managed-fast-2"})
        h.gate.release(0)
        return changed, await pending, await h.settings()

    changed, connect, settings = run(scenario())
    assert changed.status_code == 200 and connect.status_code == 200
    assert settings.json()["tools_qualified"] is True and settings.json()["roles"] == ["agent"]


@pytest.mark.parametrize("pending_roles,config_role", [
    pytest.param(["coding"], "coding", id="cloud-coding"),
    pytest.param(["coding", "research"], "research", id="cloud-multi-role-research"),
    pytest.param(["reasoning"], "reasoning", id="existing-cloud-reasoning-control"),
])
def test_pending_cloud_connect_is_superseded_by_a_managed_role_choice(
        monkeypatch, pending_roles, config_role):
    import service.main as main

    probe_started, release_probe = asyncio.Event(), asyncio.Event()
    saved, reassigned = [], []

    class FakeClient:
        def __init__(self, *, target, timeout):
            pass

        async def models(self):
            probe_started.set()
            await release_probe.wait()
            return ["cloud-model"]

        async def aclose(self):
            pass

    monkeypatch.setattr(main, "OMLXClient", FakeClient)
    monkeypatch.setattr(main, "set_cloud_provider", lambda *a, **k: saved.append(a))
    monkeypatch.setattr(main, "set_role", lambda *a: reassigned.append(a))
    monkeypatch.setattr(main, "_sync_keep_warm", lambda: None)
    monkeypatch.setattr(main, "role_to_model", lambda role: "managed")
    monkeypatch.setattr(main, "models_config", lambda: {"roles": {config_role: "managed"}})

    async def exercise():
        pending = asyncio.create_task(main.connect_cloud_inference({
            "provider": "openai-compatible", "base_url": "https://example.com",
            "api_prefix": "/v1", "model_id": "cloud-model", "context_window": 8192,
            "credential_name": "Wisp", "roles": pending_roles}))
        await asyncio.wait_for(probe_started.wait(), 1)
        await main.config({"role": config_role, "model": "managed"})
        assert not main._local_provider_operation_lock.locked()
        release_probe.set()
        with pytest.raises(main.HTTPException) as error:
            await pending
        return error.value

    error = asyncio.run(asyncio.wait_for(exercise(), 30))
    assert error.status_code == 409
    assert saved == [] and reassigned == [(config_role, "managed")]


def test_disconnect_and_cloud_disconnect_still_supersede_a_pending_connect(h, monkeypatch):
    engine = h.engine()
    monkeypatch.setattr(h.main, "disable_cloud_provider", lambda: None)

    async def scenario():
        h.gate.hold = {0}
        pending = asyncio.create_task(h.connect(engine, ["agent"]))
        await h.gate.started(0)
        assert (await h.call("DELETE", "/inference/cloud")).status_code == 200
        h.gate.release(0)
        return await pending

    assert run(scenario()).status_code == 409
