"""Ideate and the engine: where it is, whether Inkling is there and loaded, and residency (D5).

Wisp keeps one chat model warm for Ask (main.py's keep-warm set; service/memory_saver.py
parks it when Wisp is idle; service/idle_unloader.py drops other idle models). Ideate fits
into that instead of fighting it:

- Before a run, if Inkling isn't loaded, the run's first event says so and Ideate calls the
  engine client's own swap helper, `OMLXClient.ensure_only(model, exclusive=True)`, the same
  call a Research job makes for its model: on Wisp's bundled engine it evicts the other
  models (Ask's included) and loads Inkling; on the user's own oMLX.app it unloads nothing
  and only loads.
- During the run Inkling is registered as in flight (`idle.begin`), so the idle unloader
  never drops it mid-run, and the run counts as a foreground turn (`idle.begin_foreground`),
  so background model work (mail gists, the Brief, Save memory's batch windows) stands
  down instead of reloading Ask's model beside it.
- After the run both are released (`idle.end`, `idle.end_foreground`). Nothing else is
  pinned or unloaded: the idle unloader lets Inkling go after Wisp's idle minutes, and
  Ask's model comes back on the next Ask turn (or Save memory's prewarm), as it always does.

A run is a child process talking to the engine directly, so these calls are made from the
runner's thread onto the service's own event loop (`bind_loop`), where the shared client
state (prewarm task, epochs) lives.
"""
from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from service.ideate import config
from service.ideate.harness import EngineTarget


class EngineUnavailable(RuntimeError):
    pass


@dataclass
class ModelState:
    reachable: bool
    listed: bool
    loaded: bool


def target() -> EngineTarget:
    """The engine Ideate's harness talks to: Wisp's configured oMLX, loopback only."""
    from service.inference.remote_policy import remote_only_profile
    if remote_only_profile() is not None:
        raise EngineUnavailable("Ideate needs Wisp's on-Mac engine. This Mac uses a cloud model.")
    from service.config import omlx_base_url
    parts = urlsplit(omlx_base_url())
    host = (parts.hostname or "").lower()
    if host == "localhost":
        host = "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parts.scheme != "http" or not loopback:
        raise EngineUnavailable("Ideate needs the engine on this Mac.")
    port = parts.port or 80
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return EngineTarget(base_url=f"http://{netloc}/v1", port=port)


def api_key() -> str:
    """The engine's key, for the harness's environment only (never written to a file)."""
    from service.config import omlx_api_key
    try:
        return omlx_api_key() or ""
    except Exception:  # noqa: BLE001 - an engine without a key
        return ""


class Residency:
    """The default engine hooks. Tests pass a stand-in with the same methods."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not loop:
            self._loop = loop
            self._client = None

    def _get_client(self):
        if self._client is None:
            from service.inference.omlx_client import OMLXClient
            self._client = OMLXClient()
        return self._client

    async def state(self) -> ModelState:
        try:
            rows = (await asyncio.wait_for(self._get_client().status(), 5))["models"]
        except Exception:  # noqa: BLE001 - engine down or not oMLX
            return ModelState(False, False, False)
        row = next((r for r in rows if isinstance(r, dict) and r.get("id") == config.MODEL_ID), None)
        return ModelState(True, row is not None, bool(row and row.get("loaded")))

    async def _ensure(self) -> None:
        await self._get_client().ensure_only(config.MODEL_ID, exclusive=True,
                                             settle_timeout=config.LOAD_TIMEOUT_S)

    def _on_loop(self, coro, timeout: float):
        if self._loop is None or self._loop.is_closed():
            coro.close()
            raise EngineUnavailable("Wisp's engine isn't ready yet.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # Called from the runner's thread.
    def state_sync(self) -> ModelState:
        return self._on_loop(self.state(), 15)

    def ensure_sync(self) -> None:
        self._on_loop(self._ensure(), config.LOAD_TIMEOUT_S + 15)

    def begin(self) -> None:
        from service import idle
        idle.begin(config.MODEL_ID)
        idle.begin_foreground()

    def end(self) -> None:
        from service import idle
        idle.end(config.MODEL_ID)
        idle.end_foreground()
