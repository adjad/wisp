"""What the setup surfaces say when the built-in engine cannot start or a download fails.

ASGI only: no sockets, engine, model, network or user state. service.main is replaced by a
stand-in module so its ensure_omlx can be made to fail in each way.
"""
import asyncio
import sys
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI

from service.engine import downloads
from service.inference.inference_errors import ModelLoadError

SECRET = "/Users/someone/private/path token=synthetic-secret"


def _start(monkeypatch, failure):
    async def ensure_omlx():
        raise failure
    fake_main = ModuleType("service.main")
    fake_main.ensure_omlx = ensure_omlx
    monkeypatch.setitem(sys.modules, "service.main", fake_main)
    monkeypatch.setattr(downloads.mode, "is_bundled", lambda: True)
    app = FastAPI()
    app.include_router(downloads.router, prefix="/engine")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8765") as client:
            return await client.post("/engine/start", json={})
    return asyncio.run(run())


def test_busy_engine_port_says_another_program_holds_it(monkeypatch):
    response = _start(monkeypatch, ModelLoadError(
        "The bundled AI engine could not start: Port 8791 is already in use by another process"))
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Another program is using the built-in engine's port (8791)" in detail
    assert "Try again in a moment" not in detail


def test_missing_engine_says_it_is_missing_from_this_copy(monkeypatch):
    response = _start(monkeypatch, ModelLoadError(
        "The bundled AI engine could not start: The bundled engine is not installed"))
    assert response.status_code == 503
    assert response.json()["detail"] == "Wisp's built-in engine is missing from this copy."


@pytest.mark.parametrize("failure", [
    ModelLoadError("The bundled AI engine did not become ready " + SECRET),
    RuntimeError("engine crashed on import " + SECRET),
])
def test_other_start_failures_point_at_the_engine_log_without_leaking_text(monkeypatch, failure):
    response = _start(monkeypatch, failure)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith("Wisp's built-in engine didn't start. Try again in a moment.")
    assert "~/.moe/engine/engine.log" in detail
    assert "synthetic-secret" not in response.text and "/Users/someone" not in response.text

