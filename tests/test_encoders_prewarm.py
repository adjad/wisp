"""Encoder pre-warm (service/encoders.py): a mic or picture press loads the tower
early on Wisp's own engine, never blocks or fails anything. A fake client; no
engine is contacted."""
import asyncio
from pathlib import Path

import pytest

from service import encoders as E
from service import idle
from service import memory_saver as M

MODEL = "gemma"


class Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class Engine:
    """The engine's HTTP side, seen through OMLXClient._client."""

    def __init__(self, answer=200, body=None):
        self.answer = answer
        self.body = body or {}
        self.posts = []
        self.during = []
        self.gate = None

    async def post(self, path, json=None, timeout=None):
        self.posts.append((path, json))
        self.during.append((idle.foreground_busy(), idle.in_flight(MODEL)))
        if self.gate is not None:
            await self.gate.wait()
        return Response(self.answer)

    async def get(self, path):
        return Response(self.answer, self.body)


class Client:
    managed = True

    def __init__(self, loaded=True, engine=None):
        from service.engine import BUNDLED_PORT
        self.base_url = f"http://127.0.0.1:{BUNDLED_PORT}"
        self.loaded = {MODEL} if loaded else set()
        self._client = engine or Engine()
        self.calls = []

    async def loaded_models(self):
        return sorted(self.loaded)

    async def load(self, model):
        self.calls.append(("load", model))
        self.loaded.add(model)

    def keep_warm(self):
        return {MODEL}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(E, "_tasks", {})
    monkeypatch.setattr(E, "_unsupported", False)
    monkeypatch.setattr(M, "_prewarm_task", None)
    monkeypatch.setattr(M, "pressure_level", lambda: M.NORMAL)
    monkeypatch.setattr(idle, "_foreground_turns", 0)
    monkeypatch.setattr(idle, "_in_flight", {})
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: True)
    import service.config
    monkeypatch.setattr(service.config, "role_to_model", lambda role: MODEL)
    from service.inference import remote_policy
    monkeypatch.setattr(remote_policy, "remote_only_profile", lambda: None)


def run(coro):
    return asyncio.run(coro)


async def settle(modality):
    await E._tasks[modality]


def test_mic_press_posts_one_prewarm_holding_the_foreground_and_in_flight():
    client = Client()

    async def go():
        assert await E.prewarm("audio", target=(client, MODEL)) == "loading"
        await settle("audio")

    run(go())
    assert client._client.posts == [("/v1/wisp/encoders/audio/prewarm", {"model": MODEL})]
    # Background work stands down and Save memory can't park the model mid-load ...
    assert client._client.during == [(True, 1)]
    # ... and both are given back afterwards.
    assert not idle.foreground_busy() and idle.in_flight(MODEL) == 0


def test_presses_while_loading_share_one_request():
    engine = Engine()
    client = Client(engine=engine)

    async def go():
        engine.gate = asyncio.Event()
        first = await E.prewarm("vision", target=(client, MODEL))
        await asyncio.sleep(0)
        second = await E.prewarm("vision", target=(client, MODEL))
        engine.gate.set()
        await settle("vision")
        return first, second

    assert run(go()) == ("loading", "loading")
    assert len(engine.posts) == 1


def test_a_parked_model_is_brought_back_before_the_tower():
    client = Client(loaded=False)

    async def go():
        await E.prewarm("audio", target=(client, MODEL))
        await settle("audio")

    run(go())
    assert client.calls == [("load", MODEL)]
    assert client._client.posts == [("/v1/wisp/encoders/audio/prewarm", {"model": MODEL})]


def test_a_model_that_stays_unloaded_is_left_to_the_request(monkeypatch):
    client = Client(loaded=False)
    monkeypatch.setattr(M, "pressure_level", lambda: M.CRITICAL)     # Save memory won't load it

    async def go():
        await E.prewarm("audio", target=(client, MODEL))
        await settle("audio")

    run(go())
    assert client.calls == [] and client._client.posts == []


def test_an_engine_without_the_route_is_asked_once():
    client = Client(engine=Engine(answer=404))

    async def go():
        await E.prewarm("audio", target=(client, MODEL))
        await settle("audio")
        return await E.prewarm("vision", target=(client, MODEL))

    assert run(go()) == "unsupported"
    assert len(client._client.posts) == 1


def test_engine_errors_are_swallowed():
    class Broken(Engine):
        async def post(self, *args, **kwargs):
            raise OSError("connection refused")

    client = Client(engine=Broken())

    async def go():
        await E.prewarm("vision", target=(client, MODEL))
        await settle("vision")

    run(go())
    assert not idle.foreground_busy() and idle.in_flight(MODEL) == 0


def test_the_users_omlx_and_remote_profiles_are_never_asked(monkeypatch):
    monkeypatch.setattr(E, "_target", lambda: None)
    assert run(E.prewarm("audio")) == "not_bundled"
    assert E._tasks == {}
    with pytest.raises(ValueError):
        run(E.prewarm("smell"))


def test_target_is_none_off_the_bundled_engine(monkeypatch):
    from service.engine import mode
    monkeypatch.setattr(mode, "is_bundled", lambda: False)
    assert E._target() is None


def test_display_keeps_typed_fields_only():
    row = {"supported": True, "reason": None, "idle_release_s": 300.0,
           "towers": {"audio": {"state": "resident", "bytes": 611_000_000, "path": "/secret"},
                      "vision": {"state": "weird", "bytes": "big"}}}
    shown = E.display(row)
    assert shown == {"on_demand": True, "reason": None, "idle_release_s": 300.0,
                     "resident_bytes": 611_000_000,
                     "towers": {"audio": {"state": "resident", "bytes": 611_000_000},
                                "vision": {"state": "unknown", "bytes": None}}}
    off = E.display({"supported": False, "reason": "unsupported_model", "towers": {}})
    assert off["on_demand"] is False and off["reason"] == "unsupported_model"


def test_status_reads_the_ask_model_row(monkeypatch):
    body = {"models": {MODEL: {"supported": True, "idle_release_s": 300,
                               "towers": {"audio": {"state": "released", "bytes": 5}}}}}
    client = Client(engine=Engine(body=body))
    monkeypatch.setattr(E, "_target", lambda: (client, MODEL))
    shown = run(E.status())
    assert shown["on_demand"] is True and shown["towers"]["audio"]["state"] == "released"
    monkeypatch.setattr(E, "_target", lambda: None)
    assert run(E.status()) is None


def test_the_endpoint_is_for_the_app_only(monkeypatch):
    from fastapi.testclient import TestClient
    import service.main as main
    seen = []

    async def fake(modality, **kwargs):
        seen.append(modality)
        return "loading"

    monkeypatch.setattr(E, "prewarm", fake)
    client = TestClient(main.app)
    assert client.post("/encoders/prewarm", json={"modality": "audio"}).json() == {"status": "loading"}
    assert client.post("/encoders/prewarm", json={"modality": "nose"}).status_code == 422
    assert client.post("/encoders/prewarm", json={"modality": "vision"},
                       headers={"Origin": "https://example.com"}).status_code == 403
    assert seen == ["audio"]


def test_only_the_app_endpoint_prewarms():
    """A text-only turn never loads an encoder: nothing on the turn path asks."""
    root = Path(__file__).resolve().parents[1] / "service"
    callers = sorted(str(p.relative_to(root)) for p in root.rglob("*.py")
                     if "encoders.prewarm(" in p.read_text())
    assert callers == ["main.py"]
